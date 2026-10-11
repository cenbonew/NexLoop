# AT-043 发放优惠：设计稿（已定案，2026-10-11）

分支 `at043-design`，从 main `eb51df4` 切出。只写文档，不含代码与迁移。

- 验收：AT-043「成本上限」——达到租户的模型或优惠预算后，阻断新的消耗，已有的合法结果保留（`planning/acceptance-tests.json`，M19，S4）。模型预算部分已由 NX-022/NX-027 覆盖（`tests/test_costs_pg.py`）；缺的是“发放优惠/折扣”这一受治理动作，NX-027 实现说明 §已知限制写明“仓库里还没有发放折扣的 Action”。
- 相关验收：AT-010「供给边界」（目录外承诺/优惠不形成未授权承诺，已通过）、AT-029（不为完成 KR 主动触达）、ADR-023 §3（联系限制只挡触达客户的 effect，NX-026 D6）。
- 约束（AGENTS.md）：只有受治理的 EIOS Action 能产生外部效果；派发时（而不只是计划时）检查权限与约束；先持久化再确认；未知结果先查询再对账，不盲目重试；模拟与影子运行不能使用真实效果凭据。

## 1. 现状核对

| 能力 | 现在在哪里 | 现状 | 缺什么 |
|---|---|---|---|
| incentive 预算额度 | 0068 `control.nexloop_budget_limits`（`budget_kind` ∈ model / incentive；incentive 的单位必须是 ISO 币种、额度为整数最小货币单位）；受治理的额度修改走 NX-022 控制修订 | 可配置 | — |
| 预留 | 0068 `authz.nexloop_reserve_budget(digest, world, kind, consumption_id, amount, unit, source_ref)`：对额度行加锁串行；同一 `consumption_id` 重放原结果，参数不同报 NXB02；未配置 NXB03；周期外或 `已用 + 本次 > 额度` 报 NXB01，不触碰任何已有消耗；`control.nexloop_budget_consumption` 只追加 | 函数可用（授予 api / domain_worker / action_worker / scheduler，0068、0097） | 没有任何调用方以 `incentive` 调用它 |
| 模型预算的派发拒绝 | `goal_controls.prepare_run_dispatch` 在 Run 派发时按 `maximum_cost` 预留 `run:<id>`；NXB01 时 Run 不派发；0131 把 NXB01 映射为观测码 `budget_exhausted` | 已实现，AT-043 模型部分有测试 | — |
| 结算 | 0120 D5：Run 任务终态时追加 `run:<id>:settle` 释放行，`runtime.nexloop_budget_settlements` 记录 released / fully_used / exceeded / result_pending_or_unknown；有未知结果时不释放 | 只针对 model | incentive 预留从不结算或释放 |
| discount 费用条目 | 0120 `control.nexloop_cost_on_incentive`：`budget_consumption` 插入一条 incentive 消耗时，同一事务写一条 `runtime.nexloop_cost_entries`（`cost_kind=discount`，`basis=budget_reservation`，`source_ref=incentive:<consumption_id>@<source_ref>`，最小货币单位），并投影为 `cost_entry` 指标观察 | 触发器可用 | 没有来源；没有“发放失败”的冲销条目 |
| effect 执行管线 | `effect_execution.py`：claim → admit（派发前在受保护的锁下重验来源、目录、Role 信封与派发控制）→ provider → observe / finalize；unknown → `authorize_effect_query` → 查询 → 对账（0065、0051 本地投递恢复） | 只服务唯一的 effect Action `nexloop.service.request:1` | 没有优惠 Action |
| 派发控制 | 0097 `runtime.nexloop_effect_dispatch_controls`：提交时快照所有者控制，派发时由 `authz.nexloop_assert_dispatch_controls` 再判一次（暂停、目标过期、控制修订） | 通用 | 预算检查没有接到 effect 派发上 |
| 联系限制与 effect 类别 | 0109/0110 联系限制；0112 `control.nexloop_action_effect_categories`（customer_contact / non_contact_service + 通知参数），配置 `deploy/configuration/action-effect-categories.v1.json` | `nexloop.service.request` 声明为 customer_contact | 新 Action 需要声明类别 |
| 供给目录（AT-010） | 0053 `authz.nexloop_assert_service_offering`：ServiceOffering / ConsumerServiceOffering 固定为 `local.json-export`，`allowed_discounts=[]`；请求带折扣或保证一律被 0056 记为 `outside_catalog_terms` 的 scope denial，并在 WebChat 回执中说明可交付范围 | 已通过 AT-010 | 没有“优惠目录”：现在任何优惠都在目录外 |
| Role 上限 | 0081 `RoleExecutionCeiling`（9 个字段：`action_resources`、`consumer_ids`、`goal_ids`、`step_ids`、`budget`（模型）、`effect_units` 等）与 `RoleAssignmentScope`，取交集，策略从不授予权限 | 已实现 | ceiling 里没有优惠金额上限 |

结论：预算的“决定”部分（`reserve_budget`、NXB01、费用条目、指标）都在；缺的是一个受治理的发放 Action、它的目录、派发时预留、失败时结算，以及一个回环 provider。

## 2. 发放优惠的受治理 effect Action

### 2.1 名称与参数

- Action：`nexloop.incentive.issue:1`，capability `incentive.issue`，幂等。D2（负责人 2026-10-11）：EIOS 风险 low、无审批；NexLoop 分类 medium，补偿控制为三重约束（目录单次上限、每 Consumer 周期上限、租户预算）。
- 冻结请求（`frozen_request.parameters`，canonical JSON，所有字段必填）：

| 参数 | 类型 | 说明 |
|---|---|---|
| `incentive_offering_id` | 64 位十六进制 | 目录中的 IncentiveOffering 对象（§2.3） |
| `offering_revision` | 整数 | 派发时必须仍是当前修订，否则拒绝（与 0053 一致） |
| `binding_id` | 64 位十六进制 | 该 Consumer 的 ConsumerIncentiveOffering 绑定 |
| `amount_minor` | 字符串，正整数 | 最小货币单位，不允许小数 |
| `currency` | ISO 4217 | 必须等于目录与 incentive 预算的币种 |
| `notification_text` | 字符串，可为空 | 附带给客户的通知；声明为通知参数（§4） |

- Consumer、租户、world、发起主体全部取自服务端的认证上下文与 Run 绑定，不从参数读取。

### 2.2 幂等键与业务意图 ID

- Agent 发起：沿用 `nexloop.service.request` 的计划槽位身份（goal / plan step / slot，0040 `slot_identity`），同一槽位重试返回原回执，Agent 不能自选新业务键。
- 人类发起：经 NX-028 受治理入口（0124 registry），幂等键是 `request_id`，业务意图 ID = `incentive:<tenant>:<request_id>` 的摘要。
- 预算消耗 ID：`incentive:<intent_id>`；`source_ref`：`effect:<intent_id>`。同一意图的重复派发（租约丢失后重领）重放同一预留，参数不同即 NXB02。

### 2.3 目录：IncentiveOffering 与绑定

新增两个形式对象类型（经现有受治理 create / edit Action 写入，不新增写路径）：

- `IncentiveOffering`：`incentive_code`、`kind`（v0.1 只有 `fixed_amount`）、`max_amount_minor`（单次上限）、`currency`、`per_consumer_period_limit_minor`（同一 Consumer 在预算周期内的累计上限）、`eligibility`、`valid_until`、`active`、`title`。
- `ConsumerIncentiveOffering`：`consumer_id`、`offering_id`、`offering_revision`、`source_principal`、`active`（与 0053 的 ConsumerServiceOffering 同形）。

派发前（admit）在同一事务里断言：对象与绑定存在且为当前修订、`active`、未过期、币种一致、`amount_minor ≤ max_amount_minor`、该 Consumer 本周期已发放额 + 本次 ≤ `per_consumer_period_limit_minor`。任何一项不满足：意图被拒，provider 零请求。

### 2.4 谁能发起

- **Agent**：只能在 Role 内发起——`RoleExecutionCeiling.action_resources` 与 Run 的 `allowed_resources` 都含 `eios:action:nexloop.incentive.issue:1`，Consumer / Goal / Step 落在 ceiling 与 scope 的交集内（0081）。金额上限由目录（单次、每 Consumer 周期）与租户 incentive 预算共同约束；是否在 ceiling 中另加优惠金额上限见 D3。Agent Host 增加一个工具 `nexloop.incentive.issue`（与 `nexloop.service.request` 同一受治理路径；工具说明写明“accepted 不等于已发放，重试返回原回执”）。
- **人类**：负责人经 NX-028 受治理入口发起（capability `incentive.issue`，只允许 human_owner，服务主体被拒），生成同样的 effect 意图，走同一派发与预算检查。负责人不能绕过预算与目录（D4 讨论是否允许负责人在目录外单次发放）。
- **服务主体、Run 凭据以外的调用、浏览器客户会话**：一律拒绝。

### 2.5 目录外优惠（AT-010）

- 发放请求引用的优惠不在目录或越过目录上限：在 admit 前记录一条 scope denial（复用 0056 的记录方式：不形成承诺、不派发，回执中说明可交付范围），Agent 收到 `outside_catalog_terms`。现有 scope denial 的 scope 形状只覆盖 `local.json-export`，需要新增优惠类的 scope 形状（契约改动，见 §6.2）。
- Agent 在消息里口头许诺目录外优惠：属于 AT-010 的既有路径（NX-019 抽取为 commitment Claim）。NX-026 登记承诺时，若承诺内容是优惠且没有对应的目录项，登记为异常 `unauthorized_incentive_promise` 并提示负责人，不登记为可履行的 Commitment（见 D5）。
- `nexloop.service.request` 的 `allowed_discounts` 保持 `[]`，服务请求里不能夹带优惠；优惠只经本 Action 发放。

## 3. 预算

1. **发放时预留。** 预留发生在 admit（派发前、provider 调用前）的同一事务中：`reserve_budget('incentive', 'incentive:<intent_id>', amount_minor, currency, 'effect:<intent_id>')`。0120 的触发器随即写 discount 费用条目并投影指标。
2. **达到上限时派发前拒绝。** NXB01（额度不足或周期外）或 NXB03（未配置）：意图转为拒绝终态，记录拒绝原因 `budget_exhausted` / `budget_unconfigured`（0131 已有映射，观测与告警自动可见），provider 零请求。提交时（Agent 调用工具时）可做一次非权威的预算状态检查以便早失败，但权威判断只在派发时。
3. **已经发放的保留。** 消耗表只追加；额度调低或周期结束都不撤销已有消耗与已发放的优惠；已入账的 discount 费用条目不改。
4. **未知结果。** provider 返回超时、连接中断等未知状态：意图进入 `unknown`，预留不释放；执行者先用同一意图 ID 向 provider 查询（`authorize_effect_query` → 查询 → `record_effect_query_observation`），按查询结果对账；查询仍未知就保持 unknown 并按 0065 的退避继续查询，绝不重新发放。
5. **明确失败的结算。** provider 明确答复“未发放”（failed / not_found 且查询确认）：追加结算 `incentive:<intent_id>:settle`（释放全部预留，`runtime.nexloop_budget_settlements` outcome=released），并追加一条 discount 冲销费用条目（`basis=budget_release`，金额为负，同一币种）。fulfilled：结算为 fully_used。unknown：不结算（与 0120 D5 的模型规则一致）。冲销条目的表示方式见 D6。
6. **与 NX-027 衔接。** 费用读端口与工作台 `/costs` 直接看到 discount 条目与冲销；`cost_entry` 指标按币种分别汇总，不跨币种相加；KR 计算沿用 0119/0120 的投影。删除流程（NX-029）对 discount 条目沿用费用条目的假名化与财务保留期。

## 4. effect 类别（ADR-023 §3）

- 发放优惠本身是给客户账户记一笔权益，不发消息、不外呼：声明为 `non_contact_service`，通知参数声明为 `["notification_text"]`（`action-effect-categories.v1.json` manifest v2）。
- 客户处于联系限制时（NX-026 D6 / 0112）：`notification_text` 为空的发放照常派发；带通知的发放整条拒绝（NXC05 `attached_notification`），不能拆成“发放 + 通知”两个请求。需要告知客户时，只能在客户来信后用绑定该入站消息的回复说明（ADR-023 §2 第 6 条）。
- 是否允许 Agent 对受限客户主动发放（即使不带通知）见 D7。
- AT-029：复评计划为完成 KR 主动发放优惠属于“主动触达”的边界情况，见 D7。

## 5. provider

- 只提供两个 provider：
  - `local.incentive-ledger`（回环）：写入私有目录中 fsync 的 JSON 账本（每个意图一行，键为 intent_id），支持按 intent_id 查询（用于 unknown 对账），可注入 timeout / 已写入但未答复等故障，与 0051 本地 JSON 投递同一模式；
  - 测试 provider（进程内），只在测试中使用。
- 不接任何真实优惠渠道（电商券、支付立减、会员积分等）；真实渠道属于 B 类，以后由负责人决定，届时另写 ADR。`REAL_DISPATCH_ENABLED` 与渠道开关对 `nexloop.incentive.issue` 保持关闭；service-grants 中该 Action 只授予回环 profile。
- 模拟 / 影子 world 不能使用任何 provider 凭据（AGENTS.md），只记录意图与预算决定。

## 6. 测试、迁移与契约

### 6.1 测试清单（真实 PG，回环 provider，合成数据）

正例：
1. 额度内发放：预留 1 条、discount 费用条目 1 条、`cost_entry` 指标 1 条、provider 恰好 1 次请求、回执 fulfilled、结算 fully_used。
2. 同一槽位重试：返回原回执，预算只预留一次，provider 只收到一次请求。
3. 负责人经受治理入口发放：同一路径，审计记录发起人。
4. 受限客户、无通知：照常派发（D7 采用推荐时）。
5. unknown 后查询确认已发放：对账为 fulfilled，预算不释放、不重复发放。
6. 明确失败：结算 released、冲销条目使成本净额为 0。

负例（AT-043 主体）：
7. 预算耗尽：第 N+1 笔在派发前被拒（`budget_exhausted`），provider 零请求，前 N 笔的消耗、费用条目与发放结果全部保留。
8. 并发：两笔同时派发、额度只够一笔：恰好一笔成功（额度行锁串行），另一笔 NXB01。
9. 额度调低到低于已用：之后的发放全部被拒，已有的保留；周期外同样被拒。
10. 未配置 incentive 预算：NXB03 拒绝。
11. unknown 不释放、不盲重试：查询未知时保持 unknown，预算不释放，provider 只收到查询请求。
12. 目录外：offering 不存在、修订过期、未绑定、超过单次上限、超过每 Consumer 周期上限、币种不符——各自被拒并记 scope denial（AT-010），provider 零请求。
13. Role 外：ceiling 或 Run 不含该 Action、Consumer 不在 scope 内——拒绝。
14. 服务主体与浏览器会话调用人类入口——拒绝。
15. 受限客户带通知——NXC05 `attached_notification`，provider 零请求。
16. 暂停控制或目标版本过期——派发时被拒（0097）。
17. 模型预算与优惠预算互不挤占：模型预算耗尽时发放不受影响，反之亦然。

### 6.2 迁移与配置、契约改动

- 迁移（一项，`nx043_incentives`）：
  - IncentiveOffering / ConsumerIncentiveOffering 对象类型的形状与目录断言函数（仿 0053，不改 0053）；
  - `nexloop.incentive.issue` 的 admit / finalize 分支：在现有 effect 执行函数上以“改名保留 + 同名包装”的方式接入（不改已发布迁移），admit 内预留、拒绝写拒绝原因；
  - 结算与冲销：incentive 消耗的终态结算（仿 0120 D5）；
  - 每 Consumer 周期累计查询所需的索引；
  - 优惠类 scope denial 的记录形状。
- 部署配置：`business-actions.v1.json`（新 Action，manifest_version 递增）、`business_actions.py` PROFILES（`incentive.issue`）、`action-effect-categories.v1.json` v2（non_contact_service + `notification_text`）、`service-grants.v1.json`（执行者与回环 profile）、0124 registry 行（人类入口）。
- Agent Host：`runtime-effect-tools.ts` 新工具 `nexloop.incentive.issue`，`provider-tool-names.ts` 名单。
- 契约（`packages/contracts/`，需负责人或调度员批准后另行提交）：
  - 新 `incentive-offering.schema.json`（目录对象与绑定的读投影）；
  - `action-intent.schema.json` 增加 `nexloop.incentive.issue:1` 的参数形状；
  - scope denial 读投影（现由 WebChat `chat-api.ts` 固定校验 `local.json-export`）增加优惠类形状；
  - `context-pack-v6.schema.json` 的 `supply` 段是否列出该 Consumer 可用的优惠目录（让 Agent 只在目录内提出），见 D8。

## 7. 决定（已定案）

- **调度员裁定（2026-10-11）**：D1、D3、D6、D8 采用推荐；D8 的 `context-pack-v6` 契约改动获批，只列该客户绑定的、当前有效的目录项和剩余额度区间，不列预算余额。
- **负责人决定（2026-10-11，经调度员转达）**：D2、D4、D5、D7、D9，原话“全部按推荐”。
- D2（负责人 2026-10-11）：EIOS 风险 low、无审批；NexLoop 分类 medium，补偿控制为三重约束（目录单次上限、每 Consumer 周期上限、租户预算）。负责人 2026-10-11 原话“全部按推荐”（经调度员转达）：在 EIOS 里按 low 发布，不接审批流，不改 vendored ActionGovernor（它对非 low 风险一律要求审批证据）。
- D8 的实现没有改动契约形状：v6 的 `open_work` 条目本身是通用形状（subsection 为字符串、content 任意），优惠目录作为 `incentive` 子段的 formal 条目加入，由 SQL 在 bind 时重算（与 NX-026 承诺条目相同的方式）。

以下为原稿中的各项与推荐：


- **D1 Action 形态。** 推荐：独立 Action `nexloop.incentive.issue:1`，不复用 `nexloop.service.request`。理由：类别（不触达）、预算（incentive）、目录与参数都不同；复用会让服务请求可以夹带优惠，破坏 AT-010。
- **D2 风险级别与审批。** 推荐：`risk_level=medium`、`approval_mode=none`，靠目录上限、每 Consumer 周期上限与租户预算三重约束；替代：单次超过某金额时需要负责人审批（引入审批流，v0.1 不建议）。定案：EIOS 风险 low、无审批，NexLoop 分类 medium（见本节开头）。
- **D3 Role ceiling 是否增加优惠金额上限。** 推荐：v0.1 不改 ceiling 形状（现为精确 9 字段），以 `action_resources` 控制能否发放、以目录控制金额；替代：ceiling 增加 `incentive_limit_minor`，需要迁移 RoleExecutionCeiling 形状与现有对象。
- **D4 负责人能否在目录外单次发放。** 推荐：不能；负责人先经受治理 Action 在目录中新增或修改 IncentiveOffering，再发放，保证每笔优惠都有目录依据。
- **D5 口头许诺目录外优惠的处理。** 推荐：NX-026 登记为异常 `unauthorized_incentive_promise` 并提示负责人，不生成可履行的 Commitment，也不自动发放。
- **D6 失败时的费用冲销表示。** 推荐：追加一条负金额的 discount 条目（`basis=budget_release`），保持费用条目只追加、净额可加总；替代：不冲销，只在结算表标记 released（成本报表需另行扣减）。
- **D7 受限客户与 KR 驱动的主动发放。** 推荐：按 ADR-023 §3 视为不触达（无通知时可发）；但复评计划为完成 KR 而发起的发放，在客户联系受限时拒绝（与 AT-029 的精神一致），只有绑定客户来信的回复或负责人发起的发放可以对受限客户执行。
- **D8 上下文是否列出可用优惠目录。** 推荐：列出（只列该 Consumer 绑定的、当前有效的目录项与剩余额度区间，不列预算余额），让 Agent 只在目录内提出优惠；需要 `context-pack-v6` 契约改动。
- **D9 真实渠道。** 推荐：v0.1 不接；真实优惠渠道属于 B 类，另立 ADR 由负责人决定，届时补渠道对账与退款冲销。
