# NX-040 F4：门槛注册、许可核对与观察期（实现说明，2026-10-20）

依据：NX-040 设计稿（e4dee217）§4.2–§4.6、§6、§7、§11 F4；D21–D27 已定案；NX-039 设计稿 §12。分支 `l2/nx040-f4`，起点 `int/post-v010-batch5`（e9aa8fbc）。

## 迁移 0257（`0257_nx040_gates_permits_watch.sql`，临时编号）

- **门槛注册**：
  - 新函数 `runtime.nexloop_evo_hard_gates_for(dimension)`：ontology_schema 用 `nx040-gates-1`，其余维度仍用 `runtime.nexloop_evo_hard_gates()`；
  - `nx040-gates-1` = G1–G8 不变，加上启用的 G9 `index_migration_impact`，以及新增的 G10 `migration_bounded`、G11 `reflow_budget`、G12 `tool_context_compatible`、G13 `authority_unchanged`；
  - `authz.nexloop_evo_bundle_freeze`（0237 函数体原地替换）按候选的维度冻结门槛清单；
  - Python 的 `GATE_VERSIONS` 同步登记；报告和评估包契约的门槛枚举只做新增。
- **发布许可**：
  - V4 实际没有建许可表（全仓库没有 `release_permit`），所以本迁移按 39 稿 §12.4 补上 `runtime.nexloop_evo_release_permits`；
  - 签发：评估器调用 `authz.nexloop_evo_permit_issue`，前提是候选为 ontology_schema、状态 `accepted_for_release`，最新报告结论为 accept，分级 minor，门槛版本 `nx040-gates-1`；有效期 7 天；同一个候选同时只有一张未使用的许可；
  - 发布函数（0254 函数体原地替换）核对后消耗许可，拒绝码为 `permit_missing`、`permit_used`、`permit_expired`、`permit_mismatch`（报告、分级或门槛版本对不上）；
  - proposal 的许可引用改为 `permit:<id>`；
  - 许可只能被标记为“已使用”一次，此外不可修改、不可删除，也不授予任何权限（AT-024）。
- **重流预算**（G11、D26）：`authz.nexloop_evo_reflow_reserve` 把预计的重流费用记在 evolution 下、来源是候选，检查候选上限和租户月度限额。超限时候选转为 `incomplete/budget_exhausted`。
- **迁移上限**（D25）：
  - 门槛函数（0254 函数体原地替换）在对象超过 10 000 个时拒绝（`migration_over_limit`），除非审核员选择分批发布；
  - 分批发布时，发布事务本身只迁移前 10 000 个对象，剩余数量记入 `ontology.nexloop_schema_migration_batches`；
  - 之后由同一个审核员入口 `authz.nexloop_schema_migrate_batch` 每次再迁 10 000 个。迁移期间旧版本的对象仍然有效，因为变更只是加法。
- **观察期**（D27）：
  - 每次 Schema 发布（publish 或 hide）都开始 7 天观察，记录在 `runtime.nexloop_schema_watches`；
  - `runtime.nexloop_schema_watch` 比较发布前一周和发布以来的两项指标：
    - 抽取任务失败率：上升超过 2 个百分点，且两段各至少 20 个任务，算劣化；
    - Context 体积（`budget_report.used`）：超过 105%，且两段各至少 20 个 pack，算劣化。
  - 劣化时 regressing，建议发布 hidden 的下一版本（`hide`），不自动回退；
  - 指标快照新增 `evolution.schema_watch`。
- 回退用的 hidden 版本沿用 F3 的 `hide`。hide 同样开始观察，不需要许可，因为回退是人的决定。

## 迁移 0258（`0258_nx040_open_ontology_schema.sql`）

只做一件事：把 `ontology_schema` 写进 NX-039 的开放维度清单（D21）。以后负责人如果要回退，删掉这一行即可，开评会重新返回 `dimension_closed`。

## 告警规则 v9

保留 v8 的全部规则，新增 `schema_release_regressing`（warning）。

## Python

- `nexloop_eios/schema_gates.py`：回答 `nx040-gates-1`，包括：
  - F1 判定的码，映射到 G1–G13；
  - 工具回归集回放（D23）：每个 Action 至少有一个合法和一个非法用例，用例在新旧两个版本下的结果必须相同，并且符合预期；用例标识与顺序无关；
  - Context 体积不超过 105%；
  - 重流预留成功；
  - 分批发布视为满足迁移上限；
  - 跑两遍核对可复现。
- `EvaluatorStore` 新增 `permit_issue` 和 `reflow_reserve`。
- `SchemaPublicationPort` 的 `publish` 新增 `batch` 参数，并新增 `migrate_batch`。

## 测试

- `tests/test_nx040_f4.py`（纯逻辑，18 项）：注册表；全部通过的正例；12 种门槛失败；分批；工具回归的合法、非法和缺用例；major 关闭；非 nx040 的评估包不回答。
- `tests/test_nx040_f4_pg.py`（5 项）：
  - 许可的四个拒绝码，以及成功发布时消耗许可并写入 proposal；许可不能再改；
  - 超过 10 000 个对象：不分批时拒绝；分批时先迁 10 000 个，再迁剩余部分；
  - ontology_schema 的评估包冻结 `nx040-gates-1`，semantic_content 仍用 `nx039-gates-1`；维度已打开；
  - 观察期告警阳性对照：失败率持平时不告警，上升后告警，建议 hide，没有任何回退；
  - 端到端：E4 候选 → F2 冻结 → 评估包 → V1 开评 → F4 门槛（工具回归、Context、重流预留计入 evolution）→ conclude → 签发许可 → F3 由人发布并消耗许可 → 开始观察。
- 改动的已有测试：
  - F3 测试为每个 proposal 预置许可，完整的签发路径在 F4 的端到端测试里；
  - F2 的“评估维度保持关闭”改为“已打开，删除这一行即关闭”。

## 限制

- 端到端测试里的 Action 原本输入 schema 为 `{}`，接受任何参数，造不出非法用例；测试先把它改成一个真实的对象 schema。生产中如果有接受一切输入的 Action，G12 会一直拦下它，需要补上输入 schema。
- hide 之后，抽取侧对被隐藏属性的匹配不会自动停止（设计稿 §6 提到“同时停止抽取侧的匹配”），需要后续切片处理。
- V5 留给 V4 的三个钩子（发布项删除、保留报告、语义观察起点），批次 5 中仍然没有接上。这不在 F4 范围内。
