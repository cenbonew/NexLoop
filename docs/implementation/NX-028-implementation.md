# NX-028 负责人工作台与人工接管：实现说明（L4：切片 2、3）

设计稿 `NX-028-design.md`（D1–D10 全部定案）。分支 `nx028-s23`，从 main `036c640` 切出，带上设计稿提交。临时迁移号 0150–0159（调度员指定；设计占位 0116 → 0150，0118 → 切片 3），合并时由调度员重排。切片 1（身份与读端口）由 L2 实现。

## 切片 2：写入口

### 迁移
| 临时号 | 内容 | 依赖 |
|---|---|---|
| 0150 `nx028_governed_entry` | D9 统一入口注册表 `control.nexloop_governed_capabilities`（owner-only、append-only，handler 签名由插入触发器校验）；`authz.nexloop_goal_governed_action` 改名保留为 `..._before_registry_v0115`（无授权）+ 新包装（校验同 0111 函数体，capability → operation / 主体规则 / handler 来自注册表）；种入 13 行；派发预判 `control.nexloop_intent_dispatch_prediction` 与其无身份的快照检查 `control.nexloop_dispatch_controls_check`；人类请求审计表 `runtime.nexloop_human_requests`；手动复评与查询执行结果的 handler | 0068 控制面、0097 快照、0106 计划 feed、0109/0110/0112 联系检查、0111 承诺人类函数、0042/0065 effect outbox |
| 0151 `nx028_workbench_actions` | `authz.nexloop_workbench_action_for`：人类会话下按 capability 查本租户唯一的已发布 Action，并报告调用者是否持有该 Action 的授权（缺授权报 403 而不是 503）；不授予任何权限 | 0150 |

签名详见设计稿 §15.2a（已通知调度员转 L3）。

### HTTP 与 Python
- `nexloop_eios/workbench_actions_http.py`：`POST /api/v1/workbench/actions/{operation}`，同源、登录 Cookie、CSRF、`Idempotency-Key`（作为受治理请求 id，重放返回终态结果）。操作：`set_control`、`release_contact_restriction`、承诺五项、`request_plan_reevaluation`、`request_effect_query`；字段逐项校验，多余字段或非 UTC 时间为 422。错误码：401 / 403（含无授权）/ 404（租户未发布该 Action）/ 409 `not_allowed_in_state`（handler 拒绝该状态）/ 409 `conflict` / 422 / 503（授权或依赖暂时无法核验）。
- `WorkbenchActions`：每次请求重新做浏览器人类认证，经 `GoalGovernedActions` 提交；`http_api.py` 只追加挂载（`backend.py` 未改）。
- `GoalGovernedActions` 增加 `request_plan_reevaluation`、`request_effect_query`；`business_actions.PROFILES` 与 `business-actions.v1.json`（v6）增加两项 human_owner Action：`nexloop.plan.request_reevaluation:1`、`nexloop.service.query_request:1`（类型挂 Consumer v1）。

### 新增 Action（需切片 1 的角色映射）
- `nexloop.plan.request_reevaluation:1`、`nexloop.service.query_request:1`：建议 owner 与 operator 都持有（设计稿 §2 表）。

### 测试（Mac，`-n 3`，开跑负载约 5–7）
- `tests/test_governed_entry_pg.py` 4 例：注册表内容、append-only、错误 handler 被拒、旧入口无授权；预判与真实派发一致（暂停 → 拒绝且 provider 零请求；恢复后旧意图 `control_revision_stale` 且仍被拒；复评后的新意图可派发并派发一次；附带通知与联系限制；预判不留锁）；手动复评（服务主体持同授权被拒、计划进入 feed、重放不重复、非 active 计划被拒）；查询执行结果（排队中的意图被拒；unknown 时只把 outbox 的到期时间提前，状态、fence、意图数不变）。
- `tests/test_workbench_actions_http.py` 4 例：未登录 / CSRF 不符 / 未知操作 / 字段错误 / 多余字段 / 非 UTC / 缺 Idempotency-Key 全部拒绝且不写；工作台暂停与恢复与派发一致（AT-006 界面部分的后端）且同键重放不重复写；解除联系限制、标记沟通类、延期、已取消承诺再取消返回 409、手动复评；登录但无授权的人类 403 且不写。
- 回归 99 + 21 例通过（review_http、goal_controls、contact_refusal、承诺三组、nx022 派发、清单、db_boundary、计划复评、effect 派发、闭环知识、web chat、http_api 等）；`check_definer_search_path.py` 0 问题。
- `test_bootstrap.py` 在临时号下有 2 例预期失败（0116–0149 断档）；把 0150 临时改为 0116 后 4/4 通过（未提交）。

## 切片 3：人工接管（D3–D6）

### 迁移 0152 `nx028_takeover`（临时号）
- 版本化策略 `control.nexloop_takeover_policies`（`deploy/configuration/takeover.v1.json` 逐字种入；部署级配置、无租户维度，同 0109 reply_policies）：默认 2 小时、上限 24 小时（D3）。
- `control.nexloop_takeovers`（FORCE RLS；只允许结束一次）、`nexloop_takeover_settlements`（D5 每条来信的处置）、`nexloop_takeover_escalations`（到期未交还）。同一 consumer 同时只能有一个接管：consumer 级与该 consumer 任一会话的接管互斥，同一会话不能重复接管。接管“生效”= 未结束且未过 `expires_at`（不等到期处理）。
- 控制事件 `takeover` / `handback`（consumer 作用域）推进控制 revision；0106 计划触发器以原函数体替换：接管开始不唤醒计划，交还以 `handback` 唤醒（D5：复评而不补发）。
- 注册表新增 `conversation.takeover`、`conversation.handback`（人类）。
- Agent 不竞争回复：
  - 派发：0112 的 `control.nexloop_contact_assert_intent` 改名保留为 `..._before_takeover_v0112` 再包一层：consumer 级接管拒绝该 consumer 的所有意图，会话级接管拒绝该会话的 Agent 回复，`NXC06 taken_over`；派发预判随之返回 `taken_over`；
  - 消息中继：0049 的 `authz.nexloop_message_run_issuance_command` 改名保留，`issue` 时会话被接管则回滚并报 NXC06（条目稍后重试）；
  - 兜底回复：0110 的 `authz.nexloop_reply_fallback_command` 同样包装；回复担保端口（0110 函数体）在 state 中报告 `taken_over`，回复担保 worker 遇到接管直接结束该条目，不升级、不启动兜底。
- 交还 / 到期（`control.nexloop_takeover_end`，D5）：接管期间到达、仍无渠道已接受回复的来信中，只有最新一条重新登记待回复（兜底开始时间从现在重新计算）且中继条目保留；更早的记为 `settled_by_takeover`，删除待回复、关闭中继条目，不补发。到期由 reply-guarantor 处理 `takeover-expiry` feed（`nexloop.takeover.expire:1`），并写升级记录。
- 顾客视图（D4）：`authz.nexloop_conversation_takeover_state`，只对会话本人，返回 `handled_by: agent | human`；`GET /api/v1/conversations/{id}/handling`（`conversation_takeover_http.py`）；WebChat 显示“人工客服处理中”状态条，不显示逐条占位。

### Python、清单与授权
- `GoalGovernedActions.take_over_conversation` / `hand_back_conversation`；工作台写入口新增两项操作；`takeovers.TakeoverExpiryWorker`；reply-guarantor 进程每轮同时处理到期接管（摘要新增 `taken_over` 与 `takeover_*` 计数）。
- `business-actions.v1.json` v7：`nexloop.conversation.takeover:1`、`nexloop.conversation.handback:1`（human_owner）。
- `service-grants.v1.json` v11：reply_guarantor 增加 `NexLoop.feed.takeover-expiry:1` 与 `nexloop.takeover.expire:1`。

### 待裁定（已问调度员）
- 员工回复（`message.staff_send`）：0042 的派发要求意图带仍有效的 origin Run 凭据，人类会话没有 Run，设计稿 §4.2“与 Agent 同一账本”不能直接成立。方案 A：接管时签发“接管 Run”（复用 0110 方式）；方案 B（推荐，v0.1）：受治理人类 Action 直接写 Message 与会话流（WebChat 即渠道），D6 约束同样强制，接外部渠道时再做 A。本切片未实现员工回复，契约 `sender_kind` 放宽也随之暂缓。

### 测试（Mac，`-n 3`；真实 Pi 单独串行；开跑负载 5–12，含其他线）
- `tests/test_takeover_pg.py` 5 例：人类专用（服务主体持同授权被拒）、时长上限、默认 2 小时、接管开始不唤醒计划、重复/冲突接管被拒、会话级 Agent 回复 NXC06 而服务交付仍可派发、consumer 级接管下排队意图预判 `taken_over` 且真实派发零请求、交还 D5（最新一条恢复、更早一条结清、已回复的不涉及、接管前的不受影响、计划以 handback 唤醒、二次交还被拒）、到期（到期即失效、worker 结束并升级、最新来信恢复、到期条目移除）、回复担保遇接管不升级不兜底、顾客状态只给本人且只有 `handled_by`。
- `tests/test_takeover_relay_pg.py` 1 例（真实 HTTPS 来信 + 实际 relay CLI，无 Pi）：接管期间 relay 不签发 Run（固定错误行、无私密信息），状态条为 human；交还后只路由最新一条，更早一条不补发。
- `tests/test_takeover_e2e_pg.py` 1 例（真实 Host/Pi，guard 4 进程）：接管前已排队的 Pi 回复派发被拒，provider 零请求，不物化。
- `apps/web/test/handling.test.ts` 5 例 vitest；`tsc --noEmit` 通过。
- 回归：slice 2/3 相关 87 例、relay/计划/目标/派发/HTTP 80 例、Pi 串行 13 例（`test_reply_fallback_pg`、`test_contact_reply_dispatch_pg`、`test_closure_refusal_versions_pg`）全部通过；`check_definer_search_path.py` 0 问题。
