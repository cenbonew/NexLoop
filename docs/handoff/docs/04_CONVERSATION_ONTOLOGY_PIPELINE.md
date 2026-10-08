# 对话到本体：清洗、提取、存储与动态更新

## 1. 范围

本模块处理消费者和 Agent 的双方对话，也接收相关业务事件。完整链路是：原始事件持久化 → 规范化 → 带证据的提取 → 对象识别 → Claim/冲突 → Mutation Proposal → EIOS 验证应用 → 派生记忆与索引更新 → 后续目标/计划重评估。

原始交互、候选知识、正式事实、派生索引是四种不同状态。不能直接将一段摘要写进消费者对象并宣布完成知识更新。

## 2. 原始事件与消息

校验渠道签名、时间窗口、事件 ID、附件类型和大小。服务端从渠道绑定解析租户；消息时间与入库时间都保存。先提交消息＋outbox，再返回 ACK。发生乱序时分配 received_sequence，另保留 provider sequence，后续重建对话不能改写原始证据。

相同 provider_event_id 的重投按技术重复处理；不同消息表达相同问题必须保留，因为可能表示问题仍存在。HTML/Markdown 清洗去掉执行内容、追踪脚本和危险链接，保留纯文本语义、引用关系及原内容 hash。

附件首版支持受限纯文本/JSON；图片、语音、PDF 先保存受限引用，不声称已理解内容。后续启用解析器须独立沙箱、大小/超时限制和来源标记，避免 SSRF、恶意文件和解析器注入。

## 3. 两条处理路径

**即时路径。** 当前原始消息直接进入本次 Run。对于明确“不要再发促销”等高优先级约束，先形成可追溯保护性 dispatch guard，再由受治理流程确认联系偏好。解释不清时阻止相关主动触达或请求澄清，不默认允许。保护性 guard 不可被低置信后台结果自动清除。

**后台路径。** 对完整对话窗口运行提取、消歧、变更验证、摘要更新和索引。任务版本绑定原始消息 hash、提取器/Schema 版本；结果提交时检查控制状态与对象 revision。旧任务完成不能覆盖已经更新的偏好。

后台提取失败进入 retry/dead_letter 并显示积压；消费者交互不等于必须等待全量知识整理。

## 4. 提取结果分类

| kind | 含义 | 可自动升级为正式状态吗 |
|---|---|---|
| `user_statement` | 消费者陈述某事 | 只成为“该消费者这样说过”，其他事实需核验 |
| `preference` | 联系/使用偏好 | 授权主体明确表达且无冲突时可更新；保留时效 |
| `constraint` | 停止联系、时间限制等 | 优先保护；执行入口仍重验 |
| `intent` | 意向、条件性计划 | 不能等同于已发生交易或确定承诺 |
| `need_problem` | 需求或报告的问题 | 可创建 reported 对象，verification_state 未验证 |
| `commitment` | Agent/企业明确作出的承诺 | 可登记，但不越过目录、权限与预算 |
| `hypothesis` | 模型归纳或推测 | 只能在假设层，不能直接写 verified fact |
| `correction` | 对已记录信息的更正 | 关联被更正 Claim/对象，依证据处理冲突 |
| `verified_fact` | 来自已验证外部来源（签名商业事件、受治理 Action receipt）的事实 | 提取器**不得**产出此类；只有验证通道可写，且必须附可核对证据 |

上表 `kind` 与 `contracts/ontology-mutation.schema.json` 的 `epistemic_kind` 枚举一一对应。

每条候选至少具有 subject、predicate、typed value、speaker、polarity、modality/condition、valid_time、source_span、extractor_version、confidence、resolution_state。confidence 是提取器自评，不是经过校准的客观概率。

## 5. 对象解析

先使用已验证会话消费者、明确对象 ID、已授权关系和业务单据号定位；再用本体名称/别名、关键词和语义相似找候选。强标识、来源权限和一致性检查决定是否可自动关联。查询候选对象之前应用 tenant/world/属性权限过滤。

没有足够证据时生成 unresolved/ambiguous，而不是随意选最高相似度者。消费者合并不使用仅基于 LLM 或向量的自动决策；可以继续保存悬挂 Claim，后续再关联。

## 6. Mutation Proposal 生命周期

> **ADR-019（2026-10-08）修订：** 提案生成前先做四层匹配判定（类型/实例/属性/值）；全匹配与部分匹配自动应用，不匹配生成 `candidate-definition` 并挂起依赖 Claim（`awaiting_definition`），经粘合或人工审核后再应用。详见 `adr/ADR-019-extraction-recall-review.md` §3。

`proposed → validated → applying → applied`；分支包括 `needs_resolution`、`conflict`、`rejected`、`superseded`。`applying` 超时须查询 EIOS receipt，不能简单转回 proposed 再次写入。

提案字段以 `contracts/ontology-mutation.schema.json` 为准：expected revisions、`operations`、`evidence_refs`、`business_intent_ref`、`schema_version`、`source_content_hash`、`risk_class`（low/medium/high）和 `rationale_summary`。允许的操作为 `create_object`、`set_property`、`invalidate_property`（失效属性）、`link_relation`、`end_relation`、`supersede_claim`（更正）；不接受任意 SQL、脚本、权限定义或文件路径。

EIOS 对每个操作执行类型/引用/权限/时效/业务约束检查，同一事务提交可原子表达的变更与 outbox。不能共事务的变更显式返回部分状态和补偿计划，不伪装全成功。

## 7. 事实优先规则

1. 经验证外部付款记录决定付款状态，客户口述保留为陈述。
2. 用户有权更新自己的明确联系偏好；行为推测不能覆盖明确偏好。
3. 同等级证据需要比较有效时间和版本，不能只比较接收时间。
4. Agent 的“我已经处理”不是外部 Action 成功证据。
5. 模拟数据永远不能更新 real 世界的消费者事实。
6. 更正通过 supersedes/corrects 链保留依据，检索默认排除失效版本。

## 8. 对话示例（合成测试）

消费者：“未来两周不要发促销。付款页面一直报错，解决后我再考虑续费。”

Agent：“我会在明天下午前给你处理进展。”

预期：保存限时促销限制；登记 reported Problem；记录有条件且不确定的续费意图；登记企业 Commitment，并确认承诺符合可交付能力。相对时间按消息与租户时区解析；无法确定“下午前”具体时间时保留歧义并按业务规则澄清，不能任意精确到某分钟。

付款状态不改变。承诺不标 fulfilled。后续 plan 加入跟进，不生成两周后的无条件促销发送。缺 Commitment 类型时保存 Claim、阻止无依据正式写入，并提出 Schema 缺口，不丢弃原文。

## 9. 结构演进与实例变化分流

> **ADR-019 修订：** 结构变化在 v0.1 增加一条**人工批准**入口：候选类型/属性/词表值/别名由持有 `ontology.schema.review` 权限的人类审核后，经 EIOS schema 注册链发布；自动发布仍不开放。

日常 Claim/实例更正是实时知识闭环，不走“必须评分提高”的演进门槛。语义访问改进由 Evo 流程处理。真正新增对象类型/属性/关系定义是 EIOS Schema 流程，需要兼容、迁移、工具和索引影响检查。新概念不能自行取得管理权限。

## 10. 验收数据集

至少覆盖否定、条件、时区、跨轮代词、同名消费者、历史订单、矛盾证据、重复投递、重复提取、撤回更正、对话中伪造管理员指令、模拟污染、双角色并发和跨租户检索。每个案例保存输入、预期 Claim、禁止的变更和证据区间。首版以固定合成集作为必过回归；真实脱敏集使用前记录授权。
