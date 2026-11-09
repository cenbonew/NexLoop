# 运行单：NX-065 工作台 SSO 在 stage 上的合成 IdP 验收

- 依据：`docs/design/nx065-synthetic-idp.md`（合成 IdP 设计、TLS 限制、密钥与撤除），`docs/release/run-nx058-oidc-enable.md`（启用和回退的具体做法，本单不重复）。
- 执行者：负责人（IdP 侧操作和浏览器登录）、调度员（部署）。判定者：L6。决定者：负责人。
- **B 类步骤**要等负责人在对话里明确确认后才能做，表里已经标出。
- 本单写成时**还没有执行**，stage 上也还没有启用 SSO。

## 0. 负责人先定

1. IdP 用方案 A（托管 IdP 的测试租户，建议）还是方案 B（自建，需要公网主机名和公共 CA 证书）。见设计稿第 2 节。
2. 启用时间。
3. 设计稿第 1 节的两项缺口（组映射、停用同步）：是这次只记录缺口，还是另开任务补上。补上之前，验收脚本的退出码是 3，NX-065 不能标完成。

## 1. 准备（不改 stage）

| 步 | 内容 | 由谁 | 类别 |
|---|---|---|---|
| 1 | 在合成 IdP 上建 client，回调地址是 `<工作台 origin>/api/v1/workbench/auth/oidc/callback`，签名算法 RS256。client secret 直接存进 Keychain（设计稿第 4 节） | 负责人 | — |
| 2 | 建合成用户 A、B、C，口令生成后直接存进 Keychain；在 IdP 上建一个组（例如叫 owner 的组），先不放人 | 负责人 | — |
| 3 | 只读核实 api 容器能访问 IdP 的 token 和 jwks 地址（方案 A 要出网） | 调度员 | — |
| 4 | 按 run-nx058 第 3 节第 3 步写 provider 文件，并在管理机上用解析器检查一遍 | 负责人 | — |
| 5 | 普通邀请一名合成成员 M1（operator），设好密码；记下它的 principal_id | 负责人 | — |

## 2. 启用（B 类）

| 步 | 内容 | 由谁 | 类别 |
|---|---|---|---|
| 6 | 安装两个 secrets 文件到 APP_HOST（run-nx058 第 1 节和第 3 节第 4 步） | 调度员 | **B 类** |
| 7 | compose 改动走 feature 分支和 PR（run-nx058 第 3 节第 5 步），需要负责人 ack-policy | 调度员 | **B 类** |
| 8 | 部署，重建 api-1、api-2；`oidc/providers` 返回刚配置的 provider | 调度员 | **B 类** |

## 3. 跑验收脚本

配置文件（0600，属于执行者本人，不进仓库）：

```json
{"schema": "nexloop-nx065-oidc-acceptance/1", "origin": "https://<APP_HOST_NAME>:<PORT>", "ca_file": "<GATEWAY_CA_FILE>",
 "provider_id": "<PROVIDER_ID>", "owner_username_file": "<0600 文件>",
 "member": {"principal_id": "<M1 的 principal_id>"},
 "users": {"A": {"subject": "<A 的 sub>"}, "B": {"subject": "<B 的 sub>"}},
 "owner_group": "<第 2 步建的组名>"}
```

```bash
security find-generic-password -s <keychain 服务名> -a <负责人账号> -w | python3 scripts/acceptance/nx065_oidc_acceptance.py --config <配置文件>
```

- 脚本会在终端（不是 stdout）里提示：打开授权地址，以 A 或 B 的身份在 IdP 登录，再把浏览器跳回的地址粘贴回来；IdP 拒绝登录时输入 `refused`。用无痕窗口，每次登录前清掉 IdP 的会话，确保登录的是提示的那个用户。
- 中途会提示到 IdP 管理端把 A 加进组或移出组、停用或启用 A，做完按回车。
- 脚本结束时会解绑 M1，并提示把 A 启用、移出组。
- stdout 每项检查一行 JSON，最后一行是汇总，不包含 sub、code、state、cookie、令牌、用户名或口令。直接把 stdout 存成证据文件。

| 检查 | 预期 |
|---|---|
| `login.unlinked_refused` | passed |
| `login.linked_signs_in.link`、`login.linked_signs_in` | passed，`methods` 为 `["oidc"]` |
| `login.callback_one_time` | passed |
| `groups.no_role_from_idp` | passed（会话和 csrf 都是 200，错误 CSRF 的对照请求 401，真实请求 403 `forbidden`） |
| `groups.mapping` | gap（产品不读组 claim） |
| `disable.idp_refuses` | passed |
| `disable.sync` | gap（session_http `[200, 200]`：停用前后会话都在）；停用前不是 200 记 failed |
| `epoch.unlink_ends_sessions` | passed（两个会话都是 401） |
| `epoch.relink_new_only` | passed（`[200, 401, 401]`） |

9 项检查必须全部出现，缺一项记 `run.incomplete` 失败；中途出异常只记一行 `run.aborted`（只写异常类型），清理步骤仍逐个执行。退出码：0 表示全部通过且没有缺口；3 表示检查都通过但还有缺口（现状预期就是 3）；1 表示有检查失败，原样记录，不要重跑覆盖；2 表示输入被拒。

## 4. 脚本之外还要手工做的

run-nx058 第 4 节表里的第 6、7、8、10、11、12、13 条（负责人不能绑定自己、重复绑定被拒、`sso_only`、仅 SSO 邀请、撤销、限速、证据检查），以及第 6 节“锁定期内 SSO 登录也会被拒”那一条，仍然按 run-nx058 手工做，用 C 和 M2、M3。

## 5. 撤除和回退

按设计稿第 5 节。关闭 SSO 是 **B 类**（改 compose、重建 api），做之前也要负责人确认。

## 证据

- 第 0 节负责人的决定（日期和结论）；
- 第 3 步的 stdout 文件（检查结果和退出码）；
- 第 4 节手工检查的逐条结果；
- compose 提交的 hash、部署后 `origin/main` 的 hash；
- 撤除做到了哪一步。
