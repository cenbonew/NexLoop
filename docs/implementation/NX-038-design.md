# NX-038 设计稿：Evo 轨迹诊断与候选任务

- **状态**：设计稿，只有文档，不写代码、不加迁移。D1–D9 已由负责人 2026-10-12 定案。2026-10-20 修订（§12，L4，分支 `l4/nx038-design`）：S6 起步前补齐预算、隔离、NX-039 接口、权限与删除、实施切片，新增 D15–D20（负责人 2026-10-20 已按推荐决定），不改 D1–D14。2026-10-18 修订（§11，分支 `l6/nx038-design`，从 main `9d1223ee` 切出）：补入 10-12 之后才有的轨迹输入（NX-037 证据包结构、0204/0208 的失败码与已纠正的工具错误、收据与对账），只新增 D10–D14，不改已定案的 D1–D9。
- **分支**：`nx038-design`，从 main `917d8ca` 切出。
- **依据**：
  - planning/tasks.json NX-038：S6，依赖 NX-037，模块 M12/M14/M22，交付物为“冻结输入、归因、预算、候选隔离”；
  - 01_PRD M12、M14、M22；
  - 02_ARCHITECTURE §3、§4；
  - 08_EVOONTOLOGY_INTEGRATION；
  - 16_ADR ADR-007（不让 Evo 成为第二事实库）；
  - 14_TEST_ACCEPTANCE AT-023、AT-024、AT-048 与 14 §6；
  - 契约 `packages/contracts/evolution-candidate.schema.json`。
- **下游**：NX-039（独立评估与语义版本发布）接手本任务产出的候选，负责硬门槛、留出集、成本与性能上限以及回退。

## 1. 目标与边界

### 要做的事

- 从已发生的 Run 轨迹中找出“语义访问没有帮到 Run”的失败。
- 把失败归到内容、工具或 Schema 维度。
- 为每个可检验的假设生成一个隔离的候选（`evolution-candidate`，状态从 `proposed` 开始）。
- 冻结它的输入与预算，交给 NX-039 评估。

### 边界（不可协商）

- **Evo 只做三件事**：语义访问、候选演进、评估（08 §1；PRD M12 第 202 行）。它不是事实库（ADR-007；02 §4 知识权威，第 64 行）。
  - 诊断只读轨迹和证据，不写业务对象、Claim 或 Commitment。
  - 候选是隔离的工件，不覆盖 active 定义（AT-023）。
- **不绕过 EIOS**：
  - 正式 Schema 变化走 EIOS Schema 流程（08 §4“正式 Schema 路径”；04_CONVERSATION_ONTOLOGY_PIPELINE 第 82 行）；
  - 新类型不能附带提权 Action（AT-024）；
  - Evo 模块不能反向控制 Model Gateway、发布脚本或数据库管理员权限（02 第 97 行）。
- **实时事实路径不进入演进**：日常 Claim 和实例更正不走“评分必须提高”的门槛（08 §4；04 第 82 行）。NX-038 不诊断事实对错，只诊断语义访问。
- **没有外部效应**：诊断与候选评估中的任何回放都没有真实外部效应权限（14 §6 第 94 行；AT-046 负例见 `docs/implementation/AT-046-simulation-credential-guard.md`）。

## 2. 输入：轨迹与证据（只读）

### 2.1 数据面（已有表）

下表引用的文件都在 `packages/eios-core/src/eios/migrations/` 下。

| 证据 | 来源 | 诊断中的用途 |
|---|---|---|
| Run 生命周期 | `runtime.jobs`、`runtime.job_events`（0001_durable_core.sql:36、:64） | Run 的结局（完成、失败、超时、取消）与时间线 |
| 上下文包与来源 | `runtime.nexloop_context_packs`、`runtime.nexloop_context_sources`（0089_nx023_context_manifests.sql:6、:15）。来源记录 section（含 `semantics`）、ref、revision、content_hash、evidence_kind、status 与 omitted_reason | 语义条目是否被纳入或省略、省略原因、对应的定义版本 |
| 模型请求 | `runtime.nexloop_model_requests`（0089_nx023_context_manifests.sql:31；0120 加 currency）、`runtime.nexloop_model_request_prompts`（0092_nx023_context_bind_v6.sql:43） | 每次调用的 context_id、请求与响应 digest、result_status、用量与费用 |
| 工具耗时 | `runtime.nexloop_run_tool_timings`（0133_nx030_ui.sql:54） | 工具调用次数与耗时；只有聚合，没有逐次调用 |
| 拒绝与效应 | `runtime.nexloop_dispatch_refusals`（0131_nx030_observability.sql:29）、`runtime.nexloop_effect_intents`（0040_effect_intents.sql:30） | 拒绝码（NXC、NXB 系列）、意图结局 |
| 候选定义与审核 | `ontology.nexloop_candidate_definitions`（0070_nx020_claim_matching.sql:22）、`ontology.nexloop_review_decisions`（0082_nx044_review_actions.sql:9） | 抽取侧未匹配的定义、人工审核结论（合并、发布或拒绝） |
| Claim | `ontology.nexloop_claims`（0066_nx019_claims.sql:21） | 默认只用 ID、epistemic_kind、resolution_state、计数；不读取值，见 §6 |
| 语义访问 | `nexloop_eios/context_engine/semantic.py`（输出 version、coverage、ambiguity，第 75 行；歧义阈值第 38、67 行） | browse/resolve 的覆盖率与歧义 |

### 2.2 缺口：语义访问没有逐次的持久记录

- **现状**：browse/resolve 的结果只以 `context_sources` 中 `semantics` 条目的形式留下（被纳入或省略）。
  - “查询无命中”“歧义未解”没有自己的记录；
  - 08 §6 v0.1 要求的“实际调用与缺口记录”目前只覆盖到 Context 侧。
- **NX-038 实现时需要新增**：一张只追加的语义访问记录表，内容如下：
  - tenant、world、run_id、context_id；
  - 查询种类（object_type、property、vocabulary_value）；
  - 查询词项的 sha256，不存原文；
  - coverage、ambiguity；
  - 返回的定义 ref 与 revision；
  - recorded_at。
- **约束**：
  - 只由 Context Engine 写入，并纳入 NX-029 保留和删除（D2）；
  - 本稿不加迁移。

### 2.3 只读视图与冻结

- **只读视图**：诊断以独立的数据库角色（D1）通过 security definer 只读函数读取上面的数据。
  - 函数返回值只有 ID、代码、计数、digest 与时间，模式与 `0161_nx037_trial_readiness.sql` 的 `authz.nexloop_nx037_run_evidence` 相同；
  - 角色没有任何写权限，也不持有服务令牌、渠道或模型凭据。
- **冻结（08 §5；M22）**：每次诊断先冻结以下内容，并写入 `input_snapshot_digest`：
  - 输入快照：参与的 Run ID 列表、各表行的 digest；
  - eios revision（Schema 与别名表的当前版本）；
  - 语义版本、评价策略版本、模型配置。
  - 同一快照必须能复现同一诊断（M22 验收）。
- **租户与 world**：
  - 每次诊断只针对一个 tenant、一个 world，不跨租户读取。
  - 只读 `world='real'` 的真实轨迹。模拟或影子轨迹不进入真实诊断（AT-046；M13“不能将推测或模拟结果升格”）。

### 2.4 删除传播

- 轨迹中被删除的对象：Consumer 删除、NX-053 的联系键抹除、NX-029 保留到期。
- **快照建立时**：排除删除日志（`control.nexloop_deletion_ledger`；0129、0140）中已登记的主体。
- **快照建立后**：发生的删除使快照失效（`incomplete`），不在旧快照上继续评估。
- **候选与留出集**：只存 ID 和 digest，不复制消息或 Claim 原文。删除后，相关候选与留出集同样失效。

## 3. 诊断产出

### 3.1 失败模式分类（归因）

每个被诊断的 Run 落到一个主要类别（08 §5“归因可能是知识不足、检索遗漏、上下文丢失，不能动辄新增类型”）：

| 类别 | 判据（只用可观察记录） | 维度（契约 `dimension`） |
|---|---|---|
| `retrieval_miss` | 消息中出现已有定义的词项或别名，但 browse 无命中（需要 §2.2 的记录） | `semantic_content`（别名或描述） |
| `ambiguity_unresolved` | resolve 返回歧义（第 67 行），Run 随后失败或转人工 | `semantic_content` |
| `context_dropped` | 所需语义条目因预算被省略（`omitted_reason`；`context_engine/budget.py:102` 的 `semantics_low_relevance`） | `semantic_tool`（返回形状或预算策略） |
| `tool_misuse` | 工具调用失败或拒绝码集中在同一工具，且与语义无关的原因（权限、开关、预算）已排除 | `semantic_tool` |
| `definition_gap` | 抽取侧反复出现未匹配的候选定义（`nexloop_candidate_definitions`），且审核拒绝合并到已有定义 | `ontology_schema`（只形成 EIOS Schema proposal 的输入，不自动发布） |
| `not_semantic` | 失败原因是权限、开关、预算、外部效应或模型错误 | 不生成候选，只计数 |

- **排除顺序**：先排除 `not_semantic`，再归入语义类。拒绝码、预算与开关失败一律不归因到语义。
- **证据要求**：每次归因都要附带：
  - 证据链：Run ID、context_id、相关 `context_sources` ordinal、model_request call_sequence、拒绝码；
  - 简短理由。
  - 不要求隐藏推理（AGENTS.md）。

### 3.2 候选结构

- **契约**：沿用 `evolution-candidate.schema.json`，不修改契约：
  - `dimension`：semantic_content、semantic_tool、ontology_schema；
  - `status`：proposed、evaluating、rejected、accepted_for_release、incomplete；
  - `publication_permit_ref` 恒为 null。
- **一个候选只含一个主要假设**（`primary_hypothesis`，08 §5）。
  - `allowed_change_scope` 列出允许改动的定义或别名 ref；
  - `patch_artifact_ref` 指向本地 Artifact 中的补丁，不在库中保存补丁正文。
- **用例集**：
  - `design_case_ids`：诊断依据的 Run 或用例 ID；
  - `validation_case_ids`：留出集 ID，按消费者或会话簇分组，避免泄漏（AT-048；14 第 94 行）。
  - 两者只存 ID。
- **预算**：
  - `maximum_rounds` 加模型费用上限：每个候选、每轮诊断都有上限，按 D-g 同样的方式记账（`runtime.nexloop_cost_entries`，data_mode real，cost_kind `model`，source_ref 指向候选）；
  - 预算耗尽时转为 `incomplete`，保留 Parent（08 §5），不伪造评分。
- **候选隔离（AT-023）**：
  - 候选存放在独立的候选表或 Artifact 中，绝不写 active 定义、别名表或 `ontology.object_type_versions`；
  - 未通过评估的候选不影响 active 定义与正式对象。
- **结构权限（AT-024）**：`ontology_schema` 维度的候选只产出 EIOS Schema proposal 的输入。
  - 它不能包含 Action 定义、授权事实或 service grant；
  - 定义发布与权限发布分离，由 EIOS 治理流程和负责人分别处理。

## 4. 与 EvoOntology 的接口

### 4.1 现状

- **锁定版本**：`versions.lock.json` 中 `upstream_sources.evoontology` 为 commit `f64413dae88d88645b1f2c069cf4e17308ad0f89`，integration `selective_reuse`；`packages.evoontology` 为 `installed: false`（source_version 1.1.0，MIT）。
- **仓库中没有任何 Evo 代码**：
  - `context_engine/semantic.py` 只借用了输出形状（第 1–7 行），没有使用或维护 Evo 存储；
  - 08 §2 说明了各上游文件对应的处理方式。

### 4.2 安装与接口核对步骤（NX-038 实现的第一步）

1. **获取来源**：只读获取锁定 commit 的源码，放在独立目录，不进入仓库历史。核对以下三项与 lock 一致：commit、MIT LICENSE 内容 hash、`source_version`。
2. **核对实际 API**：
   - 对照 08 §2 核对 `runtime/runtime.py`、`ontology/models.py`、`evolution/session.py`、`evolution/adapter.py`、`evaluation/evaluation.py`、`validate.py` 在该 commit 的签名；
   - 记录实际类、函数、字段与 08 的假设之间的差异（AGENTS.md：“Verify EIOS/Pi APIs at frozen commits before writing adapters”）。
3. **选择复用方式**（D3）：
   - 推荐**选择性移植**：把 `evolution/session.py` 的状态机和 `evaluation/evaluation.py` 的对比逻辑移植到 `packages/eios-core` 或独立包；
   - 每个移植文件在 `versions.lock.json` 的 `upstream_sources` 中逐文件登记：原路径、目的路径、内容 hash、修改原因、测试映射（08 §7）；
   - 第三方许可写入 THIRD_PARTY_NOTICES（13 第 39 行）。
4. **不移植的部分**（08 §3）：
   - JSON 版本目录存储；
   - MCP 管理操作；
   - 主机脚本执行能力；
   - 任意 workspace 选择。
   - evolve/build Skills 改写为受限后台任务提示，不赋予修改代码或发布的权限。
5. **建立契约测试**：以移植后的状态机对 `evolution-candidate.schema.json` 做往返测试，确认 `proposed → evaluating → accepted_for_release | rejected | incomplete` 的转移与上游语义一致，并把差异写进 provenance。
6. **安装完成后**：把 `packages.evoontology.installed` 改为 true，并记录实际移植范围。这是 lock 的变更，走 PR，不在本稿中执行。

### 4.3 adapter 接口（NexLoop 侧）

- **`TraceSource`**：按冻结快照返回只读轨迹记录（§2）。
- **`Diagnoser`**：轨迹 → 归因（§3.1）。规则部分是确定性的；模型辅助部分只能看到 ID、digest 和允许的元数据（§6）。
- **`CandidateProposer`**：归因 → 候选（§3.2）。只写入候选存储。
- **`EvaluationAdapter`**（上游 `evolution/adapter.py` 的位置）：给 NX-039 用，在同一用例集上比较 Parent 与候选。
- 四者都不持有发布、业务写入或效应凭据。

## 5. 人工审核路径（复用 S3）

- **复用的机制**：S3 已有人工审核链：
  - `ontology.nexloop_review_decisions`：0082_nx044_review_actions.sql:9，decision 取值 approve、merge_into、reject，outcome 取值 rejected、merged、published、publication_failed，带 reflow；
  - `review-decision.schema.json`；
  - ADR-019：v0.1 起别名与映射更新由人工审核驱动。
- **不同路径如何复用**：
  - `semantic_content` 类候选（别名、描述、映射）在通过 NX-039 评估后进入同一审核队列：
    - 审核结论用同一个受治理的人类 Action 记录；
    - 发布仍走现有别名与映射发布及 reflow；
    - 审核员身份由服务端派生。
  - `ontology_schema` 类候选不走这个队列发布，只作为 EIOS Schema proposal 的附件进入 Schema 维护流程（08 §4；PRD M12 关键规则）。
- **审核记录关联**：审核记录需要能关联到演进候选。
  - 推荐在审核 record 中引用 `candidate_id`（evolution）、评估报告 digest 和 NX-039 门槛结果；
  - 是否扩展 `review-decision` 契约或新增关联表，见 D4。
- **停止与回退**：负责人可以随时停止某个维度的演进（08 §6 v0.2a“保留人类停止/回退入口”）。语义版本回退不自动倒退真实业务事实（PRD M12 验收）。

## 6. 隐私

- **Claim 值默认不读**：
  - 诊断默认不读 `ontology.nexloop_claims` 的值（value、subject.text、source.quote、condition），只用 ID、epistemic_kind、resolution_state 与计数；
  - 读取值必须有登记白名单：按 predicate 或候选维度逐项登记，并说明用途、负责人批准和有效期（D5）；
  - 与 AT-064 的边界一致：Context 和 Role 包不读 Claim 的结构化值，见 `tests/test_claim_store_pg.py` 第 235 行起的 AT-064 断言。
- **消息原文**：诊断记录、候选与评估报告中不出现消息原文，只用 content_hash 与 ID。
  - 模型辅助诊断需要看文本时，只能看登记白名单内、经脱敏的片段；
  - 每次看到的内容都要记录 `model_request` 快照（M14 验收：可核对实际发送的内容）。
- **假设层**：`epistemic_kind=hypothesis` 的内容不作为归因依据（AT-064）。
- **删除传播**：见 §2.4。候选和留出集只存 ID 与 digest，删除后失效，不保留可复原的内容。
- **跨租户**：诊断、候选与评估都限定单租户。跨租户聚合（例如通用别名）不在 v0.2a 范围内（D6）。

## 7. 评估与不回归门槛（交给 NX-039，本任务负责冻结）

NX-038 产出候选时，需要一并冻结评估条件，NX-039 不得临时更改（08 §5“评分相等是否接受由任务定义，不允许 Agent 临时改变规则”）：

- **硬门槛**（14 §6，第 94 行；08 §5 发布门槛），任一失败即拒绝，与评分分开（M22 验收）：
  - 引用与结构有效；
  - 零越权、零跨租户、零事实污染；
  - 没有禁止的行为回退（例如拒收后的联系、必需约束丢失）；
  - 索引与迁移影响检查通过（ontology_schema 维度）。
- **成本与性能**：候选的模型费用、上下文 token、端到端时延不超过冻结上限。时延分解沿用 NX-031 §4.2。
- **改善标准**：在留出集上改善，或达到预设的非劣标准（D7），并重复评估。留出集按消费者或会话簇分组（AT-048）。
- **确定性语义回归**：08 §6 v0.1 要求的语义回归必须保持全部通过。

## 8. 验收映射

| 验收 | NX-038 的覆盖 |
|---|---|
| AT-023 候选隔离 | 候选从不写 active 定义、别名表或正式对象；未通过的候选不影响 active。测试：候选生成前后 active 定义与对象的 digest 不变 |
| AT-024 结构权限 | ontology_schema 候选中出现 Action、授权或 grant 内容时直接拒绝。定义与权限分别发布 |
| AT-048 验证泄漏 | 冻结时按消费者和会话簇分组。测试：构造同一消费者的相关对话跨分组，检测并重新分组 |
| M22 可复现 | 同一 `input_snapshot_digest` 得到同一诊断与候选集合 |
| AT-046（负例） | 诊断角色没有效应凭据。回放不调用任何效应入口 |

## 9. 需要负责人决定的事项

【负责人 2026-10-12 定案：D1–D9 全部按推荐。】

| 编号 | 问题 | 推荐 | 代价 |
|---|---|---|---|
| D1 | 诊断以什么身份读取 | 新建只读数据库角色 `nexloop_evo_reader`，只能执行 §2.3 的只读函数，并以服务凭据（world real）认证 | 多一个角色与凭据要管理 |
| D2 | 语义访问逐次记录（§2.2）是否在 NX-038 内新增 | 是：由 Context Engine 只追加写入，只存词项哈希，纳入 NX-029 保留期（建议 90 天） | 一次迁移，加上 Context Engine 写入路径的改动 |
| D3 | EvoOntology 复用方式 | 选择性移植状态机与评估对比，逐文件登记 provenance；不安装上游包作为运行时依赖 | 上游修复需要手工 cherry-pick |
| D4 | 审核记录如何关联演进候选 | 不改 `review-decision` 契约，新增关联表（decision_id ↔ evolution candidate_id、评估报告 digest） | 多一张表；契约改动更直接，但需要提案 |
| D5 | Claim 值白名单 | 默认不读；白名单按 predicate 登记，每项需负责人批准、写明用途与有效期（建议 90 天），到期自动失效 | 诊断能力受限，部分 definition_gap 只能靠计数判断 |
| D6 | 跨租户聚合 | v0.2a 不做 | 小租户样本少，候选更难过非劣门槛 |
| D7 | 非劣标准 | 设计集与留出集均不劣于 Parent（差值 ≥ −1 个百分点），且硬门槛零失败；评分相等时不接受，候选维持 `rejected` | 保守，接受的候选会变少 |
| D8 | 诊断触发频率与预算 | 每周一次，或累计 200 个已结束 Run 时触发；每次诊断模型费用上限 ¥20，每个候选最多 3 轮 | 改进节奏慢 |
| D9 | 模型辅助诊断 | v0.2a 先只用确定性规则（§3.1）；模型辅助诊断等 D5 白名单与脱敏方案落地后再开 | 一部分归因只能落到 `not_semantic` 或保持未分类 |

## 10. 不在本任务范围

- 候选评估的执行、语义版本发布与回退：NX-039。
- 正式 Schema 的自动发布：v0.2b，见 08 §6。
- 模拟世界与实验：v0.3。
- 修改 `packages/contracts/*.schema.json`：如需改动，按 D4 另提提案。

## 11. 修订（2026-10-18）：NX-037 轨迹作为输入

本节只补充 10-12 定稿之后才出现的输入，不改变 §1–§10 和已定案的 D1–D9。

**样本**：NX-037 试运行 `4f5d795c`（发布 10）的轨迹。本节只使用它的**证据包结构**（`scripts/nx037/evidence_pack.py`，schema `nexloop.nx037-evidence.v1`）和 SQL 证据函数的**字段定义**。真实记录留在 CI 主机的私有证据目录，本稿不读取、不引用任何值。

### 11.1 新增的轨迹字段

| 来源 | 字段（全部是 ID、代码、计数、时间或 digest） | 诊断用途 |
|---|---|---|
| `runtime.nexloop_nx037_run_evidence`（当前定义在 0208；0204 加入 `failure`） | `model{calls,ms,cost,unfinished,failed,unknown}`、`tool{calls,ms,max_ms}`、`costs`、`intents`（按状态计数）、`intent_ids`、`refusals`（按拒绝码计数）、`failure{code,failure_code,failure_detail,failure_tool}`、`recovered_tool_errors[{tool,code}]` | Run 结局、失败落在哪个工具、模型自行纠正过哪些工具错误、派发被拒的原因 |
| 试运行记录（`interactions.jsonl` → 证据包 `interactions/records.json`） | `kind`（五类场景）、`outcome`、`error_code`、`time_ms{end_to_end,model,tool,platform}`、`model`、`tool_calls`、`cost`、`intents`、`intent_ids`、`refusals`、`failures`、`recovered_tool_errors`、`receipts`、`reconciliation`、`facts`、`persona`、`driver`、`test_stack`；作废记录另在 `voided` | 把 Run 证据放回“交互”语境：场景是什么、期望行为是否发生、收据与对账是否了结 |
| 收据与对账 | `receipts`（交付收据 ID）、`reconciliation`（`effect_reconciliation` 的了结状态） | 区分“工具没调对”和“外部结果未知或待核对” |

- 这些字段在写入时已经只允许 ID、代码、数字、时间和固定词表（`evidence_pack.check`），所以可以直接作为诊断输入，不需要额外脱敏。
- §6 的隐私规则不变：诊断仍然不读消息原文和 Claim 值。

### 11.2 两条输入路线（D10）

| 路线 | 做法 | 适用 |
|---|---|---|
| 在库路线 | 诊断角色（D1，`nexloop_evo_reader`）调用新的只读函数，按 Run 返回与 `nexloop_nx037_run_evidence` 相同的字段。不复用 `nexloop_metrics` 的授权，每次读取都按租户和 world 限定 | 正式诊断 |
| 证据包路线 | 导入一个证据包的 `records.json`：先用 `evidence_pack.check` 复查只含允许值，以证据包的 sha256 作为 `input_snapshot_digest`，然后按 §2.3 冻结 | 设计期与回归用例，例如用 NX-037 证据包的**结构**生成合成样本，做确定性归因回归 |

- 两条路线输出同一种 `TraceSource` 记录（§4.3），归因规则只写一套。
- 证据包路线不连库，也拿不到任何凭据。

### 11.3 归因规则的补充（接 §3.1）

在 §3.1 的排除顺序之前，先过滤以下记录（只计数，不归因）：

- `test_stack=true`；
- 已作废的记录；
- `driver` 不是 `real`、且由负责人标为演练的试运行。
- `persona=simulated_agent`（负责人操作的 AI 顾客）不过滤，但单独标注，按 D12 处理。

各信号的归因：

| 信号 | 归因 |
|---|---|
| `refusals` 中的 NXC（控制或暂停）、NXB（预算）码；真实派发开关关闭；`interaction_timeout`；`reconciliation` 未了结、意图状态为 unknown | `not_semantic` |
| 场景 `restricted_after_refusal` 或 `human_takeover` 中**没有发送** | 这是期望行为，不算失败 |
| 同一场景里**发送了**（`intents` 出现 accepted 及之后的状态） | 严重的行为回退，交给安全回归，不进演进候选 |
| `failure.failure_tool` 指向同一工具，`failure_code` 为 `invalid_effect_request` 一类参数错误，且排除了权限、开关、预算 | `tool_misuse` → `semantic_tool`（工具描述或参数说明） |
| `recovered_tool_errors`：Run 成功，但模型先犯错再自行纠正 | 弱信号。只在达到 D13 的阈值后，才对同一 `tool` 形成 `semantic_tool` 候选 |
| `failure_tool=unregistered_tool` | 模型调用了不存在的工具。按 `semantic_tool` 计入，假设方向是“工具清单或说明不清”，不是增加工具 |

每次归因附带的证据链（§3.1）补充：`interaction_id`、`kind`、`failure_tool`、`failure_code`、`recovered_tool_errors` 的序号。

### 11.4 候选不是业务事实；工具类候选的审核

- §3.2、§5 不变：候选是隔离工件，只进候选存储；审核结论由受治理的人类 Action 记录；不写 active 定义、别名表或业务对象。
- **新增的工具类候选**：`semantic_tool` 维度的产出，可能落在 Agent Host 工具清单的描述或参数说明上。
  - 工具清单属于代码与配置，所以这类候选通过 NX-039 后，只生成一份**变更提案**：附评估报告 digest 和门槛结果，进入审核队列，再按正常的代码评审和发布流程改动。
  - 候选本身从不修改工具清单、提示词或 Agent Host 配置。
  - 同一个工具的工具调用授权、效应上限和联系约束都不在候选可改的范围内：`allowed_change_scope` 只允许描述文字和参数说明，提权内容按 AT-024 一律拒绝。

### 11.5 与 EvoOntology 的边界（补充 §1、§4）

- 轨迹诊断的归因、候选状态机与评估对比，按 D3 选择性移植。Evo 的存储不保存任何轨迹副本：快照只存 ID 与 digest（§2.3、§2.4），轨迹本身留在 PostgreSQL 或私有证据目录。
- Evo 的 Term、Mapping 只作为语义视图，不能用来记录消费者事实，也不能作为工具调用的依据。
- 候选的任何结论，都要经过 EIOS 受治理发布（语义版本、别名与映射发布）或代码评审，才会生效。

### 11.6 契约与迁移（汇总，实现时用临时编号）

| 项 | 内容 | 来源 |
|---|---|---|
| 迁移 1 | 角色 `nexloop_evo_reader` 与只读轨迹函数（§11.2 在库路线），只返回 §11.1 的字段 | D1、D10 |
| 迁移 2 | 语义访问逐次记录表（§2.2），纳入 NX-029 保留期 | D2 |
| 迁移 3 | 演进候选表与事件表（`evolution-candidate` 契约的落库），以及审核关联表（decision_id ↔ candidate_id、报告 digest） | §3.2、D4 |
| 契约 | `evolution-candidate.schema.json` 不改。新增 `trajectory-sample.schema.json`：把证据包的记录结构（`nexloop.nx037-evidence.v1` 的 records 部分）收为正式契约，`TraceSource` 两条路线共用；单独 PR | D11 |
| 费用 | 沿用 `runtime.nexloop_cost_entries`，cost_kind `model`，source_ref 为 `evolution-candidate:<id>`（§3.2） | 不需要迁移 |

### 11.7 新增的负责人决定项

| 编号 | 问题 | 推荐 | 代价 |
|---|---|---|---|
| D10 | 轨迹输入路线 | 正式诊断只走在库路线；证据包路线只用于设计期与确定性回归用例，并且只用结构生成合成样本 | 用不上已有证据包里的真实分布，要等库内积累 |
| D11 | 是否把证据包的记录结构收为正式契约 | 是：新增 `trajectory-sample.schema.json`，单独 PR；`evidence_pack.check` 与 `TraceSource` 都按它校验 | 多维护一个契约，记录字段变更要走契约流程 |
| D12 | 负责人操作的 AI 顾客（`persona=simulated_agent`）的轨迹 | 可用于语义和工具诊断（系统本身是真实运行的），但打上标注，不进入用于证明“真实效果改善”的留出集；`test_stack` 的轨迹一律排除 | 早期真实顾客少，留出集更小 |
| D13 | `recovered_tool_errors` 形成候选的阈值 | 同一工具、同一错误码，在冻结窗口内至少出现 3 次，且涉及至少 2 个不同消费者 | 少见的问题要等更久 |
| D14 | 试运行 `4f5d795c` 的真实轨迹何时可以进入正式诊断 | 本稿只用结构。首次用真实轨迹做正式诊断，需要负责人届时单独批准：确认删除传播、保留期和 D12 的标注都已就位 | 多一次审批 |

## 12. 修订（2026-10-20）：S6 起步

本节由 L4 在 v0.1.0 之前补写，起点是 `origin/integration/v010-docs-20261020`（15f071c0），分支 `l4/nx038-design`。不改 §1–§11，也不改已定案的 D1–D14。补的是 S6 开工前还缺的五块：

- 预算与成本上限的落地方式；
- 候选评估的隔离：影子运行与模拟世界；
- 与 NX-039 的接口；
- 权限与删除的补充；
- 实施切片与工作量。

### 12.1 10-18 之后出现的新事实

| 事实 | 位置 | 对本设计的影响 |
|---|---|---|
| 召回质量函数与 doctor 检查 | 0221 `authz.nexloop_recall_quality`；`nexloop-doctor --recall-quality` | 按类型给出向量覆盖（没有向量的条目数）和语义近邻。`retrieval_miss` 在 D2 的逐次记录落地之前，可以先用它作为聚合输入（只用计数与 ID） |
| 测试人设标记 | 0220 `control.nexloop_consumer_personas`（ADR-034），stage 已把 4 个合成顾客标为 `simulated_agent` | D12 可以在库内执行：在库路线（§11.2）按人设标记筛选，不必只依赖证据包字段 |
| 模拟凭据隔离 | 0165 与 AT-046 前置（`docs/implementation/AT-046-simulation-credential-guard.md`） | 凭据只要含 `real` 就只能含 `real`；runtime worker 和 guard 只接受 `real` 或 `test`，`simulation` 和 `shadow` 在参数阶段就被拒绝。所以候选评估若要真的跑 Run，只能在 `test` world，见 §12.3 |
| EvoOntology 冻结核对与来源审计 | `docs/implementation/evo-ontology-frozen-check.md`、`evo-ontology-source-audit.md` | §4.2 的第 1、2、4 步已经完成。结论：语义访问不移植代码；状态机只移植规则，用 PG 重写；`EvaluationGate` 原样移植，外层加门槛。还剩第 3、5、6 步，在实现切片 E1 中完成 |
| 世界模型 W1–W4（冻结链，v0.1.0 之后集成） | 工作台“企业世界模型 / 能力目录 / 数据资产” | 演进候选可以在知识工作台和企业世界模型页展示（只读）。这不在 NX-038 范围，列为 NX-039 之后的界面项 |

### 12.2 预算与成本上限

**三层上限**，任一层用尽，候选或诊断就转为 `incomplete`，原因 `budget_exhausted`（移植的状态机规则：预算冻结后不能在途追加，只能另起一轮并经负责人批准 `extend_budget`）：

| 层 | 上限（推荐值） | 计量 | 检查时机 |
|---|---|---|---|
| 每次诊断 | 模型费用 ¥20（D8）；墙钟 30 分钟；参与 Run 不超过 2000 个 | `runtime.nexloop_cost_entries`，cost_kind `model`，source_ref 为 `evolution-diagnosis:<id>` | 每次模型调用前 |
| 每个候选 | 最多 3 轮（D8）；模型费用 ¥10；设计用例和留出用例各不超过 256 个（契约上限） | source_ref `evolution-candidate:<id>` | 每轮开始前，以及每次模型调用前 |
| 每个租户每月 | 演进总费用 ¥100（D15） | 演进类 source_ref 的月度合计 | 每次模型调用前，在 SQL 中判定 |

- **为什么单独设月上限**：运营 Run 用的模型预算（`control.nexloop_budget_limits` 的 `model` 类，D-g 的累计上限）不能被演进消耗。演进花多了，会让真实顾客的回复先被预算拒绝（NXB01）。
- **推荐实现（D15）**：在 `budget_kind` 增加 `evolution`。负责人仍通过现有的受治理 `Budget.set` 设置；演进的模型调用只计入并检查这一类，不计入 `model`。这需要一个迁移，放宽 0068 的 check 约束。
- **确定性层（§12.3 的 L0）没有模型调用**，只受用例数与墙钟限制。
- **不允许伪造**：预算耗尽时保留 Parent，不给评分，不把未完成的评估当作“未劣化”（08 §5）。

### 12.3 候选评估的隔离：影子运行与模拟世界

**原则**：候选内容永远不写入 active 定义、别名表、工具清单或业务对象（AT-023）。评估时，候选只以“叠加层”的方式被读到，而且只能在非 real 的评估会话中读到。

**叠加层（overlay）**：

- 候选补丁（`patch_artifact_ref`，本地 Artifact）只在评估会话中加载。
- Context Engine 的语义读取接受一个 `evaluation_overlay`：候选 ID 加补丁 digest。SQL 侧核对：会话 world 不是 `real`，且候选状态为 `evaluating`、属于同一租户。任一不符就拒绝，读取回到 active。
- real world 的会话永远不能带叠加层，在函数入口就拒绝。这一条是 AT-023 的第二道防线：即使代码误传，real 的 Run 也看不到候选。

**三个评估层级**：

| 层级 | 做法 | 用到 Run 或模型吗 | 适用维度 | 在 NX-038 中 |
|---|---|---|---|---|
| L0 确定性回放 | 冻结用例的语义访问回放：同一批 browse/resolve 查询分别对 Parent 和“Parent + 叠加层”求覆盖率、歧义与命中 ref。D2 的逐次记录正好提供查询词项哈希与期望 ref | 都不用 | `semantic_content` | 实现 |
| L1 测试 world 模型回放 | 在 `test` world 用专用评估凭据（只含 `test`，没有任何效应授权）跑 Run，Context 带叠加层，用例是合成或由结构生成的（D16） | 用模型；Run 在 test world | `semantic_tool`、需要模型的 `semantic_content` | 只定接口与隔离，执行归 NX-039 |
| L2 真实流量影子 | 对真实 Run 并行生成“候选版本会怎么做”，只记录，不派发 | 用模型；需要 shadow 运行时 | 全部 | **不做**：runtime worker 不接受 `shadow`（0165）。按 14 §6，影子只记录拟采取的动作；它属于 NX-043（S7） |

**不调用真实效应**（AT-046 负例在 S6 的部分）：

- L1 的 Run 在 test world 中：效应执行入口只认 real；外部效应开关对非 real world 关闭（0153 `closed_layer` 返回 `world`）；凭据又不能跨 world（0165）。即使模型伪造参数，也到不了任何外部渠道。
- 模拟世界（Twin）与 AT-046 全文属于 S7。NX-038 不使用 `simulation` world。

**不复制真实内容**：L1 不把真实消息复制进 test world。用例只来自合成样本，或由真实轨迹的**结构**生成（§11.2 证据包路线）。用真实内容做 L1，需要 D5 白名单、脱敏，以及负责人的单独批准（D16）。

### 12.4 与 NX-039 的接口

**交接点**：NX-038 产出状态为 `proposed` 的候选，加一份冻结的**评估包**；NX-039 接手 `proposed → evaluating → accepted_for_release | rejected | incomplete` 之后的全部转移。NX-038 自己不写 `evaluating` 之后的任何状态。

**评估包（提案契约 `evolution-evaluation-bundle.schema.json`）**：只含 ID、digest、版本与数字：

| 字段 | 含义 |
|---|---|
| `candidate_id`、`parent_revision`、`candidate_revision`、`patch_digest` | 比较对象 |
| `input_snapshot_digest` | §2.3 的冻结快照 |
| `design_case_ids`、`validation_case_ids`、`case_groups` | 用例与分组：每个用例所属的消费者簇或会话簇 ID，供 AT-048 检查 |
| `hard_gates_version`、`hard_gates` | §7 的硬门槛清单（引用、结构、零越权、零跨租户、零事实污染、无禁止的行为回退、索引与迁移影响） |
| `metrics`、`non_inferiority` | 指标定义与非劣标准（D7：差值 ≥ −1 个百分点，相等不接受） |
| `levels` | 允许的评估层级（L0，或 L0 加 L1） |
| `budget_remaining` | §12.2 三层上限的剩余额度 |
| `evaluator_version`、`model_profile`、`anonymize_seed` | 可复现：A/B 匿名化的种子，或逐条记录 swap 结果（frozen-check §2.3 指出上游用 `random.random()`） |

**评估报告（提案契约 `evolution-evaluation-report.schema.json`）**：

- 逐条硬门槛：pass 或 fail，附证据 ID；
- 设计集与留出集上 Parent 和候选的分数；
- swap 记录、费用、耗时；
- 结论：accept、reject、incomplete 及原因。

报告的 digest 写入候选事件。审核关联（D4 关联表）引用这个 digest。

**NX-039 不得改动**评估包里冻结的任何条件（08 §5）。条件需要变化时，NX-038 重新冻结，得到一个新候选。

### 12.5 权限、数据与删除（补充 §2.3、§2.4、§6）

- **角色**：
  - `nexloop_evo_reader`（D1）：只读轨迹函数；
  - 候选的写入只能通过 security definer 函数：以服务凭据认证，world `real`，用途 `evolution`。函数只能写候选表与事件表，不能写定义、别名、授权或业务对象。
  - 两者都不持有模型、渠道或效应凭据之外的东西。L1 的模型调用走 test world 的 runtime 路径（§12.3）。
- **可见性**：候选表按租户启用 RLS。工作台只读：负责人可见；审核人通过审核队列可见（ADR-025 的成员读取派生）；运营不可见。
- **补丁可能带顾客措辞**：`retrieval_miss` 的别名候选可能来自顾客的说法。因此补丁 Artifact 要登记来源 Consumer 的引用（只存 ID）。来源 Consumer 被删除时（`control.nexloop_deletion_ledger`）：
  - 删除对应补丁 Artifact；
  - 候选转为 `incomplete`，原因 `missing_data`（移植的合法原因之一）；
  - 已经 `accepted_for_release` 但还没发布的候选同样处理。
  - 已经发布的别名，走现有的别名删除与 reflow（NX-029、NX-045），不在演进内处理（D17）。
- **快照失效**：删除日志新增条目时，涉及该主体的冻结快照和留出集全部标为 `incomplete`，不在旧快照上继续评估（§2.4）。

### 12.6 S6 验收覆盖

S6 的验收是 AT-023、AT-024、AT-048（AT-046 在 S7，这里只覆盖它在服务端的负例）。

| 验收 | 怎么覆盖 | 测试形态 |
|---|---|---|
| AT-023 候选隔离 | 候选只进候选表和 Artifact；叠加层只在非 real 评估会话中生效（§12.3）；未通过的候选不改变 active | PG：生成候选并跑完 L0 评估后，active 定义、别名表、`ontology.object_type_versions` 和业务对象的 digest 都不变；real 会话带叠加层被拒绝 |
| AT-024 结构权限 | `ontology_schema` 和 `semantic_tool` 候选的 `allowed_change_scope` 只允许描述、别名和可选属性等内容；出现 Action 定义、授权事实、service grant 或工具授权就拒绝；定义发布与权限发布分开 | PG：构造“新增类型并附带提权 Action”的候选，写入函数拒绝；即使候选通过评估，生成的 Schema proposal 里也没有任何授权内容 |
| AT-048 验证泄漏 | 冻结时按消费者簇和会话簇（同一消费者的相关会话为一簇）分配设计集与留出集；评估包携带 `case_groups`，NX-039 开始前复查 | 单元与 PG：构造同一消费者的相关对话分落在两边，冻结函数检测到后重新分组，并在报告中记录重分组次数；留出集冻结后不可修改 |
| AT-046（S7，负例部分） | 诊断与评估没有真实效应凭据；L1 只在 test world 运行 | 复用 `tests/test_at046_simulation_isolation_pg.py` 的断言，加上评估凭据只含 `test` 的断言 |

### 12.7 实施切片与工作量

| 切片 | 内容 | 迁移与契约 | 估计 |
|---|---|---|---|
| E1 移植与契约测试 | §4.2 第 3、5、6 步：移植状态机规则与 `EvaluationGate`，逐文件登记 provenance 和 THIRD_PARTY_NOTICES，做 `evolution-candidate` 往返测试 | lock 变更（走 PR）；不改契约 | 1.5 天 |
| E2 轨迹读取 | `nexloop_evo_reader` 加只读轨迹函数（§11.2 在库路线），证据包路线，`TraceSource`，人设标记筛选（§12.1） | 迁移 1；契约 `trajectory-sample`（D11） | 2 天 |
| E3 语义访问逐次记录 | D2：Context Engine 只追加写入，纳入 NX-029 保留 | 迁移 2 | 1.5 天 |
| E4 归因与候选 | §3.1、§11.3 的确定性规则；候选表与事件表；三层预算（§12.2）；冻结与 AT-048 分组；删除失效（§12.5） | 迁移 3；`budget_kind evolution`（D15） | 3 天 |
| E5 隔离与交接 | 叠加层（§12.3）；L0 确定性回放；评估包与报告契约；审核关联表（D4） | 迁移 4；契约 bundle 与 report | 2.5 天 |

- 合计约 **10.5 个开发日**，大约 5 轮 CI。
- 顺序：E1 → E2 → E4 → E3 → E5。E3 可以与 E4 并行；E5 依赖 E4。
- 每个切片单独交付，迁移用临时编号。

### 12.8 契约与迁移（更新 §11.6）

| 项 | 内容 | 状态 |
|---|---|---|
| 迁移 1 | `nexloop_evo_reader` 与只读轨迹函数 | 同 §11.6 |
| 迁移 2 | 语义访问逐次记录表 | 同 §11.6 |
| 迁移 3 | 候选表与事件表、审核关联表；`budget_kind` 增加 `evolution`（D15） | §11.6 加预算 |
| 迁移 4 | 叠加层读取：Context Engine 语义读取函数增加 `evaluation_overlay` 参数，并在入口拒绝 real world | 新增 |
| 契约 | `trajectory-sample.schema.json`（D11）；`evolution-evaluation-bundle.schema.json`、`evolution-evaluation-report.schema.json`（§12.4）；`evolution-candidate.schema.json` 不改 | 只是提案，每个契约单独 PR，本稿不改 `packages/contracts/` |

### 12.9 新增的负责人决定项

【负责人 2026-10-20 已决定：D15–D20 全部按推荐。】

| 编号 | 问题 | 推荐 | 理由 | 代价 |
|---|---|---|---|---|
| D15 | 演进费用的预算口径 | 新增 `budget_kind evolution`，每个租户每月 ¥100，由负责人经 `Budget.set` 设置；演进的模型调用只计入这一类 | 运营模型预算不被演进挤占，避免真实回复先被 NXB01 拒绝；沿用已有的受治理设置与派发时检查 | 一个迁移；负责人多管一项预算 |
| D16 | L1 模型回放能不能用真实轨迹内容 | 不能。L1 只用合成样本或由结构生成的用例；用真实内容需要 D5 白名单、脱敏和负责人的单独批准 | 不把真实消息复制进 test world；与 §6、D14 一致 | 早期语义工具类候选的评估代表性有限 |
| D17 | 补丁中可能带顾客措辞，删除如何传播 | 补丁登记来源 Consumer 引用；删除时连带删除补丁，候选转为 `incomplete/missing_data`；已发布的别名走现有删除与 reflow | 候选不能成为删除的漏网之处 | 多一张引用表 |
| D18 | 评估在哪个 world 运行 | `test` world 加专用评估凭据（只含 `test`）。不为 NX-038 增加 `shadow` 运行时；影子留给 NX-043 | 现有隔离（0165、0153）已覆盖 test world，不必扩大 runtime 接受的 world | 不能在真实流量上比较 |
| D19 | 首批开放的演进维度 | 先只开 `semantic_content`（L0 即可评估）；`semantic_tool` 等 L1 和 D16 就绪后再开；`ontology_schema` 只产出 EIOS Schema proposal 的输入 | 从风险最低、可以确定性评估的维度开始 | 工具描述类的改进要晚一些 |
| D20 | 三层预算的推荐值 | 每次诊断 ¥20 / 30 分钟 / 2000 个 Run；每个候选 3 轮 / ¥10；每个租户每月 ¥100 | 与 D8 一致；按 NX-037 试运行的单次调用费用量级估算，上限足够跑 L0 加少量 L1 | 数值保守，可按实际用量调整 |
