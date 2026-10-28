# 工作台与顾客端 UI/UX 全面审查（L9，2026-10-28，基于 main 17aa74c1）

## 0. 范围、方法与前次巡检的关系

**范围**：`apps/web` 全部页面和组件。
- 顾客端：登录（`Login.tsx`）、已登录页（`Account.tsx`）、WebChat、消息回执。
- 工作台：登录、总览、消费者（列表、详情、会话）、承诺、Action/异常、审批、计划、目标、联系限制、企业世界模型、数据资产、数据健康、知识工作台与审核证据、告警、运行状态、审计、设置（含真实派发面板）、成员与账号、账号安全、运营人设、能力目录、实验空间，以及邀请页和重置页。

**方法**：
1. 逐文件读源码。
2. 在本机起仓库自带的合成烟测栈：`NEXLOOP_DEV_SMOKE_SEED=large scripts/dev/workbench_smoke.sh`。它只监听 loopback，使用一次性 PostgreSQL、合成租户和 40 位合成客户。用内置浏览器以合成 owner 和合成顾客身份逐页走查，宽度为 1440 和 375，浅色和深色各看一遍。
   - 截图只留在本机，不入库，因为仓库是公开的。
   - 没有连接 stage，没有登录任何真实账号。
3. 检查口径借用 design-critique（层级、一致性）、accessibility-review（WCAG 2.1 AA）和 ux-copy（术语、报错）。
4. 对比度按 token 的十六进制值计算（WCAG 相对亮度公式）。

**与前次巡检的关系**：10-20 的 `workbench-ui-audit.md` 和 `workbench-ui-spec.md` 已经落地 kit 第 1、2 批，包括 PageHeader、Card、EmptyState、IdText、status-labels、DataTable 改造和 FormField。本文只记录**现在仍然存在**的问题，以及前次没覆盖的问题，主要是交互、可访问性和顾客端。前次第 3 批遗留的清理项归在 C1。

**严重程度**：
- **P0 阻断**：页面不可用或数据错乱；
- **P1 重要**：主要任务明显受阻，或不满足 WCAG AA；
- **P2 一般**：一致性、文案和效率问题；
- **P3 打磨**。

**约束标注**：
- “**等 L8**”：问题落在 `Login.tsx`、`Account.tsx`（L8 正在这两个文件里做单点登录），等 L8 合入后再改。
- “**需后端**”：需要改接口、契约或迁移，由调度员另行派工。

## 1. 问题清单（按严重程度）

### P0

| 编号 | 页面/组件 | 现象 | 建议改法 | 预计改动文件 |
|---|---|---|---|---|
| A1 | Action / 异常（`Remediations`） | **打开页面后整个工作台白屏**，已在浏览器里复现。<br>控制台报 `Rendered more hooks than during the previous render`。<br>原因：`Remediations` 在 `if(query.isPending) return …` 之后才调用 `useState`。<br>应用没有错误边界，一个区块出错就卸载整个 `WorkbenchApp`，侧栏和导航都会消失，只能刷新页面。<br>现有单测用静态渲染、数据一开始就在，所以没有覆盖到这条路径。 | 把 `useState` 移到所有提前 return 之前。<br>在页面外加一层错误边界：只替换内容区，显示“此页出错”，提供重试，并把技术细节折叠起来；侧栏保留。<br>补一个测试：数据从加载中变为已加载时，组件能重新渲染。 | `workbench/pages/Actions.tsx`、`workbench/WorkbenchApp.tsx`（新增 `ErrorBoundary`，放在 `workbench/kit.tsx`） |

### P1

| 编号 | 页面/组件 | 现象 | 建议改法 | 预计改动文件 |
|---|---|---|---|---|
| B1 | 全局焦点环 | `--focus` 是 #93b4ff，在白底上对比度只有 2.06:1，在 `--bg` 上是 1.93:1。<br>不满足 WCAG 1.4.11（非文本对比度至少 3:1），键盘用户看不清焦点在哪里。 | 浅色改为 `--focus:#2463eb`（5.17:1）。<br>outline 保持 3px，外加 1px 白色间隔（box-shadow），在深色块上也能看清。 | `styles.css` |
| B2 | 表单控件边框 | 输入框、下拉框和文本框用的是 `--border`，对比度在浅色下 1.23:1、深色下 1.52:1。<br>走查截图里，登录页的输入框几乎看不出边界（1.4.11）。 | 新增 `--control-border`（浅色 #7b889c，3.59:1；深色 #5b6f8f，3.52:1），只用于表单控件；卡片和表格边框继续用 `--border`。 | `styles.css` |
| B3 | 语义提示类没有样式 | 页面用了这些 class，但 CSS 里没有对应规则：<br>`.warning` 只在 `.review` 下有样式；`.impact-summary` 是所有受治理表单的“影响：…”提示；还有 `.one-time-secret`、`.kv`、`.workbench-summary`、`.refusal-reason`，以及顾客端的 `.banner`。<br>结果：<br>① 告警页“告警系统不可用…”和总览“已暂停：…”看起来是普通正文；<br>② 每个写操作的“影响”说明和其他说明没有区别；<br>③ 一次性重置码或邀请令牌没有醒目框；<br>④ 数据健康每张卡的 `dl.kv` 走浏览器默认样式，dd 缩进 40px，每个值单独占一行（走查截图）。 | 新增 `Notice` 组件，`tone` 取 `info`、`warning`、`danger`、`success`，带图标，按 tone 分配 `role`（`warning`/`danger` 用 alert，其他用 note 或 status）。用它替换上述 class。<br>数据健康的 `dl.kv` 改用 kit 的 `KeyValue`。 | `workbench/kit.tsx`、`styles.css`、`pages/Observability.tsx`、`pages/Overview.tsx`、`pages/DataHealth.tsx`、`actions.tsx`、`pages/Members.tsx`、`pages/Approvals.tsx`、`RealDispatchPanel.tsx`、`Review.tsx` |
| B4 | 按钮层级 | 表单的 submit 一律是主色蓝，没有危险态。“撤销成员”“擦除个人数据”“停用这位 owner”“拒绝”“取消承诺”和“保存”看起来一样。<br>反过来，真实派发确认框里的“取消”却用了红色危险样式（`.rd-close`）。<br>页面上也没有次要按钮的约定。 | 定义三种按钮：`primary`、`secondary`、`danger`（class 加 token）。`Form` 新增 `tone?: 'danger'`，所有不可逆或撤权类操作都传 `danger`。<br>确认框里的“取消”一律用 `secondary`。 | `styles.css`、`actions.tsx`（`Form`）、`pages/Members.tsx`、`pages/Approvals.tsx`、`RealDispatchPanel.tsx`、`Review.tsx` |
| B5 | 一键执行、无需确认的行内操作 | 这些操作点一下就立刻执行，并且用页面写死的原因：<br>① 撤销邀请（不可逆）；<br>② 人设“停用”或“重新启用”，原因写死为“负责人停用”；<br>③ 沟通偏好“不采纳”或“恢复采纳”；<br>④ 审批“撤回”，原因写死为“发起人撤回”。<br>误触没有撤销途径，审计里的原因也不是本人写的。 | 新增 `ConfirmAction`：点一次后原地展开确认条，写清对象、影响和原因输入框，再点一次“确认”才提交，可以按 Esc 或点“取消”收起。<br>原因改为本人填写；如果后端允许空原因，就预填建议文字。 | `workbench/kit.tsx`、`actions.tsx`、`pages/Members.tsx`、`pages/Personas.tsx`、`pages/Approvals.tsx` |
| B6 | 按钮禁用却不说原因 | 很多提交按钮在条件不满足时直接变灰（不透明度 .55），看不出缺什么。例如：<br>审批的“批准/拒绝”要先填原因；单人模式；人设发布要填标识、名字、语气和原因；邀请时用户名不合规。<br>审批表格里“批准”“拒绝”是两个独立表单，共用一个原因框，但表格里看不出这层关系。 | `Form` 新增 `disabledReason`。禁用时在按钮旁显示一行提示（如“填写原因后可提交”），按钮用 `aria-describedby` 关联到这行提示。 | `actions.tsx`、`pages/Approvals.tsx`、`pages/Personas.tsx`、`pages/Members.tsx` |
| B7 | 路由切换 | `document.title` 始终是“NexLoop”，不符合 WCAG 2.4.2（A 级）。<br>切换页面后焦点仍停在侧栏链接上，读屏用户不知道内容已经换了。<br>也没有“跳到主要内容”链接：侧栏有约 25 个链接，键盘用户每页都要逐个跳过。 | 路由变化时设置 `document.title = 页面名 · 负责人工作台 · NexLoop`，并把焦点移到页面的 h1（h1 设 `tabIndex=-1`）。<br>在页头最前面加一个 skip link。 | `workbench/WorkbenchApp.tsx`、`workbench/kit.tsx`（PageHeader）、`styles.css` |
| B8 | 审批页 | 没有走 `Page` 外壳：<br>① h1 吃到顾客端的全局样式，字号 44px，没有卡片底，和其他页面明显不一致（走查截图）；<br>② 没有 lead；<br>③ 加载和出错时用的是旧的 `Empty`。<br>内容方面：<br>① 摘要显示成 `key=value` 原码；<br>② 发起人显示完整的 principal；<br>③ digest 整串显示；<br>④ “执行被拒（code）”显示原始码；<br>⑤ “owner”是英文。 | 改用 `Page` 和 `PageHeader`。<br>三栏改为 Tabs，每个 tab 带计数，默认选“待我审批”。<br>摘要的 key 用标签表映射，没有映射的放进“技术细节”；发起人用 `IdText`；digest 显示 12 位并可复制；拒绝码用 `refusal_code` 域翻译。<br>发起人的可读名需要后端在审批视图里返回 username（**需后端**）。 | `pages/Approvals.tsx`、`status-labels.ts`/`labels.ts` |
| B9 | 成员与账号 | 每行的“操作”折叠里会叠放最多 8 个完整表单。例如一位在用的运营成员：改角色、停用、撤销、签发重置码、提升为 owner，每个都带原因框，展开后有半屏长，而且全部堆在表格的一个单元格里。<br>用词混用：“owner”和“负责人”；“账户 {account_status}”显示原始码。<br>邀请区有两个表单共用一个用户名，第二个写着“用户名与有效期同上”。 | 每行改为“选择操作”下拉，选中后只展开这一个表单，表单放在表格下方，并注明“对象：××”。危险操作用 B4 的危险态。<br>“owner”统一为“负责人”（术语表“成员角色”一行）；`account_status` 加标签映射。<br>邀请改为一个表单，角色选项里加上“负责人（需审批）”。 | `pages/Members.tsx`、`labels.ts` |
| B10 | 为单个客户指定人设 | 必须手工粘贴 64 位客户 ID，提示文字是“在消费者详情页复制客户 ID 后粘贴”。这是主要任务，步骤多，而且容易粘错。 | 把指定入口放到客户详情的“运营人设”区块里，客户已经确定，只需选人设和填原因。<br>人设页保留规则和默认人设；单个客户的指定改成一句提示加跳转。 | `pages/Personas.tsx`（`ConsumerPersona`、`AssignForm`）、`pages/Consumers.tsx` |
| B11 | 客户详情的信息层级 | 首屏依次是联系限制、运营人设、沟通偏好、说明段落、属性，多数都是空状态。最常用的“会话”和“承诺”被挤到首屏以下，接管和暂停这些操作在页面最底部。<br>标题写“消费者详情”，面包屑写“客户详情”。 | 调整顺序：客户摘要 → 会话（最近 5 个）→ 承诺摘要 → 操作 → 人设与偏好 → 属性 → 联系限制（受限时提到第二位，并显示警告 Notice）。<br>为空的区块收成一行。<br>标题和面包屑统一为“客户详情”。 | `pages/Consumers.tsx`、`WorkbenchApp.tsx`（crumbs） |
| B12 | 顾客端 WebChat | 把内部概念直接展示给顾客：<br>① 自己的消息显示“发送者：synthetic-principal-a”，即 principal 标识；<br>② “执行模式：确定性测试（不代表真实模型或真实渠道）· 企业服务入口 · 业务世界：real”；<br>③ “仅显示已提交消息。‘已接受’表示消息入库……可查询实际治理回执”；<br>④ 每条消息下都有“查询处理与交付回执”按钮，回执文案里有“治理回执”“Provider”；<br>⑤ 标题“消费者对话”是企业内部视角。<br>首屏的消息列表被吸底的输入区盖住，只露出一条细边（1440×900 截图）。 | 顾客视角的文案：<br>① 自己的消息显示“我”；<br>② 执行模式和业务世界只在非 real 或测试档时显示一枚“测试环境”徽标；<br>③ 说明段落收进“关于消息状态”折叠；<br>④ 回执按钮改为次要的文字链接“查看处理进度”，结果文案去掉“治理”“Provider”；<br>⑤ 标题改为“我的对话”。<br>消息区给最小高度，输入区只在消息超过一屏时吸底。<br>注意：`messagePresentation` 等文案可能被 AT-015 等验收锁定，改之前要逐条核对测试和验收口径。“当前工作空间 00000000…”在 `Account.tsx` 里（**等 L8**）。 | `WebChat.tsx`、`MessageReceipt.tsx`、`chat-api.ts`（`messagePresentation`）、`styles.css`；`Account.tsx`（等 L8） |
| B13 | 术语不一致 | ① “口令”和“密码”：工作台登录框写“密码”，旁边的链接写“忘记口令”，重置页和邀请页写“口令”；<br>② “owner”和“负责人”：成员、审批页面写 owner，其他页面写负责人；<br>③ “审核员”和“审核人”；<br>④ “消费者”“客户”“顾客”：导航用“消费者”，页面用“客户”，会话里写“顾客”。 | 按术语表统一：<br>① 一律用“密码”，与顾客端登录一致；<br>② 角色名一律用“负责人”；<br>③ 一律用“审核人”；<br>④ 导航和页面标题用“客户”。会话里来信一方仍写“顾客”还是也改成“客户”，需要负责人拍板，暂按术语表“客户”执行。<br>注意：这一项会改很多测试的文本断言。 | 多个文件（`AccountPages.tsx`、`WorkbenchApp.tsx`、`route.ts` 的 `PAGES`、`Members.tsx`、`Approvals.tsx`、`Knowledge.tsx`、`RealDispatchPanel.tsx`，以及相关测试） |

### P2

| 编号 | 页面/组件 | 现象 | 建议改法 | 预计改动文件 |
|---|---|---|---|---|
| C1 | 旧组件残留（前次规范第 3 批未完成） | `Empty`（旧，审批页和 `Page` 内部在用）、`Failure`、`Block`、`Short`（3 处）、`IdRef`/`shortId`/`memberAlias`（审计页）、`when()`/`beijing()`（11 处）、`NoWrap` 和 `IdRef` 里的内联 style，这些仍在用。<br>同一页面上并存两套空状态和两种 ID 写法。 | 按规范附录 A 的套用顺序收尾，做完后删除旧组件。 | `pages/Page.tsx`、`pages/PageOps.tsx`、`pages/Observability.tsx`、`pages/Consumers.tsx`、`pages/Commitments.tsx`、`pages/DataHealth.tsx`、`pages/Knowledge.tsx`、`actions.tsx`、`ui.tsx` |
| C2 | 空状态太啰嗦 | 卡片内的每个空小节都输出“○ 暂无X”加“有新记录时会出现在这里。”两行。总览有 5 处，数据资产有 6 处，客户详情有 4 处，同一句话在一屏里重复多次，真正的数字反而被淹没。 | 卡片内的紧凑空状态只保留一行“暂无X”，不显示下一步说明；整页为空时仍显示完整的 EmptyState。 | `workbench/kit.tsx`（`EmptyState` 的 compact 分支）、`styles.css` |
| C3 | Tab 停留点过多 | `IdText` 的 `<code>` 设了 `tabIndex=0`，后面还有一个复制按钮，所以每个 ID 占两个 Tab 停留点。消费者列表每行至少 3 个，50 行就是 150 多次 Tab。<br>`.table-scroll` 不需要横向滚动时也可以获得焦点。 | 去掉 code 的 `tabIndex`，完整值改由复制按钮的 `aria-label` 和 title 提供。<br>`.table-scroll` 只在实际溢出时设 `tabIndex=0`（挂载后测量一次，窗口大小变化时再测）。 | `workbench/kit.tsx`、`workbench/DataTable.tsx` |
| C4 | 设置页重复列出成员 | “成员与角色（只读）”把成员显示成截断的 principal，例如“synthetic-st…ipal”，看不出是谁。“成员与账号”页显示的是用户名，两页信息重复且对不上。 | 删掉设置页的成员表，改为一行摘要加跳转：“N 位成员，在‘成员与账号’管理”。 | `pages/Settings.tsx` |
| C5 | 真实派发面板 | ① 界面上出现部署层环境变量名 `REAL_DISPATCH_ENABLED`、主机角色名 `APP_HOST` 和“compose 环境变量”；<br>② 状态徽标“未设置（关）”折成两行；<br>③ “取消”用了红色危险样式（见 B4）。 | 改为“部署层开关（由运维在部署配置中设置，工作台读不到）”，变量名放进“技术细节”。<br>徽标加 `white-space:nowrap`。 | `RealDispatchPanel.tsx`、`styles.css`、`actions.tsx`（DONE_TEXT） |
| C6 | 告警页 | ① 没有 lead；<br>② 正文写着“只针对真实世界（real）”；<br>③ 没有可静默的告警时，“静默（可选）”标题下面是空的。 | 补 lead；“real”改为“真实数据”；没有表单时不渲染这个小节。 | `pages/Observability.tsx`、`pages/PageOps.tsx` |
| C7 | 能力目录 | 所谓“可读名”只是把英文按点号拆开，例如“ai disclosure set”，负责人照样读不懂。<br>“来源任务”列显示 NX-0xx，是内部任务号。 | 主名称改用业务 Action 的中文名映射，没有映射的回退到技术名；来源任务列移进技术细节或删掉。 | `pages/Capabilities.tsx`、`labels.ts` |
| C8 | 消费者列表 | “客户”列已经是“客户 0717·1d0（未登记名称）”，旁边的“ID”列又显示同一个哈希。<br>每行都重复“（未登记名称）”。<br>默认排序“按 ID”对负责人没有意义。 | 删掉 ID 列，复制按钮放进客户列。“未登记名称”改为列头下的一句说明，或者只用一枚小徽标。<br>默认排序改为“最近会话”，这一项**需后端**提供排序键；在那之前保留现状。 | `pages/Consumers.tsx` |
| C9 | 窄屏外壳 | 375 宽时，品牌、时区和菜单按钮占了约 230px 才轮到内容。<br>菜单按钮不吸顶，按 Esc 不能关闭菜单，打开菜单后焦点也不进入导航。 | 窄屏时品牌行和菜单按钮合成一行；时区说明挪到页脚。菜单支持 Esc 关闭，打开后焦点移到第一个链接，关闭后回到按钮。 | `workbench/WorkbenchApp.tsx`、`styles.css` |
| C10 | 全局元素选择器外溢 | 顾客端为 `header`、`main`、`h1`、`form`、`label`、`.note`、`.error` 写的全局样式会漏进工作台。审批页 44px 的 h1 就是这么来的；客户近期往来的 `<header>` 也继承了 32px、650 字重，要靠覆盖才压住。 | 顾客端样式收进 `.shell:not(.workbench)` 作用域，或者给顾客端加 `.customer` 根类。 | `styles.css`、`App.tsx`（加根类，Login 和 Account 不动） |
| C11 | 报错文案里的 HTTP 码 | `STATE_TEXT` 和 `WRITE_TEXT` 把（403）（404）（409）（422）写进正文，例如“当前账号无权执行此操作（403），未执行。” | 正文只留人话，状态码放进 EmptyState 的“技术细节”或 title。 | `workbench/api.ts`、相关测试 |
| C12 | 知识工作台 | ① 403 文案里出现 `ontology.schema.review`、`nexloop.knowledge.read`、`workbench_roles`；<br>② 评估区出现 `classify_change`、“L0/L1 指标”；<br>③ 没有 lead；<br>④ 空队列用的是旧文案“暂无记录。”；<br>⑤ “停止”演进开关是灰色小按钮，没有危险态。 | 权限说明改成角色名的人话，代码收进技术细节；补 lead；改用 EmptyState；停止按钮用 danger，并走确认（B5）。 | `pages/Knowledge.tsx`、`pages/SemanticReleases.tsx` |
| C13 | 加载状态 | 进入工作台时先在白底上显示一行“正在加载…”。`Remediations` 的“补救暂不可读。”和证据面板的“证据暂不可读。”都没有重试按钮。`WithMember` 在每个表单位置都单独显示一行“正在加载…”。 | 会话检查期间显示外壳骨架；各区块统一用 `EmptyState`（loading 或 unavailable，带重试）；`WithMember` 加载时不渲染任何内容。 | `WorkbenchApp.tsx`、`pages/Actions.tsx`、`actions.tsx` |
| C14 | 工作台登录卡 | “忘记口令？请 owner 为你签发重置码”里的链接是浏览器默认的蓝紫色，没走 token；用词问题见 B13。<br>L8 的单点登录很可能也要改这张卡（`WorkbenchApp.tsx` 里的 `WorkbenchLogin`）。 | 链接颜色用 `--accent`。登录卡的结构调整**等 L8 合入后**再做，本线只改全局 token。 | `styles.css`；`WorkbenchApp.tsx`（等 L8） |
| C15 | 运营人设编辑器 | 人设列表下面直接展开约 15 个字段的发布表单，列表为空时整页几乎都是表单。 | 改为“新建人设 / 基于此版本编辑”按钮，点击后再展开表单；有已发布版本时默认收起。 | `pages/Personas.tsx` |
| C16 | 中英混写 | 数据健康、运行状态、数据资产里有 profile、embedding、Agent Host、WAL、“Claim 匹配空转”等。 | 按术语表处理：实体名（Claim、Artifact）保留英文，内部组件名改中文（如“嵌入配置”“智能体主机并发”“WAL 归档”改为“日志归档”）。 | `pages/DataHealth.tsx`、`pages/Observability.tsx`、`pages/DataAssets.tsx` |

### P3

| 编号 | 页面/组件 | 现象 | 建议改法 | 预计改动文件 |
|---|---|---|---|---|
| D1 | `styles.css` | ① 早期规则里还有写死的 14px、8px、9px 圆角等，没有走 token；<br>② `prefers-reduced-motion` 规则重复了两次；<br>③ `.workbench-layout` 和 `.time-rel` 各定义了多次。 | 第一片整理 token 时一并清掉。 | `styles.css` |
| D2 | `MetricCard` | 没有 `href` 时回退成 `href="#"`。 | 只有 `onOpen` 时渲染成按钮。 | `workbench/kit.tsx` |
| D3 | `Review.tsx` | `ReviewWorkbench`、`CandidateView`、`QueueList` 在应用里已经没有入口（只有测试引用），工作台只用到 `DecisionBar`。 | 确认没有路由后移除，或者在文件里注明只供测试使用。 | `Review.tsx`、`test/review.test.ts` |
| D4 | 深色模式 | 整体可用，对比度见附录。 | — | — |

### 补充（10-28 对照 L2 的参考审查 `l2/ui-ux` df192a7b 后补入）

L2 被撤回前也做过一版审查，内容与本文大体重合：它的 P0-1、P0-2 对应本文 A1，P1-2/3 对应 B8，P1-5 对应 B3，P1-6 对应 B12，P1-7 对应 C12，P1-8 对应 C7，P2-1/2 对应 C1、C13。下面几条是本文原先漏掉的，编号接着往下排。

| 编号 | 严重 | 页面/组件 | 现象 | 建议改法 | 预计改动文件 |
|---|---|---|---|---|---|
| B14 | P1 | 禁用的主按钮和危险按钮 | 现在用 `opacity:.55` 表示禁用，蓝底白字叠半透明后只剩约 2:1。WCAG 对禁用控件不做对比度要求，但负责人常常看不清按钮上的字，也就不知道这是什么操作；配合 B6 的禁用原因，按钮本身也应该能读清。 | 禁用态改用中性底和中性字（`--neutral-soft` 加 `--neutral-text`，6.9:1），不再靠透明度。 | `styles.css`、`RealDispatchPanel.tsx`（开启按钮改用 `primary` class，不再靠 `:not()` 选择器上色） |
| C17 | P2 | 成员与账号 | 文案里露出迁移号“运维重置工具 0198”；列名“owner 投票资格”中英混写；角色徽标前的“–”是 neutral tone 的符号，看上去像减号。 | 去掉迁移号；列名并入 B13 的术语统一；neutral tone 的徽标不显示符号。 | `pages/Members.tsx`、`ui.tsx` |
| C18 | P2 | 总览 | 卡片名“待核对的外部动作”和导航名“Action / 异常”不一致；首屏放着“控制修订号”这种内部概念。 | 卡片名与导航对齐；修订号移到目标页（目标页已经有）。 | `pages/Overview.tsx` |
| C19 | P2 | 企业世界模型的关系图 | `SchemaGraph.tsx` 仍有 7 处内联 style（前次规范 R8 的余项）。 | 移入样式表，用 token。 | `graph/SchemaGraph.tsx`、`styles.css` |
| D5 | P3 | 品牌 | 深色模式下 Logo 放在白色圆角块里，显得突兀。 | 需要一份深色版 Logo 资源，由负责人提供或批准后再改。 | `Brand.tsx`、`public/brand/` |

L2 的 P1-9（单选、复选框 18px，部分链接高 16–20px）对应的是 WCAG 2.5.8（2.2 版的 AA，2.1 版没有这一条），按 P2 处理，并入 C9 窄屏一片：选项整行作为点击区，表格链接加纵向内边距。

## 2. 需要后端、契约或其他线处理的事项

| 编号 | 事项 | 说明 |
|---|---|---|
| X1 | 审批视图返回发起人的用户名或成员别名 | 用于 B8；现在只有 `requested_by` 的 principal。 |
| X2 | 审批摘要字段的标签 | 用于 B8。可以由策略随 `summary` 下发 `{key,label}`，也可以由前端维护映射表；如果由后端下发，就要改契约。 |
| X3 | 消费者列表的“最近会话时间”排序键 | 用于 C8。 |
| X4 | 顾客端会话的标题或时间字段 | `WebChat.tsx` 注释里已经提过；用于 B12。 |
| X1–X4 前端 | 10-28 已按 L2 的接口说明（`l2/ui-backend-x1-x4` fa8232dd，`docs/design/ui-backend-x1-x4.md`）接好 | 新字段都按可选处理，旧服务端照旧显示。审批：发起人显示用户名（为 null 时显示“成员信息不可用”：已擦除、没有账号或不是本租户成员），摘要标签优先用服务端的；客户列表：有时间时出现“最近会话”列，服务端返回该字段时排序新增“最近有会话的在前”；顾客对话按“开始时间 · 共 N 条”命名。两边合入后要在烟测栈上联调一次，并把客户列表的默认排序改为“最近有会话的在前”（C8，旧服务端不认这个排序键，所以先不改默认值） |
| X5 | 等 L8 | `Login.tsx`、`Account.tsx` 和工作台登录卡（B12 的“当前工作空间”、C14）。 |

## 3. 实施切片（第二阶段，按严重程度，每片一个提交）

| 片 | 内容 | 覆盖 |
|---|---|---|
| S0 | P0：修 `Remediations` 的 hooks 顺序，加内容区错误边界，补“加载 → 已加载”的重渲染测试 | A1 |
| S1 | 设计 token 与基础组件：`--focus`、`--control-border`、按钮三种 tone、`Notice`、`ConfirmAction`、`Form` 的 `tone` 和 `disabledReason`；替换无样式的语义类；数据健康改用 `KeyValue`；清理 D1 | B1–B4、B6（组件）、D1、C5 部分 |
| S2 | 外壳可访问性：页面标题、焦点移到 h1、skip link、窄屏菜单的 Esc 和焦点、窄屏页头；禁用按钮可读 | B7、C9、B14 |
| S3 | 审批页套用 `Page`；三栏仍放在同一页（已批准待执行的请求不能藏在 Tab 后面），标题带计数、顶部一行汇总，单人模式收进折叠；摘要、发起人和拒绝码改为可读形式；错误边界只显示错误类型和追踪号（L6 对 S0 的建议） | B8（X1、X2 之前先用前端映射）、A1 补充 |
| S4 | 成员页：一次只展开一个操作，危险态加确认；邀请合并为一个表单；角色名统一为“负责人” | B9、B5 部分 |
| S5 | 行内一键操作加确认（撤销邀请、人设停用、偏好、撤回审批），原因由本人填写 | B5 |
| S6 | 客户详情的层级调整：会话（最近 5 个，可展开全部）→ 承诺 → 操作 → 人设与偏好 → 属性 → 联系限制（生效时提到第二位并显示警告）；空区块收成一行；暂停主动联系改为提示条。为单个客户指定人设移到客户详情（客户已确定，只选人设和填原因；没有可用人设时不显示表单），人设页的“分配”只保留规则和默认，并给出去客户列表的入口 | B10、B11 |
| S7a | 术语统一（负责人 10-28 决定）：界面一律用“客户”（代码标识仍是 Consumer），登录凭证叫“密码”，owner 叫“负责人”，审核角色叫“审核人”；测试里的文案断言一并更新。核对过验收清单，没有锁定这些文案。顾客端 WebChat 的标题留给 S9 | B13、C17 部分 |
| S7 | 文案：HTTP 码移入技术细节（读取失败和写入结果都在“技术细节”里写 HTTP 码和代码）；知识工作台的权限说明改成角色名、补 lead 和空状态、评估区去掉内部名、停止演进开关改为危险态并就地确认；告警页补 lead、“real”改为“真实数据”、没有可静默的告警时不显示静默小节；数据健康、运行状态、数据资产的内部组件名改中文（嵌入配置、智能体主机并发、日志归档）；中性徽标不再显示“–”；总览卡片名与导航对齐、去掉控制修订号 | C6、C11、C12、C16、C17、C18 |
| S8 | 收尾：`Page` 的空、失败状态改用 `EmptyState`（失败的 HTTP 码在技术细节里），删除旧的 `Empty`、`Block`、`Short`、`IdRef`、`NoWrap` 和 `shortId`（`when()`/`beijing()` 是时间格式函数，不是组件，保留）；卡片内的紧凑空状态只留一行；ID 不再占两个 Tab 停留点，表格滚动区只在溢出时可获得焦点；设置页的成员表改为一行摘要和跳转；会话检查期间显示工作台骨架，`WithMember` 加载时不渲染，证据读取失败带重试；关系图的内联样式移入样式表并给节点加焦点样式。C15 已在人设页那一片（814f02a0）完成 | C1–C3、C4、C13、C15、C19 |
| S9 | 顾客端 WebChat 与处理进度（负责人 10-28 决定：客户只看到对话和简单状态）。核对过 AT-010（说明可交付范围，保留）、AT-015（只锁显示一致性）、AT-033（结果不明时提示不要重复发送，保留）；措辞只被 NX-016 的前端证据测试 `scope-denial.node-test.mjs` 锁定，已随之更新。不含 `Account.tsx`（等 L8） | B12 |

每片都会：
- 跑 `pnpm exec vitest run apps/web/test` 和 `apps/web` 的构建（`tsc --noEmit && vite build`）；
- 带关键交互和可访问性断言；
- 在烟测栈上复看改动的页面。

## 附录：对比度（WCAG 相对亮度）

| 组合 | 比值 | 结论 |
|---|---|---|
| 浅色：`--muted` / `--surface` | 5.12 | 通过 |
| 浅色：`--muted` / 行悬停 `--hover` | 4.73 | 通过 |
| 浅色：`--accent` / `--accent-soft`（导航当前项） | 4.53 | 勉强通过 |
| 浅色：`--focus` / `--surface` | **2.06** | 不满足 1.4.11（B1） |
| 浅色：`--border` / `--surface`（输入框边框） | **1.23** | 不满足 1.4.11（B2） |
| 深色：`--border` / `--surface`（输入框边框） | **1.52** | 不满足 1.4.11（B2） |
| 深色：`--muted` / `--surface` | 7.89 | 通过 |
| 深色：`--focus` / `--surface` | 8.36 | 通过 |
| 各状态徽标的文字 / 底色（浅色和深色） | 5.4–12.4 | 通过 |
