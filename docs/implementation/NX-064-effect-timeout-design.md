# NX-064 设计：效应请求时限（Host 2 s）在高负载下超时的产品处理与按 p99 定值

- 状态：**设计稿，未实现**。执行人：L4。判定人：L6（待审）。基于 main 2bad63f9。
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

激励工具（`runtime-effect-tools.ts:372,390`）写死 `2000`，走自己的端点，没有接 `effect_timeout_ms`。它不在 NX-064 的用例里，但属于同一类风险，列为遗留。

### 1.2 超时后的路径（`runtime-effect-tools.ts:225-240, 283`）

1. 第一次请求超过时限：`request.destroy()` 触发 `error`，记为 `runtime_effect_ambiguous`（结果不明确）。
2. 如果 Run 的 `not_after` 还晚于“现在 + 时限”，用**完全相同的请求体**再发一次（ADR-028）。guard 按 0040 的唯一键（Run 的 consumer、goal、step、slot）推导意图：载荷相同，返回的就是同一个 intent，不会再预留额度，也不会产生第二个效应；载荷不同则冲突（409）。所以这一次重发在语义上是**查询**，不是盲目重试。
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
| 结论 | 不单独采用 | **不采用** | **推荐** |

### 推荐：C，具体内容

1. **定值规则**（写入运行单，不进代码）：
   - `effect_timeout_ms = clamp(ceil(p99_host × 1.5 / 100) × 100, 2000, 5000)`。
   - `p99_host` 取 stage 7 天里 Host 端的 p99。没有 Host 端数据时，用 guard 端 p99 加 300 ms 估算 TLS 和排队时间。
   - 结果低于 2000 时不收紧，保持 2000（ADR-022 的下限语义）；高于 5000 时**不**提高上限，转为性能问题（ADR-029b 的信封预刷新或 guard 拓扑），另行处理。
2. **补 Host 端时延直方图**（小改动）：效应客户端在每次请求结束时（无论成功还是失败）按操作记一条，用与 AT-060 相同的对数分桶，进 Host 的进程样本（kind `host`）。这样 p99 才是 Host 实际看到的时间，不只是 guard 处理时间。只有数字。
3. **观测口径对齐**：guard 样本的 `timeouts` 现在按常量 2000 计数（`observability.py:30`）。效应操作改为按两档计数：超过 2000，以及超过一个告警阈值（例如 0.8 × 部署时限，由告警规则给值）。guard 端拿不到 Host 的配置，不读它。
4. **告警**：新增一条规则，7 天内 `effect_submit` 中超过 2000 的比例 > 1%，或者 Host 诊断行 `runtime_effect_unavailable` 的计数 > 0。规则按“未配置不得静默”：没有 `guard_op` 样本时报“无数据”，不报通过。
5. **产品行为不改**：超时后仍是“同载荷再试一次，仍不明确则 Run 失败”。下面两种做法都不采用：
   - 把 `runtime_effect_unavailable` 加入可恢复码，让模型再调一次：模型拿不到 intent_id，第二次 submit 的载荷可能不同，会触发 409 冲突或产生另一个意图，违背“先查询”。
   - 超时后给模型一个“checking”结果：Host 不知道 intent 是否存在，这个结果对客户来说可能是错的。

   回复已有兜底 Run。企业单的失败，由第 4 条的告警和 Run 失败诊断暴露给负责人。
6. **ADR**：第 1 条（按 p99 定值的规则）和第 2、3 条（观测口径）写成 ADR-029c（或新号），作为 ADR-029b §3a 末尾“以后另写 ADR”的落点。不改 ADR-022 §4 的默认值。

## 4. 测试计划

1. **确定性复现（每个 PR 都跑，不依赖负载）**：
   - Host 单元测试：假 guard 在固定延迟后才应答，延迟分别为时限 −200 ms、+200 ms，以及第一次慢、第二次快。分别断言：成功；两次请求、同一请求体、诊断行、`runtime_effect_unavailable`；第二次返回同一个 intent。
   - `effect-timeout-config.test.ts` 已经覆盖其中一部分（1 s、3 s 的实际限时和诊断行），这里补上“第二次返回同一个 intent，不新增”的断言。
2. **PG 回归**：guard 端注入延迟（测试用的 hook，只在 deterministic-test profile 生效）。第一次 submit 已经提交、Host 已经放弃时，断言：
   - 只有一个 intent；
   - Run 为 `runtime_failed`；
   - action worker 派发一次，消息算已答复，不起兜底 Run。

   另一组：第一次没有提交时，断言到期后起一个兜底 Run。用 `taskpolicy` 或 CPU 抢占来制造负载不稳定，所以不用。
3. **高负载回归（不进每个 PR，挂在 NX-031 矩阵上）**：
   - 在负载矩阵里加一格“效应路径”：按部署并发跑真实 Pi 的回复 Run，从 `process_samples.jsonl` 的 `guard_op` 直方图补算 `effect_submit` 的 p99，再加上新的 Host 端直方图。
   - 门槛：p99 × 1.5 不超过部署时限。
   - 这是度量门禁，在 CI 主机按需或每晚跑，不放进全并行阶段。
4. **HEAVY_TESTS 里的用例能不能挪回全并行阶段**：**暂时不能**。
   - 全并行阶段的负载每轮不同，这个用例只给出“通过或失败”，不给分位数。挪回来只会重现偶发，证明不了任何事。
   - 挪回的条件：第 2 条的 Host 端直方图上线后，在 CI 主机上全并行跑 20 轮，这个用例所在 worker 的 `effect_submit` 最大值 < 0.8 × 时限，并且 20 轮全部通过。达到之前，它留在低并行阶段：时限和断言不变（ci-flake-audit 候选 1 的建议）。
   - 不在测试配置里把 `effect_timeout_ms` 调大：那会放宽被测的时限。

## 5. 待定与遗留

1. stage 的实际分布还没有测：§2 的 SQL 由调度员（执行人）只读跑一次，结果写回本文，然后再定 §3 第 1 条的具体值。
2. 激励工具的 2 s 写死在 `runtime-effect-tools.ts:372,390`，没有接 `effect_timeout_ms`：建议在实现时一并接入，或者写明它不随配置变化的理由。
3. 兜底 Run 的预算是 `active_timeout_seconds` 60 s。时限提高到 5000 时，一次工具调用最多 10 s，仍在预算之内；实现时要核对 `start_after_seconds + 兜底 Run 最长时长 < reply_window_seconds` 的装载检查不受影响。
4. guard 样本的 `timeouts` 口径（常量 2000）在部署配置了别的时限后会失真：见 §3 第 3 条。
