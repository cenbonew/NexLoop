# ADR-038｜对象之间的关键关联改为 EIOS 正式关系类型

状态：**提议**。方向由负责人 2026-10-22 定为 C；本 ADR 的具体取舍（§3 标“待拍板”的各条）等负责人确认后改为“采用”。

关联：

- 设计稿 `docs/design/relation-types-design.md`（事实核对、逐条清单、关系类型定义、写入/回填/读方、分阶段计划、风险）；
- 安全不变式“只有受治理的 EIOS Action 可改正式业务对象”（AGENTS.md）；
- ADR-025 / ADR-035（工作台成员读与审计）、NX-029（擦除）、NX-040 D24（Schema 演进首批不开放 `add_relation_type`）；
- 迁移 0001（`ontology.relation_type_versions`、`ontology.relations`）、0016（关系 link 的受治理入口）、0223 / 0230（世界模型 W2 邻居）、0315（擦除期间的间接归属解析，分支 l5/erasing-indirect-read）。

## 1. 背景

stage（release 36 前后，调度员只读）有 20 个 Object Type，但 `ontology.relation_type_versions` 和 `ontology.relations` 都是 0 行。对象之间的关联全部靠三种方式表达：

1. **引用属性**：string 属性里存另一个对象的 id，类型描述为空，例如 `Commitment.made_to → Consumer`、`Goal.consumer_id → Consumer`、`PlanStep.goal_id → Goal`、`Message.conversation_id → Conversation`；
2. **连接对象**：ConsumerOwnership、ConsumerRoleLink、MessageAssignment、ConsumerServiceOffering、ConsumerIncentiveOffering；
3. **runtime / control 表里的列**（consumer_id 等），不是正式对象。

后果：

- 世界模型 W2 的邻居（0223）只能靠“引用属性”启发式地猜关联（值看起来像另一个对象的 id），没有方向、基数和类型的正式定义；
- 擦除（NX-029）和读守卫各自按属性名解析“这个对象属于哪个 Consumer”，已经出现过分叉（0314 → 0315 才补上 Commitment、CommercialRecord、RelationshipAssessment 等间接归属）；
- 关联没有统一的权限、审计和 world 隔离语义。

## 2. 决定（方向 C）

1. **关键关联以正式关系表达**：在 `ontology.relation_type_versions` 发布关系类型，在 `ontology.relations` 存关系实例。关系类型是系统目录的一部分，与系统 Object Type 一样由受信配置（configurator）发布，**不**经 NX-040 的 Schema 演进入口（D24 维持不变）。
2. **只有受治理的 Action 写关系**：
   - 新建或修改一个带引用的对象时，**同一个受治理 Action、同一事务**内写对应的关系。具体做法是一个只供受治理定义函数内部调用的 SQL 助手 `ontology.nexloop_relation_put` / `ontology.nexloop_relation_drop`：不授予任何应用角色，只校验关系类型、端点类型、world 和基数。调用它的 Action 必须在其 `change_scope` 里声明该关系类型。
   - 已有的独立入口 `authz.nexloop_link_relation_action`（0016，capability `ontology.relation.link`）保留，用于单独建立关系的 Action。NexLoop 目前没有 unlink 入口（上游 EIOS 有 `ontology.relation.unlink`，vendored 的 Python 适配器引用的 `ontology.app_relation_unlink` 在 NexLoop 迁移中不存在），按同样的模式补一个 `ontology.relation.unlink` 入口。
3. **归属只有一个来源**：关系类型的定义带 `consumer_ownership: true` 标记的，就是“这个对象属于哪个 Consumer”的唯一依据。读守卫（`runtime.nexloop_assert_not_erasing`）、`object_fields` 和擦除 purge 都改为通过 `runtime.nexloop_object_consumers` 读这些关系。0315 的按属性解析只在双写期内作为后备，切读后删除。
4. **不跨 world**：关系的两个端点与关系本身必须在同一 tenant、同一 world；在 SQL 助手和表约束两层强制（见设计稿 §3.4）。
5. **分阶段**：先定义与双写，再回填，然后切读，最后（另行拍板）决定是否去掉冗余的引用属性。每一阶段可以独立上线、独立验收。

## 3. 待负责人拍板

1. **冗余引用属性是否最终去掉。** 推荐**保留**，作为已发布 Schema 的一部分：去掉属于 NX-040 的 breaking change（major），而且已发布的 v1 不可变。读方切到关系以后，属性只作兼容输出，不再作为判断依据。
2. **哪些连接对象保留为对象。** 推荐：
   - MessageAssignment、ConsumerRoleLink、ConsumerServiceOffering、ConsumerIncentiveOffering **保留为对象**，并加关系。它们有自己的属性和生命周期（revision、active、valid_until、state）。
   - ConsumerOwnership **保留为对象**，并加 `owned_by` 关系；principal 不是正式对象，所以 `principal_id` 仍然只是属性。
3. **RoleExecutionCeiling / RoleAssignmentScope 的 id 数组**（consumer_ids、goal_ids、step_ids）**不改成关系**。它们是授权范围的取值集合，不是对象之间的关联；改成关系会让授权依赖关系表的完整性。
4. **回填的执行身份。** 推荐由 configurator 以一个专门的受治理 Action `nexloop.relation.backfill:1` 执行：低风险、不需审批、可重放，只写关系，不写对象。不用直接 SQL。

## 4. 不做的事

- 不开放 NX-040 的 `add_relation_type`（D24 不变）；关系类型只由系统目录发布。
- 不把 runtime / control 表的 consumer_id 等列改成正式关系：它们不是正式业务对象，擦除仍按 NX-029 各表的规则处理。
- 不给关系实例加可编辑的属性（metadata 维持为空对象），也不做多跳查询或图计算。
- 不改已发布的迁移（published.json 冻结）；所有变更走新迁移，并登记复制件守护。
