# PR 草稿：NX-032 G-PLAN / G-ROLE-READ（plan reevaluation 的生产入口与 Role Source 的派生 READ）

分支 `claude/l6-g-plan-4edba0`，基于 main `0eba7c54`。迁移号为临时号 **0211**，最终编号以调度员合入顺序为准。

## 范围

补齐 2026-10-15 组装演练留下的缺口，见 `docs/release/assembly-dryrun-gaps.md`。原来 plan-reevaluator 在 stage 上没有任何计划，从不发起 Role Run；按本 PR 组装后，它的首轮发起一个受治理的 Role Run。

| 缺口 | 内容 |
|---|---|
| G-PLAN | 新工具 `nexloop_eios.plan_setup`：以 plan_reevaluator 自己的凭据和库角色（`nexloop_domain_worker`）调用 `PlanPort.establish`。私有配方；计划设置取自 reevaluator 挂载的 plan-reevaluation 文件；`plan_id` 为稳定意图，重放输出 `replay: true` |
| G-ROLE-READ | Role Run 发起时，role_source 逐对象 READ 被拒。用 SQL 派生补上（方案 A，调度员 2026-10-17 裁定），见下文 |
| G-ROLE-CATALOG | Role Context 要求目录 binding 的 `source_principal` 等于 Run 的 Source，而 stage 的 binding 指向 relay_source。**没有代码改动**：用现有 `catalog_setup` 换 role_source 凭据再建一份目录 |
| G-GOAL | stage 原来没有发布 NX-022 目标的入口。新工具 `nexloop_eios.goal_setup`：owner 以自己的 workbench 会话执行 Metric.approve 与 Goal.publish，形态同 `context_strategy_setup` |

- **配方示例**：`deploy/examples/nx037/plan-setup.example.json`、`goal-setup.example.json`。
- **命令单**：`docs/release/run-g-plan-20261017.md`，含 stage 步骤（B 类）、只读核对、撤销与限制。AT-015 之后执行。
- **不在范围**：planning 状态、契约 schema、消息 Run 路径。

## 迁移 0211（`0211_nx032_role_source_read.sql`）

为什么不能逐对象授予：给 role_source 加 object/property READ，会改变它凭据的 requested_scopes（`object.read`/`property.read`）。`service_grants.apply` 会因 `credential_rotation_required` 拒绝，而 stage 没有凭据轮换路径。所以照 0161（`relay_source_read_rule`）的形态，改由 SQL 在每次读取时派生：

1. **fact kind**：新增 `role_source_read_rule`，key 为 `[source principal]`。payload 只有 `tenant_id, principal_id, active, valid_until`，主体必须是已有的 service subject_authority。`control.nexloop_configure_manifest` 以当前定义（0162）为底逐字复制，只加了这个 kind 及其形状检查。
2. **`authz.nexloop_role_source_derivation`**：只派生 READ，只在 world real，只用于无 Run 上下文的 service 凭据。下列条件全部满足才派生：
   - 规则有效（active，且未过 valid_until）；
   - 存在一个计划，它的**当前版本**状态为 `active`（0106 `nexloop_plan_current`），且它的 PlanStep 的 `submitter_principals` 含这个 Source；
   - recipe 前后一致：link 指向 recipe 的 role 和计划的 consumer；step 和 binding 指向计划的 consumer；binding 指向 recipe 的 offering，且 `source_principal` 就是这个 Source；
   - consumer 不在 erasing 状态；
   - 属性必须在对象当前 schema 版本中声明，且不属于 owner 受限组。

   覆盖的对象：计划的 Consumer、step 的 Goal、PlanStep、EffectControl、ServiceOffering、ConsumerServiceOffering、RoleDefinition、ConsumerRoleLink、RoleExecutionCeiling（role 的 `ceiling_ref`）、RoleAssignmentScope（link 的 `scope`）。字段表固定，按生产发起路径实测得出，是最小集合。
3. **路径选择**：`authz.nexloop_property_access_basis` 改名为 `_before_role_source_v0211` 再包装。前一版本已返回 derived 时（如 relay Source），原样返回；主体对该资源有已配置 grants 时，配置优先。
4. **证明校验**：`authz.nexloop_assert_role_source_read` 每次使用时重新派生，并比对规则 hash、plan_id、plan_version、consumer、类型和受限组 hash。
5. **读入口**：两个读入口（`nexloop_assert_read_authority` 和内层 `_before_message_read_v0072`）改名再包装，只多一个 `derivation = 'role-source-v1'` 分支，其余原样转交。

迁移不写逐对象事实，不推进 authority epoch，不改业务数据，不改已发布的文件和 checksum。没有 down 迁移。

## Python 与清单

- `property_access`：新增 `RoleSourceReadRule` 与 `role_source_claims`；`object_reads._derived` 增加 role-source 分支；`trusted_configuration.FACT_MODELS` 登记新 kind。
- `deploy/authorization/stage-principals.v1.json` 升到 manifest_version 4，新增 `role_source_read_rules` 段，只有 `role_source` 一条，必须是 effect role。
- `scripts/deploy/stage_secrets.py register`：
  - 经 0050 写入规则，只写与库中不同的部分；
  - 输出新增 `role_source_read_rules_written`；`message_read_rules_written` 的含义不变；
  - dry-run 列出规则所属的主体。
- 测试辅助：`tests/test_assembly_dryrun_pg.py` 的 `assemble_role` 在同一次演练里只组装一次。

## 测试

新增 `tests/test_plan_setup_stage_pg.py`，按 stage 方式组装租户，5 例：

- **建立与重放**：重放输出 `replay: true`；同一 plan_id 换 recipe 被拒；目标版本不是当前版本被拒；Context 策略未发布被拒。
- **凭据与角色**：用非 plan_reevaluator 的凭据被拒；用 API 角色而不是 domain worker 被拒。
- **派生的边界**：
  - 计划建立前 10 类对象全拒，建立后全可读；
  - 非计划对象（relay 目录、企业 slot）、超出字段表的属性、其他 Source 都拒；
  - 计划 invalidated 后立即全拒；
  - 没有写逐对象事实，role_source 的凭据 scope 不变。
- **SQL 层**：其他租户、无规则的主体、有规则但不是 step submitter 的主体（种入故障规则）都派生不到。
- **首轮**：plan-reevaluator 的 stage 命令（`--once`）输出 `launched: 1`。断言了 Run 凭据、Role 策略绑定、Context v6 pack、`operations` 队列和预算 4/8/120/2.00 CNY。

另有：`tests/test_nx037_assembly_examples.py` 校验两个新配方示例；读授权、relay 派生、stage register、计划复评、组装演练等定向回归。在 main `47383ae9` 之上（Node 24，`LC_ALL=C.UTF-8`，Agent Host 和 web 的 dist 已重建）：

- 定向 PG 回归：`pytest -n 3` 共 26 个文件，**256 passed**，耗时 7 分 16 秒。包括 test_bootstrap、test_function_search_path、test_claim_store_pg（AT-064），以及真实 Pi 的 relay 多消费者用例；
- `scripts/ci/stages/static.sh`：退出码 0，其中含 `check_definer_search_path`。

## 风险

- **读授权入口被包装**：它们是所有派生读取的热路径。新分支只做一次字符串比较；`property_access_basis` 只在已配置 grants 失败、且前一版本也没有派生时，才多查一次规则 fact。消息 Run 路径（relay 签发、`queued`）、relay 派生、只派生一次的读路径（ADR-031）都在回归中验证过。合入时仍需跑全量 CI。
- **configure_manifest 整体复制**：如果 main 上另有迁移改了它，必须重新以最新定义为底。合入前用 `grep` 核对 0162 之后没有新的定义。
- **规则的收回**：`register` 只写 `active: true`，从清单删掉这一段不会让规则失效。好在没有 active 计划时，规则不派生任何东西。停用工具是后续改动。
- **stage 上的真实调用**：首轮 Role Run 用 deepseek-flash，每个 Run 不超过 2 CNY。可能提出的 `nexloop.service.request` 照常受三层真实派发开关、EffectControl 和联系约束限制。命令单 §6 要求负责人先确认。
- **不可逆**：迁移只能前向修复。

## 提交

- `37fbba52` G-ROLE-READ：派生迁移、stage-principals v4、register。
- `10b37f66` G-PLAN：plan_setup、goal_setup、测试、命令单。
- `546dd1ad` 重编号到临时 0211。
- 本说明另作一个提交。

prepush-scan 的结果（基准 main `47383ae9`）见完成报告。

🤖 Generated with [Claude Code](https://claude.com/claude-code)
