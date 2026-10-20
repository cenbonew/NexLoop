# NX-040 设计稿：开放兼容 Schema 的受控演进

- **状态**：设计稿，只有文档，不写代码，不加迁移。**D21–D27 与 §7.3 的三处分歧已决定，全部按推荐（负责人 2026-10-20，原话“S6 全部按推荐”）。** 2026-10-20 已按 NX-039 设计稿（`l2/nx039-design` 7dc51f5a，§12「给 NX-040 的接口」）对齐，见 §7。
- **分支**：`l4/nx040-design`，从本地 `int/post-v010-batch2`（cbd41f57）切出。
- **依据**：
  - planning NX-040：S6，依赖 NX-039，模块 M11/M12，交付物是“迁移、索引、工具兼容，权限不变，破坏变更关闭”；
  - 08_EVOONTOLOGY_INTEGRATION §4「正式 Schema 路径」与 §6（v0.2b）；
  - 04_CONVERSATION_ONTOLOGY_PIPELINE §8、§9；
  - 14_TEST_ACCEPTANCE AT-023、AT-024、AT-048；
  - ADR-007、ADR-019；
  - NX-038 设计稿 §3.1、§3.2、§11.4、§12、D19；
  - AGENTS.md 的迁移规则：每个 schema 只有一个迁移 owner；已发布的 checksum 不变；ORM 不能自动建表或改表。
- **与 NX-039 的关系**：本稿引用 NX-039 设计稿（下称“39 稿”）§12 的六个接口：评估入口、门槛注册表、变更分级函数、发布许可、回退与删除、独立性。两稿结论冲突时以 39 稿为准，冲突本身写在 §7.3。

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
| 没有 “schema proposal” 表；语义版本目前只是一个计算出来的字符串 | 调查结论；39 稿 §7.1 | 需要新增 proposal 记录（§3）；语义发布（版本号、指针、发布项）由 NX-039 新增，只覆盖语义层（39 稿 §7.1） |
| Agent Host 的工具清单不由 Schema 生成 | 调查结论 | 工具兼容只需看 Action 契约和 Context 输出，不用看 Host 代码 |

## 3. 路径：从 Evo 候选到 EIOS 正式发布

```
NX-038 诊断 definition_gap
  → ontology_schema 候选（proposed；只是 proposal 的输入，D19）
  → Schema proposal（本任务新增：冻结的结构化变更 + 兼容性报告 + 权限证明）
  → NX-039 评估：同一评估包与报告契约、同一 evaluator 角色、同一候选状态机（39 稿 §12.1）；
    门槛版本 nx040-gates/1（G1–G8 沿用，启用 G9，新增 G10–G13，§4.6）；分级 classify_change（§4.1）
  → accepted_for_release → NX-039 签发发布许可 evolution_release_permits（39 稿 §12.4）
  → 发布决定（D22：人工审核；或白名单内的受限自动发布）
  → EIOS Schema 发布函数：核对并使用许可 → authz.nexloop_review_decide（现有门槛 + §4、§5 新增门槛）
    → object_type_versions 新行
  → ReviewReflowWorker 重流；Context 的 ontology_schema_revision 随之更新（不产生语义发布，§7.1）
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
4. **评估与许可**（39 稿 §12.1、§12.4）：
   - proposal 冻结后，对应的 `ontology_schema` 候选由 NX-039 开评。NX-040 不新增评估器角色，也不另建状态机：候选状态只有契约中的五种，proposal 自己只记录 `frozen`、`published`、`withdrawn`。
   - 开放 `ontology_schema` 维度的方式：NX-040 交付 G9–G13、负责人决定之后，用一个迁移修改 NX-039 的开放维度清单（数据库里一行可审计配置）。在此之前开评返回 `dimension_closed`。
   - 评估通过后，NX-039 签发一份发布许可（candidate_id、report_digest、分级、门槛版本、签发和到期时间）。EIOS Schema 发布函数执行前核对：许可存在、未过期、未使用；报告 digest 与 D4 关联表一致；分级不是 major。许可只能用一次，使用时写入 proposal。许可不授予任何 Action 权限。
5. **发布者**：
   - 默认由持有 `ontology.schema.review` 的人类审核后发布（ADR-019 现状）；
   - 受限自动发布是否开放，由 D22 决定（已定：首批不开放，全部人工发布），只适用于 §4.1 中标为“可自动”的操作。
6. **生效**：
   - Schema 版本发布后，Context 的 `ontology_schema_revision`（`contracts.py:944`）随之更新。
   - Schema 发布**不**产生语义发布，语义发布只含别名和补充说明（39 稿 §7.1）。新属性需要别名或补充说明时，另起 `semantic_content` 候选，走 NX-039 的语义发布。
   - 语义发布的回退不触及 Schema（39 稿 §12.5）。

## 4. 兼容性判定

proposal 进入评估前和发布前各判定一次。发布时 Parent 必须仍是最新版本，否则 proposal 作废、需要重新冻结。每个维度给出 pass 或 fail 和证据，任一 fail 都关闭这次发布。

### 4.1 变更白名单（其他一律关闭，§6）

| 操作 | 条件 | 分级（`classify_change`，39 稿 §12.3） | 可自动发布（D22） |
|---|---|---|---|
| `add_optional_property` | 属性名新，`required=false`，有默认值或允许为空，值类型 ∈ {string, integer, number, boolean, datetime}，不是封闭词表，`visibility` 不高于 normal | minor | 是 |
| `add_vocabulary_value` | 目标属性是已有的封闭词表；值是新的，并且不与已有的值或别名冲突 | minor | 是 |
| `add_object_type` | 新类型没有任何 Action，`only_edit_via_actions=true`，没有主键，没有关系；创建实例仍需后续受治理的 Action（另行发布，§5） | minor | 否（只能人工） |
| `add_relation_type` | — | refused（不在本任务范围，见 D24） | 否 |
| 白名单以外的一切 | §6 | major：一律关闭，候选转 `rejected/breaking_change`，不进审核队列 | — |

- 分级由函数决定并写入报告，人和 Agent 都不能手工改（39 稿 D39-7）。`ontology_schema` 维度没有 patch 级。

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
- **语义读取**：Parent 中所有可读的 ref，在新 eios revision 下仍然可以解析；当前语义发布的发布项（39 稿 §8.3）指向的 ref 全部仍然存在。

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

### 4.6 门槛版本 `nx040-gates/1`（39 稿 §12.2）

G1–G8 沿用 `nx039-gates/1`，语义不改；本稿启用 G9 并新增 G10–G13。签名统一为 `gate(bundle, parent_snapshot, candidate_snapshot) → {id, result, evidence_ids[]}`。

| ID | 门槛 | 对应本稿 |
|---|---|---|
| G9 | 索引与迁移影响：无 DDL；lineage 与 catalog 不变 | §4.2 |
| G10 | 对象迁移的影响有上限，且可以由下一个加法版本撤回其效果（§6）；对象只改 `schema_version` | §4.2、D25 |
| G11 | 索引重流的数量、时间和费用有上限；嵌入 profile 维度不变 | §4.3、D26 |
| G12 | 工具与 Context 契约兼容：后继 Action 的 schema_hash 不变，工具回归集结果一致 | §4.4、D23 |
| G13 | 授权事实与权限对象的 digest 前后不变 | §5 |

- G9–G12 的检查只在一次性测试库或 test world 中执行，不在 real 上试运行（39 稿 §12.6）。发布事务里只复核 G13 与 0082 的 authority 指纹。

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

- 白名单以外的任何操作由 `classify_change` 判为 major，候选转 `rejected`，原因码 `breaking_change`（39 稿 §7.2、§12.3），不进审核队列。
- 报告中附细分码，便于定位：
  - `breaking_remove_property`
  - `breaking_change_value_type`
  - `breaking_required_property`
  - `breaking_remove_vocabulary_value`
  - `breaking_primary_key`
  - `breaking_relation_endpoints`
  - `breaking_metric_definition`
  - `requires_ddl`
  - `authority_content`（同时触发 G2 fail）
- proposal 冻结时就能判定的，在冻结时直接拒绝，不占用评估预算。
- **人工维护路径**：沿用现有的 Schema 维护方式，即受信配置加负责人发布（`catalog_publish`），由负责人在计划窗口内处理，不在 NX-040 范围。

**回退**（39 稿 §12.5：Schema 回退走 EIOS 流程，不能借用 `SemanticRelease.rollback`）：

- 加法发布不能靠删除已发布的定义来回退，否则回退本身就是破坏性变更。
- **默认回退：下一个加法版本**。新属性在下一版本中标为 `visibility: hidden`，同时停止抽取侧对它的匹配（候选定义按拒绝处理）。这一版本经同一条 EIOS 发布链发布，不需要 DDL，已写入的数据保留。与 39 稿“新迁移”说法的差异见 §7.3 分歧 1。
- **真正删除**：只能走人工维护路径，并受 NX-029 的保留和删除规则约束。
- **不自动回退**（与 39 稿 D39-10 一致）：发布后观察 7 天；硬门槛指标劣化、抽取错误率上升超过 2 个百分点或 Context 体积超出上限时告警，并在工作台提示回退，由人决定。
- **删除传播**（39 稿 §9、§12.5）：许可只存 ID 与 digest；候选的来源 Consumer 被删除时，未使用的许可作废，proposal 转 `withdrawn`，原因 `missing_data`。

## 7. 与 NX-039 的对齐（39 稿 §12）

### 7.1 对应关系

| 39 稿接口 | 本稿采用 | 位置 |
|---|---|---|
| §12.1 同一套评估包、报告与状态机，同一个 evaluator 角色；`ontology_schema` 由 `dimension_closed` 拦下，开放方式是一个修改开放维度清单的迁移 | 采用。proposal 只有 `frozen`、`published`、`withdrawn`，评估状态都在候选上 | §3 第 4 步、§9 迁移 C |
| §12.2 门槛注册表 | 采用：`nx040-gates/1` = G1–G8 + G9–G13 | §4.6 |
| §12.3 分级函数，major 一律关闭 | 采用：三种白名单操作都为 minor，其余为 major（`breaking_change`），关系类为 refused | §4.1、§6 |
| §12.4 发布许可（只用一次，不授予权限） | 采用：EIOS Schema 发布函数核对并使用许可 | §3 第 4 步 |
| §12.5 Schema 回退不借用语义回退；删除后许可作废 | 采用；“新迁移”一词有分歧，见分歧 1 | §6 |
| §12.6 G10、G11 只在一次性测试库或 test world 中执行 | 采用，并扩展到 G9–G12 | §4.6 |
| §7.1 语义发布只覆盖别名和补充说明 | 采用：Schema 发布不产生语义发布；语义版本与 eios revision 相互独立 | §3 第 6 步 |
| D39-10 不自动回退 | 采用，原 D27 改为告警加人工决定 | §6、D27 |

### 7.2 原因码（与 39 稿共用）

| 原因码 | 何时返回 | 来源 |
|---|---|---|
| `dimension_closed` | `ontology_schema` 还没有开放 | 39 稿 §2.2 |
| `bundle_mismatch`、`candidate_not_ready`、`leakage_detected`、`holdout_exhausted`、`dimension_stopped`、`budget_exhausted` | 开评前复查 | 39 稿 §2.2 |
| `breaking_change` | 分级为 major | 39 稿 §7.2 |
| `parent_moved` | 发布时 Parent 已经不是最新版本，proposal 作废，需要重新冻结 | 39 稿 §8.2；本稿 §4 |
| `permit_missing`、`permit_expired`、`permit_used`、`permit_mismatch` | 发布时许可核对失败 | 本稿新增，接 39 稿 §12.4 |
| `missing_data` | 来源 Consumer 被删除，proposal 转 withdrawn，许可作废 | 39 稿 §9 |

### 7.3 与 NX-039 稿的三处分歧（已决定，负责人 2026-10-20）

下面三处都按本稿的推荐定案。各条先写结论，原来的分析保留在后面。

1. **“Schema 回退走 EIOS 迁移流程（新迁移）”**（39 稿 §12.5）。**结论（已决定，负责人 2026-10-20）：回退是“发布一个新的 Schema 版本”，不生成 SQL 迁移。L2 会相应修改 NX-039 稿 §12.5 的措辞。** 原分析：在 EIOS 中，Schema 是 `object_type_versions` 的数据行，不是 DDL。本稿的回退是“下一个加法版本”：经同一条发布链，不产生 SQL 迁移。按 AGENTS.md 的迁移规则，演进不应生成 SQL 迁移。**建议**：把 39 稿中的“新迁移”理解为“新的 Schema 版本”。负责人确认后，请 L2 改一个词即可。如果负责人要求的确实是 SQL 迁移，那么回退就不在演进范围内，只能走人工维护路径。
2. **`add_object_type` 的分级**。**结论（已决定，负责人 2026-10-20）：分级定为 minor；发布方式另行规定，只能人工发布（D22）。分级和发布方式是两件独立的事。** 原分析：39 稿 §12.3 建议“受约束的新类型”为 minor，本稿也采用 minor，但本稿把它限定为只能人工发布（D22），而 39 稿的分级本身不区分人工和自动。两者不冲突：分级（minor）与发布方式（只能人工）是分开的两件事（当时交负责人确认，已确认）。
3. **观察期阈值**。**结论（已决定，负责人 2026-10-20）：观察期阈值统一为 2 个百分点；Schema 变更另外监测抽取错误率。** 原分析：39 稿 D39-10 用的是“在线未命中率或歧义率劣化超过 2 个百分点”；本稿原 D27 用的是“抽取错误率上升超过 1 个百分点”。已按 39 稿改为 2 个百分点，但指标不同：Schema 变更主要影响抽取，所以本稿额外监测抽取错误率（当时交负责人确认，已确认）。

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
| 迁移 A | `ontology.nexloop_schema_proposals`（不可修改；有 Parent schema digest、结构化变更、来源 ID、兼容性报告 digest、权限证明 digest、状态只有 `frozen`、`published`、`withdrawn`，评估状态在候选上；`permit_ref` 记录所用的发布许可），附事件表；RLS 按租户；写入只通过 security definer 函数（演进写入角色），schema owner 仍是 nexloop_owner |
| 迁移 B | `ontology.nexloop_review_publication_gates` 增加 §4、§5 的门槛：白名单、权限 digest、后继 Action 一一对应、lineage 不变。用包装函数实现（参照 0103 的做法），不修改已发布的迁移 |
| 迁移 C | NX-038 E4 候选表的 dimension check 放开 `ontology_schema`，限于 proposal 输入（D21）；F4 再用一个迁移在 NX-039 的开放维度清单中加入 `ontology_schema`，并注册 `nx040-gates/1` |
| 契约 | `schema-proposal.schema.json`（proposal 的冻结内容）；`schema-compatibility-report.schema.json`（各维度 pass 或 fail、证据 ID、两份权限 digest）。两份契约都只用 ID、digest、代码和计数，单独 PR |
| 不改 | `evolution-candidate`、`ontology-mutation`、`context-manifest` |

所有迁移按惯例使用临时编号，由调度员在合并时重编。

## 10. 实施切片

| 切片 | 内容 | 依赖 | 估计 |
|---|---|---|---|
| F1 兼容性判定库 | 纯逻辑：白名单、§4.2–§4.4 的判定、拒绝码；基于 `assert_object_compatible` 扩展；单元测试覆盖每个拒绝码 | 无 | 1.5 天 |
| F2 proposal 记录与冻结 | 迁移 A；冻结函数；`ontology_schema` 窄口（迁移 C）；契约提案 | E4 | 2 天 |
| F3 发布门槛与权限证明 | 迁移 B；发布事务内的权限 digest；后继 Action 一一对应；AT-024 三处负例 | F1、F2 | 2 天 |
| F4 门槛注册、许可核对与观察期 | `nx040-gates/1` 注册（G9–G13）；发布函数核对并使用许可；回退用的 hidden 版本；7 天观察告警；工具回归集回放；打开 `ontology_schema` 维度的迁移 | NX-039 V1、V2、V4 | 2.5 天 |

合计约 8 个开发日。F1 可以马上开始；F2 和 F3 不依赖 NX-039 的实现；F4 依赖 NX-039 的评估器（V1、V2）和许可表（V4 迁移 C）。

## 11. 负责人决定的事项（已决定，负责人 2026-10-20：全部按推荐）

下表的“推荐”列就是定案内容。

| 编号 | 问题 | 推荐（已定案） | 理由 | 代价 |
|---|---|---|---|---|
| D21（已决定，负责人 2026-10-20） | `ontology_schema` 维度何时打开 | F2 只打开 proposal 输入窄口（E4 候选的 dimension check）；评估维度在 F4 交付 G9–G13 后，用一个迁移修改 NX-039 的开放维度清单（39 稿 §12.1）；之前开评返回 `dimension_closed` | 与 D19 一致：候选只是输入，不直接发布 | 多一个状态机 |
| D22（已决定，负责人 2026-10-20） | 是否开放受限自动发布 | 首批**不开**：全部由 `ontology.schema.review` 人工发布。运行一个月、没有回退后，再考虑对 `add_optional_property` 和 `add_vocabulary_value` 开放自动发布（负责人开关，默认关） | 加法变更风险低，但它是真实 Schema；先积累证据 | 人工审核量增加 |
| D23（已决定，负责人 2026-10-20） | 工具回归集 | 用冻结的合成用例：每个 Action 至少一个合法和一个非法参数用例，再加上 Context 体积基线；不使用真实消息 | 与 D16 一致；可以确定性回放 | 需要维护回归集 |
| D24（已决定，负责人 2026-10-20） | 是否包含 `add_relation_type` | 首批不包含 | 关系涉及端点、基数和召回关联，影响面大 | 关系类缺口要等更久 |
| D25（已决定，负责人 2026-10-20） | 一次发布的对象迁移上限 | 10 000 个对象；超过就分批，或转人工计划窗口 | 避免长事务；与短事务的规则一致 | 大租户需要分批 |
| D26（已决定，负责人 2026-10-20） | 重流的嵌入费用计入哪类预算 | 计入 `evolution`（D15），并单独设 proposal 级上限 | 演进带来的费用不挤占运营的 model 预算 | 演进预算消耗更快 |
| D27（已决定，负责人 2026-10-20） | 发布后的观察与回退 | 不自动回退（与 D39-10 一致）。发布后观察 7 天；硬门槛指标劣化、抽取错误率上升超过 2 个百分点或 Context 体积超出上限时告警并提示回退；回退是发布下一个 hidden 版本，由人决定 | 与 NX-039 一致，避免误报时来回抖动；加法 Schema 只能用下一版本撤回效果 | 劣化期间要等人处理；抽取错误率这一指标见 §7.3 第 3 条（已决定：统一 2 个百分点，另外监测抽取错误率） |

## 12. 不在本任务范围

- 破坏性变更的人工维护流程本身。
- 新类型的 Action 和授权的发布（负责人通过受信配置完成）。
- 工作台中的 proposal 展示：沿用审核队列，不新建页面。
- S7 的模拟和影子运行。
