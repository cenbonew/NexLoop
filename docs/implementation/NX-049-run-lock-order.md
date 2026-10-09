# NX-049：同一 Run 的 authorize 与 effect_tool 统一加锁顺序

状态：已实现，分支 `nx049-run-lock-order`（BASE `dispatch/integration-s3y` `f0ee8a3`），L4。无迁移。

## 1. 环路（确定性复现所得）

```
DETAIL: Process A waits for ExclusiveLock on advisory lock [...]; blocked by process B.
        Process B waits for ShareLock on transaction N; blocked by process A.
A: authorize（verb=authorize）的 runtime_activation_command，等在 0039 的
   pg_advisory_xact_lock('nexloop-runtime-execution:'||run_id)
B: 同一 Run 的 effect_tool，执行到 effect_catalog_hint（意图准入）
```

- **authorize（A）**：0039 在语句中途取执行锁。在此之前，基线检查链（0037/0038 及各层包装）已经对一批行加了 `FOR SHARE`（`RowShareLock`）。阻塞时实测 A 持有的锁包括：Run 凭证、runtime 任务、effect 上下文/计划绑定、业务对象、Role 绑定、授权事实等，没有写锁。
- **effect_tool（B）**：在 98473bf 的 Run 锁之后，第一次 guard 已经取得执行锁，并一直持有到提交。随后的意图准入要对上述某一行加排他锁，被 A 的共享锁挡住，于是成环。

## 2. 修复

`runtime_activation._execution_lock`：两条路径都在**任何行锁之前**先取 Run 的执行锁（0039 同一个 key，事务内可重入）。

- **authorize**：在执行 `runtime_activation_command` 的同一事务里，先取执行锁。Run 由已验证的 Run 凭证确定（`_identity(first['_run_digest'])`）。所有证明在此之前已在事务外构建好。
- **effect_tool**：两次 guard 都先在 Python 中构建好已签名的信封，再取执行锁，然后执行语句。第一次 guard 时执行锁会提前到语句开头，之后的取锁都是重入。
- **统一顺序**：`[仅 tool：Run 锁 nexloop-role-effect] → 执行锁 → 行锁`。0039 在语句内仍会再取一次执行锁（重入）。所有证明、租约、fence、期限检查都在等锁之后、在语句内重新执行（"锁等待后最终期限"不变）。等待时间受 `lock_timeout` 约束（3 s）。

### 比较过、放弃的方案

在 authorize 前取工具请求的 Run 锁（98473bf/0085 的 key）：

| 方案 | 结果 |
|---|---|
| 排他模式 | 消除死锁，但 authorize 要等工具请求的整个事务，包括工具在持锁期间用 Python 构建 guard 证明的时间 |
| 共享模式（authorize 之间互不阻塞） | 同样消除死锁，本机两组时延基本相同 |

本机 3 轮的 authorize 时延（ms）：

| 方案 | p95 | max |
|---|---|---|
| 修复前（`runlock-before`） | 484–618 | 598–712 |
| Run 锁，共享模式（`runlock-shared`） | 766–910 | 843–1055 |

部署主机约慢 2.5 倍，这会逼近或超过 2 s，所以没有采用。执行锁方案下，工具请求的持锁时间从证明构建之后才开始，与修复前 authorize 本来就要在语句中途等待的那段时间相同。

## 3. 量化（本机，`scripts/perf/nx049_profile.sh`，各 3 轮，v4[complete] 与两 Pi 串行）

| | authorize p50/p95/max（ms） | effect submit p50/max（ms） | 死锁 |
|---|---|---|---|
| 修复前 `runlock-before`（f0ee8a3） | 402–488 / 484–618 / 598–712 | 662–772 / 676–863 | 无（本组未触发） |
| 执行锁方案 `runlock-exec` | 404–484 / 477–547 / 587–718 | 638–778 / 663–815 | 无 |

同一 Run 内的互相等待，即执行锁语句本身的耗时：

| 用例 / 请求 | 等待超过 5 ms 的次数 | p95 | max |
|---|---|---|---|
| v4 authorize | 53/228 | 82 ms | 186 ms |
| v4 effect_tool | 6/18 | 26 ms | 53 ms |
| 两 Pi authorize | 29/210 | 78 ms | 207 ms |
| 两 Pi effect_tool | 2/18 | 15 ms | 15 ms |

- 修复前这段等待发生在 SQL 语句中途，Python 侧看不到；端到端时延没有变化，说明等待只是被提前了，并没有新增。
- 按部署主机慢 2.5 倍折算，最长约 0.5 s，在 2 s 时限内。
- 剖析钩子只保留每请求耗时最高的 12 个分解项，小于这些项的锁等待记为 0，因此 p50 偏低；大的等待都能看到。

## 4. 测试

**`tests/test_same_run_lock_order_pg.py`**：

- `test_same_run_authorize_and_effect_tool_never_deadlock`（确定性复现）：
  1. effect_tool 在意图准入之前停住，此时已持有执行锁；
  2. 同一 Run 的 authorize 开始，等到它在 advisory 锁上阻塞；
  3. 放行 effect_tool。
  - 修复前 3/3 出现 40P01（authorize 被选为牺牲者）；
  - 修复后 5/5 通过（执行锁方案又跑 3/3），两个请求都成功，放行后约 0.30 s 完成。
- `test_authorize_takes_the_execution_lock_before_its_rows_and_not_the_tool_run_lock`：
  - 工具独占的 Run 锁不会阻塞 authorize；
  - 有人持有执行锁时，authorize 在取任何行锁之前等待（阻塞时它的 `RowShareLock` 计数为 0），释放后成功。

**回归**：

- 受影响面 101 个测试文件（`-n 6`，pool_depth 插件）：1079 passed、1 failed。
  - 失败的是 `test_runtime_dispatch.py::test_reclaimed_authorized_execution_missing_storage_is_not_rebuilt`。该用例的任务租约只有 1 s，在 `-n 6` 负载下 authorize 超时，以失败关闭结束。
  - 单独连跑 3 次都通过；同一文件与硬约束集一起，在 `-n 6` 下连续两遍都是 83 passed。
- 硬约束集，两遍各 83 passed：
  - capacity / effect_units（`test_backend_lifecycle_capacity`、role_policy_*）；
  - memo（`test_read_assert_memo`）；
  - 锁等待后最终期限系列（queue_permit_expiry、receipt_tail、receipt_revoke_wait、precise_pg_admission_outage）；
  - 两 Pi、runtime_effect_tools、runtime_dispatch、本文件。
