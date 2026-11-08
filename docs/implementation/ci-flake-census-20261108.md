# CI 不稳定用例盘点（2026-11-08，窗口 30 天）

- 数据：CI 主机 `/srv/nexloop/vcs/queue/results/*/attempt-*` 的 `result.json` 和 `failures.json`。汇总脚本只读，由调度员在 CI 主机上执行，结果只含用例名、计数、时间和 CI 已脱敏的消息摘要。
- 范围：2026-10-09 到 11-08，失败 attempt 69 次，通过 94 次。
- 执行者：L2 写脚本、做分类；调度员在 CI 主机上执行。

## 总体分类（按失败 attempt 的主要原因）

| 类别 | 次数 | 说明 |
|---|---|---|
| 环境 | 约 11 | `stage_failed:dependency-python` 8 次、`dependency-node` 2 次（同一批 attempt 连带出现 `suite_missing:*` 各 10 次、`test_count_dropped:python+python-heavy+python-deadline-gate` 11 次）；10-26 fb0e3d4f 一次 CI 主机上 `initdb` 失败、`StorageUnavailable`，连带约 25 个用例 setup 失败 |
| 真实回归 | 约 45 | 同一个 sha 重跑仍失败，后来被修好。主要是 10-29 前后的一批（bootstrap、deploy_render、compose_bootstrap、db_boundary、connection_budget、doctor、wheel_install、core_sandbox、effect_execution_sql、assembly_dryrun、context_strategy_setup，与 `serial-build` 失败 4 次同期），以及漏改白名单、计数或示例的单个用例（tenant_constants、consumer_erasure_coverage、tenant_fk、claim_store、function_copy_guard、nx041_stand_ins、api_route_inventory_pg、i35_owner_property_read、closure_refusal_versions、workbench_oidc、nx037_assembly_examples、ci_stages heavy 计数） |
| 不稳定 | 约 13 | 同一个 sha 重跑通过，或者与当次改动无关。到 11-08 为止，除 runtime_worker 的 SIGTERM 外都已修复（见下表） |

计数是按每个失败 attempt 的主要原因归类后的近似值：一个 attempt 可能同时出现几类失败，所以三类加起来不等于 69。

## 不稳定 top 5 与对应的处理

| 用例 | CI 上的失败消息 | 原因 | 处理 |
|---|---|---|---|
| `test_message_runtime_effect_e2e::test_same_message_pi_effect_worker_and_current_human_receipt` | `'observed_fulfilled' == 'fulfilled'` | 负载下最终确认时授权暂时不可用，产品按设计停在 observed_fulfilled，之后由对账推进 | 6c1dd3fd：断言最终收敛，另加注入用例 |
| `test_scope_denials::test_real_pi_distinct_tool_calls_persist_denial_and_human_http` | 10-19、10-20：`JSONDecodeError`；11-01：`request_scope: schema is false` | 前两次是负载问题：进程内 guard 与测试的 inspect 轮询抢请求容量；11-01 是真实回归（S85 8cfac8a5 只在供给允许时提供 request_scope） | 10-20：guard 改为 4 个进程；11-01：a4471218 |
| `test_remediation_pg::test_the_port_bounds_the_round_at_5_seconds_and_the_planner_reports_it`（另有 `..._runs_once_a_minute...`） | 10-25：`not_due` | 不同测试共用进程级的 `_LAST_ROUND` 轮次状态 | bb9f8871（状态可注入）；本分支 040d1a8a 及其后续：去掉墙钟上限，改为在端口会话里断言 `statement_timeout` 为 5s |
| `test_nx049_memo_stress_pg::test_action_memo_never_serves_a_revoked_or_expired_allow[None]` | `179 >= 200`、`196 >= 200` | 600 秒时间窗口在负载下提前结束 | 16347009、f6d92f01：改为按固定撤销轮数结束，所有撤销线程共用一个 1800 秒截止 |
| `test_nx056_approvals_store_pg::test_a_request_is_frozen_once_and_never_flooded` | `timedelta(days=1, microseconds=1) == timedelta(days=1)` | **产品缺陷**：`approval_open` 读了两次时钟 | d626b36b（迁移 0372，只读一次时钟） |

其他不稳定项：
- `test_runtime_pg_bridge::test_actual_pg_lease_expiry_stops_pi_tool_dispatch`：`_queue.Empty`，98959b99 已修（改为等 PG 判定租约过期）。
- `test_context_v6_relationships_pg::test_v6_carries_v4_relationships_with_hypotheses_as_evidence_only`：`runtime_failed`，原因未查明；09539fb6 让断言输出完整结果，98959b99 挪进 heavy 阶段，之后没再失败过。
- `test_runtime_worker::test_cli_dispatch_loops_share_the_queue_each_task_claimed_once`：request-timeout 那个原因由 9582cc8b 修复；SIGTERM 后不退出那个原因还没复现，a605c26f 加了诊断（失败时带出线程调用栈），上线后还没有再触发。

## 以后再做盘点

- 汇总脚本（ci_flake_census.py：每个用例的失败次数、sha 数、重跑通过数、最近一次时间；非用例原因；SIGTERM 诊断命中数）和取消息摘要的脚本（ci_flake_messages.py）都不在仓库里，在 L2 会话的 scratchpad 中。需要时请 L2 再提供；如果要长期使用，可以另行收进 `scripts/ci/`。
- `rerun_passed` 会被环境问题抬高：一次 CI 主机故障之后，同一个 sha 重跑通过，看起来就像不稳定。分类前先排除 setup 阶段的环境失败。
