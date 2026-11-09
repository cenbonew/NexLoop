# NX-062 M2b：机器接入生命周期，作为负责人的受治理 Action

- **依据**：`docs/implementation/NX-062-design.md` §1.4、§2.2、§3.6；ADR-041 §2.1–2.2 与 Q-4（只有负责人可签发和吊销）。执行：L10。接在 M2a（dba6b02e）之后。
- **迁移**：`0435_nx062_machine_lifecycle.sql`（临时号）。内容是统一受治理入口 0124 的 6 行注册和对应的 handler，没有改入口本身，也没有复制已有函数体。
- **范围调整（已报调度员）**：机器主体的 EIOS 授权事实、机器专用的事实快照，与 M5 的 0124 机器分支（C7）一起做。原因是 C1 要求 service 快照拒绝机器摘要，所以现有的事实读取路径读不到机器的事实；先按模板写入事实，到 M5 很可能要返工。本片不写任何 EIOS 事实。

## 新增的 Action（全部只属负责人）

| capability | 操作 | 说明 |
|---|---|---|
| `machine_client.register` | `register_machine_client` | 登记企业程序：client_id、显示名、用途 |
| `machine_client.deactivate` | `deactivate_machine_client` | 停用，同一事务里吊销名下所有密钥 |
| `machine_client.activate` | `activate_machine_client` | 重新启用，旧密钥不恢复 |
| `machine_key.issue` | `issue_machine_key` | 第一把密钥：world 只能是 real 或 test，scope 必须在允许表内，有效期 1–90 天 |
| `machine_key.rotate` | `rotate_machine_key` | 下一把密钥，旧钥在不超过 7 天的重叠期内结束 |
| `machine_key.revoke` | `revoke_machine_key` | 立即吊销一把密钥 |

- subject_rule 一律为 `human`。handler 自己再检查一次：调用者必须是在任的负责人（`nexloop_workbench_member_active(..., true)`），机器主体一律拒绝；操作者会话的 world 必须是 `real`。
- 密钥由工作台 HTTP 层在提交时生成（`machine_keys.generate`）。SQL 只收到 prefix、当前 pepper 版本下的 HMAC 摘要和版本号。密钥明文只在执行成功的那次响应里出现一次（`Cache-Control: no-store`），请求体不能自带 prefix 或 digest 等字段（返回 422）。
- **同一请求重试**：每次提交都会生成新密钥，请求体与已认领的不同，所以返回 409，不会多签发，也不会再显示第一把密钥。工作台邀请也是这个行为。负责人用新请求重新签发，并吊销丢失的那把。这里没有加 execution-time fields：那张表（0347）属于审批机制，与本片无关。
- 拒绝时返回固定原因码 `machine access: <code>`，HTTP 层映射为 409 与 `details.reason`。原因码：client_exists、principal_conflict、client_not_found、client_inactive、rotate_required、key_limit、key_not_found、key_not_active、key_conflict、scope_not_allowed、world_not_allowed、invalid、owner_world_only；owner_required 映射为 403。
- 审计：每次变更都写一行 `runtime.audit_events`（trace_id `machine_access`，因此进入 NX-059 哈希链）。只记 id、prefix、world、scope、到期时间和原因码，不记摘要和密钥。

## 配置与清单的改动

| 文件 | 改动 |
|---|---|
| `deploy/configuration/business-actions.v1.json` | v57：新增 6 个 Action |
| `deploy/configuration/capabilities.v1.json` | v10：新增 6 个 capability，schema_hash 由 `catalog_manifests.schema_hash` 计算 |
| `deploy/configuration/system-catalog-names.v1.json` | 新增 6 个名字 |
| `deploy/authorization/workbench-roles.v1.json` | v54：owner 从 98 个 Action 增加到 104 个，进入 owner-only 集合，新增 slice `machine_access` |
| `deploy/authorization/api-routes.v1.json` | v13：工作台写入口新增 6 个 operation（路由数不变） |
| `nexloop_eios/browser_http.py`、`http_api.py` | `BrowserConfiguration.machine_key_peppers_file` 与命令行参数 `--machine-key-peppers-file`。不配置时签发和轮换返回 503，其余操作照常可用 |

## 部署影响（写给调度员）

1. 迁移 0435（组批时会重新编号），按惯例先做备份。
2. **新 Action 必须先 publish**：`stage_secrets.py publish` 先 dry-run，确认计划里有这 6 个 Action；未发布前工作台调用返回 404（not_found）。stage 的发布总数从 136 变为 142（`test_stage_register_pg`）。
3. 工作台角色清单 v54 需要重新配置，负责人才会拿到这 6 个授权。
4. pepper 文件：只有签发和轮换需要。本片**没有**改 compose，也没有挂载 secret；不配置时签发返回 503。compose、secret 挂载，以及 pepper 的备份与恢复演练，放到 M3 和机器 realm 一起做，届时再写运行单。
5. 本片没有新路由，也不改机器 realm；机器密钥签发出来后还不能用于任何调用（M3 之前没有 `/machine/v1`）。

## 测试

- `tests/test_machine_lifecycle_pg.py`（真实 PG，经真实工作台 HTTP 入口），4 个用例：
  - 完整生命周期：登记、重复登记、签发（只显示一次，库和审计里查不到密钥和摘要）、同一请求重试（409，不显示密钥）、第二把密钥要求轮换、轮换、超过 2 把上限、吊销、重复吊销、停用级联、停用后轮换被拒、重新启用不恢复，以及审计序列与 actor。
  - 固定原因码与封闭的请求体：身份字段、自带 digest 或 prefix、91 天、sim world、空 scope、布尔天数一律 422；scope 不在允许表、永不授予、主体不存在、密钥不存在返回对应原因码。
  - 操作者 403。直接调用 handler 时，非负责人或机器主体返回 owner_required，test world 返回 owner_world_only，sim 和 shadow 的 key world 返回 world_not_allowed；注册行全部是 human；任何应用角色都没有 handler 的执行权限。
  - pepper 文件权限不对时返回 503，不签发。
- 相关回归（`-n 2`）：test_machine_lifecycle_pg、test_machine_access_pg、test_governed_entry_pg、test_business_actions、test_workbench_roles、test_workbench_read_pg、test_api_route_inventory(_pg)、test_catalog_manifests(_pg)、test_system_catalog_pg、test_stage_register_pg、三项擦除覆盖、test_function_copy_guard、test_workbench_actions_http、test_action_claim_settle_pg、test_workbench_invitations_pg、test_owner_reset_and_invite_pg、test_stage_secrets、test_http_api，共 301 passed，另有 1 项失败：`test_stage_secrets::test_the_agent_host_starts_with_the_generated_runtime_files`。原因是这次没有切到 Node 24；切到 Node 24 后单独重跑通过。
- 首次失败：
  - 签发响应缺少 `replayed`，密钥没有显示。已在 handler 返回里补上。
  - 对重试的断言写得太弱。改成 409 并重新核对。
  - handler 在设置 `eios.tenant_id` 之前读了负责人角色，直接调用时被拒。已提前设置。
  - `test_workbench_roles` 的版本与计数需要随清单更新。
- `test_bootstrap`：把编号临时改成 0430、0431 后 4 passed，跑完已恢复（0434/0435 是空号后的临时号）。
- `check_definer_search_path.py` 0 problems。无 xfail 或 skip。

## 变异检查

每次只改迁移中的一处，跑 `tests/test_machine_lifecycle_pg.py`，跑完恢复：

| 变异 | 结果 |
|---|---|
| 去掉负责人在任检查 | 1 failed |
| 去掉 owner world 检查 | 1 failed |
| 去掉 rotate_required 预检 | 1 failed |
| 去掉 scope 预检 | 1 failed |
| 去掉 key_limit 预检 | 1 failed |
| 签发不写审计 | 1 failed |
| 轮换时去掉主体停用检查 | 1 failed |
| 去掉 client_exists 检查 | 1 failed |
| key world 允许 sim | 1 failed（补了直接调用 handler 的用例后） |
| 检查函数中不设置 tenant | 1 failed |
| 去掉“机器主体前缀”判断 | **存活**：负责人在任检查已经拒绝机器主体，两者冗余，有意保留双重检查 |
