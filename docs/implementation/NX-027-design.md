# NX-027 接入商业事件与费用指标：设计稿（调度员已审，D6 负责人已定；未实现）

分支 `nx027-design`，从 main `587e7c2` 切出。本稿只有文档，没有代码和迁移；文中的迁移号都是占位。

NX-027 依赖 NX-026（承诺，`nx026-design` 中的设计稿 `c6d52cd`，尚未实现）。NX-026 又依赖 NX-025（`nx025-impl`，0108–0110 尚未合入）。

依据（本稿中的简称）：
- `PRD`：01_PRD.md。M08（158–166）、M19（268–276）、M18（258–266）、M03（108–116），J01（44），研究指标（320–324），真实与测试声明（340–342）。
- `D03`：03_DOMAIN_DATA_MODEL.md。§1 所有权（9–19），金额（27），表组（51、57），CommercialRecord（72），保留（96–98）。
- `D04`：04_CONVERSATION_ONTOLOGY_PIPELINE.md。`verified_fact` 只来自核验通道（37），付款状态以核验记录为准（61），更正链（66），付款页报错的例子（70–76）。
- `D09`：09_API_AND_EVENT_CONTRACTS.md。webhook 签名与重放窗口（11），幂等（13），`POST /webhooks/{connector_id}`（28），`GET /metrics` `/costs` `/commercial-observations`（36），事件信封（58），`commercial.fact.verified`（60），429 预算（66）。
- `D13`：13_SECURITY_PRIVACY_AND_OPEN_SOURCE.md。webhook 限制（15），环境与模式是两个维度（25），模拟不回流（27）。
- `D18`：18_SOURCES_AND_ASSUMPTIONS.md:54。真实付款来源尚未确认，先做通用签名事件适配器，只做技术运行验证，不声称营收增长。
- ADR-002/007/009/017（16_ADR.md），ADR-019、ADR-023（adr/）。
- 实现说明：NX-022-goals-controls.md、NX-023-D、NX-047。
- 代码（main `587e7c2`）：0068 NX-022 控制面，0089/0100 模型请求与结果，0041/0042 effect 账本，0055 原生 web 入站幂等，0106 计划复评。

交付物（planning）：签名事件、核验状态、币种与窗口、real 与 test 分开。

验收：
- **AT-011**：顾客说已付款但没有交易证据 → 只记录 statement，不更新已核验的付款状态。
- **AT-012**：同一个签名付款 webhook 重复 10 次 → 只产生一条规范的业务变化，退款和币种处理正确。
- **AT-042**：test / simulation 付款 → 不计入 real 收入或续费。
- **AT-043**：租户的模型或折扣预算用尽 → 阻断新的消耗，已有的合法结果保留。

## 0. 结论先行

1. **三层分开。** 外部原始事件、规范商业记录、指标与费用是三层，各有唯一的写入者。
   - **原始事件**（证据层，`runtime.nexloop_commercial_events`）：只由签名入口写入，记录核验状态，本身不宣布任何付款（D03:13）。
   - **规范商业记录**（EIOS 正式对象 `CommercialRecord`）：只由服务主体 `commercial_recorder` 经受治理 Action 写入，并且只从**已核验**的原始事件派生。
   - **指标观察与费用条目**：派生层，可重建，本身不成为事实（D03:19）。
2. **签名核验在 SQL 中完成。** 连接器密钥由 `nexloop_owner` 持有。入口服务只传原始字节、签名头和时间戳。租户、world、数据模式都由连接器配置决定，信封里的值一概不信（D09:11、58）。重放窗口、event ID 去重、大小限制、速率限制都在入口执行。
3. **幂等、晚到与更正。**
   - 同一个 `(connector, source_event_id)` 只会被处理一次；ID 相同而摘要不同，记为冲突（409）。
   - 更正和退款都是新事件，各自产生新记录，通过 `corrects` / `refunds` 链指向原记录，从不原地改写。
   - 晚到的旧事件不覆盖更新的状态：规范记录的当前状态按 provider 序号或 `occurred_at` 判定，晚到者只进链、只留证。
4. **real 与 test 的处理（决策 D1）。** 建议按事件信封契约：连接器绑定一种模式，test 连接器只能写 test world；real world 中只存在 real 商业数据。0068 允许 world real 里出现 `data_mode='test'`，本任务不再往 real world 写 test 数据，并补一条约束测试。
5. **费用。**
   - 新增 append-only 的 `runtime.nexloop_cost_entries`，按种类分开记：model / channel / discount / service / labour（PRD:272）。每条都必须带币种和来源，不同币种不相加。
   - 模型费用来自 `runtime.nexloop_model_requests.cost`（已有，按调用记录）。
   - **补上 NX-022 缺的“结算”**：Run 结束后，追加一条释放行，把预留额与实际额的差额还回去。这样预算上限反映的是实际花费，同时保持“上限必须真正阻断新消耗”（AT-043）。
   - incentive 预算接到折扣类 Action 上（AT-043 的折扣部分）。
6. **KR 度量。**
   - 指标观察由投影服务从已核验记录和费用条目确定性地生成。
   - 给 `nexloop_metric_observations` 补上更正链（`supersedes`），并真正执行 `refund_rule`。
   - 增加冻结的 cohort（分母成员在窗口开始或定义批准时冻结，PRD:324），新的 `compute_key_result` 按这些规则计算。
7. **与 NX-026 衔接**：核验通道为引用该消费者订单或付款的承诺写一条 `commercial_event` 证据（ref `commercial:<record_id>`）。只有已核验的签名事件才写。
8. **与 J01 / 复评衔接**：新增 work feed `commercial-verified`，唤醒相关计划（例如付款后撤销续费提醒，PRD:44）。不新增控制事件种类。
9. **迁移**：三个，占位为 `nx027_commercial_intake`、`nx027_metrics_costs`、`nx027_commercial_feeds`，编号接在 NX-025（0108–0110）和 NX-026 之后。

## 1. 现状（已核对代码，main `587e7c2`）

| 方面 | 现状 | 缺口 |
|---|---|---|
| 入站签名事件 | 没有。SQL 中的 HMAC 校验都是 NexLoop 自己签发的信封（`authz.nexloop_authority_signing_keys`）。effect provider 只出站，结果靠按 intent 查询拉取（`effect_provider.py`） | 第三方签名事件的入口、密钥、核验状态 |
| 入站幂等的模板 | `0055_native_web_inbound.sql`：`(tenant, world, namespace, principal, provider_event_id)` + `payload_digest`，同一 ID 不同摘要即拒绝 | 可直接沿用 |
| 指标与 KR | 0068：`nexloop_metric_definitions`（`aggregation`、`currency`、`maturity_seconds`、`refund_rule`、`cohort_rule` 为自由文本）、`nexloop_key_results`、`nexloop_metric_observations`（append-only，带 `data_mode` 检查）、`record_metric_observation`（仅服务主体，NXM01）、`compute_key_result` | 没有调用方；没有来源核验；没有更正链；`refund_rule` 只返回不执行；cohort 不冻结。NX-022 说明第 81 行已注明这些归 NX-027 |
| 预算 | 0068：`nexloop_budget_limits`（`model`/`incentive`）、`nexloop_budget_consumption`（append-only），`reserve_budget`（NXB01/02/03）。Run 派发时按 `maximum_cost` 一次性预留（`goal_controls.prepare_run_dispatch`，`run:<id>`） | 没有结算或释放；incentive 预算没有接入；effect 不做预算检查（0097 中快照的 `budgets` 为 `[]`） |
| 模型费用 | `runtime.nexloop_model_requests.cost numeric(18,8)`、`usage`、`result_status`（0089/0100，只对 v6 Run 记录）；费用来自 Pi 的 `usage.cost.total` | 没有币种列；没有汇总；没有进入预算和指标 |
| 渠道费用 | effect 账本只记单位数（`budget_units`/`reserved_units`），没有金额 | 没有金额来源 |
| 对象类型 | `system-object-types.v1.json` 只有 ContextStrategy 和 ContextManifest。NX-026（D1）将新建 `business-object-types.v1.json`（Commitment） | 没有 CommercialRecord 类型 |
| 复评唤醒 | 0106 会被控制事件、失败 job、策略、effect 观察唤醒；预留了 `nexloop_plan_commitment_due` | 商业记录与指标观察不会唤醒计划 |

## 2. 概念与边界

| 层 | 名称 | 写入者 | 能否作为事实 | 说明 |
|---|---|---|---|---|
| 原始事件 | `CommercialEvent`（`runtime.nexloop_commercial_events`） | 签名入口（`authz.nexloop_commercial_ingest`） | 否：证据层 | 保存核验状态、摘要和规范化后的载荷，不宣布付款 |
| 规范记录 | `CommercialRecord`（EIOS 正式对象） | `commercial_recorder`，经 `CommercialRecord.record:1`、`CommercialRecord.correct:1` | **是**：`verified_fact`，带可核对的证据（原始事件 ID、连接器、签名核验时间） | D03:72 的字段；订单、付款、退款等用 `kind` 区分 |
| 顾客陈述 | Claim（NX-019，`epistemic_kind=statement`） | 提取管线 | 否 | “我已经付了”只是陈述（AT-011），永远不能升级为 CommercialRecord |
| 指标观察 | `nexloop_metric_observations` | `metric_projector` | 否：派生层，可重建 | 来源是 CommercialRecord 或 cost_entries，带 `source_ref`/`evidence_refs` |
| 费用条目 | `runtime.nexloop_cost_entries` | 各来源的触发器或服务（§6） | 记录层（与预算账本分开） | 按种类和币种记，不相加不同币种 |

边界：
- **不新增第二事实库**（ADR-007）。CommercialRecord 是 EIOS 正式对象，与 Consumer 在同一个对象库中。指标和费用只是派生或记录，可以从正式对象和原始账本重建。
- **只有受治理 Action 能写正式对象**（ADR-019）。入口不直接写 `ontology.objects`。
- **模拟不回流**（D13:27）。simulation 或 shadow 的输出不会进入商业记录或 real 指标。

## 3. 商业事件如何进入（签名入口）

### 3.1 连接器（受治理配置）

`control.nexloop_commercial_connectors`：

| 字段 | 说明 |
|---|---|
| tenant_id, world | 连接器所属。事件的租户和 world **只从这里取** |
| connector_id | URL 中的不透明 ID |
| data_mode | `real` / `test`；必须与 world 一致（D1） |
| signing_scheme | v1 只支持 `hmac-sha256-v1`：签名覆盖 `timestamp + '.' + raw_body` |
| key_material | 由 `nexloop_owner` 持有，应用角色无法读取（与 `authz.nexloop_authority_signing_keys` 相同的保护方式）。轮换时允许新旧两把并存，旧的那把带到期时间 |
| accepted_kinds | 允许的事件种类，例如 `order.paid`、`payment.succeeded`、`refund.succeeded`、`subscription.renewed`、`order.cancelled` |
| currencies | 允许的 ISO 币种 |
| replay_window_seconds | 默认 300 |
| revision, status | 停用后入口立即拒绝 |

- **配置方式**：经受治理的人类 Action `nexloop.commercial.connector.configure:1` 写入（走 NX-022 的 `goal_governed_action` 入口，或者可信配置 0050）。密钥只以私有文件导入，不进入任何日志。
- **只有负责人能新建 real 连接器**；test 连接器同样需要人类 Action，但只能落在 test world。

### 3.2 入口路径

`POST /api/v1/webhooks/commercial/{connector_id}`（D09:28）：
1. 网关和 API 先做大小上限（例如 64 KiB）、按连接器的速率限制、`Content-Type` 检查。
2. API 以受限角色调用 `authz.nexloop_commercial_ingest(connector_id, timestamp, signature, raw_body)`，在一个事务内完成以下步骤：
   - 找到连接器（停用 → 拒绝）。
   - 校验 `|now − timestamp| ≤ replay_window`。超出窗口的事件**不写证据行**，只计数，防止被用来灌数据。
   - 用当前或尚在轮换期的密钥计算 HMAC，并做常量时间比较。不通过 → 写一条 `verification_state='rejected_signature'` 的行，只存摘要，不存载荷，然后返回 401。
   - 按 JSON Schema（新契约 `commercial-event.schema.json`，§9）校验载荷；`kind` 必须在 `accepted_kinds` 中，币种必须在 `currencies` 中。不通过 → `rejected_schema`，返回 400。
   - **去重**：`(tenant, world, connector_id, source_event_id)` 是唯一键。
     - 已存在且 `payload_digest` 相同 → `duplicate`，返回 200。provider 侧会认为成功，不再重发。
     - 已存在而摘要不同 → `conflict`，返回 409，写审计。
   - 首次到达 → `verified`。分配 `received_sequence`（每个连接器单调递增），同时保留 provider 的 `provider_sequence`（D04:11）。
   - 在**同一事务**中写 outbox 或 work feed 条目 `commercial-verified`（先持久化再 ACK，D03:51）。
3. 只有提交成功后才返回 2xx。

`runtime.nexloop_commercial_events` 的字段：tenant, world, data_mode, connector_id, source_event_id, received_sequence, provider_sequence, kind, occurred_at, received_at, payload_digest, payload（规范化后保留的白名单字段，不存原始请求头）, verification_state, signature_key_id, verified_at。

- append-only；状态只增加不回退。
- 应用角色没有表权限，只能通过函数访问。
- FORCE RLS 按 `eios.tenant_id`。

### 3.3 从原始事件到规范记录

`commercial_recorder`（后台服务，只有 `CommercialRecord.record:1` / `correct:1` 两项授权）消费 `commercial-verified` feed：
- 只处理 `verified` 的事件。
- 规范记录的 ID 由服务端确定性推导：`record_id = sha256('commercial:'||tenant||world||connector||external_id||kind)`。`external_id` 是 provider 的订单号或付款号，不是 event ID。所以同一笔付款的多个事件（例如 `pending` 之后 `succeeded`）落在同一条记录上，由 Action 推进它的状态。
- Action 的 `request_id` 为 `commercial-'||source_event_id`。这里的幂等来自稳定的业务意图（AGENTS.md），不来自 Run 或 job ID。
- **字段**（D03:72）：kind, external_id, consumer_ref, amount_minor（bigint）, currency（char(3)）, verification_state=`verified`, occurred_at, source_ref=`commercial-event:<id>`, data_mode, provider_status。
- **consumer_ref 的映射**：载荷只带 provider 侧的客户标识，经连接器配置的映射表（`control.nexloop_commercial_customer_links`，由人类 Action 或已核验的身份关联写入）解析成 Consumer。映射不到时：
  - 记录照样写入，`consumer_ref` 为空并标为 `unlinked`；
  - 不猜测、不调模型；
  - 进入负责人工作台的待关联列表（NX-028）。
- 金额一律是整数最小单位。币种的小数位从 ISO 4217 表查得，不在代码中硬编码。

### 3.4 状态推进、晚到与更正

| 情形 | 处理 |
|---|---|
| 同一笔交易的后续事件（`pending` → `succeeded`） | `CommercialRecord.correct:1` 按 `expected_revision` 推进状态。状态机只允许前进（例如 `pending → succeeded → refunded_partial/refunded`），倒退的事件只入链不改状态 |
| 晚到的旧事件（provider 序号或 `occurred_at` 更早） | 不覆盖当前状态，只在记录的事件链里追加一条，标为 `late`（M11：晚到的旧事实不得无条件覆盖新状态）。provider 序号不可比时，以 `occurred_at` 和 `received_sequence` 为准，写入判定依据 |
| provider 的更正事件（例如金额更正） | 新事件 → 新修订，`corrects` 指向被更正的那次修订。旧修订保留，`superseded_at` 填上（D03:25 的有效时间与记录时间） |
| 退款 | 单独的 `kind=refund` 记录，`refunds` 指向原付款记录。原记录的状态推进为部分退款或全额退款。累计退款超过原金额的事件进入 `conflict` 待人工处理，不自动截断 |
| 拒付（chargeback） | 同退款，单独 kind |
| 同一个 event ID 而载荷不同 | 409 + 审计，不产生业务变化（AT-012） |
| 顾客说已付款 | 只有 Claim，没有 CommercialRecord。付款状态不变（AT-011）；付款页报错的例子同理（D04:70–76） |

## 4. 规范商业状态的下游效果

1. **`commercial.fact.verified` 事件**（D09:60）：规范记录每产生一次新修订，同一事务内写一条 work feed `commercial-verified`（与 §3.2 的原始事件 feed 区分：一个是原始事件待处理，一个是记录已变更，分别用 `commercial-raw` 和 `commercial-verified` 两种 kind）。
2. **计划复评**（NX-024）：0106 的唤醒逻辑新增一个来源。记录变化后，唤醒同一 consumer 上 `waiting_external` 或绑定续费、催付类目标的计划。复评可以结论为“已付款，旧提醒作废”（PRD:44，J01）。
   - 这一点由复评决定，不由 NX-027 直接取消 intent。
   - 已经排队、尚未派发的提醒，在派发时仍按 NX-022 的控制快照与 revision 检查（不新增控制事件种类）。
3. **承诺证据**（NX-026 §4.2）：
   - 如果某个承诺的履约要求涉及付款或退款（例如“三天内退款”），且承诺带有对应的商业引用，记录写入时由 `commercial_recorder` 在同一事务中写一条 `commercial_event` 证据（ref `commercial:<record_id>`，`verification='verified_signed'`）。
   - 匹配规则是确定性的：同一 consumer、kind 对应，并且承诺的 `commercial_match` 参数（例如外部订单号）一致。匹配不到就不写，不猜测。
   - test world 的记录只会写 test world 的证据。
4. **Consumer 的商业状态**：Consumer 对象上的“最近付款”“订阅到期”等派生属性，由投影提供给上下文（v6 `supply`）。它们不写回 Consumer 的正式属性，以免形成第二份可写的事实副本（D02:58）。

## 5. 指标与 KR 度量（M19、M03）

### 5.1 指标观察的生成

`metric_projector`（服务主体，只有 `record_metric_observation` 的授权，以及读取商业记录和费用条目的 Function）：
- 对每个已批准的 MetricDefinition，读取它声明的来源（新增定义字段 `source_kind ∈ {commercial_record, cost_entry}` 和 `source_filter`，例如 `kind in (payment, renewal)`），为每条源记录生成一条观察。
- `observation_id` 的确定性推导：`sha256(metric_id||version||source_record_id||source_revision)`。同一修订重复投影，结果是 `replayed`。
- `data_mode` 继承来源记录，`occurred_at` 取业务时间，`evidence_refs` 包含 `commercial:<record_id>@<revision>`。

### 5.2 更正链（补 0068 的缺口）

在 `nexloop_metric_observations` 上新增列：`supersedes_observation_id`、`retracted boolean`（默认 false）。
- 源记录出现新修订，或者被退款、被更正时，投影写一条新观察，`supersedes` 指向旧观察。
- 源记录被撤回（例如 provider 宣布订单作废）时，写一条 `retracted=true` 的观察。
- 表仍然 append-only，旧观察不改。
- 计算 KR 时只取每条链上的最新一条，撤回的不计。

### 5.3 退款规则与 cohort（真正执行）

- `refund_rule`：
  - `net_of_refunds`：退款按原付款的 `occurred_at` 回溯冲减，口径由 MetricDefinition 固定（PRD:324）；
  - `gross`：不冲减；
  - `not_applicable`：退款记录不参与计算。
- **冻结的 cohort**：新增 `control.nexloop_metric_cohorts(metric_id, version, kr_key, cohort_key, member_ref, frozen_at, evidence_ref)`。
  - 分母成员在 KR 窗口开始时冻结。以续费率为例：成员是窗口内到期的订阅，由已核验的订阅记录确定。
  - 成员只能追加冻结前已知的；冻结后迟到的成员单独列为 `late_cohort_members`，不改变分母（PRD:324 “不能每次报告变化时重选分母”）。
  - `cohort_rule` 从自由文本改为受限的结构化规则：v1 只支持 `expiry_in_window`、`event_in_window`。
- **新的 `compute_key_result`**：沿用 0084 的做法，把现有函数改名为私有别名，再包一层新的外层函数（已发布的迁移逐字节不变）。返回：
  - `value`、`numerator`、`denominator`（各自按币种）；
  - `immature`（未成熟的观察）、`excluded_non_real`；
  - `late_cohort_members`；
  - `corrections_applied`；
  - `refund_rule_applied`。
- **币种**：带币种的定义只汇总同币种的观察。出现其他币种的观察时，计入 `excluded_other_currency`，不做换算（PRD:272）。

### 5.4 KR 的结果从哪里来，到哪里去

- KR 值只能由 `compute_key_result` 从已批准的定义计算，不执行模型生成的 SQL（PRD:114）。Agent 不能改定义、分母或窗口（AT-005 已保持）。
- 计算结果作为复评的输入，经 v6 goal 节提供给 Run，并附上数据窗口、成熟度和 real 标识。
- 新观察**不**直接唤醒复评，避免每条付款都触发一次复评。改由 `commercial-verified` 唤醒（§4.2），或者在复评周期中读取最新的 KR。

## 6. 费用与成本指标

### 6.1 费用条目

`runtime.nexloop_cost_entries`：

| 字段 | 说明 |
|---|---|
| tenant, world, data_mode | data_mode 继承来源 |
| entry_id | 确定性 ID：`sha256(kind||source_ref)`，重复写入幂等 |
| cost_kind | `model` / `channel` / `discount` / `service` / `labour` |
| amount | `numeric(20,8)`（模型费用需要高精度，D03:27） |
| currency | char(3)，必填 |
| basis | `provider_reported` / `configured_rate` / `operator_entered` |
| source_ref | 例如 `model:<run>:<seq>`、`effect:<intent>:<attempt>`、`incentive:<consumption_id>`、`manual:<intent>` |
| run_id, consumer_ref, goal_version_ref | 可空，用于归属展示（不表示因果） |
| occurred_at, recorded_at, recorded_by | — |
| corrects_entry_id | 更正链；不原地改 |

每一类费用的来源：
- **model**：`runtime.nexloop_model_requests` 写入 `cost` 时（0100 的结果写入路径），同一事务内追加一条费用条目。
  - **币种（决策 D4）**：模型请求本身没有币种列。建议新增 `currency` 列，取值来自可信的 provider 档案（`trusted-model-profile`，现在是 USD），并校验与 Run 命令的 `budget.currency` 相同；不同就拒绝写结果。
  - `basis='provider_reported'`。
- **channel**：effect 账本里只有单位数。v0.1 的金额来自 Offering 或 provider 绑定上配置的单价（`basis='configured_rate'`），在 effect attempt 被接受时追加。provider 能回报实际费用时，以更正链替换。没有配置单价的渠道不记金额，只记单位数，并在 `/costs` 中标为“未计价”。
- **discount**：折扣或优惠类 Action 在 `reserve_budget('incentive', …)` 成功时，同一事务追加一条（与 NX-026 中“涉及金钱的履约 intent 预留 incentive 预算”一致）。
- **service / labour**：人类 Action `nexloop.cost.record:1`，`basis='operator_entered'`。

### 6.2 与 NX-022 预算的对接（预留与结算）

- **现状**：每个 Run 派发时按 `maximum_cost` 一次性预留，之后从不释放，所以预算按上限而不是实际花费消耗。
- **新增 `authz.nexloop_settle_budget(digest, world, kind, consumption_id, actual_amount)`**：
  - 只能对已存在的预留行调用一次，在 `nexloop_budget_consumption` 中追加一条 `amount = actual − reserved`（≤ 0）的**释放行**（`consumption_id = <原 id>:settle`，`source_ref` 指向原预留）；
  - `actual > reserved` 时不释放、也不补扣，记一条审计。Host 侧按次的上限检查已经保证实际不超过预留（`pi-runtime-adapter` 的 `consume`/`checkCost`）；
  - 账本仍然 append-only，“已用”的算法不变（按行求和），所以超限判定（NXB01）自然反映结算后的余额；
  - 重复调用的处理方式与 `reserve_budget` 一致：相同 → `replayed`，不同 → NXB02。
- **调用时机**：Run 结束时（派发器收到终态，或 Run 超时或被取消之后），按该 Run 在 `nexloop_model_requests` 中的费用合计结算。结果未知的调用（`result_status='unknown'`）按预留上限计，不释放（保守做法，与“结果未知不能盲目当失败”的原则一致）。
- **incentive 预算**：折扣 Action 调用 `reserve_budget('incentive', amount_minor, currency)`。用尽时返回 NXB01，Action 被拒绝（AT-043 的折扣部分）；已经入账的合法结果保留。
- 降低预算上限时，已有的合法消耗不改写（0068 已有的行为）。

### 6.3 成本指标

- 成本类 MetricDefinition 的 `source_kind=cost_entry`，按 `cost_kind` 和币种汇总，例如“本月模型费用（USD）”。
- **单位成果成本**（例如每次续费的成本）只作为两个同币种指标的比值来展示，并标明“相关性，不是归因”（PRD:274）。不把付款归功于最后一条消息，不计算 ROI 结论。
- 不同币种的成本和收入分别列出，不换算（换算需要单独的汇率来源与 ADR）。

## 7. real / test / simulation

- **决策 D1（建议）**：采用事件信封契约（`event-envelope.schema.json`：mode=real 当且仅当 world=real）。
  - test 连接器只写 test world，real world 中只有 real 的商业数据；
  - 0068 的观察表在 world real 中仍允许 `data_mode='test'`，这条检查不改（已有数据不受影响）；但商业记录、费用条目和指标投影三处另加约束：world=real ⇒ data_mode=real；
  - AT-042 因此在结构上成立：real world 的 KR 计算根本读不到 test 付款。
  - **替代方案**：允许 test 连接器写 real world 并标 `data_mode='test'`，依赖计算时过滤。缺点是 real 库中混入测试数据，一个过滤遗漏就会污染 real 指标。
- simulation / shadow 没有连接器，也不能调用入口（AT-046，模型伪造的 mode 由服务端拒绝）。
- 工作台和 API（`/metrics`、`/costs`、`/commercial-observations`）每一项都显示 world 和 data_mode（D10:7）。

## 8. 删除、保留与隐私（D6：负责人 2026-10-10 决定，原话“同意你的建议，默认保留 1 年”）

**删除 Consumer 时（NX-029），商业记录不删除，假名化后保留：**
- **换成不可回溯的假名**：`consumer_ref` 和 provider 的客户标识（记录字段、`commercial_customer_links` 中的映射）。
  - 删除时，为这个 Consumer 生成一个随机假名（不由原标识推导，不是 HMAC 或哈希），同一 Consumer 的所有记录共用它，以便对账时还能看出这些记录属于同一主体；
  - 原标识到假名的对应关系不保存。删除凭据中只记录“已假名化的记录数”，不记录对应关系；
  - provider 之后再推送这个客户的事件时，会得到一个新的、无关联的 `unlinked` 记录，不会重新关联到已删除的主体。
- **保留**：金额、币种、时间（`occurred_at` 和记录时间）、kind、状态、更正链和退款链、来源事件引用，用于对账与审计。
- **个人字段从源头不存**：载荷中的个人字段（姓名、邮箱、地址等）在入口就不在白名单里，所以删除时不需要额外擦除。
- **指标观察**：本来只有引用和数值，不含个人信息。删除不改变已冻结 cohort 的计数，cohort 成员引用换成同一个假名。
- **费用条目**：`consumer_ref` 同样换成假名，金额和来源保留。

**保留期：**

| 数据 | 保留期 | 依据与配置 |
|---|---|---|
| 规范商业记录（CommercialRecord，含修订与更正链） | **财务保留期，默认 365 天** | 部署配置项 `financial_retention_days`，写在版本化的 `deploy/configuration/commercial.v1.json` 中；企业可以按自身合规要求另配 |
| 费用条目（`cost_entries`） | 同上，`financial_retention_days`，默认 365 天 | 同上 |
| 原始商业事件（`commercial_events`，含签名不通过的摘要行） | **跟随事件与消息的保留期**（03_DOMAIN_DATA_MODEL §7 的产品默认，D03:96），不额外延长 | 沿用事件和消息的保留配置，不新增单独的配置项 |
| 指标观察与冻结 cohort | 跟随指标聚合的保留期（D03:96，默认 1 年） | 沿用现有的产品默认 |

- **到期退出与 NX-029 衔接**：
  - 由 NX-029 的保留清理按配置执行；
  - 规范记录和费用条目到期后，内容清除，只留不含金额和原标识的审计 tombstone（D03:29：append-only 不等于永久保留原文）；
  - 已经计算出并超出保留期的 KR 结果，不因源记录退出而重算。
- **配置校验**：`financial_retention_days` 必须是正整数。配置值短于更正窗口或指标成熟窗口时，doctor 给出警告，但不阻断，由企业自行决定。

## 9. 契约、Action、服务主体与配置

- **新契约** `packages/contracts/commercial-event.schema.json`：入口载荷，要求 `event_id`、`kind`、`external_id`、`occurred_at`、`amount_minor`（整数）、`currency`、`customer_ref`，可选 `provider_sequence`、`related_external_id`（退款时指向原付款）、`status`。附加属性一律拒绝。
  - 注：Claim 契约中的 `money.amount` 是 JSON number，与 D03:27 的整数最小单位不一致。NX-027 不改 Claim 契约，但商业事件只接受 `amount_minor`。
- **对象类型**：在 NX-026 新建的 `deploy/ontology/business-object-types.v1.json` 中追加 `CommercialRecord` v1（与 Commitment 同一份清单）。
- **business Actions**（`deploy/configuration/business-actions.v1.json`）：
  - `CommercialRecord.record:1`、`CommercialRecord.correct:1`：服务主体，执行者 `commercial_recorder`；
  - `nexloop.commercial.connector.configure:1`、`nexloop.commercial.customer.link:1`、`nexloop.cost.record:1`：只限人类负责人。
- **服务主体**（`deploy/authorization/service-grants.v1.json`）：
  - `commercial_recorder`：记录 Actions、读取原始事件、写承诺证据；
  - `metric_projector`：只有 `record_metric_observation` 和只读 Function；
  - 结算由现有的 `runtime_worker` 或调度器在 Run 终态时调用（新增对 `settle_budget` 的授权）。
- **配置**：`deploy/configuration/commercial.v1.json`，包含事件种类与状态机、ISO 4217 小数位表、默认重放窗口和保留期。
- **连接预算**：两个新的常驻服务各一个进程、各一个连接池。如果 `nx049-deploy-config` 已合入，需要在 `deploy/stage/connection-budget.v1.json` 中登记，doctor 会核对合计 ≤ 60。

## 10. 迁移形状（占位编号，接在合并时的最高号之后：NX-025 的 0108–0110 和 NX-026 之后）

1. **`nx027_commercial_intake`**：
   - 表：`control.nexloop_commercial_connectors`（含密钥，应用角色无权限）、`control.nexloop_commercial_customer_links`、`runtime.nexloop_commercial_events`；
   - 函数：`authz.nexloop_commercial_ingest`（入口）、`authz.nexloop_commercial_event_read`（供 recorder 使用）、连接器配置与客户关联的受治理端口（走 NX-022 的 `goal_governed_action`。0109 刚改过这个函数，所以要沿用 0084 的改名包装方式，再包一层）；
   - work feed kind `commercial-raw`、`commercial-verified`：需要放宽 `runtime.nexloop_work_feed` 的 CHECK。0106 定义了它，NX-025 和 NX-026 都会改，须在合并时按最新版本重写；
   - 全部函数 `search_path = pg_catalog, pg_temp`，SECURITY DEFINER 并显式设置 search_path（0098 规则）；不创建 TEMP 对象。
2. **`nx027_metrics_costs`**：
   - `nexloop_metric_observations` 新增 `supersedes_observation_id`、`retracted` 列（加列不改已发布迁移）；
   - `nexloop_metric_definitions` 新增 `source_kind`、`source_filter`、结构化的 `cohort_rule_v2`（旧列保留，新定义填新列）；
   - 新表 `control.nexloop_metric_cohorts`、`runtime.nexloop_cost_entries`；
   - `nexloop_model_requests` 新增 `currency` 列（D4），在 0100 的结果写入路径上用包装函数追加费用条目；
   - 函数：`authz.nexloop_settle_budget`，新版 `compute_key_result`（改名包装），`authz.nexloop_cost_read`、`authz.nexloop_metric_read`；
   - 约束：商业、费用和投影三类表都要满足 world=real ⇒ data_mode=real（D1）。
3. **`nx027_commercial_feeds`**：
   - 复评唤醒：在 0106 的唤醒来源中加入商业记录变更；
   - 承诺证据写入：调用 NX-026 的 `authz.nexloop_commitment_command` 中的 evidence 动词，不直接写它的表；
   - 读取端口 `GET /commercial-observations`、`/costs`、`/metrics` 对应的 Function。

已发布的迁移文件与校验和都不变。不运行 ORM 自动建表。

## 11. 测试清单（真实 PG、合成数据，关键用例不得 skip；E2E 走真实 HTTP 入口、真实 Host 与 Pi，使用确定性 provider）

**入口与核验**
- 签名正确 → `verified`，写入 feed，返回 2xx。签名错误、密钥过期、时间戳超出窗口、连接器停用 → 分别是 401、401、拒绝且不写证据行、拒绝；都不产生业务变化。
- 载荷不符合 schema、kind 不允许、币种不允许、金额不是整数 → 400，`rejected_schema`。
- **AT-012**：同一事件重复 10 次 → 只有一条原始事件被处理（其余为 `duplicate`），只有一次 `CommercialRecord.record` 业务变化，只有一条 KR 观察；同一 ID 不同载荷 → 409 + 审计。
- 并发的同一事件（多个线程同时到达）→ 只有一个首次到达，其余为 duplicate（唯一键加事务）。
- 租户与 world 只取自连接器：信封里伪造的 tenant 或 world 被忽略；跨租户的连接器 ID 被拒绝。
- 密钥在任何日志、错误信息和响应中都不出现（扫描输出）。

**规范记录与更正**
- 先 pending 再 succeeded → 同一条记录的两次修订；先 succeeded 再晚到的 pending → 状态不回退，晚到者入链并标 `late`。
- provider 金额更正 → 新修订，`corrects` 链完整，旧修订保留。
- 部分退款与全额退款 → 原记录状态推进，退款记录链接到原记录；超额退款 → `conflict`，不自动截断。
- **AT-011**：只有顾客陈述（Claim）而没有事件 → 没有 CommercialRecord，付款状态不变；付款页报错的消息 → 不变。
- 映射不到 consumer → 记录照写，`unlinked`，不猜测，出现在待关联列表中。

**real / test**
- **AT-042**：test 连接器的付款只进入 test world；real world 的 KR 计算读不到它，`excluded_non_real` 不受影响；在 real world 中试图写 `data_mode='test'` 的商业记录、费用或投影 → 被约束拒绝。
- simulation / shadow 的 Run 不能调用入口（服务端拒绝）。

**指标与 KR**
- 投影的确定性：同一修订投影两次 → `replayed`；新修订 → 新观察带 `supersedes`；撤回 → `retracted`，KR 不计。
- `net_of_refunds` 回溯冲减、`gross` 不冲减、`not_applicable` 忽略退款，三者结果各不相同，并与手算一致。
- cohort 冻结：冻结后迟到的成员不改变分母，单独列出；Agent 或服务改不了已批准的定义（AT-005 保持）。
- 币种：同一定义中出现其他币种的观察 → 计入 `excluded_other_currency`，不相加。
- 成熟窗口：未成熟的观察单独列出，不计入。

**费用与预算**
- 模型调用写入结果 → 同一事务追加 model 费用条目，币种与 Run 预算一致；币种不一致 → 拒绝写结果。
- Run 结束结算：实际额小于预留 → 追加释放行，余额回升；重复结算 → replayed；实际额大于预留 → 不补扣，写审计；有结果未知的调用 → 按预留计。
- **AT-043**：
  - 模型预算用尽 → 新 Run 派发被 NXB01 阻断，已完成的 Run 及其结果不受影响；
  - 折扣预算用尽 → 折扣 Action 被拒绝，已入账的折扣不受影响；
  - 降低上限后，已有的合法消耗不改写。
- 渠道单价未配置 → 只记单位数，`/costs` 标“未计价”。

**下游**
- J01：已核验付款 → `commercial-verified` 唤醒该 consumer 的续费提醒计划 → 复评结论为不再提醒 → 已排队的提醒在派发时按控制快照处理。端到端使用真实 Host 与 Pi，确定性 provider。
- NX-026：付款类承诺在记录写入时得到 `commercial_event` 证据并合格；匹配不到时不写；test world 的记录不会给 real 承诺写证据。

**删除与保留**（D6）
- 删除 Consumer 后：
  - 商业记录、费用条目、观察和 cohort 中的 `consumer_ref` 与 provider 客户标识都换成同一个随机假名；
  - 金额、币种、时间、更正链和退款链不变，KR 计数不变；
  - 数据库中找不到原标识到假名的对应关系；原始事件中没有个人字段。
- 删除之后，provider 再推送同一客户的事件 → 新记录为 `unlinked`，不会重新关联到已删除的主体。
- `financial_retention_days` 默认 365；配置为非正整数时启动失败；配置值短于成熟窗口时 doctor 给出警告。
- 到期清理（与 NX-029 联测）：规范记录和费用条目只留 tombstone（不含金额和原标识）；原始事件按事件和消息的保留期退出。

## 12. 与现有分支的文件重叠

| 分支 | 重叠点 | 处理 |
|---|---|---|
| `nx025-impl`（0108–0110，未合入） | `goal_controls.py`、`plan_reevaluation.py`、`work_feed.py`、`background_services.py`、`container_entrypoint.py`、`service-grants`、契约；0109 重写了 `nexloop_goal_governed_action`，并放宽了控制事件的 `event_kind` | NX-027 在 NX-025 合入后实现，用改名包装再包一层 governed action。不新增控制事件种类 |
| `nx026-design`（未实现） | `business-object-types.v1.json`（新建）、work feed CHECK、`nexloop_commitment_command` 的 evidence 动词、`goal_governed_action`、`service-grants` | CommercialRecord 追加进同一份清单；证据只通过 NX-026 的端口写入；实现顺序排在 NX-026 之后 |
| `nx049-deploy-config` / `nx049-host-concurrency` | `doctor.py`、`deploy/stage/connection-budget.v1.json` | 新增的两个常驻服务登记进连接预算 |
| `nx051-design`（文档） | Message 上的 provider sequence 与 reply-to | 没有文件重叠。“provider 序号”的处理口径建议两边保持一致 |
| main 上已有的文件 | 0068（指标、预算）、0089/0100（模型请求）、0106（work feed、复评唤醒）、`effect_*`（L4）、`apps/agent-host`（费用上报，不改） | 都用加列或改名包装的方式，已发布的迁移不改；`effect_*` 的渠道单价接入需要与 L4 协调 |

## 13. 裁定（调度员 2026-10-10）与仍待决事项

已裁定：
- **D1 采用**：test 连接器只写 test world，不写 real。商业记录、费用条目和指标投影都约束为 world=real ⇒ data_mode=real（§7）。
- **D2**：v0.1 只做通用签名适配器和 test 连接器，不声称营收增长。真实连接器上线前，由负责人确认来源、签名方案和保留期（B 类）。**本任务不做真实连接器。**
- **D3 采用**：CommercialRecord 是 EIOS 正式对象，走 Action 和对象版本（§2、§3.3）。
- **D4 采用**：`nexloop_model_requests` 加 `currency`，取自可信的 provider 档案；与 Run 预算币种不一致时，拒绝写结果（§6.1）。
- **D5 采用**：结算用追加释放行，不改写已有行；结果未知的调用按预留额计（§6.2）。
- **D7 裁定**：v0.1 记录单位数。部署配置里配了单价，就同时计算金额（`basis='configured_rate'`）；没配只记单位数，`/costs` 标“未计价”。单价写在版本化的部署配置里（`deploy/configuration/commercial.v1.json` 中按 provider 绑定或 Offering 列出），不由运行时 Agent 或 API 设置。

- **D6 负责人已定（2026-10-10，原话“同意你的建议，默认保留 1 年”）**：
  1. 删除 Consumer 时，商业记录不删除，假名化保留：`consumer_ref` 和 provider 的客户标识换成不可回溯的假名，金额、币种、时间和更正链保留，用于对账与审计；
  2. CommercialRecord 和费用条目的财务保留期是配置项，默认 365 天，写在版本化的部署配置中，企业可以另配；到期退出与 NX-029 衔接；
  3. 原始商业事件跟随事件和消息的保留期（03 §7 的产品默认），不额外延长。
  
  细节见 §8。

实现前提：NX-025、NX-026 合入 main。之后按 §14 的四个切片依次实现。D6 的保留与假名化在切片一中实现；到期清理的执行由 NX-029 负责，NX-027 提供配置项、tombstone 形状和假名化端口。

## 14. 实现切片建议（NX-025、NX-026 合入后）

1. **切片一**（AT-011、AT-012）：入口、连接器、原始事件、recorder、CommercialRecord 类型与 Actions、更正和晚到处理、test world 隔离。
2. **切片二**（AT-042、M19）：投影、更正链、退款规则、冻结 cohort、新版 `compute_key_result`、`/metrics`。
3. **切片三**（AT-043、费用）：费用条目（model、discount）、结算、incentive 接入、`/costs`；渠道费用视 D7 决定。
4. **切片四**（衔接）：`commercial-verified` 唤醒复评、承诺证据、J01 端到端。
