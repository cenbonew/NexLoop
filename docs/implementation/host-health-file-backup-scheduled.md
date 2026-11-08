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

## 5. 调度员核对的结果（2026-11-08）与结论

| 项 | 结果 |
|---|---|
| timer 与 service | `LastTriggerUSec` 可以从 bus 读到；service 处于 `activating`，`InactiveExitTimestamp = StateChangeTimestamp`（本轮刚开始） |
| 开始与结束 | **情况 A 已确认**：每轮的 Starting 都紧跟在上一轮 Finished 之后，相隔约 10 毫秒（07:41:43.349→.359，08:00:47.948→.956，08:20:16.069→.080） |
| 08:00:47 那一轮各段耗时 | artifact repo1 约 36 秒；artifact repo2 约 9 分钟（file_count 0）；runtime repo1 约 36 秒；runtime repo2 约 9 分钟 |
| 探针 | 没有 trigger 解析失败。最初报的“28 次”是**误报**：grep 数到的是 JSON 键名，各行实际都是 `"trigger_unparsed": []`（调度员 2026-11-08 更正） |
| `nexloop_host_timer_runs` | 最后一条记录是 2026-11-07T22:15:45Z 开始的一轮（failure，scheduled）。R71 之后**一条都没有**：故障期间每轮约 10 分钟，在周期之内，所以那时能被认作定时运行 |

**这次比 §2 多查出一层：成功的几轮根本没被记录，而不只是“被记成手工运行”**。
- 每轮背靠背地开始，service 在探针每分钟一次的采样里**总是** `activating`。
- 探针只在 service 回到 `inactive` 或 `failed` 之后才记录一轮，所以一轮都记不下来。
- 即使记下来了，开始时间也不在 trigger 后 5 秒内（情况 A），同样不算定时运行。

### 5.1 repo2 为什么慢

两段 repo2 各约 9 分钟，而 artifact 那一段一个文件都没有，也要 9 分钟。所以时间花在每次调用 restic 的**固定开销**上，而不是上传量：

- 执行器对每个 restic 命令都带 `--no-cache`（`file_backup.py` 的 `_restic`）。没有缓存时，`backup` 每次都要从 TOS 下载全部索引文件；`check` 也要重新下载索引，列出 `data/` 下的全部对象，并读取所有快照引用到的 tree blob。
- 对象存储上，这些都是大量小请求和分页列举，延迟累加起来就是这 9 分钟；repo1 走 DATA_HOST 的 rest-server，延迟低，只要 36 秒。
- **没有更轻的 check**：`restic check` 不带 `--read-data` 时已经只查结构，是最轻的模式。能省时间的是 **缓存**：
  - 去掉 `--no-cache`，用一个持久的缓存目录（systemd `CacheDirectory=nexloop-file-backup`，0700，属备份用户）；
  - `check` 加 `--with-cache`。默认情况下，check 会新建一个临时缓存，等于没有缓存。
- restic 的缓存里存的是仓库文件的原样，也就是**加密后的**索引、快照和 tree 包，不含明文。仓库代码和文档里没有找到当初选 `--no-cache` 的理由；推测是因为 sandbox 里备份用户的 home 不可写，用 `CacheDirectory` 就能解决这个问题。

### 5.2 三个方案的比较

| | A. check 挪到每日维护 | B. repo2 拆成每小时一次的独立 timer | C. 周期改为 30 分钟 | **D. 启用 restic 缓存（新增，推荐）** |
|---|---|---|---|---|
| 单轮时长 | 少了 check，但 repo2 的 backup 仍要下载全部索引，估计每段仍要几分钟 | 15 分钟这一轮只剩 repo1，约 1–2 分钟；repo2 每小时一轮，约 18 分钟 | 不变，约 19 分钟，小于 30 分钟，能被认作定时运行 | 预计 repo2 每段降到 1–2 分钟（第一次运行时要建缓存，仍然慢） |
| RPO | 不变（15 分钟） | repo1 仍是 15 分钟；**repo2（异地）变成 1 小时** | **全部变成 30 分钟** | 不变（15 分钟） |
| 设计变化 | “每轮 verified 依赖 check”（NX-035 §4.3）放松为“每日 check”；需要 **L6 判** | 异地副本的 RPO 变化，需要**负责人决定** | NX-035 D2 定下的 RPO 15 分钟变化，需要**负责人决定**；告警阈值也要改 | 无：仍然每轮 check，只是读缓存；缓存是加密的，需要 **L6 确认** |
| 改动量 | 执行器和维护任务 | 两个 unit、installer、host-health 加一个 timer key、告警规则加一条 | timer 和告警阈值（新规则版本） | 执行器加 `--cache-dir`，check 加 `--with-cache`；unit 加 `CacheDirectory` |
| 风险 | TOS 上的问题要到每日维护才发现 | repo2 的失败晚一小时才看到 | 无 | 缓存损坏时 restic 会自己重建；缓存所在磁盘需要几百 MB |

**推荐 D**：不改 RPO，不改“每轮 check”的设计，只消除每次调用 restic 的固定开销。

- 先在 APP_HOST 上只读测一次：以备份用户执行 `restic snapshots` 预热缓存，再对 repo2 计时一次 `restic check --with-cache`。两条命令都只读仓库；缓存写在本机，不写 `/srv`。
- 如果带缓存后一轮仍然超过 10 分钟，再在 B 和 C 之间请负责人决定。
- A 不推荐：它放松了现有设计，而且只省掉一半的时间。

### 5.3 已经提交的代码修复（探针）

1. **`trigger_unparsed`**：探针调用 `systemctl`、`busctl` 和 `docker` 时，环境变量设为 `TZ=UTC`、`LC_ALL=C`。
   - 这样 busctl 读不到时，systemctl 输出的可读时间是 `Sun 2026-11-08 00:15:00 UTC`，`last_trigger` 能按 UTC 解析；其他时区仍然不猜测，照样报 unparsed。
   - stage 上其实没有解析失败（见上表，“28 次”是误报）。这一项保留下来作为加固：以后 bus 读不到时，不会因为时区缩写或本地语言而报 unparsed。
2. **`timer_overrun`**（新的本地告警行，不改 SQL）：service 还在运行，而 timer 的上一次 trigger 晚于本轮开始时，说明这一轮比周期长，探针写一行 `<3>nexloop-host-health timer_overrun host=… timer=…`。以后这种“永远不计入”的情况会直接在 journal 里看到，不需要再从 freshness 倒推。
3. 测试：
   - UTC 可读时间能解析，其他时区仍是 unparsed；
   - 命令以 UTC 和 C locale 运行；
   - 跨周期的一轮报 overrun，周期内的一轮不报。

## 6. 方案 D 的实测与实现

**实测**（调度员 2026-11-08，APP_HOST，repo2）：

- 以 root 身份临时执行。原因是口令文件只在 unit 的 sandbox 里可读；仓库一直被背靠背的备份锁着，所以都加了 `--no-lock`；缓存放在临时目录，跑完已删除。
- 结果：

  | 命令 | 不带缓存 | 第一次建缓存 | 有缓存后 |
  |---|---|---|---|
  | `snapshots` | 10 秒 | 10 秒 | 2 秒 |
  | `check` | 478 秒 | — | 24 秒（`--with-cache`） |

- 缓存大小 60 MB。按这个算，repo2 每段从约 9 分钟降到 1 分钟以内，单轮远低于 15 分钟。

**实现**（等 L6 确认缓存的安全性后再上线）：

- **执行器** `nexloop_eios/file_backup.py`：新增 `--cache-dir`。
  - 给了这个参数时，所有 restic 调用都用这个缓存目录，`check` 加 `--with-cache`；不给时和原来一样，用 `--no-cache`。
  - 缓存目录必须是调用者自己的 0700 目录，否则拒绝运行。
  - 结果行新增 `cache` 字段，取值为 `used`、`rebuilt` 或 `off`。
- **缓存损坏不会导致每轮失败**：
  - 带缓存的 `backup` 或 `check` 一旦失败，就清空缓存目录（只清内容，不删目录本身），再不带缓存把同一个命令重做一次，结果行记 `cache: rebuilt`。下一轮会重新建缓存。
  - 真正的仓库问题在不带缓存重做时仍然失败，照常记为 failed，不会被掩盖。
  - 不依赖 restic 自己能否发现缓存损坏：restic 校验缓存里的文件时，能否自动改从后端读取，各版本行为不一，所以执行器自己兜底。
- **unit 与安装器**：
  - 缓存目录是 `/var/lib/nexloop-file-backup/cache`，与 staging 并列，由安装器创建（0700，属 `nexloop-file-backup`）；
  - unit 的 `ReadWritePaths` 加上它；`/etc/nexloop/file-backup.env` 新增 `FILE_BACKUP_CACHE`；四条 ExecStart 都加 `--cache-dir ${FILE_BACKUP_CACHE}`；
  - `--uninstall` 时与 staging 一样整棵删除（不跟随链接）。
- **为什么用 `ReadWritePaths` 加安装器建目录，而不用 systemd 的 `CacheDirectory=`**：
  - 两者都能让 `ProtectSystem=strict` 的 sandbox 可写；
  - 但安装器第 5 步要用 `systemd-run -p …` 在同样的 sandbox 里自检，也要负责卸载。沿用 staging 的做法，自检、卸载和“目录不能是链接”的检查都能同样覆盖缓存目录；
  - `CacheDirectory=` 会落在 `/var/cache`，那是另一套生命周期，安装器和卸载都还要另外处理。
- **安全性**：
  - 缓存里存的是仓库文件的原样，即 restic 加密后的索引、快照和 tree 包，没有明文；
  - 目录 0700，只有备份用户能读写；
  - 仓库口令仍只在 sandbox 里可读。

**测试**：

- `tests/test_file_backup.py`：
  - 带缓存时 backup 和 check 都用缓存，check 带 `--with-cache`，缓存保留到下一轮；
  - 不给缓存目录时行为不变；
  - 缓存损坏时，backup 那一步和 check 那一步各一个用例：都会清空缓存、不带缓存重做，结果 verified、`cache: rebuilt`；
  - 真正的 check 失败重做后仍然 failed；
  - 缓存目录不是 0700 时拒绝运行。
- `tests/test_file_backup_install.py`：unit 的 `ReadWritePaths`、四条 `--cache-dir`、安装器建目录、卸载时删除。
- 测试用的 restic 替身（`tests/support/restic_stand_in.py`）现在也接受 `--cache-dir`，并能模拟“带缓存才失败”的情况。
