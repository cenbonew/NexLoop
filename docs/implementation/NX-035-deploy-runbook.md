# NX-035 备份部署运行手册（B 类步骤，逐步由负责人确认）

依据：

- `NX-035-design.md`：§3 数据分类与仓库，§4 执行者与清单，§7 演练与部署前门槛，§8 doctor 与告警，§13 D1–D8（已定）；
- `NX-035-local-implementation.md`：已实现部分与未完成项；
- `planning/ROADMAP.md` O6 定案：
  - 备份口令为 32 位以上的随机串，保存在密码管理器和纸质副本两处；
  - 异地仓库使用国内 S3 兼容对象存储，与两台主机不在同一个物理地点，由 restic 和 pgBackRest 在客户端加密；
  - 两台主机系统盘未加密，部署时为备份仓库、演练目录和秘密目录各建一个 LUKS 加密卷，不重装系统。

本手册只是文档，任何会话都不得自行执行其中的步骤。格式与 `NX-032-deploy-runbook.md` 相同：

- **参数**：命令里出现的都是参数，取值来自负责人管理机上的私有 env 文件，本手册和仓库都不写真实值。参数名沿用 NX-032 的 `$SSH_ALIAS_DATA`、`$SSH_ALIAS_APP`、`$NEXLOOP_ROOT`、`$EVIDENCE_DIR`，以及设计稿 §12 的 `$PG_BACKUP_REPO`、`$FILE_BACKUP_REPO`、`$DRILL_ROOT`、`$DRILL_REPORT_DIR` 等。
- **口令和凭据**：一律只以 0600 文件路径传入，或者由负责人在终端中交互输入；不出现在命令行参数、环境变量值、日志或证据文件中。
- **执行位置**：管理机、DATA_HOST、APP_HOST，含义与 NX-032 相同。
- **每步标注**：
  - 是否为 B 类：B 类步骤必须由负责人确认后才执行；
  - 是否需要 sudo 口令：sudo 口令由负责人本人输入，不交给任何会话。
- **记录**：每步前后的 inventory 和 doctor 输出保存在 `$EVIDENCE_DIR`，不进仓库。前后核对的方法与 NX-032“每一步通用的前后核对”相同，`existing_services_unchanged` 必须为 true。

## 实现状态：哪些命令现在就能用，哪些要等部署切片

| 能力 | 状态 | 本手册中的处理 |
|---|---|---|
| 清单表、恢复日志、恢复围栏、迁移门槛（`nexloop-migrate`）、`doctor --backup`、`backup.v1.json`、告警规则 v3 | 已合入 | 直接使用 |
| `nexloop-backup pg` | 已改为调用 pgBackRest（`backup`、`verify`、`info`），清单写入工具、版本与校验证据（[NX-035-pgbackrest.md](NX-035-pgbackrest.md)，清单 schema v2） | 第 2.5、2.6 步直接使用；`--tool pg_basebackup` 只用于没有 pgBackRest 的开发与测试集群 |
| `nexloop-backup journal` | 已有 | 直接使用 |
| 文件备份执行者 `nexloop-file-backup`（Artifact 和已终态 Run 目录，restic） | 已实现（迁移 0194；见 [文件备份执行者实现说明](NX-035-file-backup.md)） | 第 3.3 步的 Artifact 与 Run 目录条目，以及第 5.1 步的 `nexloop-file-backup` 定时任务；部署前还有三项待定，见该实现说明 §5 |
| 恢复命令行 `nexloop-restore`（含 `--target-name` 和 `--target-time`） | 已实现（见 [恢复命令行](NX-035-restore-cli.md)） | 第 4 步直接使用 |
| pgBackRest 和 restic 写入 `versions.lock.json` 与第三方许可清单 | 已写入版本与来源（`deployment_tools`）；2026-10-14 管理机下载并验签后状态为 `verified_not_installed`，已回填 sha256 | 第 2.1、3.1 步安装时核对官方校验值并回填锁文件 |

**结论**：第 1 步和第 3 步中恢复日志的部分现在就能执行；第 2 步中“清单写入”和第 4 步都要等部署切片的代码合入。NX-032 的第 5 步（迁移）也必须等这两步完成。

## 与 NX-032 的先后关系

| 顺序 | 步骤 | 原因 |
|---|---|---|
| 1 | **本手册第 1 步**：LUKS 卷 | NX-032 第 1 步建立的 `$NEXLOOP_ROOT/secrets` 必须已经落在加密卷上，所以 LUKS 卷要最先建 |
| 2 | NX-032 第 0–4 步 | 建目录、证书、防火墙，启动 infra。启动 PG 时就要用第 2.4 步的归档配置；如果先用模板原样启动，第 2.4 步要再重启一次 |
| 3 | **本手册第 2 步**（pgBackRest）和**第 3 步**（restic） | 得到第一份已验证的全量备份，并让归档进入健康状态 |
| 4 | **本手册第 4 步**：恢复演练门槛 | 先用合成部署数据演练一次。真实租户进入之前（NX-037）还要再演练一次 |
| 5 | NX-032 第 5 步：迁移 | `nexloop-migrate` 要求清单中有 60 分钟以内、`catalog_digest` 与当前库相同的已验证备份，WAL 和恢复日志不能积压，也不能处于恢复围栏中。迁移完成后立即再做一次差异备份（第 2.6 步） |
| 6 | NX-032 第 6 步，以及本手册第 5 步 | 启动应用；启用定时任务、doctor 和告警 |

**没有已验证的备份，就不迁移。**这一点没有豁免，`nexloop-migrate` 没有跳过开关。

## 0. 负责人提前准备的材料

全部材料都保存在负责人那里，任何一项都不进仓库，也不交给任何会话。

| 材料 | 要求 | 保管 |
|---|---|---|
| S3 兼容对象存储 | 国内服务商；一个专用桶，地域与两台主机不在同一物理地点；开启版本控制或对象锁（可选，用于防勒索）；关闭公开访问 | 桶名和 endpoint 写入私有 env：`$S3_ENDPOINT`、`$S3_REGION`、`$S3_BUCKET` |
| S3 访问密钥 | 专用子账号，只有这个桶的读、写和列举权限。如果要在仓库端执行 restic prune，还需要删除权限；或者 prune 改在管理机上用另一把密钥执行 | 0600 文件 `$OFFSITE_CREDENTIAL_FILE`，只放在 DATA_HOST 和 APP_HOST 的加密秘密卷中 |
| pgBackRest 仓库口令（repo1、repo2 各一个） | 32 位以上的随机串 | 密码管理器加纸质，两处保管；主机上只以 0600 配置项的形式存在，只有备份身份能读 |
| restic 仓库口令（文件 repo1、repo2 各一个） | 同上 | 同上：两处保管，主机上为 0600 文件 `$FILE_BACKUP_PASS_FILE` |
| LUKS 口令（每台主机每个卷一个，或每台主机一个） | 32 位以上的随机串 | 两处保管；不写入主机（见 1.3） |
| 磁盘容量 | 先在两台主机上用 inventory 读出可用空间（只读）；容量建议见 1.1 | — |
| 演练主机 | 按 D6，`$DRILL_HOST` 用 APP_HOST，与主库分离 | — |
| 维护窗口 | 第 2.4 步要重启 PG；第 4 步约 2 小时 | — |

**口令轮换**（D5）：每年一次。轮换时新建仓库，旧仓库到期后自然淘汰；不在原仓库里改口令。

## 1. 建 LUKS 加密卷（DATA_HOST、APP_HOST）

**B 类；需要 sudo 口令；LUKS 口令由负责人在终端交互输入。**

### 1.1 卷与容量建议

两台主机都用文件型 LUKS 容器：在已有的未加密文件系统上建一个镜像文件，不重新分区，也不重装系统。

| 主机 | 卷 | 挂载点 | 容量建议 |
|---|---|---|---|
| DATA_HOST | `nexloop-secrets` | `$NEXLOOP_ROOT/secrets` | 256 MiB |
| DATA_HOST | `nexloop-file-repo` | `$FILE_BACKUP_REPO`（文件 repo1，以及恢复日志目录 `$JOURNAL_DIR`） | 不低于 2 ×（Artifact 总量 + 已终态 Run 目录总量），且不低于 20 GiB |
| APP_HOST | `nexloop-secrets` | `$NEXLOOP_ROOT/secrets` | 256 MiB |
| APP_HOST | `nexloop-pg-repo` | `$PG_BACKUP_REPO`（pgBackRest repo1） | 不低于 3 × 数据库大小 + 14 天 WAL 量，且不低于 50 GiB |
| APP_HOST | `nexloop-drill` | `$DRILL_ROOT` | 不低于 2 × 数据库大小 + Artifact 总量，且不低于 30 GiB |

- WAL 量可以用 NX-030 快照中的 WAL 生成速率估算。在 200 客户的规模下，初值按上表的下限即可。
- 所有卷加起来不得超过可用空间的 70%，超过时先停下，由负责人决定是否扩容。

### 1.2 命令（以 APP_HOST 的 `nexloop-pg-repo` 为例，其他卷同理）

```
ssh "$SSH_ALIAS_APP" sudo install -d -m 0700 "$LUKS_IMAGE_DIR"
ssh "$SSH_ALIAS_APP" sudo fallocate -l "$PG_REPO_SIZE" "$LUKS_IMAGE_DIR/nexloop-pg-repo.img"
ssh -t "$SSH_ALIAS_APP" sudo cryptsetup luksFormat --type luks2 --pbkdf argon2id "$LUKS_IMAGE_DIR/nexloop-pg-repo.img"
ssh -t "$SSH_ALIAS_APP" sudo cryptsetup open "$LUKS_IMAGE_DIR/nexloop-pg-repo.img" nexloop-pg-repo
ssh "$SSH_ALIAS_APP" sudo mkfs.ext4 -L nexloop-pg-repo /dev/mapper/nexloop-pg-repo
ssh "$SSH_ALIAS_APP" sudo install -d -m 0700 "$PG_BACKUP_REPO"
ssh "$SSH_ALIAS_APP" sudo mount /dev/mapper/nexloop-pg-repo "$PG_BACKUP_REPO"
```

- `luksFormat` 会要求输入大写的 `YES` 并两次输入口令；`open` 会再要求输入一次口令。这三处都由负责人在 `ssh -t` 的终端里亲自输入。
- 挂载后，目录的属主改为对应的服务身份：秘密目录按 NX-032 第 1 步的服务 UID；pgBackRest 仓库的属主是 `$PGBACKREST_REPO_USER`；文件仓库的属主是 `$FILE_BACKUP_USER`。

**预期**：

- `cryptsetup luksDump <img>` 显示 `Version: 2` 和一个 keyslot；
- `findmnt "$PG_BACKUP_REPO"` 显示挂载自 `/dev/mapper/nexloop-pg-repo`。

**撤销**：在卷还是空的时候执行：
```
sudo umount <挂载点>
sudo cryptsetup close <名称>
sudo rm <img>
```
删除前先用 `ls -A <挂载点>` 确认卷是空的，或者只含本步刚建的内容。

**核对**：

- inventory 对比，只多出 `nexloop` 前缀的挂载点和 dm 设备；
- `stat -c '%a %U'` 显示挂载点的权限为 700，属主正确。

### 1.3 开机解锁方式与风险（B 类，由负责人选择）

| 方式 | 做法 | 风险 |
|---|---|---|
| **A. 人工解锁（推荐）** | 不写 `/etc/crypttab` 的自动解锁。主机重启后，由负责人登录执行 `cryptsetup open` 和 `mount`。依赖这些卷的服务（PG 的归档、备份定时任务、读取秘密的应用容器）在 systemd 中加 `RequiresMountsFor=<挂载点>`，卷未挂载时不会启动 | 重启之后、负责人解锁之前，NexLoop 不可用，备份也会中断（`backup_age` 等告警会提醒）。好处是口令永远不落在未加密的盘上 |
| B. 钥匙文件放在系统盘 | 在 crypttab 中引用系统盘上的钥匙文件 | 系统盘未加密，钥匙文件和密文在同一块盘上，等于没有加密。**不采用** |
| C. TPM 或 clevis 绑定 | 需要先只读核实主机有没有 TPM 2.0，以及 Secure Boot 的状态 | 盘被整机带走时仍会自动解锁。未经核实前不采用；需要时另行决定 |

无论选哪一种，主机重启后都要按第 5 步的 doctor 核对：卷已挂载，秘密可以读取，备份恢复了正常。

## 2. pgBackRest：安装、配置、stanza、首个全量备份与校验

**B 类；第 2.1 步需要 sudo 口令；第 2.4 步需要重启 PG。**

### 2.0 前置条件（未满足就不开始）

1. **版本与许可**：
   - 版本固定为 `$PGBACKREST_VERSION`，必须支持 PG 18，写入 `versions.lock.json` 的 `release_tools`，并附包的 sha256；
   - 许可证为 MIT，登记到第三方清单；
   - 这两项通过 PR 合入后才继续。
2. **PG 镜像**：DATA_HOST 的 PG 镜像必须包含同一版本的 pgBackRest，因为 `archive_command` 是在容器内执行的。
   - 这个镜像由 NX-034 的构建流程产出，并写入发布清单（digest）；
   - 核对命令：`ssh "$SSH_ALIAS_DATA" docker exec nexloop-data-postgres-1 pgbackrest version`，输出必须是 `pgBackRest $PGBACKREST_VERSION`。
3. **非 superuser 实测（D1）**：在一次性集群上（CI 主机，或一次性的本地集群）用 `nexloop_backup` 角色完成 `backup` 和 `verify`。该角色只有 REPLICATION 权限和 `pg_backup_*`、`pg_switch_wal`、`pg_create_restore_point` 的执行权。**实测失败时报调度员，不能改用 superuser。**
4. **部署切片的清单写入**：`nexloop-backup` 写清单的实现已经合入（见上文“实现状态”）。

### 2.1 APP_HOST 安装 pgBackRest（repo1 的仓库主机）

```
ssh "$SSH_ALIAS_APP" sudo install -m 0755 "$PGBACKREST_PKG_DIR/pgbackrest" /usr/local/bin/pgbackrest
ssh "$SSH_ALIAS_APP" sudo useradd --system --no-create-home --shell /usr/sbin/nologin "$PGBACKREST_REPO_USER"
```

- 安装前，在管理机上把包的 sha256 与锁文件中的值核对一致。
- **预期**：`pgbackrest version` 输出与锁文件中的版本相同。
- **撤销**：
  ```
  sudo rm /usr/local/bin/pgbackrest
  sudo userdel "$PGBACKREST_REPO_USER"
  ```
- **核对**：inventory 对比中，只新增了这个文件和这个用户。

### 2.2 受限 SSH 身份（DATA_HOST → APP_HOST）

- **做法**：在 APP_HOST 上，`$PGBACKREST_REPO_USER` 的 `authorized_keys` 只放一把专用公钥，并限定为：
  - `restrict,command="pgbackrest ${SSH_ORIGINAL_COMMAND#* }"`，来源地址限定为 `from="$DATA_HOST_ADDR"`；
  - 或者使用 pgBackRest 的 TLS 服务器模式（`tls-server-*`）。二选一，推荐 TLS 模式，因为它可以复用 NX-032 的 CA。
- **私钥位置**：DATA_HOST 的秘密卷中，0600。
- **撤销**：删除这把公钥或 TLS 证书。
- **核对**：从 DATA_HOST 用这个身份只能执行 pgbackrest，开交互 shell 会失败。

### 2.3 配置文件

M8 定为 TLS 服务器模式，两台主机各一个配置文件：

- **APP_HOST**：`/etc/pgbackrest/pgbackrest-server.conf`，样例为 `deploy/stage/systemd/pgbackrest-server.conf.example`，由 `nexloop-pgbackrest-server` 使用。
  - `repo1-path=<PG_BACKUP_REPO>`，填加密卷的实际挂载点。
  - 证书在 `$NEXLOOP_ROOT/secrets/pgbackrest-server/`（样例为 /srv/nexloop），是 `stage_secrets.py` 生成的主机文件，见 NX-032-stage-secrets.md。
- **DATA_HOST**：`$NEXLOOP_ROOT/secrets/pgbackrest/pgbackrest.conf`，容器内路径为 `/etc/pgbackrest/pgbackrest.conf`，权限 0600，uid 999。样例为 `deploy/stage/config/pgbackrest.conf.example`，只含占位符。要点：
  - `pg1-path` 必须等于 compose 的 PGDATA，即 `/var/lib/postgresql/18/docker`；
  - `repo1-host-type=tls`，并设置 `repo1-host`、`repo1-host-port`；
  - `repo1-host-cert-file`、`repo1-host-key-file`、`repo1-host-ca-file` 指向容器内的 `/etc/pgbackrest/tls/{client.crt,client.key,ca.crt}`；客户端证书 CN 为 `pgbackrest-data-client`，与服务器的 `tls-server-auth` 一致；
  - repo2（异地 S3）整段可选，对象存储就绪前只配 repo1；
  - 首次备份时库内还没有 `nexloop_backup`，用 `pg1-user=nexloop_bootstrap`，并在 pgpass 中临时加入 bootstrap 行；2.5b 之后改回 `nexloop_backup`，同时删掉 bootstrap 行（与 2.4 的说明一致）。

DATA_HOST 侧的核心配置如下（完整内容见样例文件）：

```
[nexloop]
pg1-path=/var/lib/postgresql/18/docker
pg1-socket-path=/var/run/postgresql
pg1-user=nexloop_backup

[global]
repo1-host-type=tls
repo1-host=__APP_HOST_NAME__
repo1-host-port=__PGBACKREST_TLS_PORT__
repo1-host-cert-file=/etc/pgbackrest/tls/client.crt
repo1-host-key-file=/etc/pgbackrest/tls/client.key
repo1-host-ca-file=/etc/pgbackrest/tls/ca.crt
repo1-path=__PG_BACKUP_REPO__
repo1-cipher-type=aes-256-cbc
repo1-cipher-pass=__REPO1_CIPHER_PASS__
repo1-retention-full-type=time
repo1-retention-full=35
repo1-retention-diff=14
start-fast=y
archive-async=y
process-max=2
log-level-console=info
```

- 保留策略与 D4 一致：PITR 窗口 14 天，每月一份完整备份，最长保留 35 天。
- `__X__` 由负责人填写，口令从密码管理器中取。渲染后用 `grep -c '__' pgbackrest.conf` 确认结果为 0。
- 这个文件不得进入任何备份仓库，也不得进入证据目录。

### 2.4 打开 WAL 归档（需要重启 PG，在维护窗口内进行）

1. 在 DATA_HOST 的 `postgresql.conf` 中加入以下三项：
   - `archive_mode = on`
   - `archive_command = 'pgbackrest --stanza=nexloop archive-push %p'`
   - `archive_timeout = 60`
   
   NX-032 的模板注释中写明了“由 NX-035 打开”。
2. 重启：`docker compose -p nexloop-data restart postgres`。

- **预期**：`show archive_mode` 返回 `on`。
- **撤销**：把 `archive_mode` 改回 `off` 并重启；仓库中已有的内容保留。
- **核对**：`select * from pg_stat_archiver` 中，`last_archived_time` 在继续前进，`failed_count` 不增长。

### 2.5 stanza、首个全量备份与校验

```
ssh "$SSH_ALIAS_DATA" docker exec -u postgres nexloop-data-postgres-1 pgbackrest --stanza=nexloop stanza-create
ssh "$SSH_ALIAS_DATA" docker exec -u postgres nexloop-data-postgres-1 pgbackrest --stanza=nexloop check
ssh "$SSH_ALIAS_DATA" docker exec -u postgres nexloop-data-postgres-1 pgbackrest --stanza=nexloop --repo=1 --type=full backup
ssh "$SSH_ALIAS_DATA" docker exec -u postgres nexloop-data-postgres-1 pgbackrest --stanza=nexloop --repo=2 --type=full backup
ssh "$SSH_ALIAS_DATA" docker exec -u postgres nexloop-data-postgres-1 pgbackrest --stanza=nexloop verify
ssh "$SSH_ALIAS_DATA" docker exec -u postgres nexloop-data-postgres-1 pgbackrest --stanza=nexloop info --output=json
```

首个全量备份不要直接调用 `pgbackrest backup`，而是通过执行者完成，这样备份、校验与清单写入是同一次操作（参数见 [NX-035-pgbackrest.md](NX-035-pgbackrest.md) §1）：

```
nexloop-backup pg --recorder-dsn-file "$PRIVATE_RECORDER_DSN" --pgbackrest-command-file "$PGBACKREST_COMMAND_JSON" --stanza nexloop --expected-version "$PGBACKREST_VERSION" --repo repo1 --kind pg_full
nexloop-backup pg --recorder-dsn-file "$PRIVATE_RECORDER_DSN" --pgbackrest-command-file "$PGBACKREST_COMMAND_JSON" --stanza nexloop --expected-version "$PGBACKREST_VERSION" --repo repo2 --kind pg_full
```

上面列出的 `--repo=1/2 --type=full backup` 两行由这两条命令代替；`stanza-create`、`check`、`info` 照旧手工执行，用于核对。

- **预期**：
  - `check` 输出 `completed successfully`；
  - `info` 中两个 repo 各有一份 full 备份，状态为 ok；
  - 清单 `runtime.nexloop_backup_manifests` 中有对应的 `pg_full` 行，状态为 `verified`。
- **撤销**：执行 `pgbackrest --stanza=nexloop stanza-delete`。这会删除仓库中这个 stanza 的全部内容，属于破坏性操作，只在首次部署失败时使用，并且需要负责人单独确认。
- **核对**：`nexloop-doctor --backup --metrics-database-url-file "$PRIVATE_METRICS_DSN" --backup-plan deploy/configuration/backup.v1.json` 中，`backup_age` 和 `wal_archive_lag` 通过。repo2 启用后，`offsite_backup_age` 也要通过。

### 2.6 迁移前后的差异备份（每次执行 NX-032 第 5 步时）

```
nexloop-backup pg --recorder-dsn-file "$PRIVATE_RECORDER_DSN" --pgbackrest-command-file "$PGBACKREST_COMMAND_JSON" --stanza nexloop --expected-version "$PGBACKREST_VERSION" --repo repo1 --kind pg_diff
```

退出码为 0（清单中这一行为 `verified`）之后，才调用 `nexloop-migrate`。迁移完成后，立即再执行一次。

- **预期**：迁移门槛输出 `{"migrated": true, ...}`。
- **撤销**：不需要。
- **核对**：迁移门槛不拒绝。如果拒绝，按输出中的拒绝代码（见 `NX-035-local-implementation.md` §3“迁移门槛”的代码表）处理，不允许绕过。

## 3. restic：文件与恢复日志的加密仓库

**B 类；安装时需要 sudo 口令。**

### 3.0 前置条件

- restic 版本固定为 `$RESTIC_VERSION`，写入 `versions.lock.json`，附发布包的 sha256；许可证为 BSD-2-Clause，登记到第三方清单。
- 下载发布包要由负责人确认，下载后核对 sha256 和签名。

### 3.1 安装（DATA_HOST 用于恢复日志；APP_HOST 用于文件）

```
ssh "$SSH_ALIAS_DATA" sudo install -m 0755 "$RESTIC_PKG_DIR/restic" /usr/local/bin/restic
ssh "$SSH_ALIAS_DATA" sudo useradd --system --no-create-home --shell /usr/sbin/nologin "$FILE_BACKUP_USER"
```

- **预期**：`restic version` 输出与锁文件中的版本相同。
- **撤销**：删除这个文件和这个用户。
- **核对**：inventory 对比中，只新增了这两项。

### 3.2 初始化仓库

```
ssh "$SSH_ALIAS_DATA" sudo -u "$FILE_BACKUP_USER" restic -r "$FILE_BACKUP_REPO/restic" --password-file "$FILE_BACKUP_PASS_FILE" init
ssh "$SSH_ALIAS_DATA" sudo -u "$FILE_BACKUP_USER" env $(cat "$OFFSITE_CREDENTIAL_ENV") restic -r "s3:$S3_ENDPOINT/$S3_BUCKET/nexloop-files" --password-file "$FILE_BACKUP_PASS_FILE_REPO2" init
```

- repo2 的 S3 凭据放在一个 0600 的 env 文件中，只含 `AWS_ACCESS_KEY_ID` 和 `AWS_SECRET_ACCESS_KEY` 两行。实际使用时由 systemd 的 `EnvironmentFile=` 读取，不在命令行中展开；上面的 `env $(cat …)` 只用于这一次手工初始化，执行前先 `set +x`。
- **预期**：`created restic repository <id>`。
- **撤销**：
  - 本地仓库：`rm -r "$FILE_BACKUP_REPO/restic"`，仓库中还没有快照时才执行；
  - S3 仓库：删除桶内的 `nexloop-files/` 前缀，由负责人在服务商控制台确认后操作。
- **核对**：`restic -r … cat config` 可以读出仓库配置；输入错误的口令必须被拒绝。

### 3.3 备份、保留与校验

| 对象 | 频率 | 命令 |
|---|---|---|
| 恢复日志导出 | 每分钟 | `nexloop-backup journal --recorder-dsn-file "$RECORDER_DSN_FILE" --journal-dir "$JOURNAL_DIR"`。分段写到加密卷上，带哈希链 |
| 恢复日志 → repo1 | 每 5 分钟 | `restic -r "$FILE_BACKUP_REPO/restic" --password-file … backup "$JOURNAL_DIR" --tag journal` |
| 恢复日志 → repo2 | 每 15 分钟 | 同上，`-r s3:…` |
| Artifact、已终态 Run 目录（APP_HOST）→ DATA_HOST 文件 repo1 和 repo2 | 每 15 分钟 | `nexloop-file-backup --kind artifact --recorder-dsn-file "$RECORDER_DSN_FILE" --source-root "$ARTIFACT_ROOT" --staging-root "$FILE_BACKUP_STAGING" --repository-file "$FILE_BACKUP_REPO_FILE" --password-file "$FILE_BACKUP_PASS_FILE" --key-ref <标签>`；Run 目录用 `--kind runtime --source-root "$RUN_ROOT"`；repo2 再各跑一次（`--repo repo2`，仓库文件指向 S3，加 `--environment-file "$OFFSITE_CREDENTIAL_ENV"`）。它按 owner 侧清单复制并重算 sha256，调用 restic 与 `restic check`，结果写入清单的 `files_*` 行 |
| 保留 | 每天 | `restic forget --keep-within 14d --keep-monthly 1 --prune`（与 D4 一致，最长 35 天） |
| 校验 | 每天 / 每周 | 每天执行 `restic check`；每周执行 `restic check --read-data-subset 1/10` |

- **预期**：
  - `backup` 输出 `snapshot <id> saved`；
  - `check` 输出 `no errors were found`。
- **撤销**：`restic forget <snapshot>`，只删除指定的快照。
- **核对**：`nexloop-doctor --backup` 中，`restore_journal_lag` 通过。文件执行者上线后，`file_backup_age` 也要通过。

## 4. 部署前门槛：一次性环境中的恢复演练（AT-050；AT-056 和 AT-059 的真实备份版本）

**B 类；需要 sudo 口令（建立和销毁演练目录）；依赖部署切片的 `nexloop-restore`。**

### 4.1 时机

- 第一次：完成第 2、3 步之后，NX-032 第 5 步之前。这时库里只有合成部署数据，用来证明真实备份可以恢复。
- 第二次：真实租户进入之前（NX-037）。
- 之后：每月一次；每次重大 Schema 变更或 Pi 存储升级之后，各加做一次（D6）。

### 4.2 步骤

1. **打恢复点**（DATA_HOST，用备份身份）：
   ```
   nexloop-backup drill-mark --recorder-dsn-file "$RECORDER_DSN_FILE" --drill-id <id> --out "$DRILL_REPORT_DIR/mark-<id>.json"
   ```
   它调用 `authz.nexloop_drill_mark`，在同一次调用中记录各只追加表的最大序号、按租户的计数和抽样行的哈希，写入一行 `drill_restore_point` 清单，并把这个标记写进新的 0600 文件。恢复到这个点的库里没有这一行（它在恢复点之后才提交），所以第 4 步要用这个文件来比对。
2. **提交合成删除请求**（用于验证 AT-059 的真实备份版本）：在恢复点之后，对 `world=test` 的合成 Consumer 提交一次删除请求，然后等待恢复日志导出。
3. **恢复到演练目录并执行恢复流程**（APP_HOST，使用 `$DRILL_ROOT`；在同一个 PG 镜像、以 `--network none` 启动的容器里执行，不挂载任何渠道、模型或效应凭据）：
   ```
   nexloop-restore --source pgbackrest --pgbackrest-bin "$PGBACKREST_BIN" --pgbackrest-config-file "$PGBACKREST_CONFIG_FILE" \
     --stanza nexloop --repo 1 --target-name drill-<id> --drill-mark-file "$DRILL_REPORT_DIR/mark-<id>.json" \
     --pg-bin "$PG_BIN" --pg-path "$DRILL_ROOT/pgdata" --port "$DRILL_PORT" --socket-dir "$DRILL_ROOT/socket" --owner-user "$OWNER_USER" \
     --restore-ref drill-<id> --journal-dir "$JOURNAL_RESTORE_DIR" --old-primary-fenced \
     --report-file "$DRILL_REPORT_DIR/restore-<id>.json" --stop
   ```
   - 只恢复到新的空目录和空闲端口，只监听本机 socket，pg_hba 只放行 owner；pgBackRest 的配置（含仓库口令）只以 0600 私有文件的路径传入。
   - 演练用 `--old-primary-fenced` 表示生产库不受影响（演练从不向同一个 stanza 归档）；真实灾难恢复时，必须先确认原主库已隔离。
   - `$JOURNAL_RESTORE_DIR` 是从 restic 恢复出来的恢复日志目录。
4. **恢复流程**（`nexloop-restore` 在上一步中依次执行）：
   - 设立围栏；
   - 校验恢复日志的哈希链，并重放；
   - 窗口对账；
   - `bootstrap.verify()`；
   - 停在 `verified_readonly`，在演练中永远不开放。
5. **一致性校验**（由 `nexloop-restore` 输出，都是数字；报告里还有 `open_scope` 的五条补偿控制，任何一条不成立都会拒绝）：
   - 迁移的版本和 checksum 一致；
   - 只追加表在最大序号以内的计数相等；
   - 抽样行的哈希相等；
   - 外键完整；
   - Artifact 清单摘要一致（切片三之后才有）；
   - 第 2 步的合成删除在恢复后已重放，该 Consumer 既不可联系，也不可检索。
6. **记录**：
   - RTO（从开始恢复到 `verified_readonly`）、RPO 和各项计数，写入 `runtime.nexloop_restore_drills`；
   - 只含数字的报告放在 `$DRILL_REPORT_DIR`，并复制一份到 `$EVIDENCE_DIR`。
7. **销毁**：
   - 停止演练容器；
   - 核对 `$DRILL_ROOT` 的目录标识与本次演练记录的一致；
   - 执行 `sudo rm -r "$DRILL_ROOT/pgdata"`。演练卷本身保留，供下次使用。

### 4.3 证据与撤销

- **预期**：
  - 报告中 `passed: true`；
  - RTO ≤ 2 小时，RPO ≤ 5 分钟（D2）；
  - `doctor --backup` 中的 `restore_drill_overdue` 通过。
- **撤销**：只需销毁演练目录。生产库只多了恢复点和清单行，这两项都是只追加的，不做回退。
- **核对**：演练结束后，生产库的 `restore_state` 仍然是 `open`，演练过程不影响生产库。
- **验收对应**：
  - **AT-050**：用真实备份完成的演练报告；
  - **AT-056**：第 2.6 步中迁移门槛先拒绝、后放行的输出；
  - **AT-059**：第 2 步的合成删除在恢复后被重放。
  
  这些证据由调度员写入 planning。AT-053 是 GitHub 断连，与本手册无关。

## 5. 定时任务、doctor 与告警接线

**B 类；需要 sudo 口令（安装 systemd 单元）。**

### 5.1 systemd timer

每个任务对应一个 service 加一个 timer，`User=` 设为对应的备份身份，`RequiresMountsFor=` 设为对应的加密卷，凭据通过 `EnvironmentFile=` 或 `--password-file` 读取，日志进入 journald，不打印任何秘密。

| 单元 | 主机 | 计划 | 内容 |
|---|---|---|---|
| `nexloop-pg-full` | DATA_HOST | 每周 | full 备份、`verify`、写入清单（repo1；repo2 同样每周一次） |
| `nexloop-pg-diff` | DATA_HOST | 每天 | diff 备份、`verify`、写入清单（repo1 和 repo2） |
| `nexloop-journal` | DATA_HOST | 每分钟 | `nexloop-backup journal` |
| `nexloop-journal-restic` | DATA_HOST | repo1 每 5 分钟，repo2 每 15 分钟 | `restic backup "$JOURNAL_DIR"` |
| `nexloop-restic-maint` | DATA_HOST | 每天，每周加深度校验 | `forget --prune` 和 `check` |
| `nexloop-file-backup` | APP_HOST | 每 15 分钟 | `--kind artifact` 与 `--kind runtime`，repo1 与 repo2 各一次（见 3.3） |

- 这些计划与 `deploy/configuration/backup.v1.json` 一致；`doctor --backup` 会核对每周的完整备份是否存在。
- **撤销**：
  ```
  systemctl disable --now <timer>
  rm <unit files>
  systemctl daemon-reload
  ```
  删除定时任务不会删除仓库中的内容。
- **核对**：`systemctl list-timers 'nexloop-*'` 中的下次执行时间与计划一致；第一次执行后，清单中出现对应的行。

### 5.2 doctor 与告警

- 告警规则 v3 中的 `backup_age`、`wal_archive_lag`、`offsite_backup_age`、`file_backup_age`、`restore_journal_lag`、`restore_drill_overdue`、`backup_verify_failed`，都从 M12 快照的 `backup.*` 读取。部署时确认已经发布 v3：`select control.nexloop_configure_alert_rules(…)` 已执行，且版本为 v3。
- 每次部署、每次主机重启，以及每周一次，都执行一遍：
  ```
  nexloop-doctor --backup --metrics-database-url-file "$PRIVATE_METRICS_DSN" --backup-plan deploy/configuration/backup.v1.json
  ```
  预期全部通过，输出只有数字。
- 工作台“运行状态”页的“备份”块显示同样的数字。

## 6. 回滚与停止

- **停止备份**：停用全部 `nexloop-*` timer，然后把 `archive_mode` 改为 `off` 并重启 PG（需要维护窗口）。仓库内容保留。
- **撤销整套备份**：需要负责人单独确认，并且只能在确认不再需要这些备份之后进行。
  1. `pgbackrest stanza-delete`；
  2. 删除 restic 仓库；
  3. 删除 S3 桶中的两个前缀；
  4. `umount` 并 `cryptsetup close` 各卷；
  5. 删除各卷的镜像文件。
  
  删除仓库之后，对应时间段的数据就无法再恢复。
- **数据恢复**（真实故障，不是演练）：按 NX-035 设计稿 §6 执行，用 `nexloop-restore --source pgbackrest --target-time <T>`（或 `--target-name`），参数见 [恢复命令行](NX-035-restore-cli.md)。恢复之后，由负责人按 D8 逐个 scope 开放派发（`nexloop.restore.open_scope:1`）。
