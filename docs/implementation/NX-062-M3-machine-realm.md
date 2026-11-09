# NX-062 M3：machine_key realm、`/machine/v1`、限速失败关闭、网关日志与 pepper 挂载

- **依据**：`docs/implementation/NX-062-design.md` §2.1、§3.1、§3.5、§6 M3，L6 条件 C1（HTTP 层）、C4、C9。执行：L10。
- **基线**：批次 152（f754de15）+ M2a 修复 18e53ec4（0436，模拟角色的 grant）。本片迁移为 `0437_nx062_machine_realm.sql`（临时号）。

## 做了什么

| 部分 | 内容 |
|---|---|
| SQL（0437） | `control.nexloop_machine_rate_policies`：只追加；由可信配置发布（configurator）；没有任何一行时一律拒绝。`runtime.nexloop_machine_rate_buckets`：按密钥和按租户的一分钟窗口，键由认证结果推出，不用客户端地址。`runtime.nexloop_machine_call_counts`：按天、租户、结果计数。`authz.nexloop_machine_admit`：认证失败不抛错，返回状态（ok、denied、rate_limited、rate_policy_missing）并提交计数，所以拒绝也进告警。`authz.nexloop_machine_capability_list`：scope ∩ 允许表 ∩ 主体授权；授权要到 M5 才有，所以现在恒为空。metrics snapshot 改名包装，real world 新增 `machine_access` 块；按 0436 的做法复制原函数的全部 grant |
| HTTP | `nexloop_eios/machine_http.py`：`GET /machine/v1/capabilities`。处理顺序：有查询串返回 400（不读也不计数）；只认 `Authorization: Bearer nxm_…`；先按 prefix 取版本，再算一次 HMAC（C2）；调用 admit；返回 401、429（带 Retry-After）或 503（rate_policy_missing / unavailable）；进程内并发上限 8，超出返回 503 busy。不读任何身份类请求头。错误只返回错误码，响应都带 no-store |
| 配置 | `ApiConfiguration.machine_key_peppers_file`：启动时读一次，读不到时 realm 对所有密钥返回 503。`deploy/configuration/machine-rate-limits.v1.json`：每分钟每把密钥 60 次、每个租户 600 次。`python -m nexloop_eios.machine_access publish-rate-policy`。告警规则 v35 = v34 加 `machine_rate_policy_missing`（critical）和 `machine_key_denials`（warning，24 小时超过 50 次），阈值写在规则文件里 |
| C8 清单 | api-routes v14：新增 realm `machine_key` 和 kind `machine_capability`（schema 同步修改），新增路由 `GET /machine/v1/capabilities`；G-2、G-3 的计数相应更新 |
| 网关（C9） | `Caddyfile.template` 增加 `handle /machine/*`，删除 X-Forwarded-For、X-Forwarded-User、X-Real-IP、X-NexLoop-Client-IP、X-NexLoop-Principal、X-NexLoop-Tenant、X-NexLoop-World、X-NexLoop-Actor 和 Cookie。整个文件没有任何 log 指令，所以不会记录 URI 或 query；`test_deploy_render` 对此有断言 |
| compose 与密钥树 | api-1 和 api-2 增加 `--machine-key-peppers-file /run/secrets/api_machine_key_peppers` 和 secret `api_machine_key_peppers`（`secrets/api/machine-key-peppers`）。`stage_secrets`：`generate` 写出版本 1；`machine-key-peppers` 命令给旧的密钥树补加，`--rotate` 追加下一版本 |
| 运行单 | `docs/release/run-nx062-machine-access.md`：部署顺序（备份 → 迁移 → 先 publish → 角色 → pepper → 速率策略 → 告警 v35 → gateway → 先 gate 后 promote → 验收）；pepper 存入钥匙串，经 `security -i` 标准输入写入，不进命令参数；恢复演练条目 P-1 至 P-6，纳入下一轮 AT-072 |

## 测试

- `tests/test_machine_realm_pg.py`（真实 PG + 真实 HTTP 应用），5 个用例：
  - **G-5 凭据矩阵（C1）**：对 machine_key 路由，无凭据、其他 Bearer、已吊销的机器密钥、workbench cookie 一律 401，同一把有效密钥返回 200，证明这些拒绝是真实的。对其他每条非豁免路由，带有效机器密钥一律 401 或 403。矩阵为空，或行数不等于“机器路由数 × 4 + 其他路由数”，测试失败。
  - **C4**：未发布策略时返回 503 rate_policy_missing，有计数，指标为 1，v35 规则会触发，SQL configurator 接受 v35。发布后，3 次返回 200 和空列表，第 4 次返回 429 带 Retry-After，计数正确。同一策略重发不产生新行；版本号不升高会被拒；只有 configurator 能发布。
  - **拒绝与查询串**：任何查询串都返回 400，不计数，响应里不出现密钥；未知 prefix 和伪造的 secret 返回同一个 401；伪造身份头不改变结果；主体停用后返回 401；denied 计数正确。
  - **无 pepper 时**：返回 503 unavailable；不带凭据时仍是 401。
  - **授权检查**：metrics 包装后原函数无人可调；新表对应用角色都不可读；能力列表只有 nexloop_api 能调用。
- `test_stage_secrets::test_machine_key_peppers_are_generated_added_and_rotated_by_appending`。
- `test_deploy_render::test_c9_the_gateway_logs_no_uri_and_the_machine_realm_gets_no_identity_header`。
- 所有解析 compose、连接预算或 Caddyfile 的测试（26 个文件，含 test_stage_compose、test_deploy_render、test_connection_budget_live_pg、test_deploy_dryrun、test_stage_secrets、test_stage_register_pg 和 nx041 的模拟组），加上路由清单 G-1..G-6、三个机器相关测试文件、test_machine_keys、bootstrap、copy guard、sim roles、三项擦除覆盖、告警和 host health、observability、http_api：**547 passed**（Node 24，`-n 2`）。
- 首次失败：
  - `test_nx041_sim_pi_run_pg` 3 项失败，原因是 0434 的模拟角色 grant 缺陷，由 18e53ec4 单独修复；
  - G-5 矩阵里 webhook 返回 404，原因是 connector 未配置，与原 G-5 测试一样补上配置；
  - `test_until_m5` 断言“没有 machine_key realm”，现在改为“只有空的能力列表路由”。

## 变异检查

每次改一处，跑对应测试，跑完恢复：

| 变异 | 结果 |
|---|---|
| 没有策略时仍放行 | 1 failed |
| 去掉每把密钥的窗口检查 | 1 failed |
| 认证失败不计数 | 1 failed |
| 能力列表去掉“M5 授权”这一项 | 2 failed |
| 不拦截查询串 | 1 failed |
| Caddy 不删除 Cookie | 1 failed |
| 不加载 pepper | 3 failed |
| metrics 包装不复制 grant | **存活**：原函数本来就没有被授权的角色（只有属主），复制循环是空操作；保留是为了与 0436 做法一致 |
| HTTP 层把 denied 当作 ok | **存活**：后续的 capability_list 会再做一次身份检查并返回 401，是有意保留的双重检查 |

## 未做与提案

- **契约**：线路规则禁止本线改 `packages/contracts`。`machine-capabilities.schema.json`（响应 `{capabilities:[{capability,kind,description}]}`）和统一错误体 `{code,message,trace_id,retryable,details}` 作为提案，请调度员决定由谁加入。
- **实地验证**：本机没有 caddy，无法 `caddy validate`；新增的 header_up 用的都是已有用法（逐个列出头名，不用通配）。上 stage 前，gate 阶段的 gateway 启动会验证配置。
- **没有改动**：M5 的授权与事实快照、写入（M5）、MCP 与 SDK（M6）。

## L6 条件 H1、H2（82b7919a 条件 PASS）

- **H1**：G-5 矩阵改为逐格断言 6 种凭据：无凭据、机器密钥、workbench cookie、consumer（WebChat）cookie、真实 service 令牌、经过签名的渠道（connector）请求。
  - service 令牌由 `seed_multi_authority` 现场签发。测试先断言它能经 `authenticate_service` 认证为 service 主体，再用它访问机器路由。
  - 渠道请求使用已配置 connector 的密钥，按 `commercial.sign` 生成签名。
  - 结果：只有机器密钥返回 200，其余 5 种一律 401。断言行数 = 机器路由数 × 6，且 6 种凭据都出现。其他每条非豁免路由带有效机器密钥一律返回 401 或 403。
- **H2**：`stage_secrets.add_machine_key_peppers` 改用 `_replace_private_durable`：先在同目录写 0600 临时文件并 fsync，再 rename 覆盖目标，最后 fsync 目录。rename 失败时删除临时文件，目标保持原样。
  - 新测试 `test_machine_key_pepper_rotation_replaces_atomically` 在 rename 时模拟崩溃，断言四点：rename 前目标仍是旧内容；临时文件已含旧内容加新版本，并已 fsync；崩溃后目标不变，且没有残留的临时文件；正常重跑后多出一行、权限 0600。
  - 变异检查：改回“先 unlink 再写”，测试变红；去掉 fsync，测试变红。
  - 文件里已有一个同名的 `_replace_private`，被凭据轮换使用，不 fsync。这个函数不在本条件范围内，没有改动，作为后续项列出。
- 测试：`test_machine_realm_pg` 和 `test_stage_secrets` 共 36 passed（Node 24，`-n 2`）。
