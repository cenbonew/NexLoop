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

## 5. 未完成项

1. **定时评估**：护栏要有定时任务调用 `runtime.nexloop_experiment_guard`，比如由告警评估器每轮调用一次。模型花费和激励花费的路径也要在花费前调用 `runtime.nexloop_experiment_spend`。两者都还没有接线，函数也没有授予任何应用角色。
2. **online 开始**：接线完成后，再解除 X1 中 online 的开始限制（`online_not_available`）。
3. **停止告警**：停止时要给负责人发告警（设计 §5），目前只写了审计。
4. **影子与筛选阶段的对账异常**：它们走替身台账，要在 X2、X3 中接入。
