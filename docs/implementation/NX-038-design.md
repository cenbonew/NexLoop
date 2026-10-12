# NX-038 设计稿：Evo 轨迹诊断与候选任务

- **状态**：设计稿，只有文档。本稿不写代码、不加迁移。D 项待负责人决定。
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
