# NX-064 设计：效应请求时限（Host 2 s）在高负载下超时的产品处理与按 p99 定值

- 状态：**设计稿 v2，L6 PASS；已实现**（见 §6）。执行人：L4。判定人：L6。v1（d89d0645）条件 PASS，同意方案 C；v2 补入条件 C1–C5，以及兜底 Run 预算的验收。基于 main 2bad63f9。
- 关联：ADR-022 §4（2 s 工具时限、不放宽）；ADR-028（结果不明确时同载荷再试一次）；ADR-029b §3a（`effect_timeout_ms` 可配置 1000–5000，按 p99 定值另写 ADR）；`docs/implementation/ci-flake-audit.md` 候选 1；AT-060（guard 按操作的时延样本）。
- 本文只读代码与文档，不连主机。下文的命令是给执行人的只读命令，主机只写角色名。

## 1. 现状：时限在哪里、对谁生效、超时后各对象的状态

### 1.1 定义与生效范围

| 位置 | 内容 | 生效对象 |
|---|---|---|
| `apps/agent-host/src/runtime-effect-tools.ts:96-98` | `EFFECT_TIMEOUT_MS=2000`，`EFFECT_TIMEOUT_MIN_MS=1000`，`EFFECT_TIMEOUT_MAX_MS=5000` | Host 的效应客户端 |
| 同文件 `:100-104` `effectTimeoutMs()` | 不填用 2000；填了必须是 1000–5000 的整数，否则 Host 拒绝启动 | 同上 |
| 同文件 `:188` | `RuntimeEffectClient` 构造时定下 `timeoutMs` | 同一 Host 进程内全部效应请求 |
| 同文件 `:283`（`once`） | `AbortSignal.timeout(timeoutMs)` 加 `request.setTimeout(timeoutMs)`：**每一次** HTTPS 请求一个时限 | `nexloop.service.request`（submit）、`nexloop.enterprise.deliver`（submit）和二者的 find |
| `apps/agent-host/src/runtime-host.ts:107,160,402` | 运行配置键 `effect_timeout_ms`，校验后传给效应客户端 | 只在 Host 端；guard 端没有对应时限 |
| `apps/agent-host/src/runtime-host.ts:21` | `GUARD_TIMEOUT_MS=2000`（授权调用 start/model/tool/outcome） | **不在 NX-064 范围**：这是 ADR-022 的授权时限，不随 `effect_timeout_ms` 变 |
| `packages/eios-core/src/nexloop_eios/observability.py:30` | `GUARD_DEADLINE_MS=2000`：guard 样本里 `timeouts` 按这个常量计数 | 只影响观测；配置了 `effect_timeout_ms` 时口径会和实际时限不一致（见 §5 第 4 条） |

激励工具（`runtime-effect-tools.ts:372,390`，端点 `/internal/v1/runtime/incentives/issue`）写死 `2000`，没有接 `effect_timeout_ms`，超时后直接报 `incentive_unavailable`，不再试。**纳入本单（C5）**，见 §3 第 7 条。

### 1.2 超时后的路径（`runtime-effect-tools.ts:225-240, 283`）

1. 第一次请求超过时限：`request.destroy()` 触发 `error`，记为 `runtime_effect_ambiguous`（结果不明确）。
2. 如果 Run 的 `not_after` 还晚于“现在 + 时限”，用**完全相同的请求体**再发一次（ADR-028）。guard 按 0040 的唯一键（Run 的 consumer、goal、step、slot）推导意图：载荷相同，返回的就是同一个 intent，不会再预留额度，也不会产生第二个效应；载荷不同则冲突（409）。所以这一次是**按 0040 键的幂等重发，等价于查询或首次创建**：第一次已经落库时，它查到那个 intent；第一次没有落库时，由它首次创建。它不是盲目重试。
3. 第二次仍不明确：Host 在 stderr 写一行诊断 `{event:'runtime_effect_unavailable', run_id, operation, effect_timeout_ms, elapsed_ms}`，工具抛出 `runtime_effect_unavailable`。
4. `runtime_effect_unavailable` **不在** `RECOVERABLE_TOOL_CODES` 里（`pi-runtime-adapter.ts:15`），所以 Run 以 `runtime_tool_error` 失败，调度端记为 `runtime_failed`（`runtime_dispatch.py:62,344`）。

### 1.3 超时后三个对象的状态

| 对象 | 状态 |
|---|---|
| **Run** | `runtime_failed`（failure_code `runtime_tool_error`，failure_tool 为该工具）。模型拿不到 intent_id，这次 Run 之后的步骤（例如 plan outcome）都不再执行。 |
| **intent** | **不确定，但最多一个**。Host 放弃等待并不会取消 guard 端的处理：guard 线程会把事务做完。所以可能出现两种情况：（a）intent 已按 0040 落库，为 `accepted`；（b）guard 事务没有提交，没有 intent。两次请求最多对应一个 intent。 |
| **对外效果** | 情况（a）：action worker 照常派发，发送前按当前权限复查（AGENTS.md）。客户会收到回复，或者企业会收到单子，尽管 Run 已经失败。未知的外部结果按 0152 先查询、对账，不重发。情况（b）：没有对外效果。 |

兜底：

- **回复**（`nexloop.service.request`）：适用 ADR-023 §2.7 的回复保障。
  - 消息绑定的回复已经 `provider_accepted` 或 `delivered` 时，算已答复，不会起兜底 Run（`0109` 第 364、374 行）。
  - 否则，到 `start_after_seconds`（v1 为 300 s）后起**最多一个**兜底 Run。兜底 Run 同样受这个时限约束，再失败就升级给负责人。
- **企业单**（`nexloop.enterprise.deliver`）：没有自动兜底。intent 若已落库，就走它自己的状态机；若没有落库，则没有单子。只能在 Run 失败的诊断中看到这次失败。

**结论**：现在的行为满足“未知结果先查询、不盲目重试”和“不产生第二个效应”。产品上的代价有三点：

1. 有效的回复已经发出，Run 却记为失败，错误计数虚高；
2. 没有落库时，客户要等 5 分钟才收到兜底回复；
3. 企业单没有兜底，只留下一次 Run 失败。

## 2. 现有 p50 / p95 / p99 数据从哪里取

| 来源 | 口径 | 只读取法 | 局限 |
|---|---|---|---|
| **stage：guard 按操作样本**（AT-060，kind `guard_op`，service `effect_submit` / `effect_find`；`runtime.nexloop_process_samples`，分钟样本保留 7 天） | guard 端处理时间。每个进程每分钟一条，带对数直方图 `h_<i>`，相对误差不超过 4.9%，可以跨进程合并 | 见下方 SQL（经 DATA_HOST 上的只读连接 `ro`） | 不含 Host 到 guard 的 TLS、排队和网络时间。所以 Host 实际看到的时间 ≥ 这里的值 |
| **stage：Host 诊断行** | Host 端时间，只在**失败**时写 | APP_HOST：`docker logs --since 168h <agent-host 容器> 2>&1 \| grep -c '"event":"runtime_effect_unavailable"'`；只看数字字段：`… \| grep '"event":"runtime_effect_unavailable"' \| python3 -c '…'` 取 `elapsed_ms` | 成功的请求没有记录，算不出分位数，只能数超时次数 |
| **CI** | 失败轮次日志里的 `runtime_effect_unavailable` 诊断行 | CI 主机：失败轮次的测试日志 `grep runtime_effect_unavailable` | 只有失败样本；全并行阶段的负载每轮不同 |
| **bench：NX-031 负载矩阵**（`scripts/load/`，结果里的 `process_samples.jsonl` 含 `guard_op`；`report.py` 的“守卫按操作分项”表） | 受控并发下 guard 各操作的 p95 和 max | CI 主机：已有轮次的 `process_samples.jsonl`；用 `report.py` 的 `guard_operations` 重新汇总 | 现在的表只给 p95 和 max，没有 p99；直方图在样本里，可以离线补算 |
| **ADR-029b §2 的本机降速测量** | guard 端 submit：正常负载 0.23–0.26 s，降速 1.20–1.43 s；find 偶尔 2.0–2.2 s | 文档 | 只有一次手工测量，不能代替 stage 的分布 |

stage 上 `effect_submit` 的分位数（7 天，跨进程合并直方图，取桶的几何中点，单位 ms）：

```sql
-- DATA_HOST, read-only connection (`ro "<sql>"` as in the run sheets)
with h as (
  select substr(e.key,3)::int i, sum(e.value::bigint) n
  from runtime.nexloop_process_samples s, jsonb_each_text(s.sample) e
  where s.kind='guard_op' and s.service='effect_submit' and s.window_start>now()-interval '7 days' and e.key like 'h\_%'
  group by 1),
c as (select i, sum(n) over (order by i) cum, sum(n) over () tot from h)
select q, round(power(1.1, min(i)-0.5)) as ms, max(tot) as calls
from c, unnest(array[0.5,0.95,0.99]) q where cum >= q*tot group by q order by q;
```

- 把 service 换成 `effect_find`，就是 find 的分位数。
- 按小时看最差窗口：同样的直方图按 `date_trunc('hour', window_start)` 分组。
- 超过 2 s 的次数：`select sum((sample->>'timeouts')::int) from runtime.nexloop_process_samples where kind='guard_op' and service='effect_submit' and window_start>now()-interval '7 days'`。
- Host 当前用的时限（APP_HOST，只看键）：`jq -r '.effect_timeout_ms // "default 2000"' <agent-host.runtime-config.json>`。

## 3. 方案选项

| | A. 按 p99 加余量定一个值 | B. 按负载自适应 | C. 可配置加上限（现状）+ 按数据定值 + 观测 |
|---|---|---|---|
| 做法 | 用 stage 测得的 Host 端 p99 乘以余量（例如 1.5），作为代码里的新默认值 | Host 按近期时延或在途数动态放宽时限 | 保留默认 2000 和 1000–5000 上限；每个部署按自己测得的 p99 写 `effect_timeout_ms`；补齐观测和告警 |
| 好处 | 简单，一个数 | 负载尖峰时少失败 | 已有实现（ADR-029b）；值随部署走，可回退；不改代码默认值，也就不放宽 ADR-022 |
| 风险 | 一个默认值套所有部署，硬件不同时偏紧或偏松；改代码默认值等于放宽 ADR-022 §4，需要新 ADR | 时限变得不可预测，同一请求在不同时刻的判定不同；负载越高，时限越长，在途越多，形成正反馈；难以测试和复现；会掩盖性能回退 | 需要人看数据定值；值偏大时，Run 在一次工具调用上最多耗时 2×时限（再试一次）；值写错时 Host 拒绝启动，这是已有行为 |
| 结论 | 不单独采用 | **不采用** | **推荐**（L6 同意） |

### 推荐：C，具体内容

1. **定值规则**（写入运行单，不进代码）：
   - `effect_timeout_ms = clamp(ceil(p99_host × 1.5 / 100) × 100, 2000, 5000)`。
   - `p99_host` 取 stage 7 天里 Host 端的 p99。没有 Host 端数据时，用 guard 端 p99 加 300 ms 估算 TLS 和排队时间，**证据里写明“估算（guard p99 + 300 ms）”**。
   - **样本不足不定值（C4）**：§2 的 SQL 没有返回行，或者 `calls < 1000` 时，不写 `effect_timeout_ms`，保持默认 2000，并在证据里标“未测”（附 calls 数）。下一轮测量再判断。
   - 结果低于 2000 时不收紧，保持 2000（ADR-022 的下限语义）；高于 5000 时**不**提高上限，转为性能问题（ADR-029b 的信封预刷新或 guard 拓扑），另行处理。
2. **补 Host 端时延直方图**（小改动）：效应客户端在每次请求结束时（无论成功还是失败）按操作记一条，用与 AT-060 相同的对数分桶，进 Host 的进程样本（kind `host`）。这样 p99 才是 Host 实际看到的时间，不只是 guard 处理时间。只有数字。
3. **观测口径对齐**：guard 样本的 `timeouts` 现在按常量 2000 计数（`observability.py:30`）。效应操作改为按两档计数：超过 2000，以及超过一个告警阈值（例如 0.8 × 部署时限，由告警规则给值）。guard 端拿不到 Host 的配置，不读它。
4. **告警（C2、C3）**：数据只取进程样本，**不读 docker logs**（告警读不到日志，读不到时会静默通过）。
   - 数据源：第 2 条新增的 Host 端计数，记在 kind `host` 的进程样本里（stage 上 service `agent-host` 已有 10058 个窗口），按操作分别记 `calls`、`ambiguous`（第一次不明确）、`unavailable`（再试后仍不明确）；guard 端 `guard_op` 的 `effect_submit`、`effect_find` 样本。
   - **三项分别判断**：`effect_submit`（guard_op）、`effect_find`（guard_op）、`host`（Host 端计数）。任何一项在窗口内没有样本，就报“无数据”，不算通过。
   - 回复与 find：7 天内超过 2000 的比例 > 1%，或者 Host 端 `unavailable` > 0，报警告。
   - **`nexloop.enterprise.deliver` 单独报负责人**：只要出现一次 `unavailable`，就单独发给负责人一条，附 run_id（Host 样本按操作记 run_id 列表，最多 16 个，只有 id）。通知里写明两种可能：
     - (a) **单子已下，但客户没被告知**：guard 事务已经提交，intent 落库，企业照常收到单子；Run 已经失败，模型没有拿到结果，没有告诉客户单子已下，客户可能以为没下成；
     - (b) **没有单子**：guard 事务没有提交。

     通知请负责人到 intent 列表按 run_id 核对是哪一种。**只通知，不自动补单**：补单会把情况 (a) 变成两张单。
5. **产品行为不改**：超时后仍是“同载荷再试一次，仍不明确则 Run 失败”。下面两种做法都不采用：
   - 把 `runtime_effect_unavailable` 加入可恢复码，让模型再调一次：模型拿不到 intent_id，第二次 submit 的载荷可能不同，会触发 409 冲突或产生另一个意图，违背“先查询”。
   - 超时后给模型一个“checking”结果：Host 不知道 intent 是否存在，这个结果对客户来说可能是错的。

   回复已有兜底 Run。企业单的失败，由第 4 条单独发给负责人的通知暴露出来。
6. **激励工具纳入本单（C5）**：
   - 接上 `effect_timeout_ms`：`issueIncentive` 改用 `this.timeoutMs`，不再写死 2000。
   - 超时后采用同样的规则：先判为不明确，用**同一请求体只再发一次**，仍不明确才报 `incentive_unavailable`。
   - 前提已核对，**成立**：Run 入口 `authz.nexloop_run_incentive_command`（0145:237，之后没有迁移改过）的 intent 键由服务端定为 `'incentive-run:'||run_id||':'||incentive_offering_id`；`runtime.nexloop_incentive_accept`（0145:136-141）遇到同键，参数相同时返回已存的 intent（`replayed:true`），参数不同时报 NXB02（Host 收到 `incentive_payload_conflict`）。所以同一 Run、同一 offering 的重发，等价于查询或首次创建，和 0040 式的键效果相同。
   - 和 0040 的差别要写进 ADR-029c：键里没有 step 和 slot，同一 Run 对同一 offering 只能发一次；比较的参数不含 `notification_text`，同键、只有通知文字不同的重发按重放处理。这两点对“只再发一次、请求体相同”的规则没有影响。
7. **ADR**：第 1 条（按 p99 定值的规则）和第 2、3 条（观测口径）写成 ADR-029c（或新号），作为 ADR-029b §3a 末尾“以后另写 ADR”的落点。不改 ADR-022 §4 的默认值。

## 4. 测试计划

1. **确定性复现（每个 PR 都跑，不依赖负载）**：
   - Host 单元测试：假 guard 在固定延迟后才应答，延迟分别为时限 −200 ms、+200 ms，以及第一次慢、第二次快。分别断言：成功；两次请求、同一请求体、诊断行、`runtime_effect_unavailable`；第二次返回同一个 intent。
   - `effect-timeout-config.test.ts` 已经覆盖其中一部分（1 s、3 s 的实际限时和诊断行），这里补上“第二次返回同一个 intent，不新增”的断言。
2. **PG 回归**：guard 端注入延迟（测试用的 hook，只在 deterministic-test profile 生效）。第一次 submit 已经提交、Host 已经放弃时，断言：
   - 只有一个 intent；
   - Run 为 `runtime_failed`；
   - action worker 派发一次，消息算已答复，不起兜底 Run。

   另一组：第一次没有提交时，断言到期后起一个兜底 Run。

   **第三组（C1）**：第一次的事务还没提交、仍持有锁时，第二次请求到达（guard 注入延迟停在提交之前，第二次在这期间发出）。断言：
   - 第二次要么返回同一个 intent，要么仍然不明确；
   - 无论哪种，**绝不出现第二个 intent**，额度也**不重复预留**（预留行数和金额不变）。

   激励工具同样补三组：已落库、未落库、第一次仍持锁。断言 `incentive_issues` 只有一行、预算只预留一次。

   不用 `taskpolicy` 或 CPU 抢占来制造负载：这样造出的负载不稳定。
3. **高负载回归（不进每个 PR，挂在 NX-031 矩阵上）**：
   - 在负载矩阵里加一格“效应路径”：按部署并发跑真实 Pi 的回复 Run，从 `process_samples.jsonl` 的 `guard_op` 直方图补算 `effect_submit` 的 p99，再加上新的 Host 端直方图。
   - 门槛：p99 × 1.5 不超过部署时限。
   - 这是度量门禁，在 CI 主机按需或每晚跑，不放进全并行阶段。
4. **HEAVY_TESTS 里的用例能不能挪回全并行阶段**：**暂时不能**。
   - 全并行阶段的负载每轮不同，这个用例只给出“通过或失败”，不给分位数。挪回来只会重现偶发，证明不了任何事。
   - 挪回的条件：第 2 条的 Host 端直方图上线后，在 CI 主机上全并行跑 20 轮，这个用例所在 worker 的 `effect_submit` 最大值 < 0.8 × 时限，并且 20 轮全部通过。达到之前，它留在低并行阶段：时限和断言不变（ci-flake-audit 候选 1 的建议）。
   - 不在测试配置里把 `effect_timeout_ms` 调大：那会放宽被测的时限。

### 验收

1. §4 第 1、2 条的用例，包括 C1 的“第一次仍持锁”这一组和激励工具的三组，全部通过；变异检查：去掉“只再发一次”的次数限制，或者让第二次换一个请求体，至少有一个用例失败。
2. 告警三项（`effect_submit`、`effect_find`、`host`）各自的“无数据”反向用例：空样本报“无数据”，不报通过。enterprise.deliver 出现一次 unavailable，就生成一条带 run_id 的负责人通知，并且不产生任何新 intent。
3. **兜底 Run 预算**：时限取上限 5000 时，一次工具调用最多 2 × 5000 ms = 10 s，仍在兜底 Run 的 `active_timeout_seconds`（v1 为 60 s）之内。并且回复保障策略的装载检查 `start_after_seconds + 兜底 Run 最长时长 < reply_window_seconds` 不受影响（测试：用上限时限装载 v1 策略，仍被接受）。
4. 定值规则按 C4：样本不足时证据标“未测”，用估算时证据标“估算”。

## 5. 待定与遗留

1. **stage 只读结果**（调度员执行 §2 的 SQL，最近 7 天，查询日 2026-11-02）：
   - `effect_submit`：calls 25，p50 ≈ 1102 ms，p95 ≈ 1334 ms，p99 ≈ 1334 ms；
   - `effect_find`：0 行，无数据；
   - kind `host`（service `agent-host`）：10058 个样本窗口，可作为 §3 第 4 条的数据源；
   - `guard_op` 的其他 service：inspect 72、model 43、model_result 26、start 47、tool 38、outcome 1。

   按 C4，样本不足（25 < 1000），**不定值，保持 2000，标“未测”**。

   需要注意：在这样低的负载下，guard 端的 p50 已经约 1.1 s，离 2 s 的余量不大；加上 Host 到 guard 的时间以后，余量更小。负载上来时，超时会先在这里出现。
2. guard 样本的 `timeouts` 口径（常量 2000）在部署配置了别的时限后会失真：见 §3 第 3 条。

## 6. 实现记录（2026-11-02，ADR-029c）

按设计 v2 实现。与设计措辞不同、或者按实测改了断言的地方：

1. **Host 端样本分两种 kind**：合计计数 `effect_calls`、`effect_ambiguous`、`effect_unavailable` 进 kind `host`，这就是 C3 说的“kind=host 的进程样本”。按操作的计数和直方图进新 kind `host_op`（service 为操作名），因为 kind `host` 每个窗口只有一条，没有按操作区分的维度。
2. **enterprise 通知的 run_id 来自失败 Run 的任务结果**（`runtime.jobs.result` 的 `failure_tool` 和 `failure_detail`），不来自 Host 样本：进程样本的 SQL 只接受数字，不能放 run_id。
3. **超过 2 s 的计数用 guard 已有的 `timeouts`**（≥ 2000 ms）。§3 第 3 条里“0.8 × 部署时限”的第二档没有做：guard 拿不到 Host 的配置。Host 端的直方图（`host_op` 的 `h_<i>`）可以按任意阈值离线计算。
4. **第一次仍持锁时（C1）的实测结果**：
   - 效应路径：重发在 Run 的 advisory 锁上等待，第一次提交后拿到同一个 intent；
   - 激励：重发以 `unique_violation` 失败（L6 的预判），guard 回 403 `incentive_unavailable`，Host 不再发。

   两种情况都只有一条 intent。测试按实测结果断言。
5. 激励工具：503、连接错误、超时都判为不明确，只再发一次；冲突立即结束。
6. **L6 条件 2 发现的已有缺陷**：用真实的 enterprise 工具跑一次失败 Run（`apps/agent-host/test/enterprise-unavailable-failure.test.ts`），发现 `failure_detail` 记成的是 `tool_error`，不是 `runtime_effect_unavailable`。原因是 pi-durable 把工具抛出的错误包装成 `<harness>\n[error] <code>\n</harness>`，Host 只认整段文字就是错误码的情况。这样一来，0405 的 enterprise 告警永远不会触发。现在 `pi-runtime-adapter.ts` 的 `toolCode` 也会识别这种包装。另外两种情况要分清：
   - 工具抛出的不是带错误码的 `RuntimeError`（例如普通的 `Error`）时，`pi-runtime-adapter.ts` 里包装工具的那一段，会在 pi-durable 包装之前把它换成 `RuntimeError('runtime_tool_unavailable')`。所以记录里是 `runtime_tool_unavailable`，这和 `toolCode` 无关；
   - `tool_error` 只在 `toolCode` 从工具结果的文字里找不到任何错误码时出现，例如返回的 isError 结果不是 `{code}` JSON。

   两种情况都不会带出任何文字。三方用的字面值由 `tests/test_effect_request_metrics_pg.py` 钉住。部署单：`docs/release/run-nx064-effect-request-metrics.md`。
