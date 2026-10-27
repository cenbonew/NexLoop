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
| D4 | **负责人 2026-10-27 改为独立仓库**：文件备份在 DATA_HOST 上用自己的 restic 仓库（独立口令，Keychain `nexloop/backup/restic-files-repo1`），经 rest-server `--append-only` 写入，不再与恢复日志共用，也不再用 sftp。原因（L6 复审 F1）：能写入共用仓库的一方可以伪造一个未来时间的快照（例如 `--time 2030 --tag journal`），restic 的 `--keep-within` 以组内最新快照为基准，下一次 forget --prune 就会把真实的恢复日志快照全部删掉。原决定（2026-10-26）是"repo1 经受限 sftp 写到 DATA_HOST，与恢复日志共用"，从未在 stage 上安装 |
| D5 | restic 用 `versions.lock.json` 里的官方二进制，安装时在主机上核对归档的 sha256 |
| D6 | 先启用；“有可备份对象却从未有过 verified 文件备份”的告警另开一项，放进下一个规则版本 |

## 6. 安装：两个入口

第 1 步在 DATA_HOST（1a pg_hba：`file_backup_data_host.sh`；1b 文件仓库和 rest-server：`rest_server_data_host.sh`），第 2–6 步在 APP_HOST，各入口分别执行。

### 6.1 APP_HOST：`scripts/deploy/file_backup_install.sh`（第 2–6 步）

私有配置见 `deploy/examples/file-backup-install.app-host.example.json`（0600 root）。只用标准库；以 root 运行之前先做信任检查：

- 发布检出及其 `.git` 的每个目录、`objects/` 以外的每个文件，属主都必须是 root，且组和其他人不可写；整条路径用 `lstat` 逐级检查，路径上不能有链接；
- `.git` 必须是目录，不能有 `objects/info/alternates` 或 `commondir`；
- `git show` 带 `--no-replace-objects`，在最小环境中运行（`env -i`，不读系统或全局配置）；
- unit 的解释器（host-health venv 的 `bin/python`）逐跳解析链接；
- 复用已有发布时逐个文件与提交核对。

口令只经 stdin，只写进 0600 文件；任何输出、参数和日志里都没有口令、DSN 或私钥。所有配置文件都原子写入（同目录临时文件再改名）；两个 host-health 文件要么都改，要么都不改（第二个写失败时恢复第一个）。

顺序（每步一条命令加一个配置文件；口令从负责人的 Keychain 经管道传入）：

1. DATA_HOST 第 1 步（§6.2）：1a pg_hba；1b 文件仓库和 rest-server，加只追加验证和 restic-maint 的守卫（§6.5）。
2. `sudo bash file_backup_install.sh <配置> --prepare`：
   - 建系统用户 `nexloop-file-backup`，home 为 `/var/lib/nexloop-file-backup`，属主 root，无 shell；
   - 建 `/srv/nexloop/secrets/backup`（root 0711）和 `/etc/nexloop/file-backup.env`；
   - 不生成 SSH 密钥：D4 已改为 rest-server。
3. `install_repo2_secrets.sh files`：repo2 的仓库、口令和对象存储环境文件，路径从 env 文件里读。
4. 口令经 stdin 传入：`printf 'recorder\t%s\nrepo1\t%s\nrepo1_rest\t%s\n' … | sudo bash file_backup_install.sh <配置>`。
   - `recorder` 是 `nexloop_backup_recorder` 现有的口令，不轮换；
   - `repo1` 是**文件仓库的 restic 口令**，Keychain `nexloop/backup/restic-files-repo1`，就是 DATA_HOST 第 1b 步 `files` 行用的那一个；
   - `repo1_rest` 是 rest-server 的 htpasswd 口令（见 §6.5）；
   - 重跑时可以不给这些行，已写的文件保留。

各步内容：

| 步 | 内容 |
|---|---|
| 2 | 用户（以前 sftp 版本可能留下的 `~/.ssh` 文件会被删除）；发布目录 `/opt/nexloop/file-backup/<sha>/`：执行者的四个模块、unit 入口 `file_backup_run.py`、沙箱自检 `file_backup_sandbox_check.py`、两个 unit 模板，只读；`current` 指向 `<sha>`。restic 从配置给出的归档解压到 `/opt/nexloop/file-backup/restic-<版本>/restic`（root，0555）；归档的 sha256 必须等于发布提交里 `versions.lock.json` 的 `deployment_tools.restic.archive_sha256`，否则拒绝，什么都不写 |
| 3 | `secrets/backup`：`recorder.dsn`（hostssl，`verify-full`，CA 复制为同目录的 `pg-ca.crt`）、repo1 的仓库文件（`rest:https://…`，见 §6.5）和口令文件，都是 0600、属于服务用户；`rest-ca.crt`（stage CA bundle，0644）。这个目录里如果有清单之外的任何文件，就拒绝（D3：沙箱能看到整个目录） |
| 4 | `/etc/nexloop/file-backup.env`（0600 root）。保留 `install_repo2_secrets.sh` 写的 `FILE_BACKUP_REPO2_OPTIONS`。从发布中安装 unit 和 timer，填入 `@PYTHON@`、`@RESTIC@`、`@MDWE@`，然后 `daemon-reload` |
| 5 | **D3 沙箱验证**：用 `systemd-run` 以 unit 自己 `[Service]` 的全部设置运行自检（同一用户、同一能力、同一挂载）。通过条件：两个数据目录和本 unit 自己的文件可读；`/srv/nexloop` 只显示 `data`、`secrets`；`secrets` 只显示 `backup`；`data` 只显示 `artifacts`、`runtime`；unit 的每个 `InaccessiblePaths=` 以及 `/srv/nexloop`、`secrets`、`data` 下的其余条目都看不到内容（不存在、被拒，或读出为空，即 `InaccessiblePaths` 挂上的空节点）；两个句柄系统调用 `open_by_handle_at`、`name_to_handle_at` 都被过滤（返回 EPERM；L6 F1：持有 `CAP_DAC_READ_SEARCH` 的进程可以借这两个调用，从任一可见 fd 打开同一文件系统上的任意 inode，绕过上面所有遮挡，即 shocker 类逃逸，所以 unit 用 `SystemCallFilter=~open_by_handle_at name_to_handle_at` 禁掉它们）；restic 是锁定的版本；两个仓库都能用口令打开（`cat config`）。`repo2_init` 为 true 时，仓库不存在才执行一次 `restic init`；repo1 从不初始化。然后真实运行一次 service，四条 ExecStart 都必须是 `verified`。任何一项不通过，timer 都不启用 |
| 6 | 在两个 host-health 文件里加 `file-backup`；`nexloop-provider-drift.timer` 已安装时也加上 `provider-drift`（APP_HOST 的探针配置原来缺这一项）。然后 `systemctl enable --now nexloop-file-backup.timer` |

**`InaccessiblePaths=` 是黑名单**（L6 S1）：第 5 步只能证明列出的路径被遮住，不能证明 APP_HOST 上没有遗漏。现有的密钥位置都已覆盖：`/srv/nexloop/secrets`（由 TemporaryFileSystem 遮住）、`/etc/nexloop`、`/var/lib/nexloop`、`/var/lib/nexloop-host-health`、`/etc/pgbackrest`；`/root` 和 `/home` 由 ProtectHome 遮住。**以后在 APP_HOST 的 `/srv/nexloop` 之外新增任何密钥位置，都要同时加进这个 unit 的 `InaccessiblePaths=`**，第 5 步随后也会检查它。

`--uninstall`：
- 移除：timer、unit、drop-in、env 文件、发布目录、restic、暂存目录、`secrets/backup` 里的文件，以及两个 host-health 文件中的 `file-backup`；
- 保留：`provider-drift`，以及用户和 home（以前 sftp 版本可能留下的 SSH 文件一并删除）。

退出码：0 完成，1 某步失败，2 拒绝。

### 6.2 DATA_HOST：第 1 步

由负责人以 root 执行，每步一条命令加一个私有配置，安装时都要负责人本人确认。

**1a `scripts/deploy/file_backup_data_host.sh`**（配置 `deploy/examples/file-backup-data-host.example.json`，0600 root）：
- 只装 pg_hba 这一行（D2），不读取、不写入任何秘密。信任检查与 §6.1 相同：发布检出、`.git`、root 要执行的目录树、解释器都要通过。
- **pg_hba**：
  - 用发布中的模板和已部署的 stage.env 重新渲染。渲染结果与已安装的 pg_hba 相比，只能多出这一条 `hostssl … nexloop_backup_recorder <APP_HOST_ADDR>/32 scram-sha-256`，不能删掉任何一行。
  - 写入方式与其他 hba 脚本相同：原地写入（同一 inode；旧内容原子写入 `config/pg_hba.conf.before-file-backup`，0600 root），确认容器里看到的是新文件，然后 HUP PostgreSQL，并从日志确认重新加载成功。任一步不成立，就把旧内容原地写回并重新加载。
  - recorder 的口令不变，这里不设置。
- **写入时的防护（L6 I1、I2）**：
  - 临时目录 `/var/lib/nexloop/file-backup-data-host` 先通过信任检查再 chmod，root 才在里面运行渲染；
  - `config/` 目录必须属于 root、组和其他人不可写；`pg_hba.conf` 和它的备份文件都不能是链接；
  - 写 pg_hba 时以 `O_NOFOLLOW` 打开，并核对打开的 inode 就是 lstat 看到的那个。
- **不再安装 sftp**：受限 sftp 账户、chroot 和 sshd drop-in 都不装（D4 于 2026-10-27 改为独立仓库）。旧配置里的 `repo_path`、`app_host_public_key` 仍能被接受，但会被忽略。
- **`--uninstall`**：只清理以前版本可能装过的 sftp 部件，也就是 sshd drop-in、授权公钥、mount unit，以及空的 chroot 目录。
  - 每一项都是"存在才删"，所以可以重复执行；只有 drop-in 原来存在时，才重新加载 sshd。
  - stage 上从没装过这些部件，执行时会输出 `no D4 sftp part was there`。
  - pg_hba 中的这一行保留：模板里有它，而没有 APP_HOST 的 DSN 时，没人能用它连接。

**1b `scripts/deploy/rest_server_data_host.sh`**：文件仓库和 rest-server `--append-only`，见 §6.5。之后还有两步：只追加验证，以及给 restic-maint 加守卫和文件仓库的维护行。

**合并顺序提示**：模板加了这一行之后，host-health、sim、evolution 三个 hba 脚本只接受各自的行。如果在本步之前重跑它们中的任何一个，会因为“多出的行不是自己的”而拒绝。所以先执行本步。

### 6.3 测试与局限

- `tests/test_file_backup_install.py`：
  - 在临时根目录下用脚本化的 runner 运行，不需要 root、systemd、数据库或 restic；
  - 信任检查按本测试的 uid 真实执行；tmp_path 以上的目录视为 root 所有，所以在 `--basetemp` 位于 /tmp 时也能通过；
  - 沙箱自检脚本和 unit 入口真实运行，restic 用一个替身脚本。
- `tests/test_file_backup_data_host.py`：DATA_HOST 第 1a 步，同样在临时根目录下用脚本化的 runner 运行（没有容器）；包括 `--uninstall` 清理以前 sftp 部件，以及重复执行的幂等性。
- 变异验证：APP_HOST 安装器和自检脚本共 40 个变异，DATA_HOST 入口 23 个变异，全部被测试杀死。
- **未覆盖**：没有在真实 systemd 下运行，也没有用真实的 restic、rest-server 或 PostgreSQL。真机上的保障是 DATA_HOST 的只追加验证（§6.5）和 APP_HOST 第 5 步当场验证：两个仓库都能打开，探针快照删不掉，真实运行四条都是 verified。验证不通过，timer 就不启用。

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
- **只作用于 repo2**：仓库位置不是 `s3:` 就拒绝（`NotRepo2`），repo1 的 rest 位置也在拒绝之列。
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

### 6.5 repo1：DATA_HOST 上的独立文件仓库，经 rest-server `--append-only` 写入（负责人 2026-10-27）

原 D4 方案经受限 sftp 写入、与恢复日志共用仓库。那样 APP_HOST 的 sftp 密钥对仓库有完整的读、写、删除权限；APP_HOST 一旦被攻破，就能删掉全部快照，包括恢复日志的快照（L6 S3）。这个方案从未在 stage 上安装。现在由 restic 的 rest-server 以 `--append-only` 提供文件备份**自己的**仓库：可以写入新数据和新快照，已有的对象都不能删除或覆盖（锁文件除外）。forget/prune 仍由 DATA_HOST 的 `nexloop-restic-maint` 直接对目录执行，不经过 rest-server，所以不受影响。

**L6 复审 F1（确认级）及修法**：只追加挡不住"伪造未来时间的快照"。持有仓库口令的 APP_HOST 可以写一个 `--time 2030-01-01 --host nexloop --tag journal` 的快照。restic 的 `--keep-within` 和 `--keep-monthly` 以组内**最新**快照为基准，不是以当前时间为基准；于是下一次维护的 forget --prune 会把这一组真实的快照全部删掉，等于 DATA_HOST 替攻击者删了数据。两项都已做：

- **(a) 未来时间守卫**：`scripts/deploy/restic_future_guard.py`（只用标准库）。
  - DATA_HOST 的 restic-maint 在每条 forget 之前都多一行守卫：`restic_maint_data_host.sh` 把它装到 `/opt/nexloop/restic-maint/`（root，只读），用主机的 `/usr/bin/python3` 运行；
  - 守卫先执行 `snapshots --json --no-lock`。只要有快照的时间晚于当前时间加 1 小时，或者有读不出的时间，就以非 0 退出；unit 停在这一步，不执行 forget/prune，`timer_scheduled_failure` 告警；
  - `restic_maint_data_host.sh` 自己的预演也先跑守卫：守卫不通过，就什么都不写；
  - APP_HOST 的 repo2 维护（`file_backup_maint.py`，§6.4）在 forget 之前做同样的检查。
- **(b) 分仓**：文件备份不再与恢复日志共用 repo1。
  - DATA_HOST 新建一个文件专用仓库（配置项 `files_repo_path`，例如 `/srv/nexloop/backups/files-repo1/restic`，属 `nexloop-backup`），用一个新的 restic 口令；
  - rest-server 只提供这一个仓库，恢复日志的仓库对 APP_HOST 完全不可见：配置里的 `journal_repo_path` 只用来核对两者不重合、也不互相嵌套；
  - APP_HOST 的 `repo1` 口令行改为这个文件仓库的口令，不再复用恢复日志的口令；
  - restic-maint 增加这个文件仓库的维护行：`restic_maint_data_host.sh --files-repo … --files-password-file …`，包括守卫、按 tag 分组的 forget、check、周日深度 check，以及它的 `ReadWritePaths`。
  - 负责人 2026-10-27 已确认：D4 改为独立仓库（§5）。

**L6 复审 F2（确认级，55271922 的复审）及修法**：守卫和 forget 是两条独立的 ExecStart，中间有空档。APP_HOST 可以先持有一把锁，等守卫通过；forget 带 `--retry-lock 10m` 在等这把锁的时候，APP_HOST 再写入一个 2030 年的快照，然后释放锁。forget 随后按新的"最新快照"计算保留窗口，把文件仓库里的真实快照全部删掉。修法按 L6 给的 (b)：维护文件仓库期间停掉 rest-server。`restic_maint_data_host.sh --files-repo` 写入的文件仓库块依次是：

1. `restic … unlock`：只删 restic 自己判为陈旧的锁（30 分钟没有刷新，没有活进程持有）；
2. `restic_future_guard.py --wait-unlocked 600`：等到仓库里没有任何锁（每 10 秒查一次 `list locks --no-lock`），也就是在两轮文件备份之间、而不是一轮的中途停服；10 分钟内锁一直在，unit 失败并告警，不停服、不 forget；
3. `ExecStart=+/usr/bin/systemctl stop nexloop-rest-server.service`（`+`：在沙箱之外，以 root 执行）；
4. 守卫（与其他仓库相同）；
5. `forget … --prune`；
6. `ExecStart=+/usr/bin/systemctl start nexloop-rest-server.service`：forget/prune 一结束就拉起，之后的 `check` 和周日深度 check 在服务恢复后进行；
7. `ExecStopPost=+/usr/bin/systemctl start …`、`ExecStopPost=/bin/sleep 3`、`ExecStopPost=+/usr/bin/systemctl is-active --quiet …`：无论哪一步失败（等锁超时、守卫拒绝、forget 失败），unit 结束时都会再拉起一次；3 秒后不是 active（例如证书出错、启动即崩溃、进入自动重启），unit 以非 0 结束，`timer_scheduled_failure` 告警。

8. `TimeoutStartSec=6h`（L6 S5）：forget 或 prune 卡住时，6 小时后 unit 被终止并失败，`ExecStopPost` 照样拉起 rest-server，它不会一直停着。unit 自己另有 `TimeoutStartSec` 时，`--files-repo` 拒绝执行（systemd 只认最后一个），要手工去掉。

rest-server 停下以后，APP_HOST 写不进任何东西，所以守卫看到的快照集合，就是 forget 看到的集合。测试 `tests/test_restic_maint_data_host.py` 用一个模型按行执行这个块，在守卫通过之后、forget 之前追加一个 2030 年的快照：新块里 forget 删掉的集合与没有攻击时完全相同；55271922 的旧块（不停服）则会删掉全部 30 个真实快照。重跑 `--files-repo` 会把旧块整个换成新块。`--files-repo` 要求 rest-server 已经 active，`/usr/bin/systemctl` 存在。

**维护窗口与告警**（restic-maint 的 timer 是 03:30，随机延迟最多 10 分钟；文件仓库块排在两个恢复日志仓库之后）：

- 停服窗口 = 文件仓库的守卫 + forget/prune 的时长。文件仓库很小，预计在一分钟以内。在这个窗口里**开始**的那一轮文件备份会失败：它的第一条 ExecStart（artifact → repo1）连不上，整个 unit 失败，同一轮的 repo2 也不做。每天最多一轮；因为先等锁再停服，正在进行的一轮不会被打断，撞上的概率大约是窗口长度 / 15 分钟。
- `timer_stale_file_backup`（阈值 2700 秒，即三个周期）：只丢一轮时，距上次成功最多约 30 分钟，不会触发。窗口要超过约 29 分钟、连丢两轮以上才会接近阈值。
- `file_backup_age`、`file_backup_unprotected`（阈值都是 3600 秒）：丢一轮时，最新的已验证快照最多约 30 分钟前，不会触发。
- **`timer_scheduled_failure`（selector `*`，24 小时内有任何一次定时运行失败就告警）会因被撞上的那一轮报一次警告**，24 小时后自动消失。这不是假象：那一轮确实没有备份。要做到零告警，需要另开任务（例如 host-health 对这一窗口豁免，或文件备份对连接被拒做短时重试），本次没有做。
- 卸载 rest-server 之前，先把这个块从 restic-maint 里去掉（`restic_maint_data_host.sh --revert`，再不带 `--files-repo` 运行一次）。L6 S6：只要 restic-maint 里还有这个块，或者有任何一行（注释不算）提到 `nexloop-rest-server.service`，或者 restic-maint 的 unit 文件是符号链接，`rest_server_data_host.sh --uninstall` 就拒绝执行，什么都不删。
- L6 S7：`ExecStopPost` 失败会让 unit 失败，这一点在真实 systemd 上实测。`rest_server_data_host.sh` 在第 R1 步之前执行 `systemd-run --wait --quiet --collect -p ExecStopPost=/bin/false /bin/true`，退出码必须非 0；如果是 0，说明这台主机的 systemd 不会因为 `ExecStopPost` 失败而判 unit 失败，维护后的"rest-server 已回到 active"检查也就无法告警，安装器拒绝执行，什么都不装。

**DATA_HOST：`scripts/deploy/rest_server_data_host.sh`**（新的 root 脚本，**安装时要负责人本人确认**，不在已给出的 root 授权范围内）

- **为什么单独做一个入口（第 1b 步）**：`file_backup_data_host.sh` 只做第 1a 步（recorder 的 pg_hba 行），它不读也不写任何秘密。rest-server 有自己的二进制、证书、两个口令和生命周期。分成两个入口，每一步都可以单独重跑或撤销，互不牵连。
- **stdin 两行，口令都从 Keychain 经管道传入**：
  - `files<TAB>…`：文件仓库的 restic 口令，Keychain `nexloop/backup/restic-files-repo1`。安装器用它以 `nexloop-backup` 身份 init 仓库；APP_HOST 的 `repo1` 行也用它。
  - `rest<TAB>…`：rest-server 的 htpasswd 口令，24–128 个 `A-Z a-z 0-9 _ -`。建议 Keychain 条目名 `nexloop/backup/rest-server-files`，由调度员新建（随机生成）；APP_HOST 的 `repo1_rest` 行也用它。

  ```bash
  { printf 'rest\t%s\n' "$(security find-generic-password -s nexloop/backup/rest-server-files -w)"
    printf 'files\t%s\n' "$(security find-generic-password -s nexloop/backup/restic-files-repo1 -w)"; } \
    | ssh <DATA_HOST> 'sudo bash <release>/scripts/deploy/rest_server_data_host.sh <私有配置>'
  ```
- **R1 二进制**：rest-server 来自发布提交里 `versions.lock.json` 的 `deployment_tools.rest_server` 所指的官方归档。归档的 sha256 必须一致；摘要还没填时直接拒绝。解出唯一的 `rest-server_<版本>_linux_amd64/rest-server`，装到 `/opt/nexloop/rest-server/<版本>/rest-server`（root，0555），再读回 `--version` 核对版本。
- **R2 秘密**：`/srv/nexloop/secrets/rest-server`（root，0711）下放三个文件：
  - `server.key`：0600，属 `nexloop-backup`；
  - `server.chain.crt`：0644，它的 SAN 必须写着 DATA_HOST_ADDR，公钥必须与私钥一致（openssl 读回核对）；
  - `htpasswd`：0600，属 `nexloop-backup`。只有一个用户 `nexloop-file-backup`，用 bcrypt（`htpasswd -niB`，apache2-utils）。口令只经 stdin 传入，只以哈希形式落盘；重跑时不给口令就保留原文件。
- **R3 文件仓库与服务目录**：
  - 文件仓库的口令经 stdin（`files<TAB>password`）写入 `/srv/nexloop/secrets/files-repo/restic.pass`。目录 root 0711；文件 0600，属 `nexloop-backup`；不在 rest-server 的沙箱里。
  - 仓库目录不存在时，为 `nexloop-backup` 建好，并以它的身份执行一次 `restic init`；已有 config 就不再 init。目录存在但既不是仓库、也不是空目录时，拒绝。之后以 `nexloop-backup` 执行 `cat config`，确认口令能打开仓库。
  - L6 S1：stdin 给出的口令先写到 `restic.pass.new`，init（需要时）和 `cat config` 都用它；能打开仓库，才替换 `restic.pass`。重跑时传错口令，安装器以 1 退出，`restic.pass` 保持原样，`restic.pass.new` 被删掉。
  - L6 S2：`files_repo_path` 和 `journal_repo_path` 先做 realpath 再比较，经符号链接指向恢复日志仓库（或其上下级）的路径同样被拒绝。
  - 服务目录 `/srv/nexloop-rest`（root，0755），里面只有一个 mount unit 绑定进来的 `restic/`，来源是文件仓库。
  - **`--private-repos` 不需要**：服务目录归 root 所有，服务用户无法在里面新建别的仓库，能访问到的只有 `restic/`；htpasswd 里也只有一个用户。如果用了 `--private-repos`，仓库就必须放在 `<path>/<用户名>/` 下，反而多一层约束。
  - 服务目录里出现 `restic/` 以外的任何东西，都会拒绝。
- **R4 防火墙**：`/etc/nexloop/rest-server.nft`（0600 root）定义一张单独的表 `inet nexloop_rest_server`：
  - 对这个端口，IPv6 全部丢弃；IPv4 只放行 APP_HOST_ADDR；
  - 写入后先用 `nft -c` 检查；
  - unit 在启动前（`ExecStartPre=+`，在沙箱之外执行）加载它，停止后（`ExecStopPost=-+`）删除它。端口开着的时候，这条规则一定在。表的写法是"声明、删除、定义"，重新加载不会让规则重复。
  - 不改 `data-host.nft.template`，所以不必重新渲染、重新应用整个 stage 防火墙。
- **R5 unit**：`nexloop-rest-server.service`。
  - 参数：`--path /srv/nexloop-rest --listen <DATA_HOST_ADDR>:<port> --append-only --tls --tls-cert … --tls-key … --htpasswd-file … --max-size <max_size_bytes>`。
  - 以 `nexloop-backup` 身份运行，与 restic-maint 对这个仓库做 forget/prune 时的属主一致。
  - 沙箱与安装器同一标准：没有任何能力；`NoNewPrivileges`；`ProtectSystem=strict`；只有 `restic/` 可写；`TemporaryFileSystem=/srv/nexloop:ro`，只露出自己的 secrets；`InaccessiblePaths` 遮住 `/etc/nexloop`、备份卷本身（`/srv/nexloop-backup`）、sftp 的 chroot 和其他密钥；`SystemCallFilter=@system-service`，并禁掉两个句柄调用；`MemoryDenyWriteExecute`。
  - 安装后读回两件事：服务是 active，防火墙表已加载。
- **`--uninstall`**：移除 unit（它停止时会删掉防火墙表）、nft 文件、mount unit、rest-server 的 secrets 和二进制；服务目录为空时一并删除。文件仓库和它的口令保留，因为里面是文件备份。恢复日志的仓库从来不碰。restic-maint 里还有文件仓库块时拒绝执行（L6 S6），先按上面 F2 一节去掉它。

**只追加的一次性验证**（运行单中的一步，L6 I1；负责人在 DATA_HOST 以 root 执行，装好 rest-server 之后、APP_HOST 切换之前）：

```bash
sudo bash scripts/deploy/rest_server_append_only_check.sh --rest-server /opt/nexloop/rest-server/<版本>/rest-server --restic /usr/local/bin/restic
```

- **准备**：在 `/var/lib/nexloop/rest-append-only-check/<随机>/` 里，用随机口令 `restic init` 一个**一次性仓库**，并做一次 backup，得到 config、keys、data、index 和一个快照。**绝不在真实仓库上做**：万一覆盖没有被挡住，会写坏真实数据。
- **起服务**：用同一个 rest-server 二进制，以 `nexloop-backup` 身份另起一个只监听 127.0.0.1 的 `--append-only --no-auth` 实例。发任何探针之前（L6 S4），这个临时进程必须还在运行，而且 `GET /config` 返回的内容必须与这个一次性仓库的 config 逐字节相同；否则（端口被别的服务占着、二进制拒绝参数）以 1 退出，一个 DELETE 或 POST 都不发。
- **逐项检查**（用 curl）：
  - data、index、keys、snapshots 各取一个已有对象：DELETE 和覆盖写都必须返回 403。覆盖写用的是 POST，这是 restic 写入的方式；rest-server 没有 PUT。
  - config：覆盖写必须返回 403；DELETE 不能成功，接受 403 或 405（rest-server 没有这条删除路由时返回 405）。
  - locks：新建一个锁再删除，都必须成功（200），这是只追加唯一允许的删除。
  - 结束时，原有的每个对象都必须逐字节不变。
- **收尾**：无论结果如何，最后都停掉临时实例，删掉一次性仓库。输出一行 JSON；全部符合预期才退出 0。

**签发证书**（运行单，在负责人的管理机上执行）：

```bash
python3 scripts/deploy/issue_certs.py server --ca-dir <CA_DIR> --out <OUT_DIR> --name rest-server --san IP:<DATA_HOST_ADDR>
```

- 生成 `rest-server.key`（0600）和 `rest-server.chain.crt`（叶证书加中间 CA），有效期 90 天，到期前 30 天轮换。
- 私钥只以文件形式传到 DATA_HOST（例如 `scp` 到 root 的 0700 目录），不经过命令行，也不经过管道打印。
- 配置里的 `tls_key_file` 和 `tls_cert_file` 指向这两个文件。安装器把它们复制到 secrets 之后，源文件可以删掉。
- APP_HOST 要钉住的 CA，用 `<CA_DIR>/bundle.crt`（中间 CA 加根 CA）。

**APP_HOST：`file_backup_install.sh`**

- 配置里的 `repo1` 写成 `{"host": "<DATA_HOST_ADDR>", "port": <port>, "cacert": "<bundle.crt 在 APP_HOST 上的路径>"}`，`"transport": "rest"` 可写可不写。旧的 sftp 写法（`host_key`、`path`）会被拒绝。
- stdin 增加一行 `repo1_rest<TAB>password`：就是 htpasswd 那个口令，24–128 个 `A-Z a-z 0-9 _ -`。
- `repo1` 那一行是**文件仓库的 restic 口令**（Keychain `nexloop/backup/restic-files-repo1`），即 DATA_HOST 的 `files<TAB>` 那一个，不是恢复日志的口令。给错的话，第 5 步的 `cat config` 返回 12，timer 不会启用。
- **仓库文件**：`rest:https://nexloop-file-backup:<口令>@<host>:<port>/restic/`，0600，属服务用户。只有文件路径会交给 restic。重跑时不给口令，就保留已经写好的同一 host:port 的位置。
- **钉住 CA**：
  - `rest-ca.crt` 复制进 `secrets/backup`；
  - env 文件写入 `FILE_BACKUP_REPO1_OPTIONS=--cacert /srv/nexloop/secrets/backup/rest-ca.crt`；
  - unit 的两条 repo1 行带上它，执行者把它原样交给 restic 的 `--cacert`，restic 于是只信任这个 bundle，不用系统根证书。
  - CA 文件不存在时，这一轮不运行，也不记行。
- **SSH**：不再使用。以前 sftp 版本可能留下的 `~/.ssh` 文件，安装、`--prepare` 和 `--uninstall` 都会删除。
- **第 5 步的门槛改为"能写新快照，但删不掉"**：
  1. 沙箱检查在 repo1 上用一个只有一个文件的目录做一次新快照（tag `nexloop-append-only-probe`，放在 staging 下）；
  2. 对这个快照执行 `restic forget <id>`，必须被拒绝（退出码非 0）；
  3. 随后列快照，它必须还在。
  
  三条任一不满足，timer 不启用；重跑时已启用的 timer 一并停用（§6.4 S2）。
- **探针快照（L6 S1）**：**每次运行安装器都会在文件仓库里留下一个探针快照**（tag `nexloop-append-only-probe`，内容是一个一行的文件）。只追加模式下删不掉它，这正是要验证的。它们由 DATA_HOST 的 restic-maint 按 tag 分组清理：保留 14 天内的全部，再加最新月份的一个。几十个这样的快照只占几 KB。

**安装顺序**（D4 已改为独立仓库：今天直接按这个顺序上线，不再先装 sftp 版）：

1. 负责人在管理机上签发证书（见上），把私钥和证书链以文件形式传到 DATA_HOST。
2. DATA_HOST（负责人本人确认，root）：
   1. `file_backup_data_host.sh`：pg_hba 那一行（§6.2，第 1a 步）；
   2. `rest_server_data_host.sh`：新建并初始化文件仓库，安装 rest-server（第 1b 步，口令见上）；
   3. `rest_server_append_only_check.sh`：一次性仓库上的只追加验证（见上）；
   4. `restic_maint_data_host.sh --files-repo <files_repo_path> --files-password-file /srv/nexloop/secrets/files-repo/restic.pass`：给每条 forget 加守卫，并加上文件仓库的维护（维护期间停掉 rest-server，见上面 F2 一节）。
3. APP_HOST：
   1. `file_backup_install.sh --prepare`；
   2. `install_repo2_secrets.sh files`；
   3. `file_backup_install.sh`，stdin 三行：`recorder`、`repo1`（文件仓库口令）、`repo1_rest`（htpasswd 口令）。第 5 步验证只追加，通过后才启用 timer。
4. 以前的 sftp 部件从没在 stage 上装过，不用撤。如果别的环境装过，`file_backup_data_host.sh --uninstall` 会清掉，可以重复执行。

**版本锁**：
- `versions.lock.json` 的 `deployment_tools.rest_server`：版本 `0.14.0`，归档 `rest-server_0.14.0_linux_amd64.tar.gz`（3799947 字节），来源 `https://github.com/restic/rest-server/releases/tag/v0.14.0`，许可证 BSD-2-Clause。
- 2026-10-27 已由调度员在 CI 主机上核验：官方 `SHA256SUMS` 的签名者指纹与 restic 0.18.0 相同，gpg 显示 Good signature，归档的 sha256 与其中那一行一致。`status` 为 `verified_not_installed`，摘要和字节数都已记入。
- 安装器在主机上再核对一次字节数和 sha256。归档必须是 root 所有、组和其他人不可写的副本：下载的文件在另一个账户的 home 里，先用 `install -o root -g root -m 0644` 复制，配置里的 `rest_server_archive` 指向这个副本。
- 这是 gzip tar 包，不是 restic 那样的 bz2 单文件。安装器在内存里读出其中那一个普通文件 `rest-server`（位于顶层或下一级目录），从不按包内路径解压到磁盘，所以不存在路径穿越。符号链接、硬链接或出现第二个候选都拒绝。
- `THIRD_PARTY_NOTICES.md` 已重新生成。

**残余风险**：
- APP_HOST 被攻破时，攻击者仍然可以读取文件仓库的全部快照（这个口令本来就在 APP_HOST），也可以写入垃圾数据或伪造快照。
  - 体量由 `--max-size` 限制；恢复时按 FILE-MANIFEST 和库中的清单行核对。
  - 伪造未来时间的快照会让守卫拒绝维护并告警，不会让真实快照被删（F1 a）；守卫与 forget 之间 rest-server 是停着的，这时写不进新快照（F2）。
  - 持续持有一把锁，可以让文件仓库的维护每天等锁超时而失败（告警），但删不掉任何东西。
  - 恢复日志在另一个仓库里，攻击者碰不到（F1 b）。
- 不能删除或覆盖已有对象。
- rest-server 是一个新的对外端口，有四道限制：TLS、认证、只放行 APP_HOST 的防火墙表、unit 沙箱。
- 后续项：把 `nexloop-rest-server.service` 加进 DATA_HOST 的 host-health services。`crash_loop` 和 `host_unit_missing` 是通配规则，加进去就会覆盖它。
