# 授权调用耗时诊断（NX-018 第 3 步与 CI B 类时限失败的依据）

只读诊断，分支 `perf-authz-diagnosis`，BASE `3ad40614e4b45db2db9a78e3881f35b65d8255f5`。产品代码与迁移未改动；所有计时钩子都在 `scripts/perf/` 的测量进程内以 monkeypatch 方式加载。测量机：Apple M4 Pro（12 核，24 GB），Homebrew PostgreSQL 18.4，Node 24，Python 3.12（uv frozen）。所有用例均为本机串行执行。

## 1. 结论（先看这里）

1. **主因是授权决策次数，不是某一条慢 SQL。**
   - 单次授权决策（EIOS fact resolver + 12 次事实往返 + 决策）在 M4 Pro 上的 p50 是 **4.1 ms**。
   - 但 Pi 每次工具 submit（`runtime_effect_tool`）平均要做约 **90–122 次决策、1088–1464 次事实往返**；Role Run 下每次 `authorize_runtime_activation` 约 **29–42 次决策、352–504 次事实往返**。
   - 约 80% 的决策是**按属性逐个做的 READ 检查**：catalog envelope（ServiceOffering / ConsumerServiceOffering 的每个属性）约 37%，role envelope（RoleDefinition / ConsumerRoleLink 的每个属性）约 31%，PlanStep 约 9%。每次 inspect/model/submit 都重新算一遍，请求内不复用。
2. **尾延迟来自 Backend 进程内的全局请求锁。**
   - `Backend._invoke` 用一把 `RLock` 串行化同一个 Backend 的所有请求，授权计算也在锁内完成。
   - 两个 Pi Run 并发时，慢的 authorize 有约 **1.23–1.32 s** 花在等这把锁（另一 Run 的 effect_tool 请求持锁 1.7 s）。这正是 L1 看到的“两 Pi 闭环 503”的形态。
3. **离 2 s 时限的余量只有约 10%。**
   - 本机（带钩子）`runtime_effect_tool` 最大 **1.82 s**，`authorize_runtime_activation` 最大 **1.77 s**，`prepare_message_context` 最大 **1.99 s**。
   - 越过 2 s 的盈亏平衡慢化系数 k = 2000/max 只有 **1.01–1.14**。sice（Ryzen 7 5700U）的单线程性能按公开基准约为 M4 Pro 的 1/2.3–1/2.6（**估算，未在 sice 实测**），所以这些用例在 sice 上越限几乎是必然的。B 类的 7 个用例与此一致。
4. 服务端 SQL 是第三位：
   - `nexloop_runtime_activation_command` 每次 50–180 ms。
   - `nexloop_context_artifact_command` 最多 1.1 s（每次约 250 次 `assert_read_authority`）。
   - 每次事实加载都重新推导身份：单个用例中 `nexloop_root_identity` 被调用约 29 万次。
5. PG 内锁等待只在刻意制造三进程竞争的 `test_actual_three_process_replay_model_tool_do_not_invert_locks` 中显著（三次运行一致：CPU 268–292 / `Lock:transactionid` 176–188 / `Lock:advisory` 152–156 个 20 ms 采样）：
   - advisory 等待全部落在 `nexloop_runtime_activation_command`（message-route advisory 锁，154 次）；
   - transactionid 等待主要落在 `nexloop_context_artifact_command`（124 次，route/outbox 行 `FOR UPDATE`）。
   - 这是该用例要验证的锁序（不反转），属于预期串行化。
   - 其余 6 个 B 类用例中 PG 侧基本全是 CPU，锁等待 ≤3 次采样；两 Pi Role 用例的串行化发生在 Python 的 Backend 锁上。

6. 连接池获取（每次 <0.01 ms）、HMAC 签名、canonical 序列化、裸往返（`select 1` p50 0.036 ms）都可以忽略。

## 2. 测量方法

| 组件 | 文件 | 作用 |
|---|---|---|
| 计时钩子 | `scripts/perf/hooks/sitecustomize.py` | 只有 PYTHONPATH 含 `scripts/perf/hooks` 时加载，子进程同样加载（测试会把 PYTHONPATH 传给子进程）。包装 psycopg `Cursor.execute`（按被调 SQL 函数归类）、`ConnectionPool.getconn`、Backend 请求锁、EIOS `AuthorizationFactsResolver.resolve`（同时计数决策目标）、`decide/decide_resolved`、`_load`（每条事实）、`authenticate_service`、Action 定义读取、`govern_published_action`、claim 预留、RuntimeActivation `_proof/_signed`、`canonical_payload`、`hmac.new`、`Backend._invoke`（按 verb）。记录包含时间、自身时间、调用计数，以及每个顶层请求的分解，进程退出时写 `proc-<pid>.json`。 |
| PG 插件 | `scripts/perf/hooks/perf_plugin.py` | 在测试自身 bootstrap 后对一次性集群开启 `track_functions='pl'`，按 20 ms 采样 `pg_stat_activity`（`wait_event_type/wait_event`，并按语句归类）；`admin` fixture 结束（池已关闭、统计已落盘）后读取 `pg_stat_user_functions`。 |
| 运行器 | `scripts/perf/run_profile.sh <label> <node...>` | 串行运行，输出到被忽略的 `scripts/perf/out/<label>/`，最后生成 `summary.md`。 |
| 汇总 | `scripts/perf/summarize.py`、`scripts/perf/slowdown.py` | 生成分解表；按慢化系数 k 统计越过 2 s 的请求数。 |
| 微基准 | `scripts/perf/test_micro_authz.py` | 预热后重复执行单次受治理 create、单次决策、Action 定义读取、`select 1`、单条事实 SQL。 |

钩子开销：同一用例不带钩子对比，call 时间 43.73 s 对 44.3 s（两 Pi Role）、3.09 s 对 3.3 s（effect tools）、13.29 s 对 13.5 s（three_process），约 1–7%。下文毫秒数均为带钩子的值，略偏保守。

复现命令：

```bash
scripts/perf/run_profile.sh micro scripts/perf/test_micro_authz.py
```

```bash
scripts/perf/run_profile.sh b-class <B 类 7 个 node id>
```

```bash
uv run --frozen python scripts/perf/slowdown.py scripts/perf/out/b-class
```

前置条件：worktree 必须已构建 `apps/agent-host/dist` 和 vendor Pi（`pnpm install --frozen-lockfile && pnpm build:pi && pnpm --filter @nexloop/agent-host build`）。第一次批跑因缺 dist，6 个用例在 4–11 s 内以 `dist/main.js` 断言失败，与时限无关，已保留在 `scripts/perf/out/b-class-nodist`（不提交）。

## 3. 数据

### 3.1 单次受治理调用（微基准，M4 Pro，预热后）

| 调用 | n | p50 ms | p95 ms | max ms |
|---|---|---|---|---|
| 受治理 `Consumer.create`（govern + 决策 + 签名 + SQL 尾验） | 40 | 27.1 | 28.6 | 40.2 |
| 单次授权决策 `authorization_service().decide` | 80 | 4.1 | 4.8 | 15.8 |
| 已发布 Action 定义读取（含 1 次决策） | 80 | 6.3 | 7.1 | 19.8 |
| 单条事实 SQL `nexloop_load_authority_fact_snapshot` | 400 | 0.077 | 0.130 | 0.36 |
| 池获取 + `select 1` | 400 | 0.036 | 0.044 | 0.21 |

单次决策的构成（钩子自身时间均值）：
- resolver Python CPU 约 2.1 ms；
- 12 次事实往返：SQL 约 0.08 ms ×12 加 Python 模型校验约 0.07 ms ×12，合计约 1.8 ms；
- `decide_resolved` 约 0.5 ms。

受治理 create 平均含 24 次事实加载、2 次决策、2 次 Action 定义读取、8 次池获取。签名、序列化、池获取的合计 <0.1 ms。

### 3.2 B 类 7 个用例（本机串行，带钩子，全部 passed）

| 用例 | wall s | call s |
|---|---|---|
| test_context_artifacts::three_process_replay | 18.5 | 13.5 |
| test_context_host_export::bound_pack_pg_guard | 20.3 | 15.7 |
| test_context_host_recovery[None] | 28.5 | 23.9 |
| test_context_host_recovery[artifact_deleted] | 18.8 | 14.1 |
| test_role_pi_effect_checkpoint::two_pi_role_runs | 54.2 | 44.3 |
| test_runtime_effect_recovery::host_and_worker_kill | 14.0 | 10.2 |
| test_runtime_effect_tools::rebuilt_tool_call | 6.8 | 3.3 |

受 2 s guard/工具 HTTP 时限约束的请求（`runtime-host.ts` 与 `runtime-effect-tools.ts` 的 `AbortSignal.timeout(2000)` 和 `setTimeout(2000)`）的服务端耗时：

| 请求 | n | p50 ms | p95 ms | max ms | 主要构成（均值 ms） | 每请求调用数 |
|---|---|---|---|---|---|---|
| `authorize_runtime_activation`（B 类合计） | 216 | 258 | 930 | 1769 | Backend 锁等待 155、resolver 88、activation SQL 77、事实加载 37+26 | 事实往返约 181 |
| 同上（两 Pi Role 单独跑） | 105 | 780 | 1611 | 1749 | Backend 锁等待 297、resolver 122、activation SQL 55 | 决策约 29–42，事实往返 352–504 |
| `runtime_effect_tool`（submit/find） | 27 | 777 | 1752 | 1820 | resolver 259、activation SQL 170、事实加载 107+78、Backend 锁 101、intent SQL 74 | 决策约 90–122，事实往返 1088–1464 |
| `prepare_message_context` | 12 | 1059 | 1789 | 1986 | context_artifact SQL 452（最高 1105）、resolver 387 | 事实往返约 108 |

最慢的 4 次 authorize 中，Backend 锁等待占 1231–1315 ms，自身工作只有约 450 ms。

慢化系数估算（`slowdown.py`，假设 CPU 与锁等待同比放大）：

| 请求 | 本机 max ms | 盈亏平衡 k | k=1.5 越限数 | k=2 | k=2.5 | k=3 |
|---|---|---|---|---|---|---|
| prepare_message_context | 1986 | 1.01 | 4/12 | 8/12 | 12/12 | 12/12 |
| runtime_effect_tool | 1820 | 1.10 | 6/27 | 7/27 | 13/27 | 20/27 |
| authorize_runtime_activation | 1769 | 1.13 | 6/216 | 8/216 | 59/216 | 67/216 |
| assert_task_lease | 830 | 2.41 | 0 | 0 | 1 | 3 |

sice 的 Ryzen 7 5700U（Zen2，15 W，4.3 GHz 加速）对比 M4 Pro：Geekbench 6 单核约 1550–1650 对约 3900，**比值约 2.3–2.6（公开基准估算，未实测）**；CPython、pydantic-core、PL/pgSQL 都是单线程 CPU 型负载，比值相近。sice 在 CI 中还有 PG 与其它进程同机竞争，k 只会更大。按 k≈2.5，三类请求都会越限，与 B 类“sice 串行失败、Mac 通过”的现象一致。本机只有约 10% 余量，Mac 通过本身也不稳。

### 3.3 决策目标分布（两 Pi Role 加 effect tools，共 4708 次决策）

| 类别 | 占比 |
|---|---|
| property READ：ServiceOffering | 26.3% |
| property READ：ConsumerRoleLink | 13.4% |
| property READ：RoleDefinition | 13.4% |
| property READ：ConsumerServiceOffering | 10.9% |
| action EXECUTE：nexloop.service.request | 7.4% |
| property READ：PlanStep | 6.7% |
| action EXECUTE：NexLoop.queue.operations | 5.5% |
| object READ（PlanStep/RoleLink/RoleDef/Offering…） | 约 11% |
| 其余 | <5% |

### 3.4 PostgreSQL 侧（`track_functions=pl`，两 Pi Role 用例）

| 函数 | 调用 | total ms | self ms |
|---|---|---|---|
| `nexloop_load_authority_fact` | 206652 | 5446 | 2831 |
| `nexloop_root_identity` | 293986 | 2090 | 2090 |
| `nexloop_assert_read_authority` | 9705 | 6238 | 1793 |
| `nexloop_load_authority_fact_snapshot` | 206652 | 7141 | 1695 |
| `nexloop_service_identity` | 253691 | 3281 | 1104 |
| `nexloop_role_run_validate_before_role_ttl_v0063` | 201 | 5237 | 79 |
| `nexloop_role_strict_read` | 651 | 5463 | 55 |

活跃后端采样：CPU 556 / ClientRead 51，无 Lock 等待。

three_process 用例（按语句归属的 20 ms 采样）：

| 语句 | Lock:advisory | Lock:transactionid | CPU |
|---|---|---|---|
| `nexloop_runtime_activation_command` | 154 | 53 | 93 |
| `nexloop_context_artifact_command` | 0 | 124 | 75 |
| `nexloop_reserve_local_artifact` | 0 | 11 | 0 |

在同一用例的早先一次运行中，`assert_effect_plan` 被调用 187 次，平均自身 22.5 ms，其中包含 ledger/context `FOR UPDATE` 的等待。

## 4. 瓶颈排序

1. **每请求重复的逐属性 READ 决策**（role/catalog/PlanStep envelope）：占 effect_tool 时间的 60% 以上，占 authorize 自身工作的大部分。
2. **Backend 全局请求锁**：并发 Run 的尾延迟，单次最多 1.3 s。
3. **每次决策 12 次独立事实往返 + Python 模型重解析**：每次决策约 1.8 ms，乘以决策数。
4. **服务端 envelope/activation SQL**：`context_artifact_command`（约 250 次 assert_read_authority）、`runtime_activation_command`、每次事实加载重推身份（约 29 万次）。
5. **PG 行锁 / advisory 锁**：只读校验也取 `FOR UPDATE` 或 message-route advisory 锁；只在三进程竞争用例中显著，并且是被测的锁序设计。

## 5. 可选优化

| # | 方案 | 预期收益（本机） | 风险 | 触及 L1 正在改的文件 |
|---|---|---|---|---|
| O1 | 请求内决策记忆化：同一 session、directory_hash、target、operation 在一次 `_invoke` 内只决策一次；SQL 尾验仍按 record_hash 核对 | effect_tool 决策数预计降 2–3 倍（需实测重复率），约 −300~600 ms | 低：不跨请求，撤权仍在提交尾和下次请求生效 | 否（`authorization.py` 或新包装层），调用方 `runtime_activation.py` 是 |
| O2 | 每个对象一次决策覆盖整组属性（新增按资源集合批量的 resolver 适配 + SQL 批量事实加载 `nexloop_load_authority_facts(keys[])`） | 去掉约 80% 的独立决策和往返，effect_tool 预计降到 300–500 ms | 中：要证明与逐属性语义等价（属性级 grant/policy 差异），涉及 EIOS 授权契约；需要新迁移 | 是（role/catalog envelope：`role_runs.py`、`service_offerings.py`、`runtime_activation.py`） |
| O3 | 解析结果按 (kind, record_hash) 缓存，省去 `model_validate_json(json.dumps())` 与 snapshot digest 重算 | 每次加载约 −0.07 ms，effect_tool 约 −100~150 ms | 低：内容寻址，hash 变化即失效 | 否（`authorization.py`），但属于共享授权核心，需要调度员协调 |
| O4 | Backend 锁改为读写语义：请求共享、关闭独占；或按 Run/activation 分锁 | 并发 Run 尾延迟去掉约 1.2–1.3 s | 中：锁本来保护关闭期间的 commit/fsync，要保持关闭语义并通过 kill/reopen 系列用例 | 是（`backend.py`） |
| O5 | SQL 侧在事务内缓存身份（例如 `set_config` 事务级 GUC 或临时表），避免每次事实加载重推 `root_identity`；`context_artifact_command` 批量断言 | 服务端 CPU 约 −30~50%，context 最慢 1.1 s 段明显下降 | 中：SECURITY DEFINER 内缓存必须绑定 digest + 事务，不能跨事务 | 否（新迁移），但改授权核心函数要谨慎 |
| O6 | 只读 verb（inspect/model 的 authorize）不对 effect ledger/context 取 `FOR UPDATE`，改成 `FOR SHARE` 或提交尾校验 | 多进程竞争下的锁等待 | 中高：当前锁序是防反转设计（three_process 用例专测），改动需重新论证 | 是（runtime/effect 链路） |
| O7 | envelope 在 Run 签发时冻结签名，每次请求只校验 digest 与撤权 epoch | 最大：每请求的 envelope 决策近似 O(1) | 高：改变“每次请求重读当前授权”的不变量，撤权要靠 epoch/directory_hash 生效，需要 ADR | 是 |

建议顺序：先做 O1 + O3（低风险，不改契约），实测后再决定 O2 / O4。O6 / O7 需要 ADR 级讨论。

## 6. 2 s 时限本身是否合理

- 时限覆盖的是一次本地 loopback HTTPS 请求，同时包含 TLS 握手和服务端的完整授权工作。服务端工作量随租户 catalog 规模、Role 属性数、PlanStep 和并发 Run 数线性增长（逐属性决策 × 请求数），所以固定的 2 s 不是对 O(1) 操作的保护，而是对一个随数据增长的操作设硬上限。在 M4 Pro 上只剩约 10% 余量，在较慢主机上必然越限。**结论：在当前工作量下，2 s 不合理；但单纯调大会掩盖结构问题，不建议先这样做。**
- 时限仍有价值：它避免 Pi 工具调用无限挂起，并保证 Run 的主动预算可控。submit 超时后的 unknown 有 `find` 查询兜底（幂等 intent），语义上安全。
- 建议：
  1. 先按 §5 把每请求工作量降到与数据规模基本无关（O1–O3，必要时 O2）；
  2. 时限改为可配置并按部署实测：p99 × 安全系数，下限 2 s，并在 doctor/readiness 中报告实测 p99；
  3. 服务端做截止时间传播：guard 收到请求时带客户端剩余预算，服务端在预算耗尽前主动拒绝，返回明确的 `deadline_exceeded`，而不是让客户端在服务端可能已提交后断开；
  4. 把连接建立/TLS 与处理时间分开计时。
- 在 O1–O4 落地前，如需让 CI 稳定，可以只在 CI profile 中按“测得 p99 × 系数”放宽，并记录为已知偏差。是否这样做由调度员/负责人决定，本诊断不修改时限。

## 7. 限制

- sice 未实测（规则禁止连接）；慢化系数来自公开基准估算。
- 带钩子的数据包含约 1–7% 的开销；`Lock` 等待来自 20 ms 采样，是近似值。
- 只覆盖 B 类 7 例、一个单进程 runtime 用例组（`test_runtime_activation.py` 30 例）和微基准；C 类并行抖动未在 6 进程并行负载下复现。
- 决策重复率（请求内同 target 多次）未单独统计，O1 收益需在实施时实测。
