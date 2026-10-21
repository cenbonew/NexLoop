# NX-043 报告 Action：`nexloop.experiment.report`

- **依据**：L6 对 X3 的审查项 4.3，以及调度员 2026-10-21 的决定（和 X6 并在一起）。
- **分支**：`l3/nx043-report` 从 `l3/nx043-x3`（7f6a293f）开始，合并了 `l3/nx043-x6`（6aecd75e，其中包含 X4）。本分支上的临时编号：
  - X4 是 0292；
  - X6 是 0293；
  - X3 影子世界从 0292 改为 **0294**；
  - 本 Action 是 **0295**。

  集成时由调度员统一重排。

## Action

- `nexloop.experiment.report` / `read_experiment_report`：human 类，只授予 owner，只能从 real 发起，带审计。
- **清单同步**：
  - business-actions（manifest_version +1）、capabilities（hash 由 `write_hashes` 生成）、system-catalog-names、workbench-roles（owner，`experiments` 分组，版本 +1）；
  - `PROFILES`、`goal_controls`、`workbench_actions_http`（错误前缀 `experiment report`）、`workbench_roles`；
  - `commercial_fixture`、`test_business_actions`、`test_governed_entry_pg`；
  - register 计数从 100 改为 101；owner 角色的 Action 数从 66 改为 67。
- **租户与 actor**：都由受治理入口提供（`p_tenant`、`p_principal`）。body 里只要出现 tenant、actor、by 这类字段，一律报 `invalid`。
- **`kind: shadow`**：参数是 world、strategy_id、strategy_version、since、until，返回 X3 的差异报告。world 必须属于本租户，否则报 `world_unknown`；窗口必须是整 UTC 天。
- **`kind: experiment`**：参数是 experiment_id、as_of，返回 X6 的结果。实验必须属于本租户，否则报 `not_found`；as_of 必须是整 UTC 天。
- **`kind: pooled`**（0296）：参数是 experiment_ids、as_of，返回同一组策略对比的多轮合并结果，规则见 X6 文档的“负责人决定”一节。
- 三种报告都在第一次读取时冻结，之后再读取返回同一份。

## 抑制规则与 X6 统一

- X6 的 `as_of` 也改为只能取整 UTC 天，否则报 `window_not_on_grid`，与影子报告和 T5 一致。这样两份报告的时间点不能只差几个小时，不能靠相减推出个别分配。
- X6 的附带计数改成与影子报告一致：成熟样本不足时各组的成熟人数、unknown 计数、排除计数，凡是少于最小样本（30）的，都只写 `none` 或 `some`（`runtime.nexloop_experiment_count`）。达到最小样本的组，照常给出成功数、比率和区间。
- 报告里不出现任何 Consumer、会话、消息或客户 id。

## 测试（`tests/test_nx043_report_action_pg.py`）

- owner 读取两种报告：只有聚合数，没有任何 id；重复读取 digest 相同；审计记录的 actor 是受治理入口给的主体；body 里带 tenant、actor 或 by 都被拒。
- 非 owner 被拒：角色清单里只有 owner 持有这个 Action；应用角色都不能执行它的 handler 和两个冻结函数；从 test 世界或 shadow 世界发起都报 `owner_world_only`。
- 跨租户被拒：另一个租户读这个租户的 shadow 世界报 `world_unknown`，读这个租户的实验报 `not_found`。
- 窗口不在网格上被拒：影子报告的 since、until，以及 X6 的 as_of，都必须是整 UTC 天；时间格式不对报 `invalid`。
