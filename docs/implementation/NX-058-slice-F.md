# NX-058 片 F：工作台单点登录（OIDC）

依据 `docs/design/account-self-service.md` §5、§2（S5）。负责人 2026-10-28 定：现在写代码，测试只用进程内的模拟 IdP；stage 接哪个真实 IdP、owner 是否只能用 SSO，排期时再定。所以实现做成可配置，**默认不启用**：没有 provider 文件时，两个 SSO 端点都返回 404 `sso_unavailable`。迁移 `0370_nx058_oidc.sql` 用的是临时编号。

## 做了什么

| 部分 | 实现 |
|---|---|
| 协议 | `nexloop_eios/workbench_oidc.py`：授权码 + PKCE（S256），state、nonce。ID token 只接受 RS256，`none`、`HS*` 和其他算法一律拒绝。签名用标准库校验：PKCS#1 v1.5 比较整段编码，不解析 ASN.1，所以不存在 Bleichenbacher 式的伪造；密钥至少 2048 位，取自 provider 的 JWKS（HTTPS、64 KB 上限、缓存 5 分钟；遇到未知 kid 时最多每分钟重取一次）。claims 的校验规则：`iss` 与配置完全相同；`aud` 包含 client_id；`azp` 出现时，或 `aud` 有多个值时，`azp` 必须等于 client_id；`exp`、`nbf`、`iat` 留 60 秒余量，`iat` 不早于 10 分钟前；`nonce` 用常数时间比较；`sub` 是非空字符串；重复的 claim 一律拒绝 |
| 浏览器绑定 | `GET /api/v1/workbench/auth/oidc/start?provider=<id>` 生成 256 位的 state（发给 IdP）和 256 位的绑定值（写入 `__Host-nexloop_workbench_oidc`：HttpOnly、Secure、SameSite=Lax，5 分钟。用 Lax，是因为 IdP 跳回来是跨站的顶层导航）。库里只存两者的 `HMAC(identity_token_key, 用途\|tenant\|值)`。nonce 和 PKCE verifier 都是 state 在私有密钥下的 HMAC，所以库里没有秘密，仅凭 URL 里的 state 也推不出它们 |
| 回调 | `GET /api/v1/workbench/auth/oidc/callback`：state 只能取用一次，谁带来都算用掉；它必须未过期，并且属于这个浏览器的绑定值。然后用 code、verifier 和 client secret（默认 `client_secret_basic`）换取 ID token，校验后按 `(provider, issuer, sub)` 找绑定。会话仍然是该成员**本地账号**的会话（`credential_kind=local_account`，`authentication_methods=["oidc"]`），所以 0027/0031 的读取复核和所有 `session_epoch` 踢下线照常生效。会话由 `control.nexloop_create_workbench_oidc_session` 在成员锁下重查绑定、membership、subject、账号之后写入，同时记一条 login event `oidc_login`（provider 和公开配置指纹，不含 sub 和令牌）。成功后 303 到 `/workbench` |
| 不自动开户 | IdP 的 subject 没有绑定时，一律拒绝（`not_linked`），不创建任何东西 |
| 只接受已配置的 IdP | provider 来自私有文件（0600，属于服务）。文件里的 URL 只允许 HTTPS，不能带 userinfo 或 fragment，不能是私网、回环、链路本地地址字面量。`link_external` 只接受文件里有的 `provider_id`，`issuer` 由入口按配置填写，请求体不能提供 |
| 失败 | 所有拒绝都是同一个 303，跳到 `/workbench?sso=failed`，不设会话 cookie，浏览器得不到原因。审计事件 `nexloop.workbench_member.sso_login_failed` 只记 provider 和固定原因：`state_invalid`、`provider_error`、`token_exchange_failed`、`id_token_invalid`、`not_linked`、`member_unavailable`。start 和 callback 各自按来源计入限速动作 `oidc_start`（0019 已预留） |
| 绑定与解绑 | 受治理的 owner Action `nexloop.workbench_member.link_external` / `unlink_external`（capability 同名，operation 为 `link_workbench_external` / `unlink_workbench_external`），只授权给 owner，并列入 owner-only 集合。在成员锁内重查：签发人仍是 active owner；目标不是请求人本人，不是 owner（`target_role_changed`：owner 的绑定按 S5 需要审批，留到接审批策略时再做），是 active 成员；一个成员只有一个绑定（`already_linked`），一个 IdP subject 只能绑一个成员（`external_identity_taken`）。用同一个 request_id 重放时返回 `replayed`。审计只记 principal、provider 和模式，不记 sub |
| 仅 SSO | `login_mode` 取值为 `sso_only` 或 `sso_or_password`。`sso_only` 时，`control.nexloop_browser_evidence` 上的触发器拒绝该 subject 的口令证据，所以口令登录和改口令都失败，表现与口令错误相同；`control.nexloop_workbench_reset_codes` 上的触发器拒绝签发，所以 `issue_reset` 和 `issue_owner_reset` 都返回 409 `target_sso_only`。以 `sso_only` 绑定时，同一事务内让会话失效、撤销未用的重置码。没有 `sso_only` 的成员照常 |
| 解绑与撤销 | 解绑让该成员所有会话失效（SSO 会话与口令会话共用账号的 epoch）。成员被撤销（revoked）时，membership 上的触发器在同一事务里删掉绑定，所以员工擦除（只处理已撤销的成员）时已经没有绑定了。`control.nexloop_workbench_external_identity_events` 只追加，只记 principal、provider、模式 |
| 密钥与 client secret | client secret 只通过 `client_secret_file`（另一个私有文件）或 `client_secret_env`（环境变量名）引用，每次换取令牌时读取，不进日志、库、审计、fixture 和提交。state、绑定和 nonce 的 HMAC 复用片 A/B 的 `identity_token_key`，各用自己的用途前缀 |

## 与设计的偏差（需 L6 和调度员确认）

- **负责人决定（2026-10-28）**：先只支持 RS256，不引入 `joserfc`。将来需要 ES256 或 PS256 时再议。

- **没有 vendored 上游 `identity/oidc.py`**：它依赖 `joserfc`（以及间接依赖的 `cryptography`），NexLoop 没有这两个依赖，本任务也不下载新依赖。现在的实现遵循上游的规则（PKCE S256、一次性 state、nonce、JWKS 白名单、HTTPS fail-closed），但不复制它的代码，只支持 RS256。如果以后要支持 ES256 或 PS256，或者想改回 vendoring，需要先引入 `joserfc`，届时再议（见上一条）。
- **owner 的绑定**：设计 S5 要求 `link_owner_external` 走审批。本片在受治理入口拒绝 owner 目标，没有新增需要审批的 Action。
- **邀请时指定“仅限 SSO”**（§5）：没有做。现在的做法是先接受邀请，再由 owner 绑定为 `sso_only`。
- **成员页不显示绑定状态**：成员读取（0356）不返回绑定，所以每个在用的 operator 和 reviewer 行都提供绑定和解绑，由服务端按 `already_linked` 或 `not_linked` 拒绝。要显示状态，需要扩展成员读取。

## 部署（默认不启用，启用时再做）

1. 迁移 0370（临时编号）。只建表、函数和触发器；不配置 provider 时，行为与以前一样。
2. 发布新 Action：stage_secrets publish（Action 数 125 → 127），工作台角色 manifest v47（owner 角色 93 个 Action）。
3. 启用 SSO 时：
   - 写私有 provider 文件：`{"version":1,"providers":[{"provider_id":…,"issuer":…,"client_id":…,"authorization_endpoint":…,"token_endpoint":…,"jwks_uri":…,"client_secret_file":"/run/secrets/…"}]}`（也可以用 `client_secret_env`）；
   - api 和 api-2 都加参数 `--workbench-oidc-providers-file <路径>`；
   - 在 IdP 上登记回调地址 `<origin>/api/v1/workbench/auth/oidc/callback`；
   - 限速策略 `oidc_start`（operator 为 nexloop_identity，每个来源 300 秒内 20 次，start 和 callback 各一个桶）已经由 `stage_secrets.py browser` 注册（`OIDC_RATE`）。不配置 provider 时注册它也没有影响：两个端点在限速之前就返回 404。只有配置了 provider、却缺少这条策略时，两个端点才返回 503（fail closed）。
4. `identity-token.key` 轮换时（片 B 的步骤），正在进行的 SSO 流程会失败，重新点登录即可；已有的绑定不受影响，因为库里存的是 sub 原值。

## 测试

- `tests/test_workbench_oidc.py`（33 项，不需要数据库）：
  - PKCE S256 与 RFC 7636 附录 B 一致；
  - 合法令牌能通过，`aud` 为列表加 `azp` 时也能通过；
  - 以下情形都被拒绝：错误的 aud 或 azp、错误的 iss、过期（含 60 秒余量的边界）、iat 过旧或在未来、nbf 在未来、nonce 不符或缺失、sub 为空、改了 payload、改了签名、用另一把密钥签名、`alg: none`、HS256、`crit`、结构错误、重复 claim；
  - 短密钥和非 RSA 密钥不会被使用；
  - 令牌交换会发送 verifier 和 secret，code 只能用一次，verifier 错误时被拒绝；
  - provider 文件的规则。
- `tests/test_workbench_oidc_pg.py`（7 项，真实 app、真实 PG、`tests/support/mock_oidc_idp.py` 模拟 IdP：自签 JWKS，经 `httpx.MockTransport`，不出进程）：
  - 正常登录：库里只有 HMAC；会话的 methods 为 `oidc`；login event 不含 sub；重放回调被拒且不新建会话；`sso_or_password` 时口令照常能用；
  - 未绑定的 subject 被拒，并且什么都没有创建；
  - 篡改 state；在另一个浏览器里回调，分为没有绑定 cookie 和持有自己绑定 cookie 两种情形，后者会用掉受害者的 state；IdP 返回 error；nonce、签名、过期、audience 被篡改：以上都被拒，并写入对应的审计原因；
  - `sso_only` 停用口令和重置码（409 `target_sso_only`），没有 `sso_only` 的成员照常；解绑后会话失效、SSO 失效，重置码可以再次签发；
  - 只有 owner 能绑定；owner 目标、请求人本人、未配置的 provider、请求体自带 issuer、重复绑定都被拒；撤销成员时绑定随之删除；事件表只追加，不含 sub；
  - 没有 provider 文件时，两个端点都返回 404；
  - 登录函数只授权给 nexloop_identity。
- 反向检查（临时改代码后跑测试，跑完已还原）：
  - 去掉 nonce 比较后，篡改用例失败；
  - 去掉 SQL 里的绑定值比较后，“另一个浏览器持有自己的绑定 cookie”用例失败。第一次反向检查时发现没有这个用例，已经补上。

## 第二批（调度员 10-28）：限速注册与工作台前端

- `scripts/deploy/stage_secrets.py`：`browser_entries` 注册 `OIDC_RATE`（见上文“部署”）；`tests/test_stage_secrets.py` 断言了它的取值。
- `GET /api/v1/workbench/auth/oidc/providers`（pre-session）：只返回 `provider_id` 和 `display_name`；SSO 未启用时返回空列表。api-routes 清单已登记。
- 前端：
  - `apps/web/src/workbench/SsoLink.tsx`：登录卡上的“用 SSO 登录”按钮，只在配置了 provider 时出现。按钮是普通链接，指向 start，所以是顶层导航，不用 fetch。回调失败（`?sso=failed`）时只显示一句提示，不说原因。成员页提供绑定表单（provider、IdP 的 sub、登录方式）和解绑表单，只对在用的 operator、reviewer 显示，且只有持有该 Action 的 owner 能看到。
  - `WorkbenchApp.tsx` 的登录卡引用它；`Members.tsx` 只加一行引用，以减少与 L9 S1 的冲突。
  - `api.ts` 新增 `readSsoProviders`（严格解析，失败时当作没有 provider），并在 `OPERATION_ACTION` 中加入两个新 Action；`actions.tsx` 加上两条完成提示。
  - 样式沿用现有的 token；L9 的 S1（529b7f7d）还没合入，合入后再对齐按钮色调。
  - 顾客侧的 `Login.tsx` 和 `Account.tsx` 不涉及 SSO，没有改动。
- 测试：`apps/web/test/workbench-sso.test.ts`（provider 列表严格解析；SSO 按钮的显示条件和链接；失败提示；哪些行有绑定控件；owner、operator 和无 provider 时的表单差异；成员页里的位置）。

- 前端文案按负责人 10-28 定的术语：客户、密码、负责人（只改了本片新增的文案；内部标识和 API 名不变）。
