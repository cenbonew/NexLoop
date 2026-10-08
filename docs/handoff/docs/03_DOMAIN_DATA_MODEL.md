# 领域与数据模型

## 1. 定义与所有权

本文件定义**目标逻辑契约**，不是对 EIOS 现存物理表的猜测。EIOS 实际表名、索引、GUC 与迁移目录由 S0 源码核验后映射；不得直接执行本文中的概念名作为迁移。

默认一个独立 NexLoop PostgreSQL 集群/实例内一个 `nexloop_stage` 数据库。EIOS 内核 schema 与新增 `nl` schema 分别管理；不能修改原 EIOS 生产库。CI 使用一次性独立数据库，绝不复用 stage DSN。

| 实体/信息 | 唯一写入权威 | 其他模块的使用方式 |
|---|---|---|
| Tenant、Identity、Membership、Policy、Grant | EIOS 通用身份/授权内核 | NexLoop 保存设置和职责引用，不另造认证权威 |
| Consumer、Problem、Commitment、Offering、Entitlement、业务 Relation | EIOS 本体及 Action | 工作台/分析为只读投影；必要时增加类型而非另造可写 customer 表 |
| 已验证订单/付款/退款 | 外部商业系统证据→EIOS 规范业务状态 | `nl.commercial_observations` 保存原始观察和核对状态，不独立宣布付款 |
| Goal、KR、Strategy、Plan | NexLoop goals/planning 模块 | 本体通过映射/只读对象视图暴露，不双写独立状态 |
| Conversation、Message、Claim、Extraction | NexLoop conversation/knowledge 模块 | EIOS 对象通过 evidence_ref 指回它们 |
| ActionIntent、Attempt、Receipt、Permit、Reconciliation | EIOS Action 内核 | NexLoop 只保留受控引用和可重建投影 |
| Run 编排状态、任务与调度 | NexLoop operation 模块 | Pi 存储只负责内部任务与会话，不做经营状态权威 |
| 语义概念/映射、候选与评估 | NexLoop semantic 模块 | 经 EIOS 导出或映射；正式 Schema 不由 JSON 工作区代管 |
| Search chunk/vector、summary、metric projection | 可重建派生层 | 随版本/删除失效，不能独立成为事实 |

## 2. 通用字段和约束

跨租户实体均含 `tenant_id UUID NOT NULL`。可进入模拟的对象和工件另外含 `world_id` 与 `mode`；real 数据固定 `world_id='real'`。唯一约束、外键和幂等范围包含租户/世界，不只按 UUID 做跨域关联。对象 ID 使用 UUID，时间存 UTC `timestamptz`，界面依据租户 IANA timezone 显示；消息时间歧义单独记录。

区分 `valid_from/valid_to`（现实有效范围）、`recorded_at/superseded_at`（系统何时知道）、`source_id/source_revision`（证据来源）和 `revision`（并发版本）。不是每张表都做完整双时间表，但 Claim、联系偏好、关键关系和变更记录必须支持这两个时间维度。

金额使用 minor units 整数＋ISO currency；模型成本可用 `numeric` 存高精度，禁止二进制浮点累计财务数值。JSONB 承载动态 payload，不替代 tenant、status、时间、版本、外键和检索热点的类型化列。

重要状态变化保存事件，不原位删除追溯。涉及个人信息的删除可按策略清除内容并保留不含原文的审计 tombstone；append-only 不意味着永久保留所有原文。

## 3. 新增 nl 逻辑表目录

下列是建议的物理模块归属。Codex 可通过 ADR 合并表，但不得丢失状态、证据和唯一约束。

| 表组 | 表/关键字段 | 关键约束 |
|---|---|---|
| 配置 | `tenant_settings(tenant_id, timezone, locale, retention_policy, revision)` | tenant PK，timezone 必须 IANA |
| 角色 | `role_definitions(id, tenant_id, name, responsibility, ceiling_ref, revision)`；`consumer_role_links(consumer_ref, role_id, scope)` | tenant+consumer+role 唯一，消费者 ref 指向 EIOS |
| 目标 | `goals(id, tenant_id, owner_subject, current_version)`；`goal_versions(goal_id, version, objective, period, constraints, status)` | goal+version 唯一，发布版本不可覆盖 |
| KR | `kr_definitions(id, goal_version, metric_ref, target, direction, window)`；`kr_observations(kr_id, value, observed_at, evidence_refs)` | 指标定义受注册，观察不改定义 |
| 对齐 | `agent_goal_versions(id, parent_goal_version, role_id, definition)`；`alignment_reviews(id, run_id, trigger, decision, evidence_refs)` | 父版本绑定，改变父目标需权限 |
| 计划 | `strategy_versions(id, owner, version, definition)`；`plans(id, consumer_ref, goal_version, current_version)`；`plan_versions`；`plan_steps` | active/superseded 明确，step 带前置/停止/复评条件 |
| 对话 | `conversations(id, consumer_ref, channel, last_sequence)`；`messages(id, conversation_id, sequence, provider_event_id, actor, body_ref, occurred_at, received_at)` | tenant+channel+provider_event_id 唯一；会话 sequence 唯一 |
| 证据 | `evidence_items(id, kind, content_hash, artifact_ref, access_labels)`；`evidence_spans(id, evidence_id, start_offset, end_offset)` | span 对应清洗/原文版本，不能跨内容 hash 引用 |
| 抽取 | `extraction_runs(id, source_hash, extractor_version, schema_version, status)`；`claims(id, kind, subject_candidate, predicate, value, validity, confidence, source_spans)` | 输入 hash+抽取器+Schema 版本唯一；相同 Claim 可关联多证据 |
| 消歧与变更 | `entity_candidates(id, claim_id, candidate_ref, basis, decision)`；`mutation_proposals(id, expected_versions, operations, evidence_refs, status, applied_receipt_ref)`；`fact_conflicts` | proposal stable ID；没有依据不自动合并身份 |
| 记忆 | `memory_items(id, scope, kind, current_version)`；`memory_versions(id, text_ref, evidence_refs, validity, access_labels)` | 当前版本指针可切换，旧版本不可用时检索禁用 |
| 检索 | `search_chunks(id, source_ref, source_revision, tokenizer_version, tsv, text)`；`embedding_records(chunk_id, model_ref, dimension, embedding_revision, embedding)` | 模型/维度不能混用；删除/失效 cascade 到派生数据 |
| 语义 | `semantic_snapshots(id, eios_schema_revision, content_digest, status)`；`semantic_records(snapshot_id, family, payload)` | active 语义版本指针唯一、只允许受治理发布 |
| 演进 | `schema_proposals(id, base_revision, hypothesis, patch, risk, status)`；`evolution_runs`；`candidate_evaluations` | 基线、数据集合、评价策略冻结；候选不可拥有发布密钥 |
| 事件 | `incoming_events(id, source, source_event_id, payload_ref, occurred_at, received_at)`；`outbox(id, aggregate_ref, topic, payload, publish_state)` | 事件幂等；业务写和 outbox 同事务 |
| 调度 | `tasks(id, kind, tenant_id, world_id, priority, due_at, state, lease_owner, lease_until, fence, attempt)` | 领取短事务；过期 fence 不能提交结果 |
| 运行 | `runs(id, triggering_event, role_ref, consumer_ref, goal_version, state, runtime_store_ref, policy_revision)`；`run_events` | request_id/run_id 幂等；runtime_store_ref 服务端生成 |
| 决策/上下文 | `context_manifests(id, run_id, call_sequence, strategies, source_versions, artifact_ref)`；`decision_records(id, run_id, intent_refs, rationale_summary, evidence_refs)` | 每次实际模型请求都有关联快照，不只 Run 第一轮 |
| 执行视图 | `action_receipt_projection(receipt_ref, run_id, current_state, source_revision)` | 只读重建视图，真实结果在 EIOS |
| 临时保护 | `consumer_dispatch_guards(consumer_ref, reason, source_event_id, revision, expires_at)` | 仅用于待确认的保守阻断；不是第二套联系偏好权威 |
| 商业/效果 | `commercial_observations`；`metric_definitions`；`metric_observations`；`cost_entries` | real/test 分离；金额币种和来源必填 |
| 运维 | `audit_links`；`retention_jobs`；`deletion_requests`；`release_records` | 内容最小化、导出权限、回收可追踪 |

## 4. 最小业务对象类型

**Consumer**：external_id_refs、display_name、locale、timezone、contact_preferences、lifecycle_status。敏感字段使用 Property 级读取授权。

**Problem**：reported_by、description_ref、reported_at、verification_state、status、related_offering、resolution_evidence。消费者报告不代表技术故障已验证。

**Commitment**：made_to、made_by、content_ref、promised_at、due_at、condition、status、fulfillment_evidence、related_goal_ref。状态 `open/conditional/in_progress/fulfilled/breached/cancelled`；完成需要证据，修改期限保留旧承诺。

**Offering/Service**：description、eligibility、delivery_action_ref、availability、cost_model、promisable_scope。目录定义由受权管理维护。

**Entitlement**：consumer_ref、offering_ref、effective_from/to、status、source_ref。

**CommercialRecord**：kind、external_id、consumer_ref、amount_minor、currency、verification_state、occurred_at、source_ref。订单和付款可拆类型，首版不得把一条用户陈述伪装成已核验记录。

关键关系：Consumer–REPORTS→Problem；Consumer–HOLDS→Entitlement；Commitment–MADE_TO→Consumer；Commitment–ADDRESSES→Problem；Entitlement–FOR→Offering。Relation Type 定义端点类型、基数和有效时间规则。`ADDRESSES` 不代表已解决，解决状态以证据表达。

## 5. 并发和一致性

更新对象时带 expected_revision；若当前状态变化，返回 `version_conflict`，重新查询并评估。不要用最后写入胜出覆盖所有业务语义。跨资源 Action 使用稳定排序的资源键获取短事务锁，避免两个运营角色同时抢预算或发重复消息。

Outbox relay 至少一次投递，消费端有 inbox/dedup。提交后通知丢失可以由轮询补偿；不在数据库事务中等待 LLM 或外部 HTTP。

新增 nl 表的租户 RLS 与 EIOS 原有 RLS 分开核验。不得直接改动 EIOS 的 GUC 名称或 SECURITY DEFINER search_path。应用/Worker 不能使用表 owner、superuser 或 BYPASSRLS 角色。PG owner 通常能绕过 RLS，需要选择受限角色和必要的 FORCE ROW LEVEL SECURITY，并用实际角色测试。[S11]

## 6. 检索与索引

首先使用结构化过滤限定 tenant/world/consumer/访问标签，再执行关键词和向量候选召回，合并排序后做来源版本校验。采用 EIOS 已有的加权 RRF 概念时必须核对实际可复用接口。

关键词：英文/标识符采用 FTS `simple`；中文增加版本化 CJK 分词/二元字串预处理，不能假定原生英文 token 方案能处理所有中文。`pg_trgm` 用于概念名称/别名模糊匹配，不用于无界全文扫描。

向量：EmbeddingProvider 由部署配置选择，S0 固定真实 model_id 和 dimension。存储按 embedding profile 隔离，新模型建立新索引再切换，不原位混合向量。初版从租户和消费者过滤后的精确相似度检索开始，按数据量和延迟再启用 HNSW；ANN 下仍需测试过滤后召回。[S10]

索引至少覆盖 `(tenant_id, world_id, consumer_ref)`、`(tenant_id, state, due_at)`、未发布 outbox 部分索引、source_event 唯一约束、会话 sequence、对象版本以及 Claim source_hash。避免为每个 JSON 字段一律建 GIN。

## 7. 生命周期与恢复

持久消息按租户策略保留；默认试运行可设置原文 90 天、上下文快照 30 天、普通日志 14 天、指标聚合 1 年，全部是产品默认而非法律期限。执行、合同或特殊保留要求由企业另配；公开样例仅用合成数据。

删除消费者需枚举所有依赖：证据、Claim、正式对象、关系、摘要、向量、Artifact、可定位的 Pi Run 文件和备份保留。先封禁检索与新触达，再异步擦除/脱敏，最后记录不含原文的完成凭据。备份按保留期退出，恢复后先重放删除清单再允许对外服务。
