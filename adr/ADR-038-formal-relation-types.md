# ADR-038｜对象之间的关键关联改为 EIOS 正式关系类型

状态：**提议**。方向由负责人 2026-10-22 定为 C；本 ADR 的具体取舍（§3 的 7 个拍板点）等负责人确认后改为“采用”。

关联：

- 设计稿 `docs/design/relation-types-design.md`（事实核对、逐条清单、关系类型定义、写入/回填/读方、分阶段计划、风险）；
- 安全不变式“只有受治理的 EIOS Action 可改正式业务对象”（AGENTS.md）；
- L6 复核 `relation-types-adr-review.md`（59552198）的 C-1、I-1 到 I-4、S-1 到 S-6，已在本修订中处理；
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
2. **关系只经受治理的写入产生**：
   - `ontology.objects` 本身只能经受治理的定义函数写入。关系由这次对象写入在**同一事务**内派生，写入方式见 §3 第 5 点（推荐由 objects 上的触发器按关系类型定义的 `source_property` 派生）。
   - 已有的独立入口 `authz.nexloop_link_relation_action`（0016，capability `ontology.relation.link`）保留，用于单独建立关系的 Action。
   - NexLoop 目前没有 unlink 入口：上游 EIOS 有 `ontology.relation.unlink`，但 vendored 的 Python 适配器引用的 `ontology.app_relation_unlink` 在 NexLoop 迁移中不存在。按 0016 的模式补一个。
   - P1 加一条结构测试：除 nexloop_owner 外，任何角色对 `ontology.relations` 和 `ontology.relation_type_versions` 都没有 INSERT、UPDATE、DELETE 权限。0001 对 `nex_eios_app`（如果这个角色存在）有一段条件授权，测试同样覆盖它。
3. **归属只有一个定义来源，读守卫与 purge 的保守程度不同**：
   - 关系类型定义里的 `consumer_ownership` 标记（`"target"`：源对象属于目标 Consumer；`"transitive"`：源对象属于目标对象所属的 Consumer）是“这个对象属于哪个 Consumer”的定义。
   - **读守卫和 `object_fields`** 通过 `runtime.nexloop_object_consumers` 读这些关系，沿传递链一直走到 Consumer。链长由关系类型图计算，不设固定的小上限；首批最长的链是 RelationshipAssessment → Message → Conversation → Consumer，共 3 跳。
   - **擦除 purge 永久使用“关系 ∪ 属性”的并集**，不随后备的移除而改变：关系缺失对读守卫意味着漏读，对 purge 意味着个人数据永久残留，所以 purge 必须保守。
   - 0315 的按属性解析在读侧只作为双写期的后备，切读后移除。
4. **不跨 world**：关系的两个端点与关系本身必须在同一 tenant、同一 world；在 SQL 助手和表约束两层强制（见设计稿 §3.4）。
5. **分阶段**：先定义与双写，再回填，然后切读，最后（另行拍板）决定是否去掉冗余的引用属性。每一阶段可以独立上线、独立验收。

## 3. 待负责人拍板

每一点：选项 → 推荐 → 理由。

1. **冗余引用属性是否最终去掉**
   - 选项：保留；或发布各类型的 v2 并去掉这些属性。
   - 推荐：**保留**。
   - 理由：已发布的 v1 不可变，去掉属性属于 NX-040 的 major 变更，而首批通道不开放 major；擦除 purge 永久需要属性作为并集的一半（§2.3）。切读后，属性只作兼容输出，不再作为判断依据。
2. **哪些连接对象保留为对象**
   - 选项：保留为对象并另加关系；或把连接对象改成关系后删除对象。
   - 推荐：MessageAssignment、ConsumerRoleLink、ConsumerServiceOffering、ConsumerIncentiveOffering、ConsumerOwnership **五个都保留为对象**，并另加关系。
   - 理由：它们各有自己的属性和生命周期（revision、active、valid_until、state）。ConsumerOwnership 的 principal 不是正式对象，只能作为属性。
3. **id 数组字段是否改为关系**
   - 涉及字段：RoleExecutionCeiling 和 RoleAssignmentScope 的 consumer_ids、goal_ids、step_ids。
   - 推荐：**不改**。
   - 理由：它们是授权范围的取值集合，不是对象之间的关联；改成关系会让授权的正确性依赖关系表的完整性。
4. **回填的执行身份**
   - 选项：(a) configurator 加专门的回填 Action；(b) 一个专门的低权限 service principal，只被授予回填这一个 Action；(c) 负责人本人执行。
   - 推荐：**(b)**。
   - 理由：configurator 是发布系统目录、签发凭据的受信配置主体，让它批量写业务关联会扩大它的权限面。(b) 的权限只有回填一项，可以在回填完成后吊销，而且不需要人工逐条操作。
5. **关系怎样随对象写入产生**
   - 选项：
     - (a) 由 `ontology.objects` 上的**提交时执行的约束触发器**（DEFERRABLE INITIALLY DEFERRED，security definer，nexloop_owner 所有）派生，规则是关系类型的 `source_property`：属性有值就 put，属性变化或清空就 drop。另加一个 BEFORE DELETE 触发器，删除对象之前先删以它为端点的关系。
     - (b) 在每个写入的定义函数里各自调用 put/drop。
   - 推荐：**(a)**。
   - 理由：
     - objects 只经受治理的定义函数写入，所以“关系只经受治理写入产生”这一点由结构保证；以后新增写入路径也不会漏写关系。
     - 不改写已发布的定义函数，复制件守护的工作量几乎为零。
     - Action 合同里的 `change_scope.relation_types` 改为声明，由发布时核对。
   - 代价：
     - 派生出错会让提交失败。
     - 基数冲突时拒绝提交（42501），不跳过，因为跳过会留下缺失的关系，在读守卫一侧造成漏读。首批关系类型全部是 many_to_one，`supersedes` 也已改为 many_to_one，所以**没有现有写入会因此失败**。只有以后发布 one_to_one 类型、而存量数据违反该基数时才会失败，所以发布前要先计数确认。
     - 有删除触发器后，只删对象、不删关系的现有清理（例如 0129 的 CommercialRecord 保留期清理）不会再被外键挡住。
     - 每次对象写入在提交时多做一次关系派生，P1 会测量 sim 数据生成和擦除批量假名化的开销。
6. **RelationshipAssessment 的 source_id / target_id 首批是否建关系**
   - 推荐：**首批不建**。
   - 理由：这两个字段的端点类型取决于它所评估的关系类型，要按端点组合分别发布；这两个字段的归属仍按属性解析。证据消息改为经 `assessment_evidence` 关系，沿传递链走到 Consumer。
7. **切读后的观察期**
   - 内容：读侧后备计数持续为 0 多少天后，才移除后备。
   - 推荐：**7 天**，同时要求 stage 上每日 verify 为 0、全库擦除测试在只读关系的模式下通过。
   - 理由：移除后备是单向的一步，回退要用新迁移恢复后备（设计稿 §7）。
## 4. 不做的事

- 不开放 NX-040 的 `add_relation_type`（D24 不变）；关系类型只由系统目录发布。
- 不把 runtime / control 表的 consumer_id 等列改成正式关系：它们不是正式业务对象，擦除仍按 NX-029 各表的规则处理。
- 不给关系实例加可编辑的属性（metadata 维持为空对象），也不做多跳查询或图计算。
- 不改已发布的迁移（published.json 冻结）；所有变更走新迁移，并登记复制件守护。
