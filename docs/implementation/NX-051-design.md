# NX-051 Message 的 provider 序号与引用回复：设计稿（待调度员审核，未实现）

- 任务：NX-051（M09，S4），对应 AT-014“消息次序：乱序入站与引用回复 → 保留原顺序与接收顺序，不破坏证据”。
- 依据：
  - `docs/handoff/docs/04_CONVERSATION_ONTOLOGY_PIPELINE.md` §11：消息时间与入库时间都保存；乱序时分配 received_sequence，另保留 provider sequence；重建对话不能改写原始证据；清洗时保留引用关系。
  - NX-019 已知限制：Message 没有 provider 序号和 reply_to，AT-014 无法判定。
  - ADR-023 §2.6/§2.7：回复绑定由服务端推导。
  - NX-047：外发链路。
- 分支：`nx051-design`，从 main `587e7c2` 切出。本稿只写文档。

---

## 1. 现状（已核对代码）

| 方面 | 现在的做法 | 位置 |
|---|---|---|
| Message 正式对象 | 属性只有 `conversation_id, sequence, actor, body, accepted_at`，经 `Message.create:1` 受治理写入 | `conversation_messages.py::conversation_schemas`；0046 |
| 接收顺序 | `sequence` 在提交时于会话行锁下分配（`last_sequence+1`），即接收顺序，此后不改 | 0046 `commit_message` |
| 原生 WebChat 入站 | `nexloop.native-message.v1`：`provider_event_id`（UUID）加 `transport_key`；按 `(namespace, principal, provider_event_id)` 去重（AT-013 已通过）；没有渠道序号、发送时间、引用 | 0055、`native_web_inbound.py`、`web_chat_http.py`、`apps/web/src/native-message.ts` |
| 外发（Agent 回复） | 意图受理同一事务写外发记录，其中 `trigger_message_id` 由服务端从消息 Run 的签发推导，兜底回复从兜底签发推导（0110）；渠道接受后物化为 Message，拿下一个 `sequence`，record 带 `direction='outbound'`、`trigger_message_id` | 0079、0110 |
| 渠道回执 | 效果观测有 `provider_reference`、`provider_state`、`observed_at` | 0042 |
| ADR-023 绑定 | 受限客户或兜底 Run 的外发，只有当外发记录的 `trigger_message_id` 指向本客户、本会话的入站消息，且在窗口内、每条至多一条时才放行 | 0109/0110 `control.nexloop_contact_assert_intent` |
| 提取窗口 | 按接收 `sequence` 取窗口，并断言输入按接收序递增（`conversation_extraction.py:179`）。更正链检查用 `source_sequence` 比先后（:512）。Claim 的 `valid_time.anchor` 用 `accepted_at`。输入版本 digest 含 message_id、sequence、content_hash、speaker | `conversation_extraction.py`、`claim_store.py` |
| 读取 | 浏览器读接口返回 `sequence`、`body`、`accepted_at`；外发项另带 `direction` | `web_chat_http.py`、`apps/web/src/chat-api.ts` |
| 契约 | `packages/contracts` 里没有 Message 契约 | — |

由此可见：接收顺序已经有了，而且不可改写；缺的是渠道一侧的顺序、渠道时间和引用关系，以及它们在提取中的用法。

## 2. 概念

| 名称 | 含义 | 来源 | 是否可改写 |
|---|---|---|---|
| `received_sequence` | 本系统受理顺序，即现有 `sequence`，名字不变 | 服务端在提交时分配 | 永不改写 |
| `provider_namespace` | 渠道命名空间，如 `native.webchat`、`nexloop.agent`，以及未来接入的签名渠道 | 服务端按入口确定 | 永不改写 |
| `provider_message_ref` | 渠道对这条消息的标识：入站为 `provider_event_id`，外发为渠道回执的 `provider_reference` | 渠道 | 永不改写 |
| `provider_sequence` | 渠道给出的会话内顺序（可空） | 渠道 | 永不改写 |
| `provider_sent_at` | 渠道给出的发送时间（可空） | 渠道 | 永不改写 |
| `trust` | `server`：服务端自己产生；`signed`：签名渠道、经验签；`client`：浏览器客户端自报，只是陈述 | 按命名空间注册 | 永不改写 |
| `reply_to` | 引用关系，原始引用（provider ref）与解析结果分开存 | 入站来自渠道；外发由服务端推导 | 原始引用永不改写；解析结果只追加事件 |

原则：渠道给出的顺序、时间和引用都是**证据**，不是权限。它们不影响授权、派发和 ADR-023 绑定，也不改写 `sequence`、`accepted_at` 或正式 Message 属性。

## 3. 入站：获取与持久化

### 3.1 原生 WebChat（现有唯一入站渠道）
- 新增请求体版本 `nexloop.native-message.v2`，v1 照常接受（字段为空）。v2 在 `provider_event_id`、`body` 之外可选带：
  - `client_sequence`：客户端为该会话维护的单调整数（1..2^53）；
  - `client_sent_at`：严格 UTC 时间；
  - `reply_to_provider_event_id`：被引用消息的 provider_event_id，可以是入站，也可以是 Agent 外发在客户端看到的 `provider_message_ref`。
- 这些都是客户端陈述，`trust='client'`。服务端只校验形状、范围和时间偏差：`client_sent_at` 超出 `accepted_at ±` 配置偏差的，原样保存并标记 `skewed`，不拒收消息。它们决不替代 `accepted_at`，也不参与授权。
- `apps/web` 在发送时填这几个字段：发送序号由本地队列给出；用户点了“引用”就带上被引用项的 id。

### 3.2 签名渠道（扩展点，本任务不接入具体渠道）
- 渠道适配器登记命名空间，`trust='signed'`。验签通过后，`provider_sequence` 和 `provider_sent_at` 视为渠道事实，在偏差窗口内可用于排序（见 §5）。
- 需要几个共同约定：同一渠道会话可能映射到本系统多个会话；重投仍按 provider_event_id 去重（AT-013 规则不变）；渠道序号回绕或重置时在命名空间配置里声明，否则按无序号处理。

### 3.3 持久化（同一提交事务，“先持久化再 ACK”）
- 新表 `runtime.nexloop_message_provider_facts`，每条消息一行，只追加，RLS 按租户：
  - 字段：`message_id` 为主键，`conversation_id`、`direction`（inbound/outbound）、`provider_namespace`、`provider_message_ref`、`provider_sequence`、`provider_sent_at`、`trust`、`skew_flag`、`recorded_at`。
  - 原生 WebChat 入站在 0055 的 `commit_message` 同一事务写入。
  - 非原生入口（受治理 API `accept_message`）写 `provider_namespace='nexloop.api'`、`trust='server'`，序号和时间为空。
- 新表 `runtime.nexloop_message_reply_links`，只追加事件，用来描述引用：
  - 字段：`message_id`、`raw_reply_ref`（原始 provider ref）、`reply_to_message_id`（可空）、`resolution`（`resolved`、`pending`、`foreign_conversation`、`unknown`）、`source`（`provider` 或 `server`）、`recorded_at`。
  - 提交时就地解析：在同一租户、同一会话内按 `provider_message_ref` 找到被引用消息即为 `resolved`。属于别的会话或别的 consumer 的，记为 `foreign_conversation`，不建立链接，也不泄露对方信息。找不到的记为 `pending`。
  - 晚到：被引用的消息之后才到时，在它的提交事务里为等待中的引用追加一条 `resolved` 事件。原 `pending` 行不改，读取时取最新事件。
- 正式 Message 对象：建议**不改 v1 类型**。provider 事实放在 runtime 侧，经受治理读取派生给需要的读者，见 §9 决定 1。

## 4. 外发：获取与持久化

- **reply_to 由服务端推导**：Agent 回复和兜底回复的 `reply_to_message_id` 就是外发记录的 `trigger_message_id`。它在意图受理时由服务端从消息 Run 或兜底签发推导，模型无法指定，也没有改引用的工具参数。物化时写一条 `source='server'`、`resolution='resolved'` 的引用事件。物化的 Message record 增加兼容字段 `reply_to_message_id`（等于 trigger）。
- **渠道回执**：渠道接受时（效果观测里 `provider_state` 为 accepted 或 fulfilled，此时 outbound 进入 `provider_accepted` 或 `delivered`），物化的同一事务写 provider 事实：
  - `provider_namespace='nexloop.agent'` 或交付渠道命名空间；
  - `provider_message_ref` 取观测的 `provider_reference`；
  - 渠道回执带序号时写 `provider_sequence`（本地 JSON 交付没有，写空）；
  - `provider_sent_at` 取观测时间；
  - `trust` 为 `server` 或 `signed`。
- 物化依旧按接收顺序拿下一个 `sequence`。渠道的发送时间和序号只作为证据并列保存。

## 5. 与 trigger_message_id、ADR-023 绑定的关系

- `trigger_message_id` 仍然是唯一的**授权依据**。ADR-023 的放行条件（同会话、同 consumer、窗口内、每条至多一条、兜底签发对应的消息）一律只看它，不读 `reply_to`。
- 外发的 `reply_to_message_id` 就是 `trigger_message_id` 的镜像，供展示和证据使用，不能单独写入，也不能和 trigger 不一致（物化时由 SQL 断言）。
- 入站的 `reply_to`（顾客引用了某条消息）只是证据。它不改变“待回复”的登记（每条入站消息仍各自登记一项），也不让 Agent 获得回复别的消息的权利。
- 顾客引用 Agent 的某条外发时，可以帮助判断那条外发的话题，但不会把被引用的外发变成可回复对象。

## 6. 对提取窗口排序的影响

- **窗口划分不变**：调度器仍按 `received_sequence` 划窗口。窗口键和“同输入版本重跑幂等”（AT-020）保持确定。晚到的消息拿新的接收序号，自然进入下一个窗口。
- **提示中的排列**：
  - 窗口内所有消息都来自同一个 `trust='signed'` 的命名空间且都有 `provider_sequence` 时，按渠道序排列；
  - 否则按接收序排列；
  - 两者不一致的消息加标记 `late`（按接收序晚到、按渠道序更早）。
  - 每条消息附上 `provider_sent_at`、可信度和已解析的 `reply_to`（被引用消息在窗口内时给出它的引用编号，在窗口外时给出原文摘要的 hash 和接收序号）。
  - 原生 WebChat 是 `client` 级可信度，这时只把客户端序号和时间作为“顾客自称”的附加信息给出，排列仍按接收序。
- **守卫规则调整**：
  - 输入断言（`:179`）改为：按“生效顺序”严格递增，同时接收序必须唯一。生效顺序指可信的渠道序，否则为接收序；
  - 更正链（`:512`）比较生效顺序，并在 `guard_flags` 中记录这次用的是哪种顺序；
  - `valid_time.anchor` 只在 `trust='signed'` 且在偏差窗口内时用 `provider_sent_at`，其余情况仍用 `accepted_at`。这影响 AT-022 晚到事实的判断，只对签名渠道生效。
- **输入版本**：digest 增加每条消息的 provider 事实和引用解析的最新事件 id。晚到解析会产生新的输入版本，从而重提取；Claim 按 claim_id 和 correlation_key 去重，不会翻倍。
- **NX-020 晚到证据规则**（`effective_at`）只对签名渠道改用渠道时间，其余不变。

## 7. 迁移形状（临时号，接在 main 当前最大号之后）

1. 新表 `runtime.nexloop_message_provider_facts`、`runtime.nexloop_message_reply_links`：只追加（引用为事件），FORCE RLS，撤销应用角色的直接权限。
2. 新表 `control.nexloop_provider_namespaces`：命名空间、可信度、偏差窗口、序号回绕声明。种入 `native.webchat`（client）、`nexloop.api`（server）、`nexloop.agent`（server）。改动走受信配置，版本化。
3. 原生入站：`authz.nexloop_native_web_message` 增加 v2 字段校验，并在 `commit_message` 同一事务写 provider 事实和引用；v1 路径保留。采用新函数体加 `create or replace`，不改已发布迁移。
4. 通用入站 `commit_message`（0046 路径）：触发器（与 0109 入站保护同一 `after insert` 位置）写 `nexloop.api` 的 provider 事实，并解析等待中的引用。
5. 外发物化（0079 的 `authz.nexloop_outbound_message_command`）：写 provider 事实和 `source='server'` 的引用事件；record 增加 `reply_to_message_id`；断言 reply_to 等于 trigger。
6. 读取：会话读函数与提取 `load_window` 读函数返回 provider 事实和最新引用。Message READ 派生规则（0077/0086）不变：能读消息就能读它的 provider 事实，引用目标仍需单独的 READ。
7. 契约：新增 `packages/contracts/conversation-message.schema.json`（读接口的消息投影，含 `reply_to_message_id`、`provider` 段，均为可选；见 §9 决定 2）；`apps/web` 的 chat-api 类型同步。

## 8. 测试清单（真实 PG，合成数据）

**AT-014 主证据**
1. 乱序入站：同一会话客户端序号为 2 的消息先到、序号为 1 的后到。接收序为 1、2，provider 事实保留客户端序号 2、1，`sequence` 与 `accepted_at` 不被改写；读接口两种顺序都可见。
2. 引用回复：顾客引用 Agent 的外发，引用解析为 `resolved` 并指向那条 Message；引用了尚未到达的消息时为 `pending`，被引用消息到达后追加一条 `resolved` 事件，原行保持不变。
3. 证据不被破坏：重建或重读会话不会改写原始 sequence、body、provider 事实；provider 事实表和引用事件表 update/delete 被拒；应用角色直接读表被拒。

**安全与隔离**
4. 引用别的会话或别的 consumer 的消息时记为 `foreign_conversation`，不建立链接，响应和读接口都不泄露对方信息；跨租户同理。
5. 客户端可信度：v2 的 `client_sent_at` 偏差过大时标记 `skewed`，消息照收；`accepted_at` 不受影响；客户端字段不能触发任何授权变化。
6. 去重（AT-013 回归）：同一 provider_event_id 重投仍按技术重复处理，provider 事实只有一行。

**外发与 ADR-023**
7. Agent 回复物化后 `reply_to_message_id = trigger_message_id`，provider 事实写入回执的 `provider_reference`；兜底回复同样如此。伪造的、不一致的 reply_to 在 SQL 断言处被拒。
8. ADR-023 回归：顾客在受限状态下引用 Agent 某条旧外发，绑定回复判定仍只看 trigger，放行和拒绝结果与引用无关；`test_contact_reply_dispatch_pg`、`test_reply_fallback_pg` 全绿。

**提取**
9. 签名渠道（合成命名空间 `synthetic.signed`）的乱序窗口：提示按渠道序排列并带 `late` 标记；更正链按生效顺序判定；`valid_time.anchor` 用渠道时间。原生 WebChat 窗口仍按接收序，并带“顾客自称”的序号和时间。
10. 晚到引用解析产生新的输入版本，重提取不产生重复 Claim（AT-020 回归）。
11. 冻结数据集补充用例：引用回复、乱序更正、跨窗口引用。原有 100 例不删、不改。

**接口与契约**
12. 浏览器 v1/v2 请求都被接受；v2 字段形状错误时返回 400，且不写任何行；读接口投影符合新契约；`apps/web` 的 vitest 覆盖发送字段与引用展示。

## 9. 需要调度员或负责人决定

1. **provider 事实与引用放在哪里**：建议放在 runtime 表，经派生读取，正式 Message 类型保持 v1。替代方案是把 `provider_sequence`、`reply_to_message_id` 等加进 Message 类型 v2，走 Schema 发布（NX-044 流程）和迁移期双版本。
2. **契约**：新增 `conversation-message.schema.json` 作为读接口投影契约（`reply_to_message_id` 与 provider 段可选），还是只在现有接口文档里扩字段？
3. **原生 WebChat 的客户端序号和时间**：确认只作为 `client` 级证据，不参与排序，也不作为时间锚点。
4. **签名渠道**：本任务只做扩展点和合成命名空间测试，不接入真实渠道（真实渠道需要凭据和负责人选择）。
5. **AT-014 的判定口径**：原生 WebChat 只有客户端陈述的序号。建议以“保留两种顺序且不改写证据，引用关系可追溯”判 passed；“按可信渠道序重排提取”以合成签名渠道测试为证，真实渠道到接入时补证。
