# 设计：candidate-template 的 Schema 专用模式（schema_only）

- **状态**：设计稿 v2，待 L6 复核，通过后实现。v1（fd786af6）L6 判条件 PASS，v2 加入 C1 和其余要求，见第 7 节。作者：L7。依据：main 83ce06b9。
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

1. `runtime.nexloop_evo_candidates.category` 的检查约束只允许 `retrieval_miss` 和 `ambiguity_unresolved`（0235:60）。schema_only 的候选不是从这两类信号来的，写成其中任何一个都等于在库里虚报来源。所以要加 **迁移 0419（临时号）**，只动约束，不重定义任何函数，copy guard 不涉及：
   - 删掉 `nexloop_evo_candidates_category_check`，重建为 `category in ('retrieval_miss','ambiguity_unresolved','owner_directed_schema')`；
   - **C1**：新增表级约束 `nexloop_evo_candidates_owner_directed_dimension check(category<>'owner_directed_schema' or dimension='ontology_schema')`。`dimension` 是这张表的列（0235:61；0253 把它的约束放宽为 semantic_content 或 ontology_schema），所以能写成表级约束，不需要靠脚本检查。
   - 现有的行全部满足这两条，所以加约束时不会因为旧数据失败。
2. 诊断开启后 30 分钟截止。r8 的候选提案必须在生成模板后 30 分钟内跑，否则报 `budget_exhausted`（diagnosis_time），并把这个诊断标为 incomplete。运行单的写法见第 7 节。
3. **诊断 ID 是确定的**：`diagnosis_id(snapshot_digest)`，同一组样本得到同一个 ID；`diagnosis_open` 遇到已有 ID 会按重放返回原诊断。如果样本没变，过期后重新生成模板，拿到的还是那个已经 incomplete 的旧诊断，走不下去。所以 schema_only 的 `input_snapshot_digest` 要在样本摘要之外再纳入模式名、属性和生成时间，例如 `sha256(freeze_digest | 'schema_only' | type_name.property_name | generated_at)`。这样每次生成都是一个新诊断，旧诊断自然失效。SQL 对它只要求是 64 位十六进制，候选的 digest 要与诊断一致。

## 2. 新模式

`s6_stage_evolution.py candidate-template --mode schema_only`。默认模式不变。

- **只为清单里的属性生成（C1）**：S6 私有配置新增 `inputs.owner_directed_properties: [{type_name, property_name, decision}]`，这次只有 `{"type_name": "Consumer", "property_name": "delivery_windows", "decision": "owner 2026-11-07 / claim-consumer-plan §10-2"}`。命令行要指定其中一项（`--property Consumer.delivery_windows`）；清单为空，或指定的属性不在清单里，就拒绝，不生成模板。
- **样本**：与现有模式相同，演进 reader 读 window 内、persona 为配置值（simulated_agent / synthetic）的 Run。**不要求失败或未命中**。
- **分簇**：按 Consumer 分簇，与 AT-048 的 `_cluster` 相同；问了同一组词的 Run 合并成一个单元，与现有逻辑相同，这样开评的泄漏检查不会拒绝。
- **最少 Consumer 数**：至少 **2 个簇单元**，一个做设计集、一个做验证集。这和 `GROUP_MINIMUM` 的"至少 2 个簇"一致。schema_only 不和 AT 共用 `HOLDOUT_ORDER`：它的留出集按自己的 seed 切，不占用 AT 的留出集序号。不足 2 个时报 Precondition，不生成模板。
- **诊断**：用这些 Run 开诊断，`attribution` 写各类别的计数，再加上 `schema_only: 1`。
- **候选体**：
  - `category` 为 `owner_directed_schema`；`candidate.dimension` 先写 `ontology_schema`（r8 本来就会覆盖）；
  - 设计集和验证集用 `split_cases` 切分；
  - patch 写 `change_kind: add_optional_property`、`target_refs: [eios:object_type:<schema_type>]`、`term_hashes: []`，`run_ids` 取诊断里的 Run；
  - `primary_hypothesis` 写负责人决定的出处（2026-11-07）。
- **模板文件**：0600，外层包一层 `{"mode": "schema_only", "property": {"type_name", "property_name"}, "decision": <清单里的 decision>, "created_at": …, "body": <候选体>}`。patch 的 `target_refs` 和候选的 `allowed_change_scope` 只含这个类型和属性的 ref。默认模式的模板保持原来的裸候选体，兼容现有文件。

## 3. 谁能用、谁拒绝

- **只有 `s6_stage_schema.py r8` 接受 schema_only 模板**：读到外层包装后取 `body`，并在结果里记 `template_mode: schema_only` 和 decision。
  - **C1**：r8 核对 `inputs.schema_type` 等于模板的 `property.type_name`，`inputs.property_change.property_name`（以及 `inputs.new_property`）等于模板的 `property.property_name`。任何一项不一致就 `Refused`，在写入任何东西之前拒绝。
- **拒绝**：
  - `s6_stage_schema.py at024`（AT-024，是验收）和 `r10`（S6-R10）遇到 schema_only 模板时以 `Refused('candidate_template mode schema_only is for owner-directed Schema proposals (r8) only')` 拒绝。r10 本身只用 basis 做本地判断，但它是 S6 验收项，一并拒绝（L6 同意）。
  - `s6_stage_evolution.py` 的每个验收项（at023、at048、r1、r3、r4、r5、r7 等）：配置里的 `inputs.candidate_template` 指向 schema_only 模板时拒绝；遇到 `owner_directed_schema` 类别的诊断或候选也拒绝。这些验收项本来就不读模板，所以加的是显式检查，防止有人把 schema_only 的诊断当成验收输入。
  - r6 不用模板，不受影响。
- **库里可辨认**：`category = owner_directed_schema`，诊断的 `attribution.schema_only = 1`。以后的验收查询和证据都可以按这两个值排除。

## 3a. 证据口径（C1：现有的统计与证据查询，按 category 过滤还是统计全部）

在 83ce06b9 上 git grep `nexloop_evo_candidates`，读这张表的只有下面这些：

| 读取方 | 读什么 | category 过滤？ | owner_directed_schema 的影响 |
|---|---|---|---|
| 0237、0241/0286/0303、0242/0250、0249:167、0257 | 按 `candidate_id` 读单个候选：开评、结论、许可、发布、删除传播 | 不过滤，按 id 读 | 只影响它自己的这个候选，与验收无关 |
| `runtime.nexloop_evo_review_summary`（0255:42–78，工作台审核队列） | 所有有报告的候选，最多 50 条，带 `dimension` 和 `change_kind` | **不过滤，统计全部** | 会出现在队列里，显示 dimension ontology_schema、change_kind add_optional_property。它确实是一次真实的 Schema 评估，应当可见。summary 不输出 category；要在界面上单独标出，就得重定义函数，本单不做，记为后续项 |
| `authz.nexloop_workbench_observe_read`（0249:420，可发布的语义候选） | `dimension='semantic_content'` 且 accepted | 按 dimension 过滤 | 被排除（表级约束保证它是 ontology_schema） |
| `s6_stage_inputs.py:152`（r6 的 semantic 输入） | `dimension='semantic_content'` 且 accepted | 按 dimension 过滤 | 被排除 |
| `s6_stage_evolution.py:333` | 按本次 AT 自己的候选 id 读状态 | 按 id 读 | 不受影响：各 AT 只读自己提的候选 |

- 结论：现在**没有任何按 category 统计**候选、accept 或许可的查询。S6 和 NX-038 的证据由验收脚本按自己提的候选 id 记录，只有工作台审核队列会统计全部候选。
- 证据规则写进 S6-stage-acceptance：
  - AT-023、AT-048、S6-R1 等演进验收的证据只引用验收脚本自己的候选；
  - 统计候选数、accept 数、许可数时，排除 `category='owner_directed_schema'`，或者单列为"负责人直接决定的 Schema 提案"；
  - 只读查询用 `where category<>'owner_directed_schema'`，单列时用 `= 'owner_directed_schema'`。

## 4. 测试（实现时）

1. 没有任何失败 Run、只有成功 Run 的 window（≥ 2 个合成 Consumer）：schema_only 也能生成模板；诊断 attribution 含 `schema_only`；模板外层 `mode` 为 schema_only，文件 0600。
2. 最少 Consumer 数：只有 1 个合成 Consumer 时报 Precondition，不写文件、不开诊断。
3. r8 用 schema_only 模板走到 accepted_for_release 并签发许可，与现有 r8 正向对照相同；候选行的 `category = owner_directed_schema`。
4. at024、r10，以及 s6_stage_evolution 的 at023、at048，遇到 schema_only 模板时都拒绝，不写任何东西。
5. 迁移 0419：放宽后的约束接受 `owner_directed_schema`，仍拒绝其他值；`owner_directed_schema` 配 `semantic_content` 的行被表级约束拒绝；默认模式的模板和 r8 不受影响（现有 S6 测试照跑）。
7. **C1 清单**：清单为空，或 `--property` 不在清单里，就不生成模板；r8 的 `schema_type`、`property_change.property_name` 与模板的 property 不一致时，在写入前拒绝。
8. **读出不报错**：带 `schema_only` 键的诊断及其候选，经工作台审核队列（`runtime.nexloop_evo_review_summary`）读取，以及经 `runtime.nexloop_evo_candidate_wire` 读出（0235:330 的 propose 返回和 0235:344 的 `nexloop_evo_candidate_read`），都正常返回，字段与普通候选相同。
9. **过期后重新生成**：模拟诊断过期（截止时间已过）后，r8 报 `budget_exhausted`；用同一组样本重新生成模板，得到新的 diagnosis_id，r8 能走通，旧诊断保持 incomplete。
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

## 7. 修订记录

### v2（2026-11-07，L6 对 v1 的条件 PASS）

- **C1 收紧豁免**：
  - `inputs.owner_directed_properties` 清单，这次只有 Consumer.delivery_windows；
  - 模板外层记 property 和 decision；
  - r8 核对 property，不一致就拒绝；
  - 0419 加表级约束：`owner_directed_schema` 只能配 `ontology_schema`（dimension 是列）；
  - 证据口径表（§3a）。
- **r10 一并拒绝**（L6 同意）。
- **新增用例**：带 `schema_only` 键的诊断经审核队列和 wire 读出不报错（§4 第 8 项）；过期后重新生成（§4 第 9 项）。
- **新发现**：诊断 ID 由样本摘要确定，样本不变就重新生成会复用已过期的旧诊断。schema_only 的 snapshot digest 加入模式名、属性和生成时间（§1 第 3 项）。
- **运行单**（实现时写进 run-claim-consumer-phase2 §2 第 1 步）：
  - 生成模板后紧接着跑 r8，中间不插人工步骤。审核（`schema_proposal.py check`）和发布在 r8 之后，r8 做完时候选已是 accepted_for_release、许可已签发，不受诊断截止时间限制。
  - r8 报 `budget_exhausted`（diagnosis_time）时，直接重新生成模板（新开诊断，不扣费），再跑 r8。
  - 旧诊断自然失效（incomplete），不用清理。
