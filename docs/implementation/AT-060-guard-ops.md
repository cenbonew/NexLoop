# AT-060：守卫按操作分项、预热排空模板残留、recall indexer 预热期积压原因

依据：调度员 2026-10-13（AT-060 正式整轮 `20261012T234421Z-b8831ff` 的分析，代码项 1、2 与问题 3）；口径变更 4A、5A 由负责人 2026-10-13 按推荐确定，记入 ROADMAP §7（见第 4 节）。门槛值不变。

## 1. 守卫样本按操作分项（临时迁移 0430）

- **记录**：guard 进程原有的整体样本（kind `guard`）不变。所有读取它的地方（告警、doctor、history monitor、报告的 `guard_p95_ms_max`）都不受影响。同一窗口内另按操作各记一条样本，kind 为 `guard_op`，service 为操作名，字段包括 `calls`、`timeouts`、`errors`、`p50_ms`、`p95_ms`、`max_ms` 和延迟直方图 `h_<i>`（见第 4 节 5A；整体样本也带直方图）。
- **操作名**：`start`、`resume`、`inspect`、`cancel`、`model`、`tool` 取 Host 请求里的 `operation`（`runtime_activation._OPERATIONS`）。`model` 带 `model_result` 时记为 `model_result`。效应端点记为 `effect_submit`、`effect_find`。另有 `outcome`、`incentive`。其他情况（包括认证失败和无法解析的请求）记为 `other`。`resolve` 由派发方在进程内直接调用，不经过 guard HTTP，所以没有这一项。
- **不改授权语义**：guard 只在请求体解析之后给调用打一个标签，这个标签不参与任何判断（`runtime_control.py` 的 `_operation_label`）。
- **SQL**（0430）：样本表的 kind 增加 `guard_op`；`authz.nexloop_record_process_sample` 对 `guard`、`guard_op` 只接受上面列出的数字键和 `h_0`…`h_116`，`guard_op` 只接受已知操作名。这是 create or replace，属主、权限和 `search_path=pg_catalog,pg_temp` 都不变。
- **报告**：每格新增"守卫按操作分项"表，列出各操作的调用数、超时、错误，p95（该操作全部调用合并，与门槛项 `guard_p95_ms` 同口径），最差一分钟 p95，max。这张表只作信息，不参与判定。旧结果没有 `guard_op` 样本，显示为 missing。
- **测试**：
  - `tests/test_guard_op_samples.py`：整体样本不变；按操作分项与分桶；真实 HTTPS guard 对各端点打标签，并且应答与原来相同；SQL 只接受数字和已知操作。
  - `tests/test_load_report.py::test_guard_operations_table_per_cell_leaves_guard_p95_unchanged`。

## 2. 进入稳态前等待模板残留排空

- 预热结束后，在 warm-up 流量继续（不计入测量）的同时，每 5 s 查询一次该格开始之前就已存在的到期 feed 条目（`collect.residue`：负载租户，pending，到期，`created_at` 早于格开始，不含 plan-reevaluate），全部为 0 即进入稳态。
- 上限 300 s（随 `--timing-scale` 缩放）。到上限仍未排空，这一格记 `warm_up_not_drained` 并继续运行，不中止。
- `cell.json` 的 `warm_up_residue` 记录：`at_start`（开始时的残留量）、`by_feed`、`drained`、`seconds`（排空用时）、`code`、未排空时的 `remaining`，以及每次查询的 `polls`。
- 不在 `snapshots.jsonl` 里增加预热阶段的快照，因为报告的 `dead_letters_delta` 以第一条快照为起点。
- **测试**：
  - `tests/test_load_matrix.py`：排空、无残留、到上限并按 timing 缩放；
  - `tests/test_load_residue_pg.py`：哪些条目算残留。

## 3. recall indexer 为什么没在 120 s 预热期内消化这批条目

结论：**indexer 在预热期间一直在消化，只是模板带来的积压远多于预热期间能处理的量**。不是 indexer 没有运行。

- **模板构建不运行 recall indexer**：`scripts/load/build_template_hold.py` 和 `tests/load_template.py` 都没有启动它。所以模板里保留了建模板时产生的全部 `recall-instance` 条目（每个 Consumer 的创建和修改都会产生一条）。每一格从同一个模板克隆出来，都要从头消化这批积压。
- **证据**：快照的 `oldest_pending_seconds` 按 `changed_at` 计算（0093:137），而 claim 按 `available_at, changed_at` 从旧到新处理（0126 的 `authz.nexloop_work_feed`）。三个格都克隆自同一模板，但稳态开始时剩余最旧条目的 `changed_at` 各不相同：
  - C1：23:56:28；
  - C4-low：23:57:10；
  - C4：23:57:44。

  如果预热期间没有消化，三者应当相同。不同说明每格在预热期间已经从最旧的开始处理了一部分。
- **吞吐**：每个 tick 至多领取 20 条（`recall_index_worker.py:21` batch=20），tick 为 1 s（`stack.py` 的 `--tick-seconds 1`），上限约 20 条/s。稳态开始后实测约 12 条/s（C4 30 s 内由 811 降到 439）。后台启动到稳态开始约 160 s，按此估算模板积压约为 811 + 160 × 12，即两千多条。模板本身没有随结果拉回本地，无法精确计数。
- **另一个让排查困难的点**：后台服务循环吞掉每个 tick 的异常且不留日志（`background_services.py` 的 `run` 循环），`process.log` 里只有 ready 一行。本次没有改它。
- **可选的后续**（本分支未做）：模板构建结束时跑一次 recall indexer 直到排空，可以让各格不再继承这批积压。改为第 2 项的等待以后，这批积压只影响预热时长。

## 4. 口径变更 4A、5A（负责人 2026-10-13，ROADMAP §7）

### 4A 空闲派发

- **门槛项 `idle_dispatch_p95_ms`**（只在 C4-low 判定，门槛 2000 ms 不变）恢复设计稿 `NX-031-design.md:187` 的定义：来信 `accepted_at` 到对应 Run 任务首次被 claim（`runtime.job_events` 的 running）。
- `scripts/load/collect.py` 的 `dispatch` 每个 Run 写一行，分三段：
  - relay 段：受理 → 任务创建；
  - 排队段：任务创建 → claim；
  - 启动段：claim → Host 首次执行授权。

  另写 `dispatch_ms`（relay + 排队，门槛口径）和 `run_start_ms`（任务创建 → 首次执行授权，即 2026-10-11 的旧口径）。
- **观察项**：`run_start_p95_ms` 和三段的 p50/p95（`dispatch_segments`）只记录，不判定。
- 上一轮 C4-low 的 4.4 s 属于旧口径（含启动段，启动段本身至少 2.4 s）。在新口径下需要重跑才有数。

### 5A 守卫 p95

- **门槛项 `guard_p95_ms`**（门槛 1500 ms 不变）：稳态窗口内全部 guard 调用的真实 p95。
- **分桶**：每个 guard 进程每个窗口写一个对数分桶直方图（`nexloop_eios.observability`，`HISTOGRAM_RATIO = 1.1`，`HISTOGRAM_LAST = 116`）：
  - 第 0 桶 ≤1 ms；
  - 第 i 桶为 (1.1^(i−1), 1.1^i] ms；
  - 第 116 桶为约 60 s 以上的开区间。
- **合并与精度**：报告把窗口内各进程各窗口的直方图相加，取第 ⌈0.95·n⌉ 个调用所在桶的几何中点 1.1^(i−0.5)。在 1 ms 到约 60 s 之间，相对误差不超过 √1.1 − 1 ≈ 4.9%，满足 ±5%。落在开区间桶时取下界，此时已远超 2 s 时限。
- **观察项**：原"最差的单进程单分钟 p95"保留为 `guard_p95_ms_worst_minute`。
- **测试**：
  - `tests/test_load_report.py::test_merged_guard_p95_is_within_5_percent_of_the_exact_p95_over_all_processes`：4 个进程、10 个窗口、40 组随机对数正态分布，与精确 p95 比较；
  - `tests/test_guard_op_samples.py::test_histogram_buckets_bound_the_relative_error_by_5_percent`。

### 旧格式结果

- 没有直方图的 guard 样本，或者只有 `dispatch_ms` 的 `dispatch.jsonl`（2026-10-11 口径），在报告里对应门槛项标为 `incomparable`，原因 `caliber_changed`。该格不判通过，AT-060 原因写成 `<格>:gate_<项>:caliber_changed`。
- 用新报告重算上一轮（`20261012T234421Z-b8831ff`）会得到这个结论。

### 报告展示

- 门槛判定每格分为"门槛项"表和"观察项"表（`guard_p95_ms_worst_minute`、`run_start_p95_ms` 和三段派发）。
- 汇总表的 guard p95 和空闲派发列改为新口径。
- 结果契约同步更新（`NX-031-results-contract.md`）。
