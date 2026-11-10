# 草稿：Consumer v2 发布后效应链中断——方案 A 的改动面、写死版本清单、拒绝场景测试（只读准备）

- **状态**：L7 草稿，2026-11-10，基于 main 23f3e2b9。只是设计准备，不含实现，不改 planning。L6 判定：A 照原样不接受，可接受更窄的 A′（§5）。负责人 2026-11-10 选定 A′，已定稿为 `adr/ADR-046-effect-scope-successor-proof.md`，运行单 `docs/release/run-r76-consumer-v2-effect-chain.md`。
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
| 同上 | 0183 | 49–51 | 效应 Action（`:1`）的 scope 必须包含 Consumer:当前 schema_version，否则报 `effect Consumer outside Action` | **A′（替代 A，见 §5）**：原条件不成立时，只有 §5 的 A′-1 至 A′-4 全部在 SQL 中成立，才执行 `:1`，并留痕“执行 :1 / 证明 :2” |
| `control.nexloop_effect_actions`（表） | 0178 | 13–24 | 全局表，append-only，stable_name 唯一，version=1 | A 不改。方案 B 才改 |

另外需要核实的检查（未发现写死 `:1`，但同样按“Action 版本的 scope 包含对象当前版本”比对，Consumer 升到 v2 后可能受影响）：
- 通用受治理对象写入：0010（create）、0015/0054（edit：`v_object.schema_version <> s->>'version'` 时拒绝）。v2 顾客只能用 `Consumer.edit:2` 编辑。claim_matcher 已经由 R74 移到 `:2`；runtime 的 Python 代码里没有调用 Consumer.edit 的地方。
- 0124/0150 治理入口上的工作台 Action（Budget.set、Control.set、Goal.publish、Contact.release、Metric.approve）：**已复现，不受影响**，见 §4.5。

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
- `message_relay._create` 和 `reply_fallback.py:65`：创建 Goal / PlanStep / MessageAssignment 时用 `action_version=1`。这些类型这次没有发布 v2；但它们属于同一类风险，已改走 Q6 helper（`create_versions.create_current`），同时改的还有 business_setup（Consumer、EffectControl）、enterprise_setup（EffectControl）和 role_assembly（Goal、PlanStep）。
- 仍写死 `action_version=1` 的 create（不在 P0 范围，列为后续）：ServiceOffering / ConsumerServiceOffering（`enterprise_setup.py`、`service_offerings.py`）、IncentiveOffering / ConsumerIncentiveOffering（`incentive_setup.py`）、RoleDefinition / ConsumerRoleLink（`role_mapping.py`）、Role 策略（`role_policies.py`）。这些类型被发布新版本时，同样需要改走 helper。
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
   8. `:2` 不存在（只有 `:1`，对象已经是 v2）。
   9. `:2` 的 scope 比 `:1` 多出一个类型（type 集合不同）。
   10. 累计比较（D1）：v1→v2 是 hide，v2→v3 是 add_optional_property，`:3` 覆盖 v3；用 `:1` 处理 v3 对象 ⇒ 拒绝（与 v2 相邻比较时会误放行）。
   11. 新增属性 visibility=hidden，或者带 default（D2）⇒ 拒绝。
8. Consumer 升到 v3，但只有 `:2` 覆盖 v2 ⇒ 拒绝（必须精确版本，不能“任一后继”）。
9. 选择和绑定之间并发发布：Consumer 在选择之后被升级 ⇒ 0179 在同一事务里重新检查并拒绝（Q3），不产生部分 context。
10. register 的 follow（Q4/Q5）：
    1. 后继 scope 多出新的类型、目标系统或 operation ⇒ register 报错退出，不派生；
    2. 未逐条声明 follow 的 Action ⇒ 不派生；
    3. 撤销前驱时，派生出的授权一起撤销。
11. 告警（Q10）：EffectContextUnavailable 或 relay 回复失败持续 15 分钟且没有成功 ⇒ 触发；告警配置为空时，覆盖率为 0，测试判失败。

每一条放宽条件（7.1–7.7）、Q2 的四个组合和 Q4 的“scope 扩大 ⇒ 不派生”，都要做变异检查。

## 4. 复现结果：工作台 Action 与队列（③）

- 测试：`tests/test_consumer_v2_goal_actions_pg.py`（本地 PG）。
  - 步骤：用 Consumer.create:1 建一个顾客；按 F3 的做法发布 Consumer v2（写入 v2 类型行，用 `schema_publication.successors()` 生成每个绑定 Consumer 的 Action 的 `:2`，再把对象升到 schema_version 2）；然后由 owner 以 `:1` 依次执行五个 Action。
- 结果：
  - Metric.approve、Goal.publish、Control.set（scope_kind=consumer，指向这个 v2 顾客）、Budget.set（在已批准预算之下调低）都成功；
  - Contact.release 停在自身的业务规则 `no active contact restriction`，这已经越过了治理入口的全部检查；
  - 没有一项因 schema scope 失败。原因：这些 handler 写的是 control 表，治理提交路径不按对象的 schema_version 比对 Action 的 scope。
- NexLoop.queue.operations：**只做了静态核对，没有运行测试**（如实标注）。当前定义是 0312 的 `authz.nexloop_runtime_activation_command_v0037`，target 固定为 `NexLoop.queue.<q>:1`，加上 `nexloop_assert_action_authority`（0107）。两者都不读 `ontology.objects`、schema_version 或 change_scope，所以不受 Consumer 版本影响。
- 结论：同类故障只在效应链上，即 0179 registrar 和 0183 intent 两处。这与 A′-2 “只在这两处使用”的要求一致。

## 5. A′ 的条件（L6 11-10 判定）与数据兼容（A′-3）

### 5.1 条件（全部在 SQL 中判定；只用于 0183 的 intent 和 0179 的 registrar）
- **A′-1 等价性**：`:2` 与 `:1` 是同一 stable_name，并且以下各项相同：
  - operation、handler、input_schema（比对摘要）、target_systems、policy_refs、approval_mode；
  - change_scope 的 type 集合相同，只有版本号不同。
  - 任一项不同即拒绝。实现方式：两个定义各去掉 version、contract_digest 和 object_types/change_scope 里的版本号后，必须完全相等（jsonb 比较），而不是只比对挑选出来的几个字段。
- **A′-2**：`:2` 是 active 的；它出现在某条 `ontology.nexloop_schema_publications` 的 successor_actions 中，且这条发布 `published_by like 'human:%'`、`permission_digest_before = permission_digest_after`；`:2` 的 scope 精确包含 Consumer 的当前版本 v。
  - 只在 0183 的 intent 和 0179 的 registrar 使用。§4 已复现，其他入口没有这个需要。
- **A′-3 数据兼容**：见 5.2。只有 `add_optional_property` 可以用 A′。
- **A′-4 留痕**：intent 的 claims、effect context 和审计中，同时记录 `executed_resource = …:1` 和 `scope_proof_resource = …:2`，并记录 publication_id。
- **A′-5 拒绝测试**：对应 §3 的第 7 组，并补上“`:2` 不存在”和“scope 多出一个类型”两项；每一项都做变异检查。v1 和 v2 两类顾客都要端到端成功。
- **A′-6 期限**：ADR 附注写明这是过渡规则（定稿：`adr/ADR-046-effect-scope-successor-proof.md`）。另加一个守护测试：一旦效应 Action 的允许名单接受 (stable_name, version>1)，即 B 合入，测试就变红，要求删除 A′。

### 5.2 Consumer v1→v2 的差异，以及 `:1` 的读写在 v2 下是否不变
- **F3 只有三种发布操作**（`schema_publication.build`，以及 0257 中逐字段核对的 `expected`）。各自的后果：
  - `add_optional_property`：只在末尾追加一个属性，`required=false`、`nullable=true`、无默认值；已有属性逐字段不变（SQL 核对 `new = old || {version+1, properties: old || [np]}`）。**只增不改，A′-3 成立。**
  - `add_vocabulary_value`：给已有的封闭词表增加一个枚举值。v2 对象中可能出现 v1 词表里没有的值，读到这个属性的 `:1` 消费方语义会变。**A′-3 不成立，A′ 必须拒绝。**
  - `hide`：把一个属性的 visibility 改为 hidden。展示和读取的语义会变。**A′-3 不成立，A′ 必须拒绝。**
- **效应链在这两处读写 Consumer 的哪些字段**：
  - 0179 registrar（bind）读：`object_id`、`type_name`、`nexloop_revision`、`schema_version`（只用于 scope 比对）、`tenant_id`/`world`；不读任何 properties。
  - 0183 intent 读：`object_id`、`nexloop_revision`（expected_versions）、`schema_version`（只用于 scope 比对）；不读 properties。
  - 效应 worker（`effect_execution.py`）不读 Consumer 的 properties。
  - 因此，在“只追加可空属性”的前提下，`:1` handler 在 v2 对象上读写的字段，含义与 v1 完全相同。
- **A′ 的 SQL 实现**：除 5.1 的条件外，还要累计比较 `:1` scope 中的 Consumer 版本 b（不是 v−1，L6 D1）与对象当前版本 v 的类型定义：
  - v 的 properties 必须以 b 的 properties 为前缀（逐元素相等）；
  - b 之后追加的每个属性都必须 `required=false`、`nullable=true`、visibility 不是 `hidden`、`default_value` 为 null 或不存在（L6 D2）；
  - 其余键（type_name、only_edit_via_actions、primary_key）相同。
  - 这样 b→v 之间的任一次 add_vocabulary_value 或 hide 都会被拒绝；每一项都做变异检查（§3 第 7 组第 10、11 项）。
- **relay/context 读 Consumer properties**（L6 建议项）：构造上下文时，`consumer_profile.py` 和 `context_engine/sources.py` 通过受授权的对象读取来读 Consumer 属性，这与效应 Action 的版本无关；新增属性只在存在对应的属性读取授权时才会出现。效应 intent 和 `:1` handler 不读 properties，所以 v2 新增的可选属性在 `:1` 的读写路径上被忽略。这一点目前是阅读代码得出的结论，没有测试证明，实现时补一条测试：v2 顾客的 `delivery_windows` 有值时，`:1` 的效应负载和 intent 不受影响。
- **stage 实际的 v2**：我无法读取 stage。请调度员只读执行下面的查询（预期 `prefix_unchanged = true`，`added` 中每一项都是 required=false、nullable=true）：

```sql
select v1.version as v1, v2.version as v2,
       (v2.definition->'properties') @> (v1.definition->'properties')
         and jsonb_array_length(v2.definition->'properties') > jsonb_array_length(v1.definition->'properties') as superset,
       (select bool_and(a.e = b.e) from jsonb_array_elements(v1.definition->'properties') with ordinality a(e,n)
          join jsonb_array_elements(v2.definition->'properties') with ordinality b(e,m) on a.n = b.m) as prefix_unchanged,
       (select jsonb_agg(jsonb_build_object('name', e->>'property_name', 'required', e->'required', 'nullable', e->'nullable', 'visibility', e->'visibility'))
          from jsonb_array_elements(v2.definition->'properties') with ordinality q(e,n)
         where n > jsonb_array_length(v1.definition->'properties')) as added,
       (select operation from ontology.nexloop_schema_proposals pr join ontology.nexloop_schema_publications pu using (proposal_id)
         where pu.tenant_id = v2.tenant_id and pu.type_name = 'Consumer' order by pu.published_at desc limit 1) as operation
  from ontology.object_type_versions v1 join ontology.object_type_versions v2 using (tenant_id, type_name)
 where tenant_id = '<TENANT_ID>' and type_name = 'Consumer' and v1.version = 1 and v2.version = 2;
```

（列名已对照 0254 的表结构：publications 有 `proposal_id`、`type_name`，proposals 有 `operation`。）

- **stage 核对结果**（调度员只读执行上面的 SQL，另加 other_keys_same，2026-11-10 08:5x）：v1=1、v2=2；`prefix_unchanged=true`；属性从 6 个变为 7 个，新增 `delivery_windows`（required=false，nullable=true，visibility=normal）；`other_keys_same=true`；operation=`add_optional_property`。**A′-3 在 stage 成立。**

## 6. 方案 B 和 C（不在 P0 内，只做记录）
- **B：效应 Action 随 schema 升版。**
  - 允许名单改为按 (stable_name, version) 存储，slot 支持多个版本；
  - Host 和 contracts 中的 `:1` 改为版本白名单；
  - 需要 ADR、改 contracts、Host 发版，以及 context-pack 新版本。
- **C：退回 v1。** 走受治理的降级或撤回发布路径（目前不存在），需要负责人决定。

## 7. A′ 实现的证据（L6 P1–P3，2026-11-10，本地 PG，`-n 2`）

### 7.1 拒绝测试（P1）
`tests/test_effect_scope_successor_proof_pg.py`，共 36 例，全部通过。
- **成功**：
  - v2 旧顾客回复成功，registrar 用 `:2`，留痕 `:1`/`:2`；
  - v1 顾客走 `:1`，不留痕；
  - v1→v2→v3 的累计追加链由 `:3` 证明。
- **拒绝（参数化 22 项）**，每项只破坏一个条件：
  - 发布记录：无发布记录、非人工发布、发布记录的类型不是 Consumer、发布记录的版本不是 v；
  - 后继定义：输入模式不同、scope 多出一个类型、target_systems 不同、capability 不同、idempotency 不同；
  - 后继本身：后继 scope 的 Consumer 版本不是 v（7.2）、后继非 active（7.1）、后继不存在、后继在另一个 world、后继在另一个租户（7.7）；
  - 新增属性：必填、必填但可空、不可空、hidden、带 default；
  - 类型定义：已有属性被修改、类型的其他键被修改。
- **单列的拒绝和检查**：
  - Consumer 升到 v3、但只有 `:2` 覆盖 v2（§3 第 8 项）：断言在 effect_bind 失败（EffectContextUnavailable）；
  - D1 累计：v1→v2 hide、v2→v3 add，执行 `:1` 被拒；
  - §3 第 9 项：选版本之后发生并发升级，0179 在同一事务内拒绝，不留 effect context，也没有 terminal claim；
  - 7.5：权限摘要不等的发布记录写入时报 CheckViolation（表约束），所以这种发布不可能存在；
  - Q1：故意选错 registrar 版本被拒；
  - Q2：四个混合组合；
  - `:1` 路径忽略新增属性；
  - B 守护测试。

### 7.2 变异检查（P2）
每次只删一个 SQL 条件，跑整个测试文件。下表列出变红的用例：

| 删除的条件 | 结果 | 变红的用例 |
|---|---|---|
| A′-1 后继 active | 1 failed | `successor_inactive` |
| A′-1 同 world | 1 failed | `successor_other_world` |
| A′-1 scope 含 Consumer:v | 1 failed | `successor_scope_wrong_version` |
| A′-1 capability 相等 | 1 failed | `successor_capability_differs` |
| A′-1 去掉版本号后定义相等 | 4 failed | `successor_policy_differs`、`successor_receipt_differs`、`successor_scope_extra_type`、`successor_target_systems_differ` |
| A′-2 人工发布 | 1 failed | `publisher_not_human` |
| A′-2 发布记录的类型和版本 | 2 failed | `publication_other_type`、`publication_other_version` |
| D1 前缀相等 | 2 failed | `test_the_comparison_is_cumulative_from_the_executed_scope`、`existing_property_changed` |
| D1 其余键相同 | 1 failed | `other_type_key_changed` |
| D2 非必填 | 1 failed | `added_property_required_nullable` |
| D2 可空 | 1 failed | `added_property_not_nullable` |
| D2 非 hidden | 1 failed | `added_property_hidden` |
| D2 无 default | 1 failed | `added_property_default` |
| Q2 精确 (type, version) | 6 failed | 四个组合、v1 顾客走 `:1`、并发升级 |
| Q1 registrar 的 scope 和快照检查（两层同时删） | 2 failed | 故意选错版本、并发升级 |
| Q4 scope 扩大即拒绝（`service_grants._rebinding_reason`） | 1 failed | `test_dry_run_stops_on_a_refused_successor[scope_widened]` |

单删一项抓不到的四个条件。每一项都有另一层防线，下面写明了另一层是什么、怎样验证：
- **A′-1 同 stable_name**：stable_name 本身在定义里，“去掉版本号后定义相等”已经覆盖它，所以单删不会变红。
- **A′-1 同租户**：proof 函数在入口设置 `eios.tenant_id=p_tenant`，新表和读到的表都有租户 RLS（force），单删这个谓词仍被 RLS 挡住。两层同时删（谓词加 row_security=off）时，owner 在 force RLS 下无法执行，函数本身失效，正常用例变红。所以这一项无法得到“只让拒绝用例变红”的结果，如实记录。
- **A′-2 权限摘要不变**：表 CHECK 约束保证摘要不等的行写不进去，单删不会变红。7.5 的断言证明了这一点。
- **A′-2 “发布记录为空即拒绝”**：publication_id 的 NOT NULL 也会拒绝。之前做过组合变异（删除判空并放开 NOT NULL），`no_publication_record` 和 `publisher_not_human` 变红。

### 7.3 新表守护（P3）
- `control.nexloop_effect_scope_proofs` 的 policy 改为租户 policy：`tenant_id=current_setting('eios.tenant_id',true)`，RLS enable 且 force。proof 函数入口设置 `eios.tenant_id` 为 intent 自己的租户。
- `tests/test_db_boundary.py` 只要求 ontology/runtime 下的表开启并强制 RLS；新表在 control 下，也已开启并强制。
- 这张表没有 consumer_id，也没有任何身份列（只有 context_id、资源 id、publication_id 和版本）。consumer、member 两个 erasure coverage 只检查带 consumer_id 或身份列的表，所以无须登记，测试通过。
- 跑过的测试共 365 个，全部通过：
  - db_boundary、tenant_fk、system_catalog、function_copy_guard；
  - consumer、member、erasure、erasure_references 四个 coverage；
  - claim_store；
  - nx041 sim（全部 `test_nx041_sim_*`）；
  - bootstrap、published_migrations；
  - 本节的三个新测试文件，以及 api_stage_ready。
- definer 检查 0 个问题。

### 7.4 P4：真实发布、真实 register、真实 relay worker
- **测试**：`tests/test_consumer_v2_stage_path_pg.py`。
- **流程**（都在 NX-032 stage 组装夹具里运行）：
  - stage 组装：bootstrap、`stage_secrets register`、browser、目录发布、business/catalog/enterprise setup、relay enrollment；
  - owner 的工作台浏览器会话（持有 ontology.schema.review）经 `SchemaPublicationPort.publish` 发布 Consumer v2（F3 `nexloop_schema_proposal_publish`）；proposal 和 release permit 按 test_nx040 的方式预置；
  - `stage_secrets.register --dry-run` 派生出且只派生出 relay_planner 和 role_planner 的 `nexloop.plan.bind_effect_context:2`，scope_diff 为 Consumer 1→2，purpose 写明这次 publication；
  - 去掉 dry-run 后执行 register，写入这两条授权；
  - 旧顾客的下一条消息经真实 message-relay worker 处理，结果 `queued`，registrar claim 的版本为 2。
- **反向对照**：把 registrar 选版强制为 `:1`（即修复前），同一测试失败，原因为 `EffectContextUnavailable`、stage 为 `before_context`，与 stage 现象一致。
- **发现**：发布后用 business_owner 新建顾客会被拒。它只有 `Consumer.create:1`，Q5 不允许它跟随，Q6 也不回落到 v1；同一文件的第二个测试固定了这一现状。R76 第 7 步“新建顾客”这一条，要等负责人决定（加 follow 声明，或显式授予 `:2`）后才能做。
- **决定与实现（Q5′，负责人 2026-11-10 “按推荐”）**：business_owner → `Consumer.create:1` 声明 follow；FOLLOWABLE 逐条 3 条；create 后继的 scope 只允许该类型版本变化（`create_scope_other_type`）。stage 路径测试改为：发布后 dry-run 派生 `Consumer.create:2`，register 后新顾客为 v2；反向对照（白名单去掉 create 时 register 被拒且不写授权；去掉声明后新顾客被拒、无半成品）。待 L6 复审。
- **未覆盖**：效应 intent（Host 和模型那一段）没有在 stage 夹具里跑；intent 的 A′ 证明由 `tests/test_effect_scope_successor_proof_pg.py` 的真实 relay 和 guard 链覆盖。
