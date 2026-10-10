# NX-028 负责人工作台与人工接管：设计稿（D1–D10 全部定案；未实现）

分支 `nx028-design`，从 `nx026-impl` `af68908`（= s4g）切出。本稿只有文档：没有代码，也没有迁移；文中迁移号都是占位。NX-028 依赖 NX-027（L3 正在开工），实现等审核和依赖合入后再定。

依据：
- PRD M20（负责人工作台）；`10_UX_AND_WORKBENCH.md` §1–§6；`09_API_AND_EVENT_CONTRACTS.md` §3；`03_DOMAIN_DATA_MODEL.md`。
- ADR-020 §3（人类权限由负责人确认）、ADR-021 §1（人类接管与“处理中”在 NX-028 单独设计）、ADR-023（联系限制：只有负责人能解除；来信必回）。
- 已实现：NX-022 控制面、NX-024 计划复评、NX-025/ADR-023（0109/0110）、NX-026 承诺（0111–0113）、NX-046 审核工作台（`review_http.py`、`apps/web`）、NX-047 外发消息。
- 并行：NX-027 设计稿（`nx027-design` `7d4938e`，L3 基于 af68908 实现中）。NX-051 已进入集成 s4h（`dispatch/integration-s4h` `32c914d` = `nx026-impl` `080b6c9` + NX-051，迁移重排为 0114/0115）。

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
  - **实现时的调度员裁定（方案 B，v0.1）**：0042 的派发要求意图带有效 origin Run，人类会话没有 Run，上面“同一账本”不能直接成立。v0.1 只限原生 WebChat 会话（WebChat 即渠道），受治理人类 Action 直接写 Message 与会话流，同一事务结清所绑定来信的待回复；外部渠道（非 WebChat）一律拒绝（NXC07），接入时改走**方案 A**（接管时签发“接管 Run”，经 effect 账本投递）。实现见 `NX-028-implementation.md` 与 0153 注释。
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

## 10. 契约（D8 调度员裁定 2026-10-10）

现状（s4h）：`packages/contracts` 下已有 `conversation-message.schema.json`（NX-051 新建，读接口的消息投影），其中 `sender_kind` 为 `const: agent`，外发消息必须带 `sender_kind`、`intent_id`、`trigger_message_id`；生成产物 `generated/contracts.ts`、`generated/openapi-components.json` 与 `nexloop_eios/contracts.py` 已包含它。

NX-028 实现时随任务一起改（已获调度员同意）：
- `conversation-message.schema.json`：`sender_kind` 改为 `enum: [agent, human_takeover]`；`human_takeover` 时 `intent_id`、`trigger_message_id` 仍必填；同步重新生成三处产物，并更新 `apps/web` `chat-api` 的校验与测试。
- 新增两个只读投影契约：`commitment-view.schema.json`（承诺：属性、四栏证据、事件、异常、`source_available`）与 `contact-restriction-view.schema.json`（限制、命中证据、升级记录）。其余工作台读接口（总览、目标、计划、Action 预判、会话视图）只在 OpenAPI 组件中描述，不进入 `packages/contracts`。

## 11. 迁移形状（占位编号：接在 s4h 的 0115 之后，从 0116 起；NX-027 若先合入，按合入顺序由调度员重排）

1. `0116_nx028_governed_entry`（D9 调度员裁定）：`authz.nexloop_goal_governed_action` 只做这一次“改名保留 + 新包装 + capability 注册表”。
   - 当前函数（0111 函数体）改名为 `authz.nexloop_goal_governed_action_before_registry_v0115`，收回权限；
   - 新表 `control.nexloop_governed_capabilities`(capability, operation, handler, subject_rule)，owner-only、append-only，种入现有 13 项（goals.* 6 项、goals.contact.release、commitment.* 5 项）与 NX-028 新增项；
   - 新包装按注册表校验 operation 与人类/Agent 规则，再分派到 handler（白名单函数名，只允许 owner 拥有的 `control.` / `runtime.` 函数）；
   - NX-027 及之后的任务只往注册表加行，不再重写入口函数。
2. `0117_nx028_staff_identity`：`workbench` 业务应用与企业成员资格的配置校验（顾客主体不得持有工作台授权）。
3. `0118_nx028_takeover`：接管表与事件；控制事件 CHECK 放宽（`takeover` / `handback`）；注册表新增 `conversation.takeover` / `conversation.handback` / `message.staff_send` / `plan.request_reevaluation` / `service.query_request`；`control.nexloop_contact_assert_intent` 再包一层（NXC06，基于 0112 的包装）；消息中继路由检查；兜底回复跳过；0079 `sender_kind` 约束放宽与员工外发写入路径；0114 外发默认事实 trigger（`runtime.nexloop_message_provider_default`）以其最新函数体修改，为员工外发写 `nexloop.staff` 命名空间（server 级，种入 `control.nexloop_provider_namespaces`）并同样导出 reply_to。
4. `0119_nx028_workbench_read`：人类读端口与派发预判函数。

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
| `authz.nexloop_goal_governed_action` | NX-027（L3）；NX-026（0111 函数体为当前最新） | D9 裁定：NX-028 在 0116 做唯一一次改名包装与注册表；NX-027 只往注册表加行（调度员同步给 L3）。若 NX-027 先合入且已有自己的入口改动，0116 以其最新函数体为改名对象 |
| `runtime.nexloop_work_feed` CHECK 与 `authz.nexloop_work_feed` | NX-027（`commercial-raw` / `commercial-verified`）；NX-026（0111） | 接管到期若用新 feed 也要改；按合入顺序取并集 |
| `control.nexloop_control_events.event_kind` CHECK | NX-027 未改；0109 改过 | 本任务放宽，取并集 |
| `control.nexloop_contact_assert_intent` | NX-026 0112 已包装；NX-051 明确未改 | 再包一层（NXC06） |
| 0079 外发记录约束、外发写入；0114 `runtime.nexloop_message_provider_default`（deferred constraint trigger `nx051_message_provider_default`）与 `control.nexloop_provider_namespaces` | NX-051 已合入 s4h（0114/0115） | 员工外发新增命名空间 `nexloop.staff`（server 级，append-only 种入），在 0118 以 0114 最新函数体 create or replace 默认事实函数：员工外发写 `nexloop.staff` 事实并以服务端 `trigger_message_id` 导出 reply_to |
| 0114 `authz.nexloop_conversation_messages_projection`、`runtime.nexloop_message_projection`、`authz.nexloop_message_provider_read` | NX-051 已合入 | 员工视角会话视图直接复用投影；投影对外发只认 agent，需要在 0118 放宽为同时认 human_takeover |
| 0115 提取更正顺序（`_v0069` 改名包装） | NX-051 已合入 | 不涉及 |
| `packages/contracts/conversation-message.schema.json` 与生成产物（`generated/contracts.ts`、`generated/openapi-components.json`、`nexloop_eios/contracts.py`） | NX-051 新建，已在 s4h | §10：sender_kind 放宽随 NX-028 改；新增 commitment-view / contact-restriction-view 两个契约 |
| `conversation_messages.py`、`native_web_inbound.py`、`web_chat_http.py`、`apps/web/src/chat-api.ts`、`WebChat.tsx`、`native-message.ts` | NX-051 已合入 s4h | 顾客端接管状态条与员工消息显示，基于 s4h 版本修改 |
| `message_relay.py` / 中继路由 SQL | 无未合入分支 | 接管检查 |
| `reply_fallback.py`、`contact_restrictions.py`（ReplyGuaranteeWorker） | 无未合入分支 | 接管期间不启动兜底 |
| NX-027 的商业记录待关联列表、指标/费用读端口 | NX-027 | 工作台只读呈现，调用其端口；待关联列表的人工关联 Action 归 NX-027 还是 NX-028 需定（D10） |
| `http_api.py`、`backend.py`（新增 `WorkbenchServices`） | NX-051 改了 `backend.py`（已在 s4h） | 基于 s4h 版本追加 |
| `apps/web`（App、Account 导航、styles） | NX-051 改了 web 测试；NX-046 已有 Review | 追加 |
| `deploy/authorization/service-grants.v1.json`、`business-actions.v1.json` | NX-027（commercial_recorder 等） | manifest_version 递增，按合入顺序 |
| `planning/*` | 调度员 | 本线不改 |

## 14. 决定事项（全部定案）

负责人 2026-10-10 对 D1–D6 的回复（调度员转达）：“全部按推荐”。D7–D10 由调度员 2026-10-10 裁定。以下保留原推荐文字，即为定案内容。


- **D1 企业成员身份**：**已定（负责人 2026-10-10，“全部按推荐”）**：推荐沿用浏览器密码登录链，新增独立的 `workbench` 业务应用和企业成员资格，由可信配置写入，不能自助注册；顾客主体永远不能持有工作台授权。备选：外部 SSO（v0.1 不推荐，增加依赖）。
- **D2 角色划分**：**已定（负责人 2026-10-10，“全部按推荐”）**：推荐 owner / operator / reviewer 三个角色（§2 表）；解除联系限制、承诺取消、标记沟通类承诺、目标/预算/暂停只给 owner；运营可接管、员工回复、attest、condition_met、延期、查询执行结果、手动复评。
- **D3 接管粒度与默认期限**：**已定（负责人 2026-10-10，“全部按推荐”）**：推荐以会话为单位（也允许整个 consumer），默认期限 2 小时、上限 24 小时，写入版本化配置；到期自动交还并升级。
- **D4 顾客视图**：**已定（负责人 2026-10-10，“全部按推荐”）**：推荐显示“人工客服处理中”状态条，不显示逐条“处理中”占位（与 ADR-021 一致）。
- **D5 交还时的积压来信**：**已定（负责人 2026-10-10，“全部按推荐”）**：推荐只对最后一条未结清来信恢复正常待回复处理，更早的记入接管记录并结清，不补发；计划复评负责后续。
- **D6 员工回复与联系限制**：**已定（负责人 2026-10-10，“全部按推荐”）**：推荐员工同样受 ADR-023 §2.6 约束（只能绑定来信回复），主动联系需负责人先解除限制。
- **D7 “查询执行结果”的执行者**：**已裁定（调度员 2026-10-10）**：人类发起，执行器服务执行一次 QUERY（复用 0065）；人类不持有对账 Action。
- **D8 工作台读接口契约**：**已裁定**：v0.1 只为承诺和联系限制两个投影建 `packages/contracts` 契约，其余放在 OpenAPI 组件；`conversation-message` 的 `sender_kind` 放宽为 `[agent, human_takeover]`，随 NX-028 实现一起改（§10）。
- **D9 `goal_governed_action` 的扩展方式**：**已裁定**：只做一次“改名保留 + 新包装 + capability 注册表”，由 NX-028 实现时做（§11 第 1 项）；NX-027 及之后的任务只往注册表加行。
- **D10 商业记录待关联列表的人工关联**：**已裁定**：Action 归 NX-027，NX-028 只提供页面入口。


## 15. 实现切片与两条线的边界（s4h 合入后由调度员分派；拟 L2 做切片 1，L4 做切片 2、3）

### 15.1 迁移归属（占位号，接在 0115 之后）

| 占位号 | 归属 | 内容 |
|---|---|---|
| 0116 `nx028_governed_entry` | 切片 2（L4） | D9 统一入口注册表；派发预判函数 `control.nexloop_intent_dispatch_prediction(tenant,world,intent)` 首版（暂停 / 控制 revision / 联系限制 / 效果类别，复用派发检查同一组函数，只读不抛错，返回 `{dispatchable, reason, detail}`）；`nexloop.plan.request_reevaluation`、`nexloop.service.query_request` 两项注册与处理函数 |
| 0117 `nx028_staff_identity` | 切片 1（L2） | `workbench` 业务应用、企业成员资格、顾客主体不得持有工作台授权的配置校验 |
| 0118 `nx028_takeover` | 切片 3（L4） | 接管状态与事件、注册表加接管/交还/员工回复三项、NXC06 派发拒绝、中继与兜底跳过、0079 约束放宽与员工外发、0114 默认事实函数加 `nexloop.staff`；以 create or replace 让 0116 的预判函数认得 `taken_over` |
| 0119 `nx028_workbench_read` | 切片 1（L2） | `authz.nexloop_workbench_read`（人类会话、按动词要求读 Action）；各动词复用已有视图函数与 0116 的预判函数 |

- 切片 1 的 0119 依赖 0116 的预判函数签名；签名在 0116 首版定下后不改，切片 3 只换函数体。若 L2 先于 0116 开工，可先按 §15.2 的签名写读端口与测试，合并时以 0116 为准。
- 两条线都不改对方的迁移文件；临时号冲突由调度员合并时重排。

### 15.2 接口约定（两条线共同遵守）

1. **Action 名与角色**（可信配置，一处定义）：
   - 读：`nexloop.workbench.read:1`、`nexloop.contact.read:1`、`nexloop.commitment.read:1`；
   - 写：`goals.*`（已有）、`goals.contact.release`（已有，owner）、`commitment.*` 五项（已有）、`nexloop.plan.request_reevaluation:1`、`nexloop.service.query_request:1`（切片 2 新增）、`conversation.takeover:1`、`conversation.handback:1`、`message.staff_send:1`（切片 3 新增）；
   - 角色到 Action 的映射（owner / operator / reviewer，§2 表）由切片 1 写进部署清单；切片 2、3 只新增 Action 定义，不改映射以外的角色逻辑。
2. **预判函数**：`control.nexloop_intent_dispatch_prediction(p_tenant text, p_world text, p_intent uuid) returns jsonb`，返回 `{"dispatchable": bool, "reason": null | "control_paused" | "control_revision_stale" | "goal_version_stale" | "object_revision_stale" | "contact_restricted" | "attached_notification" | "taken_over", "detail": {...}}`，owner-only，SECURITY DEFINER，不抛异常。
3. **读端口动词与返回形状**（切片 1 实现，切片 2、3 的写入只需在返回中出现对应字段）：`overview`、`goals`、`consumers`、`consumer`、`conversation`、`plans`、`actions`（每个意图带预判）、`commitments` / `commitment`（`commitment-view` 契约）、`contact`（`contact-restriction-view` 契约）、`takeovers`（切片 3 加表后返回内容，此前返回空列表 + `status:'unavailable'`）。
4. **HTTP 路由**：
   - 切片 1：`GET /api/v1/workbench/*`（`workbench_http.py`）；
   - 切片 2、3：`POST /api/v1/workbench/actions/{operation}`（`workbench_actions_http.py`，CSRF + Idempotency-Key，正文即受治理 Action 载荷，服务端补 request_id），返回 `{outcome_id, operation, ...}` 或固定错误码（`forbidden`、`not_allowed_in_state`、`conflict`、`unavailable`）。
5. **Python 服务**：切片 1 新增 `Backend.authenticate_workbench(inspected_session)` → `WorkbenchServices`（只读）；切片 2、3 新增 `WorkbenchActions`（同一认证入口返回，写入走 `GoalGovernedActions` 与新 Action 适配器）。两类放在不同模块。
6. **前端**：
   - 切片 1：工作台壳与导航、`apps/web/src/workbench/api.ts`（读客户端与 AT-045 状态模型）、各页面只读组件 `apps/web/src/workbench/pages/*.tsx`；每个页面预留 `actions` 插槽（`ReactNode`），默认不渲染；
   - 切片 2、3：`apps/web/src/workbench/actions/*.tsx` 与 `actions-api.ts`，通过插槽挂到页面；顾客端接管状态条（`WebChat.tsx`、`chat-api.ts`）归切片 3；
   - 共享的 `styles.css` 只追加，不改已有规则。

### 15.2a 已定签名（切片 2 首个提交，临时迁移 0150，对应设计占位 0116）

1. **统一入口注册表**（D9）：`control.nexloop_governed_capabilities`
   - 列：`capability text primary key`（如 `goals.control.set`）、`operation text unique`（载荷 `operation` 必须等于它）、`subject_rule text`（`human` | `agent_or_human`）、`handler regprocedure`、`source_task text`（`NX-0xx`）、`registered_at`；
   - owner-only、append-only；插入触发器要求 handler 是 `nexloop_owner` 拥有、位于 `control` 或 `runtime` schema、签名恰为 `(p_tenant text, p_world text, p_principal text, p_subject text, p_intent text, body jsonb) returns jsonb` 的非集合函数；
   - `authz.nexloop_goal_governed_action` 已改名保留为 `authz.nexloop_goal_governed_action_before_registry_v0115`（无授权），新包装按注册表校验 operation 与主体规则，再以 `select <handler>(tenant, world, principal, subject_kind, intent, body)` 调用；签名、许可、已发布合同、实时身份、claim 栅栏与提交尾校验与 0111 函数体相同；
   - 已种入 13 行：goals.* 6 项、goals.contact.release、commitment.* 5 项（handler 为对原函数的薄适配），以及 NX-028 的 `plan.request_reevaluation`、`service.query_request`；
   - **其他任务（NX-027 起）怎么加**：在自己的迁移里新建 handler 函数（上述签名、owner 为 `nexloop_owner`、撤销 PUBLIC），再 `insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values(...)`；Python 侧在 `goal_controls.CAPABILITIES` 加 operation → capability，并加提交方法；不再改入口函数。
2. **派发预判**：`control.nexloop_intent_dispatch_prediction(p_tenant text, p_world text, p_intent uuid) returns jsonb`，owner-only，SECURITY DEFINER，不抛异常、不写入（检查在一个总会回滚的子事务里运行）。返回 `{"dispatchable": bool, "reason": ..., "detail": {...}}`，`reason` 取值：`null`（可派发）、`not_queued`（不在排队：意图非 accepted 或 outbox 不在 pending/leased）、`control_paused`、`control_revision_stale`（含快照缺失）、`goal_version_stale`、`object_revision_stale`、`contact_restricted`、`attached_notification`、`taken_over`（切片 3 起）、`unavailable`（意图不存在或其他错误）。检查逻辑：0097 最新快照 → `control.nexloop_dispatch_controls_check`（0068 派发检查的无身份版本，规则相同）→ `control.nexloop_contact_assert_intent`（0109/0110/0112 链）。

### 15.3 文件边界

| 文件 / 目录 | 切片 1（L2） | 切片 2、3（L4） |
|---|---|---|
| 0116、0118 | — | 独占 |
| 0117、0119 | 独占 | — |
| `packages/contracts/commitment-view.schema.json`、`contact-restriction-view.schema.json` 与生成产物 | 独占 | — |
| `packages/contracts/conversation-message.schema.json`（sender_kind 放宽）与生成产物 | — | 独占（切片 3）；生成产物文本冲突按合并顺序重生成 |
| `workbench_http.py`、`WorkbenchServices` | 独占 | — |
| `workbench_actions_http.py`、`WorkbenchActions`、`goal_controls.py` 新方法 | — | 独占 |
| `http_api.py`（挂路由）、`backend.py`（认证入口） | 新增读路由与 `authenticate_workbench` | 只在同一入口追加写路由与 `WorkbenchActions` |
| `deploy/authorization/service-grants.v1.json`、成员/角色清单 | 独占（角色映射） | — |
| `deploy/configuration/business-actions.v1.json` | — | 新增五项 Action 声明 |
| `deploy/configuration/takeover.v1.json`（D3 期限） | — | 独占（切片 3） |
| `message_relay.py`、`contact_restrictions.py`、`reply_fallback.py` | — | 独占（切片 3） |
| `apps/web/src/workbench/pages/*`、`workbench/api.ts`、`App.tsx`/`Account.tsx` 导航 | 独占 | — |
| `apps/web/src/workbench/actions/*`、`actions-api.ts`、`WebChat.tsx`、`chat-api.ts` | — | 独占 |
| 测试 | `test_workbench_read_*`、`apps/web/test/workbench-*.test.ts` | `test_workbench_actions_*`、`test_takeover_*`（含真实 Pi 端到端，单独串行） |

### 15.4 切片内容与验收

1. **切片 1 身份与读端口（L2）**：企业成员与角色；读端口与只读页面；AT-045 页面状态；AT-003 界面部分；两个投影契约。测试 §12 第 1、2、13、14 项与 15 项的只读部分。
2. **切片 2 写入口（L4）**：D9 统一入口注册表；派发预判；暂停与恢复（AT-006 界面）；解除联系限制；承诺五项 Action；手动复评；查询执行结果。测试 §12 第 3、10、11、12、16 项。
3. **切片 3 人工接管（L4）**：接管状态与到期、NXC06、中继与兜底跳过、员工回复（含契约放宽）、交还复评、顾客状态条。测试 §12 第 4–9 项（第 9 项真实 Host/Pi，guard 4 进程，单独串行）。

切片 2、3 的页面联调在切片 1 合入后进行；此前两条线各自用 HTTP 与 SQL 测试验收。
