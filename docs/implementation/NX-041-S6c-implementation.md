# NX-041 S6c：sim 进程组的专用数据库角色

依据：D3、L6 `nx041-s6-review` 1.2、方案 `build/nx041-s6c-plan.md` 的方案 b（调度员 2026-10-21 决定）。起点 `int/nx041-s1s5` 6a731e20。迁移 `0268_nx041_sim_roles.sql`（编号是临时的，只出现在文件名和 catalog 里）。

## 实现

| 内容 | 位置 |
| --- | --- |
| 三个 login 角色：`nexloop_sim_worker`（runtime worker 的调度进程）、`nexloop_sim_guard`（guard 子进程）、`nexloop_sim_api`（sim 世界的事件入队和 Run 凭据）。都没有特权，`noinherit`，**不是任何角色的成员**，因此不会继承任何 real 授权 | 0268 §1 |
| 授权白名单（初始清单）：<br>· 三个角色共有：`service_identity_snapshot`、`load_authority_facts`、`load_authority_fact_snapshot`、`verify_backend_signer`、`record_process_sample`。<br>· worker 另有：`queue_command`、`runtime_activation_command`、`runtime_activation_hint`、`run_tool_timing`。<br>· guard 另有：`runtime_activation_command`、`runtime_activation_hint`、`record_run_tool_timing`、`sim_effect_submit`、`sim_effect_query`。<br>· api 另有：`queue_command`、`runtime_activation_command`、`issue_run_credential`。<br>其余一律不授权。`tests/test_nx041_sim_roles_pg.py` 固定了每个角色能执行的 security definer 函数集合，只多出对 public 开放的 `erasure_permits`。以后任何新授权都会让这个测试失败，必须有意识地修改清单 | 0268 §2 |
| 角色判断：有 8 个函数按角色名检查调用方，包括两个对外入口所包装的内层版本（`queue_command_before_refusals_v0150`、`runtime_activation_command_v0037`），以及 `runtime_activation_hint`、`run_tool_timing`、`record_run_tool_timing`、`record_process_sample`、`sim_effect_submit`、`sim_effect_query`。这些检查改为读取 `authz.nexloop_app_role(p_world)`：<br>· sim 角色只在 sim/shadow 世界里被视为对应的 real 角色（sim_api 对应 api，sim_worker 和 sim_guard 对应 domain_worker）；没有 world 参数的进程样本也按此对应；<br>· 在 real、test 或其他 world 里，sim 角色不被视为任何应用角色，检查一律拒绝；<br>· real 角色的结果就是 `session_user`，行为与原来完全相同。<br>函数体原样复制，只替换了这几处判断 | 0268 §3 |
| 进程：<br>· `APPLICATION_ROLES` 加入三个 sim 角色；<br>· runtime worker 和 guard 按 world 组检查自己的数据库角色：real 组仍是 domain_worker 或 scheduler；simulation 组的调度进程只能是 sim_worker，guard 只能是 sim_guard；<br>· simulation 组必须提供 `--guard-database-url-file`，且 `--guard-workers` 至少为 2，guard 在子进程里用自己的 DSN 运行，不与调度进程共用连接池；real 组不接受这个参数 | `assembly.py`、`runtime_worker.py`、`runtime_guard_worker.py` |
| sim compose 的 DSN 改为各进程自己的角色：`sim_worker_dsn`、`sim_guard_dsn`、`sim_stand_in_worker_dsn`、`sim_files_dsn`。sim 的 secret 目录里不再有任何 real 角色的口令。`nexloop_sim_api` 目前没有对应的服务，留给事件入队（S3 和孪生）使用 | compose |

## 部署步骤（只写说明，不改 deploy/ 下的 secret）

1. **口令**：迁移创建的角色可以登录，但没有口令。由负责人为 `nexloop_sim_worker`、`nexloop_sim_guard`、`nexloop_sim_api` 分别生成口令，存入 Keychain（`nexloop/sim/<角色>`），再执行 `alter role ... password`。然后在 `NEXLOOP_SIM_SECRET_DIR` 下写出 `sim_worker_dsn`、`sim_guard_dsn`（权限 0600）；api 的 DSN 等对应的服务上线时再写。
2. **pg_hba**（DATA_HOST，手工修改，或者等 stage 模板支持 sim 子网参数）：
   - 三个 sim 角色、`nexloop_stand_in_worker` 和 `nexloop_sim_files` 只允许从 APP_HOST 上 sim 子网（`NEXLOOP_SIM_SUBNET`，经主机出口地址）连接，使用 `hostssl nexloop_stage <这些角色> <APP_HOST 地址>/32 scram-sha-256`。
   - real 角色的规则不变。
   - 如果 sim 与 real 出口地址相同、无法按来源区分，就依靠两点：每个角色口令不同，以及 sim 的 secret 目录里只有 sim 口令。这一限制要写进部署单。
3. **校验**：sim 容器用 sim 角色连接后，执行 `select has_function_privilege(current_user,'authz.nexloop_real_dispatch_summary()','execute')`，应当返回 false。

## 测试

`tests/test_nx041_sim_roles_pg.py`：
- 三个角色都没有特权和成员关系，能执行的函数恰好等于白名单，不能直接读写任何表；
- sim worker 和 sim guard 在 real、test 世界里，角色检查直接拒绝；在 sim 世界里角色检查通过，之后由凭据检查拒绝伪造的 digest；
- 不在清单上的函数，以及 real 状态摘要和 readiness 函数，都没有执行权限；
- real 角色在各个 world 里行为不变；
- runtime worker 和 guard 的参数及角色规则；
- sim compose 只使用 sim 角色的 DSN。

## 限制

- 白名单是初始清单。D10（每个世界三个凭据）定了之后，要用 `track_functions` 跑一次 sim Run 端到端，实测校正。sim Run 的上下文来源由 L3 的 S7b 负责。
- 函数体是从迁移中整段复制的，涉及 0037、0133、0188、0209、0262。如果其他线在集成前也改了这些函数，以集成时的最新函数体为准，再套用同样的角色判断替换。

## L6 审查修复（`nx041-s6c-review.md` 中“可能”级的几项）

| 条目 | 处理 |
| --- | --- |
| 1.2 已存在的同名角色 | 迁移对三个角色做法与 S4 的 N2 相同：不管角色是不是迁移前就有，都执行一遍校正——设置属性（login、noinherit，其余特权全关）、撤销所有成员关系、撤销各 schema 中原有的表、序列、函数和 schema 权限，然后只授予白名单。新增测试：迁移前先建三个属性错误、带 `pg_read_all_data` 成员关系的同名角色，迁移后它们的形态和可执行函数集合都与白名单完全一致 |
| 1.3 `sim_api` 最小授权 | `nexloop_sim_api` 只保留共用入口（身份、授权事实、签名校验、进程样本）。队列、Run 激活和签发 Run 凭据都撤掉，等 sim 事件入队真正需要时再授予。S7b 的装配入口由 L4 在合入 Host 协议时加上 |
| 1.4 扫描范围 | 白名单测试的扫描范围加上 `public` schema |
| 1.5 进程样本 | sim 角色写的进程样本改写到单独的表 `runtime.nexloop_sim_process_samples`，带 `world_group='simulation'` 和写入角色，保留 7 天。real 的快照、小时汇总和告警只读 `runtime.nexloop_process_samples`，因此 sim 样本既不会混进 real 的统计，也不会掩盖 real 的告警。测试：sim 和 real 用同一个 instance 名各写一条，两边互不影响 |
| 2.3 NULL world | `authz.nexloop_app_role(p_world)` 在 `p_world` 为 NULL 时，sim 角色一律视为 world 之外，所有带 world 的角色检查都会拒绝。唯一不带 world 的进程样本改为调用单独的 `authz.nexloop_app_role_unscoped()`。测试覆盖 sim worker 和 sim guard 传 NULL world 时被拒 |
| 4.2 部署后检查 | `scripts/deploy/post_deploy_check.py` 新增 `check_sim_roles` 和 `--sim-secret-dir` 参数：sim secret 目录里的每个 `sim_*_dsn` 都必须以对应的 sim 角色登录（`current_user = session_user = 预期角色`），没有特权和成员关系，也没有 real dispatch 摘要的执行权；结果只给出文件名和角色名，不输出 DSN。对应关系是 `SIM_DSN_ROLES`：worker、guard、api、stand_in_worker、files。新增测试覆盖：错误的角色、未知的文件名、连不上、空目录 |
