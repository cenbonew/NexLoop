# 负责人工作台 UI 规范（草案 v1，2026-10-20，待负责人过目）

**状态**：草案。负责人确认之前不改任何页面。

**依据**：
- 巡检结果：`workbench-ui-audit.md`，文中 R1–R10 指它的跨页问题；
- 负责人对发布 23 截图的意见；
- EIOS portal 的结构和样式：上游冻结版本 `apps/portal/src/styles/tokens.css`、`layout.css` 和 `components/*`。只参考结构与样式，不复制代码。

**术语**：实体名用 EIOS 英文、说明用中文、状态码一律给中文标签，这些由 L5 起草的工作台术语表规定。术语表定稿后放在 `docs/design/workbench-glossary.md`。本规范只规定这些文字怎么显示，不另列术语；下文“术语表”都指那份文件。

**适用范围**：负责人工作台 `apps/web/src/workbench/`。顾客端 WebChat 与登录页只共用 token。

## 1. 设计 token

所有颜色、字号、间距、圆角、阴影只通过 `:root` 上的 CSS 变量取值，暗色模式在同一组变量上覆盖，页面和组件里不再写死数值（R8）。下表中“沿用”指保留现有 token；“新增”的数值参考 EIOS，并按 NexLoop 现有的蓝色系品牌调整。

### 1.1 颜色

| token | 用途 | 亮 / 暗 |
|---|---|---|
| `--bg` `--surface` `--surface-2` `--hover` | 页面背景、卡片、次级底色、悬停 | 沿用 |
| `--text` `--text-2` `--muted` | 正文、次要文字、说明 | 沿用 |
| `--border` `--border-strong` | 分隔线、控件边框 | 沿用 |
| `--accent` `--accent-soft` `--primary-*` `--focus` | 品牌色、选中底色、主按钮、焦点框 | 沿用 |
| `--ok-text` / `--ok-soft` | 成功、已完成 | 沿用 |
| `--warn-text` / `--warn-soft` | 等待、受限、需关注 | 沿用 |
| `--danger` / **`--danger-soft`** | 失败、违约、拒绝 | 新增 soft 底色：`color-mix(in srgb, var(--danger) 12%, transparent)` |
| **`--info-text` / `--info-soft`** | 进行中、信息 | 新增，用来取代现在写死的 `#1c3d8f` / `#b9cdf7` |
| **`--neutral-soft`** | 中性徽标、未设置 | 新增，取 `var(--surface-2)` |

现有写死的 9 个颜色（`#b54708`、`#e8f7ee`、`#166534`、`#f3c1bd`、`#f1c27d`、`#b9cdf7`、`#a7c4f2`、`#c4a8f5`、`#1c3d8f`）全部改为上表中的 token，必须带暗色值。

### 1.2 字号层级

只允许以下 6 级。行高统一：正文 1.6，标题 1.3。

| token | 值 | 用于 |
|---|---|---|
| `--fs-h1` | 28px（< 640px 时 24px），600 | 页面标题，每页一个 |
| `--fs-h2` | 18px，600 | 区块和卡片标题 |
| `--fs-h3` | 15px，600 | 卡片内的小节 |
| `--fs-body` | 15px | 正文、表格正文 |
| `--fs-small` | 13px | 说明、表头、徽标、次要 ID |
| `--fs-metric` | 24px，600，`tabular-nums` | 指标卡的数字 |

- 现有 `--fs-small` 13 和 `--fs-body` 15 保留，新增 `--fs-h1`、`--fs-h2`、`--fs-h3`、`--fs-metric`。
- 页面里的 `fontSize:22` 之类内联写法一律删除。
- 数字（计数、金额、毫秒）一律用 `font-variant-numeric: tabular-nums`。

### 1.3 间距、圆角、阴影

- **间距**：沿用 `--space-1` 到 `--space-6`（4、8、12、16、24、32）。
  - 页面区块之间 `--space-6`；
  - 卡片内边距 `--space-4`；
  - 卡片栅格间距 `--space-3`；
  - 表单字段之间 `--space-4`。
  - 不再写 `margin-top:30px` 之类的魔数。
- **圆角**：
  - `--radius-sm` 6：徽标、输入框；
  - `--radius` 10：按钮、表格外框；
  - `--radius-lg` 12：卡片、抽屉；
  - 胶囊形（999px）只用于 StatusBadge。
- **阴影**：新增两级。
  - `--shadow-1`：`0 1px 2px rgb(0 0 0 / 6%)`，静止的卡片；
  - `--shadow-2`：`0 12px 32px rgb(0 0 0 / 12%)`，抽屉、浮层、可点击卡片的悬停态。
  - 暗色模式下透明度加倍。

## 2. 页面骨架

每页结构固定，用 `PageHeader` 组件（见第 13 节）：

```
面包屑（仅详情页 / 子页）
H1 标题                                   [页面操作区：按钮]
说明（lead，最多两行，max-width 64ch，--muted）
工具栏（Tabs / 筛选 / 视图切换），渲染一次，加载时不卸载
数据区（加载 / 不可用 / 空 / 内容）
```

- 标题、说明、操作区在同一个 flex 头部里：操作区靠右；窄屏时换到说明下面。
- **每页都要有 lead**，总览也不例外，用一句话说明这一页回答什么问题。
- 页面里的二级视图用 Tabs，并且必须配 `role="tabpanel"`。“查看方式”（例如 Table / Graph）用分段控件 Segmented，放在标题下面，不和实体并列（世界模型修复中已经这样做）。
- 页面容器不再 `overflow-x:auto`（R3）；需要横向滚动的只有表格和关系图。

## 3. 卡片与栅格

- **栅格**：`.card-grid` 用 `repeat(auto-fill, minmax(min(100%, 18rem), 1fr))`，间距 `--space-3`，`align-items: stretch`（R4）。只保留这一处定义，删掉 `16rem` 那一份。
- **断点**：
  - ≥ 1440：内容最宽 1440；
  - 1024–1439：标准；
  - 640–1023：侧栏收为菜单按钮；
  - < 640：单列，表格改为卡片列表。
  - 不再新增其他断点。
- **卡片**：`Card` 组件。
  - 标题行：标题，右侧放可选的状态徽标；
  - 内容区；
  - 底部操作区：贴底，用 `margin-top:auto`，保证同一排卡片的按钮对齐。
  - 卡片本身 `display:flex; flex-direction:column; height:100%`。
- **指标卡**（`MetricCard`）：
  - 内容依次为标签（`--fs-small`）、数字（`--fs-metric`）、一句说明；
  - 最小高度 132px；
  - 可点击时整卡是一个链接，悬停时加 `--shadow-2`，边框变为 `--accent`；
  - 不可点击时用 `<div>`，不要用按钮样式（现在世界模型的计数卡是按钮套卡片样式）。
- **不可用的区块**：仍占同样的格子大小，内容换成 EmptyState 的 unavailable 形态（见第 8 节），不要另起一种虚线卡片。

## 4. 键值列表

- 用 `KeyValue` 组件，渲染成 `<dl>`，两列栅格：键列宽 `minmax(8rem, 30%)`，键用 `--muted` 和 `--fs-small`，值用 `--fs-body`。
- 窄屏（< 640px）改为键在上、值在下。
- 值是 ID 时用 IdText，是状态时用 StatusBadge，是时间时用 Time。值不要直接放 JSON 或原始代码（R1）。
- 一组键值超过 8 项时分组，每组一个 `--fs-h3` 小标题。

## 5. 表格

- **只用 DataTable**，不再在页面里直接写 `<table>`（R3）。
- **横向滚动**：DataTable 外面包一层 `.table-scroll`，`overflow-x:auto`。表格最小宽度等于各列最小宽度之和，滚动只发生在表格内部，标题和工具栏不动。
  - 有滚动时，在右边缘加渐隐阴影作为提示。
  - 第一列（main）用 `position:sticky; left:0` 固定。
- **表头固定**：行数超过 20 时 `thead th{position:sticky; top:0}`，背景用 `--surface`。
- **数字**：标了 `num` 的列右对齐，用 `tabular-nums`，不换行。`num` 只用于真正的数字列（计数、金额、毫秒）。版本号、“v3”之类的不算数字，左对齐。
- **列宽**：`main` 最小 10rem，`wide` 最小 14rem。长编码不再用 `overflow-wrap:anywhere` 从中间断开，改用 IdText 截断。
- **< 640px**：沿用现在的“行转卡片”布局，每行是一张卡片，列名作为键，组织方式与 KeyValue 一致。
- **空表**：表格不渲染，直接换成 EmptyState，不出现一个只有表头的空表。
- **行键**：用业务 ID，不用数组下标。
- **分页**：用统一的 Pager（上一页 / 下一页 + “第 N 页 · 共 M 条”）。基于游标的列表不显示总页数。

## 6. 长 ID

全站只用一个 `IdText` 组件，替换现有的 6 种截断写法和 2 种复制写法（R2）。

- **默认截断**：保留前 8 位和后 4 位，中间用“…”，例如 `548560cd…a3f0`；等宽字体，`--fs-small`。
- **悬停或获得焦点**：通过 `title` 和 `aria-label` 给出全称。
- **复制**：右侧放一个 24px 的图标按钮（`aria-label="复制 <种类> ID"`）。点击后图标变成 ✓，持续 1.5 秒，并发一条 `Announce`“已复制”。
- **展开**：可选的 `expandable` 模式，点击后显示全称并换行，用在抽屉和详情页。
- **种类前缀**：`kind` 参数给出中文种类名，例如“客户”“消息”“意图”，显示为“客户 548560cd…a3f0”。
- **能显示名称时优先显示名称**：客户、成员有显示名时，主文字用名称，ID 作为次要文字或放在悬停提示里。联系限制和计划页现在只显示哈希，应改成名称。
- **列表里不出现整串 64 位 ID**。目标页的控制事件、总览的暂停范围、承诺事件的 principal 都要改成 IdText。范围（scope）用“范围标签 + IdText”的形式，例如“客户 548560cd…a3f0”“Action nexloop.enterprise.deliver”。

## 7. 状态徽标与状态码

- **任何状态码都不直接显示**（R1）。页面只显示 `StatusBadge`，或者经过 `label()` 得到的中文标签。
- **映射表**：
  - 由一份 `status-labels.ts` 统一维护，形式为 `{域: {码: {label, tone}}}`，域例如 intent、commitment、commitment_exception、plan、goal_kind、scope_kind、event_kind、budget_kind、evidence_kind、read_purpose、audit_action、queue、feed、service、data_mode 等。
  - 中文标签的文字以术语表为准，这份文件只是它在代码里的落地。
  - 现在 api.ts 里分散的 `*_TEXT` 映射全部迁过去。
- **语气（tone）**：

| tone | 颜色 | 前置符号 | 用于 |
|---|---|---|---|
| `ok` | ok | ✓ | 完成、已交付、已通过 |
| `pending` | info | ● | 进行中、待处理 |
| `limited` | warn | ! | 受限、需人工、等待审批、未知结果 |
| `fail` | danger | ✕ | 失败、违约、拒绝、死信 |
| `neutral` | neutral | — | 未设置、已取消、已停用 |

- **未知码**：显示“未知状态”，徽标为中性色，`title` 里写原码，并且在开发构建里打一条控制台警告。页面上不出现原码本身。
- **技术名**：capability、stable_name、Action 名一类，属于实体的标识，不算状态码。它们放在主标题下面，用等宽字体、`--muted` 的次要文字显示（参考 EIOS capability-card）。主标题用术语表里的显示名。
- **文案里不出现** 内部编号（AT-003、v0.1）和 HTTP 状态码（403、503）。HTTP 状态码只放在可展开的“技术细节”里，见第 8 节。

## 8. 加载 / 不可用 / 空 / 无权限

统一用 `EmptyState` 组件，取代现有 15 种以上的说法（R5）。每种只有一个标准标题；页面可以补一句具体原因，但标题不变。

| 形态 | 何时 | 标题 | 说明（默认） | 样式 | 角色 |
|---|---|---|---|---|---|
| `loading` | 首次加载 | 正在加载 | — | 骨架条（3 行），不用整段文字 | `role="status"` + `aria-busy` |
| `empty` | 读取成功、确实没有数据 | 暂无 X | 有新记录时会出现在这里。 | 实线浅底，图标“○” | `role="status"` |
| `filtered` | 有数据，但被筛选掉了 | 没有符合筛选条件的 X | 换一个筛选条件，或选择“全部”。 | 同 empty | `role="status"` |
| `unavailable` | 读取失败，或后端没有提供 | X 暂不可用 | 未加载，不代表数量为零。 | 虚线边框，图标“!”，可带重试按钮 | `role="alert"` |
| `forbidden` | 角色没有权限 | 无权查看 X | 写清楚需要哪个角色，不列 Action 代码 | 同 unavailable，不带重试 | `role="alert"` |
| `not_enabled` | 下一阶段的功能 | X 尚未启用 | 写明在哪一阶段开放 | 同 empty | `role="status"` |

- “X”取区块或实体的名称，例如“暂无承诺”“Embedding 预算暂不可用”。“无。”“暂无。”“没有X”这类单字或无主语的说法一律替换。
- **不可用和空必须看得出区别**：前者是虚线边框加“!”，并说明“不代表为零”；后者是实线、没有警告色。
- 卡片里的空状态用紧凑形态，没有外框，只保留图标和一行标题，但用词同上。
- 失败时都给“重试”按钮（R10）。技术细节放在可展开的“技术细节”里：错误码、trace_id、HTTP 状态码。

## 9. 表单

- **字段**：用 `FormField` 组件，标签在上，控件在下，一行一个字段。说明文字放在控件下方，用 `--fs-small` 和 `--muted`。错误提示紧跟在控件下面，用 `--danger`，并通过 `aria-describedby` 关联到控件。
- **控件宽度**：文本框和下拉框最宽 32rem（现在常常占满半页），数字和时间输入框 10rem。
- **单选组**：`<fieldset>` 加 `<legend>`，legend 的样式和字段标签一致。
  - 选项竖排，每项一行。
  - 选项多于 3 个，或带说明文字时，改为选项卡片：`repeat(auto-fit, minmax(14rem, 1fr))`，选中的卡片加 `--accent` 内描边。
- **复选框**：单个复选框单独占一行，复选框在左，文字在右，说明文字缩进对齐。多个相关的复选框放进同一个 fieldset，都要有 legend。不允许复选框和单选组挤在同一行（R7，AI 身份披露）。
- **成对的控件**（例如时段的开始和结束）：放在一个 fieldset 里，legend 写“时段”，两个输入框各自有可见标签“开始”“结束”。
- **分组**：长表单用 `--fs-h3` 小标题分节，节与节之间间距 `--space-6`。
- **操作区**：放在表单底部，主按钮在左，次按钮紧随其后。危险操作用 danger 描边按钮，并要求先确认。“原因”字段固定是最后一个输入框。
- **只读展示**和**编辑表单**分开放：只读部分用 KeyValue，编辑部分用卡片包起来的表单，标题写“修改 X”。

## 10. 导航层级

- **侧栏分组**：依次为运营、模型与资产、监控、管理。组标题用 `--fs-small`、`--muted`、字距 0.06em，组与组之间用分隔线。
- **可展开的条目**：参照 EIOS 的导航（“企业世界模型”已经按 `l6/world-model-ui-fix` 实现）。
  - 条目由“链接 + ▸/▾ 按钮”组成，按钮有 `aria-expanded`；
  - 访问到该组下的页面时自动展开，用户也可以手动折叠；
  - 子项缩进，左侧有竖线，用 `--fs-small`。
- **选中态**：用 `--accent-soft` 底色和 `--accent` 文字。子项被选中时，父条目只把文字变为 `--accent`，不加底色。
- **页面内的层级**：最多两层，即 Tabs，加上 Segmented（视图切换）或筛选。三层以上就拆成侧栏子项或单独的页面。
- **面包屑**：详情页和子页显示，最后一项不可点击。
- **窄屏**：用菜单按钮展开侧栏，点击条目后自动收起（沿用现有做法）。

## 11. 图表与关系图

- **统一的画布**：
  - 宽度取容器实测宽度；高度在 240 到 560 之间；默认缩放到铺满（fit）。
  - 节点标签在屏幕上不小于 12px：缩小到这个下限后就改为平移，不再缩小。
  - 工具条依次为放大、缩小、适应画布；没有数据时显示 EmptyState。
- **键盘**：
  - Tab 在节点之间移动，Enter 或空格选中；
  - 画布获得焦点时，方向键平移、+/− 缩放、0 适应画布、Esc 取消选中。
- **颜色**：节点按名称哈希从 8 个 hue token 里取色（新增 `--hue-1` 到 `--hue-8`，带暗色值），不写死颜色。边用 `--text`，透明度 0.85，线宽 2，带箭头。
- **没有边时**：用网格布局，并提示“暂无已发布的 X”。没有坐标信息时不排成一圈。
- **文字替代**：每张图都要有一个 `aria-label` 说明（例如“N 个 Object Type，M 条 Link”），同样的数据一定也能用表格查看。
- **以后加柱状图或折线图时**：
  - 不引入图表库，除非经 ADR 批准；
  - 坐标轴和数字用 `tabular-nums`；
  - 颜色只用状态 tone 和 hue token；
  - 每张图都带数据表作为替代。

## 12. 内容规则（与术语表配合）

- **实体名**：用术语表里的 EIOS 英文，例如 Object Type、Action、Claim、Artifact。说明性的句子用中文，英文实体名与中文之间不加空格（例如“3 个 Action”中“个”和数字之间有空格，按现有排版习惯保留数字两侧的空格）。
- **内部字段名**：revision、feed、p95 这类不出现在给用户看的文字里，改用术语表里的中文说法，例如“修订号”“后台任务”“95 分位”。
- **时间**：只用 `Time` 组件，显示北京时间加相对时间。`when()` 字符串函数退役（R9）。
- **括号**：中文语境下统一用全角“（）”。
- **知识工作台的证据**：按来源消息分组。每条消息的原文只显示一次，最多 6 行，可以展开；下面列出由这条消息得出的 Claim，用术语表里的谓词标签。同一条原文不重复显示。

## 13. 公共组件清单

| 组件 | 新建 / 改造 | 接口要点 |
|---|---|---|
| `PageHeader` | 新建，从 `Page` 中拆出 | `title`；`lead`（必填）；`actions?: ReactNode`；`crumbs?`；`toolbar?`。`Page` 继续负责 query 状态，内部使用 PageHeader |
| `Card` | 新建，取代 `.workbench-block` 的直接使用 | `title`；`badge?: StatusBadge`；`footer?: ReactNode`（贴底）；`compact?`；`status?: 'ok'\|'unavailable'\|'forbidden'`，非 ok 时内容换成 EmptyState |
| `CardGrid` | 新建 | `min?='18rem'`；默认等高；不接受内联样式 |
| `MetricCard` | 新建 | `label`；`value: number\|string`；`hint?`；`to?: Route`，有 `to` 时整卡是链接；`tone?` |
| `KeyValue` | 新建，取代 `dl.facts` | `items: {key, value: ReactNode, hint?}[]`；`groups?`；窄屏时自动改为竖排 |
| `DataTable` | 改造 | 增加：`.table-scroll` 外层、`stickyHeader?`（超过 20 行时默认开启）、`stickyFirst?`、`empty: {title, kind}`（空表时直接换成 EmptyState）、`rowKey` 必须是业务键；`num` 只用于数字 |
| `IdText` | 新建，合并 `Short`、`CopyId`、`IdRef`、`shortId`、`short`、`memberAlias` | `value`；`kind?`（中文种类名）；`name?`（有名称时优先显示）；`head=8`、`tail=4`；`copy=true`；`expandable?`；`to?: Route` |
| `StatusBadge` | 改造 | 新签名 `StatusBadge({domain, code})`，从 `status-labels.ts` 取 label 和 tone；保留 `{text, tone}` 作为自定义文字的出口；未知码显示“未知状态” |
| `status-labels.ts` | 新建 | `label(domain, code)`、`tone(domain, code)`；整合 api.ts 里的 `*_TEXT`；单测保证每个后端枚举都有映射 |
| `EmptyState` | 改造 `Empty`、`Failure` 与 `SECTION_TEXT` | `kind: loading\|empty\|filtered\|unavailable\|forbidden\|not_enabled`；`subject`（“承诺”“Embedding 预算”）；`reason?`；`onRetry?`；`details?`（技术细节）；`compact?` |
| `FormField` | 新建，取代 `.field` | `label`；`hint?`；`error?`；`children`（控件）；`width?: 'full'\|'md'\|'sm'`；自动生成 id 和 `aria-describedby` |
| `ChoiceGroup` | 新建 | `legend`；`type: 'radio'\|'checkbox'`；`options: {value, label, hint?}[]`；`layout?: 'stack'\|'cards'`；默认竖排 |
| `Segmented` | 新建，从 WorldModel 的 ViewSwitch 抽出 | `label`；`options`；`value`；`onChange`；按钮带 `aria-pressed` |
| `Tabs` | 改造 | 渲染或配套输出 `role="tabpanel"`，用 `aria-labelledby` 与标签关联 |
| `Time` | 沿用 | `when()` 退役 |
| `Pager` | 沿用并统一 | 游标列表和偏移列表共用一套样式 |
| `MessageExcerpt` | 新建，用于知识证据和会话历史 | `body`；`maxLines=6`；可展开；`source?: IdText` |

## 14. 实施计划（规范确认之后）

每批一个分支，全部完成后统一跑 tsc、vitest，以及两项真实浏览器测试（布局、世界模型），并更新截图。

| 批次 | 范围 | 工作量 |
|---|---|---|
| **第 1 批：基础与高频问题** | token 补齐（danger-soft、info、neutral、shadow、字号层级、hue），替换写死的颜色；新建 PageHeader、Card/CardGrid/MetricCard、IdText、status-labels 和 StatusBadge 新签名、EmptyState、DataTable 改造。页面：总览、运行状态、数据资产（负责人点到的卡片参差、表格截断、不可用样式），目标与对齐（控制事件的长哈希），知识工作台审核证据（按消息分组）。术语表定稿后，状态码映射一次铺满所有页面 | 约 3 天 |
| **第 2 批：表单与运营页** | 新建 FormField、ChoiceGroup、Segmented，改造 Tabs。页面：运营人设（AI 身份披露、外联控制、分配）、设置与治理（含真实派发面板）、联系限制、Action/异常（筛选进工具栏）、计划与运行、消费者（详情页的空状态统一）、承诺/交付 | 约 2.5 天 |
| **第 3 批：模型与目录** | 企业世界模型各子页（在 `l6/world-model-ui-fix` 的基础上去掉内联样式，套用 MetricCard、KeyValue、IdText），能力目录（技术名改为次要文字），告警、审计、实验空间；关系图换用 hue token；删除旧组件（`Short`、`CopyId`、`IdRef`、`when()`、局部的 `Block`、`.workbench-block` 直接用法） | 约 2 天 |

- **前置条件**：L5 的术语表定稿，状态码的中文标签以它为准。第 1 批可以先搭好 `status-labels.ts` 的结构；术语表没定之前，标签暂取 api.ts 现有的中文，缺的标为 TODO，页面上显示“未知状态”。
- **不在本规范范围**：顾客端 WebChat 的排版、品牌色调整、暗色模式开关（继续跟随系统设置）。

## 附录 A：第 1 批组件接口（2026-10-20 冻结，第 2、3 批按此套用）

- **位置**：代码在 `apps/web/src/workbench/kit.tsx`、`status-labels.ts`、`DataTable.tsx` 和 `ui.tsx`（StatusBadge），单测在 `apps/web/test/workbench-kit.test.ts`。
- **接口变更规则**：
  - 冻结之后只允许新增可选属性。
  - 要改名、删除或改变默认值，须先改本附录，并通知各线。
- **样式**：只用 class 和 token，组件不接受 `style`。

| 组件 | 签名 | 约定 |
|---|---|---|
| `PageHeader` | `{title, lead?, actions?, toolbar?, titleId?='workbench-page-title'}` | `Page` 已经内置，页面通常不直接用；`lead` 每页必填（§2） |
| `Page` | 不变：`{title, lead?, query, actions?, toolbar?, empty?, emptyKind?, forbidden?, announce?, children}` | 头部改为 PageHeader；加载、出错时的状态沿用原有的 `Failure`/`Empty`，第 3 批统一改为 EmptyState |
| `EmptyState` | `{kind: 'loading'\|'empty'\|'filtered'\|'unavailable'\|'forbidden'\|'not_enabled', subject?, reason?, next?, onRetry?, details?, compact?}` | 标题固定为：正在加载 X / 暂无 X / 没有符合筛选条件的 X / X 暂不可用 / 无权查看 X / X 尚未启用。X 以拉丁字母开头或结尾时自动加空格。unavailable 和 forbidden 用 `role=alert`，其他用 `role=status`。只有 unavailable 显示重试按钮。`details` 收在“技术细节”里。`data-state` 属性等于 kind |
| `Card` | `{title, badge?, footer?, compact?, state?: 'unavailable'\|'forbidden', subject?, label?, children}` | 输出 `<article class="card">`。footer 贴底。有 `state` 时卡片位置不变，内容换成紧凑的 EmptyState，footer 隐藏 |
| `SectionCard<T>` | `{title, section: Section<T>, footer?, children: (data: T) => ReactNode}` | 部分响应的区块（api 的 `Section`）用它，取代 `Block` |
| `CardGrid` | `{children, min?: '14rem'\|'18rem'\|'24rem', label?}` | 等高（`align-items: stretch`），默认最小列宽 18rem |
| `MetricCard` | `{label, value, hint?, onOpen?, href?, tone?: 'ok'\|'limited'\|'fail'}` | 有 `onOpen` 或 `href` 时整卡是 `<a>`，`aria-label` 为“label value，查看”；否则是 `<div>`。不要再用按钮套卡片 |
| `KeyValue` | `{items: {key, value, hint?}[], label?}` | 输出 `<dl class="key-value">`；窄屏时改为竖排；value 里放 StatusBadge、IdText 或 Time，不放原始代码 |
| `DataTable<T>` | `{rows, columns: Column<T>[], rowKey, rowClass?, label?, className?, stickyHeader?, empty?: {subject?, filtered?}}`，其中 `Column = {label, cell, main?, num?, wide?}` | 外层有 `.table-scroll` 容器，可横向滚动、可获得焦点；main 列固定在左边；超过 20 行（或 `stickyHeader`）时表头固定；`num` 列右对齐，用于计数、金额、毫秒，版本号不算。传了 `empty` 且没有行时输出 EmptyState；不传时保留旧的空表结构（兼容） |
| `IdText` | `{value, kind?, name?, head?=8, tail?=4, copy?=true, expandable?}` | 显示为 head…tail，等宽字体；`title` 和 `aria-label` 是“kind 全称”；复制按钮的 `aria-label` 为“复制{kind} ID”，点击后图标变 ✓ 并朗读“已复制”。有 `name` 时名称作为主文字，哈希退为次要文字。可见文字中不出现 64 位全称 |
| `shorten(value, head?, tail?)` | `string → string` | 与 IdText 截断规则相同，用于不需要复制的纯文本 |
| `ScopeText` | `{kind, value}` | 先显示范围的中文标签（`scope_kind` 域），后接引用：`consumer:<hex>` 或 64 位哈希用 IdText，`*` 不显示，`eios:action:` 前缀去掉后用等宽字体 |
| `StatusBadge` | `{domain, code, tone?}` 或 `{text, tone, code?}` | 状态码一律用前一种：中文标签和 tone 取自 status-labels，原码只放在 `title` 和 `data-code` 里。后一种只用于不是状态码的说明文字 |
| `status-labels.ts` | `DOMAINS`、`statusLabel(domain, code)`、`statusTone(domain, code)`、`known(domain, code)`、`unknownCodes()`、`UNKNOWN_LABEL`、`type Domain`、`type Tone` | 每个域对应 L5 `labels.ts` 中的一张表（以术语表为准）。新增状态码时补 `labels.ts`；新增域时在 `DOMAINS` 里登记，页面里不再自建映射表。没有标签的码显示“未知状态”：开发构建打印警告，`unknownCodes()` 可以列出这些码 |
| `Counts`（pages/Observability） | `{values, domain?, subject?, labels?}` | 新代码一律传 `domain`：每项显示中文标签加数字，原码只放在 `title` 里；没有数据时显示紧凑的空状态。`labels` 是旧写法，第 2、3 批迁移完成后删除 |

**已有域（DOMAINS）**：

| 类别 | 域 |
|---|---|
| 承诺 | commitment_status、commitment_event、commitment_evidence、commitment_exception |
| Claim、召回与候选 | claim_kind、claim_resolution、recall_index、candidate_kind、candidate_status、merge_configuration、artifact_purpose |
| 任务、队列与进程 | job_status、queue、feed、process_sample |
| 意图与投递 | intent_state、outbox_state、provider_state、delivery_state |
| 控制与派发 | scope_kind、control_event、escalation_reason、data_mode、refusal_code |
| 计划、目标与预算 | plan_status、plan_outcome、goal_kind、budget_kind、cost_kind |
| 告警与备份 | alert_severity、backup_repo |
| 审核、审计与成员 | review_decision、review_outcome、audit_target、member_role |

**第 2、3 批套用顺序**：
1. 页面的状态码改用 `StatusBadge({domain, code})` 或 `statusLabel`；
2. 长 ID 改用 IdText，删除页面内自写的截断；
3. 卡片改用 CardGrid、Card、SectionCard，加上 MetricCard；
4. 表格传入 `empty`，删除页面里手写的 `<table>`；
5. 空状态改用 EmptyState；
6. 删除页面里的内联样式。

## 附录 B：第 2 批组件接口（2026-10-20 冻结，规则同附录 A）

| 组件 | 签名 | 约定 |
|---|---|---|
| `FormField` | `{id, label, hint?, error?, width?: 'full'\|'md'\|'sm' = 'md', children}` | 标签在上，下面依次是控件、提示（`${id}-hint`）、错误（`${id}-error`，`role=alert`）。控件由调用方传入，`id` 必须一致。宽度上限：md 32rem，sm 10rem。类名中保留 `field`，旧的选择器仍然有效 |
| `describedBy(id, {hint?, error?})` | `string \| undefined` | 生成控件的 `aria-describedby` |
| `ChoiceGroup<V>` | `{legend, name, options: {value, label, hint?}[], value, onChange, layout?: 'stack'\|'cards' = 'stack', hint?}` | 单选：fieldset 加 legend，每项一行，提示缩进放在文字下方。选项多于 3 个或带说明时用 `cards`。不和其他控件放在同一行 |
| `CheckboxGroup` | `{legend, hint?, items: {key, label, hint?, checked, onChange}[]}` | 相关的复选框放在同一个 fieldset 里，每项一行；单个开关也用它（一个 item），不再写裸的 `label.choice` |
| `Segmented<V>` | `{label, options: {value, label}[], value, onChange}` | 查看方式切换（例如 Table / Graph），按钮带 `aria-pressed`；放在标题下面，不和 Tabs 并列 |
| `Tabs` | 新增可选属性 `id` | 传了 `id` 时，每个标签的 id 为 `${id}-tab-${key}`，`aria-controls` 指向 `${id}-panel`；不传时输出不变 |
| `TabPanel` | `{id, value, children}` | `role=tabpanel`，由当前选中的标签标注（`aria-labelledby`），可获得焦点；与同一 `id` 的 Tabs 配对使用 |
| `KeyValue` | 新增可选属性 `className` | 兼容需要沿用旧 class 的地方，例如 `facts` |
| `status-labels` 新增的域 | `persona_status`、`persona_source`、`consent_mode`、`outreach_purpose`、`ai_disclosure_mode`、`ai_when_asked`、`takeover`、`human_request`、`read_purpose` | `read_purpose` 暂时借用 `audit_target` 的表；`data_assets`、`world_model` 这类用途码还没有标签，会显示“未知状态”，待术语表补充 |

**表单布局约定**（§9 的落地写法）：
- 长表单用 `<section className="form-section"><h3>…</h3>…</section>` 分节。
- 成对的控件（开始和结束、两个上限）放进 `<div className="field-pair">`，每个控件各自用 FormField，各有可见的标签。
- 规则行用 `fieldset.rule-row`，行内每个控件也用 FormField，各有可见标签，不靠 `aria-label` 代替。
