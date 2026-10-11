# 回复保障：原回复结果不明时先对账（分支 `reply-unknown-reconcile`，临时迁移 0210）

依据：调度员 2026-10-11 裁定，以及 AGENTS 不变式“unknown 的外部结果不能当作失败盲目重试，先查询和对账”。来源是 NX-031 切片 3 的压测发现：原回复投递超时后结果为 unknown，回复保障仍在 300 s 后启动了兜底；兜底回复被 NXC05 拒绝（每条来信只回复一次）后，这个 intent 一直停在 accepted。

## 行为

reply-due 到期、来信尚未了结时，reply worker 按以下顺序处理（`contact_restrictions.ReplyGuaranteeWorker`）：
1. 已了结或已升级：完结。
2. 被接管：完结（不变）。
3. 兜底 Run 已经跑满时长：升级为 `fallback_unanswered`（不变）。
4. **原回复是 unknown**（这条来信绑定的外发记录 `delivery_state='unknown'`）：
   - 第一次遇到时，调用 `reconcile`：把该 intent 的 effect outbox 到期时间提前到现在。走的是现有的查询路径（`EffectDispatcher` 的 reconcile 阶段，和 0124 人工“请求查询”是同一条路），不重发，状态和幂等键都不变；同时在 `control.nexloop_reply_reconciles` 记下开始时间（每条来信一行，幂等）。
   - 对账窗口内：reply-due 延后到窗口结束（`reply_reconciling`）。查询观察到已送达时，外发记录变成 delivered，reply-due 随即按已回复了结。
   - 窗口结束仍是 unknown：升级给负责人，原因码 `reply_unknown`，evidence 为 `intent_id`、`reconcile_started_at`、`window_seconds`、`restricted`。**不发兜底**，因为原回复可能已经送达。
5. 原回复明确失败（intent 为 failed），或者根本没有原回复：照常启动兜底 Run。

## 配置：`reconcile_window_seconds`
- 回复策略升级为第 2 版：`deploy/configuration/reply-guarantee.v2.json`，0210 把它写进 `control.nexloop_reply_policies`（与文件规范化 JSON 逐字节一致，有测试检查）。v1 文件和数据库里的第 1 版都不变。
- 键位于 `fallback.reconcile_window_seconds`，默认 600 s，取值范围 30–3600 s。v1 策略没有这个键时使用默认值。
- 加载时要求：`start_after_seconds + reconcile_window_seconds + 兜底 Run 最长时长 < reply_window_seconds`，保证对账之后如果要走兜底，仍能在回复窗口内完成。v2 是 300 + 600 + 120 < 1800。

## 兜底 intent 被终结性拒绝（只限“每条来信只回复一次”）
- `authz.nexloop_fail_refused_effect`（只允许 action worker 调用）：一个从未派发过（没有 attempt）、仍处于 accepted 的**绑定回复** intent，如果被 NXC05 拒绝、且拒绝原因属于“每条来信只回复一次”这一类，就和它的 outbox 一起转为 failed。拒绝码 NXC05 保留在 `runtime.nexloop_dispatch_refusals`（0131）。外发记录随之变成 failed，快照的 `effects.by_state` 把它计为 failed，不再和待派发的 effect 混在一起。
- **按拒绝原因区分，不只看 NXC05 这个码**（调度员 2026-10-11 修正）。NXC05 同时也表示联系限制，而联系限制可以由 owner 解除（Contact.release），所以不是终结性的。终结性的原因只有三个：
  - `already_answered`：来信已回复（SQL 原因文字 `inbound message already answered`）。SQL 不采信执行器传来的原因，会重新核对：同一来信是否确实已有另一条回复处于派发中、已接收、已送达或结果不明；
  - `reply_window_closed`：回复窗口已关（`reply window closed`）；
  - `fallback_bound_elsewhere`：兜底回复绑定到了另一条来信（`fallback reply bound to another message`）。
- 其他 NXC05，即联系限制类（外联不是回复、回复没有绑定该客户的来信、附带通知），保持原行为：留在 accepted，下次派发时按当前约束重新判断。
- 原因文字从哪里来：派发路径上的 SQL 拒绝，经 `goal_controls.ControlPlane._call` 转成 `ControlDenied(reason)`。现在 `ControlDenied` 多带一个 `detail`，即 SQL 里写死的固定原因文字（不含任何数据），执行器据此归类（`effect_execution.one_reply_reason`）。拒绝的行为和类型都不变。
- 控制类拒绝（NXC01–NXC04、NXC06）也不是终结性的：按 NX-022 的重估语义，新的 Run 会用当前快照重新提交同一个业务 intent，随后可以派发（`test_nx022_dispatch_e2e` 有测试覆盖）。

## `nexloop_inbound_answered` 与 reply-due 了结条件的区别（保持不变）
- `runtime.nexloop_inbound_answered` 把 `dispatching`、`provider_accepted`、`delivered`、`unknown` 都算作“已回复”：只要第一条回复可能已经到达，就不再派发第二条回复（NXC05），防止重复回复。
- reply-due 只有在渠道**确认接收**（`provider_accepted` / `delivered`）时才了结。
- 两者之间的空档正是 unknown：它不能算作已送达（reply-due 不了结），也不能算作失败（不能盲目兜底）。本改动用对账窗口填上这个空档。

## 测试（`tests/test_reply_reconcile_pg.py`；真实链路，串行运行）
- unknown → 查询确认已送达 → 了结，不兜底、不升级；窗口内重复处理只保留一次对账，导出文件只有一份。
- unknown → 窗口结束仍不明 → 升级为 `reply_unknown`，不兜底；重放不会重复升级。
- 原回复明确失败（账本状态注入）→ 照常启动兜底。
- 策略第 2 版已发布，并与文件一致；窗口的默认值、上下限和总时长约束。
- 兜底被“来信已回复”拒绝 → intent 和外发记录都变成 failed，拒绝码为 NXC05（`test_reply_fallback_pg` 中收紧了原有断言）。
- 联系限制类拒绝不终结：intent 留在 accepted，限制不再生效后能正常派发（`test_contact_effect_categories_pg`，新用例）；原有用例 `test_restricted_consumer_commitment_contact_fulfilment_is_blocked_and_service_fulfilment_works` 断言未改，验证行为已恢复。
- 原因归类（纯函数）：三个一次回复类原因判为终结，联系限制类、其他码和非拒绝错误都不判为终结；psycopg 错误和 `ControlDenied` 两条路径都覆盖。
