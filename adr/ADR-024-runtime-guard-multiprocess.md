# ADR-024｜Runtime guard 多进程

状态：**提议**（待负责人决策；未采用前，代码默认值与部署形态都不变）。

关联：
- docs/handoff/docs/11_DEPLOYMENT.md §6（连接预算：应用连接池总和不超过 60）、§7（Agent Host 全局并发 4）；
- deploy/examples/postgresql.conf.template（`max_connections = 80`，其中 20 预留给运维和故障核对）；
- NX-049：
  - 设计稿 `docs/implementation/NX-049-multiprocess-guard.md`；
  - 原型 `docs/implementation/NX-049-mp-proto.md`（设计稿与原型随 integration-s4b `5a416ef` 合入，本分支的 BASE main `a9f4afb` 中还没有这两份文件）；
  - 剖析 `NX-049-analysis-round2.md`、`NX-049-resolve.md`；
  - L4 的 `NX-049-roundtrips.md`、`NX-049-run-lock-order.md`。

## 1. 背景

Agent Host 每次调用模型或工具前，都要经过 Runtime Worker 的 loopback guard 授权（`authorize`、`effects/submit`、`effects/find`）。Host 对每个请求设 2 s 时限，不自动重试。

现在的 guard 运行在 Runtime Worker **同一个 Python 进程**内（`ThreadingHTTPServer`），与调度器共用一个 `Backend`：
- 连接池 `NEX_EIOS_DB_POOL_MAX` 默认 4；
- `LifecycleLock` 的准入容量为 `pool_max // 2` = 2。

同一 Run 的并行工具调用、多个 Role 同时运行，会让 2–4 个 guard 请求重叠。它们的 Python 部分在 GIL 下串行执行。

NX-049 的剖析结论：

1. **单个请求不慢，重叠才慢。** 部署主机（8a+8b 之后，扣除测量探针）：
   - 单独的 authorize 0.99–1.10 s；
   - 重叠的 authorize 中位数 1.30–1.53 s，最慢 1.9 s；
   - effect submit 每次都处在重叠中，2.0–2.25 s，是剩下的超时项。
2. **已经做过的优化：**
   - 已排除的环境因素：JIT、CPU 调频、O3 缓存失效；
   - Python 侧：8a/8b/8c，`resolve_facts` 降了 27–33%（部署主机）；
   - SQL 往返：手段 5 + 4a；
   - 同一 Run 的锁顺序修复。
   
   这些都做完后，单进程模型下仍不能在全量负载下稳定通过（§3）。
3. 调大连接池（方案 E）只能去掉准入排队，GIL 串行仍在，单独作用有限（§3）。

## 2. 决定（提议）

1. **进程模型。** Runtime Worker 的 guard 改为“父进程 + N 个 pre-fork guard 子进程”：
   - 父进程创建并绑定 loopback 监听套接字，子进程通过继承的文件描述符在同一个端口上 `accept`。不使用 `SO_REUSEPORT`：同一用户的其他进程也能绑定同一端口，分走连接。
   - 调度器留在父进程。每个子进程打开自己的 `Backend`，启动时核对 SQL 角色，先认证一次，然后才开始接收连接。
   - 请求处理器和全部校验与单进程时相同（`create_runtime_guard_server`，只新增一个可选参数 `listen_socket`）。
   - Host 契约不变：guard URL、2 s 时限、不重试、各状态码的语义都不变。
2. **开关。** 新增 `nexloop-runtime-worker --guard-workers N`（1..16）。**N=1 时与现状逐项等价**，走原来的代码路径。
3. **默认值。**
   - **代码默认值保持 1**：开发机、最小安装和单元测试仍是单进程。
   - **部署配置（版本化的 Compose 或 systemd 配置）设为 4**：作用于 APP_HOST 上的 Runtime Worker，以及测试机全量 CI 中的运行时端到端用例（通过 `NEXLOOP_TEST_GUARD_WORKERS=4`）。
   - 理由见 §4。负责人也可以选择把代码默认值一并改成 4；这样所有环境都是多进程，但开发机的资源和连接数也会同步增加。
4. **监管与停机**（原型已实现）：
   - 子进程意外退出时立即替换；60 s 内重启超过 4 次，或替代进程起不来时，父进程停止 claim，以退出码 1 退出，交给外层服务管理器。
   - 停机时，父进程停止 claim，向子进程转发 SIGTERM。子进程停止接收新连接，等已接受的请求全部应答完（最多 15 s），关闭 `Backend`（`LifecycleLock` 等在途的 commit/fsync）后退出。父进程最多等 20 s，超时就强制结束子进程，并以失败退出。
   - 父进程被 SIGKILL 时，子进程从管道 EOF 得知，自行排空退出。
5. **不变的约束。**
   - guard 只监听 loopback，传输密钥认证 Host；
   - 每个请求都重新认证，并读取当前权限；
   - 只有受治理的 Action 能修改正式业务对象，持久化先于 ACK；
   - 结果未知时先查询或对账，不盲目重发；
   - 不跨请求复用判定（O5b 级别 2 仍未采用）；
   - 业务串行化全部在 PostgreSQL 中（advisory 锁、行锁、租约和 fence），不依赖进程内锁。

## 3. 实测依据

**部署主机**（x86_64，16 线程；原型 `b3da5f6`；不带钩子；v4[complete] 与两 Pi 各 3 次，合计 6 次）：

| 配置 | 通过 | 两 Pi 墙钟 |
|---|---|---|
| base（N=1，连接池 4） | 4/6 | 约 75 s |
| pool8（N=1，`NEX_EIOS_DB_POOL_MAX=8`） | 5/6 | 约 75 s |
| **mp4（N=4，每个子进程连接池 4）** | **6/6** | **64–65 s** |

**部署主机剖析**（8a+8b，单进程，扣除探针；用来估算多进程的收益，见设计稿 §6）：
- authorize 重叠请求的中位数从 1.30–1.53 s 降到约 1.05–1.2 s；
- effect submit 从 2.0–2.25 s 降到约 1.5–1.7 s。

effect submit 里服务端激活 SQL 的 0.45–0.86 s（3 次调用）不随进程模型变化。

**本机**（Mac，负载 10–13，只作参考）：
- N=4 时，重叠请求的 Python 部分降了约 26–30%，authorize p50 降了 15–25%；
- 一个 guard 子进程在运行中的 RSS：中位约 90 MB，最大约 102 MB（两个用例，在 N=4 下采样 173 次）。

**原型测试**：
- N=1：260 项回归一次通过；
- N=4：78 项通过；
- 新增测试覆盖：子进程中途被杀（对 Host 等同于超时，子进程被替换）、停机排空（在途请求得到完整应答，端口释放）、反复崩溃（父进程以退出码 1 退出，不再 claim）。

## 4. 为什么取 N=4

- **并发量**：部署主机上同一 Worker 同时在处理的受时限 guard 请求，实测最多 4 个。Agent Host 的全局并发也是 4（11_DEPLOYMENT §7）。N=4 时，在实测峰值下每个请求都能有自己的进程，GIL 串行基本消失。
- **准入容量**：每个子进程的准入容量仍是 `pool_max // 2` = 2，合计 8，是实测峰值的 2 倍，不会再出现单进程时每轮 250–490 ms 的准入排队。
- **实测**：N=4 在部署主机上 6/6 通过，两 Pi 墙钟减少约 14%。
- **N 再大的边际收益**：在实测并发下，N>4 的收益很小，却会线性增加连接数、内存和冷缓存，所以不取更大的值。
- **N=2**：在 3–4 个请求重叠时仍有两个请求共用一把 GIL。没有单独实测，不推荐作为默认值。

## 5. PG 连接预算与 `max_connections`

规则：应用连接池总和不超过 60，`max_connections = 80`，其中 20 预留给运维和故障核对（11_DEPLOYMENT §6、postgresql.conf 模板）。

Runtime Worker 占用的连接：

| 形态 | 计算 | 连接数 |
|---|---|---|
| N=1（现状） | 1 个 `Backend`（调度器与 guard 共用）× 4 | 4 |
| N=4，父进程连接池与子进程相同 | 父进程 4 + 4 × 4 | 20 |
| N=4，父进程连接池单独设为 2（建议，见下） | 父进程 2 + 4 × 4 | 18 |

按 11_DEPLOYMENT §7 的初始进程数（API 2、Domain Worker 1、Action Worker 1、Scheduler 1，每个连接池 4）做示意核算，其他服务合计约 20 个连接：
- N=1 时总计约 24；
- N=4 时总计约 38–40。

都在 60 以内，余量约 20。

要求：
- 部署 doctor（D0/D1）按“每个服务进程的 pool max × 进程数”逐项求和，确认 ≤ 60，并且 `max_connections` ≥ 80。不满足时启动前就失败，不在运行中无界等待。
- 建议给父进程单独提供连接池上限（例如 `--dispatcher-pool-max 2`），因为父进程只跑调度器。原型目前让父子进程共用 `NEX_EIOS_DB_POOL_MAX`，这一项作为采用后的跟进。
- 实际的服务进程数以部署时的 inventory 为准，上表只是示意，不是确认的事实。

## 6. 部署机资源核算（APP_HOST）

| 项 | N=1 | N=4 | 依据 |
|---|---|---|---|
| Runtime Worker 进程数 | 1 | 5（父进程 1 + 子进程 4） | — |
| 内存增量 | — | 约 +0.4–0.6 GB | 子进程 RSS 本机实测约 90–102 MB；Linux 上以 inventory 实测为准 |
| CPU | guard 的 Python 部分实际只用 1 个核 | 峰值最多约 4 个核 | 部署主机有 16 线程，与 PG 后端、Node Host、Pi 共享 |
| PG 连接 | 4 | 18–20 | §5 |
| 文件描述符 | 1 个监听 | 1 个监听（父进程持有，子进程继承） | 端口数不变 |

- 内存 limits 的写法遵循 11_DEPLOYMENT §7：不承诺所有服务同时吃满，不能让 OOM killer 优先杀运行中的服务。
- 部署前用 inventory 只读核对可用内存（例如 `free -m`），以及 PG 的 `max_connections` / `reserved_connections`（`show max_connections;`）。不猜测任何数值。
- 服务管理器的设置：
  - systemd 用 `KillMode=mixed`（SIGTERM 只发给主进程，由它转发并等待；超时后统一 SIGKILL），`TimeoutStopSec` ≥ 40 s（子进程排空 15 s，父进程等待 20 s）；
  - Compose 用 `stop_grace_period: 40s`。

## 7. 持有机密的进程数

| 机密（都只以私有文件路径传递，mode 0600，不经环境变量或命令行） | N=1 | N=4 |
|---|---|---|
| DSN（受限 Worker 角色）、服务凭据、签名密钥 | 1 | 5 |
| guard 传输密钥、guard TLS 私钥 | 1 | 5 |
| Host 控制密钥、Host CA | 1 | 1（子进程不需要） |

- 所有进程都在同一个服务用户下运行，读取同一组文件，没有新增文件，也没有新增权限。
- 子进程的 stdout 只输出固定的 ready 行，stderr 被丢弃，退出码只是固定语义；不输出凭据、DSN 或 Run 内容。
- 风险的变化是：内存中持有机密的进程从 1 个变成 5 个，任意一个进程的内存被同用户读取，后果与现在相同。负责人需要明确接受这一点。

## 8. 后果与约束

- **锁竞争**：同一 Run 的请求从“GIL 下交替执行”变成“真正并行”，PG 中的锁等待和死锁窗口会变大。L4 的锁顺序修复（`NX-049-run-lock-order.md`）已是前提，验收时要统计 40P01。
- **进程内缓存**：O3、8a、8b、8c 各进程一份，正确性不受影响（内容寻址、弱引用、请求内不复用判定）；代价是冷启动未命中和内存各乘以 N。
- **Artifact 写入**：`LocalBlobStore` 的目录 `flock` 会出现跨进程竞争。单次写入是毫秒级，预计影响很小，验收时观察。
- **新的失败模式**：子进程在请求中途崩溃。对 Host 来说等同于超时：authorize 按拒绝处理，effect 走 `find` 对账；不引入新的语义。
- **观测**：剖析钩子按 pid 输出。报告的 `concurrency.processes` 和 `overlapped_any_process` 用来区分进程内重叠和跨进程重叠。

## 9. 回退

- 部署配置改回 `--guard-workers 1`（测试改回 `NEXLOOP_TEST_GUARD_WORKERS=1`），重启 Runtime Worker 即可。
- 没有 schema、数据、契约或 Host 的变化，也不需要迁移。
- 回退后的行为就是 N=1 的现状。原型的回归已证明两者逐项等价。

## 10. 验收线

采用前必须全部满足：

1. **部署主机全量 CI 在 N=4 下连续两轮通过**：全量并行负载，运行时端到端用例使用 `NEXLOOP_TEST_GUARD_WORKERS=4`。
2. **N=1 等价**：原型的 N=1 回归集合在采用时的 HEAD 上全部通过。
3. **语义回归**：在 N=4 下全部通过，包括停机排空、子进程中途被杀、反复崩溃、kill/reopen、锁等待后最终期限、effect intent 对账。
4. **记录但不作为门槛**：两轮全量中 guard 请求的 p95、超时次数、40P01 次数、子进程重启次数、峰值 PG 连接数与内存。

## 11. 否决的方案

- **按 Run 分片**：要改 Host 契约，而且同一 Run 内的重叠（v4 只有一个 Run）分不开。
- **把判定计算放进进程池**：`ResolvedAuthorizationContext` 是进程内的不透明签发句柄，不能跨进程。
- **`SO_REUSEPORT`**：同一用户的其他进程能抢占端口。
- **只调大连接池（方案 E）**：实测 5/6，GIL 串行仍在；可以和 N=4 叠加，但不能替代它。
- **free-threaded CPython**：取决于 C 扩展生态是否就绪，作为长期选项另行评估。

## 12. 未决问题（需要负责人确认）

1. 是否采用；采用时，代码默认值保持 1、只在部署配置里设 4（本提议），还是代码默认值也改成 4。
2. 是否接受持有机密的进程从 1 个变为 5 个（§7）。
3. 父进程的独立连接池上限（§5）作为采用后的跟进项，是否需要在采用前完成。
