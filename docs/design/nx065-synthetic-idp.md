# NX-065 stage 验收用的合成 IdP

- 状态：2026-11-10 设计，2026-11-16 按源码基线 `236003b3` 对齐。负责人已选托管 IdP 的测试租户（见[当前运行单](../release/run-nx065-oidc-acceptance.md) §0）；启用时间仍待负责人确定。本次源码核对不证明 stage 已启用或验收完成。
- 依据：[启用与回退运行单](../release/run-nx058-oidc-enable.md)、[NX-067 设计](nx067-oidc-group-roles-and-disable-sync.md)，迁移 0365 与正式 `0445_nx067_oidc_gaps.sql`（以 Migration Catalog 为准），`nexloop_eios/workbench_oidc.py`，`tests/support/mock_oidc_idp.py`。
- 产出：验收脚本 `scripts/acceptance/nx065_oidc_acceptance.py`（本地测试 `tests/test_nx065_oidc_acceptance_pg.py`），运行单 `docs/release/run-nx065-oidc-acceptance.md`。

## 1. 历史缺口、当前实现与验收边界

**旧基线（0365，NX-067 实现之前）**：SSO 只绑定已有成员，不处理组映射；IdP 停用只阻止重新登录，已有会话等待解绑、撤销或过期。旧版验收脚本将组映射和停用同步记为 `gap`、退出码 3。当时提出的“把映射放进 provider 文件”只是未采用的方案，已由 NX-067 的受治理配置取代，不能据此配置当前系统。

**当前源码（`236003b3`）**：NX-067 已提供组角色上限、SSO 会话最长时长及 back-channel logout，正式迁移为 `0445_nx067_oidc_gaps.sql`。

- 组映射由负责人通过 `nexloop.workbench_member.set_sso_group_mapping:1` 设置或清空，**不写进 provider 私有文件**。有效权限取角色清单与组上限的 Action 交集，只降不升；未命中时锁住全部工作台 Action，清空映射才恢复清单权限。具体规则与派发复核见 NX-067 §2、§6。
- 停用同步由两项共同覆盖：provider 的 `max_session_seconds`（默认 28800，允许 900–43200）和启用后的 back-channel logout。后者验证 logout token 后递增成员的 `session_epoch`，结束全部会话；前者给出无推送时的失效上限。SCIM 不在范围内。
- 当前验收脚本分别报告 `groups.mapping`、`disable.sync.backchannel` 和 `disable.sync.max_session`；没有映射或未开 back-channel 仍是 `not_configured` gap，不能记为通过。全部必需检查通过且没有 gap 才退出 0；步骤、配置与清理以[当前运行单](../release/run-nx065-oidc-acceptance.md) §3–5 为准。

**审查与现场边界**：NX-067 设计经 L6 条件 PASS，Z1–Z5 及负责人取舍见 NX-067 §6。离线探针修复 `68a26497` 经 L6 复审为 P2 CLOSED / 局部 PASS（36 项纯内存回归）：只接受固定缺失 question 的精确 `409 / not_allowed_in_state / details.reason=not_found` 作为授权阳性，裸 404 不算。该结论只关闭判读缺口，不证明 PG/app 集成、真实 IdP、网络/TLS 或 stage 验收通过。NX-065/067 在该基线仍为 `in_progress`；部署、启用和现场验收仍按运行单执行并取得对应证据。

## 2. TLS：自建 IdP 要先解决证书信任

`OidcClient` 建 httpx 客户端时用的是 `trust_env=False`，没有指定 `verify`，所以只信任 certifi 自带的公共 CA，不认 `SSL_CERT_FILE`，也不认 stage 网关的私有 CA。另外 issuer 和四个端点都必须是 HTTPS 主机名，不能是私网 IP 字面量（`_https`）。

因此：
- **方案 A（负责人已选）：托管 IdP 的独立测试租户**。证书本来就是公共 CA 签发的，主机名也是公网域名，不需要在我们的主机上起任何服务。前提是 api 容器能出网访问它的 token 和 jwks 地址，要先用只读方式核实。
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
