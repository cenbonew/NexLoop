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

## L6 审查（`nx043-x6-review.md`）

- **2.1 D2 与 D6 冲突**：已交给负责人取舍，代码不动。**按现行规则，单轮线上实验不会输出统计结果**：候选组最多 20 人（D2），低于每组 30 人的最小样本（D6），报告恒为 `not_reported / insufficient_sample`，成熟人数只写 none 或 some。
- **2.2 用不同 as_of 做差分**：
  - as_of 只能取整 UTC 天，不在网格上报 `window_not_on_grid`；
  - 新报告的 as_of 必须晚于这个实验的上一份报告，否则报 `as_of_not_after_last`；
  - 与上一份报告相比，每组至少新增 `runtime.nexloop_experiment_report_min_increase()`（10）个成熟样本，否则报 `too_few_new_matured`，不写入报告；
  - 同一个 as_of 返回已冻结的那份；
  - 报告表内部保存两组的成熟人数，用于下一次比较，不出现在输出里。
- **报告的生成入口**：统一通过受治理 Action `nexloop.experiment.report` 生成（见 `NX-043-report-action.md`），冻结函数本身没有授予任何应用角色。
- **2.3 排除计数**：随报告 Action 一起处理了，少于 30 只写 none 或 some。
- **测试**：`test_a_new_report_comes_later_and_after_at_least_ten_new_matured_per_arm`。第一份报告两组各 35 人；同一个 as_of 返回已冻结的那份；更早的 as_of 被拒；只多 5 人被拒；多 15 人通过。

## 负责人决定（2026-10-21，按推荐）：D2 与 D6 冲突改为多轮合并统计

- **决定**：候选组上限 20（D2）和每组最小样本 30（D6）都不改。同一组策略对比的多轮实验可以合并统计。“同一组策略对比”指：候选策略版本和对照策略版本都相同，同一租户，并且都在 real world（stage 为 online）。
- **实现**：迁移 `0299_nx043_pooled_reports.sql`（临时编号），入口是 `nexloop.experiment.report` 的 `kind: pooled`，参数为 `experiment_ids`（2–50 个）和 `as_of`。
  - 指定的实验必须构成同一组对比，否则分别报 `strategy_mismatch`、`maturity_mismatch`（成熟窗口或最小样本的定义不同）、`metric_mismatch`、`not_online_real`；有不存在的实验报 `not_found`。
  - **去重**：同一个 Consumer 出现在多个实验里时只计一次，取最早的那次分配，包括当时的组别和时间。
  - **最小样本**按合并后每组的人数判断。
  - **防差分**：以对比（候选、对照策略版本对）为单位，与单个实验的规则相同：as_of 只能取整 UTC 天；必须晚于该对比上一份合并报告；每组至少新增 10 个成熟样本。往合并集里加一个实验同样算一次变化，也要满足这个门槛。
  - **冻结**：合并报告写进 `control.nexloop_experiment_pooled_reports`，只能追加。同一个合并集加同一个 as_of，返回已冻结的那份。
  - **共用的统计函数**：单个实验的报告和合并报告共用 `runtime.nexloop_experiment_paid`、`_unknown`、`_result`，区间、unknown 的处理和抑制规则完全一致。
- **X1 登记时的提示**：`nexloop.experiment.register` 的返回值里增加 `notice` 字段。online 阶段候选组人数小于最小样本时，提示“单轮不出统计，请合并同一组对比的多轮实验”。登记不会因此被拒。这需要按 0290 的函数体重定义登记函数，已在复制件守护里登记。
- **测试**（`tests/test_nx043_pooled_pg.py`）：
  - 两轮各 20 人：单独报告是样本不足，合并后出结果（40 对 40，差异 0.2）；
  - 对照策略版本不同被拒；成熟窗口不同被拒；
  - 同一批 Consumer 在第 3 轮又被分到候选组，仍按第 1 轮的对照组只计一次；
  - 新加一个实验只多 5 人时，不出新结果；as_of 更早被拒；不在网格上被拒；再多 10 人后出结果。

## L6 复核可选项（`nx043-erasure-wiring-report-review.md`）

- **L3-1.3 “只读”Action 首次读取会写表**：business-actions 里这个 Action 的 purpose 已写明：它不是纯读操作，第一次读取会冻结报告（只能追加，冻结本身就是审计记录），之后的读取返回已冻结的那份。
- **L3-2.3 最小新增人数与候选组上限的关系**：单个实验的候选组最多 20 人，加上每次至少新增 10 人的要求，一个实验最多只能出 2 份报告；而且单个实验本来就不会出统计结果（见上文 2.1）。按负责人的决定，统计改由合并报告承担，新增 10 人的门槛按合并后的每组计算，所以随着轮次增加，合并报告可以持续更新。门槛值不变。

## L6 合并统计审查（`nx043-pooled-review.md`）

- **4.1 擦除后的墓碑行**：单实验报告和合并报告都排除 `consumer_id like 'pseudonym:%'` 的分配行（墓碑由 L5 的 NX-029 接线 aeb5f43a 写入），分子分母都不计。所以擦除不会把这个人变成“已成熟、未付款”，统计口径也不随擦除事件变化。报告新增 `left_out.erased`，只写 none 或 some。
  - 测试：擦除前，候选组 40 人、16 人成功；擦除一位付过款的候选者，并新增 11 人之后，候选组 50 人、15 人成功。这个人从分母消失，也没有按未付款计入；报告里只写 `erased: some`，没有任何 id 或假名。单实验报告同样排除墓碑行。
  - 本分支上分配表的 CHECK 还不接受假名，那个放宽在 L5 的线上。测试里在测试库中按 aeb5f43a 的规则临时重放了这个放宽。
- **2.2 交叉污染**：采用报告的第一种做法，在统计时排除。
  - 合并报告：在合并集里被分到过两个组的 Consumer，两组都不计入；只在一个组出现多次的，仍按最早那次分配只计一次。
  - 单实验报告：在同一组对比的另一个实验里被分到另一组的 Consumer，同样不计入。
  - 报告新增 `left_out.cross_allocated`，只写 none 或 some。测试：第 1 轮的 20 名对照者在第 3 轮又进了候选组，合并时两组都不计这 20 人，单独看第 3 轮也不计。
  - **X4 接线要求（L5 的线，本分支不改）**：分配时应排除“曾在同一组对比（候选和对照策略版本都相同）的任何实验里被分配过”的 Consumer，新增排除原因建议命名为 `in_same_comparison`，从源头避免一个人先后接受两种策略。在 X4 接上之前，上面统计侧的排除保证了这类人不会进入结果。
- **1.2 指标方向**：合并时比较整个 `primary_metric` 对象（key 和 direction）；direction 不同报 `metric_mismatch`，补了测试。
- **3.2 成功数差值不受门槛约束**：接受现状。两份报告之间每组至少新增 10 个成熟样本，并且被擦除和交叉分配的人都已排除，所以成功数的变化总是和至少 10 个新人混在一起，无法确定到个人。

## 与 X4 对齐：同组对比也要求 world 相同（L6 复核 bb876cb5，调度员 2026-10-21）

- `runtime.nexloop_experiment_leave_out` 判断“同一组对比”时，除了候选、对照策略版本相同，还要求实验的 world 相同，与 X4 的 `in_same_comparison` 一致。同一个 Consumer id 出现在另一个 world 的同组对比实验里，不算交叉分配，不会被排除。
- 这个函数在第九批预演的迁移 0291 里，所以没有改原文件，而是新开迁移 `0302_nx043_leave_out_world.sql`（临时编号）重定义它，函数体与 0291 只差这一个条件，已在复制件守护里登记。
- **测试**：同一个 Consumer id，在 world A 的实验里进了候选组、在 world B 的实验里进了对照组，不触发排除；在 world A 的另一个实验里进了对照组时，才算交叉分配（阳性对照）。
