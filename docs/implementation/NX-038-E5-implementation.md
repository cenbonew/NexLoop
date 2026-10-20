# NX-038 E5：隔离与交接（实现说明，2026-10-20）

依据：NX-038 设计稿 §12.3、§12.4、§12.5、§12.7 E5，D4、D7、D16、D18、D19（均已定案）。分支 `l6/nx038-e5`，起点 `l4/nx038-e4`（1522c521，迁移到 0235）。

## 叠加层（§12.3，AT-023）

- **SQL 入口** `authz.nexloop_evo_overlay(digest, world, candidate_id, patch_digest)`，只授予 Context Engine 的 `nexloop_api` 和 `nexloop_domain_worker`：
  - world 不是 `test` 时，在入口就拒绝（42501）。这是第二道防线，real 会话无论传什么凭据都读不到候选；
  - 凭据必须已登记为本租户的**评估凭据**（D18，见下文）；
  - 候选必须属于同一租户、状态为 `evaluating`，补丁 digest 也必须一致；
  - 只返回 `change_kind`、`target_refs`、`term_hashes`，不返回补丁的 run_ids；不写任何表。
- **评估凭据**：表 `authz.nexloop_evo_evaluation_credentials` 由可信配置写入。触发器只接受 `worlds = {test}` 的服务凭据，登记后不能修改。
- **Context Engine**（`context_engine/semantic.py`）：
  - 新增 `EvaluationOverlay` 和 `load_overlay`；
  - `SemanticAdapter(overlay=...)` 在构造时拒绝 real 会话（第一道防线）；
  - 叠加层的 ref 以精确别名匹配排在 recall 结果前面，仍然经过原有的 READ 门禁和签名定义读取；
  - 结果标注 `overlay`。
- **active 定义与别名表**从不写入。

## L0 确定性回放（§12.3，D16）

`nexloop_eios/evolution/replay.py` 是纯逻辑，不访问数据库，不读时钟，不调用模型，也没有效应入口。

- **输入**：冻结的评估包、补丁（只有 ref 和哈希），以及 E3 的访问记录（只用合成样本，或由结构生成的用例）。
- **打分**：对每个用例的访问记录分别按 Parent 和“Parent + 叠加层”求覆盖率。
  - alias 补丁：把 `target_refs` 加到它命中的词项哈希上；
  - description 补丁：不改变查找，所以与 Parent 打平，不会被接受。
- **结论**：
  - 硬门槛逐条判 pass 或 fail，并附证据 ID，与评分分开判定；任一门槛失败即 `reject/hard_gate_failed`。
  - 评分用移植来的 `EvaluationGate.decide_gt`，非劣差值为 0.01（D7）；设计集和留出集都通过才 accept，分数相等不接受。
  - 缺用例时为 `incomplete/missing_data`，不当作通过。
- **AT-048**：开始前用 `check_bundle` 复查，簇跨两边就抛出 `BundleLeak`，不打分。

## 评估包与报告（§12.4）

- **契约**（只新增）：`evolution-evaluation-bundle.schema.json`、`evolution-evaluation-report.schema.json`，各带 example。生成后 `--check` 通过。
- **`authz.nexloop_evo_bundle_freeze(digest, candidate_id, levels, seed)`**：
  - 由 SQL 用已存的候选（ID、digest、用例、`case_groups`、版本）、固定的硬门槛清单（`runtime.nexloop_evo_hard_gates()`，版本 `nx038-hard-gates-1`）、D7 规则，以及服务端计算的三层预算余额，拼出评估包；
  - 调用方只能选 levels（`[L0]` 或 `[L0, L1]`）和匿名化种子；
  - 只冻结 `proposed` 的候选。相同参数重放；条件有变时拒绝，按规则应另起新候选；
  - 写入 `bundle_frozen` 事件。
- **`authz.nexloop_evo_report_record(digest, report)`**：
  - 报告必须对应已冻结的评估包 digest；level 必须在包内；evaluator 版本一致；每个冻结的硬门槛都要有答复；有任何门槛失败时不能 accept；
  - 写入 `report_recorded` 事件；
  - **不改变候选状态**，`evaluating` 之后的状态归 NX-039。
- 评估包、报告和审核关联三张表都只能追加，owner 也不能 UPDATE、DELETE 或 TRUNCATE。

## 审核关联表（D4）

- 新增 `runtime.nexloop_evo_review_links`：关联 decision_id、candidate_id 和 report_digest；`gate_result` 由报告结论派生（accept 为 pass，其余为 fail）。
- 外键指向 `ontology.nexloop_review_decisions` 和已记录的报告。
- 写入函数 `authz.nexloop_evo_review_link`，执行权限只给 `nexloop_evo_writer`。
- `review-decision` 契约不改。

## 迁移 0236（临时编号）

- **内容**：放宽事件表的 `event_kind` check（新增 bundle_frozen、report_recorded、review_linked），新建上述四张表和五个函数。
- **search_path**：所有函数都固定为 `pg_catalog,pg_temp`；definer 检查结果为 0。
- **编号引用**：正文不引用任何迁移编号，0001–0235 未改动。

## 测试

`tests/test_evolution_e5_pg.py`：

- 纯 L0：确定性（M22）；打平不接受；越界补丁的硬门槛失败；缺数据为 incomplete；有不属于用例的访问记录时，跨租户门槛失败；
- AT-048 复查；
- 适配器在 real 会话中拒绝叠加层；
- PG：
  - 冻结、重放和条件变更被拒；
  - 报告的各项拒绝与重放；
  - 记录报告不改变状态；
  - 审核关联的重放，以及跨租户读不到；
  - 冻结的三张表不可删除；
  - 受保护表的 digest 不变（AT-023）；
  - `nexloop_evo_reader` 不能冻结评估包；
  - 叠加层：real 被拒，未登记的凭据被拒，real 凭据不能登记，`proposed` 状态被拒，`evaluating` 状态可读，digest、候选或角色不符都被拒，simulation 被拒（AT-046 的服务端负例）。

## 不在 E5 范围

- L1 的执行（test world 的模型回放），以及 `evaluating` 之后的状态转移：属于 NX-039。
- 发布与激活：没有任何入口。
- 工作台展示。
