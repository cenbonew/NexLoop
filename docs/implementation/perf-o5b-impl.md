# O5b 级别 1 实现：语句内读断言去重（分支 `o5b-impl`）

BASE `dispatch/integration-s3q` `57a224c`，临时迁移 `0095_perf_o5b_read_assert_memo.sql`。设计稿见 `o5b-design` 分支 `7a8037b` 的 `docs/implementation/perf-o5b-design.md`，调度员裁定：只做级别 1，同意授予 TEMP 权限，并补齐硬要求 (a)(b) 与副作用审计。只新增一个迁移，不改已有迁移；未修改 `runtime_activation.py`、`context_artifacts.py` 及 Host（L3 正在改动）。

## 1. 包装链审计（s3q 的一次性库，`pg_proc` 递归展开）

`authz.nexloop_assert_read_authority`（0084 分派函数）向下共展开 20 个函数：

| 层 | 函数 |
|---|---|
| 分派 | 0084 → `_before_property_access_v0083`（0077 分派） |
| 核心 | `_before_message_read_v0072` = 0034 版核心 |
| 核心调用 | `lock_credential`、`service_identity_snapshot`（服务 / Run / 浏览器三路）、`root_identity(_snapshot)`、`service_identity`、`load_authority_fact(_snapshot)`、`fact_coverage`、`assert_agent_parents` |
| 派生 | `assert_derived_property_access`、`property_access_derivation`、`assert_derived_message_read`（含 v0080 / v0077 两层）、`assert_derived_consumer_read` |

结论：

1. **写入**：整条链没有任何 INSERT / UPDATE / DELETE。
2. **`set_config`**：唯一的副作用是 `set_config('eios.tenant_id', …, true)`，出现在 8 个函数中：
   - 核心、`load_authority_fact`、`assert_agent_parents`；
   - 浏览器身份快照；
   - 派生属性、派生消息（三层）。

   取值都是本次 claims 或 binding 的租户。命中路径把完整检查结束时的 `eios.tenant_id` 原样恢复，会话状态与完整检查后一致（测试 `test_repeated_assertion…` 比较了 binding）。
3. **锁**：
   - 核心、`lock_credential`、各派生函数对凭据行、事实行、租户行、规则行、对象行加 `FOR SHARE`；
   - **浏览器身份快照读取的会话、账号、成员、应用表不加锁**。这类输入只靠“可见快照不变”保护：其他事务的任何提交都会使键作废。
   - 链内没有 advisory 锁。
4. **与时钟有关的条件**：
   - claims 的 `expires_at`（核心检查两次；派生函数要求 claims 的到期不晚于嵌套 envelope 与规则 `valid_until`）；
   - 服务凭据、Run 凭据（及其 Source 凭据）的 `expires_at`；
   - 浏览器会话的 idle / absolute 期限、成员的 `valid_until`、账号的 `locked_until`；
   - 消息派生规则的 `valid_until`：它与时钟单独比较，没有被 claims 的到期约束。
5. **会话级 advisory 锁**：产品迁移中只有事务级 `pg_advisory_xact_lock`；会话级只出现在迁移执行器本身，不在请求路径上。
6. **未限定名的风险**：既有函数的 `search_path` 设为 `pg_catalog`，而 PostgreSQL 会在其前隐式先搜索 `pg_temp`。本次测试在会话的 `pg_temp` 中放置影子 `pg_locks` / `pg_class`，读取路径没有触发探针。但对既有函数未作全量审计，列为后续事项。

## 2. 实现（0095）与相对设计稿的细化

- **外层包装**：改名后的 `_before_read_memo_v0095` 就是原来的分派函数；新的 `nexloop_assert_read_authority` 只缓存“允许”。
- **键**：
  ```
  sha256(statement_timestamp ‖ pg_current_snapshot() ‖ TimeZone ‖ 调用前的 eios.tenant_id ‖ digest ‖ world ‖ claims)
  ```
  键在每次调用时现算（VOLATILE 函数，每次求值都取新快照），因此一定晚于语句中此前取得的所有锁。条目存的是**完整检查开始前**的键：如果完整检查自身发生过等待，紧接着的相同调用也一定未命中。这满足硬要求 (b)。
- **命中条件**（全部满足才命中，否则走完整检查）：
  1. 键相同；
  2. 本后端持有的 advisory 锁数与存入时相同；
  3. claims 的 `expires_at` 晚于当前时钟；
  4. 用同一段代码重跑 `service_identity_snapshot`：凭据、Run、Source、浏览器会话和成员的时钟条件全部重新判定；重跑成功且 directory_hash 未变。
- **细化 1：advisory 屏障改为比较本后端持有的 advisory 锁数，不改任何函数体。** 任何新取得的 advisory 锁，无论是否等待过，都会作废之后的命中。好处是不必改 0039 / 0065 / 0085 的函数体（也避开了 L3 的文件），以后新增的 advisory 锁自动覆盖。重复获取已持有的同一把锁不会等待，所以不算屏障。
- **细化 2：消息派生 claims（`accepted-message-v1`）一律走完整检查。** 原因是它的规则 `valid_until` 单独与时钟比较，没有被 claims 的到期约束。可去重的范围是配置授权和 `type-property-v1`；后者的规则期限与 type READ 都被 claims 的 `expires_at` 约束。
- **细化 3：命中路径重跑身份快照**，代替自行计算身份期限。这样做更稳妥，单次命中成本约为 0.18 ms，高于设计稿估计的 30 µs。即便如此，收益仍高于验收线，见 §4。
- **存放**：
  - 会话临时表 `pg_temp.nexloop_read_memo`，`ON COMMIT DELETE ROWS`，属主 `nexloop_owner`，没有授予任何权限；
  - 每次使用前校验 `relkind`、临时持久性、命名空间 = `pg_my_temp_schema()`、属主、`relacl is null`；任何一项不符就关闭 memo；
  - 所有引用都写全限定名，`search_path=pg_catalog,pg_temp`（pg_temp 显式放在最后）；
  - 只读事务中不建表，直接走完整检查。
- **写纪元**：在审计列出的 20 张输入表上加语句级 `AFTER INSERT/UPDATE/DELETE/TRUNCATE` 触发器，清空本会话的 memo。触发器不看开关，只看表的属主。
- **开关**：`authz.nexloop_read_memo_settings` 由属主持有，默认开启；会话 GUC `nexloop.read_memo=off` 只能关闭，不能开启。
- **TEMP 权限**：迁移中显式 `grant temporary on database <当前库> to nexloop_owner`。只作用于 NexLoop 自己的干净 bootstrap 链。

## 3. 测试（`tests/test_read_assert_memo.py`，20 项）

### 3.1 确定性测试

| 类别 | 测试 | 内容 |
|---|---|---|
| 基本 | `test_repeated_assertion_in_one_statement_runs_one_full_check` | 同一语句中 5 次断言只做 1 次完整检查；开关关闭时 5 次完整检查、结果相同；binding 一致 |
| 键 | `test_tenant_context_is_part_of_the_key` | 调用前的租户上下文不同即不命中 |
| 开关 | `test_session_switch_can_only_disable` | GUC 只能关闭 |
| 作用域 | `test_separate_statements_never_share` | 同一事务内的不同语句互不命中 |
| 本事务写入 | `test_own_write_invalidates_and_revocation_is_seen_in_the_same_statement` | 本事务写入（即便是不改任何行的写）清空 memo；同一语句内本事务撤权，下一次断言即拒绝 |
| 过期 | `test_claims_expiry…`、`test_credential_expiry…` | 语句内 claims 到期、凭据到期：命中路径复核时钟，到期后 0 次允许 |
| advisory | `test_advisory_lock_is_a_barrier`、`test_advisory_wait_without_xid…` | 新 advisory 锁即屏障；持有者没有 xid（快照不变）时，等锁之后仍完整重跑；等锁期间 claims 到期 → 拒绝 |
| 行锁 | `test_row_lock_wait_changes_the_snapshot_and_reruns[commit/rollback]` | 持有者提交或中止后快照都会变化、完整重跑；对照：取锁不等待时快照不变，可以命中 |
| **(b)** | `test_snapshot_taken_before_a_lock_wait_is_never_a_hit_basis` | 第一次完整检查在事实行锁上等待（持有者随后提交）：第二次相同调用仍未命中（misses 1→2→2），只有第三次命中 |
| 子事务 | `test_subtransaction_rollback_removes_entries` | 子事务回滚后条目消失；提交后清空 |
| 只读事务 | `test_read_only_transaction_runs_full_checks_without_error` | 只报完整检查自身的 FOR SHARE 错误，不建 memo 表 |
| 隔离 | `test_isolation_between_digests_worlds_and_tenants` | 交叉的 digest 与 claims 组合被完整检查拒绝，从不跨 digest 命中 |
| 拒绝 | `test_denials_are_never_memoized` | 拒绝不写入 memo |

### 3.2 伪造负例（调度员要求的三种，以 `nexloop_api` 原始连接执行受治理读取）

1. `test_forged_precreated_memo_table_disables_the_memo`：会话角色预先在 `pg_temp` 建同名对象（放行一切的视图，读取即记录探针）。结果：合法读取照常通过，撤权后被拒，探针 0 次。
2. `test_search_path_and_shadow_catalog_objects_do_not_reach_the_memo`：设置 `search_path=pg_temp,public`，并放置影子 `pg_locks` / `pg_class` 视图。结果：读取结果正确，探针 0 次。
3. `test_session_role_cannot_write_entries_even_inside_a_subtransaction`：在 savepoint 中，会话角色对属主的 memo 表执行 INSERT / SELECT / DELETE / DROP / ALTER OWNER，以及调用 `read_memo_ready()`，全部被拒；随后撤权的读取被拒。

### 3.3 硬要求 (a)：并发撤权压力测试 `test_concurrent_revocation_never_serves_a_stale_allow`

- **负载**：
  - 2 个撤权工作线程，各 100 个周期：一个撤销 / 恢复 grant 事实，一个撤销 / 恢复服务凭据（恢复时改变 expires_at，使旧 claims 永久失效）；共 200 次撤销。
  - 4 个断言线程，每条语句断言 6 次；同一组 claims 连用 4 条语句，跨过之后的撤销；其中一半 claims 只有 30 ms，会在语句中途到期。
- **违规判据**：
  - 语句开始于某次撤销提交之后、claims 解析于该次撤销之前，却得到了允许；
  - 或者在 claims 到期时刻及之后得到了允许。
- **结果**：

  | 轮次 | 撤销 | 语句 | 陈旧语句 | 断言 | 允许 | 命中 | 违规 |
  |---|---|---|---|---|---|---|---|
  | 早期版本（6×4 ms，40 ms claims，无复用）轮 1 | 200 | 936 | 420 | 5616 | 2074 | 600 | **0** |
  | 早期版本 轮 2 | 200 | 840 | 392 | 5040 | 1668 | 388 | **0** |
  | 定稿版本（6×8 ms，30 ms claims）轮 1 | 200 | 2688 | 278 | 16128 | 7978 | 1347 | **0** |
  | 定稿版本 轮 2 | 200 | 1408 | 287 | 8448 | 3686 | 284 | **0** |

### 3.4 变异对照：证明测试能抓到错误（一次性测试文件，事后已删除）

| 变异 | 抓到它的测试 |
|---|---|
| 键中去掉语句时间戳与可见快照 | 行锁等待测试、快照时机测试 (b) |
| 去掉 advisory 锁数比较 | 两项 advisory 测试 |
| 触发器不清空 memo | 本事务撤权测试 |
| 命中路径去掉 claims 到期复核 | 压力测试：**298 次违规** |

“去掉快照键”这一变异在服务凭据路径上不会产生违规：首次断言已对所有输入加了 FOR SHARE，撤权根本无法在语句中途提交。快照条件在这条路径上属于纵深防御；对未加锁的浏览器身份输入，它是主要防线。

### 3.5 首次失败与修正
- 一次性 PG 的 socket 路径超过 103 字节，PG 起不来（环境问题），改用 `/tmp` 下的短目录。
- `misses` 为 2 而不是 1：键中包含调用前的租户上下文（为正确性有意保留），语句中第一次调用会让第二次不命中。处理：测试按应用实际情况预先设置租户，另加一项键测试。
- 测试辅助代码的问题：
  - 静态引用临时表导致解析失败，改用动态查询；
  - 子事务测试中，表本身是在被回滚的子事务里创建的，随回滚一起消失；
  - 两处 fixture 在新身份入库后会话已过期，需要重新认证。
- 变异首轮有两项没被抓到：40 ms 的 claims 没有横跨一条语句（改为 30 ms，间隔 8 ms 后被抓到），以及上面说明的快照键变异。

## 4. 收益（本机串行，L4 的 13 例集合，开 / 关交替各两轮；机器上同时有其他会话在跑，load avg 6–10）

由临时 pytest 插件在 bootstrap 之后关闭开关，作为“关”的一组。插件只在 scratch 目录中，仓库内未改动任何 perf 脚本。

| 指标（pg_stat_user_functions 合计） | 关 1 / 关 2 | 开 1 / 开 2 | 变化 |
|---|---|---|---|
| `runtime_activation_command` 均值 | 65.0 / 68.2 ms | 45.8 / 43.9 ms | **−32.7%** |
| 完整读断言次数 | 97636 / 97972 | 29459 / 29320 | **−69.9%** |
| `load_authority_fact` 调用 | 181 万 | 99 万 | −45% |
| `root_identity` 调用 | 268 万 | 159 万 | −41% |
| `context_artifact_command` 均值 | 101.6 / 100.8 ms | 71.5 / 70.0 ms | −30% |
| `effect_intent_command` 均值 | 82.4 / 83.5 ms | 52.7 / 52.6 ms | −36% |
| `relationship_context_snapshot` 均值 | 57.1 / 69.1 ms | 20.3 / 20.6 ms | −67% |

| 请求（Python 侧） | 关 max / k | 开 max / k |
|---|---|---|
| authorize_runtime_activation | 1287 ms / 1.55，1348 ms / 1.48 | 1148 ms / 1.74，1112 ms / 1.80 |
| runtime_effect_tool | 1512 ms / 1.32，1449 ms / 1.38 | 1318 ms / 1.52，1277 ms / 1.57 |
| prepare_message_context | 959 ms / 2.09，966 ms / 2.07 | 789 ms，762 ms / 2.63 |

- 13 例在四轮中全部通过。`relationship_context_v4[complete]` 的 wall time 为 51.2 / 57.8 s（关）对 45.8 / 45.8 s（开）。
- 实测去重率 69.9%，处于设计稿 §7.1 区间的上界。`runtime_activation_command` 降幅 32.7%，**高于验收线约 20%**。比设计稿估计的 41–58% 低，原因是命中成本较高（重跑身份快照，以及每次命中一次子事务）。
- 测试机连续 2 轮全量（另一条验收线）由调度员负责。

## 5. 回归
- 142 个相关测试文件，6 个 worker 并行，开关开启：**1569 passed / 6 failed**（829.5 s，`.ci-results/o5b-regression.xml`）。
  - **3 项属临时编号的预期失败**：`test_bootstrap` ×2、`test_effect_execution_sql::test_42…`，因为迁移数 92 ≠ 版本号 0095。按惯例在合并重新编号时消失（L4 的 O2b 临时号 0095 也是如此）。
  - **3 项 B 类时延用例**在 6 个 worker 并行负载下失败：`context_host_export`（503 / `retry_wait`）、`relationship_context_v4[complete]`（`runtime_transport_unavailable`）、`two_pi_role_runs`（时延断言）。同一检出串行复跑这 3 项：开关开 **3 passed**（105.8 s），开关关 **3 passed**（115.4 s）。
- 开关关闭的同一组并行对照（`.ci-results/o5b-regression-off.xml`，705.6 s）：1556 passed / 19 failed。
  - 12 项是本文件的 memo 测试：关闭后按设计必然失败。
  - 3 项是临时编号失败（同上）。
  - 4 项 B 类时延用例：`two_pi_role_runs`、`outbound_messages_pg::test_scope_denied_reply…`、`runtime_dispatch::test_worker_renewal…`、`runtime_worker::test_cli_sigterm…`。

  开关开启时，这类失败是 3 项，而且与关闭时的几项不同（只有 `two_pi_role_runs` 重叠）。可见 6 worker 并行下的时延失败来自负载，与 memo 无关；开启 memo 后同类失败没有增加。

## 6. 限制与后续
- 命中成本约 0.18 ms，主要来自重跑身份快照和异常块产生的子事务。可以继续优化：例如改为不用异常块的身份期限复核，但前提是逐路径证明期限计算与快照函数一致。
- advisory 锁数比较只看数量：同一语句中，在被回滚的子事务里取得又释放的 advisory 锁不会被识别为屏障。目前代码中不存在这种用法；而且这种情况下快照未变、时钟照常复核。
- 消息派生 claims 没有去重；要纳入，需要先证明规则 `valid_until` 被 claims 的到期约束，或在命中路径中复核规则期限。
- 既有 `search_path=pg_catalog` 的函数中未限定的目录关系名，需要另行做一次全量审计（隐式 `pg_temp` 优先）。
