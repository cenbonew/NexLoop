# EvoOntology 来源核对（NX-038 §4 第 1、2、4 步）

本次只做只读核对并出报告。没有移植代码，没有加迁移，也没有改 `versions.lock.json`（`packages.evoontology.installed` 仍为 false）。上游源码只放在仓库之外的临时目录（下文记为 `<SCRATCH>`），不进入本仓库。

- **依据**：
  - `docs/implementation/NX-038-design.md` §4；
  - `docs/handoff/docs/08_EVOONTOLOGY_INTEGRATION.md` §2、§3、§5、§7；
  - `docs/handoff/docs/18_SOURCES_AND_ASSUMPTIONS.md` S06 与第 40 行；
  - NX-038 D3（负责人已定案：选择性移植，不作为运行时依赖）。
- **分支**：`evo-install-check`，从 s5j `3339878` 切出。

## 1. 来源与许可

| 项 | 锁定值（`versions.lock.json`） | 核对结果 |
|---|---|---|
| 仓库 | `upstream_sources.evoontology.repository` = `https://github.com/ruc-datalab/EvoOntology` | 一致 |
| commit | `f64413dae88d88645b1f2c069cf4e17308ad0f89` | 一致。获取方式：浅获取该 commit（`git fetch --depth 1 origin <commit>`），`git rev-parse HEAD` 结果相同 |
| 归档 | 无锁定值 | `git archive --format=tar HEAD` 的 sha256 为 `d44449f686fcde9731560057a6ed277a09c553f5646b7fe89d8f84cbb1a16226`，共 283 个受跟踪文件 |
| 版本 | `packages.evoontology.source_version` = `1.1.0` | 一致（`pyproject.toml` 中 `version = "1.1.0"`） |
| 许可 | `packages.evoontology.license` = `MIT` | 一致。根目录 `LICENSE` 为 MIT，“Copyright (c) 2026 Meiduo Chong”，sha256 `140ba67c27276d23ccb07533d1604629f000ae350e2ba75190618bb8f3c9e38e` |
| 第三方许可 | — | 只有 `evoontology/visualization/vendor/LICENSE.cytoscape.txt`（Cytoscape.js，MIT，“Copyright (c) 2016-2024, The Cytoscape Consortium”），随 `cytoscape.min.js` 一起出现；两个插件目录中各有一份相同的副本。拟移植的文件不涉及它 |
| 运行依赖 | — | `pyproject.toml` 的 `dependencies = []`，核心没有第三方运行依赖。可选依赖 `bird`（httpx、openai、python-dotenv、pyyaml）只用于基准测试 |

- **结论**：许可与锁定一致。
  - 移植时需要在 THIRD_PARTY_NOTICES 中加入 EvoOntology 的 MIT 文本与版权行（13_SECURITY_PRIVACY_AND_OPEN_SOURCE 第 39 行）；
  - Cytoscape 不移植，不需要加入。

## 2. 模块清单（冻结 commit）

- **核心包**：`evoontology/`，共 4617 行 Python（含可视化）。
- **三份副本，取根目录为唯一来源**：`plugins/claude-code/evoontology/` 与 `plugins/evoontology-codex/evoontology/` 都与根目录 `evoontology/` 逐字节相同（`diff -rq` 无差异）。上游用 `scripts/sync_plugin_core.py` 同步这三份。按 08 §7 与 18 第 40 行“固定一个核心来源”，只以根目录 `evoontology/` 为来源。

| 模块 | 行数 | 作用 |
|---|---|---|
| `evolution/session.py` | 610 | 演进运行的状态机：轮次、预算、评估记录、接受或不完整 |
| `evaluation/evaluation.py` | 118 | 评估门：有真值时比较均分；无真值时用 LLM Judge 统计胜负，加 A/B 匿名化 |
| `evolution/adapter.py` | 56 | 评估 adapter 协议与结果规范化 |
| `trajectory/trajectory.py` | 179 | 轨迹持久化（工作区 JSON 文件） |
| `trigger/trigger.py` | 193 | 演进触发条件：新轨迹数、天数 |
| `validate.py` | 179 | 语义版本记录与引用检查（工作区文件） |
| `ontology/models.py`、`ontology/store.py` | 196、263 | Term、Mapping、Relation、Constraint、Evidence；版本化 JSON 存储与发布 |
| `runtime/runtime.py`、`runtime/tools.py`、`runtime/ops.py`、`runtime/mcp_server.py` | 497、405、279、220 | SemanticLayer 的 browse/resolve/manifest；MCP 工具与管理操作 |
| `workspace.py`、`workflow.py` | 201、352 | 工作区路径解析，build/evolve 工作流 |
| `visualization/*` | — | 预览与渲染 |

仓库中其余内容：`benchmarks/`（bird、ddr_10k、insightbench，基准 adapter 与 LLM provider）、`plugins/`（Claude Code 与 Codex 插件、Skills、hooks、启动脚本）、`tests/`、`assets/`（约 13 MB 图片和视频）。

## 3. 与 08 文档、设计稿 §4 的差异

“行”指冻结 commit 中 `<SCRATCH>/evoontology/<路径>` 的行号。

| 08 / 设计稿中的假设 | 实际 | 差异 |
|---|---|---|
| `runtime/runtime.py` 有 browse/resolve、manifest、ambiguity | `SemanticLayer`（第 56 行）：`manifest(exposed_tools)`（第 96 行）、`browse(query, kind="term", limit=6)`（第 155 行）、`resolve(mentions, context)`（第 262 行）、`_disambiguate`（第 355 行）；resolve 结果带 `status: ambiguous` 与 `coverage_status`（unavailable 或 partial）（第 292–312 行） | **存在，形状不同**：上游没有数值 coverage，用的是 `coverage_status`；歧义以每个 mention 的状态表示。NexLoop 的 `context_engine/semantic.py:75` 是数值 coverage 加布尔 ambiguity，只借用了形状，不需要移植 |
| `ontology/models.py` 有 Term/Mapping/Constraint/Evidence | `Term`（第 58 行）、`Mapping`（第 83 行）、`Relation`（第 124 行）、`Constraint`（第 149 行）、`Evidence`（第 179 行），都有 `from_dict` | **存在**，另有 `Relation`，08 未提及。按 08 §2 只作为语义视图的参考，不移植为业务存储 |
| `evolution/session.py`：演进任务与候选预算、状态 | `EvolutionSession`（第 94 行）：`start_run(parent_version, adapter, max_rounds, acceptance)`（第 108 行）、`begin_round(hypothesis, candidate_version)`（第 202 行）、`record_round`（第 249 行）、`record_evaluation(subject, result, role)`（第 300 行）、`accept(new_version)`（第 403 行）、`_publication_gate`（第 432 行）、`mark_incomplete(reason)`（第 465 行）；状态为 `running`/`accepted`/`incomplete`（第 42–45 行），默认最多 8 轮，至少拒绝 2 次后才能判为 incomplete（第 47–48 行）；不完整的合法原因有 6 个（第 52–61 行）；持久化在工作区 JSON（第 13–19 行） | **存在，状态集合不同**。上游是“一个运行内多轮”：被拒绝的候选回到 running，进入下一轮。NexLoop 契约是“一个候选一个状态”：proposed → evaluating → accepted_for_release / rejected / incomplete。映射见 §4.1 |
| `session.accept` 只把候选标为通过 | 实际在第 403–429 行完成：校验、评估门、`SemanticStore.publish`（发布为新版本并切换 `active.json`）、推进触发检查点 | **语义冲突**。上游的 accept 就是发布和激活；NexLoop 的 `accepted_for_release` 不等于已发布（08 §5），发布属于 NX-039 和人工审核。**不移植 accept 的发布部分** |
| `evaluation/evaluation.py`：候选对比与匿名 A/B | `EvaluationGate`（第 24 行）：`decide_gt`（第 28 行，候选均分严格大于 Parent 才接受）、`decide_judge`（第 44 行，没有严重错误且胜场严格多于对方）、`anonymize`（第 76 行）、`decode`（第 92 行） | **存在**。门槛是“严格改善”，与 NX-038 D7 已定的“非劣（≥ −1 个百分点）且评分相等不接受”不同。移植时门槛参数化，按 D7 实现，并加入硬门槛（08 §2 “增加硬约束、样本门槛与重复评估”） |
| `evolution/adapter.py`：统一评估接口 | `EvolutionAdapter` 协议（第 14 行）：`evaluate(subject, cases, output_hint)`，返回 `metrics`、`cases`、`artifact_paths`；`normalize_result`（第 43 行） | **存在**，`subject` 是语义版本名，`output_hint` 是文件路径提示。NexLoop 侧以候选 ID 代替版本名，不使用文件路径（§5） |
| `validate.py`：记录与引用检查 | `validate(root, version)`（第 19 行），检查工作区中的版本文件与引用 | **存在**，绑定在工作区文件上。只参考检查项，不移植（NexLoop 的候选不在文件工作区中） |
| 设计稿 §4.3 `TraceSource` | 上游 `trajectory/trajectory.py` 的 `TrajectoryStore`（第 105 行）：`append`、`load`、`list_since`、`count_since`，存在工作区文件中，由基准 adapter 写入（第 1–11 行） | **概念对应，实现不可用**。NexLoop 的轨迹在 PostgreSQL（设计稿 §2），不移植 |
| 设计稿 §2 的触发（D8：每周或 200 个 Run） | `EvolutionTrigger`（trigger.py 第 24 行）：最少新轨迹数与最少天数 | **概念相同**。D8 的阈值由 NexLoop 自己实现，不移植 |

## 4. 拟移植与不移植清单（D3：选择性移植状态机与评估对比）

### 4.1 拟移植（改写，逐文件登记来源）

| 上游文件 | sha256（冻结 commit） | 移植内容 | 依赖 | 秘密与数据扫描 |
|---|---|---|---|---|
| `evoontology/evolution/session.py` | `849d847f4e75541f3a71268523e9760f7f757fc841f596cb406dd3f3cf0e551c` | 状态与转移规则：合法的 incomplete 原因集合；冻结预算；“被拒绝不等于运行结束”；评估门输入的校验（第 432–463 行：成对分数与唯一用例 ID、有限数、协议与冻结的验收协议一致、必须显式排除不可接受的回退）。把运行内多轮映射为每轮一个 NexLoop 候选：本轮被拒绝时，本候选记为 `rejected`，下一轮生成新候选，并保留 parent 链；预算耗尽时记为 `incomplete` | 上游依赖 `ontology.store`、`trigger`、`workspace`、`validate` 与文件系统 JSON，**全部不随移植带入**。改为 PostgreSQL 候选表加 NexLoop 契约，只保留规则 | 无 URL、凭据、子进程、网络或 `os.environ` 访问。没有嵌入数据 |
| `evoontology/evaluation/evaluation.py` | `028d6f23d498a40a9a0c822579e36991b293f3c91cede8af915ecf8b00d3abaa` | `decide_gt`、`decide_judge` 的聚合，以及 `anonymize`、`decode`。阈值参数化为 D7 的非劣标准，并增加硬门槛 | 只用标准库 `random`；匿名化改为由冻结种子决定，以便复现 | 同上，无 |
| `evoontology/evolution/adapter.py` | `2f2b5a1163176115a4eafa241ffbf0deeaef7558543595901c1ba79618b231be` | `EvolutionAdapter` 协议形状与 `normalize_result` 的校验 | 只用 `typing` | 同上，无 |

- **登记方式**：移植时按 08 §7 在 `versions.lock.json` 的 `upstream_sources` 中逐文件登记：原路径、目的路径、上表的内容 hash、修改原因、测试映射。
- **对照测试**：上游测试 `tests/test_evolution_session.py` 与 `tests/test_evaluation.py` 可以作为行为对照，不复制。

### 4.2 不移植

| 部分 | 原因 |
|---|---|
| `ontology/store.py`、`workspace.py`、`validate.py`、`trajectory/`、`trigger/` | 都是文件工作区存储或检查，不能作为业务主存储（08 §3）；NexLoop 用 PostgreSQL 与自有检查 |
| `session.accept` 的发布与激活（第 403–429 行），`SemanticStore.publish`、`set_active`、`promote` | 发布权威不能并行复制（08 §2）；`accepted_for_release` 不等于已发布；发布属于 NX-039 与人工审核 |
| `runtime/runtime.py`、`runtime/tools.py`、`runtime/ops.py`、`runtime/mcp_server.py` | 语义入口已由 `context_engine/semantic.py` 实现（只借用形状）；不向运营 Agent 暴露 MCP 管理操作（08 §3） |
| `workflow.py`、`plugins/*`（Skills、hooks、`launch-*` 脚本、`check-reminder.py`） | 不导入宿主脚本的执行能力，不允许模型选择任意 workspace（08 §3）；Skills 改写为受限后台任务提示，不复制原文 |
| `benchmarks/*` | 基准 adapter 和 LLM provider（含可选的 openai、httpx 依赖），与 NexLoop 无关 |
| `visualization/*`（含 Cytoscape vendor 代码） | 不需要；不引入第三方 JS |
| `assets/*` | 图片和视频（约 13 MB），不需要 |

- **扫描结论**：对整个冻结树执行 `git grep`，检查凭据形状（sk-、私钥头、`API_KEY`/`SECRET`/`TOKEN` 赋值）与私网地址，**没有命中**。
- **数据库文件**：只在 `evoontology/workflow.py` 中出现 sqlite 字样，该文件不移植；树中没有数据库文件。

## 5. NexLoop 侧 4 个 adapter 接口与上游的映射（设计稿 §4.3）

| NexLoop 接口 | 上游对应 | 映射与差异 |
|---|---|---|
| `TraceSource` | `trajectory.TrajectoryStore`（第 105 行）加 `from_message_trace`（第 53 行） | 上游以“任务”为存储单位、以工具调用为诊断单位，存在工作区文件中。NexLoop 以 Run 为单位，通过冻结快照从 PostgreSQL 只读函数读取（设计稿 §2.3），只取 ID 和 digest |
| `Diagnoser` | 上游没有代码。诊断与归因在 evolve Skill 中（`plugins/*/skills/evolve-ontology/SKILL.md`），由模型完成 | NexLoop 先用确定性规则（设计稿 §3.1；D9 已定：先只用确定性规则）。Skill 只作为流程参考，不复制 |
| `CandidateProposer` | `EvolutionSession.begin_round(hypothesis, candidate_version)`（第 202 行）加 `SemanticStore.save_version` | 上游在工作区中写一个候选版本目录。NexLoop 写候选表与 Artifact 补丁（`evolution-candidate.schema.json`，`publication_permit_ref` 恒为 null） |
| `EvaluationAdapter` | `EvolutionAdapter.evaluate(subject, cases, output_hint)`（adapter.py 第 17 行）加 `EvaluationGate` | `subject` 从版本名改为候选 ID 与 Parent revision；`cases` 改为冻结的用例 ID；不使用 `output_hint` 文件路径；门槛按 D7 加硬门槛 |

## 6. 风险

1. **状态模型不同**：上游一个运行内多轮，NexLoop 一个候选一个状态。映射规则（被拒绝即 `rejected`，下一轮生成新候选并保留 parent 链）需要用测试固定，并在来源登记中写明差异。
2. **门槛语义不同**：上游要求严格改善，D7 是非劣加硬门槛。移植后不能保留上游默认值，否则会与负责人的定案冲突。
3. **发布耦合**：上游的 accept 会直接发布并激活。如果整份移植 `session.py`，可能把“绕过人工审核直接激活”带进来；只移植规则可以避免。
4. **分歧维护**：选择性改写之后，上游修复只能手工对照（AGENTS.md：“fixes are cherry-picked by hand”）。来源登记中的内容 hash 用于发现上游变化。
5. **匿名化的随机性**：上游 `anonymize` 使用全局 `random`，不可复现；需要改为由冻结种子决定（M22 可复现）。
6. **许可**：移植后的文件保留 MIT 版权行，THIRD_PARTY_NOTICES 中加入 EvoOntology 条目；仓库其余部分的许可声明不能替换它（13 第 39 行）。

## 7. 本次执行的操作（只读）

1. 在 `<SCRATCH>/evo/src` 中执行 `git init`；添加 origin 为锁定仓库 URL；`git fetch --depth 1 origin f64413dae88d88645b1f2c069cf4e17308ad0f89`；`git checkout FETCH_HEAD`；`git rev-parse HEAD`。
2. `git archive --format=tar HEAD`，计算 sha256；用 `git ls-files` 统计文件数。
3. 计算 `LICENSE` 与拟移植文件的 sha256；`diff -rq` 比较三份核心副本。
4. 用 `grep` 和 `sed` 阅读签名与行号。
5. 对整个树执行 `git grep` 扫描凭据与私网地址。

上游的任何代码、测试或脚本都没有执行。
