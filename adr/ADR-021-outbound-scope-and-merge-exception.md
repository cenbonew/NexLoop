# ADR-021｜Agent 外发首版范围与一次性合并例外

状态：**采用**（负责人 2026-10-09 确认）。
关联：ADR-020（§2 Agent 外发消息持久化、§4 合并标准）；NX-018、NX-047、NX-028；AT-009。

## 1. NX-047 首版产品范围

| 问题 | 决定 | 理由 |
|---|---|---|
| 人类接管的员工身份与接管权限 | 首版只覆盖 Agent 外发；员工身份与接管权限在 NX-028（负责人工作台与人工接管）时单独设计，并由负责人确认 | 人类权限属于负责人决定范围（ADR-020 §3），且需要完整的接管状态机 |
| 顾客视图是否显示“处理中”占位 | 不显示；只展示渠道已接受或已送达的外发消息 | 与 M09“未获证实的草稿不显示为已发送”一致 |
| v0.1 是否支持撤回未投递消息 | 不支持；只保留取消尚未提交的外发意图（docs/06 §9） | 撤回涉及渠道能力与已受理动作的补偿，超出 v0.1 范围 |

## 2. 一次性合并例外（ADR-020 §4）

**背景。** 合并 NX-018 的方案 B（Message READ 受治理派生，迁移 0075–0077）修复了登记在基线中的 21 个回归（Mac 21/21 通过；CI 测试机 16 例转绿，5 例在测试机上因 2 s 工具时限失败，Mac 通过）。同一合并带入一个确定性新增失败：`tests/test_relationship_context_v4.py::test_real_human_message_v4_bound_artifact[complete]`，对应 AT-009 尚未完成的 v4 两 Pi 正向场景，在 Mac 与测试机上都因 2 s 工具时限失败。

**决定。** 负责人同意作为一次性例外合并：

- 该用例登记入合并基线 B 类（时限），绑定 AT-009，`planning/acceptance-tests.json` 中 AT-009 记为 `failed`。
- 退出条件：授权时延优化 O1/O4 落地，或 NX-018 完成 v4；之后该用例必须通过并从基线移除。
- 本例外不改变 ADR-020 §4 的一般规则：其它新增失败仍阻止合并。

## 3. 基线随本次合并的调整（证据见调度员记录）

- A 类（main 真实回归）21 例中 16 例在测试机转绿，从基线移除。
- 5 例改归 B 类（测试机时限）：测试机同一检出串行 2/2 失败（`runtime_transport_unavailable` 或 Pi 拒绝截止超限），Mac 在同一合并结果上串行通过。
- `tests/test_review_workbench_pg.py::test_reject_cooldown_keeps_evidence_and_blocks_requeue_until_expiry` 暴露 NX-046 的真实缺陷（0074 触发器两次调用 `clock_timestamp()`，冷却期不是精确 30 天），登记为 A 类，修复中（新迁移，不改 0074）。
- `tests/test_role_post_accept.py::test_active_role_query_keeps_original_governed_finalize` 测试机串行通过，登记为 C 类（并行负载抖动）。
