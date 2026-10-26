# NX-035 D6：文件备份兜底告警 `file_backup_unprotected`

分支 `l7/file-backup-alert`，从 main 1a391ee1 切出。迁移 `0360_file_backup_unprotected.sql` 是临时编号，合入时由调度员重编号。告警规则 v20 = v19（76 条）加这一条，共 77 条。

依据：负责人 2026-10-26 的 D6——先启用文件备份；"有可备份对象却从未有过 verified 文件备份"的告警另开一项，放进下一个规则版本。

## 1. 为什么现有规则不够

- `file_backup_age`（v3）读 `backup.file_backup_age_seconds`，也就是最近一次 verified 文件快照的年龄。
- 从未有过 verified 文件快照时，这个指标不存在，规则永远不会触发。stage 在 10-26 之前正是这种情况：文件备份从没启用过，库里却已有 Artifact 和已终态 Run（NX-061 B9）。
- `timer_stale_file_backup`（v13）要求 APP_HOST 的 host-health 配置里列了 `file-backup` 这个 timer；没列出时，它同样不会触发。

## 2. 指标 `backup.file_backup_unprotected_seconds`

在 `runtime.nexloop_metrics_snapshot(tenant, world)` 的 backup 块里新增，只对快照所属的租户计算，只在 world 为 `real` 时计算。迁移按 0354 的做法，把快照函数改名一次再包一层。

- **可备份对象**：与文件备份清单（0194）列出的范围完全一致：
  - 状态为 available、仍在保留期内的 Artifact；
  - 绑定的任务已终态、没有排队等待 NX-029 清除、也没有清除完毕的 Run。
- **已覆盖**：同类（files_artifact / files_runtime）最近一次 verified 快照的 `started_at` 晚于这个对象变为可备份的时间。
  - 快照是在开始时读的清单，所以用开始时间作为界线。在快照运行期间才变为可备份的对象不算已覆盖。
  - failed 的快照不覆盖任何对象。
- **变为可备份的时间**：必须是之后的更新改不动的时刻（L6 S5）。如果用 `updated_at`，一个已覆盖的对象在终态之后又被改写，比如延长保留期、补写 result，就会被重新算作未保护而误报。
  - **Artifact** 取 `least(updated_at, lease_until)`。
    - 理由：finalize（0005）只有在上传租约有效时才能把 pending 变为 available，而 reservation 每次最多把租约设到 30 秒之后。所以 `lease_until` 落在"变为 available"之后 30 秒以内。
    - 对象保持 available 期间，没有任何路径改动 `lease_until`；今天唯一的后续写入是转入 deleting。
    - 只要没有别的写入，`updated_at` 就恰好是变为 available 的那一刻。以后即使有写入改了它，`least()` 也会被 `lease_until` 限住，误差不超过 30 秒。
    - 不用 `created_at`：一个 pending 的 Artifact 可以被反复重新 reservation，`created_at` 可能早于真正 available 很久。拿它判断"已覆盖"，会漏掉那段时间里开始的快照并未包含它这种情况。
  - **Run** 取所绑任务最近一次终态事件的时间（`runtime.job_events` 只插入、每次状态转换一行）。
    - 被重新排队后再次结束的任务，确实是新数据，所以取最近一次。
    - 没有终态事件的任务（某条路径没写事件），回落到任务的 `created_at`。这个值固定不变，只会让结果偏旧，不会被后续更新推后。
- **取值**：未覆盖的可备份对象中最旧那个距今的秒数。每个对象都已覆盖，或者根本没有可备份对象时，这个键不存在（缺省，不记为 0）。
- PG 备份尚未登记（backup 块的 `status` 为 `unavailable`）时，这个键也照样计算。文件备份自身的缺口不会因此被掩盖。

与 `file_backup_age` 的分工：

| 情况 | 哪条规则触发 |
|---|---|
| 备份停了，期间没有新的可备份对象 | `file_backup_age`（超过 1 小时） |
| 从未备份过，或者备份停了而期间有了新对象 | `file_backup_unprotected`（未覆盖超过 1 小时） |

## 3. 规则（v20）

```json
{"rule_id": "file_backup_unprotected", "metric": "backup.file_backup_unprotected_seconds", "condition": "gt", "threshold": 3600,
 "for_seconds": 0, "severity": "warning"}
```

阈值与 `file_backup_age` 相同，都是 1 小时：文件的 RPO 是 15 分钟（D2），1 小时是三个周期再加余量。级别 warning，与 `file_backup_age` 一致。

## 4. 测试与验证

- `tests/test_file_backup_unprotected_pg.py`：在一次性 PG 18 上运行，复用 0194 测试的注入辅助函数。覆盖：
  - 清单不列出的对象不计入：pending、deleting、deleted、过期、非 real world 的 Artifact，未终态、排队清除、非 real world 的 Run；
  - 按租户计算；
  - 从未备份时，从两类中最旧的那个对象算起；
  - 按类覆盖；
  - 快照运行期间才变为可备份的对象不算已覆盖；
  - failed 快照不覆盖；
  - 终态之后又被更新（延长保留期、改写 result、`updated_at` 被推到将来、后续的非终态事件）都不会误报；任务重新排队后再次结束则算作新数据（L6 S5）；
  - v20 = v19 加一条。
- `tests/test_file_backup_unprotected_alert_pg.py`：用真实的告警评估器。Artifact 已可备份 2 小时、从未备份时，这条规则触发，`file_backup_age` 不触发；回到 1 小时以内后解除。
- 两个 host-health 测试中"最新规则"的引用改为 v20（v20 不涉及 timer）。
- 变异验证：迁移和规则共 15 个变异，S5 修改后又加 7 个，只存活 1 个，是等价变异：去掉 Artifact 查询里的 `a.tenant_id=p_tenant` 后，FORCE RLS 的 `local_artifact_tenant` 策略仍按 `eios.tenant_id` 只返回本租户的行。这个条件保留，作为双重保险。
