# AT-060：守卫按操作分项、预热排空模板残留、recall indexer 预热期积压原因

依据：调度员 2026-10-13（AT-060 正式整轮 `20261012T234421Z-b8831ff` 的分析，代码项 1、2 与问题 3）。口径问题（空闲派发定义、守卫 p95 的合并方式）待负责人确认，本文不改任何口径或门槛。

## 1. 守卫样本按操作分项（临时迁移 0430）

- **记录**：guard 进程原有的整体样本（kind `guard`）不变。所有读取它的地方（告警、doctor、history monitor、报告的 `guard_p95_ms_max`）都不受影响。同一窗口内另按操作各记一条样本，kind 为 `guard_op`，service 为操作名，字段包括 `calls`、`timeouts`、`errors`、`p50_ms`、`p95_ms`、`max_ms`，以及固定延迟桶 `le_50`…`le_2000`、`gt_2000`（每个桶计入区间 (上一个界, 本界] 内的调用数）。
- **操作名**：`start`、`resume`、`inspect`、`cancel`、`model`、`tool` 取 Host 请求里的 `operation`（`runtime_activation._OPERATIONS`）。`model` 带 `model_result` 时记为 `model_result`。效应端点记为 `effect_submit`、`effect_find`。另有 `outcome`、`incentive`。其他情况（包括认证失败和无法解析的请求）记为 `other`。`resolve` 由派发方在进程内直接调用，不经过 guard HTTP，所以没有这一项。
- **不改授权语义**：guard 只在请求体解析之后给调用打一个标签，这个标签不参与任何判断（`runtime_control.py` 的 `_operation_label`）。
- **SQL**（0430）：样本表的 kind 增加 `guard_op`；`authz.nexloop_record_process_sample` 对 `guard_op` 只接受上面列出的数字键和已知操作名。这是 create or replace，属主、权限和 `search_path=pg_catalog,pg_temp` 都不变。
- **报告**：每格新增"守卫按操作分项"表，列出各操作的调用数、超时、错误，p50、p95（与 `guard_p95_ms_max` 同口径，即最差的单进程单窗口），max，以及 `p95≤`（窗口内该操作全部调用的第 95 百分位所在桶的上界，跨进程合并）。这张表只作信息，不参与判定。旧结果没有 `guard_op` 样本，显示为 missing。
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
