# NX-035 部署切片：`nexloop-backup pg` 改用 pgBackRest，备份清单 schema v2

依据：
- 调度员 2026-10-14，对应 L5 S5 核对 NX-035 (b) 第 1、4 项；
- [设计稿](NX-035-design.md) D1；
- [部署运行手册](NX-035-deploy-runbook.md) 第 2 步。

同期进行：
- L4 做 `nexloop-restore`，读本清单；
- L5 做 `nexloop-file-backup`（restic），写本清单的 `files_*` 行。

**清单格式由本文规定**（§2、§3）。

## 1. 命令

```
nexloop-backup pg --recorder-dsn-file <PRIVATE_RECORDER_DSN> --pgbackrest-command-file <PGBACKREST_COMMAND_JSON> --stanza nexloop
                  [--pgbackrest-config-file <PRIVATE_PGBACKREST_CONF>] --expected-version <锁文件中的版本>
                  [--repo repo1|repo2] [--kind pg_full|pg_diff|pg_incr]
```

- **`--pgbackrest-command-file`**：一个 JSON 数组，是运行 pgbackrest 的命令前缀。
  - 在 DATA_HOST 上通过 PG 容器运行时，例如 `["docker","exec","-u","postgres","<PG_CONTAINER>","pgbackrest"]`；
  - 直接运行时为 `["/usr/local/bin/pgbackrest"]`。
  - 这个文件里不放凭据。
- **`--pgbackrest-config-file`**：可选，指定 pgbackrest 自己的配置文件（`pgbackrest.conf`，其中含仓库口令和对象存储密钥）。
  - 执行者只检查它的权限是 0600 且可读，不读取其内容，也不记录内容或路径；
  - 文件以 `--config=` 交给 pgbackrest；
  - 通过容器运行、配置在容器内时，不传这个参数。
- **`--expected-version`**：取 `versions.lock.json` 中 `deployment_tools.pgbackrest.version` 的值。版本不一致时，执行者在写任何清单行之前就拒绝。
- **执行顺序**：
  1. `version`；
  2. `info --output=json`，记下已有的 label；
  3. 写 pending 行（`tool=pgbackrest`、`tool_version`）；
  4. `--repo=N --type=T backup`；
  5. `--repo=N verify`；
  6. `--repo=N info --output=json`，取出本次新增的那份备份；
  7. 写 verified 或 failed 行，附带校验证据。
- **verified 的条件**：`verify` 退出码为 0，且 info 中这份备份的 `error` 为 false，且 stanza 的 `status.code` 为 0。否则记为 failed。
- **结果**：
  - 退出码 0 表示 verified，1 表示其他情况；
  - stdout 输出一个 JSON 对象，只含 id、label、版本、系统标识、时间线和 LSN；
  - pgBackRest 没有完成备份时，只留下 pending 行（与 pg_basebackup 路径一致），退出码 1。
- **`--tool pg_basebackup`**：保留本地路径（`--replication-dsn-file --pg-bin --target`），用于没有 pgBackRest 的开发和测试集群。两种参数混用会被拒绝。
- **`backup_id`**：默认取 UTC 时间到微秒，再加类型字母（F/D/I），所以每次尝试都不同。同一秒内的第二次备份是新的一次尝试，不会被当作重放。

## 2. 备份清单 schema：`nexloop-backup-manifest/2`

表 `runtime.nexloop_backup_manifests`。v1 是 0146 的列；v2 是迁移 0193新增的三列，都可为空，旧行保持为空。

| 列 | 类型 | 说明 |
|---|---|---|
| `manifest_id` | bigint | 自增 |
| `backup_id` | text `[A-Za-z0-9._:-]{1,64}` | 一次尝试的标识。PG 备份的 pending 行和完成行用同一个值 |
| `repo` | `repo1` / `repo2` | |
| `kind` | `pg_full` `pg_diff` `pg_incr` `files_artifact` `files_runtime` `journal`（`drill_restore_point` 由演练写） | |
| `started_at`、`completed_at` | timestamptz | PG 备份由服务器在 pending 时记录开始时间；pgBackRest 行的完成时间取其 `timestamp.stop` |
| `system_identifier`、`timeline` | | PG 备份的完成行必须与 pending 行（服务器当时的身份）一致，否则拒绝 |
| `start_lsn`、`stop_lsn` | pg_lsn | PG 备份完成行必填；pgBackRest 取 info 中的 `lsn` |
| `catalog_revision`、`catalog_digest` | | 服务器在 pending 时记录。迁移门槛用它判断“备份之后是否迁移过” |
| `release_id`、`key_ref` | | 可选 |
| `deletion_watermark`、`journal_seq` | | 服务器记录 |
| `file_manifest_digest`、`file_count`、`bytes` | | `files_*` 行必填 |
| `verify_status` | `pending` / `verified` / `failed` | |
| **`tool`**（v2） | `pgbackrest` / `pg_basebackup` / `restic` / `nexloop` | PG 备份写在 pending 行，完成行继承；单行条目（journal、files）直接写 |
| **`tool_version`**（v2） | text `[0-9A-Za-z.+_-]{1,32}` | 工具自己报告的版本，应等于锁文件中的版本 |
| **`verification`**（v2） | jsonb，最大 16 KiB | 校验证据，见 §3。**`tool=pgbackrest` 的行没有它就不能记为 verified** |

**写入规则**：只能通过 `authz.nexloop_record_backup(entry)`，以 `nexloop_backup_recorder` 身份写入。各类条目允许的键如下：

| 条目 | 允许的键 |
|---|---|
| PG pending | `backup_id repo kind verify_status release_id key_ref tool tool_version` |
| PG 完成 | `backup_id repo kind verify_status completed_at system_identifier timeline start_lsn stop_lsn verification` |
| journal | `backup_id repo kind verify_status started_at completed_at journal_seq key_ref tool tool_version verification` |
| files | `backup_id repo kind verify_status started_at completed_at file_manifest_digest file_count bytes key_ref release_id tool tool_version verification` |

- 除 `verification`（对象）外，所有值都是字符串或数字；
- 同一个 (repo, backup_id, kind, verify_status) 再写一次是重放，返回原来的行。

## 3. 校验证据：`nexloop-backup-verification/1`

公共键，所有工具都必须有：`schema`（固定为 `nexloop-backup-verification/1`）、`tool`、`tool_version`。其余键按工具约定：

**pgBackRest**（本切片写入）：
- `stanza`、`repo_key`（1 或 2）；
- `label`：pgBackRest 的备份标签。**恢复时用它**：`pgbackrest restore --set=<label>`；
- `type`（full/diff/incr）、`prior`（差异或增量备份所依赖的上一份的标签）；
- `timestamp_start`、`timestamp_stop`（ISO，UTC）；
- `archive_start`、`archive_stop`（WAL 文件名，前 8 位十六进制即时间线）；
- `size`、`repository_size`；
- `backup_error`（bool）、`info_status`（`{code, message}`）、`db_version`、`backrest_format`；
- `verify`：`{exit_code, summary}`，其中 summary 是 verify 输出的最后一行，只保留安全字符，最多 200 字。

**restic**（给 L5 的约定，L5 可以增加键，不要改公共键）：
- `repository_id`：restic 仓库 id，不是路径；
- `snapshot_id`、`tree`；
- `paths_count`；
- `check`：`{exit_code, summary, read_data_subset}`。

只有在 `restic check` 退出码为 0 时，才记为 verified。

**L4 恢复读取的方式**：
- 取 `kind like 'pg_%'`、`verify_status='verified'`、指定 repo 的行，按 `completed_at` 选择；
- 用 `verification->>'label'` 执行 restore；
- 用 `system_identifier`、`timeline`、`stop_lsn` 核对恢复结果；
- 差异或增量备份的依赖链看 `verification->>'prior'`。

## 4. 迁移门槛

门槛函数没有改（0147）：
- 它看最近一行已完成的 PG 备份；
- verify 失败就记为 `failed`，门槛输出 `latest_backup_not_verified` 并拒绝（AT-056）；
- 之后的一次 verified 备份会让门槛重新允许，失败行保留，作为这次尝试的记录。

## 5. 版本与许可

- **`versions.lock.json` 的 `deployment_tools`**：
  - pgBackRest 2.56.0，MIT；restic 0.18.0，BSD-2-Clause；
  - 只写版本号和官方发布页地址，状态为 `pinned_not_installed`；
  - 包和压缩包的 sha256 在安装时核对官方校验值后回填（运行手册 2.1、3.1，B 类，由负责人确认）；
  - 仓库和测试都不联网下载。
  - **安装前，负责人要在发布说明中确认所选的 pgBackRest 版本支持 PostgreSQL 18**；不满足就换版本，并同时修改锁文件。
- **`THIRD_PARTY_NOTICES.md`**：由 `scripts/check_licenses.py` 从锁文件生成“Deployment tools”一节，许可按许可策略核对，不在 allowed 中就报问题。
  - 本机缺 `playwright-core`，生成器无法完成整份重新生成，所以这一节是按生成器的格式插入的；
  - `tests/test_deployment_tools_lock.py` 断言渲染结果与文件中的这一节逐字一致；
  - 整份文件的一致性由 CI 的 licenses 阶段检查。

## 6. 测试

`tests/test_backup_pgbackrest_pg.py` 在一次性的归档 PG 18 集群上运行。pgBackRest 用替身 `tests/support/fake_pgbackrest.py`，它的系统标识、LSN 和 WAL 文件名取自真实集群。测试覆盖：
- verified 时清单字段齐全（tool、版本、证据的全部键），门槛允许；
- verify 失败、备份 `error=true`、stanza 状态异常这三种情况：记为 failed，门槛拒绝（`latest_backup_not_verified`），迁移不执行；之后一次 verified 让门槛重新允许；
- 版本不符时不写任何行；备份失败时只留 pending 行；另一个集群的备份被清单拒绝；
- pgBackRest 行没有证据不能记为 verified；证据的 schema 不对或超过 16 KiB 会被拒绝；`tool` 必须在允许的枚举之内；
- 命令行：凭据只从私有文件读取，配置文件权限不是 0600 时拒绝，输出和清单中都没有口令或配置路径。

`tests/test_deployment_tools_lock.py`：锁文件条目、许可核对、清单中的这一节。
