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
| 10 | 核对该次测试发布的 manifest、已部署版本及现场迁移回执；从对应 Migration Catalog 核对 0365、0371、`0445_nx067_oidc_gaps.sql`。若尚需部署或迁移，按批准的部署单和已验证备份执行；已应用的迁移不重复执行。用 `stage_secrets.py publish` 指定 `--out` 和 `--tenant`；无 DSN 的 `--dry-run` 只核编译计数，有 DSN 才能比对现场。随后按部署单正式发布 business-actions：新增的 `nexloop.workbench_member.set_sso_group_mapping` 发布之前会是 404。再按本版本的角色清单 v57 应用成员和授权 | 调度员 | **B 类** |
| 11 | 安装两个 secrets 文件到 APP_HOST（run-nx058 第 1 节和第 3 节第 4 步）：值从 Keychain 用管道传入，只比较摘要 | 调度员 | **B 类** |
| 12 | compose 改动走 feature 分支和 PR（run-nx058 第 3 节第 5 步），需要负责人 ack-policy | 调度员 | **B 类** |
| 13 | 重建 api-1、api-2。`oidc/providers` 返回刚配置的 provider；`POST .../oidc/backchannel/<provider_id>` 走登记的工作台 origin（正确 Host），显式带 `Content-Type: application/x-www-form-urlencoded`、空请求体时返回 400（已启用）或 404（未启用）。裸 POST 会在检查 provider 前被返回 400；429/503 等保留为未判定 | 调度员 | **B 类** |

## 3. 跑验收脚本

配置文件（0600，属于执行者本人，不进仓库）：

```json
{"schema": "nexloop-nx065-oidc-acceptance/1", "origin": "https://<APP_HOST_NAME>:<PORT>", "ca_file": "<GATEWAY_CA_FILE>",
 "provider_id": "<PROVIDER_ID>", "owner_username_file": "<0600 文件>",
 "member": {"principal_id": "<M1 的 principal_id>", "username_file": "<M1 用户名的 0600 文件>"},
 "users": {"A": {"subject": "<A 的 sub>"}, "B": {"subject": "<B 的 sub>"}},
 "owner_group": "<owner 组名>",
 "group_mapping": {"operator_group": "<operator 组名>", "reviewer_group": "<reviewer 组名>", "expected_revision": 0},
 "backchannel": true, "backchannel_wait_seconds": 60, "max_session_seconds": 900}
```

先核对既有托管测试租户的非秘密配置和托管人，再按需准备 §1 的测试资源。使用此次专用 provider：从既有受治理记录确认当前映射为空及其 revision、provider 下没有其他绑定、M1 未绑定；在 IdP 控制台确认 A 已启用且不在三个测试组。设置非空映射会结束该 provider 所有绑定成员的会话。任一前置无从核对，停止在首次变更之前。控制台回调、组名、账号和私有配置原件均不进验收证据。

负责人在已有安全终端中指定两个钥匙串条目的服务/账号及私有验收文件路径。第一行是工作台 owner 口令，启用 back-channel 时第二行必须是 M1 的工作台口令；两项只经管道交给脚本。M1 是现有独立合成 operator。

```bash
set -o pipefail
{
  security find-generic-password -s "${keychain_service:?}" -a "${owner_account:?}" -w &&
  security find-generic-password -s "${member_keychain_service:?}" -a "${member_account:?}" -w
} | python3 scripts/acceptance/nx065_oidc_acceptance.py --config "${acceptance_config:?}"
```

- 脚本会在终端（不是 stdout）里提示：打开授权地址，以 A 或 B 的身份在 IdP 登录，再把浏览器跳回的地址粘贴回来；仅在浏览器明确显示 IdP 拒绝登录时输入 `refused`。用无痕窗口，每次登录前清掉 IdP 的会话，确保登录的是提示的那个用户。
- 人工拒绝记录只含观察来源、配置中的 provider 标识和合成用户槽位。终端 EOF 或 Ctrl-C 中断验收或清理时，负责人须按下列清理项核对现场。
- 中途会提示到 IdP 管理端把 A 加进组或移出组、停用 A（并结束 A 在 IdP 的全部会话，这一步会触发 back-channel）、启用 A，做完按回车。
- `group_mapping` 会由脚本用负责人的受治理 Action 设置，仅在写入结果明确且持有本次映射版本时尝试清空。按下方逐项回执核实终态，未核实记 `unknown`。`expected_revision` 填这个 provider 当前的映射版本，第一次是 0。
- 最后一项会等 `max_session_seconds + 60` 秒（900 秒时约 16 分钟），这期间不要关掉终端。
- 脚本按已确认的初态恢复其成功改变的组（含 reviewer）和 A 的启用状态；从 IdP 管理端及受治理的解绑、清空映射记录核对终态，再输入 `verified`。脚本另查 owner 登出后会话为 401。
- stdout 每项检查一行 JSON，最后一行是汇总，不包含 sub、code、state、cookie、令牌、用户名、口令或组名。直接把 stdout 存成证据文件。

| 检查 | 预期 |
|---|---|
| `login.unlinked_refused` | passed |
| `login.linked_signs_in.link`、`login.linked_signs_in` | passed，`methods` 为 `["oidc"]` |
| `login.callback_one_time` | passed |
| `groups.no_role_from_idp` | passed（会话和 csrf 都是 200，错误 CSRF 的对照请求 401，真实请求 403 `forbidden`） |
| `disable.idp_refuses` | passed |
| `disable.sync.backchannel` | passed（同一 M1 的 OIDC 与口令会话，`session_http`、`password_http` 均为 `[200, 401]`；测量从人工确认停用后开始）；`backchannel: false` 时为 gap `not_configured`；等满 `backchannel_wait_seconds` 仍是 200 记 failed |
| `epoch.unlink_ends_sessions` | passed |
| `epoch.relink_new_only` | passed（`[200, 401, 401]`） |
| `groups.mapping` | passed（两次 CSRF 获取均为 200；固定缺失 question 探针第一项必须为 409 / `not_allowed_in_state` / `details.reason=not_found`，`operator_probe_verified=true`；第二项为 Action 的 403，`narrowed=true`。裸404、408/422、重定向、限流或其他非预期状态均不算阳性；CSRF 失败对应 null）；`group_mapping: null` 时为 gap `not_configured` |
| `groups.unmapped` | passed：先确认 operator 会话具有探针权限，再将 A 移出两个映射组并在 IdP 控制台回读。新登录被拒、无会话，先前 operator 探针由精确 409 变为受治理的 403。此项独立于仅命中 reviewer 的检查 |
| `disable.sync.max_session` | passed（`[200, 401]`） |

11 项业务检查必须全部出现，缺一项记 `run.incomplete` 失败；中途出异常只记一行 `run.aborted`（只写异常类型），已知结果的清理步骤独立执行；终端中断后停止，转人工核对。退出码：0 表示全部通过且没有缺口（目标）；3 表示检查都通过但还有 not_configured 的缺口；1 表示有检查失败，原样记录，不要重跑覆盖；2 表示输入被拒。

### 启用后中断与逐项回执

保留首次 stdout 和实际退出码。步骤 13 后、脚本启动前或负责人登录期间中断，可能没有业务检查行，另记已观察到的启用结果、停止位置和时间。初态确认中断记 `run.aborted`；验收主体遇到 EOF 或 Ctrl-C 直接汇总；清理中断记失败步骤并停止余项。人工见证的 IdP 拒绝取 `disable.idp_refuses` 的观察来源、provider 和合成用户槽位。撤销取对应检查行的前后会话状态。

按 M1 解绑、映射清空、A 的启用状态及三个组各自的成员关系、owner 登出分别留私有回执，记步骤、合成槽位、时间、观察来源、结果和脱敏证据引用。IdP 项以管理端读回为据，owner 登出以登出成功后同一会话返回 401 为据。清理失败时保留 `cleanup.progress.confirmed_steps`；其中 `unlink` 或 `clear_mapping` 也可能表示该步无需写入。成功时保留脚本的人工核实汇总及 owner 会话读数。各项有独立最终观察才记“已确认”，相反状态记“未恢复”，不适用须列依据，其余记 `unknown`。后续获准恢复及读回另起一行，保留首次结果。配置撤除另按第 5 节留证。

## 4. 脚本之外还要手工做的

run-nx058 第 4 节表里的第 6、7、8、10、11、12、13 条（负责人不能绑定自己、重复绑定被拒、`sso_only`、仅 SSO 邀请、撤销、限速、证据检查），以及第 6 节“锁定期内 SSO 登录也会被拒”那一条，仍然按 run-nx058 手工做，用 C 和 M2、M3。第 5 条 M1 普通口令登录可共用本次双会话的前置身份读回。第 9 条须在两个仍有效的 M1 会话（SSO、口令）上实测解绑前后状态；脚本在 back-channel 阶段取得的失效回执只证明停用。第 1、3、4、9 条的只读计数及审计字段仍按原单补齐。

## 5. 撤除和回退

按 NX-065 设计稿第 5 节。另外：
- 脚本持有本次映射版本且写入结果明确时才尝试清空。核受治理返回的 provider、revision 恰为原版本加一及 `cleared=true`，另从受治理记录独立核对映射为空、相应上限为 null。未核实记 `unknown`，先对账，不直接重发。手工设置过的映射，在核实当前版本并获准后，用 `set_sso_group_mapping` 携该版本传 `{}`，再独立读回。
- 出现非预期 HTTP、回包不可读、超时或中断时，保留首份结果，核对已知的绑定、mapping revision、A 的启用与三个组、owner 会话。映射提交返回 503、响应不明或关键前置未成立时直接收尾，禁止继续业务登录、最长时长等待和再次提交；未执行项列入 `run.incomplete`。保留首个 HTTP 状态和原 `request_id`，据此从既有受治理记录查证；IdP 侧在管理端回读。仅处置确由本次变更且仍需恢复的项。终端中断不会自动清理。
- 关闭 SSO 是 **B 类**（改 compose、重建 api），做之前也要负责人确认。
- 关闭前先按 run-nx058 §5 处置测试成员（含仅 SSO 和待绑定成员）及其会话。阶段性复用已有应用、租户、账号、组或文件时，逐项回到已登记初态；删除专属临时资源须核实归属和撤除授权。仅关 API 参数不会吊销已有会话。
- 迁移 `nx067_oidc_gaps` 不回退。没有映射、没开 back-channel 时，它只把 SSO 会话限定在 `max_session_seconds` 之内。

## 证据

- 第 0 节负责人的决定（日期和结论）；
- 第 3 步的 stdout 文件（检查结果和退出码）；
- 第 4 节手工检查的逐条结果；
- compose 提交的 hash、部署后 `origin/main` 的 hash；
- 撤除做到了哪一步。

### 离线判读补充（2026-11-16 P2）

固定不存在 question 的受治理 handler（0397 `runtime.nexloop_governed_answer_human_question`）抛 `human question: not_found`/22023，经 HTTP 层映射为409、code=`not_allowed_in_state`、details.reason=`not_found`。此前404阳性 mock 不代表该 handler 的实际语义：`WorkbenchActions.run` 在 Action未发布、grant检查前也可 LookupError→404/not_found，故404→403仅保留为歧义负例。408/request_timeout及422/invalid_request也在 bound/run前产生。验收只输出状态码和固定布尔 `operator_probe_verified`，不输出或保存原响应正文、错误文字、trace_id或details。此为脚本判读修正，不修改身份/SQL/IdP策略，也不构成 stage 执行许可。
