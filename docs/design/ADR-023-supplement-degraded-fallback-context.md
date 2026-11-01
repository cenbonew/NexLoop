# ADR-023 补充（草稿）｜兜底回复的降级上下文

状态：**草稿**，待负责人确认。确认后并入 `adr/ADR-023-explicit-refusal-hard-stop.md`，或另立新 ADR 编号。
关联：ADR-023 §2.6/§2.7（每条来信都有回复，受限期间只允许绑定回复）；trial e80e360c（10/18，customer-1 的 v6 上下文超过 64 KiB）；c10176d4（限制 Claim 候选数，按字节裁剪）；0206（临时编号，本分支）。

## 1. 背景

c10176d4 之后，非必带条目会按配额裁剪，直到 pack 不超过 64 KiB。但必带条目（钉住的条目）永远不裁，包括：
- 联系限制和拒收 Claim；
- 未确认的意图与出站；
- 进行中的计划和 Commitment；
- 最近一轮顾客原话。

如果某位客户只算必带条目就超过 64 KiB，或者超过契约规定的单区块条目上限（例如 `open_work` 最多 128 条），主路径和兜底都无法组包，“每条来信都有回复”这一条就做不到了。

## 2. 决定

1. **只对兜底回复 Run 降级。** 主路径不变：组包失败就照旧失败，交给兜底处理。
2. **安全条目一条都不丢。** 降级上下文保留全部约束、联系限制和拒收（带 `contact_limit` / `negation` 标签的 Claim）；空间够时再加上最近一轮顾客原话；其余非必带条目全部不带。
3. **必带的进行中事项按引用列出，不携带内容。** 未确认意图与出站、计划、Commitment 写进 `insufficient`，形如 `{"code":"mandatory_exceeds_budget","section":"open_work","refs":[...]}`。模型因此知道还有哪些未结事项，但看不到内容。
4. **SQL 只为兜底 Run 放行这种缺省。** v6 区块校验（0206 重定义的 `authz.nexloop_context_v6_sections`）只在同时满足以下条件时，接受必带进行中事项的缺省：
   - 当前 Run 记录在 `authz.nexloop_message_fallback_issuances` 中（是兜底回复 Run）；
   - 被缺省的条目属于 `open_work`；
   - 该条目的引用在上述 insufficient 条目中列出。

   联系限制或拒收缺失时，照旧以 `context v6 pinned source missing` 拒绝。
5. **只保留安全条目仍放不下，就不回复。** 不会构造缺少联系限制的上下文。兜底以 `fallback_failed` 升级给负责人，证据中带 `check=fallback_context_unsafe`。
6. **派发时的保护不变。** ADR-023 的联系限制控制仍在派发时检查：受限期间只允许绑定回复，且一条来信最多一条。降级只影响模型能看到的内容，不放宽任何派发检查。

## 3. 后果

- 极端历史下客户仍能收到兜底回复。回复的依据只有当前来信、联系限制和拒收、最近一轮原话，以及未结事项的引用。
- 兜底回复可能不知道某个具体 Commitment 或未确认回复的细节，只知道它们存在（按引用）。因此提示词和工具说明不允许它对这些事项作出新承诺，这一点沿用现有的兜底提示约束。
- 如果 Source 读不到会话 Claim（`required_source_unreadable`，evidence），降级与普通兜底一样不带 Claim。此时联系限制由派发控制保证，与现状一致。如果负责人要求更严格（读不到 Claim 就不回复），可以在第 5 条中增加这一条件。
- 契约不变：`mandatory_exceeds_budget` 本来就在 v6 的 insufficient 枚举中，`refs` 上限为 512。

## 4. 需负责人确认

- 第 2 条的取舍：是否在降级上下文中保留最近一轮原话。
- 第 3 条中“读不到 Claim 时是否仍然降级回复”。

## 5. 修订记录

### 2026-11-01（负责人拍板，S87 决策 A = ④ 保守版，选项 ①；S88，迁移 0395 临时编号）

S87 把上下文估算改为 v2（条目按包中完整对象计、固定段计入 `used`）后，长历史客户的必带条目加固定段可能超过 **token 预算**（不只是 64 KiB）。负责人拍板：

1. **例外推广到 message 与 plan Run。** 0395 重定义 `authz.nexloop_context_v6_sections`：去掉"Run 必须在 `authz.nexloop_message_fallback_issuances` 中"的条件，其余照 §2 第 4 条——只有 `open_work` 的必带事项、只有在 insufficient 中以 `mandatory_exceeds_budget`（section `open_work`）按引用列出时，才接受其缺省；联系限制与拒收（constraints、evidence）任何 Run 都不能缺。
2. **触发条件改为 token 预算。** 主路径与 plan Run 组包时，若预算报告给出 `mandatory_exceeds_budget`（必带 + 固定段超过 `available`），依次尝试：保留最新 k 条必带进行中事项的正文、其余按引用列出；再不行，全部按引用列出。**只有降级能让整包回到预算内时才采用**；若联系限制与拒收本身已超预算，降级没有收益且会让模型看不到"已有未确认订单"，此时保留完整包，按原样报 `mandatory_exceeds_budget`（section 为空）。兜底回复在 64 KiB 字节上限上的原有降级（§2）不变。
3. **k 的默认值与位置**：`DEGRADED_OPEN_WORK_KEEP = 5`（`packages/eios-core/src/nexloop_eios/context_artifacts.py`），按 `at` 取最新；它是代码常量而非策略字段——策略的键由 0088 与 `context_engine.strategy.validate` 固定，改为可配置需另发策略结构。
4. 模型仍能从 insufficient 的 `refs` 知道还有哪些未确认意图与出站（数量即 refs 条数），因此"历史上有未确认订单时不重复下单"（f4e3629a）的依据仍在；真实模型行为在 S84 运行单的对话回归中验证。

### 否决的做法（本次）

| 做法 | 理由 |
|---|---|
| ② 待决意图 / 承诺做摘要条目 | 摘要是新的事实来源，需要可追溯到原条目的来源证明与新的 SQL 核对规则，改动最大；暂不做 |
| ③ 直接提高 `input_token_budget` | 成本决策：覆盖 stage p95（18095）需 ≥ 22000（预留 3900），先观察 v2 估算 7 天的 `context_input_ratio` 与裁剪率再定 |
| 降级联系限制或拒收 | 违反 §2 第 2 条，任何 Run 都不允许 |
