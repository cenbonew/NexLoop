# S-DUP：重复抽取的修正设计

状态：设计稿，未实现。基线为第 115 批 d6080b59。相关计划：`claim-to-consumer-property-plan.md` §9.7、§11.3。

## 1. 问题与数据（调度员 2026-11-02，DATA_HOST 只读，tenant stage，world real）

- 有 Claim 的消息里，重复最多的前十条：044a82cdc250 有 54 条 Claim，用了 31 种 predicate，来自 24 次首次运行；739687e99460 有 42 条；e0822a72dd45 有 36 条；其余也都在 21–30 条。
- 窗口大小绝大多数在 10 条以内，最大 31 条。
- 总量：Claim 1460 条。按（消息、原文区间、kind、polarity、取值）去重后剩 836 条，按（消息、区间、kind）去重后剩 663 条。**43% 的 Claim 只是措辞不同的重复。**

两个原因都由数据证实：

1. **滑动窗口重抽**：`authz.nexloop_claim_extraction_feed` 的 `due` 分支（`0069_nx019_extraction_queue.sql`）返回的窗口是会话截至最新待抽消息的**最近 `window` 条**（默认 50）。所以每来一轮新消息，旧消息都会再抽一遍；窗口内容变了，`input_digest` 也就变了，replay 拦不住。
2. **去重键含自由文本**：`claim_id` 由 predicate、value、modality、condition、subject_text、`EXTRACTOR_VERSION` 等字段摘要而成（`conversation_extraction.py`，`claim['claim_id']=digest([...])`）。写入用的是 `on conflict(tenant_id,world,claim_id) do nothing`（0066），模型措辞一变就是新 id。`correlation_key` 里也有 predicate。

## 2. 主修：窗口拆成“目标”和“上下文”

- **队列载荷**：`due` 改为返回两组消息。`message_ids` 是本轮的**目标**，即 feed 里尚未入队、序号不大于 `through_sequence` 的消息。`context_message_ids` 是目标之前最多 `context` 条（默认 10）已处理消息，**只作上下文**。窗口总数仍受 `window` 限制。新迁移替换 `due` 分支（临时号 0411），`mark` 和 `backlog` 不变。
- **兼容**：已在队列里的旧任务没有 `context_message_ids`，按全部为目标处理，行为与现在相同。
- **worker / extractor**：`ClaimExtractionWorker` 把两组消息都交给 `ConversationClaimExtractor.extract`。`load_window` 按顺序载入两组，每条消息带 `role: target|context`。`input_digest` 同时包含两组消息和它们的角色。
- **提示词**（`PROMPT_VERSION` 升到 `/5`，`EXTRACTOR_VERSION` 升到 `/5`）：每行消息标出 `context: true/false`；明确要求“只从 context=false 的消息提取 Claim；context=true 的消息只用来理解指代和更正”。
- **服务端守卫**（`normalize`，与模型无关）：引用上下文消息的 Claim 一律丢弃，记入 rejected，原因 `context_message`。即使模型不守规矩，也不会再为旧消息写 Claim。
- **跨窗口更正**：目前 `corrects` 只能指向同一次输出里的 Claim，跨消息的更正之所以能成立，靠的正是滑动窗口把旧消息重抽一遍。拆分之后：
  - payload 增加 `existing_claims`：上下文消息上现有的当前 Claim（ref、kind、predicate、值、原文），只读；
  - 模型可以写 `corrects_existing: <ref>`；
  - 服务端把 ref 映射回 `claim_id`，并沿用现有校验（目标非 hypothesis、顺序早于本消息、NX-051 的有效顺序），写入 `corrects_claim_id`；
  - `0115` 记录函数的更正链接逻辑不变，只是目标可以是已有 Claim。
- 工作量约 1.5 人日：0411、`claim_extraction_jobs`、`claim_store.load_window`、`conversation_extraction`（提示词、payload、守卫、existing_claims）及测试。

## 3. 兜底：规范化去重键

- 键：`(tenant_id, world, source_message_id, span_start, span_end, epistemic_kind, polarity, 规范化取值)`。规范化取值复用 `normalize_value_key`，与 `correlation_key` 使用的是同一个函数。
- 位置：记录函数（新迁移，临时号 0412，复制 0115 的最新函数体，改动处加标记）。每条待写的 Claim，如果已存在一条键相同、未被取代（`superseded_at is null`，状态不是 `superseded`）的 Claim，就**不再写新行**，只把本次运行关联到已有的 `claim_id`（`nexloop_extraction_run_claims`）。返回结果里新增计数 `deduplicated`。
- 键里不含 predicate、modality、condition、subject_text。取值或 polarity 真有变化，以及 correction，仍写新 Claim。modality 不同（例如一次 requested、一次 asserted）**算同一条**，保留先记录的那条；这个取舍写进测试（见 §6）。
- 加一个普通索引 `(tenant_id, world, source_message_id, span_start, span_end, epistemic_kind)` 供查找。不能建唯一索引，因为历史数据已有重复。
- 工作量约 0.5 人日。

## 4. claim_id 公式不改

已发布的 `claim_id` 被证据、匹配结果、审核候选、`dependent_claims`、0228 偏好忽略记录等引用，重算会断开这些引用。去重靠 §3 的“已有就不写”，不改 id 的算法。

## 5. 历史重复：不删，只在视图里折叠

- 不删除、不改写历史 Claim。它们是证据，删除还会破坏引用和审计。
- 折叠视图（可选）：在工作台证据视图和客户详情里，按 §3 的键分组，每组显示最早的一条，并标注“另有 N 条相同表述”。匹配器、0228 和上下文包的数据不变。
- **推荐：做折叠视图，约 0.5 人日，放在主修之后。** 理由：43% 的重复会让负责人在客户详情里看到大量相同条目；只改显示，不改数据，风险小。这一项由负责人决定。

## 6. 回归与验收

- **冻结集**：`tests/data/nx019_extraction_cases.json` 全部用例（其中 14 处 `corrects`）在新版本下结果不变。冻结用例都是单窗口，全部消息都是目标，所以期望不变；用例文件本身不改。
- **新增用例**：
  - 两轮窗口：第二轮只抽新消息；模型回应里引用上下文消息的 Claim 被丢弃（`context_message`）。
  - 跨窗口更正：第二轮的“刚才说错了”经 `corrects_existing` 链接到第一轮的 Claim，第一轮那条被取代。
  - 兜底去重：同一区间、同一 kind、同一取值、不同 predicate 的第二次写入只关联、不新增；取值不同则新增。
  - modality 不同算同一条（§3 的取舍）。
- **stage 验收**（只读 SQL，部署之后跑）：新版本运行产生的 Claim 里，每条消息按（区间、kind、polarity、取值）分组后没有超过 1 条的组。

```sql
select left(source_message_id,12), span_start, span_end, epistemic_kind, polarity, value::text, count(*)
  from ontology.nexloop_claims
 where tenant_id='stage' and world='real' and extractor_version='nx019-extractor/5'
 group by 1,2,3,4,5,6 having count(*)>1;
```

预期 0 行。

- 测试清单按 batch-new-migration 的要求：`test_claim_store_pg`（AT-064：0411、0412 都读写 `nexloop_claims`，白名单要加上理由）、`test_claim_extraction_jobs_pg`、`test_conversation_extraction`、`test_extraction_dead_letters_pg`、`test_message_provider_extraction_pg`、`test_bootstrap`、`test_published_migrations`、`test_function_search_path`、`check_definer_search_path`。

## 7. 顺序与依赖

1. 主修（§2）和兜底（§3）同批交付，迁移临时号 0411、0412。
2. 2 号线的渠道偏好重抽（`contact_channel_reextract.py --apply`）**等主修部署之后再执行**，否则重抽会再产生一批重复。重抽窗口本身只含候选消息，都是目标，不受拆分影响。
3. 折叠视图（§5）视负责人决定，放在主修之后。
4. 修好之前，Claim 条数不能当指标（计划 §11.3）。

## 8. 风险

- 上下文从“最近 50 条全量”降到“目标加最多 10 条上下文”，模型可能看漏更早的指代。`context` 设为参数，可以按回归结果调大。
- 模型可能不遵守 `context: true`。由 §2 的服务端守卫兜住，代价只是被丢弃的 Claim 计入 rejected。
- `existing_claims` 会把上下文消息上已有 Claim 的原文再交给模型一次。这些原文本来就在上下文消息正文里，不增加新的数据暴露。

## 9. L6 条件（2026-11-02，条件 PASS）与实现

本节覆盖 §2、§3 中与它冲突的写法。实现在同一分支，迁移临时号 0411（`extraction_target_context`）和 0412（`extraction_dedupe`）。

### C1 existing_claims 的范围

- `existing_claims` 不随 10 条上下文走，而是取**这个会话当前有效的 Claim**：显式、未被取代、取值未擦除、有来源消息；按来源序号取最近 30 条（`claim_store.EXISTING_CLAIMS_LIMIT`），在 payload 中按 kind 分组，ref 为 `e1`、`e2`……
- 模型用 `corrects_existing: "<ref>"` 指回 10 条之前的内容。客户端（`normalize`）和服务端（0412 的包装函数）各校验一次：更正的种类必须是 correction；目标属于同一会话、不是 hypothesis、未擦除、未被取代、来源消息早于本消息。窗口外的消息不在本窗口的有效顺序里，所以这里按受理序号比较。
- 只有带上下文的窗口才附 existing_claims。冻结集都是单窗口，payload 不变。
- 用例：`test_extraction_dedupe_pg::test_context_is_never_extracted_and_a_correction_reaches_a_claim_outside_the_window`，被更正的 Claim 在 10 条上下文之前。

### C2 去重键改为包含 modality 和“有无 condition”

- §3 中“modality 不同算同一条、保留先记录的”**作废**。键为（消息、原文区间、kind、polarity、**modality**、**是否有 condition**、规范化取值）。
- 所以先 requested“只接收站内消息可以吗？”、后 asserted“只接收站内消息”会是两条，后一条进入 0228 偏好并生效。用例：`test_a_requested_then_asserted_statement_is_two_claims_and_the_later_one_counts`。
- 规范化取值：`ontology.nexloop_claim_value_key`（0412）与 `conversation_extraction.normalize_value_key` 一致。取值类型是键的一部分，所以字符串 `"3"` 和数字 `3` 不同。字符串去掉所有空白并转小写，所以 `"AB 12"` 等于 `"ab12"`，`" 站内消息 "` 等于 `"站内消息"`。其他类型（数字、布尔、金额）按 JSON 比较。用例覆盖“取值不同但规范化后相同”会被合并，以及 `"3"` 与 `3` 不会被合并。

### C3 部署顺序（部署单）

1. 停止 claim-extraction-worker（见 C4，不是必须，但这样最简单）。
2. 备份后执行迁移 0411、0412：兜底（0412）**不晚于** worker 升级。
3. 升级并启动 claim-extraction-scheduler 和 claim-extraction-worker（backend 镜像）。
4. 验收：§6 的查询，另查一次旧任务处理期间新增的 `/5` Claim 有没有重复：

```sql
select left(source_message_id,12), span_start, span_end, epistemic_kind, polarity, modality, (condition_text<>'') has_condition,
       ontology.nexloop_claim_value_key(value), count(*)
  from ontology.nexloop_claims
 where tenant_id='stage' and world='real' and extractor_version='nx019-extractor/5'
 group by 1,2,3,4,5,6,7,8 having count(*)>1;
```

预期 0 行。

5. 主修部署之后，才执行 2 号线的渠道偏好重抽：`contact_channel_reextract.py --apply`。它使用部署代码中的提取器版本，输出中的 `extractor_version` 必须是 `nx019-extractor/5`，否则停下。先 dry-run。

### C4 旧 worker 与新载荷

- 旧 worker（`/4`）只读 `payload['message_ids']`，忽略 `context_message_ids`。它会把目标当作完整窗口来抽取，没有上下文，**不会进死信**。0412 之后，它写入的重复也会被去重拦下。
- 新 worker 拿到 0411 之前入队的旧任务（没有 `context_message_ids`）时，按全部为目标处理。
- 所以不是必须先停 worker。部署单仍建议按 C3 停一下，迁移完与新 worker 一起起来，避免升级期间旧 worker 在没有上下文的情况下处理窗口。

### 其他

- AT-064 白名单加入 `_extraction_target_context` 和 `_extraction_dedupe`，并写明理由（`tests/test_claim_store_pg.py`）。
- 冻结集 `tests/data/nx019_extraction_cases.json` 是回归门槛，在 `/5` 下全部通过，用例文件未改动。
- 折叠视图（§5）暂不做，等负责人决定。
- copy guard：0411 对 0069 的取窗函数、0412 对 0066 记录函数的改动，登记在 `tests/support/s_dup_edits.py`。0115 的包装函数改名为 `..._v0115`，由新的包装函数替代（改名后重建，不算 redefinition）。
- 已知局限：取窗仍以 `window` 为目标上限。一次积压超过 `window` 条待抽消息时，最早的那些会被 mark 而不抽取，这与原来的滑动窗口相同。

### 去重键的单一来源（负责人 2026-11-02：历史重复只在显示上折叠，由 L2 在 K5 证据图接口里做）

- SQL：`ontology.nexloop_claim_statement_key(source_message_id, span_start, span_end, kind, polarity, modality, condition_text, value)`，定义在 0412，只读、immutable，已授权给 `nexloop_api`、`nexloop_domain_worker`。0412 的写入去重直接用它比较。取值部分是 `ontology.nexloop_claim_value_key(value)`。
- Python：`nexloop_eios.conversation_extraction.statement_key(...)`（取值部分是 `claim_value_key`），生成与 SQL 完全相同的文本：`v1|消息|起|止|kind|polarity|modality|有无condition(0/1)|类型:规范化取值`。
- 两边的规范化规则相同：字符串去掉 Python `\s` 那一组 Unicode 空白（SQL 侧写成同一显式字符集）并转小写；其他值用 jsonb 文本形式（键按长度再按字节排序）。`test_extraction_dedupe_pg::test_python_and_sql_statement_keys_are_the_same_text` 用 18 组取值逐字比较两边的结果，包括全角空格、不换行空格、"3" 与 3、金额对象、有无 condition。
- K5 折叠应使用这个键：SQL 里调用 `ontology.nexloop_claim_statement_key`，Python 里调用 `statement_key`，不要另写一套。
- 局限：大小写转换依赖数据库的区域设置，个别特殊字母（如土耳其语 İ）两边可能不同；超大或指数形式的数字，jsonb 与 Python 的文本形式可能不同。这两类不会出现在常规取值里。

## 10. 组批前补充（L6 2026-11-02 PASS 后）

### 10.1 退回方法

迁移只往前，不回退。已发布的 0411、0412 不改。

- 需要退回去重时，新写一个迁移：
  1. 把当前的 `authz.nexloop_record_claim_extraction` 改名（例如改为 `..._s_dup`），收回其授权；
  2. 把 `authz.nexloop_record_claim_extraction_v0115` 改名回 `authz.nexloop_record_claim_extraction`，恢复 `nexloop_api`、`nexloop_domain_worker` 的执行授权；
  3. `..._v0066` 的去重改动再以登记过的反向编辑重建（copy guard 登记）。
- 需要退回取窗拆分时，同样新写迁移，以 0069 的 due 分支重建取窗函数（copy guard 登记）。
- 代码侧回到 `/4` 的 backend 镜像即可，旧 worker 与新载荷兼容（§9 C4）。已写入的 `/5` Claim 不删除。

### 10.2 验收第三条只读 SQL：被同一次运行引用的重复

部署后，`/5` 里如果还有键相同的多条 Claim，只应来自两类：被同一次运行的推导或更正引用、因而不走去重的 Claim，以及重试或 0411 之前入队的旧任务。

```sql
with v5 as (
  select c.claim_id, c.first_input_digest,
         ontology.nexloop_claim_statement_key(c.source_message_id,c.span_start,c.span_end,c.epistemic_kind,c.polarity,c.modality,c.condition_text,c.value) k
    from ontology.nexloop_claims c
   where c.tenant_id='stage' and c.world='real' and c.extractor_version='nx019-extractor/5' and c.source_message_id is not null),
dup as (select * from v5 where k in (select k from v5 group by k having count(*)>1)),
cited as (
  select distinct d.claim_id from dup d
    join ontology.nexloop_claims o on o.tenant_id='stage' and o.world='real' and o.first_input_digest=d.first_input_digest
     and (o.corrects_claim_id=d.claim_id or o.derived_from ? ('claim:'||d.claim_id) or o.derived_from ? d.claim_id)),
run as (
  select r.input_digest, bool_or(m->'context'='true'::jsonb) has_context
    from ontology.nexloop_extraction_runs r cross join lateral jsonb_array_elements(r.message_refs) m
   where r.tenant_id='stage' and r.world='real' group by r.input_digest)
select (d.claim_id in (select claim_id from cited)) cited_in_run, coalesce(run.has_context,false) new_window, count(*)
  from dup d left join run on run.input_digest=d.first_input_digest
 group by 1,2 order by 1,2;
```

预期：只有 `cited_in_run=true`，或者 `new_window=false`（旧任务、首个窗口重试）的行；`cited_in_run=false and new_window=true` 为 0。否则说明去重有漏洞，停下报告。

### 10.3 局限（补充）

- **被引用的 Claim 不走去重**：同一次运行里被推导（`derived_from`）或更正（`corrects_claim_id`）引用的 Claim 照常写新行，这是一个小旁路。后续做法：对它们也去重，并把同一次运行中的 `derived_from` 和 `corrects_claim_id` 改指到已有的 `claim_id`。
- **先查再插、不加锁**：写入时先按键查找、再插入，没有锁或唯一约束。两个 worker 并发处理同一条消息时，可能各写一条。现在每个会话只有一个 worker（取窗按会话、队列按任务），可以接受；如果以后同一会话并行抽取，需要按（租户、会话）加咨询锁。

## 11. S-DUP-2：更正生效时一并取代同键重复

### 11.1 问题

0412 之前的重复抽取留下了同一句话的多条 Claim。更正只指向其中一条，那一条被取代后，其余几条仍是有效状态。匹配器、0228 偏好和上下文包都会读到这些仍有效的旧说法；页面上同一句话会同时出现“已更正”和“有效”两项。

### 11.2 设计（迁移临时号 0414，`claim_duplicate_supersede`）

- **判断依据只用 0412 的键**：`ontology.nexloop_claim_statement_key`（来源消息、原文区间、kind、polarity、modality、有无 condition、规范化取值）。
- **触发点**：在 `ontology.nexloop_claims` 上加 `after update of resolution_state` 触发器（与 0111 承诺的取代触发器同一做法）。只有当一条 Claim 变成 superseded，**并且是更正造成的**才动作，判断方法是：存在 `corrects_claim_id` 指向它的 Claim，或存在一个已 applied、`supersedes_claim` 指向它的提案。满足时，把同键、仍有效的其余 Claim 一并改为 superseded。
  - 走的是现有的取代写入：只更新 `resolution_state`，`superseded_at` 由 0139 的触发器盖时间戳，与单条更正一致。匹配器 apply、审核等任何取代路径都覆盖到。
  - 被连带取代的重复自己没有更正指向它，所以不会继续级联。
  - 没有更正的取代（例如提案被新提案替代）不连带。
- **可追溯**：新表 `ontology.nexloop_claim_supersessions`，每条被连带取代的重复写一行：`claim_id`、`corrected_claim_id`（被更正的那一条）、`correction_claim_id`（做更正的 Claim，或提案对应的 Claim）、`cause='statement_duplicate'`、时间。只存 id，不存原文；只追加。证据链由此可以从任何一条重复追溯到更正。
- **共用函数**：`ontology.nexloop_supersede_statement_duplicates(tenant, world, claim, correction)` 由触发器和补跑共用，返回取代条数。
- **历史补跑**：`authz.nexloop_claim_duplicate_supersede_backfill`，Action 为 `eios:action:nexloop.claim.supersede_duplicates:1`，协议 `nexloop-claim-duplicate-supersede-v1`。
  - 身份用 claim_matcher（取代本来由匹配器写入），新增一条授权；`deploy/` 的策略确认由调度员处理。
  - tenant 和 world 由凭据推导，只允许 `nexloop_domain_worker` 角色调用。
  - 跳过有擦除记录的客户；人设不跳过，因为这是数据一致性修正，不产生外发。
  - 可重复执行，被取代的重复不再有效。dry-run 和执行都只输出数量。
  - 命令行：`scripts/deploy/claim_duplicate_backfill.py`，先 dry-run 再 `--apply`。
- AT-064 白名单加入 `_claim_duplicate_supersede`。这里只改 `resolution_state`，不形成正式值。

### 11.3 部署

1. 迁移 0414 与第 116 批一起或紧随其后执行。
2. 用 claim_matcher 凭据执行补跑：先 dry-run，记录 `corrected / duplicates / skipped_erasure`，再 `--apply`。第二次执行预期全为 0。
3. 只读核对：有更正的已取代 Claim，同键下不应再有仍有效的重复。

```sql
select count(*) from ontology.nexloop_claims s join ontology.nexloop_claims d on d.tenant_id=s.tenant_id and d.world=s.world
  and d.source_message_id=s.source_message_id and d.span_start=s.span_start and d.span_end=s.span_end and d.epistemic_kind=s.epistemic_kind
  and d.claim_id<>s.claim_id and d.resolution_state<>'superseded'
  and ontology.nexloop_claim_statement_key(d.source_message_id,d.span_start,d.span_end,d.epistemic_kind,d.polarity,d.modality,d.condition_text,d.value)
     =ontology.nexloop_claim_statement_key(s.source_message_id,s.span_start,s.span_end,s.epistemic_kind,s.polarity,s.modality,s.condition_text,s.value)
 where s.tenant_id='stage' and s.world='real' and s.resolution_state='superseded'
   and ontology.nexloop_claim_correction_of(s.tenant_id,s.world,s.claim_id) is not null;
```

预期 0（有擦除记录的客户除外）。

### 11.4 第 116 批部署单应注明的已知问题（修好之前）

> 已知问题（S-DUP-2）：0412 之前重复抽取留下的同一句话，更正只取代其中一条，其余重复仍是有效状态，匹配器、0228 偏好和上下文包仍会读到已被更正的旧说法；页面会同时显示“已更正”和“有效”。修正在 S-DUP-2（迁移 `claim_duplicate_supersede` 加补跑 `claim_duplicate_backfill.py`），部署后执行补跑。

### 11.5 局限

- 连带范围只限同一条来源消息、同一原文区间的同键重复。不同消息里说过的同一句话不在此列，那是不同的陈述，需要各自的更正。
- 补跑一次扫描整个租户的已取代 Claim，在同一事务里执行；表变大后要按会话分批。
