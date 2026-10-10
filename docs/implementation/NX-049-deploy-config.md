# NX-049：ADR-024 选项 1 的部署落地

分支 `nx049-deploy-config`，BASE main `9016021`。

负责人在 2026-10-10 选择了 ADR-024 的选项 1：**代码默认值保持 1，部署配置设为 4**。本次只改部署配置、doctor、runtime_worker 相关代码和文档，没有迁移。

## 1. 改动

| 文件 | 改动 |
|---|---|
| `deploy/stage/connection-budget.v1.json`（新增） | 声明 APP_HOST 上每个会连接 PG 的服务进程，以及它们的 pool max 和进程数。Runtime Worker 一项的取值是 `guard_workers=4`、`dispatcher_pool_max=2`、`guard_pool_max=4`；当前合计 38 |
| `deploy/stage/compose.runtime-worker.example.yaml`（新增） | 先运行一次性的 `runtime-worker-budget`（`nexloop-doctor --budget-only`），成功后才启动 `runtime-worker`（`--guard-workers 4 --dispatcher-pool-max 2`，`NEX_EIOS_DB_POOL_MAX=4`，`stop_grace_period: 40s`）。主机、端口、路径、镜像和密钥都来自私有 env 文件或私有目录 |
| `deploy/stage/systemd/nexloop-runtime-worker.service.example`（新增） | 预算检查放在 `ExecStartPre`，取值与 Compose 示例相同；`KillMode=mixed`，`TimeoutStopSec=40` |
| `deploy/stage/README.md`（新增） | 进程形态与连接公式；前提“生产 Agent Host 全局并发 ≤ 4”（目前是部署前提，Host 代码不强制）；预算检查的用法；回退方式（改回 1 并重启）；持有机密的进程数 |
| `nexloop_eios/connection_budget.py`（新增） | 预算计算与校验。上限写死在代码里，部署文件不能放宽：合计 ≤ 60，`max_connections` ≥ 80。Runtime Worker 一项：N=1 时为 `guard_pool_max`，N>1 时为 `dispatcher_pool_max + N × guard_pool_max`。字段、取值范围、重名、未知字段都严格校验 |
| `nexloop_eios/doctor.py` | 新增 `--connection-budget FILE` 与 `--budget-only`（启动前检查：只做预算校验和 `show max_connections`，不需要 `--artifact-root`）。任一项不满足，或文件错误、连不上数据库时，退出码 1 |
| `nexloop_eios/assembly.py`、`backend.py` | `open_core` / `open_backend` 新增 `pool_max_size` 参数（默认 4，取值 1..32） |
| `nexloop_eios/runtime_worker.py` | 新增 `--dispatcher-pool-max`（默认 2，取值 1..32），只在 N>1 时作为父进程的连接池上限。guard 所用 Backend 的连接池取 `NEX_EIOS_DB_POOL_MAX`（默认 4，取值 2..32，非法值启动失败）：N=1 时就是唯一的 Backend，N>1 时传给每个子进程 |
| `nexloop_eios/runtime_guard_worker.py` | 子进程新增 `--pool-max`；`GuardWorkerPool(pool_max=…)` |

N=1、不设环境变量时，行为与之前完全相同：只有一个 Backend，连接池 4，准入容量 2。

## 2. 发现：`NEX_EIOS_DB_POOL_MAX` 之前没有生效

`open_core` 原来构造 `StorageSettings(database_url=…)` 时用的是默认值 `pool_max_size=4`。读取 `NEX_EIOS_DB_POOL_MAX` 的是 `StorageSettings.from_env()`，NexLoop 的路径不调用它。

因此：
- 部署主机上 **pool8 那组对比（5/6）实际仍是 4 个连接**，它与 base（4/6）的差别属于正常波动，不能用来评价方案 E。
- 设计稿、原型文档和 ADR-024 中“由 `NEX_EIOS_DB_POOL_MAX` 控制连接池”的说法，在这之前都不成立。前两份文档已加更正说明；ADR-024 不在本分支，留待合并时一并更正。
- 现在 Runtime Worker 会显式传入这个值。其他服务（API、Domain Worker 等）仍是固定的 4，这正是 `connection-budget.v1.json` 中的取值；它们是否也改为可配置，另行决定。

## 3. 测试

`tests/test_connection_budget.py`（23 项）：
- 声明的 stage 预算在政策范围内（合计 38，Runtime Worker 18）；N=1 时只计 guard 连接池。
- `max_connections` 下限：80 和 100 通过，79 不通过。
- 合计超过 60（`guard_workers=10`，合计 62）不通过。
- 11 种非法文档被拒绝：schema 不对、服务为空、重名、进程数为 0、类型不对、未知字段、名字不合规、`guard_pool_max=1`、`guard_workers=17`、缺字段、两种形态混用。
- doctor CLI 连真实的一次性 PG：
  - 预算通过时退出码 0，输出中不含 DSN；
  - 合计超限时退出码 1；
  - 文件损坏时退出码 1；
  - 数据库不可达时退出码 1；
  - `--budget-only` 不带预算文件时退出码 2。
- 示例与预算一致：Compose 和 systemd 的 `--guard-workers`、`--dispatcher-pool-max`、`NEX_EIOS_DB_POOL_MAX` 与声明一致；启动前检查存在；`KillMode=mixed`，40 s。
- 示例中没有真实地址：除回环地址外不得出现 IPv4，也不得出现 `.lan`、`.local` 之类的主机名。
- Runtime Worker 参数：
  - 默认 N=1 时连接池为 4（不变）；
  - N=4 时父进程 2、子进程 4；
  - 环境变量设为 6 时生效；
  - 非法的环境变量值（1、33、x、空）和非法的 `--dispatcher-pool-max`（0、33）都会让启动失败。

`tests/test_runtime_worker.py::test_backend_pool_size_is_explicit`：用真实 PG 验证 `open_backend(pool_max_size=3)` 得到 `max_size=3`、准入容量 1；`pool_max_size=0` 被拒绝。

回归（本机，`-n 4`）：
- **209 passed，348 s**，一次通过。范围：上面两组测试，`test_runtime_worker`（含多进程的 7 项）、`test_runtime_guard_transport`、`test_runtime_host_admission`、`test_backend_lifecycle_capacity`、`test_vendor_imports`、`test_wheel_install`、v4[complete]、两 Pi，以及所有引用 doctor 的测试。
- `NEXLOOP_TEST_GUARD_WORKERS=4` 下，v4[complete] 和两 Pi：2 passed，68.9 s。
