# NX-062 M2a：机器主体与密钥存储、身份函数、realm 互斥、能力允许表

- **依据**：`docs/implementation/NX-062-design.md` §1、§2.2、§3.2、§6 M2，§10 中的 L6 条件 C1、C2、C3。执行：L10。基线 038dba21，接在 M1（d826b53c）之后。
- **迁移**：`0434_nx062_machine_access.sql`（临时号，调度员 2026-11-09 指定从 0434 起）。只新增对象；对现有对象的唯一改动，是把 `authz.nexloop_service_identity_snapshot` 改名一次并包装（`..._before_machine_v0434`，收回所有 grant）。没有复制已有函数体，因此不触发 copy guard。
- **M2 拆成两步**：本片 M2a 只做存储、身份和授权边界。M2b 再做两件事：一是把生命周期函数登记为受治理 Action（负责人执行，Q-4）；二是给机器主体写 EIOS 授权事实。M2b 的做法沿用工作台角色模板（0335 `nexloop_workbench_role_templates` + `nexloop_workbench_fact_instance`）：每个能力的事实模板由可信配置预先编译，SQL 只按主体做实例化，API 不能自己构造事实。这样拆，是因为事实的生成方式需要单独审查。M2a 不开放任何路由，生命周期函数不授权给任何角色，所以本片没有可用的入口。

## 对象

| 对象 | 内容 |
|---|---|
| `authz.nexloop_machine_clients` | 企业程序（EIOS `SERVICE` 主体），principal/subject 由 client_id 确定性推出；status 为 active 或 inactive；身份列不可改；不可删除 |
| `authz.nexloop_machine_keys` | 只存 HMAC 摘要（小写 hex）与 pepper 版本；`prefix_id` 唯一；一把密钥对应一个 world；scope 必须是允许表中的当前能力；有效期不超过签发后 90 天，只能缩短；每个主体最多 2 把有效密钥，第二把只能在第一把 7 天内到期时签发；吊销是终态；不可删除 |
| `authz.nexloop_machine_key_pepper_version(prefix)`、`authz.nexloop_machine_identity_snapshot(prefix, digest)` | 只授权给 `nexloop_api`。按 C2，先按 prefix 取得行的 pepper 版本，API 只算一次 HMAC，再交给快照函数；所有有效性判断都读 `clock_timestamp()` |
| `authz.nexloop_service_identity_snapshot`（包装） | 按 C1，机器摘要、机器主体一律拒绝（`authentication kind conflict`） |
| 写入侧触发器 | 按 C1：service credentials、browser token realms、browser memberships 不能写入机器的摘要或主体；机器主体与机器密钥也不能占用这三类已有的主体或摘要 |
| `control.nexloop_machine_read_verbs`、`control.nexloop_machine_capabilities` | 按 C3：只能追加；通过追加 revision 行（active/retired）改变状态，取最新一行；白名单为“已登记的读取动词”或“subject_rule 不是 human 的 governed capability”；永不授予黑名单为 `control.nexloop_machine_never_grant_markers()`，与 `nexloop_eios.machine_access.NEVER_GRANT_MARKERS` 锁定一致，并覆盖 `service_grants.FORBIDDEN_MARKERS`；来源只能写 `migration:` 或 `action:`；任何应用角色都没有权限 |
| 生命周期函数 | register、set_status（停用时级联吊销）、issue、rotate（重叠不超过 7 天）、revoke：只属 owner，不授权给任何角色（M2b 才接入统一入口） |

## 擦除覆盖

- `authz.nexloop_machine_clients` 和 `authz.nexloop_machine_keys` 加入了成员擦除覆盖的 `STAFF_KEPT`：`issued_by_principal` 记录的是登记或签发的负责人，与其他员工操作一样保留作审计。
- `authz.nexloop_machine_clients` 加入了消费者擦除覆盖的 `IDENTITY_KEPT`：它的 principal 是企业程序，与消费者无关。
- 两张表都没有 consumer_id，也不存任何正文。

## 测试（真实 PG）

`tests/test_machine_access_pg.py`，15 个用例：

- 签发后全库查不到明文；身份快照的形状正确。
- C2：按 prefix 查找，只认该行的 pepper 版本；错误的 prefix 或 secret 返回同一条拒绝信息。
- 过期判断：同一个事务里，`clock_timestamp()` 越过到期时间后，下一次调用即被拒。
- 吊销：在另一条连接上立即生效，且不可逆。
- 密钥上限 2 把、重叠不超过 7 天、有效期不超过 90 天、到期时间不能延长。
- 停用级联吊销，重新启用不会让旧密钥复活；主体停用的那一层单独也会拒绝。
- world 绑定与不可变列。
- C1 读取侧：service 快照拒绝机器摘要；改名后的内层函数不再有 grant。
- C1 写入侧：service、browser realm、membership 三处都拒绝。
- C1 交叉测试：在真实 registered stage 上（`test_stage_register_pg.registered`），service token 走机器快照被拒；nxm 密钥走 `authenticate_service`、HMAC 摘要走 service 快照都被拒；用伪造的跨 realm 行（绕过触发器）时，两边的读取侧同样拒绝。
- C3：revision 只能追加、取最新一行；白名单和黑名单；名单与 Python 锁定；任何应用角色都没有表或函数权限；`nexloop_api` 能调用快照，但不能读表。

真实 HTTP：本片没有路由。最接近的检查是交叉测试中用真实连接池走 `authenticate_service`。HTTP 层从 M3 开始。

## 变异检查

每次只改迁移中的一处（同时更新 catalog 校验和），跑 `tests/test_machine_access_pg.py`，跑完恢复。16 项全部变红：

| 变异 | 结果 |
|---|---|
| 包装函数去掉机器摘要判断 | 2 failed |
| 包装函数去掉机器主体判断 | 1 failed |
| 机器快照去掉 C1 交叉判断 | 1 failed |
| 去掉 status=active 判断 | 2 failed |
| 去掉主体 active 判断 | 1 failed |
| `clock_timestamp()` 改为 `now()` | 1 failed |
| 去掉摘要比较 | 1 failed |
| 去掉 2 把上限 | 1 failed |
| 去掉 7 天重叠限制 | 1 failed |
| 允许表去掉黑名单判断 | 1 failed |
| 允许 human 能力 | 1 failed |
| 去掉 revision 连续性判断 | 1 failed |
| 去掉 scope 必须在允许表中的判断 | 2 failed |
| 去掉 service credentials 写入侧触发器 | 1 failed |
| 允许延长到期时间 | 1 failed |
| 停用不级联吊销 | 1 failed |

## 其他检查

- `check_definer_search_path.py`：第一次 FAIL，包装函数的 search_path 缺 `pg_temp`；修正后 0 problems。
- 相关测试，`-n 2`：test_machine_access_pg、test_function_copy_guard、test_stage_register_pg、test_service_credential_rotation_pg、test_credential_scope_rotation_pg、test_browser_session_creation、test_browser_identity_reads、test_authority_fact_cache、test_run_credentials 共 176 passed。擦除覆盖三项（member、consumer、whole database）全部 passed。
- 一处首次失败：whole database 测试报 `IdentityUnavailable`，原因是排除触发器在 browser token realms 上读了 `new.binding`（plpgsql 会规划整条 IF 表达式），已改为嵌套 IF 并补了用例。
- `tests/test_bootstrap.py`：用临时号 0434 时有 2 项失败，因为 catalog 版本必须连续、长度必须等于 `bootstrap_revision`，而 0430–0433 是空号。把编号临时改成 0430 再跑，`test_bootstrap` 等 40 项全部 passed，跑完已恢复。组批重新编号后这两项会通过。
- 无 xfail 或 skip。
