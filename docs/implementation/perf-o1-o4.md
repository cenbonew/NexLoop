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

### s3m 复测与 O2/O5 落地设计（分支 `perf-o2-o5-design`，BASE `aebbfa8ff8ea087ac6fe88fd0d0500b6cc7c4695` = dispatch/integration-s3m）

只读测量，未改产品代码与迁移。新增测量项：
- 钩子增加 0077/0084 派生证明帧（`message_read_basis`、`derived_message_read_envelope`、`property_access_basis`、`derived_claims`）和 envelope 帧（`role_envelope_for_run`、`catalog_envelope_from_hint`）。
- `perf_plugin` 增加可选追踪：只在一次性测试库中，用 RAISE LOG 包装 `assert_read_authority`，记录（后端 pid、语句开始时间、claims 摘要、目标），由 `scripts/perf/read_assert_dup.py` 统计同一条语句内的重复。

复现命令：

```bash
scripts/perf/o1_o4_set.sh s3m
```

```bash
touch scripts/perf/out/.trace_read_assert && scripts/perf/run_profile.sh trace-ra <v4 与两 Pi 用例>
```

#### 1. 锁修复 + 阶段 B 后的现状（13 例，本机串行，带钩子，单轮；对比 O1 后的 `o1o4-after`）

| 请求 | 决策/请求 | p50 ms | p95 ms | max ms |
|---|---|---|---|---|
| authorize_runtime_activation | 25.9→28.2 | 281→290 | 818→**1108** | 1620→**2178** |
| runtime_effect_tool | 27.8→29.8 | 567→578 | 1138→**1521** | 1548→**1795** |
| prepare_message_context | 6.6→6.6 | 492→504 | 943→978 | 943→980 |

- 12/13 通过。`relationship_context_v4[complete]` 再次失败，`runtime_transport_unavailable`：同一进程中 authorize 最大 2178 ms，其中 `runtime_activation_command` SQL 自身占 1392 ms。
- 越限所需慢化系数 k（越大越安全）：authorize 0.92、effect_tool 1.11、prepare_message_context 2.04。尾延迟相对 O1 后回升，来源是阶段 B 与 v4 新增的服务端读校验，以及 v4 用例中 120 个 `Lock:advisory` 采样（约 2.4 s，`runtime_activation_command` 内）。
- 跨请求重复仍是主体：每次 authorize/tool 有 28–30 个**互不相同**的决策（请求内重复约 0），全部来自每个请求都要重读的 envelope。

#### 2. `runtime_activation_command` 的组成（v4 用例，`track_functions=pl`）

外层 0085→0081→0065→0063→0062 是包装链，各层自身耗时只有 17–88 ms/169 次，可以忽略。每次调用平均 **167 ms**，主要构成如下：

| 子项 | 调用 | 累计 ms | 均值 ms | 占 runtime_activation_command |
|---|---|---|---|---|
| `assert_read_authority`（经 0084→0083→0072 包装链） | 28791（约 170 次/命令） | 24899 | 0.86 | **约 88%** |
| └ 0072 核心自身 | | 8348 self | | |
| └ 事实加载 `load_authority_fact(_snapshot)` | 467052（约 16 次/断言） | 18972 | 0.04 | |
| └ 身份重推 `root_identity` / `service_identity` / `root_identity_snapshot` | 686082 / 582143 / 146610 | 4782 + 2432 + 2010 self | | |
| `relationship_context_snapshot`（v4） | 185 | 13863 | 74.9 | 包含上面的断言 |
| `read_assessment_object` | 458 | 13504 | 29.5 | 包含上面的断言 |
| `service_catalog_scope` | 200 | 7138 | 35.7 | 包含上面的断言 |
| `role_formal_current`（两 Pi） | 190 | 3951 | 20.8 | |

0077/0084 派生证明本身的开销很小：`assert_derived_message_read` 819 次合计 737 ms；0084 包装层自身 75 ms；`message_read_basis` 60 次合计 20 ms。真正的成本在被包装的断言核心被调用的**次数**。

**同一条语句内的重复**（RAISE LOG 追踪）：

| 用例 | 语句数 | 断言 | 语句内去重后 | 重复率 | 每语句中位 / 最多 |
|---|---|---|---|---|---|
| v4[complete] | 300 | 30329 | 6553 | **78.4%** | 33 / 312 |
| two_pi_role_runs | 117 | 17579 | 4863 | **72.3%** | 156 / 282 |

重复最多的目标：v4 中是 Consumer、RelationshipAssessment 及其各属性、link_type；两 Pi 中是 PlanStep、ConsumerRoleLink、RoleDefinition、Goal、Consumer、EffectControl。

#### 3. Python 侧决策分布（13 例共 20924 次决策）
- 服务目录 catalog envelope：ServiceOffering 属性 34.1%，ConsumerServiceOffering 属性 14.2%，二者对象 READ 各 2.8%，合计 **约 54%**。
  - `catalog_envelope_from_hint` 共 610 次，均值 **153 ms**，含其中的决策。
- Role envelope：RoleExecutionCeiling、RoleAssignmentScope、ConsumerRoleLink、RoleDefinition 属性，约 12%；`role_envelope_for_run` 均值 27 ms。
- RelationshipAssessment 属性（v4）：6.4%。
- 每次决策的 12 条事实中，7 条是身份类（subject、membership、actor、authentication、application、subject_authority、revision），对同一会话恒定。每请求约 338–358 次事实往返中，约 58% 是这类重复。

#### 4. 收益排序与落地顺序（预期收益为本机估算）

| 序 | 项 | 内容 | 预期收益 | 改动文件 / 迁移 | 与 L1 剩余工作的冲突 |
|---|---|---|---|---|---|
| 1 | **O2a** 请求内身份类事实去重 | 在 `PostgresAuthorityUnitOfWork._load` 下增加按 (token digest, directory hash, world, kind, key) 的请求级事实 memo，只对 7 类身份事实生效；grants/scope/controls/policies/resource_graph 仍每次读取 | 每请求事实往返约 −58%（338→约 140），authorize/tool 约 −50~100 ms | `authorization.py`、`browser_authorization.py`；无迁移 | 低：L1 不改授权适配层 |
| 2 | **O5a** 事务内身份推导缓存 | `load_authority_fact` / `service_identity` 在同一事务内按 (digest, world) 只推导一次；凭据行已被 `lock_credential` 以 FOR SHARE 锁住，事务内身份不可变 | 服务端 CPU 约 −15~20%（v4：约 9.2 s / 54 s） | 新迁移（临时编号）重定义 `authz.nexloop_service_identity` / `nexloop_load_authority_fact` 的内部调用 | 低–中：L1 不改身份核心，但与 0077/0084 的 `assert_read_authority` 包装共处同一调用链，要先对齐最新包装 |
| 3 | **O2b** catalog / role envelope 按对象批量决策 | 每个对象一次批量事实加载（一次往返取全部属性的 grants/scope/controls/policies），逐属性在内存中各自 resolve，签名证明仍逐属性列出 | catalog envelope 153 ms → 约 40~60 ms，每次 authorize/tool 约 −100 ms（sice 约 −250 ms） | `service_offerings.py`、`role_runs.py`、`object_reads.py`（批量 API）、`authorization.py`；新迁移 `authz.nexloop_load_authority_facts(digest,world,keys)` | 中：L1 下一步改 context 与读派生（`object_reads._derived`、0084 路径），须等其合入后再做 |
| 4 | **O5b** 语句内读断言去重 | 同一条语句、同一检查阶段内对相同 claims 只断言一次 | `runtime_activation_command` 约 −40~60%（v4 慢调用 1392 ms → 约 600–800 ms） | 新迁移，修改断言包装链 | **高**：必须与 L1 共同设计，见下 |

**建议顺序：** O2a → O5a（两项都不碰 L1 的文件，可以立即排期）→ 等 L1 的 v5 Context/读派生合入 → O2b → 与 L1 共同设计后做 O5b。

**O5b 的硬约束（来自 L1）：**
- 只能在**同一检查阶段**内去重。
- 任何锁等待（`FOR UPDATE/SHARE`、advisory lock）之后的提交尾断言必须重新执行，因为“锁等待后最终期限”测试依赖它。
- 去重键要包含 claims 全文摘要和阶段计数。
- 实施前要先统计重复中有多少属于“锁后尾验”：本次只测到语句级重复率 72–78%，没有区分阶段。

#### 5. O2（按对象批量决策）与逐属性语义等价：证明要点
1. **判定函数不变**：每个属性 p 的结果 = EIOS resolver 对 (主体, `eios:property:T/id/p`, op) 的完整判定（grants ∧ scope ∧ controls ∧ policies ∧ application 限制 ∧ revision）。批量只改变**事实获取方式**（一次往返），不改变判定，也不用对象级结论替代属性级结论。
2. **事实集合相同**：批量加载返回的每条事实与逐条 `load_authority_fact_snapshot` 的 payload 和 record_hash 逐字节相等（同一 `security definer` 逻辑，同一身份绑定校验）。
3. **身份类事实共享不改变语义**：这 7 类事实的键只依赖会话，与目标无关；共享前要校验 directory hash。
4. **证明形状不变**：签名 claims 仍为每个属性一份，含 `target_resource` 与 12 条事实 hash；SQL 侧逐属性复核的结构不变。批量只是一个对 Python 侧的优化。
5. **有效期**：每个属性证明的 `expires_at` 按各自决策计算，不得取组内最大值；整组复用时取组内最早到期。复用的 context 保留首次 `trusted_now`，不延长。
6. **失败隔离**：某个属性被拒或缺事实时，只有该属性失败（与逐属性调用一致），不能让整组成功或整组失败。

**测试清单（O2 实施时）：**
- 等价性性质测试：随机生成对象与属性的 grant/scope/control/policy 组合（含缺失、过期、world 不符、application 限制），批量与逐属性的允许/拒绝结果和证明逐项相等。
- 撤权：撤销单个属性 grant 后，下一请求只有该属性被拒；同一请求内经 SQL 尾验拒绝。
- 跨主体、跨 world、跨租户批量互不混入。
- 有效期：组内某个属性 grant 先到期，该属性的证明 `expires_at` 不晚于它。
- record_hash 逐字节一致：批量加载与逐条加载对比。
- 并发两 Run：批量结果不共享。
- 性能回归：catalog/role envelope 的决策数与耗时，用 `scripts/perf/o1_o4_set.sh` 前后对比。

#### 6. 限制
- 单轮、带钩子（开销 1–7%）。追踪运行中 v4 因 RAISE LOG 额外开销再次 `runtime_failed`，追踪数据只用于统计重复，不作耗时依据。
- sice 未实测。
- O5b 中“锁后尾验”所占比例尚未测出（需要在包装中记录阶段，留作 O5b 设计前的第一步）。

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

## O2b：服务目录 / Role 授权信封按对象批量决策（分支 `perf-o2b`，BASE `f9eff09065a7c378518d3f7d9ae58b3d786e0697` = dispatch/integration-s3n；临时迁移 0095）

### 实现
- **迁移** `0095_perf_o2b_batch_fact_load.sql`：`authz.nexloop_load_authority_facts(digest, world, keys jsonb)`。
  - 在一次往返内逐条调用现有的 `nexloop_load_authority_fact_snapshot`，因此身份绑定、Run 资源与键校验、payload 和 record_hash 与单条读取完全相同。
  - 返回 ok / missing / error。被单条函数拒绝的键报 error，由调用方回退到单条读取，照旧抛错。
  - 最多 512 个键，格式错误整批拒绝。不改已有迁移。
- **`authorization.py`**：
  - `resolve_authorities(pool, session, queries)`：同一会话的多个查询共用一个 REPEATABLE READ 事务、一次 live identity / directory 核对、一次事实预取。
  - 每个查询仍由 EIOS resolver 的 `resolve_in_unit_of_work` 单独判定，各自 entries、各自 savepoint，失败只影响该查询。
  - 预取范围按 resolver 的常规加载键预测，未命中的事实回退为单条读取，正确性不依赖预测。
  - O1 请求 memo 照常读取和写入；浏览器会话与单查询仍走原路径。
  - 新增 `_assert_query_binding`，抽出原有绑定检查，原检查不变。
- **`object_reads.py`**：
  - 新增 `AuthorizedObjectReader.authorities(targets, operation)`：对每个目标产出与 `_authority` 相同的证明（抽出 `_claims` 共用），顺序相同。
  - 非“明确允许”的目标，以及全部 Message/Conversation 目标，按原顺序走未改动的单条 `_authority`（含 0077/0084 派生、配置优先），拒绝时抛出的异常与原来相同。
  - `get` 改用该批量 API。
- **`service_offerings._read_envelope`** 改用批量 API。服务目录、Role 绑定（`role_runs` 经此函数）、Role 策略和 formal reads 都随之批量化，`role_runs.py` 本身无需改动。
- 公开签名均未改变（`_authority`、`get`、`_read_envelope`、`resolve_authority` 保持原样）。

### 硬约束对照
- **签名证明仍逐属性列出，判定函数不变**：每个属性一份 claims（`target_resource` + 事实 hash），SQL 侧复核结构不变。
- **失败只影响单个属性**：批量结果逐目标给出；信封的拒绝语义与原来一致（按原顺序在首个被拒目标处抛出）。
- **撤权、过期、换主体、换 world 照旧拒绝**：见下面的测试。
- **锁等待后的提交尾验未省**：批量只在 Python 侧取证；`role_tail`、`receipt_revoke_wait`、`backend_lifecycle_capacity` 中的最终期限测试均通过。

### 测试（`tests/test_batch_authorities.py`，10 passed；O2a 的等价/负向测试搬到批量路径）
- 批量 ≡ 逐条：5 个目标（允许 ×2、过期 grant、应用限制外、缺失）的 allowed、authoritative、expires_at、证明 hash 逐项相等；被拒或缺失的目标不影响其他目标；只发生一次批量往返。
- SQL 批量 ≡ 单条快照：ok 项与 `load_authority_fact_snapshot` 结果逐字节相等；不存在的 grant 报 missing；他人 agent 键报 error，且单条调用确实拒绝；格式错误的批量请求整体拒绝。
- 撤权：membership suspended 或撤销单个 grant 后，旧会话整批被拒；新会话只有被撤销的那个目标被拒。
- 身份过期：证明 `expires_at` 不晚于 membership 到期；到期后整批被拒。
- 跨主体 / world / 租户：同一请求内两个会话的批量互不混用；第二个会话缺授权的目标被拒，它的 grants 事实都属于它自己。
- 并发两个请求的批量结果相互独立。
- 对象投影端到端：批量 claims 与单条 claims 除 `expires_at`（以调用时刻为界）外逐项相等；`get` 正常；缺少属性 READ 时与单条路径一样整体拒绝。

### bootstrap 与回归
- `test_bootstrap.py` 在 0095（与 0087 之间有空号）下 2 项失败，原因与 O5a 相同：该测试要求编号连续。本地临时改为连续的 0088（不提交）后 4 passed，合并重编号后即可通过。
- 回归三批共 268 passed：
  - 91 passed（328 s）：batch、O2a、O1、jobs memo、fact cache、`role_tail`、`receipt_revoke_wait`、`backend_lifecycle_capacity`、`role_policy_dispatch`、`service_offering_pg`、`role_mapping`；
  - 122 passed（270 s）：`role_post_accept`、`role_context_pack`、`message_read_derivation`、`claim_store`、`recall`、`postgres_authority`、`browser_business`、`object_edits`、`runtime_activation`；
  - 55 passed（177 s）：`role_context_v5`、`role_policy_binding` / `enforcement` / `governance` / `types`、`offering_runtime_pg`。

### L1 关注的两项（本机，安静时段）

| 项目 | before（f9eff09 的三个文件） | after（O2b） |
|---|---|---|
| `test_backend_lifecycle_capacity.py`（10 例）+ effect_units 共 11 例 | 11 passed，73.9 s | 11 passed，71.0 s |
| effect_units 单项 | 6.87 s | 5.69 s |
| `eight_concurrent_requests_complete_on_pool_of_four` | 5.55 s | 5.38 s |

### 前后对比（13 例集合，bash，本机串行，带钩子）

**机器负载的影响（如实记录）：** 早先三轮 before 中有两轮恰逢 L1 在本机运行 6 进程并行复现（load avg 13–25）：
- 基线分别失败 2 例（scope_denials 的 JSONDecodeError、v4 的 `runtime_transport_unavailable`）和 5 例（4 个端到端报 `runtime_transport_unavailable`，以及 scope_denials）；
- 同期 O2b 的两轮均 13/13 通过。

负载不同，这组数据**不作为对比依据**，只说明在高负载下余量差异会被放大。另有一轮 before 因 agent-host dist 未按 s3n 重建而失败（Host 启动即退出），已重建后作废重跑。

下表数据取自 L1 进程结束、load avg 约 5–8 时交替运行的 before-q1 → after-q1 → before-q2 → after-q2，四轮均 13/13 通过：

| 指标 | before | after |
|---|---|---|
| 决策数 / 请求（authorize / effect_tool） | 28.3 / 29.8 | 28.4 / 29.8（批量不减少决策，减少的是事务和往返） |
| 单条事实快照 SQL | 261394 | **96830**，另加批量 3826 次（每次 5.5 ms） |
| resolver 合计 / 每决策 | 293.3 s / 6.96 ms | **225.7 s / 5.35 ms** |
| 服务目录信封均值 | 123.4 ms | **108.9 ms（−12%）** |
| Role 信封均值 | 24.0 ms | **20.4 ms（−15%）** |
| authorize p50 / p95 / max | 248 / 1005 / 1366 | **232 / 949** / 1517 |
| runtime_effect_tool p50 / p95 / max | 501 / 1373 / 1523 | 491 / 1399 / 1562 |
| prepare_message_context p95 / max | 1080 / 1225 | **957 / 986** |
| 越限所需慢化系数 k（authorize / effect_tool / prepare_message_context） | 1.46 / 1.31 / 1.63 | 1.32 / 1.28 / 2.03 |

### 结论
- 收益集中在 p50/p95 与 CPU 总量：resolver 时间 −23%，信封 −12~15%，authorize p50 −6%，p95 −6%。最慢一次（max）落在单轮噪声内，没有可靠改善。
- 收益低于设计稿预估（信封 153 → 40~60 ms），原因是：批量只省掉每个属性的事务开启、identity 核对和事实往返（约 1.6 ms/决策）；EIOS resolver 对每个属性的完整判定（约 5 ms/决策）仍是下限。要再大幅下降，只能减少决策次数本身：要么跨请求复用（违反 L1 约束，不做），要么改由 SQL 侧一次性判定整组属性（需要改动 EIOS 判定路径，属于更大范围的设计）。
- 高负载下 O2b 版本的端到端稳定性优于基线，但这是在负载不同的条件下观察到的，只作参考。

## O5b 第一步：语句内重复读断言的阶段分类（只测量；分支 `perf-o5b-measure`，BASE `194e12b700085533fb82ab820fbab499cfea2927` = main）

### 方法
沿用 `perf_plugin` 的读断言追踪（只作用于一次性测试库，`touch scripts/perf/out/.trace_read_assert` 开启），并在 RAISE LOG 中为每次 `assert_read_authority` 增加两项：
- 当时的可见快照摘要 `md5(pg_current_snapshot()::text)`；
- 本后端已持有的锁数（advisory 锁数，以及全部已授予锁数，取自 `pg_locks`）。

`scripts/perf/read_assert_phase.py` 对同一条 SQL 语句（后端 pid + 语句开始时间）中、claims 摘要与此前某次断言相同的每个重复断言，与上一次相同断言比较，分为四类：

| 类别 | 判据 | 含义 |
|---|---|---|
| `same_snapshot` | 快照不变，期间未新取锁 | 可见数据完全相同，结果只可能因时钟（到期）而不同 |
| `same_snapshot_lock` | 快照不变，期间本后端新取了锁 | 取了锁但没有新提交可见；即使有等待，也没有改变任何可见数据 |
| `new_snapshot_lock` | 快照变化，期间新取了锁 | L1 所说的锁等待后提交尾验：必须重做 |
| `new_snapshot` | 快照变化，期间未观察到新锁 | 有其他事务提交，可见数据可能已变：必须重做 |

这些函数在 READ COMMITTED 事务中执行：每条内部语句取新快照，所以“快照不变”等价于“自上次相同断言以来没有任何新提交可见”。“新取锁”以 `pg_locks` 计数增加判定；行级 FOR SHARE/UPDATE 只在首次涉及某张表时增加计数，因此锁计数是下界。

运行条件：本机安静时段（load avg 约 7，无其他线测试）串行，v4[complete] 与两 Pi 用例各两轮，四次运行全部通过。

```bash
touch scripts/perf/out/.trace_read_assert && bash scripts/perf/run_profile.sh o5b-phase-N <v4[complete]> <two_pi>
```

```bash
uv run --frozen python scripts/perf/read_assert_phase.py scripts/perf/out/o5b-phase-N
```

### 数据（两轮，第一轮 / 第二轮）

| 用例 | 语句数 | 断言 | 重复 | same_snapshot | same_snapshot_lock | new_snapshot_lock | new_snapshot |
|---|---|---|---|---|---|---|---|
| v4[complete] | 300 / 300 | 30329 / 30329 | 23776（78.4%） | 14554 / 14615（61.2% / 61.5%） | 6019 / 6132（25.3% / 25.8%） | 1618 / 1505（6.8% / 6.3%） | 1585 / 1524（6.7% / 6.4%） |
| two_pi_role_runs | 117 / 117 | 17579 / 17579 | 12716（72.3%） | 10929 / 11034（85.9% / 86.8%） | 1332 / 1373（10.5% / 10.8%） | 151 / 110（1.2% / 0.9%） | 304 / 199（2.4% / 1.6%） |

各类括号内的百分比是占重复的比例。

**可安全去重**（`same_snapshot` + `same_snapshot_lock`）：
- v4 约 **87%** 的重复，即约 **68% 的全部断言**；
- 两 Pi 约 **97–98%** 的重复，即约 **70–71% 的全部断言**。

**必须重做**（快照变化）：
- v4 约 13% 的重复（约 10% 的全部断言），其中一半伴随新取锁；
- 两 Pi 约 2.5–3.6% 的重复。

重复最集中的目标：v4 中是 Consumer、RelationshipAssessment 及其各属性、link_type；两 Pi 中是 PlanStep、ConsumerRoleLink、RoleDefinition、Goal、Consumer、EffectControl。

### 对 O5b 设计的输入
1. **去重键必须包含可见快照**，不能只看“同一语句”：v4 中有约 13% 的同语句重复发生在快照变化之后，按语句去重会漏掉锁等待后的提交尾验。键至少包含 claims 摘要、`pg_current_snapshot()`、本事务写纪元（见第 3 条）、digest、world、`eios.tenant_id`；结果中的时间戳文本依赖会话 TimeZone 时也要计入。
2. **命中时必须复核时钟**。读断言核心（0034 版，经 0072/0083/0084 包装）中与时间相关的条件有：
   - claims 的 `expires_at > clock_timestamp()`，在断言开头和结尾各检查一次；
   - 服务身份快照内凭据（以及 Run 凭据）的 `expires_at > clock_timestamp()`。

   复核这几项就能保留 L1 的“锁等待后最终期限”语义：数据没变时，结果只可能随时钟变化。
3. **本事务自身的写入也要使缓存失效**。快照不反映本事务自己的写入。读断言依赖的表除了 `authz.nexloop_authority_facts`、`authz.nexloop_service_credentials`、`control.nexloop_tenants`（三者的改动都会经 0004 epoch guard 触及 tenants 行），还有浏览器会话、账号等表（浏览器路径的会话 touch 会在业务事务内写入）。键中要带一个由这些表上的触发器推进的事务内纪元。
4. **锁语义**：首次断言已对凭据行、各授权事实行、tenants 行加了 FOR SHARE，持有到事务结束。同一事务内跳过重复断言不会少持任何锁，也不会改变锁顺序（锁已在首次断言时按原顺序取得）。
5. **预期收益（估算）**：在 v4 中，`assert_read_authority` 约占 `runtime_activation_command` 时间的 88%（见 s3m 复测节）。去掉约 68% 的断言，扣除每次计算键约 10–15 µs 的开销（参考 O5a 实测），`runtime_activation_command` 预计降低约 50–60%。v4 最慢的 activation SQL 约 1.4 s，预计可降到约 0.6–0.7 s。
6. **负载敏感性**：`new_snapshot*` 的比例取决于并发提交量。本机单测中其他进程很少，生产或测试机全量并发时这一比例会更高，可去重部分随之减少，但正确性不受影响，因为键中有快照。

### 限制
- 锁计数是下界：行锁不逐行出现在 `pg_locks` 中，因此 `same_snapshot` 中可能含有少量“已取行锁但未等待”的情况。这些情况快照未变，同样可以安全去重。
- 追踪本身（每次断言多两次 `pg_locks` 查询）会拖慢运行，本节数据只用于分类，不作为耗时依据。
- 只覆盖两个用例；其他路径（例如浏览器人类会话）的比例未测。
