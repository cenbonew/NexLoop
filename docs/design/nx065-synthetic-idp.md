# NX-065 stage 验收用的合成 IdP

- 状态：设计稿，2026-11-10，L4。和选哪家 IdP 无关的部分可以先做；IdP 选型和启用时间由负责人定。
- 依据：`docs/release/run-nx058-oidc-enable.md`（启用、验收和回退），迁移 0365，`nexloop_eios/workbench_oidc.py`，`tests/support/mock_oidc_idp.py`。
- 产出：验收脚本 `scripts/acceptance/nx065_oidc_acceptance.py`（本地测试 `tests/test_nx065_oidc_acceptance_pg.py`），运行单 `docs/release/run-nx065-oidc-acceptance.md`。

## 1. 先看现状：任务里有两项产品目前不支持

NX-065 要求验收“组映射到角色”和“IdP 侧停用后同步”。按 0365 和 `workbench_oidc.py` 的现状：

- **组映射**：ID token 只读 `iss`、`sub`、`aud`、`azp`、`exp`、`iat`、`nbf`、`nonce`，不读组 claim。成员的角色只来自工作台角色清单（workbench-roles），SSO 只决定“这个 IdP 用户是哪个已有成员”。
- **停用同步**：没有 back-channel logout，没有 SCIM，也没有定期检查。IdP 停用用户后，这个用户发不起新的 SSO 登录（IdP 自己会拒绝），但已经建立的会话一直有效，直到负责人解绑、撤销成员，或者会话过期。

验收脚本把这两项如实报成 `gap`，不报 `passed`；只要还有 gap，脚本就以退出码 3 结束，NX-065 也就不能标成完成。脚本同时验证在现状下应该成立的事：IdP 侧的组不会给出任何角色（成员执行负责人才能做的 Action 会得到 403）；停用后无法再登录；解绑会结束所有 SSO 会话（session epoch）。

要补上这两项，需要新的产品工作，包括新迁移（临时号从 0443 起）和新的契约，由负责人决定做不做、怎么做。可选做法：
- 组映射：在 provider 文件里加 `group_claim` 和“组 → 角色”的映射，**只能降级、不能升级**（例如只允许映射到 operator 或 reviewer，并且不能超过清单里给的角色），每次登录时在 SQL 里比对。
- 停用同步：支持 OIDC back-channel logout（IdP 推送 logout token，验证签名后让这个成员的 session epoch 加一）；或者设定 SSO 会话的最长时长，到期后必须重新经过 IdP。

## 2. TLS：自建 IdP 要先解决证书信任

`OidcClient` 建 httpx 客户端时用的是 `trust_env=False`，没有指定 `verify`，所以只信任 certifi 自带的公共 CA，不认 `SSL_CERT_FILE`，也不认 stage 网关的私有 CA。另外 issuer 和四个端点都必须是 HTTPS 主机名，不能是私网 IP 字面量（`_https`）。

因此：
- **方案 A（建议先用）：托管 IdP 的独立测试租户**。证书本来就是公共 CA 签发的，主机名也是公网域名，不需要在我们的主机上起任何服务。前提是 api 容器能出网访问它的 token 和 jwks 地址，要先用只读方式核实。
- **方案 B：自建的开源 IdP**，作为单独的 compose project 起在 APP_HOST 上（和 NexLoop 的 compose 分开，停掉或删掉都不影响 NexLoop）。前提是有一个公网可以解析的主机名和公共 CA 签发的证书（例如 ACME）。只有私有 CA 的话，需要改代码让 `OidcClient` 接受 CA 文件，这属于新的代码改动，要另开任务。
- 两种方案都**不**放在 DATA_HOST 上：DATA_HOST 只放数据和备份，根权限的授权范围已经固定。

## 3. 只服务合成测试，不碰真实账号

- IdP 侧：方案 A 用独立的测试租户，方案 B 用独立的 realm。关闭自助注册，不连接任何真实目录（LDAP、AD、企业 IdP 联邦）。只建合成用户 A、B（运行单里另外加 C 测 `sso_only`），用户名一看就是测试用户，不使用真实人名和真实邮箱。
- 工作台侧：只把**专门为这次验收邀请的合成成员** M1（operator 或 reviewer）绑定到 A。脚本只操作配置文件里写的那一个 principal，跑完会解绑。负责人本人按设计就不能绑定 SSO（0365）。
- stage 上的 SSO 开关是 api 的 `--workbench-oidc-providers-file` 参数，对整个 stage 工作台生效。只要没有给某个成员做绑定，那个成员的登录方式就完全不变（未绑定的 IdP 用户会被拒绝，原因是 `not_linked`，也不会自动开户）。所以真实账号不会受影响，前提是不去绑定它们。
- 证据里只写 A、B、C、M1，不写 sub、用户名、主机名或地址。

## 4. 密钥：生成后直接进 Keychain，绝不进仓库

| 值 | 谁生成 | 存哪 |
|---|---|---|
| client secret | IdP 生成（方案 B 也可以由我们生成后填进 IdP） | Keychain `nexloop/stage/oidc/<provider_id>/client-secret`；APP_HOST 的 secrets 文件（0600，见 run-nx058 第 1 节） |
| 合成用户口令 | 本机生成：`python3 -c 'import secrets;print(secrets.token_urlsafe(24))'`，输出直接用管道交给 `security add-generic-password -U -s nexloop/stage/oidc/<provider_id>/user-<A\|B\|C> -a <账号> -w`，不在终端显示 | Keychain |
| IdP 管理口令（方案 B） | 同上 | Keychain `nexloop/stage/oidc/<provider_id>/idp-admin` |
| ID token 签名密钥 | IdP 自己生成（RS256，至少 2048 位） | 只在 IdP 内部，不导出 |
| 负责人工作台口令 | 已有 | Keychain（只通过管道交给验收脚本的 stdin） |

- `~/.nexloop/vault-index.md` 只记条目名、用途和日期，不记值。
- state、nonce 和 PKCE 复用已有的 `identity-token.key`，不需要新密钥。
- 生成口令用独立的脚本文件，不放进 `python -` 的 heredoc（之前出过事故：报错时把口令打进了 traceback）。

## 5. 撤除

1. 跑完验收脚本后，M1 已经是未绑定状态（脚本在 `finally` 里解绑）。如果手工绑定过 C 或别的成员，由负责人逐个解绑，或者撤销这些合成成员。
2. 关闭 SSO：按 run-nx058 第 5 节，去掉 `--workbench-oidc-providers-file`，重建 api-1 和 api-2。
3. 删除 APP_HOST 上的两个 secrets 文件。
4. IdP 侧：作废 client secret，删除 client 和合成用户。方案 A 删除测试租户；方案 B 停掉并删除那个 compose project 和它的数据卷（这是专门为验收起的、可以丢弃的环境）。
5. 在 Keychain 里把条目标记为作废，或者直接删除，并更新 vault-index。
6. 迁移 0365、0371 不回退。没有 provider 文件时，它们不改变任何行为。

## 否决的做法

| 做法 | 否决理由 |
|---|---|
| 用测试里的进程内 mock IdP 直接在 stage 上验收 | 它不走真实网络和 TLS，验证不了 api 容器出网、证书信任和 IdP 自己的登录页 |
| 让 `OidcClient` 读 `SSL_CERT_FILE` 来信任 stage 私有 CA | 要改代码，并且会让 api 的出网信任受环境变量影响；如果需要，另开任务明确地加一个 CA 文件参数 |
| 把合成 IdP 起在 DATA_HOST 上 | DATA_HOST 只放数据和备份，根权限的授权范围已经固定 |
| 把组 claim 直接当角色用（验收前顺手实现） | 会让 IdP 管理员能给自己升级成负责人；属于产品决定，要另开任务，并带上只降不升的约束 |
| 用真实员工账号或真实目录联邦做验收 | 违反“只用合成数据”；真实 IdP 接入另起一单 |
