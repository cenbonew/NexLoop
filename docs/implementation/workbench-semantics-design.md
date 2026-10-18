# 工作台业务语义第 7 批设计稿（F03 / F04 / F05 / F09 / F10）

状态：D1–D6 已由负责人 2026-10-18 按推荐拍板，并在分支 `l6/workbench-semantics-7` 实现（PR #9）；D2 的导出例外见 §7。分支 `l6/workbench-semantics-design`，从 main `f307387c` 切出。来源是负责人 2026-10-18 的工作台走查报告（私有，`local-only/audit/walkthrough-20261018/report.md`），本稿只引用问题编号，不复制报告内容。

实现排在 L3（共用组件）和 L4（页面顺序）之后。本稿只定数据与语义，表格、筛选栏、面包屑等共用件用 L3 的产出，不另造。

## 0. 现状核对（代码事实）

| 项 | 事实 | 位置 |
|---|---|---|
| 承诺列表接口 | `GET /api/v1/workbench/commitments?consumer_id=&all=`。SQL verb `commitments` 返回每个承诺的 `nexloop_commitment_view`（加 `consumer_id`、`source_message_id`），按 `prepared_at` 排序；另返回 `exceptions` | `workbench_http.py:157`；0130 第 154 行；view 在 0111 第 692 行 |
| 承诺对象属性 | `made_to, made_by{sender_kind,sender_principal,role_ref,run_id,intent_id}, content_ref{claim_id,message_id,span,content_hash}, promised_at, due_at, due_precision, condition, related_goal_ref, supersedes, status, late, fulfillment_basis, fulfillment_evidence`。**没有** `predicate`、`precision`、`content`、责任人字段 | `business-object-types.v1.json`；0111 第 555 行 |
| 承诺正文 | 即 view 的 `quote`：来源 Claim 的引文，只在来源 Message 仍存在时取（0111 D7）。Python `_commitment` 再按来源 Message 的 `workbench-member-v1` READ 判定：读不到时 `quote=null`、`quote_status='restricted'`，并写一条读取审计 | `workbench_reads.py:326` |
| 前端承诺列表 | 主列取 `properties.predicate`，该属性不存在，所以**总是退回显示 ID**（走查 10/11 的现象）。期限精度读 `properties.precision`，实际名是 `due_precision`，所以“最晚界”永远不出现 | `pages/Commitments.tsx:15,35` |
| 消费者详情的承诺小节 | 取 `properties.content`（不存在），否则取 `c.quote` | `pages/Consumers.tsx:50` |
| F05 | “已结束”标签请求 `?all=true` 后在前端过滤；但 SQL 的 `exceptions` **不按承诺状态过滤**，也不随标签变化，所以“已结束无记录”时下方仍列出未结束承诺的异常 | 0130 第 159 行；`Commitments.tsx:33` |
| F04 列表 | `WorkbenchViews._display_name`：先看 `display_name` 是否 `present`，没有就判为 `absent`（显示“未登记名称”），之后才看 `readable` | `workbench_reads.py:221` |
| F04 详情 | `consumer()`：先看 `display_name` 是否在 `withheld`（判断依据是 `not readable`，**不看 present**），在就判为 `withheld`（显示“名称受限”） | `workbench_reads.py:248` |
| F04 根因 | 对 owner 受限、且对象上又没有 `display_name` 的客户，列表先判 present 得到“未登记”，详情先判 readable 得到“受限”，两处判断顺序相反。另外 `nexloop_workbench_consumer_fields` 对不可读字段也返回 `present`，向成员泄露了受限字段的有无 | 0123 第 113 行 |
| 别名 | `consumerAlias(id)` 为 `客户 xxxx·yyy`，由 ID 确定，稳定 | `api.ts:146` |
| 分页现状 | consumers：`after` + `limit`，按 object_id 游标，无总数、无筛选。commitments：不分页、无总数。actions：只有 `state`、`consumer_id`、`limit`。审计：`before` | `workbench_http.py` |
| 路由 | `/workbench/<page>[/<id>]`，查询串不入路由 | `route.ts` |
| 契约 | `packages/contracts/commitment-view.schema.json` 的 `additionalProperties: false`，必填 12 个键。工作台其他列表没有公开契约，只有 `api.ts` 的严格解析 | — |

## 1. F03 承诺列表的业务语义

目标：不进详情就能回答“对谁、承诺了什么、何时到期、现在什么状态、谁来处理”。

### 1.1 每列的数据来源（不新增读取权限）

| 列 | 来源 | 权限与边界 |
|---|---|---|
| 承诺摘要（主列） | `quote` 截取前 60 字。`quote_status` 为 restricted 时显示“内容受限”，unavailable 时显示“来源消息已删除”，none（agent_report 等非 Claim 来源）时显示“无原文”。有 `condition` 的加前缀“条件：” | 沿用现有路径。quote 已经过 0111 D7（Message 存在）和来源 Message 的逐条 READ（workbench-member-v1），读取有审计。**这是 AT-064 白名单中已允许的工作台读取，不新增 Claim 读取路径**，也不进入 Context |
| 客户 | `consumer_id` 经 §2 的统一名称状态 | 每个**不同**客户按 consumer 名称做一次逐属性 READ。同一请求内去重，每客户只读一次、审计一行，不按承诺行重复 |
| 期限 | `due_at` 与 `due_precision`（修正前端的 `precision` 错名）；`late=true` 时标“已逾期” | 承诺属性，已随 `nexloop.commitment.read` 返回 |
| 状态 | `status`，加 `fulfillment_basis`（undetermined 时不显示） | 同上 |
| 承诺方 | `made_by.sender_kind`：agent 显示“Agent（角色）”，staff 显示该成员的工作台别名，不显示 principal 原值 | 同上，不新增读取 |
| 下一步 / 责任人 | 见 D1。v1 不新增字段，按状态派生“待谁处理”：conditional 时等客户条件；open / in_progress 时由系统跟进（承诺方）；breached 或有异常时由负责人处理 | 纯展示规则，不写库 |
| ID | 次级小字，可复制 | — |

### 1.2 接口改动

- `GET /commitments` 的每一项增加 `consumer_display`，形状同 `DisplayName`，在 Python `WorkbenchViews.commitments` 中按去重后的客户补上。也可以不加，由前端用 §2 的批量名称接口补，见 D3。
- **契约**：`commitment-view.schema.json` 不允许额外键。因此：
  - 方案 a：只在工作台列表外层加 `consumer_display`，不进 view（`{commitments:[{view, consumer_display}]}` 会破坏现有解析）；
  - 方案 b：给 view 增加可选键 `consumer_display`，契约升小版本。

  推荐方案 b，走 PR。lines 不改契约，提案见 §8。
- 摘要截断、标签和派生的“下一步”都在前端完成，不改 SQL。
- **修复**（不涉契约）：前端 `predicate` 改为 `quote` 摘要，`precision` 改为 `due_precision`；`Consumers.tsx` 的 `properties.content` 改为同一摘要函数，并遵守 `quote_status`。

### 1.3 不越权的要点

- 不把 Claim 的结构化值（`condition_text` 以外的字段）、Claim 列表或证据正文搬进列表。
- 摘要只来自已裁定可显示的 `quote`。
- 受联系限制的客户：联系限制约束的是对外联系，不约束成员阅读；读取仍按 workbench-member-v1 判定，不放宽也不收紧（与现有详情页一致）。
- 已擦除的客户或消息：NX-029 擦除已清空引文；view 返回 `unavailable` 或 null，列表照实显示。

## 2. F04 名称：缺失 / 受限 / 加载失败三态统一

### 2.1 统一判定（一个函数，列表、详情、承诺、计划、Action 各页共用）

按以下顺序判定，前一条成立就停：

1. 调用失败（异常、超时、非 403 的 5xx）→ `failed`，显示“名称加载失败”，可重试；
2. 成员没有 Consumer 读取权（`WorkbenchForbidden` 或 403）→ `restricted`，原因 forbidden；
3. `display_name` 不可读（owner 受限组）→ `restricted`，原因 withheld。**不看 present**：受限时不应让成员推断有没有名字；
4. 可读但对象上没有，或为空 → `missing`，显示“未登记名称”；
5. 可读且有值 → `ok`。

- `restricted` 与 `missing` 都显示稳定别名 `客户 xxxx·yyy`，后面加状态小字（“名称受限” / “未登记名称”）。`failed` 也显示别名，加“名称加载失败”。
- 列表和详情改为调用同一个 `display_state(consumer_id, fields)`。详情已读到的 properties 直接传入，不重复读取，也不多一条审计。

### 2.2 SQL 侧（需迁移，见 D2）

`nexloop_workbench_consumer_fields` 对 `readable=false` 的字段改为不返回 `present`（置 null）。这样服务端不再泄露受限字段的有无。

- 改法：`create or replace` 同签名，属新临时迁移（接在当时 main 最后一条之后）。
- 影响：Python 侧对受限字段本就不再看 present（§2.1 第 3 条），所以不依赖这个改动也能做到前端一致；迁移只是收紧服务端。

### 2.3 前端

- `DisplayName` 类型改为 `ok | missing | restricted(reason) | failed`，`DISPLAY_NOTE` 只保留三种文字。
- 旧服务端的 `absent` / `withheld` / `forbidden` 在解析时映射为新状态，兼容一个发布周期。

## 3. F05 承诺标签与异常范围

- **推荐**（D4 推荐 a）：异常跟随当前标签与筛选。
  - SQL verb `commitments` 的 `exceptions` 改为只取当前结果集内承诺的异常（`subject_ref = 'commitment:'||id`），再加上 `subject_ref` 为 `claim:…` 的注册前异常。后者只在“未结束”标签中出现，因为它们还没有对应的承诺对象。
  - 页面另设折叠区“全部承诺异常（N）”，只显示计数和跳转到 Action / 异常页的链接，不重复列内容。
- **标签计数**：verb 增加返回 `counts: {open: n, ended: n, exceptions_open: n, exceptions_all: n}`。同一 SQL 内 `count(*)`，不读内容、不审计。标签写作“未结束（3）”“已结束（0）”。
- **范围参数**：用 `status_group=open|ended|all` 取代现有 `all=true`（`all` 保留为 `status_group=all` 的别名，兼容一期）。服务端过滤，前端不再二次过滤，于是列表、计数、异常同出一源。
- 需要迁移：`create or replace authz.nexloop_workbench_read`，以当时最新定义为底逐字复制，只改 commitments 分支。返回形状变化落在工作台 JSON，不在 `commitment-view` 契约之内。

## 4. F09 检索、筛选、排序、分页

### 4.1 服务端参数（只对非正文列）

| 页面 | 新参数 | 说明 |
|---|---|---|
| commitments | `status_group`、`consumer_id`、`due_before` / `due_after`、`late`、`origin`、`sort=due_at\|registered_at\|status`、`order`、`limit`（≤100）、`cursor`（不透明，编码排序键和 id） | 返回 `total`（同条件 `count(*)`）与 `next_cursor` |
| consumers | `restricted`、`has_open_commitments`、`sort=recent_activity\|open_commitments\|id`、`limit`、`cursor` | 现有 `after` 保留；`total` 为租户 Consumer 数 |
| actions | 现有 `state`、`consumer_id`，加 `since` / `until`、`cursor`、`total` | — |
| audit | 现有 `before`，加 `kind`、`principal`（工作台别名映射在前端） | — |

全部是 SQL 侧的结构化过滤，参数严格校验（同 `workbench_http.query` 的白名单方式）；不接受自由 SQL 或任意字段名。

### 4.2 关键字检索（D5）

名称和承诺正文都要逐条 READ 并审计。服务端按关键字检索它们，等于先读全部再过滤：会产生大量读取审计，而且等于用检索绕开“只读所需”。

- v1 推荐：关键字只在**已加载的当前页**内由前端匹配（名称、摘要、别名、ID 前缀），并明确标注“仅当前页”；
- 跨页检索：ID 和别名前缀可在服务端做，因为不涉及内容。
- 名称或正文的全量检索作为 D5，另行设计（例如受审计的“检索读取”目的，按次计数）。

### 4.3 URL 与返回位置

- 筛选、排序、游标写入查询串，例如 `/workbench/commitments?status=open&consumer=<id>&sort=due_at`。`route.ts` 扩展 `query` 字段并做白名单解析；未知键丢弃。
- 返回列表时由 React Query 的 key 包含查询串，恢复数据；滚动位置记在 `history.state`（每个路由一项），返回时恢复。不写 localStorage。
- 分页用“加载更多 / 上一页”游标；总数显示“共 N 条”。

## 5. F10 客户—会话—承诺的上下文跳转

- **客户头部**（详情、会话、该客户的承诺列表共用）：别名或名称（§2）、联系限制状态、未结束承诺数（可点）、活跃计划数、会话数。
- **面包屑**：`消费者 › <客户> › 会话 <short id>`；`承诺 › <客户筛选>`。组件由 L3 提供。
- **承诺数可点**：跳到 `/workbench/commitments?consumer=<id>&status=open`，用 §4 的服务端 `consumer_id` 过滤（现成参数）。列表头显示“客户：<名称>”筛选标签，可清除。
- **历史往来**：会话页右侧标签“历史往来”，复用现有 `/consumers/{id}/history`（已分页，正文逐条 READ）。默认只列每条的方向、时间、所属会话和前 40 字摘要，展开后才显示全文，当前会话的滚动位置不动。
- **接口改动**：无。consumer 详情已返回 `commitments` 和 `conversations`；计数直接用。

## 6. 迁移、契约与权限影响汇总

| 项 | 迁移 | 契约 | 权限 |
|---|---|---|---|
| F03 | 无 | `commitment-view` 加可选 `consumer_display`（推荐；PR，由调度员提交） | 无新增；客户名称每客户一次逐属性 READ，审计有去重 |
| F04 | 推荐 1 条：`consumer_fields` 对不可读字段不返回 present（收紧） | 无（工作台 JSON） | 收紧，不放宽 |
| F05 | 1 条：`workbench_read` 的 commitments 分支，加 `status_group`、`counts`、范围一致的 `exceptions` | 无 | 无变化；计数不读内容 |
| F09 | 与 F05 合并到同一条迁移（commitments 的过滤、排序、游标、total），consumers、actions 的过滤在同一条 | 无 | 无变化；关键字不在服务端读内容 |
| F10 | 无 | 无 | 无 |

- 迁移合计 1–2 条，用临时编号。`workbench_read` 是大函数，以当时最新定义为底逐字复制，只改所需分支。
- 回归要跑 0130 相关测试：`test_workbench_*`、AT-064（`test_claim_store_pg` 不受影响，因为不新增 Claim 读取文件）。
- **对 NX-037 / 消息 Run 路径**：无影响，只改工作台读函数和页面。

## 7. 需要负责人拍板的 D 项（附推荐）

| D | 问题 | 选项 | 推荐 |
|---|---|---|---|
| D1 | 承诺的“责任人”是什么 | a. 不新增字段，按状态派生“待谁处理”（§1.1）；b. 新增 Commitment 属性 `assignee`，由新的人工 Action 指派（schema、Action、迁移、审计） | **a**。v0.1 先用派生规则，等确有多人分工再做 b |
| D2 | 是否收紧 `consumer_fields` 对受限字段的 present 泄露 | a. 收紧（迁移）；b. 只在 Python 侧不使用 present | **a**。受限的含义应包括“不暴露有无” |
| D2 补充 | **导出例外，负责人 10/18 选 A** | 导出（NX-029；AT-003：受限组扣留并计数）保留真实的扣留计数。迁移 0218（临时编号）在 `authz.nexloop_data_export` 的 open 里，用 `nexloop_workbench_member_may_read` 计算“有值且不可读”的个数，只返回总数 `withheld_present`，不返回字段名。这条路径只有请求人本人能用，需持有 `nexloop.consumer.export:1`，下载有审计。D2 只作用于工作台读取 | 已实施 |
| D3 | 承诺列表中的客户名称由谁补 | a. 服务端在 commitments 响应里补 `consumer_display`（契约加可选键）；b. 前端另调批量名称接口 | **a**。一次请求，服务端去重审计 |
| D4 | 承诺异常的范围 | a. 跟随当前标签与筛选，另有“全部承诺异常（N）”折叠计数；b. 始终显示全部，单独成区并写明范围 | **a** |
| D5 | 关键字检索的范围 | a. v1 只在当前页内匹配，服务端只做 ID / 别名前缀；b. 服务端全量检索名称和正文（需要新的受审计检索目的） | **a**。b 另立设计 |
| D6 | 承诺方为员工时显示什么 | a. 工作台别名；b. 成员显示名（另需成员名称来源） | **a** |

## 8. 契约提案（lines 不改契约，交调度员）

`packages/contracts/commitment-view.schema.json`：在 `properties` 中加可选 `consumer_display`（不进 `required`）：

```
{"type":"object","additionalProperties":false,"required":["status"],
 "properties":{"status":{"enum":["ok","missing","restricted","failed"]},
   "reason":{"enum":["forbidden","withheld"]},"name":{"type":"string","maxLength":80}}}
```

`ok` 时有 `name`；`restricted` 时有 `reason`；其他状态都不带 name。旧消费者不受影响，因为是可选键。

## 9. 实现顺序（排在 L3、L4 之后）

1. 前端纯修复（predicate→quote 摘要、precision→due_precision、Consumers 承诺小节）和 §2.1 的 Python 统一名称函数，不需要迁移、不改契约，可先单独合入；
2. F05 + F09 的 commitments 迁移，以及前端标签计数、URL 筛选；
3. F04 SQL 收紧（D2）；
4. F10 头部、面包屑和跳转，用 L3 组件；
5. 契约可选键（D3），走 PR。

每一步都配测试：commitments 分支的 PG 测试（范围、计数、游标、受限客户、受限 quote）、名称三态的单元与 PG 测试、前端 vitest（URL 解析与恢复）。
