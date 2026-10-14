# NX-035 切片三：文件备份执行者 `nexloop-file-backup`

分支 `l5/nx035-file-backup`，从 main 7fb06a2 切出。迁移 `0450_nx035_file_backup_inventory.sql` 是临时号，合入时由调度员重编号；`versions.lock.json` 的 `bootstrap_revision` 也在合入时一并更新，本分支不改它。

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

## 2. 迁移 0450：owner 侧清单

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

1. **读清单**：从 0450 按页读出全部条目。条目的 ref 不符合规定形状时，整次运行停止，什么都不写。
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
  - 0450 只列出有效的 Artifact 和已终态、未清除的 Run，不包括其他 world，跨租户分页正确；
  - 只有 recorder 能调用，参数校验生效，recorder 仍然读不到表；
  - 两类各一次端到端运行，经 0146 写入 verified 行。
- **未覆盖**：没有用真实的 restic 运行。开发机和 CI 主机都没有安装 restic；版本锁与安装由 L2 处理，安装后应在部署主机上用真实 restic 跑一次第 3 步。

## 5. 部署前待定的三项（B 类，由调度员与负责人决定）

1. **recorder 的连接位置**：设计 §4.1 规定 recorder 只能从 DATA_HOST 本机 socket 连接，而文件执行者运行在 APP_HOST，所以需要一条只允许 `nexloop_backup_recorder` 从 APP_HOST 经 TLS 连接的 pg_hba 规则，或者改为把清单读取和记录交给 DATA_HOST 上的代理执行。目前倾向前者：这个角色只能执行三个记录函数和本清单函数。
2. **读权限**：执行者用户需要能只读访问 Artifact 根目录和 Agent Host 的 Run 根目录，这两个目录的属主是 API 和 Agent Host 的服务用户。可以用一个只读组实现，或者以 ACL 方式只授予读和遍历权限，不授予写权限。
3. **仓库位置**：文件 repo1 在 DATA_HOST，经受限 SSH 身份以 `sftp:` 方式写入（设计 §3.1）；repo2 在 S3。两者的位置都只写在仓库文件里。
