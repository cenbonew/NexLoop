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
| candidate-definition.schema.json | ADR-019：未匹配到 Schema/实例的候选定义（类型/属性/词表值/别名/实例），只能暂存、粘合或进人工审核 |
| review-decision.schema.json | ADR-019：人类审核决定（approve/merge_into/reject），reviewer 由服务端会话解析，审核是受治理人类 Action |
| claim.schema.json | NX-019：从已持久化消息提取的带原文证据 Claim（候选知识，非正式对象）；提取器永不产出 verified_fact，隐性推断只能是 hypothesis |
| context-pack-v6.schema.json | NX-023：Context pack v6。消息 Run 携带冻结的 v2 核心（bindings、user_statement、formal_facts、current_constraints、supply），带可选 relationship_context 段时为 v4 核心（当前陈述只收已解决的用户陈述，hypothesis 只作证据），绑定时由 SQL 重新推导；Role Run 携带冻结的 v3/v5 核心（role 段、服务触发作为 current_event），加策略、目标与控制快照、正式状态、未确认执行、带标签证据、语义、预算报告与显式 insufficient；每个条目带来源 revision、规范内容 hash 与读取时的当前 READ 决策，SQL 逐条对源行复核；正式区不收 Claim，真实世界不收 hypothesis；不含浮点数；v2–v5 冻结，旧 Run 继续用原协议 |
| workbench-world-model.schema.json | 企业世界模型 W1：工作台只读视图 `GET /api/v1/workbench/world-model`，即租户已发布模型的元数据（对象类型及属性、关系类型、事件类型、Action、Function、实例计数）。不含实例内容；负责人限制组的属性只给名字和 withheld；没有写入对应物 |
| consumer-port-event.schema.json | NX-041 S7：消费者端口事件。孪生（NX-042）在模拟世界中产生入站事件的唯一入口；孪生只给合成 Consumer、事件种类、模拟内容和自身的版本、种子、步数，世界时间、事件 ID、序号由服务端给出；只接受已登记的 sim/shadow 世界，全部为 simulation 数据，不是事实，也不触发任何 Action |
| context-pack-sim.schema.json | NX-041 S7b：模拟世界中 Run 的 Context。v6 只适用于 real（bindings.world_id 固定为 real），模拟世界用自己的包、自己的装配入口；来源只有合成 Consumer 的生成画像与人设（S3）和孪生经消费者端口写入的事件（S7）。条目沿用 v6 的条目形状，每条都标 source simulated，不是事实；不读取任何 real 的会话、Claim、对象或记忆 |
| consumer-twin.schema.json | NX-042 T1：消费者孪生的一个版本——明确写出的假设（只是假设，不是事实）、决策层参数（每个行为都是区间，不给单个概率，D4）与生成器版本；不含任何真实内容（D5），长期保留、不随模拟世界删除；负责人经受治理 Action 发布（D7），版本号、摘要、校准（报告引用与状态）和状态（published / retired，只追加）由服务端冻结；没有通过校准报告的版本为“未校准”，NX-043 不能选用 |
| twin-sample.schema.json | NX-042 T2：模拟世界中种子化群体抽样的一个孪生实例——每个行为给出区间与名义覆盖率（不给单一概率，D4）和在区间内抽出的一次模拟结果（抽到的概率不输出）；同一种子与孪生版本逐字节一致；一律为模拟输出（origin twin、data_mode simulation），带孪生版本与校准状态，未校准的孪生可以运行但输出标明“未校准”，NX-043 不能选用（D7）；永远不是事实、用户陈述或 Claim |

## Schema之外必须实现的语义检查

JSON Schema验证不是授权。tenant/actor/world从已认证服务端上下文绑定，并和body一致性核对；不允许调用者任选租户。`mode=real`必须`world_id=real`，其他模式必须独立世界；shadow虽读取经授权的真实快照，派生产物仍写shadow世界。

所有ref必须在本租户/世界下解析并校验Property权限。datetime区间必须有序；not_after必须当前有效；请求deadline取预算和凭据有效期的较小值。digest由服务端按明确的canonical JSON规则重算，不信任客户端传入。参数由已注册Action特定Schema二次验证。

幂等ID只能由稳定业务意图派生，不由每次模型tool_call_id派生。同ID换payload返回409。`expected_versions`缺少必需资源时拒绝；示例不是完整运行permit。

对象创建使用服务端分配/验证的stable ref。已验证事实必须有可核对证据，不能因为`epistemic_kind=verified_fact`而提升可信度。`set_property`不可绕开特定类型/属性Action规则；Schema新增/删除与权限变化不在实例mutation中。

演进design/validation case集合不得重叠，近重复和同一客户的相关案例须按策略分组；候选版本不得覆盖parent；`accepted_for_release`不代表已经发布。Publisher在独立身份下生成许可并调用EIOS，不修改此文件让候选获取权限。

## 示例与检查

`examples/*.valid.json`全部是合成test场景，不包含真实消费者、真实密钥或可用生产URL。占位hash只是格式样例。运行`python tools/validate_handoff.py`仅验证交接包结构；部署前还必须实现语义、权限和真实PG契约测试。

## ADR-019 附加语义检查

`candidate-definition` 永远不能被直接应用；`status=published` 只能由 `review-decision.approve` 经 EIOS schema 注册链产生。`review-decision.reviewer_ref` 必须是已认证的人类主体且持有 `ontology.schema.review` 权限，服务端重算并忽略客户端传入值。`merge_into` 的 `merge_target_ref` 必须与候选 `kind` 同类且同租户。`reject` 不得携带 `publication`。模拟/shadow 世界的候选不进入 real 审核队列。
