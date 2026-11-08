# host_health 不计 file-backup 的“定时成功”：诊断

- **来源**：调度员 2026-11-08 派工。文件备份已经恢复（从 06:55 起每轮 `Result=success`），但 `app_host:file-backup` 的 `timer_success_age_seconds` 一直在涨（07:27 为 172641，08:24 为 176066），`timer_scheduled_failures_24h` 一直是 45。同一台机器上的 `provider-drift` 和 `file-backup-maint` 正常。
- **作者**：L4，基于批次 133 `d1a3c43c`，分支 `l4/host-health-timer-scheduled`。只读分析，**还没有改代码**：修法取决于下面第 3 节命令的结果。

## 1. “定时成功”是怎么判定的

**探针**（`scripts/deploy/host_health_probe.py` 中的 `sample_timers`，每分钟一次）：

- 读 timer 的 `LastTriggerUSec`（经 busctl，单位是微秒），以及 service 的 `ActiveState`、`Result`、`InactiveExitTimestamp`（本轮开始）和 `StateChangeTimestamp`（本轮结束）。
- service 还在 `activating` 时不记录。结束后记录一轮：`result = success`，当且仅当 `Result=success` 并且 `ActiveState=inactive`。
- **`scheduled` 当且仅当本轮开始时间减去 timer 的 `LastTrigger` 在 −1 到 `grace_seconds`（默认 5 秒）之间。**

**SQL**（0318 `authz.nexloop_record_host_observation`）：

- 只有探针给出 scheduled、开始时间不早于 LastTrigger 前 1 秒，并且这是这个 trigger 记录的第一轮时，才算 scheduled；
- `timer_success_age_seconds` 从最后一次“scheduled 并且 success”的结束时间算起。

不看 `InvocationID`，也不看 `ExecMainExitTimestamp`。设计意图是：手工执行 `systemctl start` 永远不刷新 freshness。

## 2. 为什么这些成功没被记上

一轮大约 19 分钟，比 15 分钟的周期长，所以每一轮都跨过下一个整点：

- **情况 A**（预计如此，需要第 3 步确认）：timer 到点时 service 还在 `activating`。systemd 照样把这次到点记为 trigger（`LastTriggerUSec` 前移），但不会再起一个实例：这次启动请求与正在运行的 job 合并，或者排在它后面，等它结束后立刻执行。
  - 如果排在后面，下一轮就在上一轮结束的那一刻开始。实测的开始时间 06:45、07:03、07:22、07:41、08:00、08:20 都不在 15 分钟的整点上，间隔约 19 分钟，正是背靠背：上一轮一结束，下一轮马上开始。
  - 这时 `本轮开始 − LastTrigger` 是好几分钟，远大于 5 秒，所以被判成不是定时运行，等同于手工运行。
- **情况 B**：探针在本轮结束后读 `LastTrigger` 时，它已经被这一轮运行期间的下一次到点刷新过，比本轮开始还晚，差值为负，同样不算 scheduled。

两种情况结论一样：**单轮时长超过周期时，没有一轮能满足“开始时间在 trigger 后 5 秒内”**。
- 这和 Persistent 补跑无关：Persistent 只在 timer 重新启用时，补跑一次错过的到点。
- `provider-drift` 每小时一次、`file-backup-maint` 每天一次，单轮都远短于周期，所以不受影响。
- `timer_scheduled_failures_24h = 45` 是上次故障期间那 45 次定时失败，24 小时窗口滑过去后会自己下降。它不再增加，说明此后的成功轮也都没被算作“定时”。

## 3. 在 APP_HOST 上执行的只读命令（调度员）

1. **timer 与 service 的时间点**（微秒值都换算成 UTC 比较）：

   ```bash
   busctl get-property org.freedesktop.systemd1 /org/freedesktop/systemd1/unit/nexloop_2dfile_2dbackup_2etimer org.freedesktop.systemd1.Timer LastTriggerUSec
   systemctl show --timestamp=unix -p ActiveState,Result,InactiveExitTimestamp,StateChangeTimestamp,ExecMainStartTimestamp,ExecMainExitTimestamp nexloop-file-backup.service
   systemctl list-timers nexloop-file-backup.timer --all
   ```

2. **每次到点和每轮的开始、结束**（区分情况 A 和情况 B）：

   ```bash
   journalctl -o short-iso-precise --since '2026-11-08 06:40' -u nexloop-file-backup.timer -u nexloop-file-backup.service | grep -E 'Starting|Started|Finished|Deactivated|Succeeded|Triggered|Main process exited'
   ```

   预期：每轮的 `Starting` 紧跟在上一轮 `Finished` 之后，间隔不到 1 秒，而不在 :00/:15/:30/:45 加随机延迟的时刻；这就是情况 A。

3. **单轮里四段各用了多久**（缩短单轮时长时要知道慢在哪一段）：

   ```bash
   journalctl -o short-iso -u nexloop-file-backup.service --since '2026-11-08 07:40' --until '2026-11-08 08:21' | grep -E '"kind": "files_(artifact|runtime)"'
   ```

   这里看的是四行结果各自的时间戳：artifact repo1 → artifact repo2 → runtime repo1 → runtime repo2，相邻两行之差就是后一段的时长。每段里都有一次 `restic check`。

4. **探针看到了什么**：

   ```bash
   journalctl -o cat -u nexloop-host-health.service --since '2026-11-08 08:00' | grep -c trigger_unparsed
   ```

   预期为 0。如果不为 0，说明是 trigger 解析的问题，与上面的分析无关。

   DATA_HOST 上用 `ro` 看记录下来的几轮：

   ```bash
   ro "select run_started_at,run_finished_at,result,scheduled,last_trigger_at from runtime.nexloop_host_timer_runs where host_role='app_host' and timer_name='file-backup' order by run_started_at desc limit 8"
   ```

   预期：`scheduled` 全为 false；`last_trigger_at` 比 `run_started_at` 早好几分钟（情况 A），或者更晚（情况 B）。

## 4. 修法（待第 3 节结果确认后实施）

根本原因是**部署节奏不对**：单轮时长超过了周期。探针要求“开始时间在 trigger 后 5 秒内”是为了不让手工运行刷新 freshness，这个规则本身没有错。三个方案，推荐按顺序考虑：

1. **缩短单轮时长，保持 15 分钟**（推荐，RPO 不变）：
   - 先用第 3 节第 3 步找出慢的段。预计是 repo2（对象存储）那两段，以及每段一次的 `restic check`。
   - 可选的做法：每轮的 `restic check` 只查结构，不读数据（现在已经是这样）；或者 check 不再每轮做、只在每日维护里做。后者改变了“每轮 verified 依赖 check”的设计（NX-035 §4.3），需要 L6 判定。
   - 另一种做法是把 repo2 两段移到一个单独的 timer（例如每小时一次）。这会让 repo2 的 RPO 变成 1 小时，需要负责人决定。
2. **把周期放宽到 30 分钟**（部署侧，最快）：
   - timer 改为 `OnCalendar=*:0/30`；
   - `timer_stale_file_backup` 的阈值从 2700 秒改为 5400 秒（三个周期），要出一个新的规则版本，编号接在告警通知的 v34 之后，由调度员排号；
   - **这改变了 NX-035 D2 定下的“文件 RPO 15 分钟”**，需要负责人确认。`file_backup_age` 和 `file_backup_unprotected` 的 1 小时阈值不用改，但余量会变小。
3. **探针接受“排队后紧接上一轮开始”的那一轮**（只在第 3 节确认是情况 A 时考虑）：
   - 探针在 `state_file` 里记住每个 timer 上一轮的开始和结束时间；如果 trigger 落在上一轮运行期间，而本轮在上一轮结束后 `grace` 秒内开始，就把本轮算作那次 trigger 的定时运行；
   - 代价：上一轮刚结束时恰好有人手工 `systemctl start`，会被误算成定时运行；
   - 只解决“看不见”，不解决“单轮比周期长”。备份实际上是背靠背在跑，没有任何空闲，RPO 已经是单轮时长，而不是 15 分钟。

**建议**：第 3 节结果出来后，先做方案 1（找出慢段）。方案 2 作为负责人确认 RPO 之后的快速办法。方案 3 不单独做，因为它会掩盖节奏问题。告警评估这边不需要改。
