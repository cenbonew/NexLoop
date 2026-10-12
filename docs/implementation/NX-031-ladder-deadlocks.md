# NX-031 阶梯负载中的死锁：复现、原因与修复

- **分支与迁移**：分支 `ladder-deadlocks`，从 main `b1b3418` 切出。迁移 `0175_nx031_delivery_definition_share_lock.sql`（合入 s5p 时由临时号 0410 重编号）。
- **范围**：本任务只处理死锁。同一事件中的派发失败、凭据过期和 activation/lease denied 由 L4 处理；本任务没有改 `runtime_dispatch.py` 和 `runtime_activation.py`。
- **背景**：在 CI 主机上用 500 客户模板（`08b2099`）跑 AT-060 阶梯时，历史回复阶段积压到 `run_tasks` 约 120，PG 日志中出现 5 次 `deadlock detected`。100 和 200 客户级没有出现。

## 1. 本机复现

- **构建命令**：`scripts/load/build_template.sh --seed 31 --consumers 500`（合成数据，一次性集群）。
- **日志设置**：集群启动后，用 `ALTER SYSTEM` 打开以下设置，不改代码：
  - `log_lock_waits=on`
  - `deadlock_timeout=500ms`
  - `log_min_error_statement=error`
- **复现结果**：
  - 历史回复阶段积压到 `run_tasks` 峰值约 298；
  - 出现 **7 次 `deadlock detected`**；
  - 另有 91 次超过 500 ms 的锁等待，其中 73 次是 advisory 锁（`pg_advisory_xact_lock(hashtextextended(...))`），最长 717 ms，全部最终获得锁，没有形成环。
- **7 次死锁全部是同一对**（日志中只看语句模式与表名和函数名，不含数据）：

| 一方 | 语句 | 等待 |
|---|---|---|
| 本地 JSON 投递（effect worker 的投递路径） | `select authz.nexloop_local_delivery_authority($1,…)` | `authz.nexloop_assert_effect_plan` 第 7 行：`control.nexloop_effect_control_ledger` 行 `for update`（由投递函数第 69/70 行调用） |
| Run 提交效应意图 | `select authz.nexloop_effect_intent_command($1,…)` | 对方事务（`ShareLock on transaction`），即被对方锁住的行 |

## 2. 原因

两条路径以相反的顺序锁同样的两行：

- **投递**（0051 `nexloop_local_delivery_authority`）：
  1. 第 32 行：租户已发布的 Action 定义行（`control.nexloop_action_definitions`，`nexloop.service.request`）`FOR UPDATE`；
  2. 第 70 行：`assert_effect_plan`，即效应控制账本行 `FOR UPDATE`（0041:47），再锁上下文行。
- **Run 提交意图**（`effect_intent_command` 链）：
  1. `assert_effect_plan`：账本行 `FOR UPDATE`（0041:47）；
  2. 0040:135：**同一个**定义行 `FOR SHARE`。
- **成环过程**：投递持有定义行（排他），等待账本行；提交持有账本行，等待定义行的共享锁。
- **为什么规模越大越容易出现**：定义行每个租户每个 Action 只有一行，所有投递都在这一行上串行；而同一个消费者的多个 Run 共用一个账本行。客户越多，积压越深，并发的投递和提交越多，两者交叠的概率就越高。这与 100/200 客户级不出现、500 客户级出现相符。

### 静态分析中的其他候选（本次未复现，未改）

- **候选一**：效应提交（0041:47 先账本 `FOR UPDATE`、再上下文 `FOR UPDATE`）与 Role Run 的 `role_formal_current`（0075:64 先上下文 `FOR SHARE`）及上下文绑定（0047:50 先上下文 `FOR SHARE`、再账本 `FOR SHARE`）顺序相反，违反 0043:36 注释写明的“ledger → context”顺序。在共用同一上下文的 Run 之间可能成环；本次复现的 7 次中没有出现。
- **候选二**：派发控制检查（control heads `FOR SHARE` → goals）与修改目标（goals `FOR UPDATE` → control heads 更新）顺序相反。需要有并发的负责人或 Agent 修改目标才会触发。
- **处理**：两者都留作已知风险，见 §5。如需修改，会涉及 0075/0047 的锁顺序，可能与 L4 的改动相邻，先由调度员协调。

## 3. 修复（0175，最小改动）

- **改动**：`authz.nexloop_local_delivery_authority` 对已发布定义行改用 `FOR SHARE`，与效应执行命令（0042）的做法一致。
- **为什么够用**：
  - 投递只需要定义在事务内不被改动。`FOR SHARE` 同样阻止该行的 UPDATE/DELETE，发布或停用仍要等投递结束；
  - 共享锁与提交路径的 `FOR SHARE` 兼容，环不再成立；
  - 投递之间也不再在这一个租户级的行上串行。
- **其余不变**：函数其余部分与 0051 逐字相同，`search_path` 改为 `pg_catalog,pg_temp`。`create or replace` 保留属主与授权。
- **没有改的部分**：没有改动加锁的其他步骤和事务边界。投递之后的意图 `FOR UPDATE` → outbox `FOR UPDATE` 顺序与提交路径一致（0040:169 之后）。

## 4. 测试

`tests/test_ladder_deadlock_pg.py`（干净 catalog，合成行）：

- `test_the_delivery_takes_the_published_definition_for_share`：当前投递函数对定义行加的是 `FOR SHARE`，从 `pg_get_functiondef` 中读出。
- `test_the_former_lock_order_deadlocks_and_the_current_one_completes`：
  - 两个连接按复现中的顺序在真实表上加锁：投递先锁定义再锁账本，提交先锁账本再锁定义（`FOR SHARE`）；
  - 修复前的 `FOR UPDATE`：PostgreSQL 恰好中止一方，报 `DeadlockDetected`；
  - 当前函数的模式：提交完成，投递随后拿到账本行，两方都提交。
- `test_deliveries_no_longer_serialize_on_the_tenant_wide_definition_row`：两个投递可以同时持有定义行；停用定义的 UPDATE 仍要等待（`lock_timeout` 报错）。
- 本地连续运行 3 次，都通过。

## 5. 修复后的再次复现与已知风险

- **再次复现**：同一命令重建 500 客户模板（包含 0175）。结果见下：

| | 修复前（0051） | 修复后（0175） |
|---|---|---|
| `deadlock detected` | 7 | **0** |
| 历史回复阶段 `run_tasks` 峰值 | 约 298 | 约 291 |
| 超过 500 ms 的锁等待 | 91（其中 advisory 73，最长 717 ms） | 79 |
| 历史回复：已回复 / 未回复 | 380 / 393 | 761 / 12 |
| 失败的 Run 任务（`runtime_dispatch_unavailable`） | 389 | 399 |

- 两次构建的环境相同：同一台开发机、同一 seed、500 客户、负载约 3–9。
- 回复数的提升说明，死锁中被中止的投递和提交事务，此前连带让对应的回复没有完成。
- `runtime_dispatch_unavailable` 失败数基本不变，属于 L4 处理的派发与凭据问题。

- **已知风险（未改）**：§2 中的两个静态候选。
- **复现中观察到的其他现象**：修复前的复现中，773 个历史 Run 有 389 个以 `runtime_dispatch_unavailable` 失败，日志中有约 2,700 行 authentication/activation/lease denied；修复后分别为 399 个和约 500 行。这些属于 L4 正在处理的派发与凭据过期，本任务没有处理。
