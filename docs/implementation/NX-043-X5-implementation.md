# NX-043 X5：停止规则与预算阻断

- **依据**：NX-043 设计稿 §4、§5，D4、D5。依赖 [X1](NX-043-X1-implementation.md) 与 [X4](NX-043-X4-implementation.md)。
- **迁移**：`0294_nx043_guardrails_budget.sql`。这是临时编号，以合入后的 catalog 为准。同一分支上还有 X1 审查修复 `0293_nx043_start_purpose_recheck.sql`。
- 本切片没有重新定义已有函数。

## 1. 护栏与自动停止（D4）

`runtime.nexloop_experiment_guard(tenant, experiment)` 评估一个处于 started 的实验，第一项触发就自动停止。检查顺序如下：

| 护栏 | 触发条件 |
|---|---|
| `ended` | 到了登记的结束时间 |
| `twin_withdrawn` | 选用的孪生不再可选用：后来的校准失败，或已作废（X1 审查 4.1） |
| `purpose_not_allowed` | 候选或对照策略里的某个目的被改为营销类或需要同意 |
| `budget_exhausted` | 模型花费达到上限，或出现任何激励花费（激励上限为 0） |
| `reconciliation_anomaly` | 已分配的消费者在分配之后的效应中，有处于 unknown 状态的，或被确认未送达的，并且数量超过登记的阈值（默认 0，也就是出现一次就停） |
| `refusal_rate` | 候选组“拒绝联系或退订”的比例比对照组高出登记的差值（默认 2 个百分点）以上 |

- **拒绝联系或退订的计法**：分配之后新出现的联系限制，或分配之后的营销同意撤回，按所在组分别计数。
- **自动停止做什么**：由 `runtime.nexloop_experiment_autostop` 执行。
  - 追加一条 stopped 事件，执行人为 `nexloop:experiment-guard`，原因写作 `guardrail: <护栏代码>`；每个实验最多写一次；
  - 写一条审计，带上当时的指标（只有数字）；
  - 之后所有消费者在决策时都回到对照策略（X4 的 `strategy_for`）。
- **已发生的外部效应只对账**：不撤回、不重发。护栏不改动任何效应。
- **负责人手动停止**：仍然经 X1 的 `nexloop.experiment.stop`。

## 2. 实验级预算（D5）

- `control.nexloop_experiment_spend`：实验自己的花费台账，按模型（model）和激励（incentive）分开记，只能追加。它与现有的预算账户并列，不新增预算类别。
- `runtime.nexloop_experiment_spend(tenant, experiment, category, amount, ref)` 的规则：
  - 花费后仍在登记上限内：准入；
  - 花费会超过上限：拒绝（`allowed: false, reason: budget_exhausted`），并自动停止实验；
  - 刚好达到上限：准入，并自动停止实验；
  - 首批的激励上限为 0，所以任何激励花费都会被拒绝，并停止实验；
  - 实验没有在运行时，任何花费都被拒绝（`not_running`）；
  - 同一个 ref 重放时返回原结果；同一个 ref 换了金额或类别，以 `request_reused` 拒绝。

## 3. 真实派发

护栏、指标和预算函数都不读也不写真实派发开关。测试核对开关表在评估、花费和停止前后完全一致，并检查函数体中没有 real_dispatch。外部效应仍然要过全部外联闸门和三层派发开关。

## 4. 测试

`tests/test_nx043_guardrails_pg.py`（9 项）：

- 平稳的实验继续运行；
- 拒绝联系率触发停止，所有人回到对照；
- 营销同意撤回算作退订；
- online 实验出现对账异常时停止，效应本身不被改动；
- 模型预算超限被阻断并停止，可重放，台账只能追加；
- 激励上限为 0，模型花费刚好达到上限时停止；
- 孪生校准被收回时停止，到达结束时间时停止；
- 目的被改为营销类时停止；
- 开关表不变，应用角色都不能调用这些函数。

部分测试为了构造前提，在一次写入里临时关闭了触发器：

| 测试场景 | 为什么要关触发器 |
|---|---|
| 写入 unknown 状态的效应意向 | 跳过它的外键链 |
| 把登记的结束时间改到过去 | 冻结触发器不允许改写登记 |
| online 实验的 started 事件 | X1 仍拒绝开始 online 实验，所以直接写入事件 |

这些都只在测试里做，生产路径不会这样写入。

## 5. 接线（`0296_nx043_guard_wiring.sql`，临时编号）

- **定时评估**：没有另起常驻进程，接在现有的告警评估服务上（`nexloop-alert-evaluator`，数据库角色 `nexloop_domain_worker`，由 `background_services` 驱动）。
  - 每个 tick 先调用 `authz.nexloop_experiment_guard_tick`，再评估告警，所以本轮告警已经能看到刚刚发生的自动停止。
  - 这个 tick 用同一个服务凭据和同一项授权 `nexloop.alert.evaluate:1`，签名用独立的协议名 `nexloop-experiment-guard-v1`。
  - 它对本租户每个处于 started 的实验调用一次护栏，只在 real 中执行。
  - 重复执行是幂等的：已停止的实验不再被检查，也不会追加第二条停止事件。
- **模型花费**：在 `control.nexloop_budget_consumption` 上加了一个插入前触发器 `nx043_experiment_budget`，与 NX-041 S6 的模拟预算触发器并列，不重定义 `authz.nexloop_reserve_budget`。
  - **哪些 Run 算实验的**：Run 的模型预留（`run:<uuid>`）满足下面任一条件就计入实验：
    - real 中，Run 的消费者（`authz.nexloop_role_run_bindings`）在某个进行中实验的候选组。对照组用的是平常的策略，不算实验花费。
    - 模拟世界或影子世界中，实验所在世界的每个 Run。
  - **在上限内**：记入实验台账；刚好达到上限时停止实验。
  - **会超过上限**：这次预留不计入实验，实验立即停止，之后的决策都回到对照策略。这次 Run 本身不会被拒，只受租户自己的模型预算约束。这里不抛异常，是为了让停止事件能保留下来：在同一个事务里拒绝的话，停止事件会一起被回滚。
  - 单位不是 CNY 的预留不计入实验。
- **激励**：现在**有**激励路径，即 Agent 在 Run 中发起的激励（`authz.nexloop_run_incentive_command`），预算由激励发放服务在派发时预留。
  - 一个消费者只要被分配进某个进行中的实验（两组都算，这样两组仍然可比），他的激励预留就以 NXB01 拒绝，激励端口会报 `budget_exhausted`。
  - 激励是真实效应，模拟世界拒绝这类效应，所以这条规则对 online 实验起作用。
- **授权**：
  - `authz.nexloop_experiment_guard_tick` 只授予 `nexloop_domain_worker`，没有签名或协议不对的调用会被拒绝；
  - 护栏、花费、指标和触发器函数不授予任何应用角色；
  - 没有新表，tenant_fk 不需要登记；没有重定义函数：快照用的是改名包装，和 0277 的做法相同；definer 检查通过。
- **停止告警**：`runtime.nexloop_metrics_snapshot` 新增 experiments 块，包含 `running` 和 `auto_stopped_24h`。新增 **alert-rules v11**（`deploy/configuration/alert-rules.v11.json`），在 v10 基础上加一条规则 `experiment_auto_stopped`：24 小时内有护栏停止，就发出 warning。
  - 部署时要把 v11 作为告警规则应用（`observability.apply_rules`）。
  - 试运行配置 `nx037-trial.v8.json` 仍然引用 v10，没有改动。
- **测试**：`tests/test_nx043_guard_wiring_pg.py`（7 项），覆盖：
  - v11 等于 v10 加这一条规则；
  - 评估 tick 触发停止，只停一次，告警发出，停止后回到对照；
  - 模型预留在上限内记账，超过上限不计入并停止，刚好达到上限也停止；
  - real 中只有候选组的 Run 计入实验；
  - 已分配消费者的激励被拒，实验外的消费者不受影响；
  - 只有告警评估服务的角色能调用 tick。

## 6. 未完成项

1. **解除 online 的开始限制**（`online_not_available`）：等这次接线审核通过后，由调度员决定。
2. **接入外联决策路径**：X4 的 `strategy_for` 还没有接到外联决策和 Context 装配上。
3. **影子与筛选阶段的对账异常**：它们走替身台账，要在 X2、X3 中接入。

## 7. 第二轮审查（L6，2026-10-21）

| 项 | 处理（`0295`） |
|---|---|
| 2.1.a 停止原因只记第一项 | 自动停止的审计 metrics 中增加 `guardrails_hit`，列出当时命中的全部护栏；停止原因仍取第一项（`runtime.nexloop_experiment_guardrails_hit`） |
| 2.1.b 护栏还没有接线 | 由本文 §5 的接线（0296）落实；解除 online 的开始限制要以这次接线为前提 |
| 2.1.c guard 的 request_id 可能被 owner 占用 | 停止事件的 request_id 改为 `guard:<实验>:<随机 uuid>`，owner 的请求不可能与它相同；“每个实验最多停止一次”由状态检查保证 |
| 2.2.a 判断用的是未舍入的金额 | 先把金额舍入到台账的 4 位小数，判断和写入都用舍入后的值（`experiment_spend` 与 0296 的预算触发器都是这样） |
| 2.3.a determination 的口径 | 不需要改：`runtime.nexloop_effect_determinations` 的 kind 只允许 `confirmed_not_delivered`（0210 的检查约束），所以只会计入 unknown 和“确认未送达” |

## 8. 接线审查（L6，2026-10-21）与 D5 的口径

迁移 `0297_nx043_wiring_review.sql`（临时编号，第九批预演基准 `5d785078` 上的分支 `l5/nx043-x5b`）。

- **D5 口径（调度员 2026-10-21 决定）**：实验上限（0–100 元）只计**候选策略带来的增量成本**。对照组属于正常业务，它的模型花费也写进实验台账，记 `arm = control`，只用于报告对比，**不计入上限**。
  - 模拟世界与影子世界的 Run 都属于实验，`arm` 为空，计入上限。
  - 指标里 `model_cny` 只算候选组（以及 `arm` 为空的行），另有 `control_model_cny` 单列对照组。
- **2.1（确认，real）Run 归属到 Consumer**：改用 `runtime.nexloop_evo_run_consumer(tenant, 'real', run_id)`，覆盖 message route、fallback binding、role binding 三条路径；原先只看 role binding，会严重少算 message Run 的花费。
- **2.2（real）**：
  - 模型记账放在独立的异常块里，块内把 `lock_timeout` 设为 200 ms。出任何错都只写一条 `runtime.nexloop_experiment_accounting_failures`，快照里体现为 `experiments.accounting_failures_24h`，**real 的模型预留照常成功**。
  - 激励的 NXB01 拒绝在块外，保持原样。
  - 旧的触发器和函数换成新名字（`control.nexloop_experiment_budget_attribution`），没有重定义。
- **2.3**：
  - SQL 层：guard tick 对每个实验单独捕获异常，失败的计入 `failed`，其他实验照常检查。
  - Python 层：告警评估器的 guard 调用失败时，只在结果里记 `experiments_guard_failed`，告警（备份、时钟等）照常评估。
- **2.4**：新增 trial v9（`deploy/configuration/nx037-trial.v9.json`），它与 v8 只差告警规则：v9 指向 alert-rules v11。`controlled_interactions.SCHEMAS` 中加入了 v9。v8 保持原样，它的测试改为显式点名 v8。
- **1.2（擦除）**：擦除时，先取出被擦除者各行原来的 cluster_key，再把所有带这些 key 的行（包括簇内其他成员）换成同一个假名，这样不会在别人的行上留下他的会话 key。
