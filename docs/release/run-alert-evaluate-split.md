# 执行单：拆分 alert_evaluate 事务上线（NX-068 缺陷 A+B）

内容：
- 迁移 `alert_evaluate_split`（临时号 0452，以合批后的编号为准）；
- 评估器 `AlertEvaluator.run_once` 改为三个事务；
- 告警规则 v39。

设计见 `docs/planning-drafts/alert-evaluate-split-20261111.md`。

## 0. 时间窗口

**不得落在 P5 执行单 §4–§6 之间。** 在部署记录里写明是在 P5 §4 之前，还是在 §6 之后。

## 1. 上线前只读核对（D5，基线）

用 owner/admin 的只读会话执行，不写任何东西：

```sql
begin transaction read only;
select calls,round(mean_exec_time::numeric,1) mean_ms,round(max_exec_time::numeric,1) max_ms,left(query,80)
  from pg_stat_statements
 where query like '%nexloop_alert_evaluate(%' or query like '%nexloop_experiment_guard_tick(%'
 order by query;
rollback;
```

- 记下 `alert_evaluate` 和 `experiment_guard_tick` 各自的平均耗时、最长耗时和调用次数。没有 pg_stat_statements 时，在记录里写明，不要估计。
- `experiment_guard_tick` 与改造前的 `alert_evaluate` 一样，在授权之后持有 tenant 行的 FOR SHARE。如果它的平均耗时超过 500 ms 或最长耗时超过 3 秒，交给调度员，另行拆分（这次没有改它）。

## 2. 部署

1. 部署包含本提交的镜像并迁移。迁移尾与 `versions.lock.json` 的 `bootstrap_revision` 相同。
   - 旧评估器配新迁移（滚动部署或回退）：签名载荷里没有 snapshot_id，T3 按旧方式在同一事务里先维护、再拍快照，然后评估，不计为跳过。
   - 新评估器配旧迁移：找不到 `authz.nexloop_alert_snapshot_take`，评估失败。所以先迁移，再换评估器。
2. 告警规则 v39，与第 1 步在同一次部署里完成：

   ```bash
   docker run $CHECK_COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/alert_rules_apply.py --manifest /opt/release/deploy/configuration/alert-rules.v39.json --check
   ```

   `--check` 应显示版本 39，只比 v38 多三条：`alert_maintenance_failed`、`alert_evaluation_skipped`、`alert_evaluation_stalled`。然后去掉 `--check` 正式应用。

## 3. 上线后只读核对

```sql
begin transaction read only;
select runtime.nexloop_metrics_snapshot(:'tenant','real')->'alert_evaluation';
-- 预期 maintenance_failed_1h 0、skipped_1h 0、minutes_since_success 0 或 1
select count(*) filter (where consumed_at is null) open,count(*) total,max(taken_at) from runtime.nexloop_alert_snapshots where tenant_id=:'tenant';
-- 预期 open 为 0 或 1，max(taken_at) 在 1 分钟以内
select outcome,count(*) from runtime.nexloop_alert_evaluation_runs where tenant_id=:'tenant' and at>now()-interval '15 minutes' group by 1;
-- 预期只有 evaluated
rollback;
```

运行 15 分钟以后，再执行一次第 1 节的 pg_stat_statements 查询，补上 `alert_snapshot_take` 和 `alert_maintain_unbound`：

```sql
select calls,round(mean_exec_time::numeric,1) mean_ms,round(max_exec_time::numeric,1) max_ms,left(query,80) from pg_stat_statements
 where query ~ 'nexloop_(alert_evaluate|experiment_guard_tick|alert_snapshot_take|alert_maintain_unbound)\(' order by query;
```

- 预期 `alert_evaluate`（即 T3）的平均耗时明显低于上线前，因为维护和快照的时间已经移到另外两个函数里。
- 记下 `experiment_guard_tick` 的平均和最长耗时，与上线前对比（D5）。

## 4. 已知的行为变化

- **维护失败不再让这一分钟的告警评估失败**：评估照常进行，失败被计数，`alert_maintenance_failed` 会触发。
- **一个 tick 里授权发生变化**，比如这一分钟恰好有一次发布或 register：这一 tick 的 T3 会被拒（ContextDenied），快照保持未消费。下一 tick 重新认证，在 120 秒有效期内复用这份快照。超过有效期没被用掉的快照不会再被评估，下一 tick 重新拍一份。
- **发布不再等维护和快照**，只会在 T3 的规则计算期间短暂等待，正常是毫秒级。

## 5. 回退

重新部署上一版镜像，并重新应用 v38。迁移不回退：
- 两张新表留着不用；
- 旧评估器调用新版 `authz.nexloop_alert_evaluate` 时走兼容路径，告警照常评估，但又回到“授权后持锁做维护和快照”的旧行为。
