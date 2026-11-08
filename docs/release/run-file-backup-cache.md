# 部署单：文件备份启用 restic 持久缓存（APP_HOST），加探针加固

- **状态**：草稿。执行者：调度员。判定者：L6（方案 D 条件 PASS，2026-11-08）。证据只记耗时、计数和状态，不记仓库地址、口令或 snapshot 内容。
- **依据**：`docs/implementation/host-health-file-backup-scheduled.md` §5、§6。
- **适用**：分支 `l4/host-health-timer-scheduled`。没有迁移，也没有告警规则变化。

## 1. 内容

- 执行器（`nexloop_eios/file_backup.py`）用 `--cache-dir /var/lib/nexloop-file-backup/cache`：restic 的持久缓存，`check` 加 `--with-cache`。
  - 带缓存的命令失败时，先清空缓存，再不带缓存重做一次。
  - 有另一个进程正在用这个缓存时，本轮不用缓存（`cache: busy`），不共享、不等待。
- 缓存目录：
  - 由安装器创建，0700，属 `nexloop-file-backup`；
  - 不在任何备份源（`/srv/nexloop/data/artifacts`、`/srv/nexloop/data/runtime`）或 staging 之内，执行器也会检查这一点，重叠就拒绝运行；
  - **可以随时删除**（`rm -rf /var/lib/nexloop-file-backup/cache/*`）。删了只会让下一轮变慢：建缓存时 repo2 每段约多 10 秒，check 回到不带缓存的耗时。
- repo2 每日维护（`file_backup_maint.py`）：
  - 每天的 `check` 仍然不带缓存（`--no-cache`），和 15 分钟那一轮完全分开；
  - 周日的 `--read-data-subset` 改为按 ISO 周号轮换（第 N/10 份，N = 周号 % 10 + 1），十个周日把全部 pack 读一遍。原来固定是 1/10，每周读的都是同一份。
- repo1 的每日兜底不变：DATA_HOST 的 `nexloop-restic-maint` 每天对文件备份的 repo1 做一次 `--no-cache check`，周日再做一次子集读取（固定 1/10，这次没改）。
- 探针（`host_health_probe.py`）：命令在 `TZ=UTC`、`LC_ALL=C` 下运行，能解析 UTC 的可读时间；单轮比周期长时写一行 `timer_overrun`。

**并发**：
- 15 分钟那一轮的四条 ExecStart 在同一个 oneshot unit 里依次执行，同一时刻只有一个 restic 进程，systemd 也不会让同一个 unit 同时跑两份；
- 每日维护不用缓存；
- 手工运行执行器又恰好撞上定时那一轮时，靠缓存目录里的 flock，后来者不用缓存。

**失败可见**：
- 维护任务有任何一步不是 ok，退出码就是 1，unit 失败。host-health 的 `file-backup-maint`（APP_HOST）和 `restic-maint`（DATA_HOST）都会计为定时失败，`timer_scheduled_failure` 告警会触发。

## 2. 部署（APP_HOST，调度员）

1. 本批的 release 部署后，**重跑文件备份安装器**（与上一次安装时用的私有配置相同，一条命令）。它会：
   - 创建缓存目录；
   - 更新 `nexloop-file-backup.service`（ReadWritePaths 和四条 `--cache-dir`）和 `/etc/nexloop/file-backup.env`（`FILE_BACKUP_CACHE`）；
   - 第 5 步在同样的 sandbox 里自检。
2. 重新安装 host-health 探针（`host_health_install`），换上新版探针。
3. 不需要手工重启：下一次定时运行就会用上新的 unit。

**核对**：

```bash
sudo -n stat -c '%U %a %n' /var/lib/nexloop-file-backup/cache
systemctl show -p ReadWritePaths nexloop-file-backup.service
grep -c -- '--cache-dir' /etc/systemd/system/nexloop-file-backup.service
```

预期：`nexloop-file-backup 700`；两个可写路径分别是 staging 和 cache；`--cache-dir` 出现 4 次。

## 3. 阳性对照（部署后看 2 到 3 轮）

1. **单轮明显短于 15 分钟**：

   ```bash
   journalctl -o short-iso -u nexloop-file-backup.service --since '<部署时间>' | grep -E '"kind": "files_(artifact|runtime)"'
   ```

   - 第一轮要建缓存，`cache` 为 `used`，repo2 两段可能仍有几分钟；
   - 从第二轮起，repo2 每段应在 1 分钟左右，整轮在 3 分钟以内；
   - 结果行里 `cache` 为 `used`；出现 `rebuilt` 时，说明有一次带缓存失败后不带缓存重做了，偶尔一次可以接受，连续出现就要报告。
2. **轮次之间有空闲**：每轮的 Starting 落在 :00/:15/:30/:45 加随机延迟上，而不再紧跟上一轮的 Finished：

   ```bash
   journalctl -o short-iso-precise -u nexloop-file-backup.service --since '<部署时间>' | grep -E 'Starting|Finished'
   ```

3. **host_health 记到成功轮次**（DATA_HOST，`ro`）：

   ```bash
   ro "select run_started_at,result,scheduled from runtime.nexloop_host_timer_runs where host_role='app_host' and timer_name='file-backup' order by run_started_at desc limit 3"
   ```

   预期每轮一行，`result = success`，`scheduled = true`。
4. **告警恢复**：`app_host:file-backup` 的 `timer_success_age_seconds` 降到 900 秒以内，`timer_stale_file_backup` 在下一个评估周期恢复。
5. **探针没有 overrun**：`journalctl -t nexloop-host-health --since '<部署时间>' | grep -c timer_overrun` 应为 0。
6. **第二天早上**：
   - `journalctl -u nexloop-file-backup-maint.service -n 3` 的结果行里，`check` 为 `ok`；
   - 周日那一天，`deep_subset` 是当周的 N/10。

## 4. 回退

1. release 回到上一版并重跑安装器：unit 恢复成不带 `--cache-dir`，执行器回到 `--no-cache`，单轮回到约 19 分钟，定时成功又会记不上（已知状态）。
2. 缓存目录可以留着，也可以删除。卸载安装器时会一并删除。
3. 探针回到上一版：`timer_overrun` 行不再出现，其他行为不变。
