# 设计：candidate-template 的 Schema 专用模式（schema_only）

- **状态**：设计稿，待 L6 复核，通过后实现。作者：L7。依据：main 83ce06b9。
- **决定**：负责人 2026-11-07 选方案 A（原话"A"）。负责人直接决定加的属性（这次是 Consumer.delivery_windows，claim-consumer-plan §10 第 2 项），**可以跳过"候选从提取或检索失败中产生"这一条**；人工审核（`ontology.schema.review` 的 check）和发布许可（nx040-gates-1 的 accept 报告）照旧。
- **为什么要它**：
  - r8 的 Schema 提案要从一个打开的演进候选开始（`inputs.candidate_template`）。
  - 现有的 candidate-template 只用"失败且有检索未命中"的 Run 做草稿，还要 ≥ 4 个簇单元（排在 `HOLDOUT_ORDER` 第 8 位）。
  - stage 上真实模型的 Run 不失败（run-s6-stage-prep A3），负责人 10-29 也定了不人为制造失败。所以候选模板生成不出来，阶段二的 Consumer v2 无法发布。

## 1. 可行性：SQL 不校验失败或未命中（已核，83ce06b9 上每个函数的最新定义）

| 函数 | 校验什么 | 失败或未命中？ |
|---|---|---|
| `authz.nexloop_evo_diagnosis_open`（0235） | 请求体的键；每个 Run 都由本租户的服务凭据签发；不含擦除中的 Consumer；租户有 evolution 预算行；**截止时间 = 开启 + 30 分钟** | 不查。`attribution` 只要求是"类别 → 数字"的对象 |
| `authz.nexloop_evo_candidate_propose`（0253） | dimension 只能是 semantic_content 或 ontology_schema；Case 是诊断里的 Run；同一簇不跨两侧（AT-048）；scope 和 patch 白名单（AT-024）；patch 来源在诊断的 Consumer 里；诊断仍为 open 且未过截止时间 | 不查 |
| `authz.nexloop_schema_proposal_freeze`（0252） | 候选是 ontology_schema、状态可用；在候选 scope 内；类型存在或不存在 | 不查 |
| `authz.nexloop_evo_evaluation_open`（0303） | bundle 一致；候选为 proposed、诊断未失效；泄漏（簇、同一 Consumer、相同词集跨两侧）；留出集使用次数 < 5；维度已开放、未停止；预算未用尽 | 不查 |
| G9–G13（`schema_gates.evaluate_schema`，本地）、`conclude`、`permit_issue`（0257） | 报告为 accept、minor，属于 nx040-gates-1 | 不查 |

要处理的约束有两处：

1. `runtime.nexloop_evo_candidates.category` 的检查约束只允许 `retrieval_miss` 和 `ambiguity_unresolved`（0235:60）。schema_only 的候选不是从这两类信号来的，写成其中任何一个都等于在库里虚报来源。所以要加 **迁移 0419（临时号）**：把约束放宽为再允许一个 `owner_directed_schema`。只改约束，不重定义任何函数，copy guard 不涉及。
   - 另一个办法是不加迁移、沿用 `retrieval_miss`，并在诊断的 `attribution` 里记 `{"schema_only": 1}`。但候选行本身会写错来源，**不推荐**。
2. 诊断开启后 30 分钟截止。r8 的候选提案必须在生成模板后 30 分钟内跑，否则报 `budget_exhausted`（diagnosis_time）。这条写进运行单。

## 2. 新模式

`s6_stage_evolution.py candidate-template --mode schema_only`。默认模式不变。

- **样本**：与现有模式相同，演进 reader 读 window 内、persona 为配置值（simulated_agent / synthetic）的 Run。**不要求失败或未命中**。
- **分簇**：按 Consumer 分簇，与 AT-048 的 `_cluster` 相同；问了同一组词的 Run 合并成一个单元，与现有逻辑相同，这样开评的泄漏检查不会拒绝。
- **最少 Consumer 数**：至少 **2 个簇单元**，一个做设计集、一个做验证集。这和 `GROUP_MINIMUM` 的"至少 2 个簇"一致。schema_only 不和 AT 共用 `HOLDOUT_ORDER`：它的留出集按自己的 seed 切，不占用 AT 的留出集序号。不足 2 个时报 Precondition，不生成模板。
- **诊断**：用这些 Run 开诊断，`attribution` 写各类别的计数，再加上 `schema_only: 1`。
- **候选体**：
  - `category` 为 `owner_directed_schema`；`candidate.dimension` 先写 `ontology_schema`（r8 本来就会覆盖）；
  - 设计集和验证集用 `split_cases` 切分；
  - patch 写 `change_kind: add_optional_property`、`target_refs: [eios:object_type:<schema_type>]`、`term_hashes: []`，`run_ids` 取诊断里的 Run；
  - `primary_hypothesis` 写负责人决定的出处（2026-11-07）。
- **模板文件**：0600，外层包一层 `{"mode": "schema_only", "decision": "owner 2026-11-07", "created_at": …, "body": <候选体>}`。默认模式的模板保持原来的裸候选体，兼容现有文件。

## 3. 谁能用、谁拒绝

- **只有 `s6_stage_schema.py r8` 接受 schema_only 模板**：读到外层包装后取 `body`，并在结果里记一行 `template_mode: schema_only`。
- **拒绝**：
  - `s6_stage_schema.py at024`（AT-024，是验收）和 `r10`（S6-R10）遇到 schema_only 模板时以 `Refused('candidate_template mode schema_only is for owner-directed Schema proposals (r8) only')` 拒绝。r10 本身只用 basis 做本地判断，但它是 S6 验收项，一并拒绝。
  - `s6_stage_evolution.py` 的每个验收项（at023、at048、r1、r3、r4、r5、r7 等）：配置里的 `inputs.candidate_template` 指向 schema_only 模板时拒绝；遇到 `owner_directed_schema` 类别的诊断或候选也拒绝。这些验收项本来就不读模板，所以加的是显式检查，防止有人把 schema_only 的诊断当成验收输入。
  - r6 不用模板，不受影响。
- **库里可辨认**：`category = owner_directed_schema`，诊断的 `attribution.schema_only = 1`。以后的验收查询和证据都可以按这两个值排除。

## 4. 测试（实现时）

1. 没有任何失败 Run、只有成功 Run 的 window（≥ 2 个合成 Consumer）：schema_only 也能生成模板；诊断 attribution 含 `schema_only`；模板外层 `mode` 为 schema_only，文件 0600。
2. 最少 Consumer 数：只有 1 个合成 Consumer 时报 Precondition，不写文件、不开诊断。
3. r8 用 schema_only 模板走到 accepted_for_release 并签发许可，与现有 r8 正向对照相同；候选行的 `category = owner_directed_schema`。
4. at024、r10，以及 s6_stage_evolution 的 at023、at048，遇到 schema_only 模板时都拒绝，不写任何东西。
5. 迁移 0419：放宽后的约束接受 `owner_directed_schema`，仍拒绝其他值；默认模式的模板和 r8 不受影响（现有 S6 测试照跑）。
6. 回归：bootstrap、published migrations、copy guard、claim_store、tenant_fk、全部 S6 测试（test_s6_stage_evolution_pg、test_s6_stage_schema_pg、test_s6_stage_inputs_pg），以及本地的 r8 正向对照。

## 5. 消耗与写入（r8 一次）

- **消耗**：只有 reflow 预留扣 **0.01 元** evolution 预算（`reflow_amount` 默认值）。开诊断、提案、开评都不扣费，没有模型调用，G9–G13 在本地计算。
- **写入**：
  - 诊断 1 个（30 分钟后自然截止）；
  - 候选、patch、Schema 提案、bundle 各 1 个；
  - 开评 1 次（test world），schema_only 自己的留出集使用 1 次；
  - reflow 预留 1 笔、结论 1 条、发布许可 1 张。
- 正式 Schema 只有在人工执行 `schema_proposal.py publish` 时才会改动。

## 6. 预计工作量

实现、迁移、测试约 0.5 人日，实现交付后再交 L6 复核。
