# 运行单：工作台单点登录（OIDC）启用与合成 IdP 验收（NX-058 片 F，stage）

- 依据：`docs/implementation/NX-058-slice-F.md`（实现、测试、L6 复核），迁移 0365（OIDC）和 0371（邀请时指定仅限 SSO），代码 `nexloop_eios/workbench_oidc.py`。
- 本单只写启用步骤和验收，**写成时没有启用**：不改迁移，不改代码。compose 的改动见第 3 步，需要单独提交。
- 执行者：调度员。判定者：L6。决定者：负责人（见第 0 节）。
- 只支持 RS256（负责人 2026-10-28）。IdP 如果只能签 ES256 或 PS256，就不能接入，需要另议。
- client secret、测试用户口令、IdP 管理口令只放在 Keychain 和 APP_HOST 的 secrets（0600），不写进文档、证据、命令行、截图或提交。

## 0. 负责人要做的决定（启用前）

1. **是否在 stage 启用**：不启用时什么都不用做。没有 provider 文件时，`oidc/start` 和 `oidc/callback` 都返回 404 `sso_unavailable`，`oidc/providers` 返回空列表，登录卡上不显示 SSO 按钮。
2. **接哪个 IdP**：先接一个**合成 IdP**做验收，例如自建的测试 realm，或托管 IdP 的独立测试租户。合成 IdP 里只建合成测试用户，不导入真实员工。确定验收通过后，再决定是否接真实 IdP（另起一单）。
3. **登录方式**：被绑定成员用 `sso_or_password`（密码仍可用）还是 `sso_only`。负责人本人不能绑定 SSO：owner 的绑定按设计 S5 需要审批，目前没有实现。

IdP 侧需要满足的条件：
- 授权码流程加 PKCE（S256）；ID token 用 RS256 签名，密钥至少 2048 位；JWKS 用 HTTPS 发布。
- issuer、authorization、token、jwks 四个地址都是 HTTPS，不带 userinfo 或 fragment，也不能是私网、回环或链路本地的 IP 字面量。用主机名可以；主机名解析到内网的情况不拦（L6 S2，已接受的局限）。
- 回调地址登记为 `<工作台 origin>/api/v1/workbench/auth/oidc/callback`，必须完全一致。
- 客户端认证方式是 `client_secret_basic`（默认）或 `client_secret_post`。
- api 容器能访问 IdP 的 token 和 jwks 地址。出网是否放行，要先用只读方式核实，不要猜。

## 1. 开关、配置和密钥放哪里

| 项 | 位置 | 说明 |
|---|---|---|
| 开关 | api 的参数 `--workbench-oidc-providers-file <路径>` | 带上这个参数即启用，去掉即关闭；没有单独的布尔开关 |
| provider 文件 | APP_HOST `$NEXLOOP_ROOT/secrets/api/oidc-providers.json`，在容器里挂成 `/run/secrets/api_oidc_providers` | 0600，属主 10001。格式见 `deploy/examples/nx058-oidc-providers.example.json`，只有占位值；最多 8 个 provider。字段以 `workbench_oidc.FIELDS` 为准，多一个字段就拒绝启动 |
| client secret | APP_HOST `$NEXLOOP_ROOT/secrets/api/oidc-client-secret`，在容器里挂成 `/run/secrets/api_oidc_client_secret` | 0600，属主 10001。provider 文件里用 `client_secret_file` 引用它；每次换取令牌时读取，不进日志、库或审计。不要用 `client_secret_env`：环境变量会出现在容器的 inspect 输出里 |
| 原始值 | Keychain：`nexloop/stage/oidc/<provider_id>/client-secret`，测试用户口令 `nexloop/stage/oidc/<provider_id>/user-<n>` | 值不进仓库；值索引 `~/.nexloop/vault-index.md` 只记条目名 |
| state、nonce、PKCE 的 HMAC 密钥 | 复用已有的 `identity-token.key` | 不需要新密钥。轮换它时（片 B 的步骤），进行中的 SSO 流程会失败，重新点登录即可；已有绑定不受影响 |
| 限速 | `oidc_start` 策略 | `stage_secrets.py browser` 已注册（`OIDC_RATE`：每个来源 300 秒 20 次）。配置了 provider 却缺这条策略时，两个端点都返回 503（fail closed） |

安装 secrets 的做法同 run-i31 第 3.3 条：值从 Keychain 用管道传入，先写临时文件再 `mv -f`，然后 `sudo sha256sum` 只比较摘要，不看内容。

## 2. 前提核对（只读）

1. 0365 和 0371 已迁移：`\df control.nexloop_create_workbench_oidc_session` 和 `\df control.nexloop_workbench_oidc_resolve` 都有结果。
2. Action 已发布：先 dry-run `stage_secrets.py publish` 和 register。`nexloop.workbench_member.link_external` / `unlink_external` 应该已经存在，预期没有变化；有变化就停下来，先按部署单补发布。
3. 限速策略 `oidc_start` 已注册（`stage_secrets.py browser` dry-run 预期没有变化）。
4. 本批镜像已部署，`GET /api/v1/workbench/auth/oidc/providers` 返回 `{"providers": []}`，`GET /api/v1/workbench/auth/oidc/start?provider=x` 返回 404。
5. 按部署单做好备份（虽然本单不迁移，但启用后会写入绑定和事件表）。

## 3. 启用步骤

1. **在合成 IdP 上建 client**：回调地址按第 0 节登记；记下 issuer、client_id 和四个地址；client secret 直接存进 Keychain。
2. **在合成 IdP 上建测试用户**：至少 3 个（A：要绑定的；B：不绑定的；C：用来测 `sso_only`）。口令直接存 Keychain。在 IdP 管理端记下各自的 `sub`（不进证据，证据里只写 A、B、C）。
3. **写 provider 文件**：从示例复制，填上第 1 步的值，`provider_id` 用小写字母、数字、`-`、`_`，最长 32 个字符。在管理机上先用解析器检查一遍（不连主机）：
   ```bash
   PYTHONPATH=<检出>/packages/eios-core/src python3 -c 'import json,sys; from nexloop_eios.workbench_oidc import parse_providers; print(sorted(parse_providers(json.load(sys.stdin))))' < <provider 文件>
   ```
4. **安装两个 secrets 文件**到 APP_HOST（第 1 节表格），核对摘要。
5. **compose 改动（单独提交，走 feature 分支和 PR，部署配置需要负责人 ack-policy）**：api-1 和 api-2 各加
   - `command` 末尾：`--workbench-oidc-providers-file, /run/secrets/api_oidc_providers`；
   - `secrets`：`api_oidc_providers`、`api_oidc_client_secret`，并在顶层 `secrets` 里声明对应文件。

   改完要跑所有读取 compose 的测试（包括 `test_stage_compose`、`deploy_render`、`connection_budget_live`）。如果要保留"一个变量就能关"的能力，可以把参数写成由私有 env 控制，但要在那个 PR 里一并设计和测试。
6. **重建 api-1、api-2**，然后核对：`oidc/providers` 返回刚配置的 `provider_id` 和 `display_name`，工作台登录卡出现"用 SSO 登录"按钮。

## 4. 合成 IdP 验收（逐条，只用合成成员和合成 IdP 用户）

准备：负责人邀请两名合成成员 M1（operator）和 M2（reviewer），走普通邀请，设好密码。

| # | 操作 | 预期 |
|---|---|---|
| 1 | IdP 用户 B（未绑定）走 SSO 登录 | 跳到 `/workbench?sso=failed`，页面只有一句提示、没有原因；没有会话 cookie；审计 `sso_login_failed` 原因是 `not_linked`；没有建出任何成员或账号 |
| 2 | 负责人在成员页把 M1 绑定到 A，方式 `sso_or_password` | Action 成功；事件表多一条 link，只有 principal、provider、模式，不含 sub |
| 3 | 用 A 走 SSO 登录 | 303 到 `/workbench`，以 M1 登录；`GET /api/v1/workbench/auth/session` 的 `authentication_methods` 是 `["oidc"]`；login event 是 `oidc_login`，不含 sub 和令牌 |
| 4 | 把第 3 步 IdP 回调的整个 URL 在新窗口再打开一次 | 回调被拒（`state_invalid`），不新建会话 |
| 5 | M1 用密码登录 | 成功，`authentication_methods` 是 `["password"]`（`sso_or_password` 下密码照常） |
| 6 | 负责人试图绑定自己，或者绑定另一位负责人 | 被拒（`target_role_changed` 或请求人本人）；provider 不在文件里时被拒；请求体自带 issuer 时被拒 |
| 7 | 把 M1 再绑定到 C，或者把 M2 绑定到 A | 分别被拒：`already_linked`、`external_identity_taken` |
| 8 | 负责人把 M2 绑定到 C，方式 `sso_only` | M2 现有会话失效；M2 密码登录失败（表现与密码错误相同）；负责人给 M2 签发重置码返回 409 `target_sso_only`；用 C 走 SSO 能登录 |
| 9 | 负责人解绑 M1 | M1 所有会话失效（SSO 会话和密码会话都失效）；再用 A 走 SSO 被拒 `not_linked` |
| 10 | 邀请合成成员 M3，登录方式选"仅 SSO（provider）"，接受邀请 | 接受页不显示密码栏；接受后成员页显示"待绑定 SSO"；绑定前密码、重置码和 SSO 都不能登录；用 `sso_or_password` 绑定被拒 409 `sso_invitation_mismatch`；用 `sso_only` 绑定到 B 后，B 能走 SSO 登录 |
| 11 | 撤销 M2 | M2 的绑定在同一事务里被删除（事件表有记录）；再用 C 走 SSO 被拒 |
| 12 | 在 1 分钟内从同一来源连续点 21 次 SSO 登录 | 第 21 次起被限速（`oidc_start`）；5 分钟后恢复 |
| 13 | 证据检查 | 截图、证据、日志里不出现 sub、令牌、client secret、测试用户口令、主机名或地址 |

计数（第 1、3、4、9 步各做一次）：
```sql
select payload->>'reason',count(*) from runtime.audit_events where action='nexloop.workbench_member.sso_login_failed' group by 1;
select mode,count(*) from control.nexloop_workbench_external_identity_events group by 1;
```
只取计数，不导出行（字段按 0365 的写法：`payload` 里只有 `provider_id` 和 `reason`）。

## 5. 回退

- **关闭 SSO**：从 api-1、api-2 的 `command` 去掉 `--workbench-oidc-providers-file`（或者回退第 3 步第 5 条的 compose 提交），重建两个 api。之后两个端点返回 404，按钮消失。已经用 SSO 建立的会话**不会因为关闭而失效**；要踢掉它们，就逐个解绑（解绑会让会话失效），或者走改密码或重置。
- **`sso_only` 成员**：关闭 SSO 之后，这些成员既不能用 SSO，也不能用密码登录，重置码也签不出来。关闭之前，先由负责人把他们解绑；解绑后签发重置码，成员用重置码设密码。待绑定（仅 SSO 邀请）的成员同理：撤销后重新用普通邀请。
- **绑定数据**：关闭不会删除绑定。重新启用时，绑定照常生效。要彻底清理，就逐个解绑，不要直接删表。
- **secrets 文件**：关闭后可以删除两个文件，并在 IdP 上作废 client secret；Keychain 里的条目标记为作废。
- 不回退迁移 0365 和 0371：没有 provider 时，它们的表、函数和触发器不改变任何行为。

## 6. 和其他登录机制怎么共存

- **本地账号**：SSO 登录得到的仍是该成员**本地账号**的会话（`credential_kind=local_account`，`authentication_methods=["oidc"]`）。所以 0027/0031 的读取复核、`session_epoch` 踢下线、改密码和重置后会话失效，对 SSO 会话一样生效。不会自动开户：IdP 用户必须由负责人显式绑定到已有成员。
- **AUTH-DOS（0404，`run-auth-session-epoch.md`）和锁定**：AUTH-DOS 之后，别人输错密码不再让已有会话失效，锁定只挡新的密码登录。但是 0365 建立 SSO 会话时还会检查账号的 `locked_until`：账号在锁定期内，**SSO 登录也会被拒**（`member_unavailable`）。也就是说，别人知道成员的用户名，连续输错 5 次密码，就能在锁定期内挡住该成员的 SSO 登录（已有会话不受影响）。`sso_only` 成员的密码证据会被触发器拒绝，但失败次数会不会计入锁定，本单没有核实。**验收时加测一次**：用 M1 的用户名输错 5 次密码，然后用 A 走 SSO，把结果记进证据；是否要改，由负责人和 L6 决定（改动需要新的迁移）。
- **AUTH-FP（0403，`run-auth-fingerprint.md`）**：SSO 会话和密码会话用的是同一份账户指纹和浏览器 claim，规则相同。绑定、解绑以 `session_epoch` 起作用，不靠指纹。
- **入口票据（ADR-043，`run-entry-tickets.md`）**：入口票据是负责人进入钉住的合成测试账号（WebChat 测试客户、参考端点只读员工），走的是另一套密钥（`entry-ticket.key`）和另一组端点，与工作台 SSO 互不影响。负责人本人仍然用密码登录工作台（负责人不能绑定 SSO），然后再用入口票据进入测试账号。
- **限速**：`oidc_start` 是单独的限速桶，和密码登录（`LOGIN_RATE`）、重置、邀请的桶互不占用。

## 证据

- 第 0 节负责人的三项决定（日期、结论）；
- 第 2 节前提核对的结果；
- 第 3 步 compose 提交的 hash 和部署后 `origin/main` 的 hash；
- 第 4 节 13 条逐条通过或未通过，加上锁定与 SSO 交互那一条的结果；
- 计数结果（只有数字）；
- 回退是否演练过（建议在合成 IdP 上演练一次关闭再启用）。
