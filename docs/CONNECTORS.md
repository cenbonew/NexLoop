# 连接器开发指南（简明版）

本文说明 NexLoop **当前仓库里**已有的外部连接点：消息怎么进来、外部效应怎么出去、商业事件怎么接入，以及测试用的回环与合成 provider。只写已有事实；没有实现的标【待确认】或【目标设计】。架构背景见 [ARCHITECTURE.md](ARCHITECTURE.md)。

> **现状**：只有原生 WebChat 一个真实交互渠道；**没有任何第三方消息渠道连接器**；真实外发渠道未开通；三层真实派发开关（部署总闸 `REAL_DISPATCH_ENABLED`、租户、租户与 Action）默认全部关闭（服务授权清单也拒绝授予真实外发 Action）。开通真实渠道属于 NX-037，需要负责人确认。

## 1. 共同规则（所有连接器都必须遵守）

- **持久化后 ACK**：入站事件先在 PostgreSQL 持久化（含去重键）再确认；同一个 provider 事件重投只算一次。
- **只有受治理 Action 产生外部效应**：连接器不直接改业务表，不持有数据库管理凭据；外发只能由 Effect Worker 在派发准入之后调用。
- **结果不明先查询**：传输不确定从不授权重试，对账用同一个 intent 查询 provider（不重发）。
- **端点与凭据来自可信配置**：Run、工具参数和浏览器都不能指定端点或凭据；凭据只经 0600 私有文件传入。
- **只用合成数据测试**，测试不访问外部网络。

## 2. 入站消息

| 入口 | 现状 | 代码 |
|---|---|---|
| 原生 WebChat（provider 命名空间 `native.webchat`） | 已实现：浏览器登录后发送消息，带幂等键与 provider 事件 ID；v2 另存客户端序号、发送时间与引用回复（NX-051）。同一事务内做拒绝联系检查、登记待提取与待回复 | `native_web_inbound.py`、`web_chat_http.py`、`conversation_messages.py` |
| 第三方渠道（短信、IM、邮件等） | 【目标设计】未实现。员工回复目前只支持原生 WebChat（`NXC07`）；渠道签名顺序只用合成渠道验证过 | 见 [NX-051 实现说明](implementation/NX-051-implementation.md)、[NX-028 实现说明](implementation/NX-028-implementation.md) |

新增入站渠道时，应复用 `ConversationMessagePort` 的受理路径（同一事务内的持久化、去重、拒绝检查与 feed 登记），而不是另写入库逻辑【待确认：具体接口以实现时的设计稿为准】。

## 3. 外发效应（渠道 provider 接口）

Effect Worker（`nexloop-effect-worker`）通过 `HttpEffectProvider`（`effect_provider.py`，协议标识 `nexloop-service-effect-http-v1`）调用一个固定来源的服务端点：

| 操作 | 请求 | 说明 |
|---|---|---|
| 派发 | `POST /v1/effects`，头 `Idempotency-Key: <intent_id>`，可选 `Authorization: Bearer <凭据>` | provider 必须按 intent ID 幂等 |
| 查询 | `GET /v1/effects/<intent_id>` | 用于 unknown 结果的对账 |

- 配置 `EffectProviderConfiguration`：`origin`（必须 https、只有主机和端口）、`credential_file`、`ca_file`、超时（默认 2 s，≤30 s）；不使用代理、不跟随重定向、有响应期限。只有显式的 `test_loopback_http` 允许 `http://127.0.0.1:<端口>`，且不能带凭据。
- 每个 Action 必须在效应类别声明（`deploy/configuration/action-effect-categories.v*.json`）中声明是否触达客户；未声明的一律按触达客户处理，受联系限制约束（ADR-023 §3）。
- 同一份声明还有 `external_effect`：由 NexLoop 之外的系统执行的效应（第三方渠道、企业外部服务端点）属于外部效应，只有三层真实派发开关都打开才派发，否则以 `real_dispatch_closed` 拒绝；未声明、定义已变化或取值非法时一律按外部处理。新连接器的 Action 默认就是外部效应（[真实派发开关实现说明](implementation/real-dispatch-switch-implementation.md)）。
- 外发消息在渠道确认接收后才由 `nexloop-outbound-recorder` 写入会话（NX-047）。

- **参考企业交付端点**：NX-037 试运行中没有企业自有端点时，可用仓库提供的替身（外部系统，不属于 NexLoop）：[部署手册](../deploy/stage/reference-endpoint/README.md)。企业可以用 `scripts/nx037/endpoint_conformance.py` 检查自己的端点是否符合本节协议。

## 4. 商业事件入口（签名 webhook）

- `POST /api/v1/webhooks/commercial/{connector_id}`，`Content-Type: application/json`；头 `X-NexLoop-Timestamp` 与 `X-NexLoop-Signature`，HMAC 覆盖 `<timestamp>.<原始 body>`，支持当前与上一把密钥（`commercial_http.py`、`authz.nexloop_commercial_ingest`）。
- 重放窗口、载荷上限、事件类型映射、币种在 [`commercial.v1.json`](../deploy/configuration/commercial.v1.json)；重复推送返回 200 只算一次，冲突返回 409 并记异常，每个连接器有令牌桶限流（429）。
- 连接器由 configurator 配置（`nexloop_eios.commercial.configure_connector`，密钥从私有文件读取）；`test` 连接器只能写非 `real` 世界。
- 详见 [NX-027 实现说明](implementation/NX-027-implementation.md)。

## 5. 测试用的回环与合成 provider

| provider | 用途 | 位置 |
|---|---|---|
| 本地 JSON 交付 | 本机私有文件系统上的真实 JSON 导出交付，带幂等日志；用于端到端测试外发与对账 | `local_json_delivery.py` |
| 回环 HTTP effect provider | `test_loopback_http=True`，只允许 127.0.0.1 | `effect_provider.py` |
| 优惠回环账本 `LocalIncentiveLedger` | AT-043 唯一的发放渠道；fsync 的 JSON 记录，可按 intent 查询，可注入失败与超时 | `incentives.py`、`nexloop-incentive-issuer --ledger-directory` |
| 确定性模型（Agent Host `deterministic-test` 档案，`faux-1`） | 不调用真实模型，代价为 0；有若干固定协议（如复评结果 `deterministic_plan_outcome`） | `apps/agent-host/src/runtime-host.ts` |
| load-rules 提取 provider | 只在 `--mode test` 下的压测用规则式提取；stage 拒绝启动 | `load_rules.py` |

确定性和回环 provider 的结果**不能**作为真实渠道或真实模型的验收证据。

## 6. 尚未具备

- 第三方渠道连接器、渠道签名顺序的真实证据、撤回未投递消息（ADR-021 不做）。
- 真实优惠发放渠道、真实外发渠道（NX-037，需负责人确认）。
- 连接器开发的脚手架或 SDK【待确认：是否需要】。
