# 负责人工作台 UI 巡检（2026-10-20，基于 origin/main 7a153a29 = stage 发布 23）

方法：
- 逐页读代码（`apps/web/src/workbench/`）；
- 在本机合成栈上跑 `test_web_layout_real_browser` 和 `test_world_model_real_browser`，在 1440 / 1024 / 390 宽度下各截图一组，含暗色模式，共 91 张。截图只留在本机，不入库。

页面清单以 `WorkbenchApp.tsx` 的路由为准。行号对应 7a153a29。规范见 `workbench-ui-spec.md`；问题编号（如 R1）在规范和实施计划里引用。

## 1. 跨页问题（规范要解决的根因）

| 编号 | 问题 | 证据 |
|---|---|---|
| R1 | **原始状态码直接显示**：承诺异常原因、意图状态、计划状态、各类 kind/scope 都没有中文标签 | 承诺异常 reason（`no_active_plan`、`made_under_contact_restriction`、`source_deleted`、`needs_resolution`、`blocked_by_contact_restriction`）在任何地方都没有中文映射：Overview.tsx:18，Commitments.tsx:104/122/131，Observability.tsx:77。`Counts` 组件显示成“死信（dead_lettered）”，没有映射的直接输出原码 |
| R2 | **长 ID 处理不一**：有 6 种截断写法、2 种复制写法，还有几处整串直接铺开 | `Short`、`CopyId`、`shortId`/`IdRef`、WorldInstances 里的 `shortId`、DataAssets 里的 `short`、`memberAlias`。整串铺开：目标页的控制事件和暂停范围 `scope_ref`（Goals.tsx:22/26），总览的暂停范围（Overview.tsx:14），承诺事件的 `principal_id`（Commitments.tsx:130） |
| R3 | **表格不能横向滚动**：滚动的是整个页面容器，标题和工具栏会跟着表格一起移动 | `.workbench-page{overflow-x:auto}`（styles.css:94）；`DataTable` 外层没有滚动容器，也没有固定表头。`td{overflow-wrap:anywhere}` 会把编码从中间断开。运行状态页的 `ProcessBlock`/`BackupBlock` 直接写 `<table>`，没有用 DataTable（Observability.tsx:43、55） |
| R4 | **同一排卡片高度不一** | `.workbench-grid{align-items:start}`（styles.css:236），栅格宽度在 :59 和 :236 各定义一次（16rem / 18rem）。截图里总览、运行状态、数据资产三页的卡片都是参差的 |
| R5 | **“不可用 / 未加载 / 空”至少有 15 种说法、3 种样式** | 例如：“暂无记录。”“暂无数据”“暂无X。”“无。”“没有X”“此区块暂不可用：未加载，不代表数量为零。”“暂时无法读取”“服务暂时不可用（503）”“暂不可读”“X不可用 + 后端尚未提供”“未加载，不代表…”。样式上，`Empty` 是虚线卡片，自写的空状态是 `p.note`（上边距 30px），放在区块里的 `Empty` 又被去掉边框（styles.css:241） |
| R6 | **中英文混杂**：实体名和中文混写；标签里出现内部字段或版本号 | “控制 revision”、“后台 feed”、“p95 毫秒”、“WAL 归档”、“依赖 Claim”、“Schema 版本”；“（AT-003）”、“v0.1”、“(403)/(503)” 直接出现在给用户看的文案里 |
| R7 | **表单布局不一**：单选组是 inline-flex，复选框不放在 fieldset 里，标签有时在控件上方、有时在旁边 | AI 身份披露（Personas.tsx:178-191）：单选 fieldset 和两个复选框挤在同一行，宽度不够时参差换行（截图 personas-disclosure-1440）。外联控制（:276-283）两个时间输入共用一个标签。Action/异常页的状态筛选没有放进 `.field`（Actions.tsx:27） |
| R8 | **大量内联样式，绕过 token** | WorldModel.tsx 8 处，SchemaGraph.tsx 7 处，WorldInstances.tsx 5 处，DataAssets.tsx 5 处。还有 9 个硬编码颜色（`#b54708`、`#e8f7ee` 等），部分不跟随暗色模式。没有阴影 token；圆角 8/9/12、字号 13/14 也都是硬编码 |
| R9 | **两套时间显示**：`Time` 组件和 `when()` 字符串函数混用 | ui.tsx 的 `Time`；Consumers.tsx:23 的 `when` |
| R10 | **加载失败时有的页面没有重试按钮**：自写加载态，没用 `Failure` | Personas.tsx 的 `ConsumerPersona`（:199-209）、`OutreachControlsTab`（:233） |

## 2. 逐页问题

### 总览（Overview.tsx）
- 卡片高度参差（R4）。总览页没有页面说明（lead）。
- 原码：暂停范围显示 `consumer:<64 位>`（:14），承诺异常原因（:18）；队列名和 feed 名（:37/:40），预算 key 和单位（:50），`model（real）` 和币种（:53）。
- 混杂：“控制 revision”（:12）、“后台 feed”（:39）。
- “商业事件与费用”不可用的区块用虚线卡片和两行提示，和其他卡片的“无。”风格不一致（R5）。

### 消费者：列表、详情、会话（Consumers.tsx）
- 原码：订单和 Action 的徽标直接显示 `a.state`（:70，应该用 `INTENT_STATE_TEXT`）；状态回退时显示原码（:67）；“规则 {rule_id}”（:130）；属性 key 和 `JSON.stringify` 出来的值（:133）；受限属性组的代码（:135）；“渠道 {namespace}”（:162）。
- “（AT-003）”出现在页面上（:134）。
- 同一页里空状态有 7 种说法（:51/61/68/71/133/140/166）。
- ID：列表用 `CopyId`；详情的部分位置用 `Short`，没有复制按钮（:58/140/163）。

### 承诺 / 交付：列表、详情（Commitments.tsx）
- 原码：异常原因（:104/122/131），证据 kind（:45），事件 kind（:130），状态回退（:121）。
- 承诺事件的 `principal_id` 整串显示（:130）；`subject_ref.slice(11,23)` 是临时截法，没有复制按钮（:104）。
- 文案里有版本号：“不可用（v0.1 不推断问题是否解决）”（:128）。
- 证据栅格（styles.css:69）没有做等高。

### Action / 异常（Actions.tsx）
- 主列显示成 `nexloop.enterprise.deliver@1`（:31）；`provider_state` 是原码（:33）；派发原因回退时是原码（:19）。
- 意图 ID 用 `Short`，没有复制按钮（:31）。
- 状态筛选的标签和 select 是两个零散元素，没有走 `toolbar`（:27-29；截图里筛选框宽度占了半页）。

### 计划与运行（Goals.tsx `Plans`）
- 原码：`status`、`goal_version_ref`、`step_key`、`outcomes[0].kind`（:35-37）；重新评估下拉里的状态（actions.tsx:185）。
- 客户只显示截短的哈希，没有名称、链接或复制。空状态是自写的（:33）。

### 目标与对齐（Goals.tsx `Goals`）
- **控制事件把长 consumer 哈希整行铺开**（负责人截图）：`{event_kind} {scope_kind}:{scope_ref}`（:26）。`SCOPE_TEXT` 只在汇总里用到。
- 原码：`goal_kind`（:19），KR 显示成 `metric_id@metric_version`（:20），暂停范围和原因（:22），预算 kind 和单位（:24）。
- 混杂：“控制 revision”、“KR”、“Action 类型”。

### 联系限制（Contact.tsx）
- 升级原因是原码（:29）；规则命中回退时显示 `已命中规则 {rule_id}`（api.ts:307）。
- 客户只显示截短的哈希，没有名称和复制（:22/29），而消费者列表里是显示名称的。
- 表头有“控制 revision”。

### 企业世界模型（WorldModel.tsx / WorldInstances.tsx / graph）
- 关系图挤成一圈、中英文混杂、Table/Graph 和实体并列：这三项已在 `l6/world-model-ui-fix`（df0ef178，待第三批集成）中修复。规范按修复后的结构写。
- 剩余问题：`value_type`、`group`、`required_scopes` 是原码；风险和审批的回退显示原码；内联样式（R8）；Object 抽屉里的完整 ID 没有复制按钮。

### 数据资产（DataAssets.tsx）
- **Embedding profile 表格被截断，没有横向滚动**（负责人截图；R3）。
- 页内自己定义了一个 `Block`，覆盖了公共的 Block（:23）。“不可用”有三种写法（:26/63/69）。
- `Counts` 显示成“中文（code）”；`media_type` 是原码；`sha256_prefix` 没有说明长度。
- 自己写的 8 位截断，没有复制按钮。
- 混杂：“Claim 与提取”“Embedding profile”“claim-matcher 空转”（实体名的英文写法按术语表统一）。

### 知识工作台 / 审核证据（Knowledge.tsx）
- **同一条消息原文重复很多次**（负责人截图）。原因：服务端给每条 Claim 都附上了来源消息的正文（api.ts:334），页面按 Claim 逐条渲染完整的 `<blockquote>`（:42-44），没有按 `source_message_id` 分组。一条消息产生 N 条 Claim，正文就出现 N 次。
- 原码：候选 `kind`（:32）、证据 `kind`（:41）、`predicate`（:43）；候选没有显示名时回退成 ID（:31）。
- 无权限文案里出现 `workbench_roles` 等代码（:16-17）。

### 告警（Observability.tsx `Alerts`）
- `selector` 是原码（:27/34）。规则 ID 作为次要文字显示，可以保留，但要统一成 IdText 的样式。
- 混杂：“只针对真实世界（real）”。

### 运行状态（Observability.tsx `Operations`）
- 卡片参差（R4，截图）；直接写 `<table>`（R3）；服务名、队列名、feed 名、仓库名是原码；`Counts` 输出意图状态和异常原因的原码。
- 不可用区块是虚线卡片，备份失败也借用了 unavailable 的样式（:54）。
- 混杂：“后台 feed 与提取”“Agent Host 并发”“p95 毫秒”“WAL 归档”。

### 审计（Observability.tsx `Audit`）
- 操作码 `action`、`target_kind` 是原码（:104/105）。`rowKey` 用数组下标（:103）。
- 成员别名只显示 6…4 位，没有复制。

### 设置与治理（Settings.tsx / RealDispatchPanel.tsx）
- `application_id`、`manifest_version`、`read_purpose` 是原码（:16/32）。成员读取审计的“用途”列直接显示 `conversation`/`consumer`（截图）。
- 真实派发面板里，`REAL_DISPATCH_ENABLED`、`APP_HOST`、`数据模式 real/test` 是原码；写入失败显示成“未完成（kind）”；“未设置（关）”徽标折成两行（截图）。
- “暂不可读”和别处的“暂不可用”不一致。

### 运营人设（Personas.tsx）
- **AI 身份披露表单的单选和复选挤在一行**（负责人截图；R7）。只读区的“配置版本”显示原始修订号。
- 分配：规则行的控件没有可见标签；角色 ID 自己截成 12 位；名称写成 `name (persona_id)`，用了 ASCII 括号；客户要手输 64 位 ID。
- 外联控制：`time_zone` 是原码；修订号写成“r{n}”；“（opt-out）/（opt-in）”中英混排；两个时间输入共用一个标签。
- 子页的加载失败没有重试按钮（R10）。工具栏的 Tabs 没有 tabpanel。

### 能力目录（Capabilities.tsx）
- capability、operation、stable_name、namespace、delivery_action 刻意用 `<code>` 显示，属于技术名，可以保留，但要按规范放成主标题下面的次要文字。`source_task` 是原码。
- `num` 列放的是文字（:57/62）。
- 混杂：“业务 Action”“交付 Action”（按术语表处理）。

### 实验空间（Settings.tsx `NotEnabled`）
- 没走 `Page`，而是自己写了 `<section><h1>`。其他方面一致。
