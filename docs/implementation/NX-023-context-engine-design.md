# NX-023 设计稿：Context Engine 与语义只读 adapter

状态：设计稿（只读 + 文档），不含产品代码。基线 main `23332dc`（迁移至 0081）。代码实现要等 L1 阶段 B 合入（它改 `runtime_activation.py`、`effect_intents.py`、`effect_execution.py` 与 v5 Context），见 §11。

依据：docs/handoff/docs/05（§1–§4、§7、§9）、ADR-019 决定 2/5、ADR-020 §1/§2、`packages/contracts/context-manifest.schema.json`、现有 Context v2/v3/v4（`context_pack.py`、`role_context_pack.py`、`relationship_context_pack.py` 及 0052/0053/0063/0076 绑定链）、L1 报告（阶段 A/B、O1 批量授权建议）、NX-021 召回、NX-022 目标与控制 revision、NX-047 外发消息、NX-019 Claim 契约。任务：NX-023（依赖 NX-021、NX-022、NX-003）；验收：AT-027、AT-028、AT-064，并为 AT-029（NX-024）与 AT-003/AT-025（world/权限）提供前提。

## 1. 现状与差距

| 已有 | 作用 | 与 NX-023 的差距 |
|---|---|---|
| Context v2（0052/0053，`context_pack.encode_pack`） | 消息 Run：当前 Message 原文、4 个正式对象 id/revision、当前控制、供给目录 | 固定 4 个事实；无目标版本、无问题/承诺、无证据检索、无预算裁剪；不是 Manifest |
| Context v3（0063，Role） | Role 绑定 + 服务触发 | 同上，另加 Role 定义 |
| Context v4（0076，关系） | ≤4 条 RelationshipAssessment，current_statements 与 evidence 分区 | 只覆盖关系评估；分区语义正是 ADR-019 决定 5 的雏形 |
| Context v5（L1 阶段 B，未合入） | Role 执行策略 `role_policy` 段（ceiling/scope/budget/effect_units，`grants_authority:false`） | 策略约束是本稿“Action 与约束”层的一部分 |
| `context-manifest.schema.json` | 每次模型调用的来源、版本、hash 契约 | **尚无任何写入方**；`call_sequence`、`request_digest`、`prompt_artifact_ref` 没有落库 |
| Host（`pi-runtime-adapter.ts`） | 每次模型/工具调用前 `authorize(command,'model'|'tool')` | 授权时不上报实际请求，无法做 AT-027 |

结论：现有 v2–v5 是“为单一场景固定形状的 pack + SQL 端整包重建对比”。NX-023 要的是**可配置、可版本化、可测量**的组装器（docs/05 §1），并把**每次实际模型请求**落成 Manifest。

## 2. 总体结构

```
               ┌──────────── Context Engine（Python，Source 服务主体，world 来自会话）────────────┐
 trigger ──►   │ Strategy(version) → SectionProviders（只读、逐资源当前授权）→ Budgeter → Pack v6 │ ──► Artifact + 0052 式绑定
 (Message /    │                     │                                          │                │
  Role trigger │                     └── sources[]（ref, revision, content_hash, evidence_kind,  │
  / reassess)  │                         access_decision_ref, section）            insufficient[] │
               └─────────────────────────────────────────────────────────────────────────────────┘
 Host 每次模型调用 ──► guard authorize('model', request_snapshot) ──► runtime.nexloop_model_requests（AT-027）
```

- **组装在服务端**：Engine 只在 Source 服务会话中运行（与现有 `ContextArtifactProducer` 同一身份边界），Run 凭据不能自组上下文。
- **Pack 与 Manifest 分开**：Pack（v6）是 Run 的输入 Artifact；Manifest 是**每次模型调用**一条，引用 Pack、记录实际请求 hash。一个 Run 多次调用 → 多条 Manifest（AT-027）。
- **扩展而非取代**：v2–v5 冻结不动（已有 Run、测试与 Host 解析继续有效）。v6 是新协议，结构上是 v5 的超集；新 Run 经策略选择 v6，旧协议按 Host 的 `context_input_protocol` 显式配置继续可用（同现有做法：不按“看起来像 JSON”猜协议）。

## 3. Context Pack v6 组成与来源

层次与 docs/05 §2 对齐；每个条目都带 `ref / revision / content_hash / evidence_kind / access_decision_ref`，汇总到 `sources[]`。

| 段 | 内容 | 来源（只读、当前授权） | 层级 |
|---|---|---|---|
| `bindings` | tenant/world/run/source principal/context_id/artifact/command_digest | 现有 v2 bindings，不变 | 必须 |
| `role` | Role 绑定与定义；v5 `role_policy`（ceiling/scope/budget/effect_units，`grants_authority:false`） | 0063/L1 阶段 B | 必须 |
| `goal` | 目标版本链（`goal:<id>@<v>` 及祖先当前版本）、KR、优先级、约束；**control_snapshot**（`control_revision`、暂停 scope、预算） | NX-022 `ControlPlane.snapshot` / `read_goal`；补逐资源 READ（NX-022 §6 遗留） | 必须 |
| `current_event` | 当前顾客 Message 原文（v2 `user_statement`）或 Role/复评触发；**标为不可信内容** | 0046 Message + L1 0077 派生 READ | 必须 |
| `constraints` | 当前 Action 契约（已发布定义摘要）、联系约束/停止促销等**正式**属性、当前控制对象、预算余量快照 | `control.nexloop_action_definitions`、Consumer 正式属性（`AuthorizedObjectReader`）、EffectControl | 必须（固定钉住） |
| `consumer_state` | Consumer 正式属性（逐属性 READ 后的投影）、相关正式对象（Product/订单等）及 revision | `ontology.objects` 经 `AuthorizedObjectReader` | 核心业务 |
| `open_work` | 未解决问题、未履行承诺、pending/unknown 外发意图与**外发投递状态**（NX-047：persisted/dispatching/unknown 只出现在这里，标“未确认执行”） | 正式 Problem/Commitment 对象（NX-020 写入后）；`runtime.nexloop_effect_intents`；`runtime.nexloop_outbound_messages`（经新增只读定义器，按 Consumer READ 过滤） | 核心业务（未确认执行状态钉住） |
| `evidence` | 相关对话（入站 + **渠道已接受**的外发消息）、关系评估（v4 语义）、Claim 原文证据（`statements` 中 `resolution_state ∈ {awaiting_definition, needs_resolution, unresolved}` 的原文，**只作证据**）、召回命中 | 0046/0079 会话流；0058/0076 评估；0073 Claim 读视图；NX-021 `OntologyRecall` | 证据 |
| `semantics` | browse/resolve 结果（定义、别名、词表、约束），带 version/coverage/ambiguity | §5 语义只读 adapter | 补充语义 |
| `experience` | 同类策略结果/失败案例（**v0.1 空段**，跨消费者私人信息默认禁止） | 预留 | 经验 |
| `budget_report` | 每段 included/omitted 计数、估算 token、裁剪原因、`insufficient[]` | Budgeter | 必须 |

**分区规则（ADR-019 决定 2/5，AT-064）**
- 正式区 = `constraints`、`consumer_state`、`open_work` 中的**正式对象**。只接受 EIOS 正式对象/属性（经受治理 Action 写入），**绝不**放 Claim。
- `hypothesis` Claim：默认**不进入 Context**（任何段）。仅研究策略可开启 `include_hypotheses`，且只能进 `evidence.hypotheses` 子段、标 `hypothesis`、带 `derived_from`，Budgeter 将其列为最先裁剪项；SQL 绑定校验正式区不含任何 Claim ref。
- `awaiting_definition` / `needs_resolution` / `rejected_definition` / `unresolved` 的 Claim：只以**原文证据**出现在 `evidence.claim_evidence`（source 为 Message span），不带结构化值；`rejected_definition` 默认排除。
- 外发消息：只有 `provider_accepted/delivered` 的进入 `evidence.conversation`；`persisted/dispatching/unknown/failed` 只在 `open_work.outbound` 以“未确认执行”出现，**不能**被表述为“已告知顾客”。
- 用户原文与工具输出永远在数据区，系统指令区只由可信模板生成（docs/05 §7）。

## 4. 实际请求快照与 hash（AT-027）

**谁记录**：Host 每次模型调用前已调用 guard `authorize(command,'model')`。v6 起该调用必须携带 `request_snapshot`，guard 在同一事务内写 `runtime.nexloop_model_requests` 后才返回授权；写失败 = 拒绝模型调用（记录先于调用，与 docs/05 §4 一致）。

`request_snapshot`（Host 计算，服务端复核可复核部分）：
- `call_sequence`（Run 内单调，从 1 开始；服务端 `unique(run_id, call_sequence)`，必须等于上一条 +1）
- `context_id`（v6 pack 的 context_id；服务端核对与已绑定 Artifact 一致）
- `model_provider`、`model_id`（真实 ID；与可信 model profile allowlist 一致）
- `tool_manifest_digest`（实际注册工具名 + schema 的 canonical hash）及工具版本列表
- `request_digest` = SHA-256（发往 provider 的**实际请求体** canonical JSON，含 messages、tools、settings；不含鉴权头）
- `prompt_artifact`：完整请求体经 Host→guard 上传为私有 Artifact（`prompt_artifact_ref`），PG 只存 hash 与受权引用；调试读取单独鉴权（docs/05 §4）
- `input_token_budget` / `output_token_budget` / provider settings 摘要
- 调用结束后 `authorize('model_result')`（新增操作）补记响应状态、用量、费用、`response_digest`；失败/未知也要记

**服务端组成 Manifest**：`model_requests` 行 + 绑定的 v6 Pack 元数据 + `sources[]` → 按 `context-manifest.schema.json` 组装（`policy_revision` = v5 策略 revision 摘要；`ontology_schema_revision` = 租户 schema 版本摘要；`semantic_snapshot_ref` §5；`context_strategy_version`；`embedding_profile_ref` = NX-021 活动 profile 或 null；`redaction_policy_ref`）。提供只读 `read_manifest(run_id, call_sequence)`（审计用，需 Run 所属 Consumer READ + 新权限 `nexloop.context.audit`）。

**可核对性**：同一 Run 的每次调用都能还原“哪个 Pack（hash）+ 哪些来源（revision/hash/授权证明 ref）+ 实际请求（hash/Artifact）”。确定性 provider 测试中由 Host 计算、服务端从 Artifact 重算 `request_digest` 比对。

## 5. 语义只读 adapter 与 EvoOntology 映射

接口（与 docs/05 §3、Evo `SemanticLayer.browse/resolve` 输出形状兼容）：

```
browse_semantics(query, kind ∈ {object_type, property, vocabulary_value, alias, any}, limit≤20)
  → {version, coverage, ambiguity, items:[{ref, kind, display_name, description, score, source_refs}]}
resolve_semantics(mentions[≤16], context{type_hint?, consumer_ref?})
  → {version, coverage, ambiguity, resolutions:[{mention, candidates:[{ref, kind, score, constraints, source_refs}], status: resolved|ambiguous|unresolved}]}
```

- **实现**：定义召回走 NX-021 `OntologyRecall(definitions=True, instances=False)`（向量 + FTS + trgm，属性闸门预过滤、来源版本校验）；定义详情读 `ontology.object_type_versions` 最新版本、别名（NX-045/046 回流）、封闭词表。只返回**定义**与 ref，不返回实例数据；解析出概念后的实例读取必须另走 `AuthorizedObjectReader`（docs/05 §3）。
- **version** = `semantic:<schema_revision_digest>@<recall config_version>@<embedding profile_id|fts>`，即 Manifest 的 `semantic_snapshot_ref`。
- **coverage** = 有结果的 mention 占比；**ambiguity** = top1 与 top2 分差 < 阈值（配置）或同分多候选时为 true，并逐 mention 给 `status`。歧义不自动选（与 NX-019/020 一致）。
- **EvoOntology**：本仓 Evo 未安装（`versions.lock.json` `installed:false`）。v0.1 只复用其**输出形状与命名**，不引入 `SemanticStore`，不维护第二份定义库；adapter 的唯一事实来源是 EIOS schema 表与 NX-021 派生索引（可重建）。将来若引入 Evo 演进/评估（S6），只读映射为 `EIOS definition ref ↔ Evo concept id` 的派生表，Evo 永远不能写 EIOS、不能成为业务事实库（AGENTS 固定决定）。
- **作为工具**：Pi 侧注册 `nexloop.semantics.browse` / `nexloop.semantics.resolve` 只读工具，经 guard `tool` 操作授权（与 effect 工具同一路径），结果计入当次 Manifest 的 sources（evidence_kind `schema`）。

## 6. 预算、裁剪顺序与 insufficient（AT-028）

- **配置**：`context_strategy`（版本化）给出 `input_token_budget`（初始 16k）、`output_reserve`（4k）、各层配额与裁剪顺序；Provider 能力小于预算时启动失败或显式降级（docs/05 §2）。
- **计数**：`TokenEstimator` 协议。默认实现为**保守上界**（UTF-8 字节数/2 向上取整，CJK 与 JSON 结构都不低估），模型适配器可替换为该模型的 tokenizer；序列化框架、工具 schema、provider overhead 先扣除。不得用字符数当 token 数。
- **固定不裁剪（pinned）**：`bindings`、`role`、`goal`（含 control_snapshot）、`current_event`、`constraints`（含否定/停止联系类正式属性）、`open_work` 中 pending/unknown 意图与未确认外发、v5 策略。若 pinned 本身超预算 → **不截断**，返回 `insufficient_context`（reason `mandatory_exceeds_budget`）。
- **裁剪顺序**（docs/05 §2）：重复证据（同 content_hash / 同 correlation_key 只留最新一条并计数）→ `experience` → `hypotheses`（若开启）→ 低相关 `semantics` → 较旧 `evidence.conversation`（保留最近 N 轮与所有含否定/联系限制的消息，按 Claim `constraint`/否定 polarity 或正式约束对应的原消息钉住）→ 较旧 `claim_evidence` → 低分召回。每次裁剪写 `budget_report.omitted[{section, ref, reason}]`，被裁的来源仍记入 sources 的 `omitted` 状态以便审计。
- **insufficient 表达**：`insufficient[]` 条目 `{code, section, refs}`，code ∈ `mandatory_exceeds_budget`、`required_source_unreadable`（当前授权缺失）、`required_source_stale`（revision 变化）、`goal_not_current`（NXC03）、`control_paused`（NXC01）、`semantic_ambiguous_required`（必需概念歧义）。非空时 Pack 仍生成（供审计），但 Run 指令区明确“上下文不足，不得编造已完成事实”，并由 NX-024 映射为 `needs_information`/`waiting_external`/`escalate`。

## 7. 权限与 world 过滤

- tenant/world 只来自 Source 会话；v6 只支持 `real` world 的 Message/Role Run（与现有会话表 world=real 一致）。simulation/shadow 的 Context 只能读本世界对象与 Claim（Claim/召回/外发表都带 world）；跨世界引用在 SQL 绑定时拒绝。
- 每个来源一条当前 READ 证明（`access_decision_ref` = 证明 hash）：对象与属性用 `AuthorizedObjectReader`（逐属性）；Message 用 L1 0077/0080 派生 READ；Claim 用 0073 读视图（需 Conversation READ）；召回用 NX-021 闸门；外发记录用新增只读定义器（需 Consumer READ）。
- **性能前提**：v6 一次组装可能数百个证明；采用 L1 报告给出的 O1 设计——`authorize_many` 在一个只读事务内批量判定，证明形状不变，SQL 校验端不改。O1 未合入前 v6 的 section 配额需保守（例如 evidence ≤ 12 条）。
- **SQL 绑定校验**（替代“整包重建对比”）：对每个 source 复核签名证明当前有效、`content_hash` 由 SQL 从源行重算一致（对象 properties 投影、Message body、Claim quote、评估行）、`evidence_kind` 与源一致；正式区 ref 不得指向 Claim/hypothesis；`budget_report` 计数与段内容一致；Pack 字节 hash 与 Artifact 一致。这样新增段不需要为每段写一份 SQL 重建器，但每个来源仍在 SQL 内重核。

## 8. 与 L1 v4/v5 Context 的关系

- **v2–v5 保持冻结**：不改其 schema、SQL 绑定或 Host 解析；现有测试不受影响。
- **v6 是超集**：`role_policy`（v5）原样作为 `role` 段的一部分；v4 的 `current_statements/evidence` 语义并入 `evidence.relationships`（同样的 user_statement/resolved 才能进 current 子段规则）。v6 的 schema 由 v5 schema 派生（同 v3/v4 的 `deepcopy(PACK_SCHEMA)` 做法），以便 Host 逐步迁移。
- **迁移路径**：Engine 先能**产出** v2/v4 等价内容（回归对比），再切新 Run 到 v6；旧协议在所有 Run 迁走后由 ADR 决定下线。

## 9. 迁移形状（临时编号，接合并时最高号之后）

1. `nx023_context_strategies`：`control.nexloop_context_strategies(tenant_id, strategy_id, version, definition jsonb, created_at)` append-only，受治理 Action `nexloop.context.strategy.publish:1`（人类 owner）。内置 `recent_plus_required_v1`、`ontology_hybrid_v1` 由 business-actions/可信配置发布，不写死在迁移。
2. `nx023_context_manifests`：
   - `runtime.nexloop_context_packs(tenant_id, world, context_id, run_id, protocol, strategy_ref, pack_digest, artifact_id, budget_report jsonb, insufficient jsonb, created_at)`；
   - `runtime.nexloop_context_sources(context_id, ordinal, section, ref, revision, content_hash, evidence_kind, access_decision_ref, status ∈ {included, omitted})`；
   - `runtime.nexloop_model_requests(tenant_id, world, run_id, call_sequence, context_id, model_provider, model_id, tool_manifest_digest, request_digest, prompt_artifact_id, input_token_budget, output_token_budget, settings_digest, requested_at, result_status, usage jsonb, cost, response_digest, completed_at)`，`unique(run_id, call_sequence)`、单调触发器、只追加（结果列一次性填写）。
   - 全部 FORCE RLS、owner nexloop_owner、应用角色无表权限。
3. `nx023_context_bind_v6`：在 L1 阶段 B 之后的最新 `nexloop_context_artifact_command`/runtime activation 链上追加 v6 分支（rename 私有 alias + 新 wrapper），实现 §7 的逐来源复核；在 guard `authorize` 链上追加 `model` 操作的 `request_snapshot` 写入与新 `model_result` 操作。
4. `nx023_semantic_read`：`control.nexloop_semantic_browse/resolve` 只读定义器（复用 NX-021 入口与闸门，追加别名/词表详情投影）；外发记录只读投影 `control.nexloop_open_outbound(consumer)`。
5. 契约（需调度员决定，未改）：新增 `context-pack-v6.schema.json`；`context-manifest` 增加可选 `call_sequence` 已有、补 `tool_manifest_digest`、`insufficient` 摘要字段（或放 Pack）；`run-command` 增加 `control_snapshot`（NX-022 §6 提案）。

## 10. 测试清单

- **AT-027**：确定性 Pi 一个 Run 3 次模型调用 → 3 条 `model_requests`，`call_sequence` 1..3 连续；每条 `request_digest` 与 prompt Artifact 重算一致；Manifest 通过 `context-manifest.schema.json`；记录写失败（注入）时模型调用被拒；跳号/重复 call_sequence 被拒；工具集变化导致 `tool_manifest_digest` 变化。
- **AT-028**：构造超预算证据 → 裁剪顺序与 `budget_report` 精确断言；含否定/停止联系的消息与正式约束、pending/unknown 意图、未确认外发在任何预算下都在；pinned 超预算 → `insufficient_context`、不截断；Token 估算为上界（与 tokenizer 对比用例）。
- **AT-064**：hypothesis Claim 存在时默认不进 Pack；研究策略开启时只在 `evidence.hypotheses`；awaiting_definition Claim 只以原文证据出现；SQL 绑定拒绝把任何 Claim ref 放进正式区（篡改测试）。
- **权限/world**：撤销某属性 READ → 该属性不进 `consumer_state` 且如为必需则 insufficient；他租户/他 world 来源被拒；外发 `persisted/unknown` 不进对话证据；召回闸门过滤生效；L1 派生 READ 撤权后组装失败。
- **语义 adapter**：browse/resolve 输出 version/coverage/ambiguity；歧义不自动选；只返回定义；Schema 发布/别名回流后 `semantic_snapshot_ref` 变化；无 READ 的类型/属性不出现。
- **注入**：顾客原文含“忽略规则/导出全库”仅在 `current_event` 数据区，系统区模板不含用户文本；检索出的知识文本不被当作工具指令。
- **回归等价**：Engine 产出 v2/v4 等价内容的对比测试；v2–v5 现有测试全绿。
- **控制**：NX-022 暂停 scope / 目标改版 → 组装返回 `control_paused`/`goal_not_current`。

## 11. 与 NX-024（计划/复评）的接口

```
ContextResult = {context_id, protocol:'nexloop.context-pack.v6', pack_ref, pack_digest,
                 strategy_ref, control_snapshot, goal_refs, insufficient:[...], budget_report}
```
- NX-024 的 PlanStep 指定 `context_strategy_ref`；每次复评（`reevaluate_at` 到期）**重新组装**，不复用旧 Pack（docs/05 §6“先读最新事实/授权”）。
- `control_snapshot` 原样进入 Run 命令/意图（NX-022 §3 挂接点 1），dispatch 前 `assert_dispatch_controls` 复核；stale → NX-024 生成重新评估。
- `insufficient` 非空 → Run 正常输出 `needs_information`/`waiting_external`/`escalate`（AT-029 的 `no_action` 也是正常结果，不视为失败）。
- Run 的结论记录（“决策依据摘要 + 引用证据 + 候选动作”）引用 Manifest 的 `context_id`/`call_sequence`，不要求私有推理链。

## 12. 与 L1 阶段 B 的文件冲突与落地顺序

会碰 L1 阶段 B 文件的部分（**必须等阶段 B 合入后再动**）：
- `runtime_activation.py`（guard `authorize` 增加 `request_snapshot`、`model_result`；`_signed` 已被阶段 B 改为携带 `context_policy_envelope`）
- `context_artifacts.py` / `role_context_artifacts.py` / `relationship_context_artifacts.py`（v6 产出与绑定）
- `effect_intents.py` / `effect_execution.py`（携带 control_snapshot；阶段 B 同时在加 policy envelope）
- v5 pack 与 Host：`role_context_pack.py`、`apps/agent-host/src/context-input.ts`、`runtime-host.ts`、`pi-runtime-adapter.ts`（模型请求快照上报）、`packages/contracts`（v5/v6）
- SQL：`nexloop_context_artifact_command`、runtime activation authorize 链（阶段 B 追加 wrapper，v6 必须接在其后）

不碰 L1 文件、可先做的部分：语义 adapter（新模块 + 只读定义器）、Strategy 表与发布 Action、Budgeter/TokenEstimator 纯模块、各 SectionProvider（只读，复用现有 reader）、Manifest/Request 表与只读审计函数、外发记录只读投影。

**建议顺序**
1. **NX-023-A（现在可做，独立分支）**：迁移 1、2（表）、4（语义/外发只读）；`context_engine/`（strategy、budget、sections、semantic adapter）纯逻辑 + PG 只读测试；不接 Run。
2. **L4 O1 批量授权**合入（或与 A 并行），否则 v6 证明数量不可承受。
3. **L1 阶段 B 合入**（v5 + 调度期 policy wrapper）。
4. **NX-023-B**：迁移 3（v6 绑定 + guard 请求快照），改 `context_artifacts.py`、`runtime_activation.py`、Host 适配与 v6 解析；契约 v6；AT-027/028/064 端到端。
5. **NX-024**：PlanStep/复评调度，消费 `ContextResult` 与 control_snapshot。
6. 回归对比通过后，新 Message/Role Run 默认 v6；v2–v5 下线另立 ADR。

## 13. 待决定

- Manifest 是否允许 v2–v5 旧协议 Run 也写 `model_requests`（建议：是，`context_id` 指旧 Artifact，sources 只含其固定来源，以便 AT-027 尽早覆盖现有 Run）。
- `prompt_artifact` 的保留期与调试读取权限（`nexloop.context.audit`）归属。
- hypothesis 是否允许任何生产策略开启（建议：仅 shadow/研究世界）。
- v6 契约与 `run-command.control_snapshot` 是否进入 `packages/contracts`。
