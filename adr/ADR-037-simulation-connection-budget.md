# ADR-037｜模拟进程组的连接预算（修订 ADR-024）

状态：**采用**。负责人 2026-10-22 决定，按推荐执行。

关联：

- ADR-024（Runtime guard 多进程；应用连接池总和不超过 60）；
- `docs/handoff/docs/11_DEPLOYMENT.md` §6（连接预算）；
- NX-041 S6（模拟进程组，`deploy/stage/compose.simulation.example.yaml`）；
- `deploy/stage/connection-budget.v1.json`、`nexloop_eios.connection_budget`、`nexloop-doctor --budget-only`、`nexloop_eios.deploy_checks.simulation_compose_budget`。

## 1. 背景

ADR-024 和 11_DEPLOYMENT §6 要求：APP_HOST 上每一个打开 PostgreSQL 连接池的服务进程都要登记进连接预算，`pool_max × 进程数` 的总和不超过 60；服务端 `max_connections` 至少 80。

NX-041 S6 加入的模拟进程组（compose 项目 `nexloop-simulation`）也在 APP_HOST 上，并且会连 PostgreSQL，但所有服务都标成了 `nexloop.budget-service: none`，没有被预算计入：

- sim-runtime-worker 的 dispatcher 连接池 2，加上 2 个 guard 子进程，每个按默认值 4，共 10；
- sim-stand-in-executor 和 sim-world-files 各用 1 个单连接。

按默认值合计 12。应用预算现在已经是 59，如果把这 12 个直接并进应用的 60，总数会到 71，超过上限。

## 2. 决定

1. **模拟进程组单独设一份连接预算，上限 10**（`SIMULATION_POOL_LIMIT`），**不计入**应用的 60。
2. **容量关系**：应用 60、模拟 10，再给管理、CI、备份和一次性运维工具（例如 sim-setup）留 7 个（`OPERATIONS_RESERVE`），三者之和不超过服务端 `max_connections` 减去 superuser 预留 3：60 + 10 + 7 = 77 = 80 − 3。
3. **模拟组的 guard 子进程连接池从 4 降到 2**：sim-runtime-worker 为 dispatcher 2 + 2 × 2 = 6，加上 stand-in、world-files 各 1，模拟组合计 **8**。
4. **单连接的服务也要登记**，每个按 1 计：它们实际占连接，只计连接池会少算。
5. **模拟组默认关闭**，只在验收时启动；验收结束后执行 `docker compose -p nexloop-simulation … --profile simulation down`。

## 3. 落地

- **预算文件**：`connection-budget.v1.json` 新增 `simulation` 段，登记 sim-runtime-worker（runtime 形态：guard_workers 2、dispatcher_pool_max 2、guard_pool_max 2）、sim-stand-in-executor 1、sim-world-files 1。模拟服务名必须以 `sim-` 开头，并且不能与应用服务重名。sim-agent-host 不连 PostgreSQL，标签保持 `none`；一次性的 sim-setup 和 sim-setup-dir 也标 `none`，它们用的是运维余量。
- **检查**：`connection_budget.evaluate` 和 doctor 分别检查以下几项：
  - 应用总数不超过 60；
  - 模拟总数不超过 10；
  - 给出服务端 `max_connections` 时，它至少为 80，并且应用总数加模拟总数加 7 不超过 `max_connections − 3`。
- **compose**：sim-runtime-worker 设置 `NEX_EIOS_DB_POOL_MAX=2` 和 `--dispatcher-pool-max 2`；三个连 PostgreSQL 的模拟服务都标上各自的登记项。部署门禁在给出模拟组 compose 时做双向核对（`simulation_compose_budget`），并核对 runtime worker 的三项连接池参数与预算一致。
- **验证**：沿用 0310 那条以真实模拟角色运行的测试（`tests/test_nx041_sim_pi_run_pg.py::test_at046_the_simulation_worker_dispatches_through_its_actual_path`），改成按预算里的值运行（dispatcher 2、2 个 guard 子进程各 2），sim Run 仍然能跑通。

## 4. 理由

- 模拟组默认关闭、只在验收时运行，把它的连接并进应用的 60，会挤占常驻服务的名额；单独设预算，既能让 doctor 看到它，又不影响应用。
- guard 连接池 4 是为 real 的并发准备的（ADR-024：`LifecycleLock` 准入 `pool_max // 2`）。模拟组只跑验收用的 Run，2 个子进程、每个池 2，可以同时处理 2 个 guard 请求，够用；上面那条测试证明一次完整的 sim Run 能在这个配置下跑通。
- 7 个运维余量，加上 3 个 superuser 预留，正好与 `max_connections = 80` 对上，不需要改服务端配置。
