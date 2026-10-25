# 设计稿｜NX-058 片 D：owner 专用成员 Action（接 NX-056 审批）

第 2.1 版，2026-10-25，L2（目标展示接口以 L3 起草的 ADR-039 修订稿为唯一来源，通过后按它同步本文 §1、§6）。第 1 版（5393d205）经 L6 复核（`build/nx058-slice-d-design-review.md`，K1、I1–I10、S1–S6），本版逐条吸收，关闭说明见 §11。

上位设计是 `docs/design/account-self-service.md` 第 7 版（下称“自助设计”）的 §1b、§2、§7、§9（AA6）；这里只写落地方式和需要确认的点，不重复上位设计已确定的规则。实现基线：第二十六批合入后的 main。届时片 C 的迁移编号为 0340（`workbench_member_changes`）、0341（`governed_authority_change`），NX-056 为 0326–0332，L3 的跟进为 0339；本文所说的 0340/0341 都指这一编号，并以片 C 的 X1 修复（a80c77aa、fc012e6e：drift 检查在写事务内、先取成员锁）为**前置条件**（I7）。迁移号临时，由 L4 组批时重编。

## 0. 范围与可批准条件

本片开放 7 个 Action（`nexloop.workbench_member.*`），全部需要审批，单人模式不适用：

| Action | handler 操作名 | 请求体 | 目标要求 |
|---|---|---|---|
| `invite_owner` | `invite_workbench_owner` | `username`、`expires_in_hours`（与 `invite` 相同，没有 role 字段） | 无目标 |
| `promote_owner` | `promote_workbench_member` | `target_principal`、`reason` | active 的 operator 或 reviewer（S2） |
| `demote_owner` | `demote_workbench_owner` | `target_principal`、`new_role`（只许 operator/reviewer，HTTP 与 SQL 两层校验，S2）、`reason` | active 或 suspended 的 owner（S3/D4） |
| `suspend_owner` | `suspend_workbench_owner` | `target_principal`、`reason` | active 的 owner |
| `resume_owner` | `resume_workbench_owner` | `target_principal`、`reason` | suspended 的 owner |
| `revoke_owner` | `revoke_workbench_owner` | `target_principal`、`reason` | active 或 suspended 的 owner |
| `issue_owner_reset` | `issue_workbench_owner_reset` | `target_principal` | active 的 owner |

**可批准条件**（I1、I2）：除请求人和目标本人外，至少有 quorum（=1）位 active、投票冷却已满（§2）的 owner。所以：

- **单 owner 租户**（I2）：7 个 Action 都不可批准，只能走运维通道（0198 + configurator 成员 apply，审计单列；自助设计 C1/Q-C）。
- **双 owner 租户**（I1）：针对另一位 owner 的 demote/suspend/resume/revoke/issue_owner_reset 都不可批准（唯一可能的批准人就是目标本人）；promote_owner 与 invite_owner 可由另一位 owner 批准。
- **发起时当场拒绝**（I1）：gate 开审批之前，对声明了 `target_subject_field` 的策略、以及所有 `not_applicable` 策略，计算“排除请求人和目标后，持有批准角色、active、冷却已满的人数”；小于 quorum 时直接拒绝 `quorum_unreachable`（409），不挂起等 24 小时过期。这是 NX-056 gate/open 的改动（0332 目前只在单人模式下返回该码），需 L3 同意，copy guard 登记。冷却未满导致的不可批准，返回同一个码并附 `eligible_from`（最早可批时刻），工作台据此提示。
- **运行手册**：owner 账户被盗时，**首选运维通道**处置（configurator `--revoke-member`，Z2；必要时 0198），不依赖审批；双 owner 和单 owner 租户只能这样做。

不在本片：第二阶段的 `link_owner_external`；工作台前端页面（只做 HTTP 接口和读模型字段，页面另排）。

## 1. 审批接线、展示与 fail closed

**策略**：`deploy/configuration/approval-policies.v1.json` 增加 7 条，均为 `condition: null`、`quorum: 1`、`approver_roles: ["owner"]`、`solo_mode: "not_applicable"`、`expires_after_seconds: 86400`；以成员为目标的 6 条 `target_subject_field: "target_principal"`（AA2），`invite_owner` 不设（S1）。清单 version 1→2，`expected_revision: 1`。

**summary_fields（K1）**：0326 的 `approval_policy_guard` 只允许白名单内、且不匹配身份禁止名单的字段，Python `approval_policies.py` 的 `_IDENTIFYING` 同样拒绝。所以 summary 只放 `new_role`（仅 `demote_owner`），其余 6 条为空数组。`control.nexloop_approval_summary_allowed` 重定义，只给 `workbench_member.demote_owner` 加 `new_role`（copy guard 与 definer 检查登记，I10）。

**目标的展示（K1，接口约定，需 L3 同意并补入 ADR-039）**：批准人必须看清要变更的是谁，但目标不进 summary、不进审计正文。约定 NX-056 审批视图（`runtime.nexloop_approval_view_row` 及其列表/详情读口）新增字段 `target_member`：

- 只对声明了 `target_subject_field` 的策略输出；值由服务端在读取时解析：`control.nexloop_approval_target_subject` → 该 subject 在本租户的成员显示标签（工作台显示名；无显示名时 principal 的脱敏形式，前 4 位 + `…`）与当前角色、状态。
- 只对“当前持有该策略批准角色的 active 成员”（即 owner）输出；请求人本人看自己的请求时也输出。
- 读时解析，不落库：成员被擦除或改名后，视图跟着变（I10 擦除）。
- `invite_owner` 的 username：**负责人二选一**（D7）。建议同样经 `target_member` 式的读时字段只给 owner 批准人看（`invitee_username`）；备选是不展示，批准人线下与请求人核对。

**handler 自查（fail closed）**：每个 owner handler 第一步读当前策略 `control.nexloop_approval_policy(capability)`，要求存在且 `condition is null`、`quorum>=1`、`solo_mode='not_applicable'`、`approver_roles` 只含 owner、目标类的 `target_subject_field='target_principal'`；任何一条不满足就拒绝 `approval_unavailable`（22023，HTTP 409）。未发布策略之前，这 7 个 Action 即使已注册也只会返回 `approval_unavailable`（自助设计 §1b 过渡要求）。

**第二层：回查本次审批（I5，即 D2 的决定）**：0327 的治理入口在调用 handler 前设置事务级 `nexloop.approval_in_flight`。这个自定义 GUC 任何会话都能 set_config，gate 的“无策略放行”分支也不清空它，所以 handler **不只读它**，而是拿它的 approval_id 回查 `control.nexloop_approval_requests`，全部满足才继续，否则 `approval_unavailable`：

- capability 等于本能力；
- `request_digest` 等于 `control.nexloop_approval_digest(tenant, world, action, capability, policy_version, 当前请求体)`；
- requester_subject 等于当前请求人的 subject；
- status 为 approved 且尚未消费（消费写在 handler 之后）；
- `policy_version` 等于 handler 自查时读到的策略版本。

伪造 GUC 的一方因此必须拿出一条真实、digest 一致、已批准未消费的审批，与正常走 gate 等价。不新建临时表；0327 只在注释里登记“owner handler 依赖 in_flight”。

**策略版本的一致性（I9）**：已核对，`control.nexloop_configure_approval_policies`（0330）只取全局锁 `nexloop-approval-policies`，**不取**租户审批锁。在 READ COMMITTED 下，gate 与 handler 是两条语句、两个快照，中间可能有新版本发布，所以“gate 读到的版本就是 handler 读到的版本”不成立。由上面的版本核对兜住：版本变了，handler 拒绝 `approval_unavailable`，审批作废，请求人在新版本下重新发起。

**消费时重新计票（I4）**：0327 的 gate 在消费时经 `nexloop_approval_valid_votes` → `approver_eligible` 重新统计有效票；加上 §2 的冷却后，“批准之后被降级、停用的 owner 的票不计入”自动成立，本片不另写计票逻辑。

**handler 拒绝的后果（I4）**：handler 的拒绝（`target_role_changed`、`last_owner`、`target_not_active`、`approval_unavailable` 等）发生在 gate 的 savepoint 内，审批记为 `execution_refused:22023` 并作废，不能复用，请求人需要重新发起。这是预期行为。HTTP 把它映射成可读原因：治理入口的结果目前只带 sqlstate，本片让 0327 的结果在 22023 时同时带出 handler 的固定原因码（消息形如 `workbench member: <code>`，只取码，不取值），工作台显示“执行被拒：目标角色已变化”等（0327 重定义，copy guard 登记，需 L3 同意）。

## 2. 投票资格：24 小时冷却，所有策略统一（I3）

自助设计 §9 AA6 已定：`vote_eligible_from` 由 NX-056 在决定时和消费时都调用，不限于 owner 策略。第 1 版的 D1 不再是开放问题。

- 新增只读函数 `control.nexloop_workbench_vote_eligible_from(tenant, world, subject, roles[]) returns timestamptz`（world 对工作台成员固定为 `real`，与 NX-056 按 (tenant, world) 计数对齐，Z4）：按 `control.nexloop_workbench_role_history`，取该 subject **当前这段连续持有 roles 并集**的起点加 24 小时；当前不持有并集内任何角色返回 null。并集内换角色不中断，全部停止（含停用）后再开始就重新计时（Z4）。owner 策略的并集只有 owner，所以被提升的成员对 owner 策略从提升时刻起算。
- `nexloop_approval_approver_eligible` 重定义（copy guard 登记）：原条件之外加 `vote_eligible_from(...) <= clock_timestamp()`。决定时（decide 拒收）和消费时（valid_votes 不计入）都经过它。
- **上线影响（I3，写入上线手册和告警说明）**：存量成员的历史起点是片 A `role_history_sync` 在部署时补的 `start`，所以**部署后 24 小时内所有审批策略都批不了**，包括首批三条（`real_dispatch.set` 开启、`consumer.erase`、`retention.release_hold`）。上线时间点需与负责人确认，避开需要审批的时段；期间只能走运维通道。新加入的批准人同样 24 小时后才能投票。
- §8 的 Python 参照实现覆盖：并集内切换、中途停用后恢复、存量 start 行、多次进出（S4）。

## 3. handler 公共步骤

一个函数 `control.nexloop_governed_change_workbench_owner`（promote/demote/suspend/resume/revoke），`invite_owner` 和 `issue_owner_reset` 分别复用片 A、片 B 的签发函数加 owner 分支。公共顺序：

1. `set_config('eios.tenant_id',p_tenant,true)`（L3 行锁触发器要求）。
2. 成员锁（0341 起即租户审批锁；gate 已持有，advisory xact lock 重入，不新增等待，I9）。
3. 策略自查与审批回查（§1 两层）。
4. 请求人必须是 active owner；目标经 `control.nexloop_approval_subject_of` 解析（AA2），解析不到 `target_not_active`；目标是请求人 `target_is_requester`。
5. 锁内重查，先目标后请求人（I9：目标的 membership、account 行 FOR UPDATE，再请求人的行）：目标当前角色与状态按 §0 的表，不符分别 `target_role_changed` / `target_not_active` / `target_not_suspended`；revoked 不能再作目标。
6. **最后一位 owner**（I8，D5 的决定）：demote、suspend、revoke 之后，若租户内 active 的 owner 数为 0，拒绝 `last_owner`。口径：只数 active 的 owner，**冷却未满的也算**（冷却只影响投票，不影响登录与处置）。三位 owner 时两份互相处置的审批在租户锁下串行，第二份执行时看到剩一人，允许；结果是只剩一位 owner，此后进入 §0 的单 owner 规则。这是安全的，写入运行手册。
7. `control.nexloop_authority_handler_mark(p_tenant)`（片 C 的 0341）：**在目标与最后 owner 校验之后、第一处授权写入之前**（I7）。handler 拒绝时 savepoint 回滚，mark 也一并回滚，请求人的 tail 不受影响。
8. 写入，沿用片 C 的写法（Z0：先撤旧角色模板实例，再追加配置版本，再授新角色模板实例）：
   - promote：撤 operator/reviewer 模板 → 配置版本 owner → 授 owner 模板；历史 stop 旧角色 + start owner。
   - demote：撤 owner → 配置版本新角色 → 授新角色；suspended 的 owner 被降级时只改配置和历史、写撤销实例，不授新角色模板，状态仍 suspended。
   - suspend / resume / revoke：与片 C 相同（resume 授当前模板，revoke 一律写撤销实例）。
9. 会话、凭据与悬挂的审批（I7）：
   - 目标 `session_epoch+1`，撤销其未用重置码；旧 permit 在下一事务被拒（端到端测试见 §7）。
   - demote/suspend/revoke 时，撤销目标签发的未用 owner 邀请和普通邀请。
   - 目标**作为请求人**、尚未消费的审批（waiting_approval 或 approved），在同一事务里由 handler 调用 NX-056 的 `control.nexloop_approval_terminate(…, 'requester_no_longer_eligible')` 终结（新终结原因，需 L3 同意；不能只等 24 小时过期，否则目标恢复后可能拿旧批准执行）。
   - 目标作为批准人投出、尚未消费的票，由消费时的重新计票自动剔除（§1），不需要另外处理。
10. 审计：动作本身、每个模板实例（片 C 的 `grants_written`）、`approval_id`。审计里只有 principal 与原因，没有 token 或码。

## 4. `invite_owner` 与接受

- 签发：只在审批通过后的执行时生成 token（gate 首次提交只开审批，不调用 handler）。邀请记录 role='owner'、`issued_via='invite_owner'`、`approval_id`。
- **token 只出现在本次 HTTP 响应里**（I6）：不写入 consumptions、claim、审计、outcome 或任何其他持久化位置；库里只有 HMAC。消费后 1 小时内重提同一请求体得到 `approval_consumed`，响应里**没有** token（NX-056 的重放只返回结果码，测试确认）。
- 接受：新函数 `accept_v3` 取代片 A 的 `accept_v2`（S3/D3，收回 identity 对 v2 的 execute）。owner 邀请要求 `issued_via='invite_owner'`，且其 `approval_id` 在消费表中的 `result='executed'`（I6，不能只看“已消费”）。接受后 owner 计入资格人数、单人模式立即失效（已有时间线触发器负责），投票冷却从接受时刻起算。
- S1：接受者与签发者为同一个人时（自助设计 C2 的 `same_source_as_issuer` 信号），额外标记并进入 `approval_solo_mode` 告警视图。
- configurator 与邀请冲突的规则（V2）不变。

## 5. `issue_owner_reset`

- 执行时（审批已消费）才生成重置码、目标 `session_epoch+1`（R5）。
- **码只出现在本次 HTTP 响应里**（I6），同 §4；之后与片 B 相同（HMAC 存储、30 分钟、一次性、撤旧码）。
- 限速与 `issue_reset` 共用签发 owner 的桶，超限 429 + Retry-After 900。
- 双 owner 时不可批准，发起即 `quorum_unreachable`（§0），走运维通道 0198（W3）。

## 6. 清单、计数与发布（I10）

- business-actions、capabilities（schema_hash）、system-catalog-names 各加 7 条；workbench-roles 版本 +1，owner 角色加 7（82→89，仍 ≤128），`slices.workbench_members` 加 7，均在 owner-only 集合。
- 计数：Actions 116→123；`test_workbench_read_pg` owner 89、伪造补 40；`test_stage_register_pg` (20, 123, 2)。
- 重定义并登记 copy guard：`nexloop_approval_summary_allowed`（K1）、`nexloop_approval_approver_eligible`（§2）、gate/open 的 `quorum_unreachable` 判断（§0）、0327 入口的原因码（§1）、审批视图的 `target_member`（§1）。后三项属 NX-056，需 L3 同意。
- **告警**（需 L3 同意）：`approval_solo_eligibility_reduced` 会因 owner 降级、停用而触发，正是想要的效果，不改规则；新增“成员第一次投批准票”（D6），与 S1 的同一人标记一起纳入告警规则 v15。
- **擦除覆盖**：审批请求体里的 `target_principal`、`username` 是成员标识，不是 Consumer。核对 0331 的 Consumer 擦除扫描不会替换它们（测试）；成员擦除或注销时，在 erasure coverage 里登记为“保留的员工身份，附原因”，与片 C 的 workbench 授权实例同一口径；目标展示读时解析，不额外落库。
- **发布顺序**（写进运行手册“启用 owner Action”一节，S5）：前置条件（X1 修复已上线、冷却起点已过 24 小时、确认可批准条件）→ **Action publish 先于策略 apply**（否则 guard 报“需要 human governed capability”；新 Action 发布前返回 404）→ 策略 v2：`approval_policies_apply.py` dry-run → apply → 核对 sha256 → 自检（策略 apply 前 7 个 Action 返回 `approval_unavailable`，apply 后返回 `waiting_approval` 或 `quorum_unreachable`）→ 回滚（策略只追加，不能回退；要关闭，发布 v3 让 handler 自查失败，例如 quorum 设为超大值，或撤销 Action 注册）。
- HTTP：`workbench_actions_http.OPERATIONS` 加 7 个校验；审批结果沿用 NX-056 的 `waiting_approval` / `approval_consumed` / `policy_changed`，新增 `quorum_unreachable` 与执行被拒的原因码映射。

## 7. 测试（PostgreSQL，经实际 app）

1. 未发布策略：7 个 Action 均 `approval_unavailable`；策略被改成允许单人或带条件时同样拒绝。
2. 清单 v2 经 `approval_policies_apply.py` 实际 apply 成功；guard 拒绝 summary 里的 `target_principal`（K1）。
3. 三位冷却已满的 owner + 一位 operator：promote 走完“请求 → 另一 owner 批准 → 请求人重提执行”；批准人在审批视图里看到 `target_member`，非 owner 读者看不到。
4. **单 owner 租户**（I2）：promote_owner 发起即 `quorum_unreachable`，handler 未被调用。
5. **双 owner 租户**（I1）：suspend_owner 另一位 owner 发起即 `quorum_unreachable`；promote_owner 可批准。
6. 目标本人投票被拒（`target_cannot_approve`），目标换用自己的另一个 principal 同样被拒（AA2）。
7. 冷却：刚提升的 owner 24 小时内投票被拒；首批策略在部署起点后 24 小时内批不了（用历史行的时间推进，不改系统时钟）。
8. 单人模式开启时自批无效（`not_applicable`）。
9. **消费时重新计票**（I4）：B 批准后被 C 的 suspend_owner 停用，A 再执行时有效票不足，回到 waiting。
10. **反向**（I4）：owner 版本对已被降级的目标返回 `target_role_changed`，审批记为 execution_refused 并作废，HTTP 显示原因码。
11. 最后一位 owner 不能被 demote/suspend/revoke；三位 owner 并发互相 suspend 时串行，最后剩一位（I8）；suspended owner 只能用 owner 版本处理。
12. **第二层**（I5）：伪造 `nexloop.approval_in_flight` 指向另一请求体或另一能力的已批准审批，handler 拒绝；策略在 gate 与 handler 之间换版本时 handler 拒绝。
13. **秘密不重放**（I6）：issue_owner_reset、invite_owner 执行后，同一请求体重提得到 `approval_consumed`，响应、consumptions、claim、审计里都没有码或 token。accept_v3 拒绝审批结果不是 executed 的 owner 邀请；用 operator 邀请伪造 owner 接受被拒。
14. **mark 与 tail**（I7）：handler 拒绝后 savepoint 回滚掉 mark，同一事务中请求人的 tail 复核仍通过；被降级的 owner 拿旧 permit 调用 owner-only Action 被拒（端到端，补片 C S-5 的缺口）。
15. **X1 与并发**（I7、I9）：promote 执行中 configurator apply 旧成员文件——不死锁，X1 拒绝 apply；demote 之后 apply 想恢复其 owner 角色被 X1 拒绝。
16. **悬挂审批**（I7）：被降级目标作为请求人的未消费审批被终结为 `requester_no_longer_eligible`。
17. 擦除：Consumer 擦除不改审批请求体里的成员标识；erasure coverage 登记通过。
18. 回归：片 C 的成员变更、NX-056 gate 与首批策略、copy guard、definer、erasure coverage、告警规则。

## 8. 合入前可先做的部分（不依赖新迁移）

- HTTP 校验器与请求体形状、策略清单条目草案、清单条目草案（注册 Action 需要 capability 行，必须随迁移一起合入，先放在分支上不进批）。
- `vote_eligible_from` 的纯 Python 参照实现与单元测试（历史行 → 起点，覆盖 S4 的情形），供 SQL 实现对照。
- 运行手册“启用 owner Action”一节的草稿。

## 9. 残余风险（需负责人知情接受，S6）

- (a) **被盗 owner 联手第二位 owner**：三位 owner 的租户里，攻击者控制一位 owner 后，只要再控制或说服另一位 owner 批准，就能提升自造身份；24 小时冷却后获得永久多数，之后可以经审批处置其余 owner。缓解：首次投票告警（D6）、同一人标记（S1）、所有 owner 可见的审计链，以及运维通道处置（不依赖审批，§0）。
- (b) **双 owner 租户里被盗的 owner 无法经审批移除**，只能走运维通道。
- (c) **冷却期内审批停摆**：部署后 24 小时、单 owner 加入第一位新成员后 24 小时，所有需要审批的动作都批不了。这是安全方向，但属于可用性风险。
- (d) **运维通道绕过审批**：configurator 和 0198 仍可直接改任何成员，依赖审计单列与运行手册约束。
- (e) 自助设计 §7 的“自造第二身份”风险对 owner 专用 Action 不成立（批准角色只有 owner），对其他策略仍按 V5 处理。

## 10. 待确认

- **D6 首次投票告警**：建议纳入告警规则 v15，需 L3 同意（§6）。
- **D7 invite_owner 的 username 展示**：读时字段只给 owner 批准人看（建议），或不展示、线下核对。需负责人选择（§1）。
- **D8 NX-056 的四项改动**（`target_member` 视图字段、发起时 `quorum_unreachable`、执行被拒原因码、`requester_no_longer_eligible` 终结原因）：需 L3 评估并补入 ADR-039；本片按这里的接口约定实现，L3 若改接口形状，以 ADR 为准。
- 第 1 版的 D1（冷却范围，I3 定为所有策略）、D2（第二层，I5 定为 in_flight + 回查）、D3（accept_v3，S3 同意）、D4（降级 suspended owner，S3 同意）、D5（只数 active，I8 同意且冷却未满也算）已关闭。

## 11. L6 复核关闭说明（第 1 版）

| 编号 | 处理 |
|---|---|
| K1 | summary 只留 `new_role`（仅 demote_owner）；白名单重定义；目标由审批视图读时解析为 `target_member`，只给 owner 批准人看（接口约定，需 L3/ADR-039）；username 二选一（D7）；测试 2、3（§1、§7） |
| I1 | 写明可批准条件；双 owner 时处置另一位 owner 不可批准，发起即 `quorum_unreachable`；运行手册首选运维通道；测试 5（§0） |
| I2 | 单 owner 租户只能走运维通道；测试 4（§0、§7） |
| I3 | 冷却对所有策略统一；上线手册写明部署后 24 小时首批策略也批不了（§2） |
| I4 | 消费时复用 gate 重新计票；handler 拒绝即 execution_refused 并作废，HTTP 显示原因码；测试 9、10（§1、§7） |
| I5 | in_flight + 回查 capability、digest、请求人、状态、策略版本；测试 12（§1） |
| I6 | token 与码只在当次响应；accept_v3 要求 `result='executed'`；测试 13（§4、§5） |
| I7 | X1 修复列为前置条件；mark 位置；悬挂审批终结为 `requester_no_longer_eligible`；测试 14、15、16（§3、§7） |
| I8 | 只数 active 的 owner，冷却未满也算；并发互相处置写明（§3） |
| I9 | 已核对策略发布只取全局锁、不取租户锁，由 I5 的版本核对兜住；锁顺序写明；promote 与 apply 并发测试 15（§1、§3） |
| I10 | 白名单与其他重定义登记 copy guard；告警 v15（D6）；擦除覆盖登记；发布顺序先 Action 后策略 v2（§6） |
| S1–S5 | 同一人标记（§4）、new_role 两层校验（§0）、v3 与降级 suspended owner（§0、§4）、参照实现覆盖情形（§2）、运行手册启用一节（§6） |
| S6 | 残余风险 (a)–(e)（§9） |
