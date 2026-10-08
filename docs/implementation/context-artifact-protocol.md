# ContextArtifact 最薄协议

当前实现提供真实 Artifact 和 Run 输入绑定；不是 S3 ContextEngine，也没有将未实现的 policy/semantic/embedding 快照字段伪填为完整 ContextManifest。

`AuthenticatedServices.prepare_message_context(message_id, run_token, command)` 只接受真实 Backend Source 服务。原 Run token 仅用于内存认证。当前 `nexloop.context.bind:1` published Action、完整 Source/Run EIOS authority 和真实 governed MessageAssignment/Goal/PlanStep/EffectControl 必须成立；Source 还需显式 Artifact CREATE、READ 权限。Action claim reserve、binding、terminal finalize 和最终权限重核在同一 PG transaction；物理 Artifact 创建先发生，登记失败留下可由既有 retention/GC 处理的 orphan，不产生队列 ACK。

pack 的 `schema_version` 是 `nexloop.context-pack.v1`。`user_statement` 保留当前真实 Message 原文及出处，`formal_facts` 仅四个实际 formal object 的 ID/revision/provenance，`current_constraints` 记录 assembly 时真正控制对象的预算、executor 和到期时间。重投返回相同 pack；实际 model/tool dispatch 重新检查当前控制对象与权限，不将 assembly-time reserved_units 当实时余额。所有 JSON integers 限于 JavaScript safe integer。

`bindings` 包含当前真实 tenant/world/Run/source/context、namespace、artifact_id 和 command_digest。namespace 是 SHA256(UTF8(tenant) || byte00 || UTF8(world))；artifact_id 由实际 existing Artifact port 的 stable request `context:<Run UUID>` 派生。command_digest 是 context_pack.py 中十四个显式 command 字段的递归 canonical JSON SHA256；排除 context_manifest_ref 避免自引用，credential_ref 在 SQL 独立强制等于真实公开 `run:<Run UUID>`。

最终 command.context_manifest_ref 是 `artifact:<32hex>`；queue input 是 canonical pack 的完整 UTF8 JSON。0052 v2 bridge 保留旧 signed protocol 的真实 Source/Run/Plan、事件、权限、租约/fence 检查，明确改用已登记 pack 摘要校验，不把旧原文 input check 当 pack 校验。ACK 仅匹配实际 committed queue/enrollment/input；同消息重投只有同 Run、command binding、Artifact 和字节才可恢复。

实际 RuntimeActivation authorize 由 0052 wrapper 覆盖；旧 inner 的所有 application EXECUTE 被撤销。owned resolve 仅返回后台私有 Source digest hint，不能发执行许可或持有 Plan/Artifact 写锁。实际 authorize 在原 Run execution advisory 后统一 ledger/context → binding/Artifact 锁序，真正认证 stored Source 并验证其 current Artifact READ 全链，最后复核原完整权限、租约/fence/TTL。失败同事务回滚 execution marker 或 Intent/额度写入。非 0049 Message Run 保留旧通用运行路径；所有 0049 Message Run 必须绑定 ContextArtifact，无 test bypass。

公开 HTTPS guard 只追加 `context_artifact: {artifact_ref, sha256, command_binding_digest}`。sha256 是完整持久输入字节摘要，不放进 pack 内形成循环。Host 必须显式启用严格 typed pack 模式并核对当前 guard attestation；Host 的显式 context_input_protocol 配置负责该接线，不根据“看起来像 JSON”猜协议。

assembly 调用真实 put/read 验证私有磁盘字节与 hash；后续 PG queue 中的 durable input 是持久复制，不是 Memory fallback。每次模型/工具守卫检查当前 PG Artifact metadata 与 current READ authority，没有在每次调用重新读取物理 blob。磁盘损坏 read 拒绝有独立证据，但不能声称已覆盖运行中物理文件修复/即时停模型。

合入主仓后 联合 focused 79 passed（39 pure、40 actual PG，含旧消息链兼容）；包括真实 typed publication/initial Human/password session/governed setup、PG ACK、current grant revoke、metadata/TTL 拒绝、真实 foreign Worker、HTTPS attestation、三独立进程并发和实际末次 TTL 回滚。此前 TTL 用例存在权限拒绝假阳性，已撤销旧结论并修正实际等待和 specific SQL tail observation；详见 evidence JSON。主仓 Runtime 回归 61 passed；尚未执行全量新头 CI、真实模型或外部渠道调用。
