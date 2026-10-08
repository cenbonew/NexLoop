# ADR-019｜对话提取 → 混合召回 → 分层匹配 → 候选定义人工审核

状态：**采用**（负责人 2026-10-08 确认六项分叉决定）。
影响：部分修订 docs/handoff/docs/04 §4–§6、§9，docs/08 §6，ADR-017；新增契约 `candidate-definition`、`review-decision`；调整 NX-019/020/021/023/025/028，新增 NX-044/045/046，新增 AT-061～AT-070。
不改变：只有受治理 EIOS Action 能写正式业务对象；Schema 不自动发布；模拟/假设不升格为事实。

## 1. 背景与问题

负责人提供了一套“用户打标”业务流程（对话抽取 → 用户标签召回 → 标签匹配与生成 → 自主标签暂存 → 同义词粘合 → 人工审核 → 标签替换 → 入库回流）。NexLoop 的事实权威是本体中的**对象类型、对象实例、属性定义、属性值、关系**，没有独立的“标签”概念。原交接规格把实例变更设计为自动应用，把 Schema 变更整体放在 v0.2。两者需要合并为一条统一的处理链，并明确哪些步骤自动、哪些步骤人工。

## 2. 术语映射

| 打标流程概念 | NexLoop 本体概念 |
|---|---|
| 标签库 | 本体 Schema：对象类型、属性定义、属性词表（封闭枚举值）、别名映射 |
| 用户历史标签 | 该消费者已有对象实例及其属性值 |
| 自主生成标签 | **候选定义**（`candidate-definition`）：新对象类型 / 新属性 / 新词表值 / 新别名 / 新对象实例 |
| 同义词粘合 | 候选并入已有定义，记录别名映射 |
| 9 大类标签 | Consumer 类型下的属性分组：demographics、needs_intent、purchase_behavior、preference、pain_point、spending_power、sentiment_attitude、lifestyle、channel_tech。作为初始 Schema 的分组元数据，不硬编码在代码里 |
| 打标 | 对象实例属性值写入（受治理 Action，`ontology-mutation`） |
| 入库 | Schema 发布（人工批准后经 EIOS schema 注册链）＋等待中的实例变更应用 |

## 3. 处理链（规范）

### 3.1 对话抽取（NX-019）
1. 按话题切分；只保留顾客有实质回应的话题（负责人提供的话题抽取 prompt 作为起点，输出 `topic / conversation_summary / user_valid_reply`）。
2. 每个话题产出 Claim：subject、predicate、typed value、speaker、polarity、modality/condition、valid_time、source_span、extractor_version、confidence、`epistemic_kind`（docs/04 §4 九类）。
3. 显性/隐性分开：隐性推断一律 `epistemic_kind=hypothesis`。

### 3.2 混合召回（NX-021）
对每条 Claim 分三路召回，各取 top-k（初始 k=5，按测试调）：
- **类型与属性定义**：向量 + PostgreSQL FTS + pg_trgm，索引字段为名称、显示名、描述、别名、词表值。
- **已有对象实例**：先强标识精确匹配（已验证会话消费者、商品编号、订单号），再名称/别名/向量。
- **该消费者已有属性值**：作为上下文带入，不作为匹配目标。
所有召回先过 tenant / world / 属性权限过滤。Embedding 由 `.env` 的 `EMBEDDING_*` 配置（当前为火山方舟 doubao-embedding-vision 多模态端点）；NX-021 第一步必须实测返回维度并写入 `EMBEDDING_DIMENSION`，索引维度一经固定不得静默变更。

### 3.3 分层匹配与生成（NX-020）
模型在召回结果内判定每条 Claim 的四层落点：**对象类型 → 对象实例 → 属性定义 → 属性值**。判定结果只有三种：

| 判定 | 条件 | 动作 |
|---|---|---|
| **全匹配** | 类型、属性、（封闭词表时）值都在 Schema 内，且实例能唯一定位 | 生成 `ontology-mutation` 提案，**自动应用**，不经人工 |
| **部分匹配** | 开放型属性（数值、文本、日期）出现新值；或实例有强标识但库中不存在 | 自动应用：写新值 / 自动创建实例 |
| **不匹配** | 新类型、新属性、封闭词表的新值、仅有名称无强标识的新实例 | 生成候选定义 + 依赖它的 Claim，一起进暂存；Claim 状态 `needs_resolution` |

负责人确认的六项决定：
1. 开放型属性新值自动写；封闭词表属性新值走审核。
2. `hypothesis` 自动写入假设层，不进审核队列，不参与自动决策与规划的正式属性。
3. 新实例：有强标识自动建；只有名称进审核。
4. 审核粒度是**候选定义**；批准后所有依赖它的 Claim 自动应用，不逐条审 Claim。
5. 审核期间依赖的 Claim 只作为原文证据存在；Agent 规划时可见原文，不可当作正式属性；Context Engine 不把它们放进正式属性区。
6. 粘合特征第一版只实现可确定性计算的四项：词相似度、核心词包含、向量聚类相似度、业务规则白名单；其余（实体识别、结构、组合加权、词频共现、意图一致性、用户行为反馈）记为后续扩展位，接口预留，不实现。

### 3.4 候选粘合（NX-045）
1. 对候选定义再做一次精确比对，对象是同租户下同类（类型对类型、属性对属性、词表值对同属性词表）的已有定义。
2. 四项特征各出 0–1 分，加权后与阈值比较；权重与阈值是租户配置，初始值由合成数据集校准并写入配置版本。
3. 达阈值 → 候选 `merged`，记一条别名映射（候选文本 → 已有定义），依赖 Claim 改指向已有定义后走 3.3 的自动路径。
4. 未达阈值 → 候选 `pending_review`，进入审核队列。
5. 同一租户内文本相同的候选去重，合并依赖 Claim，不重复排队。

### 3.5 人工审核（NX-044、NX-046）
- 审核人是具备租户内 `ontology.schema.review` 权限的人类主体，身份由服务端会话解析；审核动作是受治理的人类 Action，有审计记录。
- 三种决定（`review-decision`）：
  - **approve**：经 EIOS schema 注册链真实发布新类型 / 属性 / 词表值；发布前执行 docs/08 §5 的硬门槛（引用有效、零越权、权限不变、迁移/索引兼容）；发布成功后应用全部等待中的 Claim；候选 `published`。
  - **merge_into**：等同 3.4 的粘合，记别名，Claim 改指向后自动应用；候选 `merged`。
  - **reject**：候选 `rejected`；依赖 Claim 保留为原文证据，`resolution_state=rejected_definition`，不进本体，不进 Context 正式属性；同文本候选在冷却期内不再排队。
- 发布失败（硬门槛不过）时候选回到 `pending_review` 并附失败原因，不自动重试。

### 3.6 回流
发布与合并结果写入召回索引（定义、别名、词表）；新实例进入实例索引。下一次对话先命中它们。AT-069 验证不再产生重复候选。

## 4. 状态机

候选定义：`staged → merged | pending_review`；`pending_review → published | merged | rejected`；任何状态可被 `superseded`（同文本候选合并）。
Claim 的 `resolution_state` 新增 `awaiting_definition`、`rejected_definition`。
实例提案生命周期（docs/04 §6）不变。

## 5. 对既有决定的修订

- **ADR-017**：v0.1 仍不启用**自动** Schema 发布；但**人工批准的** Schema 发布（新类型、属性、词表值、别名）进入 v0.1。自动兼容扩展仍在 S6（NX-040）。
- **docs/08 §6**：别名/映射更新与“只读 browse/resolve”一起进入 v0.1；候选评估仍在 v0.2。
- **docs/04 §6**：提案在 `proposed` 之前增加“分层匹配判定”；`needs_resolution` 细分为 `awaiting_definition` 等。
- **docs/04 §9**：三路分流保持，但“结构变化”多了一条人工批准入口。

## 6. 不做什么

- 不做逐条 Claim 审核，不做“全部先审再写”。
- 不让模型直接写 Schema；approve 只能由人类主体发出。
- 不把 `hypothesis` 和审核中的 Claim 当成正式属性。
- 不在 v0.1 做自动 Schema 发布、不做 Evo 候选评估驱动的发布。
- 模拟 / shadow 世界的候选不进入 real 世界的审核队列。

## 7. 验收

AT-061～AT-070（`planning/acceptance-tests.json`）。关键不可变条件：无审核的 Schema 变更为 0；审核中的 Claim 出现在正式属性或 Agent 决策依据中为 0；非授权主体的审核请求被拒。

## 8. 任务影响

修改：NX-019、NX-020、NX-021、NX-023、NX-025（依赖）、NX-028（依赖）。新增：NX-044（审批与 Schema 发布路径对齐）、NX-045（候选暂存、粘合与队列）、NX-046（最小审核工作台与回流）。NX-044 先对已完成的 NX-012 / NX-015 做差距检查，再改。
