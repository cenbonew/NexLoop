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

代码：`packages/eios-core/src/nexloop_eios/conversation_extraction.py`（纯逻辑/提示词/provider/守卫/时间解析）、`claim_store.py`（PG 端口）、迁移 `0066_nx019_claims.sql`（临时编号，合并时由调度员重编号）。

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
| 幂等 | claim_id 由 tenant/world/会话/消息/span/语义字段/提取器版本确定性计算；同 `input_digest` 重跑：Python 预查直接重放、SQL 函数在咨询锁下再次判定重放，均不新增行 |

## 3. 存储与授权（0066_nx019）

- 表：`ontology.nexloop_extraction_runs`、`ontology.nexloop_conversation_topics`、`ontology.nexloop_claims`、`ontology.nexloop_extraction_run_claims`。全部 owner=nexloop_owner、ENABLE+FORCE RLS（`eios.tenant_id`）、对 api/worker/scheduler/identity 撤销全部表权限。外键绑定 `runtime.nexloop_conversations`（tenant/world/conversation）。
- 写入：`authz.nexloop_record_claim_extraction`（grant nexloop_api、nexloop_domain_worker）。复核：HMAC 签名与参数摘要；**仅活跃 service 凭据**（浏览器 Human 会话拒绝）；`nexloop_assert_action_authority` 对 `eios:action:nexloop.claim.extract:1`（前后两次）；Conversation 对象+2 字段、每条 Message 对象+5 字段的当前 READ 证明（`nexloop_assert_read_authority`）；消息属于该会话、序号严格递增、正文 hash 与 Message 对象一致；speaker、consumer_id 服务端推导；每条 Claim 的 span 必须在 SQL 中逐字回放 quote。
- 读取：`authz.nexloop_read_conversation_claims` 需当前 Conversation 对象 READ；只返回 `runs/statements/hypotheses`，**没有正式属性区**。
- 未新增消息表；未写任何正式对象、授权事实或 Action claim（测试断言计数不变）。

## 4. Provider

- CI：`DeterministicExtractionProvider` 按输入载荷摘要回放冻结的合成输出；未登记输入→`ExtractionProviderUnavailable`，绝不编造。
- 真实：`OpenAICompatibleExtractionProvider` 复用 `ModelProfile`（allowlist：deepseek / deepseek-flash / https://api.deepseek.com），key 只进 Authorization 头；错误信息不回显响应体或头。只由 `tests/verification_real_extraction.py` 在显式设置 `NEXLOOP_NX019_REAL_ENV_FILE` 时调用；默认 CI 不收集该文件。
- 提示词模板 `SYSTEM_PROMPT`（`PROMPT_VERSION=nx019-conversation-extraction-prompt/1`）由负责人话题抽取 prompt 改写：保留 topic / conversation_summary / user_valid_reply，扩展为 message_refs 与 Claim 字段，加入数据≠指令、显性/隐性、条件否定、时间原文抄写规则。仓库内示例全部为合成数据。

## 5. 数据集与覆盖

`tests/data/nx019_extraction_cases.json`：37 例中文合成用例，冻结。覆盖：docs/04 §8 示例、条件/否定/暂不/正反问（12）、相对时间与时区（8，含同一时刻上海/洛杉矶）、反复投诉与技术重复（3）、提示注入与严格 schema（4）、隐性推断（3）、话题切分/原文落地/说话人/类型值/更正（7）。每例含输入、模拟模型输出（多例刻意错误）、预期 Claim、禁止模式与原文区间（span 由断言回放）。

docs/04 §10 与 docs/14 §5 的 100 例目标**尚未达到**；未覆盖：跨轮代词、同名消费者、历史订单、矛盾证据、撤回更正链、模拟污染、双角色并发、跨租户检索（依赖 NX-020/021 或多租户夹具），由后续扩充。

## 6. 已知限制

- 当前持久化的 Message 只有消费者（Human 浏览器）入站消息；Agent 回复以 effect 投递存在，不是 Message 对象。因此真实存储路径上 speaker 全为 consumer，企业 commitment Claim 只能在 Agent 消息成为 Message 对象后产生（纯逻辑与数据集已覆盖 agent 路径；SQL 已按 actor≠owner 推导 agent）。
- Message 没有 provider 序号与 reply-to 字段；提取按接收序号（sequence）排序，不能表达“引用回复”。AT-014 因此不能判定通过。
- 守卫是中文规则集合，不是语言理解：对未覆盖的说法可能过严（拒绝）或保守（tentative），不会把条件/否定升级为确定结论，但可能漏掉信息。
- 提取运行未接入后台队列/outbox 触发（调用方需给出会话与消息窗口）；失败重试/dead_letter 仍沿用调用方队列语义，未单独实现。
- 契约：`packages/contracts` 无 Claim schema。提案见 §7，未改契约。

## 7. 契约提案（未实施）

建议新增 `claim.schema.json`：`claim_id, tenant_id, world_id, conversation_id, consumer_id, topic_key, subject{kind,ref,text}, predicate, value{type,value}, speaker, polarity, modality, condition, time_expression, valid_time{kind,status,start,end,timezone,anchor,latest_bound_window?}, source{message_id,sequence,span_start,span_end,content_hash,quote}|null, derived_from[], extractor_version, confidence, epistemic_kind(九类枚举，提取器产出时排除 verified_fact), resolution_state(unresolved|hypothesis_only|needs_resolution|awaiting_definition|rejected_definition|resolved|superseded), correlation_key, guard_flags[]`。NX-020 消费前由负责人/调度员决定是否纳入契约。
