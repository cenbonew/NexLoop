# NX-035 备份 / PITR 与恢复演练：设计稿

状态：**设计稿，未实现；D1–D8 已定（§13）**，实现在 NX-032、NX-034 之后分派。分支 `nx035-design`，从 main `700f4b2` 切出。NX-035 依赖 NX-032（双机 TLS 与隔离部署）和 NX-034（发布归档与 GitHub 同步），都还没开始，所以先只写设计。本稿引用的 NX-029 和 NX-030 设计与实现目前在 `dispatch/integration-s4l`，尚未进入 main。

**公开仓库约束**：本稿不写任何真实主机名、地址、目录、用户或凭据，全部用参数表示（§12）。实际取值只放在私有 env 文件和 `local-only/` 中。

## 0. 依据

- **规划**：NX-035「完成备份/PITR 与恢复演练」，S5，模块 M23。交付物是“恢复隔离实例、删除水位、外部效应窗口、实际 RPO/RTO”（`planning/tasks.json`；`15_BACKLOG_AND_MILESTONES.md:62`）。
- **验收**：
  - AT-050 恢复演练：从备份恢复隔离实例，核验删除水位、版本、权限和外部效应窗口；
  - AT-056 迁移无备份：没有可验证的备份就拒绝迁移，不能为开发方便跳过；
  - AT-059 数据恢复删除：旧备份含已删除的消费者时，先重放删除清单，不恢复可联系状态。AT-059 属于 NX-029（S4），它的重放接口在这里与备份对接；
  - 相邻：AT-057 版本回滚，属于 NX-034 的发布流程，见 §10。
- **运维手册** `17_OPERATIONS_RUNBOOK.md`：
  - §4 备份分级：开发级每日逻辑备份，RPO 可能达 24 小时；受控试运行用 pgBackRest 加连续 WAL 归档，数据库 RPO ≤ 5 分钟、RTO ≤ 2 小时，文件 RPO ≤ 15 分钟；
  - §4 备份清单的内容：数据库时间和 LSN、各 schema 版本、release SHA、Artifact 哈希清单、Run 存储引用、加密密钥引用、删除水位；
  - §5 PostgreSQL、Artifact、Pi 检查点和外部服务不在同一个分布式事务里；两机互备不等于异地备份；
  - §6 恢复八步；§9 演练频率：试运行前完整恢复一次，之后每月一次，重大 Schema 或 Pi 存储升级后重做；每天检查备份年龄和归档延迟。
- **部署** `11_DEPLOYMENT.md`：
  - DATA_HOST 跑 PostgreSQL 18，存放主库和 WAL，接收来自 APP_HOST 的加密工件备份；
  - APP_HOST 存放 Runtime SQLite、Artifact，接收来自 DATA_HOST 的 PG 备份；
  - D2：“没有可验证备份不执行迁移”；§11：不是高可用，真实商业化前必须增加独立的离线或异地备份。
- **其他文档**：
  - `12_LOCAL_CI_AND_RELEASE.md:11,48,54`：CI 不挂载备份密钥；发布前生成可验证备份；需要还原数据库时进入灾难恢复流程，并阻断 real dispatch；
  - `06_ACTION_RUNTIME_AND_RECOVERY.md:64,92`：不能在运行中只复制 `.sqlite` 而丢掉 WAL；Run 文件按保留策略删除，备份中的内容到期清理。
- **ADR**：
  - ADR-003：Valkey 可丢失，不备份；
  - ADR-005：Pi SQLite 是例外，须有属主、备份与删除；
  - ADR-015：本地 Artifact 须授权访问、备份与删除；
  - ADR-018：两机不是 HA，真实恢复先隔离、再对账。
- **上游设计**：
  - NX-029 设计 §3（保留总表中“备份”一行写着“由 NX-035 定”）、§7.2（tombstone）、§9（删除水位、删除列表、`runtime.nexloop_replay_deletions`、readiness）、D5（删除后保留哈希联系限制）；
  - NX-029 实现说明（切片一）：水位与重放属于切片四，**尚未实现**；
  - NX-030 设计 M12（备份年龄与归档延迟，读 NX-035 的备份清单，表不存在时为 unavailable）、D3（备份超过 26 小时告警，WAL 归档延迟超过 5 分钟告警），以及 doctor `--observability`。

## 1. 目标与不做

**目标**

1. PostgreSQL 18 物理基础备份加连续 WAL 归档，可以恢复到任意时间点（PITR）。
2. Artifact 文件和已终态的 Pi Run 文件按清单做加密备份。
3. 备份同时存两处：跨主机副本，以及异地副本（异地何时启用见 D3）。
4. 备份清单表，供 M12 计算备份年龄，也供迁移门槛和恢复使用。
5. 恢复时被删除的数据不会复活：重放删除列表、联系限制和撤权，并通过 readiness 门槛强制执行。
6. 恢复围栏：恢复出来的实例在核验完成之前不能派发真实效应；外部效应窗口要先对账。
7. 定期在一次性环境中做恢复演练，给出一致性校验和实际的 RPO/RTO。
8. 部署前门槛：没有已验证的备份，就不能执行迁移。
9. doctor 检查和告警。

**不做**

- 高可用和自动故障切换（ADR-018）。
- 逻辑复制。
- 备份 Valkey（ADR-003）。
- 备份 Git 仓库和发布归档：它们由 NX-034 负责，GitHub 只是代码副本，不承担数据备份。
- 秘密与私有配置的备份：由负责人线下保管，见 D5。
- 跨租户的部分恢复：v0.1 只做整库恢复，没有单租户回滚。

## 2. 现状核对（代码事实，main 700f4b2）

| 事实 | 位置 | 影响 |
|---|---|---|
| 仓库中没有任何备份实现：没有 pg_dump、pgBackRest、archive_command 的代码或配置 | `git grep` | 全部新建 |
| 迁移入口只有两个：`bootstrap.bootstrap()`（只用于新建的隔离空库）和 `compose_bootstrap`（只有 `--mode test`）。**没有面向已有数据库的生产迁移命令** | `nexloop_eios/bootstrap.py`、`compose_bootstrap.py` | 迁移门槛要落在 NX-032/034 的部署迁移入口上（§7），bootstrap 本身不改 |
| `bootstrap.verify()` 会比对 `control.schema_migrations` 与 catalog 的版本和 checksum | `bootstrap.py:27-32` | 直接用作恢复后的迁移校验 |
| NX-030 的快照中 `backup` 块固定为 unavailable，doctor `--observability` 也把备份报为 unavailable，不算失败 | s4l 0131、`doctor.py` | 由 NX-035 换成读取清单表 |
| NX-029 只完成了切片一；水位、删除列表、重放和 readiness 尚未实现 | s4l `NX-029-implementation.md` | §6 依赖 NX-029 切片四；本稿只定义对接，不重复实现 |
| 运行时文件的格式是 `<runtime-root>/<run_id>/runtime.sqlite`，WAL 模式、`synchronous=FULL`，由单个 Host 持有 | NX-014、`test_runtime_dispatch` | 活跃的 SQLite 不能直接复制（§4.3） |
| NX-022 有派发控制（暂停等）和拒绝代码，NX-030 D8 记录拒绝 | 0068/0097、0131 | 恢复围栏复用派发拒绝的路径，新增一个拒绝代码（§6.1） |

## 3. 数据分类与备份方式

| 数据 | 所在主机 | 方式 | 目标 RPO | 备注 |
|---|---|---|---|---|
| PostgreSQL 集群（业务、队列、账本、审计、迁移 catalog、角色） | DATA_HOST | pgBackRest 完整、差异和增量备份，加连续 WAL 归档（D1） | ≤ 5 分钟（D2） | 物理备份含全局角色和策略；`archive_timeout` 设为 60 秒 |
| WAL | DATA_HOST | 同上，`archive_command`，异步归档 | 同上 | 归档积压触发告警（§8） |
| 本地 Artifact 文件 | APP_HOST | 文件级加密快照，按 Artifact 清单（ref 加 sha256）每 15 分钟一次（D1） | ≤ 15 分钟 | 只备份清单中的文件，不备份临时目录 |
| 已终态的 Pi Run 目录 | APP_HOST | 同上，只备份已终态、已关闭的 Run（D7） | ≤ 15 分钟 | 活跃 Run 不备份；恢复后按不一致处理（§6.4） |
| 恢复日志（删除列表、联系限制、撤权的导出，§6.2） | DATA_HOST 导出 | 追加写入的加密分段文件，每分钟一段，带哈希链 | ≤ 1 分钟 | 弥补 PITR 回退造成的删除丢失 |
| Valkey | DATA_HOST | 不备份 | — | ADR-003 |
| 私有配置、秘密、备份密钥 | 两机私有目录 | 不进入任何备份仓库；由负责人线下保管（D5） | — | 密钥和备份不能放在同一个位置 |

### 3.1 备份仓库

- **repo1，跨主机**：数据库备份放在 APP_HOST 的 `<PG_BACKUP_REPO>`，文件备份放在 DATA_HOST 的 `<FILE_BACKUP_REPO>`。两处都通过 NX-032 定义的受限 SSH 身份 `<BACKUP_SSH_IDENTITY>` 传输，只能写入备份目录。
- **repo2，异地**：`<OFFSITE_REPO>`，可以是 S3 兼容对象存储，也可以是第三台主机（D3）。
- **加密**：两个仓库都在客户端加密（pgBackRest 的 `repo-cipher-type=aes-256-cbc`，以及文件工具自带的加密）。密码文件 `<BACKUP_CIPHER_PASS_FILE>` 只有备份身份能读，CI 身份读不到。
- **密钥引用**：清单中只记录密钥引用 `key_ref`（一个不可逆的标签），从不记录密钥本身。
- **单写者**：每个仓库只有一个写入身份。旧主库被隔离之前，恢复出来的实例不能向同一个 stanza 归档（§6.1）。

### 3.2 计划（默认值写进 `deploy/configuration/backup.v1.json`，有界校验，按新版本调整）

| 项 | 默认 | 说明 |
|---|---|---|
| 完整备份 | 每周一次 | |
| 差异备份 | 每天一次 | |
| 增量备份 | 每 6 小时一次 | 可选 |
| WAL | 持续归档，`archive_timeout=60s` | |
| 备份完成后校验 | 每次 | `pgbackrest verify` 和文件仓库的 check；结果写入清单的 `verified` 状态 |
| 文件快照 | 每 15 分钟 | |
| 保留 | PITR 窗口 14 天；每月完整备份保留 1 份，最长 35 天（D4） | 决定了被删数据在备份中最多残留多久 |
| 恢复日志导出 | 每分钟 | 导出延迟超过 5 分钟告警 |

## 4. 备份执行者与清单

### 4.1 执行者

- **两个执行者**：
  - DATA_HOST 上的 `nexloop-backup`：调用 pgBackRest，导出恢复日志；
  - APP_HOST 上的 `nexloop-file-backup`：负责 Artifact 和 Run 目录。
- 它们都是独立的 OS 用户和容器，没有业务 API 凭据，也没有渠道或模型的秘密。
- **数据库访问**：使用新的 login 角色 `nexloop_backup_recorder`。
  - 它只能执行 `authz.nexloop_record_backup(...)`、`authz.nexloop_record_file_backup(...)` 和 `authz.nexloop_restore_journal_since(...)`，读不到任何表。
  - pgBackRest 的备份连接使用独立角色 `nexloop_backup`。目标是只授予 PostgreSQL 内置的备份权限（`pg_backup_start` / `pg_backup_stop` / `pg_switch_wal` / `pg_create_restore_point` 的执行权，以及 `REPLICATION`），**不使用 superuser**。
    - pgBackRest 能否在固定版本下以非 superuser 完成备份和校验，必须在实现前实测；实测不行时报调度员定夺，不能默认改用 superuser。
  - 这些连接由 NX-032 的 pg_hba 规则限定为 DATA_HOST 本机 socket。

### 4.2 清单表 `runtime.nexloop_backup_manifests`

部署级，无租户列；只追加，FORCE RLS，只有 owner 能读，应用角色没有任何权限，与 NX-030 的进程样本同一模式。

| 列 | 说明 |
|---|---|
| backup_id | 工具给出的标签，例如 pgBackRest 的 `20261010-020000F`；与 repo 一起唯一 |
| repo | `repo1` 或 `repo2`（标签，不写地址） |
| kind | `pg_full` / `pg_diff` / `pg_incr` / `files_artifact` / `files_runtime` / `drill_restore_point` |
| started_at, completed_at | 开始和完成时间 |
| system_identifier, timeline | 数据库的系统标识和时间线，用于确认备份来自本集群 |
| start_lsn, stop_lsn | PG 类备份的 LSN 范围 |
| catalog_revision, catalog_digest | 备份开始时的迁移版本，以及 `(version, checksum)` 列表的 sha256 |
| release_id | 当时运行的发布标识（NX-034 的 release manifest） |
| deletion_watermark | **备份开始时**删除列表的最大水位。取开始时刻是为了保守：开始到结束之间的条目不一定包含在备份中，重放是幂等的，从开始水位往后重放即可 |
| journal_seq | 备份开始时恢复日志的最大序号（§6.2） |
| file_manifest_digest, file_count, bytes | 文件类备份的清单摘要（排序后的 `ref, sha256` 列表的 sha256）、文件数和字节数 |
| key_ref | 加密密钥引用 |
| verify_status, verified_at | `pending` / `verified` / `failed`。校验结果作为新行追加（同一 backup_id 的后续状态），不修改旧行 |

约束：

- 只接受数字、哈希、LSN、标签和时间。任何自由文本字段都会被拒绝。
- 同一 `(repo, backup_id, kind, verify_status)` 重复写入视为重放。

### 4.3 文件备份的一致性

- **Artifact**：
  - 清单取自 `runtime.nexloop_local_artifacts` 中仍然有效的条目，由 owner 侧导出函数只给出 ref、sha256 和大小；
  - 执行者按清单复制文件，复制后重算 sha256，不一致就跳过该文件并在清单中计为失败；
  - 已删除或到期的 Artifact 不进入新的快照。
- **Pi Run 目录**：
  - 只备份已终态的 Run，此时 Host 已经关闭存储，WAL 已经 checkpoint。复制前用 SQLite 的 `PRAGMA integrity_check` 校验文件副本；
  - 活跃 Run 不备份。运行中只复制 `.sqlite` 而丢掉 WAL 是被禁止的做法（`06:64`）；
  - 若以后要备份活跃 Run，需要 Host 提供在线备份 API（D7）。

## 5. 保留期与不复活

- **备份保留与 NX-029 保留总表的关系**：
  - 备份中的个人数据最多残留“备份保留期”这么久，D4 推荐最长 35 天。这一行写回 NX-029 保留总表（由调度员同步给 L3）。
  - 删除凭据、tombstone、删除列表和恢复日志的保留期必须长于备份保留期（NX-029 §9）。NX-029 现定 1 年，满足这一要求。
  - `retention.v1.json` 的校验要增加一条：`backup.max_retention_days < min(deletion_ledger_days, tombstone_days)`，不满足就拒绝新版本。
- **备份不改写**：
  - 删除不去修改已有备份，而是靠恢复时重放（§6.2）。
  - 备份到期后整体轮换掉（pgBackRest 的 expire，以及文件仓库的 forget 和 prune）。
  - 删除凭据中写明 `backup_purge_due_at`，即最后一份含该数据的备份的到期时间，向负责人说明数据最终从备份中消失的时间。
- **不复活机制**：见 §6.2 到 §6.3。

## 6. 恢复

### 6.1 恢复围栏（restore fence）

恢复出来的实例来自过去的某个时间点，那时的状态是“正常运行”。所以围栏必须在任何应用角色连接之前写入：

1. **恢复到新目录、新端口**，绝不覆盖原有的数据目录。
   - 恢复工具 `nexloop-restore` 拒绝以下目标：非空的目录；与运行中实例相同的端口；从 pg_hba 可以到达生产应用主机的监听地址。
   - 恢复参数为 `recovery_target_time=<T>` 或 `recovery_target_name=<restore point>`，以及 `recovery_target_action=promote`，生成新的时间线。
2. **只放行 owner**：首次启动时 `listen_addresses` 仅限本机 socket，pg_hba 只允许 owner 通过本机 socket 连接。
   - 物理备份包含所有角色及其口令，恢复工具因此立即把所有应用角色设为 `NOLOGIN`，包括 api、domain_worker、scheduler、configurator、identity、metrics、backup_recorder 等；
   - 然后写入围栏：`control.nexloop_restore_state`，单行，只有 owner 可写。字段为 `state`（`restored` → `replayed` → `verified_readonly` → `open`）、`restored_from`（backup_id、目标时间 T、目标 LSN）、`old_timeline`、`window_start`（即 T）、`window_end`（见 §6.3），以及各步的时间。
3. **围栏的效果**：
   - **派发**：state 不为 `open` 时，Run 准入和 effect 准入都以新的拒绝代码 **NXR01 `restore_unverified`** 拒绝，并按 NX-030 D8 记入拒绝表。
     - 实现采用“改名保留 + 新包装”，包装的是当前生效的准入函数（`authz.nexloop_queue_command` 的现行包装链，以及 effect 准入的入口），不修改任何已发布的迁移。
   - **readiness**：API、各后台服务和 Runtime Worker 的 readiness 检查 `runtime.nexloop_restore_readiness()`。
     - `restored` 或 `replayed` 时都不就绪；
     - `verified_readonly` 时只允许读取和入站受理（API 就绪，派发仍被拒绝）；
     - `open` 时正常。
   - **口令轮换**：恢复后所有应用角色的口令一律轮换。恢复出来的实例不能沿用备份中的凭据。
4. **隔离旧实例**：
   - 原主库和原 APP_HOST 上的 Host、Worker 必须停止并确认，即运行手册的“隔离所有可能的旧 Host owner”；
   - 原主库不能再向同一个 stanza 归档。新实例在原主库确认隔离之前不开启归档；归档报错时执行者立即停止，不自动重试；
   - 真实灾难恢复时，由维护者在 `nexloop-restore` 中显式确认 `--old-primary-fenced`，否则恢复工具不进入 `replayed`。

### 6.2 重放：删除、联系限制、撤权（AT-059）

**问题**：PITR 回到时间点 T 后，数据库中的删除列表也回到了 T。T 之后发生的删除、联系限制和撤权都随之丢失。NX-029 §9 只说了“重放备份水位之后的删除列表”，而这份列表本身也在被回退的库里。

**设计：恢复日志（restore journal）。**

- **导出函数** `authz.nexloop_restore_journal_since(p_seq)`：owner 定义的 definer 函数，只授权给 `nexloop_backup_recorder`。它按全局序号返回以下几类条目，都不含原文：
  - `deletion`：NX-029 删除列表中的条目，包括水位、租户、world、Consumer ID 或数据类，以及截止时间；
  - `contact_restriction`：联系限制的新增与解除（NX-025），以及 NX-029 D5 的哈希化外部标识；
  - `revocation`：工作台成员的移除或降级（NX-028 的成员事实）、服务凭据吊销、浏览器会话批量吊销的标记。只记录 ID 和时间。
  - 全局序号由 owner 侧的序列和触发器写入 `control.nexloop_restore_journal`。这张表只追加，FORCE RLS，只给 owner，并由上述三类来源的追加触发器同步写入。
- **导出**：`nexloop-backup` 每分钟调用一次，把新条目写成加密分段文件，同时写入 repo1 和 repo2。
  - 每段带前一段的 sha256，形成哈希链；
  - 导出进度 `journal_exported_seq` 作为清单行写回（kind 为 `journal`）。导出延迟超过 5 分钟就告警。
- **重放**：恢复工具在 `restored` 状态下依次执行：
  1. 读取恢复出来的库中的最大 journal 序号 `S_db`；
  2. 从仓库读取所有序号大于 `S_db` 的分段，校验哈希链（断链则停止，进入人工处置）；
  3. 逐条重放：
     - `deletion` 调用 NX-029 的 `runtime.nexloop_replay_deletions`。按 Consumer 删除的条目会先阻断联系与检索，再进入 purging；
     - `contact_restriction` 写回限制，使用原条目 ID，幂等；
     - `revocation` 吊销对应的成员事实和凭据；
  4. 重新执行一次到期扫描，与 T 之后已经到期的数据对齐（扫描是幂等的，NX-029 §7）；
  5. 不论 journal 内容如何，**吊销全部浏览器会话、Run 凭据和服务会话**，并按 configurator 的成员文件重新应用工作台成员（成员清单是库外的版本化配置）；
  6. 全部成功后进入 `replayed`。
- **readiness**：在 `replayed` 之前，NX-029 定义的 readiness 本来就不会就绪（未重放），与围栏双重保证。
- **剩余风险（如实说明）**：
  - journal 每分钟导出一次。如果故障发生在一条删除请求受理后、下一次导出前（≤ 1 分钟），这条删除会随数据库一起丢失，请求本身也会丢失。
  - 缓解办法是 §6.3 的入站对账：如果渠道能重新拉取这段时间的入站消息，顾客的删除请求会作为消息重新进入，按 NX-029 D1 再次生成删除请求。
  - 不能重新拉取的渠道，在恢复报告中列出窗口，由负责人处置。

### 6.3 外部效应窗口

- **窗口定义**：
  - **开始 `window_start` = T**，即恢复目标时间；
  - **结束 `window_end`** 取以下来源中最晚的时间：
    - 旧时间线上最后一个已归档 WAL 段的时间；
    - 恢复日志最后一段的时间；
    - 外部渠道证据中的最后时间（provider 回执、出站记录服务的落盘记录）；
    - 进程样本外送到监控的最后时间（若有）。
  - 两者都写进 `control.nexloop_restore_state`。
- **对账（`verified_readonly` 之前完成）**：
  1. **在 T 时仍未终态的意图**：包括 `dispatched`、`unknown` 和已认领的意图。走已有的“查询后对账”路径（NX-015 回执，`effect_execution` 的 query），绝不重发。
  2. **T 之后新建、已随回退丢失的意图**：
     - 对能按时间范围查询的渠道（本地 JSON 投递、`message_provider_facts`、出站记录服务），列出窗口内的外部引用，与恢复后的意图对比；
     - 外部有、库里没有的，按外部引用**重建**为 `reconstructed` 状态的意图并标记为 `effect_succeeded_unverified`，不生成新的意图 ID 去补发（运行手册 §6.5）。
  3. **不能按时间查询的渠道**：该渠道的 scope 保持阻断（NX-022 的 scope 控制），由负责人人工处置后再解除。
  4. **入站**：渠道支持时，重新拉取窗口内的入站消息并经正常入站幂等受理（`provider_event_id` 去重）。WebChat 客户端断线重连使用 `Last-Event-ID`，只能补回服务端已有的事件；窗口内由顾客新发、但服务端已经丢失的消息无法补回，在报告中列出。
- **对账结果**：每个渠道的计数（查询数、一致数、重建数、无法核实数）写进恢复报告，只有数字。

### 6.4 文件恢复与对齐

- **Artifact**：
  - 从备份中恢复所有被恢复后的数据库引用的 ref，然后逐个校验 sha256；
  - 库里引用了、文件缺失或哈希不符的，标记为 `missing`，读取时返回不可用，不伪造内容；
  - 有文件但库中不再引用、或者在删除列表中的，直接删除，不进入恢复后的存储。
- **Pi Run 目录**：
  - 只恢复已终态的 Run；
  - 在 T 时仍活跃的 Run，标记为与数据库不一致，按运行手册 §6.6 处理：先对账，再创建新的复评，不让过时的 Pi 任务自行恢复写入权限；
  - Run 存储超前于数据库（文件中有、库中没有）时，不打开，标记为 blocked，等待人工处理。
- 恢复到 Host 时遵守单 owner 锁，不能在网络文件系统上直接打开 SQLite（`02:111`）。

### 6.5 校验并开放

1. `bootstrap.verify()`：迁移版本与 checksum 完全一致。
2. 角色、策略与权限：
   - FORCE RLS 表清单、definer 函数的 search_path（`check_definer_search_path.py`）、`test_db_boundary` 中的角色权限检查，都要与备份时 release 的预期一致；
   - 应用角色仍为 NOLOGIN，或已轮换口令。
3. 计数与抽样（§7.2 中的同一组检查）。
4. 合成 smoke，只在 `world=test` 下进行，不使用真实效应凭据。
5. 进入 `verified_readonly`。此时 API 只读并受理入站，派发仍被拒绝。
6. **开放真实派发**：由负责人逐个 scope 发起受治理的人类 Action `nexloop.restore.open_scope:1`（owner 专属，走 0124 的统一入口）。全部 scope 开放后 state 置为 `open`，并记录实际的 RPO 和 RTO：
   - **RPO** = 故障时间（`window_end`）减去 T；
   - **RTO** = 从恢复开始到 `verified_readonly` 的时间；
   - 开放派发的时间另行记录，因为它取决于人工对账。

## 7. 恢复演练与部署前门槛

### 7.1 演练（AT-050）

- **环境**：
  - 一次性的隔离环境，位于 `<DRILL_HOST>` 的 `<DRILL_ROOT>`，独立端口，loopback 监听，没有任何渠道、模型或真实效应凭据；
  - 磁盘加密；结束后销毁。“一次性测试环境内的破坏性清理”属于 AGENTS.md 允许的范围；销毁前确认目标路径就是本次演练创建的目录（工具记录目录标识并比对）。
- **频率**：试运行前一次，之后每月一次，重大 Schema 或 Pi 存储升级后各一次（`17:§9`，D6）。
- **步骤**：
  1. 在生产库上创建命名恢复点：`select pg_create_restore_point('drill-<id>')`，由 owner 定义的函数 `authz.nexloop_drill_mark()` 执行，只授权给备份身份。
     - 在**同一个函数调用中**记录各张只追加表的 `max(id)` 或最大序号，以及按租户计数，只有数字，写成 `drill_restore_point` 类型的清单行；
     - 同一调用中还要抽样：对每张只追加表，按 id ≤ max 随机抽 N 行，记录每行的 sha256（列值规范化后求哈希）。
  2. 用 `nexloop-restore` 恢复到 `recovery_target_name=drill-<id>`。
  3. 执行 §6.1 到 §6.5 的全部步骤。但演练环境**永远停在 `verified_readonly`**，不开放派发。
  4. **一致性校验**：
     - 迁移的版本与 checksum 一致；
     - 只追加表在 id ≤ 记录的 max 范围内，计数必须相等；
     - 抽样行的哈希必须相等。受治理清除（NX-029 §8）会改写行，所以先排除恢复点之后进入删除列表的行，再比较；
     - 可变表的计数允许差异，但外键与引用必须完整；
     - Artifact 清单摘要一致，抽样文件的 sha256 一致；
     - 删除重放：用一个演练专用的合成删除请求（`world=test` 的合成 Consumer，在打恢复点之后提交）验证它在恢复后被重放，并且不可联系、不可检索（AT-059 的真实备份版本）。
  5. **记录**：
     - RTO：从恢复开始到 `verified_readonly`；
     - RPO：演练中用“故障时间 = 恢复点后最后一次 journal 导出的时间”模拟；
     - 写进 `runtime.nexloop_restore_drills`：只追加，只有数字和代码；
     - 演练报告（只有数字）放在 `<DRILL_REPORT_DIR>`，不进公开仓库。
  6. 销毁演练环境。
- **CI 版本**（公开、合成数据）：
  - 测试夹具在临时目录中初始化一个 PG18 集群，`archive_mode=on`，WAL 归档到临时目录；用合成数据跑基础备份和 PITR，恢复到另一个端口；
  - 覆盖围栏、journal 重放、外部效应窗口（本地 JSON 投递的时间查询）、迁移门槛和 doctor；
  - CI 不挂载任何真实备份密钥（`12:11`），使用测试专用的口令文件。

### 7.2 “No migration without a verified backup” 的门槛（AT-056）

- **新增迁移入口** `nexloop-migrate`：在已有的 NexLoop 库上执行已审核的迁移，只用迁移专用身份。
  - 它由 NX-035 提供，NX-032/034 的部署阶段调用它；
  - `bootstrap` 仍然只用于全新的空库：它会检查原 EIOS 角色，并且只在没有任何迁移记录时执行。
- **前置检查**，全部由 owner 侧函数 `authz.nexloop_migration_gate()` 计算，任何一项不满足就拒绝，退出码非零，并且不读取迁移文件：
  1. 存在一行 `pg_*` 类型的清单，并且 `verify_status=verified`；
  2. 该备份的 `system_identifier` 和 `timeline` 与当前集群一致；
  3. 该备份完成时间距今 ≤ `migration_gate.max_backup_age_minutes`（默认 60 分钟）。部署阶段先触发一次差异备份并完成校验，然后才调用迁移；
  4. 该备份的 `catalog_digest` 等于当前库的 catalog 摘要，即备份之后没有执行过迁移；
  5. WAL 归档健康：`pg_stat_archiver` 中最近一次失败早于最近一次成功，归档延迟 ≤ 5 分钟。这样迁移本身的 WAL 也可以恢复；
  6. 恢复日志的导出延迟 ≤ 5 分钟；
  7. 当前不处于恢复围栏中（state 为 `open`）。
- **没有跳过开关**：不提供 `--skip-backup` 或任何环境变量来绕过。测试环境要迁移，也要先在测试集群上做一次真实的测试备份。
- 迁移完成后，部署阶段立即再做一次差异备份。

## 8. doctor 与告警

- **快照 `backup` 块**：在新迁移中 `create or replace` NX-030 的 `runtime.nexloop_metrics_snapshot`，把 `backup` 由固定的 unavailable 换成实际读取。
  - 只有数字。字段：最近一次已验证的 PG 备份年龄（秒）、最近一次完整备份年龄、各 repo 的最近备份年龄、WAL 最后归档年龄、归档失败标志、journal 导出延迟、文件快照年龄、最近演练年龄、最近演练的 RPO 和 RTO（秒）。
  - 表中无行时仍为 unavailable，不显示为 0（AT-045）。
  - 工作台运行状态页的“备份”块（nx030-ui）改为显示这些数字。
- **告警规则**：`alert-rules.v1.json` 出新版本，指标路径指向快照的 `backup.*`，严重级别沿用 NX-030 D3 的规则表：

| 规则 | 条件 | 依据 |
|---|---|---|
| backup_age | 最近一次已验证的 PG 备份超过 26 小时 | NX-030 D3 |
| wal_archive_lag | 最后归档超过 5 分钟，或出现归档失败 | NX-030 D3（RPO ≤ 5 分钟） |
| offsite_backup_age | repo2 的最近备份超过 26 小时（启用 repo2 后） | D3 |
| file_backup_age | 文件快照超过 1 小时 | D2 |
| restore_journal_lag | journal 导出延迟超过 5 分钟 | §6.2 |
| restore_drill_overdue | 最近一次演练超过 35 天 | `17:§9`，D6 |
| backup_verify_failed | 最近一次校验失败 | §3.2 |

- **doctor `--backup`**：只使用只读的 `nexloop_metrics` 角色。
  - 检查上表全部条件，以及 `backup.v1.json` 与清单的计划是否一致（例如该有的每周完整备份是否存在）；
  - 有 restore_state 行且不为 `open` 时报失败；
  - 原有的 `--observability` 不再把备份报为 unavailable，改为调用同一组检查。

## 9. 迁移与代码形状（占位，编号由调度员分配）

1. `nx035_backup`：
   - 清单表、journal 表与导出函数、`nexloop_backup_recorder` 角色；
   - `restore_state`、`restore_drills`、`drill_mark`、`migration_gate`；
   - 新的快照函数（`backup` 块）；
   - NXR01 拒绝代码，以及准入包装（改名保留 + 新包装）；
   - definer 函数都设 `search_path pg_catalog,pg_temp`。
2. 注册表新增一行：`restore.open_scope`（owner 专属，加入 business-actions 和 workbench-roles）。
3. Python：
   - `nexloop_eios/backup.py`：`nexloop-backup`、`nexloop-file-backup`、`nexloop-restore`、`nexloop-migrate`；
   - doctor `--backup`；
   - `deploy/configuration/backup.v1.json`。
4. 部署示例：
   - `deploy/stage/` 下的 pgBackRest 配置示例和 systemd timer 示例，全部用参数；
   - PG18 的 `archive_command` 和 `archive_timeout` 示例。
5. `versions.lock.json`：固定 pgBackRest 和文件备份工具的版本或镜像 digest。实现前先在固定版本上核实它们对 PG18 的支持，不靠 README 推断。

## 10. 与 NX-032、NX-034、NX-029、NX-030 的边界

| 事项 | 归属 | NX-035 的做法 |
|---|---|---|
| 两机目录、OS 用户、受限 SSH 身份、TLS、pg_hba、端口 | NX-032 | 只引用参数：`<BACKUP_SSH_IDENTITY>`、`<PG_BACKUP_REPO>` 等 |
| PG18 实例的 `archive_mode`、`wal_level` 等配置的部署 | NX-032 | 提供示例值和检查（doctor），由 NX-032 部署 |
| 发布归档、release manifest、镜像 digest、GitHub 同步 | NX-034 | 清单中记录 `release_id`；不备份 Git 和发布归档 |
| 部署阶段的顺序：drain、备份、迁移、重启、smoke | NX-034（发布入口），NX-032（主机） | 提供 `nexloop-migrate` 和门槛函数，部署阶段必须调用 |
| AT-057 版本回滚 | NX-034 | 需要还原数据库时进入本稿 §6 的流程 |
| 删除水位、删除列表、重放函数、readiness | NX-029（切片四） | 本稿增加恢复日志的导出和库外重放，调用 NX-029 的重放函数 |
| M12 指标位、告警评估器、doctor `--observability` | NX-030 | 填充 `backup` 块，增加规则 |
| 独立的离线或异地备份 | NX-035（D3） | repo2 |

## 11. 验收与测试清单

| AT | 覆盖方式 |
|---|---|
| AT-050 | CI：临时 PG18 集群，合成数据，基础备份、PITR 恢复到命名恢复点，经过围栏、重放、窗口对账和一致性校验（计数、抽样哈希、迁移 checksum、Artifact 摘要），记录 RTO。实际环境：在一次性环境中用真实备份按 §7.1 演练，有真实证据（数字报告）后才能标为 passed |
| AT-056 | `nexloop-migrate` 在以下情况都拒绝，且没有读取迁移文件：无清单、未校验、超龄、system_identifier 不符、备份后已迁移、归档失败、处于围栏中。不存在跳过参数（测试会扫描 CLI 参数和环境变量） |
| AT-059（与 NX-029 联合） | 备份含已删除的 Consumer：在 T 之后删除，PITR 回到 T。不重放时 readiness 不就绪、派发返回 NXR01；重放后不可联系、不可检索；哈希化的联系限制仍然生效 |
| AT-045（继承） | 无清单时运行状态页的备份块显示 unavailable，不显示 0 |
| 其他 | journal 断链时停止；恢复后应用角色为 NOLOGIN 或已轮换；旧会话和 Run 凭据全部失效；恢复工具拒绝非空目录和运行中实例的端口；活跃 Run 标记为不一致；Artifact 缺失时标为 missing，不伪造；backup_recorder 读不到任何表，清单拒绝自由文本；告警规则在阈值上触发；doctor `--backup` 的各个分支 |

真实环境的证据（演练报告、实际 RPO/RTO）与 CI 的合成证据分开记录；只有 CI 证据时，不把 AT-050 标为 passed。

## 12. 参数（私有 env 提供，公开仓库只写名字）

`<DATA_HOST>`、`<APP_HOST>`、`<DRILL_HOST>`、`<PG_BACKUP_REPO>`、`<FILE_BACKUP_REPO>`、`<OFFSITE_REPO>`、`<BACKUP_SSH_IDENTITY>`、`<BACKUP_CIPHER_PASS_FILE>`、`<FILE_BACKUP_PASS_FILE>`、`<OFFSITE_CREDENTIAL_FILE>`、`<DRILL_ROOT>`、`<DRILL_REPORT_DIR>`、`<RUNTIME_ROOT>`、`<ARTIFACT_ROOT>`、`<BACKUP_RECORDER_DSN_FILE>`、`<STANZA>`。

所有密码或凭据都以 0600 的文件路径传入（`read_private_text`），不经命令行参数或环境变量的值传递，也不写日志。

## 13. 负责人决定的事项（D 项）

裁定状态：**全部已定。** D2–D6、D8 由负责人 2026-10-10 决定，原话“全部按推荐”；D1、D7 由调度员 2026-10-10 裁定，同样采用推荐（见各项）。

能按现有文档或 ADR 定的，不列为 D 项。例如：

- 不备份 Valkey（ADR-003）；
- 恢复先隔离、再对账（ADR-018、`17:§6`）；
- 迁移前必须有可验证的备份（`11:D2`、AGENTS.md）；
- 不在运行中复制 SQLite（`06:64`）；
- 备份年龄超过 26 小时告警（NX-030 D3）。

- **D1 备份工具。**（**调度员裁定 2026-10-10：采用推荐。** 数据库用 pgBackRest、文件用 restic，都固定版本，许可证记入第三方清单。实现前必须实测 pgBackRest 在固定版本下能否不用 superuser 完成备份；不行时先报调度员，不能默认改用 superuser。）
  - **推荐**：数据库用 pgBackRest，文件用 restic，两者都固定版本并写入 `versions.lock`：
    - pgBackRest：运行手册已点名（`17:§4`）；支持加密、多仓库（repo1 / repo2）、`verify`、异步归档和差异 / 增量备份；
    - restic：加密、去重，有 `check` 校验。
  - **替代**：PG18 原生的 `pg_basebackup --incremental` 加 `pg_combinebackup` 和自写的 `archive_command`，外加 age 加密。依赖少，但要自己维护归档、校验和过期，出错面更大。
- **D2 RPO / RTO 目标。**（**负责人 2026-10-10 决定：“全部按推荐”。**）
  - **推荐**：沿用运行手册的受控试运行级别：数据库 RPO ≤ 5 分钟、RTO ≤ 2 小时；文件 RPO ≤ 15 分钟。RTO 计到 `verified_readonly`，开放派发的时间单独记录。
  - 开发和合成演示阶段不承诺 RPO。
  - **替代**：放宽为 RPO ≤ 1 小时、RTO ≤ 8 小时，可以省掉 journal 的分钟级导出和部分告警。
- **D3 异地存放。**（**负责人 2026-10-10 决定：“全部按推荐”。**）
  - **推荐**：在**受控真实试运行开始前**启用 repo2，加密后存到负责人指定的第三方位置（S3 兼容对象存储或第三台主机，参数为 `<OFFSITE_REPO>`），每天至少一份差异备份加持续 WAL。在此之前文档明确写出“两机互备不是异地备份”。
  - **替代**：推迟到正式商业化前（`11:§11` 的底线）。试运行期间遇到两机同时损坏、勒索或机房事故时会丢失全部数据。
- **D4 备份保留期（决定被删数据在备份中最多残留多久）。**（**负责人 2026-10-10 决定：“全部按推荐”。**） “被删除的数据在备份中最多残留 35 天”这一行由调度员转告 L3，加进 NX-029 的保留总表。
  - **推荐**：PITR 窗口 14 天，加每月完整备份 1 份，最长 35 天；删除凭据中写明 `backup_purge_due_at`；对外隐私说明中写“删除后最长 35 天从备份中消失”。
  - **替代**：90 天，可追溯更久，但被删数据残留时间更长，需要更新隐私说明。
- **D5 备份密钥的保管。**（**负责人 2026-10-10 决定：“全部按推荐”。**）
  - **推荐**：两份离线副本，由负责人保管（例如一份纸质，一份放在独立的密码管理器）。主机上只放运行所需的口令文件，权限 0600，只有备份身份能读；CI 永远读不到；每年轮换一次，轮换时新建仓库，旧仓库到期后自然淘汰。
  - **替代**：口令只存在主机上（主机损坏时备份不可解密，不可接受，只作对照）。
- **D6 演练的数据与地点。**（**负责人 2026-10-10 决定：“全部按推荐”。**）
  - **推荐**：每月演练用**真实备份**，恢复到 `<DRILL_HOST>` 上的一次性加密目录（推荐 APP_HOST，与主库分离），只走 loopback，无任何外部凭据，结束后销毁，报告只有数字。重大升级后加做一次。
  - **替代**：只用合成数据演练（不能证明真实备份可以恢复，不满足 AT-050 的本意）。
- **D7 活跃的 Pi Run 文件。**（**调度员裁定 2026-10-10：采用推荐。** 活跃 Run 不备份，恢复后按不一致处理，重新评估。）
  - **推荐**：v0.1 只备份已终态的 Run。恢复时在 T 仍活跃的 Run 按不一致处理，对账后重新复评（运行手册 §6.6 已有）。
  - **替代**：给 Agent Host 加在线备份接口（SQLite backup API），可以保住活跃 Run 的中间状态，但会增加 Host 的改动和单 owner 锁的复杂度。
- **D8 恢复后开放派发的方式。**（**负责人 2026-10-10 决定：“全部按推荐”。**）
  - **推荐**：负责人逐个 scope 发起受治理的 Action `nexloop.restore.open_scope:1`，owner 专属。外部效应窗口中无法核实的 scope 不允许开放，handler 会检查该 scope 的对账计数。
  - **替代**：维护者用部署命令一次性全部开放（更快，但跳过了逐 scope 的确认）。

### 本地部分的裁定（调度员 2026-10-11）

- **Q1**：恢复日志分段在本地不加密，用哈希链加 0600 权限，只含 ID、哈希、序号和时间。分段的静态加密由文件备份仓库（restic，客户端加密）在部署时完成，是受控试运行前的门槛项。不引入 age。
- **Q2**：本地部分不重建意图（§6.3 第 2 步）。外部有、库里没有的引用计入“无法核实”；有这类计数的渠道、不能按时间查询的渠道，以及恢复时刻仍有未终态意图的渠道，都由恢复工具通过 NX-022 的 action_type 暂停。`open_scope` 之后这些渠道仍然阻断。重建留给切片三。
- **Q3**：迁移门槛不提供任何豁免。本地用 `pg_basebackup` 做真实备份，用 `pg_verifybackup` 校验，部署时换成 `pgbackrest verify`。

实现见 `NX-035-local-implementation.md`。

## 14. 实现切片建议（NX-032、NX-034 完成后）

1. **切片一**：清单表、`nexloop_backup_recorder`、`nexloop-backup`（PG 部分）、`backup.v1.json`、快照的 `backup` 块、告警规则、doctor `--backup`、迁移门槛和 `nexloop-migrate`（AT-056）。
2. **切片二**：恢复围栏、NXR01、`nexloop-restore`、journal 的导出与重放（依赖 NX-029 切片四）、CI 中的 PITR 测试（AT-059 的备份版本，以及 AT-050 的 CI 部分）。
3. **切片三**：文件备份（Artifact 和已终态的 Run）、外部效应窗口对账、`restore.open_scope`。
4. **切片四**：在真实两机环境上首次完整演练，记录实际 RPO/RTO；之后按月演练（AT-050 的真实证据）。
