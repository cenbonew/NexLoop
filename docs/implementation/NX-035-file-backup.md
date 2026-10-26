# NX-035 切片三：文件备份执行者 `nexloop-file-backup`

分支 `l5/nx035-file-backup`，从 main 7fb06a2 切出。迁移 `0194_nx035_file_backup_inventory.sql`（开发时临时号 0450，合入时重编号）；`versions.lock.json` 的 `bootstrap_revision` 也在合入时一并更新，本分支不改它。

依据：

- [NX-035 设计稿](NX-035-design.md)：§3 数据分类与仓库，§4.1 执行者，§4.2 清单表，§4.3 文件备份的一致性，D1（restic）；
- [备份部署运行手册](NX-035-deploy-runbook.md)：§3 restic，§5.1 定时任务。

分工：pgBackRest 与版本锁由 L2 承担，恢复命令行由 L4 承担。本分支只涉及文件备份。

## 1. 与共用清单格式的关系

不改清单表和记录函数。0146 的 `runtime.nexloop_backup_manifests` 与 `authz.nexloop_record_backup` 已经为 `files_artifact`、`files_runtime` 两类预留了以下字段，本执行者按原样写入：

| 字段 | 写入的值 |
|---|---|
| `backup_id` | restic 快照 id（64 位十六进制）；restic 失败、没有快照时为 `files-a-<UTC 时间>` 或 `files-r-<UTC 时间>` |
| `repo` | `repo1` 或 `repo2` |
| `kind` | `files_artifact` 或 `files_runtime` |
| `verify_status` | `verified` 或 `failed`（见 §3） |
| `started_at`、`completed_at` | 开始与完成时间 |
| `file_manifest_digest`、`file_count`、`bytes` | 文件清单摘要（见 §3）、文件数、字节数 |
| `key_ref` | 可选。口令的标签，不是口令本身 |

文件类备份只写一行，没有 `pending` 行。这与 0146 对非 PG 类的规定一致。M12 的 `file_backup_age_seconds` 和告警规则 `file_backup_age` 已经按这两类的 verified 行计算，无需改动。

## 2. 迁移 0194：owner 侧清单

新增函数 `authz.nexloop_file_backup_inventory(kind, after, limit)`：

- **调用者**：只有 `nexloop_backup_recorder` 能调用（按 `session_user` 判断，与其他记录函数相同）。它仍然读不到任何表。
- **返回**：`{kind, entries}`，按 ref 排序，用 keyset 分页（`after` 加 `limit`，limit 在 1–1000 之间）。条目只有 ref、哈希和大小。
- **`files_artifact`**：只列出 `status='available'` 且仍在保留期内的 Artifact，字段为 `ref`（即 `object_key`，形如 `<64 hex>/<32 hex>`）、`sha256`、`bytes`。删除中、已删除、未完成上传和已过期的 Artifact 都不会进入新快照。
- **`files_runtime`**：只列出这样的 Pi Run：它绑定的所有任务都已终态（`succeeded`、`failed`、`dead_lettered`、`cancelled`），而且没有排队中或已完成的 NX-029 清除项。字段为 `ref`，即 run_id，也就是 Host 的目录名。活跃 Run 不会被列出。
- **范围**：只包括 world `real`。模拟和影子世界按 NX-041 D2 是一次性的，不备份。
- **租户**：表都开启了 FORCE RLS，所以函数逐个租户设置 `eios.tenant_id` 读取，结束后恢复调用方原来的设置；全局前 `limit` 条一定落在各租户前 `limit` 条的并集之中。
- **安全属性**：SECURITY DEFINER，属主为 `nexloop_owner`，`search_path` 固定为 `pg_catalog,pg_temp`；不授予 public，只授予 recorder 执行权。

## 3. 执行者 `nexloop_eios/file_backup.py`

```
nexloop-file-backup --kind artifact|runtime --recorder-dsn-file F --source-root D --staging-root D
    --repository-file F --password-file F [--environment-file F] [--repo repo1|repo2] [--key-ref LABEL] [--restic BIN]
```

一次运行只备份一类，写一个仓库：

1. **读清单**：从 0194 按页读出全部条目。条目的 ref 不符合规定形状时，整次运行停止，什么都不写。
2. **复制到暂存目录**：在 `--staging-root`（必须是 0700、属于本用户的目录）下新建一个暂存目录。
   - **Artifact**：逐级以 `O_NOFOLLOW` 打开源文件，任何一级是链接都不跟随；复制后对副本重算 sha256 和大小，与清单不一致就删除副本，计为 `mismatch`；源文件不存在或是链接，计为 `missing`。
   - **Run**：
     - 只复制 `runtime.sqlite`；
     - 旁边有非空的 `-wal` 文件时，说明存储还没关闭或没做 checkpoint，不复制，计为 `not_checkpointed`；
     - 副本必须通过 SQLite 的 `PRAGMA integrity_check`（只读、immutable 打开），不通过就删除，计为 `integrity`；
     - 目录已不存在时计为 `absent`，例如被 NX-029 清除，或 Run 从未建立存储。`absent` 不算失败。
3. **写文件清单**：把 `FILE-MANIFEST.json`（schema `nexloop-file-backup/1`，内容为 `[ref, sha256, bytes]` 列表和摘要）写进暂存目录，随快照一起保存，恢复时可以据此核对。`file_manifest_digest` 是排序后的 `[ref, sha256]` 列表的规范 JSON 的 sha256，与设计 §4.2 一致。
4. **restic 备份与校验**：调用 `restic --no-cache --repository-file F --password-file F backup --json --tag <kind> --host nexloop .`，从输出的 summary 行取得快照 id；随后执行 `restic check`。
5. **写清单行**：只有 restic 成功、check 通过、并且没有任何文件被跳过（`missing`、`mismatch`、`not_checkpointed`、`integrity` 都为 0）时，才写 `verified`，否则写 `failed`。一个持续损坏的 Artifact 会让每次备份都是 `failed`，`file_backup_age` 告警因此会响，需要人工处理。
6. **清理**：无论成功与否，最后都删除暂存目录。

**凭据**：

- recorder DSN、restic 仓库位置、restic 口令、对象存储环境文件，都是 0600、属于执行者用户的普通文件（`read_private_text`）；
- 传给 restic 的只是文件路径（`--repository-file`、`--password-file`）；
- 环境文件只接受 `AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY`、`AWS_DEFAULT_REGION` 三个键，值只放进 restic 子进程的环境；子进程的环境中另外只有 `PATH` 和 `LANG`；
- 不打印任何凭据，也不打印 restic 自己的输出。结果行只有计数、标签和哈希。

**退出码**：0 表示 verified；1 表示 failed 或配置错误。配置错误时只输出错误类型。

## 4. 测试

- `tests/test_file_backup.py`：不需要 PostgreSQL。清单与记录函数用内存替身，restic 用替身 `tests/support/restic_stand_in.py`。覆盖：
  - 复制、重算哈希，并写入一条 verified 行；
  - 不一致、缺失、链接都被排除，此时该行为 failed；
  - Run 存储：checkpoint、integrity、absent 三种情况；
  - restic 失败或 check 失败时写 failed 行，暂存目录被删除；
  - 凭据不出现在输出里，restic 只拿到 AWS 键；
  - 不安全的配置在复制之前就被拒绝；
  - 清单 ref 越界时整次停止；
  - 命令行的退出码。
- `tests/test_file_backup_pg.py`：在一次性 PG 18 上运行。覆盖：
  - 0194 只列出有效的 Artifact 和已终态、未清除的 Run，不包括其他 world，跨租户分页正确；
  - 只有 recorder 能调用，参数校验生效，recorder 仍然读不到表；
  - 两类各一次端到端运行，经 0146 写入 verified 行。
- **未覆盖**：没有用真实的 restic 运行。开发机和 CI 主机都没有安装 restic；版本锁与安装由 L2 处理，安装后应在部署主机上用真实 restic 跑一次第 3 步。

## 5. 部署前待定的三项：负责人已定（2026-10-26，D1–D6 全部按推荐）

原来的三项待定（recorder 的连接位置、读权限、仓库位置）由负责人 2026-10-26 的 D1–D6 决定，安装方法见 §6：

| 项 | 决定 |
|---|---|
| D1 | 现在启用（不再是部署日 M12 的“不启用”） |
| D2 | DATA_HOST 的 pg_hba 加一条 `hostssl`：只允许来自 APP_HOST 的 `nexloop_backup_recorder`，`scram-sha-256`；客户端 `verify-full` |
| D3 | 方案②：unit 只有 `CAP_DAC_READ_SEARCH`（越过 API 与 Agent Host 的 0700/0600 只读，不能写；不改它们的属主、组或 ACL）；沙箱用 `TemporaryFileSystem=/srv/nexloop:ro` 盖住整个 `/srv/nexloop`，只读绑定两个数据目录和 `secrets/backup` |
| D4 | repo1 经受限 sftp（chroot）写到 DATA_HOST，与恢复日志共用同一个 restic 仓库（同一口令、同一属主） |
| D5 | restic 用 `versions.lock.json` 里的官方二进制，安装时在主机上核对归档的 sha256 |
| D6 | 先启用；“有可备份对象却从未有过 verified 文件备份”的告警另开一项，放进下一个规则版本 |

## 6. 安装：两个入口

第 1 步在 DATA_HOST，第 2–6 步在 APP_HOST，两个入口分别执行。

### 6.1 APP_HOST：`scripts/deploy/file_backup_install.sh`（第 2–6 步）

私有配置见 `deploy/examples/file-backup-install.app-host.example.json`（0600 root）。只用标准库；以 root 运行之前先做信任检查：

- 发布检出及其 `.git` 的每个目录、`objects/` 以外的每个文件，属主都必须是 root，且组和其他人不可写；整条路径用 `lstat` 逐级检查，路径上不能有链接；
- `.git` 必须是目录，不能有 `objects/info/alternates` 或 `commondir`；
- `git show` 带 `--no-replace-objects`，在最小环境中运行（`env -i`，不读系统或全局配置）；
- unit 的解释器（host-health venv 的 `bin/python`）逐跳解析链接；
- 复用已有发布时逐个文件与提交核对。

口令只经 stdin，只写进 0600 文件；任何输出、参数和日志里都没有口令、DSN 或私钥。所有配置文件都原子写入（同目录临时文件再改名）；两个 host-health 文件要么都改，要么都不改（第二个写失败时恢复第一个）。

顺序（一条命令加一个配置文件；口令从负责人的 Keychain 经管道传入）：

1. `sudo bash file_backup_install.sh <配置> --prepare`：
   - 建系统用户 `nexloop-file-backup`，home 为 `/var/lib/nexloop-file-backup`，属主 root，无 shell；
   - 生成一次 SSH 密钥，以后不覆盖；
   - 建 `/srv/nexloop/secrets/backup`（root 0711）和 `/etc/nexloop/file-backup.env`；
   - 打印公钥，供 DATA_HOST 第 1 步授权。
2. DATA_HOST 第 1 步（§6.2）。
3. `install_repo2_secrets.sh files`：repo2 的仓库、口令和对象存储环境文件，路径从 env 文件里读。
4. `printf 'recorder\t%s\nrepo1\t%s\n' … | sudo bash file_backup_install.sh <配置>`：
   - `recorder` 是 `nexloop_backup_recorder` 现有的口令，不轮换；
   - `repo1` 是恢复日志 repo1 的 restic 口令（同一个仓库，同一个口令）；
   - 重跑时可以不给这两行，已写的文件保留。

各步内容：

| 步 | 内容 |
|---|---|
| 2 | 用户和密钥；发布目录 `/opt/nexloop/file-backup/<sha>/`：执行者的四个模块、unit 入口 `file_backup_run.py`、沙箱自检 `file_backup_sandbox_check.py`、两个 unit 模板，只读；`current` 指向 `<sha>`。restic 从配置给出的归档解压到 `/opt/nexloop/file-backup/restic-<版本>/restic`（root，0555）；归档的 sha256 必须等于发布提交里 `versions.lock.json` 的 `deployment_tools.restic.archive_sha256`，否则拒绝，什么都不写 |
| 3 | `secrets/backup`：`recorder.dsn`（hostssl，`verify-full`，CA 复制为同目录的 `pg-ca.crt`）、repo1 的仓库文件（`sftp:nexloop-repo1:<path>`）和口令文件，都是 0600、属于服务用户。这个目录里如果有清单之外的任何文件，就拒绝（D3：沙箱能看到整个目录）。另外写 `~/.ssh/config` 和 `known_hosts`（root 0644）：别名、密钥、固定的主机公钥、`StrictHostKeyChecking yes`、`BatchMode yes` |
| 4 | `/etc/nexloop/file-backup.env`（0600 root）。保留 `install_repo2_secrets.sh` 写的 `FILE_BACKUP_REPO2_OPTIONS`。从发布中安装 unit 和 timer，填入 `@PYTHON@`、`@RESTIC@`、`@MDWE@`，然后 `daemon-reload` |
| 5 | **D3 沙箱验证**：用 `systemd-run` 以 unit 自己 `[Service]` 的全部设置运行自检（同一用户、同一能力、同一挂载）。通过条件：两个数据目录和本 unit 自己的文件可读；`/srv/nexloop` 只显示 `data`、`secrets`；`secrets` 只显示 `backup`；`data` 只显示 `artifacts`、`runtime`；unit 的每个 `InaccessiblePaths=` 以及 `/srv/nexloop`、`secrets`、`data` 下的其余条目都看不到内容（不存在、被拒，或读出为空，即 `InaccessiblePaths` 挂上的空节点）；两个句柄系统调用 `open_by_handle_at`、`name_to_handle_at` 都被过滤（返回 EPERM；L6 F1：持有 `CAP_DAC_READ_SEARCH` 的进程可以借这两个调用，从任一可见 fd 打开同一文件系统上的任意 inode，绕过上面所有遮挡，即 shocker 类逃逸，所以 unit 用 `SystemCallFilter=~open_by_handle_at name_to_handle_at` 禁掉它们）；restic 是锁定的版本；两个仓库都能用口令打开（`cat config`）。`repo2_init` 为 true 时，仓库不存在才执行一次 `restic init`；repo1 从不初始化。然后真实运行一次 service，四条 ExecStart 都必须是 `verified`。任何一项不通过，timer 都不启用 |
| 6 | 在两个 host-health 文件里加 `file-backup`；`nexloop-provider-drift.timer` 已安装时也加上 `provider-drift`（APP_HOST 的探针配置原来缺这一项）。然后 `systemctl enable --now nexloop-file-backup.timer` |

**`InaccessiblePaths=` 是黑名单**（L6 S1）：第 5 步只能证明列出的路径被遮住，不能证明 APP_HOST 上没有遗漏。现有的密钥位置都已覆盖：`/srv/nexloop/secrets`（由 TemporaryFileSystem 遮住）、`/etc/nexloop`、`/var/lib/nexloop`、`/var/lib/nexloop-host-health`、`/etc/pgbackrest`；`/root` 和 `/home` 由 ProtectHome 遮住。**以后在 APP_HOST 的 `/srv/nexloop` 之外新增任何密钥位置，都要同时加进这个 unit 的 `InaccessiblePaths=`**，第 5 步随后也会检查它。

`--uninstall`：
- 移除：timer、unit、drop-in、env 文件、发布目录、restic、暂存目录、`secrets/backup` 里的文件，以及两个 host-health 文件中的 `file-backup`；
- 保留：`provider-drift`，以及用户、home 和 SSH 密钥（DATA_HOST 上授权的公钥仍然匹配）。

退出码：0 完成，1 某步失败，2 拒绝。

### 6.2 DATA_HOST：第 1 步（`scripts/deploy/file_backup_data_host.sh`）

由负责人以 root 执行，一条命令加一个私有配置（`deploy/examples/file-backup-data-host.example.json`，0600 root）。不读取、不写入任何秘密。信任检查与 §6.1 相同：发布检出、`.git`、root 要执行的目录树、解释器都要通过。

| 步 | 内容 |
|---|---|
| 1a | **pg_hba（D2）**：用发布中的模板和已部署的 stage.env 重新渲染。渲染结果与已安装的 pg_hba 相比，只能多出这一条 `hostssl … nexloop_backup_recorder <APP_HOST_ADDR>/32 scram-sha-256`，不能删掉任何一行。写入方式与其他 hba 脚本相同：原地写入（同一 inode；旧内容保存为 `config/pg_hba.conf.before-file-backup`），确认容器里看到的是新文件，然后 HUP PostgreSQL 并从日志确认重新加载成功。任一步不成立，就把旧内容原地写回并重新加载。recorder 的口令不变，这里不设置 |
| 1b | **受限 sftp（D4）**：登录账户就是仓库的属主 `nexloop-backup`，一个仓库只有一个 uid。具体做法：<br>· chroot 目录 `/srv/nexloop-sftp`（root，0755，路径上每一级都属于 root）；<br>· 用 mount unit 把 `repo_path`（恢复日志的 restic 仓库）绑定挂载为 `restic/`（可读写，restic 要写入），挂载选项 `nosuid,nodev,noexec`；备份卷上的其他内容（恢复日志分段等）不在 chroot 里；<br>· 授权公钥 `/etc/ssh/nexloop-authorized-keys/nexloop-backup`，带 `restrict,from="<APP_HOST_ADDR>"`；<br>· sshd drop-in `00-nexloop-file-backup.conf`：`Match User nexloop-backup`（对所有来源地址生效），只认这个密钥文件、只允许公钥认证；`ChrootDirectory`；`ForceCommand internal-sftp -u 0077`；不分配 tty，任何转发都不允许；文件以 `Match all` 结尾。<br>先执行 `sshd -t`，再用 `sshd -T -C user=nexloop-backup,addr=<APP_HOST_ADDR>` 读回实际生效的设置。sshd 对同一个关键字取第一次出现的值，如果别处更早设置了某个值，或者 `AllowUsers`/`DenyUsers` 挡住了这个用户，就拒绝，并恢复原来的 drop-in 和公钥文件。检查通过后才重新加载 sshd |

`--uninstall` 移除 drop-in、授权公钥和 mount unit，并重新加载 sshd；chroot 目录为空时一并删除。pg_hba 中的这一行保留：模板里有它，而 APP_HOST 没有 DSN 时没人能用这一行连接。

**pg_hba 的写入（L6 I1、I2）**：临时目录 `/var/lib/nexloop/file-backup-data-host` 先通过信任检查再 chmod，root 才在里面运行渲染；`config/` 目录必须属于 root、组和其他人不可写；`pg_hba.conf` 和备份文件都不能是链接。备份用原子写入（0600 root），写 pg_hba 时以 `O_NOFOLLOW` 打开，并核对打开的 inode 就是 lstat 看到的那个。sshd 的检查也覆盖 `AllowGroups`/`DenyGroups`（按 `id -nG` 列出的组）。

**D4 的代价（L6 S3）**：
- APP_HOST 的 sftp 密钥对 repo1 有完整的读、写、删除权限。restic 要删除自己的锁文件，sftp 做不到只允许追加。
- APP_HOST 一旦被攻破，攻击者可以删除或改写 repo1 里的全部快照，包括恢复日志的快照。
- 不受影响的部分：repo2（S3，凭据独立）；恢复日志分段本身（不在 chroot 里）。

**评估：restic rest-server `--append-only`**：
- **可行**：
  - append-only 模式下，rest-server 拒绝删除或覆盖已有对象，锁文件除外；`restic backup` 照常工作。
  - forget/prune 由 DATA_HOST 本机的 `nexloop-restic-maint` 直接对目录执行，不经过 rest-server，不受影响。
- **需要新增**：
  - rest-server 二进制：写入版本锁，安装时核对 sha256，同 D5；
  - DATA_HOST 上一个 unit：以 `nexloop-backup` 运行，只提供这一个仓库；
  - TLS 证书：可以用现有的 stage CA 签发；
  - htpasswd 认证：口令经 stdin；
  - nft 规则：只允许 APP_HOST 访问它的端口。
- **APP_HOST 一侧**：仓库文件改为 `rest:https://…`。本步的 sftp 账户和 chroot 随之撤掉。
- **局限**：仍然可以写入新数据，也可以读取全部快照（restic 口令本来就在 APP_HOST）。它防的只是删除和改写。
- **建议**：先按 D4 上线。rest-server 作为后续项，由负责人决定，因为它多一个对外服务，也多一个版本锁条目。

**合并顺序提示**：模板加了这一行之后，host-health、sim、evolution 三个 hba 脚本只接受各自的行。如果在本步之前重跑它们中的任何一个，会因为“多出的行不是自己的”而拒绝。所以先执行本步。

### 6.3 测试与局限

- `tests/test_file_backup_install.py`：
  - 在临时根目录下用脚本化的 runner 运行，不需要 root、systemd、数据库或 restic；
  - 信任检查按本测试的 uid 真实执行；tmp_path 以上的目录视为 root 所有，所以在 `--basetemp` 位于 /tmp 时也能通过；
  - 沙箱自检脚本和 unit 入口真实运行，restic 用一个替身脚本。
- `tests/test_file_backup_data_host.py`：DATA_HOST 第 1 步，同样在临时根目录下用脚本化的 runner 运行（没有容器，也没有 sshd）。
- 变异验证：APP_HOST 安装器和自检脚本共 40 个变异，DATA_HOST 入口 23 个变异，全部被测试杀死。
- **未覆盖**：没有在真实 systemd 或 sshd 下运行，也没有用真实的 restic、sftp 或 PostgreSQL。这些由 DATA_HOST 第 1 步的 `sshd -T` 读回，以及 APP_HOST 第 5 步当场验证（两个仓库都能打开，真实运行四条都是 verified）；验证不通过，timer 就不启用。

### 6.4 文件 repo2 的每日维护（`nexloop-file-backup-maint`；调度员 2026-10-26 定方案甲）

文件 repo2（TOS `nexloop/restic-files/`）原来没有任何维护任务，快照会无限增长。restic 默认按 host+paths 分组，而文件快照每次都来自新的暂存目录，所以按默认分组也永远删不掉（见 `NX-035-restic-maint-groupby.md`）。repo1 是恢复日志的仓库，由 DATA_HOST 的 `nexloop-restic-maint` 维护，这里不碰。

- **unit**：`nexloop-file-backup-maint.service` 加 timer，每天 04:07，随机延迟最多 120 秒。
  - 以 `nexloop-file-backup` 身份运行，与文件备份共用同一个 env 文件和同样的 `@…@` 令牌。
  - 沙箱与 `nexloop-file-backup.service` 相同，包括遮住 `/srv/nexloop`、`InaccessiblePaths`、禁掉两个句柄调用。但更严：
    - 它不读数据，所以没有任何能力（`CapabilityBoundingSet=` 为空）；
    - `/srv/nexloop` 下只露出 repo2 的三个文件（仓库、口令、`offsite.env`；L6 S1），读不到 `recorder.dsn` 和 repo1 的口令；
    - 服务用户的 `~/.ssh` 被 `InaccessiblePaths` 遮住；
    - 没有可写路径。
    
    测试保证两个 unit 只在这几处不同。
- **入口 `scripts/deploy/file_backup_maint.py`**，按顺序：
  1. `forget --retry-lock 10m --group-by host,tags --keep-within 14d --keep-monthly 1 --prune`：每类文件快照各自保留 D4 的窗口；
  2. `check --retry-lock 10m`；
  3. 周日（主机时间）再加 `check --read-data-subset 1/10`。
- **只作用于 repo2**：仓库位置不是 `s3:` 就拒绝（`NotRepo2`），repo1 的 sftp 位置也在拒绝之列。
- **未配置时不会静默通过**：以下情况都以非零退出，并只打印错误类型：
  - 仓库文件、口令文件或对象存储环境文件缺失，或不是 0600 本用户所有；
  - 环境文件里没有两个 AWS 键（`OffsiteKeysMissing`），或有三键之外的键；
  - 仓库不存在（restic 退出码 10）；
  - forget、check 或深度 check 任一失败。
- **输出**：一行 JSON，只有状态和计数，不含任何秘密。
- **避免撞锁**：
  - 文件备份在 :00/:15/:30/:45 跑（加最多 60 秒随机延迟），04:07 加最多 2 分钟正好落在两轮之间，也在 DATA_HOST 的 restic-maint（03:30）之后；
  - prune 用 `--retry-lock 10m`，等正在进行的备份放开共享锁；
  - 反过来，执行者的 backup 和 check 现在也带 `--retry-lock 5m`（`nexloop_eios/file_backup.py`）：在 repo1 或 repo2 上撞到维护任务的排他锁时，这一轮等待，而不是记一条 failed。
- **安装器**：
  - 第 4 步安装两个 unit 和两个 timer；
  - 第 5 步在维护 unit 自己的沙箱里跑一次 `--dry-run`（只做 forget 预演，不删除，不 check），不 ok 就不启用任何 timer；
  - 重跑安装时，第 5 步任一门槛失败（沙箱检查、维护预演、真实运行），都会先 `disable --now` 两个 timer 再退出，输出里写明 `step 5 failed: … disabled`（L6 S2）。这样上一次安装启用的 timer 不会在未通过门槛的配置上继续运行；host-health 随后会报这两个 timer 不新鲜，保持可见；
  - 第 6 步把 `file-backup-maint` 写进两个 host-health 文件，并 `enable --now` 两个 timer；
  - `--uninstall` 移除两个 unit、两个 timer 及其 host-health 条目。
- **告警规则 v21** = v20 加 `timer_stale_file_backup_maint`（`app_host:file-backup-maint`，26 小时，与 restic-maint 同一口径），共 78 条。失败由通配规则 `timer_scheduled_failure` 覆盖，unit 缺失由 `host_unit_missing` 覆盖。APP_HOST 的两份 host-health 示例都已列入 `file-backup-maint`。
- **测试**：
  - `tests/test_file_backup_maint.py`：restic 替身记录参数和环境变量名；
  - `tests/test_file_backup_install.py`：维护 unit 的安装、预演、启用、卸载，以及预演失败时停止；
  - `tests/test_stage_systemd_units.py`：时间错开、`Persistent`；
  - v21 = v20 加一条。
  - 变异验证 27 个，全部杀死。

**每周深度 check 的下载量和费用（L6 S3，估算）**：

- `check --read-data-subset 1/10` 每次随机读出约 1/10 的 pack 数据。数据从 TOS 经公网下载，计公网流出流量和 GET 请求费。
- 每日的 `check` 不读数据，只下载索引和快照元数据，通常在 MB 量级以下。prune 只下载需要重新打包的 pack，通常也很少。
- **现有数据量**（调度员 10-26 的主机核对）：Artifact 根目录约 8 KB，Run 根目录约 17 MB。
  - 每类快照都是当前清单的完整副本，但 restic 会去重和压缩，repo2 的 pack 总量与文件总量同一量级，约 20 MB。
  - 14 天内新增的数据也只按变化量计入。
- **每周下载量** ≈ pack 总量 / 10，现在约 2 MB。
- **费用** ≈ 下载量 × TOS 公网流出单价，再加 GET 请求费（每个 pack 一次请求，现在只有几个到几十个）。
  - 单价以火山引擎 TOS 当期价目表为准，本文不写死。
  - 按每 GB 1 元以内的量级估算，现在每周不到 0.01 元。
- **随数据增长**：每周下载量随 pack 总量线性增长。例如 pack 总量到 10 GB 时，每周约 1 GB，费用按同一单价乘 1 GB。
  - 如果以后觉得太贵，可以把深度 check 改为每月一次，或者改成 `--read-data-subset 1/20`。属于运维取舍，需要负责人决定。

### 6.5 repo1 只追加：DATA_HOST 的 rest-server `--append-only`（负责人 2026-10-27 决定现在就做）

D4 的 sftp 账户对 repo1 有完整的读、写、删除权限。APP_HOST 一旦被攻破，就能删掉 repo1 的全部快照，包括恢复日志的快照（L6 S3）。现在改为由 restic 的 rest-server 以 `--append-only` 提供 repo1：可以写入新数据和新快照，已有的对象都不能删除或覆盖（锁文件除外）。forget/prune 仍由 DATA_HOST 的 `nexloop-restic-maint` 直接对目录执行，不经过 rest-server，所以不受影响。

**DATA_HOST：`scripts/deploy/rest_server_data_host.sh`**（新的 root 脚本，**安装时要负责人本人确认**，不在已给出的 root 授权范围内）

- **为什么单独做一个入口**：`file_backup_data_host.sh` 还保留第 1a 步（recorder 的 pg_hba 行），而它的 `--uninstall` 正好是切换之后撤掉 sftp 账户、chroot 和 sshd drop-in 的命令。rest-server 有自己的二进制、证书、口令和生命周期。分成两个入口，撤 sftp 和装 rest-server 互不牵连，各自可以回滚。
- **R1 二进制**：rest-server 来自发布提交里 `versions.lock.json` 的 `deployment_tools.rest_server` 所指的官方归档。归档的 sha256 必须一致；摘要还没填时直接拒绝。解出唯一的 `rest-server_<版本>_linux_amd64/rest-server`，装到 `/opt/nexloop/rest-server/<版本>/rest-server`（root，0555），再读回 `--version` 核对版本。
- **R2 秘密**：`/srv/nexloop/secrets/rest-server`（root，0711）下放三个文件：
  - `server.key`：0600，属 `nexloop-backup`；
  - `server.chain.crt`：0644，它的 SAN 必须写着 DATA_HOST_ADDR，公钥必须与私钥一致（openssl 读回核对）；
  - `htpasswd`：0600，属 `nexloop-backup`。只有一个用户 `nexloop-file-backup`，用 bcrypt（`htpasswd -niB`，apache2-utils）。口令只经 stdin 传入，只以哈希形式落盘；重跑时不给口令就保留原文件。
- **R3 服务目录**：`/srv/nexloop-rest`（root，0755），里面只有一个 mount unit 绑定进来的 `restic/`（来自 repo_path）。
  - **`--private-repos` 不需要**：服务目录归 root 所有，服务用户无法在里面新建别的仓库，能访问到的只有 `restic/`；htpasswd 里也只有一个用户。如果用了 `--private-repos`，仓库就必须放在 `<path>/<用户名>/` 下，反而要改变恢复日志仓库的位置。
  - 服务目录里出现 `restic/` 以外的任何东西，都会拒绝。
- **R4 防火墙**：`/etc/nexloop/rest-server.nft`（0600 root）定义一张单独的表 `inet nexloop_rest_server`：
  - 对这个端口，IPv6 全部丢弃；IPv4 只放行 APP_HOST_ADDR；
  - 写入后先用 `nft -c` 检查；
  - unit 在启动前（`ExecStartPre=+`，在沙箱之外执行）加载它，停止后（`ExecStopPost=-+`）删除它。端口开着的时候，这条规则一定在。表的写法是"声明、删除、定义"，重新加载不会让规则重复。
  - 不改 `data-host.nft.template`，所以不必重新渲染、重新应用整个 stage 防火墙。
- **R5 unit**：`nexloop-rest-server.service`。
  - 参数：`--path /srv/nexloop-rest --listen <DATA_HOST_ADDR>:<port> --append-only --tls --tls-cert … --tls-key … --htpasswd-file … --max-size <max_size_bytes>`。
  - 以 `nexloop-backup` 身份运行。必须是这个用户：恢复日志在本机写的也是这个仓库，属主要一致。
  - 沙箱与安装器同一标准：没有任何能力；`NoNewPrivileges`；`ProtectSystem=strict`；只有 `restic/` 可写；`TemporaryFileSystem=/srv/nexloop:ro`，只露出自己的 secrets；`InaccessiblePaths` 遮住 `/etc/nexloop`、备份卷本身（`/srv/nexloop-backup`）、sftp 的 chroot 和其他密钥；`SystemCallFilter=@system-service`，并禁掉两个句柄调用；`MemoryDenyWriteExecute`。
  - 安装后读回两件事：服务是 active，防火墙表已加载。
- **`--uninstall`**：移除 unit（它停止时会删掉防火墙表）、nft 文件、mount unit、secrets 和二进制；服务目录为空时一并删除。repo1 本身不动。

**签发证书**（运行单，在负责人的管理机上执行）：

```bash
python3 scripts/deploy/issue_certs.py server --ca-dir <CA_DIR> --out <OUT_DIR> --name rest-server --san IP:<DATA_HOST_ADDR>
```

- 生成 `rest-server.key`（0600）和 `rest-server.chain.crt`（叶证书加中间 CA），有效期 90 天，到期前 30 天轮换。
- 私钥只以文件形式传到 DATA_HOST（例如 `scp` 到 root 的 0700 目录），不经过命令行，也不经过管道打印。
- 配置里的 `tls_key_file` 和 `tls_cert_file` 指向这两个文件。安装器把它们复制到 secrets 之后，源文件可以删掉。
- APP_HOST 要钉住的 CA，用 `<CA_DIR>/bundle.crt`（中间 CA 加根 CA）。

**APP_HOST：`file_backup_install.sh`**

- 配置里 `repo1` 改为 `{"transport": "rest", "host": "<DATA_HOST_ADDR>", "port": <port>, "cacert": "<bundle.crt 在 APP_HOST 上的路径>"}`。`transport` 不写时仍是 `sftp`（D4）。
- stdin 增加一行 `repo1_rest<TAB>password`：就是 htpasswd 那个口令，24–128 个 `A-Z a-z 0-9 _ -`。repo1 的 restic 口令（`repo1` 那一行）不变。
- **仓库文件**：`rest:https://nexloop-file-backup:<口令>@<host>:<port>/restic/`，0600，属服务用户。只有文件路径会交给 restic。重跑时不给口令，就保留已经写好的同一 host:port 的位置。
- **钉住 CA**：
  - `rest-ca.crt` 复制进 `secrets/backup`；
  - env 文件写入 `FILE_BACKUP_REPO1_OPTIONS=--cacert /srv/nexloop/secrets/backup/rest-ca.crt`；
  - unit 的两条 repo1 行带上它，执行者把它原样交给 restic 的 `--cacert`，restic 于是只信任这个 bundle，不用系统根证书。
  - CA 文件不存在时，这一轮不运行，也不记行。
- **SSH**：`~/.ssh/config` 和 `known_hosts` 删除。密钥保留，与 `--uninstall` 的口径一致。
- **第 5 步的门槛改为"能写新快照，但删不掉"**：
  1. 沙箱检查在 repo1 上用一个只有一个文件的目录做一次新快照（tag `nexloop-append-only-probe`，放在 staging 下）；
  2. 对这个快照执行 `restic forget <id>`，必须被拒绝（退出码非 0）；
  3. 随后列快照，它必须还在。
  
  三条任一不满足，timer 不启用；重跑时已启用的 timer 一并停用（§6.4 S2）。探针快照留在 repo1 里，由 DATA_HOST 的 restic-maint 按 tag 分组的保留规则清理。

**迁移路径**：

1. 今天先按 D4 用 sftp 装上（§6.1、§6.2）。
2. 调度员下载 rest-server 归档并核验，把 sha256 填进 `versions.lock.json`，见下文"版本锁"。
3. 负责人签发证书（见上），在 DATA_HOST 执行 `rest_server_data_host.sh`，需要本人确认。
4. APP_HOST 配置改为 `transport: rest`，口令行经 stdin 传入，重跑 `file_backup_install.sh`；第 5 步验证只追加，通过后才启用 timer。
5. 负责人在 DATA_HOST 执行 `file_backup_data_host.sh <配置> --uninstall`：撤掉 sftp 账户、chroot、sshd drop-in 和 mount unit（pg_hba 那一行保留）。

**版本锁**：
- `versions.lock.json` 的 `deployment_tools.rest_server`：版本 `0.13.0`，归档 `rest-server_0.13.0_linux_amd64.tar.gz`，来源 `https://github.com/restic/rest-server/releases/tag/v0.13.0`，许可证 BSD-2-Clause。
- `status` 目前是 `pending_download`，`archive_sha256` 和 `archive_verified` 留空，安装器会拒绝。
- 调度员下载归档后，用该 release 的 `SHA256SUMS` 及其签名核验，再把摘要、`verified_not_installed` 和核验记录填进去。如果届时有更新的版本，三个字段一起改，安装器和测试都从锁里读，不需要改代码。
- `THIRD_PARTY_NOTICES.md` 已重新生成。

**残余风险**：
- APP_HOST 被攻破时，攻击者仍然可以读取全部快照（restic 口令本来就在 APP_HOST）、写入垃圾数据或伪造快照。体量由 `--max-size` 限制；恢复时按 FILE-MANIFEST 和库中的清单行核对。
- 不能删除或覆盖已有对象。
- rest-server 是一个新的对外端口，有四道限制：TLS、认证、只放行 APP_HOST 的防火墙表、unit 沙箱。
- 后续项：把 `nexloop-rest-server.service` 加进 DATA_HOST 的 host-health services。`crash_loop` 和 `host_unit_missing` 是通配规则，加进去就会覆盖它。
