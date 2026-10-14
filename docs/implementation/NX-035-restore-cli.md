# NX-035 恢复命令行 `nexloop-restore`

依据：NX-035 设计稿 §6（恢复围栏、重放、窗口对账、校验）与 §7.1（演练），部署运行手册第 4 步，L5 的 S5 核对第 2 项（调度员 2026-10-14 派工）。这是 AT-050、AT-056 真实演练和 AT-072 的前提。分支 `l4/nx035-restore`，自 main `7fb06a2`。

## 1. 做什么

把 PostgreSQL 恢复到一个**新的**实例，并停在 `verified_readonly`。

- **目标实例**：`--pg-path` 必须不存在或为空，`--port` 必须空闲，并且不在 `--running-port` 列出的端口里；只监听本机 socket（`--socket-dir`），归档关闭；pg_hba 只放行 `--owner-user` 通过本机 socket 连接。它**从不覆盖任何现有库**，也不开放派发。开放派发由负责人逐个 scope 执行 `nexloop.restore.open_scope:1`。
- **恢复目标**：二选一，必选。
  - `--target-name`：命名恢复点，例如演练的 `drill-<id>`；
  - `--target-time`：带时区的 ISO 8601 时间，即按时间点恢复（PITR）。
  
  两者都以 `promote` 结束，生成新的时间线。
- **来源**：
  - `--source pgbackrest`（部署，D1）：执行 `pgbackrest --config=<私有文件> --stanza=… --repo=1|2 --pg1-path=<新目录> --type=name|time --target=… --target-action=promote --archive-mode=off [--set=<备份标签>] restore`。
  - `--source basebackup`（本机）：复制 `pg_basebackup` 目录，用 `cp` 从 `--wal-archive` 回放 WAL。
- **随后与库函数 `backup.restore` 相同的流程**：
  1. 所有可登录的应用角色设为 NOLOGIN；
  2. 写入恢复围栏；
  3. 校验恢复日志的哈希链并重放（`--journal-dir`）；
  4. 外部效应窗口对账（`--evidence-file`；不提供时，所有渠道都按"不能按时间查询"处理，保持暂停）；
  5. 执行 `bootstrap.verify()`；
  6. 进入 `verified_readonly`；
  7. 执行开放前的核对（第 2 节）。
- 旧时间线在首次启动前从恢复出来的数据目录用 `pg_controldata` 读取，两种来源走同一套流程。

## 2. 开放前核对（`backup.post_restore_checks`，只有数字和布尔值）

- **迁移账本与校验和**：`bootstrap.verify()`，版本与 checksum 必须和 catalog 完全一致。
- **行数抽样**：各只追加表的行数。提供 `--drill-mark-file`（由 `nexloop-backup drill-mark` 生成）时，还与标记比对：记录的最大序号以内的计数、抽样行的哈希、catalog 摘要。
- **`nexloop.restore.open_scope` 的五条补偿控制**（负责人 2026-10-11 定案；本地实现说明 §7）：

| 控制 | 核对内容 |
|---|---|
| `owner_only` | `restore.open_scope` 在受治理注册表中登记，handler 是会检查 owner 的那个函数，属主为 `nexloop_owner` |
| `per_scope` | 每个活跃租户各有一个 `real` scope，并且都还没有开放 |
| `unverified_paused` | 每个不能按时间查询、或有"无法核实"计数的渠道，在本表中标为暂停，并且对应的 NX-022 `action_type` scope 确实处于暂停（按该渠道的租户在行级安全下核对） |
| `report_complete` | 围栏处于 `verified_readonly`，重放、对账、校验的时间和窗口结束时间都已记录 |
| `opening_recorded` | 没有缺少开放人或意图记录的已开放 scope |

任一控制不成立，恢复以 `RestoreRefused` 拒绝，原因里列出不成立的控制名和计数。`passed` 只有在账本一致、五条控制都成立，并且（有标记时）没有计数或抽样差异、catalog 一致时才为真。

## 3. 参数与凭据

- 凭据只来自私有文件（0600、属主为本账户，`read_private_text`）：
  - pgBackRest 配置（含仓库口令）：工具只核对它是私有文件，然后把路径交给 pgBackRest，从不读出或输出其内容；
  - `--evidence-file`、`--drill-mark-file`。
- 不经命令行参数或环境变量传递凭据。本模块不读取环境变量（AT-056 的无豁免测试会扫描源码）。
- `--old-primary-fenced` 必须提供，否则拒绝。真实灾难恢复时，先确认原主库和原 APP_HOST 上的 Host、Worker 已经隔离（设计 §6.1 第 4 条）。
- `--window-start` 默认取 `--target-time`，或者标记文件中的时间；`--window-end` 默认取当前时间。
- **输出**：一行只含数字和代码的 JSON。`--report-file` 写入新的 0600 文件。`--stop` 在核对后停止实例（用于演练）。退出码：0 通过；1 拒绝或失败；2 配置错误。

`nexloop-backup drill-mark --recorder-dsn-file … --drill-id <id> --out <新文件>`：调用 `authz.nexloop_drill_mark`，把标记写入新的 0600 文件。恢复到该恢复点的库里没有这条标记行，因为它在恢复点之后才提交，所以恢复时通过文件传入。

## 4. 与 L2 备份清单的对齐

L2 的 pgBackRest 备份（`l2/nx035-pgbackrest`）在本分支切出时还没有提交。恢复命令行目前**不读取**清单表 `runtime.nexloop_backup_manifests`。它依赖的约定只有三条，请 L2 在定清单格式时对齐：

- pgBackRest 的备份标签就是清单的 `backup_id`，这样 `--set <backup_id>` 可以指定某次备份；
- stanza 名在部署中固定（例子用 `nexloop`）；仓库编号与清单的 `repo` 对应（`repo1` 对应 `--repo 1`，`repo2` 对应 `--repo 2`）；
- 演练恢复点的名称是 `drill-<id>`（`authz.nexloop_drill_mark` 已经如此）。

恢复后的围栏行 `restored_from` 目前记录 `target_name` 或 `target_time`；`backup_id` 只在库函数调用时传入，命令行尚未提供。如果 L2 希望把恢复与清单行关联，可以加一个 `--backup-id` 参数，同时传给 `--set`。

## 5. 测试

都在临时目录中的一次性 PG 18 归档集群上运行：

- `tests/test_restore_cli_pg.py`：
  - **拒绝**：未确认隔离、目标是正在运行的数据目录、pgBackRest 配置文件不是私有文件（报 `PermissionError`，输出中不含口令）、同时给两个目标（退出码 2）；
  - **base backup 到命名点**：带标记，通过，五条控制成立，计数一致，报告文件 0600，实例停止，源集群不受影响；
  - **pgBackRest 到时间点**：pgBackRest 在开发机上没有安装，所以用一个替身可执行文件。它记录自己收到的参数，再铺下同一个 base backup 和恢复设置。测试断言它收到的 `restore` 参数完全符合预期，流程通过，恢复设置只监听 socket、关闭归档、只放行 owner。
- `tests/test_restore_drill_pg.py`：在原有 AT-050 本地演练中加入核对断言；并在一个事务里把一个未核实渠道恢复成未暂停后回滚，确认此时 `unverified_paused` 不成立、`passed` 为假。

## 6. 未做（如实说明）

- **真实 pgBackRest 的端到端执行**：本机没有安装，只用替身验证了调用参数；需要在 CI 主机或演练环境中按运行手册第 4 步实测。
- 口令轮换、按 configurator 成员文件重新应用工作台成员：仍留到部署切片，属于开放之前的人工步骤。
- 意图重建（`reconstructed`）：仍留到切片三。
