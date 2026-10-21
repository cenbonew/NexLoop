# NX-041 D10：每个 sim world 三个凭据，sim Run 端到端

**依据**：负责人定案 D10「按推荐」：每个 sim 或 shadow world 固定 3 个服务凭据，分别给 api、worker、agent 三种用途，每个只绑定这一个 world；删除 world 时三个一起吊销。

**迁移**：`0273_nx041_d10_credentials.sql`，临时编号。0260 已经随第七批合进集成头，所以没有原地改它，新开迁移更安全；它和 L2 新开的数量上限迁移只会在 catalog 上冲突。

## 凭据规则

| 项 | 实现 |
| --- | --- |
| 用途列 | 新增 `authz.nexloop_service_credentials.sim_use`，取值 api、worker、agent。CHECK 规定：sim 或 shadow 凭据必须填用途，real 和 test 凭据不能填。已有的 sim 凭据当时都是该 world 唯一的凭据，回填为 api。 |
| 唯一性 | 部分唯一索引 `nexloop_service_credentials_one_active_sim_use`，作用于 `(worlds[1], sim_use) where status='active'`，并发插入时由它保证唯一。触发器给出可读的拒绝原因：「每个 world 每种用途只有一个 active 凭据」；缺用途时报「names its use」。 |
| 其余规则不变 | worlds 不可改，用途同样不可改；吊销后不能重新激活；主体不与 real 或 test 共享；world 必须已登记且处于 active。删除 world 时吊销它的全部 active 凭据，S2 的逻辑本来就是这样，现在一次吊销三个。 |
| 复制件守护 | `authz.nexloop_sim_world_credential_check`（S2 的函数体，换成按用途的规则）和 `authz.nexloop_issue_run_credential`（0034 的函数体，加一句 `app_role` 判断）都登记在 `tests/test_function_copy_guard.py` 中。 |

## sim Run 端到端（AT-046 完整验收）

接线顺序按 S7b 和 sim-context-ref 的文档，每一步都用 sim 组自己的角色和该 world 自己的凭据：

1. `nexloop_sim_api` 用 api 凭据签发 Run 凭据。Python 侧签发时的角色检查，现在允许 sim world 里的 sim_api；SQL 侧用 `app_role` 判断，sim 角色只能在自己的 world 里签发。
2. `sim_context_register` 按引用固定 Run 的 input，同时写 manifest。
3. 用 `pack_text` 原样 accept 并登记 Run。自拼的包会被 `input_not_registered` 拒绝：测试从错误链里取出数据库的原始错误来断言，避免被权限错误误判通过。
4. `nexloop_sim_worker` 用 worker 凭据认领任务、创建激活。
5. sim guard 用 `nexloop_sim_guard` 角色、worker 凭据、`--world-group simulation`，起两个进程；sim Host 配置为 `world_group simulation`、sim 上下文协议、deterministic-test 档位、CNY，用 Node 24 跑完一个真实的 Pi Run。

测试 `tests/test_nx041_sim_pi_run_pg.py::test_at046_a_simulation_process_group_runs_a_complete_pi_run` 断言以下几点：
- Run 的结果为 succeeded，运行状态存放在 sim world 自己的目录下；
- 运行文件里不出现任何凭据或密钥；
- 该租户的服务凭据只属于这个 sim world；
- real 的效应表、外发表、Claim 表和消息表行数都不变。

S7 原来那条 strict xfail 已经翻转，改成普通测试：先断言 `pi_run_blockers` 返回为空，再指向上面这条端到端测试。另有一条探测的阳性对照：打桩一个阻塞项，仍然会抛出。

### 为端到端补的授权（都写在 0273，S6c 白名单同步更新）

| 角色 | 新增 | 原因 |
| --- | --- | --- |
| 三个 sim 角色 | `usage on schema control`，`select on control.schema_migrations` | 打开后端时要核对迁移版本。real 角色在 0002 就有这项授权；这张表只有版本号和校验和，没有业务数据，是 S6c 不能读写任何表这条规则下唯一的例外。 |
| `nexloop_sim_api` | `issue_run_credential`、`queue_command`、`runtime_activation_command`、`read_action_bundle`、`role_run_hint` | 签发 Run 凭据，accept 和 register，以及其中读取 Run Action 定义和 Role Run 提示的步骤。 |
| `nexloop_sim_worker` | `read_action_bundle`、`role_run_hint` | 创建激活时读取 Run 的 Action 定义和 Role Run 提示。 |
| `nexloop_sim_guard` | `read_action_bundle`、`role_run_hint` | guard 授权 Run 时需要。 |

这些函数都按凭据或 Run 摘要以及 world 取数，函数体里没有角色限制；sim 凭据只对自己的 world 有效。

### track_functions 实测（端到端跑一次，统计 `pg_stat_user_functions`）

实际调用了 64 个函数：
  - `authz.nexloop_app_role`
  - `authz.nexloop_assert_action_authority`
  - `authz.nexloop_assert_action_authority_before_action_memo_v0106`
  - `authz.nexloop_assert_agent_parents`
  - `authz.nexloop_fact_coverage`
  - `authz.nexloop_is_fallback_run`
  - `authz.nexloop_is_message_run`
  - `authz.nexloop_issue_run_credential`
  - `authz.nexloop_load_authority_fact`
  - `authz.nexloop_load_authority_fact_snapshot`
  - `authz.nexloop_load_authority_fact_snapshot_with_identity`
  - `authz.nexloop_load_authority_fact_with_identity`
  - `authz.nexloop_load_authority_facts`
  - `authz.nexloop_lock_credential`
  - `authz.nexloop_nonbrowser_assert_action_authority_v0044`
  - `authz.nexloop_nonbrowser_identity_snapshot_v0044`
  - `authz.nexloop_queue_command`
  - `authz.nexloop_queue_command_before_refusals_v0150`
  - `authz.nexloop_read_action_bundle`
  - `authz.nexloop_read_action_definition`
  - `authz.nexloop_read_memo_advisory_locks`
  - `authz.nexloop_read_memo_invalidate`
  - `authz.nexloop_read_memo_key`
  - `authz.nexloop_read_memo_ready`
  - `authz.nexloop_read_memo_table_ok`
  - `authz.nexloop_role_policy_claims_current`
  - `authz.nexloop_role_policy_claims_tail`
  - `authz.nexloop_role_run_hint`
  - `authz.nexloop_role_ttl_claims`
  - `authz.nexloop_role_ttl_proof`
  - `authz.nexloop_root_identity`
  - `authz.nexloop_root_identity_snapshot`
  - `authz.nexloop_runtime_activation_command`
  - `authz.nexloop_runtime_activation_command_before_formal_current_v0065`
  - `authz.nexloop_runtime_activation_command_before_inspect_scope_v0360`
  - `authz.nexloop_runtime_activation_command_before_model_requests_v0090`
  - `authz.nexloop_runtime_activation_command_before_role_policy_v0081`
  - `authz.nexloop_runtime_activation_command_before_role_ttl_v0063`
  - `authz.nexloop_runtime_activation_command_before_roles_v0062`
  - `authz.nexloop_runtime_activation_command_v0037`
  - `authz.nexloop_runtime_activation_command_v0038`
  - `authz.nexloop_runtime_activation_command_v0051`
  - `authz.nexloop_runtime_activation_command_v0052`
  - `authz.nexloop_runtime_activation_hint`
  - `authz.nexloop_service_identity`
  - `authz.nexloop_service_identity_snapshot`
  - `authz.nexloop_sim_context_command`
  - `authz.nexloop_sim_context_register`
  - `authz.nexloop_sim_effect_tenant`
  - `authz.nexloop_sim_run_input_fixed`
  - `authz.nexloop_verify_backend_signer`
  - `control.nexloop_sim_world_status`
  - `runtime.nexloop_budget_on_job_terminal`
  - `runtime.nexloop_commercial_ts`
  - `runtime.nexloop_consumer_persona`
  - `runtime.nexloop_nx022_capture_task_controls`
  - `runtime.nexloop_plan_on_job_failed`
  - `runtime.nexloop_sim_clock_at`
  - `runtime.nexloop_sim_consumer_known`
  - `runtime.nexloop_sim_render_version`
  - `runtime.nexloop_world_clock`
  - `runtime.nexloop_world_kind`
  - `runtime.nexloop_world_now`
  - `runtime.nexloop_world_valid`

- 白名单入口中**被调用的**：`authz.nexloop_issue_run_credential`, `authz.nexloop_load_authority_fact_snapshot`, `authz.nexloop_load_authority_facts`, `authz.nexloop_queue_command`, `authz.nexloop_read_action_bundle`, `authz.nexloop_role_run_hint`, `authz.nexloop_runtime_activation_command`, `authz.nexloop_runtime_activation_hint`, `authz.nexloop_service_identity_snapshot`, `authz.nexloop_sim_context_register`, `authz.nexloop_verify_backend_signer`。
- 白名单入口中**没有被调用的**：`authz.nexloop_record_process_sample`, `authz.nexloop_record_run_tool_timing`, `authz.nexloop_run_tool_timing`, `authz.nexloop_sim_effect_query`, `authz.nexloop_sim_effect_submit`, `runtime.nexloop_erasure_permits`。它们都属于这次没有走到的路径：工具调用（`run_tool_timing` / `record_run_tool_timing`，这个 Run 没有开 effect_tools）、替身（`sim_effect_submit` / `sim_effect_query`）、进程采样（`record_process_sample`），以及对 public 开放的 `erasure_permits`。
- 没有出现「被调用但不在白名单」的入口：调用清单里除了上面这些白名单入口，其余都是由它们以 owner 身份间接调用的内部函数。

## 限制

- 端到端用的是 deterministic-test 档位、不开 effect_tools 的 Run（调用一次模型就结束）。sim 组带工具的 Run，在已有 Host 测试里由替身覆盖（`nx041-sim-host-tools`），这里没有把真实 Pi 加工具跑一遍。
- agent 用途的凭据已经签发，但这次端到端没有用到：Run 凭据由 api 凭据签发，worker 和 guard 用 worker 凭据。agent 凭据留给后续由 Agent 侧直接持有的场景。
- 受信配置 manifest 的用途字段已在下一节补上。

## 受信配置 manifest 签发 sim 凭据（部署前必须补的一项）

迁移：`0274_nx041_d10_manifest.sql`，临时编号。

| 项 | 实现 |
| --- | --- |
| Python 校验（`trusted_configuration.validate_manifest`） | 凭据条目允许可选键 `sim_use`。只绑定一个 sim 或 shadow world 的凭据，必须带 `sim_use`，取值 api、worker、agent；real 和 test 凭据带了这个键就拒绝。 |
| SQL（`control.nexloop_configure_manifest`，以 0211 的最新函数体为底稿） | 同一条规则在 SQL 侧再校验一遍，绕过 Python 直接调用也会被拒；插入改成显式列名并写入 `sim_use`。原来是按位置插入 8 个值，而 `expires_at` 和 `sim_use` 之间还有一列 `agent_invocation`，按位置写入第 9 个值会落错列。重放同一份 manifest 时，冲突检查也会比对 `sim_use`。这次重定义已登记到复制件守护。 |
| 契约 / deploy | manifest 没有对应的 JSON 契约（`packages/contracts` 下没有），`deploy/` 下也没有 manifest 示例，所以都没有改动。`service_grants` 只生成 real 和 test 的 manifest，不涉及 sim 凭据。 |

测试 `tests/test_nx041_d10_manifest_pg.py`：

- 一份 manifest 同时签发 api、worker、agent 三种用途的凭据：全部成功，`sim_use` 都写入了，各自能在本 world 认证；重放不改变任何东西。
- 再签发一个已经存在的用途：被拒。
- 缺少用途、用途非法、real 凭据带了用途、test 凭据带了用途：Python 校验和 SQL 都拒绝。阳性对照：带了合法用途的 sim 凭据，两条路径都能通过。
- real 回归：service-grants 的真实 manifest 在前后各应用一次，结果不变；real 凭据都不带用途。

## 授权复核（L2 `nx041-d10-grants-review.md`）

| 项 | 处理 |
| --- | --- |
| **P1 第二道防线** | 迁移 `0275_nx041_d10_grants_review.sql` 重定义了 `authz.nexloop_read_action_bundle`（底稿是 0016）和 `authz.nexloop_role_run_hint`（底稿是 0063），开头各加一句判断：`authz.nexloop_app_role(p_world)='nexloop_sim_outside_its_world'` 时拒绝，也就是 sim 角色以 real 或 test world 调用会被挡住。real 角色的 `app_role` 就是它自己的 session_user，所以 real 路径逐字不变。两个函数的 search_path 都补上了 `pg_temp`，满足 definer 检查。两处重定义都已登记到复制件守护。负例：三个 sim 角色拿着 real 凭据的摘要，以 real 或 test 调用这两个函数，都报 `simulation role outside its world`；阳性对照：nexloop_api 做同样的调用能越过这道判断。 |
| **P2 逐项必要性** | 用一次性探测逐项收回授权，每次串行重跑真实 Pi 的 sim 端到端（Node 24），共 13 项，**收回任何一项都会导致失败**，失败点也正好在用到这项授权的那一步。所以没有多余的授权，白名单不变。各项的失败情况见下表。 |
| **P3 invoker 函数** | 选「断言」，没有统一 `revoke ... from public`。实测三个 sim 角色和 nexloop_api 在 authz、control、runtime、ontology 下可调用的 invoker 函数都是 0 个；而且 bootstrap 角色现在新建的函数，默认就不对 PUBLIC 开放执行。统一 revoke 要改一大批现有函数，还会波及 real 角色，换不来实际收益。断言能拦住将来的回归：测试要求每个 sim 角色在这些 schema 下能调用的 invoker 函数为空集。阳性对照：临时建一个函数并 `grant ... to public`，断言能发现它。 |

P2 实测，每次单独收回一项授权：

| 收回的授权 | 失败点 |
| --- | --- |
| sim_api：`read_action_bundle` | 签发 Run 凭据：`published Run Action unavailable` |
| sim_api：`role_run_hint` | accept：没有执行权 |
| sim_api：`select control.schema_migrations` | 打开后端：没有表权限 |
| sim_api：`issue_run_credential` | 签发 Run 凭据：没有执行权 |
| sim_api：`queue_command` | accept：没有执行权 |
| sim_api：`runtime_activation_command` | accept 的登记步骤：没有执行权 |
| sim_worker：`read_action_bundle` / `role_run_hint` | 创建激活：`runtime activation unavailable` |
| sim_worker：`select control.schema_migrations` / `usage on schema control` | 打开后端：没有权限 |
| sim_guard：`read_action_bundle` / `role_run_hint` | Host 启动 Run：guard 返回 `runtime_authorization_unavailable` |
| sim_guard：`select control.schema_migrations` | guard 进程起不来：`guard worker unavailable` |

## 负责人 2026-10-21 扩展 D10：新增 twin 用途（NX-042 T3）

- **第 4 种用途 `twin`**：每个 sim world 新增一种凭据用途，供消费者孪生的端口使用。它只绑定这一个 world，只能调用孪生端口的三个入口：`authz.nexloop_twin_bind`、`_observe`、`_act`。
- **迁移 `0284_nx042_twin_credential.sql`**（临时编号）：
  - `sim_use` 的 CHECK 改为 `nexloop_service_credentials_sim_use_kind`，接受 api、worker、agent、twin 四种；
  - 部分唯一索引不变，仍是每个 world、每种用途只能有一个 active；
  - `authz.nexloop_sim_world_credential_check` 只改了报错文字，列出四种用途；
  - 受信配置 manifest 的 `control.nexloop_configure_manifest`（0274 的函数体）接受 `twin`；
  - 这两处重定义都已在复制件守护里登记；
  - Python 侧 `trusted_configuration` 同步接受 `twin`。
- **端口入口**：通过 `authz.nexloop_twin_port_tenant` 只接受本 world 的 twin 凭据；api、worker、agent 凭据一律报 `twin port: credential_use`。
- **删除 world**：S2 的删除会吊销该 world 的全部凭据，所以四种一起吊销，删除测试的计数从 3 改为 4。
- **测试**：manifest 测试覆盖四种用途；T3 测试补了 api、worker、agent 凭据被拒的负例，以及“每个 world 只能有一个 active 的 twin 凭据”；T7 的端到端测试改用 twin 凭据认证端口。
