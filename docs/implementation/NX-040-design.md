# NX-040 设计稿：开放兼容 Schema 的受控演进

- **状态**：设计稿，只有文档，不写代码，不加迁移。D21–D27 待负责人决定。
- **分支**：`l4/nx040-design`，从本地 `int/post-v010-batch2`（cbd41f57）切出。
- **依据**：
  - planning NX-040：S6，依赖 NX-039，模块 M11/M12，交付物是“迁移、索引、工具兼容，权限不变，破坏变更关闭”；
  - 08_EVOONTOLOGY_INTEGRATION §4「正式 Schema 路径」与 §6（v0.2b）；
  - 04_CONVERSATION_ONTOLOGY_PIPELINE §8、§9；
  - 14_TEST_ACCEPTANCE AT-023、AT-024、AT-048；
  - ADR-007、ADR-019；
  - NX-038 设计稿 §3.1、§3.2、§11.4、§12、D19；
  - AGENTS.md 的迁移规则：每个 schema 只有一个迁移 owner；已发布的 checksum 不变；ORM 不能自动建表或改表。
- **与 NX-039 的关系**：NX-039 设计稿由 L2 同时起草，其中有一节“给 NX-040 的接口”。本稿涉及这一接口的地方都标了 **【待对齐 NX-039】**。L2 交付后按它的接口逐条对齐，届时不改本稿已定的边界。

## 1. 目标与非目标

### 目标

- 把 NX-038 `ontology_schema` 维度的诊断产出（`definition_gap`：抽取侧反复出现、审核又拒绝合并的未匹配定义）变成一份 **EIOS Schema proposal**。
- 让它走 EIOS 已有的正式发布链：
  - 先做兼容性、迁移、索引、工具和权限检查；
  - 通过 NX-039 的独立评估；
  - 再经人工或受限的自动发布生效；
  - 最后刷新投影和索引。
- 只开放**加法兼容**的变化（08 §6 v0.2b）：
  - 给已有类型新增可选属性；
  - 给已有封闭词表新增值；
  - 新增一个不带任何 Action 的受约束类型。
- 每次发布都给出机器可核对的证明：已有对象和工具调用仍然有效，权限前后完全相同。

### 非目标

- 不做破坏性变更：删除类型或属性、改值类型、把属性改成必填、删词表值、改主键、改关系端点或基数、改指标口径。这些仍走人工维护路径（§5），NX-040 只负责关闭入口并给出明确的拒绝码。
- 不做权限演进：Action 定义、授权事实、service grant、角色、工具授权、联系约束一律不在演进范围（AT-024；NX-038 §11.4）。
- 不做数据库 DDL：演进不生成、也不运行任何 SQL 迁移。Schema 在 EIOS 中是数据（`ontology.object_type_versions` 的新行），不是表结构。
- 不改写实时事实：Claim 和实例更正仍走实时路径（08 §4），不受评分门槛约束。
- 不处理模拟世界和影子运行（S7）。

## 2. 现状（2026-10-20，cbd41f57）

| 事实 | 位置 | 对本设计的意义 |
|---|---|---|
| 类型版本按 (tenant, type_name, version) 存储，行不可改，写入会推进 authority epoch | `0001_durable_core.sql:122`；`0009_action_schema_bundle.sql:2-12` | 新版本就是新增一行，旧版本保留，可用于对比和回退判断 |
| 属性和词表值存在 `definition` jsonb 中，没有单独的表 | `0082_nx044_review_actions.sql:93-113` | 兼容性在定义层判定，不需要 DDL |
| 人工审核发布链：候选定义（0070）→ 审核决定（0082）→ `authz.nexloop_review_decide` 跑门槛 `nexloop_review_publication_gates`，再插入新版本和后继 Action，把对象迁到新版本 | `0082:67`、`:154`、`:224-234`；`review_actions.py:77,107`；`0103` 包装 | NX-040 的发布**复用这条链**，不另建一条 |
| 发布必须保持权限不变：发布前后比较租户 authority 指纹，变了就拒绝 | `0082:235-236` | 这是“权限不变”现成的第一道证明 |
| 加法兼容检查 | `eios/ontology/semantics.py:225` `assert_object_compatible`（主键不变，属性只能加）；`schema_contract_digest` `:105` | 兼容性判定的核心，NX-040 在外层补齐其余维度 |
| 发布后的重流 | `review_actions.py:211` `ReviewReflowWorker`；`0082:261` `authz.nexloop_review_reflow` | 索引和投影刷新的现成入口 |
| 迁移校验：catalog sha256 不符时报 `MigrationDrift`，lineage 必须完全一致 | `nexloop_eios/bootstrap.py:6-31` | “不新增 DDL”可以机器核对：发布前后 `control.schema_migrations` 相同 |
| Action 的契约快照不可变，契约变化必须换 capability_version 和 Action 版本 | `catalog_manifests.py:8,41,88`；`capabilities.v1.json` 的 decision 注释 | 工具兼容按 schema_hash 判定 |
| 没有 “schema proposal” 表；没有语义快照导出，也没有语义版本标识 | 调查结论 | 需要新增 proposal 记录（§3）；语义版本由 NX-039 定义【待对齐 NX-039】 |
| Agent Host 的工具清单不由 Schema 生成 | 调查结论 | 工具兼容只需看 Action 契约和 Context 输出，不用看 Host 代码 |

## 3. 路径：从 Evo 候选到 EIOS 正式发布

```
NX-038 诊断 definition_gap
  → ontology_schema 候选（proposed；只是 proposal 的输入，D19）
  → Schema proposal（本任务新增：冻结的结构化变更 + 兼容性报告 + 权限证明）
  → NX-039 独立评估（硬门槛 + 留出集）→ accepted_for_release      【待对齐 NX-039】
  → 发布决定（D22：人工审核；或白名单内的受限自动发布）
  → authz.nexloop_review_decide（现有门槛 + 本稿 §4 的新增门槛）→ object_type_versions 新行
  → ReviewReflowWorker 重流 → 新语义版本引用新的 eios revision          【待对齐 NX-039】
```

1. **候选**：NX-038 E4 现在只开放 `semantic_content`，SQL 拒绝其他维度。NX-040 实施时只为 `ontology_schema` 打开一个**窄口**：
   - 候选的补丁只能是 §4.1 白名单内的一种结构化变更；
   - `allowed_change_scope` 只能是 `eios:object_type:` 或 `eios:property:` 开头的 ref；
   - 不含任何 Action、授权或工具内容（AT-024，E4 的 scope 检查已经覆盖）。
2. **Schema proposal**（新增记录，§9 迁移 A）：
   - 冻结 Parent 的 schema digest（`schema_contract_digest`）；
   - 结构化变更：类型名、操作、属性定义，或要加的词表值；
   - 来源：候选 ID、NX-038 诊断 digest、依赖的候选定义 ID 和 Claim ID（只存 ID）；
   - 兼容性报告（§4）和权限证明（§5）的 digest。
   - proposal 不可修改，内容变了就新建一份。
3. **与 ADR-019 的候选定义合流**：proposal 发布时生成或复用一条 `ontology.nexloop_candidate_definitions`，kind 为 property、vocabulary_value 或 object_type，来源标为 `evolution`。然后走与人工审核**同一个** `authz.nexloop_review_decide`，所以：
   - 对象迁移、重流、挂起 Claim 的恢复，全部复用现有代码；
   - 人工审核队列里能看到这条 proposal，附评估报告 digest。
4. **发布者**：
   - 默认由持有 `ontology.schema.review` 的人类审核后发布（ADR-019 现状）；
   - 受限自动发布是否开放，由 D22 决定，只适用于 §4.1 中标为“可自动”的操作。
5. **生效顺序**：
   - 先发布 Schema 版本，再由语义层发布引用这个 eios revision 的新语义版本；
   - Context 的 `ontology_schema_revision` 和 `semantic_snapshot_ref`（`contracts.py:944,955`）随之更新；
   - 语义版本回退不撤销 Schema 发布（§6）。

## 4. 兼容性判定

proposal 进入评估前和发布前各判定一次。发布时 Parent 必须仍是最新版本，否则 proposal 作废、需要重新冻结。每个维度给出 pass 或 fail 和证据，任一 fail 都关闭这次发布。

### 4.1 变更白名单（其他一律关闭，§6）

| 操作 | 条件 | 可自动发布（D22） |
|---|---|---|
| `add_optional_property` | 属性名新，`required=false`，有默认值或允许为空，值类型 ∈ {string, integer, number, boolean, datetime}，不是封闭词表，`visibility` 不高于 normal | 是 |
| `add_vocabulary_value` | 目标属性是已有的封闭词表；值是新的，并且不与已有的值或别名冲突 | 是 |
| `add_object_type` | 新类型没有任何 Action，`only_edit_via_actions=true`，没有主键，没有关系；创建实例仍需后续受治理的 Action（另行发布，§5） | 否（只能人工） |
| `add_relation_type` | — | 否（不在本任务范围，见 D24） |

### 4.2 迁移

- **不产生 DDL**：proposal 只产出一行 `object_type_versions` 和后继 Action 版本。发布前后 `control.schema_migrations` 和 catalog 必须完全相同，`bootstrap.verify` 通过。
- **已发布的 checksum 不变**；任何 ORM 都不会被调用来建表或改表。
- **对象迁移**：现有对象从 vN 迁到 vN+1（`0082:231-234` 的做法）。
  - 判定内容：每个对象按新定义校验都有效（加法变更下应当恒成立）；
  - 迁移只改 `schema_version`，不改属性值；
  - 抽样 digest 前后对比，只允许 `schema_version` 不同。
- **规模上限**：一次发布最多迁移 N 个对象（D25），超过就分批，或转人工计划窗口。

### 4.3 索引

- **召回索引**：
  - 新属性或新词表值的定义条目需要重流（`ReviewReflowWorker`）；
  - 判定内容：嵌入 profile 的维度不变；预计新增的向量数和费用都在上限内（计入 NX-038 D15 的 evolution 预算，还是计入 model 预算，见 D26）。
- **数据库索引**：`ontology.objects` 的属性存在 jsonb 中，加法变更不需要新索引。如果 proposal 声明需要新的表达式索引，就判为破坏性，关闭，转人工 DDL 维护。
- **语义快照**：新版本的语义快照必须能由新 eios revision 完整生成，而且旧快照的所有 ref 在新快照中仍然可以解析。【待对齐 NX-039：快照格式与生成函数】

### 4.4 工具签名

- **Action 契约**：
  - 后继 Action 只允许 `object_types` 的版本号变化，输入 schema 和 capability 的 schema_hash 必须保持不变；
  - 已有 Action 的输入、输出与 effect 范围都不能变；
  - 用 `catalog_publish.diff` 的冲突规则核对：同名同版本内容不同时拒绝。
- **Agent 可见的工具**：Host 的工具清单不由 Schema 生成，所以工具名和参数不受影响。
  - 判定内容：对冻结的工具回归集（D23），在新、旧两个 eios revision 下各回放一次 Context 与工具参数校验，结果必须一致；
  - 新属性只能出现在读结果中，不能成为必填参数。
- **Context 输出**：`context-manifest` 的结构不变，只更新 `ontology_schema_revision` 的值。

### 4.5 契约

- `packages/contracts/` 的现有契约一律不改：
  - `ontology-mutation` 的操作集合不变；
  - `evolution-candidate` 的 `dimension: ontology_schema` 已经存在。
- 新增的两个契约见 §9，只是提案。

## 5. 权限不变的证明

发布事务中，同时满足以下四条才提交：

1. **authority 指纹不变**：沿用 `0082:235-236`，发布前后租户 authority 指纹必须相等，否则拒绝（“publication must not change authority”）。
2. **权限对象 digest 不变**：对以下表在发布前后各算一次 digest，必须相等：
   - `authz.nexloop_authority_facts`
   - `authz.nexloop_service_grants`
   - 工作台角色映射
   - 联系约束表
   - `control.nexloop_action_definitions` 中**非后继**的行
   - 两份 digest 写进 proposal 的权限证明。
3. **后继 Action 的映射是一一的**：每个后继 Action 恰好对应一个旧 Action，并且只是 `object_types` 的版本号 +1，不新增 Action，不提高 risk_level，不新增 effect 类别。
4. **新类型没有任何 Action**（`new_type_publishes_no_actions`，`0082` 已有这条门槛）。新类型的 Action 和授权由负责人通过受信配置另行发布，即“定义与权限分开发布”（AT-024）。

测试形态：

- **AT-024 负例**：构造“新增类型并附带提权 Action”的 proposal，在三个位置都被拒绝：候选写入、proposal 冻结、发布门槛。
- **正例**：一次加法发布前后，上面四条全部可以复算。

## 6. 破坏性变更关闭与回退

**关闭**：

- 白名单之外的任何操作，在 proposal 冻结时就被拒绝，并给出固定的拒绝码，例如：
  - `breaking_remove_property`
  - `breaking_change_value_type`
  - `breaking_required_property`
  - `breaking_remove_vocabulary_value`
  - `breaking_primary_key`
  - `breaking_relation_endpoints`
  - `breaking_metric_definition`
  - `requires_ddl`
  - `authority_content`
- 这些码计入 NX-038 诊断的 `marked` 计数，并在审核队列中显示为“需要人工维护”。系统不会自动替人发起维护。
- **人工维护路径**：沿用现有的 Schema 维护方式，即受信配置加负责人发布（`catalog_publish`），由负责人在计划窗口内处理，不在 NX-040 范围。

**回退**（加法变更的回退不能删除已经发布的定义，否则回退本身就成了破坏性变更）：

- **语义层回退（默认）**：
  - 把语义版本回退到上一个版本，于是新属性或新值不再出现在 Context 和语义快照里；
  - Schema 版本保留，已经写入的数据也保留。
  - 这一步由 NX-039 的回退入口执行。【待对齐 NX-039】
- **写入停用**：
  - 新属性可以在下一版本中标为 `visibility: hidden`；这也是加法变更，可以走同一条链；
  - 同时停止抽取侧对它的匹配（候选定义按拒绝处理）。
- **真正删除**：只能走人工维护路径，并且受 NX-029 的保留和删除规则约束。
- **回退窗口**：发布后 D27 天内，如果出现回退触发条件，就自动执行语义层回退，并通知负责人：
  - 硬门槛指标劣化；
  - 抽取错误率上升；
  - Context 体积超出上限。

## 7. 与 NX-039 语义版本的对应关系【整节待对齐 NX-039】

| 概念 | 本稿假设 | 对齐时要确认的事 |
|---|---|---|
| 语义版本 | 内容是 (eios revision digest、别名与映射集合、描述集合) 的组合，有单调递增的编号 | 编号格式；它与 `semantic_snapshot_ref` 的关系 |
| 一次 Schema 发布 | 产生新的 eios revision；随后发布一个新语义版本引用它。一个 eios revision 可以被多个语义版本引用 | 由谁触发新语义版本（NX-039 的发布入口，还是 NX-040 发布后自动触发） |
| 评估 | proposal 作为 `ontology_schema` 候选，由 NX-039 评估：硬门槛加上 §4、§5 的报告 digest | 评估包里如何携带兼容性报告与权限证明；L0 能否评估结构变化 |
| 回退 | 语义版本回退不撤销 Schema；Schema 不提供自动回退 | NX-039 回退时，对“引用了新 Schema 字段的语义条目”如何处理 |
| 冻结 | proposal 冻结 Parent 的 schema digest；评估期间 Parent 有新发布时，proposal 作废 | NX-039 的冻结条件中是否包含 eios revision |

## 8. S6 验收覆盖

| 验收 | 怎么覆盖 | 测试形态 |
|---|---|---|
| AT-023 候选隔离 | proposal 在发布前只存在于 proposal 表和候选表中，`object_type_versions`、对象、别名与 Action 的 digest 都不变；评估失败的 proposal 不留下任何正式变化 | PG：冻结并评估一份 proposal（评估结果为 reject），受保护表的 digest 前后相等 |
| AT-024 结构权限 | §5 的四条；新类型不带 Action；后继 Action 一一对应，并且只改版本号 | PG：提权负例在三个位置被拒绝；正例的权限 digest 前后相等 |
| AT-048 验证泄漏 | `ontology_schema` 候选的用例分组沿用 E4 的簇切分（同一 Consumer 不跨设计集和留出集） | 复用 E4 的单元测试与 SQL 检查 |
| NX-040 交付物 | 迁移：lineage 不变；索引：重流量在上限内，profile 不变；工具：回归集在新旧 revision 下结果一致；破坏性变更：每个拒绝码一个负例 | PG 与单元测试；工具回归集由 D23 决定 |

## 9. 契约与迁移（只写提案）

| 项 | 内容 |
|---|---|
| 迁移 A | `ontology.nexloop_schema_proposals`（不可修改；有 Parent schema digest、结构化变更、来源 ID、兼容性报告 digest、权限证明 digest、状态 `frozen → evaluating → accepted_for_release / rejected / incomplete → published / withdrawn`），附事件表；RLS 按租户；写入只通过 security definer 函数（演进写入角色），schema owner 仍是 nexloop_owner |
| 迁移 B | `ontology.nexloop_review_publication_gates` 增加 §4、§5 的门槛：白名单、权限 digest、后继 Action 一一对应、lineage 不变。用包装函数实现（参照 0103 的做法），不修改已发布的迁移 |
| 迁移 C | NX-038 E4 候选表的 dimension check 放开 `ontology_schema`，限于 proposal 输入（D21） |
| 契约 | `schema-proposal.schema.json`（proposal 的冻结内容）；`schema-compatibility-report.schema.json`（各维度 pass 或 fail、证据 ID、两份权限 digest）。两份契约都只用 ID、digest、代码和计数，单独 PR |
| 不改 | `evolution-candidate`、`ontology-mutation`、`context-manifest` |

所有迁移按惯例使用临时编号，由调度员在合并时重编。

## 10. 实施切片

| 切片 | 内容 | 依赖 | 估计 |
|---|---|---|---|
| F1 兼容性判定库 | 纯逻辑：白名单、§4.2–§4.4 的判定、拒绝码；基于 `assert_object_compatible` 扩展；单元测试覆盖每个拒绝码 | 无 | 1.5 天 |
| F2 proposal 记录与冻结 | 迁移 A；冻结函数；`ontology_schema` 窄口（迁移 C）；契约提案 | E4 | 2 天 |
| F3 发布门槛与权限证明 | 迁移 B；发布事务内的权限 digest；后继 Action 一一对应；AT-024 三处负例 | F1、F2 | 2 天 |
| F4 重流、回退与语义版本衔接 | 重流费用上限；语义层回退入口对接；工具回归集回放 | NX-039 | 2 天【待对齐 NX-039】 |

合计约 7.5 个开发日。F1 可以马上开始；F2 和 F3 不依赖 NX-039 的实现；F4 等 NX-039 的接口。

## 11. 待负责人决定的事项

| 编号 | 问题 | 推荐 | 理由 | 代价 |
|---|---|---|---|---|
| D21 | `ontology_schema` 维度何时打开 | NX-040 的 F2 只打开 proposal 输入窄口；proposal 必须经 NX-039 评估后才可能发布 | 与 D19 一致：候选只是输入，不直接发布 | 多一个状态机 |
| D22 | 是否开放受限自动发布 | 首批**不开**：全部由 `ontology.schema.review` 人工发布。运行一个月、没有回退后，再考虑对 `add_optional_property` 和 `add_vocabulary_value` 开放自动发布（负责人开关，默认关） | 加法变更风险低，但它是真实 Schema；先积累证据 | 人工审核量增加 |
| D23 | 工具回归集 | 用冻结的合成用例：每个 Action 至少一个合法和一个非法参数用例，再加上 Context 体积基线；不使用真实消息 | 与 D16 一致；可以确定性回放 | 需要维护回归集 |
| D24 | 是否包含 `add_relation_type` | 首批不包含 | 关系涉及端点、基数和召回关联，影响面大 | 关系类缺口要等更久 |
| D25 | 一次发布的对象迁移上限 | 10 000 个对象；超过就分批，或转人工计划窗口 | 避免长事务；与短事务的规则一致 | 大租户需要分批 |
| D26 | 重流的嵌入费用计入哪类预算 | 计入 `evolution`（D15），并单独设 proposal 级上限 | 演进带来的费用不挤占运营的 model 预算 | 演进预算消耗更快 |
| D27 | 自动回退的窗口与触发条件 | 发布后 7 天；触发条件是硬门槛指标劣化、抽取错误率上升超过 1 个百分点、Context 体积超出上限。只回退语义层，并通知负责人 | 加法 Schema 不能真正撤销，语义层回退是安全的最小动作 | 可能出现误回退，需要人工复核 |

## 12. 不在本任务范围

- 破坏性变更的人工维护流程本身。
- 新类型的 Action 和授权的发布（负责人通过受信配置完成）。
- 工作台中的 proposal 展示：沿用审核队列，不新建页面。
- S7 的模拟和影子运行。
