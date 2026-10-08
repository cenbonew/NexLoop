# API、事件与工具契约

## 1. 契约地位

这里和 `contracts/` 定义的是 **NexLoop v1 目标接口**，不是声称 EIOS/Pi 已存在这些端点。Codex 应根据目标生成 OpenAPI 与 TS/Pydantic 类型，再实现适配。EIOS 原入口保留于集成映射，不暴露未经治理的底层接口。

外部基础路径 `/api/v1`，内部 `/internal/v1` 不经公网/浏览器代理；版本化 JSON 使用 UTF-8。列表 cursor 分页，默认25最大100；时间 RFC3339；金额含币种。错误返回 `code/message/trace_id/retryable/details`，details 不泄露其他租户对象是否存在。

## 2. 身份与通用头

浏览器使用 server session＋CSRF；消费者 session 只限自己；集成 webhook 使用绑定租户的签名和重放窗口；内部 runtime 使用有 audience、有效期和 run 绑定的服务凭据。tenant_id 由服务端派生。管理动作需要显式资源权限，不根据 URI 就推断授权。

客户端写请求必须带 `Idempotency-Key`；更新可变实体带 `If-Match` 或 body expected_version（二者同时存在必须一致）。复用 key 但 payload 不同返回409。幂等范围按 tenant、world、actor class、操作、意图限定；请求响应记录按保留策略清理。

## 3. 外部资源接口

| 资源组 | 接口 | 语义 |
|---|---|---|
| 身份 | `GET /session`；`POST /session/switch-tenant`；`POST /session/logout` | 已认证身份与当前租户；切换重新核验 |
| 配置/角色 | `GET/PATCH /settings`；`GET/POST /roles`；`PATCH /roles/{id}` | 管理员权限与 revision |
| 目标 | `GET/POST /goals`；`GET /goals/{id}`；`POST /goals/{id}/versions`；`POST /goals/{id}/publish` | immutable goal versions |
| 对齐 | `POST /goals/{id}/alignments`；`GET /alignments/{id}` | 202 创建对齐任务 |
| 消费者 | `GET/POST /consumers`；`GET /consumers/{id}`；`GET /consumers/{id}/timeline` | 建立与读取走 EIOS；敏感字段过滤 |
| 偏好/身份 | `POST /consumers/{id}/preference-changes`；`POST /identity-resolutions` | 变更提案/受治理 Action，不直写 ORM |
| 关系/角色 | `GET /consumers/{id}/relationships`；`POST /consumer-role-links` | 同一世界内关联 |
| 对话 | `GET/POST /conversations`；`GET/POST /conversations/{id}/messages` | 消息幂等接受；入库成功后返回202/消息引用 |
| 流 | `GET /conversations/{id}/events` | SSE，Last-Event-ID 可恢复，只输出已提交内容 |
| 事件接入 | `POST /events/ingest`；`POST /webhooks/{connector_id}` | 签名校验、schema、event dedup |
| 计划 | `GET /plans`；`GET /plans/{id}`；`POST /plans/{id}/reassess` | 复评不等于无条件执行 |
| 干预 | `POST /interventions` | 指定 scope/reason/expected_revision；立即更新控制状态 |
| 执行 | `GET /runs`；`GET /runs/{id}`；`GET /actions/{receipt_id}`；`POST /actions/{id}/reconcile` | 状态来自 EIOS；对账权限独立 |
| 问题/承诺 | `GET /problems`；`GET /commitments`；`POST /commitments/{id}/fulfillment-evidence` | 提交证据后核验，不直接“标已完成” |
| 知识 | `GET /claims`；`GET /mutation-proposals`；`POST /mutation-proposals/{id}/resolve` | 人工/自动消歧与依据 |
| 本体 | `GET /ontology/types`；`GET /ontology/objects/{id}`；`GET /ontology/relations` | 通过受控读取 view |
| 演进 | `GET/POST /schema-proposals`；`POST /schema-proposals/{id}/evaluate`；`POST /schema-proposals/{id}/publish` | v0.2 开放发布；v0.1 请求明确 unavailable，不伪造 |
| 效果 | `GET /metrics`；`GET /costs`；`GET /commercial-observations` | 指标定义/币种/成熟窗口/世界可见 |
| 数据权利 | `POST /data-exports`；`POST /deletion-requests`；`GET /deletion-requests/{id}` | 单独权限、异步履行与审计 |
| 工件 | `GET /artifacts/{id}` | 校验 tenant/world/subject，不能静态目录公开 |

## 4. 内部 RuntimeAdapter

`POST /internal/v1/runs/{run_id}/start` 接收冻结版本、角色 ref、消费者 ref、context manifest ref、budget 和短时凭据引用，返回202。相同 run_id 的重试不创建新 Pi submission。

`GET /internal/v1/runs/{id}` 返回内部 task 状态，不泄露密钥。`POST /internal/v1/runs/{id}/cancel` 只中止/取消可取消工作，Action 状态另查。恢复启动必须读取 PG 业务状态后决定 resume，不让过期本地文件自行取得执行许可。

内部工具网关统一 `POST /internal/v1/tool-calls`，工具名来自 allowlist；服务端绑定 Run 身份与目标，禁止调用者提交任意 DSN、URL、workspace、SQL 或 Shell。

## 5. 首版工具集合

只读：`read_consumer_state`、`read_goal`、`read_open_commitments`、`query_ontology`（结构化、受限）、`search_memory`、`browse_semantics`、`resolve_semantics`、`get_action_receipt`。

受治理写：`propose_instance_mutation`、`create_or_update_plan`、`submit_action_intent`、`schedule_reassessment`、`escalate_issue`。工具执行本身不是绕开 EIOS Action 的授权。

业务 Action Pack 至少提供：`nexloop.message.send`、`nexloop.contact.preference.set`、`nexloop.problem.report`、`nexloop.commitment.create`、`nexloop.commitment.record_fulfillment`、`nexloop.service.deliver`、`nexloop.followup.schedule`。它们是拟注册的 NexLoop 能力名，不是当前上游 API。

## 6. 事件协议

事件 envelope 含 schema_version、event_id、tenant_id、world_id、mode、event_type、source、source_event_id、occurred_at、recorded_at、subject_ref、correlation_id、causation_id、payload。public ingress 不直接信任 envelope 的 tenant/role 字段，由认证接入层写入。

基础 topic：conversation.message.received、knowledge.extraction.completed、ontology.mutation.applied、goal.version.published、control.intervention.applied、action.receipt.updated、commitment.due、commercial.fact.verified、memory.index.updated。

事件循环保护：消费者 handler 记录 inbox；派生事件保留 causation；不把“记忆更新”默认广播为所有角色立刻重跑；同一控制 revision/对象变化合并唤醒。

## 7. HTTP 状态与恢复

200/201 表示对应读或同步记录完成；202 表示已持久化接受，不能写成业务已经生效。401 未认证；403 已认证但无权限；404 在调用者可见范围内未找到；409 revision/idempotency 冲突；422 schema/语义无效；429 预算/限流；503 权威依赖不可用。

retryable 错误只说明允许重新查询/提交同一幂等请求，不代表可重新产生外部副作用。对 unknown 动作返回明确状态和 reconcile_after，不以500诱导客户端换 key 重发。

## 8. 兼容策略

v1 只追加可选字段；枚举新增需客户端处理未知值；删除/改类型走版本升级。每次发布生成 OpenAPI、JSON Schema 与示例一致性报告。目标契约变更通过 ADR，不直接修改已发版的含义。
