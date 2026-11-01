# S89 修法 ① 运行单（复评派发拒绝按 self 节流；迁移 plan_denial_self_cause）

内容：迁移 0399 `plan_denial_self_cause`（组批定号；bootstrap_revision 0399）——复评 Run 被派发拒绝时按 `cause='self'` 重新登记，进入 0106 的自触发节流。只改触发器函数，代码不变。

## 上线
随 R 迁移（先备份）；`check_definer_search_path` 预期 0 problems。无镜像或配置变更。

## 副作用（必须知道）
被节流的条目由 worker 以 `_retry(..., 'self_trigger_throttled', max_attempts=20)` 处理，**仍计次数**。预算长时间耗尽时，约 20 个窗口后条目死信：stage 设置 `self_window_seconds 600` → 约 **3.3 小时**，触发 `feed_dead_letter`（critical）。死信后该计划不再自动复评，直到有新的触发（Budget.set 等控制事件会重新登记，touch 把条目恢复为 pending、次数清零）。这一点由 S90（work_feed defer，不计次）消除。

## 观察方式（DATA_HOST ro，只读计数）
1. 每计划每小时的复评 Run 数，应 ≤ `self_trigger_cap × 3600 / self_window_seconds`（stage：2 × 6 = 12）：`s89-plan-loop` 第 1 条（`hr` 别名已修正）。
2. 被派发拒绝的复评 Run 按码计数：`s89-plan-loop` 第 2 条。
3. 节流中的条目与其次数（预警死信）：
   ```
   ro "select item_key, attempts, last_code, available_at from runtime.nexloop_work_feed where tenant_id=current_setting('eios.tenant_id') and world='real' and feed='plan-reevaluate' and last_code='self_trigger_throttled' order by attempts desc limit 20"
   ```
   attempts 接近 20 且 `refusals.last_5m` 中 NXB01 仍在出现 → 预算长期耗尽，按 `refusal_budget_exhausted` 告警处理预算；`feed_dead_letter` 若因此触发，是本修法的已知副作用，不是新故障。

## F1 a) 判读 feed_dead_letter（L6）
预算长期耗尽约 3.3 小时后出现的 plan-reevaluate 死信（`feed_dead_letter`，critical），是 **S89 节流 + "稍后再试仍计次"（K0 §2.2）叠加的已知结果**，S90 的 defer 会消除它。但**每次仍须逐条确认原因**，不得一律忽略：用下面 F2 的查询与"节流中的条目"查询确认该条目 `last_code='self_trigger_throttled'`、其触发为 `dispatch_denied / budget_exhausted`（或 control_snapshot_missing），且同期 `refusals.last_5m` 有 NXB01；不满足这三条的死信按普通故障处理。

## F1 b) 恢复：预算期间会不会"自然滚动"（已核实，0068 / 0106）
- **不会自然滚动。** 预算是 `control.nexloop_budget_limits` 每个 budget_kind 一行，带显式 `period_start / period_end`（0068 :109-111）；期间结束后消费检查直接以 `'budget period closed'` 抛 **NXB01**（0068 :575）——与额度用尽同码，同样触发派发拒绝与节流。没有任何定时任务或触发器在期间结束时开启新期间，也就不产生控制事件。
- **能重新登记计划的是 Budget.set。** 受治理 Action `Budget.set`（system-actions）经 `control.nexloop_nx022_bump(..., 'budget','budget', budget_kind, ...)`（0068 :380）写一条 scope `budget` 的控制事件；0106 的 `runtime.nexloop_plan_on_control_event` 对 `control_snapshot->'budgets'` 含该 kind 的活跃计划执行 `plan_feed_touch(cause 'external')`——touch 对已死信的条目同样恢复为 pending、次数清零（0093），于是计划自动继续。
- **人工处理办法**（预算恢复后计划仍未继续时）：
  1. 负责人以受治理的 `Budget.set` 开启新期间或调整额度（run-assembly §5 的 `real_tenant_init.py budget` 方式）——这本身就会重新登记相关计划；
  2. 单个计划：负责人经工作台/API 的受治理操作 `request_plan_reevaluation`（capability `plan.request_reevaluation`，`goal_controls.py` 321）请求复评；
  3. 不用直接 SQL touch。
- **列为 S90 验收项**："defer 落地后，预算恢复（Budget.set）时被推迟的计划自动继续，且推迟期间不计次、不死信"；另：期间结束（NXB01 'budget period closed'）与额度用尽同样走 defer。

## S90 之后（迁移 work_feed_defer 合入后，以本节为准）
- **副作用消除**：复评条目的最新触发是"本计划 Run 因预算耗尽（或期间结束，同为 NXB01）被派发拒绝"时，worker 以 `defer` 推迟 900 秒（`plan_reevaluation.DEFER_SECONDS`），**不计次数、不死信、不建 Run**（不进入 self 窗口）；暂停（precheck 判 paused）同样 defer。预算长期耗尽不再出现 `feed_dead_letter`。
- **独立上限**：一串连续推迟最长 7 天（`MAX_DEFER_SECONDS`，以 `runtime.nexloop_work_feed.deferred_since` 计）；超过后回到计次重试（可见于 feed attempts，最终死信）。长期推迟期间该条目的 `changed_at` 不变，`feed_backlog_age`（warning，> 600 s）会如实显示积压——这是"长期推迟由积压告警体现"的设计，判读时对照 `last_code='budget_exhausted'` / `'control_paused'`。
- **恢复不再需要人工处理**：负责人执行 `Budget.set`（开启新期间或调整额度）→ 预算控制事件重新登记计划（最新触发变为 external，条目立即到期）→ 下一轮正常复评；恢复（resume）同理。上面 F1 b 的"人工处理办法"只在 7 天上限之后、或负责人要求立即复评时才需要。
- 告警口径不变：defer 不是失败，不计入 failure_rate，不触发 `feed_dead_letter`；v28 不变。
- **F1 b 在 S90 后的结论（核实实现后的准确说法）**：预算期间不会自然滚动，也就不会自己产生控制事件（0068，见 F1 b）。defer 每 900 秒让条目再被领取一次，但 worker 只看**最新触发**：只要最新触发仍是"本计划 Run 因预算耗尽被拒"，它就再推迟一次，**不会**主动去试预算是否已恢复。预算恢复的唯一途径是负责人的 `Budget.set`（新期间或新额度），它写的预算控制事件会把计划重新登记为 external 触发，下一次领取即继续。因此：预算恢复后不会卡在死信、也不需要人工 touch；若 7 天内一直没有 Budget.set，推迟串到上限后回到计次重试（可见、最终死信），这是"防无限推迟"的设计。
- **Q1 判读 `feed_backlog_age`**：长时间推迟时条目的 `changed_at` 不变，`feed_backlog_age`（warning，> 600 s）会报警。对负责人主动暂停或预算耗尽这是**预期噪音**：先用下面的只读查询看 `last_code`——是 `control_paused` 或 `budget_exhausted` 则无需处理（等 resume / Budget.set）；是其它码按普通积压处理。
- 只读观察：`select item_key, last_code, attempts, deferred_since, available_at from runtime.nexloop_work_feed where tenant_id=current_setting('eios.tenant_id') and world='real' and feed='plan-reevaluate' and deferred_since is not null order by deferred_since limit 20`。

## F2 每计划每小时复评 Run ≤ 上限（只读 SQL）
上限按该计划自己的设置计算：`self_trigger_cap × ceil(3600 / self_window_seconds)`（stage 2 × 6 = 12）。返回 0 行即符合；有行即超出（附该小时 Run 数与上限）。
```
ro "$(cat <<'SQL'
with r as (
  select r.plan_id, date_trunc('hour', r.created_at) hr, count(*) runs
    from runtime.nexloop_plan_runs r
   where r.tenant_id=current_setting('eios.tenant_id') and r.world='real' and r.created_at > now()-interval '7 days'
   group by 1,2),
 s as (
  select distinct on (p.plan_id) p.plan_id, (p.settings->>'self_trigger_cap')::int cap, (p.settings->>'self_window_seconds')::int win
    from runtime.nexloop_plans p
   where p.tenant_id=current_setting('eios.tenant_id') and p.world='real'
   order by p.plan_id, p.version desc)
select r.plan_id, r.hr, r.runs, s.cap*ceil(3600.0/s.win)::int hourly_limit
  from r join s using (plan_id)
 where r.runs > s.cap*ceil(3600.0/s.win)::int
 order by r.runs desc limit 20
SQL
)"
```
注意：一个窗口可能跨两个整点小时，按整点统计的上限是近似值（偏宽松）；外部触发（目标改版、Budget.set、暂停/恢复）的复评不受节流，也会计入。

## 附：只读核查 case（r_s89_stage.case.sh）

```
  # S89 (L7): read-only check of the dispatch-denial reevaluation loop on stage, numbers and codes only. ro() as r66_data.sh.
  s89-plan-loop)
    echo '== 1. plans with the most reevaluation Runs in any 1 hour of the last 7 days (expect: no plan far above self_trigger_cap)'
    ro "$(cat <<'SQL'
select plan_id, date_trunc('hour', created_at) hr, count(*) runs
  from runtime.nexloop_plan_runs
 where tenant_id=current_setting('eios.tenant_id') and world='real' and created_at > now()-interval '7 days'
 group by 1,2 having count(*) > 2 order by 3 desc limit 20
SQL
)"
    echo '== 2. reevaluation Runs whose task was denied at dispatch, by code, and how many plans they belong to (7 days)'
    ro "$(cat <<'SQL'
select j.result->>'code' code, count(*) runs, count(distinct r.plan_id) plans, min(r.created_at) first, max(r.created_at) last
  from runtime.nexloop_plan_runs r
  join authz.nexloop_runtime_run_bindings b on b.run_id=r.run_id
  join runtime.jobs j on j.tenant_id=b.tenant_id and j.job_id=b.task_id
 where r.tenant_id=current_setting('eios.tenant_id') and r.world='real' and r.created_at > now()-interval '7 days' and j.status='failed'
   and j.result->>'code' in ('control_paused','goal_version_stale','control_revision_stale','object_revision_stale','budget_exhausted','control_snapshot_missing')
 group by 1 order by 2 desc
SQL
)"
    echo '== 3. the settings that bound it (self window and cap per active plan)'
    ro "$(cat <<'SQL'
select settings->>'self_window_seconds' window_s, settings->>'self_trigger_cap' cap, count(*) plans
  from runtime.nexloop_plans p
 where tenant_id=current_setting('eios.tenant_id') and world='real'
   and version=(select max(version) from runtime.nexloop_plans q where q.tenant_id=p.tenant_id and q.world=p.world and q.plan_id=p.plan_id)
 group by 1,2
SQL
)"
    ;;
```
