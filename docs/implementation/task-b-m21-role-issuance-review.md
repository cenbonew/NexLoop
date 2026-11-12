# M2.1 Role 签发疑点核实（本地合成证据）

基线为批次 170 `8d9f41531d38557a9e281e2734fcdf927c2af4e3`，M2.1 重放迁移尾为 0453。本报告仅核实遗漏范围，没有修改 Role 生产代码、Host 或 contracts，也没有连接服务器。

## 可复核路径

- `packages/eios-core/src/nexloop_eios/background_services.py` 将 `arguments.effect_action` 传给 `RoleRunLauncher`。
- `plan_reevaluation.py::RoleRunLauncher.issue` 把 `[self.effect_action]` 传给 `role_policies.issue_role_run`，没有按 plan 的 Consumer 选择后继。
- `role_policies.py::issue_role_run` 经 `authz.nexloop_issue_role_policy_run` 原子签发 Run 并绑定当前 Role policy；0081 的 SQL 检查 ceiling 包含 Run 的精确 `allowed_resources`，不做 Consumer 效应版本选择。
- `RoleRunLauncher.activate` 首先调用 `Planner.bind_effect_context`。M2.1 的 registrar 在此按 Consumer 重算效应版本并要求 executor/Run proof 精确匹配。

## 最小复现

`tests/test_task_b_role_issuance_probe_pg.py` 复用 `test_plan_role_launcher_pg.role_planning` 的真实 PostgreSQL/受治理 Role fixture，直接调用生产 launcher。没有 mock 签发或 bind。

1. Consumer v1，对照：配置 `nexloop.service.request:1`，选择也是 `:1`，Role Run 持有 `:1`，policy binding 存在，Planner bind 成功。
2. Consumer v2，后继经 configurator 准入且 fixture 模拟 follow facts 完成：SQL 选择 `:2`，同一个配置入口仍签发只有 `:1` 的 Role Run，policy binding 仍可成立。独立验证该 Run 能证明 `:1`、不能证明选择出的 `:2`；生产 `activate` 在 effect bind 被拒绝，没有为该 Run 写 context link。

证据是本地合成 PG，不是实际 F3 发布、真实 register、Host 运行或 stage 验收。fixture 的 follow facts 不证明 M2.3/M3 的真实授权跟随路径。

## 影响与边界

- 确认 Role reevaluation 的配置式签发遗漏了按 Consumer 选版本；已有 M2.1 的 relay/reply fallback 改动不覆盖它。
- 在配置仍为 `:1`、Consumer 已选择 `:2` 时，Role 链不能继续 bind。不能只凭 message relay 的回归宣称所有 Run 签发链已兼容后继。
- 这是拒绝执行的兼容性缺口，未发现它可绕过权限或产生未授权外发。复现不用于断言缺口首次由 M2.1 引入，也不证明未准入的 Consumer v2 Role 路径已经兼容。
- Role ceiling 是精确版本的权限上限，不能通过自动扩权使 `:2` 通过。后续修复须保留当前 Source/Role ceiling 检查；不在本轮擅自改签发 SQL、Role policy 或扩大 M2.2。
- M2.1 候选仍按 F1 禁止手动准入；解除禁令或启用 M2.3 前，须由调度员/L6 明确安排此缺口的修复与回归。Host v8 单独完成不能证明本缺口解决。

首轮探索有两项失败：断言误查 `effect_contexts.run_id`（列不存在），以及未准入 Consumer v2 的 bind 也被拒绝（未独立归因）。均保留原始日志；最终复现收窄到 Consumer v1 对照及已准入 Consumer v2，并改查 `effect_run_contexts.run_id`。这些失败未被算成有效的版本不匹配证据。

增加 proof 断言后的首次运行另有 1 failed / 1 passed：越出 Run allowed_resources 的证明被包装为 `AuthorizationUnavailable`，而探索断言误期待 `ValueError`。最终改为精确断言该拒绝类型，生产代码没有因此调整；日志另存 attempt3。

## R1 追加归因（按独立 L6 2026-11-12 11:47 要求）

没有丢弃“无准入 v2”首败。`tests/test_task_b_role_r1_pg.py` 在批次 170 与候选上使用完全相同的探针、`task_b_fixture` 和重新认证流程。批次 170 的 source fixture 来自本地 `git archive 8d9f4153`，位于忽略目录，不是额外 worktree；没有切换正在回归的分支。运行时 PYTHONPATH 分别指向各自源码，日志中的模块路径及 registrar 文件 hash 可核对来源。

首轮两边均 1 failed（10.24s / 10.30s）：trace 将拒绝定位到 registrar `_execute` 的 reservation disposition 检查，未进入 `register()`；不是 GUC、过期目录会话或 M2.1 的版本匹配 SQL。最终增加两种夹具，而不是缩小覆盖：

| 同一无准入 Consumer v2 场景 | 批次 170 | 候选 |
| --- | --- | --- |
| 复用 v1 已 bind 的 PlanStep | reservation=conflict，bind 拒绝，无新 Run context link | 同样 conflict、拒绝、无 link |
| 新的、从未 bind 的 PlanStep，显式配置对应 READ 与 Role policy | bind 成功，1 个 link | bind 成功，1 个 link |

新 PlanStep 通过实际 `PlanStep.create:1` 创建（在升级前），Role ceiling/scope 的 step_ids 通过受治理编辑明确设置；没有自动增加 effect Action 权限。精确 READ 的 provisioning 与 publication/follow 同属测试事实夹具，不是真实 register 证据。每次更新后 Source/Planner/executor/Run 重新认证。

**原因**：`EffectContextRegistrar.bind` 的业务 payload 不含 Run ID。旧 PlanStep 的同一 payload 产生相同 `context:<request_digest>` intent；Consumer 升到 v2 后 registrar 选择 `:2`，而既存 terminal claim 的 `binding.action_reference` 是 `:1`。`0007_action_claims.sql` 对同一 intent 的不同 binding 返回 conflict（不改写旧 claim）。两边 trace 均明确记录 `same_prior_intent=true`、`action_version=2`、`disposition=conflict`。

**裁定范围**：这是批次 170 已存在的旧 PlanStep 重绑/恢复路径缺口（幂等守卫本身按设计拒绝不同 binding），不是候选新增的无准入回退，也不是通过重新认证就能消除的 fixture 错误；首轮复用了旧 step，因而不能用来说明所有无准入 v2 新 Role 计划都失败。全新 step 的正对照已验证两边均可 bind。既有 Role 计划在 Consumer 升级后复用同一 step 仍可能受影响，需另行安排治理/恢复语义，不能改 intent ID 或清 SQL claim 绕过。此项不在本轮擅自修复。

最终成对测试：批次 170 为 2 passed / 17.61s；候选为 2 passed / 18.19s。同一探针 SHA-256 `d5902aab95cec3c94aeafe6cc60e46fa12d5d037dfa0543bf1d361c441de7d87`，同一 helper SHA-256 `6a75ed7e0b4568116be57b51b366bd856f3c8d712c837495475ecca4dee5d88d`。首轮失败、补充矩阵首轮与最终矩阵的日志/命令/源码 hash 均保留。

已准入 :2 的 Role 缺口探针也改为使用上述全新 PlanStep，排除旧 claim 冲突干扰；最终结果另见交付报告。原 18.09s 的 Role 探针日志保留为 before-fresh-step，不冒充最终版本证据。
