# 工作台补齐设计：企业世界模型、数据资产、能力目录（调研与设计，不含实现）

- 来源：负责人 2026-10-19 走查（参考 NEX-EIOS 前端）。由调度员分派。
- 范围：
  1. 调研 EIOS 前端；
  2. 逐项对照 NexLoop 判断取舍；
  3. 命名；
  4. 关系图方案；
  5. 分批实施与待拍板事项。
- 本文不写代码。EIOS 前端只作参考，不搬运。NexLoop 对 EIOS 是 vendored 核心（ADR-006），不依赖它的前端。tennis、sports8、TOS 领域代码一律不引用。

## 1. EIOS 前端调研（冻结提交 1e363db，只读）

### 1.1 位置与技术栈

- 位置：EIOS 冻结工作树 `apps/portal`。
- 技术栈：React 19、Vite、TypeScript、react-router-dom 7、@tanstack/react-query 5、lucide-react（图标）、zod。
- 不用 UI 组件库，也不用任何图表或关系图库。关系图是手写的 SVG 加自写布局：
  - `features/world-model/graph/layout.ts`：节点少时排成一行或一圈，节点拥挤时改用确定性的力导向布局；
  - `components/graph-viewport.ts`：平移和缩放；
  - `relation-graph.tsx`：渲染。
- 导航定义在 `src/components/AppShell.tsx`，路由在 `src/app/router.tsx`，接口客户端在 `src/adapters/http/client.ts`。

### 1.2 完整导航结构

| 分组 | 页面（路由） | 展示 | 主要接口 | 可写操作 |
|---|---|---|---|---|
| — | 概览 `/` | 告警摘要、数据集、调度、近期运行 | `/api/alerts/summary`、`/api/data/datasets`、`/api/orchestration/*` | 无 |
| **构建与资产** · 企业世界模型 | 总览 `/world-model` | 九类概念的汇总表（类型、一等模型、分层、记录数），可切换表格和关系图（`?view=graph&focus=type:id`） | `GET /api/ontology/schema`、`/api/ontology/objects?limit=1`、`/api/definitions?kind=…` | 无 |
| | Object Type、Object、Property、Link、Function、Action、Object Set、Object View、Interface（`/world-model/:concept`） | 每类概念一张表：名称、版本、Property 数、主键 / 所属类型、数据类型、约束 / 关系、方向、基数 / 定义表（状态、Contract Digest、Required Scopes）；Object 可看属性值；Object View 可预览 | `/api/definitions/{kind}…`、`/api/object-sets/{name}`、`/api/objects/{type}/{id}` | 新建定义（先注册 v1 草稿，再发布 v2）；Object 增改；明确不支持删除 |
| | 绑定 `/world-model/bindings`（不在侧栏） | 数据集到对象类型的映射、运行记录 | `/api/bindings*` | 新建、停用、物化 |
| | 实例子图 `/objects/:type/:id`（`/subgraph`、`/lineage`） | 实例邻域图，深度 2，按组限量，大组折叠为簇；行级血缘 | `/api/objects/{type}/{id}/subgraph` 等 | 无 |
| **构建与资产** · 数据资产 | 数据集、数据管道、连接器与同步、数据健康、Artifact | 数据集的列、行和事务；转换 DAG；连接器回执；期望检查；Artifact 列表与下载 | `/api/data/*`、`/api/artifacts*` | 新建管道并构建、预签名下载 |
| **构建与资产** · 企业应用 | 网球馆日历 | 领域页面 | `/api/tennis/*` | **排除** |
| **构建与资产** | 能力目录 `/capabilities` | 能力列表，含版本与契约 | `/api/capabilities*` | 回显调用 |
| **运行与治理** | 调用与任务（Invocations、Jobs、Runs、事件时间线） | 列表、详情、尝试、步骤、事件 | `/api/invocations*`、`/api/jobs*`、`/api/runs*` | 无 |
| | 审批中心 | 待审批的项目 | `/api/approvals*` | 批准、拒绝 |
| | 监控看板（列表视图和组件依赖图） | 组件状态、指标、证据 | `/api/ops/monitoring` | 无 |
| | 告警中心 | 告警列表 | `/api/alerts*` | 标记已读、删除 |
| | 调度中心 | 计划、触发器、运行、剧本 | `/api/orchestration/*` | 新建、暂停、恢复、删除、手动运行 |
| **系统管理** | 审计日志 | 审计列表与详情 | `/api/audits*` | 无 |
| | 用户与权限 | 账号、角色、Scope、密钥 | `/api/access/*` | 增删账号，签发、轮换、吊销密钥 |

领域代码排除清单：
- `features/tennis/*`、`features/share/tennis-*`；
- 调度中心的 `sync-panel` 等组件；
- 告警和监控图中导入的球场标签辅助函数；
- fixture 数据。

上面这些文件夹杂了领域词，NexLoop 不参考它们的实现。

## 2. 对照 NexLoop：取舍

NexLoop 工作台现有三组页面：
- 运营：总览、消费者、承诺/交付、Action/异常、计划与运行、目标与对齐、联系限制；
- 监控：告警、运行状态、审计；
- 管理：知识工作台、设置与治理、本体/演进（未启用）、实验空间（未启用）。

读取都走 `authz.nexloop_workbench_read(verb…)`（0117/0130），每个 verb 都在 SQL 里核对 EIOS 授权。NexLoop 有的核心表包括：
- 本体：`ontology.object_type_versions`、`relation_type_versions`、`event_type_versions`、`objects`、`relations`、`events`；
- 控制面：`control.nexloop_action_definitions`、`nexloop_function_definitions`、`nexloop_governed_capabilities`、`nexloop_metric_definitions`；
- 候选与审核：`ontology.nexloop_candidate_*`、`nexloop_review_decisions`；
- 召回：`nexloop_recall_*`。

规模参考：系统目录（`system_catalog`）有 16 个对象类型、37 个 Action、2 个 Function；业务 Action 清单 34 项。

图例：✅ 适合做成只读视图；⚠️ 需要新的只读接口或契约；❌ 不适合放进工作台。

| EIOS 能力 | 判断 | NexLoop 做法 |
|---|---|---|
| 世界模型总览（概念汇总表） | ✅⚠️ | 新增 verb `world_model`：返回已发布定义的计数和版本（对象类型、关系类型、事件类型、Action、Function、能力、指标），只有元数据，不含实例内容 |
| Object Type / Property / Link 表 | ✅⚠️ | 同一个 verb 返回当前生效版本的结构：属性名、类型、约束、主键、关系方向与基数。负责人标为受限的属性只显示名字和“受限”，与消费者详情的 `withheld` 一致 |
| Action、Function 表 | ✅⚠️ | 显示名称、版本、风险、审批方式、副作用声明、契约摘要、效应类别（v3）和真实派发开关状态。全部来自已发布的定义，不能编辑 |
| Object Set、Object View、Interface | ❌（暂不） | NexLoop 内核没有这三类一等定义，不为展示去造概念。等 EvoOntology 语义视图落地后再评估 |
| Object 实例浏览 | ✅⚠️ | 只按类型列出 ID、版本和更新时间。属性值走现有的 `nexloop_read_object`，逐对象核对 READ，并写读取审计（ADR-025）；没有 READ 时显示“无权限”，不显示为空。不做全文导出 |
| 实例子图（深度 2） | ✅⚠️ | 新增 verb `object_neighbors`：深度 1（深度 2 交给 D 项决定），每组最多 25 个邻居，超出的折叠计数；每个邻居都核对 READ，不可读的只计入“不可见 n 个” |
| 新建或编辑定义、Object 增改、绑定物化 | ❌ | 违反工程契约：正式 schema 和业务对象只能由受治理的 EIOS Action 修改。schema 演进走候选审核（知识工作台，`ontology.schema.review`）和可信配置发布。工作台只做链接，引到“知识工作台 · 待审核”的入口 |
| 数据资产：数据集、管道、连接器 | ❌（原样不做）/ ✅（换成 NexLoop 自己的资产） | NexLoop 不是数据集平台。对应的资产换成：Artifact（已有 `/api/v1/artifacts`）、Claim 与提取运行、召回索引（profile、条目数、积压）、备份清单（repo1/repo2）、合并配置 |
| 数据健康 | ✅ | 已有运行状态页，可以加一个“数据质量”区块：提取积压、死信、召回回填进度、`staged_without_configuration`（0216） |
| 能力目录 | ✅⚠️ | 新增 verb `capabilities`：受治理的能力（`nexloop_governed_capabilities`）、业务 Action 清单、效应类别、渠道路由和派发开关。只读，不做 EIOS 那种回显调用 |
| Invocations、Jobs、Runs、事件时间线 | ✅⚠️ | “计划与运行”已经有 Plan，Run 只作为证据出现。可以新增 verb `runs`：Run 列表（状态、时长、工具次数、成本，不含模型原文），详情链到 Run 证据。这一项放到后续批次 |
| 审批中心 | 已有替代 | NexLoop 的审批是候选审核（知识工作台）和 Action 审批模式，不另建页面 |
| 监控看板与组件图 | ⚠️（后续） | 运行状态页已有。组件依赖图需要服务拓扑的数据，本批不做 |
| 告警中心的删除和标记已读 | ❌ | NexLoop 告警只能静默，事件可审计，不能删除 |
| 调度中心的增删改 | ❌ | 调度由目标、计划和受治理的控制决定，没有自由的定时任务入口 |
| 审计日志 | 已有 | 第 6 批已做（F11） |
| 用户与权限的增删和密钥 | ❌ | 成员与角色只能经可信配置变更（NX-028 D1）。设置页保持只读，最多加一张“角色 → Action”只读矩阵 |

## 3. 命名：本体/演进 → 企业世界模型（推荐）

理由：
1. 负责人和 EIOS 都用“企业世界模型”，是业务人员能看懂的词；“本体”偏技术。
2. 这个页面展示的是**已发布的正式模型**，不是演进过程。演进（候选、审核、合并）已经有知识工作台。叫“本体/演进”容易让人以为可以在这里改 schema。
3. 路由 `/workbench/ontology` 保留，旧链接不失效，只改显示名。页内副标题写“已发布的对象、关系、Action 与能力（只读）；变更经知识工作台审核与可信配置发布”。
4. 文档和代码中，概念名 Object Type、Property、Link、Action、Function 保持英文，与 EIOS 和契约一致；中文作为次级说明。

## 4. 关系图

推荐**自写 SVG，不引入库**，这也是 D2 的推荐项。
- 规模：schema 图约 16–40 个对象类型节点、20–60 条关系边；实例邻域每组最多 25 个，总计不超过 150 个节点。这个量级用 SVG 和确定性布局就够了。EIOS 用同样的方式跑过同等规模。
- 许可证：不新增依赖，THIRD_PARTY_NOTICES 不需要改动。
- 备选：如果以后要上千节点，或要求交互式拖拽，可以用 `cytoscape`（MIT，在 license-policy v1 的 allowed 内），需要重新生成 NOTICES，并通过 `check_licenses --check`。`d3-force` 是 ISC，也可以用。不考虑 GPL 系或许可证不明的库。
- 布局：确定性布局，同样的输入画出同样的图，截图测试才稳定：
  - 节点不超过 8 个时排成一圈；
  - 节点更多时，按关系的连通分量分组后排成环；
  - 再多就用固定种子的简单力导向，迭代 200 次。
  - 布局代码放在 `apps/web/src/workbench/graph/layout.ts`，是纯函数，用 vitest 测试。
- 交互：
  - 平移、缩放（滚轮或按钮）；
  - 点击节点后在右侧抽屉显示属性、出入关系，以及“查看实例”；
  - 键盘：Tab 遍历节点，Enter 打开；
  - 无障碍：SVG 带 `role="img"` 和 `aria-label`，同时提供“以表格查看”的等价视图（图上的信息在表格里都能看到）。
- 与现有组件配合：
  - 页内用 `ui.tsx` 的 `Tabs` 切换“表格 | 关系图”，状态放在 URL（`?view=graph&focus=…`），可以链接、可以回退；
  - 表格视图复用 `DataTable`（窄屏下自动变成卡片）；
  - 图只是表格之外的另一种视图，数据来自同一个 query，不重复请求；
  - 窄屏（640 px 以下）默认显示表格，图的入口照常保留；
  - 颜色用 styles.css 的现有色板变量，每个对象类型按名字的哈希映射到 8 个色相中的一个。

## 5. 分批实施

| 批次 | 内容 | 主要文件 | 迁移与契约 | 验收 |
|---|---|---|---|---|
| W1 世界模型（schema） | 页面改名；总览计数；Object Type、Property、Link、事件类型、Action、Function 六个标签页的表格；schema 关系图 | 迁移（临时编号）：`authz.nexloop_workbench_read` 加 verb `world_model`，SQL 只读，返回当前版本的定义元数据，受限属性按 `withheld`；`workbench_reads.py`、`workbench_http.py` 新增路由；`api.ts` 解析器；`pages/WorldModel.tsx`；`graph/layout.ts`、`graph/SchemaGraph.tsx`；`route.ts`、`WorkbenchApp.tsx`（启用页面） | 迁移一个；只读接口契约新增一份 `packages/contracts/workbench-world-model.v1.json`；roles 清单不变（见 D1） | vitest：解析器、布局确定性、表格与图同源；PG：只读、跨租户不可见、受限属性只显示名字；浏览器：1440/390 无横向滚动，图可键盘操作 |
| W2 实例浏览与邻域 | 按类型列出实例；对象详情（逐对象 READ）；邻域子图 | 迁移：verb `objects`（分页，只有 ID、版本、时间）、`object_neighbors`（深度 1，每组 25 个）；读取审计沿用 ADR-025 | 一个迁移，契约补充 | PG：无 READ 的计入“不可见”、审计行写入、每组上限；浏览器：抽屉、深链接 |
| W3 能力目录 | 能力、业务 Action、效应类别、渠道路由、派发开关，汇成一张目录（只读） | 迁移：verb `capabilities`；`pages/Capabilities.tsx`；放在“管理”组 | 一个迁移 | 与设置页的派发开关同源，不重复展示状态 |
| W4 数据资产 | Artifact 列表（已有接口）、Claim 与提取运行统计、召回索引、备份清单、合并配置状态 | 前端为主；需要的统计复用 metrics snapshot（0216 candidates 块、backup 块）；提取和召回的计数可能要扩展 metrics（一个迁移） | 视 metrics 字段决定 | 不出现消息原文；计数缺失时标为“不可用”，不当成 0 |
| W5（可选）运行记录 | Run 列表与详情（状态、工具次数、成本、证据链接） | 迁移：verb `runs` | 一个迁移 | 不含模型原文和提示词（工程契约：不要求隐藏的推理过程） |

导航调整（建议）：
- **运营**：总览、消费者、承诺/交付、Action/异常、计划与运行、目标与对齐、联系限制；
- **模型与资产**：企业世界模型、能力目录、数据资产、知识工作台；
- **监控**：告警、运行状态、审计；
- **管理**：设置与治理、实验空间（未启用）。

这样对齐 EIOS 的“构建与资产 / 运行与治理 / 系统管理”，同时保留 NexLoop 以运营为先的顺序。

## 6. 需要负责人拍板（D 项与推荐）

| D | 问题 | 推荐 |
|---|---|---|
| D1 | 世界模型（schema 元数据）谁能看 | 负责人和运营都能看，审核人也能看，挂在现有的 `nexloop.workbench.read` 上。理由：只有定义元数据，没有个人数据。实例属性值仍然逐对象核对 READ |
| D2 | 关系图是自写 SVG 还是引入库 | 自写 SVG，不新增依赖；规模超过 150 节点时再评估 cytoscape（MIT） |
| D3 | 实例邻域的深度 | 先做深度 1，每组 25 个；深度 2 等实际数据量出来后再定 |
| D4 | 页面名称 | 改为“企业世界模型”，路由不变 |
| D5 | 是否在工作台提供 schema 变更入口 | 不提供编辑。只放“去知识工作台审核候选”的链接，变更继续走候选审核和可信配置 |
| D6 | Object Set、Object View、Interface | v0.1 不做；等语义视图（EvoOntology）有了定义再评估 |
| D7 | 批次顺序 | W1 → W3 → W2 → W4，W5 视需要。W1 和 W3 都是纯元数据，风险最低；W2 涉及个人数据读取和审计，放在后面 |

## 7. 边界与不变量（实施时必须守住）

- 所有新 verb 都是 SQL 只读函数（definer，`search_path=pg_catalog,pg_temp`）。租户、world 和身份从签名 claims 推导，不接受客户端传入。
- 工作台不提供任何写入正式 schema 或对象的入口。
- 实例内容逐对象核对 READ 并审计。受限、删除、更正的结果按现有标签显示，不显示为空，也不显示为 0。
- 不复制 EIOS 前端代码，只参考它的交互模式（表格与图切换、URL 中的 focus、按组上限）。
- 共用组件（DataTable、Tabs、Empty、StatusBadge）由 L3 维护。图需要的新共用能力先在页面内实现，合并时再收敛。
