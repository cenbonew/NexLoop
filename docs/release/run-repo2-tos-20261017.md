# 命令单：repo2 异地备份接入火山引擎 TOS（2026-10-17 起，交互结束后执行）

**方案与依据**：[NX-035-repo2-tos](../implementation/NX-035-repo2-tos.md)（上游 NEX-EIOS 的 TOS 对接方法，以及按 NexLoop 条件做的调整）。只用角色名和变量。

**类别标注**：
- **B**：需负责人确认；
- **sudo-A**：需要 APP_HOST 的 sudo；
- **sudo-D**：需要 DATA_HOST 的 sudo。

**时机**：
- 交互期间不做；
- 第 2–3 步会让 WAL 开始推到 repo2，首次全量会占用上传带宽，选在低峰时做；
- 第 7 步的断网演练只在维护窗口做。

## 0. 负责人需要提供（全部 B）

| 项 | 说明 | 保存位置 |
|---|---|---|
| TOS 桶 | **新建、专用**，不与任何业务桶共用；只写桶名 | 私有 env（`__TOS_BUCKET__`） |
| region | 例如 `cn-shanghai`；S3 端点为 `tos-s3-<region>.volces.com` | 私有 env（`__TOS_REGION__`） |
| 版本控制 | 建议**开启**，历史版本 14 天后过期（误删或恶意删除可在 14 天内找回）；不开启也可以（方案 §3.1） | 负责人决定，记录在变更记录中 |
| 对象锁（WORM） | 建议**不开启**：开启后 pgBackRest expire 和 restic prune 删不掉过期对象 | 同上 |
| 两个子用户的 AK/SK | DATA_HOST 子用户（`nexloop/pgbackrest/`、`nexloop/restic-journal/`），APP_HOST 子用户（`nexloop/restic-files/`）；只读的恢复子用户可选 | 钥匙串 `nexloop/backup/tos-data-access-key`、`tos-data-secret-key`、`tos-app-access-key`、`tos-app-secret-key`（及恢复子用户的） |
| 三个客户端口令 | pgBackRest repo2 cipher、restic journal repo2、restic files repo2，各自 32 位以上随机串，与 repo1 不同 | 钥匙串 `nexloop/backup/repo2-cipher-pass`、`restic-repo2-journal`、`restic-repo2-files` |

主账号的 AK **不得**用于备份。

## 1. TOS 控制台：建桶与治理（负责人本人；B）

1. 建桶：私有 ACL，地域为上面的 region，默认加密 SSE-TOS（AES256），版本控制和对象锁按 §0 的决定。
2. 桶策略：按 `deploy/examples/tos/bucket-policy.example.json` 配置，只允许 TLS。注意使用 TOS 的写法：`"Version": "1.0"`、`tos:securetransport`，照抄 AWS 写法会被拒（上游实测）。
3. 生命周期：按 `deploy/examples/tos/lifecycle.example.json` 配置：
   - 未完成的分段上传 1 天后终止；
   - 开启了版本控制的，历史版本 14 天后过期；
   - **不要**给 `nexloop/` 设任何过期规则，否则会把仓库删坏。
4. 子用户与策略：按 `deploy/examples/tos/iam-policy-data-host.example.json` 配置：
   - 列举动作写 `tos:ListObjects`，前缀条件键写全小写的 `tos:prefix`；
   - 分段上传相关的动作名，先对照 TOS 的 IAM 动作列表；
   - APP_HOST 子用户只限 `nexloop/restic-files/`；
   - 为两个子用户各生成一对 AK/SK，直接存入钥匙串，不经剪贴板历史，也不写进任何文件。
5. **核对清单**：每月复核一次，代替上游的每日自动审计：
   - ACL 为 private，匿名访问为空；
   - 策略只拒绝非 TLS 请求；
   - 默认加密为 AES256；
   - 版本控制的状态与决定一致；
   - 生命周期只有上面的规则；
   - 两个子用户的权限只覆盖各自的前缀；
   - 没有主账号的 AK 在使用中。

## 2. DATA_HOST：pgBackRest repo2（**B**，sudo-D）

### 2.1 把片段写进配置

```
C=$NEXLOOP_ROOT/secrets/pgbackrest/pgbackrest.conf
sudo cp -p "$C" "$C.before-repo2"
sudoedit "$C"     # 在 [global] 段末尾加入 deploy/stage/config/pgbackrest-repo2-tos.conf.example 的全部设置行，再填入 __X__
sudo grep -c '__' "$C"; sudo stat -c '%a %u' "$C"
```

- **预期**：第一个数为 `0`（没有占位符残留）；权限为 `600 999`。
- **口令和密钥的填写**：在 sudoedit 中由负责人从钥匙串粘贴。不要用 `echo`、命令行参数或 shell 变量传递。
- **撤销**：`sudo install -o 999 -g 999 -m 0600 "$C.before-repo2" "$C"`。

### 2.2 连通性与 stanza

```
sudo docker exec -u postgres nexloop-data-postgres-1 pgbackrest --stanza=nexloop --repo=2 stanza-create
sudo docker exec -u postgres nexloop-data-postgres-1 pgbackrest --stanza=nexloop check
sudo docker exec -u postgres nexloop-data-postgres-1 pgbackrest --stanza=nexloop --repo=2 info
```

- **出处**：pgBackRest 的命令，形态同 NX-035 部署手册 §2.3、§2.4（`docker exec -u postgres <PG 容器>`）。
- **预期**：
  - `stanza-create` 输出 `completed successfully`；
  - `check` 对 repo1 和 repo2 都通过（它会推一个 WAL 段到两个仓库并确认）；
  - `info` 显示 repo2 的 stanza 状态为 ok，暂时没有备份。
- **从这一刻起，WAL 会推到两个仓库。**
- **失败时**：
  - HTTP、TLS 或权限错误：核对 region、端点、桶名、AK 和子用户的策略；
  - 签名或寻址错误：核对 `repo2-s3-uri-style`（片段中为 `host`）；
  - 不能排除时，执行 2.1 的撤销，恢复为只有 repo1，把错误码报给调度员。
  - **不要**把 `repo2-storage-verify-tls` 改为 n。

## 3. 首次全量与校验（**B**，DATA_HOST）

```
sudo -u nexloop-backup /opt/nexloop/backup-venv/bin/nexloop-backup pg --recorder-dsn-file "$NEXLOOP_ROOT/secrets/backup/recorder.dsn" \
  --pgbackrest-command-file "$PGBACKREST_COMMAND_JSON" --stanza nexloop --expected-version "$PGBACKREST_VERSION" --repo repo2 --kind pg_full
```

- **出处**：`backup.backup_main` 的 `pg` 子命令；`--repo` 可选 repo1 或 repo2，执行器以 `--repo=2` 依次执行 backup、verify、info。形态同 NX-035 部署手册 §2.5。
- **预期**：`ok: true`，`verify_status: verified`。清单 `runtime.nexloop_backup_manifests` 中多一行 repo2 的 `pg_full`，状态为 `verified`。
- **耗时**：取决于数据库大小和上传带宽。先用 `pgbackrest --repo=1 info` 看最近一次 full 的大小，再估算。

## 4. restic repo2：journal（DATA_HOST）与 files（APP_HOST）（**B**，sudo-D、sudo-A）

1. **env 文件**：
   - DATA_HOST：按 `deploy/stage/config/restic-repo2.env.example` 写 `/etc/nexloop/restic-repo2.env`（root:nexloop-backup，0600），并在 `/etc/nexloop/restic.env` 中补上 repo2 的三项 AWS 变量和两个文件路径（nexloop-restic-maint 使用）；
   - APP_HOST：按 `deploy/stage/config/file-backup-repo2.env.example` 写 file backup 的 env 文件（属主为 nexloop-file-backup 用户，0600）。
   - 文件中的 repository 是 `s3:https://tos-s3-<region>.volces.com/<bucket>/nexloop/restic-journal`（APP_HOST 为 `…/restic-files`），写在 repository 文件里；口令写在 password 文件里，都是 0600。
2. **初始化并验证**（每台主机各一次）：
   ```
   sudo -u nexloop-backup sh -c 'set -a; . /etc/nexloop/restic-repo2.env; set +a; exec restic --no-cache --repository-file "$RESTIC_REPOSITORY_FILE" --password-file "$RESTIC_PASSWORD_FILE" init'
   ```
   - 预期输出 `created restic repository`。之后执行一次 `snapshots`，预期为空列表。
   - APP_HOST 用同样的形态，换成它自己的 env 文件和 nexloop-file-backup 用户。
   - **如果 `init` 报 bucket 或寻址错误**：可能是 TOS 不接受 restic 默认的 path 风格。先用同一条命令加 `-o s3.bucket-lookup=dns` 再试一次：
     - 成功了：说明需要这个选项，但现有 unit 和 `file_backup` 还不能传它。**停在这里**，报调度员走代码改动，方案 §5 已列为后续项；
     - 仍然失败：核对 AK 和策略。
3. **首次备份**：
   - DATA_HOST：`sudo systemctl start nexloop-journal-restic@repo2`；
   - APP_HOST：按 nexloop-file-backup 中 repo2 的那两行，手动执行一次 artifact 和 runtime 的备份；
   - **预期**：`journalctl` 中没有错误；`restic snapshots` 中有快照；清单中有 repo2 的 `files_*` 行，状态为 `verified`。

## 5. 定时作业（**B**，sudo-D、sudo-A）

- **pg-full、pg-diff**：确认已安装的 unit 是仓库中“repo1 然后 repo2”两条 `ExecStart` 的版本：
  ```
  systemctl cat nexloop-pg-diff | grep -c -- '--repo repo2'
  ```
  预期 `1`。如果为 0，从发布提交的 `deploy/stage/systemd/` 复制 `.example` 文件替换，然后 `daemon-reload`。
- **journal**：`sudo systemctl enable --now nexloop-journal-restic@repo2.timer`（每 15 分钟）。
- **restic-maint**：确认 unit 中有 repo2 的 forget、prune、check 几行，以及 `restic.env` 中有 repo2 的变量。
- **file-backup**（APP_HOST）：按 systemd README 的启用说明执行；repo2 的几行带 `--environment-file`。
- **核对**：
  - `systemctl list-timers 'nexloop-*'` 的下次执行时间与计划一致；
  - 第二天检查清单中 repo2 的 `pg_diff`、`files_*` 和 journal 行，都是 `verified`。

## 6. doctor --backup 换用 v2 计划（**B**）

前提：第 3 步的 repo2 `pg_full` 状态为 `verified`。

```
sudo -u nexloop-backup /opt/nexloop/backup-venv/bin/nexloop-doctor --backup --metrics-database-url-file "$NEXLOOP_ROOT/secrets/backup/metrics.dsn" \
  --backup-plan "$RELEASE_CHECKOUT/deploy/configuration/backup.v2.json"
```

- **前提**：backup-venv 的版本要包含本分支的 `doctor.BACKUP_REPOS`，否则 v2 计划会被判为 `backup_plan_file: false`。
- **预期**：全部 true，`required_repos_recent` 中也包含 repo2。
- **之后**：凡是写 `backup.v1.json` 的地方（定时 doctor、部署门禁的 backup 输入），都改成 v2。
- 告警规则 v4 中 repo2 的年龄规则（selector `repo2`，26 小时）在 repo2 有备份后自动生效。

## 7. 恢复演练与断网演练（维护窗口；**B**）

1. **从 repo2 恢复演练**：形态完全同 run-33，只改两处：
   - 演练配置 `pgbackrest-drill.conf` 用 repo2 的 S3 设置，凭据用**恢复子用户**的只读 key，演练前启用、演练后停用；
   - `nexloop-restore` 改用 `--repo 2`（`backup.restore_main` 的 `--repo` 可选 1 或 2）。
   - **判据**同 run-33：`verified_readonly`，`drill_compare` 无差异。`tool_seconds` 中包含公网下载时间，就是 repo2 的 RTO，记录下来。
2. **断网演练**（验证方案 §5 的 WAL 风险）：
   - 在 DATA_HOST 上临时阻断对 TOS S3 端点的出站，阻断 5 分钟，方法由负责人选；
   - 观察 `pg_wal` 的段数，以及 `doctor --backup` 的 `wal_archiving`；
   - 恢复连通后，WAL 应自动补推，`wal_archiving` 回到 true；
   - 记录 pgBackRest 在推送失败期间对 repo1 的实际行为：是否仍推到了 repo1。

## 8. 撤销

| 步骤 | 撤销方式 |
|---|---|
| 2 | 恢复 `pgbackrest.conf.before-repo2`，回到只有 repo1；WAL 也只推 repo1 |
| 4、5 | `systemctl disable --now nexloop-journal-restic@repo2.timer`；pg-full、pg-diff 换回只有 repo1 的版本；restic-maint 删去 repo2 的几行；doctor 换回 v1 |
| 桶中的数据 | 保留，不删。确实要删时，由负责人在控制台单独操作，并先确认 repo1 的备份和最近一次演练都正常 |

## 9. 证据

记录以下内容，放入 `local-only/evidence/v0.1-evidence/backup-restore/`：
- 第 2 步 `check` 和 `info` 的输出；
- 第 3 步的执行器输出和清单行；
- 第 4 步的 restic 快照 ID；
- 第 6 步的 doctor 输出；
- 第 7 步的演练报告和 RTO、RPO。

不记录 AK/SK、口令、桶名或主机地址；桶名写作 `<TOS_BUCKET>`。
