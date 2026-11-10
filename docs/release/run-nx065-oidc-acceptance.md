# 运行单：NX-065 工作台 SSO 在 stage 上的合成 IdP 验收

- 依据：
  - `docs/design/nx065-synthetic-idp.md`：合成 IdP 设计、TLS 限制、密钥与撤除；
  - `docs/design/nx067-oidc-group-roles-and-disable-sync.md`：组映射只降不升、停用同步，迁移 `nx067_oidc_gaps`；
  - `docs/release/run-nx058-oidc-enable.md`：启用和回退的具体做法，本单不重复。
- 执行者：负责人（IdP 侧操作和浏览器登录）、调度员（部署）。判定者：L6。决定者：负责人。
- **B 类步骤**要等负责人在对话里明确确认后才能做，表里已经标出。
- 本单写成时**还没有执行**，stage 上也还没有启用 SSO。

## 0. 负责人的决定（2026-11-10，“都按推荐”）

1. IdP 用托管 IdP 的测试租户，不自建（设计稿第 2 节方案 A）。
2. 两项缺口（组映射、停用同步）另开任务补上，即 NX-067。补上并部署之后，验收脚本的目标退出码是 0。
3. 还要负责人定的：启用时间。
4. NX-067 的取值先按设计现值，以后可以改：
   - 一个映射都没命中时，锁住全部权限；
   - SSO 会话最长 8 小时，每个 provider 可以在 900–43200 秒之间调；
   - 只有清空映射才把上限清回 null。

## 1. 在托管测试租户里配置（不改 stage）

所有值都在 IdP 的管理控制台里填写或读取。生成的密钥和口令直接进 Keychain，不写进文档、命令行、截图或提交。

| 步 | 内容 | 由谁 | 类别 |
|---|---|---|---|
| 1 | 新建一个应用（OIDC，Web，授权码 + PKCE S256），ID token 签名 RS256。登录回调地址填 `<工作台 origin>/api/v1/workbench/auth/oidc/callback`，必须完全一致 | 负责人 | — |
| 2 | client secret 生成后直接存进 Keychain：`nexloop/stage/oidc/<provider_id>/client-secret`；vault-index 只记条目名 | 负责人 | — |
| 3 | 打开 back-channel logout（产品里常叫 Back-Channel Logout 或 Logout URI），地址填 `<工作台 origin>/api/v1/workbench/auth/oidc/backchannel/<provider_id>`，并要求 logout token 带 `sub`。产品不支持 back-channel 的话跳过这一步，配置里写 `backchannel: false`，验收会报 not_configured | 负责人 | — |
| 4 | 让 ID token 带上组 claim，claim 名用 `groups`，值是用户所在组的名字数组 | 负责人 | — |
| 5 | 建三个组：一个映射到 operator，一个映射到 reviewer，一个表示 owner（用来证明组不会给出角色）。先都不放人 | 负责人 | — |
| 6 | 建合成用户 A、B、C。口令用独立的脚本文件生成，直接管道交给 `security add-generic-password`，不在终端显示。在管理端记下各自的 `sub`，只写进私有配置，不写进证据 | 负责人 | — |
| 7 | 只读核实 api 容器能访问 IdP 的 token 和 jwks 地址（要出网） | 调度员 | — |
| 8 | 按 run-nx058 第 3 节第 3 步写 provider 文件，另加 `"max_session_seconds": 900` 和 `"backchannel_logout": true`（第 3 步跳过时为 false），在管理机上用解析器检查一遍 | 负责人 | — |
| 9 | 普通邀请一名合成成员 M1（operator），设好密码；记下它的 principal_id | 负责人 | — |

## 2. 部署和启用（B 类）

| 步 | 内容 | 由谁 | 类别 |
|---|---|---|---|
| 10 | 部署带迁移 `nx067_oidc_gaps` 的版本（迁移前按部署单做备份）。用 `stage_secrets.py publish` 先 dry-run，再正式发布 business-actions：新增的 `nexloop.workbench_member.set_sso_group_mapping` 发布之前会是 404。再按角色清单 v56 应用成员和授权 | 调度员 | **B 类** |
| 11 | 安装两个 secrets 文件到 APP_HOST（run-nx058 第 1 节和第 3 节第 4 步）：值从 Keychain 用管道传入，只比较摘要 | 调度员 | **B 类** |
| 12 | compose 改动走 feature 分支和 PR（run-nx058 第 3 节第 5 步），需要负责人 ack-policy | 调度员 | **B 类** |
| 13 | 重建 api-1、api-2。`oidc/providers` 返回刚配置的 provider；`POST .../oidc/backchannel/<provider_id>` 不带参数时返回 400（开了 back-channel）或 404（没开） | 调度员 | **B 类** |

## 3. 跑验收脚本

配置文件（0600，属于执行者本人，不进仓库）：

```json
{"schema": "nexloop-nx065-oidc-acceptance/1", "origin": "https://<APP_HOST_NAME>:<PORT>", "ca_file": "<GATEWAY_CA_FILE>",
 "provider_id": "<PROVIDER_ID>", "owner_username_file": "<0600 文件>",
 "member": {"principal_id": "<M1 的 principal_id>"},
 "users": {"A": {"subject": "<A 的 sub>"}, "B": {"subject": "<B 的 sub>"}},
 "owner_group": "<owner 组名>",
 "group_mapping": {"operator_group": "<operator 组名>", "reviewer_group": "<reviewer 组名>", "expected_revision": 0},
 "backchannel": true, "backchannel_wait_seconds": 60, "max_session_seconds": 900}
```

```bash
security find-generic-password -s <keychain 服务名> -a <负责人账号> -w | python3 scripts/acceptance/nx065_oidc_acceptance.py --config <配置文件>
```

- 脚本会在终端（不是 stdout）里提示：打开授权地址，以 A 或 B 的身份在 IdP 登录，再把浏览器跳回的地址粘贴回来；IdP 拒绝登录时输入 `refused`。用无痕窗口，每次登录前清掉 IdP 的会话，确保登录的是提示的那个用户。
- 中途会提示到 IdP 管理端把 A 加进组或移出组、停用 A（并结束 A 在 IdP 的全部会话，这一步会触发 back-channel）、启用 A，做完按回车。
- `group_mapping` 会由脚本用负责人的受治理 Action 设置，脚本结束时清空。`expected_revision` 填这个 provider 当前的映射版本，第一次是 0。
- 最后一项会等 `max_session_seconds + 60` 秒（900 秒时约 16 分钟），这期间不要关掉终端。
- 脚本结束时会清空映射、解绑 M1，并提示把 A 启用、移出各组。
- stdout 每项检查一行 JSON，最后一行是汇总，不包含 sub、code、state、cookie、令牌、用户名、口令或组名。直接把 stdout 存成证据文件。

| 检查 | 预期 |
|---|---|
| `login.unlinked_refused` | passed |
| `login.linked_signs_in.link`、`login.linked_signs_in` | passed，`methods` 为 `["oidc"]` |
| `login.callback_one_time` | passed |
| `groups.no_role_from_idp` | passed（会话和 csrf 都是 200，错误 CSRF 的对照请求 401，真实请求 403 `forbidden`） |
| `disable.idp_refuses` | passed |
| `disable.sync.backchannel` | passed（`session_http` 为 `[200, 401]`）；`backchannel: false` 时为 gap `not_configured`；等满 `backchannel_wait_seconds` 仍是 200 记 failed |
| `epoch.unlink_ends_sessions` | passed |
| `epoch.relink_new_only` | passed（`[200, 401, 401]`） |
| `groups.mapping` | passed（`operator_action_http` 第一项不是 403，第二项是 403，`narrowed` 为 true）；`group_mapping: null` 时为 gap `not_configured` |
| `disable.sync.max_session` | passed（`[200, 401]`） |

10 项检查必须全部出现，缺一项记 `run.incomplete` 失败；中途出异常只记一行 `run.aborted`（只写异常类型），清理步骤仍逐个执行。退出码：0 表示全部通过且没有缺口（目标）；3 表示检查都通过但还有 not_configured 的缺口；1 表示有检查失败，原样记录，不要重跑覆盖；2 表示输入被拒。

## 4. 脚本之外还要手工做的

run-nx058 第 4 节表里的第 6、7、8、10、11、12、13 条（负责人不能绑定自己、重复绑定被拒、`sso_only`、仅 SSO 邀请、撤销、限速、证据检查），以及第 6 节“锁定期内 SSO 登录也会被拒”那一条，仍然按 run-nx058 手工做，用 C 和 M2、M3。

## 5. 撤除和回退

按 NX-065 设计稿第 5 节。另外：
- 映射由脚本清空；手工设置过的映射，用 `set_sso_group_mapping` 传 `{}` 清空，所有上限回到 null。
- 关闭 SSO 是 **B 类**（改 compose、重建 api），做之前也要负责人确认。
- 迁移 `nx067_oidc_gaps` 不回退。没有映射、没开 back-channel 时，它只把 SSO 会话限定在 `max_session_seconds` 之内。

## 证据

- 第 0 节负责人的决定（日期和结论）；
- 第 3 步的 stdout 文件（检查结果和退出码）；
- 第 4 节手工检查的逐条结果；
- compose 提交的 hash、部署后 `origin/main` 的 hash；
- 撤除做到了哪一步。
