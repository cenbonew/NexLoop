# NX-035 本地部分：实现说明

依据：`NX-035-design.md`（D1–D8 已定，§13），以及调度员 2026-10-11 对本地部分的三项裁定（Q1–Q3，见 §6）。

- 分支 `nx035-local`，从 main `414549b` 切出。
- 临时迁移号：`0180_nx035_backup.sql`、`0181_nx035_restore.sql`，依赖 NX-029 切片四（0135）和 NX-030（0131）。

**范围**：只在本机的一次性 PostgreSQL 18 集群上实现和测试。不连接任何服务器，不碰任何现有数据库。

**AT-050 / AT-056 现在只有本地证据。** 部署环境的演练报告要在 NX-032 完成后，按 §7.1 在一次性环境中用真实备份完成，才能作为真实证据。

## 1. 工具与本地替代

| 设计（部署） | 本地实现 | 部署时 |
|---|---|---|
| pgBackRest 完整/差异备份 + WAL 归档（D1） | `pg_basebackup`（plain 格式，WAL 随流，SHA-256 清单），由非超级用户 `nexloop_backup`（REPLICATION）执行；WAL 用 `archive_command` 复制到归档目录 | 换成 pgBackRest（repo1/repo2）；需实测它在固定版本下能否不用 superuser 完成备份 |
| `pgbackrest verify` | `pg_verifybackup`（校验清单和 WAL） | 换成 `pgbackrest verify` |
| 恢复日志分段加密存放 | 用哈希链串起来的 JSON 分段，0600，**本地未加密**（Q1） | 静态加密由文件备份仓库（restic，客户端加密）完成，是**受控试运行前的门槛项** |
| restic 文件备份（Artifact、已终态 Run） | 未实现（§7） | 切片三 |

**工具形态**：
- 库：`nexloop_eios/backup.py`；
- 命令：`nexloop-backup pg|journal`、`nexloop-migrate`；
- `restore()` 是库函数，由演练测试调用。命令行的 `nexloop-restore` 留到部署切片，届时与 pgBackRest 的恢复一起做；
- 所有凭据都通过 0600 文件传入（`read_private_text`），不经命令行参数或环境变量。

## 2. 迁移 0180：清单、恢复日志、演练、M12

### 角色
- `nexloop_backup_recorder`（可登录）：只能执行记录函数，读不到任何表。
- `nexloop_backup`（可登录，REPLICATION）：
  - 可以执行 `pg_backup_start/stop`、`pg_switch_wal`、`pg_create_restore_point`；
  - 不是 superuser，没有任何 NexLoop 表的权限。测试对两个角色都断言：业务表和备份/恢复表都读不到，实际 select 被拒；
  - 这两个凭据都不进入 runtime、Agent 或任何 Run。

### `runtime.nexloop_backup_manifests`
- 部署级，只追加，FORCE RLS，只有 owner 能读。
- 只接受标签、哈希、LSN、数字和时间；多出的键或自由文本一律拒绝。
- PG 备份先写一行 `pending`：此时由**服务端**记录集群标识与时间线、迁移 catalog（版本加摘要）、删除水位和恢复日志位置。完成时追加一行 `verified` 或 `failed`，复制开始行的值，并核对清单中的 system identifier 必须是本集群的。

### 恢复日志 `control.nexloop_restore_journal`
- 由以下来源的触发器追加写入：
  - 删除台账（NX-029）；
  - 联系限制（不带顾客原话 `matched_text`，也不带解除理由）；
  - 哈希化的已删除联系键（NX-029 D5）；
  - 服务凭据吊销；
  - 工作台成员被暂停或吊销。
- 导出走 `authz.nexloop_restore_journal_since`，只有记录员角色能执行。

### 演练
- `authz.nexloop_drill_mark` 在同一调用里完成两件事：
  - 记录每张只追加表的最大 id 和行数，并抽样 5 行，记下每行的 sha256；
  - 创建命名恢复点。
- 恢复后用 `runtime.nexloop_drill_compare` 在恢复实例上比对；演练结果由 `authz.nexloop_record_drill_result` 写入，只有数字和布尔值。

### M12 指标
- 做法：`runtime.nexloop_metrics_snapshot` 改名保留（`…_before_backup_v0180`）后加包装，只替换 `backup` 块。
- 没有 PG 备份时 `backup` 块仍为 `{"status":"unavailable"}`，不显示 0（AT-045）。
- 字段（只有数字）：

| 字段 | 含义 |
|---|---|
| `age_seconds` | 最近一次已验证的 PG 备份的年龄（沿用 v1/v2 的 `backup_age` 规则所指的键） |
| `full_backup_age_seconds` | 最近一次完整备份的年龄 |
| `repo_backup_age_seconds.{repo}` | 各仓库最近一次备份的年龄 |
| `verify_failed` | 最近一次校验是否失败 |
| `wal_last_archived_age_seconds` | WAL 最后一次归档距今的时间 |
| `wal_archive_failing` | 归档是否在失败 |
| `journal_export_lag_seconds` | 最早一条未导出日志条目的年龄 |
| `file_backup_age_seconds` | 文件快照的年龄（未实现，所以缺省） |
| `drill_age_seconds` | 距上次通过的演练的时间；从未演练时，从首次已验证备份起算 |
| `drill_rpo_seconds` / `drill_rto_seconds` | 最近一次演练实测的 RPO 和 RTO |

- 缺数据的指标直接缺省，不填 0。

### 告警规则
- 新文件 `alert-rules.v3.json`，v1 和 v2 不改。
- `backup_age` 规则不变，新增 7 条：

| 规则 | 阈值 |
|---|---|
| `wal_archive_lag` | 300 秒 |
| `wal_archive_failing` | 大于 0 |
| `offsite_backup_age` | repo2 超过 26 小时 |
| `file_backup_age` | 1 小时 |
| `restore_journal_lag` | 300 秒 |
| `restore_drill_overdue` | 35 天 |
| `backup_verify_failed` | 大于 0 |

### 拒绝代码
- `NXR01 restore_unverified`。

## 3. 迁移 0181：恢复围栏、重放、窗口、开放、迁移门槛

### 状态
- `control.nexloop_restore_state` 只有一行，只有 owner 能写。状态依次为 `restored` → `replayed` → `verified_readonly` → `open`。
- 每一步都有一个 owner 函数，乱序调用会被拒绝。

### 围栏
- `authz.nexloop_assert_dispatch_controls` 是 Run 和效应派发都要调用的检查（0068）。做法是改名保留（`…_before_restore_v0181`）后加包装。
- 存在未开放的恢复行时：
  - 除非该租户的 `real` scope 已由负责人开放，否则一律以 NXR01 拒绝；
  - 拒绝由现有路径记入拒绝表（NX-030 D8）；
  - `test` 和 `simulation` 两个世界要等整个恢复进入 `open` 后才开放。
- Python 侧：`REASONS` 加入 NXR01，效应拒绝记录的正则也接受 `NXR..`。
- 就绪状态（readiness）：`runtime.nexloop_restore_readiness()`，以及给应用角色用的 `authz.nexloop_restore_readiness()`：
  - `restored` 和 `replayed`：不就绪；
  - `verified_readonly`：可读，但不派发；
  - `open`：正常。

### 重放
`runtime.nexloop_restore_replay` 按原键幂等地处理每类条目：
- 删除条目交给 NX-029 的 `runtime.nexloop_replay_deletions` 重放（在它的 begin/finish 括号内）；
- 联系限制：控制版本号更新的那条胜出，`matched_text` 置空；
- 已删除联系键；
- 服务凭据吊销；
- 成员暂停或吊销。

重放结束（`runtime.nexloop_restore_replayed`）时，不论日志内容如何，都会删除全部浏览器会话、吊销全部 Run 凭据。

### 外部效应窗口（Q2）
`runtime.nexloop_restore_reconcile` 按租户、世界和 action type 逐个统计：
- 恢复时刻仍未终态的意图：只走现有的查询和对账路径，不重发；
- 窗口内的外部引用数；
- 与终态意图一致的数量；
- 库里没有的数量（无法核实）。

以下渠道一律用 NX-022 的 `set_control` 暂停对应的 `action_type` scope：
- 不能按时间查询的；
- 有“无法核实”计数的；
- 有未终态意图的。

结果写入 `control.nexloop_restore_channels`，并列在恢复报告中（每个渠道的计数和是否暂停）。

### 开放
`nexloop.restore.open_scope:1`（capability `restore.open_scope`，operation `open_restore_scope`）：
- 经 0124 注册表进入，只有 owner 能做：角色清单 v7 只授予 owner，handler 还会再查一次当前的工作台角色；
- 只能在 `verified_readonly` 时开放本租户的 `real` scope；
- 无法核实的渠道如果没有处于暂停状态，就拒绝开放；
- 全部 scope 开放后，状态进入 `open`，同时记录实测 RPO（窗口结束减去恢复目标时刻）和 RTO（从恢复开始到 `verified_readonly`）；
- 开放后，被暂停的渠道仍然阻断（NXC01），需要负责人用现有的 resume 解除。
- 配套变更：business-actions v14、workbench-roles v7、HTTP 操作表、`GoalGovernedActions.open_restore_scope`。

### 迁移门槛（AT-056、Q3）
`authz.nexloop_migration_gate()` 列出当前不能迁移的全部原因：

| 原因代码 | 含义 |
|---|---|
| `no_backup_manifest` | 没有任何 PG 备份清单 |
| `latest_backup_not_verified` | 最近一次 PG 备份未通过校验，包括校验失败的情况 |
| `backup_too_old` | 最近一次备份超过 60 分钟 |
| `backup_from_another_cluster` | 备份的集群标识或时间线与当前集群不符 |
| `migrated_after_backup` | 备份之后已经迁移过，catalog 摘要不同 |
| `wal_archiving_off` | 未开启 WAL 归档 |
| `wal_archive_lagging` | 超过 5 分钟没有归档 |
| `wal_archive_failing` | 最近一次失败晚于最近一次成功 |
| `restore_journal_export_lagging` | 有超过 5 分钟未导出的日志条目 |
| `restore_fence_active` | 处于恢复围栏中 |

- `nexloop-migrate` 只有 `--database-url-file` 一个参数。被拒绝时退出码为 1，不应用任何迁移，也**不读取任何迁移文件**。
- **没有任何豁免、开关或环境变量；本次测试未使用任何豁免。**
- 阈值目前是固定值；`backup.v1.json` 尚未提供（见 §7）。

## 4. 恢复工具（`backup.restore`）

1. 拒绝以下目标：非空目录、已被占用或属于运行中实例的端口，以及没有 `old_primary_fenced` 确认的恢复。
2. 复制基础备份，写入 `recovery.signal`、`restore_command`、恢复目标（名称或时间）和 `promote`；只监听本机 socket；pg_hba 只放行 owner 通过本机 socket 连接；关闭归档。
3. 启动实例，等它完成 promote。随后把所有以 `nexloop_` 开头、可登录的应用角色设为 NOLOGIN。
4. 依次执行：`restore_begin` → 校验日志哈希链（断链时停止，抛出 `JournalBroken`）→ 重放 → `replayed` → 窗口对账 → `bootstrap.verify()` → 确认没有可登录的应用角色 → `verified_readonly`。
5. 返回仍在运行的实例，以及一份只有数字的报告。演练永远停在 `verified_readonly`。

## 5. 测试（都在临时目录中的一次性 PG 18 集群上运行）

| 文件 | 内容 |
|---|---|
| `tests/backup_cluster.py` | 开启 WAL 归档的一次性集群夹具，覆盖 `pg` 夹具 |
| `tests/test_backup_pg.py`（5 个） | 两个备份角色读不到任何表、清单拒绝自由文本或换集群、只追加；快照的 `backup` 块（无备份时 unavailable，之后为数字）以及 v3 规则；**AT-056** 门槛的各项拒绝（无清单、**校验失败**、超龄、别的集群、日志滞后、归档失败、恢复围栏、迁移之后）和放行路径，CLI 拒绝时不读迁移文件；不存在任何豁免（扫描 CLI 参数和源码）；日志断链时停止 |
| `tests/test_restore_fence_pg.py`（1 个） | 真实效应执行器和工作台 HTTP：围栏期间派发返回 NXR01 并记入拒绝表；就绪状态的各阶段；窗口报告逐渠道计数，并暂停 action_type；operator 不能开放、恢复引用不符不能开放；owner 开放后进入 `open`，记录 RPO/RTO；**被暂停的渠道开放后仍是 NXC01、发送数为 0**，resume 之后新意图只派发一次 |
| `tests/test_restore_drill_pg.py`（1 个，**AT-050 本地证据 + 备份版 AT-059**） | 真实 `pg_basebackup` 加 `pg_verifybackup`；打命名恢复点；之后加联系限制并由 owner 删除 Consumer，导出日志（分段 0600，不含原文）；恢复工具拒绝占用的端口、非空目录、缺少隔离确认；PITR 到新目录和新端口，恢复点之前的库经日志重放后再次删除 Consumer，联系限制恢复，会话和 Run 凭据清零，应用角色为 NOLOGIN；删除水位位于备份开始水位与最后一次导出之间；计数和抽样哈希一致，catalog 一致；停在 `verified_readonly`，NXR01；源集群不受影响；演练结果写回，快照中可以看到 RPO 和 RTO |

本地实测（合成数据，不是部署证据）：RTO 约 0.95 秒，模拟 RPO 约 0.08 秒。

## 6. 调度员裁定（2026-10-11）

- **Q1**：恢复日志分段不在本地加密，用哈希链加 0600 权限。分段中只有 ID、哈希、序号和时间，有测试断言其中没有原文字段或消息内容。分段的静态加密由文件备份仓库（restic，客户端加密）在部署时完成，是受控试运行前的门槛项。不引入 age。
- **Q2**：本切片不重建意图。恢复报告逐个渠道列出“无法核实”计数和被暂停的 action_type；`open_scope` 之后被暂停的渠道仍然阻断（已有测试证明）。
- **Q3**：不提供任何豁免。测试用 `pg_basebackup` 做真实备份，用 `pg_verifybackup` 校验；清单存在但校验失败时同样拒绝迁移（已有测试）。

## 7. 待负责人裁定：`nexloop.restore.open_scope` 的风险级别

- **现状**：business-actions v14 暂按 EIOS 风险 `low` 发布，NexLoop 的分类是 `high`。
  - 原因与 AT-043 D2 相同：vendored EIOS 的 ActionGovernor 对 `low` 以上的风险一律要求审批证据，NexLoop 的部署清单校验也只接受 `low`。
  - ActionGovernor 没有改动。
  - 清单（v14）的 decision 和 purpose 里都已写明“暂按 low，待负责人裁定”。
- **补偿控制**：
  - 只有 owner 能执行（角色清单加 handler 双重检查）；
  - 按 scope 逐个开放；
  - 有“无法核实”计数的渠道，只要未处于暂停，就不能开放；
  - 恢复报告是前置条件（重放、窗口对账、`bootstrap.verify`、`verified_readonly`）；
  - 每次开放都留下 scope 行（开放人、意图 ID、时间）和受治理入口的意图记录。

## 7. 未完成项

- **重建意图**（`reconstructed` / `effect_succeeded_unverified`，设计稿 §6.3 第 2 步）：留给切片三。
- **各渠道的“按时间查询”读取器**：例如本地 JSON 投递、出站记录服务。目前对账函数接收调用方提供的证据；没有证据的渠道一律按“不能按时间查询”处理，保持暂停。
- **文件备份**（Artifact 和已终态 Run 目录，restic）以及 §6.4 文件对齐：留给切片三。
- **恢复工具的命令行、`--target-time` 场景、口令轮换**（开放时轮换应用角色口令并重新允许登录）、**按 configurator 成员文件重新应用工作台成员**：留到部署切片。
- **readiness 接入**：`runtime.nexloop_restore_readiness()` 尚未接入 API、各后台服务和 Runtime Worker 的 `/health/ready`；派发由围栏保证关闭。
- **doctor `--backup`**、`deploy/configuration/backup.v1.json`（阈值和计划可配置）、工作台“备份”块的 UI：都未做。按调度员的范围，这次只做快照和告警规则 v3。
- **部署相关**：pgBackRest 和 restic 的版本固定写入 `versions.lock.json`、非 superuser 的实测、systemd timer 与 `archive_command` 示例，都依赖 NX-032。
- **迁移编号**：`tests/test_effect_execution_sql.py::test_42_actual_bootstrap_and_publication_checksum` 要求迁移编号连续，即迁移数量等于 head。临时号 0180、0181 会让它失败；集成时按顺序改为正式号（0145、0146）即可恢复。
