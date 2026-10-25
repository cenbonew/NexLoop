# 设计稿｜NX-058 片 D：owner 专用成员 Action（接 NX-056 审批）

第 1 版，2026-10-25，L2。上位设计是 `docs/design/account-self-service.md` 第 7 版（下称“自助设计”）的 §1b、§2、§7；这里只写落地方式和需要确认的点，不重复上位设计里已经确定的规则。实现基于第二十六批合入后的 main（片 C 为 0340/0341，NX-056 的 0326–0332 与 L3 的 0339）。迁移号临时，由 L4 组批时重编。

## 0. 范围

本片开放 7 个 Action（`nexloop.workbench_member.*`），全部需要审批，单人模式不适用：

| Action | handler 操作名 | 请求体 | 目标要求 |
|---|---|---|---|
| `invite_owner` | `invite_workbench_owner` | `username`、`expires_in_hours`（与 `invite` 相同，没有 role 字段） | 无目标 |
| `promote_owner` | `promote_workbench_member` | `target_principal`、`reason` | active 的 operator 或 reviewer |
| `demote_owner` | `demote_workbench_owner` | `target_principal`、`new_role`（operator/reviewer）、`reason` | active 或 suspended 的 owner |
| `suspend_owner` | `suspend_workbench_owner` | `target_principal`、`reason` | active 的 owner |
| `resume_owner` | `resume_workbench_owner` | `target_principal`、`reason` | suspended 的 owner |
| `revoke_owner` | `revoke_workbench_owner` | `target_principal`、`reason` | active 或 suspended 的 owner |
| `issue_owner_reset` | `issue_workbench_owner_reset` | `target_principal` | active 的 owner |

不在本片：第二阶段的 `link_owner_external`；工作台前端页面（只做 HTTP 接口和读模型字段，页面另排）。

## 1. 审批接线与 fail closed

NX-056 的 gate（0327）在**没有策略**或策略条件不命中时直接放行。owner 专用 Action 不能依赖“策略恰好已发布”，所以：

- **策略**：`deploy/configuration/approval-policies.v1.json` 增加 7 条，均为 `condition: null`、`quorum: 1`、`approver_roles: ["owner"]`、`solo_mode: "not_applicable"`、`expires_after_seconds: 86400`；以成员为目标的 6 条 `target_subject_field: "target_principal"`（AA2），`invite_owner` 不设。`summary_fields` 只放 `target_principal`、`new_role`（不放 reason 和 username 以外的自由文本；`invite_owner` 放 `username`）。清单 version 1→2，`expected_revision` 1。
- **handler 自查（fail closed）**：每个 owner handler 第一步读当前策略 `control.nexloop_approval_policy(capability)`，要求存在且 `condition is null`、`quorum>=1`、`solo_mode='not_applicable'`、`approver_roles` 只含 owner、目标类的 `target_subject_field='target_principal'`；任何一条不满足就拒绝 `approval_unavailable`（22023，HTTP 409）。gate 与 handler 在同一事务里，策略只追加、发布持锁，所以 gate 用到的版本就是 handler 读到的版本；为防万一，handler 还比较“本事务内 gate 消费的审批”的策略版本（见 D2）。
- 未发布策略之前，这 7 个 Action 即使已注册也只会返回 `approval_unavailable`，与自助设计 §1b 的过渡要求一致。

## 2. 投票资格：24 小时冷却（X2）

现状：NX-056 的 `control.nexloop_approval_approver_eligible` 只看“active 的 human 成员 + 最新配置里持有批准角色”，**没有**自助设计 §7 要求的 24 小时冷却。owner 专用 Action 必须有它，否则“提升自己的第二身份后立刻互批”可行。

- 新增只读函数 `control.nexloop_workbench_vote_eligible_from(tenant, subject, roles[]) returns timestamptz`：按 `control.nexloop_workbench_role_history`，取该 subject **当前这段连续持有 roles 并集**的起点加 24 小时；当前不持有并集内任何角色返回 null。并集内换角色不中断，全部停止后再开始就重新计时（Z4）。
- 存量成员（Y6）：片 A 的 `role_history_sync` 在部署时为现有成员补 `start`，起点就是部署时刻，所以部署后 24 小时内 owner 专用 Action 批不了，走运维通道。运行手册写明。
- `nexloop_approval_approver_eligible` 重定义（copy guard 登记）：原条件之外加 `vote_eligible_from(...) <= clock_timestamp()`。决定时（decide）和消费时（valid_votes）都经过它，所以冷却未满的票决定时被拒、消费时不计入。
- 适用范围见 D1。

## 3. handler 公共步骤

一个函数 `control.nexloop_governed_change_workbench_owner`（promote/demote/suspend/resume/revoke），`invite_owner` 和 `issue_owner_reset` 分别复用片 A、片 B 的签发函数加 owner 分支。公共顺序：

1. `set_config('eios.tenant_id',p_tenant,true)`（L3 行锁触发器要求）；fail closed 策略自查（§1）。
2. 成员锁（即审批锁，gate 已持有，重入）。
3. 请求人必须是 active owner；目标经 `control.nexloop_approval_subject_of` 解析（AA2），解析不到 `target_not_active`；目标是请求人 `target_is_requester`。
4. 目标当前角色与状态在锁内重查：非 owner 目标对 owner 版本一律 `target_role_changed`（promote 相反：目标是 owner 时 `target_role_changed`）；状态按上表，不符 `target_not_active` / `target_not_suspended`；revoked 不能再作目标。
5. **最后一位 owner**：demote、suspend、revoke 之后，若租户内 active owner 数为 0，拒绝 `last_owner`（见 D5）。
6. `control.nexloop_authority_handler_mark(p_tenant)`（片 C 的 0341），之后的授权写入不使请求人的 tail 复核失败；mark 关 read memo。
7. 写入，沿用片 C 的写法（Z0：先撤旧角色模板实例，再追加配置版本，再授新角色模板实例）：
   - promote：撤 operator/reviewer 模板 → 配置版本 owner → 授 owner 模板；历史 stop 旧角色 + start owner。owner 策略的批准角色只有 owner，所以被提升的成员对这些策略从提升时刻起算 24 小时（自助设计 §7）。
   - demote：撤 owner → 配置版本新角色 → 授新角色；suspended 的 owner 被降级时只改配置和历史，不授新角色模板，状态仍 suspended。
   - suspend / resume / revoke：与片 C 相同（resume 授当前模板，revoke 一律写撤销实例）。
8. 会话与凭据：目标 `session_epoch+1`，撤销其未用重置码；revoke/demote 时撤销目标签发的未用邀请（owner 签发的邀请，降级后不应继续有效）。
9. 审计：动作本身、每个模板实例（片 C 的 `grants_written`）、`approval_id`（由 gate 的结果带入审计 payload）。

## 4. `invite_owner` 与接受

- 签发：只在审批通过后的执行时生成 token（gate 首次提交只开审批，不调用 handler，所以发起请求不产生 token，R5 同理）。邀请记录 role='owner'，`issued_via='invite_owner'`。
- 接受：片 A 的 `accept_v2` 只接受 operator/reviewer 的邀请。新迁移以新函数 `accept_v3` 取代（v2 收回 identity 的 execute），owner 邀请要求 `issued_via='invite_owner'` 且签发时的审批已消费（以邀请记录上的 `approval_id` 关联消费表核对）。接受后 owner 计入资格人数，单人模式立即失效（已有时间线触发器负责）；投票冷却从接受时刻起算。
- configurator 与邀请冲突的规则（V2）不变。

## 5. `issue_owner_reset`

- 执行时（审批已消费）才生成重置码、目标 `session_epoch+1`（R5）；码只出现在请求人这次执行的响应里，之后与片 B 相同（HMAC 存储、30 分钟、一次性、撤旧码）。
- 限速与 `issue_reset` 共用签发 owner 的桶，超限 429 + Retry-After 900。
- 只有两位 owner 且目标就是另一位时，无人可批（目标不计票），走运维通道 0198（W3）。

## 6. 清单与计数

- business-actions、capabilities（schema_hash）、system-catalog-names 各加 7 条；workbench-roles 版本 +1，owner 角色加 7（82→89，仍 ≤128），`slices.workbench_members` 加 7，均在 owner-only 集合。
- 计数：Actions 116→123；`test_workbench_read_pg` owner 89、伪造补 40；`test_stage_register_pg` (20, 123, 2)。
- HTTP：`workbench_actions_http.OPERATIONS` 加 7 个校验；审批相关结果沿用 NX-056 已有的 `waiting_approval` / `approval_consumed` / `policy_changed` 响应。

## 7. 测试（PostgreSQL，经实际 app）

1. 未发布策略：7 个 Action 均 `approval_unavailable`；策略被改成允许单人或带条件时同样拒绝。
2. 两位满 24 小时的 owner + 一位 operator：promote 走完“请求 → 另一 owner 批准 → 请求人重提执行”；执行前目标已被改角色则 `target_role_changed`。
3. 目标本人投票被拒（`target_cannot_approve`），目标换用自己的另一个 principal 投票同样被拒（AA2）。
4. 冷却：刚提升的 owner 在 24 小时内投票被拒；时间线以测试时钟推进（数据库侧用历史行的时间，不改系统时钟）。
5. 单人模式开启时自批无效（`not_applicable`）。
6. 最后一位 owner 不能被 demote/suspend/revoke；suspended owner 只能用 owner 版本处理。
7. issue_owner_reset：发起请求不踢会话、不生成码；执行时才生成并踢会话。
8. invite_owner：执行才有 token；接受后资格人数变化、单人模式失效；用 operator 邀请伪造 owner 接受被拒。
9. tail 复核：请求人执行后其会话仍有效；被改动的目标会话立即失效。
10. 回归：片 C 的成员变更、NX-056 gate、copy guard、definer、erasure coverage（新表或新列登记）。

## 8. 合入前可先做的部分（不依赖新迁移）

- HTTP 校验器与请求体形状、策略清单条目草案、清单条目草案（可以先写，但注册 Action 需要 capability 行，必须随迁移一起合入，所以先放在分支上不提交到批）。
- `vote_eligible_from` 的纯 Python 参照实现与单元测试（历史行 → 起点），供 SQL 实现对照。

## 9. 待确认

- **D1 冷却适用范围**：建议**所有** NX-056 策略统一加 24 小时冷却（自助设计 §7 的 X2 原意，NX-056 只调用这里提供的只读函数）。影响：首批三条策略（real_dispatch.set 开启、consumer.erase、retention.release_hold）部署后 24 小时内批不了；新加入的批准人 24 小时后才能投票。备选：只对 `workbench_member.*_owner` 生效（改动小，但其他策略仍可被“刚加入的第二身份”批准，即自助设计 §7 的残余风险加重）。需负责人决定。
- **D2 fail closed 的写法**：建议 handler 自查当前策略（§1），并由治理入口把本事务 gate 返回的 `approval_id` 作为 handler 的只读上下文（一张 owner 持有、事务内的临时记录，不用会话 GUC），handler 核对该审批的 capability 与策略版本。只自查策略也能 fail closed，加上 approval_id 核对是纵深防御，需要改 0327 的入口（copy guard 登记）。请 L6 选择是否要第二层。
- **D3 invite_owner 接受函数升 v3**：是否接受用新函数取代 `accept_v2`（同片 C 的做法）。
- **D4 降级 suspended owner**：建议允许（只改配置和历史，状态仍 suspended），这样停用的 owner 可以降级后再按非 owner 规则处理；备选是要求先 resume_owner。
- **D5 最后一位 owner 的计数口径**：建议只数 active 的 owner（suspended 的 owner 无法登录，不能作为“仍有 owner”）。
- **D6 首次投票告警**（自助设计 §7 V5 的缓解措施之一）：NX-056 目前没有“成员第一次投批准票”的告警。建议随本片在 `approval_solo_mode` 告警视图里加一条，属于 NX-056 的读模型改动，需 L3 同意。
