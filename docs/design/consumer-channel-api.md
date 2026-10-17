# 消费者通道 RESTful API（第 2 期）：设计稿

**状态**：设计稿，不实现。D1–D7 已由负责人在 2026-10-17 决定（§10），实施排入 S6。第 1 期（[客户端库、CLI 与 MCP](../implementation/consumer-channel-cli-mcp.md)）复用的是浏览器会话。本稿设计一个**不依赖浏览器会话**的正式消费者通道：渠道或集成方用自己的凭据，代表某个已绑定的消费者，向 NexLoop 投递消费者消息并读取回复。

**依据**：负责人 2026-10-17 的要求（开放 API，可以用 MCP、REST 或 CLI 发消费者消息）；ADR-023（显式拒绝与联系限制）；回复保证；NX-051（原生通道入站的 provider 事件 ID）；`web_chat_http` 与 `native_web_inbound` 的现有受治理路径。

## 1. 目标与非目标

- **目标**：
  1. 非浏览器调用方，例如测试驱动、第三方渠道适配器或企业系统，可以用**渠道级凭据**为**已绑定的消费者**发消息、读回复、查回执；
  2. 进入**与 WebChat 相同**的受治理 Conversation Action：消息 Action、联系限制、回复保证、预算与真实派发开关都照常生效，没有任何旁路；
  3. 租户与消费者身份只由服务端根据凭据与绑定推出，请求体里的租户 ID、消费者 ID 一律不被采信；
  4. 有速率限制、审计与幂等（provider 事件 ID）。
- **非目标**：
  - 不提供员工、工作台或负责人的写操作；
  - 不允许调用方创建消费者或冒充员工回复；
  - 不改变联系限制与回复保证的语义；
  - 不提供“直接写库”的批量导入。

## 2. 身份模型

| 概念 | 设计 |
|---|---|
| 渠道（channel） | 一个集成方在一个租户下的登记项：`channel_id`、`provider_namespace`（如 `api.test-driver`、`api.partner-x`）、状态与有效期。由负责人经受治理的配置 Action 登记 |
| 渠道凭据 | 渠道的服务主体凭据：签名的 bearer 令牌（D1 已决定；mTLS 留待对外合作方需要时再评估）。只能经受治理的 Action 签发，可以单独吊销，有到期时间；服务端只存 digest |
| 消费者绑定 | `(channel_id, external_consumer_ref) → consumer_id`：渠道侧的客户引用映射到 NexLoop 的 Consumer。绑定只能经受治理的 Action 建立，由负责人或经授权的员工完成；可以吊销；一个外部引用只对应一个消费者 |
| 有效身份 | 每个请求的租户、渠道主体与消费者都由“凭据 → 渠道 → 绑定”推出。请求只给出 `external_consumer_ref`。没有生效绑定时，返回 403 并记审计，不存在“自动建消费者”的路径 |
| 测试人设 | 测试驱动用的渠道和绑定标为测试人设（与 NX-037 的 `simulated_agent` 一致），在系统内带 persona 标记，在分析与客户事实中默认排除（D5 已决定，见 ADR-034） |

## 3. 端点（版本前缀 `/api/channel/v1`）

| 方法与路径 | 作用 | 受治理路径 |
|---|---|---|
| `POST /consumers/{external_ref}/conversations` | 新建会话（幂等） | `Conversation.create`（与 WebChat 相同，以渠道代表消费者的身份执行，见 D2） |
| `POST /conversations/{id}/messages` | 投递一条消费者消息。请求体：`provider_event_id`（必填，渠道侧唯一）、`body`、可选的 `client_sent_at` 与 `reply_to`（NX-051 v2 字段） | 与 `native_web_inbound` 相同：provider namespace 取渠道的 `provider_namespace`；同一事件 ID 重放时 `created: false`，内容不同时返回 409 |
| `GET /conversations/{id}/messages?after=&limit=` | 读消息，包括回复 | 消费者读取路径（与 WebChat 相同的 projection） |
| `GET /conversations/{id}/events` | SSE，`Last-Event-ID` | 同上 |
| `GET /messages/{id}/receipt`、`/run` | 回执与 Run 状态 | 同上 |
| `POST /webhooks`（可选，D4） | 登记回复回调地址：回复写入后推送签名事件 | 走出站效应路径，是外部效应：受三层真实派发开关与联系限制约束 |

- **身份来源**：所有端点都从凭据推出身份。路径中的会话 ID 必须属于该消费者，否则 403。
- **错误格式**：与 WebChat 相同，`code`、`message`、`trace_id`、`retryable`。

## 4. 治理与不可绕过的约束

1. **同一入口**：消息只经 `Message.create`（以及原生事件的那条受治理路径）进入；读取用 `nexloop.conversation.read`，与 WebChat 共用 SQL 侧的授权检查；不新增任何写表的路径。
2. **联系限制**：渠道只能投递“消费者发来的消息”，这不是外发。回复仍由 Run 和回复保证产生，出站时照常检查联系限制（ADR-023）。消费者已拒收时，入站照常受理，但不会出现被禁止的主动外发。
3. **回复保证**：入站消息与 WebChat 消息完全同等，回复保证的时限与兜底不变。
4. **真实派发开关**：通过渠道 API 触发的企业交付等外部效应，仍要经过三层开关；渠道凭据本身不带任何派发权限。
5. **预算**：Run 的费用照常计入租户预算；渠道可以另设一个消息数上限（D3）。
6. **不信任客户端**：时间、顺序、回复关系都只作为客户端证据（NX-051），不作为排序或时间锚点。

## 5. 速率限制与配额

- **两层**：按渠道（每分钟消息数）与按消费者（每分钟消息数）。超限返回 429 加 `Retry-After`，与浏览器登录限流使用同一类计数器（`PostgresBrowserRateLimiter` 的通道版本）。
- **默认值**（D3 已决定）：测试渠道每个消费者每分钟 10 条，每个渠道每分钟 120 条；正式渠道按合作方另定。
- 请求体上限 32 KiB，消息正文上限 8192 字符，与 WebChat 一致。

## 6. 审计与证据

- **每个请求一行审计**：渠道 ID、消费者 ID、操作、结果代码、`provider_event_id`、trace_id 与时间。只记 ID 与代码，不记正文。
- **对象记录**：消息对象上记 provider namespace 与事件 ID（NX-051 已有），所以能区分它是 WebChat 消息还是渠道 API 消息。
- **吊销**：吊销凭据或绑定后，之后的请求立即被拒；已受理的消息不受影响。

## 7. 安全

- **传输**：只走 TLS，经 Gateway 进入。渠道 API 与 WebChat 共用 API 进程，但使用独立的路由与身份解析，不接受浏览器 cookie。
- **令牌**：不放在 URL 里；日志中只出现 digest。轮换时新旧令牌短暂并存（D1）。
- **CORS**：不提供，这个 API 不给浏览器使用。
- **错误信息**：不泄露资源是否存在。别人的会话与不存在的会话一律返回 404（D7 已决定），WebChat 同步改为 404。

## 8. 与第 1 期的关系

- 第 1 期的 CLI 与 MCP 可以加一个 `--channel-token-file` 模式，改走第 2 期的端点。命令与工具名保持不变，Codex 的配置只换参数。
- 第 1 期在第 2 期上线之后继续保留，用于测试浏览器路径本身。

## 9. 测试计划（实现时）

- **真实 PG**：
  - 凭据 → 渠道 → 绑定 → 消息，与 WebChat 结果一致（同一个 Action、同样的授权检查）；
  - 吊销凭据或绑定后立即拒绝；
  - 跨租户、跨消费者的会话访问被拒；
  - 联系限制下入站照常受理、出站被挡；
  - 回复保证时限与 WebChat 一致；
  - 事件 ID 重放与冲突；
  - 两层限流；
  - 审计行只含 ID 与代码。
- **负例**：请求体带 `tenant_id` 或 `consumer_id` 时返回 422 并忽略；浏览器 cookie 不被接受；令牌出现在 URL 中时拒绝。

## 10. 已决定（负责人 2026-10-17，全部按推荐）

| # | 事项 | 结论 | 后续 |
|---|---|---|---|
| D1 | 渠道凭据形式 | **bearer 令牌**：渠道是服务主体，凭据放在私有文件里，可以轮换并存，经受治理的 Action 签发与吊销 | 对外合作方需要时，再评估 mTLS |
| D2 | 以谁的身份执行受治理 Action | **渠道服务主体代表消费者执行**：新增授权事实 `consumer_channel_binding`，消息记录 `on_behalf_of_consumer` | [ADR-033](../../adr/ADR-033-consumer-channel-acts-for-consumer.md)（proposed） |
| D3 | 默认限流与配额 | **测试渠道：每个消费者每分钟 10 条，每个渠道每分钟 120 条**；正式渠道按合作方另定 | — |
| D4 | 首版是否提供回复 webhook | **首版不做 webhook**，只用轮询或 SSE | 将来作为外部效应另立任务 |
| D5 | 测试人设标记 | **在系统内标记 persona**：消费者属性加受治理 Action，渠道绑定时强制检查，下游默认排除 | [ADR-034](../../adr/ADR-034-test-persona-marking.md)（proposed） |
| D6 | 归属阶段 | **v0.1 之后，排入 S6**；v0.1 期间测试继续用第 1 期 | — |
| D7 | 别人的或不存在的会话返回什么 | **统一返回 404**，并与 WebChat 同步 | 实施时同时改 WebChat 的相关测试 |

选项与工作量的对比见 [拍板单](consumer-channel-api-decisions.md)。
