# NX-028 负责人工作台与人工接管：设计稿（待调度员审核，未实现）

分支 `nx028-design`，从 `nx026-impl` `af68908`（= s4g）切出。本稿只有文档：没有代码，也没有迁移；文中迁移号都是占位。NX-028 依赖 NX-027（L3 正在开工），实现等审核和依赖合入后再定。

依据：
- PRD M20（负责人工作台）；`10_UX_AND_WORKBENCH.md` §1–§6；`09_API_AND_EVENT_CONTRACTS.md` §3；`03_DOMAIN_DATA_MODEL.md`。
- ADR-020 §3（人类权限由负责人确认）、ADR-021 §1（人类接管与“处理中”在 NX-028 单独设计）、ADR-023（联系限制：只有负责人能解除；来信必回）。
- 已实现：NX-022 控制面、NX-024 计划复评、NX-025/ADR-023（0109/0110）、NX-026 承诺（0111–0113）、NX-046 审核工作台（`review_http.py`、`apps/web`）、NX-047 外发消息。
- 并行：NX-027 设计稿（`nx027-design` `7d4938e`）、NX-051 实现（`nx051-impl` `33b7f78`，迁移 0120/0121）。

交付物（planning）：所有薄切片页面，以及暂停、对账、证据的交互。
验收：
- AT-006 的界面部分：排队后暂停，界面状态与派发结果一致；
- AT-044：人类接管会话期间来了新消息，Agent 不竞争回复；交还后重新评估；
- AT-045：未知、加载中、403、部分数据都有准确标签，不出现空白的伪成功；
- 承接：AT-041 的“负责人可见”、AT-040 的界面表述、AT-003 的界面部分（无属性权限时字段与证据不可见）、M20 验收（暂停与恢复端到端；候选不显示为正式；模拟不显示为真实）。AT-070 已通过，保持不回退。

## 0. 结论先行

1. **工作台是人类的受治理界面，不是第二套权限。** 每个页面的读取都要求当前浏览器 Human 会话持有对应 Action 的 EXECUTE（与 NX-046 审核队列同一做法）；每个写入都是已有的受治理人类 Action，SQL 仍按 `subject_kind='human'` 拒绝服务主体和 Agent。界面只是入口。
2. **企业员工身份是新增的，需要负责人确认（D1、D2）。** 现在的浏览器 Human 只有一种：顾客本人。工作台需要“企业成员”身份，以及负责人 / 运营两个角色。
3. **人工接管是一个新的控制面状态（D3–D5）。** 接管期间：Agent 对该会话的外发在派发时被拒，消息中继不为该会话的新来信启动 Run，兜底回复 Run 不启动；员工的回复走与 Agent 同一条受治理外发账本；交还时推进控制 revision，触发计划复评，积压的来信不自动补发。
4. **联系限制、承诺、暂停、unknown 对账都已有后端，NX-028 补的是人类可用的读端口和 HTTP 入口。** 现有读端口（`nexloop.contact.read`、`nexloop.commitment.read`）只接受服务主体，需要新增接受人类会话的读路径。
5. **契约改动一处**：NX-051 的 `conversation-message` 契约把 `sender_kind` 固定为 `agent`，员工回复需要放宽为 `agent | human_takeover`（§10，先报调度员，审核通过后再改）。

## 1. 现状（已核对代码）

| 能力 | 后端 | 人类入口 | 缺口 |
|---|---|---|---|
| 浏览器登录、会话、CSRF | `browser_http.py`、`browser_sessions.py` | `apps/web` Login / Account | 只有顾客身份；没有企业成员身份与角色 |
| 审核队列（NX-046/044） | `review_http.py`、`ReviewServices` | `Review.tsx` | 已有，作为工作台第一个页面保留 |
| 目标、KR、指标、暂停/恢复、预算（NX-022） | `GoalGovernedActions`、`ControlPlane`、`authz.nexloop_goal_governed_action` | 无 HTTP | 读端口只按会话 tenant/world 过滤，未逐资源授权（NX-022 §6 已注明） |
| 计划、复评、Run 结果（NX-024） | `runtime.nexloop_plans` 等，`PlanPort`（服务主体） | 无 | 人类只读端口；“手动触发复评”入口 |
| 联系限制与解除（ADR-023） | 0109 限制表、命中证据、`nexloop.contact.read`（服务主体）、`goals.contact.release`（人类） | 无 | 人类读端口与解除入口 |
| 来信必回的升级记录 | `control.nexloop_reply_escalations` | 无 | 负责人可见 |
| 承诺（NX-026） | 0111 登记表、账本、异常；`nexloop.commitment.read`（服务主体）；五项人类 Action | 无 | 人类读端口与五项 Action 入口 |
| Action / unknown / 对账 | 0040/0042/0065 账本，`nexloop.service.receipt_reconcile`（服务主体执行） | 无 | 人类读端口；“查询执行结果”入口 |
| 会话与外发 | 0046/0079，NX-051 读投影 | WebChat（顾客） | 员工视角的会话视图；人工接管与员工回复 |
| 商业事件、费用、指标（NX-027） | L3 实现中 | — | 工作台只读呈现，依赖 NX-027 端口 |

## 2. 企业成员身份与角色（D1、D2）

- **身份**：沿用现有浏览器认证链（`BrowserBusinessSession`，密码登录、CSRF、会话吊销），新增“企业成员”成员资格：
  - 一个独立的浏览器业务应用 `workbench`（`control.nexloop_browser_business_applications` 中另一行），与顾客的 WebChat 应用分开；
  - 成员主体是 `subject_kind='human'` 的租户成员，由可信配置写入，不能自助注册；
  - 顾客主体在 SQL 中不能持有任何工作台 Action 的授权（配置校验拒绝）。
- **角色**（建议，D2）：

| 角色 | 读 | 写 |
|---|---|---|
| 负责人 owner | 全部页面 | 目标/指标/预算/暂停与恢复、解除联系限制、承诺五项 Action、人工接管与员工回复、手动复评、发起“查询执行结果” |
| 运营 operator | 除“设置与治理”外的全部页面 | 人工接管与员工回复、承诺的 attest / condition_met / 延期、发起“查询执行结果”、手动复评 |
| 审核人 reviewer | 知识工作台（NX-046） | 三种审核决定（NX-044） |

  - ADR-023 §2.4 规定“只有负责人能解除”联系限制，因此解除只给 owner。
  - 承诺的取消与标记为沟通类承诺建议只给 owner（取消会让违约不再可见；标记改变履约口径）。
  - 角色就是一组 Action 授权，按 NX-048 清单方式由可信配置下发；不在应用里另建角色表。

## 3. 页面与接口（v0.1 薄切片）

所有 HTTP 路由在 `/api/v1/workbench/...` 下，挂在生产 `create_app`，与审核路由同一套 Cookie + Host/Origin + CSRF（写）+ `Idempotency-Key`（写）规则。tenant、world 一律由会话推导。

| 页面 | 读（新的人类读端口） | 写（已有或新增的受治理人类 Action） |
|---|---|---|
| 总览 | 当前目标/KR（NX-022 compute）、未履行承诺数与异常、unknown 数与最老年龄、队列积压（work feed backlog）、联系限制数、接管中的会话、费用摘要（NX-027） | 进入对象；暂停 scope |
| 目标与对齐 | 目标树、版本、KR、指标定义、控制事件史 | `goals.version.publish`、`goals.metric.approve`、`goals.budget.set`；手动复评（新 `nexloop.plan.request_reevaluation`，写 T-manual 触发） |
| 消费者列表 / 详情 | Consumer 投影（逐属性 READ，AT-003）、联系限制与命中证据、开放承诺、计划、近期会话 | 暂停主动联系（`goals.control.set` consumer scope）、解除联系限制（owner） |
| 会话 | NX-051 投影（顺序、发送者、回执、引用）、对应 Run、提取标记、接管状态 | 开始/结束接管、员工回复（§4） |
| 计划与运行 | Plan 版本、触发记录、下一次复评、Run 结果 | 手动复评 |
| Action / 异常 | 意图、尝试、观察、unknown 原因、对账记录、派发被拒原因 | 查询执行结果（新 `nexloop.service.query_request`，只排队一次 QUERY，不重发） |
| 承诺 / 交付 | 承诺列表与详情：内容、责任方、条件、期限与精度、四栏证据、事件史、异常 | 五项人类 Action（§6） |
| 知识工作台 | NX-046 现有 | NX-044 现有 |
| 设置与治理 | 渠道状态、模型 profile 摘要、预算、保留期、成员与角色（只读） | 预算（已有）；保留/删除请求属 NX-029 |
| 本体/演进、实验空间 | — | 显示“未启用/下一阶段”，不提供可点击的发布 |

- **读端口**：新增 `authz.nexloop_workbench_read(digest, world, text, signature, payload)`，协议 `nexloop-workbench-read-v1`，只接受浏览器人类会话（与 0109 的服务主体端口相反），按动词要求不同的读 Action：
  - `overview` / `goals` → `nexloop.workbench.read:1`；
  - `contact` → `nexloop.contact.read:1`（同一 Action，人类会话也可持有）；
  - `commitments` → `nexloop.commitment.read:1`；
  - `actions` / `plans` / `conversations` → `nexloop.workbench.read:1`，会话与消息内容另需逐 Message READ（沿用 0077/0086 的派生或配置授权，不放宽）。
  - 读端口内部复用已有视图函数（`runtime.nexloop_commitment_view`、0109 的限制查询、NX-051 投影），不复制逻辑。
- **前端**：`apps/web` 按页面增加组件与 `workbench-api.ts`；沿用 react-query、无新框架；表格服务端分页，筛选写入 URL。

## 4. 人工接管（AT-044，D3–D5）

### 4.1 状态

`control.nexloop_takeovers`（新表，append-only 事件 + 当前行）：
- 字段：tenant, world, takeover_id, scope_kind（`conversation` | `consumer`）, scope_ref, taken_by, started_at, expires_at, ended_at, end_reason（`handback` | `expired` | `revoked`）, control_revision。
- 开始与结束都是受治理人类 Action（`conversation.takeover` / `conversation.handback`，经 NX-022 governed 入口），同事务写控制事件 `takeover` / `handback` 并推进控制 revision（`control_events.event_kind` CHECK 放宽）。
- 到期由 keeper 类后台（复用 reply-guarantor 进程，或新的轻量 feed）结束，写 `expired`，同样推进 revision。

### 4.2 接管期间

- **Agent 不竞争回复**：
  - 派发：包装 0112 的 `control.nexloop_contact_assert_intent`（改名保留 + 新包装），接管覆盖该 consumer 或会话时，非员工的意图一律拒绝（新 SQLSTATE `NXC06 taken_over`），provider 收到零请求。
  - 控制 revision 推进，所以接管前已排队的 Agent 意图同样因 NXC02 被拒，不会在接管期间漏发。
  - 消息中继：来信照常持久化、照常登记待回复，但不为接管中的会话签发新的消息 Run（中继在路由前读接管状态，返回 `taken_over`，不算失败）。
  - 兜底回复（ADR-023 §2.7）：接管期间不启动兜底 Run；待回复由员工回复结清。
- **员工回复**：受治理人类 Action `Message.staff_send:1`，走与 Agent 相同的 effect 意图与外发账本：
  - 外发记录 `sender_kind='human_takeover'`、`sender_principal`=员工主体、`trigger_message_id` 由服务端从界面选中的来信推导（同会话、同 consumer），没有 Run；
  - 0079 的 `sender_kind` CHECK 与“agent 必有 Run”约束需要放宽（新迁移改约束，不改已发布文件）；
  - 投递、物化、READ 派生、提取都沿用 NX-047；提取时员工消息也是企业一方（speaker=agent），其中的承诺同样登记为 Commitment（made_by 记员工）；
  - 联系限制下：员工回复同样只能是绑定来信的回复（ADR-023 §2.6 不因人类而放宽；要主动联系只能先由负责人解除限制）。
- **顾客视图**（D4）：会话顶部显示“人工客服处理中”的状态条（UX §4 要求的人工接管状态），不显示逐条“处理中”占位，与 ADR-021 对 Agent 消息的决定一致。

### 4.3 交还

- 交还推进控制 revision；NX-024 T3 让该 consumer 的计划进入复评，复评 Run 在 v6 中读到接管期间的对话（含员工回复），自行判断。
- **不补发积压回复**（UX §3）：接管期间的来信视为已由人工处理；交还时仍未结清的来信，只对最后一条恢复正常的待回复处理（Agent 或兜底），更早的记入接管记录并结清（D5）。
- 到期未交还同样处理，并给负责人一条升级记录。

## 5. 联系限制的查看与解除（ADR-023）

- 消费者列表与详情显示限制状态、原因、规则版本、命中原文片段、限制时间、控制 revision；总览显示受限数量。
- 命中原文片段属于消费者消息内容：只有同时持有该 Message READ 的人能看到原文，否则显示“已命中规则 `<rule_id>`（原文需授权）”。
- 解除：owner 在详情页填写原因，调用 `goals.contact.release`（已有，SQL 只认人类）；成功后显示新的控制 revision 和“之前被拒的意图不会重放，计划将重新评估”。
- 来信必回的升级记录（`fallback_failed` / `fallback_unanswered` / `fallback_unavailable`）在总览与会话页可见，含证据。

## 6. 承诺页面与五项人类 Action（NX-026）

- 列表：开放（conditional / open / in_progress / breached）与已结束分栏；每行显示状态、期限与精度（`latest_bound` 显示“最晚界，推导值”）、条件、责任方、异常标记。
- 详情：承诺原文（来源消息仍存在且有 READ 时）、四栏证据（请求创建 / 已交付 / 客户确认 / 问题解决——第四栏显示“不可用”，D5 of NX-026）、事件史、异常。
- 文案约束（AT-040）：“消息已送达”只出现在“已交付”栏，并注明“不代表承诺已兑现”；状态只取对象 `status`，界面不自行推断。
- 五项 Action：取消（原因必填）、延期（新期限 + 原因，展示“将新建承诺并取代旧承诺”）、确认履约 attest（发生时间 + 原因）、条件已满足、标记为沟通类承诺（展示“只对标记之后送达的消息生效”）。按钮只对状态允许的承诺可用；SQL 仍会拒绝不允许的转移，界面显示服务端返回的原因。
- 异常（`breached`、`blocked_by_contact_restriction`、`condition_unresolved_at_due`、`no_due_date`、`no_active_plan`、`made_under_contact_restriction`、`source_superseded`、`source_deleted`、`schema_gap`）在总览聚合，可进入对应承诺（AT-041 的“负责人可见”）。

## 7. AT-006 的界面部分（暂停）

- 暂停：表单明确 scope（tenant / role / consumer / strategy / action_type）、原因、期限；提交后显示新的控制 revision，并提示“已提交的外部动作不会自动撤销”（UX §3）。
- 一致性：Action 页对每个排队意图显示派发预判，由 SQL 读端口计算，与派发检查同一函数：
  - 快照 revision 早于影响它的控制事件 → “控制已变更，派发时将被拒，等待复评”；
  - 受暂停影响 → “已暂停”；
  - 联系限制 / 接管 → 对应原因。
- 验收测试：暂停后，界面读取的预判与随后真实派发的结果（`admission_unavailable`、provider 零请求）一致；恢复后旧意图仍显示“需复评”，复评后的新意图显示可派发并真实派发一次。

## 8. Action / unknown 与对账

- 默认动作是“查询执行结果”，不是“重发”（UX §3）：新人类 Action `nexloop.service.query_request:1` 只把该意图标记为待 QUERY（复用 0065 的对账路径，由执行器服务执行），不换幂等键，不重发。
- 页面显示已尝试的核对证据（attempt、observation、对账记录）；补偿不在 v0.1（ADR-021 §1：不支持撤回）。

## 9. 页面状态（AT-045）

- 每个页面统一处理 loading、empty、partial、stale、forbidden（403）、unauthenticated（401）、unavailable（503）、invalid（响应格式不符）、unknown（业务状态未知），沿用 NX-046 的口径与文案风格。
- 部分数据：读端口按区块返回 `{status: 'ok'|'forbidden'|'unavailable', data}`，前端逐块渲染，不因一块失败而整页空白，也不把缺失的块显示为“0”或“无”。
- unknown 业务状态显示“待核对”，不用红色失败。

## 10. 契约提案（先报调度员，审核通过后再改）

- `packages/contracts/conversation-message.schema.json`（NX-051 新建）：`sender_kind` 由 `const: agent` 改为 `enum: [agent, human_takeover]`；`human_takeover` 时 `intent_id`、`trigger_message_id` 仍必填。生成产物同步重新生成。
- 工作台读接口：建议新增 `workbench-*.schema.json` 一组只读投影契约（总览、承诺、限制、意图预判），或先只在 OpenAPI 组件中描述。请调度员决定是否进入 `packages/contracts`（D8）。

## 11. 迁移形状（占位编号，接在 NX-027 与 NX-051 合入后的最高号之后）

1. `nx028_staff_identity`：`workbench` 业务应用与成员资格的配置校验（顾客主体不得持有工作台授权）。
2. `nx028_takeover`：接管表与事件；控制事件 CHECK 放宽（`takeover` / `handback`）；`goal_governed_action` 增加 `conversation.takeover` / `conversation.handback` / `message.staff_send` / `plan.request_reevaluation` / `service.query_request`；`contact_assert_intent` 再包一层（NXC06）；消息中继路由检查；兜底回复跳过；0079 的 `sender_kind` 约束放宽与员工外发写入路径。
3. `nx028_workbench_read`：人类读端口与派发预判函数。

已发布迁移不改；替换一律用改名保留 + 新包装，或以最新函数体 create or replace。

## 12. 测试清单（真实 PG、合成数据；HTTP 用真实登录 Cookie；真实 Pi 端到端单独串行）

身份与权限：
1. 企业成员登录后按角色看到页面；顾客主体登录工作台全部 403；运营调用 owner 专属动作（解除限制、承诺取消）被 SQL 拒绝。
2. 授权撤销后下一次请求立即 403（每次请求重新做 PG 认证，同 NX-046）。

AT-006（界面）：
3. 排队意图 → 暂停 consumer → Action 页预判“已暂停”→ 真实派发 `admission_unavailable`、provider 零请求；恢复后预判“需复评”；复评后的新意图真实派发一次。

AT-044（接管）：
4. 接管会话 → 顾客发新消息（真实 HTTP）→ 不签发消息 Run、不启动兜底；Agent 的旧意图与新意图派发被拒（NXC06 / NXC02），零请求。
5. 员工回复：绑定来信、经账本投递、物化为 `human_takeover` Message、顾客可见；结清待回复。
6. 受限客户被接管：员工绑定回复可发；员工主动外发被拒。
7. 交还 → 计划进入复评（T3）；接管期间的较早来信不补发，最后一条未结清来信按正常路径处理。
8. 接管到期 → 自动结束、升级记录、同 7。
9. 真实 Host/Pi 端到端：接管期间 Pi 被故意要求回复 → 派发拒绝，provider 零请求（单独串行，guard 4 进程）。

联系限制与承诺：
10. 限制列表、命中证据（有/无 Message READ 两种显示）、owner 解除后状态与控制 revision；运营解除被拒。
11. 承诺列表与详情的四栏证据、异常聚合；五项 Action 的正例与状态不允许时的服务端拒绝；“消息已送达”不出现在状态位置（AT-040 界面）。
12. 到期违约后总览出现异常，进入详情可见违约事件（AT-041 界面）。

AT-045 与 AT-003：
13. 每个页面的 401 / 403 / 503 / 格式无效 / 空 / 部分数据 / 待核对各一例（vitest + 真实 HTTP）。
14. 无属性 READ 的字段与证据在详情与导出中不可见。

M20 其他：
15. 候选定义在知识工作台标“候选”，不出现在本体正式视图；test / simulation 数据在总览与指标中带标签，不计入 real（依赖 NX-027 端口）。
16. 手动复评与“查询执行结果”各只排队一次，重放幂等，不重发外部动作。

## 13. 与现有分支的文件重叠

| 文件 / 对象 | 重叠分支 | 处理 |
|---|---|---|
| `authz.nexloop_goal_governed_action` | NX-027（L3，连接器配置的人类 Action 也走这里，设计 §10 计划改名包装）；NX-026（0111 已重写） | 冲突风险最高：两条线都要加 capability。建议 NX-027 先合入，NX-028 在其最新函数体上加；或由调度员指定只改名包装一次、两线都往同一新包装里加 capability（D9） |
| `runtime.nexloop_work_feed` CHECK 与 `authz.nexloop_work_feed` | NX-027（`commercial-raw` / `commercial-verified`）；NX-026（0111） | 接管到期若用新 feed 也要改；按合入顺序取并集 |
| `control.nexloop_control_events.event_kind` CHECK | NX-027 未改；0109 改过 | 本任务放宽，取并集 |
| `control.nexloop_contact_assert_intent` | NX-026 0112 已包装；NX-051 明确未改 | 再包一层（NXC06） |
| 0079 外发记录约束、外发写入 | NX-051 的 constraint trigger `nx051_message_provider_default` 为外发写 `nexloop.agent` 事实并导出 reply_to | 员工外发需要新命名空间（建议 `nexloop.staff`，server 级）与同样的 reply_to 导出；需要改 NX-051 的 trigger 逻辑 → 在 NX-051 合入后以其最新函数体修改 |
| `packages/contracts/conversation-message.schema.json` 与生成产物 | NX-051 新建 | §10 契约提案 |
| `conversation_messages.py`、`web_chat_http.py`、`apps/web/src/chat-api.ts`、`WebChat.tsx`、`native-message.ts` | NX-051 修改 | 顾客端接管状态条与员工消息显示，在 NX-051 合入后基于其版本修改 |
| `message_relay.py` / 中继路由 SQL | 无未合入分支 | 接管检查 |
| `reply_fallback.py`、`contact_restrictions.py`（ReplyGuaranteeWorker） | 无未合入分支 | 接管期间不启动兜底 |
| NX-027 的商业记录待关联列表、指标/费用读端口 | NX-027 | 工作台只读呈现，调用其端口；待关联列表的人工关联 Action 归 NX-027 还是 NX-028 需定（D10） |
| `http_api.py`、`backend.py`（新增 `WorkbenchServices`） | NX-051 改了 `backend.py`（5 行） | 文本冲突，语义不冲突 |
| `apps/web`（App、Account 导航、styles） | NX-051 改了 web 测试；NX-046 已有 Review | 追加 |
| `deploy/authorization/service-grants.v1.json`、`business-actions.v1.json` | NX-027（commercial_recorder 等） | manifest_version 递增，按合入顺序 |
| `planning/*` | 调度员 | 本线不改 |

## 14. 需要负责人 / 调度员决定的事（附推荐）

- **D1 企业成员身份**：推荐沿用浏览器密码登录链，新增独立的 `workbench` 业务应用和企业成员资格，由可信配置写入，不能自助注册；顾客主体永远不能持有工作台授权。备选：外部 SSO（v0.1 不推荐，增加依赖）。
- **D2 角色划分**：推荐 owner / operator / reviewer 三个角色（§2 表）；解除联系限制、承诺取消、标记沟通类承诺、目标/预算/暂停只给 owner；运营可接管、员工回复、attest、condition_met、延期、查询执行结果、手动复评。
- **D3 接管粒度与默认期限**：推荐以会话为单位（也允许整个 consumer），默认期限 2 小时、上限 24 小时，写入版本化配置；到期自动交还并升级。
- **D4 顾客视图**：推荐显示“人工客服处理中”状态条，不显示逐条“处理中”占位（与 ADR-021 一致）。
- **D5 交还时的积压来信**：推荐只对最后一条未结清来信恢复正常待回复处理，更早的记入接管记录并结清，不补发；计划复评负责后续。
- **D6 员工回复与联系限制**：推荐员工同样受 ADR-023 §2.6 约束（只能绑定来信回复），主动联系需负责人先解除限制。
- **D7 “查询执行结果”的执行者**：推荐由人类发起、执行器服务执行一次 QUERY（复用 0065），人类不直接持有对账 Action。
- **D8 工作台读接口契约**：是否为工作台只读投影新增 `packages/contracts` 契约（推荐：v0.1 先只对会被前端以外调用的承诺与限制投影建契约，其余在 OpenAPI 组件描述）。
- **D9 `goal_governed_action` 的扩展方式**：推荐调度员指定一次“改名保留 + 新包装 + capability 注册表”，NX-027 与 NX-028 都只往注册表加行，避免两线反复重写同一函数。
- **D10 商业记录待关联列表的人工关联**：推荐 Action 归 NX-027（业务语义在那边），NX-028 只提供页面入口。

## 15. 实现切片建议（NX-027、NX-051 合入后）

1. 身份与读端口：企业成员与角色、`workbench_read`、总览/消费者/承诺/限制/计划/Action 只读页面，AT-045 页面状态。
2. 写入口：暂停与恢复（AT-006 界面）、解除限制、承诺五项 Action、手动复评、查询执行结果。
3. 人工接管：接管状态、派发拒绝、中继与兜底跳过、员工回复（含契约放宽）、交还复评、顾客状态条，AT-044 与真实 Pi 端到端。
