# ADR-033｜消费者通道：渠道服务主体代表已绑定的消费者执行

状态：**proposed**（提议，待批准）。来源：负责人 2026-10-17 对消费者通道 API 第 2 期的拍板，D1–D7 全部按推荐，其中 D2 =“渠道服务主体代表消费者执行”。按 D6，实施放在 v0.1 之后，排入 S6。编号为临时编号，合入时由调度员确认。

关联：[消费者通道 API 设计稿](../docs/design/consumer-channel-api.md)；[拍板单](../docs/design/consumer-channel-api-decisions.md)；ADR-023（显式拒绝与联系限制）；NX-051（原生通道入站的 provider 事件 ID）；ADR-034（测试人设标记）。

## 1. 背景

现在消费者消息只有一个入口：WebChat 的浏览器会话。人类主体登录，取得消费者归属（ConsumerOwnership），然后执行 `Conversation.create`、`Message.create` 与 `nexloop.conversation.read`；授权事实来自浏览器认证绑定（`BrowserAuthenticationBinding`，要求 HUMAN 主体）。

第 2 期要让非浏览器的调用方（测试驱动、渠道适配器）为**已绑定的消费者**投递消息。这类调用方没有浏览器会话，也不是消费者本人。

**不复用 EIOS 的委托凭据**（`DelegatedAuthenticationBinding`，`credential_kind: delegation`）。原因是：它的语义是“Agent 代表某个人类会话长期执行”，必须有 `source_session_id` 以及 agent、session 的 epoch；而渠道代表的是**一个消费者**，背后并不存在人类会话。硬套这个结构，就得伪造一个源会话，这违反“不伪造人类身份”的硬约束。

## 2. 决定

1. **渠道是服务主体**：每个渠道在租户下登记为一个服务主体，使用 NexLoop 现有的服务凭据（`api_key` 类认证绑定，D1：bearer 令牌）。渠道登记、签发凭据、吊销，都只能经受治理的配置 Action 完成，执行者是负责人。
2. **新增“消费者通道绑定”**：`(channel_principal_id, external_consumer_ref) → consumer_id`，由受治理 Action `nexloop.consumer_channel.bind` 与 `nexloop.consumer_channel.unbind` 建立和吊销。执行者是负责人，或持有该授权的员工。绑定带修订号、状态与有效期；一个外部引用只对应一个消费者，一个消费者在同一渠道下也只有一个外部引用。
3. **代表关系作为授权事实**：新增一种 NexLoop 授权事实 `consumer_channel_binding`，内容为渠道主体、消费者 ID、绑定修订、状态、有效期与 persona（见 ADR-034）。消息 Action 的授权检查在“人类归属”之外，再接受“当前有效的渠道绑定”：
   - 渠道服务主体执行 `Conversation.create`、`Message.create`、`nexloop.conversation.read` 时，SQL 侧要求该主体对目标消费者持有一条有效的绑定事实；
   - 每次授权都按当前事实检查，与现有的复查规则一致；绑定被吊销或过期，立即拒绝；
   - 渠道主体**只能**执行这三个 Action，以及原生入站路径；不得执行任何员工、工作台或效应 Action。服务授权清单中对渠道主体的授予是这三个加原生入站，并且只在绑定有效时生效。
4. **记录与审计**：
   - 消息对象的 `actor` 记**渠道服务主体**；同时记 `on_behalf_of_consumer`，即消费者 ID；provider namespace 记渠道的 namespace（NX-051 已有）；
   - 请求审计记渠道、消费者、操作、结果代码、`provider_event_id` 与 trace_id，不记正文。
   - 下游（Run、回复保证、联系限制）一律按**消费者**处理：消息归属以 `on_behalf_of_consumer` 为准，不以 actor 为准。
5. **身份只由服务端推出**：请求只能给出 `external_consumer_ref`。租户、消费者与渠道主体，都从凭据和绑定事实推出。请求体里出现 `tenant_id`、`consumer_id` 时，返回 422。
6. **不改的东西**：
   - WebChat 的浏览器路径与它的授权检查不变；
   - 联系限制与回复保证的语义不变：渠道投递的是入站消息，回复仍由 Run 产生，出站时照常检查联系限制；
   - 三层真实派发开关不变：渠道主体本身没有任何派发权限。

## 3. 后果

- **优点**：
  - 只有一个执行主体，审计清楚；
  - 吊销渠道凭据，该渠道的所有请求立即失效；吊销某个绑定，只影响这一个消费者；
  - 不需要为每个消费者管理短期令牌（这是 D2 的选项 B）。
- **契约改动**：
  - 授权事实增加一种 `consumer_channel_binding`；
  - 消息对象增加 `on_behalf_of_consumer` 字段，并按“新版本的对象类型”发布，旧对象不变；
  - 消息 Action 的授权函数增加“渠道绑定”分支。
  - 以上都按“新迁移 + 新契约版本”实施，不改已发布的迁移，校验和保持不变。
- **代价**：
  - 消息授权检查多一个分支；为此要补“与 WebChat 结果一致”的等价测试；
  - 读取路径（projection）要能按 `on_behalf_of_consumer` 归属消息。
- **风险**：渠道凭据一旦泄露，攻击者就能以所有已绑定的消费者发消息。缓解措施有三：限流（D3）、凭据只放私有文件且可以轮换并存（D1）、绑定按需建立并带有效期。

## 4. 不做

- 不让渠道创建消费者，也不提供“无绑定时自动建消费者”的路径。
- 不让渠道冒充员工回复，也不让渠道读其他消费者的会话：访问别人的或不存在的会话，一律返回 404（D7）。
- 不提供 webhook（D4）。回复推送属于外部效应，另立任务。

## 5. 测试（实施时）

- **真实 PG**：
  - 凭据 → 绑定 → `Message.create`，与 WebChat 走同一个 Action，消息、外发队列与 Run 的结果相同；
  - 吊销凭据或绑定后立即拒绝；
  - 跨租户、跨消费者访问返回 404；
  - 请求体带 `tenant_id` 或 `consumer_id` 时返回 422；
  - 联系限制下入站照常受理、出站被挡；回复保证时限与 WebChat 一致；
  - 渠道主体执行任何其他 Action 都被拒；
  - 审计行只含 ID 与代码。
- **等价测试**：同一条消息分别经 WebChat 与渠道路径进入，产生的 Message、outbox、Run 输入，除 actor、`on_behalf_of_consumer`、provider namespace 之外逐字段相同。

## 6. 回滚

撤销渠道服务授权、吊销全部绑定，渠道路径就完全不可用，WebChat 不受影响。新增的事实种类与字段保留（只追加），不做 down 迁移。
