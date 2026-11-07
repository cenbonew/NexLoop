# 部署单：NX-064 效应请求计数与告警 v29（stage）

- **状态**：草稿。执行者：调度员。判定者：L6。证据只记计数、规则数和 run_id 前缀，不记消息或参数。
- **适用**：分支 `l4/nx064-effect-timeout-design`（设计 v2 与实现，ADR-029c）。迁移 `effect_request_metrics`：临时号 0405，接在第 112 批的 0404 之后，以组批后的编号为准。告警规则 v28 → v29。compose 没有改动。
- **内容**：
  - Agent Host 按操作统计效应请求，统计项为 calls、ambiguous、unavailable 和耗时直方图。
  - runtime worker 每分钟把这些计数写成进程样本：kind `host` 里放合计，kind `host_op` 里按操作放。
  - 快照新增 `effect_requests` 块；告警规则 v29 新增 7 条。
  - 激励工具改用 `effect_timeout_ms`，结果不明确时用同一请求体只再发一次。
  - Host 记录失败工具的错误码时，现在能认出 pi-durable 包装过的错误码。之前抛出的 `runtime_effect_unavailable` 会被记成 `tool_error`。
- **不改的**：`effect_timeout_ms` 不填，保持 2000。stage 2026-11-02 的只读测量中 `effect_submit` 只有 25 次调用，按 ADR-029c 标“未测”。

## 1. 一起上线（同一批，不能拆开）

**Host、runtime worker 和迁移必须一起上线**：

- 只有迁移、没有新 Host：kind `host` 样本里没有效应计数，`host_effect_no_data` 一直报。
- 只有新 Host、没有新 runtime worker：worker 读不懂新键，`read_host_metrics` 按不可用处理，整条 host 样本都不写；这时原来的并发计数也会断掉。
- 只有新 worker、没有迁移：SQL 拒收 `host_op` 和新键，样本写入失败。

顺序：

1. 部署前备份，按现行流程执行 `sudo -n systemctl start nexloop-pg-diff.service`。
2. 部署本批镜像并迁移：迁移尾为组批后的编号，`versions.lock.json` 的 `bootstrap_revision` 与之相同。
3. 重建 `agent-host` 和 `runtime-worker`，其余服务照常。
4. `post_deploy_check.py` 全部通过。

## 2. 告警规则 v29（DATA_HOST，配置员窗口，沿用现行告警版本发布流程）

```bash
docker run $CHECK_COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/alert_rules_apply.py --manifest /opt/release/deploy/configuration/alert-rules.v29.json --check
```

去掉 `--check` 正式应用，其余参数（`--bootstrap-dsn-file` 等）与上一次 v28 的发布相同。预期 103 条：v28 的 96 条，加上 7 条：

- `effect_submit_no_data`、`effect_find_no_data`、`host_effect_no_data`：warning；
- `effect_submit_over_2s`、`effect_find_over_2s`：warning，超过 2 s 的比例 > 1%；
- `host_effect_unavailable`：warning；
- `enterprise_deliver_unavailable`：critical，单独报负责人。

## 3. 上线后的预期告警（不调规则）

- **`effect_find_no_data` 会持续报 warning**：stage 上 `effect_find` 在最近 7 天没有样本，只读结果为 0 行。没有查询流量时它一直报，**这是预期**：“没有数据”不算通过，所以不要为它调规则或阈值。有了查询流量后，它在 24 小时窗口内会自动恢复。
- `effect_submit_no_data`：stage 有回复流量时不会报；如果 24 小时内没有任何回复，也会报，同样属于预期。
- `host_effect_no_data`：第 1 节的三样都上线、runtime worker 写出第一条带效应计数的 host 样本后（约 1–2 分钟），在下一个评估周期恢复。**超过 10 分钟还在报**，就说明 Host 或 worker 版本不对，或者采样停了，需要排查。
- `enterprise_deliver_unavailable`（critical）：出现时，按告警说明到 intent 列表按 run_id 核对是哪一种：(a) 单子已下但客户没被告知，或 (b) 没有单子。**只通知，不自动补单。**

## 3a. 失败 Run 记录里的错误码分布会变（这是修正，不是回归）

部署后，失败 Run 的 `failure_detail`（`runtime.jobs.result`，以及 NX-037 证据包里的 failures）中，**`tool_error` 的计数会下降，具体错误码的计数会上升**，例如：

- `runtime_effect_unavailable`、`incentive_unavailable`、`incentive_payload_conflict`、`outreach_unavailable`、`run_outcome_unavailable`；
- `runtime_tool_unavailable`：工具抛出普通错误时，`pi-runtime-adapter.ts` 里包装工具的那一段给出的错误码。

原因是 Host 现在能读出 pi-durable 包装过的错误码；以前这些失败一律记成 `tool_error`。`tool_error` 今后只表示工具结果的文字里没有任何错误码。按错误码统计的看板或对比，在部署前后不能直接相加。证据包的白名单已经补上这些错误码（`scripts/nx037/evidence_pack.py`，由 `tests/test_nx037_evidence_codes.py` 从 Host 源码核对），不会把它们当成未知值丢掉。

## 4. 只读核对

```sql
select runtime.nexloop_metrics_snapshot('<TENANT_ID>','real')->'effect_requests';
select kind, service, count(*) from runtime.nexloop_process_samples
 where kind in ('host','host_op') and window_start>now()-interval '15 minutes' and (kind='host_op' or sample ? 'effect_calls') group by 1,2;
```

- 预期：`host` 一行，每分钟约 1 条；`host_op` 只有在这段时间内有效应请求的操作才有。
- `effect_requests.host.present` 为 true，`effect_requests.effect_find.no_data` 为 1（见第 3 节）。

## 6. 度量门禁：Host 端 p99 与部署时限（设计 §4.3，ADR-029c §1.2）

- **执行者**：调度员。全部只读，不改任何配置。判定者：L6。
- **脚本**：`scripts/acceptance/nx064_p99_gate.py`。它只读一个文件，不连接任何东西，输出一行 JSON：`samples`、`p50_ms`、`p99_ms`、`p99_upper_ms`、`gate`、`reason`、`suggested_effect_timeout_ms`。
  - `gate` 的判定：
    - `pass`：p99 所在桶的上界 × 1.5 不超过部署时限；
    - `fail`：超过；
    - `insufficient`：请求不足 1000 次、直方图为空，或者读不到来源。
  - **insufficient 不算通过**：证据里照 ADR-029c 标“未测”，附上 `samples`，`effect_timeout_ms` 保持不写。
  - 判定用的是桶的上界，不是几何中点，所以不会因为直方图约 4.9% 的估计误差而误判通过。
  - 退出码：0 pass，1 fail，2 insufficient，3 输入被拒。
- **需要的入口与凭据**：不需要新凭据。
  - 直方图：DATA_HOST 上第 4 节已经在用的只读会话 `ro`（bootstrap socket、`begin read only`、`set local role nexloop_owner`，最后 rollback；定义同 run-nx057 §0.3）。
  - 部署时限：APP_HOST 上私有 Agent Host 运行配置里的 `effect_timeout_ms`，只读这一个键。
  - 不走 Host 的 `/internal/v1/metrics`：那个端点只在回环上，要 runtime worker 的 metrics 凭据，而且计数从 Host 启动时累计，重启就清零；进程样本表里已经有同样的数据（每分钟一份增量）。如果以后要用这个端点，需要的凭据是 runtime worker 读 Host metrics 用的那一份，由负责人决定是否发放，L4 不去拿。

步骤：

1. **部署时限**（APP_HOST，只读）：`jq '.effect_timeout_ms // 2000' <私有 agent-host 运行配置>`。没有这个键时就是 Host 默认的 2000。
2. **导出 7 天的 Host 端直方图**（DATA_HOST，只读）。`ro` 输出的第一行是租户 ID，用 `tail -n +2` 去掉：

   ```bash
   ro "select json_build_object('kind',kind,'service',service,'window_start',window_start,'sample',sample) from runtime.nexloop_process_samples
        where kind='host_op' and service='effect_submit' and window_start>now()-interval '7 days' order by window_start" | tail -n +2 > nx064-host-op.jsonl
   ```

   文件里只有计数和桶号，没有 Run id、消息或参数；它只在执行机上留存，证据里不放原文件。
3. **判定**：

   ```bash
   python3 scripts/acceptance/nx064_p99_gate.py --samples nx064-host-op.jsonl --limit-ms <第 1 步的值>
   ```

   需要时，把 `--operation` 换成 `effect_find` 或 `incentive` 再跑一次。
4. **没有 Host 端数据时**（gate 为 insufficient，reason 为 `empty_histogram`）：可以用 guard 端估算。第 2 步把 `kind='host_op'` 换成 `kind='guard_op'`（0186/0188：guard 按操作的样本，service 为操作名，同样带 `h_<i>`），第 3 步加 `--kind guard_op`。输出会带 `estimate: guard p99 + 300 ms`，证据里照写“估算”。**用 `--kind guard_op` 得出的 pass 只是估算，不能代替 Host 端的实测**：证据里记为“估算（guard p99 + 300 ms）”，不能记为“通过”。按它定的值，等 Host 端样本够 1000 次后要用第 2、3 步重新判定。
5. **CI 主机的负载矩阵**：跑完一格后，直接用该格结果目录里的 `process_samples.jsonl`：`--samples <结果目录>/process_samples.jsonl`。

**证据**：只记脚本输出的那一行 JSON，以及部署时限的来源（“私有配置第 1 步的值”或“默认 2000”）。

- `pass`：可以按 `suggested_effect_timeout_ms`，经负责人确认后写进部署配置。它按 ADR-029c 的公式算出：clamp(ceil(p99 × 1.5 / 100) × 100, 2000, 5000)；超过 5000 时为 null，转为性能问题处理。
- `fail`：说明当前时限偏紧，按 ADR-029c 重新定值，不要改脚本的门槛。
- `insufficient`：标“未测”。

## 5. 回退

1. 告警规则回到 v28：用同一个工具发布 `alert-rules.v28.json`，规则按文件版本发布，不手工删除。
2. 镜像回到上一个发布：Host 和 worker 一起回退。迁移不回退：旧代码不写 `host_op`，也不读新键；多出来的 kind 和快照块不影响旧代码。
