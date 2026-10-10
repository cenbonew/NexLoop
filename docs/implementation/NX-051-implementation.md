# NX-051 Message 的 provider 序号与引用回复：实现说明

设计稿见 `NX-051-design.md`（调度员已审，§10 五点裁定）。分支 `nx051-impl`，从 main `29035c8` 切出，带上设计稿提交。迁移号 `0120`、`0121` 是临时编号（调度员指定 0120–0129），合并时由调度员重排。全部测试使用合成数据，在本地真实 PostgreSQL 上运行；不接入任何真实渠道（裁定 4）。

## 1. 存储（0120，裁定 1）

- `control.nexloop_provider_namespaces`：命名空间、可信度（server / signed / client）、偏差窗口、序号回绕声明。只追加。种子数据：
  - `native.webchat`：client，300 秒；
  - `nexloop.api`：server；
  - `nexloop.agent`：server。
- `runtime.nexloop_message_provider_facts`：每条 Message 一行，主键 (tenant, world, message_id)。记录 provider 引用、序号、发送时间、可信度和 `skewed`。只追加，FORCE RLS，应用角色无直接权限。
- `runtime.nexloop_message_reply_links`：引用事件。
  - 解析结果为 resolved、pending、foreign_conversation 或 unknown，来源为 provider 或 server。
  - 晚到的解析只追加一条 `resolved` 事件，原行不变。读取时取最新事件。
- Message 正式类型仍为 v1，`sequence` 和 `accepted_at` 从不改写，没有发布新 Schema。

## 2. 写入路径

- **扩展点**：`runtime.nexloop_record_provider_facts`，仅属主可调用。签名渠道适配器在自己的 Message 提交事务里调用它。
  - 写入用 `on conflict do nothing`，先写者为准；
  - 偏差相对 `accepted_at` 计算；
  - 写入成功后，解析指向该 provider 引用的 pending 引用。
- **默认事实**：constraint trigger `nx051_message_provider_default`，设为 deferrable initially deferred，在提交时运行。它挂在 `runtime.nexloop_conversation_messages` 上。
  - 外发：写 `nexloop.agent` 事实，`message_ref` 和 `sent_at` 取效果回执的 `provider_reference` 与 `observed_at`；同时写一条 `source='server'` 的引用，指向服务端导出的 `trigger_message_id`。
  - 带原生事件的入站：写 `native.webchat` 事实，`message_ref` 为 provider_event_id。
  - 其他入站：写 `nexloop.api` 事实。
  - 最后，解析指向这条新 Message 的 pending 引用。
  - 与设计稿 §7.3–7.5 的差异：0046、0055、0079 的已发布函数一个都没改。也没有改 L4 正在修改的 `control.nexloop_contact_assert_intent`。外发的引用由 trigger 导出，不存在可以伪造 reply_to 的输入，因此不需要设计稿 §8.7 里“伪造 reply_to 在 SQL 断言处被拒”这一步。
- **原生 WebChat v2**（裁定 3）：新增 `authz.nexloop_native_client_facts`，签名协议为 `nexloop-native-client-v1`，只授予 `nexloop_api`，只接受浏览器会话。
  - `NativeWebMessagePort.accept_native_message` 在 `commit_message` 之后、同一事务内调用它；重放请求不调用。
  - 它只认发送者本人、本条 Message 的原生事件，写入 client 级事实，以及 `source='provider'` 的引用。
  - 客户端序号和时间只作为证据，不参与排序，也不作为时间锚点。
- **HTTP**：`POST …/native-messages` 接受 v1 `{schema_version, provider_event_id, body}`，也接受 v2（另加 `client_sequence`、`client_sent_at`、`reply_to`，三个键必须都出现，值可以为 null）。v2 形状在写入前校验，错误返回 422（设计稿写的是 400，这里沿用该端点现有的 422），不写任何行。

## 3. 读取（裁定 2）

- 新契约 `packages/contracts/conversation-message.schema.json`，是读接口的消息投影。`reply_to_message_id` 和 `provider` 段为可选，契约是唯一事实源。生成产物已重新生成：`contracts.ts`、`openapi-components.json`、`contracts.py`。
- `authz.nexloop_conversation_messages_projection`：先调用 0046 的会话读函数（不改动，同一签名信封），再给每条消息加上 `reply_to_message_id` 和 `provider`。`conversation_messages._read` 的 `messages` 动词走这里。
- `authz.nexloop_message_provider_read`：只接受调用者自己当前有效的 Message READ 证明，每条用 `assert_read_authority` 核验；Message READ 的派生规则（0077/0086）不变。提取的 `load_window` 通过它读取，只附带 `resolved` 状态的引用。
- `apps/web`：
  - `chat-api` 校验并保留 `reply_to_message_id` 和 `provider`，列表仍按 `sequence` 排列；
  - `native-message` 发送 v2：本标签页内每个会话的本地序号、冻结在逻辑消息上的客户端时间、引用时的 `reply_to`；重试时内容完全相同；
  - 引用的界面入口（引用按钮）不在本任务范围内，API 层已支持。

## 4. 提取（设计稿 §6）

- `effective_rank`：窗口内每条消息都带同一 signed 命名空间、唯一、未偏差的序号时，用渠道序；否则用接收序。窗口划分仍按接收序。
- 提示排列按生效顺序。
  - signed：附上 `channel_order` 和 `late`；
  - client：只以 `customer_stated` 给出序号和时间；
  - 已解析的引用：给出 `reply_to_ref`，即窗口内的引用编号；在窗口外时为 `'outside_window'`。设计稿原写“原文 hash + 接收序号”，这里简化了。
  - 没有 provider 证据时，载荷与 v1 逐字节相同，冻结数据集不变。
- 输入版本：只对带证据的消息，在 digest 里追加证据元素。晚到的引用解析产生新的输入版本；`claim_id` 不含输入版本，Claim 不会重复（AT-020）。
- 更正链按生效顺序判定，并记 `correction_order_signed_channel`；Claim 也按生效顺序排列，保证更正排在目标之后。
- `valid_time.anchor` 只在 signed 且未偏差时用渠道时间，并记 `anchor_signed_channel_time`。
- **0121**（实现时发现的缺口）：0069 的服务端更正检查只认接收序，会拒绝签名渠道序下合法的更正。
  - 0121 把 0069 的记录函数改名为 `_v0069` 并收回权限，新包装用同一套生效顺序规则重做更正检查，然后调用 0066 的记录函数，更正链接与 0069 相同。
  - 规则由 SQL 从 provider 事实表自行导出，不信任客户端。
  - AT-064 边界测试的白名单加入了 `_nx051_claim_correction_order`。
- 未做：NX-020 晚到证据的 `effective_at` 改用渠道时间（设计稿 §6 最后一条），以及冻结数据集补充用例（§8.11）。两者都只对签名渠道有意义，等真实签名渠道接入时补。

## 5. 测试

- `tests/test_message_provider_order_pg.py`（8 个）：
  - 乱序 v2（接收序 1、2，客户端序 2、1，两种顺序都可见）；
  - v1 与普通 API 的默认事实；
  - 引用的 resolved、pending 后晚到 resolved（原行不变）、跨会话 foreign（不暴露）、自引用 unknown；
  - 偏差标记，消息照收；
  - 重放只有一行事实；
  - 非法 v2 字段不写任何行；
  - 只追加、应用角色无权限；
  - HTTP v1/v2 接受，非法 v2 返回 4xx 且不写行。
- `tests/test_message_provider_extraction_pg.py`（3 个，合成命名空间 `synthetic.signed`，裁定 5）：
  - 载荷按渠道序排列并带 late；
  - 接收序在前、渠道序在后的更正被 Python 和 SQL 两侧接受；
  - 锚点使用渠道时间；
  - SQL 拒绝违反生效顺序的伪造更正；
  - 晚到引用解析产生新的输入版本，Claim 不重复。
  - 签名适配器由测试临时授予 `nexloop_api` 扩展点 EXECUTE（及 runtime USAGE）来代替，测试结束后收回；生产中该函数仅属主可调用。
- `tests/test_outbound_messages_pg.py`（NX-047 真实链路）：Agent 回复的 `reply_to_message_id` 等于 `trigger_message_id`；provider 为 `nexloop.agent`，`message_ref` 和 `sent_at` 等于效果回执的值；引用事件来源为 server。
- 回归：`test_contact_reply_dispatch_pg`、`test_reply_fallback_pg` 均通过，ADR-023 绑定判定不受影响。

## 6. AT-014 证据口径（裁定 5）

保留两种顺序且不改写证据、引用关系可追溯：由原生 WebChat 与 NX-047 外发的真实 PG 测试证明。“按可信渠道序重排提取”目前只有合成签名渠道（`synthetic.signed`）的测试证据，**真实渠道接入时补充真实渠道证据**。
