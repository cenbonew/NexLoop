# NX-032 合成租户组装本地演练：缺口清单（2026-10-15）

这次演练在本地干净 PG 上，用生产工具和 stage 自己的清单，从 bootstrap 一直走到每个 worker 的首轮调用。

- **测试**：`tests/test_assembly_dryrun_pg.py`，路径和测试专用部分写在 `tests/assembly_dryrun_fixture.py`。
- **配方**：都来自 `deploy/examples/nx037/`，包括 business、catalog、enterprise-setup、relay-enrollment-enterprise 和 identity-human 的示例。占位符换成合成值，租户用 `tests/support/tenants.py`，没有真实名称。
- **证据**：本地测试，不是 stage 结果。

## 1. 已打通的步骤

| 步骤 | 工具 | 结果 |
|---|---|---|
| bootstrap、phase A 树、setup 凭据 | `stage_secrets.py generate`、`setup-credentials` | 通过 |
| 登记服务主体 | `stage_secrets.py register --bootstrap-dsn-file` | 通过，23 个主体 |
| 浏览器登录链（G1） | `stage_secrets.py browser` | 通过 |
| 客户的 Human | `nexloop_eios.initial_identity --create` | 通过 |
| 发布 | 测试辅助 `publish_catalog`，替代 T3：0050 清单经配置者窗口 apply | 通过 |
| business setup | `business_setup`：owner 为 `business_owner`，executor 为 `effect_executor` | 通过 |
| catalog setup | `catalog_setup`：maintainer 为 `catalog_maintainer`，Source 为 `relay_source` | 通过 |
| enterprise setup | `enterprise_setup` | 通过 |
| relay 登记 | `relay_enrollment`，owner 凭据，带企业槽位 | 通过 |
| worker 配置文件 | `tenant_config.py` 的 `recall-types` 与 `relay-run-profile`，加上仓库里的其他配置文件 | 通过 |

worker 首轮：每个服务都按 `deploy/stage/compose.app.example.yaml` 中的命令运行一次（`--once`）。

- **首轮完成，退出码 0**：message-relay、action-worker、webchat-delivery（端口可连）、outbound-recorder、claim-extraction-scheduler、recall-indexer（已为 Consumer 建索引）、plan-reevaluator、reply-guarantor、commitment-keeper、commercial-recorder、retention-keeper、alert-evaluator、incentive-issuer、domain-worker。
- 例外见第 2 节。

## 2. 缺口

| ID | 缺口 | 证据（测试） | 影响 | 建议 |
|---|---|---|---|---|
| D-QUEUE（已修，compose 改为 `--queue operations`，调度员 2026-10-15） | Compose 中 runtime-worker 用 `--queue runtime`，但 `runtime_worker` 只被授予 `NexLoop.queue.operations:1`。relay 与 Role queue 也都把 Run 放进 `operations` | `test_each_worker_first_round`（当前退出码 1，`dispatch unavailable`）；`test_runtime_worker_first_round_on_the_granted_queue`（改成 `operations` 后首轮完成） | stage 上 runtime-worker 每轮都认领失败，消息 Run 永远不会派发 | 改 `compose.app.example.yaml` 为 `--queue operations`，属部署配置改动，由调度员决定 |
| G-WEBCHAT-AUTH（已补，分支 `l4/g-webchat-auth`） | webchat 客户没有业务授权：0045 浏览器业务授权快照需要 `control.nexloop_browser_business_applications` 行、该行调用方应用的 `application` 事实，以及该 Human 的 grant 事实。原来 stage 工具都不登记这些 | `test_the_webchat_consumer_opens_a_conversation_and_writes_after_the_consumer_role_is_applied`：应用前没有业务应用行；经 `scripts/deploy/webchat_consumers.py --apply --bootstrap-dsn-file` 登记后（配置者窗口，0050），客户经 stage API（compose `api-1`）的 `/api/v1` 登录、开会话、发消息（202 accepted），relay 首轮认领该消息并建出 MessageAssignment | 已解除 | 工具 `nexloop_eios.webchat_consumers`：公开角色 `deploy/authorization/webchat-consumer-role.v1.json`（只允许客户资源：建会话、写消息、读会话、回执与范围拒绝两个 Function），私有配方见 `deploy/examples/nx037/webchat-consumers.example.json`（persona 只允许 `simulated_agent`）。每个 Human 的事实与 `workbench_roles` 共用 `human_facts` |
| G-CONTEXT-STRATEGY（已补，分支 `l4/g-context-strategy`） | relay 的 `--context-strategy recent_plus_required` 要求租户有已发布的策略（`control.nexloop_context_strategies`）；原来组装步骤里没有任何一步发布它，而发布 Action `nexloop.context.strategy.publish` 在 SQL 中只允许人类（0088） | `test_the_owner_publishes_the_relay_strategy_and_the_relay_issues_the_run`：owner 经 `nexloop_eios.context_strategy_setup`（私有用户名、密码文件登录 workbench，固定 request_id，再执行为 replay）发布后，relay 首轮输出 `queued`，消息的 Run 已签发并进入运行时；`test_the_webchat_consumer_opens_...` 保留未发布策略时的行为（退出码 1，策略表为 0） | 已解除 | workbench roles v11 的 owner 角色带上该 Action（owner 专属）；配方示例 `deploy/examples/nx037/context-strategy-setup.example.json` |
| G-WORKBENCH-AUTH（已补，分支 `l4/g-context-strategy`） | workbench 应用没有 0045 业务应用行，成员授权事实也没有生产写入路径（`workbench_roles` 只写 `configure_workbench` 和打印事实），owner 在 stage 上执行不了任何 workbench Action | 同上测试：`scripts/deploy/workbench_roles.py --apply --bootstrap-dsn-file` 两次（同一状态），之后 owner 会话执行 `Budget.set`（real_tenant_init 第 4 步）成功，`nexloop_nx037_tenant_state` 中模型预算为 100 | 已解除 | `workbench_roles --apply`：先写调用方应用事实与业务应用行（`configure_workbench` 要求已发布的业务应用），再写成员，最后写成员授权（0050）。成员文件格式见 `deploy/examples/nx037/workbench-members.example.json` |
| G-ROLE（已解决，l3/g-role） | 原缺口：没有主体持有 `RoleDefinition.create`，也没有工具创建角色 | `test_the_assembled_role_is_what_a_role_run_binds`；`test_stage_role_scope_pg.py` | — | 已补：服务主体可声明 `requested_scopes`（只允许 `ontology.roles.manage`）；新增 stage 主体 `role_assembler` 与工具 `nexloop_eios.role_assembly`（配方 `deploy/examples/nx037/role-assembly.example.json`）。工具创建常驻 Goal/PlanStep、ceiling、RoleDefinition、assignment scope 和 ConsumerRoleLink；owner 随后带 role_id/link_id/step_id 重新登记 relay；`tenant_config` 再从已发布的 Role 生成 run profile |
| G-PLAN（已补，分支 `claude/l6-g-plan-4edba0`） | 复评计划没有生产入口：`PlanReevaluationPort.establish`（0106 `verb establish`）只有测试调用 | `tests/test_plan_setup_stage_pg.py::test_plan_setup_establishes_as_the_plan_reevaluator_and_replays` | 已解除 | 工具 `nexloop_eios.plan_setup`：以 plan_reevaluator 自己的凭据和库角色（`nexloop_domain_worker`）调用 establish；私有配方 `deploy/examples/nx037/plan-setup.example.json`（角色组装、目录、business 的 ID，NX-022 目标版本，Context 策略，步骤）；计划设置取自 reevaluator 挂载的 `plan-reevaluation.v2.json`；plan_id 为稳定意图，重放 `replay: true`。目标版本由 owner 经 `nexloop_eios.goal_setup` 发布（G-GOAL） |
| G-ROLE-READ（已补，同一分支，迁移 0205 临时编号） | Role Run 发起时 role_source 逐对象 READ 被拒（`plan_reevaluation.py:211`，复现见下一列）。按 register 清单逐对象授予会改变 role_source 凭据的 requested_scopes（`credential_rotation_required`，`service_grants.py:561/627`） | `test_role_source_reads_only_the_objects_of_an_active_plan`（计划前全拒；计划建立后 10 类对象可读；非计划对象、超出字段表、其他 Source、计划失效后都拒；无逐对象事实、无新 scope）；`test_the_derivation_is_scoped_to_the_tenant_the_rule_and_the_step_submitter`；`test_the_plan_reevaluator_first_round_issues_the_role_run`（首轮 `launched: 1`） | 已解除 | 调度员 2026-10-17 方案 A：0205 `role_source_read_rule`（stage-principals v4 新段，`stage_secrets register` 经 0050 编译），SQL 每次读取时派生 READ，只覆盖当前 active 计划 recipe 所指且 PlanStep.submitter_principals 含该 Source 的对象，字段表固定 |
| G-ROLE-CATALOG（已补，无代码改动） | Role Context 读取目录 binding 时要求 `source_principal` 等于 Run 的 Source（`service_offerings.read_catalog`，第 112 行）；stage 的目录 binding 指向 relay_source | 同上首轮测试（补之前 activate 报 `offering_unavailable`） | Role Run 的 Context 无法绑定 | 用现有 `catalog_setup` 再建一份目录：maintainer 不变，`--source-credential-file` 换成 role_source 的凭据；计划 recipe 用这份的 offering/binding |
| G-GOAL（已补，同一分支） | stage 上没有发布 NX-022 目标的入口（workbench 写入口没有 publish_goal / approve_metric） | `publish_goal`（同一测试文件，两次执行，第二次重放） | 计划无目标版本可引用 | 工具 `nexloop_eios.goal_setup`：owner 以自己的 workbench 会话执行 Metric.approve 与 Goal.publish（形态同 context_strategy_setup）；配方 `deploy/examples/nx037/goal-setup.example.json` |
| G-ROUTES | `action-worker/provider-routes` 与 `webchat-provider-config` 是 manual 文件，没有生成器 | 演练按 `NX-037-provider-routing.md` §2 自行写出（`manual_files`），只配 webchat 路由 | stage 需要手写；写错时 action-worker 启动失败 | `stage_secrets` 增加生成，或在 run sheet 中附模板。enterprise 路由属阶段 C |
| G5 | claim-extraction-worker 与 claim-matcher 需要 `model.env`（阶段 C） | 两者 `skip` | 不能启动 | 已知，阶段 C |
| T3 | `stage_secrets.py publish` 还未合入；演练用测试辅助代替 | `compile_catalog`：T3 合入后自动改用 `catalog_publish.compile_catalog`，在此之前用 system_catalog 加业务清单 | 合入后需重跑本演练，核对清单方式与 system_catalog 是否一致 | T3 合入后重跑 |

## 3. 没有覆盖到的部分

- **消息还没有走完整条链**：G-WEBCHAT-AUTH 补齐后，客户消息已经由 relay 首轮认领（见上表）；owner 发布策略后（G-CONTEXT-STRATEGY 已补），relay 首轮签发 Run 并交给运行时（`queued`）。runtime → Agent Host → 回复投递仍未覆盖，Agent Host 与模型 key 属阶段 C。
- 没有启动 agent-host 和 gateway。API 进程（compose `api-1`，执行档 disabled）只在 G-WEBCHAT-AUTH 的测试中启动，用于客户的 `/api/v1` 请求。
- workbench 只覆盖 owner（`workbench_roles --apply`、`Budget.set`、策略发布）；operator、reviewer 两个角色不在本演练中。
