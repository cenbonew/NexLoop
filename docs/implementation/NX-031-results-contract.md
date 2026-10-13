# NX-031 矩阵结果目录契约（矩阵执行器 → 报告生成器）

**谁读谁写**：
- 矩阵执行器和采集器（L4，`scripts/load/` 中除报告外的部分）**写入**本目录；
- 报告生成器 `scripts/load/report.py`（L2）**只读**本目录，生成汇总报告。

**依据**：设计稿 `NX-031-design.md` §3.2（格子）、§4（测量）、§4.4（身份核对）、§5.3（CI 同机影响）、§6.1（门槛）、D6、D8。

**内容约束**：全部文件只含代码、计数、时延、时间戳和不透明 ID，**不含任何消息原文、姓名、联系方式或密钥**（D8）。报告生成器只输出聚合值，不复制任何 ID。

**存放位置**：原始目录放在 `.ci-results/load/<run_id>/`（已被 git 忽略），保留 30 天。

## 1. 目录结构

```
<run_dir>/
  run.json                      # 整轮运行的身份
  cells/<cell_id>/
    cell.json                   # 格子配置与完成情况（必需）
    snapshots.jsonl             # NX-030 指标快照，稳态与排空期每 30 s 一行
    process_samples.jsonl       # runtime.nexloop_process_samples 中窗口内的行（guard / guard_op / pool / host）
    runs.jsonl                  # 每个消息 Run 的分解时延（D6）
    acks.jsonl                  # 驱动端每次入站 POST 的 ACK 时延
    dispatch.jsonl              # 派发分段：relay、排队、启动（4A，2026-10-13）
    refusals.jsonl              # 窗口内的派发拒绝（runtime.nexloop_dispatch_refusals）
    alerts.jsonl                # 窗口内的告警事件（control.nexloop_alert_events）
    checks.json                 # §4.4 的 7 项身份与结局核对
```

缺少某个文件时，报告把对应指标标为 `missing`。门槛格中的任何必需指标缺失，该格判为 `failed`，不当作通过。

## 2. 文件格式

### `run.json`

```json
{"run_id": "20261011T220000Z-<short_sha>", "seed": 31, "consumers": 1000, "manifest_digest": "<sha256>",
 "commit": "<40 hex>", "catalog_digest": "<sha256>", "catalog_head": "0133", "matrix_version": 1,
 "template": {"key": "<sha256>", "build_seconds": 1800}, "started_at": "<ISO8601>", "finished_at": "<ISO8601>",
 "host": {"cpus": 16, "mem_gib": 13}}
```

### `cells/<cell_id>/cell.json`

```json
{"cell": "C4", "role": "gate", "host_concurrency": 4, "rate_per_s": 1.0, "background": true, "ci_background": false,
 "completed": true, "aborted": null,
 "window": {"steady_start": "<ISO8601>", "steady_end": "<ISO8601>"},
 "drain": {"drained": true, "seconds": 140},
 "host_metrics": "m09",
 "ci": null}
```

- `cell`：取值为 `C1`、`C2`、`C4`、`C8`、`C4-low`、`C4-peak`、`C4-quiet`、`C4-ci` 之一。
- `role`：`gate` 只允许出现在 `C4` 和 `C4-low` 上。报告会按设计核对这一点，不一致时拒绝生成。
- `aborted`：`null`，或者一个代码，例如 `template_clone_failed`、`service_start_failed`、`timeout`。
- `host_metrics`：`m09` 表示 Host 有直接样本；`approx` 表示用 `runtime_transport_unavailable` / `retry_wait` 近似（§4.1）。
- `ci`：只有 `C4-ci` 有值：`{"run_id": "<ci run id>", "wall_seconds": 4200, "deadline_gate_failures": 0, "failed": 0}`。

### `snapshots.jsonl`

每行格式：`{"at": "<ISO8601>", "phase": "steady"|"drain", "snapshot": <runtime.nexloop_metrics_snapshot 的原样 JSON>}`

报告使用其中的 `queues`、`feeds`、`extraction`、`connections.total` 和 `refusals`。

### `process_samples.jsonl`

每行格式：`{"kind": "guard"|"pool"|"host", "service": "...", "instance": "...", "window_start": "<ISO8601>", "window_seconds": 60, "sample": {...}}`

`sample` 是 NX-030 写入的原样内容：
- guard：`calls`、`timeouts`、`errors`、`p50_ms`、`p95_ms`、`max_ms`，以及延迟直方图 `h_<i>`（5A，2026-10-13：i 为 0–116，第 0 桶 ≤1 ms，第 i 桶为 (1.1^(i−1), 1.1^i] ms，最后一桶为开区间上沿，只写非空桶）；
- guard_op（AT-060，迁移 0186）：按操作的同样字段与直方图，`service` 为操作名；
- pool：`requests_waiting`、`requests_errors`、`pool_size` 等；
- host：`active_runs`、`waiting`、`admission_timeouts` 等。

### `runs.jsonl`（D6，AT-060 的逐 Run 分解）

每行格式：

```json
{"e2e_ms": 5400, "run_ms": 4800, "model_ms": 3100, "tool_ms": 820.5, "tool_calls": 3, "queue_wait_ms": 120, "admission_wait_ms": 0, "outcome": "replied", "failure": null}
```

- `tool_ms` 和 `tool_calls` 取自 `runtime.jobs.result.tool_timing`（`total_ms`、`calls`）；没有时为 `null`。
- `model_ms` 是这个 Run 所有 `runtime.nexloop_model_requests` 的 `completed_at − requested_at` 之和。
- `e2e_ms` 是触发消息的 `accepted_at` 到回复（或兜底、升级）落库的时间差。还没有回复时（处理中、受限、被接管）为 `null`，这类 Run 只按 outcome 计数。
- `run_ms`（调度员 2026-10-11 裁定 2）：Host 接受这个 Run（runtime worker 首次为它登记激活，`authz.nexloop_runtime_activations.created_at` 的最小值，随即提交 Host start，早于任何 Pi 任务）到它的任务结束（`runtime.jobs.updated_at`，状态为 succeeded/failed）。模型和工具时间都落在这段时间内，平台时间包含授权等待。此前从首次执行授权起算，Pi 生成在等待这次授权时就已开始，guard 慢时会出现负值。平台耗时 = `run_ms − model_ms − tool_ms`，三者覆盖同一时间范围；结果为负视为数据异常，报告计数并判 AT-060 不通过，不会截成 0。端到端不参与相减。
- `model_ms` 优先取任务结果中 Host 报告的 `model_timing.total_ms`（方案 A2）。
- `outcome` 取值：`replied`、`fallback`、`escalated`、`restricted`、`takeover`、`in_progress`。
- `failure`（NX-031，调度员 2026-10-11 裁定 1）：失败的 Run 的原因，未失败为 `null`。`runtime_authorization_unavailable` 是 guard 未应答（Host 对同一次授权有界重试后仍不可用），`runtime_authorization_denied` 是 guard 明确拒绝，其余为任务结果码（如 `runtime_failed`）。报告按原因分开计数。
- **不写 run_id**：报告用不到，也就不让 ID 进入报告流程。

### `acks.jsonl` / `dispatch.jsonl`

- `acks.jsonl`：`{"ack_ms": 38.2, "status": 202}`。只有状态为 202 的行计入 ACK 时延；其余状态按代码计数。
- `dispatch.jsonl`：`{"relay_ms": 120, "queue_ms": 180, "dispatch_ms": 300, "start_ms": 2400, "run_start_ms": 2580}`。口径（负责人 2026-10-13，4A，ROADMAP §7；恢复设计稿 §4.6）：`dispatch_ms` 为来信受理（`accepted_at`）到对应 Run 任务首次被 claim（`runtime.job_events` 的 running），等于 relay 段（受理 → 任务创建 `runtime.jobs.created_at`）加排队段（任务创建 → claim），2 s 门槛只适用于它。`start_ms`（claim → Host 首次执行授权）和 `run_start_ms`（任务创建 → 首次执行授权，即 2026-10-11 的旧口径）只观察。未发生的段为 null。只有 `dispatch_ms` 的旧格式行（2026-10-11 口径）在报告里标为 incomparable，不判通过。

### `refusals.jsonl` / `alerts.jsonl`

- `refusals.jsonl`：`{"code": "NXC05", "at": "<ISO8601>"}`。
- `alerts.jsonl`：`{"rule_id": "guard_timeout_rate", "severity": "warning"|"critical", "kind": "firing"|"resolved", "at": "<ISO8601>"}`。

### `checks.json`（§4.4）

```json
{"run_binding": {"checked": 812, "violations": 0}, "outbound_ownership": {"checked": 790, "violations": 0},
 "claim_ownership": {"checked": 2400, "violations": 0}, "model_context": {"checked": 812, "violations": 0},
 "cross_access_probe": {"checked": 50, "violations": 0}, "duplicate_effects": {"checked": 790, "violations": 0},
 "unresolved_work": {"checked": 600, "unresolved": 0}}
```

- 前 6 项任何一项 `violations > 0`，AT-060 判为不通过（任何格子都适用）。
- 第 7 项 `unresolved` 计入门槛。

## 3. 报告的判定口径（§6.1，只判 C4 和 C4-low）

| 项 | 计算方式 |
|---|---|
| 2 s 工具时限超时率 | guard 样本按分钟合并各服务的 `calls` 和 `timeouts`，在稳态窗口内取每个连续 5 分钟窗口的 `timeouts/calls`，取最大值，要求 ≤1% |
| guard p95 | 5A（负责人 2026-10-13，ROADMAP §7）：稳态窗口内全部 guard 调用的 p95，由各进程各窗口的直方图合并，取所在桶的几何中点（误差 ≤ ±4.9%），要求 ≤1500 ms（`guard_p95_ms`）。原"每分钟各服务 `p95_ms` 的最大值"保留为观察项 `guard_p95_ms_worst_minute`。没有直方图的旧结果标为 incomparable，不判通过 |
| 死信 | 快照中 `queues.*.dead_lettered` 加 `feeds.*.dead_lettered` 在格子结束时减去格子开始时的增量，要求为 0（另外报告开始时的绝对值） |
| ACK p95 | `acks.jsonl` 中 202 的行，要求 ≤500 ms |
| 空闲队列派发 p95 | 只判 C4-low，`dispatch.jsonl` 的 `dispatch_ms`（受理 → claim，4A），要求 ≤2000 ms；启动段与 `run_start_p95_ms` 只观察 |
| 排空时间 | `drain.drained` 为 true，并且 `seconds` ≤600 |
| 连接总数 | 快照中 `connections.total` 的峰值，要求 ≤60 |
| critical 告警 | `alerts.jsonl` 中 severity 为 critical 且 kind 为 firing 的条数，要求为 0 |
| 非预期派发拒绝 | 不在预期集合中的代码计数，要求为 0。预期集合默认为 `NXC05`、`NXC06`，可用 `--expected-refusals` 调整，报告中会写明实际使用的集合 |
| 身份核对 | 前 6 项违例为 0，第 7 项未结局为 0 |

**AT-060 的结论**（§6.2）：
- **passed**：8 个格子全部 `completed`，并且都有逐 Run 分解；两个门槛格全部达标；所有格子前 6 项违例都为 0。
- **blocked**：有格子没有运行（缺少 `cell.json`）。
- **failed**：其他情况，包括格子中止、门槛格缺少指标、门槛不达标或有违例。

不达标时只记录，不放宽任何门槛（§7）。

## 4. 车道脚本与矩阵执行器的接口（`scripts/load/run_on_lane.sh`）

车道脚本由调度员在 CI 车道上执行（D9），不进入 `scripts/ci/check`。

**整体流程**：先在夜间窗口内做预检，然后取得 CI 资源锁并**持锁到结束**，依次执行：
1. 模板：`scripts/load/build_template.sh --seed <s> --consumers <n> --out <run_dir>/template`（L4 已交付）；
2. 矩阵：`uv run --frozen python scripts/load/matrix.py --template <run_dir>/template --matrix deploy/load/matrix.v1.json --out <run_dir> --lane-root <run_dir>/lane`；
3. 报告：`uv run --frozen python scripts/load/report.py <run_dir> --out <run_dir> [--ci-baseline <json>]`。

**对矩阵执行器（L4）的约定**：
- **参数**：接受上面 4 个参数。
- **输出**：写出第 1、2 节中的 `run.json` 和 `cells/`。
- **一次性资源的位置**：所有一次性集群、Valkey、Artifact 目录和进程工作目录都放在 `--lane-root` 下面。车道脚本结束时（无论成败）会停止该目录下所有带 `postmaster.pid` 的集群，并终止命令行中含 run 目录路径的进程，然后报告残留进程数。
- **C4-ci 格**：用环境变量 `LOAD_CI_COMMAND` 给出的命令（`scripts/ci/check`）在锁内启动全量 CI，**不再自己取 CI 锁**（锁已由车道脚本持有）。
- **退出码**：中途失败时返回非零。车道脚本会保留现场：`lane-summary.json` 记录步骤、退出码、各步耗时、已有格子、负载、磁盘和残留进程数，`failure-summary.md` 附上失败步骤日志的最后 40 行。这些文件都只在被忽略的 run 目录里。

**车道脚本的预检**（§5.1）。不满足时退出码为 2，CI 锁被占用时为 75：
- 在 `LOAD_WINDOW`（默认 22:00–06:00）以内；在窗口外执行必须显式加 `--outside-window`；
- 环境中没有任何部署或数据库连接变量（`DATABASE_URL`、`PG*` 等）；
- 环境中没有模型或渠道凭据（`MODEL_*`、`*_API_KEY`、`*_TOKEN` 等）；
- `REAL_DISPATCH_ENABLED` 为 false（脚本还会显式导出这个值和 `NEXLOOP_TEST_GUARD_WORKERS=4`）；
- 检出没有本地改动；
- 工具、矩阵文件和磁盘空间齐备。

**清理旧数据**：加 `--prune-old` 时，删除 `.ci-results/load/` 下 30 天前的 run 目录（D8），只匹配本车道 run id 格式的目录，并逐一列出。
