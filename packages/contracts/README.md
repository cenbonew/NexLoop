# NexLoop v1 目标契约

这些 JSON Schema 与合成示例是交接规格，不是已实现的 OpenAPI，也不是上游接口。Codex应生成类型与契约测试，并让 FastAPI、TypeScript RuntimeAdapter 和 EIOS bridge 共用一份语义定义。

| 文件 | 用途 |
|---|---|
| event-envelope.schema.json | 服务端正规化后的事件；不是无鉴权public body |
| action-intent.schema.json | 待核验的业务意图；不是执行许可 |
| ontology-mutation.schema.json | 有证据的实例变更提案；不含DDL/Schema改写 |
| context-manifest.schema.json | 每次实际模型请求的来源、版本与工件清单 |
| run-command.schema.json | 内部RuntimeAdapter启动命令；凭据只传reference |
| evolution-candidate.schema.json | 冻结的候选演进与评估输入；不能自携发布许可 |

## Schema之外必须实现的语义检查

JSON Schema验证不是授权。tenant/actor/world从已认证服务端上下文绑定，并和body一致性核对；不允许调用者任选租户。`mode=real`必须`world_id=real`，其他模式必须独立世界；shadow虽读取经授权的真实快照，派生产物仍写shadow世界。

所有ref必须在本租户/世界下解析并校验Property权限。datetime区间必须有序；not_after必须当前有效；请求deadline取预算和凭据有效期的较小值。digest由服务端按明确的canonical JSON规则重算，不信任客户端传入。参数由已注册Action特定Schema二次验证。

幂等ID只能由稳定业务意图派生，不由每次模型tool_call_id派生。同ID换payload返回409。`expected_versions`缺少必需资源时拒绝；示例不是完整运行permit。

对象创建使用服务端分配/验证的stable ref。已验证事实必须有可核对证据，不能因为`epistemic_kind=verified_fact`而提升可信度。`set_property`不可绕开特定类型/属性Action规则；Schema新增/删除与权限变化不在实例mutation中。

演进design/validation case集合不得重叠，近重复和同一客户的相关案例须按策略分组；候选版本不得覆盖parent；`accepted_for_release`不代表已经发布。Publisher在独立身份下生成许可并调用EIOS，不修改此文件让候选获取权限。

## 示例与检查

`examples/*.valid.json`全部是合成test场景，不包含真实消费者、真实密钥或可用生产URL。占位hash只是格式样例。运行`python tools/validate_handoff.py`仅验证交接包结构；部署前还必须实现语义、权限和真实PG契约测试。
