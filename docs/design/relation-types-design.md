# 正式关系类型：设计稿（ADR-038）

状态：设计稿，供 L6 复核与负责人拍板。基于 main 0a7d7eef。本阶段只写设计，不改代码。

## 1. 事实核对：现有的关系能力

依据：NexLoop 迁移 0001、0016、0058、0223、0230、0132 起的擦除迁移、0315；vendored `packages/eios-core/src/eios`；EIOS 冻结提交 1e363db 的只读工作树。

| 能力 | 现状 | 缺口 |
| --- | --- | --- |
| 表 | `ontology.relation_type_versions(tenant_id, relation_name, version, definition)`，版本不可变（0016 的 `schema_version_immutable` 触发器），改动推进权限纪元。`ontology.relations(relation_id PK, tenant_id, world, relation_name, schema_version, source_type/id, target_type/id, metadata, created_at)`，唯一索引 `(tenant_id, world, relation_name, source, target)` | 端点外键是 `(tenant_id, type, object_id) → ontology.objects`，**不含 world**；world 隔离只在 link 函数里校验（`o.world=p_world`），表上没有约束 |
| 建立（受治理） | `authz.nexloop_link_relation_action`（0016）：签名命令、Action 定义 capability `ontology.relation.link`，要求 low 风险、无审批、无策略；`control.nexloop_relation_action_bindings` 把 Action 与关系类型版本绑定；两端各要一份编辑权限证明，并核对类型在 Action 的 change_scope 内；Action claim 用于幂等；relation_id 是 `sha256(tenant, world, name, source, target)`，确定性；同一对重复写入时 metadata 不同则报冲突；按 one_to_one / many_to_one / one_to_many 检查基数；用命名空间咨询锁串行化。Python 侧是 `nexloop_eios.relation_actions.GovernedRelationLinker`（`backend.link_relation`） | 端点 id 只接受 64 hex；**没有任何系统 Action 绑定关系**（系统目录 `system_catalog.py` 只发布 Object Type 和 Action），所以 stage 上是 0 行 |
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
| ConsumerOwnership | consumer_id | Consumer | 一→一 | 0046 commit_owner（ConsumerOwnership.create） | C：对象保留，加 `owned_by`（→ Consumer，归属） |
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
  count(distinct o.properties->>r.prop) as distinct_targets
from refs r join ontology.objects o on o.type_name=r.type_name
group by 1,2,3,4 order by 1,2,4;
```

再加两条：

- `select world, count(*) from ontology.objects group by 1;`
- `select type_name, count(*) from ontology.objects where tenant_id=<TENANT> group by 1 order by 1;`

判读：

- `with_value − target_exists_same_world` 是**悬空引用**数（目标不存在，或已被擦除删除）。回填时这些行不建关系，单独列出。
- `target_in_other_world > 0` 说明有跨 world 引用，与 ADR-038 §2.4 冲突，要先查明再回填。
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
| owned_by | ConsumerOwnership → Consumer | one_to_one | target |
| role_link_of | ConsumerRoleLink → Consumer | many_to_one | target |
| service_offering_of | ConsumerServiceOffering → Consumer | many_to_one | target |
| incentive_offering_of | ConsumerIncentiveOffering → Consumer | many_to_one | target |
| scope_for_consumer | RoleAssignmentScope → Consumer | many_to_one | target |
| assessment_evidence | RelationshipAssessment → Message | many_to_one | transitive |
| step_in_goal、step_under_control、commitment_for_goal、supersedes、assignment_for_message、assignment_goal、assignment_step、assignment_control、role_link_role、service_offering_ref、incentive_offering_ref、scope_of_role、role_ceiling | 见 §2 | many_to_one（supersedes 为 one_to_one） | 无 |

**待核**：

- RelationshipAssessment 的 source_id / target_id 是被评估关系的两端，类型由它评估的关系类型决定。首批**不**为它建关系，归属仍按 0315 解析这两个字段。如果以后要统一，就加 `assesses_source` / `assesses_target`，按端点类型分类型发布。
- `fulfillment_basis`、`ceiling_ref` 的取值要先用 §2 的 SQL 核实是否是对象 id。

### 3.3 归属的唯一来源

- `runtime.nexloop_object_consumers(tenant, world, type, id)`（0315 引入）改为：
  1. 读该对象作为 source、带 `consumer_ownership` 标记的关系：`target` 直接给出 Consumer；`transitive` 再递归一层（Message → Conversation → Consumer；RelationshipAssessment → Message → …）。递归深度上限 2。
  2. 对象本身是 Consumer 时，返回它自己。
  3. **双写期后备**：关系不存在时，按 0315 的属性规则解析，并记一个计数（`relation_fallbacks`），供切读前确认它为 0。
- 读守卫（`assert_not_erasing`）、`object_fields`、擦除 purge 的“哪些对象属于这个 Consumer”三处都只调这一个函数。擦除 purge 的 references 阶段（0227 的 `o.properties->>'consumer_id'=…` 那一段）改为“以该 Consumer 为 target 的归属关系的 source 对象”。

### 3.4 world 与删除 / 擦除语义

- **不跨 world**：
  - `nexloop_relation_put` 和 link 入口都核对两端 `o.world = 关系 world`。
  - 另加一条新迁移的约束触发器 `ontology.nexloop_relation_world_guard`（before insert/update on ontology.relations）：两端对象必须存在于同一 tenant、同一 world。这弥补了外键不含 world 的缺口。
- **删除端点对象**：现有的擦除 purge 已经先删两端是该对象的关系，再删对象，行为不变。正常业务里对象不会被物理删除，只改状态，关系保留。
- **引用改变**（例如 CommercialRecord 解除链接、ConsumerRoleLink 改 role）：在写属性的同一个 Action、同一事务中 `drop` 旧关系、`put` 新关系。
- **擦除 Consumer**：
  - blocking 起，读守卫通过归属关系把该 Consumer 的全部对象（直接和传递的）判为不可读，与今天 0315 的结果一致；
  - purge 的 references 阶段删除归属对象及其关系；Consumer 自身被删除前，删除所有以它为端点的关系（现有 0227 的语句）；
  - 不删除、只假名化的对象（例如 0307 对 CommercialRecord 重新推导），要同时删除它的归属关系（属性改为假名后关系不能再指向原 Consumer）。

## 4. 写入路径

### 4.1 同事务双写

- 新增两个只供内部调用的 SQL 助手：`ontology.nexloop_relation_put(tenant, world, relation_name, source_id, target_id)` 和 `ontology.nexloop_relation_drop(...)`。
  - owner 为 nexloop_owner，`revoke all from public`，**不授予任何应用角色**，只被受治理的定义函数调用。
  - 校验：关系类型的最新版本存在；端点类型与定义一致；两端同 world；基数（沿用 0016 的判断和命名空间锁）；relation_id 的算法与 0016 相同。
  - 幂等：同一对已存在就什么都不做。
- 各写入方在**同一函数、同一事务**里，于写对象属性之后调用：
  - 0046 系（Conversation、Message、ConsumerOwnership 的 prepare/commit）；
  - 通用 `ontology.object.create` / `object.edit` 路径：按 Action change_scope 声明的关系，从属性推导需要 put 或 drop 的关系；
  - Commitment 创建 / 更正；CommercialRecord 的链接 / 解除链接；激励（0145）；RoleLink edit。
  - 每个被改写的函数都要在复制件守护中登记 revert（只多出 put/drop 调用）。
- **Action 合同**：调用 put/drop 的 Action 在 `governance.change_scope` 中加 `relation_types`（名称 + 版本）。0050 发布时核对它与 `relation_action_bindings` 一致；没有声明就不能写关系（入口函数检查）。
- **重试**：
  - 对象创建本身已有 Action claim 幂等；重放时 put 也是幂等的，不会重复写。
  - 未知结果时按既有规则先查询再对账，不盲目重试（AGENTS.md）。

### 4.2 双写期与切读期

- **阶段 2（双写）**：所有新写入同时写属性和关系；读方仍读属性。上线后，一个只读核对（§5 的 verify）每天比对“属性推导出的关系集合”与“实际关系集合”，差异必须为 0。
- **阶段 4（切读）**：读方改读关系；后备计数为 0 持续 7 天后，删除 0315 的属性后备（新迁移）。
- **冗余属性**：推荐保留（ADR-038 §3.1）。如果负责人决定去掉，需要发布每个类型的 v2（NX-040：去掉属性属于 major，首批通道不开放），这是另一个任务。

## 5. 存量回填

- **入口**：一个受治理的 Action `nexloop.relation.backfill:1`。
  - capability `ontology.relation.backfill`，low 风险、无审批、target postgres。
  - change_scope 列出全部首批关系类型与端点类型。
  - 由 configurator（受信配置主体，与发布系统目录的是同一个主体）执行，命令行 `scripts/deploy/relation_backfill.py`，经私有配置，口令走 stdin。
  - SQL 端是 `authz.nexloop_relation_backfill(digest, world, text, signature, payload)`：核对签名、Action 定义与绑定，然后**按批**（每批 500 个对象、一个短事务）对每个 `source_property` 有值、目标在同 world 存在的对象调用 `nexloop_relation_put`。
  - **不使用直接 SQL、superuser 或冒充身份**。
- **可重跑**：put 是幂等的；以 `(relation_name, 最后处理的 source_id)` 作游标，存进 Action claim 的 outcome，中断后从游标继续。
- **可校验**：`relation_backfill.py verify` 只读，对每个关系类型给出四个数：属性有值且目标存在的源对象数、实际关系数、悬空引用数、跨 world 引用数。前两个必须相等，后两个单独列出，不建关系。
- **回滚**：回填只增加关系、不改对象。回滚就是由同一主体执行 `nexloop.relation.backfill:1` 的 `mode: revert`，按回填记录的 relation_name 批量删除，删除也走受治理入口。阶段 4 之前，读方不依赖关系，回滚不影响业务。
- **演练**：先在本地栈，再在 stage 上演练。stage 先跑 §2 的计数 SQL，再 dry-run（只计数），再正式执行，最后 verify。

## 6. 受影响的读方

| 读方 | 现状 | 改动 |
| --- | --- | --- |
| 世界模型邻居（0223、`workbench_reads.object_neighbors`） | 关系 + 引用属性，后者靠值的形态猜测 | 阶段 4 后只读关系（有方向、类型、基数）；引用属性的分组去掉。可见性仍是两端对象各自的成员读（ADR-025/035）；每个 25 上限与不可见计数不变 |
| 成员读权限（ADR-025/035） | 没有关系的读定义 | 不新增“读关系”权限：关系边只有在两端都可读时才显示，关系本身不带值。ADR-035 补一句说明 |
| 擦除传播（NX-029） | 各迁移按属性名找归属；0315 统一了读侧解析 | 读守卫、`object_fields`、purge 共用 `nexloop_object_consumers`（§3.3）；`test_erasure_whole_database_pg` 仍然是兜底 |
| 语义层 / E3 / EvoOntology | 不读关系 | 不变（ADR-038 §4）。语义 ref 不扩展到 `eios:relation:` |
| RelationshipAssessment（0058） | 按 relation_name / version 引用关系类型，version 0 表示无类型 | 首批发布关系类型后，新的评估可以引用真实的类型版本；已有的 version 0 评估不变 |
| S6 / X7 验收脚本 | `s6_stage_inputs.py` 读 `ontology.object_type_versions`；X7 的 nx043_stage 不读关系 | 不受影响；S6 的 R3 等只涉及语义读取 |
| 0315 的测试 `test_erasing_indirect_read_pg` | 按属性解析 | 阶段 4 改为“关系存在时按关系，不存在时按后备”，再加一个后备为 0 的断言 |

## 7. 分阶段实施计划

| 阶段 | 内容 | 上线后验收 | 估计 |
| --- | --- | --- | --- |
| P0 | 本设计 + ADR 拍板；stage 计数 SQL 结果入档 | 负责人拍板 §3 各项；计数表 | 0.5 天（本文） |
| P1 定义 | 系统目录 `relation_types()`；首批关系类型发布；`relation_world_guard` 触发器；`nexloop_relation_put/drop` 助手；unlink 入口（0016 模式）；`relation_action_bindings` 写入 | stage 发布后 `relation_type_versions` 有首批类型、`relations` 仍为 0；link/unlink 入口 PG 测试；跨 world 被拒 | 2–3 天 |
| P2 双写 | 各写入方同事务 put/drop（0046 系、object.create/edit、Commitment、CommercialRecord link、激励、RoleLink）；复制件守护逐一登记；每日 verify | 新建对象后关系数 = 属性推导数；verify 差异为 0；全量擦除与复制件守护测试通过 | 4–6 天（函数多，登记多） |
| P3 回填 | `nexloop.relation.backfill:1` + `relation_backfill.py`（dry-run / run / verify / revert）；本地栈、stage 演练 | stage verify：前两数相等；悬空与跨 world 单独列出；回滚演练一次 | 2–3 天 |
| P4 切读 | `nexloop_object_consumers` 读关系（属性后备并计数）；邻居改读关系；purge references 阶段改读关系 | 后备计数为 0 持续 7 天；0315 测试与全库擦除测试通过；世界模型邻居有关系分组 | 2–3 天 |
| P5 收尾 | 删除属性后备（新迁移）；是否去掉冗余属性另行拍板 | 后备代码不存在；擦除测试通过 | 0.5–1 天 |

总计约 11–16 个工作日，单线。P1 可与 P0 的拍板并行准备，但不上线。

**需要负责人拍板的点**：

1. 冗余属性是否保留（推荐保留）。
2. 哪些连接对象保留为对象（推荐五个都保留）。
3. id 数组字段不改为关系（推荐不改）。
4. 回填执行身份（推荐 configurator + 专门的回填 Action）。
5. RelationshipAssessment 的 source/target 首批不建关系（推荐首批不建）。
6. 后备观察期 7 天是否足够。

## 8. 风险与不做的事

- **定义函数改写面大**：P2 要改写 0046 系、object.create/edit、Commitment、商务链接等多个定义函数，每个都要登记复制件守护，最容易出现过期副本。缓解：每个函数只多出 put/drop 一行调用，按函数分提交，每个都配 revert。
- **基数冲突暴露存量脏数据**：例如同一个 ConsumerOwnership 指向两个 Consumer。回填会报基数冲突。缓解：P3 先 dry-run 并列出冲突，由负责人决定先修数据（经受治理 Action）再回填。
- **悬空引用**：被擦除删掉的目标留下了悬空属性。回填不建这些关系，单独列出；它们本身由擦除覆盖测试兜底。
- **性能**：读守卫在有擦除中 Consumer 的租户里，每次读多一到两次关系索引查询（`(tenant, world, relation_name, source_type, source_object_id)` 已有唯一索引覆盖，但这个索引的列顺序以 relation_name 开头）。可能需要补 `(tenant_id, world, source_type, source_object_id)` 索引，P1 一并评估。
- **表层外键不含 world**：靠触发器补上；触发器加在已有表上，属于新迁移，不改 0001。
- **不做**：
  - 不开放 Schema 演进的 `add_relation_type`；
  - 不把 runtime/control 列改为关系；
  - 关系不带属性；
  - 不做多跳图查询；
  - 不让语义层或 EvoOntology 读关系；
  - 不改已发布的迁移。
