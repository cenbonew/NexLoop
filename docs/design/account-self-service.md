# 设计稿｜NX-058 工作台账号自助：邀请、找回、重置；随后企业单点登录

状态：**第 5 版**（L6 第四轮无确认级，片 A、B 已开工；X1、X2、Y1–Y6 写入本版，片 C 之前生效）（l2/account-self-service，基于 main 910f367f）。第 2 版处理了 L6 第一轮复核的 C1–C3、I1–I5、S1–S6；第 3 版处理第二轮的 R1–R6、T1–T4；第 4 版处理第三轮的 V1–V6、W1–W5；第 5 版处理第四轮的 X1、X2、Y1–Y6。逐条关闭说明见 §10。负责人 2026-10-22 Q4 选推荐 A：v0.2 先做邀请、找回与重置，第二阶段再做单点登录（OIDC）。

## 0. 现状（只读盘点）

| 能力 | 现状 | 位置 |
|---|---|---|
| 开户 | 没有自助。账号来自 `initial_identity.py`（argon2）；成员与授权只能由 configurator 通过受信配置写入（`workbench_roles.py apply`，0116/0140，manifest 用 0050 签名） | `nexloop_eios/workbench_roles.py:199-256`，`0116_nx028_staff_identity.sql` |
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
    1. 取 advisory lock（Y1）：与 NX-056 现行文本相同，是**租户级**的同一把锁，也就是 NX-056 成员变更和审批消费共用的那把，所以不存在“新成员已提交、消费已计完人数”的窗口。加锁顺序固定为：先取这把 advisory lock，再 `select … for update` 成员、账号、邀请等行。NX-056 的消费事务也是先取锁、再锁审批行；处理函数在同一事务内再次取锁属于重入，不产生新的等待，所以不会与成员变更事务形成死锁。
    2. 重查邀请仍为 issued、未过期，签发人仍是 active owner；令牌改为 accepted。
    3. 账号转为 active（写入口令哈希），`nexloop_browser_memberships` 转为 active，`valid_from` 取接受时刻（`valid_from` 只用于登录，不用于投票冷却，见 V6）。
    4. **追加一版 `control.nexloop_workbench_configurations`**：内容是最新一版的 members 加上这名成员，角色取邀请时冻结的 role，roles 不变。0116/0140 的 `nexloop_configure_workbench` 本身不动，仍只接受 configurator 的 session。抽出一个内部函数 `control.nexloop_workbench_configuration_append(tenant, roles, members, source)`，**不授权给任何角色**，只由 owner 持有的函数调用；它做与 configure 相同的校验（角色表、`nexloop_workbench_grant_guard`、成员不能是顾客），`source` 记为 `invitation:<id>`。configurator 的公开入口改为调用它，行为不变。这样 0140 的“只有 configurator 能写成员”变为“只有 configurator，或者以一条有效邀请为依据，能追加成员”，不向 API 或 identity 角色开放任意写入。
    5. 第 3、4 步命中 NX-056 挂在这两张表上的触发器，**在同一事务内**向资格人数时间线追加一行（新成员立即计入，见 R1）。
    6. **角色授权事实**：今天授权只经由 0050 签名 manifest 写入（`publish_human_authority`，密钥在 configurator 手里），SQL 无法签名。v0.2 在两条路里选一条，需要 L6 和负责人决定（§9 Q-D）：
       - (A，推荐) 新增“由配置派生授权”：这个函数按冻结 role 的 Action 集合，为新成员写入与 `compile_member` 结果相同的 GrantFacts，范围受 `nexloop_workbench_grant_guard` 约束。这是一条新的授权写入路径，需要一份短 ADR。
       - (B) 授权仍由 configurator 发布：接受后，成员处于 active 但没有任何授权，工作台显示“等待运维发布授权”，由 configurator 的定时对账任务发布 0050 manifest。不需要新的授权路径，但多了运维依赖和一段等待。
    7. 记一条 login event `invitation_accepted`。
  - **与 configurator 全量 apply 的关系**（V2、X1）。选“apply 发现冲突就拒绝”，不做自动合并：
    - 配置的每个版本记录 `source`，取值为 `configurator`、`invitation:<id>` 或 `action:<intent_id>`。`workbench_roles.py apply` 写入新版本前，先读最新一版，并找出自上一次 configurator 版本以来，由 `invitation:*` 或 `action:*` 版本造成的成员或角色变化（加入、提升、降级、停用、撤销）。
    - 如果 apply 的结果会让其中任何一项回到变化前的状态，或者产生不同的结果，apply 就拒绝，返回 `members_changed_by_governed_actions`，列出这些 principal 及其当前角色。运维要先把成员文件改成与之一致，再 apply。
    - 这样，用一份旧成员文件 apply 一次，不会悄悄撤销一次已经批准的 promote 或 demote，也不会悄悄移除经邀请加入的成员。
    - apply 不会悄悄去掉经邀请加入的成员。要移除，只能用 §1b 的受治理 Action，或者先修改成员文件再 apply（运维通道，单独审计）。
    - 不做自动合并的理由：成员文件仍是 configurator 路径的唯一事实来源，“文件里没有但库里有”这种分歧必须让人看到。
    - 测试：接受邀请之后，用不含该成员的文件 apply 会被拒绝；`promote_owner` 和 `demote_owner` 执行之后，用旧角色的文件 apply 会被拒绝；两种情况下成员、角色和授权都不变。把文件改成一致后 apply 成功，版本内容不变。
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
| `suspend_owner` / `resume_owner` / `revoke_owner` | 停用、恢复、撤销 owner | 需要，单人模式不适用 |
| `issue_reset` | 对 operator 或 reviewer 签发重置码 | 不需要 |
| `issue_owner_reset` | 对 owner 签发重置码 | 需要，单人模式不适用 |
| `revoke_invitation` | 撤销未用的邀请 | 不需要 |
| （第二阶段）`link_external` / `link_owner_external` | 建立外部身份关联 | 后者需要，单人模式不适用 |

- **执行时重查目标角色和状态**：每个 Action 在执行事务里，持有 §1 的那把租户级锁，读取最新一版配置中目标当前的角色，以及目标 membership 的状态。非 owner 版本发现目标是 owner，或者 owner 版本发现目标不是 owner，都直接拒绝（`target_role_changed`），请求人需要改用另一个 Action 重新发起。所以“请求时是 operator、执行前被提升为 owner”不会绕过审批。过渡期的内建规则也按同样的方式判断。
- **目标状态**（Y5）：
  - `promote_owner`、`issue_owner_reset`、`issue_reset`、`change_role` 要求目标为 active，否则拒绝（`target_not_active`）；
  - 已 suspended 的 owner 仍按 owner 处理：恢复、撤销、降级都只能用 owner 专用 Action，不能用非 owner 的 `revoke` 撤销；
  - 已 revoked 的成员不能再成为任何 Action 的目标，只能重新邀请。
  
  以上都补测试。
- 写入同样经由 §1 的内部 append 函数追加配置版本（`source` 记为 `action:<intent_id>`），并按 §4 的失效条件让会话失效、撤销未用的邀请和重置码。
- 最后一位 active owner 不能被降级、停用或撤销，一律直接拒绝。
- **与 configurator 的关系**：configurator（修改成员文件后 apply、`revoke_member`）仍然可以直接降级或撤销任何成员，包括 owner。这是运维通道，与 0198 一样不受审批约束，但每次使用都在审计中单独列出（§3、§7）。

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

- **需要审批的动作**：§1b 中所有 owner 专用的 Action：`invite_owner`、`promote_owner`、`demote_owner`、`suspend_owner`、`revoke_owner`、`issue_owner_reset`，以及第二阶段的 `link_owner_external`。
  - 这些策略都**不带条件**：Action 本身就是 owner 专用的。目标角色由服务端在执行时重查，不依赖请求体（V1）。
  - quorum 都是 1，批准角色是 owner，由另一位 owner 批准；都标为 `solo_mode: not_applicable`，开启单人模式也不能自批。
  - **目标本人不计入批准人**（Y4）：有三位及以上 owner 时，`issue_owner_reset`、`demote_owner`、`suspend_owner`、`resume_owner`、`revoke_owner` 的目标本人不能批准针对自己的请求。处理函数在消费时重查这一点，这与“请求人不能自批”同样按 subject 判断。
  - 理由：新增、接管、移除一位 owner，改变的正是单人模式成立的前提，或者其他 owner 的否决能力，不应由单人模式自己批准（C1、R6）。
  - 只有一位 owner 的租户做这些事，一直只能走运维通道：0198 加 configurator 的成员 apply，并在审计中单独列出。NX-056 落地前后口径一致。
  - 最后一位 owner 不能被降级、停用或撤销。
- **资格人数与投票资格**（R1、V6、X2）：
  - 成员一转为 active，并在配置中持有某策略的批准角色，就计入该策略的资格人数，单人模式立即失效。人数仍取 NX-056 的时间线。
  - **投票资格**的唯一数据来源是本任务新建的 `control.nexloop_workbench_role_history`，**长期保留**：
    - 只追加，任何角色都不能改；
    - 由 append 函数和 configurator 的入口写入，每行是 (tenant, subject, role, 进入或离开, 时间)。
  - NX-056 的时间线只记人数和集合摘要，查不出“某个 subject 首次进入的时间”，所以不用它判断投票资格。
  - 投票资格起点 = 该 subject 最近一次**进入**该策略某个批准角色的时间，加 24 小时：
    - 跨策略版本计算：策略换版本，不重新计时；
    - 离开所有批准角色后再进入，重新计时。
    
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
| C | §1b 的成员管理 Action：`change_role`、`promote_owner`、`demote_owner`、`suspend(_owner)`、`revoke(_owner)`、`invite_owner`、`issue_owner_reset`；执行时重查目标角色；最后一位 owner 保护；角色历史表；失效条件全集（I4）；角色变更或撤销时让会话失效（先写测试固定现状，S4）；owner 专用 Action 的内建双人确认和 24 小时投票冷却（过渡规则）；apply 冲突拒绝（V2） | 3 天 |
| D | 工作台前端：成员页的邀请和重置入口、最近重置记录、接受邀请页（清 fragment、no-referrer）、重置页、登录后的重置提示 | 2 天 |
| E | （待 Q-A）员工离职擦除 | 1 天 |
| F | 第二阶段 OIDC：vendored 上游、关联 Action（owner 目标需审批）、回调、配置、测试 | 4–5 天 |

v0.2（A–D）约 11 天：第 2 版的 C2、C3、I4 带来 +2 天，第 3 版的 R1、R2 带来 +0.5 天（T4），第 4 版的 V1、V2、V4 给片 C 带来 +1.5 天，主要是拆分后的成员管理 Action 和 apply 冲突拒绝；如果 Q-D 选 (A)，片 A 再加 0.5 天写 ADR。E 视负责人决定而定，F 另行排期。A 与 B 可以并行，C 依赖 A 的授权写入入口，D 依赖 A 和 B 的端点。NX-056 落地后，把 C 的内建规则迁移为策略，约 0.5 天，计入 NX-056 的接入。

## 9. 待负责人或 L6 确认

- **Q-A**：员工离职擦除是否纳入 v0.2。
- **Q-B**：邀请默认有效期。第 2 版按 S6 改为 24 小时（可设 1–72 小时），请确认。
- **Q-C**：已按 C1 定为“单人租户新增 owner、重置 owner 口令只能走运维通道”，不再是开放问题；负责人如有异议可以推翻。
- **N-1（对 NX-056 的需求，需转给 L3）**：
  - 策略模型支持 `solo_mode: not_applicable`；
  - 资格人数：成员一转为 active 就计入，时间线只记人数；
  - 投票资格：判断时调用本任务提供的只读函数 `control.nexloop_workbench_vote_eligible_from(tenant, subject, roles)`，它取角色历史表中最近一次进入批准角色的时间加 24 小时。只作用于能否投票，不作用于人数；不使用 `valid_from`，也不使用 NX-056 时间线（R1、V6、X2）；
  - 工作台配置的追加入口 `nexloop_workbench_configuration_append` 也要触发时间线；
  - 成员变更与消费使用同一把 advisory lock，按 NX-056 现行文本为租户级；加锁顺序为先取 advisory lock、再锁行（R2、Y1）；
  - 一个成员第一次投批准票时告警一次（V5）。
- **Q-D**：接受邀请时的角色授权，走 (A) 由配置派生授权（推荐，需要短 ADR），还是走 (B) 由 configurator 定时发布（§1 第 6 步）。
  - 选 (B) 时的计数口径（W4）：成员 active 且在配置中持有角色，但还没有授权事实。NX-056 按配置中的角色计数，所以此时已经计入人数（安全方向）；但他投票时会在派发重查授权时失败，所以在授权发布之前，他事实上不能投票。
  - 选 (A) 时，ADR 必须写明信任关系的变化：“由配置派生的授权不经 0050 签名”。授权事实的来源从“持有签名密钥的 configurator”扩大为“经受治理入口生成、又经 append 函数校验的配置版本”。

## 10. L6 复核关闭说明（第 2、3、4 版）

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
| V2 | apply 发现最新一版里有经邀请加入、且不在成员文件中的成员时拒绝（`members_added_by_invitation`），不做自动合并；补了测试要求（§1、§3） |
| V3 | 提示改为完成重置后 7 天内每次登录都显示，不能提前关闭；“账号安全”页长期显示 90 天的重置记录，本人不能删除；写明提示只是告知手段（§2） |
| V4 | 定义了成员管理 Action（§1b），纳入片 C（+1.5 天）；configurator 路径写明为运维通道，单独审计（§1b、§3、§7） |
| V5 | 残余风险按真实场景改写：第二身份可以作为其他策略的第二批准人；补充缓解措施，写入 ADR（§7） |
| V6 | 投票资格起点统一为“首次进入批准角色集合的时间线记录加 24 小时”，不用 `valid_from`；过渡期用只追加的角色历史表，定义相同（§1、§7、N-1） |
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
| Y4 | 有三位及以上 owner 时，目标本人不计入批准人，消费时重查（§7） |
| Y5 | 执行时同时重查目标的成员状态：提升和重置要求 active；suspended 的 owner 仍按 owner 处理；revoked 的成员不能作为目标；补测试（§1b） |
| Y6 | 运行手册写明部署后 24 小时内 owner 专用 Action 都批不了（§7） |
