# NX-039 设计稿：独立评估与语义版本发布

- **状态**：设计稿，只有文档，不写代码，不加迁移。**D39-1 到 D39-14 已决定（负责人 2026-10-20：「S6 全部按推荐」）**。同日按 NX-040 稿的分歧 1–3 修订 §12.3、§12.5 和 §8.4（见各节）。
- **分支**：`l2/nx039-design`，从本地集成分支 `int/post-v010-batch2`（cbd41f57）切出。这个分支已包含 NX-038 的 E1–E4，E5 由 L6 在做。
- **依据**：
  - planning/tasks.json 的 NX-039：S6，依赖 NX-038，模块 M12、M22，交付物是“硬门槛、留出、成本/性能限制和回退”；
  - 01_PRD 的 M12（验收：候选不覆盖 active；缺少迁移或回归证据就不发布；回滚语义版本不自动倒退真实业务事实）与 M22（验收：同一数据集和版本能复现测评条件；发布硬门槛与评分分开，任一关键约束失败即拒绝）；
  - 02_ARCHITECTURE 第 53、64、97 行：Evo 不能反向控制 Model Gateway、发布脚本或数据库管理员权限；
  - 08_EVOONTOLOGY_INTEGRATION §4–§6；
  - 16_ADR：ADR-007、ADR-008、ADR-017，以及 `adr/ADR-019`；
  - 14_TEST_ACCEPTANCE：AT-023、AT-024、AT-048（S6），AT-046（S7，只覆盖负例），§5；
  - NX-038 设计稿 §7、§12（尤其是 §12.4 与 NX-039 的接口）和 D1–D20；
  - E3、E4 实现说明（`NX-038-E3-implementation.md`、`NX-038-E4-implementation.md`）。
- **下游**：NX-040「开放兼容 Schema 受控演进」，依赖 NX-039，由 L4 同时起草。§12 是专门给它的接口。

## 1. 目标与非目标

### 目标

1. **独立评估**：接手 NX-038 冻结的评估包，在不能改动任何冻结条件的前提下，把候选从 `proposed` 转到 `evaluating`，再转为 `accepted_for_release`、`rejected` 或 `incomplete`，并产出一份可复现的评估报告。
2. **硬门槛与评分分开**：任一硬门槛失败，候选就是 `rejected`，评分再高也不行（M22）。
3. **留出集与防泄漏**：留出集冻结后不能修改，按消费者簇和会话簇分组，开评前复查（AT-048），并限制同一留出集被反复使用。
4. **成本与性能上限**：评估本身的花费，以及候选上线后的时延、token 和费用，都不能超过冻结上限。
5. **语义版本发布与回退**：`accepted_for_release` 的候选只能经受治理的人类 Action 发布为一个语义版本；任何时候都能回退到上一个版本，回退不倒退真实业务事实（M12）。
6. **删除传播**：Consumer 被删除后，评估、报告、留出集和已发布的语义条目都要跟着处理，不留可复原的内容。

### 非目标

- **不诊断，不生成候选**：这是 NX-038 的工作。
- **不发布正式 Schema**：`ontology_schema` 维度在 NX-039 一律不进入评估执行，只保留接口，见 §12；兼容 Schema 演进是 NX-040。
- **不做影子运行和模拟世界**：L2 影子属于 NX-043（S7），Twin 属于 S7（NX-038 D18）。
- **不改工具清单和 Agent Host 配置**：`semantic_tool` 候选通过评估后只生成变更提案，按代码评审流程处理（NX-038 §11.4）。
- **不自动发布**：v0.2a 的发布都要人工批准（ADR-017、ADR-019 的方向不变）。
- **不证明收入因果**（M22 关键规则）：报告只说明候选在冻结用例上的表现。

## 2. 输入：评估包（接 NX-038 E5）

### 2.1 交接点

- NX-038 E5 产出两样东西：状态为 `proposed` 的候选（E4 的 `runtime.nexloop_evo_candidates`），以及一份冻结的评估包（提案契约 `evolution-evaluation-bundle.schema.json`，NX-038 §12.4）。
- 评估包只含 ID、digest、版本与数字：
  - 比较对象：`candidate_id`、`parent_revision`、`candidate_revision`、`patch_digest`；
  - 快照：`input_snapshot_digest`；
  - 用例与分组：`design_case_ids`、`validation_case_ids`、`case_groups`；
  - 门槛：`hard_gates_version`、`hard_gates`；
  - 指标：`metrics`、`non_inferiority`；
  - 层级：`levels`（L0，或 L0 加 L1）；
  - 预算：`budget_remaining`；
  - 可复现：`evaluator_version`、`model_profile`、`anonymize_seed`。
- NX-039 从 `proposed` 开始接手全部转移。NX-038 只保留 `proposed → incomplete`（停止）和删除失效这两条转移，见 E4 实现说明。

### 2.2 开评前的复查（任一不符就不开评，候选保持 `proposed`，返回原因码）

| 检查 | 原因码 | 依据 |
|---|---|---|
| 包的 digest 与 E5 写入时的 digest 一致；包里的冻结条件与候选行、诊断行一致（digest、Parent、评估器版本、模型配置） | `bundle_mismatch` | 08 §5：不允许临时改变规则 |
| 候选状态是 `proposed`，所属诊断没有失效，没有 Consumer 正在删除 | `candidate_not_ready` | E4 删除失效 |
| `case_groups` 复查：没有一个簇同时出现在设计集和留出集；按 §4.2 的近重复规则再查一遍 | `leakage_detected` | AT-048 |
| 留出集的使用次数没有超过上限（§4.3） | `holdout_exhausted` | §4.3 |
| 维度在本阶段开放：`semantic_content` 开放（NX-038 D19）；`semantic_tool` 等 D39-6；`ontology_schema` 不开放 | `dimension_closed` | D19、§12 |
| 该维度没有被负责人停止（§8.5） | `dimension_stopped` | 08 §6 |
| `budget_remaining` 足够开评（至少一次 L0） | `budget_exhausted` | NX-038 §12.2 |

- 复查通过后，在**同一个事务**里把候选转为 `evaluating`，写一条候选事件，内容只有包 digest、评估器身份和开评时间。

### 2.3 E5 尚未定稿时的约定

E5 和本稿同时进行。下面三点由 NX-039 依赖，请 E5 保持：

1. 包与报告的契约分开提交，各自单独提 PR。本稿只读包；报告的写入由 NX-039 负责，契约结构见 §2.4。
2. 叠加层（`evaluation_overlay`）只在非 real 的评估会话中生效，real 会话带叠加层就在函数入口被拒绝（NX-038 §12.3）。NX-039 的 L0 和 L1 都通过这个叠加层读取候选。
3. L0 确定性回放的实现放在 E5。NX-039 只调用它，并负责重复、门槛判定和报告。

### 2.4 评估报告（提案契约 `evolution-evaluation-report.schema.json`，与 E5 共享）

报告由 NX-039 写入，只追加，写入后不可修改。内容：

- 逐条硬门槛：门槛 ID、结论（`pass`、`fail` 或 `not_applicable`），以及证据 ID（例如 case_id、ref、拒绝码、表 digest）；
- 设计集与留出集上，Parent 和候选各自的指标值、差值、重复次数和区间（§3.3）；
- 匿名化的 swap 记录：每一对比较用的是种子，或逐条记录的交换结果（frozen-check §2.3 指出上游用的是 `random.random()`，这里必须可复现）；
- 费用、墙钟耗时、模型调用次数；
- 结论：`accept`、`reject` 或 `incomplete`，附原因码；
- `report_digest`：对报告的规范化 JSON 求 sha256，写进候选事件和 D4 审核关联表。

## 3. 评估执行

### 3.1 层级（沿用 NX-038 §12.3）

| 层级 | NX-039 怎么用 | 适用维度 |
|---|---|---|
| L0 确定性回放 | 冻结用例的语义访问回放：同一批查询（E3 记录的词项哈希），分别对 Parent 和“Parent 加叠加层”求覆盖率、歧义率和命中 ref。不用模型，不跑 Run | `semantic_content` |
| L1 test world 模型回放 | 在 `test` world，用专用评估凭据（只含 `test`）跑 Run，Context 带叠加层。用例只用合成样本，或由结构生成（NX-038 D16） | `semantic_tool`；需要模型判断的 `semantic_content` |
| L2 真实流量影子 | 不做（NX-038 D18、NX-043） | — |

### 3.2 指标（冻结在包里，NX-039 只计算，不定义）

- `semantic_content` 的 L0 指标：
  - 命中率：期望 ref 出现在 browse 前 k 项中的用例比例；
  - 歧义率：resolve 返回歧义的比例；
  - 误命中率：返回了期望之外的 ref，而且它排在期望 ref 之前的比例。
- L1 指标：
  - 场景成功率；
  - 工具参数错误率；
  - 每个 Run 的模型调用次数和 token 数。
- **非劣标准**（NX-038 D7，已定案）：设计集和留出集都要求 `候选 − Parent ≥ −1 个百分点`；并且至少在一个主要指标上**严格改善**，评分相等时不接受。

### 3.3 重复与随机性（D39-2）

- L0 是确定性的，只跑一次，但要验证可复现：同一个包重跑一次，报告 digest 必须相同。这一步计入 M22 的验收。
- L1 每个用例跑 3 次（模型温度按 `model_profile` 冻结）。指标取 3 次的中位数，报告同时写最小值和最大值。只要有一次触发硬门槛，就算这个用例失败，不取中位数。
- 匿名 A/B（移植的 `EvaluationGate`）：比较对的左右顺序由 `anonymize_seed` 决定，报告记录每一对的交换结果。

### 3.4 状态转移（NX-039 拥有）

| 从 | 到 | 条件 |
|---|---|---|
| `proposed` | `evaluating` | §2.2 复查通过 |
| `evaluating` | `rejected` | 任一硬门槛失败；或非劣不成立；或评分相等 |
| `evaluating` | `accepted_for_release` | 硬门槛全部通过，并且非劣成立、有严格改善 |
| `evaluating` | `incomplete` | `budget_exhausted`；`missing_data`（删除或快照失效）；`evaluator_error`（评估器异常，不能伪造结果）；`holdout_exhausted` |
| `accepted_for_release` | `incomplete` | 发布前出现删除失效（E4 已有） |

- 任何一轮结束后都不允许“再跑一次直到通过”。要再评，NX-038 必须重新冻结，生成一个新候选（NX-038 §12.4）。`maximum_rounds = 3`（D20）指的是同一假设最多重新冻结 3 次，每一轮都占用同一个候选级预算。

## 4. 硬门槛与留出集

### 4.1 硬门槛清单（`hard_gates_version` = `nx039-gates/1`）

硬门槛与评分分开计算，任一失败就是 `rejected`。门槛由代码定义，版本号进入评估包；NX-039 不能在包外增减门槛。

| ID | 门槛 | 怎么判 | 维度 |
|---|---|---|---|
| G1 | 引用与结构有效 | 补丁中的每个 ref 在 Parent 中存在且可读；补丁只有固定的四个键（E4）；`allowed_change_scope` 覆盖补丁的全部目标 | 全部 |
| G2 | 零越权 | 叠加层读取的每一项都带当前 READ 证明（与 NX-021 recall 的门控相同）；补丁和 scope 中没有 Action、授权事实、grant 或工具授权（AT-024） | 全部 |
| G3 | 零跨租户 | 用例、ref 和来源 Consumer 都属于同一租户；评估会话的租户取自凭据 | 全部 |
| G4 | 零事实污染 | 评估前后，受保护表的 digest 不变：active 定义、`ontology.object_type_versions`、别名表、业务对象、Claim、Commitment，以及语义版本当前指针（AT-023） | 全部 |
| G5 | 无禁止的行为回退 | 安全回归集全部通过：`restricted_after_refusal`、`human_takeover` 场景不发送；拒收后不联系；必需约束不丢失。L0 的对应项是：叠加层不能让任何“联系约束”“拒绝”类定义的命中率下降 | 全部 |
| G6 | 确定性语义回归 | 08 §6 v0.1 要求的语义回归套件全部通过 | 全部 |
| G7 | 成本与性能 | 不超过 §6 的冻结上限 | 全部 |
| G8 | 语义不冲突 | 别名文本不能在同一租户中已经指向另一个 canonical ref；描述改动不能让两个定义变得无法区分（同一用例上两者的命中分差缩到歧义阈值以内）。一旦冲突，就按 §7 判为 major，直接关闭 | `semantic_content` |
| G9 | 索引与迁移影响 | NX-039 判为 `not_applicable`；NX-040 启用（§12） | `ontology_schema` |

### 4.2 留出集与防泄漏

- **分组单位**：沿用 E4 的分组，同一个 Consumer 的相关会话是一个簇，没有 Consumer 的 Run 自成一簇。
- **开评前复查**（§2.2），在 E4 分组规则之外再加两条近重复规则：
  1. 设计集和留出集中出现相同词项哈希集合的用例对，视为同一簇；
  2. 同一 Consumer 在冻结窗口前后的 Run，不能一边在设计集、一边在留出集（时间泄漏）。
- 发现泄漏时，NX-039 **不自行重分组**，因为重分组属于冻结条件。候选保持 `proposed`，返回 `leakage_detected`，由 NX-038 重新冻结。报告不受影响，因为还没有开评。
- **冻结后不可修改**：留出集登记为一行，记录 `holdout_id`、用例 ID 集合的 digest 和创建时间，只追加。评估时用 digest 核对。

### 4.3 留出集的使用次数（D39-3）

同一个留出集反复用于选择候选，会让候选逐渐适配留出集，形成自适应过拟合。规则：

- 同一个 `holdout_id`，最多给 **5 个**候选做最终判定（不管结论是 accept 还是 reject）。
- 用满之后，新候选返回 `holdout_exhausted`，必须由 NX-038 用新的冻结窗口生成新的留出集。
- 报告写明这是该留出集的第几次使用。

## 5. 独立性：谁来评、凭据隔离、test world

### 5.1 三个角色互不重叠

| 身份 | 能做 | 不能做 |
|---|---|---|
| `nexloop_evo_reader`（E2） | 只读轨迹 | 写任何东西 |
| `nexloop_evo_writer`（E4） | 冻结诊断，提交候选，停止 `proposed` | 开评；写 `evaluating` 之后的状态；写报告 |
| **`nexloop_evo_evaluator`（新，D39-1）** | 开评、读包、读留出集、写报告、写 `evaluating` 之后的状态转移 | 提交或修改候选；改包；写定义、别名、语义版本、授权或业务对象；发布 |
| 人类审核员或负责人（受治理 Action） | 审核报告，发布或回退语义版本 | — |

- **生成者不评自己**：数据库层面，评估函数只授予 `nexloop_evo_evaluator`。这个登录角色的凭据与 writer 的不同，由不同的服务主体认证。同一个服务主体不能同时拿到 writer 和 evaluator 的授权，在授予函数里检查。
- **评估器代码独立**：`evaluator_version` 冻结在包里。评估器的代码（门槛、指标、L0 回放）与候选生成代码放在不同模块，候选生成模块不能 import 评估模块，由测试守护。
- **审核独立**：通过评估只是 `accepted_for_release`，发布还需要人类审核（§8），而且审核员不能是候选的来源 Consumer。

### 5.2 凭据隔离

- `nexloop_evo_evaluator` 没有任何效应、渠道或模型凭据。L1 的模型调用走 test world 的 runtime 路径，用专用评估凭据。
- 专用评估凭据只含 `test` world（0165 规则：含 real 就只能含 real），没有任何效应授权。效应执行入口只认 real，外部效应开关对非 real 关闭（0153 `closed_layer` 返回 `world`）。
- 评估器不读消息原文和 Claim 值（NX-038 §6、D5），只读 ID、哈希、ref 和计数。

### 5.3 test world 的边界

- L1 的 Run 在 test world 中。所用的定义从 real 的 Parent 只读复制一份到 test world（只复制定义和别名，不复制业务对象），叠加层再加在上面。
- 复制按 `parent_revision` 冻结，评估结束后删除，删除时留 tombstone。
- test world 的任何输出都不能回流到 real：报告只有数字与 ID；没有任何函数能把 test world 的对象写进 real。

## 6. 成本与性能上限

### 6.1 评估本身的花费（沿用 NX-038 §12.2 的三层上限，D20 已定）

- 评估的模型调用计入候选级预算：每个候选 ¥10，3 轮。
- 每次模型调用前调用 `authz.nexloop_evo_reserve`（E4）。用尽时转为 `incomplete/budget_exhausted`，保留 Parent，不给评分。
- 墙钟上限（D39-4）：每次评估 L0 最多 5 分钟，L0 加 L1 最多 30 分钟。超时就是 `incomplete/evaluator_timeout`。
- 租户每月 ¥100 的 evolution 预算（D15）也覆盖评估。

### 6.2 候选上线后的性能和成本（G7，冻结在包里）

| 指标 | 上限（推荐值，D39-5） | 测量 |
|---|---|---|
| 语义读取时延（browse/resolve） | p95 不高于 Parent 的 110%，而且增量不超过 20 ms | L0 回放时，在同一台评估机器上交替测量 Parent 和候选，各 50 次 |
| 上下文 token | `semantics` 段的 token 不高于 Parent 的 105% | L0：按 Context 的预算估算器计算 |
| 每个 Run 的模型调用次数和费用（仅 L1） | 不高于 Parent 的 105% | L1，取 3 次中位数 |
| 别名数量 | 单次发布最多新增 50 条别名，每个 ref 最多 20 条 | 静态检查 |

时延分解沿用 NX-031 §4.2。测量环境写进报告；跨机器得到的数字不能互相比较。

## 7. 语义版本规则

### 7.1 什么是语义版本

- 现在的语义版本只是一个计算出来的字符串：`semantic:<digest>@<recall config>@<profile>`（`SemanticAdapter._version`），没有发布记录，也没有回退目标。
- NX-039 新增**语义发布**（semantic release）：每个租户和 world 各自维护一个 `MAJOR.MINOR.PATCH` 版本号，以及一个指向当前版本的指针。
- 一个发布包含若干条发布项，每一项都来自一个 `accepted_for_release` 的候选。
- 发布项是**语义层的呈现内容**：别名，以及定义的补充说明（`semantic_note`）。它们不是 EIOS 正式定义。EIOS 的 `ontology.object_type_versions`、属性和权限不由语义发布改动（ADR-008 的三路分流：语义优化路径与正式 Schema 路径分开）。

### 7.2 变更分级

| 级别 | 变更 | NX-039 是否开放 |
|---|---|---|
| patch | 给已有 ref 增加别名（不与其他 ref 冲突，G8）；修改已有的补充说明，而且不改变 L0 的期望命中集合 | 开放 |
| minor | 给已有 ref 新增补充说明；同时动到多个 ref 的别名分配，但 G8 不冲突；`semantic_tool` 的说明变更提案（只是提案，见 §1） | 开放（`semantic_tool` 按 D39-6） |
| major | 别名改指向另一个 ref；删除别名（回退和删除传播除外）；补充说明与正式定义的含义冲突；任何让两个 ref 变得无法区分的改动；任何涉及正式定义、属性、类型、权限或指标口径的改动 | **一律关闭**：分类为 major 就转 `rejected/breaking_change`，不进审核队列 |

- **分类由函数决定**（D39-7）：`classify_change(parent, patch)` 返回 `patch`、`minor`、`major` 或 `refused`，并写进报告。审核员和 Agent 都不能手工改级别。
- **只升不降**：一次发布含多个候选时，版本号按其中最高的级别递增。
- **回退不改版本号的单调性**：回退会产生一个新的发布行（例如从 `1.4.0` 回退到 `1.3.2`，新行是 `1.4.1`，内容等于 `1.3.2`，原因 `rollback`），版本号不会往回走，便于审计和缓存失效。

## 8. 发布与回退

### 8.1 原则

- **只能走受治理路径**：发布和回退都是受治理的人类 Action（`SemanticRelease.publish`、`SemanticRelease.rollback`），通过 Action 注册表、当前权限和 security definer 函数执行。
- **不绕过 EIOS**：语义发布不写 EIOS 正式定义和授权。需要正式 Schema 的变化走 EIOS Schema 流程，不在本任务内。
- **Evo 不能控制发布**：
  - `nexloop_evo_writer`、`nexloop_evo_evaluator` 对发布函数没有执行权限；
  - 候选契约中的 `publication_permit_ref` 恒为 null，契约不改；发布许可存在发布记录里，不在候选上；
  - Evo 模块不调用部署脚本，不持有数据库管理员凭据，也不能改 Model Gateway 配置（02 第 97 行）。
- **发布不需要部署**：发布是数据库里的一次受治理写入，不经过发布脚本、镜像或迁移。

### 8.2 发布流程

1. 候选为 `accepted_for_release` 时，报告 digest 写入 D4 审核关联表，候选进入现有的审核队列（NX-046 工作台）。
2. 审核员在工作台查看：报告的门槛表、指标、分级，以及补丁内容（只有 ref、别名或说明，没有消息原文）。
3. 审核员执行 `SemanticRelease.publish`（scope `ontology.semantic.release`，D39-8）。函数在同一个事务中：
   - 复查：候选仍是 `accepted_for_release`；报告 digest 与关联表一致；分级不是 major；维度没有被停止；没有 Consumer 正在删除；Parent 等于当前发布（不一致就是 `parent_moved`，需要重新评估）；
   - 写一行发布记录和对应的发布项，把当前指针移到新版本；
   - 把候选标为已发布（写候选事件 `published`，并引用发布 ID；候选状态本身仍是 `accepted_for_release`，因为契约里没有 published 状态，08 §5 也说明“accepted_for_release 不等于已发布”）；
   - 对受影响的 ref 排入 recall 重建（复用 NX-045 的 reflow 队列），不同步重建。
4. 审核员身份由服务端派生（ADR-019、NX-044）。审核员不能是候选的来源 Consumer。

### 8.3 读取

- recall 与定义读取（`authz.nexloop_context_read` 的 definitions，以及 NX-021 recall）在 NX-045 别名之外，再读取当前发布的发布项。
- 当前指针按租户和 world 存储；只有 real world 有正式发布，test world 的叠加层仍走 E5 的 `evaluation_overlay`。

### 8.4 回退

- `SemanticRelease.rollback(target_version)`：受治理的人类 Action。它写一个新发布行，内容等于目标版本，原因 `rollback`，再移动指针，并对受影响的 ref 排入 reflow。
- **发布项只追加**，回退不删除任何行。
- **不倒退真实业务事实**（M12）：在被回退的发布生效期间，已经由别名匹配产生的 Claim 和实例更正保持不变。回退报告列出这段时间内经被回退别名匹配的 Claim ID（只有 ID 和计数），交人工复核（D39-9）。
- **自动回退**：不做（D39-10）。发布后有 7 天观察期，在线指标（E3 记录的未命中率、歧义率）劣于发布前一周超过 2 个百分点时，给负责人告警，并在工作台提示回退，由人决定。这个 2 个百分点的阈值也是 NX-040 Schema 发布观察期的统一阈值；Schema 变更另外监测抽取错误率（负责人 2026-10-20，NX-040 分歧 3）。

### 8.5 停止

- 负责人可以按维度停止演进（08 §6“保留人类停止/回退入口”），这是一个受治理 Action。
- 停止后：
  - 新的开评返回 `dimension_stopped`；
  - 正在评估的候选转为 `incomplete/stopped`；
  - 发布被拒绝；
  - 回退不受影响，停止期间仍然可以回退。

## 9. 删除传播（接 NX-029 与 NX-038 §12.5、D17）

| 对象 | Consumer 被删除时 | 依据 |
|---|---|---|
| `evaluating` 中的候选 | 转为 `incomplete/missing_data`，正在跑的 L1 Run 取消，test world 中的复制删除 | E4 的触发器扩展到 `evaluating` |
| 留出集 | 含该 Consumer 的留出集标为失效，之后的开评返回 `holdout_exhausted`，原因写 `missing_data` | §4.3 |
| 评估报告 | 报告只追加。报告中的用例 ID 写成“用例 ID 的 digest 加计数”，不直接写 Run ID 或 Consumer ID，所以删除后不需要改报告，也无法反推出被删除的人 | D39-11 |
| 已发布的发布项 | 来源是该 Consumer 措辞的别名（经 E4 的 `nexloop_evo_patch_sources` 追溯）：写一个新发布，原因 `erasure`，内容是去掉这些别名的当前版本；同时排入 reflow。不能等人工 | D17 |
| 回退目标 | 删除之后，回退到任何旧版本时，都会过滤掉已删除来源的发布项，避免删除后被回退恢复回来 | 删除优先于回退 |

- **保留期**（D39-12）：评估报告、发布记录和发布项保留 365 天，走 NX-029 的 `governance_audit` 口径；当前版本和它之前的一个版本永远保留，以便回退。
- 每次删除都留 tombstone，只记计数，原因为 `consumer_erasure` 或 `retention_expiry`。

## 10. S6 验收覆盖

| 验收 | 怎么覆盖 | 测试形态 |
|---|---|---|
| AT-023 候选隔离 | G4：评估前后受保护表的 digest 不变；叠加层只在非 real 会话中生效；未通过评估的候选不改变当前发布指针；发布只能经人类 Action | PG：一个候选完整走完 `proposed → evaluating → rejected`，前后 active 定义、别名表、语义发布指针和业务对象的 digest 不变；real 会话带叠加层被拒绝；evaluator 角色调用发布函数被拒绝 |
| AT-024 结构权限 | G2：补丁或 scope 中有 Action、授权或 grant 就不通过；major 一律关闭；`ontology_schema` 不进入评估执行；发布函数不写授权 | PG：构造“附带提权 Action 的类型候选”，开评返回 `dimension_closed`，即使绕过开评直接调用门槛函数也返回 G2 fail；发布前后授权事实的 digest 不变 |
| AT-048 验证泄漏 | §2.2、§4.2：开评前复查簇和近重复；留出集冻结后不可修改，用 digest 核对；使用次数有上限 | 单元与 PG：同一消费者的相关对话跨两边时返回 `leakage_detected`；冻结后修改留出集被拒；第 6 次使用返回 `holdout_exhausted` |
| AT-046（S7，负例部分） | 评估器没有效应凭据；L1 只在 test world；evaluator 凭据含 real 时被拒 | 复用 `tests/test_at046_simulation_isolation_pg.py` 的断言，加上评估凭据只含 `test`、evaluator 调用效应入口被拒 |
| M12 验收 | 缺少报告、门槛失败、分级为 major 时不能发布；回退不倒退业务事实 | PG：没有报告 digest 时发布被拒；回退后 Claim 与对象的 digest 不变，回退报告列出受影响的 Claim ID |
| M22 验收 | 同一个包重跑得到同一份 L0 报告 digest；硬门槛与评分分开 | 单元：L0 重放 digest 相同；构造“评分最高但 G5 失败”的候选，结论为 reject |

## 11. 实施切片与工作量

| 切片 | 内容 | 迁移与契约 | 估计 |
|---|---|---|---|
| V1 评估器角色与开评 | `nexloop_evo_evaluator`；§2.2 复查；`proposed → evaluating`；留出集登记与使用计数 | 迁移 A；报告契约（与 E5 共享） | 2 天 |
| V2 门槛与指标 | G1–G8 实现与门槛注册表；L0 指标、非劣判定、重复、匿名 A/B 种子；报告写入；`classify_change` | 无新迁移（用迁移 A 的报告表） | 3 天 |
| V3 L1 test world | 定义复制、专用评估凭据、3 次重复、预算与墙钟；G5 安全回归集 | 迁移 B（复制与清理） | 2.5 天 |
| V4 语义发布与回退 | 发布表与发布项、当前指针、两个人类 Action、recall 与定义读取合并发布项、reflow、停止开关 | 迁移 C；Action 注册（受治理） | 3 天 |
| V5 删除传播与观察期 | §9 全部；回退过滤；7 天观察告警；保留期类别 | 迁移 D | 2 天 |
| V6 工作台 | 审核队列展示报告、门槛和分级；发布、回退、停止按钮（复用 NX-046 页面框架） | 无 | 2 天 |

- 合计约 **14.5 个开发日**，大约 6 轮 CI。
- 顺序：V1 → V2 → V4 → V5 → V6。V3 可以与 V4 并行，前提是 D39-6 决定开放 `semantic_tool`；不开放时 V3 推迟。
- 每个切片单独交付，迁移用临时编号。新增的受治理 Action 需要同步更新 `test_stage_register_pg` 中的 Action 计数，并在部署单中写明 stage_secrets publish 的 dry run。

## 12. 给 NX-040 的接口

NX-040「开放兼容 Schema 受控演进」（交付物：迁移、索引与工具兼容，权限不变，破坏性变更关闭）复用 NX-039 的评估、分级和发布骨架，只增加 `ontology_schema` 维度特有的部分。L4 起草 NX-040 时，请引用下面六个接口。

### 12.1 评估入口：同一套包、报告与状态机

- NX-040 使用同一份 `evolution-evaluation-bundle` 和 `evolution-evaluation-report` 契约，`dimension = ontology_schema`。
- 开评、状态转移和报告写入都走 NX-039 的同一组函数（`nexloop_evo_evaluator`）。NX-040 不新增第二个评估器角色。
- NX-039 在 §2.2 用 `dimension_closed` 拦下 `ontology_schema`。开放的方式只能是：NX-040 交付 G9 并完成负责人决定之后，以一个迁移修改开放维度的清单。这个清单是数据库中的一行可审计配置，不是代码常量。

### 12.2 门槛注册表

- 门槛按 `hard_gates_version` 注册。NX-039 定义 `nx039-gates/1`（G1–G9，其中 G9 为 `not_applicable`）。
- NX-040 发布 `nx040-gates/1`：沿用 G1–G8 不改，启用 G9，并可以追加门槛，例如：
  - G10 迁移可回滚，并且对现有行的影响有上限；
  - G11 索引重建的时间和空间有上限；
  - G12 工具与 Context 契约兼容；
  - G13 授权事实的 digest 前后不变。
- 每个门槛函数的签名是：`gate(bundle, parent_snapshot, candidate_snapshot) → {id, result: pass|fail|not_applicable, evidence_ids[]}`。
- 门槛只能新增，不能修改语义。修改门槛就要升级 `hard_gates_version`，旧的包仍按旧版本判定。

### 12.3 变更分级函数

- `classify_change(dimension, parent, patch) → patch | minor | major | refused`，由 NX-039 实现 `semantic_content` 部分。
- NX-040 为 `ontology_schema` 补充以下分级（建议值，最终由 NX-040 决定）：
  - minor：新增可选属性；新增受约束的新类型（PRD M12、08 §6 v0.2b）。负责人 2026-10-20（NX-040 分歧 2）：新类型的分级是 minor，但它的发布方式由 NX-040 另行规定，只能人工发布，不进入任何自动发布路径。
  - major，一律关闭：删除、改类型、改权限、改指标口径。
- 两个维度共用同一条规则：**major 一律关闭，不进审核队列**。

### 12.4 发布许可

- 语义发布（§8）只覆盖语义层，不覆盖 EIOS 正式 Schema。
- `ontology_schema` 候选通过评估后，NX-039 提供一个**发布许可记录**（`evolution_release_permits`：candidate_id、report_digest、分级、门槛版本、签发时间、到期时间）。NX-040 的 EIOS Schema 发布函数在执行前核对这份许可：
  - 许可存在、没有过期、没有被使用；
  - 许可中的报告 digest 与关联表一致；
  - 分级不是 major。
- 许可只能使用一次，使用时写入 EIOS Schema proposal 的引用。
- 许可不授予任何 Action 权限：定义发布与权限发布分开（AT-024）。

### 12.5 回退与删除的语义

- 语义发布的回退（§8.4）不触及正式 Schema。NX-040 的 Schema 回退是**发布一个新的 Schema 版本**，内容等于回退目标，经与正式发布相同的那条发布链（许可核对、受治理 Action、reflow），**不生成 SQL 迁移**；也不能借用 `SemanticRelease.rollback`（负责人 2026-10-20，NX-040 分歧 1）。
- 删除传播（§9）的规则对 NX-040 同样适用：许可只存 ID 与 digest；候选来源 Consumer 被删除时，未使用的许可作废。

### 12.6 独立性与凭据

§5 的角色划分、凭据隔离和 test world 规则对 NX-040 原样适用。G10、G11 的迁移影响检查只能在一次性的测试数据库或 test world 中执行，不能在 real 上试运行迁移（AGENTS.md：不在生产库上试迁移）。

## 13. 负责人决定事项（已决定，负责人 2026-10-20）

【负责人 2026-10-20：「S6 全部按推荐」，D39-1 到 D39-14 全部按下表的推荐定案。】

| 编号 | 问题 | 推荐 | 理由 | 代价 |
|---|---|---|---|---|
| D39-1 | 评估器的身份 | 新建登录角色 `nexloop_evo_evaluator`，只能执行评估函数；同一服务主体不能同时拿到 writer 和 evaluator 的授权 | “独立评估”要求生成者不能评自己；与 D1、E4 的角色划分一致 | 多一个角色与凭据要管理 |
| D39-2 | 评估的重复次数 | L0 跑一次，再重放一次验证 digest 相同；L1 每个用例 3 次，取中位数；任一次触发硬门槛就算失败 | L0 是确定性的；L1 有模型随机性，3 次是成本与稳健之间的折中 | L1 的费用是单次的 3 倍 |
| D39-3 | 留出集的使用上限 | 同一个留出集最多判定 5 个候选，之后必须换新的冻结窗口 | 防止自适应过拟合；早期候选少，5 次够用 | 留出集消耗快时，需要更多真实轨迹 |
| D39-4 | 评估墙钟上限 | L0 5 分钟；L0 加 L1 30 分钟 | 与诊断的 30 分钟上限（D20）同量级；L0 本身很快 | 大用例集的 L1 可能跑不完，只能转为 incomplete |
| D39-5 | 上线后的性能与成本上限 | 语义读取 p95 不高于 Parent 的 110% 且增量不超过 20 ms；`semantics` 段 token 不高于 105%；L1 的每个 Run 的模型调用次数和费用不高于 105%；单次发布最多 50 条别名，每个 ref 最多 20 条 | 语义层的改进不应让 Run 变慢或变贵；别名上限防止召回噪声 | 数值偏保守，需要用实测调整 |
| D39-6 | NX-039 是否开放 `semantic_tool` 的评估 | 先不开放，只开放 `semantic_content`；`semantic_tool` 在 V3（L1）跑通、且 NX-038 D16 的合成用例够用时再开，届时只产出代码变更提案 | 与 D19 一致；L1 的成本和代表性都还没有验证 | 工具说明类的改进更晚 |
| D39-7 | 变更分级由谁决定 | 由 `classify_change` 函数决定并写入报告，人和 Agent 都不能手工改 | 避免把破坏性变更标成 patch 绕过关闭规则 | 分类规则要覆盖周全，有遗漏时偏向 major |
| D39-8 | 发布与回退的权限 | 新 scope `ontology.semantic.release`，默认只授予负责人；审核员需要负责人单独授予；回退与发布用同一个 scope | 发布改变所有 Run 看到的语义，影响面大于单条审核 | 负责人要多处理一类审批 |
| D39-9 | 回退时，已经经别名匹配产生的 Claim 怎么处理 | 不自动改动；回退报告列出这些 Claim 的 ID 与计数，交人工复核 | M12：回滚语义版本不自动倒退真实业务事实 | 人工复核的工作量 |
| D39-10 | 是否自动回退 | 不自动回退；发布后观察 7 天，在线未命中率或歧义率劣化超过 2 个百分点时告警并提示回退 | v0.2a 保留“人类停止/回退入口”（08 §6），自动回退可能在误报时来回抖动 | 劣化期间要等人处理 |
| D39-11 | 报告里如何引用用例 | 只写用例 ID 集合的 digest 和计数，另存一张可删除的用例引用表 | 报告只追加且要长期保留，不能含可在删除后反推个人的 ID | 复核报告时要再查引用表 |
| D39-12 | 评估与发布记录的保留期 | 报告、发布记录和发布项保留 365 天（`governance_audit` 口径）；当前版本和前一个版本永久保留 | 与 NX-029 D6 一致；回退至少需要前一个版本 | 存储略增 |
| D39-13 | 语义发布项放在哪里 | 新建发布表与发布项表，recall 与定义读取把它与 NX-045 的别名合并读取；不改 NX-045 的别名表（它的 `candidate_id` 外键指向抽取候选） | 不改已有表的语义，两条来源（人工审核的抽取别名与演进发布）可以分别回退 | 读取路径多合并一个来源 |
| D39-14 | 补充说明（`semantic_note`）能否进入 Context | 能，作为 `semantics` 段的说明文字；只能来自发布项，不能来自候选；长度上限 200 字 | 描述类改进需要到达模型才有效 | 多一个需要审核措辞的内容来源 |

## 14. 契约与迁移（只是提案）

| 项 | 内容 | 切片 |
|---|---|---|
| 迁移 A | `nexloop_evo_evaluator` 角色；评估报告表（只追加）；留出集登记与使用计数；开评与状态转移函数；writer 与 evaluator 授权互斥的检查 | V1、V2 |
| 迁移 B | test world 的定义复制与清理函数；专用评估凭据的发放（只含 `test`） | V3 |
| 迁移 C | `ontology.nexloop_semantic_releases`（版本号、指针、原因：publish、rollback、erasure）与 `ontology.nexloop_semantic_release_items`（ref、别名或说明、来源候选、来源 Consumer 引用）；发布、回退、停止三个受治理 Action 的 definer；definitions 读取与 recall 合并发布项；scope `ontology.semantic.release`；`evolution_release_permits`（§12.4，只建表，签发留给 NX-040 启用） | V4 |
| 迁移 D | 删除传播触发器的扩展（`evaluating`、留出集、发布项）；保留期类别；观察期指标函数 | V5 |
| 契约 | `evolution-evaluation-report.schema.json`（与 E5 共享，单独 PR）；`semantic-release.schema.json`（发布记录：版本、原因、发布项 ref、报告 digest；单独 PR）；`evolution-candidate.schema.json` 不改（`publication_permit_ref` 仍为 null） | V1、V4 |

- 本稿不改 `packages/contracts/`，也不加迁移。迁移在实现时用临时编号，合并时由调度员重新编号。
- 新增受治理 Action 时，需要同步：`deploy/configuration/business-actions.v1.json`、能力清单、`test_stage_register_pg` 的计数，以及部署单中的发布核对。

## 15. 不在本任务范围

- 诊断、归因、候选生成和评估包冻结：NX-038。
- 正式 Schema 的兼容演进与发布：NX-040（接口见 §12）。
- 影子运行、模拟世界和线上实验：NX-043 与 S7。
- 工具清单和提示词的实际修改：按代码评审流程处理，NX-039 只产出提案。
- 跨租户聚合：v0.2a 不做（NX-038 D6）。
