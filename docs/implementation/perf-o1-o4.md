# O1 请求级授权决策 memo 与 O4 Backend 锁

分支 `perf-o1-o4`，BASE `57031eea48d328e6f261c0de7fb9c8b8f5718bcb`（= dispatch/integration-s3f 合并 perf-o3-fact-cache）。无迁移，不改任何时限。背景见 `perf-authz-diagnosis.md` 与 `perf-o3-fact-cache.md`。

## O4：结论是无需再改锁
L1 的 4455521 已经把 Backend 全局 `RLock` 改为 `LifecycleLock`：请求共享且可重入，关闭独占并等待在途的 commit/fsync。授权计算已经不在互斥区内。

本基线实测（13 个用例，测量钩子包装 `LifecycleLock.__enter__` 并保留 `exclusive()`）：
- 请求锁等待：1917 次进入，合计 <1 ms（诊断时旧 RLock 下单次最高 1.3 s）；
- 连接池获取：33576 次，合计 0.35 s，最大 8.5 ms。

因此没有再改锁的实现，只补了 O1 与生命周期锁交互的负向测试（关闭期间的请求，见下）。按约定保留“请求共享、关闭独占”的语义。

## O1：实现
- `nexloop_eios/authorization.py`：
  - `authority_request_scope()`：基于 contextvars，每个线程/请求一个 memo；嵌套调用复用外层，最外层退出即丢弃。
  - `resolve_authority(pool, session, query, entries)`：可直接替换 `F.AuthorizationFactsResolver(PostgresAuthorityProvider(pool,session,entries)).resolve(query)`。没有 scope 时行为与原来完全相同。
- **memo 键**：会话类型、token digest、directory hash（覆盖 tenant authority_revision 与凭据行）、world，以及去掉 request_id/trace_id 后的完整查询 JSON（主体、凭据、租户、requested scopes、调用应用 digest、Agent invocation、Run run_id/audience、目标资源类型/ID/操作）。条目最长复用 5 s。
- 命中时返回同一个已 resolve 的 context，并把当次的事实 `record_hash` 条目**深拷贝**回调用方的 entries。签名证明照常携带这些 hash，由 SQL 提交尾按当前事实复核。解析异常不进 memo。
- `backend.py`：`_invoke`、`_invoke_browser`、`_invoke_review` 的入口在原 `with self._lock` 处加上 `authority_request_scope()`。
- 热点调用点改用 `resolve_authority`，每处一行：`object_reads`、`runtime_activation`、`action_definitions`、`durable_queue`、`effect_execution`、`effect_intents`、`context_artifacts`、`postgres_action_claims`、`run_credentials`。其余调用点（含 L1、L3 的 claim/candidate/role 等文件）未改。

## 正确性测试（`tests/test_authority_request_memo.py`，10 passed）
- scope 外不 memo；scope 内同一查询只 resolve 一次；entries 深拷贝，调用方修改不污染 memo；嵌套 scope 复用；新请求从空 memo 开始。
- **撤权**：下一请求生效。旧会话因 directory 变化被拒，重新认证的会话也被拒。
- **grant 变化**（execute→read）：下一请求被拒。
- **请求内**：先预热 memo 再撤权，同一请求里的受治理 create 被 SQL 提交尾拒绝，没有写入任何对象。
- **跨主体、跨 world、跨目标、跨操作**：都会重新 resolve，不命中。
- **并发两个请求**（两线程）：memo 字典互相独立、互不可见。
- **关闭期间的请求**：在途请求持有共享锁时，`_shutdown` 一直等到请求结束；关闭后的请求得到 `BackendClosed`，memo 不残留。

## 回归
`.ci-results/o1o4-regression.xml`：28 个文件 **331 passed**，713 s，覆盖以下各组：
- memo 与缓存；
- bootstrap、backend、postgres_authority；
- runtime：activation、authority、dispatch、worker、guard transport、host admission；
- 队列：queue、commit lease；
- effect：intents、dispatch、execution sql、runtime bridge；
- 对象与 Action：object edits、action definitions、action claims、run credentials；
- context pack、role tail / mapping / post accept；
- offering runtime、browser business、goal controls。

## 前后对比（本机 M4 Pro，串行，带钩子，单轮）

命令：

```bash
scripts/perf/o1_o4_set.sh o1o4-before
```

```bash
scripts/perf/o1_o4_set.sh o1o4-after
```

```bash
uv run --frozen python scripts/perf/compare.py scripts/perf/out/o1o4-before scripts/perf/out/o1o4-after
```

before 在 O1 提交前的同一基线上运行（含 O3 与 L1 锁），after 在 O1 提交后运行。

### 受 2 s 时限约束的请求（ms）

| 请求 | 每请求决策数 | p50 | p95 | max |
|---|---|---|---|---|
| authorize_runtime_activation | 28.8→25.9 | 299→281 | 1050→**818** | 2095→**1620** |
| runtime_effect_tool | 53.9→27.8 | 763→**567** | 1583→**1138** | 1901→**1548** |
| prepare_message_context | 81.6→6.6 | 903→**492** | 1726→**943** | 1849→**943** |

### 越限所需慢化系数 k（2000/max，越大越安全）
- authorize：1.23（诊断时 1.13，O1 前 0.95）
- effect_tool：1.29（诊断时 1.10）
- prepare_message_context：2.12（诊断时 1.01）

### 用例 wall time（before → after，全部 rc=0，除 v4 首行）

| 用例 | before | after |
|---|---|---|
| three_process_replay | 18.0 | 11.9 |
| context_host_export | 17.6 | 15.9 |
| context_host_recovery[None] | 25.2 | 22.6 |
| context_host_recovery[artifact_deleted] | 16.6 | 15.5 |
| two_pi_role_runs | 47.7 | 42.3 |
| runtime_effect_recovery | 12.1 | 11.9 |
| runtime_effect_tools | 6.1 | 5.8 |
| local_message_delivery_assembly | 17.5 | 16.4 |
| message_driven_delivery | 33.1 | 28.4 |
| message_relay::cli_same_message_actual_pi_effect | 19.4 | 16.3 |
| native_web_delivery | 33.2 | 28.0 |
| scope_denials::real_pi_distinct_tool_calls | 14.4 | 12.4 |
| relationship_context_v4[complete] | 52.5（**失败**） | 54.6（**通过**） |

v4 在 before 中失败的原因：同一进程中一次 `authorize_runtime_activation` 耗时 2095 ms，超过 Host 的 2 s 时限，导致 `runtime_failed`。其中服务端 `nexloop_runtime_activation_command` 占 925 ms，决策 72 次（去重后 49 次）。after 中这类请求最大为 1620 ms。

## 结论与剩余
- O1 + O3 + L1 的锁之后，effect_tool 与 authorize 的 max 降到 1.55–1.62 s，k≈1.23–1.29；prepare_message_context 已有约 2 倍余量。
- 按公开基准估算 sice 的 k≈2.3–2.6，前两类请求在 sice 上仍可能越限，B 类时限失败预计减少但不会清零。
- 剩下的工作量主要是**跨请求重复**的逐属性 READ 决策（authorize 每次约 25 个不同决策，O1 按约定不跨请求）和服务端 `runtime_activation_command`（单次最高约 0.9 s）。下一步属于 O2（按对象批量决策 + 批量事实加载，需要迁移与语义等价论证）和服务端 SQL 侧优化（`perf-authz-diagnosis.md` §5 的 O5）。
- 数据为单轮、带钩子（开销 1–7%）；sice 未实测。
