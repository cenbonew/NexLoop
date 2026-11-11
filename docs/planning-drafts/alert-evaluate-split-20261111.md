# 设计：拆分 alert_evaluate 事务，让发布不再被告警评估挡住（NX-068 缺陷 A+B）

状态：设计稿，等 L6 判定后再写迁移。起因见 `tests/test_publish_tenant_lock_pg.py`：2026-11-11 stage 上 R77 的 P5 r8-publish 返回 503，原因是锁 tenant 行超时。

## 1. 现状

`AlertEvaluator.run_once()`（`nexloop_eios/observability.py`）每分钟有两个受治理的事务。

1. `authz.nexloop_experiment_guard_tick`（NX-043 X5）：授权以后检查实验守卫。
2. `authz.nexloop_alert_evaluate`（0131 §9），在同一个事务里依次做：
   1. `authz.nexloop_plan_port_tenant(...)` 授权。经 0192 的 identity-once 检查，对 `control.nexloop_tenants` 的该租户行加 **FOR SHARE**，持有到事务结束。
   2. `runtime.nexloop_observability_maintain()`：全局维护，包括 0215 的增量汇总和 0449 的冒烟轮次。
   3. `runtime.nexloop_metrics_snapshot(t,'real')`：快照。叠了很多层外包函数，是最重的读取。
   4. 逐条规则计算，更新 `control.nexloop_alert_state`（逐行 FOR UPDATE），写 `control.nexloop_alert_events`。

F3 发布写 `object_type_versions` 和 `action_definitions` 时，epoch guard 触发器要对同一 tenant 行加 **FOR NO KEY UPDATE**，与 FOR SHARE 冲突。所以 2.2 到 2.4 花多久，tenant 行的共享锁就持多久，同时进行的发布就要等多久。API 的 lock_timeout 是 3 秒，超过就返回 503。

本地实测（空库）：维护 1.3 ms，快照 27 ms，整次评估 20 ms。stage 上评估慢了很多（`alert-evaluate-timeout-explain.md` 有记录），问题就出在这里。

## 2. 目标与约束

- 授权检查和随后持有的 tenant 行 FOR SHARE **只覆盖规则计算和状态写入**（2.4），不覆盖维护和快照。
- 不改 0192 的锁语义，不延长发布的 lock_timeout（L6 已同意）。
- 告警结果与现在等价：同一个快照得出同样的触发和恢复；`for_seconds` 的计时口径不变。
- 维护和快照不需要授权凭据即可执行，但只能由告警评估器的数据库角色（`nexloop_domain_worker`）调用，结果不出数据库。

## 3. 拆分方案

`run_once()` 改为三个事务，加上原有的守卫事务，顺序如下。

**T1 维护（不需要授权，不碰 tenant 行）**
- 新函数 `runtime.nexloop_alert_maintain_unbound()`：security definer，只调用 `runtime.nexloop_observability_maintain()`，返回它的结果。只授权给 `nexloop_domain_worker`。
- 维护是全局的，本来就不读任何租户的授权；实测它单独执行时不持 tenant 行锁。
- 失败时由 Python 捕获，记为 `maintenance_failed`，后面的事务照常执行。现在维护一失败，整次评估就回滚，这一点会变，见 §5。

**T2 快照（不需要授权，只有外键的 KEY SHARE）**
- 新表 `runtime.nexloop_alert_snapshots(snapshot_id uuid, tenant_id→tenants, world, taken_at timestamptz, snapshot jsonb, consumed_at)`：RLS、owner 只写，登记进 real_effect_tables 和擦除覆盖清单（快照只有计数和代码，不含个人数据）。
- 新函数 `runtime.nexloop_alert_snapshot_take(p_tenant text)`：security definer，只授权给 `nexloop_domain_worker`。执行 `metrics_snapshot(p_tenant,'real')`，插入一行，`taken_at=clock_timestamp()`，返回 `snapshot_id`。
- 租户只能是评估器凭据所属的租户：Python 从已经认证的 session 取 `tenant_id`。T3 会核对这一行属于授权得出的租户，对不上就拒绝。T2 拿到的只是一份计数，不进 API，也不进日志。
- 外键插入只对 tenant 行加 KEY SHARE，与发布的 NO KEY UPDATE 兼容。

**T3 评估（需要授权，持锁时间短）**
- `authz.nexloop_alert_evaluate` 换成新版本（同名外包，参数多一个 `snapshot_id`，进入签名载荷的 body）：
  1. 授权，同现在。从这一步开始持有 FOR SHARE。
  2. 读 T2 那一行：必须属于授权得出的租户，`consumed_at is null`，`taken_at >= clock_timestamp() - 120s`。否则返回 `{evaluated:false, reason:'snapshot_stale'|'snapshot_unknown'}`，不改任何状态。
  3. 按 §4 的单调规则计算并写状态，`now_at` 改用 `taken_at`。
  4. 标记 `consumed_at`，删除这个租户 1 天以前的快照。
- 持锁时间只剩规则数乘以选择器数次单行更新，正常是毫秒级。

**守卫事务（`experiment_guard_tick`）**：同样是授权以后做一次扫描，但它只遍历已开始的实验，本地和 stage 上都没有慢的迹象。不在本设计范围内，先按 §6 的方法量一下它的持锁时间再决定。

## 4. 对告警结果一致性的影响

| 方面 | 现在 | 拆分后 |
|---|---|---|
| 快照与状态写入 | 同一个事务，原子 | 不同事务。T2 和 T3 之间有几十毫秒到几秒的间隔，用的是 T2 那一刻的数值 |
| `now_at`（`breached_since`、`for_seconds`、`last_evaluated_at`） | T3 时的 `clock_timestamp()` | 快照的 `taken_at`，即数值真正被观测到的时刻，口径更准确 |
| 两个评估器并发 | alert_state 逐行 FOR UPDATE 串行化，各自用自己的快照，后提交的覆盖先提交的 | 先锁 state 行，再看 `last_evaluated_at >= taken_at` 是否成立，成立就跳过这一行。较旧的快照不能覆盖较新的结果，也不会因为乱序重复产生 firing 或 resolved 事件 |
| 维护与快照的先后 | 维护在快照之前，冒烟结果能被同一次快照看到 | 不变（T1 在 T2 之前） |
| 维护失败 | 整次评估回滚，这一分钟没有告警评估 | 维护失败只记一个计数，告警照常评估。建议把 `maintenance_failed` 计入快照，可以直接加一条规则 |
| 消失的选择器自动恢复 | 按快照里不存在判定 | 不变，但同样受上面的单调规则约束 |
| 规则版本 | T3 读最新版本 | 不变 |

用 `taken_at` 当时间：`for_seconds` 的计时从“评估时刻”改成“观测时刻”，两者相差就是 T2 到 T3 的间隔，比阈值的最小粒度（秒）小得多。

## 5. 与现在不同的地方（需要 L6 确认）

1. 维护与告警的命运分开：维护失败不再让这一分钟的告警评估一起失败（§4）。
2. 新增两个不需要凭据、只授权给 domain worker 的函数。理由是这两步本来就不读授权、不写业务对象，代价是它们不再受 `nexloop.alert.evaluate:1` 授权的约束。如果 L6 要求维护和快照也必须经过授权，就只能另造一种不锁 tenant 行的授权检查，等于改 0192，不建议。
3. 快照落表：多一张运行时表，每个租户每分钟一行，保留 1 天，T3 负责清理。

## 6. 验证计划（写迁移时一起做）

- **持锁时间**：沿用 `tests/test_publish_tenant_lock_pg.py` 的方法，把维护或快照拖慢到 6 秒，发布不再被挡；同时断言发布等待期间 `pg_blocking_pids` 里没有评估器。T3 本身仍会短暂挡住发布，作为对照：等待时间 ≥ 0.5 秒且最终发布成功。
- **一致性**：
  - 现有的 `test_observability_pg`、各告警规则测试全部照旧通过，结果字段兼容，`maintenance` 仍然返回；
  - 新测试：乱序快照不覆盖较新的状态；过期或已用过的快照拒绝且不写任何东西；快照属于别的租户时拒绝；维护失败时告警照常评估。
- **守卫**：nx067 ceiling guard（新函数不读授权，不需要登记）、definer search_path、db_boundary、tenant_fk、system_catalog、copy guard、擦除覆盖、nx041 sim。
- **stage 只读核对**：上线前后对比 `pg_stat_statements` 里 `alert_evaluate` 的 mean 和 max；打开 `log_lock_waits` 以后（等负责人决定），看 `nexloop_tenants` 上的锁等待是否消失。

## 7. 工作量

一个临时号迁移：新表、两个函数、alert_evaluate 换成新版本。`observability.py` 的 `run_once` 改为三段。另外是测试，约 1 天。
