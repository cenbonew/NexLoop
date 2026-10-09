# NX-049 部署主机时限分析（第一轮剖析）

依据：调度员在部署主机上运行 `scripts/perf/nx049_profile.sh deploy-idle 5`（main `a824777` + `cb36234`，合并提交 `27e5d18`，主机空闲，load 0.2–0.9）的汇总；Mac 对照为同一脚本的 `mac-baseline 3`（见 `NX-049-profile.md`）。原始日志不入库，下面只放精简表。

主机：
- 部署主机：x86_64 ThinkPad（16 线程），Ubuntu，Linux 7.0，PostgreSQL 18.6（Ubuntu 包），Node v24.13.0，Python 3.12.10。
- Mac：Apple M 系 12 核，PostgreSQL 18.4（Homebrew），版本相同的 Node 与 Python。

> **第二轮更正**（`NX-049-analysis-round2.md`）：§1.3 中“个别层被放大 4–35 倍”不是这些层本身变慢，而是同一 guard 进程内 2–3 个请求重叠时等待 Python GIL 的时间，被记到了等待发生时所在的那一层。单独请求在部署主机上各层是均匀的约 2.5 倍。JIT（手段 1）、CPU 调频（手段 2）、O3 缓存（手段 3）均已排除。

## 0. 结论先行

1. **瓶颈是计算，不是网络或排队。**
   - Host 发出到 TLS 握手完成约 3.4–5.7 ms（Mac 1.7–4.3 ms）；guard 开始处理前的排队约 6–10 ms（Mac 2–6 ms）。
   - 时间几乎全部在 guard 内部的 Python 授权判定和 SQL 往返上。PostgreSQL 活动采样以 `CPU` 为主，锁等待很少：每次运行 0–12 次 `Lock:advisory`，Mac 上反而更多（30–44 次）。
2. **两边做的工作量完全一样，只是每单位工作更慢。** 对比 Mac 上处于同一位置的请求，所有计数都相同：
   - v4 第 9 次 authorize：`authz:load_fact` 552 次、`decide_resolved` 69 次、`resolve_facts` 46 次、事实快照 SQL 121 次、连接借出 37 次。
   - 两 Pi 第 5 次 authorize：对应计数为 780 / 68 / 65 / 46 / 25。

   请求内的授权判定没有重复（Mac 上每次 authorize 平均 55.9 次判定，去重后仍是 55.9 次），所以问题不在重复计算。
3. **超时的那次请求在 Mac 上的对应请求：两 Pi 0.96 s、v4 0.61 s；在部署主机上是 2.1–2.4 s，倍率 2.4–3.4。** 但各层放大并不均匀：
   - Python 授权计算约放大 1.4–2.3 倍，与两台机器单线程性能差距相当；
   - 少数几项被放大 4–35 倍（见 §1），这是要优先解释的部分。
4. 只靠“部署主机约慢 2 倍”这一项，同一类请求也会落在 1.2–1.9 s，两 Pi 的 p95 仍贴近 2 s。要稳定做到 p95 < 1.5 s，需要两步：
   - 先消除被异常放大的部分（大概率是环境：PG JIT、CPU 调频，以及部署主机上 O3 解析缓存是否生效）；
   - 再从每个请求的固定开销里削掉约 0.3 s（连接、事务和逐事实加载的合并），不降低检查强度。

## 1. 时间去向与倍率

### 1.1 每次运行（Host 端看到的 guard authorize，毫秒）

| 用例 | 部署主机 p50 / max（5 轮） | Mac p50 / max（3 轮） | p50 倍率 | 部署主机第一次超 2 s 的位置 |
|---|---|---|---|---|
| v4[complete] | 1114–1137 / 2001–2006 | 621–643 / 893–948 | ≈1.8 | 第 9 次 authorize（5/5，ABORT_ERR，约 2001 ms） |
| 两 Pi | 1290–1777 / 2001–2006 | 814–824 / 1051–1082 | ≈1.6–2.2 | 第 5 次 authorize（5/5，ABORT_ERR） |

两个用例的失败都发生在**第一次进入“模型/工具调用期 authorize”**的时候。Mac 上从这一次起，单次 authorize 从约 0.4 s 跳到 0.6–0.9 s（v4，第 7 次起）或从 0.5 s 跳到 0.8–1.0 s（两 Pi，第 4 次起）。原因是这一阶段每次都要额外构造并在 SQL 中复核角色、目录、正式对象以及（v4 的）关系信封。

### 1.2 超时那次请求的分层（Python 侧自身耗时，毫秒）

部署主机的分解取自能和 guard 线程对上的那次请求（v4 第 1 轮、两 Pi 第 2 轮）；Mac 取同一位置请求 3 轮的均值。

| 层 | v4 部署 | v4 Mac | 倍率 | 两 Pi 部署 | 两 Pi Mac | 倍率 |
|---|---|---|---|---|---|---|
| 整个请求 | 2105 | 614 | **3.4** | 2288 | 964 | **2.4** |
| `sql:nexloop_runtime_activation_command` | 577 | 122 | **4.7** | 224 | 179 | 1.25 |
| `authz:resolve_facts`（Python 判定前的事实组装） | 554 | 240 | 2.3 | 880 | 397 | 2.2 |
| `authz:batch_resolve`（O2b 批量路径的 Python 部分） | 352 | 10 | **≈35** | 651 | 104 | **6.3** |
| `sql:nexloop_load_authority_fact_snapshot` | 149 | 71 | 2.1 | 50 | 12 | 4.2 |
| `authz:decide_resolved`（决策计算） | 106 | 74 | 1.4 | 105 | 68 | 1.5 |
| `sql:select current_user, session_user, rolsuper…`（应用角色核验） | 60 | 9 | **≈7** | 55 | 22 | 2.5 |
| `sql:nexloop_service_identity_snapshot` | 60 | 13 | **4.6** | 44 | 30 | 1.5 |
| `sql:nexloop_read_assessment_object`（v4 关系读取） | 65 | 18 | 3.6 | — | — | — |
| `sql:nexloop_load_authority_facts`（O2b 批量 SQL） | 42 | 16 | 2.6 | 88 | 103 | 0.85 |

其他：
- Host 启动（Node 开始到监听）261 ms，Mac 120 ms，×2.2。
- 服务端 TLS 握手 p50 2.2 ms，Mac 0.9 ms。
- 调度器与测试到 Host 的请求在部署主机上未出现超时：失败发生在 Host 发往 guard 的请求，即 ABORT_ERR，没有到 `runtime_transport_unavailable` 那一步。

### 1.3 放大最多的几项

1. **`authz:batch_resolve` 的 Python 自身耗时（×6–35）。** 这是 O2b 批量路径里 SQL 以外的部分：读取结果、按 O3 缓存解析事实、组装 unit of work。批量 SQL 本身（`load_authority_facts`）并没有变慢（×0.85–2.6），所以放大来自 Python 侧处理批量结果，最可疑的是 **O3 事实解析缓存在部署主机上未命中或未生效**。部署主机的汇总里没有缓存命中计数，需要原始 `proc-*.json` 中的 `fact_parse_cache`。
2. **v4 的运行激活 SQL（×4.7），而两 Pi 只有 ×1.25。** v4 的激活链在 SQL 内重算关系快照与正式对象现时性，查询更大、带 jsonb 处理。这类更贵的语句在部署主机上被不成比例地放大，最符合 **PostgreSQL JIT** 的特征：Ubuntu 的 PG 18 包带 LLVM，`jit=on` 是默认值，而 Mac 的 Homebrew PG 编译时没有 LLVM，根本不会走 JIT。成本高的语句一旦超过 `jit_above_cost`，每次执行都要付编译开销。
3. **很小的语句也被放大（角色核验 ×2.5–7，身份快照 ×1.5–4.6）。** 单条只有零点几毫秒，部署主机上却是 1–2 ms。这符合**笔记本 CPU 调频**（powersave / 平衡模式下，短促突发的负载来不及升频）。由于每个请求有 25–37 次连接借出，每次都要做角色核验、设定隔离级别、取时间，这类固定开销被放大后累计到 100–200 ms。

## 2. 候选手段（不放宽 2 s，不降低授权检查强度；按 收益 / 风险 / 改动面 排序）

| # | 手段 | 预期收益（部署主机，每请求） | 风险 | 改动面 |
|---|---|---|---|---|
| 1 | **确认并关闭 PostgreSQL JIT**：对应用角色设置 `jit=off`（`ALTER ROLE … SET jit=off`），或在部署实例的 postgresql.conf 中关闭；测试集群同样关闭以保持一致 | 高：解释 v4 激活 SQL 的 ×4.7 及部分快照类 SQL 放大，估计 300–450 ms | 低：OLTP 小事务不需要 JIT；授权逻辑与检查不变 | 部署配置或 bootstrap 中一条角色设置；测试 conftest 一个启动参数 |
| 2 | **确认并固定部署主机的 CPU 性能模式**（`performance` 调频或电源配置；生产主机同样记录） | 中–高：小语句与 Python 计算都会受益，估计 15–30% | 低：只是运维设置 | 运维，不改代码；部署说明中写明要求 |
| 3 | **核对 O3 事实解析缓存在部署主机上是否命中**（`batch_resolve` 自身 ×6–35）；若因环境（进程模型、键差异等）失效，修复 | 中：300–550 ms | 低：缓存只缓存解析结果，不缓存判定（O3 原设计） | 先只做诊断；需要修复时改 `authorization.py`（L1 的 O3 范围） |
| 4 | **单请求合并连接与事务**：一次 authorize 只借一个连接、只做一次应用角色核验和隔离级别设置，各项判定仍在本请求内取当前数据（不跨请求复用任何判定，属于 O5b 级别 1 范围） | 中：25–37 次借出降到 1–3 次，估计 100–200 ms | 中：事务边界变化，需论证“每项检查仍读当前状态”且锁顺序不变（参考 0085 的同 Run 锁序） | `runtime_activation.py` + `authorization.py`，需和 L1、L4 协调 |
| 5 | **逐事实快照加载并入 O2b 批量路径**：每请求 46–121 次 `load_authority_fact_snapshot` 改为批量 | 中：约 50–150 ms，`resolve_facts` 自身也会下降 | 中：与 O2b 同类改造，事实快照语义不变 | `authorization.py`（L1） |
| 6 | **激活 SQL 内的实现效率**：同一事务中对同一对象、同一证明的重复 READ 断言与身份快照合并（事务内去重，不跨事务），减少激活链里的重复复核 | 中：v4 激活 SQL 的主要部分 | 中–高：涉及授权 SQL 包装层，必须证明每项检查在本事务内仍然执行且对象未变 | SQL 迁移（L1 的授权包装层，需协调） |
| 7 | **（需要 ADR，并由负责人确认）跨请求复用授权判定（O5b 级别 2）** | 高 | 高：改变“每次调用读取当前权限”的语义；已裁定未采用 | 只有 1–6 都做完仍达不到 p95 < 1.5 s 时才提 |

预计效果：
- 1–3 都是环境或诊断项，代价最小。如果它们把异常放大的部分消除，部署主机会接近“Mac × 单线程性能比（约 2 倍）”，即 v4 约 1.2 s、两 Pi 约 1.9 s；两 Pi 仍不够。
- 再加上 4、5，每请求约削掉 0.2–0.35 s，两 Pi 降到约 1.5–1.7 s，p95 仍偏紧；这时 6 才有必要。
- 7 不在建议范围内，只作为最后的决策项。

不建议做的：
- 拆分或延长时限；
- 去掉 Host 在提交前后各一次的授权：这不影响单个请求的时延，而且是防 TOCTOU 的设计；
- 减少角色、目录、正式对象或关系信封的复核。

## 3. 下一轮在部署主机上的验证

脚本不变（`scripts/perf/nx049_profile.sh`），只换环境变量或主机设置。**同一主机上交替执行**，每组 3 轮。

**手段 1、2、3 不改代码，下一轮一起做：**

1. **先采集环境事实**，附在结果里发回：
   ```bash
   "$NEXLOOP_TEST_PG_BIN/pg_config" --configure | tr ' ' '\n' | grep -i llvm
   cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
   powerprofilesctl get 2>/dev/null
   .venv/bin/python -c "import psycopg;print(psycopg.pq.__impl__)"
   ```
2. **JIT 的 A/B**（libpq 会读取 `PGOPTIONS`，测试集群的所有连接都会生效，无需改代码）：
   ```bash
   scripts/perf/nx049_profile.sh deploy-jit-on 3
   PGOPTIONS='-c jit=off' scripts/perf/nx049_profile.sh deploy-jit-off 3
   ```
   判读：对比 `sql:nexloop_runtime_activation_command`、`load_authority_fact_snapshot`、`service_identity_snapshot` 的倍率和失败轮数。v4 激活 SQL 如果降到约 2 倍 Mac（250 ms 左右），即可确认 JIT 是主因。
3. **CPU 性能模式的 A/B**（需要主机管理员）：切换到 performance 后执行 `scripts/perf/nx049_profile.sh deploy-perf 3`，再切回。判读：看角色核验那条 SQL 与身份快照的单次耗时是否回到 Mac 的 1–2 倍。
4. **O3 缓存**：从上面任意一组的输出中额外发回 `proc-*.json`。它含 `fact_parse_cache` 的 hits / misses，以及按请求的完整分层。判读：如果 misses 接近或等于 hits + misses，说明缓存未生效，转入手段 3 的修复。

**手段 4–6 要改代码**：在各自分支上实现后，用同一脚本做 `deploy-before` / `deploy-after` 交替各 3 轮。判读：
- 每请求的 `pool:getconn`、角色核验次数（4），或 `load_authority_fact_snapshot` 次数（5）应明显下降，其余计数不变；
- 第一次超 2 s 的位置后移或消失；
- authorize p95 < 1.5 s，10/10 通过才算达标。

手段 6 还需要把汇总中 PG 函数的 `total_ms`（目前只汇总了 self）一并输出。这是 `nx049_report.py` 的一个小改动，可以在下一轮之前补上，`pg-*.json` 里已经有这些数据。

## 4. 本轮数据的局限

- 部署主机只发回了汇总。分层只来自每轮第一次超时、且能与 guard 线程对上的那次请求，10 轮中有 2 轮对上了；其余轮次只有 Host 端时间。原因是 guard 处理在 Host 放弃后才结束，记录晚于 Host，导致部分轮次没有配对。分层所用的两轮在 Host 端的表现与其他轮一致（约 2001 ms，ABORT_ERR）。
- 测量钩子本身有开销，两台机器都一样，所以倍率可比，绝对值偏高。
- 部署主机的 PG 函数统计只有整次运行的累计 self 时间，其中还混着 relay、组装和夹具配置（例如 `configure_manifest`），无法归到超时的那一次请求上；也就分不清 SQL 往返里多少是函数体执行，多少是计划生成、JIT 编译和语句级开销。下一轮需要按请求时间窗统计（见 §3 对 `nx049_report.py` 的补充），以及手段 1 的 A/B。
