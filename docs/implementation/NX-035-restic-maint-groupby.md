# NX-035：restic 保留按 tag 分组，以及文件 repo2 的维护

分支 `l7/restic-maint-groupby`，从 main 1a391ee1 切出。依据：L7 在 S3 评估中顺带发现、调度员 2026-10-26 要求在启用文件备份之前修好的问题。

## 1. 问题

- **定义在哪里**：
  - 仓库里是 `deploy/stage/systemd/nexloop-restic-maint.service.example` 和 `.timer.example`；
  - DATA_HOST 上是部署日按 runbook 第 5.1 步由负责人手工复制的 `/etc/systemd/system/nexloop-restic-maint.service`，路径可能已按主机改过；
  - 环境文件是 `/etc/nexloop/restic.env`；
  - 没有安装器管理它，也没有别的脚本。
- **它做什么**：每天 03:30，对 repo1（本机路径）和恢复日志的 repo2（TOS `nexloop/restic-journal/`）各执行一次 `forget --keep-within 14d --keep-monthly 1 --prune` 加 `check`，周日再加 `check --read-data-subset 1/10`。
- **缺陷**：
  - forget 没有 `--group-by`，restic 默认按 `host,paths` 分组。
  - repo1 里将有 APP_HOST 的文件快照（`files_artifact`、`files_runtime`）。每次文件快照的路径都是一个新的暂存目录 `stage-<uuid>`，所以每个快照自成一组。
  - `--keep-within 14d` 是相对组内最新快照计算的，于是每组那唯一的一个快照永远保留，快照无限增长：每 15 分钟一次，每类每天约 96 个，一年约 7 万个。数据会去重，但快照、索引和 `check` 的开销都会一直增长。
  - 恢复日志的快照路径固定，不受影响。
- **文件的 repo2**：TOS `nexloop/restic-files/`，口令和子用户密钥都在 APP_HOST 上，目前**没有任何维护任务**。DATA_HOST 的 restic-maint 管的 repo2 是恢复日志那个仓库。
- 文件备份到 10-26 为止从未启用，所以 repo1 和文件 repo2 里还没有积压，现在修不用清理。

## 2. 修法：按 tag 分组

两条 forget 都改为：

```
forget --retry-lock 10m --group-by host,tags --keep-within 14d --keep-monthly 1 --prune
```

- 恢复日志（tag `journal`，路径固定）与文件快照的两类各成一组，各自保留 D4 的窗口：14 天内的全部快照，再加 1 个月度快照，最长 35 天（backup.v1.json `retention`）。
- 文件快照每 15 分钟一次，14 天约 1344 个快照，但它们之间几乎完全去重。如果以后嫌快照太多，可以改成：

  ```
  --keep-within 2d --keep-daily 14 --keep-monthly 1
  ```

  这样不缩短窗口，只降低旧快照的密度。这属于保留策略的选择，要负责人决定，本次不改。
- 保留 host：主机名始终是 `nexloop`，留着它只是为了将来真有第二台主机时不混在一起。
- `--retry-lock 10m`（L6 S2）：prune 要拿排他锁。APP_HOST 的文件备份每 15 分钟、恢复日志备份每 5 分钟各拿一次共享锁；撞上时，每日维护等对方放锁，最多 10 分钟，而不是直接失败。反过来，prune 持锁期间开始的备份仍会失败，该轮记为 failed，下一轮补上。执行者是否也加 `--retry-lock`，留作后续项。

## 3. 仓库内的改动

- unit 示例的两条 forget 行，以及说明注释。
- `tests/test_stage_systemd_units.py`：断言两条 forget 都带 `--retry-lock 10m --group-by host,tags`，并且只有这两条。
- runbook §3.3 的保留一行。`deploy/stage/systemd/README.md` 的 restic-maint 一行要等第三十六批合入后再改：它与文件备份那一行相邻，现在改会冲突。
- **在 DATA_HOST 上应用**：`scripts/deploy/restic_maint_data_host.sh`，由负责人以 root 执行，一条命令，不需要配置文件，也不读任何秘密。
  - 不替换已安装的 unit，因为它的路径可能是主机自己的；只在 forget 行里把 `forget --keep-within 14d --keep-monthly 1 --prune` 改成 §2 的版本。forget 行必须正好是 1–2 条（每个仓库一条），预期的文字也不能出现在别处，否则拒绝，请手工修改。
  - unit 文件和它上面的每一级目录都必须属于 root、组和其他人不可写，路径上不能有链接；备份文件也不能是链接。
  - **先预演**：每条 forget 把 `--prune` 换成 `--dry-run --json`，用 `systemd-run` 在 unit 自己的沙箱里执行（同一用户、同一 EnvironmentFile），打印每组的 host、tags、保留数和删除数，不打印快照路径或仓库位置。
    - 预演显式传 `--expand-environment=yes`（L6 S1），让 unit 里 `${VAR}` 形式的路径与正式运行时一样展开，不依赖 systemd-run 的默认行为。
    - **版本前提：systemd 254 或以上**。更老的 systemd-run 不认识这个选项，会直接报错，于是预演失败，什么都不写，不会出现"没展开就跑"的情况。
    - DATA_HOST 的 systemd 版本我无法核对：线路规则不允许我连接这台主机，本地证据里也没有记录。按版本锁，主机是 Ubuntu 26.04，其 systemd 远高于 254，但这只是推断。执行前请负责人或调度员只读核对一次：`systemctl --version | head -1`。
    - 预演要求 `systemd-run` 的退出码为 0，并且最后一行是 restic 的 JSON（L6 S3）。任一条件不满足，就什么都不写。
  - 然后原子改写 unit：旧文本保存为 `.before-groupby`，再执行 `systemctl daemon-reload`。
  - `--revert` 改回原来的写法。
  - 测试：`tests/test_restic_maint_data_host.py`；变异验证 14 个，加上这次修改 S1–S3 的 5 个，全部杀死。
- **授权：安装时要负责人本人确认。** `restic_maint_data_host.sh` 是一个新的 DATA_HOST root 脚本，不在负责人给调度员的 root 授权范围内；那份授权只覆盖缺口核对安装器。
  - 运行步骤：
    1. 只读核对 `systemctl --version | head -1`，确认版本 ≥ 254；
    2. 负责人本人确认后，执行 `sudo bash scripts/deploy/restic_maint_data_host.sh`；
    3. 看预演打印的每组保留数和删除数，再看第 3 步是否完成；
    4. 次日 03:30 之后，确认 `restic-maint` 的定时轮次成功（host-health 的 `timer_stale_restic_maint` 不触发）。
  - 也可以并进 `file_backup_data_host.sh`（第 1 步，本来就由负责人以 root 执行），但那个分支在第三十六批，所以先单独交付。

## 4. 文件 repo2 的维护：谁、在哪台主机

| 做法 | 秘密 | 监控 | 将来做只追加时 |
|---|---|---|---|
| **甲（推荐）**：APP_HOST 上新增 `nexloop-file-backup-maint.service` 加 timer（每天），以 `nexloop-file-backup` 身份在同样的 D3 沙箱里运行。对文件 repo2 执行 `forget --retry-lock 10m --group-by host,tags --keep-within 14d --keep-monthly 1 --prune`、`check`，每周加深度 check。由 `file_backup_install` 安装：第 4 步装 unit，第 6 步启用并写入 host-health | 不挪动：口令和子用户密钥本来就在 APP_HOST | host-health 加 `file-backup-maint`。`timer_scheduled_failure` 和 `host_unit_missing` 是通配规则，自动覆盖；新鲜度规则要在下一个规则版本里加一条 `timer_stale_file_backup_maint`（26 小时） | 如果以后让 APP_HOST 的 TOS 子用户不能删除（S3 评估中的 E），维护就要迁到 DATA_HOST |
| 乙：并进 DATA_HOST 的 restic-maint | 要把文件 repo2 的口令放到 DATA_HOST；还需要负责人在火山引擎控制台把 DATA_HOST 子用户的策略扩到 `nexloop/restic-files/`。restic.env 只能放一组 AWS 键，不能同时放 APP_HOST 子用户的密钥 | 现有的 `timer_stale_restic_maint` 已覆盖 | 维护已经在 DATA_HOST，与"APP_HOST 只追加"方向一致 |

**已定为甲，并已实现**（调度员 2026-10-26，分支 `l7/file-backup-maint`；见 `NX-035-file-backup.md` §6.4）。以下为当时的推荐理由：秘密不扩散，也不需要负责人改控制台策略。改动在 `file_backup_install`（第三十六批）和告警规则（v20 正在 L6 审）上，所以我准备等这两项合入之后作为下一个分片接着做：unit 和 timer、安装器第 4/6 步、host-health 条目、规则 v21、测试。如果负责人选乙，我改为在本分支补 restic-maint 的行，并在 `install_repo2_secrets.sh` 增加一个 DATA_HOST 角色。

**启用顺序**：DATA_HOST 应用本修复，并且文件 repo2 的维护（甲或乙）就位之后，再启用文件备份的 timer。
