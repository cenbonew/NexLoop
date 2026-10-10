# NX-049 备选设计：多进程 runtime guard（供负责人决策）

状态：**设计稿，未实现**。分支 `nx049-mp-design`，BASE main `35c790e`（已含 8a/8b/8c 和 L4 的手段 5/4a）。

定位：这是“Python 侧与 SQL 侧的优化都做完，全量负载下仍不能稳定低于 2 s”时才启用的结构性方案。采用前需要 ADR，并由负责人确认（§8）。

依据：
- 剖析数据见 `NX-049-analysis-round2.md` 与 `NX-049-resolve.md`。
- 部署主机数据为 `deploy-8ab-r{1,2,3}`（8a+8b，带 PG 函数探针，下文的耗时都已扣除探针时间）。


> **更正（nx049-deploy-config）**：在此之前，`open_backend` → `open_core` 构造 `StorageSettings(database_url=…)` 时用的是默认值 4，**并不读取 `NEX_EIOS_DB_POOL_MAX`**（只有 `StorageSettings.from_env()` 读它，NexLoop 的路径不调用）。因此本文中“由 `NEX_EIOS_DB_POOL_MAX` 控制连接池”的说法当时并不成立；部署主机上的 pool8 对比实际仍是 4 个连接。现在 Runtime Worker 会显式把这个值传给 guard 所用的 Backend，见 `NX-049-deploy-config.md`。

## 0. 结论先行

1. **瓶颈的结构**：所有 guard 请求都在**同一个 Python 进程**里，由线程处理。同一 Run 内的并行工具调用、多个 Role 同时运行，都会让 2–4 个请求重叠，Python 部分在 GIL 下串行。
   - 部署主机（8a+8b）扣除探针后：
     - 单独的 authorize 约 0.99–1.10 s；
     - 重叠的 authorize 中位数 1.30–1.53 s，最慢 1.9 s；
     - effect submit 每次都处在重叠中，2.0–2.25 s，正是现在超时的那一项。
2. **多进程能去掉“重叠放大”，但去不掉单个请求本身的耗时。** 估算（§6）：
   - authorize 重叠请求从 1.30–1.53 s 降到约 1.05–1.2 s；
   - effect submit 从 2.0–2.25 s 降到约 1.5–1.7 s，能回到 2 s 以内，但仍高于 p95 1.5 s 的目标。
   - effect submit 里服务端激活 SQL 约 0.45–0.86 s（3 次调用），这部分只能靠 SQL 侧削减。
3. **推荐形态是 pre-fork**：父进程持有监听套接字，N 个 guard 子进程通过继承的文件描述符在同一个端口上 `accept`。
   - 不推荐“按 Run 分片”：v4 只有一个 Run，它的重叠全部发生在**同一 Run 内**（并行工具调用），按 Run 分片会把这些请求留在同一进程里，收益最小（§3）。
4. **业务上的串行化都在 PostgreSQL 里**（advisory 锁、行锁、租约），不依赖进程内的锁。所以多进程不改变一致性语义。
   - 会变的是：容量预算（连接数 = 进程数 × 每进程连接池）、进程内缓存从一份变成 N 份、进程监管与崩溃语义、Artifact 写入的跨进程 flock 竞争（§4）。
5. **迁移不碰业务数据，回退只需把进程数改回 1**（§7）。
6. 在上 ADR 之前，建议先做一个小原型，用部署主机上的实测把 §6 的估算坐实（§7 第 1 步）。

## 1. 现在的进程/线程模型

### 1.1 部署形态（`nexloop-runtime-worker`，`nexloop_eios/runtime_worker.py`）

一个 Runtime Worker 进程，对应一个 world 和一个 queue：

| 组件 | 运行位置 | 说明 |
|---|---|---|
| `RuntimeDispatcher` 循环 | 主线程 | 每个 tick 重新认证并 claim 任务。Run 的租约和 fence 在 PostgreSQL 中 |
| guard HTTPS 服务 | 一个 `serve_forever` 线程，外加每个连接一个线程（`ThreadingHTTPServer`） | `runtime_control.create_runtime_guard_server`：只监听 loopback；连接槽 8，请求槽 8，超出直接 503；套接字超时 3 s |
| `Backend` | 进程内单例 | 一个 psycopg 连接池（`NEX_EIOS_DB_POOL_MAX`，默认 4）。`LifecycleLock` 的容量为 `max(1, pool_max // 2)`，默认 **2**：先进先出准入，等待 10 s 后抛 `BackendBusy`，guard 返回 503 |
| `_FreshGuard` | guard 线程内 | 每个请求重新读取服务凭据文件并认证，不缓存权限 |
| 进程内缓存 | 进程级 | O3 `FACT_PARSE_CACHE`（2048 项）、8a 的 `_VERIFIED`、8c 的 `_VIEWS`（都是弱引用）、8b 的 LRU（各 8192 项） |
| 请求级状态 | `contextvars` / 线程局部 | 4a 的请求连接作用域（每请求最多 2 个连接）、O2a 的请求 memo、`LifecycleLock` 的重入深度 |
| Artifact 存储 | 进程内的 `LocalBlobStore` | 写入时对目录加 `flock` 独占锁，本来就支持跨进程 |

Host（Node，`apps/agent-host`）与 Worker 是两个独立进程：
- 每个 guard 请求都新建一个 TLS 连接（`agent:false`），2 s 后由 `AbortSignal` 中止。
- **不自动重试**：authorize 失败一律按拒绝处理，该 Run 失败。
- effect submit / find 失败时，返回 `runtime_effect_unavailable`。intent id 稳定，`find` 是 replay-safe 的；遇到未知结果，先查询或对账，不盲目重发。

### 1.2 测试形态

`test_relationship_context_v4.py`、`test_role_pi_effect_checkpoint.py` 等用例使用 `tests/test_runtime_host_admission.guard_server`：
- 在 **pytest 进程内**起一个 guard 线程；
- 与测试代码、调度器和 relay 共用同一个 `Backend` 和同一把 GIL；
- Host 由 `scripts/agent_host.py` 作为子进程拉起。

所以测试形态和部署形态一样，都是“单进程、多线程”。区别只在于测试进程里还跑着测试代码本身，GIL 竞争更重一些。

### 1.3 实测的重叠深度

部署主机 8a+8b 的 6 次运行中：
- 同时在处理的受时限请求最多 **4 个**；
- 相互衔接的重叠组最长有 6 个（v4）和 11 个（两 Pi）请求；
- 每轮有 4 个请求在 `LifecycleLock` 的准入队列里等了 250–490 ms，原因是容量只有 2。

## 2. 可选的结构

| 方案 | 做法 | 主要问题 |
|---|---|---|
| **A. pre-fork guard 进程（推荐）** | 父进程（现在的 Worker）创建并绑定监听套接字，然后拉起 N 个 guard 子进程（`subprocess` 加 `pass_fds`，exec 一个新解释器，不用 `fork` 复制带线程的进程）。每个子进程在继承的套接字上运行现有的 `GuardServer`，各自打开 `Backend`。调度器仍在父进程中，Host 只看到一个 guard 端口 | 资源 ×N；需要进程监管（§4.7） |
| B. 按 Run 分片 | N 个 guard 端口，Host 按 `hash(run_id)` 选端口 | 要改 Host 契约。同一 Run 的并行工具调用仍在同一进程，几乎没收益（§3） |
| C. 只把判定计算放进进程池 | guard 线程把 resolve/decide 交给 `ProcessPoolExecutor` | `ResolvedAuthorizationContext` 是进程内的不透明签发句柄（`_IssuanceRegistry`），设计上不能跨进程；事实还要序列化往返。**不可行** |
| D. free-threaded CPython（3.13t / 3.14t） | 不改进程模型，去掉 GIL | 依赖 pydantic-core、psycopg 等 C 扩展的 free-threaded 支持和整体稳定性。属于长期选项，本稿不展开 |
| E. 只调大连接池（`NEX_EIOS_DB_POOL_MAX` 从 4 调到 8，容量从 2 变成 4） | 配置改动 | 只能去掉准入排队（每轮 4 个请求，各 250–490 ms），GIL 串行仍在。可以作为 A 之前的低成本步骤，先单独实测 |

**不使用 `SO_REUSEPORT`。** 同一用户的其他本地进程也能用 `SO_REUSEPORT` 绑定同一端口，分走连接。由父进程创建套接字、子进程继承文件描述符，可以保持“端口只属于这个 Worker”。macOS 上 `SO_REUSEPORT` 也不做负载均衡。

## 3. 为什么不按 Run 分片

- v4[complete] 用例只有一个 Run，所以它的重叠请求全部来自**同一个 Run** 的并行调用。两 Pi 有两个 Role Run，重叠既可能跨 Run，也可能在同一 Run 内。剖析事件里没有记录 run_id，两者各占多少尚未拆分；原型阶段可以在钩子中按 run_id 的摘要分组统计。
- 按 Run 分片后，同一 Run 的请求仍落在同一进程，GIL 串行照旧。
- 按连接分配（方案 A）时，内核在多个 `accept` 者之间分配连接，同一 Run 的并行请求会自然落到不同进程。

## 4. 对各项机制的影响

### 4.1 锁与一致性

- 业务串行化全部在 PostgreSQL 中：
  - 同一 Run 的 `nexloop-role-effect:<run>`、`nexloop-runtime-execution:<run>` advisory 锁；
  - Action 锁；
  - 行锁（`FOR SHARE` / `FOR UPDATE`）；
  - Run 的租约和 fence。

  这些在多个进程之间同样生效。仓库中没有依赖“只有一个进程”的进程内锁。
- **变化**：同一 Run 的请求从“GIL 下交替执行”变成“真正并行”，PG 中的锁竞争和死锁窗口会变大。L4 正在修的两 Pi 同 Run 死锁（40P01），在多进程下会**更容易**出现。所以它必须先修好，并在多进程原型下复测。
- 0098 的 `search_path` 规则和 SECURITY DEFINER 函数与进程模型无关，不受影响。

### 4.2 连接池容量（`pool_max // 2` 的假设）

- `LifecycleLock` 的容量公式按**每个进程**计算：每个请求最多占 2 个连接（4a 的 `SCOPED_CONNECTIONS = 2`，由 `pool_depth_plugin` 验证）。在每个子进程内这个假设不变，不需要改。
- 总连接数 = N × `pool_max`（guard 子进程）+ 父进程（调度器）的连接池 + 其他服务。
  - 例如 N=4、`pool_max`=4，guard 一共 16 个连接，可同时准入 8 个请求。
  - 部署主机 PG 的 `max_connections` 要按“所有 Worker × N × `pool_max`”加余量来核对（读取方式见 §5），并写进部署说明。
- 可以给父进程（只跑调度器）配更小的连接池，例如 2。

### 4.3 `LifecycleLock` 与停机

- 每个进程各有一把，“请求共享、关闭独占、关闭等待在途 commit/fsync”的语义在进程内保持不变。
- 停机流程：
  1. 父进程收到 SIGTERM，停止 claim，并向每个子进程转发 SIGTERM；
  2. 子进程停止 `accept`（关闭监听），`exclusive()` 等在途请求结束，然后关闭连接池；
  3. 父进程等所有子进程退出（有上限），再关闭自己的 `Backend`。
- 现有的 kill/reopen 系列用例要在多进程下再跑一遍（§7）。

### 4.4 进程内缓存（O3、8a、8b、8c）

- 每个进程一份，内容寻址、弱引用、请求内不复用判定，所以**正确性不受影响**。
- 代价：
  - 冷启动未命中 ×N：每个进程首次解析约 400–750 次，每次约 1 ms 量级；
  - 内存 ×N：每个进程最多 2048 个事实，另有 8192 项的 LRU。
- 命中率在每个进程的预热阶段会低一些。部署主机已经证明，稳定阶段的命中率是 100%。
- 8a 的信任边界不变：每个进程只信任自己用完整校验产生的实例，跨进程不共享任何登记。

### 4.5 请求级连接作用域（4a）与请求 memo（O2a）

都绑定在 `contextvars` 和线程局部上，作用范围是单个请求，在子进程内照常工作，不需要改。

### 4.6 Host 侧语义

- Host 不需要改：guard URL 不变，每个请求仍新建连接，仍是 2 s 时限、不自动重试。
- 新的失败模式是**子进程在请求中途崩溃**。Host 看到的是连接被重置，与超时一样处理：
  - authorize：拒绝，Run 失败，与现状相同；
  - effect submit：`runtime_effect_unavailable`。intent 在 ACK 之前已持久化，intent id 稳定，所以结果未知时由 `find` 或对账确认，不会盲目重发。这与现在的超时语义一致，没有新的语义。
- 准入不足（`BackendBusy`）仍返回 503，Host 当作拒绝。

### 4.7 进程监管

- 父进程监管子进程：
  - 子进程意外退出时，按退避重新拉起，并有上限；
  - 一段时间内反复崩溃时，父进程停止 claim 并以非零码退出，交给外层（systemd 或 Compose）处理，不进入无限循环。
- 子进程在启动时用 `verify_application_role` 和签名密钥校验，与现在的启动预检一致。子进程只有在**通过预检之后**才开始 `accept`。
- 机密材料（DSN、服务凭据、签名密钥、guard 密钥、TLS 私钥）仍然只通过私有文件路径传递，不经过环境变量或命令行，子进程自己读取。持有这些机密的进程数从 1 个变成 N+1 个，ADR 要写明这一点。

### 4.8 Artifact 写入

`LocalBlobStore` 用目录 `flock` 让写入者串行，本来就支持跨进程。v6 的 authorize 会写 context artifact，多进程时这把锁会出现跨进程竞争。单次写入时间短（毫秒级），预计影响不大，原型阶段要量一下 `flock` 的等待时间。

### 4.9 剖析与观测

- `scripts/perf/hooks` 已经按 pid 输出 `proc-<pid>.json`，报告按 pid 判定请求是否重叠。多进程后，“同一进程内重叠”的统计自然会下降，要新增“跨进程重叠”的统计。
- Host 的时间线不受影响。

## 5. 部署形态与资源

| 项 | 现在 | 多进程（N=4） |
|---|---|---|
| Worker 进程 | 1 | 1 个父进程（调度器）+ 4 个 guard 子进程 |
| 内存 | 1 × Python 进程 | 约 +4 × Python 进程。本机仅加载产品模块时 RSS 约 69 MB；加上连接池、缓存和运行中的数据，按每个子进程 120–200 MB 估算，原型阶段实测 |
| PG 连接（单个 Worker） | `pool_max` = 4 | 4 × 4 + 父进程 2 = 18 |
| 可同时准入的 guard 请求 | 2 | 8（覆盖已观测到的最大并发 4） |
| CPU | Python 部分实际只用 1 个核 | 最多 4 个核，加上 PG 后端。部署主机有 16 个线程 |
| 配置 | — | 新增 `--guard-workers N`（默认 1）。部署说明写明 N、`pool_max`，以及 PG `max_connections` 的核对方法 |

部署主机的 `max_connections` 和内存余量要用只读方式获取（例如 `show max_connections;`、`free -m`），不要猜测。

## 6. 在部署主机上的收益估算

方法：
- 多进程后，同时在处理的请求分布在不同进程里，GIL 串行带来的放大消失。
- 每个请求的耗时接近“单独请求”的耗时，再加上 PG 竞争带来的少量增长（按 5–10% 估算；PG 在这些窗口中以 CPU 为主，16 个线程有余量）。
- 数据来自 `deploy-8ab-r{1,2,3}`，均扣除测量探针。8c 的增益（每请求再少约 70–110 ms）没有计入。

| 请求 | 现在（单进程） | 多进程估算 | 说明 |
|---|---|---|---|
| authorize，单独 | 0.99–1.10 s | 0.99–1.10 s | 不变 |
| authorize，重叠（中位 / 最慢） | 1.30–1.53 / 1.9 s | 约 1.05–1.2 / 1.3 s | 去掉 GIL 放大和准入排队 |
| effect submit（总在重叠中） | 2.0–2.25 s | 约 1.5–1.7 s | 按 authorize 的重叠/单独比 1.3–1.4 折算；服务端激活 SQL 的 0.45–0.86 s 不变 |

判断：
- 多进程可以把现在的超时项（effect submit）拉回 2 s 以内，但余量只有 0.3–0.5 s。
- 要稳定满足 p95 < 1.5 s，仍需要削减 effect submit 的服务端 SQL：激活 SQL 内有 675 次读断言、约 5000 次事实加载、约 8500 次 `root_identity`（`NX-049-resolve.md` §5）。
- 两者叠加时，估计 effect submit 可以到 1.2–1.4 s。
- 以上是估算，需要原型实测确认（§7 第 1 步）。

## 7. 迁移路径与回退

1. **原型（不改默认行为）**：
   - `runtime_worker` 新增 `--guard-workers N`，默认 1，即现状；
   - 新增 guard 子进程入口（例如 `python -m nexloop_eios.runtime_guard_worker --listen-fd 3`），复用 `create_runtime_guard_server` 的处理器与校验；
   - 测试夹具 `guard_server` 增加一个子进程模式，在部署主机上用剖析脚本做 N=1 和 N=4 的交替对比，各 3 轮；
   - 剖析报告补上“跨进程重叠”的统计。
2. **语义回归**（多进程模式）：
   - `test_backend_lifecycle_capacity`、kill/reopen 系列、“锁等待后最终期限”系列、`test_runtime_guard_transport`；
   - O3、8a/8c 的等价测试，以及 effect intent 的未知结果与对账用例；
   - 新增：子进程在请求中途被杀死时，Host 侧表现为超时语义，intent 可以通过 `find` 对账；父进程停机时等所有子进程排空；子进程反复崩溃时父进程退出。
3. **部署**：
   - 先在部署主机（测试环境）以 N=4 运行全量 CI，确认全量并行负载下 v4 和两 Pi 稳定通过；
   - 核对 PG 连接数和内存；
   - 再写入部署说明。
4. **回退**：把 `--guard-workers` 改回 1，重启 Worker 即可。没有 schema、数据或契约变化，Host 不用改。

前置条件：L4 的同 Run 死锁修复要先合入（§4.1）；建议先单独实测方案 E（只调大连接池），确认准入排队的那部分收益。

## 8. ADR 要点

- **决策**：Runtime Worker 的 guard 由“单进程多线程”改为“父进程 + N 个 pre-fork guard 子进程，共享一个由父进程持有的监听套接字”，默认 N=1。
- **背景**：GIL 下同一进程内重叠请求串行，是在 2 s 工具时限下失败的主要结构性原因（剖析数据见本文与 `NX-049-analysis-round2.md`）。
- **不变的约束**：
  - guard 只监听 loopback，传输密钥认证 Host；
  - 每个请求都重新认证，并读取当前权限；
  - 只有受治理的 Action 能改正式业务对象，持久化先于 ACK；
  - 结果未知时先查询或对账；
  - 不跨请求复用判定（O5b 级别 2 仍未采用）；
  - Host 契约不变。
- **会变的事项**：
  - 连接预算（N × `pool_max`）与 PG `max_connections` 的要求；
  - 持有机密的进程数从 1 变为 N+1；
  - 进程内缓存从一份变成 N 份；
  - 新增子进程崩溃这一失败模式（语义与超时相同）；
  - 进程监管与停机顺序；
  - Artifact `flock` 的跨进程竞争。
- **否决的方案**：按 Run 分片（同一 Run 内的重叠无法分开）、把判定放进进程池（签发句柄不能跨进程）、`SO_REUSEPORT`（同用户进程可以抢占端口）。free-threaded CPython 作为长期选项另行评估。
- **验收**：
  - 部署主机全量 CI 并行负载下，v4[complete] 与两 Pi 连续 N 轮通过；
  - guard 请求 p95 < 1.5 s（或负责人另定的阈值）；
  - 第 7 节列出的语义回归全部通过；
  - N=1 与现状逐项等价。
- **回退**：设 N=1 即可；无数据迁移。
