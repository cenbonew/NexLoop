# EvoOntology 冻结版本核对与复用方案（2026-10-18，ROADMAP §10 提前项）

- **分支**：`l7/s6-prep`，从 main `9d1223ee` 切出。只读核对，没有安装任何东西，也没有改动 lock。
- **依据**：
  - AGENTS.md 固定决定：EvoOntology 选择性复用，只用于语义访问、候选演进与评估，不得成为第二个业务事实库，也不得绕过 EIOS；
  - `docs/handoff/docs/08_EVOONTOLOGY_INTEGRATION.md`；
  - `docs/implementation/NX-038-design.md` §4；
  - `docs/implementation/NX-023-context-engine-design.md` §5。
- **来源**：
  - 任务要求读私有附件 `local-only/LOCAL_SOURCES.md`，但该文件在开发机上**不存在**；
  - 实际使用的是开发机上已有的克隆 `<UPSTREAM_DIR>/EvoOntology`（路径不写入公开文档）；
  - 只用 `git --no-optional-locks` 的 `rev-parse`、`ls-tree`、`show`、`log` 读取，没有改动该克隆。

## 1. 冻结提交核对

| 项 | lock 中的值 | 实际 | 结论 |
|---|---|---|---|
| commit | `upstream_sources.evoontology.commit` = `f64413dae88d88645b1f2c069cf4e17308ad0f89` | 克隆的 HEAD 即该提交，提交于 2026-09-29T13:51:20+08:00，标题为 docs: add bilingual documentation pairs | 一致 |
| 版本 | `packages.evoontology.source_version` = 1.1.0 | 该提交的 `pyproject.toml` 中 `version = "1.1.0"` | 一致 |
| 许可证 | `packages.evoontology.license` = MIT | 根目录 `LICENSE` 为 MIT，Copyright (c) 2026 Meiduo Chong；sha256 `140ba67c…3e38e`，与仓库 `licenses/EvoOntology-MIT.txt` **逐字节一致** | 一致 |
| 运行时依赖 | — | `dependencies = []`，只用标准库；`requires-python >= 3.10` | 不引入第三方包 |
| 发行渠道 | — | PyPI 上没有 `evoontology` 包（PyPI JSON 接口返回 404） | 只能 vendored，或者走 git 依赖 |
| 结构 | — | 共 283 个文件：核心 `evoontology/`；`plugins/claude-code/evoontology/` 与 `plugins/evoontology-codex/evoontology/` 是两份副本，13 个核心文件的 git blob **与根目录完全相同**（由 `scripts/sync_plugin_core.py` 同步）；另有 `benchmarks/`（bird、ddr_10k、insightbench） | 只以根目录 `evoontology/` 作为唯一来源（08 §7：不要同时复制根目录与插件内的同一份核心） |
| 子组件许可证 | — | `evoontology/visualization/vendor/LICENSE.cytoscape.txt` | 可视化部分不复用，不涉及 |

## 2. 要复用的接口（以该提交的实际签名为准）

### 2.1 语义访问：`evoontology/runtime/runtime.py`（497 行）、`ontology/models.py`（196 行）

- **`SemanticLayer`**：
  - `load(store_path, version=None)`：加载 active 版本，或显式指定的版本；
  - `version()`、`manifest(exposed_tools)`；
  - `browse(query="", kind="term", limit=6)`：`kind` 取值为 `term | mapping | relation | constraint | all`；空查询返回 `needs_query`，不允许全量枚举；
  - `resolve(mentions=None, context=None)`：最多处理 5 个 mention，多出的部分进入 `skipped`；
  - `execute(name, arguments)`：把 `browse_semantics`、`resolve_semantics` 两个工具名分派到上面两个方法。
- **返回形状**：
  - browse：`{status: ok|needs_query, items, matched_total, catalog_total, version, note}`；
  - resolve：`{status: ok|partial, results, requested_count, processed_count, skipped, version, note}`；
  - 只认生命周期为 `validated` 或 `active` 的记录。
- **数据结构**（`ontology/models.py`，都是 dataclass，带 `from_dict`）：

| 类 | 字段 |
|---|---|
| `Term` | id, name, type, definition, scope, aliases, evidence_refs, lifecycle_state |
| `Mapping` | id, term_id, database_source, table, column, semantic_filter, aggregation_semantics, grain, validation, evidence_refs, lifecycle_state, confidence_level |
| `Relation` | id, source, relation_type, target, connection_condition, description, evidence_refs, lifecycle_state |
| `Constraint` | id, target, constraint_type, trigger_keywords, severity, scope, confidence_level, description, evidence_refs, lifecycle_state |
| `Evidence` | id, source, query, result, validation_method, timestamp |

- **与 08 和现有实现的差异**：
  1. NexLoop 的 `context_engine/semantic.py` 已经按 NX-023 实现了 browse/resolve。它的返回形状是 `{version, coverage, ambiguity, items|resolutions…}`，和上游**不是**同一形状：没有 `status`、`matched_total`、`note`，多了 `coverage` 与 `ambiguity`，上限也不同（NexLoop 为 20 和 16，上游为 6 和 5）。NX-023 文档里“与 Evo 输出形状兼容”的说法要修正为“借用了命名与思路，返回形状不同”。
  2. 上游的匹配是词项、名称、别名的字符串匹配加停用词（`_SEARCH_STOPWORDS`），没有向量检索。这与 08 §3 的判断一致；NexLoop 用的 NX-021 召回更强。
  3. `Mapping` 是以 SQL 表列为中心的（database_source/table/column/aggregation_semantics/grain），NexLoop 要映射到 `eios:object_type:`/`eios:property:` 引用。`Constraint` 只是带描述和关键词的记录，**不是**执行拦截器（08 §3），不能用来代替 EIOS 的 Action 授权。
- **结论**：语义访问**不移植代码**，保持现状。NX-023 的实现是唯一事实来源，读的是 EIOS 定义表与 NX-021 派生索引。将来如果需要 Evo 的概念 ID，只建 `EIOS definition ref ↔ Evo concept id` 的派生映射表（可以重建）。

### 2.2 候选演进：`evoontology/evolution/session.py`（610 行）、`evolution/adapter.py`（56 行）

- **`EvolutionSession`** 的方法：
  - `start_run(parent_version, *, adapter="", max_rounds=None, acceptance=None, …)`、`resume(run_id)`、`latest_run()`；
  - `begin_round(hypothesis, candidate_version)`：在冻结预算内开一轮，预算用尽时转为 `incomplete/budget_exhausted`；
  - `record_round(*, decision, metrics, artifact_refs, notes)`：**只记录 reject**；
  - `record_evaluation(subject, result, *, role)`；
  - `accept(new_version)`：**校验、发布、切换 active.json、推进检查点**；
  - `mark_incomplete(reason)`、`extend_budget(max_rounds)`、`must_continue()`、`rejected_count()`、`status()`；
  - `confirm_trajectory_sources(sources)` 与相关的轨迹来源方法。
- **常量**：
  - 状态：`RUNNING = running`、`ACCEPTED = accepted`、`INCOMPLETE = incomplete`；`TERMINAL_STATES` 为 accepted 与 incomplete；
  - `DEFAULT_MAX_ROUNDS = 8`、`DEFAULT_MIN_REJECTS_BEFORE_INCOMPLETE = 2`；
  - `INCOMPLETE_REASONS` 为 budget_exhausted、user_interrupted、missing_data、missing_permissions、unreliable_evaluation、external_block。
- **持久化方式**：run 目录 `run_<n>` 下的 JSON 文件；版本名必须匹配 `^(ontology|semantic)_v\d+$`；依赖 `ontology.store`、`workspace`、`trigger`、`validate`。
- **`EvolutionAdapter`**（`Protocol`）：`evaluate(subject, cases=None, output_hint=None) -> {metrics, cases?, artifact_paths?}`。`normalize_result(result)` 要求必须有 `metrics` 字典，`cases` 与 `artifact_paths` 必须是列表。
- **与 NexLoop 契约的差异**：

| 项 | 上游 | NexLoop（`evolution-candidate.schema.json`，08 §5） |
|---|---|---|
| 粒度 | 一个 run 包含多轮，每轮一个候选 | 一条候选记录，含 dimension、primary_hypothesis、预算等 |
| 状态 | running → accepted \| incomplete；reject 是**每轮的决定**，不是 run 的终态 | proposed → evaluating → accepted_for_release \| rejected \| incomplete |
| 接受的含义 | `accept()` 在同一步里**发布新版本并切换 active** | `accepted_for_release` **不等于已发布**；发布由 EIOS 治理与人工审核另行完成（ADR-019、NX-038 §5） |
| 存储 | 本地 JSON 工作区 | PG（候选存储），状态变更走受治理的路径 |

- **结论**：只**移植状态机的规则**：预算冻结与耗尽即 incomplete、incomplete 的合法原因集合、“只有在外部原因下才能 incomplete，单纯停滞不算”、轮次计数与 extend_budget 的语义。用 PG 重写，**不移植** `accept()` 中的发布与切换 active，也不移植文件工作区。`EvolutionAdapter` 与 `normalize_result` 可以原样移植，作为 NX-038 §4.3 `EvaluationAdapter` 的基础。

### 2.3 评估：`evoontology/evaluation/evaluation.py`（118 行）

- **`EvaluationGate`**（静态方法）：
  - `decide_gt(parent_scores, candidate_scores)`：**候选平均分严格大于 Parent 才接受**，返回 `{accept, parent_score, candidate_score, delta, protocol: ground_truth}`；
  - `decide_judge(verdicts)`：每条 verdict 为 `{winner: candidate|parent|tie, critical_error}`。条件是没有任何 critical_error，且候选胜场多于 Parent 胜场；
  - `anonymize(parent_answer, candidate_answer, swap=None)`：随机给两边贴 A/B 标签，返回 `{A, B, A_is, B_is}`；
  - `decode(judgment, mapping)`：把 A/B 的结论还原成 candidate/parent。
- **NexLoop 要补的部分**（08 §2）：
  - 硬约束：零越权、零跨租户、零事实污染；
  - 样本门槛与重复评估；
  - 非劣标准由任务预先定义（上游的 `>` 不允许持平，也没有非劣选项）；
  - `anonymize` 用的是 `random.random()`，可审计性要求记录 swap 种子或每条的 swap 结果。
- **结论**：`EvaluationGate` 可以**原样移植**（它是纯函数），再在外层加上 NexLoop 的门槛。

### 2.4 校验：`evoontology/validate.py`（179 行）

- `validate(root, version)` 检查的是工作区 JSON 文件：active.json、版本文件、记录引用。
- NexLoop 要的是类型、权限、兼容性和迁移检查（08 §2），对象是 EIOS 定义。
- **结论**：只借用“引用完整性检查”的思路，不移植代码。

### 2.5 明确不复用

- `ontology/store.py` 的 `SemanticStore`：JSON 版本目录，以及 `save_version`、`promote`、`publish`、`set_active`。它会构成第二个定义库，并带有发布权。
- `runtime/mcp_server.py`、`ops.py`、`tools.py`：MCP 管理操作与工具暴露。
- `workspace.py`、`workflow.py`：任意工作区选择，以及主机侧流程。
- `trajectory/`、`trigger/`：NexLoop 有自己的轨迹与触发来源（NX-038 §2、§3）。
- `visualization/`、`benchmarks/`、`plugins/`。

## 3. vendored 还是依赖：建议**选择性 vendored**

- **不作为依赖**：
  - PyPI 上没有这个包，只能用 git 依赖，那样运行时就会依赖外部仓库，而且无法逐文件审计修改；
  - 整包安装会带入 `SemanticStore` 与发布路径，违反“不得成为第二个业务事实库”的要求。
- **vendored 范围**（第一批，NX-038 实现时执行，本次不执行）：

| 上游路径 | 目的路径（建议） | 处理 | upstream sha256 |
|---|---|---|---|
| `evoontology/evaluation/evaluation.py` | `packages/eios-core/src/nexloop_eios/evolution/evo_evaluation.py` | 原样移植；外层另加 NexLoop 门槛 | `028d6f23d498a40a9a0c822579e36991b293f3c91cede8af915ecf8b00d3abaa` |
| `evoontology/evolution/adapter.py` | `…/evolution/evo_adapter.py` | 原样移植 | `2f2b5a1163176115a4eafa241ffbf0deeaef7558543595901c1ba79618b231be` |
| `evoontology/evolution/session.py` | `…/evolution/candidate_state.py` | **改写**：只保留状态机规则，存储改为 PG，删除 accept 中的发布与切换 active；provenance 标为 adapted | `849d847f4e75541f3a71268523e9760f7f757fc841f596cb406dd3f3cf0e551c` |
| `evoontology/ontology/models.py` | 暂不移植 | 如需概念视图，只作为只读的派生数据类 | `a86676d8b571c56f680d9ecb5e5f1b6a92a44c8407d2c3c79b82bd08028ad6ee` |
| `evoontology/runtime/runtime.py` | 不移植 | NX-023 只借用命名 | `e7bd47e4318add3c43c4c2a96cdd80962db4098c6d9c565053000bdc081fe9a5` |

- **边界**：移植后的代码只操作候选存储与评估结果，不持有发布、业务写入或效应凭据（NX-038 §4.3）。候选只能产出 EIOS schema proposal 的输入，不能产出 Action、授权或 service grant（NX-038 §3）。

## 4. provenance 怎么记

参照 `packages/eios-core/PROVENANCE.json` 的格式，避免两套写法：

1. 新建 `packages/eios-core/src/nexloop_eios/evolution/EVO_PROVENANCE.json`：

   ```json
   {"upstream": "https://github.com/ruc-datalab/EvoOntology", "commit": "f64413dae88d88645b1f2c069cf4e17308ad0f89",
    "license": "MIT", "license_file": "licenses/EvoOntology-MIT.txt",
    "license_sha256": "140ba67c27276d23ccb07533d1604629f000ae350e2ba75190618bb8f3c9e38e",
    "files": [{"source_path": "evoontology/evaluation/evaluation.py", "destination": "...", "upstream_sha256": "028d6f23...",
               "nexloop_sha256": "...", "adaptations": [], "tests": ["tests/test_evo_evaluation.py"], "owner": "<维护者角色>"}]}
   ```

   - 每个移植文件写明：原路径、目的路径、上游内容 sha256、移植后 sha256、修改原因列表（`adaptations`）和对应测试；
   - 原样移植时 `adaptations` 为空，且两个 sha256 相同。
2. **每个移植文件的文件头**保留 MIT 版权与许可声明（Copyright (c) 2026 Meiduo Chong），并加一行注明来源：`Ported from EvoOntology f64413da evoontology/<path>; modifications: …`。
3. **`versions.lock.json`**：
   - `upstream_sources.evoontology` 增加 `provenance` 字段指向上面的文件，`integration` 保持 `selective_reuse`；
   - `packages.evoontology.installed` 保持 false，因为没有安装这个包；建议另加 `vendored_files` 计数，或写 `integration: vendored_selective`，表示“移植而非安装”。改 lock 走 PR。
4. **NOTICE 与 THIRD_PARTY_NOTICES**：根目录 `NOTICE` 已列出 EvoOntology（MIT）。移植后由 `scripts/check_licenses.py` 的 vendored 检查（读 `versions.lock.json` 与 provenance）纳入，再用 `--write-notices` 重生，并跑 `--check`。
5. **测试**：
   - 增加一个 provenance 测试：逐文件重算 sha256，与 provenance 记录比对；
   - 契约测试：移植后的状态机与 `evolution-candidate.schema.json` 往返一致（NX-038 §4.2 第 5 条）。
6. **更新上游**：不追 master。先 diff 冻结提交与新提交，跑契约测试，然后**手工**挑选需要的修改，并在 provenance 中记录新的 upstream_sha256。

## 5. 结论

- 冻结提交、版本与许可证都与 lock 一致；核心只用标准库；插件副本与根目录核心完全相同，只取根目录一份。
- 语义访问保持 NX-023 现状，不移植上游代码。
- 演进只移植状态机规则，并用 PG 重写，去掉发布权；评估的 `EvaluationGate` 与 `EvolutionAdapter` 原样移植。
- 采用选择性 vendored，并做逐文件 provenance。
- 需要更正一处文档：NX-023 中“返回形状与 Evo 兼容”的说法。
- 私有附件 `local-only/LOCAL_SOURCES.md` 缺失，请负责人或调度员补上，记录上游克隆的位置。
