# 0056 范围拒绝说明：技术配置与运行边界

0056 已作为独立追加迁移登记到 NexLoop catalog 和 versions.lock.json。仅在指定 disposable PostgreSQL 上验证；没有部署或向既有环境迁移。旧迁移与旧 `nexloop.conversation.service_receipt:1` 不修改。

配置入口沿用真实 `nexloop_eios.trusted_configuration.apply_manifest` / configurator CLI，不新增伪造的业务配置 Action。专用受限 `nexloop_configurator`、显式私有 DSN/签名密钥/服务凭据文件及期望 authority revision 是技术配置前提；公开 manifest 只有引用与 typed authority facts，不含原始密码、token 或密钥。技术配置角色不得直接创建 Consumer/Goal/PlanStep/Control/Message。

必须独立配置以下正式 publication/权限：

1. `nexloop.service.scope_denial.record:1`：published Action，low risk / approval none / atomic，输入严格等于 `scope_denials.record_schema()`。当前窄实现按已有真实 Action claim reserve/finalize 记录技术运营拒绝事实，不赋予 `service.request` SEND 或 external-effect 权限。Definition 与 capability 必须真实 typed 校验、schema/reference 对应本租户正式本体；不可只发布同名字符串。
2. 真正 Source 的当前 SERVICE Actor/Role/Grant/Application resource allowlist 独立包含该 record Action 的 EXECUTE。只有目录 READ 不够。运行时 Run allowlist 仍是原有 EFFECT，不增加 record 权限；后台从 owned activation 中解析真正存储 Source，不接受 caller 自报身份。
3. `nexloop.conversation.scope_denial:1`：独立 published Function，capability `nexloop.conversation.scope_denials.read`，atomic / `has_side_effects=false`。输入、输出必须严格等于 `conversation_scope_denials.query_schemas()`；三个正式 Consumer/Conversation/Message schema references 必须真实匹配。
4. 真实 HUMAN 当前 membership/Actor/Role/Grant/Application resource allowlist 独立授予新 Function EXECUTE，同时保留原 `nexloop.conversation.read:1` 与当前 Consumer ownership。新 Function 自行执行完整权限/ownership/Message ACK/Run 关联校验，不借旧 receipt Function 权限，不要求伪造 SERVICE 或 HUMAN。

上述 Source credential/application 配置须在真实新 Run 签发前完成；配置导致 authority epoch 变化时按真实认证链刷新服务 Session，再签发 Run，Human 重新真实登录。不能复制旧 source digest 或从 SQLite 生成 grant。

运行流程：原 Intent submit 因当前 signed catalog `allowed=false` 整事务回滚后，后台重新计算 original opaque activation/Run/fence/lease/Artifact/catalog 链；Source 新 record EXECUTE + real Governor reservation；同事务当前 catalog 重新导出固定八字段 scope、插入 immutable row、terminal finalize、尾复核。任一权限/TTL失败全回滚。相同 Run+scope 重投稳定；不同 parameters 冲突409。没有 Intent/outbox/额度/Promise/provider send。

Human endpoint `GET /api/v1/messages/{message_id}/scope-denial` 使用实际 Cookie→current BrowserBusinessSession→独立 Function。返回 `message_id` 与 nullable `denial`，不是 receipt。投影要求 record Action terminal/succeeded、principal/request binding 与 outcome_id 匹配；目前不重算 PG 时间字符串对应的完整 terminal outcome_digest，不能据此声称外部效果已履行。

前端现有治理 receipt 始终优先；无履行成功时显示真实范围拒绝解释。新 query 缺权限/不可用时明确报错，不能填造说明。`product_ready`、stage 或真实渠道状态不因此改变。

确定性整链验收仅通过受信私有测试配置 `deterministic_effect_request_scope` 提供 typed test provider 输入，要求 `deterministic-test` + `deterministic_message_from_input=true` + `effect_tools=true`。真实模型 profile 明确拒绝此字段，不能隐式覆盖真实模型请求。
