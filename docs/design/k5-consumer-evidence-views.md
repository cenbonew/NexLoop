# K5 客户证据视图：关系图、画像卡、消息处理链路、会话指标（草案，2026-11-02）

- **用途**：让工作台在真实数据上做到对外演示页（www.zingnex.com/nexloop/）的四块效果。演示页由浏览器本地生成合成数据，只作形态参照；代码与数据都不照搬。读者是负责人、调度员、后端各线和 L6 审查。
- **状态**：草案 v2，只有设计，没有实现。v1（f8424930）经 L6 条件 PASS，v2 写入了五个条件，见 §2.10。
- **依据**：K4l（51ad9af4，Object 页空列与「没有值 / 没有权限」的区分）；D-G1（逐属性 READ、withheld）；ADR-025（负责人限制的属性组）；NX-019、NX-020、NX-023、NX-026、NX-027、NX-028、NX-030、NX-045/046、NX-054；`docs/design/workbench-world-model-design.md`。

文中路径简写：WH = `packages/eios-core/src/nexloop_eios/workbench_http.py`，WR = `.../workbench_reads.py`，M = `packages/eios-core/src/eios/migrations/`。

## 1. 现状

| 块 | 已有读取（契约） | 缺什么 |
|---|---|---|
| ① 单客户关系图 | `GET /objects/{type}/{id}/neighbors`（WH:357，`workbench-object-neighbors.schema.json`）只返回本体 Link 和属性指针，不含 Claim、提案、候选和 Action。<br>`GET /commitments?consumer_id=`（WH:217，`commitment-view.schema.json`）带 `origin`（claim/extend）、`claim_id`、`message_id` 和 quote（消息 READ 不成立时为 `quote_status=restricted`）。<br>`GET /actions?consumer_id=`（WH:172）读 `nexloop_effect_intents`。<br>`/outreach-evidence?consumer_id=`（WH:512）只给负责人看，不返回正文。<br>`/review/{candidate_id}`（WH:537）只给审核人，也不能按客户过滤。 | 没有按客户读取 Claim 的接口。`nexloop_claims`（M/0066:21）里已有 `source_sequence`、`speaker`、`quote`、span、`epistemic_kind`、`resolution_state`。<br>匹配结论的 `decision` / `reason`（M/0070）没有任何读取，也就答不了「为什么还是候选」。 |
| ② 画像卡 | `GET /objects/Consumer/{id}`（WH:306，`workbench-object-values.schema.json`）返回对象 `revision`，逐属性 `ok` / `missing` / `withheld(reason)`。<br>`GET /consumers/{id}`（WH:136）提供显示名、会话和受限属性组。<br>即将发生的承诺：`/commitments?consumer_id=&status_group=open&sort=due_at&order=asc&due_after=<now>`（WH:221-230），已支持排序和分页。 | 只有对象级修订号，没有逐属性修订号。第一版只显示对象修订号，不做逐属性。 |
| ③ 单条消息处理链路 | 各步的关联 ID 都已存在：<br>• 消息→运行：`nexloop_message_run_issuances`（M/0049）<br>• 运行→上下文：`nexloop_context_packs`（M/0089）<br>• 运行→模型调用：`nexloop_model_requests`<br>• 运行→Action：`nexloop_effect_submissions`（M/0040）<br>• Action→回执：M/0048<br>• 消息→Claim：`source_message_id`<br>• Claim→匹配→提案：M/0070<br>现有的单段读取：`/effect-determination/{intent_id}`、`/outreach-evidence?intent_id=`。 | 没有串起整条链路的读取，也没有统一的 trace id（`correlation_key` 是语义去重键，不是 trace）。 |
| ④ 会话指标卡 | `/metrics`、`/data-health` 和 `/outreach-metrics` 都只有租户级汇总。 | 没有会话级读取。<br>费用表 `nexloop_cost_entries`（M/0120:43）有 `run_id` 但没有 `conversation_id`，要经 `message_run_issuances` 才能关联到会话。 |

## 2. 方案

### 2.1 放在哪里（回答 d）

- **①②放在客户详情页**（`/workbench/consumers/{id}`）。
  - 画像卡放在页首，替换现在的「属性」段。K4l 刚改过的 `consumer-properties` 文案会并入画像卡，并沿用 K4l 的三种状态句（没有值 / 受限属性组 / 无读取权限）和 `NO_VALUES_TEXT`。实现前先 rebase 到包含 K4l 的 main，所以不会冲突。
  - 关系图做成同页的一个新段「关系与证据」。
  - 为了不让 `Consumers.tsx`（约 200 行）继续膨胀，新组件放进新文件 `pages/ConsumerEvidence.tsx`。`Consumers.tsx` 只增加两处挂载，不改 K4l 的逻辑。
- **③④放在对话页**（`/workbench/consumers/conversations/{id}`，即 `Consumers.tsx` 里现有的会话子路由）。
  - 会话指标卡放在对话页顶部。
  - 处理链路放在单条消息的「处理链路」展开里：每条入站消息旁有一个按钮，点开后在右侧抽屉里显示。窄屏时抽屉改为全屏层。
  - 新文件为 `pages/MessageTrace.tsx` 和 `pages/ConversationMetrics.tsx`。
- 不新开页面：四块内容都附着在某个客户或某个会话上，单独成页会多一层导航。

### 2.2 ① 单客户关系图

- **节点**：
  - 中心是 Consumer。
  - 周围五组：事实（Claim）、承诺（Commitment）、Action（effect intent）、主动触发（outreach intent）、候选（mutation proposal / candidate definition）。
  - 每组最多画 12 个节点，多出的合并成一个「另 N 个」计数节点，和 `/neighbors` 的折叠规则一致。
- **边**：只画后端返回的真实关联，没有就不画。
  - 承诺 → 来源 Claim：`commitment.claim_id`。
  - 候选 → 依赖的 Claim：`dependent_claims` 或 `claim_matches.claim_id`。
  - Action → 承诺：intent 与 plan step 的关联。
- **侧栏证据链**：点中节点后，侧栏按类型显示以下内容（就是新接口的字段）。
  - Claim：原文第几轮（`source_sequence`）、谁说的（`speaker`：客户 / AI）、原文片段（`quote`）、`epistemic_kind`、置信度、`resolution_state`。
  - 承诺：`origin` 和推导链，即 claim → commitment；`extend` 的承诺会显示被取代的那一条。
  - 候选：「为什么还是候选」，用 `decision` 和 `reason` 的代码映射成中文句子。映射表放前端，未知代码原样显示，不猜。
- **新接口 A**：`GET /api/v1/workbench/consumers/{id}/evidence-graph`，只读，按成员当前角色过滤。
  - 字段草案：
    ```
    { consumer_id, generated_at,
      groups: [{ kind: claim|commitment|action|outreach|candidate, total, invisible, items: [...] }],
      edges: [{ from:"<kind>:<id>", to:"<kind>:<id>", rel: derived_from|depends_on|fulfils }] }
    claim item:      { claim_id, predicate, epistemic_kind, resolution_state, confidence,
                       source: { conversation_id, sequence, speaker,
                                 quote|null, quote_status: ok|reviewer_only|restricted|corrected|superseded|erased },
                       state: active|corrected|superseded|erased }
    commitment item: 沿用 commitment-view 的摘要字段 + origin
    action item:     { intent_id, operation, state, created_at }
    outreach item:   { intent_id, purpose, state, created_at }   // 仅负责人：其他角色这一组 total 照计、items 为空、invisible=total
    candidate item:  { proposal_id|candidate_id, status, match_decision, match_reason_code, dependent_claim_ids[] }
    ```
  - 新增契约 `packages/contracts/workbench-consumer-evidence.schema.json`，以及配套的 example 和 test_contracts。
  - 归属：Claim 部分是 NX-019 的线，匹配和候选是 NX-020 的线，汇总读取放在 NX-028 的工作台读取线。具体由调度员按当前各线分配。

### 2.3 ② 客户画像卡

- 数据来自现有接口，不需要新接口：`GET /objects/Consumer/{id}` 加 `GET /consumers/{id}`，再加上面那条即将发生的承诺查询（只取 3 条）。
- 布局：
  - 最上面一行是即将发生的承诺：截止时间、内容摘要、`late` 标记。
  - 其下是属性。按 Object View 的分组顺序排列，标题显示「修订 r{revision}」，第一版是对象级修订号。
  - withheld 属性显示「受限」徽标和原因，不显示值；`missing` 属性按 K4l 的规则处理。全部为空时显示一句说明，不画「—」表格。
- **逐属性修订号**（需要新增字段，归属是 EIOS 对象读取，即 NX-054 那条线）：在 `workbench-object-values` 的每个属性上加可选的 `revision` 字段，向后兼容。如果负责人决定第一版不需要，就不加（见 Q2）。

### 2.4 ③ 单条消息处理链路

- **九步**：消息 → relay → 上下文 → AI 运行 → 受治理的 Action → 外部回执 → 提取 → 匹配 → 本体更新。
  - 每一步显示状态和时间。状态共六种：`done` / `running` / `failed` / `skipped`（不适用，例如这条消息没有触发 Action） / `none`（应有但没有记录） / `restricted`（无读取权限）。
  - 只显示 ID、代码和时间，不显示消息正文以外的内容，和其他 observe 读取一致。
- **新接口 B**：`GET /api/v1/workbench/messages/{message_id}/trace`。
  - 字段草案：
    ```
    { message_id, conversation_id, steps: [
      { step: message|relay|context|run|action|receipt|extraction|matching|ontology_update,
        status: done|running|failed|skipped|none|restricted,
        at: date-time|null, finished_at: date-time|null,
        refs: ["run:<id>","intent:<id>","claim:<id>",...],   // 最多 10 个
        code: string|null } ] }
    ```
  - 服务端按已有的关联键逐段查找：`message_run_issuances` → `context_packs` / `model_requests` → `effect_submissions` → 回执；`claims.source_message_id` → `claim_matches` → `mutation_proposals`。
  - 读取权限：先要求对消息所在会话有 READ。Action 和回执沿用 `/effect-determination` 的角色要求，角色不够时该步返回 `restricted`。
  - 契约：`workbench-message-trace.schema.json`。归属是 NX-030（观测与审计）那条线。

### 2.5 ④ 会话级指标卡

- **新接口 C**：`GET /api/v1/workbench/conversations/{id}/metrics`。
  - 字段草案：
    ```
    { conversation_id, as_of,
      response_ms: { p50, p90, samples } | null,      // 入站 accepted_at → 首条出站
      model_cost: { amount, currency, runs } | null,  // model_requests.cost 经 message_run_issuances
      claims: int, commitments: int,
      gate_blocks: { total, by_code: [{ code, count }] } } // scope denials + contact 限制 + dispatch 拒绝
    ```
  - 费用只给负责人看（与 `/metrics` 一致）。其他角色返回 `model_cost: null`，并带 `withheld: ["model_cost"]`。
  - 契约：`workbench-conversation-metrics.schema.json`。费用部分归 NX-027，其余归 NX-030。

### 2.6 权限（回答 b）

- 每个节点、每个值都按服务端当前角色过滤，前端不做拼接推断。
  - 被挡住的 Claim、承诺或候选不画节点，只在组标题上计数（「不可见 N 个」），和 `/neighbors` 的 `invisible` 一样。
  - 不画通向不可见节点的边。
  - quote 在消息 READ 不成立时为 `restricted`，侧栏显示「原文受限」，不显示片段。
  - 正在删除的客户（`erasing`）按 NX-029 整体隐藏，接口返回 404 或 `erasing`，页面沿用现有的删除中提示。
- 画像卡的属性沿用 D-G1 和 K4l：withheld 不当作空值，空值不当作受限。
- 每次读取都按现有方式记入读取审计。新接口的 purpose 分别为 `consumer_evidence`、`message_trace` 和 `conversation_metrics`。

### 2.6a v2 补充的权限规则（L6 条件 1–3）

- **原文片段只给审核人看（条件 1）**：
  - 新接口返回 `quote` 的条件是：成员有 `ontology.schema.review` 或 `nexloop.knowledge.read`，并且对来源消息有 READ。这和 `/review/{candidate_id}` 以及前端的 `REVIEWER_ONLY_TEXT`（`pages/Knowledge.tsx:17`）一致。
  - 其他角色（包括 operator）只看到轮次和说话人，`quote=null`，`quote_status=reviewer_only`。侧栏的文字和 `REVIEWER_ONLY_TEXT` 一致，并说明需要什么角色。
  - 承诺自带的 quote 仍按 `commitment-view` 的现有规则返回。是否放宽 Claim 原文，交负责人决定（Q5）。
- **属性受限时，相关 Claim 一并受限（条件 2）**：
  - 判断 Claim 对应哪个属性，唯一依据是匹配结论里的 `eios:property:<Type>/<name>`。它在 `claim_matching.py:304-310` 解析，记录在 M/0070 的 `nexloop_claim_matches`。
  - 这个属性对成员是 withheld 时（D-G1，原因可能是 `restricted`、`not_permitted` 或 `erasing`），或者属于负责人限制的属性组时（ADR-025，即 `/consumers/{id}` 的 `withheld` 组），该 Claim 不返回，只计入本组的 `invisible`。由它推导出的承诺和候选也一样，不能从这些节点反推出来。
  - 还没有匹配结论的 Claim：`predicate` 是抽取器写的自由文本（M/0066，最长 120 字），没有可靠的属性对应关系。只要该类型有任何对当前成员 withheld 的属性或受限属性组，这类 Claim 就只计入 `invisible`，不返回 predicate 和原文。
  - 宁可多挡，不按字面猜测对应关系。前端不做任何推断。
- **更正、取代和删除（条件 3）**：
  - Claim 被更正时，有 `epistemic_kind=correction` 的后继，经 `derived_from` 关联（M/0069、M/0115）。被取代时 `resolution_state=superseded`。这两种情况的节点分别显示「已更正」和「已取代」，不再返回原文和 predicate 的值，只给出后继 Claim 的引用（后继同样受上面的规则约束）。
  - 来源消息或客户被删除的 Claim，按 NX-029 和 M/0144 的删除传播处理，显示「已删除」，只计数，不返回任何内容。
  - 消息「撤回（withdrawn）」：`claim.schema.json` 和 M/0066 都没有这个状态。设计不新造，由后端确认有没有对应事实（Q6）；在确认之前不显示这个状态。

### 2.7 没有数据时（回答 c）

- 现在 stage 上 claims 约 1480 条，mutation_proposals 是 0 条，所以第①块的「候选」组和第③块的「匹配 → 本体更新」两步都没有数据。
  - 第①块：候选组照常显示组标题和「0」，组里写：「还没有候选：提取的事实尚未产生本体更新提案。」不画任何占位节点。
  - 第③块：
    - 「匹配」有 `claim_matches` 记录时，显示结论代码。
    - 没有记录时，该步状态为 `none`，文字为「没有匹配记录」。
    - 「本体更新」没有提案时为 `none`，文字为「没有本体更新提案」。
    - 时间显示为「—」，并说明这一步「应当发生但没有记录」，与 `skipped` 的「不适用」区分开。
  - 后端接口如果暂时取不到某一段，就返回 `none`，不能省略这一步；前端始终画满九步。
- 演示数据、示例节点和随机数一律不进入页面。vitest 用契约 example 测试，真实浏览器测试用本地合成租户的真实写入。

### 2.8 关系图怎么画（回答 e）

- 复用仓库里已有的 `apps/web/src/workbench/graph/`（手写 SVG：`SchemaGraph.tsx` 加 `layout.ts`）。它已经满足 D2 的键盘可达、标签字号下限、适应画布和表格替代视图这些要求。
- 新增一个径向布局 `radialLayout`：中心节点加五个扇区，放在 `layout.ts` 里。
- **不引入新依赖**。web 现有的运行依赖只有 react、react-dom 和 @tanstack/react-query。
- 同一份数据始终提供表格视图，和 Object Type 页的 Table / Graph 一样。

### 2.9 实现顺序

1. 后端接口 A（证据图）。
2. 前端画像卡（只用现有接口，可以先做）。
3. 前端关系图。
4. 后端接口 B 和 C，然后前端③④。

画像卡不依赖任何新接口，可以和后端并行。

### 2.10 v2：接口范围与实现要求（L6 条件 4、5）

- **只服务真实世界**：三个新接口都只在 world=real 时提供。sim 或 shadow 世界返回 404，并带说明代码 `real_world_only`。前端在模拟世界里显示「只在真实世界提供」。
- **上限与分页**：参照 `/neighbors`（WR:590）。
  - 证据图每组最多 25 项，超出的部分记为 `collapsed`，取下一页用 `?kind=<组>&after=<游标>`。
  - 处理链路每步的 `refs` 最多 10 个，超出的记为 `more`。
  - 指标接口是固定大小的聚合，按会话计算，`gate_blocks.by_code` 最多 20 项。
- **实现时必须做的**：
  - 读 `nexloop_claims` 的 SQL 要更新 `tests/test_claim_store_pg.py` 里 AT-064 的白名单。
  - 三个新契约各配一个 `packages/contracts/examples/<name>.valid.json`，并跑 test_contracts、test_published_contracts 和 `generate --check`。
  - 新读取的审计 purpose 加入现有的 purpose 列表。

## 3. 验收要点

- 四块都只读真实接口。用一个合成租户的真实写入（WebChat 对话加 business_setup）跑真实浏览器测试，并截图 1440 和 390 两种宽度。
- 权限测试至少覆盖三种角色：负责人、operator、无 Property 读权限的成员。
  - withheld 属性、受限原文和不可见节点都不在 DOM 里出现值或片段，只出现计数或徽标。
- 第③块在 mutation_proposals 为 0 的租户上，九步齐全：「匹配」和「本体更新」显示 `none` 的文字，没有任何占位值。
- 关系图可以只用键盘操作，表格视图与图的节点数一致。新契约的 example 通过 test_contracts、test_published_contracts 和 `generate --check`。
- 权限测试再加三种情况：operator 看不到 Claim 原文；属性受限时相关 Claim 只计数；被更正或取代的 Claim 不返回原文。sim 世界下接口返回 404。
- 测试里没有 xfail 或 skip。prepush-scan 4 项 PASS。

## 4. 需要负责人决定的问题

| 编号 | 问题 | 建议 |
|---|---|---|
| Q1 | 主动触发（outreach）对非负责人角色显示为计数，还是整组隐藏？ | 显示计数，组里写「仅负责人可见」，与 `invisible` 的含义一致。 |
| Q2 | 画像卡要不要逐属性修订号（需要改 EIOS 对象读取的契约）？ | 第一版只显示对象修订号，逐属性的以后再做。 |
| Q3 | 会话指标里的模型费用，operator 能不能看？ | 不能，沿用 `/metrics` 只给负责人看的规则。 |
| Q4 | 处理链路要不要统一的 trace id 表？ | 第一版不加，服务端按现有关联键逐段查。等查询成本有实测数据后再议。 |
| Q5 | Claim 原文要不要对 operator 放宽（现在只给审核人和知识只读角色看）？ | 不放宽。operator 处理对话时本来就能看到消息原文，证据图只给出轮次，需要时回到对话里查看。 |
| Q6 | 消息或 Claim 有没有「撤回」这个事实？现有 schema 没有。 | 由后端（NX-019）确认。没有的话，设计里不出现「撤回」。 |

## 5. 否决的做法

| 做法 | 否决理由（拍板日期） |
|---|---|
| 照搬演示页的前端代码或合成数据 | 演示页的数据是浏览器本地生成的，不是业务事实（调度员转达负责人要求，2026-11-02）。 |
| 前端分别调用 `/commitments`、`/actions`、`/review` 等接口，自己拼关系图 | 各接口的权限口径不同，拼出来的边会泄露不可见节点的存在；`/review` 也不能按客户过滤（2026-11-02 设计）。 |
| 引入 cytoscape 或 d3 一类的图库 | 仓库已有手写 SVG 图组件，满足无障碍和字号要求，新依赖会增加许可证和体积负担（2026-11-02 设计）。 |
| 没有数据的步骤直接不显示 | 不显示会让人以为链路本来就没有这一步，掩盖 mutation_proposals 为 0 的问题（2026-11-02 设计）。 |
