# NX-043 X6：结果统计与报告

- **依据**：NX-043 设计稿 §6、§8、D6；依赖 X4（L5 的 f35de896）。
- **迁移**：`0293_nx043_experiment_reports.sql`。临时编号，接在 X4 的 0292 后面。前面有空号，所以 `test_bootstrap.py` 的计数断言在重排之前会失败，这与 X1、X4 的情况相同。
- **契约**：没有新增契约。报告沿用 NX-039 的报告结构：schema_version、report_id、冻结输入 digest、hard_gates、scores、conclusion、reason、created_at，另加实验统计字段。

## 统计

- **入口**：`runtime.nexloop_experiment_report_freeze(tenant, experiment, as_of, by)`，只有 owner 能调用，没有授予任何应用角色。`as_of` 不能晚于现在。
- **成熟窗口**（D6）：取冻结预登记里的 `maturity.window_days`，默认 14 天。只统计 `allocated_at + 窗口 <= as_of` 的分配（X4 的 candidate、control 两组）；窗口还没到的样本不计入。
- **主要指标**：本切片只支持 `payment`，即窗口 `[allocated_at, allocated_at + 窗口)` 内，同一实验世界里，与该 Consumer 关联的客户有一笔已核验的支付。“已核验”指：记录类型 payment，状态 paid 或 succeeded（与 0118 相同），disposition 为 applied。其他指标返回 `not_reported` / `metric_not_supported`。
- **unknown**（M19）：Consumer 在窗口内有状态为 unknown 的效应意图，就计入 unknown，并且**不算成功**，即使同期有支付也一样。
- **最小样本**（D6）：任一组成熟样本少于 `maturity.min_samples_per_group`（默认 30），就返回 `not_reported` / `insufficient_sample`，只给出两组的成熟人数；不给成功数、比率、区间和差异，避免缩小到个人。
- **区间**：各组比率给 Wilson 95% 区间；候选组减对照组的差异给 Newcombe（Wilson）95% 区间（`newcombe-wilson-95`）。
- **措辞**：报告写明“在该实验设计、该窗口、该样本下……不是通用的因果效应，也不能把某一条消息当作付款原因”（设计 §6）。样本不足时只写“没有结论”。
- **其他字段**：排除计数（X4 的排除原因）、`hard_gates` 中的 `minimum_sample`、`guardrails: not_evaluated`（护栏由 X5 接入）、`data_mode`。

## 冻结

- 报告写进 `control.nexloop_experiment_reports`，只能追加（update、delete、truncate 都会被拒）。
- `report_id` 由实验 id 和 `as_of` 派生；同一个 `as_of` 再次调用，返回已冻结的那份（`replayed: true`），digest 不变。
- 报告里只有聚合数，没有 Consumer、客户引用或任何 id。

## 发现：D2 与 D6 冲突

X1 按 D2 把候选组限制在最多 20 人，而 D6 要求每组至少有 30 个成熟样本才能出结论。所以按现在的预登记规则，一个线上实验**永远只能报告 `insufficient_sample`**。本切片按 D6 执行，测试里为了够到 30 人，直接写入了分配记录。需要负责人决定：放宽 D2 的候选组上限，或者降低 D6 的最小样本，或者允许跨多轮实验累计。

## 测试（`tests/test_nx043_reports_pg.py`）

- **样本不足**：候选组 20 人（D2 上限）、对照组 40 人，返回 `insufficient_sample`，只有成熟人数，没有比率、成功数和区间；未成熟的分配不计入。
- **区间与 unknown**：两组各 40 人。候选组 20 人支付，其中 2 人另有 unknown 效应，所以成功数是 18；对照组 10 人支付。窗口外的支付和 pending 状态的支付都不计入。Wilson 区间与手算结果一致（[0.3071, 0.6017]），差异估计 0.2，其 Newcombe 区间包含 0.2，措辞不写成因果。
- **冻结**：重复调用 digest 相同；update、delete、truncate 都被拒；报告里没有任何 Consumer 或客户引用；`as_of` 在未来时被拒；未知实验报 `not_found`；应用角色都不能调用。
- **其他**：test 世界的支付不算 real 的结果；不支持的指标返回 `metric_not_supported`。
