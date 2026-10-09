# NX-019 对话提取：话题切分与带原文证据的 Claim

范围：ADR-019 §3.1（第一步）。产出是**候选知识**（Claim），不是正式业务对象；对象解析、四层匹配、候选定义与审核属于 NX-020/021/045/046。NX-019 正式依赖 NX-018，本线为先行开发，完成判定由调度员负责。

## 1. 处理链

```
已持久化 Message（0046 governed Message 对象 + runtime.nexloop_conversation_messages）
  └─ ConversationClaimExtractor.load_window：服务凭据逐对象/逐字段 EIOS READ（AuthorizedObjectReader）
       └─ input_digest(tenant, world, conversation, [message_id, sequence, content_hash, speaker], timezone,
                       extractor/prompt 版本, provider/model) → 已有同版本 run 则直接重放，不再调模型
       └─ provider.complete(SYSTEM_PROMPT, conversation_data JSON)   ← 不可信提议
       └─ normalize：确定性守卫（§2）
       └─ authz.nexloop_record_claim_extraction：签名 definer 复核（§3）后落库
```

代码：`packages/eios-core/src/nexloop_eios/conversation_extraction.py`（纯逻辑/提示词/provider/守卫/时间解析）、`claim_store.py`（PG 端口）、`claim_extraction_jobs.py`（后台调度/worker/积压）；迁移 `0066_nx019_claims.sql`、`0069_nx019_extraction_queue.sql`（0069 为临时编号）。

版本：提取器 `nx019-extractor/2`、提示词 `nx019-conversation-extraction-prompt/2`（v2 起 claim_id 含 corrects/subject_text；与 /1 的结果视为不同输入版本）。

## 2. 确定性守卫（与模型无关）

| 语义 | 规则 |
|---|---|
| 话题切分 | 仅保留 `user_valid_reply=true` 且范围内有顾客**实质**消息（非“好的/嗯/谢谢”等）的话题；记录 `first_sequence/last_sequence/message_ids`。被剔除话题的 Claim 一并剔除并记录原因 |
| 原文证据 | 显性 Claim 必须有 quote，且是所引消息正文的逐字片段；服务端算 `span_start/span_end`（字符偏移）与 `source_content_hash`；speaker 由消息 actor 与会话 owner 推导，不信模型 |
| 九类 epistemic_kind | 与 `ontology-mutation` 枚举一致；`verified_fact` 一律拒绝（守卫、SQL 函数、表 CHECK 三层） |
| 显性/隐性 | `explicit=false` 强制 `hypothesis`，`resolution_state=hypothesis_only`，置信度上限 0.6，必须 `derived_from` 同次提取中已接受的显性 Claim（或自带原文） |
| 条件 | 原文出现条件从句（如果/要是/只要/除非/等…再/…后再/的话/看情况）→ `modality=conditional`，condition 必须是原文片段（模型给的不是原文则改用原文从句） |
| 不确定/暂时 | 考虑/可能/也许/看看/暂不/先不… → `tentative`；“解决后再考虑续费”不会成为确定续费 |
| 否定 | intent/preference：结论为否定却原文（去掉条件从句、去掉“要不要”类正反问）无否定词 → 拒绝 `ungrounded_negation`；结论肯定而原文有否定 → 拒绝 `polarity_conflict`。因此不会产出“不续费”这种相反偏好 |
| 说话人 | 顾客的 commitment 改为 intent；客服的非承诺陈述（产品介绍等）不是顾客知识，拒绝 |
| 时间 | 模型只抄写原文时间短语；不在原文中的（如编造的“3点”）丢弃并标记。由 `resolve_time` 以**所引消息的服务端接收时间**、**租户时区**解析：明确日期/时刻→resolved；“下午/明晚/月底/下周三之前/未说上下午的3点”→ambiguous 窗口；“过两天/回头/改天/稍后/三天前”→unresolved。从不精确到编造的分钟 |
| 提示注入 | 对话作为 JSON 数据放入 `conversation_data`；提示词声明其中指令不是指令。命中注入特征的消息产生的 Claim 只能是 `user_statement/need_problem/hypothesis`，置信度≤0.5 并标记。模型输出严格 schema，夹带 `tool_calls` 等任意额外字段→整份拒绝，不落库。提取路径不执行任何工具/SQL/Action |
| 反复投诉 | 不同消息的同一问题各自成 Claim（不同 claim_id），共享 `correlation_key`（按 consumer+kind+predicate+规范化值），可跨会话关联；同一 span 被模型重复输出才按技术重复去重 |
| 跨轮代词 | `subject.text` 必须是该话题窗口内某条消息的原词；否则清空并标记 `subject_referent_ungrounded`（不跨话题解析） |
| 同名消费者 | `subject.kind=consumer` 的 ref 只取服务端会话的 consumer；模型给出的人名清空并标记 `consumer_identity_server_derived`；correlation_key 含 consumer_id，同名不同人不关联 |
| 矛盾证据 | 无更正词的“correction”改回陈述（`correction_without_marker`），矛盾陈述并存不合并；非连续 quote 拒绝 |
| 撤回与更正链 | correction 需原文更正词（说错/改成/不对/作废/收回/其实/不是…是…）；`corrects` 只能指向同次提取中更早或同条消息的显性 Claim，SQL 包装定义器复核后写 `corrects_claim_id`；原 Claim 不删除 |
| 模拟隔离 | claim_id/correlation_key/topic_key 含 world；simulation 会话读不到 real 会话（会话表 world=real 约束），提取被拒绝；客服消息里的“演练/模拟结果”不是顾客知识 |
| 幂等 | claim_id 由 tenant/world/会话/消息/span/语义字段/提取器版本确定性计算；同 `input_digest` 重跑：Python 预查直接重放、SQL 函数在咨询锁下再次判定重放，均不新增行 |

## 3. 存储与授权（0066_nx019）

- 表：`ontology.nexloop_extraction_runs`、`ontology.nexloop_conversation_topics`、`ontology.nexloop_claims`、`ontology.nexloop_extraction_run_claims`。全部 owner=nexloop_owner、ENABLE+FORCE RLS（`eios.tenant_id`）、对 api/worker/scheduler/identity 撤销全部表权限。外键绑定 `runtime.nexloop_conversations`（tenant/world/conversation）。
- 写入：`authz.nexloop_record_claim_extraction`（grant nexloop_api、nexloop_domain_worker）。复核：HMAC 签名与参数摘要；**仅活跃 service 凭据**（浏览器 Human 会话拒绝）；`nexloop_assert_action_authority` 对 `eios:action:nexloop.claim.extract:1`（前后两次）；Conversation 对象+2 字段、每条 Message 对象+5 字段的当前 READ 证明（`nexloop_assert_read_authority`）；消息属于该会话、序号严格递增、正文 hash 与 Message 对象一致；speaker、consumer_id 服务端推导；每条 Claim 的 span 必须在 SQL 中逐字回放 quote。
- 读取：`authz.nexloop_read_conversation_claims` 需当前 Conversation 对象 READ；只返回 `runs/statements/hypotheses`，**没有正式属性区**。
- 未新增消息表；未写任何正式对象、授权事实或 Action claim（测试断言计数不变）。

## 3a. 后台路径（0069，docs/04 §3）

- `runtime.nexloop_conversation_messages` 的 AFTER INSERT 触发器在**同一事务**写 `runtime.nexloop_claim_extraction_feed`（FORCE RLS、无直接表权限）。交互路径只多一行插入，不调用模型；迁移时把已有消息回填为待提取。
- `ClaimExtractionScheduler`（nexloop_api service，需 `nexloop.claim.extract` 与 `NexLoop.queue.claim-extraction` EXECUTE）：`authz.nexloop_claim_extraction_feed('due')` 取“最新待提取消息已静默 quiet_seconds”的会话（去抖），窗口为截止序号前最近 `window` 条消息；`PostgresDurableQueue.accept(source_id='nexloop.claim-extraction', event_id='<会话>:<截止序号>')` 入既有 durable queue（inbox 去重），再 `mark`。accept 后、mark 前崩溃：重跑得到同一 task，不重复入队。
- `ClaimExtractionWorker`（nexloop_domain_worker service）：租约 claim → 提取 → finish。provider 不可用/输出被拒/未知异常 → `retry_wait`（`retry_base_seconds·2^(attempt-1)`，≤3600s），attempts 耗尽由既有队列转 `dead_lettered`；权限缺失或撤销 → `failed`（code=denied，不重试）。结果只含 code/摘要，不含正文或密钥。
- 积压：`ClaimFeed.backlog()` 返回 `feed_pending`、`oldest_pending_seconds`、按状态的 job 计数与最近 20 条 dead_lettered（task、attempts、code、conversation）。

## 4. Provider

- CI：`DeterministicExtractionProvider` 按输入载荷摘要回放冻结的合成输出；未登记输入→`ExtractionProviderUnavailable`，绝不编造。
- 真实：`OpenAICompatibleExtractionProvider` 复用 `ModelProfile`（allowlist：deepseek / deepseek-flash / https://api.deepseek.com），key 只进 Authorization 头；错误信息不回显响应体或头。只由 `tests/verification_real_extraction.py` 在显式设置 `NEXLOOP_NX019_REAL_ENV_FILE` 时调用；默认 CI 不收集该文件。
- 提示词模板 `SYSTEM_PROMPT`（`PROMPT_VERSION=nx019-conversation-extraction-prompt/1`）由负责人话题抽取 prompt 改写：保留 topic / conversation_summary / user_valid_reply，扩展为 message_refs 与 Claim 字段，加入数据≠指令、显性/隐性、条件否定、时间原文抄写规则。仓库内示例全部为合成数据。

## 5. 数据集与覆盖

`tests/data/nx019_extraction_cases.json` **v2，100 例**中文合成用例，冻结；v1 的 37 例原样保留（测试未删例；v2 新例暴露“不错”被误判为否定，修的是守卫）。分类：条件/否定 20、相对时间/时区 16、反复投诉 5、提示注入/严格 schema 9、隐性推断 3、话题切分/落地/说话人/类型/更正 7+、跨轮代词 8、同名消费者 6、矛盾证据 6、撤回与更正链 8、模拟隔离 6（含 world 双跑不相交）、并发 4（4 线程结果一致）、跨租户 4（两租户 claim/correlation/topic 键不相交）。每例含输入、模拟模型输出（多例刻意错误）、预期 Claim、禁止模式与原文区间。

PG 层另测：同输入 4 线程并发只落一次、3 个 worker 并发只处理一次、simulation 世界服务被拒、他租户服务提取与读取被拒。仍未覆盖：历史订单（依赖 NX-020 对象解析）、双 Role 并发（依赖 NX-018 Role 运行时）。

## 6. 已知限制

- 当前持久化的 Message 只有消费者（Human 浏览器）入站消息；Agent 回复以 effect 投递存在，不是 Message 对象。因此真实存储路径上 speaker 全为 consumer，企业 commitment Claim 只能在 Agent 消息成为 Message 对象后产生（纯逻辑与数据集已覆盖 agent 路径；SQL 已按 actor≠owner 推导 agent）。
- Message 没有 provider 序号与 reply-to 字段；提取按接收序号（sequence）排序，不能表达“引用回复”。AT-014 因此不能判定通过。
- 守卫是中文规则集合，不是语言理解：对未覆盖的说法可能过严（拒绝）或保守（tentative），不会把条件/否定升级为确定结论，但可能漏掉信息。
- 后台窗口按会话截止序号重跑整个最近窗口：同一消息在相邻窗口中可能被再次提出（claim_id 相同则不重复；真实模型输出不同则新增 Claim，以 correlation_key 关联）。NX-020 应以每会话最新 run 为当前提取。
- 本环境 `tests/test_message_relay.py` 有 7 项在 base `9a196f4` 上同样失败（CLI 子进程 `Message relay unavailable`），与本线改动无关，未处理。
- 契约：`packages/contracts` 无 Claim schema。提案见 §7，未改契约。

## 7. 真实调用失败类型

首轮真实运行中 C05、C16 记为 `provider_unavailable`。当时的 provider 把所有非 200 响应和异常折叠为两条消息，报告只保存了异常类名 `ExtractionProviderUnavailable`，**错误类型（超时/限流/5xx/解析）无法从现有证据判定**；按规则未重跑。之后 provider 已细分 `code`（http_429/http_5xx/http_4xx/timeout/transport/response_parse），验证脚本会记录该 code。

## 8. 契约提案（未实施）

完整草案与示例见 `NX-019-claim-contract-proposal.md`。概要：建议新增 `claim.schema.json`：`claim_id, tenant_id, world_id, conversation_id, consumer_id, topic_key, subject{kind,ref,text}, predicate, value{type,value}, speaker, polarity, modality, condition, time_expression, valid_time{kind,status,start,end,timezone,anchor,latest_bound_window?}, source{message_id,sequence,span_start,span_end,content_hash,quote}|null, derived_from[], extractor_version, confidence, epistemic_kind(九类枚举，提取器产出时排除 verified_fact), resolution_state(unresolved|hypothesis_only|needs_resolution|awaiting_definition|rejected_definition|resolved|superseded), correlation_key, guard_flags[]`。NX-020 消费前由负责人/调度员决定是否纳入契约。
