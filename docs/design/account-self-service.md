# 设计稿｜NX-058 工作台账号自助：邀请、找回、重置；随后企业单点登录

状态：**第 7 版**（负责人 2026-10-23 定 Q-D 为 **C**；片 B 已实现，见 `docs/implementation/NX-058-slice-B.md`；片 A、片 C 按本版实现；第 7 版处理 L6 第六轮的 AA1、AA2、AA4–AA6）（l2/account-self-service，基于 main 910f367f）。第 2 版处理了 L6 第一轮复核的 C1–C3、I1–I5、S1–S6；第 3 版处理第二轮的 R1–R6、T1–T4；第 4 版处理第三轮的 V1–V6、W1–W5；第 5 版处理第四轮的 X1、X2、Y1–Y6。逐条关闭说明见 §10。负责人 2026-10-22 Q4 选推荐 A：v0.2 先做邀请、找回与重置，第二阶段再做单点登录（OIDC）。

## 0. 现状（只读盘点）

| 能力 | 现状 | 位置 |
|---|---|---|
| 开户 | 没有自助。账号来自 `initial_identity.py`（argon2）；成员与授权只能由 configurator 通过受信配置写入（`workbench_roles.py apply`，0116/0140；0050 manifest 认的是 `nexloop_configurator` 的数据库登录、正文 sha256 和 `expected_revision`，并不验证 manifest 签名，Z3） | `nexloop_eios/workbench_roles.py:199-256`，`0116_nx028_staff_identity.sql` |
| 口令 | argon2id，只在 Python 中校验；SQL 拒收不以 `$argon2id$` 开头的哈希（0198:28）；历史保留 10 个。“至少 12 位”和“不与历史重复”在 Python：`identity_password_reset.py:17,32,70` | 0021、0024、0198 |
| 重置 | 只有运维通道：`control.nexloop_operator_reset_browser_password`（0198），由 `nexloop_identity` 执行，CLI 为 `identity_password_reset.py`。重置会让 `session_epoch+1` 使所有会话失效、清除锁定，并记一条 login event | 0198 |
| 状态值 | 成员 `invited\|active\|suspended\|revoked`，账号 `invited\|active\|disabled`：表里已有 invited，但没有任何代码路径签发邀请 | 0021 |
| 限速 | 0019 计数表。动作已预留 `password_reset`、`password_reset_complete`、`recovery`、`invitation_accept`、`oidc_start`。L3 的 59611c88 已加上按来源 IP 和按用户名（NFKC/casefold 后 HMAC）两个维度 | `browser_http.py:49-79`，`browser_rate_limits.py` |
| 会话失效 | 会话读取时比较账号的 `credential_revision` 和 `session_epoch` | 0027 |
| 邮件 | 没有任何 SMTP 或出站邮件实现；stage 上的邮箱多为 `.invalid` | — |
| 擦除 | NX-029 只覆盖 Consumer，员工账号不在擦除范围 | 0132 及以后 |
| 统一审批 NX-056 | 设计第 5 版已定（以实现分支 l3/unified-approvals-impl 1502005e 上的 `docs/design/unified-approvals.md` 为准，T3；实现进行中）：受治理入口挂起需审批的 Action，按 subject 禁止自批；**单人模式**由 owner 开启，资格人数当前恰好 1 且过去 24 小时一直不超过 1 时允许自批；资格人数按策略记在只追加的时间线上 | `docs/design/unified-approvals.md`（l3） |
| 审计链 NX-059 | 已在 stage 启用（release 41）：`runtime.audit_events` 进链并定时封印 | 0320 |
| 上游（1e363db） | 有通用实现，没有领域耦合，都**没有 vendored**：`identity/invitations.py`（372 行，sha256 令牌、24 小时、一次性）、`identity/resets.py`（363 行，30 分钟、一次性、口令历史）、`identity/recovery_codes.py`（487 行，HMAC 存储、用后撤销会话）、`identity/oidc.py`（1187 行，PKCE、nonce、JWKS 白名单）、`adapters/identity_oidc.py`（863 行）、SMTP relay（`adapters/identity_delivery.py`、`identity_admin_delivery.py`）。已 vendored 的有 `identity/models.py`、`admin_lifecycle.py`、`delivery.py`、`rate_limits.py`、`sessions.py`、`local_accounts.py` | 上游 `src/eios/identity/` |

## 1. 成员邀请（owner 发起）

- **受治理 Action**：`nexloop.workbench_member.invite` 邀请 operator 或 reviewer，`nexloop.workbench_member.invite_owner` 邀请 owner（§1b，V1；authority 都是 `human_owner`）。负责人在工作台填写用户名和角色，提交走现有受治理入口；tenant 和 actor 从会话推导，不接受请求体传入。
- **角色只能来自受治理的角色表**：`deploy/authorization/workbench-roles.v1.json` 中的 owner、operator、reviewer。邀请时冻结角色，授予的权限不得超出该角色（复用 0116 的 `nexloop_workbench_grant_guard`）。
- **`invite_owner` 需要审批，且单人模式不适用**（C1）：见 §7。`invite` 的请求体 role 只接受 operator 或 reviewer。
- **令牌**（I2、S1）：
  - 32 字节随机数，base64url 编码，只显示一次，在负责人的工作台页面上，形如 `/workbench/invite#<token>`。
  - 库里只存 `HMAC(identity_token_key, 'nexloop-invite/1|'||tenant||'|'||token)`。`identity_token_key` 是新的私有配置密钥，与限速用的 `browser_rate_key` 分开。库被读出时无法离线校验令牌，记录天然绑定 tenant 和用途；查找时用这个值做等值查询。
  - 前端读出 fragment 后立即用 `history.replaceState` 清除；接受页设 `Referrer-Policy: no-referrer`；令牌只放在 POST 请求体里，不进入前端错误上报。
  - 默认有效期 24 小时（S6；令牌由 owner 线下转交，窗口要短），可设 1–72 小时。一次性：被接受、被撤销或过期后都失效。
- **数据**：新表 `control.nexloop_workbench_invitations`，字段为 tenant、invitation_id、username_key、role、令牌 HMAC、issued_by（subject 和 principal）、issued_at、expires_at、state（issued / accepted / revoked / expired）、revoked_reason、accepted_principal。账号与成员先以 `invited` 状态写入，复用 0021 的状态值。
- **未认证端点的 tenant 来源**（R4）：接受邀请和完成重置都没有会话。tenant 取自服务端的工作台配置 `BrowserConfiguration.tenant_id`（一个工作台入口只服务一个租户，登录端点 `browser_http.py:77-91` 也是这样取 tenant 的），与 HMAC 输入、限速键使用同一个值。请求体里没有 tenant 字段；即使带了也拒收。将来如果一个入口服务多个租户，改为按受信的 Host 映射选租户，仍然不从请求体读取。
- **接受**：`POST /api/v1/workbench/auth/invitation/accept`，请求体为令牌和口令。
  - 限速（I3）：`invitation_accept` 只按来源计数（猜测防护靠来源限速加 256 位熵）。同一张令牌连续失败 5 次（例如口令不合规）即作废，需要重新签发；失败不计入任何用户名桶。
  - 口令规则：抽取 `identity_password_reset.py` 的 Python 校验（至少 12 位、不与历史重复）为共用函数（I5），口令只在 Python 中做 argon2id 哈希。
  - **在同一个事务里重查签发人**（I4）：签发人当前仍是该租户的 active owner；否则令牌作废，返回统一的失败响应。
  - **写入目标**（S2、R2）：新增 `control.nexloop_accept_workbench_invitation`，由 nexloop_owner 持有、security definer、只授权给 `nexloop_identity`，以邀请记录为唯一依据，登记到 copy guard。它在**同一个事务**里依次完成以下各步：
    1. 取 advisory lock（Y1）：与 NX-056 现行文本相同，是**租户级**的同一把锁，也就是 NX-056 成员变更和审批消费共用的那把，所以不存在“新成员已提交、消费已计完人数”的窗口。加锁顺序固定为：先取这把 advisory lock，再 `select … for update` 成员、账号、邀请等行。对 NX-056 的要求（N-1）：A7 实现时，消费事务也先取这把锁、再锁审批行；处理函数在同一事务内再次取锁属于重入，不产生新的等待，所以不会与成员变更事务形成死锁。片 B 已按这个锁键实现（`control.nexloop_workbench_member_lock`）。
    2. 重查邀请仍为 issued、未过期，签发人仍是 active owner；令牌改为 accepted。
    3. 账号转为 active（写入口令哈希），`nexloop_browser_memberships` 转为 active，`valid_from` 取接受时刻（`valid_from` 只用于登录，不用于投票冷却，见 V6）。
    4. **追加一版 `control.nexloop_workbench_configurations`**：内容是最新一版的 members 加上这名成员，角色取邀请时冻结的 role，roles 不变。0116/0140 的 `nexloop_configure_workbench` 本身不动，仍只接受 configurator 的 session。抽出一个内部函数 `control.nexloop_workbench_configuration_append(tenant, roles, members, source)`，**不授权给任何角色**，只由 owner 持有的函数调用；它做与 configure 相同的校验（角色表、`nexloop_workbench_grant_guard`、成员不能是顾客），`source` 记为 `invitation:<id>`。configurator 的公开入口改为调用它，行为不变。这样 0140 的“只有 configurator 能写成员”变为“只有 configurator，或者以一条有效邀请为依据，能追加成员”，不向 API 或 identity 角色开放任意写入。
    5. 第 3、4 步命中 NX-056 挂在这两张表上的触发器，**在同一事务内**向资格人数时间线追加一行（新成员立即计入，见 R1）。
    6. **角色授权事实**（Q-D = C，负责人 2026-10-23）：按 §1c，把 configurator 发布的该角色授权模板实例化给新成员：只替换 principal，并重算依赖它的摘要；同时写一条实例化记录。
    7. 记一条 login event `invitation_accepted`。
  - **与 configurator 全量 apply 的关系**（V2、X1）。选“apply 发现冲突就拒绝”，不做自动合并：
    - 配置的每个版本记录 `source`，取值为 `configurator`、`invitation:<id>` 或 `action:<intent_id>`。`workbench_roles.py apply` 写入新版本前，先读最新一版，并找出自上一次 configurator 版本以来，由 `invitation:*` 或 `action:*` 版本造成的成员或角色变化（加入、提升、降级、停用、撤销）。
    - 如果 apply 的结果会让其中任何一项回到变化前的状态，或者产生不同的结果，apply 就拒绝，返回 `members_changed_by_governed_actions`，列出这些 principal 及其当前角色。运维要先把成员文件改成与之一致，再 apply。
    - 这样，用一份旧成员文件 apply 一次，不会悄悄撤销一次已经批准的 promote 或 demote，也不会悄悄移除经邀请加入的成员。
    - apply 不会悄悄去掉经邀请加入的成员。要移除，只能用 §1b 的受治理 Action，或者先修改成员文件再 apply（运维通道，单独审计）。
    - 不做自动合并的理由：成员文件仍是 configurator 路径的唯一事实来源，“文件里没有但库里有”这种分歧必须让人看到。
    - **configurator 点名撤销不受 X1 限制**（Z2）：`workbench_roles.py apply_revocations`（`--revoke-member`）明确点名的 principal 算一次显式移除。即使该成员是经邀请或受治理 Action 加入或变更的，这次 apply 对它也不触发 X1，并在审计中单独列出；对未点名的其他成员，X1 照常检查。
    - 测试：接受邀请之后，用不含该成员的文件 apply 会被拒绝；`promote_owner` 和 `demote_owner` 执行之后，用旧角色的文件 apply 会被拒绝；两种情况下成员、角色和授权都不变。把文件改成一致后 apply 成功，版本内容不变。经邀请加入的成员被 `--revoke-member` 点名撤销时成功，其他经邀请加入的成员不受影响。
  - 接受时记录来源摘要；与签发者签发时会话的来源相同，就在审计里标记 `same_source_as_issuer`（C2；只作信号，不阻断）。
- **资格人数与投票资格分开**（C2、R1）：
  - **资格人数**（决定单人模式是否生效）：成员一转为 active 就计入，时间线在接受的同一事务里追加一行，单人模式**立即**失效，并写审计和 `approval_solo_mode` 告警。这与 NX-056 “人数只会偏大才安全”的口径一致。
  - **投票资格**（能否作为批准人）：`control.nexloop_workbench_role_history` 中该 subject 最近一次**进入**该策略批准角色的时间，加 24 小时（V6、X2；见 §7），执行时重查。
  - 这样 owner 自己接受邀请造出的第二身份，既不能马上作为第二批准人，也会立即关掉单人模式。剩余风险见 §7 的 T1。
- **审计**：签发、撤销、接受都写 `runtime.audit_events`，并自动进 NX-059 审计链。接受事件关联签发事件（同一 invitation_id，同时记签发人和接受后的 principal）。审计里不含令牌、口令，也不含用户名摘要（S3）。
- 负责人可以撤销邀请：Action `nexloop.workbench_member.revoke_invitation`。

## 1b. 成员管理 Action（V1、V4）

现状：成员的降级、停用和撤销只能由 configurator 完成（`workbench_roles.py revoke_member`，以及修改成员文件后 apply）。本任务新增受治理 Action，并**按目标角色拆分**。NX-056 的策略条件只能是“请求体字段等于常量”，不能拿目标成员当前的角色作条件；所以凡是取决于目标当前角色的动作，都拆成 owner 专用和非 owner 两个 Action，执行时由服务端重查目标当前的角色（V1）。

| Action（`nexloop.workbench_member.*`） | 用途 | 审批 |
|---|---|---|
| `invite` | 邀请 operator 或 reviewer（请求体 role 只接受这两个） | 不需要 |
| `invite_owner` | 邀请 owner | 需要，单人模式不适用 |
| `change_role` | 在 operator 和 reviewer 之间改角色 | 不需要 |
| `promote_owner` | 把 operator 或 reviewer 提升为 owner | 需要，单人模式不适用 |
| `demote_owner` | 把 owner 降为 operator 或 reviewer | 需要，单人模式不适用 |
| `suspend` / `resume` / `revoke` | 停用、恢复、撤销 operator 或 reviewer | 不需要 |
| `suspend_owner` / `resume_owner` / `revoke_owner` | 停用、恢复、撤销 owner | 需要，单人模式不适用（`resume_owner` 也需要，Z1） |
| `issue_reset` | 对 operator 或 reviewer 签发重置码 | 不需要 |
| `issue_owner_reset` | 对 owner 签发重置码 | 需要，单人模式不适用 |
| `revoke_invitation` | 撤销未用的邀请 | 不需要 |
| （第二阶段）`link_external` / `link_owner_external` | 建立外部身份关联 | 后者需要，单人模式不适用 |

- **目标字段写死**（AA2）：每个以成员为目标的 Action，请求体里只有一个必填的目标字段 `target_principal`，取值为该租户的 principal。处理函数解析目标 subject 时，调用与 NX-056 `control.nexloop_approval_target_subject` **同一个**解析函数 `control.nexloop_approval_subject_of(tenant, principal)`；解析不到就直接拒绝（`target_not_active`），不像审批那样退回原值。发布这些 Action 的审批策略时，`target_subject_field` 必须设为 `target_principal`。测试：目标本人换用自己的另一个 principal 投票，仍被 NX-056 拒收（`target_cannot_approve`）。
- **执行时重查目标角色和状态**：每个 Action 在执行事务里，持有 §1 的那把租户级锁，读取最新一版配置中目标当前的角色，以及目标 membership 的状态。非 owner 版本发现目标是 owner，或者 owner 版本发现目标不是 owner，都直接拒绝（`target_role_changed`），请求人需要改用另一个 Action 重新发起。所以“请求时是 operator、执行前被提升为 owner”不会绕过审批。过渡期的内建规则也按同样的方式判断。
- **目标状态**（Y5）：
  - `promote_owner`、`issue_owner_reset`、`issue_reset`、`change_role` 要求目标为 active，否则拒绝（`target_not_active`）；
  - 已 suspended 的 owner 仍按 owner 处理：恢复、撤销、降级都只能用 owner 专用 Action，不能用非 owner 的 `revoke` 撤销；
  - 已 revoked 的成员不能再成为任何 Action 的目标，只能重新邀请。
  
  以上都补测试。
- 写入同样经由 §1 的内部 append 函数追加配置版本（`source` 记为 `action:<intent_id>`），并按 §4 的失效条件让会话失效、撤销未用的邀请和重置码。
- **降权要先撤授权**（Z0，写法见 §1c）：`nexloop_configure_workbench` 写入新版本后，会检查每一条现行的 Human 授权是否仍在持有人的新角色之内，超出就整体拒绝（0116:112-118），append 做同样的检查。所以 `demote_owner`、`change_role`、`suspend(_owner)`、`revoke(_owner)` 必须在**同一事务**里：先写入超出新角色的授权的撤销集（与 `revoke_member` 相同的空集授权），再追加配置版本。不能跳过这项检查，否则被降级或撤销的人会保留原有授权，因为执行路径 `assert_action_authority` 不会按当前配置角色重查。撤销与恢复的写法见 §1c。
- 最后一位 active owner 不能被降级、停用或撤销，一律直接拒绝。
- **与 configurator 的关系**：configurator（修改成员文件后 apply、`revoke_member`）仍然可以直接降级或撤销任何成员，包括 owner。这是运维通道，与 0198 一样不受审批约束，但每次使用都在审计中单独列出（§3、§7）。

## 1c. 授权写入：角色模板实例化（Q-D = C）

负责人 2026-10-23 定 Q-D 为 C。ADR-020 §3 的修订见该 ADR 的“§3 修订（2026-10-23）”。

- **模板由谁写**：configurator 经 0050 发布每个工作台角色的**授权模板**，这是 manifest 新增的一类内容 `workbench_role_templates`。模板由 `compile_member` 用占位 principal 和占位 subject 生成，内容就是这个角色在 0050 中应写的全部 Human 授权事实（grants 等），只是主体换成了占位符。模板带版本号和整份内容的 sha256，存在只追加表 `control.nexloop_workbench_role_templates` 中（tenant、role、version、facts、digest、发布记录）。“角色授予什么”只能由 configurator 编写，可以重放，与今天的成员授权同源。
- **SQL 做什么**：只有一个内部函数 `control.nexloop_workbench_grants_instantiate(tenant, principal, subject, role)`，不授权给任何角色。它取该角色当前版本的模板，只做两件事：
  1. 把占位 principal 和占位 subject 替换为目标成员的；
  2. 按写明的规范形式重算依赖 principal 的那一个摘要（与 `compile_member` 的 `_digest` 相同）。
  
  然后经 0050 写授权事实的同一个内部写入函数写入，epoch 照常加一。SQL 不编写授权内容。
- **撤销**：同一个函数的撤销形式写空集，与 `revoke_member` 的输出相同。
- **调用点只有受治理事务**，都以邀请或 intent 为依据（AA1）：

  | 方向 | 调用的 Action |
  |---|---|
  | 增加 | 接受邀请、`promote_owner`、`change_role`、`resume`、`resume_owner` |
  | 撤销 | `demote_owner`、`change_role` 的旧角色部分、`suspend`、`suspend_owner`、`revoke`、`revoke_owner` |
  
  停用会撤掉授权，所以恢复时要按当前角色的模板**重新授予**（AA1）。同一事务的顺序固定为：先撤销超出新角色的部分，再增加新角色的部分，最后追加配置版本（Z0）。
- **实例化记录**：`control.nexloop_workbench_grant_instances`，只追加，每行是 (tenant、principal、role、模板版本、模板摘要、写入事实的摘要、来源 invitation_id 或 intent_id、时间)；撤销也记一行。审计不借用 `configuration_publications`，另写 audit_event，并引用这条记录。
- **模板换版**（AA4）：configurator 发布新版本模板时，**同一次 0050 发布**按新模板重写所有当前经实例化持有该角色的成员的授权，并为每人写一条新的实例化记录（来源 `template:<version>`）。所以已经实例化的授权不会停在旧版本上。X1 照常检查成员文件与配置的分歧，与模板无关。
- **黄金测试**：
  - 每个角色的 SQL 实例化结果，与 `compile_member` 对同一成员的输出逐字节相同（规范化 JSON 比较）；
  - 模板换版后，重写的结果与按新模板 `compile_member` 的输出相同；
  - 撤销形式与 `revoke_member` 的输出相同。

## 2. 找回与重置（不依赖邮件）

- **主路径：负责人为成员签发一次性重置码**。按目标角色拆成两个 Action（§1b、V1）：
  - `issue_reset`：目标是 operator 或 reviewer，不需要审批，但按签发的 owner 限速，并且写审计、通知成员；
  - `issue_owner_reset`：目标是 owner，走与“提升为 owner”相同的审批，单人模式不适用（C3，见 §7）。
  
  执行时服务端重查目标当前的角色。
  - **只有两位 owner 时**（W3）：能批准 `issue_owner_reset` 的另一位 owner，正是被重置的目标本人。他如果因为忘记口令而需要重置，就无法登录批准，所以这种情况走运维通道（0198）。运行手册写明这一点。
  - 目标是“仅 SSO”的成员时拒绝签发（S5，第二阶段起生效）。
  - 重置码是 128 位随机数，只显示一次，由负责人线下转交本人。库里存 `HMAC(identity_token_key, 'nexloop-reset/1'||'|'||tenant||'|'||code)`（I2）。
  - 有效期 30 分钟，一次性。新签发一张会撤销同一账号所有未用的旧码（I4）。
  - 不需要审批时（目标是 operator 或 reviewer），签发时**立即**让该账号的所有现有会话失效（`session_epoch+1`），防止被盗用的会话继续存活。
  - 需要审批时（目标是 owner，R5）：提交请求只写审计，**不**生成重置码，也**不**让会话失效；等审批通过后，由请求人重新提交、执行时，才生成重置码并执行 `session_epoch+1`。所以只发起请求，无法反复把对方踢下线。
- **完成重置**：`POST /api/v1/workbench/auth/password/reset`，**请求体只有重置码和新口令，没有用户名**（I3）。
  - 码的 128 位熵足以定位记录，所以没有用户名的枚举面，也没有“攻击者耗尽某个用户名的桶，让成员无法完成重置”的锁死问题。
  - 限速：`password_reset_complete` 只按来源计数；同一张码连续失败 5 次（口令不合规）即作废。
  - 失败一律返回 400 `reset_invalid`（码错误、过期、已用都一样），响应时间用假哈希校验对齐。
  - 重置的失败**不计入** `local_login` 的锁定计数，所以重置流程不能被用来把账号锁死（I3）。
  - 在同一事务里重查签发人仍是 active owner，否则作废（I4）。
  - 写入时复用 0198 的效果：新哈希、口令历史、会话失效、清除锁定。
- **成员知情**（C2）：
  - 重置完成后的提示（R3、V3）：完成重置后的 **7 天内，每次登录都显示**“你的口令于某时经某人签发的重置码被改过”（含签发人的显示名），**不能提前关闭**，到期自动结束。这样即使接管者先登录、再改一次口令，也消不掉这个提示；
  - 成员本人的“账号安全”页长期显示最近 90 天的重置记录（签发人、时间），本人不能删除；
  - 这些提示只是告知手段。真正的约束是所有 owner 都能看到的成员页记录和 NX-059 审计链；
  - 完成重置时也记录来源摘要，与签发人签发时会话的来源相同，就在审计里标记 `same_source_as_issuer`，与接受邀请对称；
  - 审计事件关联签发人和完成人（同一 reset_id）；
  - 工作台成员页向所有 owner 显示最近 30 天的重置记录：签发人、目标、时间、是否完成。
- **成员自助“忘记口令”**：v0.2 不提供，因为没有邮件出口，无法证明本人身份。登录页显示“请联系工作台负责人”。负责人本人忘记口令时，走 §3 的运维通道。
- **将来接入邮件出口的扩展点**：
  - 重置码和邀请令牌的“投递”抽象成接口 `IdentityDelivery.deliver(kind, principal, secret_once)`。v0.2 的实现是“只显示给签发者”。
  - 将来移植上游的 SMTP relay（`adapters/identity_delivery.py`），只投递到已验证的邮箱。投递 worker 使用 claim/ack，信封不留密钥（上游 `password_reset_requests.py` 的契约）。
  - 届时再开放成员自助“忘记口令”：按来源限速，无论用户名是否存在都返回相同的响应。

## 3. 与运维重置工具（0198）的关系

- **保留 0198 作为紧急通道**：负责人本人被锁死、或租户唯一的 owner 不可用时使用。它只能在数据主机上由运维用 `nexloop_identity` 执行，不改动。
- 运行手册写明：日常使用 §2 的负责人签发重置码；0198 只用于紧急情况，每次使用后在 login event 里可以查到，审计时单独列出。
- 两条路径共用口令规则（`identity_password_reset.py` 的 Python 校验，I5）和“重置后会话失效”的语义。实现时把 0198 的 SQL 效果抽成一个内部函数，供两者调用；不改 0198 已发布的签名，而是用新迁移重定义并登记到 copy guard。
- 0198 运维重置同时撤销该账号所有未用的邀请和重置码（I4）。
- configurator 的成员 apply 和 `revoke_member` 也属于运维通道（V4）：可以直接改任何成员的角色，包括 owner，不受审批约束；每次使用都在审计中单独列出。apply 与邀请成员冲突时直接拒绝（§1 的 V2）。

## 4. 安全

- **令牌**：邀请令牌和重置码统一用 `HMAC(identity_token_key, 用途|tenant|令牌)` 存储（I2），密钥放在私有配置中；按这个值做等值查询。令牌不进日志、审计、fixture，也不进 URL query（只放在 fragment 或请求体里，S1）。
- **限速**（I3）：
  - `invitation_accept`、`password_reset_complete` 只按来源计数，来源 IP 只在受信代理之后读取（L3）；
  - 单张令牌或码连续失败 5 次即作废；
  - 不设按用户名的计数，所以不会出现“他人耗尽桶、锁死成员”；
  - 重置失败不进入 `local_login` 的锁定计数；
  - `issue_reset`、`invite` 按签发 owner 计数，防止被盗的 owner 会话大量签发或借此反复踢人。
- **枚举防护**：接受邀请和完成重置都不带用户名；失败响应完全一致；响应时间用假哈希校验对齐。
- **失效条件**（I4）。以下事件发生时，撤销该账号或该成员所有未用的邀请和重置码，并让会话失效（`session_epoch+1`）：
  - 0198 运维重置；
  - 成员被 suspended 或 revoked；
  - 角色变更；
  - 账号自行改口令；
  - 新签发一张重置码（只撤销旧码）；
  - 完成重置。

  另外，接受邀请和完成重置时，都在同一事务里重查签发人当前仍是 active owner。
- **会话**：“撤销成员只清授权、不撤会话”是现状的缺口（盘点结果：`workbench_roles.py:175` 的 `revoke_member` 只处理授权；S4 要求用测试固定）。片 C 先写一个测试证明现状，再补上：角色变更或撤销成员时，同步 `session_epoch+1`。
- **审计**：签发、撤销、接受、重置、0198 紧急重置都写审计事件，并自动进 NX-059 审计链。审计只记动作、主体和对象的 principal、时间、关联 id（invitation_id / reset_id），不记秘密，**也不记用户名或其摘要**（S3），以免擦除与防篡改冲突。
- **擦除传播**（S3，范围待 Q-A 确认）。员工离职擦除时：
  - 停用账号；
  - 清除口令哈希和口令历史；
  - 清除 login events 的来源摘要和用户名摘要；
  - 删除邀请和重置记录（包括 `username_key`）。

  principal 保留，以维持审计可追溯；审计链里本来就只有 principal 和动作。

## 5. 单点登录（OIDC，第二阶段：只定接口与范围）

- **范围**：企业 IdP 的授权码流程，带 PKCE（S256）、state（5 分钟、一次性）、nonce；用 JWKS 白名单校验 ID token；只支持 OIDC，不做 SAML（上游也没有 SAML）。
- **账号关联**：`external_identity(issuer, subject) → principal`。只有负责人能建立关联（受治理 Action `workbench_member.link_external`，或者在邀请时指定“仅限 SSO”）。目标是 owner 的关联，走与“提升为 owner”相同的审批，单人模式不适用（S5）。首次登录**不自动开户**。
- **接口**：
  - `GET /api/v1/workbench/auth/oidc/start?provider=<id>`，限速动作 `oidc_start`，已预留；
  - `GET /api/v1/workbench/auth/oidc/callback`；
  - 配置：私有文件中的 provider 列表，包括 issuer、client_id、client secret 的文件路径、允许的 JWKS URI、claim 映射。
- **移植**：上游 `identity/oidc.py` 与 `adapters/identity_oidc.py` 是通用实现，按 ADR-006 vendored 并记录 provenance。fail-closed 的规则保持不变：只允许 HTTPS，拒绝私网和链路本地地址，读取文件时做加固。
- **与本地口令并存**：每个成员可以设为“本地口令”“仅 SSO”或“两者皆可”，由负责人决定。

## 6. 上游可移植性（ADR-006）

| 上游 | 处理 |
|---|---|
| `identity/invitations.py`、`identity/resets.py` | **移植语义**：令牌哈希、有效期、一次性、口令历史。NexLoop 的状态和授权模型不同（0116 成员、0050 授权事实），直接 vendored 需要大量适配，所以写成新模块，在 provenance 里记录参照的源文件 |
| `identity/recovery_codes.py` | 暂不采用（v0.2 用负责人签发的重置码代替自助恢复码）；HMAC 存储方式参照它 |
| `identity/oidc.py`、`adapters/identity_oidc.py` | 第二阶段整体 vendored，带 provenance 和 sha256 |
| SMTP relay（`adapters/identity_delivery.py` 等） | 邮件出口落地时 vendored |
| 已 vendored 的 `models.py`、`admin_lifecycle.py`、`rate_limits.py`、`sessions.py` | 复用其中的模型和状态枚举 |

## 7. 与统一审批（NX-056）的关系

对齐 NX-056 设计第 5 版（l3/unified-approvals-impl 1502005e 上的文档，§15 单人模式与资格人数时间线，A7 的 `solo_mode` 属性）。

- **需要审批的动作**：§1b 中所有 owner 专用的 Action：`invite_owner`、`promote_owner`、`demote_owner`、`suspend_owner`、`resume_owner`、`revoke_owner`、`issue_owner_reset`，以及第二阶段的 `link_owner_external`（Z1 补上 `resume_owner`）。
  - 这些策略都**不带条件**：Action 本身就是 owner 专用的。目标角色由服务端在执行时重查，不依赖请求体（V1）。
  - quorum 都是 1，批准角色是 owner，由另一位 owner 批准；都标为 `solo_mode: not_applicable`，开启单人模式也不能自批。
  - **目标本人不计入批准人**（Y4、Z1）：`issue_owner_reset`、`demote_owner`、`suspend_owner`、`resume_owner`、`revoke_owner` 的目标本人不能批准针对自己的请求，不论有几位 owner。
    - 这条规则放进 **NX-056 的计票**，与“请求人不能自批”放在一起：决定时拒收目标本人的票；消费时这一票不计入，有效票不够就退回 waiting。不放在处理函数里，因为处理函数在“已消费”之后才调用，目标本人抢先投一票，就能每次把针对自己的请求作废。
    - 这需要 NX-056 的策略或 Action 声明如何解析目标 subject（N-1）。
    - 只有两位 owner 时，唯一能批准的人就是目标本人，审批无法完成，统一按 W3 走运维通道。
  - 理由：新增、接管、移除一位 owner，改变的正是单人模式成立的前提，或者其他 owner 的否决能力，不应由单人模式自己批准（C1、R6）。
  - 只有一位 owner 的租户做这些事，一直只能走运维通道：0198 加 configurator 的成员 apply，并在审计中单独列出。NX-056 落地前后口径一致。
  - 最后一位 owner 不能被降级、停用或撤销。
- **资格人数与投票资格**（R1、V6、X2）：
  - 成员一转为 active，并在配置中持有某策略的批准角色，就计入该策略的资格人数，单人模式立即失效。人数仍取 NX-056 的时间线。
  - **投票资格**的唯一数据来源是本任务新建的 `control.nexloop_workbench_role_history`，**长期保留**：
    - 只追加，任何角色都不能改；
    - 由 append 函数和 configurator 的入口写入，每行是 (tenant, world, subject, role, 事件, 时间)。world 对工作台成员固定为 `real`，与 NX-056 按 (tenant, world) 计数对齐（Z4）。
    - 事件只有两种（Z4）：**开始持有**一个角色、**停止持有**一个角色。停用（suspended）记为停止持有，恢复（resume）记为开始持有；撤销、降级记为停止持有原角色，并开始持有新角色（如有）。
  - NX-056 的时间线只记人数和集合摘要，查不出“某个 subject 首次进入的时间”，所以不用它判断投票资格。
  - 投票资格起点（Z4，统一口径）= 该 subject **当前这段连续持有该策略批准角色并集的起点**，加 24 小时：
    - 在并集内换角色不中断，例如批准角色是 {owner, operator} 时，从 operator 升为 owner 不重新计时；
    - 跨策略版本计算：策略换版本，不重新计时；
    - 停止持有并集中的全部角色（包括被停用），之后再开始持有，就重新计时；所以停用很久的人恢复后，也要再等 24 小时。
    
    刚被提升为 owner 的老成员，对 owner 策略也要等 24 小时。
- **新成员加入后 24 小时内**（W2、Y3）：这一条只针对“只有一位 owner、正在使用单人模式，又加入了第一位新成员”的场景。此时单人模式已经失效，而新成员还不能投票，所以 24 小时内所有需要审批的动作都**无法批准**，只能等待或走运维通道。这是安全方向。已有其他满 24 小时的批准人时，不受影响。这个场景下，工作台常驻提示“单人模式已失效，新成员将于某时起可以审批”，运行手册写明这一点。
- **部署后的 24 小时**（Y6）：存量成员在角色历史表中没有更早的记录，以迁移部署时刻作为“进入”时间，所以**部署后 24 小时内，所有 owner 专用 Action 都批不了**，只能走运维通道。运行手册和上面的常驻提示一并说明。
- **残余风险，需负责人知情接受**（V5，改写 T1）：
  - owner 可以自己接受一份 operator 邀请，造出第二身份。24 小时后，这个身份可以作为 NX-056 **其他策略**的第二批准人，与 owner 凑成两人审批。前提是这些策略的批准角色包含 operator 或 reviewer，例如首批的 `real_dispatch.set`、`consumer.erase`、`retention.release_hold`。这条路恰好绕开了单人模式的告警和提示，因为接受邀请时单人模式已经失效。
  - owner 专用 Action 的批准角色只有 owner，所以这条路对它们走不通：第二身份要成为 owner 需要 `promote_owner`，而批准它的必须是另一位已有的 owner。
  - 缓解措施：
    - 签发邀请和接受邀请时的 `same_source_as_issuer` 信号，进入 `approval_solo_mode` 同一告警视图；
    - 一个成员**第一次投批准票**时告警一次；
    - 所有 owner 都能看到成员页上的签发记录和审计链；
    - 建议负责人在配置 NX-056 首批策略时，把批准角色限定为 owner。
  - 技术上无法区分“一个人的两个账号”和“两个人”，所以这一风险写入 ADR，由负责人知情接受。
- **NX-056 落地前的过渡**（T2、V6）：上述 owner 专用 Action 在本任务中先以内建规则实现：需要另一位 active owner 在其本人会话中确认。确认人的资格按以下条件判断，执行时重查：
  - 按 subject 区分，不是请求人；
  - 当前是 `nexloop_browser_memberships` 中 status=active 的成员，并在最新一版配置中持有 owner 角色；
  - 投票冷却：按上面的角色历史表，取最近一次进入 owner 的时间加 24 小时。存量成员以迁移部署时刻为起点。
  
  NX-056 落地后，投票资格**仍然读这张角色历史表**（X2）；NX-056 只需要在判断投票资格时调用它提供的只读函数。

## 8. 分片与工作量

| 片 | 内容 | 量 |
|---|---|---|
| A | 邀请：表、HMAC 令牌、Action（invite / revoke_invitation）、接受端点、`nexloop_accept_workbench_invitation`（受控授权写入）、签发人重查、限速、审计关联、测试 | 3 天 |
| B | 负责人签发重置码、只带码的完成端点、单码失败作废、不进入登录锁定、与 0198 共用 SQL 效果和 Python 口令校验、成员知情提示、运行手册 | 2.5 天 |
| C | §1b 的成员管理 Action：`change_role`、`promote_owner`、`demote_owner`、`suspend(_owner)`、`resume(_owner)`、`revoke(_owner)`、`invite_owner`、`issue_owner_reset`；降权时同事务先写撤销集（Z0，写法依 Q-D）；`--revoke-member` 例外（Z2）；执行时重查目标角色；最后一位 owner 保护；角色历史表；失效条件全集（I4）；角色变更或撤销时让会话失效（先写测试固定现状，S4）；owner 专用 Action 的内建双人确认和 24 小时投票冷却（过渡规则）；apply 冲突拒绝（V2） | 4 天 |
| D | 工作台前端：成员页的邀请和重置入口、最近重置记录、接受邀请页（清 fragment、no-referrer）、重置页、登录后的重置提示 | 2 天 |
| E | （待 Q-A）员工离职擦除 | 1 天 |
| F | 第二阶段 OIDC：vendored 上游、关联 Action（owner 目标需审批）、回调、配置、测试 | 4–5 天 |

v0.2（A–D）约 12 天（第 6 版之前是 11 天）：第 2 版的 C2、C3、I4 带来 +2 天，第 3 版的 R1、R2 带来 +0.5 天（T4），第 4 版的 V1、V2、V4 给片 C 带来 +1.5 天，主要是拆分后的成员管理 Action 和 apply 冲突拒绝；第 6 版的 Z0、Z2、`resume` 给片 C 再加 1 天，合计约 12 天。Q-D 定为 C：0050 新增角色模板一类内容、实例化函数与记录、模板换版重写、黄金测试、修订 ADR-020 §3，约 +1.5 天，合计约 13.5 天。E 视负责人决定而定，F 另行排期。A 与 B 可以并行，C 依赖 A 的授权写入入口，D 依赖 A 和 B 的端点。NX-056 落地后，把 C 的内建规则迁移为策略，约 0.5 天，计入 NX-056 的接入。

## 9. 待负责人或 L6 确认

- **Q-A**：员工离职擦除是否纳入 v0.2。
- **Q-B**：邀请默认有效期。第 2 版按 S6 改为 24 小时（可设 1–72 小时），请确认。
- **Q-C**：已按 C1 定为“单人租户新增 owner、重置 owner 口令只能走运维通道”，不再是开放问题；负责人如有异议可以推翻。
- **N-1（对 NX-056 的需求，需转给 L3）**：
  - 策略模型支持 `solo_mode: not_applicable`；
  - 资格人数：成员一转为 active 就计入，时间线只记人数；
  - 投票资格（对接项，AA6：0322 的消费计票目前还没调用它；片 C 之后对接，并补“决定时可以投票、消费前资格失效则不计入”的测试）：调用本任务提供的只读函数 `control.nexloop_workbench_vote_eligible_from(tenant, world, subject, roles)`，它取角色历史表中“当前这段连续持有批准角色并集的起点”加 24 小时。**决定时和消费时都调用**。只作用于能否投票，不作用于人数；不使用 `valid_from`，也不使用 NX-056 时间线（R1、V6、X2、Z4）；
  - 目标本人不计入批准人：策略或 Action 声明目标 subject 的解析方式，决定时拒收，消费时不计入（Z1）；
  - 工作台配置的追加入口 `nexloop_workbench_configuration_append` 也要触发时间线；
  - 成员变更与消费使用同一把 advisory lock，按 NX-056 现行文本为租户级；加锁顺序为先取 advisory lock、再锁行（R2、Y1）；
  - 一个成员第一次投批准票时告警一次（V5）。
- **Q-D（已定：C，负责人 2026-10-23）**：受治理入口通过“configurator 发布的角色模板 + SQL 实例化”增加和撤销 Human 授权，见 §1c。ADR-020 §3 已修订（同一份 ADR，不另写并列 ADR）。第 6 版中 A、B 两个方案的比较不再重复，见 L6 第五轮复核报告。

## 10. L6 复核关闭说明（第 2–6 版）

| 编号 | 处理 |
|---|---|
| C1 | 选 (a)：邀请或提升为 owner、对 owner 签发重置码、owner 外部身份关联，都标为单人模式不适用；单人租户一直只能走运维通道；对 NX-056 的需求列为 N-1（§7） |
| C2 | 新成员不能立即作为第二批准人（投票冷却 24 小时，起点见 V6、X2）；资格人数在接受时立即计入，单人模式立即失效（R1）；接受时记录来源，与签发者相同则标记；重置完成后 7 天内每次登录都显示提示（V3）；审计用 invitation_id / reset_id 关联签发人与完成人；成员页向所有 owner 显示最近的重置记录（§1、§2、§7） |
| C3 | 对 owner 的 `issue_reset` 走与提升为 owner 相同的审批，单人模式不适用；对其他角色签发时写审计、通知成员，按签发 owner 限速（§2、§7） |
| I1 | 现状表改为 NX-056 第 5 版（ed0d58d1）的事实，§7 对齐它的单人模式和资格时间线 |
| I2 | 邀请令牌和重置码统一为 `HMAC(identity_token_key, 用途\|tenant\|令牌)`，密钥与 `browser_rate_key` 分开（§1、§2、§4） |
| I3 | 完成重置的请求体只带码，不带用户名；接受邀请和完成重置只按来源限速，单张令牌或码失败 5 次作废；重置失败不进入 `local_login` 的锁定计数（§1、§2、§4） |
| I4 | 失效条件全集（0198 运维重置、成员停用或撤销、角色变更、自行改口令、新签发重置码、完成重置）；接受和完成时在同一事务里重查签发人（§1、§2、§4） |
| I5 | 口令规则的出处改为 `identity_password_reset.py:17,32,70`，共用的是这段 Python 校验（§0、§1、§3） |
| S1 | 前端读出 fragment 后立即 `replaceState` 清除，接受页 `Referrer-Policy: no-referrer`，令牌只放在 POST 请求体里，不进错误上报（§1） |
| S2 | 新增 `control.nexloop_accept_workbench_invitation`：nexloop_owner 持有、security definer、只授权给 nexloop_identity，以邀请记录为唯一依据，角色冻结，登记到 copy guard；不放宽 0140（§1） |
| S3 | 擦除范围补上 login events 的用户名和来源摘要，以及邀请的 `username_key`；审计和审计链里不放用户名摘要（§1、§4） |
| S4 | 片 C 先写测试固定“撤销成员不撤会话”的现状，再修（§4、§8） |
| S5 | owner 目标的 `link_external` 需要审批；对仅 SSO 的成员拒绝签发重置码（§2、§5、§7） |
| S6 | 邀请默认有效期改为 24 小时，可设 1–72 小时（§1、§9 Q-B） |
| R1 | 资格人数与投票资格分开：成员一转为 active 就计入人数，单人模式立即失效；24 小时冷却只作用于投票（§1、§7、N-1） |
| R2 | 写清了写入目标：membership 转为 active；用不授权给任何角色的内部 append 函数追加工作台配置版本（configurator 入口改为调用同一函数，不放宽 0140）；在同一事务里命中 NX-056 时间线触发器；使用同一把 advisory lock；授权事实走 Q-D 的 (A) 或 (B)；测试要求接受邀请后时间线立即多一行（§1） |
| R3 | 已由 V3 取代：提示在完成重置后 7 天内每次登录都显示，不能提前关闭；完成重置时也记录 `same_source_as_issuer`（§2） |
| R4 | 未认证端点的 tenant 取自服务端工作台配置 `BrowserConfiguration.tenant_id`，与登录端点相同；请求体中的 tenant 一律拒收（§1） |
| R5 | 需要审批时，提交请求只写审计；重置码生成和 `session_epoch+1` 都在审批通过后、执行时才发生（§2） |
| R6 | 降级、停用、撤销另一位 owner 纳入同一组策略，单人模式不适用；最后一位 owner 不能被降级、停用或撤销（§7） |
| T1 | 残余风险（自造第二身份等满 24 小时后凑成两人审批）写入 §7，由负责人知情接受 |
| T2 | 已由 V6、X2 取代：过渡期和 NX-056 落地后都按 `control.nexloop_workbench_role_history` 中最近一次进入批准角色的时间加 24 小时计算，存量成员以部署时刻为起点（§7） |
| T3 | 引用已改为 l3/unified-approvals-impl 1502005e 上的文档（§0、§7） |
| T4 | 工作量改为约 9.5 天，Q-D 选 (A) 再加 0.5 天（§8） |
| V1 | 凡是依赖目标当前角色的动作，都拆成 owner 专用和非 owner 两个 Action（§1b）；执行时在同一把锁下重查目标角色，不一致就拒绝；策略不带条件，过渡规则也按同样的方式判断（§1b、§2、§7） |
| V2 | apply 发现最新一版里有经邀请加入、且不在成员文件中的成员时拒绝（第 5 版起改为 X1 的 `members_changed_by_governed_actions`，覆盖所有受治理的成员和角色变化），不做自动合并；补了测试要求（§1、§3） |
| V3 | 提示改为完成重置后 7 天内每次登录都显示，不能提前关闭；“账号安全”页长期显示 90 天的重置记录，本人不能删除；写明提示只是告知手段（§2） |
| V4 | 定义了成员管理 Action（§1b），纳入片 C（+1.5 天）；configurator 路径写明为运维通道，单独审计（§1b、§3、§7） |
| V5 | 残余风险按真实场景改写：第二身份可以作为其他策略的第二批准人；补充缓解措施，写入 ADR（§7） |
| V6 | 已由 X2、Z4 取代：投票资格起点是 `control.nexloop_workbench_role_history` 中“当前这段连续持有批准角色并集的起点”加 24 小时，不用 `valid_from`，也不用 NX-056 时间线（§1、§7、N-1） |
| W1 | 已由 Y1 取代：与 NX-056 现行文本一致，用租户级的同一把锁（§1） |
| W2 | 写明新成员加入后 24 小时内所有审批都批不了，并提示（§7） |
| W3 | 只有两位 owner 时，重置另一位 owner 走运维通道（§2） |
| W4 | 写明了 Q-D 选 (B) 时的计数口径和投票效果，以及选 (A) 时 ADR 必须写明的信任变化（§9） |
| W5 | §10 表头改为“第 2、3、4 版”；C2 行注明已由 R1 取代 |
| X1 | apply 与自上一次 configurator 版本以来由 `invitation:*` 或 `action:*` 版本造成的任何成员或角色变化不一致时，一律拒绝（`members_changed_by_governed_actions`）；测试覆盖邀请成员缺失、promote 和 demote 回滚（§1） |
| X2 | `control.nexloop_workbench_role_history` 长期保留，是投票资格的唯一来源；起点是最近一次进入批准角色的时间，跨策略版本计算，离开后再进入重新计时；删掉了“NX-056 落地后改读其时间线”；N-1 改为调用本任务提供的只读函数（§7、§9） |
| Y1 | 锁与 NX-056 现行文本一致，用租户级的同一把锁；写明加锁顺序：先 advisory lock，后行锁（§1） |
| Y2 | §10 的 C2、R3、T2、T3、W1 行按正文更新，删去旧句 |
| Y3 | “24 小时内都批不了”只针对单 owner 加第一位新成员的场景（§7） |
| Y4 | 已由 Z1 取代：目标本人不计入批准人，放进 NX-056 的计票，不限 owner 人数（§7、N-1） |
| Y5 | 执行时同时重查目标的成员状态：提升和重置要求 active；suspended 的 owner 仍按 owner 处理；revoked 的成员不能作为目标；补测试（§1b） |
| Y6 | 运行手册写明部署后 24 小时内 owner 专用 Action 都批不了（§7） |
| Z0 | Q-D 扩大为“受治理入口怎样增加和撤销授权”，交负责人决定；§1b 写明降权要在同一事务里先写撤销集，再追加配置版本，不能跳过检查；片 A 和片 C 中依赖它的部分等待决定（§1、§1b、§9） |
| Z1 | 目标本人不计入批准人，移到 NX-056 计票（决定时拒收、消费时不计入），不限 owner 人数；只有两位 owner 时走运维通道；需审批列表补上 `resume_owner`；列入 N-1（§1b、§7、§9） |
| Z2 | configurator `--revoke-member` 点名的 principal 算显式移除，不触发 X1，单独审计；补测试（§1） |
| Z3 | 改正说法：0050 认的是 configurator 的数据库登录、正文 sha256 和 `expected_revision`，不验证 manifest 签名；真正变化的是由谁决定授权（§0、§1、§9） |
| Z4 | 角色历史统一为“开始持有、停止持有”两种事件；停用算停止持有，恢复算开始持有；加 world 列；起点为“当前这段连续持有批准角色并集的起点”；资格函数在决定和消费两个时点都调用（§7、N-1） |
| Y2 残留 | §10 标题、V2 行的旧错误码、V6 行“首次……时间线记录”的说法都已改为现行口径 |
| Q-D | 负责人定为 C：角色模板实例化（§1c），ADR-020 §3 已修订 |
| AA1 | 恢复（`resume`、`resume_owner`）按当前角色模板重新授予，列入 §1c 的“增加”清单；测试覆盖“停用撤销授权、恢复重新授予” |
| AA2 | 目标字段写死为 `target_principal`；处理函数与 NX-056 共用 `control.nexloop_approval_subject_of`，解析不到就拒绝；策略的 `target_subject_field` 设为它；测试覆盖目标本人换用另一个 principal 投票被拒（§1b） |
| AA4 | 模板换版时，同一次 0050 发布按新模板重写所有已实例化的成员，并写新的实例化记录；黄金测试覆盖换版（§1c） |
| AA5 | §8 开头改为 12 天，加上 C 方案合计约 13.5 天 |
| AA6 | `vote_eligible_from` 接入 0322 记为片 C 之后的对接项，并写明要补的测试（§9 N-1） |
