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
| D-QUEUE | Compose 中 runtime-worker 用 `--queue runtime`，但 `runtime_worker` 只被授予 `NexLoop.queue.operations:1`。relay 与 Role queue 也都把 Run 放进 `operations` | `test_each_worker_first_round`（当前退出码 1，`dispatch unavailable`）；`test_runtime_worker_first_round_on_the_granted_queue`（改成 `operations` 后首轮完成） | stage 上 runtime-worker 每轮都认领失败，消息 Run 永远不会派发 | 改 `compose.app.example.yaml` 为 `--queue operations`，属部署配置改动，由调度员决定 |
| G-WEBCHAT-AUTH | webchat 客户没有业务授权：0045 浏览器业务授权快照需要 `control.nexloop_browser_business_applications` 行和该 Human 的 grant 事实。stage 工具都不登记这两样：`stage_secrets browser` 明确不登记，`register` 只管服务，`workbench_roles` 只管员工 | `test_the_webchat_consumer_has_no_business_authority_after_assembly` | 客户无法建会话、发消息，relay 没有输入 | 需要一个客户角色到 grant 的受信配置工具（类似 `workbench_roles`），以及 webchat 的 browser business application。需要设计 |
| G-ROLE | 没有任何 stage 主体持有 `RoleDefinition.create`（scope `ontology.roles.manage`），也没有生产工具创建 RoleDefinition 或 ConsumerRoleLink | 演练中 RoleDefinition 由测试管理员直插（`tenant_config` 函数） | `relay-run-profile` 无法在 stage 生成；plan-reevaluator 的 Role Run 没有角色 | 增加一个角色组装主体与工具，例如由 business_owner 或独立的 role maintainer 执行 RoleDefinition/ConsumerRoleLink.create。另外按 T6 的发现，消息 Run 的 `role_ref` 只是标签，也可以改为固定标签，需要决策 |
| G-ROUTES | `action-worker/provider-routes` 与 `webchat-provider-config` 是 manual 文件，没有生成器 | 演练按 `NX-037-provider-routing.md` §2 自行写出（`manual_files`），只配 webchat 路由 | stage 需要手写；写错时 action-worker 启动失败 | `stage_secrets` 增加生成，或在 run sheet 中附模板。enterprise 路由属阶段 C |
| G5 | claim-extraction-worker 与 claim-matcher 需要 `model.env`（阶段 C） | 两者 `skip` | 不能启动 | 已知，阶段 C |
| T3 | `stage_secrets.py publish` 还未合入；演练用测试辅助代替 | `compile_catalog`：T3 合入后自动改用 `catalog_publish.compile_catalog`，在此之前用 system_catalog 加业务清单 | 合入后需重跑本演练，核对清单方式与 system_catalog 是否一致 | T3 合入后重跑 |

## 3. 没有覆盖到的部分

- **首轮都是空闲的一轮**：没有消息经过 relay → runtime → Agent Host → 回复投递。原因有两个：G-WEBCHAT-AUTH 使客户无法发消息；Agent Host 与模型 key 属阶段 C。补齐 G-WEBCHAT-AUTH 后，可以在本演练里加一步：写入一条客户消息，再跑 relay 的首轮。
- 没有启动 agent-host、gateway 和 API 进程本身。API 的 stage 预检另有测试。
- workbench 员工的授权链（`workbench_roles`）不在本演练中。
