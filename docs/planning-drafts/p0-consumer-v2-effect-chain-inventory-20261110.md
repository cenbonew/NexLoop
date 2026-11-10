# 草稿：Consumer v2 发布后效应链中断——方案 A 的改动面、写死版本清单、拒绝场景测试（只读准备）

- **状态**：L7 草稿，2026-11-10，基于 main 23f3e2b9。只是设计准备，不含实现，不改 planning。方案 A 待 L6 判定、负责人决定。
- **现象**：stage 11-09 04:53:49Z 发布 Consumer v2（NX-040 F3）。之后对旧顾客的 relay 回复全部失败，失败点在 effect_bind（`registrar schema scope unavailable`）。customer-5（Consumer v1）正常。
- **原因链**：
  - F3 把真实世界中该类型的对象全部升到 v2，并为 change_scope 含 Consumer 的 Action 生成 `:2`；`:1` 保持 active。
  - 效应链上的每一处 SQL 检查都要求“所用 Action 版本的 change_scope 精确包含对象当前的 (type, schema_version)”。
  - 但效应链的 Action 版本被多处写死为 `:1`：SQL、Python、Host、contracts 和允许名单。

## 1. 方案 A 下需要改的 SQL 函数（只列当前生效的最新定义）

| 函数 | 当前定义 | 行号 | 内容 | A 下的改法 |
|---|---|---|---|---|
| `authz.nexloop_effect_context_command` | 0179 | 48 | `c->>'resource_id'` 必须等于 `…configure_control:1` / `…bind_effect_context:1` | 改为“该 verb 对应 stable_name 的某个 active 精确版本”（`^eios:action:<name>:[1-9][0-9]*$`，并且 `pub` 存在、active） |
| 同上 | 0179 | 63 | `definition_reference.version` 必须为 `'1'` | 改为等于 `pub.definition->>'version'` |
| 同上 | 0179 | 114–118 | configure：registrar 的 scope 和 schemas 快照必须包含 EffectControl:当前版本 | 不改（只要选到覆盖的版本即可）；Consumer 发布不影响 EffectControl |
| 同上 | 0179 | 165–171 | bind：registrar 的 scope 和快照必须精确包含 step/goal/control/consumer 四个对象的 (type, version) | 不改。Python 选出覆盖四个对象的最高版本（L6 Q2），SQL 仍逐对象精确比对（Q1） |
| `authz.nexloop_effect_intent_command_v0040` | 0183 | 23 | 效应 Action 必须在全局允许名单 `control.nexloop_effect_actions` 中（stable_name 是主键，version 固定为 1） | 不改。A 不升级效应 Action |
| 同上 | 0183 | 49–51 | 效应 Action（`:1`）的 scope 必须包含 Consumer:当前 schema_version，否则报 `effect Consumer outside Action` | **A 的核心放宽**：原条件不成立时，若同时满足以下条件，仍放行 `:1`：①同一租户、同一 world 下，存在同 stable_name 的 active 后继，其 scope 精确包含 Consumer:v；②该后继出现在一条 `ontology.nexloop_schema_publications` 的 successor_actions 中，且这条发布由 human 发布、`permission_digest_before = permission_digest_after`；③后继与 `:1` 的 operation、target_systems、input_schema 和 capability 相同，scope 只有版本号上升（与 L6 Q4 同一条件） |
| `control.nexloop_effect_actions`（表） | 0178 | 13–24 | 全局表，append-only，stable_name 唯一，version=1 | A 不改。方案 B 才改 |

另外需要核实的检查（未发现写死 `:1`，但同样按“Action 版本的 scope 包含对象当前版本”比对，Consumer 升到 v2 后可能受影响）：
- 通用受治理对象写入：0010（create）、0015/0054（edit：`v_object.schema_version <> s->>'version'` 时拒绝）。v2 顾客只能用 `Consumer.edit:2` 编辑。claim_matcher 已经由 R74 移到 `:2`；runtime 的 Python 代码里没有调用 Consumer.edit 的地方。
- 0124/0150 治理入口上的工作台 Action（Budget.set、Control.set、Goal.publish、Contact.release、Metric.approve 等），它们的 object_types 都包含 Consumer。要逐个核实 handler 是否按对象 schema_version 比对 scope。若比对，旧顾客在工作台上的这些写入同样会失败。0432 只解决了“选哪个版本”，没有解决 scope。

## 2. 写死版本的清单

change_scope 含 Consumer 的系统 Action 共 16 个（共 41 个）：Budget.set、Consumer.create、Contact.release、Control.set、Goal.propose、Goal.publish、Metric.approve、NexLoop.queue.operations、nexloop.context.bind、nexloop.context.bind_role、nexloop.enterprise.deliver、nexloop.plan.bind_effect_context、nexloop.service.query、nexloop.service.receipt_reconcile、nexloop.service.request、nexloop.service.scope_denial.record。

下面按“是否在 A 下需要改”列出这些 Action 被写死 `:1` / `version=1` 的位置（共 126 处，来自 `git grep`，不含测试和迁移）。

### 2.1 A 下必须改（runtime 路径）
- `packages/eios-core/src/nexloop_eios/effect_contexts.py`：
  - :90，`_claim` 的 target 写死 `':1'`；
  - :101，`get_with_schemas(action, 1)`；
  - :136、:137、:144，三处 `_proof` 写死 `':1'`。
  - registrar 自己的版本要改为按 Q2 选择；效应 Action（executor/run proof、:110、:113）在 A 下保持 `:1`。
- `packages/eios-core/src/nexloop_eios/business_setup.py:71`：`Consumer.create` 写死 `action_version=1`。类型已发布 v2 时，会新建 v1 顾客，造成版本倒挂。改为 Q6 helper：取已发布且有效的版本，并验证对应 create 的后继已发布、调用方有授权，否则抛错；启动自检也做这项检查。
- `deploy/authorization/service-grants.v1.json`：给 registrar 的 `bind_effect_context` 和 `configure_control` 逐条声明 follow_latest_version（Q5），并扩展 Q4 的 scope 差异校验。

### 2.2 A 下保持 `:1`（效应 Action 不升级；改动属于方案 B）
- **Python**：
  - `message_relay.py`：:18 `EFFECT`，:20 `DELIVER`，:155 `reader.get('nexloop.service.request', 1)`；
  - `reply_fallback.py:24`；
  - `effect_execution.py`：:31 SEND，:41 QUERY，:42 RECONCILE，:475；
  - `context_pack.py`：:28、:29；
  - `service_offerings.py`：:13、:31、:54；
  - `contracts.py`：:456、:467、:491、:504、:855、:866、:890、:903，以及 :2073/:2098 的生成 schema。
- **Host 和 Web**：
  - `apps/agent-host/src/context-input.ts`：:60、:327；
  - `runtime-effect-tools.ts:563`、`runtime-host.ts:139`（注释）；
  - `apps/web/src/workbench/realDispatch.ts:16`；
  - `apps/reference-endpoint/reference_endpoint.py`：:18、:203（注释和 docstring）。
- **contracts（按约束不能改）**：
  - `packages/contracts/context-pack-v6/v7.schema.json`，各 7 处 const/enum；
  - `generated/contracts.ts`、`openapi-components.json`；
  - `examples/*`。
- **配置**：
  - `deploy/configuration/action-effect-categories.v3.json`；
  - `nx037-trial.v2–v9.json`、`nx037-interaction-plan.v1–v3.json`；
  - `remediation-rules.v1.json`；
  - `scripts/deploy/real_dispatch_switch.py:18`。

### 2.3 工作台和授权（0432 已处理“选哪个版本”；scope 见 §1 末尾的“需要核实”）
- `apps/web/src/workbench/api.ts:511` 及同文件约 40 处 `eios:action:<X>:1`：只是前端标识，实际执行版本由 0432 的 action_for 决定。
- `packages/eios-core/src/nexloop_eios/workbench_roles.py`：:56、:57 的 owner_only 集合；`deploy/authorization/workbench-roles.v1.json`，6 处。
- `deploy/authorization/stage-principals.v1.json`：14 处，是精确版本授权。由 register 的 follow 派生 `:2`，不改文件。

### 2.4 不受影响或单独判断
- `reply_fallback.py:103` 的 `step_revision=1, goal_revision=1`：是对象的 nexloop_revision（刚创建的 Goal/PlanStep 恒为 1），不是 schema 版本，不改（L6 Q6）。`message_relay.py:319` 同理。
- `message_relay._create` 和 `reply_fallback.py:65`：创建 Goal / PlanStep / MessageAssignment 时用 `action_version=1`。这些类型这次没有发布 v2；但它们属于同一类风险，改为走 Q6 helper（发布这些类型的新版本时，同样会倒挂）。
- `outreach.py:17–19`、`plan_reevaluation.py:35–36`、`sim_world_credentials.py:68`（NexLoop.queue.operations:1）：
  - outreach.* 和 plan.* 的 object_types 不含 Consumer，不受这次发布影响；
  - NexLoop.queue.operations 含 Consumer，但它是队列操作，需要核实其 SQL 是否检查 Consumer 的 scope。
- `scripts/acceptance/*`、`scripts/nx037/*`、`scripts/load/stack.py`：验收和压测脚本，跟随正式路径，不阻塞 P0。

## 3. 拒绝场景测试列表（草稿）

前提：每条测试都用真实的 F3 发布（`nexloop_schema_proposal_publish`）把 Consumer 升到 v2；涉及 registrar 的测试都要完整跑 relay 全链（issue → effect_bind → context → runtime accept → effect intent）。

**应该成功（对照组）**
1. 旧顾客（Consumer v2，Goal/PlanStep/EffectControl v1）：registrar 选 `:2`，效应 Action 用 `:1`（经 lineage 放行），回复成功。
2. customer-5 这类 v1 顾客：registrar 落 `:1`，效应 Action 用 `:1`（原条件），回复成功。
3. 新建顾客：按 Q6 写 v2，与 1 相同，回复成功。

**应该拒绝**
4. Python 故意选错 registrar 版本（选 `:1` 处理 v2 顾客）⇒ 42501 `registrar schema scope unavailable`（Q1）。
5. registrar `:2` 存在，但 planner 没有 `:2` 的有效 execute 授权 ⇒ 不选 `:2`，结果为 EffectContextUnavailable，不静默放行。
6. Q2 的四个混合组合：Consumer、Goal、PlanStep、EffectControl 中某一个对象的版本不在候选 scope 内 ⇒ 不选该版本；没有任何候选覆盖时拒绝。
7. 效应 Action 放宽的各项条件分别不满足时，都报 `effect Consumer outside Action`：
   1. 后继不 active；
   2. 后继的 scope 不包含 Consumer:v；
   3. 后继不在任何发布的 successor_actions 中（例如手工插入的定义）；
   4. 发布者不是 human；
   5. 发布的权限 digest 前后不等（写不进去，CheckViolation）；
   6. 后继与 `:1` 的 operation、target_systems、input_schema 或 capability 不同；
   7. 后继在另一个租户或另一个 world。
8. Consumer 升到 v3，但只有 `:2` 覆盖 v2 ⇒ 拒绝（必须精确版本，不能“任一后继”）。
9. 选择和绑定之间并发发布：Consumer 在选择之后被升级 ⇒ 0179 在同一事务里重新检查并拒绝（Q3），不产生部分 context。
10. register 的 follow（Q4/Q5）：
    1. 后继 scope 多出新的类型、目标系统或 operation ⇒ register 报错退出，不派生；
    2. 未逐条声明 follow 的 Action ⇒ 不派生；
    3. 撤销前驱时，派生出的授权一起撤销。
11. 告警（Q10）：EffectContextUnavailable 或 relay 回复失败持续 15 分钟且没有成功 ⇒ 触发；告警配置为空时，覆盖率为 0，测试判失败。

每一条放宽条件（7.1–7.7）、Q2 的四个组合和 Q4 的“scope 扩大 ⇒ 不派生”，都要做变异检查。

## 4. 方案 B 和 C（不在 P0 内，只做记录）
- **B：效应 Action 随 schema 升版。**
  - 允许名单改为按 (stable_name, version) 存储，slot 支持多个版本；
  - Host 和 contracts 中的 `:1` 改为版本白名单；
  - 需要 ADR、改 contracts、Host 发版，以及 context-pack 新版本。
- **C：退回 v1。** 走受治理的降级或撤回发布路径（目前不存在），需要负责人决定。
