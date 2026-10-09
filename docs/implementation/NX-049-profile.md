# NX-049：部署主机时限剖析方案（只测量）

目的：在部署主机（CI 测试机）上看清受 2 s 时限约束的调用把时间花在哪里。只新增/修改 `scripts/perf/` 与本文；产品代码不变，测量钩子只在测量进程树内生效。

## 1. 采集什么

| 层 | 来源 | 记录 |
|---|---|---|
| Host（Node） | `scripts/perf/hooks/host_timeline.mjs`，由测量钩子在 `scripts/agent_host.py` 进程内给 Node 追加 `--import`（agent_host 只放行白名单环境变量，钩子在 `os.execve` 处补 `NODE_OPTIONS` 与输出目录） | Host 发出的每个 guard 请求（authorize、effects/submit、effects/find）：开始墙钟、socket、TCP 连接、TLS 握手完成、请求发出、响应头、结束或错误（含超时）；Host 处理的每个 runs/start、runs/inspect；进程启动到开始监听的时间 |
| Python 测试进程 | `hooks/sitecustomize.py`（已有，增加时间线模式） | 每个 Backend 请求（`invoke:authorize_runtime_activation`、`invoke:runtime_effect_tool`、`invoke:create_runtime_activation`、`invoke:prepare_message_context`、relay、dispatcher）的墙钟与分层耗时（授权判定、事实加载、每个 SQL 函数、连接池等待、请求锁、签名）；guard HTTP 处理（从请求到响应、线程）；服务端/客户端 TLS 握手；测试或调度器到 Host 的 HTTP 请求；拉起 Host 的时刻 |
| PostgreSQL | `hooks/perf_plugin.py`（已有，增加时间线模式） | `track_functions` 的函数自身耗时；每 20 ms 采样活动后端的等待事件，以及锁等待（等待语句、已等待毫秒、阻塞者 pid 与语句）；服务器日志中 ≥200 ms 的语句与锁等待（`log_lock_waits`，`deadlock_timeout` 保持默认，锁行为不变；**不记录绑定参数**，`log_timezone=UTC`） |

所有时间都是墙钟，测试进程、Host 与 PostgreSQL 的记录可以对齐。只记录路径、状态码、函数名与耗时，不记录请求头、请求体、签名、令牌或参数。

## 2. 调度员在测试机上怎么执行

前提（测试机已具备）：Node 24 在 PATH、`uv`、PostgreSQL 18、CI 运行目录的 `src`（本分支检出）与其中已有的 `.venv`；Agent Host 已构建（未构建时：`pnpm install --frozen-lockfile && pnpm build:pi && pnpm --filter @nexloop/agent-host build`）。

```bash
sudo -iu nexloop-ci
cd <CI 运行目录>/src
git fetch && git checkout <nx049-profile-l3 的提交>
export NEXLOOP_TEST_PG_BIN=<PostgreSQL 18 的 bin 目录>
scripts/perf/nx049_profile.sh deploy-idle 5
```

- 用例：`test_relationship_context_v4.py::test_real_human_message_v4_bound_artifact[complete]` 与 `test_role_pi_effect_checkpoint.py::test_actual_two_pi_role_runs_one_shared_intent_one_real_loopback_effect`，串行、每个跑 N 轮（默认 3），每次一个一次性 PostgreSQL（`tests/conftest.py`）。
- 脚本先检查 Node 24、`NEXLOOP_TEST_PG_BIN/initdb`、已构建的 Host；优先用仓库 `.venv`，否则 `uv run --frozen`。locale 自动选 `en_US.UTF-8`，没有则 `C.UTF-8`。
- 输出到 `scripts/perf/out/<label>/`（已被 git 忽略）。请发回 `summary.json`、`summary.md`、`host.txt`、`walls.jsonl` 和 `pytest-*.log`；`proc-*.json`、`host-*.jsonl`、`pg-*.json`、`pglog-*.log` 体积较大，需要深挖时再发。
- 失败用例不会中断脚本；`rc` 记录在 `walls.jsonl` 与汇总表中。

## 3. 怎么读输出

`summary.md` 的总表每行一次运行：用例、轮次、rc、墙钟、Host 看到的 guard authorize p50/p95/max、effect submit p50/max、**第一次超过 2 s 的调用**（路径、Host 端总耗时、错误）。

之后每次运行一节 JSON：有超时就是“first over limit”，否则是“slowest guard request”。字段含义：

- `host_request.phases_ms`：Host 端从发出起的累计时刻。`tls_handshake_done_ms` 远大于几毫秒，说明 TLS 或连接建立慢；`response_headers_ms` 约等于总耗时，说明时间花在 guard 内部；有 `timeout_ms` 或 `error`（`AbortError`、`ECONNRESET`），说明 Host 在 2 s 时放弃了。
- `guard_server.total_ms`：Python guard 从读到请求到写完响应的时间。`queued_before_handler_ms` 是 Host 发出到 guard 开始处理的间隔，大则说明线程或连接排队。
- `python_request.breakdown_ms`：该请求内各层的自身耗时（毫秒）。`authz:resolve_facts`、`authz:decide_resolved` 是 Python 侧授权判定；`sql:<函数>` 是该 SQL 调用的往返时间（含服务端执行与锁等待）；`pool:getconn` 是连接池等待；`lock:backend_request_lock` 是后端请求锁等待；`sign:hmac`、`serialize:*` 是签名与序列化。`calls` 是各项调用次数。
- `pg_lock_waits`：该请求时间窗内采样到的锁等待，含等待语句、已等待毫秒和阻塞者。
- `pg_activity_samples_20ms`：时间窗内应用后端的等待事件直方图。`CPU:-` 多表示服务端在算；`Lock:*`、`LWLock:*`、`IO:*` 分别对应锁、内部轻量锁、IO。
- `pg_slow_statements`：时间窗内 ≥200 ms 的语句（只有函数名）。

运行级字段：
- `host_startup`：Node 启动到监听，以及 Python 拉起到监听。
- `tls_handshakes`：Python 侧所有握手的分布。
- `dispatcher_to_host`：测试或调度器到 Host 的 start/inspect 请求。`runtime_transport_unavailable` 通常对应这里的超时或连接错误。
- `pg_top_functions_self_ms`：整次运行中 SQL 函数自身耗时的前几名。
- `lock_wait_samples`：整次运行的锁等待采样数。

判读顺序：
1. 先看第一次超 2 s 时 Host 的 `phases_ms`，判断问题在连接/TLS，还是在 guard 内部。
2. 如果在 guard 内部，比较 `queued_before_handler_ms` 与 `guard_server.total_ms`。
3. 再看 `breakdown_ms` 的前几项，分清是 Python 授权、某个 SQL 函数，还是连接池或锁。
4. 最后用 `pg_lock_waits`、`pg_activity_samples_20ms` 区分服务端计算、锁等待和 IO。

测量本身有开销（Python 包装与 20 ms 采样），所以只在同一套脚本跑出的 Mac 与测试机数据之间做比较。

## 4. Mac 对照数据（本机，3 轮，负载约 2.8–3.6）

`scripts/perf/nx049_profile.sh mac-baseline 3`：6/6 通过。Apple M 系 12 核，Node v24.13.0，PostgreSQL 18.4，Python 3.12.10。

| 用例 | 墙钟 s | guard authorize p50/p95/max ms | effect submit p50/max ms | 最慢 guard 请求 |
|---|---|---|---|---|
| v4[complete] | 43.1–43.6 | 627–643 / 734–784 / 898–948 | 959–1012 / 1096–1310 | submit 1096–1310 ms |
| 两 Pi | 49.5–50.1 | 818–824 / 941–1020 / 1059–1082 | 1126–1133 / 1137–1143 | submit 1137–1143 ms |

最慢那次请求的分解（3 轮均值，毫秒）：
- **v4[complete]**：`sql:nexloop_runtime_activation_command` 428，`authz:resolve_facts` 284，`authz:decide_resolved` 87，`sql:nexloop_effect_intent_command` 83，`sql:nexloop_load_authority_fact_snapshot` 67，`sql:nexloop_effect_catalog_hint` 43。
- **两 Pi**：`authz:resolve_facts` 413，`sql:nexloop_runtime_activation_command` 185，`sql:nexloop_effect_intent_command` 106，`authz:batch_resolve` 91，`authz:decide_resolved` 90，`sql:nexloop_load_authority_facts` 62。
- **共同项**：Host 端 TLS 握手完成约 2–4 ms；guard 排队约 2–6 ms；guard 处理时间约等于 Host 看到的总耗时。
- **启动与锁**：Host 启动约 120 ms（拉起到监听约 230 ms）；每次运行锁等待采样 30–44 次，最慢请求时间窗内为 `Lock:advisory` / `Lock:transactionid` 各若干次。
- **值得注意**：测试到 Host 的 `runs/inspect` 在 Mac 上单次就有 1.3–1.7 s。inspect 本身要经过 guard 授权，并与模型、工具调用共用 Host 适配器的串行队列。部署主机上的 `runtime_transport_unavailable` 建议先看 `dispatcher_to_host` 是否是这些请求超时。

## 5. 文件

- `scripts/perf/nx049_profile.sh`：执行入口。
- `scripts/perf/nx049_report.py`：汇总，输出 `summary.json` / `summary.md`。
- `scripts/perf/hooks/host_timeline.mjs`：Node 预加载。
- `scripts/perf/hooks/sitecustomize.py`、`scripts/perf/hooks/perf_plugin.py`：已有钩子，增加 `NEXLOOP_PERF_TIMELINE=1` 时间线模式；不开时行为不变。

## 6. 报告 v2 新增（nx049-report-v2）

- **按请求的 PG 函数耗时**：在时间线模式下，每条调用 authz/control/runtime/ontology 函数的语句执行完后，测量钩子在同一连接上读取一次本后端的 `pg_stat_xact_user_functions`，与该连接上一次读数作差（后端尚未上报的计数会跨事务累积；读数回落视为已上报，从零重新算），然后把差值记到所属请求上。
  - `summary.json` 中，每次运行的 `pg_functions_per_request` 给出受时限约束的请求平均每次的函数 calls / total_ms / self_ms（按 total 取前 15）。
  - 超时或最慢那次请求的 `python_request.pg_functions_by_total`、`pg_functions_by_self` 给出该请求自己的函数分解。
  - 探针本身的耗时记在 `measurement_probe_ms`，读数时从请求总时间中扣除。Mac 上约占最慢请求的 5–10%（50–105 ms）。
  - `NEXLOOP_PERF_PGFUNC=0` 可以关闭探针，只保留其余时间线。
- **O3 事实解析缓存**：每次运行的 `fact_parse_cache` 汇总该运行内各测试进程的 hits / misses / hit_rate；`summary.md` 总表新增“O3 hit rate”一列。Mac 对照：v4 0.982（29573/543），两 Pi 0.978（33279/756）。
- 对照数据：`mac-v2`（2 轮，4/4 通过）。带探针时 authorize p50 约 683–694 / 848–856 ms，不带探针时（v1）为 627–643 / 818–824 ms。

建议的 JIT A/B 两组都带探针（倍率可比）。如果带探针后第一次超时提前、影响判读，再加一组 `NEXLOOP_PERF_PGFUNC=0` 的对照。
