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
