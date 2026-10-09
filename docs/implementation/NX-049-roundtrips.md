# NX-049 手段 5 与 4a：减少每请求的 SQL 往返

状态：已实现，分支 `nx049-roundtrips`（BASE `dispatch/integration-s3w` `4448c40`），L4。
依据：`docs/implementation/NX-049-analysis-round2.md` §4/§7。手段 4b（合并成 RR 只读事务）这次不做。

## 1. 改动

### 手段 5：逐事实快照并入批量预取（`authorization.py`）

- **单查询路径也做预取**：`resolve_authority` 走的是单个授权单元（`PostgresAuthorityProvider.open_unit_of_work`）。它现在像 O2b 批量路径一样，按 `_predicted_fact_keys` 预取本次判定会用到的事实快照：
  - 用同一个批量函数 `authz.nexloop_load_authority_facts`，逐键调用的是原来的单事实函数；
  - 只缓存 `ok` / `missing` 两种结果，其他结果在使用时按原样单读，报错方式与原来相同；
  - 本请求内 O2a 已记住的身份类事实不再预取。
- **开单元只用一条语句**：同一个 REPEATABLE READ 事务里，原来分三次往返读取的内容并成一条语句：
  - 实时身份快照，即 `nexloop_service_identity_snapshot`；
  - 单元的可信时间，即 `clock_timestamp()`；
  - 预取的事实批量。

  身份过期的判定、读到的状态都不变。
- **O2b 批量路径**：`resolve_authorities` 关闭单元自带的预取（`prefetch_on_open=False`），仍由它自己一次预取所有查询的事实。
- **证明内容不变**：`entries`（证明里的 record hash）只在实际 `_load` 时追加，所以证明内容与原来相同。

### 手段 4a：单请求共用连接（新文件 `request_connection.py`，以及 `backend.py`、`assembly.py`、`authorization.py`）

- **连接池门面**：Backend 把连接池包成 `RequestConnectionPool`。4 个请求入口（`_invoke`、`run_request`、`_invoke_browser`、`_invoke_review`）在 `authority_request_scope` 之外，再开一个 `request_connection_scope`。
- **作用域内取连接的规则**：
  - 请求自己的连接空闲（没有未结束的事务）时，复用它；
  - 它正处于事务中时，即嵌套工作（例如 effect_tool 外层事务内构建的证明和信封），用请求的第二个连接，同样只在它空闲时复用；
  - 再深一层，照旧另取一个池连接；
  - 作用域外，门面就是原连接池。
- **事务边界不变**：每段代码仍在原来的位置开始和结束自己的事务。块结束时若留有事务则提交，出错则回滚，与连接池自身的 `connection()` 行为一致。每请求最多两个连接，与 `LifecycleLock` 的容量假设一致，由 `tests/support/pool_depth_plugin` 验证。
- **角色核验**：`assembly.verify_application_role` 在请求自己的连接上只做一次。结果记在作用域里，不记在池连接上；下一个请求，或作用域外的连接，都会重新核验。
- **隔离级别并入 BEGIN**：授权单元把 REPEATABLE READ READ ONLY 设成连接属性，由 psycopg 写进 `BEGIN` 语句。事务结束后恢复连接原来的默认值，所以被复用的连接不受影响。
- **未改动的文件**：`runtime_activation.py`、`effect_execution.py` 都没有改。activation 和 effect_tool 路径经过 Backend 的请求入口，由门面统一覆盖。effect worker 在独立进程中，使用自己的连接池，不在 guard 时限内。
- **O3 缓存未动**：`authorization.py` 里的 `FACT_PARSE_CACHE` 一段没有改。

## 2. 每请求计数（本机 Mac，`scripts/perf/nx049_profile.sh`，各 2 轮，中位数）

前：`nx049rt-before`（4448c40）。后：`nx049rt-final`（本分支）。

| 用例 | 请求 | getconn | 角色核验 | SET TRANSACTION | 逐事实快照 | O2b 批量 | 身份快照（含合并语句） | clock_timestamp | activation 命令 | SQL 往返合计（不含探针） |
|---|---|---|---|---|---|---|---|---|---|---|
| v4 | authorize | 39 → 1 | 39 → 1 | 25.5 → 0 | 128.5 → 0 | 4 → 4 | 30.5 → 30.5 | 25.5 → 0 | 2 → 2 | 260.5 → 43 |
| v4 | effect_tool | 43 → 2 | 43 → 2 | 25.5 → 0 | 128.5 → 0 | 4 → 4 | 32.5 → 32.5 | 25.5 → 0 | 3 → 3 | 273.5 → 53 |
| 两 Pi | authorize | 25 → 1 | 25 → 1 | 13 → 0 | 46 → 0 | 8 → 8 | 19 → 19 | 13 → 0 | 2 → 2 | 132 → 36 |
| 两 Pi | effect_tool | 29 → 2 | 29 → 2 | 13 → 0 | 46 → 0 | 8 → 8 | 21 → 21 | 13 → 0 | 3 → 3 | 145 → 46 |

- 计数钩子按语句中的第一个函数命名。开单元的合并语句因此计在"身份快照"下，次数不变；它同时完成了原来的 clock 和事实预取，所以 clock 一列为 0，预取不单独出现。
- 业务 SQL 的次数不变：activation 命令、effect intent、目录提示、读断言等。
- "逐事实快照降到个位数"已达成，降到 0。但每个授权单元仍是一个独立的 RR 事务，v4 每请求约 25 个，两 Pi 约 13 个。按"每项检查在各自事务里读当前状态"的约束，单元之间不合并；进一步合并属于 4b。

## 3. 时延（本机，同上两组；负载 前约 2.8，后约 6）

| 用例 | 请求 | p50 前 → 后（ms） | max 前 → 后（ms） |
|---|---|---|---|
| v4 | authorize | 610 → 552 | 952 → 788 |
| v4 | effect_tool | 952 → 800 | 1006 → 984 |
| 两 Pi | authorize | 768 → 756 | 940 → 1017 |
| 两 Pi | effect_tool | 1037 → 1015 | 1088 → 1067 |

- Mac 上单线程快，往返本身便宜，单独请求的收益小，这与分析稿 §4 的判断一致。重叠请求的收益要到部署主机上测：每少一次往返，就少一次重新获取 GIL。
- 后一组测量时机器负载更高，两 Pi 的 max 略升，在噪声范围内。部署主机上的验收仍按分析稿 §8：两个用例重叠请求中最慢的一次都低于 1.5 s，且 6/6 通过。

## 4. 发现：同一 Run 的 authorize 与 effect_tool 之间有锁顺序反转（已有问题，未修）

剖析运行 `nx049rt-after2` 中，两 Pi 的第 1 轮出现过一次 PostgreSQL 死锁，effect submit 因此在 2 s 处中止。双方如下：

- authorize 请求：在 `runtime_activation_command` 中等待 advisory 锁 `nexloop-runtime-execution:<run>`；它的事务此前已经锁住了某些行。
- 同一 Run 的 effect_tool 请求：外层事务里的 guard 已经持有这把执行锁，随后在 `effect_catalog_hint` 中要锁的行，正被前者的事务持有。

两条路径的加锁顺序都由已有 SQL 和已有事务边界决定，本分支没有改变其中任何一个：门面只复用空闲连接，从不把两个事务合并；会话级 advisory 锁只在迁移工具中使用。

- 交替对照中，改动前 5 次、改动后 5 次，两 Pi 全部通过；`nx049rt-final` 4 轮也全部通过，没有再出现。
- 推测这是已有的低概率竞态，需要锁设计的负责方评估：同一 Run 的 tool 请求已由 run 锁串行，但 authorize 与 tool 之间还没有串行。

## 5. 测试

- `tests/test_nx049_roundtrips_pg.py`（4 例）：
  - 单查询单元的预取与不预取结果逐项相等（结果、权威标记、期限、record hash），且逐事实单读为 0；请求作用域内也一样；
  - 请求作用域复用连接，每个块是独立事务；嵌套工作用第二个连接，超出预算另取，与原来一样；隐式事务在块末提交，出错时回滚；作用域结束后没有借出的连接；
  - 角色核验每个请求连接一次，下一个请求重新核验，作用域外每次都核验；
  - 授权单元的事务是 REPEATABLE READ READ ONLY，结束后连接默认值已恢复。
- 硬约束集，启用 `-p support.pool_depth_plugin`，208 passed：
  - capacity、read_assert_memo；
  - queue_permit_expiry、receipt_tail、receipt_revoke_wait、precise_pg_admission_outage（锁等待后的最终期限）；
  - role_policy_*（effect_units）；
  - role_pi / role_runtime checkpoint、relationship v4、runtime_effect_*、effect_dispatch、runtime_dispatch、message_relay、host_control；
  - O2a/O2b 等价测试（`test_batch_authorities`、`test_identity_fact_memo`、`test_authority_request_memo_jobs`）。
- 大范围定向回归（145 个测试文件，含 pool_depth 插件）：1505 passed。
