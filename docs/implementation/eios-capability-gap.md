# EIOS 能力差距分析：企业世界模型与数据资产（2026-10-20）

- **状态**：分析文档，不改代码。D-G1–D-G6 已决定（负责人 2026-10-20：全部按推荐）。实施分工：D-G1 由 L2 负责，D-G5 由 L3 负责，D-G2 由 L4 负责。
- **分支**：`l4/eios-gap-analysis`，基点 `origin/main` 7a153a29。
- **核对对象**：
  - 上游 EIOS 冻结版本：只读工作树，即 `versions.lock.json` 中记录的冻结提交；只引用路径，不复制代码；
  - NexLoop vendored 核心：`packages/eios-core/src/eios`；
  - NexLoop 后端：`packages/eios-core/src/nexloop_eios`；
  - 工作台：`apps/web/src/workbench`。
- **说明**：本文“上游位置”中的路径都是相对上游仓库根目录的路径。上游的领域代码（场馆、赛事、对象存储适配器一类）只被识别为“领域专属”，没有阅读和引用其内容。

## 结论（先读这里）

1. **负责人的判断是对的：EIOS 的能力确实没有全部用起来。** 但缺口分成性质完全不同的三类，不能一概补齐：

   | 类别 | 能力 | 判断 |
   |---|---|---|
   | A. 已 vendored，但没接通 | Object Set、Object View、Interface（核心里只有 Python 契约，NexLoop 没有表、读取或页面）；Object 的属性值（工作台只显示 ID 与 revision） | **可以补，风险低**。不引入新的数据源，也不改变写入路径 |
   | B. 有意保持只读 | 企业世界模型里不能编辑；Object、Link 的编辑只走受治理 Action | **保持现状**。这是 0230 / W 设计 D5 的定案，也符合“只有受治理 Action 能改业务对象” |
   | C. 根本没有 vendored | 数据集、数据管道（transform 与投影）、连接器与同步、数据健康、Lineage | **不应该直接搬**。上游的数据集绑定会把数据集行投影写入 `ontology.objects`，绕过受治理 Action；连接器会引入新的外部数据源和凭据边界。这些都与 NexLoop 的固定决定冲突，要引入必须先有 ADR |

2. **推荐的优先级**：
   - **P1（引入）**：Object 属性值的只读展示（按属性安全过滤）；Object Set（只读、按读取时求值，用于目标和外联人群的定义与预览）。
   - **P2（引入）**：数据健康，以 NexLoop 自己的队列、重流和投递为对象，不引入上游的 pipeline 报告；Object View，只做只读展示。
   - **延后**：Interface、Function 结果缓存、Lineage。
   - **先写 ADR 再决定**：数据集加投影管道（D-G3）、通用连接器（D-G4）。
   - **不引入**：上游的领域连接器、会话代理登录（session broker）、对象存储预签名下载（与 ADR-015 本地 Artifact 首发不符）。
3. 推荐路径合计约 **9–11 个开发日**（P1 + P2）。C 类如果负责人决定引入，另需 ADR 和约 **12–18 个开发日**。

## 1. 逐项核对表

图例：✅ 有；◐ 部分；— 没有。行号以 2026-10-20 的代码为准。

### 1.1 企业世界模型

| 能力 | 上游位置 | 已 vendored？ | 已接通？ | 差距说明 |
|---|---|---|---|---|
| Object Type | `src/eios/ontology/models.py:485`；`0001_durable_core.sql`（`ontology.object_type_versions`）；页面 `apps/portal/src/features/world-model/concept-page.tsx` | ✅ 同名模型与表（`eios/ontology/models.py:485`，`0001:122`） | ✅ 只读：`authz.nexloop_workbench_world_model`（0230），表格或关系图 | 只读是定案（D5）；结构变化走候选审核（ADR-019）和 NX-040 |
| Object | `ontology/store.py`、`read_models/objects.py`；`api/objects.py`（详情、子图） | ✅ `ontology.objects`（`0001:147`） | ◐ 0223 的 `objects` / `object_neighbors`，经过审计的成员读取检查；**页面只显示 ID、revision 和邻居，不显示属性值** | 属性值展示是真正的缺口（D-G1） |
| Property | `models.py:373`；属性安全 `0075_property_security.sql` | ✅ `models.py:373`、`authz/property_security.py` | ✅ 只读列表；owner 限制的属性组只显示名称并标 withheld | 没有缺口 |
| Link | `models.py:554`；`relation_type_versions`、`relations`；`0019_relation_lifecycle`、`0026_relation_valid_time` | ✅ 类型与实例表（`0001:130,160`）；有效时间的扩展迁移没有带入 | ✅ 类型列表 + 邻居图；编辑走受治理的关系 Action（0016） | 关系的有效时间（bitemporal）没有带入。NexLoop 用 Claim 的 valid_time 表达时效，暂不需要 |
| Function | `definitions.py:1601`；`0020_function_result_cache.sql` | ✅ 定义；`control.nexloop_function_definitions`（0048） | ✅ 只读列表 | 结果缓存没有带入，延后 |
| Action | `definitions.py:1657`；`api/actions.py:105`；审批流 | ✅ 定义、治理与写入尝试（`eios/actions/*`） | ✅ 世界模型的 Action 页、能力目录的业务 Action 页，执行在 Actions 页 | 没有缺口 |
| Object Set | `definitions.py:1781`；`ontology/object_set_execution.py:281`；`object_set_revision_store.py`；`0009`（`object_set_revisions`）、`0028`；`GET /api/object-sets/{name}/{version}/objects` | ◐ 只有定义契约（`definitions.py:1781`）；**执行器、revision 存储和表都没有带入** | — | 读取时求值的保存查询，可用于目标人群、外联对象与审核范围（D-G2） |
| Object View | `definitions.py:1864`；`.../resolved`；`features/objects/view-renderer.tsx` | ◐ 只有定义契约 | — | 只是展示布局，价值在工作台的对象详情（P2） |
| Interface | `definitions.py:1908`；`.../resolved` | ◐ 只有定义契约 | — | 多个类型共享形状时才需要；目前的租户类型少，延后 |

### 1.2 数据资产

| 能力 | 上游位置 | 已 vendored？ | 已接通？ | 差距说明 |
|---|---|---|---|---|
| 数据集 | `src/eios/datasets/models.py:198`（Descriptor）、`:274`（Commit）；`0016_dataset_assets.sql`（`integration.datasets` 等）；`api/data_layer.py`；`features/data-assets/dataset-*-page.tsx` | — | — | 带 schema 的事务型表，提交以内容 digest 标识。NexLoop 没有“数据集”这一层（D-G3） |
| 数据管道 | `datasets/transform.py:526`（Source / Filter / Select / Derive / Join / Aggregate / Union / Output）、`build.py:115,599`、`transform_registry.py`；`connectors/pipeline.py:359,490`；`0027`、`0029`、`0017`；`api/transform_builder.py`；`features/data-assets/pipeline-page.tsx`（新建投影管道） | — | — | 在进程内用 Python 执行。**增量构建在上游也只是预留接口**（`build.py` 第 11 行注释写的是“future”），实际以全量为主。数据集到对象的绑定（`ontology/binding.py`，`0032_dataset_bindings.sql`）会把行投影写入对象（D-G3） |
| 连接器与同步 | `src/eios/connectors/`：通用的 rest、file、structured、xlsx 来源；调度 `scheduling.py:73,151,469`；凭据 `secrets.py:61,191,227`；会话代理；另有多个领域连接器；`0011`、`0018`、`0034`、`0036`、`0117` | — | ◐ NexLoop 只有签名的商业事件接入（`control.nexloop_commercial_connectors`，0118，由部署配置），不是通用连接器 | 新的外部数据源、凭据边界和调度器（D-G4） |
| 数据健康 | `connectors/expectations.py`、`read_models/data_layer.py`；`0017`（`integration.data_health_reports`）；`GET /api/data/health`、`/api/objects/{type}/{id}/health`；`health-page.tsx` | — | ◐ 0225 在 metrics 快照里加了 `data_assets`，只有计数与队列状态；页面对后端没提供的项显示“不可用” | 上游的健康报告依附于 pipeline 和连接器；NexLoop 需要的是自己数据流的健康（D-G5） |
| Artifact | `read_models/artifacts.py`、`api/artifacts.py`（列表、详情、presign）；`0001`、`0005`、`0006`；对象存储依赖 | ✅ `runtime/artifacts.py:830`、`runtime.artifact_references`；NexLoop 本地 Artifact（`local_artifacts.py`、`postgres_artifacts.py`、`artifact_orphans.py`） | ✅ 0226：只有元数据，按页列出，每条记入读取审计，删除中的 Consumer 隐藏 | 没有详情页和下载。下载会把内容带出工作台，需要单独的审计和权限设计；预签名的对象存储不引入（ADR-015） |
| Lineage（横切） | `src/eios/lineage/models.py:69,102,146`；`0024`、`0123` | — | — | 依附于数据集和管道；NexLoop 的证据链由 Claim 的 evidence、上下文来源和效应收据承担，延后 |

## 2. 逐项判断

### 2.1 A 类：已 vendored，但没接通

**Object 属性值（D-G1，推荐引入，P1）**
- **符合性**：只读展示，不改变写入路径。属性安全（`authz/property_security.py`）和成员读取派生（ADR-025、ADR-035）已经存在。只需要在 0223 的 `objects` 读取里按当前 READ 证明和属性组限制返回值，被限制的项标为 withheld。
- **风险**：属性值可能含个人数据。必须沿用读取审计，并在删除中隐藏，做法与 0226 Artifact 相同。
- **工作量**：1 个迁移（读取函数加属性值）加页面列。约 2 天。不需要新 ADR（ADR-025、ADR-035 已覆盖）。

**Object Set（D-G2，推荐引入，P1）**
- **用途**：
  - 目标与计划的人群范围（“过去 30 天咨询过续费的消费者”）；
  - 外联候选的来源（外联切片已经有按条件筛选的需求）；
  - 审核和导出的范围。
- **符合性**：
  - 只读查询，在读取时求值，不写对象；
  - 定义本身是受治理的配置，由负责人发布，与 Function、Action 的发布方式相同（`catalog_publish`）；
  - 求值必须在 SQL 里按租户、world、当前 READ 证明和属性安全执行，不能把对象全集读进 Python 再过滤（上游的 `InMemoryObjectSetExecutor` 不适合 NexLoop 的规模和权限模型）。
- **不引入**：上游的 revision 快照（冻结成员集合）。快照会在删除传播中成为漏网的副本；需要冻结时只记 digest。
- **工作量**：
  - 定义表与发布：1 个迁移，复用 `trusted_configuration`；
  - SQL 求值器：支持属性过滤、一层 Link 遍历、排序和计数；不做聚合和语义搜索；
  - 工作台：只读页面和预览。
  - 约 4–5 天。需要一个短 ADR，写明求值位置（SQL）、不存成员快照、定义的发布权限。

**Object View（推荐引入，P2）**
- 只是展示布局，用于对象详情。定义同样走受信配置，工作台读取后按它渲染。不涉及权限变化，因为属性过滤仍在 SQL 中完成。约 2 天，不需要 ADR。

**Interface（推荐延后）**
- 多个对象类型共享形状时才有价值，例如“可联系的主体”。目前每个租户的类型少，现有的 Action 绑定按具体类型已经够用。等 NX-040 开放加法 Schema 演进、类型数量增长后再评估。

### 2.2 B 类：有意保持只读（不改）

- 世界模型的编辑入口：W 设计 D5（负责人 2026-10-19 已定）决定世界模型只读，结构变化走候选审核（ADR-019）和 NX-040，实例变化走受治理 Action。上游页面上的“新建 Object Type”“新建对象”弹窗不引入。
- 需要在世界模型页里“改一个对象”时，跳转到现有的 Actions 页执行对应的受治理 Action。这是一个导航改进，可以与 D-G1 一起做。

### 2.3 C 类：没有 vendored

**数据集加投影管道（D-G3，推荐先写 ADR，暂不引入）**
- **冲突点**：
  1. 上游的数据集绑定把数据集行投影写成 `ontology.objects`。在 NexLoop 中，正式对象只能由受治理的 EIOS Action 改变（AGENTS.md 安全不变量）。直接投影就是一条不受治理的写入路径。
  2. 数据集是第二套业务数据存储（`integration.*`）。它虽然仍在 PostgreSQL 里，但“哪一份是权威”需要重新定义；与 ADR-007 的精神（不建第二事实库）相冲突。
  3. 增量构建在上游也只是预留接口，搬过来得到的主要是全量构建。
- **如果要引入**，必须满足：
  - 投影的输出不能直接写对象。它只能生成 `ontology-mutation` 提案（与对话提取同一条路径：四层匹配，再由受治理 Action 应用），或者调用一个专门的批量受治理 Action，有幂等、预算和审计；
  - 数据集只作为“暂存与证据”，不是业务事实；
  - 删除传播与保留期必须覆盖数据集行。
- **工作量**：ADR 加 vendoring（`datasets/`、`transform.py`、`build.py`，逐文件 provenance）、改写绑定为提案路径、工作台的数据集与管道页，约 8–10 天。**推荐延后到 S6 之后**：目前的真实数据来源只有对话和签名商业事件，没有表格型数据的需求证据。

**通用连接器与同步（D-G4，推荐不引入通用框架；按需单独 ADR）**
- **冲突点**：
  - 新的外部数据源；
  - 凭据边界：上游用环境变量或加密文件解析凭据，还有会话代理登录；而 NexLoop 的规则是 runtime 不持有渠道凭据，凭据集中在 Keychain 和私有配置；
  - 调度器常驻；
  - 公开仓库不能带任何领域连接器。
- **现状已经够用**：商业事件走签名接入（0118）；消息走渠道。新的数据源应当按“签名事件推送到 NexLoop”的方式接入，而不是由 NexLoop 主动拉取外部系统。
- **如果确有拉取需求**（例如 CRM 的只读同步），按单个连接器写 ADR：
  - 凭据只在接入服务中；
  - 同步结果只生成 Claim 或 mutation 提案；
  - 有 cursor 与对账；
  - 未知结果不盲目重试。
  - 约 5–8 天一个。

**数据健康（D-G5，推荐引入 NexLoop 版本，P2）**
- 不搬上游依附于 pipeline 的健康报告，改为 NexLoop 自己数据流的健康：
  - 抽取队列积压与死信；
  - 召回索引覆盖率（0221 已有召回质量函数）；
  - 重流积压；
  - 商业事件接入的失败与延迟；
  - Artifact 孤儿；
  - 删除传播进度。
- 这些数据大多已经存在于各个读取和 doctor 检查里，缺的是一个统一的健康读取和页面块。
- 约 2–3 天，不需要 ADR。

**Lineage（推荐延后）**
- NexLoop 的证据链（Claim 的 evidence、上下文来源、效应收据、候选事件）已经覆盖“这个结论从哪里来”。只有引入数据集和管道后，才需要表级的 Lineage。随 D-G3 一起决定。

**Artifact 详情与下载（D-G6，推荐延后）**
- 列表已经接通。下载会把内容带出系统，需要定义：谁能下载、下载的审计、对删除中的主体如何处理。
- 预签名的对象存储不引入（ADR-015）。等有明确的运营需求再做，约 2 天。

## 3. 建议的优先级与切片

| 切片 | 内容 | 依赖 | 估计 | ADR |
|---|---|---|---|---|
| G1 对象属性值 | 0223 读取加按 READ 证明和属性组过滤的属性值；页面列；从世界模型跳转到 Actions 页 | 无 | 2 天 | 不需要 |
| G2 Object Set | 定义表与受信发布；SQL 求值（过滤、一层 Link、排序、计数）；只读页面与预览；不存成员快照 | G1（共享属性过滤） | 4–5 天 | 短 ADR（求值位置、不存快照、发布权限） |
| G3 数据健康 | 统一健康读取（抽取、召回、重流、接入、Artifact 孤儿、删除传播）；DataAssets 页的健康块，替换现在的“不可用” | 无 | 2–3 天 | 不需要 |
| G4 Object View | 定义走受信配置；对象详情按 View 渲染 | G1 | 2 天 | 不需要 |
| 延后 | Interface；Function 结果缓存；Lineage；Artifact 下载 | — | — | — |
| ADR 后再定 | 数据集加投影管道（D-G3）；单个拉取型连接器（D-G4） | ADR | 8–10 天；每个连接器 5–8 天 | 需要 |

推荐顺序：G1 → G3 → G2 → G4。G1 和 G3 可以并行。G2 的 Object Set 可以直接服务外联与目标人群，价值最高，但需要先过一个短 ADR。

## 4. 负责人决定的事项

【已决定，负责人 2026-10-20：D-G1–D-G6 全部按推荐。】

| 编号 | 问题 | 推荐 | 理由 | 代价 |
|---|---|---|---|---|
| D-G1 | 工作台是否显示对象的属性值 | 显示，按当前 READ 证明与属性组过滤，被限制的标 withheld，读取写审计，删除中的主体隐藏 | 目前只显示 ID，世界模型“看不到数据”的感受主要来自这里；权限与审计的机制已经存在 | 一次迁移；需要复核审计量 |
| D-G2 | 是否引入 Object Set | 引入：受治理配置发布，在 SQL 中读取时求值；不存成员快照；先只读，后续给目标与外联引用 | 目标人群和外联候选都需要可复用、可审计的对象范围 | 一个短 ADR；4–5 天 |
| D-G3 | 是否引入数据集与投影管道 | 暂不引入。如果引入，投影只能生成 mutation 提案或调用批量受治理 Action，数据集只作暂存与证据；需要 ADR | 上游的投影会直接写对象，绕过受治理 Action；目前没有表格型数据的需求证据；增量构建在上游也是预留接口 | 有表格数据需求时要多等一个 ADR 周期 |
| D-G4 | 是否引入通用连接器 | 不引入通用框架和领域连接器。外部数据优先以签名事件推送；确需拉取时，按单个连接器写 ADR | 凭据边界与常驻调度器的风险大；公开仓库不能带领域代码 | 每个新数据源都要单独评审 |
| D-G5 | 数据健康的口径 | NexLoop 自己数据流的健康（抽取、召回、重流、接入、Artifact 孤儿、删除传播），不搬上游的 pipeline 健康 | 上游的健康依附于 NexLoop 没有的管道；NexLoop 真实的风险点在自己的队列里 | 2–3 天 |
| D-G6 | Artifact 详情与下载 | 延后；不引入预签名对象存储 | ADR-015 本地 Artifact 首发；下载需要单独的审计设计 | 运营暂时只能看元数据 |

## 5. 附：核对方法

- **上游**：只读查看冻结工作树的导航定义（`apps/portal/src/components/AppShell.tsx` 第 101–126、147、159 行）、路由、`src/eios/{ontology,datasets,connectors,lineage,read_models,api}` 与迁移文件名。没有阅读或引用领域连接器的内容。
- **NexLoop**：
  - vendored 核心的目录：`actions adapters approvals authz capabilities composition control identity kernel migrations ontology persistence runtime`，其中没有 `datasets`、`connectors`、`lineage`、`read_models`；
  - provenance 在 `versions.lock.json` 和 `packages/eios-core/PROVENANCE.json`；
  - 后端读取：`workbench_reads.py`、`workbench_http.py`；迁移 0222–0230；
  - 页面：`WorldModel.tsx`、`WorldInstances.tsx`、`Capabilities.tsx`、`DataAssets.tsx`。
- **限制**：本文以 7a153a29 为准。L6 的世界模型页修复（df0ef178，导航改为按概念分页）在第二批集成分支上，还没进 main；它改变的是页面组织，不改变本文的差距判断。
