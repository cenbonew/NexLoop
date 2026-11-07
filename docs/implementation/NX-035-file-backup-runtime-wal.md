# APP_HOST 文件备份 files_runtime 一直判 failed：滞留的 WAL（诊断与修法）

- **来源**：调度员 2026-11-08 派工。NX-055 E3 检查（alert-rules v32）报 freshness fail：`app_host:file-backup` 最近一次定时成功约在 47 小时前，24 小时内定时失败 45 次；告警 `timer_stale_file_backup` 从 10-30 起反复触发（fire_count 11）。
- **最近一次输出**：`{"ok": false, "kind": "files_runtime", "verify_status": "failed", "file_count": 110, "listed": 119, "skipped": {"missing": 0, "not_checkpointed": 1, "integrity": 0}, "absent": 8, "restic": "ok", "check": "ok"}`。
- **作者**：L4，基于 main `41684401`。代码修复在分支 `l4/file-backup-runtime-verify`。

## 1. verify 的判定（`nexloop_eios/file_backup.py`）

`status = 'verified' if checked and left_out == 0 else 'failed'`，其中 `left_out = sum(skipped.values())`。

| 计数 | 出现的条件 | 是否导致 failed |
|---|---|---|
| `absent` | 清单里有这个 Run，但 `<runtime-root>/<run_id>` 目录不存在（`lstat` 返回 FileNotFoundError） | **不导致**：它不在 `skipped` 里 |
| `skipped.missing` | 路径存在但不是目录，或者目录里没有 `runtime.sqlite` | 导致 |
| `skipped.not_checkpointed` | 目录里有非空的 `runtime.sqlite-wal`，或者 `-wal` 不是普通文件 | 导致 |
| `skipped.integrity` | 复制出来的副本没通过 `PRAGMA integrity_check` | 导致 |

数量对得上：110（复制成功）+ 1（not_checkpointed）+ 8（absent）= 119（listed）。

**结论**：推测里的“清单有、文件没有，被判成失败”不成立。8 个 absent 只是报告出来，不影响判定。**真正让每一轮都判 failed 的，是那 1 个 `not_checkpointed`**：

- 清单（0194 `authz.nexloop_file_backup_inventory`）认为这个 Run 的所有任务都已终态，“Host 已经关闭存储”；
- 但它的目录里留着一个非空的 WAL。

只要这个 Run 不被 NX-029 清理，它每 15 分钟都会再被列一次、再被跳过一次，所以连续失败、不会自愈。restic 和 check 都是 ok，正好说明问题只出在这一个文件上。

## 2. 哪一边错了

**设计假设**（NX-035 设计 §4.3）：“只备份已终态的 Run，此时 Host 已经关闭存储，WAL 已经 checkpoint”。0194 用“所有绑定任务都已终态”来近似这个条件。

**这个近似不总成立**。PG 里任务终态，不等于 Host 已经正常关闭了 SQLite。下面几种情况都会让 WAL 留下来，而且之后不会再有人去 checkpoint 它：

- Host 进程被杀（OOM、重启、`docker compose up` 重建、SIGKILL），连接没有关闭；
- Run 在 PG 里先被判失败或取消（租约丢失、超时、runtime worker 回收），Host 这边那次 Run 后来被丢弃，没有走正常的关闭路径；
- 重启后的 Host 不会重新打开已经终态的 Run。

所以错的是**清单的假设**（以及执行器把“WAL 非空”一律当成“暂时还在写”）。8 个 absent 那一边没有错：

- NX-029 清理经 Host 的 `/internal/v1/runs/purge` 完成，并且会写 `runtime.nexloop_run_purge_items`，而清单已经排除了有清理记录的 Run；
- 剩下的 absent，最可能是**从来没建过存储**的 Run：在 Pi 创建 `runtime.sqlite` 之前就结束了，比如 Host 准入已满、启动被拒、启动前就失败。

这一点要用 §4 第 5 步核对。

## 3. 从什么时候开始

- journal 只保留约 60 小时，最早能看到的失败是 11-07 04:15；最近一次成功约在 47 小时前。
- 告警从 10-30 起反复触发，说明 10-30 前后就有过同样的情况：每出现一个滞留 WAL 的 Run，失败会一直持续到它被清理为止，期间可能自愈过（那个 Run 被清理后恢复），之后又来了新的。
- 准确的时间线用 §4 第 6 步查：`runtime.nexloop_backup_manifests` 按时间看 `files_runtime` 的 verified 和 failed 交替，再和 Host 的重建时间对照（第 4 步）。

## 4. 在 APP_HOST 上执行的只读命令（调度员执行）

都不写 `/srv` 下的任何文件。凡是要打开 SQLite 的，一律用 `immutable=1`，避免生成或修改 `-shm`。`ro` 是 DATA_HOST 上已有的只读会话（定义同 run-nx057 §0.3）。

1. **最近几轮的输出**（可以看到每一轮的 `not_checkpointed` 和 `absent` 计数）：

   ```bash
   journalctl -u nexloop-file-backup.service --since '2026-11-06' -o short-iso | grep '"kind": "files_runtime"' | tail -n 20
   ```

2. **哪个 Run 留着非空的 WAL**（只列目录名、时间、大小）：

   ```bash
   sudo -n find /srv/nexloop/data/runtime -mindepth 2 -maxdepth 2 -name runtime.sqlite-wal -size +0c -printf '%TY-%Tm-%Td %TH:%TM  %s  %h\n'
   ```

   `sudo -n` 不可用时，用备份执行者的用户执行：`sudo -n -u nexloop-file-backup find …`。它在 sandbox 里对这个目录有读权限。

3. **这个存储现在有没有进程打开**（预期没有。有的话，说明 Host 里还挂着一个僵尸 Run，这时不能复制）：

   ```bash
   docker exec <agent-host 容器> sh -c 'ls -l /proc/[0-9]*/fd 2>/dev/null | grep -c <RUN_ID>'
   sudo -n stat -c '%y %s %n' /srv/nexloop/data/runtime/<RUN_ID>/runtime.sqlite*
   ```

4. **Host 是什么时候被重建或重启的**：

   ```bash
   docker inspect -f '{{.State.StartedAt}} {{.RestartCount}}' <agent-host 容器>
   journalctl --since '2026-10-28' -o short-iso | grep -E 'agent-host.*(Started|Stopped|Killed|OOM)' | tail -n 20
   ```

5. **这个 Run 在 PG 里的状态，以及 8 个 absent 是谁**（DATA_HOST）：

   ```bash
   ro "select b.run_id,j.status,j.updated_at,j.result->>'failure_detail' from authz.nexloop_runtime_run_bindings b join runtime.jobs j on j.tenant_id=b.tenant_id and j.job_id=b.task_id where b.run_id='<RUN_ID>'"
   ro "select b.run_id from authz.nexloop_runtime_run_bindings b where b.world='real'
        and not exists(select 1 from runtime.jobs j where j.tenant_id=b.tenant_id and j.job_id=b.task_id and j.status not in ('succeeded','failed','dead_lettered','cancelled'))
        and not exists(select 1 from runtime.nexloop_run_purge_items i where i.tenant_id=b.tenant_id and i.world=b.world and i.run_id=b.run_id) order by 1" | tail -n +2 > listed.txt
   ```

   然后在 APP_HOST 上：

   ```bash
   sudo -n ls /srv/nexloop/data/runtime | sort > present.txt
   comm -23 listed.txt present.txt
   ```

   得到的就是那 8 个 absent。再对它们各跑一次第一条查询，看 `failure_detail`：预期都是在启动前就结束的 Run。

   这些查询跨租户，`ro` 的 `set_config` 只设一个租户；表有行级安全时，需要对每个租户各跑一次，stage 只有一个租户。

6. **时间线**：

   ```bash
   ro "select date_trunc('hour',completed_at),verify_status,count(*) from runtime.nexloop_backup_manifests where kind='files_runtime' and completed_at>'2026-10-25' group by 1,2 order by 1"
   ```

## 5. 修法（分支 `l4/file-backup-runtime-verify`）

### 5.1 执行器：安静的滞留 WAL 与存储成对复制（已实现）

`stage_runs`：已终态的 Run 如果 WAL 非空，而且数据库和 WAL 两个文件都已经 `WAL_QUIET_SECONDS`（30 分钟）没有变化，就把这一对一起复制，在 staging 里合并：

1. 复制前、复制后，各取一次两个文件的 stat（类型、大小、mtime、inode），必须完全相同；不同就说明还有写入者，照旧计为 `not_checkpointed`。
2. 在 staging 的副本上执行 `PRAGMA wal_checkpoint(TRUNCATE)`，删掉 staging 里的 `-wal` 和 `-shm`；快照里只有一个已合并的 `runtime.sqlite`，然后照常做 `integrity_check`。
3. **不会少复制**：下面两种情况都计为 `integrity`，不会写进快照。理由是 SQLite 遇到读不懂的 WAL 会静默丢弃，而一个至少有一帧的 WAL 一定会改动数据库的某一页。
   - WAL 头部不是 SQLite 的 magic；
   - WAL 超过头部长度，但合并后数据库没有任何变化。
4. 源文件只读，不在源目录做 checkpoint。复制源存储是只读的；在 staging 打开副本会改动的是副本，不是 `/srv`。
5. 结果行新增 `recovered_wal` 计数，它不算跳过。WAL 不到 30 分钟的，仍然计为 `not_checkpointed`：可能还在写，下一轮再看。

这与设计 §4.3 一致：§4.3 禁止的是“复制数据库而丢掉 WAL”，成对复制再合并正好补上这个缺口。活跃 Run 仍然不备份：清单只列终态 Run，执行器还要求文件已经安静、复制前后不变。

测试（`tests/test_file_backup.py`）：

- 用一个真实的、提交了但没有 checkpoint 的 WAL：结果为 verified，`recovered_wal` 为 1，副本里有 WAL 中的行，源文件逐字节不变；
- 一分钟前刚改过的 WAL：仍然 not_checkpointed；
- 读不懂的旧 WAL：integrity，不进快照；
- 复制期间变化的：not_checkpointed。

### 5.2 部署后

- 修好的执行器部署后，下一轮（15 分钟内）预期 `verify_status: verified`、`recovered_wal: 1`、`absent: 8`。
- 如果 §4 第 3 步发现还有进程打开着这个存储，修法不会复制它（文件在变化，或者不到 30 分钟），需要先处理 Host 里那个僵尸 Run。
- 不需要、也不应该手工删除 `/srv` 下的 WAL：那里面是已提交但还没写回数据库的数据。

### 5.3 不建议的做法

- **把 `not_checkpointed` 改成不计失败**：这会让真正还在写的存储悄悄漏备份。
- **在源目录上 checkpoint**：会写 `/srv`，而且可能和 Host 争锁。

## 6. 告警只在工作台可见：工作台外的最小通知（只是建议，不实现）

**现状**：告警写进 `control.nexloop_alert_state` 和 `control.nexloop_alert_events`，只在工作台显示。负责人不打开工作台，就看不到；`timer_stale_file_backup` 从 10-30 起触发了 11 次，没人看到。

**最小方案**：在 APP_HOST 上加一个 systemd timer，每 15 分钟只读一次告警状态，有新的 firing 或者恢复时，发一条工作台外的通知。

1. **读取**：用只读 owner 会话查 `control.nexloop_alert_state where firing`，取 `dedupe_key`、`severity`、`first_fired_at`、`fire_count`。只取规则名和计数，不取任何业务内容。
2. **去重**：本地文件 `/var/lib/nexloop-alert-notify/last.json` 记下已通知过的 `(dedupe_key, first_fired_at)`。只在新出现、恢复，以及 critical 持续超过 24 小时时各发一次，避免刷屏。
3. **通道**（一个就够，由负责人选）：
   - (a) 企业微信或飞书群机器人的 webhook，最简单；
   - (b) 邮件（SMTP 走现有出口）；
   - (c) 负责人手机上的推送服务。

   webhook 地址或口令放 0600 文件，按现有约定放进 Keychain 和 vault 索引，不进仓库。
4. **自检**：通知器本身也是一个 timer，把它加进 NX-055 E3 检查的 freshness 项，和 `file-backup` timer 用同样的检查方式。通知器停了，下一次 E3 检查就能发现。
5. **验收**：人为制造一条 warning（比如在测试租户上把阈值降到必然触发），15 分钟内收到通知；恢复后收到恢复通知；同一条告警持续期间不重复发。

**需要负责人决定**：用哪个通道、通知哪些级别（建议 critical 和 warning 都发，info 不发）、谁来收。

**成本**：一个只读脚本、一个 timer、一个凭据文件，不改告警规则，不改 SQL。
