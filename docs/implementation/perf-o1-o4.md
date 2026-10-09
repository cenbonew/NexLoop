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

## O1 推广到 L2/L3 后台工作单元（分支 `perf-o1-extend`，BASE `ef0f591ace8a0426d7f481cdfcd6629b030b43ee` = dispatch/integration-s3h）

后台任务不经过 Backend 请求，因此把**一个工作单元**当作一个请求 scope。新增装饰器 `authority_request_scoped`：进入时建立 scope，退出（包括抛异常）时丢弃。改动清单（每处一行改为 `resolve_authority`，加一行装饰器）：

| 文件 | 调用点 | 工作单元入口（scope） |
|---|---|---|
| `claim_store.py` | 提取授权证明 | `ConversationClaimExtractor.extract` |
| `claim_extraction_jobs.py` | feed 授权证明 | `ClaimExtractionScheduler.run_once`、`ClaimExtractionWorker.run_once` |
| `claim_matching.py` | match 证明 | `ClaimMatcher.match_claim`、`ClaimMatcher.apply` |
| `candidate_merge.py` | 审核队列证明 | `CandidateGluer.process`（审核读取无重复，未加 scope） |
| `recall.py` | `EiosRecallAuthorizer.readable` 中的 decide | `OntologyRecall.recall` |

`outbound_messages.py`、`review_http.py` 没有直接的 resolve 调用点（经由已改的模块），未改动。未碰 L1 阶段 B 将改的 `runtime_activation.py`、`effect_intents.py`、`effect_execution.py`、`role_runs.py`、`service_offerings.py`、`context_artifacts.py`（本分支对这些文件无 diff）。

### 负向测试（`tests/test_authority_request_memo_jobs.py`，7 passed）
- 装饰的单元正常返回或抛异常后，memo 都被丢弃；两个单元的 memo 不是同一个对象。
- 召回：单元内每个决策只 resolve 一次，下一单元重新 resolve；撤销对象 READ 后，下一单元不再返回该对象。
- 召回：在**同一外层 scope**内，换一个缺少属性 READ 的主体，不会用到前一主体的 memo（命中被隐藏）；换 simulation world 只看到本 world 的对象。
- Claim 调度器：撤销 extract grant 后，下一单元抛 `ClaimExtractionDenied`；在同一外层 scope 内，只有队列权限的主体访问同一资源仍被拒。

### 回归
14 个文件 132 passed（186 s，`.ci-results/o1-extend-regression.xml`）：
- memo（请求级与后台单元）及 fact cache；
- bootstrap；
- claim extraction jobs / matching / store；
- candidate merge、merge configuration；
- recall；
- review workbench / http；
- outbound messages、claim contract。

### 前后对比（scripts/perf，7 个 L2/L3 测试文件，单轮）

| 工作单元 | n | 决策/单元 | p50 ms | p95 ms | max ms | 合计 s |
|---|---|---|---|---|---|---|
| extract_window | 33 | 41.7→23.5 | 340→191 | 1357→692 | 1382→704 | 13.10→7.85 |
| match_claim | 63 | 3.3→2.0 | 135→108 | 174→151 | 191→176 | 6.63→5.48 |
| recall | 84 | 13.4→10.2 | 95→76 | 125→109 | 136→136 | 6.41→5.51 |
| match_apply | 21 | 10.2→5.0 | 92→65 | 115→74 | 159→91 | 1.58→1.06 |
| claim_extract（worker） | 11 | 1.5→1.0 | 142→141 | 322→205 | 322→205 | 1.48→1.10 |
| glue_process | 16 | 3.9→1.1 | 27→14 | 279→189 | 281→198 | 0.91→0.58 |
| claim_schedule | 8 | 2.5→1.8 | 21→18 | 23→22 | 23→22 | 0.14→0.13 |
| review_pending / candidate | 5 / 3 | 不变 | 不变 | 不变 | 不变 | 不变 |

命令：

```bash
scripts/perf/run_profile.sh ext-before <7 个测试文件>
```

`ext-before` 在本次改动提交前运行，`ext-after` 在提交后运行。7 个文件两轮均全部通过，wall time 变化在 ±2.5 s 以内（这些文件的耗时主要是 fixture 与 PG 启动）。

## O2/O5 规划（等 L1 阶段 B 合入后再做）

纳入 L1 给出的设计约束：

1. **同 Source 身份类事实一次去重加载。**
   - subject、membership、actor、authentication、application、subject_authority、revision 这类事实，对同一 Source 会话在每次决策中都相同。
   - 在单个请求/单元内批量加载一次，同时按 (digest, directory_hash) 校验。
   - 实现方式是新增批量事实加载函数，与 O5 的“事务内身份推导缓存”合并，需要临时迁移。
2. **同请求复用证明，但不跨请求。**
   - 跨请求复用会破坏 L1 的“锁等待后最终期限”测试：证明必须在每个请求重新取得，锁等待后的提交尾按当时的期限判定。
   - O2 的“按对象一次决策覆盖整组属性”也只在请求内生效。
3. **证明有效期不延长。**
   - 复用的 context 保留首次 resolve 的 `trusted_now`，签名证明的 `expires_at = min(decision.expires_at, now+25s)` 不会因复用而后移。
   - O2 的批量决策取组内最早到期时间作为整体到期时间；任何一个属性的 grant/policy 到期都使整组失效。
4. **拆锁保留“关闭等在途提交”。**
   - 若 O2/O5 引入新的并发（例如批量加载的并行）或按 Run 分锁，仍须保留 LifecycleLock 的“请求共享、关闭独占、关闭等待在途 commit/fsync”语义，并通过现有 kill/reopen 系列用例。

**O2 语义等价论证要点（实施前需写成测试）：**
- 逐属性 READ 的结果等于“对象 READ ∧ 每个属性的 grant/scope/control/policy 各自成立”。
- 批量决策必须逐属性给出结果，不能用对象级结论替代属性级拒绝。
- 签名证明仍按属性列出各自的 `target_resource` 与事实 hash，SQL 侧逐属性复核的结构不变。

**O5 待办：**
- 事务内身份推导缓存：每次事实加载都重推 `nexloop_root_identity`，单个用例中约 29 万次。缓存必须绑定 digest 与事务。
- `context_artifact_command` 的批量 `assert_read_authority`。
- `runtime_activation_command` 单次最高约 0.9 s 的分解。

## O2a：请求内身份类事实只加载一次（分支 `perf-o2a`，BASE `aebbfa8ff8ea087ac6fe88fd0d0500b6cc7c4695` = dispatch/integration-s3m）

### 实现
只改 `authorization.py` 与 `browser_authorization.py`，无迁移。
- `PostgresAuthorityUnitOfWork._load` 拆为 `_fetch`（数据库读取，浏览器子类覆盖）和带 memo 的 `_load`。
- 在请求 scope 内，`IDENTITY_FACT_KINDS` = {subject, membership, actor, authentication, application, subject_authority, revision} 只读取一次。
- memo 键：会话类型、token digest、directory hash、world、kind、key、模型类；条目最长 5 s。
- grants、scope、controls、policies、resource_graph（以及 agent 系事实）每次决策照常读取。
- 命中时把同一 record_hash 写回 entries 和 `_loaded`，签名证明形状不变。
- 每个 unit of work 仍先核对 live identity / directory hash（未改），身份撤权使旧会话在下一次决策前即被拒。
- 身份事实的有效期仍由 EIOS resolver 在每次决策时以当时的 `trusted_now` 判定。

### 等价性与负向测试（`tests/test_identity_fact_memo.py`，10 passed）

| 文档 §5 要点 | 测试 |
|---|---|
| 判定不变 | `test_equivalence_outcome_proof_hashes_and_expiry`：允许 ×2、过期 grant、应用限制外、缺事实，共 5 个目标；无 scope 与 scope 内的 allowed / authoritative / expires_at / 证明 (kind, key, record_hash) 全部相等；scope 内每条身份事实只读一次，总读取数下降 |
| 事实逐字节相同 | `test_record_hash_from_memo_equals_fresh_read`：memo 命中的 record_hash 等于此刻直接调用 `nexloop_load_authority_fact_snapshot` 的结果 |
| 撤权 | `test_identity_revocation_denies_in_next_request[membership_suspended / subject_disabled]`：旧会话在下一请求被拒；重新认证后同样被拒 |
| 请求内撤权 | `test_revocation_inside_request_is_not_served_from_memo`：同一请求内撤销 membership 后，下一个目标的决策因 live directory 核对被拒 |
| 有效期 | `test_identity_expiry_is_evaluated_per_decision_with_memoized_fact`：membership 2 s 后到期；首个证明的 `expires_at` 不晚于到期时刻；到期后同一请求内的另一个目标被拒 |
| 跨主体 / world / 租户 | `test_memo_never_shared_across_principal_world_tenant[principal/world/tenant]`：同一 scope 内第二个会话的身份事实全部重新读取，且都属于它自己 |
| 跨请求与并发 | `test_memo_not_shared_across_requests_or_concurrent_requests`：下一请求重新读取；两线程并发请求的 memo 不是同一个对象 |

O1 的 `test_concurrent_requests_have_isolated_memos` 原来断言 memo 只有 1 条；现在 memo 中还有身份事实条目，因此改为只统计决策条目（语义不变）。

### 回归
- 批次 1：17 个文件 1 failed / 207 passed（失败即上面那条 O1 断言，修正后 10/10）。
- 批次 2：13 个文件 149 passed。
- 覆盖：memo / cache、bootstrap、backend、postgres_authority、browser business、runtime activation / authority / dispatch / worker / guard transport、durable queue、action definitions / claims、run credentials、object edits、goal controls、service grants、effect intents / dispatch / execution sql、role mapping / post accept、context pack、claim extraction / matching、recall、review workbench。

### L1 关注的两项（本机，只测不改）

| 测试 | before（aebbfa8 的两个文件） | after（O2a） |
|---|---|---|
| `tests/test_backend_lifecycle_capacity.py`（7 例） | 全部通过；最长 `eight_concurrent_requests_complete_on_pool_of_four` 5.35 s | 全部通过；该项 4.70 s |
| `test_role_policy_dispatch::test_effect_units_concurrent_distinct_submissions_stay_within_ceiling` | 通过，5.57 s | 通过，5.26 s |

这两项在本机前后都通过，只在测试机全量中失败，与时延的关系需在 sice 上确认。

### 前后对比（13 例集合，本机串行，带钩子；before/after 各两轮，共 26 次运行）

| 指标 | before | after |
|---|---|---|
| 事实快照 SQL 调用 | 504264 | **261584（−48%）** |
| 事实加载累计（Python 计） | 104.0 s | 60.9 s |
| resolver 累计 | 334.6 s | 297.5 s |
| authorize p50 / p95 / max | 280 / 1076 / 1496 ms | **251 / 980 / 1420 ms** |
| runtime_effect_tool p50 / p95 / max | 572 / 1520 / 1600 ms | **492 / 1357 / 1481 ms** |
| prepare_message_context p50 / max | 496 / 964 ms | 488 / 964 ms |
| 越限所需慢化系数 k（authorize / effect_tool） | 1.34 / 1.25 | **1.41 / 1.35** |
| 以 k=2 估算 authorize 越限次数 | 126 / 1104 | **36 / 1102** |

- 4 次运行中 authorize 的最慢一次都在 2 s 以下，`relationship_context_v4[complete]` 4 次都通过。但 before 两轮也都通过；s3m 复测那一轮曾失败（最慢 2178 ms）。该用例处于时限边缘、结果不稳定，O2a 降低了尾部，但不能单靠两轮证明其已稳定通过。
- 运行顺序：after → before → after-2 → before-2，交替进行以减少顺序偏差。

### 测量过程中的失误
在 shell 中 `source` 了 `o1_o4_set.sh`（脚本内含 `exec`），导致以空标签运行。`run_profile.sh` 的 `rm -rf "$out"` 删掉了被 git 忽略的整个 `scripts/perf/out/`（此前各轮的本地测量输出）。这些输出不在仓库中；本节数据均为删除后重新测得，那次误运行的结果作为 after 第一轮保留。仓库内文件与提交均不受影响。
