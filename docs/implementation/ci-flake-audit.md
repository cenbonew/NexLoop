# CI 偶发失败审计（2026-10-28，基于 d60a833f）

范围：只读代码与 git 历史，不连主机、不改测试。输入是调度员从 CI 主机汇总的最近 60 轮失败，已去掉 /tmp 配额耗尽、PG 启动失败造成的条目。
已加固项不再重复：guard transport 连接超时、runtime_worker CLI 退出超时（L2），审批过期 1 微秒（L3，产品问题）。

执行人：L5。判定人：L6（待审）。

## 一、四个候选

| # | 用例 | 定性 | 根因 | 建议 | 改动量 |
|---|---|---|---|---|---|
| 1 | `test_context_v6_relationships_pg.py::test_v6_carries_v4_relationships_with_hypotheses_as_evidence_only` | 负载偶发（老偶发，未根除） | 固定时限：Host 效应请求 2 s（ADR-022 §4），ADR-028 再试一次后仍超时 → `runtime_tool_error` → `runtime_failed` | 放入低并行阶段，并输出耗时诊断 | 约 1 行（lib.sh）+ 可选 3 行 |
| 2 | `test_tenant_constants.py::test_sql_tenant_positions_name_only_synthetic_tenants` | 真问题，已修 | 某批代码在测试里写了未登记的租户字面量 | 无需加固 | 0 |
| 3 | `test_work_feeds_pg.py::test_other_tenant_and_world_never_see_or_drain_the_instance` | 真问题，已修 | 测试仍用旧世界名 `'simulation'`，被新加的世界检查拒绝 | 无需加固 | 0 |
| 4 | `test_runtime_pg_bridge.py::test_actual_pg_lease_expiry_stops_pi_tool_dispatch[1]` | 负载偶发 | 固定 1 s 租约从 fixture 认领时起算，Pi 启动在负载下超过 1 s；另外 `receive` 用 Python 时钟睡 1.1 s，与 PG 时钟是两套 | 改成按条件等待：租约期覆盖启动时间，再等 PG 判定到期 | 约 15 行 |

### 1. v6 relationships（偶发，固定 2 s 效应时限）

- **历史**：NX-025 实施记录和 ADR-029b §1 都登记过这个偶发。ADR-029b 在本机用 `taskpolicy -b` 降速复现过 9/9：guard 处理效应 submit 用时 2.49–4.18 s，超过 Host 的 2 s 效应时限。
- **已经落地的修复**：ADR-029b 让效应请求复用 Run 缓存里的信封，代码在 d60a833f 中（`EFFECT_REUSE_KINDS`、`effect_timeout_ms`）。降速条件下 submit 降到 1.20–1.43 s。
  - 剩余风险 1：ADR-029b §2 自己记录过，信封剩余有效期不足 5 s 时会重建，这时 find 偶尔要 2.0–2.2 s。
  - 剩余风险 2：CI 全并行时负载高于本机降速条件。
- **为什么这个用例没有被兜住**：同类的两个实际 Pi v6 用例已经在 `scripts/ci/stages/lib.sh` 的 `HEAVY_TESTS` 里（`test_context_v6_pg` 和 `test_role_context_v6_pg` 的 `…records_every_model_call`），本用例不在，所以仍在全并行阶段运行。它也走同一个 `RuntimeDispatcher(total_timeout=45, request_timeout=10)` 和 2 s 效应时限。
- **建议（不放松语义）**：
  1. 把本用例加进 `HEAVY_TESTS`。2 s 时限和断言都不变，与同类处理方式一致。（1 行）
  2. 可选：失败消息里带上 Host 已有的诊断行（stderr 中 `runtime_effect_unavailable` 的 `elapsed_ms`），让下次失败能直接区分是超时还是真错误。（约 3 行）
  - 不建议在测试配置里把 `effect_timeout_ms` 调到 5000：这会放宽被测的时限。
- **产品侧观察（不在本次范围）**：ADR-029b §3a 已说明生产高负载下同样可能超时，按 p99 定时限要另写 ADR。

### 2. tenant constants（真问题，某批代码，已修）

- 这是纯静态扫描，没有时序依赖，不可能是偶发。
- e4d1e48a（NX-056 follow-up，10-25 13:04）在测试的 SQL 里写了 `set_config('eios.tenant_id','00000000-0000-4000-8000-0000000000ff',true)`。这个值不是登记过的合成租户，所以断言 `found <= known` 失败，断言消息里正好就是这个 uuid。
- 2a59998e（10-25 17:55）改为使用 `TENANT_OTHER`，d60a833f 已包含这个修复；当前 `git grep 0000000000ff` 没有结果。
- 两次失败应当都来自含 e4d1e48a、但不含 2a59998e 的批次。请调度员在 CI 主机上用失败轮次的提交核对一下（`git merge-base --is-ancestor 2a59998e <seq 的 head>` 应为假）。
- 加固：无。这个检查本身就是想要的门禁。

### 3. work feeds（真问题，某批代码，已修）

- 在 `authz.nexloop_service_credentials` 上，凡是可能产生 CheckViolation 的约束都是确定性的：
  - 0004 的 binding/tenant 检查；
  - 0165 的 real_alone；
  - 0259 的 `nexloop_worlds_valid`，它是 immutable 函数；
  - 0273/0284 的 sim_use。
  - 触发器 `nexloop_sim_world_credential_check` 抛的是 42501，不是 23514。
  - 因此不存在时钟或并发导致的路径。
- cd81d0d1（NX-041 S1，10-20）把世界名改成了 `sim:<uuid>`，`'simulation'` 不再是合法世界。本用例当时仍传 `world='simulation'`，`seed_multi_authority` 插入凭据时被 `nexloop_service_credentials_worlds_valid` 拒绝。
- 6b408716（10-21 11:53，"Batch 7 CI seq 46 fixes: … the work feed isolation test uses a sim world"）改用 `SIM_WORLD` 后修好。两次失败应当落在 10-20 到 10-21 之间的批次，请同样用失败轮次的 head 核对。
- 加固：无。

### 4. PG lease expiry bridge（偶发，固定短租约 + 两套时钟）

- 参数 `[1]` 经 `authority` fixture 的 `claim_task(lease_seconds=1)` 生效。租约在 fixture 认领时就开始计时，而用例接着还要依次完成以下步骤，才轮到它想测的事：
  - 启动 Node 进程（`pi-runtime-pg-process.mjs`）；
  - 获取 owner 锁；
  - 经 Unix socket 做 start 授权；
  - 等到 `accepted` 和 `generation_started` 两个事件；
  - 之后才 `time.sleep(1.1)`，再测"过期后 tool 被拒"。
- 负载下，Node 冷启动加 start 授权超过 1 s 时，租约在 start 授权之前就已到期。start 被拒，`accepted` 永远不会到来，`receive()` 等满 15 s。
- `_queue.Empty` 的来源：`receive()` 内部的 `events.get(timeout=…)` 在截止时抛的是 `queue.Empty`，不是它自己的 `AssertionError('actual Pi event unavailable')`，所以失败消息里也看不出卡在哪个事件。
- 另一个隐患：`sleep(1.1)` 用的是 Python 的时钟，而过期判定用 PG 的 `clock_timestamp()`，只隔 0.1 s。不过睡眠只会更长，这个方向是安全的；主要风险在前一段。
- **建议（不放松语义，仍然测"真实过期后被拒"）**：
  1. 租约从启动开始就要覆盖启动时间：参数改为一个足够长的值，例如 5 s，至少要大于 `receive` 的单步上界；或者在 `generation_started` 之后再把租约缩短到 1 s。后一种需要一个"缩短租约"的入口，现在没有，所以不推荐。
  2. 到期用条件等待代替 `sleep(1.1)`：轮询 PG，直到 `lease_expires_at <= clock_timestamp()`（同一个时钟、同一个判定者），上界 15 s；然后再发 release。
  3. `receive()` 捕获 `queue.Empty`，改为抛出带事件名和已收到事件列表的 `AssertionError`。这只改诊断，不改语义。
- 改动量：测试文件内约 15 行，不改产品代码。

## 二、同类隐患扫描（tests/，d60a833f）

扫描方法：
- 用正则加小脚本扫 `tests/**/*.py`；
- 逐条读了高风险命中；
- 低风险项只计数，没有逐条读。

### A. 模块级或收集时求值的时间（`NOW = datetime.now(...)` 等）

收集到运行之间可能隔很久：全量 CI 一轮约 1.5 小时，而且 `-n` 下各 worker 收集的时刻不同。用 `NOW + 小窗口` 去和数据库的 `now()` 比较，就会偶发。

| 风险 | 位置 | 说明 | 建议 |
|---|---|---|---|
| 低（P2 实测后下调） | `tests/test_s6_stage_evolution_pg.py:98` | 窗口 `until = NOW + 1 min`。原判断是收集后超过 1 分钟运行就会出窗。l5/flake-p2 用"运行晚于收集 2 小时"实测，旧代码仍然通过：窗口和全部合成数据都相对同一个 `NOW`，阶段脚本并不拿窗口和数据库的 now() 比较。只有预算期 `NOW + 29 天` 这类条件，要在收集后约 29 天才会失效 | 在用例内求值：`now = datetime.now(UTC)`（或取 PG 的 `select now()`）；约 3 行 |
| 高 | `tests/test_goal_controls.py:177` | 注释写明 `NOW - 5 min` 应在 1 h 成熟窗口内。收集后超过 55 分钟才运行时，就落到窗口外 | 同上，用例内取时间；约 2 行 |
| 中 | `tests/test_nx037_tenant_init_pg.py:28,88,175` | `credential_expires_at = NOW + 2h`，`PERIOD` 从 `NOW - 1h` 开始。以一轮 1.5 小时计尚有余量，但余量不大 | 改为用例内或 fixture 内求值；约 4 行 |
| 中 | `tests/test_nx043_pooled_pg.py:17`、`test_nx043_report_action_pg.py:21`、`test_nx043_reports_pg.py:20` | `TODAY` 是收集时的 UTC 零点。收集在 UTC 零点之前、运行在零点之后（即北京时间 08:00 前后）时，"整 UTC 日"的计算会错一天 | fixture 内求值；每个文件约 2 行 |
| 低（需确认） | `test_commercial_metrics_pg.py:9`、`test_evolution_e4_pg.py:26`、`test_budget_approval_pg.py:24`、`test_stage_tenant_configure_pg.py:22`、`test_nx022_dispatch_e2e.py:28`、`test_file_backup_pg.py:22`、`test_data_health_pg.py:24`、`test_retention_pg.py:31` | 多为"若干小时或天以前"这种宽窗口，或是只用来写入的时间戳。commercial_metrics 用的是 `NOW - 1h / -3h`，如果它所比较的窗口是"最近 N 小时"，就和上面属于同一类 | 逐个确认；统一做法是用例内求值 |
| 高 | `tests/test_nx043_budget_drill_pg.py:68` | 参数化在收集时求值 `soon(120)`。该用例要验证"超过 1 h 被拒"，但 `ends_at` 是收集时算出的固定时刻，随时间推移离现在越来越近。在收集后约 60 分钟以内运行时仍大于 1 h；超过约 60 分钟，就会变成"1 h 内"而被接受，断言失败 | 改为在用例内计算（参数只传分钟数）；约 2 行 |

另：本次没有发现参数化里直接调用 `time.time()` 的写法。

### B. 固定短租约，且租约期内还有其他工作（与第 4 项同类）

`lease_seconds=1` 之后紧接着 `sleep(1.1)` 去测"到期"，这个方向是安全的。危险的是认领之后、预期租约仍然有效的那一步之前，夹着其他工作：

| 风险 | 位置 | 说明 |
|---|---|---|
| 中 | `tests/test_backend_queue.py:54` | `claim_task(lease_seconds=1)` 之后先做了一次断言，再 `renew_task`。如果两步之间超过 1 s，renew 会因租约已失而失败 |
| 中 | `tests/test_runtime_dispatch.py:61,72,91,156` | 1–2 s 租约，后面接实际派发步骤。需要逐条确认中间是否经过 Host 或 Pi |
| 中 | `tests/test_runtime_effect_bridge.py:65` | `lease_seconds=1`，再加上人为 `pg_sleep(2)` 触发器，用来测不可用。需确认期望值不依赖"2 s 内一定先到期或一定未到期" |
| 低 | `tests/test_durable_queue.py:89,113,121`、`test_postgres_artifacts.py:39,59,122,173,322`、`test_queue_commit_lease.py:23,34,39,41`、`test_queue_process_recovery.py:27` | 多为"认领后立刻测到期或抢占"，方向安全，但都依赖 Python 睡眠与 PG 时钟的 0.1 s 余量 |

统一建议：
- 租约期应覆盖到期之前要做的全部工作，可以加大租约，或者在 renew 之前不夹别的步骤；
- 需要"已到期"时，按 PG 判定做条件等待（等到 `lease_expires_at <= clock_timestamp()`，上界 15 s），不用固定睡眠。

### C. `subprocess.wait(timeout=…)` 小于最坏上界

只命中 3 处，都是 5 s：
- `test_effect_provider_fixture.py:71,83`：都在 kill 之后，SIGKILL 之后 5 s 足够；
- `test_queue_permit_expiry.py:67`：这是 `threading.Event` 的 wait，不是子进程；5 s 内应完成签名，负载下偏紧，可考虑放宽到 15 s，与 `receive` 一致。

d60a833f 已经修好 `runtime_worker` CLI 的退出超时，本扫描没有发现小于 5 s 的子进程等待。

### D. 固定短睡眠

`time.sleep(≤1.1 s)` 共 194 处，没有逐条读。大部分是"等租约到期"（B 类，方向安全）或"给后台线程一点时间再检查没有发生某事"，例如 `test_runtime_pg_bridge.py:126` 的 `sleep(.1)` 之后断言没有 `tool_executed`：
- 后者在负载下只会漏报（假通过），不会造成假失败；
- 但它削弱了检测能力，属于质量问题，不是偶发来源。

## 三、建议优先级（供调度员分派）

| 优先级 | 项 | 改动量 | 说明 |
|---|---|---|---|
| P1 | 第 4 项 lease expiry：条件等待，`receive` 加诊断 | 约 15 行 | 已在 CI 上出现 |
| P1 | 第 1 项 v6 relationships：加入 `HEAVY_TESTS`，可选诊断 | 1–4 行 | 已在 CI 上出现，属老偶发 |
| P2 | A 类高风险项 goal_controls；s6_stage_evolution 一并改为用例内求值（实测并非偶发来源） | 约 5 行 | goal_controls 尚未在 CI 出现，但运行晚于收集约 55 分钟后必然触发 |
| P2 | A 类 budget_drill 参数化 `soon(120)` | 约 2 行 | 运行晚于收集约 60 分钟后必然触发 |
| P3 | A 类中风险的 nx043 `TODAY`、nx037；B 类 test_backend_queue、test_runtime_dispatch 逐条确认 | 每处 2–6 行 | — |
| — | 第 2、3 项 | 无 | 已修的真问题，只需在 CI 主机核对失败轮次的 head |

## 四、局限

- 没有读 CI 主机上的原始日志。第 2、3 项的结论依据 git 历史和断言内容推断，需要用失败轮次的 head 核对。
- 第 1、4 项的根因依据代码与 ADR-029b 中的测量，本次没有在降速条件下重新复现。
- A、B 类的"低风险"项只做了扫描计数，没有逐条阅读。
