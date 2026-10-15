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
| G-CONTEXT-STRATEGY | relay 的 `--context-strategy recent_plus_required` 要求租户有已发布的 ContextStrategy 对象；组装步骤里没有任何一步发布它（owner 的 `nexloop.context.strategy.publish`） | 同上测试：relay 首轮在 v6 上下文准备处退出码 1（`ContextArtifactUnavailable`，策略不可用），租户内 ContextStrategy 为 0 | 消息拿到了，但 Run 不能进入运行时；该消息退回待下一轮，次数用尽后交给回复保障 | 增加 owner 发布策略的组装步骤（经受治理 Action），或 stage 首次试运行先不传 `--context-strategy`（回到 v2 上下文），需要决策 |
| G-ROLE（已解决，l3/g-role） | 原缺口：没有主体持有 `RoleDefinition.create`，也没有工具创建角色 | `test_the_assembled_role_is_what_a_role_run_binds`；`test_stage_role_scope_pg.py` | — | 已补：服务主体可声明 `requested_scopes`（只允许 `ontology.roles.manage`）；新增 stage 主体 `role_assembler` 与工具 `nexloop_eios.role_assembly`（配方 `deploy/examples/nx037/role-assembly.example.json`）。工具创建常驻 Goal/PlanStep、ceiling、RoleDefinition、assignment scope 和 ConsumerRoleLink；owner 随后带 role_id/link_id/step_id 重新登记 relay；`tenant_config` 再从已发布的 Role 生成 run profile |
| G-PLAN | 复评计划没有生产入口：`PlanReevaluationPort.establish`（0106 `verb establish`，计划 recipe 含 role_id/link_id/step_id）只有测试调用 | 代码检索（`plan_reevaluation.py` 第 115 行，无生产调用方） | plan-reevaluator 没有计划，不会发起 Role Run（首轮为空闲） | 需要一个以 plan_reevaluator 主体建立计划的工具，输入为角色组装的输出加 NX-022 目标版本 |
| G-ROLE-READ（未确认） | Role Run 发起时 role_source 要逐对象读取 RoleDefinition、ConsumerRoleLink 等（`plan_reevaluation.RoleRunLauncher.issue`）；stage-principals 的决定说这些 READ 在“租户的角色组装（阶段 C）”中授予，目前没有工具授予 | 未测：本演练没有计划，不会走到发起 | G-PLAN 补上后，Role Run 可能在读取时被拒 | 与 G-PLAN 一起设计并验证 |
| G-ROUTES | `action-worker/provider-routes` 与 `webchat-provider-config` 是 manual 文件，没有生成器 | 演练按 `NX-037-provider-routing.md` §2 自行写出（`manual_files`），只配 webchat 路由 | stage 需要手写；写错时 action-worker 启动失败 | `stage_secrets` 增加生成，或在 run sheet 中附模板。enterprise 路由属阶段 C |
| G5 | claim-extraction-worker 与 claim-matcher 需要 `model.env`（阶段 C） | 两者 `skip` | 不能启动 | 已知，阶段 C |
| T3 | `stage_secrets.py publish` 还未合入；演练用测试辅助代替 | `compile_catalog`：T3 合入后自动改用 `catalog_publish.compile_catalog`，在此之前用 system_catalog 加业务清单 | 合入后需重跑本演练，核对清单方式与 system_catalog 是否一致 | T3 合入后重跑 |

## 3. 没有覆盖到的部分

- **消息还没有走完整条链**：G-WEBCHAT-AUTH 补齐后，客户消息已经由 relay 首轮认领（见上表）；之后停在 G-CONTEXT-STRATEGY。runtime → Agent Host → 回复投递仍未覆盖，Agent Host 与模型 key 属阶段 C。
- 没有启动 agent-host 和 gateway。API 进程（compose `api-1`，执行档 disabled）只在 G-WEBCHAT-AUTH 的测试中启动，用于客户的 `/api/v1` 请求。
- workbench 员工的授权链（`workbench_roles`）不在本演练中。
