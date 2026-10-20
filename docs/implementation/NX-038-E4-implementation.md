# NX-038 E4：归因与候选（实现说明，2026-10-20）

依据：NX-038 设计稿 §3.1、§3.2、§11.3、§12.2、§12.5、§12.6、§12.7 E4。D15–D20 已定案。分支 `l4/nx038-e4`，起点 c5d9806c（第一批集成，迁移到 0234）。

## 归因规则（`nexloop_eios/evolution/attribution.py`，纯逻辑；写入端口在 `nexloop_eios/evolution_store.py`，包外）

- **输入**：
  - E2 的轨迹样本（`trajectory-sample`）；
  - E3 的语义访问记录（`authz.nexloop_semantic_access_read` 的返回值）。
- 两者都只有 ID、代码、计数、哈希和 ref，所以产出也只有这些，不读也不产出消息原文、Claim 值、提示词或查询原文。
- **分类**：每个样本只落一个类别。按下面的顺序判定，先命中的类别生效。每条都带证据链（run_id、interaction_id、场景、人设、相关的 access_id、ref、词项哈希或失败码）和一句理由。

| 顺序 | 类别 | 判据 | 维度 |
|---|---|---|---|
| 1 | `filtered` | `test_stack` | 只计数 |
| 2 | `not_semantic` | 拒绝码 NXC*/NXB*；错误码 interaction_timeout、reconciliation_missing、effect_evidence_unavailable、delivery_missing；对账 unresolved 或意图 unknown | 不归因 |
| 3 | `safety_regression` | `unexpected_delivery`，或者 restricted_after_refusal / human_takeover 场景中发生了发送 | 交给安全回归，不进候选 |
| 4 | `expected_behavior` | 上述两个场景中没有发送 | 不算失败 |
| 5 | `tool_misuse` | 失败落在 `unregistered_tool`，或者某个工具出现参数类失败码 | `semantic_tool`（只标记） |
| 6 | `ambiguity_unresolved` | resolve 返回歧义，且这次访问的结果没有被使用（没有 `use` 记录），Run 失败 | `semantic_content` |
| 7 | `retrieval_miss` | browse 无结果，或 resolve 的全部词项都未解析，Run 失败 | `semantic_content` |
| 8 | `none` | 没有语义信号 | — |

- **弱信号**：`recovered_tool_errors` 中同一 (tool, code) 在一次诊断里出现至少 3 次、涉及至少 2 个 Consumer（D13）时，标记为 `semantic_tool`，不生成候选。
- **没有实现的两类**：`context_dropped` 和 `definition_gap` 需要的输入（上下文来源的省略原因、未匹配的候选定义）不在 E2/E3 的输出中，E4 不产出这两类。
- **冻结**：`freeze_digest` 是样本快照 digest 与访问记录的规范化 digest，同样的输入得到同样的 digest（§2.3）。诊断 ID 由 digest 经 uuid5 算出，所以重复提交会回放同一个诊断。

## 候选草稿（D19）

- **只开放 `semantic_content` 维度**。`semantic_tool` 和 `ontology_schema` 只计数（诊断的 `attribution` 里记为 `marked_*`），SQL 也拒绝这两个维度的候选。
- **分组**：
  - `retrieval_miss` 按未命中的词项哈希分组；
  - `ambiguity_unresolved` 按歧义涉及的 ref 集合分组。
  - 每组至少要有 3 个 Run、2 个簇（与 D13 的门槛相同），不够的计入 `below_minimum`。
- **一个候选只有一个假设**：
  - 假设文字由规则生成，只含哈希前缀和计数；
  - `allowed_change_scope` 只列定义的 ref；
  - 补丁 `{change_kind: alias|description, target_refs, term_hashes, run_ids}` 不含任何自由文本。
- **AT-048 分组**：簇按 Consumer 划分（同一 Consumer 的相关会话是一个簇；没有 Consumer 的 Run 自成一簇）。
  - 先按 Run 的哈希粗分。
  - 如果同一簇被分到两边，就把整簇移到它多数成员所在的一边，记一次重分组。
  - 如果某一边为空，再移一个簇过去，也记一次。
  - 重分组次数随候选一起保存（`regroups`）。
- **轮数**：`maximum_rounds = 3`（D20）。

## 迁移 0235（临时编号，`0235_nx038_attribution_candidates.sql`）

- **D15**：
  - `budget_kind` 的 check 放宽为 model、incentive、evolution；evolution 的单位必须是 ISO 货币代码。
  - 新加一条消费 check：budget_kind 为 evolution，当且仅当 source_ref 是 `evolution-diagnosis:<uuid>` 或 `evolution-candidate:<uuid>`。所以演进费用只计入 evolution，其他类不能借用演进来源。
  - 负责人仍通过受治理的 `Budget.set` 设置。Python `GoalControls.set_budget` 同步接受 evolution。
- **角色**：新增 `nexloop_evo_writer`（login、noinherit），只能执行下面的 authz 函数。
  - 调用方必须是有效的服务凭据，不带 Run 上下文，也不是浏览器令牌；
  - tenant 取自凭据，world 只能是 real。
  - E2 的 `nexloop_evo_reader` 仍然只读，不能执行任何写入函数。
- **表**：都在 runtime 下，RLS 按租户强制，应用角色没有任何表权限。
  - `nexloop_evo_diagnoses`：冻结快照，含 digest、Run ID（不超过 2000）、Consumer ID、版本、归因计数、费用上限 20、截止时间（开启后 30 分钟）、状态。
  - `nexloop_evo_candidates`：按契约落库；dimension 由 check 固定为 semantic_content；轮数不超过 3，费用上限 10；设计用例与留出用例不能重叠。
  - `nexloop_evo_patches`：补丁（ID、哈希、ref）。
  - `nexloop_evo_patch_sources`：补丁的来源 Consumer（D17 的引用表）。
  - `nexloop_evo_candidate_events`：事件表，只追加，owner 也不能 UPDATE、DELETE 或 TRUNCATE。
- **函数**：
  - `authz.nexloop_evo_diagnosis_open` / `_close`：冻结与关闭诊断；
  - `authz.nexloop_evo_candidate_propose`：提交候选；
  - `authz.nexloop_evo_candidate_read`：读取候选；
  - `authz.nexloop_evo_candidate_stop`：只能把 proposed 转为 incomplete；
  - `authz.nexloop_evo_reserve`：三层预算。
- **propose 的检查**（任何一项不符都拒绝）：
  - 维度（D19）、轮数（D20）；
  - 与冻结诊断的 digest、Parent、评估器版本、模型配置一致；
  - 用例都属于诊断中的 Run，而且不重叠；
  - scope 只允许 `eios:object_type:`、`eios:property:`、`nexloop:vocabulary:` 开头的 ref（AT-024：Action、授权事实、grant、工具一律拒绝）；
  - 每个用例都有簇，并且没有簇同时出现在两边（AT-048）；
  - 补丁只有四个固定键；
  - 来源 Consumer 都属于诊断，而且没有在删除中。
- **边界**：迁移不写定义、别名、授权事实、业务对象、Claim 或 Commitment，没有发布或激活入口（ADR-007、D18）。NX-038 只写 proposed，以及 proposed 到 incomplete 的转移；`evaluating` 之后的状态属于 NX-039。唯一的例外是删除失效。

## 三层预算（§12.2，D20）

`authz.nexloop_evo_reserve(scope, id, consumption_id, amount, unit)` 在每次模型调用前按下面的顺序检查：

1. 记录仍然开放；
2. 诊断没有超过 30 分钟；
3. 诊断累计不超过 20、候选累计不超过 10；
4. 租户本期 evolution 限额（锁住限额行，用法与 0068 相同）。

任何一层用尽时：

- 诊断或候选转为 `incomplete/budget_exhausted`；
- 函数返回 `reserved=false` 和用尽的层（tier），不记任何消费；
- 函数不抛异常，所以状态转移会提交。

同一个 consumption_id 重放时，返回原来的结果。E4 本身没有模型调用（L0 是确定性的），这个函数是给 E5/NX-039 用的入口。

## 删除失效（§12.5，D17）

- `control.nexloop_consumer_erasures` 在插入或状态变为 purging、completed 时触发 `runtime.nexloop_evo_invalidate_consumer`：
  - 以这个 Consumer 为来源的补丁被删除，来源行级联删除；
  - 对应候选转为 `incomplete/missing_data`，包括已经 `accepted_for_release` 但还没发布的；
  - 冻结快照中含有这个 Consumer 的诊断，连同它下面未终结的候选，也转为 `incomplete/missing_data`；
  - 写一条 tombstone，item_class 为 `evolution_candidate`，只记计数。
- 已经发布的别名不在演进范围内，走 NX-029、NX-045 的现有路径。
- 新诊断不能包含正在删除或已删除的 Consumer。

## 测试

- `tests/test_evolution_e4_pg.py`：
  - 纯规则：分类顺序与维度标记、D13 弱信号、digest 可复现、2000 上限；
  - AT-048 分组：不泄漏，重分组被计数，结果确定；
  - 草稿门槛与确定性；
  - PG：
    - 往返与隔离（AT-023：受保护表的 digest 不变；读回的值就是契约值）；
    - AT-024、D19、D20、AT-048 的拒绝；
    - 角色与租户边界；
    - 三层预算；
    - 删除失效。
- `tests/test_goal_controls.py::test_nx038_d15_owner_sets_the_evolution_budget_through_budget_set`：负责人经受治理的 Budget.set 设置 evolution 预算；它不计入 model；通用的预留不能把 evolution 花在非演进来源上。

## 不在 E4 范围

- 叠加层与 L0 回放、评估包与报告契约、审核关联表（D4）：属于 E5。
- 工作台展示：在 NX-039 之后。
- 候选表的保留期：还没有纳入 NX-029 的保留类。候选只含 ID、哈希和 ref，删除由上面的失效路径覆盖。
