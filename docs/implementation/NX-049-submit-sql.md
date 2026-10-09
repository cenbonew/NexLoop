# NX-049：effect submit 服务端激活 SQL 的削减（临时迁移 0107）

状态：已实现，分支 `nx049-submit-sql`（BASE main `1d28796`），L4。

## 1. 测量：一次 submit 的 3 次激活调用

方法：仅用于测量的 pytest 插件（不入库）包住 `RuntimeActivationPort._execute`。在 effect_tool 内，每次 `runtime_activation_command` 前后，于同一连接、同一事务读取 `pg_stat_xact_user_functions`（`track_functions='pl'`），得到这次调用自己的服务端函数增量。

- 统计都在事务内读取，不包含剖析钩子的 pg_stat 探针查询，因此不需要再扣除探针开销。
- `track_functions` 本身给每次函数调用增加少量计时开销，所以下表毫秒数是带统计的上限，次数是准确的。

一次 submit（`runtime_effect_tool`）的 3 次调用：

| # | verb | 作用 | v4 服务端（ms，带统计） | 两 Pi |
|---|---|---|---|---|
| 0 | resolve | 由激活引用得到 Run，返回私有 Run digest | 1.9 | 2.0 |
| 1 | authorize（guard 前） | 完整授权：队列/Run/租约/期限、Run 证明、Context 绑定、关系与正式事实、Role、目录范围、执行锁与执行标记 | 149.6 | 93.0 |
| 2 | authorize（guard 后，意图写入之后） | 与 #1 相同的完整复核（意图写入后的最终检查） | 113.0 | 85.0 |

#1 和 #2 都是必须的：#2 是意图写入之后的最终复核，不能合并。重复主要出现在一次 guard 语句**内部**（v4，guard #1）：

- **激活命令的包装链层层"调两次"**：v0062 → v0052 调 2 次 → v0051 调 2 次 → v0038 调 3 次 → v0037。基线检查链 v0037 因此在一条语句里被完整执行 12 次（两 Pi 6 次），约占 27%。
  - 各层第二次调用都是"本层检查（可能等待）之后的最终复核"；
  - 0039 的第一次调用只为在加锁前拿到 run_id。
- **action 断言**：48 次（两 Pi 24 次）全部是完整检查，O5b 只覆盖读断言。主要是每次 v0037 对同一组声明（队列 EXECUTE 和各个 Run 证明）重复断言。
- **读断言**：256 次，其中完整检查 85 次。
  - 用追踪（`perf_plugin` 的 RAISE LOG 模式）看到：最重的语句里约 300 次断言只有约 50 组不同声明，重复都发生在可见状态没有变化时；
  - 其中未被 O5b 命中的主要是 Message 读（`accepted-message-v1` 派生，每条语句约 30 次），因为 O5b 把派生声明一律排除在 memo 之外。
- **底层原语**：root_identity 2864 次、事实快照加载 1644 次、服务身份快照 664 次。它们大多随上述重复而来。

## 2. 削减（迁移 `0107_nx049_submit_sql.sql`）

所有判定、返回的 binding 和证明都不变；每项检查都在它运行时可见的状态上执行；凡是可能发生等待之后的检查，仍然完整重跑。

### 2.1 0039 加锁前预检

先对命令中的 run_id 用 `pg_try_advisory_xact_lock` 试取执行锁：

- 能立即取得（没有发生等待）时，"加锁前预检"与"加锁后复核"之间没有任何变化，只运行一次完整的 0037/0038 检查。之后，经过验证的 run_id 必须与已加锁的一致，否则回到原来的"加锁 → 复核"。
- 取不到时，照旧执行"预检 → 等锁 → 复核"。
- 写执行标记之后的最终复核不变。

同 Run 锁顺序修复之后，guard 和 authorize 在进入语句之前已持有这把锁，所以总是走快路径。试取锁从不等待，所以即使用未验证的 key 去试取，也不会阻塞任何人；验证失败时事务回滚，锁随之释放。函数体其余部分与 0039 逐字相同。

### 2.2 action 断言使用 O5b 级别 1 memo

`authz.nexloop_assert_action_authority` 外加 memo 包装，内层改名为 `..._before_action_memo_v0106`。命中条件与 0096 的读断言完全相同：

- 同一语句、可见快照不变、没有新的 advisory 锁、没有对授权输入表的自身写入（0096 的失效触发器；action 链读的是同一批表）；
- 每次命中都重新检查声明期限，并重新计算实时身份快照、比较 directory hash；
- 拒绝不进 memo。

复用同一张会话临时表和同一个开关；键里加了 `action-authority` 前缀，与读断言分开。

### 2.3 读断言 memo 的准入扩大到 accepted-message-v1

0096 的包装函数体不变，只把准入的派生类型从 `type-property-v1` 扩到 `type-property-v1, accepted-message-v1`。依据：

- accepted-Message 检查本身就拒绝期限晚于规则 `valid_until` 或嵌套 Consumer READ 期限的声明，所以命中时重新检查声明期限，就覆盖了这条链的全部时钟条件。这与 O5b 对已配置声明的依据相同。
- 它读取的输入都在 0096 的失效触发器内：Message 对象、受理 outbox、会话与会话消息、外发记录、规则与授权事实。
- `purpose-message-v1`（方案 B）依赖 runtime.jobs，不在触发器覆盖范围内，仍然一律完整检查。

### 2.4 不做：root_identity 按事务 memo

微基准（本机，每次调用）：

| 操作 | 耗时 |
|---|---|
| `nexloop_root_identity` 推导 | 5.8 µs |
| 一次 memo 查找（计算键 + 统计 advisory 锁 + 查临时表） | 26 µs |

memo 查找比推导本身贵约 4.5 倍，不划算，这与 O5a 的结论一致。它的调用次数会随 2.1 与 2.2 自然下降。

## 3. 效果（一次 submit 内两次 guard，本机，带函数统计，中位数）

| 用例 / guard | 服务端 ms | v0037 次数 | action 断言完整检查 | 读断言完整检查 | root_identity | 事实快照 |
|---|---|---|---|---|---|---|
| v4 / #1 | 149.6 → 116.0 | 12 → 8 | 48 → 5 | 85 → 59 | 2864 → 1736 | 1644 → 822 |
| v4 / #2 | 113.0 → 101.6 | 12 → 8 | 48 → 2 | 45 → 45 | 2202 → 1475 | 1164 → 624 |
| 两 Pi / #1 | 93.0 → 82.3 | 6 → 4 | 24 → 2 | 53 → 44 | 1633 → 1133 | 936 → 564 |
| 两 Pi / #2 | 85.0 → 79.0 | 6 → 4 | 24 → 2 | 44 → 44 | 1489 → 1133 | 828 → 564 |

读断言总次数不变（memo 命中也计在包装函数里），减少的是其中的完整检查。不带探针的端到端前后对比见 §4。

## 4. 端到端（本机，不带 PG 函数探针，前后交替 3 轮，`sub-before-{1,2,3}` / `sub-after-{1,2,3}`）

`NEXLOOP_PERF_PGFUNC=0 scripts/perf/nx049_profile.sh`，每轮 v4[complete] 与两 Pi 各一次，前后交替。前一组在 1d28796 的 catalog 上撤下 0107。期间本机负载 4–11（其他线在跑），以交替抵消漂移。

"激活 SQL"是请求内 `runtime_activation_command` 语句的客户端计时之和（服务端执行加往返，不含探针）。

| 用例 / 请求 | n | 激活 SQL p50（ms） | 激活 SQL p95（ms） | 端到端 p50（ms） | 端到端 p95（ms） | 端到端 max（ms） |
|---|---|---|---|---|---|---|
| v4 effect_tool | 18/18 | 243 → 201（−17%） | 339 → 224（−34%） | 704 → 666 | 875 → 805 | 889 → 825 |
| v4 authorize | 228/228 | 124 → 107（−14%） | 168 → 138（−18%） | 453 → 455 | 593 → 581 | 719 → 732 |
| 两 Pi effect_tool | 18/18 | 184 → 158（−14%） | 207 → 169（−18%） | 819 → 824 | 948 → 898 | 1128 → 911 |
| 两 Pi authorize | 210/210 | 94 → 83（−12%） | 134 → 110（−18%） | 550 → 573 | 801 → 703 | 1710 → 799 |

- 6 轮全部通过（rc=0），PG 日志中没有死锁。
- 激活 SQL 稳定下降 12–34%。端到端 p50 的变化在负载噪声内；p95 和 max 下降。
- 部署主机服务端约慢 2.5 倍，按此折算，effect submit 的激活 SQL 约省 65–100 ms（p95 约 290 ms）。

## 5. 测试

`tests/test_nx049_submit_sql_pg.py`（6 例）：

- **action 断言**：
  - 同一语句内重复只做 1 次完整检查；关闭开关后每次都是完整检查，binding 相同；
  - 新 advisory 锁是屏障；同语句内自身撤销授权，下一次即被拒；
  - 声明在语句内过期时，每次命中都重新检查；拒绝不进 memo。
- **accepted-message 读断言**：同一语句内 1 次完整检查；规则停用、Message 删除都在下一次被拒。
- **0039**：其他事务持有执行锁时，create 走原来的"预检 → 等待 → 复核"路径，释放后成功。

## 6. 回归

- 受影响面 118 个测试文件（`-n 5`，pool_depth 插件，负载 7–11）：1294 passed、2 failed。
  - `test_effect_execution_sql::test_42` 要求迁移编号连续；临时号 0107 前缺 0106，属于预期内的空号失败（与 NX-022 时相同）。
  - `test_local_message_delivery_assembly::test_explicit_configuration_to_same_human_real_json_delivery` 在高负载下 runtime worker 返回 `retry_wait`；单独连跑 3/3 通过。
- 恢复改动后版本后复跑 38 passed：本文件、`test_read_assert_memo`、同 Run 锁顺序、search_path 检查。
- 硬约束所在文件全部在上面的回归集合中：memo、锁等待后最终期限（queue_permit_expiry、receipt_tail、receipt_revoke_wait、precise_pg_admission_outage）、capacity、effect_units（role_policy_*）、同 Run 锁顺序。
