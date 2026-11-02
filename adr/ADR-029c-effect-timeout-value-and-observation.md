# ADR-029c｜效应请求时限：按测量定值、Host 端观测、激励工具同规则

状态：**采用**（Accepted）。NX-064 设计 v2（`docs/implementation/NX-064-effect-timeout-design.md`）经 L6 判 PASS，调度员 2026-11-02 派实现。

关联：ADR-022 §4（2 s 工具时限，不放宽默认值）；ADR-028（结果不明确时同一请求体再发一次）；ADR-029b §3a（`effect_timeout_ms` 可配置 1000–5000，“按 p99 定值以后另写 ADR”，即本文）。

## 1. 决定

1. **默认值不变**：代码默认仍是 2000 ms，上下限仍是 1000–5000 ms（`runtime-effect-tools.ts`）。各部署按测量结果配置 `effect_timeout_ms`，代码里不写死某个部署的值。
2. **定值规则**（写进运行单，由执行人按测量结果填写）：
   - `effect_timeout_ms = clamp(ceil(p99_host × 1.5 / 100) × 100, 2000, 5000)`。
   - `p99_host` 取 Host 端 7 天的 p99，数据来自 kind `host_op` 的直方图（见第 3 条）。没有 Host 端数据时，可以用 guard 端 `guard_op` 的 p99 加 300 ms 估算，**证据里写明“估算（guard p99 + 300 ms）”**。
   - **样本不足不定值**：查询没有返回行，或者 `calls < 1000` 时，不写 `effect_timeout_ms`，保持 2000，证据里标“未测”，并附上 calls 数。
   - 算出来超过 5000 时不提高上限，转为性能问题另行处理。
   - stage 2026-11-02 的只读结果：`effect_submit` 只有 25 次调用，标“未测”，保持 2000。
3. **观测**：只用进程样本，不读日志。
   - Host 进程按操作累计效应请求的计数：操作分为 `effect_submit`、`enterprise_submit`、`effect_find`、`incentive`；计数有 `calls`、`ambiguous`（第一次请求不明确）、`unavailable`（以不可用结束），另有每次 HTTP 请求在 Host 端的耗时，按 guard 的对数直方图分桶 `h_<i>`。这些计数随 `/internal/v1/metrics` 提供。
   - runtime worker 每分钟读一次，把增量写成两种样本：
     - kind `host`：所有操作合计的 `effect_calls`、`effect_ambiguous`、`effect_unavailable`。只要 Host 可读就写，没有请求时记 0；
     - kind `host_op`：每个操作一条，service 为操作名。这个窗口里该操作有请求时才写。
   - 迁移 `effect_request_metrics` 让 SQL 接受上述键，只接受数字。
4. **告警**（规则 v29，快照块 `effect_requests`，看最近 24 小时）：
   - 三项分别判断：`effect_submit` 和 `effect_find` 读 guard_op 样本，`host` 读带效应计数的 host 样本。任何一项没有样本，就报对应的“无数据”告警，**不算通过**。
   - 超过 2 s 的比例 > 1% 时报警告；Host 端出现 `unavailable` 时报警告。
   - **`nexloop.enterprise.deliver` 单独报负责人**，级别 critical。数据取自该租户失败 Run 的任务结果（`runtime.jobs.result` 中的 `failure_tool` 和 `failure_detail`），附最多 16 个 run_id。告警说明写明两种可能：
     - (a) **单子已下，但客户没被告知**：Host 放弃等待后，guard 仍提交了 intent；Run 失败，模型没有拿到结果；
     - (b) 没有单子。

     请负责人按 run_id 到 intent 列表核对。**只通知，不自动补单。**
5. **激励工具同规则**：
   - `issueIncentive` 改用 `effect_timeout_ms`，原来写死 2000。
   - 超时、连接错误或 503 判为不明确，**同一请求体只再发一次**，仍不明确时报 `incentive_unavailable`。
   - 冲突（NXB02 → 409）和其他确定的拒绝都立即结束，不再发。

## 2. 激励端的幂等键，与 0040 的差别

前提核对的结论：**成立**。

- Run 入口 `authz.nexloop_run_incentive_command`（0145:237，之后没有迁移改过）由服务端把键定为 `'incentive-run:'||run_id||':'||incentive_offering_id`。
- `runtime.nexloop_incentive_accept` 遇到同键时：参数相同，返回已存的 intent（`replayed:true`）；参数不同，报 NXB02。

和 0040 的差别有三点：

1. 键里没有 step 和 slot：同一 Run 对同一 offering 只能有一条 intent。
2. 比较的参数不含 `notification_text`：同一个键、只有通知文字不同的请求按重放处理。
3. **先查后插，不加锁**。第一次的事务还没提交时到达的重发，查不到旧行，就去插入，在主键上等待；第一次提交后，重发以 `unique_violation`（23505）失败，**不是 NXB02**。guard 按 SQLSTATE 把它映射为 403 `incentive_unavailable`（`runtime_control.py`），Host 立即结束这次调用，不会发第三次。

这三点都不会产生第二条 intent：预算在派发时按 intent 行预留，所以也不会重复预留。代价只是：在第 3 点的情形下，Run 得到的是“不可用”，而不是那条已经存在的 intent。

效应路径（0040）的实际行为不同：同一 Run 的工具请求先在 Run 的 advisory 锁（`nexloop-role-effect:<run>`）上排队，所以第一次仍持锁时，重发会等待；第一次提交后，重发查到同一个 intent（`tests/test_effect_resend_pg.py` 第三组按实测结果断言）。

## 3. 不采用

- **按负载自适应时限**：时限不可预测，负载越高、时限越长、在途越多，形成正反馈；也难以测试。
- **把 `runtime_effect_unavailable` 设为可恢复码，让模型再调一次**：模型拿不到 intent_id，第二次的载荷可能不同，违背“先查询”。
- **超时后给模型一个“checking”结果**：Host 不知道 intent 是否存在。

## 4. 验证

- Host（vitest）：`apps/agent-host/test/effect-metrics.test.ts`。
  - 计数按操作区分；直方图的分桶与 guard 相同。
  - 激励工具：用配置的时限，同一请求体只再发一次；503 判为不明确；冲突立即结束。
  - 已有的 `effect-timeout-config.test.ts`、`effect-ambiguous-retry.test.ts` 不变，并且通过。
- Python：`tests/test_host_metrics_pg.py`，计数写成 `host` 和 `host_op` 两种样本，Host 重启后从 0 计。
- PG：
  - `tests/test_effect_request_metrics_pg.py`：样本 SQL；三项各自的“无数据”；超过 2 s 的比例；enterprise 通知只取本租户，最多 16 个 run_id；v29 保留 v28 的全部规则。
  - `tests/test_effect_resend_pg.py`：效应路径三组。
  - `tests/test_incentive_resend_pg.py`：激励三组，第三组断言实测的 `unique_violation`。
- 兜底 Run 预算：`tests/test_effect_timeout_fallback_budget.py`。时限取上限 5000 ms 时，兜底 Run 最多 2 次工具调用，每次最多 2 个请求，合计 20 s，小于 `active_timeout_seconds` 60 s；所有回复保障策略仍能装载。
