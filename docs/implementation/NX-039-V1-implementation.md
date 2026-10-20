# NX-039 V1：评估器角色与开评（实现说明，2026-10-20）

依据：NX-039 设计稿 §2.2、§3.4、§4.2、§4.3、§5.1、§11 V1、§12.1；D39-1、D39-2、D39-3（负责人 2026-10-20 定案）。分支 `l2/nx039-v1`，起点 `l6/nx038-e5`（9eff4b28，E1–E5，迁移到 0237）加上设计稿的两个提交。

## 迁移 0241（临时编号，`0241_nx039_evaluation_open.sql`）

- **评估器角色**：`nexloop_evo_evaluator`，login、noinherit，只能执行 `authz.nexloop_evo_evaluation_open`，对任何表都没有权限。
- **一个主体只有一种用途**（D39-1）：
  - 表 `authz.nexloop_evo_principals` 每个服务主体只有一行，用途是 writer 或 evaluator，所以同一个主体不可能同时登记两种用途；
  - 登记后不能修改、删除或清空；
  - 登记由可信配置写入，应用角色都没有表权限。
- **writer 入口**：`runtime.nexloop_evo_writer_tenant` 在原地替换，函数体与 0235 相同，只多一条检查：登记为 evaluator 的主体会被拒绝。因此评估器不能提交候选、冻结评估包、写报告或写审核关联。
- **evaluator 入口**：`runtime.nexloop_evo_evaluator_identity` 要求：
  - 会话用户是 `nexloop_evo_evaluator`；
  - 凭据是有效的服务凭据，不带 Run 上下文，也不是浏览器令牌；
  - 凭据的主体在本租户登记为 evaluator；
  - tenant 取自凭据，world 只能是 real。
- **开放维度与停止**：
  - `control.nexloop_evo_open_dimensions` 是可审计的一行配置（设计 §12.1），初始只有 `semantic_content`（D19、D39-6）；
  - `control.nexloop_evo_dimension_stops` 是负责人按租户、按维度的停止开关。设置它的受治理 Action 在 V4，V1 只负责在开评时检查。
- **留出集**（D39-3）：
  - `runtime.nexloop_evo_holdouts`：用验证用例 ID 去重排序后的 sha256 登记，只追加；
  - `runtime.nexloop_evo_holdout_uses`：每次开评记一次使用，同一个留出集最多 5 次（`runtime.nexloop_evo_holdout_limit()`）。两张表 owner 也不能修改或删除。
- **开评** `authz.nexloop_evo_evaluation_open(digest, candidate_id, bundle_digest)`：按顺序做 7 项复查，第一个不通过的项作为原因码返回，不抛异常，也不改变任何状态：

| 顺序 | 原因码 | 检查 |
|---|---|---|
| 1 | `bundle_mismatch` | 评估包存在，digest 与传入的一致，并且与包内容重算的结果一致；包中的 Parent、候选修订、快照、评估器版本、模型配置、补丁 digest、用例和分组与候选行一致 |
| 2 | `candidate_not_ready` | 候选是 `proposed`；所属诊断没有失效；没有来源 Consumer 正在删除 |
| 3 | `leakage_detected` | 每个用例都有分组；没有一个分组同时在两边；设计集和留出集中没有两个用例在 E3 记录里问过完全相同的词项集合（§4.2 的近重复规则） |
| 4 | `holdout_exhausted` | 这个留出集用过的次数少于 5 次 |
| 5 | `dimension_closed` | 候选所在维度在开放清单中 |
| 6 | `dimension_stopped` | 本租户没有停止这个维度 |
| 7 | `budget_exhausted` | 候选级费用还有剩余；租户本期的 evolution 限额在有效期内，而且没有用完 |

- **通过时**，在同一个事务中：
  - 登记留出集（已存在则跳过）；
  - 写一条使用记录（第几次使用，以及评估器主体）；
  - 候选从 `proposed` 转为 `evaluating`；
  - 写候选事件 `evaluation_opened`，记录 bundle digest、留出集 digest 和第几次使用。
- **重放**：同一个候选用同一个评估包再次开评，返回同一个结果，并标注 `replayed`。
- **报告**：沿用 E5 的报告表和 `evolution-evaluation-report` 契约，V1 不新增。E5 的 `authz.nexloop_evo_report_record` 目前只授权给 writer；按设计 §5.1，应该改由评估器写报告，这一步放在 V2（门槛与指标）。

## Python

`nexloop_eios/evolution_store.py` 新增 `EvaluatorStore`，只有 `open` 一个方法，并核对当前角色。原因码列在 `OPEN_REFUSALS`。

## 测试 `tests/test_evolution_v1_pg.py`（5 项）

- **一个主体只有一种用途**：
  - 已登记为 evaluator 的主体再登记为 writer，违反唯一约束；登记行不能修改；
  - 评估器的凭据调用 writer 的读取和冻结，都被拒绝；
  - writer 的凭据（无论是否登记为 writer）都不能开评；
  - writer 角色执行不了开评函数；
  - 评估器角色没有任何表权限，也不能执行 writer 函数。
- **开评与重放**：`proposed → evaluating`，记下第 1 次使用；重放返回相同结果；事件内容正确；受保护表的 digest 不变（AT-023）；留出集表 owner 也删不掉。
- **7 项复查**：每一项都单独触发，返回各自的原因码，候选保持 `proposed`。
- **近重复**：设计集和留出集各有一个 Run，在 E3 记录中问了相同的词项，返回 `leakage_detected`。
- **使用上限**：同一个留出集上，前 5 个候选依次开评，记为第 1 到第 5 次使用；第 6 个候选返回 `holdout_exhausted`。
