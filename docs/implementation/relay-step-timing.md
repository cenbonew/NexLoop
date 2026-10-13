# relay 按步骤计时与多进程（AT-060 后续）

依据：调度员 2026-10-13（AT-060 C4-low 重跑 `20261013T061107Z-e5e7ebd` 的分析：relay 段 p50 3.47 s，分布集中，时间花在处理上而不是轮询），第 1 项与第 6 项。第 2–5 项（凭据重认证、合并事务、复查范围、复用 Goal/PlanStep）等负责人对 2 s 目标的决定，本分支不做。

## 1. relay 按步骤计时（迁移 0188）

- **步骤**：与 `message_relay.py` `MessageRelay._run_once` 的实际步骤一一对应：
  - `backlog` 积压检查，`claim`（claim / claim_next），`recover`（已有路由的恢复），`lookup`；
  - `goal_create`、`step_create`、`enterprise_step_create`、`assignment_create`（受治理对象创建）；
  - `vault_prepare`、`source_proofs`，`issue`（签发 Run 凭据，含写 vault），`effect_bind`（效应上下文绑定）；
  - `context_prepare`（v2 或 v6 上下文构建），`message_bind`、`own_route`，`route_authorize`（Run 证明与路由授权）；
  - `runtime_accept`（任务入队），`ack`、`release`；
  - `handoff`（交给回复保障）。
  
  另有 `total`（已入队来信从 claim 到 release）。`authenticate` 是 CLI 每个 tick 的凭据重认证（`message_relay_cli.py`），每个 tick 都记。
- **记录方式**：每条被认领的来信，每一步记一次耗时，只有数字，不含原文和 ID。空闲 tick 什么都不记（只计 `authenticate`）。失败的尝试记已经走过的步骤，不记 `total`。计时只是插入时间点，不改变任何调用和顺序。
- **采样**：每个 relay 进程每 60 s 为每个步骤写一条样本，kind 为 `relay_step`，service 为步骤名。字段与 `guard_op` 相同（`calls`、`p50_ms`、`p95_ms`、`max_ms`，以及对数直方图 `h_<i>`）。迁移 0188 把 kind `relay_step` 加入样本表，并只接受上面列出的步骤名和数字键，函数体与 0186 相同。
- **报告**：每格新增"relay 各步骤"表：每一步的次数，以及各 relay 进程直方图合并后的 p50/p95（与 `guard_p95_ms` 同一种合并，误差 ≤ ±5%）。只作信息，不参与判定。
- **测试**：
  - `tests/test_relay_step_timing.py`：提交语义、空闲不记、SQL 只收数字和已知步骤；
  - `tests/test_load_chain_pg.py::test_the_relay_records_each_step_of_a_queued_message`：真实 relay 入队一条来信，各步骤都有记录，相加等于 total；
  - `tests/test_load_report.py::test_relay_steps_table_per_cell`。

## 2. relay 多进程

- **负载栈**：
  - `stack.start_message_chain(..., relay_processes=1)` 可设 1–3，默认 1。每个进程用自己的 vault 和 artifact 目录：一个 vault 只能有一个属主进程，`relay.owner` 是排他锁。
  - `matrix.py --relay-processes`、`run_on_lane.sh` 的 `LOAD_RELAY_PROCESSES`（默认 1）。
  - `cell.json` 记录 `relay_processes`。
- **不会重复路由**（0141 `claim_next`）：
  - 同一消费者同一时刻只交给一个 relay：有消费者级 advisory 锁，并检查该消费者是否还有未过期租约的在途来信；
  - 每条来信在自己的 advisory 锁、`for update` 和租约 fence 下认领；
  - 路由表以 message_id 为主键，一条来信最多一条路由。
  
  测试 `tests/test_load_chain_pg.py::test_two_relays_route_every_message_exactly_once`：两个 relay 各用自己的 vault 和 backend 同时运行。证明了：
  - 一个 relay 持有的消费者，另一个 relay 会跳过；
  - 全部来信各只有一条路由、一个 Run、一个任务；
  - 两个 relay 都处理了来信。
- **崩溃后的行为（多副本时会变）**：单个 relay 重启时，从自己的 vault 恢复在途来信。多副本时，如果一个副本崩溃、其他副本在租约（30 s）过后接手同一条来信，接手的副本 vault 里没有这条来信的凭据，会按现有规则把它交给回复保障（handoff），而不是重新签发凭据或重复发送。这是现有语义下的降级，不会重复。
- **部署**：`deploy/stage/compose.app.relay-replicas.example.yaml` 是可选覆盖文件：`message-relay-2` 继承 `message-relay`，只把 vault 换成 `data/relay-vault-2`；第 3 个副本照此添加。默认不启用。部署门禁按 `nexloop.budget-service` 标签核对容器数与连接预算，所以只有在预算声明了相同的 relay 进程数时才能通过（有测试）。

## 3. 连接预算（上限 60）

当前（main 2bbeff6，含 L3）合计 **59/60**，用 `nexloop_eios.connection_budget.evaluate` 计算。relay 每个进程连接池 2，加一个副本就是 **61**，超出。可选方案如下，均在 60 以内，但**都需要先测量再改预算文件**，本分支没有改：

| 方案 | relay | 腾出来源 | 合计 | 前提 |
|---|---|---|---|---|
| A | 2 进程 × 池 1 | 不挪 | 59 | NX-031 D7 实测 relay 池 1 会超时或失败；需要重测，并确认采样线程不需要第二个连接 |
| A′ | 3 进程 × 池 1 | 不挪 | 60 | 同 A，且没有任何余量 |
| **B（推荐，用于 2 个副本）** | 2 进程 × 池 2 | `api-browser-identity` 每进程 4 → 3（2 个 API 进程共腾出 2） | 59 | 需要在登录高峰（C4-peak 的工作台与客户登录）下复测 ACK 和登录延迟；`--identity-pool-max 3` |
| C（3 个副本） | 3 进程 × 池 2 | `api-browser-identity` 4 → 2（腾出 4） | 59 | 同 B，压力更大 |

API 主池（2×4）和 guard 池（4×4）不在考虑范围：按 NX-031 D7，降低它们需要夜间 ACK 数据并重跑 ADR-022 时限门槛。

**建议**：CI 主机先用负载栈比较 relay 1 和 2（负载车道不经过部署预算），确认多进程的收益；有收益再按方案 B 测登录高峰，然后改预算与 Compose。

## 4. v0.1 门槛与 v0.2 计划（负责人 2026-10-13 选 A，ROADMAP §7）

- **门槛变化**：门槛项 `idle_dispatch_p95_ms`（来信受理 → Run 任务被 claim，只在 C4-low 判定）的门槛由 2000 ms 改为 **5000 ms**（`scripts/load/report.py` 的 `THRESHOLDS`）。
- **依据**：
  - AT-060 C4-low v6 重跑（`20261013T061107Z-e5e7ebd`）的空闲派发 p95 为 6762.5 ms。几乎全部时间在 relay 段：p50 3466 ms，p95 6630 ms，分布集中，下限约 3.07 s。排队段 p95 只有 295 ms。
  - 这段时间是每条来信串行的受治理步骤：三次受治理对象创建、凭据签发、效应绑定、v6 上下文构建、路由授权等，每一步都要授权或签名。不是轮询造成的。
  - 在不改变语义和复查范围的前提下（本文第 1、2 节：计时、多进程），单条下限降不到 2 s。所以 v0.1 以 5 s 作门槛，PRD 的 2 s 留作 v0.2 目标。
- **跟踪 v0.2 目标**：新增观察项 `idle_dispatch_v02_target`，只记录 p95 是否 ≤2000 ms，在报告的"观察项"表中显示"达到 / 未达到"，不参与判定。测试：4999 通过，5001 失败，2001 只让观察项显示未达到。
- **v0.2 计划**：用 ADR 改 relay 结构。依据第 1 节的步骤计时，从调度员分析中的第 3–5 项选择，这几项都会改变语义或复查范围，需要 ADR 并经负责人确认：
  - 同一来信的串行往返合并成一个事务，或让互不依赖的步骤并行；
  - 参照 ADR-027/028 缩小 relay 路径上的复查范围；
  - 每个顾客复用 Goal/PlanStep。

  第 2 项（空闲 tick 不重复认证）需负责人确认凭据吊销的生效时间。ADR 落地后重跑 C4-low，以 `idle_dispatch_v02_target` 达到为准。
