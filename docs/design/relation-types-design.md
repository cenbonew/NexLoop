# 正式关系类型：设计稿（ADR-038）

状态：设计稿，供 L6 复核与负责人拍板。基于 main 0a7d7eef。本阶段只写设计，不改代码。

修订 1：处理 L6 复核 `relation-types-adr-review.md`（59552198）的 C-1、I-1 到 I-4 和 S-1 到 S-6，对应改动在各节标注。

## 1. 事实核对：现有的关系能力

依据：NexLoop 迁移 0001、0016、0058、0223、0230、0132 起的擦除迁移、0315；vendored `packages/eios-core/src/eios`；EIOS 冻结提交 1e363db 的只读工作树。

| 能力 | 现状 | 缺口 |
| --- | --- | --- |
| 表 | `ontology.relation_type_versions(tenant_id, relation_name, version, definition)`。版本不可变：触发器函数 `authz.nexloop_schema_version_immutable` 在 0009 定义，0016 把它挂到这张表上；改动推进权限纪元。`ontology.relations(relation_id PK, tenant_id, world, relation_name, schema_version, source_type/id, target_type/id, metadata, created_at)`，唯一索引 `(tenant_id, world, relation_name, source, target)` | 端点外键是 `(tenant_id, type, object_id) → ontology.objects`，**不含 world**；world 隔离只在 link 函数里校验（`o.world=p_world`），表上没有约束。`ontology.objects` 的主键本身也是 `(tenant_id, type_name, object_id)`（0001），所以同一租户内一个 object_id 只能在一个 world 中存在。源侧索引 `ontology_relations_source(tenant_id, source_type, source_object_id)` 已存在（0001）（S-1） |
| 建立（受治理） | `authz.nexloop_link_relation_action`（0016）：签名命令、Action 定义 capability `ontology.relation.link`，要求 low 风险、无审批、无策略；`control.nexloop_relation_action_bindings` 把 Action 与关系类型版本绑定；两端各要一份编辑权限证明，并核对类型在 Action 的 change_scope 内；Action claim 用于幂等；relation_id 是 `sha256(tenant, world, name, source, target)`，确定性；同一对重复写入时 metadata 不同则报冲突；按 one_to_one / many_to_one / one_to_many 检查基数；用命名空间咨询锁串行化。Python 侧是 `nexloop_eios.relation_actions.GovernedRelationLinker`（`backend.link_relation`） | 端点 id 只接受 64 hex；**没有任何系统 Action 绑定关系**（系统目录 `system_catalog.py` 只发布 Object Type 和 Action），所以 stage 上是 0 行。擦除 purge 删除关系的语句都不带 world 条件，这依赖 objects 主键在租户内唯一（S-1） |
| 表权限 | 静态核查迁移：NexLoop 应用角色（nexloop_api、domain_worker 等）对 `ontology.relations` 和 `relation_type_versions` 没有任何授权；0003 撤销了 nexloop_owner 新建表的 public 默认权限。唯一的例外是 0001 的条件授权：如果 `nex_eios_app` 角色存在，就给它 relations 的 select 和 insert | 线上实际授权未核实；P1 加结构测试：除 nexloop_owner 外，任何角色对这两张表都没有 INSERT、UPDATE、DELETE 权限（包括 nex_eios_app）（S-1） |
| 删除（受治理） | NexLoop **没有**。上游 EIOS 有 capability `ontology.relation.unlink`（scope `ontology.relation.write`，`eios/ontology/capabilities.py`）；vendored 的 `eios/adapters/postgres/ontology_store.py:unlink_relation` 调用 `ontology.app_relation_unlink`，这个函数在 NexLoop 迁移中**不存在** | 需要补 unlink 入口（同 0016 的模式）；擦除用的直接 `delete from ontology.relations`（0132/0138/0139/0144/0145/0227）是擦除 purge 的特权路径，不是业务入口 |
| schema_version | 关系实例记录类型版本，link 时核对 `relation_type_versions` 与绑定一致 | 关系类型没有发布入口：NX-040 的 F1 拒绝 `add_relation_type`（D24）。系统目录需要加关系类型的发布（configurator，同 Object Type） |
| 权限推导 | 建立：`authz.nexloop_assert_relation_create_authority` + 两端的编辑证明。RelationshipAssessment 引用关系类型时用 `ResourceType.LINK_TYPE` 证明 | **读**：ADR-025/035 的成员读（`authz.nexloop_workbench_member_may_read`）只定义 `eios:object:` / `eios:property:` 六种类型，**没有关系的读定义**。0223 的邻居对关系边只要求两端对象可读 |
| 审计 | 建立：Action claim 记终态 outcome（relation_id） | 关系的建立和删除不写 `ontology.events`，也没有工作台读审计。擦除删除关系时只计数 |
| world 隔离 | link 函数要求两端对象在 p_world；关系行带 world | 表层没有约束（见上）；`relation_id` 的哈希包含 world |
| 复制件守护 | 0016 早于基线 0259，不受守护；之后重定义的任何函数（包括 link）都要登记 | 新迁移改写 Conversation/Message/Commitment 等定义函数时，每一个都要登记 revert |
| EvoOntology / 语义层 | 语义层（`context_engine/semantic.py`）只认 `eios:object_type:` / `eios:property:` / `nexloop:vocabulary:` 三种 ref，不读关系；NX-040 `schema_compat` 拒绝关系类型的变更（`relation_type_closed`） | 首批不改：关系不进语义层，EvoOntology 不读关系（ADR-038 §4） |
| 已有读方 | 0223 / 0230 世界模型邻居（governed relations + 引用属性）；擦除 purge 删除两端是被删对象的关系；RelationshipAssessment（0058）按 relation_name 和 version 引用关系类型 | 邻居今天只有引用属性这一路有数据 |

## 2. 清单：引用属性与连接对象

判据：

- **A. 变成正式关系**：值是另一个正式对象的 id，语义是“属于 / 指向”，读方（邻居、归属、擦除）需要它；
- **B. 保留属性并登记**：值不是正式对象（principal、外部 ref），或者是授权范围的取值集合，或者只是历史快照；
- **C. 连接对象保留为对象，另加关系**：对象有自己的属性、revision 或生命周期（active、valid_until、state）。

基数中的“多→一”指许多源对象指向同一个目标对象。

| 类型 | 字段 | 目标类型 | 基数 | 写入方（受治理入口） | 建议 |
| --- | --- | --- | --- | --- | --- |
| Goal | consumer_id | Consumer | 多→一 | Goal.create（ontology.object.create）、goals.* Action | A `goal_of`（归属） |
| PlanStep | consumer_id | Consumer | 多→一 | PlanStep.create | A `plan_step_of`（归属） |
| PlanStep | goal_id | Goal | 多→一 | PlanStep.create | A `step_in_goal` |
| PlanStep | control_id | EffectControl | 多→一 | PlanStep.create | A `step_under_control` |
| EffectControl | consumer_id | Consumer | 多→一 | EffectControl.create / nexloop.effect.configure_control | A `control_of`（归属） |
| Conversation | consumer_id | Consumer | 多→一 | 0046 prepare/commit_conversation（Conversation.create） | A `conversation_of`（归属） |
| Conversation | owner_principal | principal | — | 同上 | B（principal 不是对象） |
| Message | conversation_id | Conversation | 多→一 | 0046 prepare/commit_message（Message.create） | A `message_in`（归属经 Conversation 传递，见 §3.3） |
| Message | actor | principal | — | 同上 | B |
| Commitment | made_to | Consumer | 多→一 | Commitment 创建 Action（commitment guard，conversation extraction） | A `commitment_to`（归属） |
| Commitment | related_goal_ref | Goal | 多→一，可空 | 同上 | A `commitment_for_goal` |
| Commitment | supersedes | Commitment | 一→一，可空 | 同上 | A `supersedes` |
| Commitment | fulfillment_basis | （待核：证据 ref） | — | 同上 | B，待核实是否指向对象 |
| CommercialRecord | consumer_ref | Consumer | 多→一，可空（unlinked） | 0118/0121 商务摄入与链接（configurator link/unlink） | A `commercial_record_of`（归属）；链接/解除链接时同时写/删关系 |
| RelationshipAssessment | source_id / target_id | relation_type 定义的端点类型 | 由关系类型决定 | 0058 assessment create/correct | B：它**评估**一条关系，本身不是关联；但其 source/target 的 Consumer 计入归属（0315 已覆盖），改为经 `assesses_*` 关系表达见 §3.2 待核 |
| RelationshipAssessment | evidence_message_id | Message | 多→一 | 同上 | A `assessment_evidence` |
| MessageAssignment | message_id / consumer_id / goal_id / step_id / control_id | Message / Consumer / Goal / PlanStep / EffectControl | 多→一 | MessageAssignment.create | C：对象保留（state、valid_until、revision 快照），加 `assignment_of`（→ Consumer，归属）、`assignment_for_message`、`assignment_goal`、`assignment_step`、`assignment_control` |
| ConsumerOwnership | consumer_id | Consumer | 多→一：`control.nexloop_consumer_owners` 的主键是 `(tenant_id, world, principal_id)`，consumer_id 没有唯一约束，一个 Consumer 可以有多个 principal（I-4） | 0046 commit_owner（ConsumerOwnership.create） | C：对象保留，加 `owned_by`（→ Consumer，归属，many_to_one） |
| ConsumerOwnership | principal_id | principal | — | 同上 | B |
| ConsumerRoleLink | consumer_id / role_id | Consumer / RoleDefinition | 多→一 | ConsumerRoleLink.create/edit | C：加 `role_link_of`（归属）、`role_link_role` |
| ConsumerServiceOffering | consumer_id / offering_id | Consumer / ServiceOffering | 多→一 | ConsumerServiceOffering.create | C：加 `service_offering_of`（归属）、`service_offering_ref`。offering_revision 是快照，B |
| ConsumerIncentiveOffering | consumer_id / offering_id | Consumer / IncentiveOffering | 多→一 | 激励 Action（0145） | C：加 `incentive_offering_of`（归属）、`incentive_offering_ref` |
| RoleAssignmentScope | role_id | RoleDefinition | 多→一 | RoleAssignmentScope.create/edit | A `scope_of_role` |
| RoleAssignmentScope | consumer_id | Consumer | 多→一 | 同上 | A `scope_for_consumer`（归属） |
| RoleAssignmentScope | goal_ids / step_ids | Goal / PlanStep | id 数组 | 同上 | B（授权范围的取值集合，ADR-038 §3.3） |
| RoleDefinition | ceiling_ref | RoleExecutionCeiling（待核） | 多→一 | RoleDefinition.create | A `role_ceiling`，待核实 ref 格式 |
| RoleExecutionCeiling | consumer_ids / goal_ids / step_ids | — | id 数组 | RoleExecutionCeiling.create/edit | B（同上） |
| 各类 | source_principal / executor_principal / submitter_principals | principal | — | — | B |

**stage 只读计数**（调度员执行；inspector，连接后设为只读）。每一行字段一个结果，用于核实目标是否真是对象、id 形态，以及回填的规模：

```sql
with refs(type_name, prop, target) as (values
  ('Goal','consumer_id','Consumer'),('PlanStep','consumer_id','Consumer'),('PlanStep','goal_id','Goal'),('PlanStep','control_id','EffectControl'),
  ('EffectControl','consumer_id','Consumer'),('Conversation','consumer_id','Consumer'),('Message','conversation_id','Conversation'),
  ('Commitment','made_to','Consumer'),('Commitment','related_goal_ref','Goal'),('Commitment','supersedes','Commitment'),('Commitment','fulfillment_basis',null),
  ('CommercialRecord','consumer_ref','Consumer'),('RelationshipAssessment','evidence_message_id','Message'),
  ('MessageAssignment','message_id','Message'),('MessageAssignment','consumer_id','Consumer'),('MessageAssignment','goal_id','Goal'),
  ('MessageAssignment','step_id','PlanStep'),('MessageAssignment','control_id','EffectControl'),('ConsumerOwnership','consumer_id','Consumer'),
  ('ConsumerRoleLink','consumer_id','Consumer'),('ConsumerRoleLink','role_id','RoleDefinition'),('ConsumerServiceOffering','consumer_id','Consumer'),
  ('ConsumerServiceOffering','offering_id','ServiceOffering'),('ConsumerIncentiveOffering','consumer_id','Consumer'),
  ('ConsumerIncentiveOffering','offering_id','IncentiveOffering'),('RoleAssignmentScope','role_id','RoleDefinition'),
  ('RoleAssignmentScope','consumer_id','Consumer'),('RoleDefinition','ceiling_ref',null))
select r.type_name, r.prop, r.target, o.world,
  count(*) as objects,
  count(*) filter (where o.properties ? r.prop and o.properties->>r.prop is not null) as with_value,
  count(*) filter (where o.properties->>r.prop ~ '^[a-f0-9]{64}$') as hex64,
  count(*) filter (where r.target is not null and exists(select 1 from ontology.objects t where t.tenant_id=o.tenant_id and t.world=o.world
     and t.type_name=r.target and t.object_id=o.properties->>r.prop)) as target_exists_same_world,
  count(*) filter (where r.target is not null and exists(select 1 from ontology.objects t where t.tenant_id=o.tenant_id and t.world<>o.world
     and t.type_name=r.target and t.object_id=o.properties->>r.prop)) as target_in_other_world,
  count(distinct o.properties->>r.prop) as distinct_targets,
  count(*) filter (where o.properties->>r.prop is not null) - count(distinct o.properties->>r.prop) as sharing_a_target
from refs r join ontology.objects o on o.type_name=r.type_name
group by 1,2,3,4 order by 1,2,4;
```

再加两条：

- `select world, count(*) from ontology.objects group by 1;`
- `select type_name, count(*) from ontology.objects where tenant_id=<TENANT> group by 1 order by 1;`

判读：

- `with_value − target_exists_same_world` 是**悬空引用**数（目标不存在，或已被擦除删除）。回填时这些行不建关系，单独列出。
- `target_in_other_world > 0`：objects 主键不含 world，所以一个 id 只在一个 world 中存在，这一列表示“目标对象存在，但它的 world 与源对象不同”。这与 ADR-038 §2.4 冲突，要先查明再回填（S-1）。
- `sharing_a_target > 0`：有多个源对象指向同一个目标。对 many_to_one 是正常的；对定义为 one_to_one 的类型（首批只有 `supersedes`）则是基数冲突。ConsumerOwnership 那一行用来确认 I-4，预期大于 0。
- `hex64 < with_value` 说明 id 不是 64 hex，link 入口会拒绝，需要先决定格式（例如 `fulfillment_basis`、`ceiling_ref`）。

## 3. 关系类型定义

### 3.1 命名与定义格式

- 名称：`snake_case`，方向一律从“依附的一方”指向“被依附的一方”（source = 引用方，target = 被引用方），与引用属性的方向相同，因此回填不需要翻转。
- 定义（`relation_type_versions.definition`，v1）：

```json
{"relation_name": "commitment_to", "version": 1, "source_type": "Commitment", "target_type": "Consumer",
 "cardinality": "many_to_one", "inverse_name": "commitments", "description": "...",
 "source_property": "made_to", "consumer_ownership": "target", "properties": []}
```

  - `source_property`：双写期的对照属性，也是回填的来源。
  - `consumer_ownership`：`target` 表示“源对象属于目标 Consumer”；`transitive` 表示“源对象属于目标对象所属的 Consumer”（例如 `message_in`：Message → Conversation → Consumer）；不写表示与归属无关。
  - `properties` 为空：关系不带可编辑属性（ADR-038 §4）。
- 发布：随系统目录由 configurator 发布（`system_catalog.relation_types()`，0050 的同一套“只接受与已有定义完全相同的重发”规则），并为每个写关系的 Action 写 `control.nexloop_relation_action_bindings`。

### 3.2 首批关系类型（归属类在前）

| 名称 | 源 → 目标 | 基数 | 归属 |
| --- | --- | --- | --- |
| goal_of | Goal → Consumer | many_to_one | target |
| plan_step_of | PlanStep → Consumer | many_to_one | target |
| control_of | EffectControl → Consumer | many_to_one | target |
| conversation_of | Conversation → Consumer | many_to_one | target |
| message_in | Message → Conversation | many_to_one | transitive |
| commitment_to | Commitment → Consumer | many_to_one | target |
| commercial_record_of | CommercialRecord → Consumer | many_to_one | target |
| assignment_of | MessageAssignment → Consumer | many_to_one | target |
| owned_by | ConsumerOwnership → Consumer | many_to_one（I-4） | target |
| role_link_of | ConsumerRoleLink → Consumer | many_to_one | target |
| service_offering_of | ConsumerServiceOffering → Consumer | many_to_one | target |
| incentive_offering_of | ConsumerIncentiveOffering → Consumer | many_to_one | target |
| scope_for_consumer | RoleAssignmentScope → Consumer | many_to_one | target |
| assessment_evidence | RelationshipAssessment → Message | many_to_one | transitive |
| step_in_goal、step_under_control、commitment_for_goal、supersedes、assignment_for_message、assignment_goal、assignment_step、assignment_control、role_link_role、service_offering_ref、incentive_offering_ref、scope_of_role、role_ceiling | 见 §2 | many_to_one（supersedes 为 one_to_one） | 无 |

**待核**：

- RelationshipAssessment 的 source_id / target_id 是被评估关系的两端，类型由它评估的关系类型决定。首批**不**为它建关系，归属仍按 0315 解析这两个字段。如果以后要统一，就加 `assesses_source` / `assesses_target`，按端点类型分类型发布。
- `fulfillment_basis`、`ceiling_ref` 的取值要先用 §2 的 SQL 核实是否是对象 id。

### 3.3 归属的唯一来源（C-1、I-1）

- **链长由类型图计算**（C-1）：
  - 首批 `transitive` 链：
    - RelationshipAssessment →(assessment_evidence) Message →(message_in) Conversation →(conversation_of) Consumer，**3 跳**；
    - Message → Conversation → Consumer，2 跳；
    - 其余都是 1 跳。
  - 发布系统目录时，由 `system_catalog.relation_types()` 计算每个带标记的类型到 Consumer 的链长，要求链无环，并把最长链 L 写进目录清单（首批 L = 3）。
  - SQL 端按 L 递归，**不设固定的小上限**；运行时另设一个安全上限 8，只用来防止环。
- `runtime.nexloop_object_consumers(tenant, world, type, id)`（0315 引入）改为：
  1. 对象本身是 Consumer 时，返回它自己。
  2. 读该对象作为 source、带 `consumer_ownership` 标记的关系：`target` 直接给出 Consumer；`transitive` 则对目标对象递归，最多 L 跳。
  3. **双写期的读侧后备**：关系不存在时，按 0315 的属性规则解析，并记一个计数 `relation_fallbacks`，切读前要确认它为 0。
- **三处调用方，保守程度不同**（I-1）：
  - **读守卫**（`assert_not_erasing`）和 **`object_fields`**：只调这个函数。P5 之后只读关系。
  - **擦除 purge**：永久使用**并集**，即“以该 Consumer 为终点的归属关系链上的源对象”∪“0315 属性规则命中的对象”，不随 P5 移除属性规则。理由：关系缺失对读守卫是漏读，对 purge 是个人数据永久残留，而且事后无法找回；属性是已发布 Schema 的一部分，并且保留（ADR-038 §3 第 1 点），所以属性规则始终可用。
- **P1 结构测试**（C-1）：
  - 对每一个带 `consumer_ownership` 标记的首批关系类型，构造一条完整的链，断言 `nexloop_object_consumers` 解析出那个 Consumer；
  - 断言目录清单中的 L 等于测试中实际出现的最长链；
  - 新增带标记的关系类型而没有对应测试时，测试失败。

### 3.4 world 与删除 / 擦除语义

- **不跨 world**：
  - `nexloop_relation_put` 和 link 入口都核对两端 `o.world = 关系 world`。
  - 另加一条新迁移的约束触发器 `ontology.nexloop_relation_world_guard`（before insert/update on ontology.relations）：两端对象必须存在于同一 tenant、同一 world。这弥补了外键不含 world 的缺口。
- **删除端点对象**：现有的擦除 purge 已经先删两端是该对象的关系，再删对象，行为不变。正常业务里对象不会被物理删除，只改状态，关系保留。
- **引用改变**（例如 CommercialRecord 解除链接、ConsumerRoleLink 改 role）：在写属性的同一个 Action、同一事务中 `drop` 旧关系、`put` 新关系。
- **擦除 Consumer**：
  - blocking 起，读守卫通过归属关系把该 Consumer 的全部对象（直接和传递的）判为不可读，与今天 0315 的结果一致；
  - purge 的 references 阶段删除归属对象及其关系；Consumer 自身被删除前，删除所有以它为端点的关系（现有 0227 的语句）；
  - 不删除、只假名化的对象，要同时删除它的归属关系，因为属性改为假名后，关系不能再指向原 Consumer。触发器方案（§4.1）下这一点自动成立：属性一变，对应的关系就会被 drop。P2 要逐项核对擦除中所有“改属性、不删对象”的步骤（0305 到 0307，以及 0307 对 CommercialRecord 的重新推导），确认每一步都触发了 drop（S-6）。

## 4. 写入路径

### 4.1 同事务派生（I-2；ADR-038 §3 第 5 点，推荐方案 a）

- **方案 a（推荐）：由触发器派生。**
  - 在 `ontology.objects` 上加 AFTER INSERT OR UPDATE OF properties 触发器 `ontology.nexloop_relation_derive`（security definer，nexloop_owner 所有，search_path 固定）。
  - 对这个对象类型的每个关系类型（`source_type` 为该类型，并且带 `source_property`），比较新旧属性值：
    - 新值有值、并且目标对象在同一 tenant、同一 world 存在 → put；
    - 旧值与新值不同，或者新值被清空 → drop 旧的那条；
    - 目标不存在（悬空引用）→ 不建关系，计入 `relation_dangling` 计数（不报错：今天的引用属性允许悬空，例如擦除后留下的引用）。
  - **基数冲突时拒绝这次写入**（42501 `relation cardinality conflict`），不跳过。跳过会让关系缺失，在读守卫一侧造成漏读。存量冲突由回填的 dry-run 先列出来。
  - 幂等：同一对关系已存在时什么都不做；relation_id 的算法与 0016 相同。
  - 不变式：objects 只经受治理的定义函数写入，所以关系也只经受治理写入产生，这是结构保证；以后新增的写入路径也会自动派生关系。
  - **不改写任何已发布的定义函数**，复制件守护只需要登记 `nexloop_object_consumers` 的改写。
  - `change_scope.relation_types` 变成声明：0050 发布 Action 时，核对它与该 Action 所写对象类型的派生关系集合一致。
- **方案 b（不推荐）**：在每个写入的定义函数里，于写属性之后各自调用内部助手 `ontology.nexloop_relation_put/drop`。缺点：
  - 要改写 0046 系、object.create/edit、Commitment、商务链接、激励、RoleLink 等多个函数，每个都要登记复制件守护；
  - 以后新增的写入路径如果漏了 put，关系与属性会悄悄不一致，进而在读侧造成漏读。
- 无论采用哪个方案，都保留一个内部助手供回填和 unlink 使用，不授予任何应用角色。
- **重试**：对象写入本身已有 Action claim 幂等，重放时派生同样幂等。未知结果时按既有规则先查询、再对账，不盲目重试（AGENTS.md）。
- **与 0016 link 入口的关系**：首批关系类型都由派生产生，不绑定 link Action。0016 的“同一对、metadata 不同就报冲突”的检查只作用于经 link 入口写的关系，派生和回填的关系不经过它。

### 4.2 双写期与切读期

- **阶段 2（双写）**：所有新写入同时写属性和（派生的）关系；读方仍读属性。上线后，一个只读核对（§5 的 verify）每天比对“属性推导出的关系集合”与“实际关系集合”，差异必须为 0。
- **阶段 4（切读）**：读方改读关系；后备计数为 0 持续 7 天后，删除 0315 的属性后备（新迁移）。
- **冗余属性**：推荐保留（ADR-038 §3.1）。如果负责人决定去掉，需要发布每个类型的 v2（NX-040：去掉属性属于 major，首批通道不开放），这是另一个任务。

## 5. 存量回填

- **入口**：一个受治理的 Action `nexloop.relation.backfill:1`。
  - capability `ontology.relation.backfill`，low 风险，无审批，target postgres；change_scope 列出全部首批关系类型与端点类型。
  - **执行身份**（S-3；ADR-038 §3 第 4 点）：推荐一个专门的低权限 service principal，只被授予这一个 Action，回填完成后吊销。其他两个选项是 configurator（权限面更大）或负责人本人执行。
  - 命令行 `scripts/deploy/relation_backfill.py`，经私有配置，凭据走 stdin。
  - SQL 端是 `authz.nexloop_relation_backfill(digest, world, text, signature, payload)`：核对签名、Action 定义与绑定，然后按批（每批 500 个对象，一个短事务）对每个 `source_property` 有值、目标在同 world 存在的对象调用派生函数。触发器方案下，回填就是“对存量对象执行同一个派生函数”。
  - **不使用直接 SQL、superuser 或冒充身份。**
- **来源标记**（I-3）：
  - 回填**新建**的关系写 `metadata = {"backfill": "<run id>"}`，同时把它们的 relation_id 清单记进该次 Action claim 的 outcome，按批追加。
  - 遇到已存在的关系（双写或触发器已经建立）就什么都不做，**不打标记，也不进清单**。
  - 派生（触发器）建立的关系 metadata 为 `{}`。
- **与擦除的交互**（S-4）：
  - 回填跳过归属于正在擦除（state 不是 pending_confirmation）的 Consumer 的对象，判断用 `nexloop_object_consumers` 的属性规则；
  - 每批开始前重新读取擦除状态；
  - 回填与 purge 用同一把 Consumer 级咨询锁互斥，防止 purge 删除关系后回填又写回来（对象只被假名化、仍然存在时，外键挡不住这种情况）。
- **可重跑**：派生是幂等的；以 `(relation_name, 最后处理的 source_id)` 作游标，存进 claim 的 outcome，中断后从游标继续。
- **可校验**：`relation_backfill.py verify` 只读，对每个关系类型给出：属性有值且目标存在的源对象数、实际关系数、悬空引用数、跨 world 引用数、因擦除而跳过的数。前两个必须相等，其余单独列出，不建关系。
- **回滚**（I-3）：由同一身份执行 `nexloop.relation.backfill:1` 的 `mode: revert`，**只删除 metadata.backfill 等于该 run id、并且在该 run 的 claim 清单中的关系**，删除同样走受治理入口。双写或派生已建立的关系不受影响。P4 之前读方不依赖关系，回滚不影响业务。
- **演练**：先在本地栈，再在 stage 上演练。stage 依次执行：§2 的计数 SQL → dry-run（只计数，列出基数冲突）→ 正式执行 → verify → revert 演练一次 → 再执行一次。

## 6. 受影响的读方

| 读方 | 现状 | 改动 |
| --- | --- | --- |
| 世界模型邻居（0223、`workbench_reads.object_neighbors`） | 关系 + 引用属性，后者靠值的形态猜测 | 阶段 4 后只读关系（有方向、类型、基数）；引用属性的分组去掉。可见性仍是两端对象各自的成员读（ADR-025/035）；每个 25 上限与不可见计数不变 |
| 成员读权限（ADR-025/035） | 没有关系的读定义 | 不新增“读关系”权限：关系边只有在两端都可读时才显示，关系本身不带值。ADR-035 补一句说明 |
| 擦除传播（NX-029） | 各迁移按属性名找归属；0315 统一了读侧解析 | 读守卫和 `object_fields` 用 `nexloop_object_consumers`（P5 后只读关系）；purge 永久使用“关系 ∪ 属性”的并集（§3.3，I-1）；`test_erasure_whole_database_pg` 仍然是兜底 |
| 语义层 / E3 / EvoOntology | 不读关系 | 不变（ADR-038 §4）。语义 ref 不扩展到 `eios:relation:` |
| RelationshipAssessment（0058） | 按 relation_name / version 引用关系类型，version 0 表示无类型 | 首批发布关系类型后，新的评估可以引用真实的类型版本；已有的 version 0 评估不变 |
| S6 / X7 验收脚本 | `s6_stage_inputs.py` 读 `ontology.object_type_versions`；X7 的 nx043_stage 不读关系 | 不受影响；S6 的 R3 等只涉及语义读取 |
| 0315 的测试 `test_erasing_indirect_read_pg` | 按属性解析 | 阶段 4 改为“关系存在时按关系，不存在时按后备”，再加一个后备为 0 的断言 |

## 7. 分阶段实施计划

| 阶段 | 内容 | 上线后验收 | 估计 |
| --- | --- | --- | --- |
| P0 | 本设计 + ADR 拍板；stage 计数 SQL 结果入档 | 负责人拍板 §3 各项；计数表 | 0.5 天（本文） |
| P1 定义 | 系统目录 `relation_types()`；首批关系类型发布；`relation_world_guard` 触发器；`nexloop_relation_put/drop` 助手；unlink 入口（0016 模式）；`relation_action_bindings` 写入 | stage 发布后 `relation_type_versions` 有首批类型、`relations` 仍为 0；link/unlink 入口 PG 测试；跨 world 被拒；**结构测试**：每个带归属标记的类型都能解析到 Consumer，目录清单中的最长链 L 正确（C-1）；除 nexloop_owner 外没有角色能写这两张表（S-1） | 2–3 天 |
| P2 双写（派生） | 推荐方案 a：objects 上的派生触发器、dangling 计数、基数冲突拒绝写入、change_scope 声明核对；逐项核对擦除中“改属性、不删对象”的步骤触发了 drop（0305–0307）。方案 b 则要在各定义函数中调用 put 并逐一登记复制件守护 | 新建对象后关系数等于属性推导数；verify 差异为 0；全库擦除与复制件守护测试通过；**修改 real 写入路径，要经 ack-policy 和 stage 验收** | a：2–3 天；b：4–6 天 |
| P3 回填 | `nexloop.relation.backfill:1` + `relation_backfill.py`（dry-run / run / verify / revert）；来源标记；跳过正在擦除的 Consumer；与 purge 互斥 | stage verify：前两数相等；悬空与跨 world 单独列出；revert 只删回填标记的关系（演练一次） | 2–3 天 |
| P4 切读 | 读守卫和 `object_fields` 读关系（属性后备并计数）；邻居改读关系；purge 改为“关系 ∪ 属性”并集 | 后备计数为 0；0315 测试与全库擦除测试通过；世界模型邻居有关系分组；**修改 real 读取路径，要经 ack-policy 和 stage 验收** | 2–3 天 |
| P5 收尾（单向） | 移除读侧的属性后备（新迁移）；purge 的并集保留不动 | 前置条件：后备计数持续 7 天为 0、stage 每日 verify 为 0、全库擦除测试在“读侧只读关系”模式下通过。回退：用新迁移恢复后备；回退的观察指标是 verify 差异与 `relation_dangling` 计数（S-5） | 0.5–1 天 |

总计约 9–13 个工作日（方案 a；方案 b 约 11–16 天），单线。P1、P3 可以独立回滚（P3 依赖 I-3 的来源标记）；P2 回滚是去掉派生，已建立的关系可以保留，因为读方还不依赖它们；P4 回滚依赖读侧后备仍在；P5 是单向的。P1 可与 P0 的拍板并行准备，但不上线。

**需要负责人拍板的点**：共 7 个，选项、推荐与理由见 ADR-038 §3。

## 8. 风险与不做的事

- **触发器带来的写入失败**（方案 a）：派生出错或基数冲突会让这次对象写入失败。缓解：P3 的 dry-run 先清点存量冲突；首批只有 `supersedes` 是 one_to_one；派生函数配单元测试和 PG 测试。如果采用方案 b，风险换成“改写多个定义函数、产生过期副本，以及新增写入路径漏 put”。
- **基数冲突暴露存量脏数据**：例如同一个 ConsumerOwnership 指向两个 Consumer。回填会报基数冲突。缓解：P3 先 dry-run 并列出冲突，由负责人决定先修数据（经受治理 Action）再回填。
- **悬空引用**：被擦除删掉的目标留下了悬空属性。回填不建这些关系，单独列出；它们本身由擦除覆盖测试兜底。
- **性能**（S-6）：
  - 读守卫保留 0134 的快速路径：租户和 world 中没有正在擦除的 Consumer 时，只做一次索引探测，不解析归属，所以没有擦除时 real 读取的性能不变。
  - 有擦除时，每跳一次查询，走已有的 `ontology_relations_source(tenant_id, source_type, source_object_id)` 索引（0001），最多 L 跳。
  - 触发器在每次对象写入时多做一次按类型的关系类型查找；关系类型可以随会话缓存，P1 测量写入耗时。
- **表层外键不含 world**：靠触发器补上；触发器加在已有表上，属于新迁移，不改 0001。
- **RelationshipAssessment 已有的关系名**（S-6）：已有的评估中 version 0 的 relation_name 可能与首批关系类型重名。P1 发布前先只读统计 `select properties->>'relation_name', properties->>'relation_version', count(*) from ontology.objects where type_name='RelationshipAssessment' group by 1, 2`，有重名就改用不冲突的名字。
- **不做**：
  - 不开放 Schema 演进的 `add_relation_type`；
  - 不把 runtime/control 列改为关系；
  - 关系不带属性；
  - 不做多跳图查询；
  - 不让语义层或 EvoOntology 读关系；
  - 不改已发布的迁移。
