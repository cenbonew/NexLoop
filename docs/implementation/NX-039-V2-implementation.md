# NX-039 V2：门槛与指标（实现说明，2026-10-20）

依据：NX-039 设计稿 §3.2、§3.3、§3.4、§4.1、§5.1、§6.2、§7.2；D7、D39-1、D39-2、D39-4、D39-5、D39-7（均已定案）。分支 `l2/nx039-v2`，起点 `l2/nx039-v1`（af88d238）。

## 纯逻辑 `nexloop_eios/evolution/evaluation.py`

- 不访问数据库，不读时钟，不调用模型，也没有效应入口。
- 输入：冻结的评估包、补丁（只有 ref 和哈希）、E3 的访问与使用记录，以及调用方测得的数据。
- 输出：一份 `evolution-evaluation-report`。

### 门槛注册表

- 按评估包里冻结的 `hard_gates_version` 回答该版本的全部门槛。版本或门槛清单不认识时直接报错，不会默认判为通过。
- `nx038-hard-gates-1`：E5 的 7 项，用于 V2 之前冻结的旧包。
- `nx039-gates-1`：设计 §4.1 的 G1–G9：

| ID | 门槛（门槛 ID 就是拒绝码） | 判定 |
|---|---|---|
| G1 | `references_valid`、`structure_valid` | 补丁目标都是定义 ref，而且出现在这些用例的 Parent 返回结果里（不认识的 ref 不放行）；补丁只有固定的四个键 |
| G2 | `zero_overreach` | 补丁里没有定义 ref 以外的东西，例如 Action（AT-024） |
| G3 | `zero_cross_tenant` | 访问记录和使用记录都属于评估包里的用例 |
| G4 | `zero_fact_pollution` | L0 只读；数据库层由 PG 测试核对受保护表的 digest |
| G5 | `no_forbidden_regression` | 原来有结果的访问不能变成没有结果；受保护的 ref（联系约束、拒绝类定义）不能从结果中消失 |
| G6 | `semantic_regression` | Run 实际用过的 ref（E3 的 `use` 记录），在同一次访问中仍要返回。叠加层的 ref 排在最前，结果按 browse 上限（10）截断，被挤出上限就算失败 |
| G7 | `cost_performance` | D39-5：语义读取的 p95 不超过 Parent 的 110%，而且增量不超过 20 ms；原本就有结果的访问，返回的 ref 数不超过 105%；单次最多 50 条别名，每个 ref 最多 20 条。**没有测量数据时判为失败** |
| G8 | `semantic_conflict` | 别名的词项已经指向另一个定义：可能是已发布的别名，也可能是 Parent 中只含这一个词项的访问返回了别的 ref |
| G9 | `index_migration_impact` | 对 semantic_content 不适用（NX-040 启用） |

- 报告逐项写 pass 或 fail 和证据 ID。`failed_gates` 按注册表顺序列出失败的门槛，每个门槛 ID 就是它自己的拒绝码。
- 任一门槛失败，结论就是 `reject/hard_gate_failed`，评分再高也一样。

### 非劣（D7）

- 设计集和留出集都要满足 `候选 − Parent ≥ −1 个百分点`，而且至少一边严格更好。
- 两边都相等时判为 `not_better`；任一边超出非劣差值时判为 `inferior`。

### 可复现（D39-2）

- L0 跑两次，第二次把记录倒序输入。两份报告的 digest 不同时，结论是 `incomplete/not_reproducible`。
- 匿名 A/B：每个用例的左右交换由 `random.Random(anonymize_seed)` 决定，写入 `swaps`。种子相同，结果就相同。

### 变更分级 `classify_change`（D39-7）

- 只根据补丁和 Parent 的记录判定，人和 Agent 都不能手工设置。

| 情形 | 分级 |
|---|---|
| 别名指向单个 ref，且没有冲突 | patch |
| 别名同时指向多个 ref，且没有冲突 | minor |
| 别名与已有含义冲突；没有词项 | major |
| 补充说明：目标已有说明 | patch |
| 补充说明：目标没有说明 | minor |
| 补充说明：不知道目标有没有说明 | major |
| 未知的变更种类 | major |
| semantic_tool、ontology_schema 维度 | major（不进入语义发布） |
| 结构不对，或指向定义以外的 ref | refused |

- major 或 refused 的候选判为 `reject/breaking_change`。

## 迁移 0238（本地临时编号）

调度员指定从 0242 起编号，但 `test_bootstrap` 要求 catalog 编号连续，而本分支只到 0237，所以本地用 0238，合并时由调度员改为 0242 或之后的编号。迁移正文不引用自己的编号。

**为什么需要迁移**：评估包冻结哪份门槛清单，以及评估器怎样写报告，都由数据库函数决定，不改函数就改不了。

- `runtime.nexloop_evo_hard_gates()` 原地替换为 `nx039-gates-1`。已冻结的评估包保留它们冻结时的清单。
- 新函数 `authz.nexloop_evo_evaluation_conclude`，只授予 `nexloop_evo_evaluator`。它核对报告与冻结包一致：门槛版本、每个冻结门槛都有答复、`failed_gates` 与答复一致。下面任何一种情况都拒绝：
  - accept 的报告有失败的门槛，分级是 major 或 refused，`reason` 不为空，或者没有通过复现；
  - 门槛失败，却用别的原因拒绝；
  - 分级是 major，却用别的原因拒绝；
  - 原因码不在允许范围内。
- 核对通过后，在同一个事务中：写入 E5 的报告表；把候选从 `evaluating` 转为 `accepted_for_release`、`rejected` 或 `incomplete`（`not_reproducible` 对应 `end_reason = evaluator_error`）；写候选事件 `evaluation_concluded`。
- D39-1：收回 `nexloop_evo_writer` 对 `authz.nexloop_evo_report_record` 的执行权限，提出候选的一方不能写报告。函数本身没改。
- 候选的 `end_reason` 增加 `evaluator_error`；事件种类增加 `evaluation_concluded`。

## 契约（只新增）

- `evolution-evaluation-report`：
  - 门槛枚举新增 `semantic_regression`、`cost_performance`、`semantic_conflict`；
  - `reason` 新增 `not_reproducible`、`breaking_change`；
  - 新增四个可选字段：`hard_gates_version`、`failed_gates`、`change_class`、`reproduced`。
- `evolution-evaluation-bundle`：门槛枚举新增同样的三项。
- 已有的字段和 example 都没改；`generate_contracts --check` 通过。

## 对已有测试的调整

- `tests/test_evolution_e5_pg.py`：
  - 冻结包现在是 V2 的门槛清单；
  - writer 写报告改为断言被拒绝；
  - 报告、审核关联和冻结删除的断言，移到 `test_evolution_v2_pg.py` 中经评估器路径完成。
- `tests/test_evolution_e1.py`：纯逻辑包允许 import 包内的 `nexloop_eios.evolution.replay`（`evaluation.py` 复用了它的 `check_bundle`）。

## 测试

- `tests/test_evolution_v2.py`（纯逻辑，34 项）：
  - 干净的别名被接受，而且可复现；
  - 10 种门槛失败各自给出自己的门槛 ID，结论是 `hard_gate_failed`；
  - 门槛与评分分开（评分高但门槛失败，仍然拒绝）；
  - 不认识的门槛版本或清单报错；
  - 非劣边界 6 例：恰好 −1 个百分点、刚超出差值、完全相等、在差值内但没有严格改善、一边改善一边持平、大幅改善但留出集劣化；
  - 匿名化按种子复现，换种子结果不同；
  - 不能复现时为 `incomplete`；
  - 分级表 12 例，不确定时判为 major；
  - major 判为 `breaking_change`。
- `tests/test_evolution_v2_pg.py`（4 项）：
  - 评估器写报告并完成接受、重放、事件，D4 审核关联使用评估器的报告 digest，报告与关联表冻结，受保护表的 digest 不变；
  - SQL 侧的 10 种拒绝，以及门槛失败时转为 `rejected`；
  - 不能复现时转为 `incomplete/evaluator_error`；
  - writer 既不能写报告，也不能完成评估。
