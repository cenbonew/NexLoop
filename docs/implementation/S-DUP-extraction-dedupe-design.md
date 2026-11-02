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
