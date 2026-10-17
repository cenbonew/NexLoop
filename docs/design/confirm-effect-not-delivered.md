# 设计稿｜负责人确认“该效应未发生”（confirm not delivered）

状态：**设计稿**，实现待负责人确认。对应的 ADR 草稿：`docs/design/ADR-033-draft-confirm-effect-not-delivered.md`。
背景：10/16 冒烟留下 2 条 unknown 回复意图（1f6ac3bd、d1dc3853）。0205 允许负责人下单重查；但 webchat-delivery 只保留了 10/16 21:42 之后的投递记录，所以重查很可能返回 not_found。按 ADR，结果不明的效应要先查询、再对账，绝不盲目重发。目前没有受治理的办法让这类意图收尾，`unknown_over_24h` 会一直告警。

## 1. 现状

| 对象 | 现有状态（约束） | 相关事实 |
|---|---|---|
| `runtime.nexloop_effect_intents.state` | accepted, dispatching, unknown, observed_fulfilled, fulfilled, **failed**, confirmed（0042） | `failed` 已是终态，表示“效应没有发生” |
| `runtime.nexloop_effect_outbox.state` | pending, leased, unknown, query_pending, fulfilled, **failed** | `failed` 之后不再被领取 |
| `runtime.nexloop_outbound_messages.delivery_state` | 转移表（0079）已允许 **unknown → failed** | 有逐条转移事件 |
| `runtime.nexloop_effect_observations` | provider_state 取 accepted / fulfilled / not_found，记录观测者 principal | not_found 观测本身就是“查询结果”的证据 |
| 指标 `effects.unknown.over_24h` | 统计 `state='unknown'` 且创建超过 24 小时的意图（0131/0141） | 意图离开 unknown，计数就下降 |
| 回复保证（0152） | 出站被**明确判失败**后，会启动兜底回复 | **关键交互**：不做处理的话，确认“未送达”会自动触发一条兜底回复，见 §5 |
| 负责人 Action 范式 | `GoalGovernedActions._submit`（goal_controls.py），以及 0124/0150 的受治理处理器；SQL 只认人类负责人 | 例如 `release_contact_restriction`、`request_effect_query`、`cancel_commitment` |

## 2. 命名建议

- **推荐**：不新增意图状态，意图沿用现有终态 `failed`，另写一条判定记录，类型为 **`confirmed_not_delivered`**。这样可以：
  - 不扩大 0042 的状态约束；
  - 已有代码中所有判断“终态”或“failed”的地方（outbox 不再领取、`_public_observation`、回执读取、nx037 观测）都不用改；
  - 区分“系统判失败”和“负责人确认未送达”的工作交给判定表。
- **备选**：新增意图状态 `confirmed_not_delivered`。语义更直白，代价是要改 0042 的约束、一系列判定函数、nx037 的 SETTLED_STATES 和契约枚举，回归面大。

出站记录统一用 `delivery_state='failed'`，转移事件来源记为 `owner_determination`。

## 3. 表与状态迁移

新表 `runtime.nexloop_effect_determinations`（只追加、不可修改），由 RLS 和 owner 策略保护：

| 列 | 说明 |
|---|---|
| determination_id uuid PK | |
| tenant_id, world, intent_id | 外键指向意图 |
| kind | 目前只有 `confirmed_not_delivered` |
| observation_id | 作为依据的那条 not_found 观测（外键） |
| confirmation_ref | 负责人确认引用，格式 `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$` |
| reason | 负责人填写的理由，1–500 字 |
| decided_by | 人类负责人的 principal，由服务端身份推导 |
| decided_at | |
| reply_followup | 负责人对回复保证的选择，见 §5：`owner_follow_up` / `fallback_reply` / `none` |

一次判定在**同一事务**里完成以下迁移：
1. 意图：unknown → failed；`governed_claim_finalized` 保持 false。
2. outbox：unknown 或 query_pending → failed，清除租约。
3. attempt：state 改为 failed，`attempt_revision+1`。
4. 出站记录：unknown → failed，写一条转移事件，来源 `owner_determination`。
5. 效应事件：新增一条，`event_kind` 需要扩展为 `determined`（改 0042 的检查约束）；也可以只依赖判定表，见 §7 的选项 Q4。
6. 回复保证：按 `reply_followup` 处理，见 §5。

全程不发送任何东西，不新建意图，不改 Action claim。

## 4. 定义器与 Action 注册

- **Action**：`nexloop.effect.confirm_not_delivered`，版本 1。
  - capability 为 `service.confirm_not_delivered`，类型挂在 Consumer:1 上（与 GOAL_ACTIONS 相同）；
  - 风险等级 low，approval none：负责人本人执行就是批准；
  - 注册方式与 `request_effect_query`（0150）一致：system_catalog 的 ActionSpec 加上受治理处理器。
- **Python**：`GoalGovernedActions.confirm_effect_not_delivered(action_name, action_version, request_id, intent_id, confirmation_ref, reason, reply_followup)`，经 `_submit` 提交 operation `confirm_effect_not_delivered`。
- **SQL 处理器**：在受治理处理器中新增一个分支，是 security definer，`search_path` 固定为 `pg_catalog,pg_temp`。检查顺序如下，任一不满足都拒绝并给出固定 sqlstate 或原因码：
  1. 调用者是**人类负责人**。服务、Agent、Run 凭据一律拒绝，与 `release_contact_restriction` 相同。
  2. `request_id` 幂等：同一个请求 id 重放时返回原结果。
  3. 意图属于本租户，状态为 `unknown`，且 outbox 不处于租约中（租约中返回 `effect_busy`，稍后再试）。
  4. **证据**：这条意图最新一条观测是 `provider_state='not_found'`，晚于它最后一次 attempt，且在它之后没有 fulfilled 或 accepted 观测。不满足返回 `no_not_found_evidence`。
  5. **最小等待期**：首次 unknown 事件距今不少于 `min_unknown_hours`（建议 24 小时），且该 not_found 观测距今不少于 `min_after_not_found_minutes`（建议 10 分钟，给可能的延迟落库留时间）。两个值放在可配置的策略文件 `deploy/configuration/effect-determination.v1.json` 中，由负责人维护。不满足返回 `wait_period`。
  6. 派发控制：恢复中的 fence 或暂停时拒绝（`controls_refuse`）。
  7. 判定表里该意图还没有判定（一条意图只能判定一次）。
- **只读视图**：工作台和 nx037 可以读取判定记录，只返回 id、类型、时间和确认引用，不返回理由原文。

## 5. 与 ADR-023（回复保证）的关系：需要负责人选择

出站记录一旦被明确判为 failed，0152 的现有逻辑会立即启动一条**兜底回复**。对 1f6ac3bd、d1dc3853 这样的存量消息，这意味着两天后顾客才收到回复，而且如果原回复其实送达了（provider 记录丢失导致 not_found），顾客会收到两条回复。

| 选项 | 做法 | 优点 | 风险 |
|---|---|---|---|
| **A. 负责人跟进（推荐默认）** | 判定时把这条消息的回复保证项结清为 escalated，原因 `owner_follow_up`；工作台显示“负责人跟进”，不自动回复 | 不会因 provider 记录缺失而重复回复；适合存量和超时很久的消息 | 顾客能否得到回复取决于负责人跟进 |
| B. 允许一次兜底回复 | 判定时 `reply_followup=fallback_reply`，沿用 0152 启动一次兜底，每条消息仍最多一次兜底 | 消息不会没人回 | 原回复其实送达时会重复；时隔很久的回复可能不合时宜 |
| C. 不处理 | 交给回复保证的现有逻辑，等同于 B | 无需改动 | 同 B，而且行为不透明 |

**推荐**：A 作为默认，B 由负责人按条选择（判定时显式指定）。存量的 1f6ac3bd、d1dc3853 建议选 A。

## 6. 测试计划（实现阶段）

- PG，使用真实 provider：投递后得到 unknown → 来源 Run 过期 → 0205 重查 → provider 不可用或 not_found → 判定前置条件逐一验证：
  - 没有 not_found 证据时拒绝；
  - 等待期未到时拒绝；
  - 服务或 Agent 身份拒绝；
  - 重放同一 request_id 时返回同一结果；
  - 第二次判定拒绝。
- 判定成功后：意图、outbox、attempt、出站记录都变为 failed；有一条转移事件；判定记录里有确认引用；provider 收到的 POST 数为 0；不新建意图；`effects.unknown.over_24h` 计数减 1，下一次告警评估把 `unknown_over_24h` 记为 resolved。
- 回复保证：选 A 时没有兜底 Run，escalation 的原因为 `owner_follow_up`；选 B 时恰好启动一个兜底 Run。
- 判定之后：effect worker 不再领取这条意图；0205 重查订单返回 `not_unknown`。
- 新增 SQL 定义器要在 AT-064 白名单里登记（如果涉及 nexloop_claims，预计不涉及）；definer 检查；static。

## 7. 需要负责人拍板

- **Q1 命名**：意图沿用 `failed` 并附判定记录 `confirmed_not_delivered`（推荐），还是新增意图状态。
- **Q2 最小等待期**：首次 unknown 后多少小时（建议 24），not_found 观测后多少分钟（建议 10）。
- **Q3 回复保证**：默认选 A 负责人跟进，B 按条选择（推荐）。
- **Q4 事件账本**：是否扩展 `event_kind` 增加 `determined`（多一处约束改动，但事件流完整），还是只用判定表（改动最小）。
- **Q5 provider 记录保留期**：not_found 可能只是 provider 删了旧记录。是否要求判定时额外填写“已核对 provider 保留期”的确认项（建议作为 reason 的必填内容提示，不做强校验）。
