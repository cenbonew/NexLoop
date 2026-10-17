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
| TOS 桶 | **已建好**（2026-10-17，`cn-shanghai`），专用，不与任何业务桶共用；公开文档写作 `<TOS_BUCKET>` | 只放在私有 env（`__TOS_BUCKET__`） |
| region | 例如 `cn-shanghai`；S3 端点为 `tos-s3-<region>.volces.com` | 私有 env（`__TOS_REGION__`） |
| 版本控制 | 建议**开启**，历史版本 14 天后过期（误删或恶意删除可在 14 天内找回）；不开启也可以（方案 §3.1） | 负责人决定，记录在变更记录中 |
| 对象锁（WORM） | 建议**不开启**：开启后 pgBackRest expire 和 restic prune 删不掉过期对象 | 同上 |
| 两个子用户的 AK/SK | DATA_HOST 子用户（`nexloop/pgbackrest/`、`nexloop/restic-journal/`），APP_HOST 子用户（`nexloop/restic-files/`）；只读的恢复子用户可选 | 钥匙串 `nexloop/backup/tos-data-access-key`、`tos-data-secret-key`、`tos-app-access-key`、`tos-app-secret-key`（及恢复子用户的） |
| 三个客户端口令 | pgBackRest repo2 cipher、restic journal repo2、restic files repo2，各自 32 位以上随机串，与 repo1 不同 | 钥匙串 `nexloop/backup/repo2-cipher-pass`、`restic-repo2-journal`、`restic-repo2-files` |

主账号的 AK **不得**用于备份。

## 1. TOS 控制台：桶设置核对、子用户、只读验证（负责人本人；B）

**现状（2026-10-17 15:1x）**：负责人已在 `cn-shanghai` 建好专用桶。桶名只写在私有 env 中，公开文档写作 `<TOS_BUCKET>`。桶策略、加密、版本控制和子用户可能还没有配置，所以从核对开始。

### 1.1 桶设置逐项核对（控制台，在桶的设置页中逐项看）

| # | 项 | 应为 | 不是时 |
|---|---|---|---|
| 1 | 地域 | `cn-shanghai` | 地域建桶后不能改：新建一个桶 |
| 2 | 访问权限（ACL） | **私有**：没有公共读、没有公共读写，匿名授权为空 | 改为私有 |
| 3 | 默认加密 | **服务端加密 SSE-TOS（AES256）**；不用 KMS 也可以 | 开启 SSE-TOS |
| 4 | 版本控制 | **建议开启**（方案 B 的主要好处：误删或恶意删除后 14 天内可找回） | 开启；以后关闭只能“暂停”，不能回到“从未开启” |
| 5 | 对象锁（WORM、合规保留） | **关闭**：pgBackRest expire 和 restic prune 需要删除自己的过期对象 | 对象锁通常只能在建桶时设定，开了就关不掉：需要新建桶 |
| 6 | 桶策略 | 只有一条：拒绝非 TLS 请求。按 `deploy/examples/tos/bucket-policy.example.json` 填写，`__TOS_BUCKET__` 换成桶名；用 TOS 的写法：`"Version": "1.0"`、`Principal: ["*"]`、`"tos:securetransport": ["false"]` | 粘贴示例；控制台报错时，以控制台的语法检查为准，但不要放宽语义 |
| 7 | 生命周期 | (a) 前缀 `nexloop/` 的未完成分段上传 1 天后终止；(b) 开了版本控制的，前缀 `nexloop/` 的历史版本 14 天后删除。**不要有任何“当前版本过期删除”的规则**（会把仓库删坏） | 按 `deploy/examples/tos/lifecycle.example.json` 新建；删掉多余的规则 |
| 8 | 跨域（CORS）、静态网站托管、镜像回源 | 都不开 | 关闭 |
| 9 | 访问日志、云审计（可选） | 建议开启，便于事后追查；投递到负责人自己的日志项目 | 可以稍后再开 |

### 1.2 两个子用户（访问控制）

两个子用户都按下面的方法建立，各自只授权自己的前缀：

| 子用户（建议名称） | 前缀 | 策略模板 |
|---|---|---|
| `nexloop-backup-data` | `nexloop/pgbackrest/`、`nexloop/restic-journal/` | `deploy/examples/tos/iam-policy-data-host.example.json` |
| `nexloop-backup-app` | `nexloop/restic-files/` | 同一个模板，两处前缀都换成 `nexloop/restic-files/*` |

1. 新建用户：**只开通编程访问**，不开通控制台登录，不加入任何用户组，不附加任何系统策略。
2. 新建自定义策略，按模板填写：
   - `__TOS_BUCKET__` 换成桶名；
   - 列举动作写 `tos:ListObjects`，条件键写**全小写**的 `tos:prefix`（上游实测：`prefix`、`tos:Prefix` 都会被拒）；
   - 对象动作：Get、Put、Delete，以及分段上传相关的动作。动作名以控制台策略编辑器的列表为准，不要用通配符 `tos:*`；
   - **不给**任何桶配置动作（策略、ACL、生命周期、加密、版本控制、删除桶）。
   - 然后把策略附加给对应的用户。
3. 为用户新建访问密钥。AK/SK **只显示一次**，直接存入钥匙串，不经剪贴板历史，不存入任何文件：
   ```
   security add-generic-password -s nexloop/backup/tos-data-access-key -a "$USER" -w     # 回车后按提示粘贴 AK；不在命令行中写值
   security add-generic-password -s nexloop/backup/tos-data-secret-key -a "$USER" -w     # 粘贴 SK
   ```
   - APP_HOST 子用户用 `nexloop/backup/tos-app-access-key`、`nexloop/backup/tos-app-secret-key`；
   - 不加值的 `-w` 会让 `security` 交互式地读取，值不会出现在命令行参数或 shell 历史中；
   - 在 `~/.nexloop/vault-index.md` 中登记条目名（不写值）。
4. 同时生成三个客户端口令，同样存入钥匙串：`nexloop/backup/repo2-cipher-pass`、`nexloop/backup/restic-repo2-journal`、`nexloop/backup/restic-repo2-files`。每个 32 位以上的随机串，与 repo1 的口令不同。

### 1.3 只读验证（负责人在自己的终端运行；调度员只看输出）

工具：`scripts/deploy/tos_prefix_probe.py`，只用标准库。
- 它**自己**用 `security find-generic-password -s <条目> -w` 从钥匙串读取 AK/SK，不经命令行参数，也不输出密钥；
- 用 S3 SigV4 签名访问 `tos-s3-cn-shanghai.volces.com`（TLS）；
- 只输出每项检查的名字、前缀、预期和实际的 HTTP 状态码，不输出对象名或响应内容。

```
python3 scripts/deploy/tos_prefix_probe.py --bucket "$TOS_BUCKET" --role data
python3 scripts/deploy/tos_prefix_probe.py --bucket "$TOS_BUCKET" --role app
```

- `$TOS_BUCKET` 来自负责人的私有 env，不要贴到聊天中；工具的输出里也没有桶名。
- 第一次运行时，macOS 可能弹窗询问是否允许 `security` 访问钥匙串条目，选择“允许”（不要选“始终允许”）。
- **预期**：`ok: true`，退出码 0。逐项为：
  - `list_own:<自己的每个前缀>`：200；
  - `list_bucket_root`：403（整个桶列不出来，说明策略按前缀限定了）；
  - `list_other:<另一个子用户的前缀>`：403；
  - `list_path_style:…`：只报告，不判定。这一项回答方案 §5 中 restic 寻址的问题：200 说明 TOS 接受 path 风格，restic 不需要加 `-o s3.bucket-lookup=dns`；403 或 400 说明需要加。
- **加 `--write`**：在自己的前缀下写入并删除一个 0 字节的探针对象 `.nexloop-probe-<uuid>`，预期 put 200、delete 204；在另一个子用户的前缀下 put，预期 403。这样也验证了删除权限，pgBackRest expire 和 restic prune 都需要它。
  - 开了版本控制时，删除后会留下一个历史版本，14 天后由生命周期清掉。
- **不符时**：
  - `list_own` 为 403：策略没有附加上，或者条件键写错了（`tos:prefix` 必须全小写）；
  - `list_bucket_root` 或 `list_other` 为 200：策略范围太宽，比如用了 `tos:*` 或资源写成了整个桶，要先改正再继续；
  - `keychain item unreadable`：条目名不对，或者钥匙串没有解锁；
  - 连接错误：检查本机网络和代理。
- 输出只有检查名和状态码，可以直接贴给调度员。

### 1.4 每月复核

每月复核一次 §1.1 的表格，再跑一次 §1.3（不加 `--write`）。这代替上游的每日自动审计（方案 §4）。

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
