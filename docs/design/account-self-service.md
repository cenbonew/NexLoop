# 设计稿｜NX-058 工作台账号自助：邀请、找回、重置；随后企业单点登录

状态：**草案，待 L6 复核**（l2/account-self-service，基于 main 910f367f）。负责人 2026-10-22 Q4 选推荐 A：v0.2 先做邀请、找回与重置，第二阶段再做单点登录（OIDC）。

## 0. 现状（只读盘点）

| 能力 | 现状 | 位置 |
|---|---|---|
| 开户 | 没有自助。账号来自 `initial_identity.py`（argon2）；成员与授权只能由 configurator 通过受信配置写入（`workbench_roles.py apply`，0116/0140，manifest 用 0050 签名） | `nexloop_eios/workbench_roles.py:199-256`，`0116_nx028_staff_identity.sql` |
| 口令 | argon2id，只在 Python 中校验；SQL 拒收不以 `$argon2id$` 开头的哈希；历史保留 10 个 | 0021、0024 |
| 重置 | 只有运维通道：`control.nexloop_operator_reset_browser_password`（0198），由 `nexloop_identity` 执行，CLI 为 `identity_password_reset.py`。重置会让 `session_epoch+1` 使所有会话失效、清除锁定，并记一条 login event | 0198 |
| 状态值 | 成员 `invited\|active\|suspended\|revoked`，账号 `invited\|active\|disabled`：表里已有 invited，但没有任何代码路径签发邀请 | 0021 |
| 限速 | 0019 计数表。动作已预留 `password_reset`、`password_reset_complete`、`recovery`、`invitation_accept`、`oidc_start`。L3 的 59611c88 已加上按来源 IP 和按用户名（NFKC/casefold 后 HMAC）两个维度 | `browser_http.py:49-79`，`browser_rate_limits.py` |
| 会话失效 | 会话读取时比较账号的 `credential_revision` 和 `session_epoch` | 0027 |
| 邮件 | 没有任何 SMTP 或出站邮件实现；stage 上的邮箱多为 `.invalid` | — |
| 擦除 | NX-029 只覆盖 Consumer，员工账号不在擦除范围 | 0132 及以后 |
| 统一审批 NX-056 | 第二十一批起已在 planning 中立项（状态 not_started），还没有设计和实现；现有的“审批”只指 Action 的 approval resolver | `planning/tasks.json` |
| 审计链 NX-059 | 已在 stage 启用（release 41）：`runtime.audit_events` 进链并定时封印 | 0320 |
| 上游（1e363db） | 有通用实现，没有领域耦合，都**没有 vendored**：`identity/invitations.py`（372 行，sha256 令牌、24 小时、一次性）、`identity/resets.py`（363 行，30 分钟、一次性、口令历史）、`identity/recovery_codes.py`（487 行，HMAC 存储、用后撤销会话）、`identity/oidc.py`（1187 行，PKCE、nonce、JWKS 白名单）、`adapters/identity_oidc.py`（863 行）、SMTP relay（`adapters/identity_delivery.py`、`identity_admin_delivery.py`）。已 vendored 的有 `identity/models.py`、`admin_lifecycle.py`、`delivery.py`、`rate_limits.py`、`sessions.py`、`local_accounts.py` | 上游 `src/eios/identity/` |

## 1. 成员邀请（owner 发起）

- **受治理 Action** `nexloop.workbench_member.invite`（新 capability `workbench_member.invite`，authority 为 `human_owner`）。负责人在工作台填写用户名、角色和备注（可选），提交走现有的受治理入口。服务端从会话推导 tenant 和 actor，不接受请求体传入。
- **角色只能来自受治理的角色表**：`deploy/authorization/workbench-roles.v1.json` 中的 owner、operator、reviewer。授予的权限不得超出该角色（复用 0116 的 `nexloop_workbench_grant_guard`）。**邀请 owner 角色需要审批**，见 §7。
- **令牌**：32 字节随机数，base64url 编码后只显示一次（在负责人的工作台页面上，形如 `/workbench/invite#<token>`；令牌放在 fragment 里，不进入服务器访问日志）。库里只存 `sha256('nexloop-invite/1|'||token)`。默认有效期 72 小时，可在 1 至 168 小时之间设置。一次性：被接受、被撤销或过期后都失效。每个用户名同一时间只能有一个未用邀请，重新签发会撤销旧邀请。
- **数据**：新表 `control.nexloop_workbench_invitations`，字段为 tenant、invitation_id、username_key（规范化后的哈希）、role、令牌哈希、issued_by、issued_at、expires_at、state（issued / accepted / revoked / expired）、accepted_principal。账号与成员先以 `invited` 状态写入，复用 0021 的状态值。
- **接受**：`POST /api/v1/workbench/auth/invitation/accept`，请求体为令牌、口令和确认口令。
  - 限速：`invitation_accept` 动作，按来源和按令牌哈希两个维度。
  - 校验：口令规则与 0198 相同（至少 12 位），口令只在 Python 中做 argon2id 哈希。
  - 在一个事务内完成：令牌失效、账号转为 active、成员转为 active、写入角色授权，并记一条 login event `invitation_accepted`。
  - 授权事实仍然走 0050 签名的路径：接受后由一个受治理的 configurator 步骤，或者一个新的 SQL 函数，在同一事务里调用现有的授权写入。实现时要先在 L6 核对：0140 要求 configurator 的 session 写成员，这条约束需要为“邀请接受”开一个受控入口，**不能**开放给 API 角色任意写入。
- **审计**：签发、撤销、接受都写 `runtime.audit_events`。NX-059 哈希链已经在 stage 启用，这些事件自动进链，防删改。审计里不含令牌，也不含口令。
- **负责人可以撤销邀请**：Action `nexloop.workbench_member.revoke_invitation`。

## 2. 找回与重置（不依赖邮件）

- **主路径：负责人为成员签发一次性重置码**，Action `nexloop.workbench_member.issue_reset`（human_owner）。
  - 重置码是 128 位随机数，只显示一次，由负责人通过线下或其他渠道转交本人。库里只存 HMAC（密钥放在私有配置中，与上游 `recovery_codes.py` 的做法一致；只存 sha256 也够，因为码的熵很高）。
  - 有效期 30 分钟，一次性。签发时**立即**让该账号的所有现有会话失效（`session_epoch+1`），防止被盗用的会话继续存活。
  - 完成重置：`POST /api/v1/workbench/auth/password/reset`，请求体为用户名、重置码和新口令。
    - 限速：`password_reset_complete` 动作，按来源和按用户名两个维度。
    - 枚举防护：用户名不存在、码错误、码过期、码已用，统一返回 400 `reset_invalid`，响应时间对齐（argon2 校验假口令）。
    - 写入时复用 0198 的效果：新哈希、口令历史、会话失效、清除锁定。
- **成员自助“忘记口令”**：v0.2 不提供无身份的自助入口，因为没有邮件出口，无法证明本人身份。登录页显示“请联系工作台负责人”。负责人本人忘记口令时，走 §3 的运维通道。
- **将来接入邮件出口的扩展点**：
  - 重置码和邀请令牌的“投递”抽象成接口 `IdentityDelivery.deliver(kind, principal, secret_once)`。v0.2 的实现是“只显示给签发者”。
  - 将来移植上游的 SMTP relay（`adapters/identity_delivery.py`），只投递到已验证的邮箱。投递 worker 使用 claim/ack，信封不留密钥（上游 `password_reset_requests.py` 的契约）。
  - 届时再开放成员自助“忘记口令”：按用户名限速，无论用户名是否存在都返回相同的响应。

## 3. 与运维重置工具（0198）的关系

- **保留 0198 作为紧急通道**：负责人本人被锁死、或租户唯一的 owner 不可用时使用。它只能在数据主机上由运维用 `nexloop_identity` 执行，不改动。
- 运行手册写明：日常使用 §2 的负责人签发重置码；0198 只用于紧急情况，每次使用后在 login event 里可以查到，审计时单独列出。
- 两条路径共用口令规则和“重置后会话失效”的语义。实现时把 0198 的效果抽成一个内部函数，供两者调用；不改 0198 已发布的签名，而是用新迁移重定义并登记到 copy guard。

## 4. 安全

- **令牌**：只存哈希；比较在 SQL 中做等值查询（哈希值本身不泄露信息）。令牌不进日志、审计、fixture，也不进 URL query（只放在 fragment 或请求体里）。
- **限速**：复用 L3 的两个维度。`invitation_accept`、`password_reset_complete` 按来源 IP（只在受信代理之后读取）和按用户名或令牌哈希计数；`issue_reset` 按 owner 计数，防止被盗的 owner 会话大量签发。
- **枚举防护**：接受邀请和完成重置的失败响应完全一致；响应时间用假哈希校验对齐。
- **会话**：签发重置码、完成重置、角色降级或撤销成员时，都让会话失效（`session_epoch+1`）。“角色变更不踢会话”是现状的缺口（盘点结果：撤销成员时只清空授权，不撤销会话）。本任务顺带补上：成员角色改变或被撤销时，同步 `session_epoch+1`。
- **审计**：签发、撤销、接受、重置、0198 紧急重置都写审计事件，只记动作、主体和对象的 principal、时间，不记秘密。
- **擦除传播**：员工账号目前不在擦除范围内。建议在本任务中加一条员工离职擦除：撤销成员后，可以对其账号执行“停用并清除口令哈希、口令历史和登录事件里的来源摘要”，保留 principal 以维持审计可追溯，邀请和重置记录随之删除。这一点需要负责人确认是否纳入 v0.2（Q-A）。

## 5. 单点登录（OIDC，第二阶段：只定接口与范围）

- **范围**：企业 IdP 的授权码流程，带 PKCE（S256）、state（5 分钟、一次性）、nonce；用 JWKS 白名单校验 ID token；只支持 OIDC，不做 SAML（上游也没有 SAML）。
- **账号关联**：`external_identity(issuer, subject) → principal`。只有负责人能建立关联（受治理 Action `workbench_member.link_external`，或者在邀请时指定“仅限 SSO”）。首次登录**不自动开户**。
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

- NX-056 已立项，但还没有设计和实现。本设计先**内建**一条最小规则：**邀请或提升为 owner** 需要另一位 owner 确认（双人原则）；只有一位 owner 时，只能走运维通道（0198 加 configurator），并在审计中注明。
- 等 NX-056 落地，“需要第二人确认”改为调用统一审批，接口保持为 `approval_required(action, subject, target) → pending/approved`。operator 和 reviewer 的邀请、重置码签发不需要审批。

## 8. 分片与工作量

| 片 | 内容 | 量 |
|---|---|---|
| A | 邀请：表、Action（invite / revoke_invitation）、接受端点、受控授权写入、限速、审计、测试 | 2.5 天 |
| B | 负责人签发重置码、完成重置端点、枚举防护、会话失效、与 0198 共用内部函数、运行手册 | 2 天 |
| C | 角色变更或撤销时让会话失效（补现有缺口）、owner 双人确认 | 1 天 |
| D | 工作台前端：成员页的邀请和重置入口、接受邀请页、重置页 | 1.5 天 |
| E | （待 Q-A）员工离职擦除 | 1 天 |
| F | 第二阶段 OIDC：vendored 上游、关联 Action、回调、配置、测试 | 4 至 5 天 |

v0.2（A 到 D）约 7 天；E 视负责人决定而定；F 另行排期。A 与 B 可以并行，C 依赖 A 的授权写入入口，D 依赖 A 和 B 的端点。

## 9. 待负责人或 L6 确认

- **Q-A**：员工离职擦除是否纳入 v0.2。
- **Q-B**：邀请默认有效期，72 小时是否合适。
- **Q-C**：只有一位 owner 时，提升为 owner 是否允许单人完成并事后审计（本稿推荐：不允许，走运维通道）。
- **L6**：接受邀请时写授权事实的入口如何与 0140 的“只有 configurator 写成员”约束协调（§1 末尾）。
