# heavy 阶段：按吞吐或时延阈值判定的用例

依据：调度员 2026-10-14 派工（发布 CI 第二次被 `test_nx049_memo_stress_pg.py::test_action_memo_never_serves_a_revoked_or_expired_allow[None]` 挡住，`assert (133 >= 200)`）。分支 `l4/heavy-stress`，自 int/s5g `08c6a4f`。

## 做了什么

- 只改了 `scripts/ci/stages/lib.sh` 的 `HEAVY_TESTS`：新增 12 个 node id（收集到 21 项），从全量并行阶段（`-n 6`）移到 heavy 阶段（`-n 2`）。
- 阈值和断言一条都没改。
- 阶段名单没变：`.ci-policy/stages.v4.json` 和 `required-suites.v2.json` 都没有改动。
- 移动的用例按 D-j 的计数组（python + python-heavy + python-deadline-gate 合计）计数，所以这次移动不算掉用例。
- `tests/test_ci_stages.py`：heavy 名单 20 → 32 项；gate 加 heavy 的收集数 27 → 48。

## 清单

| 用例 | 判定方式 | 移动理由 |
|---|---|---|
| `test_nx049_memo_stress_pg.py::test_action_memo_never_serves_a_revoked_or_expired_allow`（全部 4 个参数） | `[None]`：`revocations>=200`；`[no_trigger]`：`own_write_statements>=100`；其余两个参数在固定 8 s 窗口内运行 | 并发撤销/断言压测，次数取决于给定时间内能完成多少轮。发布 CI 的失败就是 `133 >= 200` |
| `test_nx049_memo_stress_pg.py::test_derived_message_memo_never_serves_a_revoked_deleted_or_moved_allow`（全部 3 个参数） | `[None]`：`revocations>=600`；变异参数在固定 8 s 窗口内运行 | 同上，派生 Message READ 压测 |
| `test_read_assert_memo.py::test_concurrent_revocation_never_serves_a_stale_allow` | `revocations>=200`、`statements>=200`、`stale_statements>=50`、`hits>0` | O5b 并发撤销压测，判定方式与上面两类相同 |
| `test_runtime_effect_recovery.py::test_same_pi_run_survives_host_and_effect_worker_kill_query_first` | 真实 Pi + Host/效应 worker 被 kill 后恢复；事件屏障上限 30 s，guard 单次调用 2 s 时限 | 和已经在 heavy 的真实 Pi/kill 类用例同属一类（ADR-020 B/C 类），对负载敏感 |
| `test_backend_lifecycle_capacity.py::test_eight_concurrent_requests_complete_on_pool_of_four` | 墙钟 `elapsed < 150` 且没有 `BackendBusy` | 并发容量加墙钟上限 |
| `test_effect_origin_expired_pg.py::test_a_dispatched_unknown_intent_stops_requery_and_unknown_backs_off` | 退避剩余时间 `3 <= seconds <= 4.5`（相对当前时间，余量 0.5 s） | 墙钟窗口，负载下语句耗时会侵占余量 |
| `test_effect_provider_transport.py::test_numeric_https_silent_tls_handshake_obeys_absolute_deadline`（全部参数） | `elapsed < 1.5` | 时延上限 |
| `test_effect_provider_transport.py::test_tls_trickle_close_response_cannot_extend_absolute_deadline`（全部参数） | `elapsed < 1.5`、`2 <= len(sent) < 30` | 时延上限，加上窗口内的发送次数 |
| `test_valkey_wakeup_product.py::test_trickled_tls_reply_has_global_deadline_after_pg_commit` | `elapsed < .8` | 时延上限 |
| `test_relay_multi_consumer_pg.py::test_enrollment_throughput_in_process` | 每个客户 `elapsed / n < 0.5` s | 吞吐阈值 |
| `test_nx037_endpoint_conformance.py::test_conforming_endpoint_passes_every_check` | 每项检查 `elapsed_ms < 2000` | 时延上限 |
| `upstream/test_eios_application_restrictions.py::test_large_restriction_sets_have_bounded_lookup_cost_without_truncation` | `perf_counter` 计时 `elapsed < 1.0` | 时延上限 |

## 查找方法与没有移动的用例

我在 `tests/` 中搜索以下几种写法：

- 在 `time.monotonic` / `perf_counter` 窗口后断言 `elapsed|latency|duration|_ms <` 或 `<=`；
- 断言 `>= <两位以上的数>`；
- `p95|p99|percentile|throughput`。

下面这些命中了搜索，但没有移动：

- 判定的是静态数量（表数、用例数、令牌长度、FK 数）或时间戳差值（TTL、`age_seconds < 120`）：与负载无关。
- `test_load_*`、`test_history_monitor`、`test_guard_op_samples`、`test_relay_step_timing`：断言的是报告格式或注入的假时钟，不测本机吞吐。

另外，仓库里没有找到名为 `CI-BASELINE` 的文件。上面四类历史用例（nx049_memo_stress、read_assert_memo、runtime_effect_recovery、effect recovery）是按派工里的名字和上面的搜索结果核对的。`test_effect_dispatch.py::...worker_sigkill_recovers_query_first` 和 `test_local_effect_worker_kill.py::...` 是靠事件等待的恢复用例，没有吞吐或时延阈值，这次没有移动。如果 CI 的失败记录里出现过它们，请告诉我，我再补上。

## heavy 阶段时长（需要关注）

本机在 `-n 2` 下单独跑这 21 项，墙钟 616 s，负载 6–7：

| 用例 | 耗时 |
|---|---|
| derived `[None]` | 569 s |
| action `[None]` | 201 s |
| read_assert 并发撤销 | 125 s |
| 其余 18 项 | 每项 ≤ 9 s |

结论：

- heavy 阶段（目前约 4 分钟）的最长路径会由 derived `[None]` 决定。本机估计 heavy 阶段会变成约 10 分钟。这只是本机数字，CI 主机上要实测，CI 主机通常更快。
- 全量并行阶段的总耗时会少掉同样的这几项。不过这三项本来就跑在 `-n 6` 的单个 worker 上，所以端到端总时长的变化取决于它们原来是不是并行阶段的长尾。
- 如果 heavy 阶段时长不能接受，可以把 `NEXLOOP_CI_HEAVY_WORKERS` 提到 3，让三个长压测分到不同 worker 上并行。这要由调度员决定，这次没有改。

## 本机结果

- 20 项通过。
- 1 项失败：`test_valkey_wakeup_product.py::...`，原因是本机 Docker 守护进程没有运行（`docker info` 返回 1），报 `owned test container command failed`。这是本机环境问题，与这次移动无关。
