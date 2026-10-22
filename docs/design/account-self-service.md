# 设计稿｜NX-058 工作台账号自助：邀请、找回、重置；随后企业单点登录

状态：**第 2 版，待 L6 复核**（l2/account-self-service，基于 main 910f367f）。第 2 版处理 L6 复核的 C1–C3、I1–I5、S1–S6，逐条关闭说明见 §10。负责人 2026-10-22 Q4 选推荐 A：v0.2 先做邀请、找回与重置，第二阶段再做单点登录（OIDC）。

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
| 统一审批 NX-056 | 设计第 5 版已定（l3/unified-approvals ed0d58d1，实现进行中）：受治理入口挂起需审批的 Action，按 subject 禁止自批；**单人模式**由 owner 开启，资格人数当前恰好 1 且过去 24 小时一直不超过 1 时允许自批；资格人数按策略记在只追加的时间线上 | `docs/design/unified-approvals.md`（l3） |
| 审计链 NX-059 | 已在 stage 启用（release 41）：`runtime.audit_events` 进链并定时封印 | 0320 |
| 上游（1e363db） | 有通用实现，没有领域耦合，都**没有 vendored**：`identity/invitations.py`（372 行，sha256 令牌、24 小时、一次性）、`identity/resets.py`（363 行，30 分钟、一次性、口令历史）、`identity/recovery_codes.py`（487 行，HMAC 存储、用后撤销会话）、`identity/oidc.py`（1187 行，PKCE、nonce、JWKS 白名单）、`adapters/identity_oidc.py`（863 行）、SMTP relay（`adapters/identity_delivery.py`、`identity_admin_delivery.py`）。已 vendored 的有 `identity/models.py`、`admin_lifecycle.py`、`delivery.py`、`rate_limits.py`、`sessions.py`、`local_accounts.py` | 上游 `src/eios/identity/` |

## 1. 成员邀请（owner 发起）

- **受治理 Action** `nexloop.workbench_member.invite`（capability `workbench_member.invite`，authority 为 `human_owner`）。负责人在工作台填写用户名和角色，提交走现有受治理入口；tenant 和 actor 从会话推导，不接受请求体传入。
- **角色只能来自受治理的角色表**：`deploy/authorization/workbench-roles.v1.json` 中的 owner、operator、reviewer。邀请时冻结角色，授予的权限不得超出该角色（复用 0116 的 `nexloop_workbench_grant_guard`）。
- **邀请 owner 需要审批，且单人模式不适用**（C1）：见 §7。
- **令牌**（I2、S1）：
  - 32 字节随机数，base64url 编码，只显示一次，在负责人的工作台页面上，形如 `/workbench/invite#<token>`。
  - 库里只存 `HMAC(identity_token_key, 'nexloop-invite/1|'||tenant||'|'||token)`。`identity_token_key` 是新的私有配置密钥，与限速用的 `browser_rate_key` 分开。库被读出时无法离线校验令牌，记录天然绑定 tenant 和用途；查找时用这个值做等值查询。
  - 前端读出 fragment 后立即用 `history.replaceState` 清除；接受页设 `Referrer-Policy: no-referrer`；令牌只放在 POST 请求体里，不进入前端错误上报。
  - 默认有效期 24 小时（S6；令牌由 owner 线下转交，窗口要短），可设 1–72 小时。一次性：被接受、被撤销或过期后都失效。
- **数据**：新表 `control.nexloop_workbench_invitations`，字段为 tenant、invitation_id、username_key、role、令牌 HMAC、issued_by（subject 和 principal）、issued_at、expires_at、state（issued / accepted / revoked / expired）、revoked_reason、accepted_principal。账号与成员先以 `invited` 状态写入，复用 0021 的状态值。
- **接受**：`POST /api/v1/workbench/auth/invitation/accept`，请求体为令牌和口令。
  - 限速（I3）：`invitation_accept` 只按来源计数（猜测防护靠来源限速加 256 位熵）。同一张令牌连续失败 5 次（例如口令不合规）即作废，需要重新签发；失败不计入任何用户名桶。
  - 口令规则：抽取 `identity_password_reset.py` 的 Python 校验（至少 12 位、不与历史重复）为共用函数（I5），口令只在 Python 中做 argon2id 哈希。
  - **在同一个事务里重查签发人**（I4）：签发人当前仍是该租户的 active owner；否则令牌作废，返回统一的失败响应。
  - 授权写入（S2）：新增 `control.nexloop_accept_workbench_invitation`，由 nexloop_owner 持有、security definer、只授权给 `nexloop_identity`。它以邀请记录为唯一授权依据，角色不得超出邀请时冻结的 role，在同一事务内完成：令牌失效、账号和成员转为 active、写入角色授权，并记一条 login event `invitation_accepted`。登记到 copy guard。**不放宽 0140 对 configurator 的约束**：成员配置的其他写入仍只走 configurator。
  - 接受时记录来源摘要；与签发者签发时会话的来源相同，就在审计里标记 `same_source_as_issuer`（C2；只作信号，不阻断）。
- **新成员不立即计入审批资格**（C2）：接受后 24 小时内，该成员不计入任何 NX-056 策略的资格人数，也不能投批准票。时间线上记录它的 `eligible_from`，与 NX-056 的 24 小时窗口衔接。这样 owner 不能靠“自己接受邀请”造出一个立即可用的第二批准人，而单人模式在资格人数变成 2 时会自动失效（见 §7）。
- **审计**：签发、撤销、接受都写 `runtime.audit_events`，并自动进 NX-059 审计链。接受事件关联签发事件（同一 invitation_id，同时记签发人和接受后的 principal）。审计里不含令牌、口令，也不含用户名摘要（S3）。
- 负责人可以撤销邀请：Action `nexloop.workbench_member.revoke_invitation`。

## 2. 找回与重置（不依赖邮件）

- **主路径：负责人为成员签发一次性重置码**，Action `nexloop.workbench_member.issue_reset`（human_owner）。
  - **目标是 owner 时**，走与“提升为 owner”相同的审批，单人模式不适用（C3，见 §7）。目标是 operator 或 reviewer 时不需要审批，但按签发的 owner 限速，并且写审计、通知成员。
  - 目标是“仅 SSO”的成员时拒绝签发（S5，第二阶段起生效）。
  - 重置码是 128 位随机数，只显示一次，由负责人线下转交本人。库里存 `HMAC(identity_token_key, 'nexloop-reset/1'||'|'||tenant||'|'||code)`（I2）。
  - 有效期 30 分钟，一次性。新签发一张会撤销同一账号所有未用的旧码（I4）。
  - 签发时**立即**让该账号的所有现有会话失效（`session_epoch+1`），防止被盗用的会话继续存活。
- **完成重置**：`POST /api/v1/workbench/auth/password/reset`，**请求体只有重置码和新口令，没有用户名**（I3）。
  - 码的 128 位熵足以定位记录，所以没有用户名的枚举面，也没有“攻击者耗尽某个用户名的桶，让成员无法完成重置”的锁死问题。
  - 限速：`password_reset_complete` 只按来源计数；同一张码连续失败 5 次（口令不合规）即作废。
  - 失败一律返回 400 `reset_invalid`（码错误、过期、已用都一样），响应时间用假哈希校验对齐。
  - 重置的失败**不计入** `local_login` 的锁定计数，所以重置流程不能被用来把账号锁死（I3）。
  - 在同一事务里重查签发人仍是 active owner，否则作废（I4）。
  - 写入时复用 0198 的效果：新哈希、口令历史、会话失效、清除锁定。
- **成员知情**（C2）：
  - 重置完成后，成员下次登录时显示一次提示：“你的口令于某时经某人签发的重置码被改过”，提示里有签发人的显示名；
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

对齐 NX-056 设计第 5 版（l3/unified-approvals ed0d58d1）。

- **需要审批的动作**：
  - 邀请 owner（`workbench_member.invite`，条件 role=owner）；
  - 把成员提升为 owner（`workbench_member.change_role`，条件 new_role=owner）；
  - 对 owner 签发重置码（`issue_reset`，条件 target_role=owner，C3）；
  - 第二阶段对 owner 建立外部身份关联（`link_external`，条件 target_role=owner，S5）。

  这几条策略的 quorum 都是 1，由另一位 owner 批准。
- **单人模式不适用**（C1，选 L6 推荐的 (a)）：
  - 这四条策略在 NX-056 的策略配置中都标为 `solo_mode: not_applicable`，所以即使开启单人模式也不能自批。
  - 理由：新增一位 owner、或接管一位 owner，改变的正是单人模式成立的前提（资格人数从 1 变成 2，或一人持有两个 owner 身份），不应由单人模式自己批准。
  - 只有一位 owner 的租户，要新增 owner 或重置 owner 口令，**一直**只能走运维通道：0198 加 configurator 的 `workbench_roles.py apply`，并在审计中注明。NX-056 落地前后口径一致，不存在“落地后悄悄放宽”。
  - 这需要 NX-056 的策略模型支持 `solo_mode: not_applicable` 属性（单人模式下直接返回 `solo_mode_not_applicable`）。已列为对 NX-056 的需求，见 §9 的 N-1。
- **新成员计入资格的时间线**（C2）：成员接受邀请后 24 小时，才进入 NX-056 资格人数时间线，记 `eligible_from`。资格人数一旦变成 2，单人模式按 NX-056 的规则自动失效，并写入审计和 `approval_solo_mode` 告警。
- **NX-056 落地前的过渡**：这几条动作在本任务中先以内建规则实现：需要另一位 active owner 在其本人会话中确认，确认人的资格与上述相同（按 subject 区分、24 小时资格、执行时重查）。NX-056 落地后迁移为它的策略，语义不变。
- operator 和 reviewer 的邀请、对它们签发重置码，都不需要审批，但都写审计、通知成员，并按签发 owner 限速。

## 8. 分片与工作量

| 片 | 内容 | 量 |
|---|---|---|
| A | 邀请：表、HMAC 令牌、Action（invite / revoke_invitation）、接受端点、`nexloop_accept_workbench_invitation`（受控授权写入）、签发人重查、限速、审计关联、测试 | 3 天 |
| B | 负责人签发重置码、只带码的完成端点、单码失败作废、不进入登录锁定、与 0198 共用 SQL 效果和 Python 口令校验、成员知情提示、运行手册 | 2.5 天 |
| C | 失效条件全集（I4）；角色变更或撤销时让会话失效（先写测试固定现状，S4）；owner 相关动作的内建双人确认和 24 小时资格（过渡规则） | 1.5 天 |
| D | 工作台前端：成员页的邀请和重置入口、最近重置记录、接受邀请页（清 fragment、no-referrer）、重置页、登录后的重置提示 | 2 天 |
| E | （待 Q-A）员工离职擦除 | 1 天 |
| F | 第二阶段 OIDC：vendored 上游、关联 Action（owner 目标需审批）、回调、配置、测试 | 4–5 天 |

v0.2（A–D）约 9 天，比第 1 版多 2 天，来自 C2、C3 和 I4。E 视负责人决定而定，F 另行排期。A 与 B 可以并行，C 依赖 A 的授权写入入口，D 依赖 A 和 B 的端点。NX-056 落地后，把 C 的内建规则迁移为策略，约 0.5 天，计入 NX-056 的接入。

## 9. 待负责人或 L6 确认

- **Q-A**：员工离职擦除是否纳入 v0.2。
- **Q-B**：邀请默认有效期。第 2 版按 S6 改为 24 小时（可设 1–72 小时），请确认。
- **Q-C**：已按 C1 定为“单人租户新增 owner、重置 owner 口令只能走运维通道”，不再是开放问题；负责人如有异议可以推翻。
- **N-1（对 NX-056 的需求）**：策略模型支持 `solo_mode: not_applicable`；资格人数时间线支持成员的 `eligible_from`（接受邀请后 24 小时）。

## 10. L6 复核关闭说明（第 2 版）

| 编号 | 处理 |
|---|---|
| C1 | 选 (a)：邀请或提升为 owner、对 owner 签发重置码、owner 外部身份关联，都标为单人模式不适用；单人租户一直只能走运维通道；对 NX-056 的需求列为 N-1（§7） |
| C2 | 新成员接受邀请后 24 小时才计入审批资格，与 NX-056 时间线衔接；资格人数变成 2 时单人模式自动失效；接受时记录来源，与签发者相同则标记；重置完成后成员下次登录时看到提示，提示含签发人；审计用 invitation_id / reset_id 关联签发人与完成人；成员页向所有 owner 显示最近的重置记录（§1、§2、§7） |
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
