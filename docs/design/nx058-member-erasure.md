# 设计增补｜NX-058 片 E：员工离职擦除

第 2.5 版，2026-10-26，L2（§2.5 按登录事件盘点结果改写，待 E6；I-E4、I-E5 与 S-E5～S-E8 已处理；E1 已定为 A；按 ADR-039 R3 §8，L3 b1661a7d，同步目标状态、erased 判定与冻结体改写）。第 1 版（8af58ba4）经 L6 复核（`build/nx058-slice-e-design-review.md`：C1–C3、I1–I6、S1–S7），本版逐条吸收，关闭说明见 §12。

负责人 Q-A 定为「A+90天」：员工离职擦除纳入 v0.2，作为 NX-058 片 E，排在片 D 之后。上位设计是 `docs/design/account-self-service.md` 第 7 版 §4（S3 擦除传播），审批接法照片 D 设计（`docs/design/nx058-owner-actions.md` v2.8），目标展示照 ADR-039 R1 §6（本稿需要修订 §6.1 与 §6.5，见 §5）。实现等片 D 合入 main 后开始，迁移用临时编号。

## 0. 范围

一个 owner 专用 Action `nexloop.workbench_member.erase_member`（能力 `workbench_member.erase_member`，handler 操作名 `erase_workbench_member`），请求体 `target_principal`、`reason`。`reason` 是枚举 `offboarding | member_request | other`（S1），不写入审计。

**作用单位是 subject**（C1，选 (a)）：一个人（subject）在本租户可以有多个 principal；擦除的是这个人在本租户的账号与残留数据，所以前提是该 subject 在本租户的**所有** membership 都已 revoked（§1）。请求体只给 principal，handler 由它解析 subject。

不在范围：Consumer 擦除（NX-029）；owner 的自我擦除；principal 本身的假名化（负责人口径：保留 principal）；运维通道的擦除——**不提供**：AGENTS 要求正式数据只由受治理 Action 改动，0198 和 configurator 都不做擦除（§7）。

## 1. 审批、前提与可批准条件

- **策略**与片 D 相同：无条件、quorum 1、批准人恰好是 `{owner}`、`target_subject_field: target_principal`、`solo_mode: not_applicable`、24 小时、`summary_fields` 为空；目标经审批视图 `target_member` 展示。handler 的两层 fail closed 自查（策略形态、`approval_in_flight` 回查）照片 D2。
- **投票冷却**（片 D1）同样适用；**可批准条件**照片 D §0：排除请求人和目标后，至少 1 位 active、冷却已满的 owner。L3 的发起时 `quorum_unreachable`（片 D 的 D8）落地后同样适用，否则请求挂起至过期。
- **单 owner 租户**（I2）：无法经审批擦除，也没有运维通道替代；只能等第二位 owner 加入并满 24 小时。运行手册写明；这是禁止自批的直接后果，负责人知情。
- **被擦除的人不能参与批准**：NX-056 按 `target_subject_field` 在决定时拒收、消费时不计目标本人的票（AA2）；且前提要求其 membership 全部 revoked，没有投票资格。
- **发起时的目标检查**（ADR-039 R3 §8.1）：在同一迁移里为 `workbench_member.erase_member` 登记 `control.nexloop_approval_capability_settings`，`target_statuses = {revoked}`、无执行期字段——必须与能力注册同一迁移、在任何请求之前。`approval_open` 按能力声明检查目标状态（B2 已把写死的列表换成这个机制），其余能力不受影响。
- **目标解析**（S-E2）：另写解析函数，由 principal 得到 subject 并锁定该 subject 在本租户的全部 membership；不复用片 D 的 `nexloop_workbench_owner_target`（它对 revoked 报 `target_not_active`，并按 owner 区分角色）。策略与审批回查直接复用 `nexloop_workbench_owner_approval`。拒绝都写成 `workbench member: <code>` 形式（`target_not_revoked`、`already_erased`），以便 R2 §7.4 的 gate 取到原因码（S-E4）。
- **锁内重查**（I1）：handler 在成员锁内重新确认：目标 principal 解析出的 subject 在本租户的全部 membership 都是 `revoked`，否则 `target_not_revoked`；该 subject 已在擦除台账中则 `already_erased`。这与片 D handler 的“revoked 不能再作目标”不冲突：那是片 D 的 5 个 Action 各自的规则，擦除是唯一以 revoked 为前提的能力。
- **竞态**（I1，测试）：审批期间目标被 configurator 重新配置或另一 principal 被重新邀请并接受 → 执行时 `target_not_revoked`，审批作废；两份擦除审批先后执行 → 第二份 `already_erased`。

## 2. 擦除内容（S3，按负责人原话，作用于 subject）

handler 在同一事务内，按片 D 的顺序（租户设置 → 成员锁 → 策略自查与审批回查 → 锁内重查 → 写入）执行：

### 2.1 账号与主体

- `control.nexloop_browser_accounts`（按 subject 一行）：状态 `disabled`；`password_hash` 置空、`password_history` 置空数组；`session_epoch+1`、`revision+1`。
- 用户名与邮箱（**E1 已定为 A，负责人 2026-10-25**）：列 `username` 与 `payload->>'username'` 同步改为 `erased-` 加 `sha256(tenant|local_account_id)` 前 16 位（0021 的 check 要求两者相等）；`verified_email` 在 payload 里（不是列），改为同一假名加 `@erased.invalid`。
- `erased-` 前缀保留给系统（I5）：工作台邀请在 HTTP 与 SQL 两层拒绝以 `erased-` 开头的用户名（0335 的正则本身允许它）。
- 原用户名释放后，同名的新员工可以被邀请：HTTP 层的同名拒绝（片 D3）与 ADR §6.4 的 `same_username_exists` 都只比对现存账号，被擦除者的账号已是假名，所以对同名重新入职都放行、标记为 false。这是预期：新员工是新的 principal 和账号，与离职者无关联；批准人如需核对是否“回聘”，看审计链与成员列表，不靠这个标记。
- `control.nexloop_browser_subjects` 的 payload status 置为 `disabled`（S5，0021:12 允许）：0198 与 0334 都要求 subject 与账号均 active，这是第二道保险。
- `control.nexloop_browser_sessions`：删除该 subject 的会话行（S2；epoch+1 已让它们失效，删除是清残留）。
- 账号行、subject 行、membership 行本身保留（principal、subject 的外键与审计追溯需要）。

### 2.2 邀请与重置记录

- `control.nexloop_workbench_invitations`：删除该 subject **被邀请**的记录（`accepting_subject` 等于该 subject，或 `accepted_principal` 属于它），连同其中的 `username`（上位设计称 `username_key`，实际列名 `username`）、`token_hmac`、`acceptor_source`。该 subject **签发**的记录保留，只清 `issuer_source`（E2 采纳 L6 建议：记录的是对他人的操作，属于他人的审计上下文）。
- `control.nexloop_workbench_reset_codes`：删除 `target_subject` 等于该 subject 的全部记录；它签发的记录只清 `issuer_source`。删除后目标本人的 90 天安全通知随之消失，这是预期（S6）。
- 已核对 main 上没有外键引用 `invitation_id` 或 `reset_id`（S6）。

### 2.3 擦除台账、删除台账与恢复重放（C2、I-E2）

两张表各管一件事：

- **擦除台账**（判定依据，永久保留）：新表 `control.nexloop_workbench_member_erasures`
  - 列：`tenant_id`、`subject_id`、`principals text[]`（擦除时该 subject 在本租户的全部 principal）、`approval_id uuid`、`erased_by_principal`、`erased_at`、`world text not null default 'real' check(world='real')`；主键 `(tenant_id, subject_id)`（一人一次）。`tenant_id` 外键引用 `control.nexloop_tenants`（`on delete restrict`：租户删除走 NX-029/NX-035 的租户清理流程，由它显式处理本表，不级联、不留孤儿）。
  - 只追加（update/delete 触发器拒绝）、owner 持有、强制 RLS（按 `eios.tenant_id`），不授给任何角色读写；只含 ID 与时间，没有用户名或摘要。 读取一律经 owner 持有的 security definer 函数（视图、handler、configurator、保留期服务），任何角色不直接读表。
  - **保留策略：永久**。不进任何保留期清理；`erased`、`already_erased`、configurator 的 `member_erased` 与保留期服务的 90 天到期都以它为准。erasure coverage 登记为“擦除台账（只含 ID 与时间）”。
- **删除台账**（恢复重放日志，365 天后清理）：`control.nexloop_deletion_ledger`。新迁移扩展其 `kind` 约束加入 `workbench_member`（0260:514-515 的写法）；每次擦除经 `runtime.nexloop_deletion_ledger_add` 写一行：`world='real'`、`kind='workbench_member'`、`subject_ref`=subject、`request_ref`=approval_id。它照旧在 `deletion_ledger_days`（365 天）后被保留期删除——它只服务于“从不超过 35 天的备份恢复后重放”，不是判定依据。 写入方式参照 0135 的 `nexloop_ledger_on_erasure`：擦除台账的 `after insert` 触发器调用 `runtime.nexloop_deletion_ledger_add`（S-E6），handler 不直接写删除台账；重放补回擦除台账行时触发器不再写一次（以删除台账的唯一键去重）。
- **重放**：NX-035 恢复后经 `runtime.nexloop_replay_deletions(p_restore, p_entries)`（0135，0260 已原地替换为按 kind 分派）重放日志条目。本片再原地替换该函数，加一个 `workbench_member` 分支，调用新的 `control.nexloop_workbench_member_erase_replay(tenant, subject, approval_id)`：幂等地重做 §2.1、§2.2、§2.4 的写入，并在缺失时补回擦除台账行（恢复会把擦除台账一起带回擦除前的状态）。copy guard 登记该函数的替换。 补回的擦除台账行：`erased_at` 取恢复日志中该条目的原始时间（`nexloop_deletion_ledger` 条目的 cutoff），`approval_id` 取条目的 `request_ref`，`erased_by_principal` 从该审批的消费记录（`approval_consumptions.consumed_by_principal`）回查（I-E4）；`principals` 取重放时该 subject 在本租户的全部 principal。
- 恢复演练测试：恢复到擦除之前的备份（含哈希、邀请、原用户名，擦除台账无此行），按日志重放后哈希为空、邀请已删除、用户名为假名、擦除台账补回、`erased` 判定成立。 另断言补回行的 `erased_at` 等于擦除时的原值、`erased_by_principal` 等于原执行人（I-E4）。

### 2.4 `invite_owner` 冻结请求体中的明文用户名（C3）

- 经 `invite_owner`（片 D3）入职的成员，其原用户名留在审批冻结请求体里。按 ADR-039 R3 §8.7 定点改写：只改 `invite_owner` 请求体中的 `username` 这一个字符串为与账号相同的 `erased-` 假名，在本事务开启的擦除作用域内、经 owner 函数执行。**查找直接以审批表为准**（I-E3），不经邀请记录：`capability='workbench_member.invite_owner'`、请求体 `username` 等于该 subject 擦除前的用户名（擦除事务内先读出原用户名再假名化账号），以及 D3 的 `accept_v3` 记下的 approval_id 关联（两条取并集）。
  - **哪些请求改写**（S-E7）：已消费（executed 或 execution_refused）、已拒绝、已撤回、已过期的 `invite_owner` 请求都改写；**仍在等待或已批准未消费**的请求不改写——同名的新邀请可能正等待审批，不属于被擦除者（同名重新入职，E1）。
  - 冻结体改写后 digest 对不上，这是有意的（与 0331 的 Consumer 假名化同一口径：这些审批都已终结，digest 只作审计记录）。改写只经 owner 持有的函数，审批表的追加保护对这一列开一个定点例外，copy guard 登记，需 L3 同意（E3）。
  - 重放（§2.3）同样重做这一步。
- 视图（ADR-039 R3 §8.6）：`invite_owner` 行的被邀请人 `username` 以 `erased-` 开头时输出 `{"kind":"erased"}`；冻结体由本节改写为该前缀，所以判定成立。片 E 之前已有的真实 `erased-…` 用户名（S-E1）由迁移自检排除：现存邀请、账号与 `invite_owner` 冻结体中没有以 `erased-` 开头的用户名，有则迁移失败、人工处理；片 E 起邀请在 HTTP 与 SQL 两层拒绝该前缀。
- 冻结体改写按 R3 §8.7：只改 `invite_owner` 请求体中的 `username` 这一个字符串，在本事务开启的擦除作用域内、经 owner 函数，由片 E 迁移重定义请求表守护并登记 copy guard。
- 测试：邀请记录在擦除前已不存在时，冻结体仍被假名化（I-E3）。

### 2.5 登录事件（片 E2；E6 已定为 A，调度员 2026-10-26，落实负责人 Q-A“A+90天”）

**盘点结果（2026-10-26，0022、0023、0024、0050、0162、0198、0334 及工作台 HTTP 的全部写入）**：`control.nexloop_browser_login_events.details` 里**没有**来源摘要或用户名摘要。设计前几版的前提不成立。实际情况：

| 事件 | details 的键 | 可识别个人的内容 |
|---|---|---|
| `local_authentication` / failure（0022） | failed_attempts、lockout_level | 无 |
| `local_authentication` / failure（0023） | failed_attempts、lockout_level、account_id、account_revision、**result_snapshot** | `result_snapshot` 是当时整份账号 payload，含**明文** `username` 与 `verified_email` |
| `local_password_state` / saved（0024） | account_id、account_revision、rehash | 无 |
| `initial_identity` / created（0050、0162） | account_id、principal_id、operator_label、business_grant_created | 无（operator_label 是运维标签） |
| `operator_password_reset`（0198、0334） | account_id、application_id、account_revision、session_epoch、operator_label | 无 |
| `owner_issued_password_reset`（0333）、`invitation_accepted`（0335） | reset_id / invitation_id、account_id、…、same_source_as_issuer（布尔） | 无 |

来源摘要只存在于邀请与重置码的 `issuer_source`、`acceptor_source`、`completer_source` 列，片 E1 擦除时已经删除或清空（§2.2），不在登录事件里。

**需要确认（E6）**：负责人“90 天后清除”的对象，按实际应理解为失败登录事件 `result_snapshot` 里的明文用户名与邮箱。两种做法：

- **A（已定）：90 天后假名化**（0353）。恢复后：擦除重放（片 E1）带回擦除台账的原始时间，恢复丢失的清除记录由下一轮重做，无需删除台账条目。保留期服务新增 verb `member_login_clear`：对擦除满 90 天、尚无清除记录的 subject，把它的失败登录事件 `result_snapshot` 中的 `username`、`verified_email` 两个字符串改为账号上的同一 `erased-` 假名（结构和其余键不变；`account_id`、`account_revision` 不动）。代价：0023 的失败回执比对（:62-66）与请求时间早晚无关；清除执行之后，这些事件的同一 request_id 重放一律不再命中。清除只在擦除满 90 天后才执行，所以此前的重放不受影响。清除记录写入只追加的 `control.nexloop_workbench_member_login_clearances`（subject、时间、行数），以它判定幂等。接线照 I4：`authz.nexloop_retention_command` 加 verb，`RetentionPort` 方法，`RetentionKeeper.run_once` 步骤和 summary 键，失败只计数；按 subject `for update skip locked`、每轮上限。
- **找不到 `erased-` 假名时**（L6 S-1，不改代码）：清除函数仍写一条 `events=0` 的清除记录。理由：擦除台账有该 subject，就说明片 E1 已在同一事务里把账号用户名改成了假名；找不到只可能是账号行本身不存在（例如从未有本地账号的成员），这时也没有可改写的快照（快照来自该账号的失败登录）。写下记录可以避免每一轮反复扫描这个 subject。
- **B：擦除时立即假名化**，不接保留期服务。理由：调查需要的 subject、时间、结果都保留，明文用户名对调查不是必需。但这与负责人“保留 90 天”的原话不一致。

两种做法都**不**动 `result_snapshot` 以外的键。片 E1 已经让账号、会话、邀请、重置码在擦除当时生效，与此无关。

### 2.6 受治理命令日志与全库兜底（S3）

- 作业、Action claims、幂等回执、`browser_evidence.facts` 中保存的邀请与重置 Action 请求体可能含 username。实现前盘点；凡含该 subject 原用户名的位置，或纳入 §2 的写入，或登记理由。
- 补员工版的全库搜索兜底测试（仿 `test_erasure_whole_database_pg.py`）：擦除后，全库文本列与 jsonb 中不再出现原用户名、原邮箱、口令哈希（登记为保留的位置除外）。

### 2.7 保留的部分

principal、subject、membership 行（全部 revoked）、授权实例与角色历史（只含 principal）、除 §2.4 外的审批记录（冻结体中的目标 principal）、`runtime.audit_events` 与 NX-059 审计链。审计链里只有 principal 和动作，没有用户名或其摘要（上位设计 §4；0333:131-133、0335:305-307 的 payload 已核对）。

## 3. erasure-coverage（I3）

- `control.nexloop_workbench_grant_instances` 与 `control.nexloop_workbench_role_history`：目标前提是全部 revoked，撤销实例与角色 stop 早已写过，擦除再写只会是空写入。所以**继续登记为保留**，理由改为“授权审计与投票资格（只含 principal）；员工擦除见 NX-058 片 E”，不伪造写入。（调度员原要求“改为接入员工擦除”，按 L6 I3 改为此口径，请确认。）
- 现状更正：这两张表目前只在 `tests/test_consumer_erasure_coverage_pg.py` 中登记，`docs/implementation/erasure-coverage.md` 里没有。本片在文档中补登记，并新增员工擦除一节。
- 新增 `tests/test_member_erasure_coverage_pg.py`：凡带员工身份列（`principal_id`、`subject_id`、`issued_by_*`、`target_subject`、`accepting_subject`、`accepted_principal`、`username`、`*_source` 等）的员工相关表，要么员工擦除或其重放中有对它的写入，要么登记理由；新表默认要求登记。
- 新表：擦除台账与登录摘要清除记录登记为保留（只含 subject、principal、时间、计数），理由“擦除台账”。

## 4. 回滚与恢复（I6）

- **擦除不可回滚**：哈希、邀请、重置码被物理删除，用户名不可逆假名化。误擦除只能重新邀请一个新账号（新的 principal）。
- **关闭功能**：撤销 Action 的注册，或在下一版策略中去掉这条策略（handler 自查返回 `approval_unavailable`）（S-E3）；已执行的擦除不受影响。
- **从备份恢复**：按 §2.3 的删除台账重放；重放完成前，被恢复的账号已是 revoked（membership），无法登录、无法投票，但哈希和原用户名短暂可见于数据库，运行手册要求恢复后立即执行重放。

## 5. 审批视图与 ADR-039 修订（I5、C1、C3）

需要以 ADR 修订的形式写入（L3）：

- **erased 判定**（ADR-039 R3 §8.6 已定，取代 §6.5）：目标成员的 subject 在擦除台账（§2.3，按 (tenant, subject) 查询）中 → `erased`；`invite_owner` 被邀请人用户名以 `erased-` 开头 → `erased`；都在第 2 步（非 owner 一律 `hidden`）之后。片 E 的迁移重定义 `runtime.nexloop_approval_target_member`，改为按台账判定并去掉 B1 过渡用的 `pseudonym:` 前缀判断（copy guard 登记）。
- 本节原先提出的 §6.1/§6.5 修订意见由 R3 §8.6、§8.7 定案，按上一条实现。
- C1 的粒度：前提是 subject 的全部 membership 已 revoked，所以“按 subject 判定 erased”不会误伤同一 subject 仍 active 的身份——那种情况下擦除根本不会发生。

## 6. 0198、configurator 与 NX-059 审计链

- **0198 运维重置**：被擦除的账号与 subject 都已 disabled，0198:45-47 与 0334:32-33 都要求二者 active，重置被拒。用擦除前的 0198 请求 ID 重放时（0198:33-37 的路径不查状态）只返回原 payload，不改变任何状态——补测试（S5）。
- **configurator**：成员文件中出现已擦除 subject 的 principal 时，apply 拒绝 `member_erased`。
- **NX-059 审计链**：擦除写一条审计事件（principal、动作、`approval_id`、删除计数，不含 reason、用户名或摘要）。擦除不改、不删既有审计事件，链的哈希不受影响；§2.4 改写的是审批冻结体，不是审计事件。保留期清除登录摘要时也写一条审计事件（计数）。

## 7. 清单与计数（S4）

- 新能力 1 个：business-actions、capabilities、catalog names 各 +1；workbench-roles owner +1、owner-only 集合测试；`test_stage_register_pg` 的 `(20, N, 2)` 按片 D 之后的数 +1。
- 审批策略：片 D 的 v2 之后为 v3，加 `workbench_member.erase_member`。
- 发布顺序与自检：B1 读擦除台账的版本先上线 → 发布 Action（自检：策略 apply 前返回 `approval_unavailable`）→ apply 策略 v3（dry-run、apply、核对 sha256；自检：返回 `waiting_approval` 或 `quorum_unreachable`）。

## 8. 测试

1. 全部 membership 已 revoked 的成员经审批擦除：账号与 subject disabled、口令与历史为空、会话行删除；邀请与重置记录删除、签发记录只清摘要；删除台账一行；principal、审计、其他审批记录保留；`target_member` 对 owner 为 `erased`，对非 owner 为 `hidden`。 擦除台账恰好一行（tenant、subject、全部 principal、approval_id、执行人），删除台账一行（world real）（S-E8）。
2. 同一 subject 另有 active principal：`target_not_revoked`，该身份不受影响、视图不显示 `erased`（C1）。
3. 竞态：审批期间被重新配置或另一身份被邀请接受 → 执行时拒绝、审批作废；两份擦除先后执行 → `already_erased`。
4. 单 owner 租户无法擦除；冷却未满的 owner 不能批准；已撤销并擦除的前 owner 不能批准任何请求（S7）。
5. `invite_owner` 入职者：冻结体用户名被假名化，视图输出 `erased`（C3）；账号列与 payload 的用户名、payload 的邮箱都是同一假名（E1）。
5a. 同名重新入职：擦除后以原用户名邀请新员工，HTTP 不拒绝；该 `invite_owner` 审批的 `target_member` 为 `invitee` 且 `same_username_exists` 为 false；接受后是新的 principal 与账号，被擦除者的台账与视图不受影响。
6. 恢复演练：恢复到擦除前的备份，重放后状态与擦除后一致（C2）。
7. 保留期：90 天边界、只清两类键、`result_snapshot` 与 `account_id` 不变、重放旧请求仍正确、清除记录幂等（I4）。
8. 0198 对已擦除账号拒绝，按旧请求 ID 重放不改状态；configurator apply 含已擦除 principal 被拒；邀请 `erased-` 前缀被拒（I5、S5）。
9. 覆盖：员工擦除覆盖测试、员工版全库兜底、Consumer 覆盖测试的登记同步（I3、S3）。
10. 回归：片 C/D、NX-056（含 B1 视图）、Consumer 擦除覆盖、retention、copy guard、definer。

## 9. 残余风险

- 恢复后到重放完成之间，哈希与原用户名在数据库中短暂可见（§4）。
- 单 owner 租户无法擦除离职员工，直到第二位 owner 加入并满 24 小时（§1）。
- 擦除不可回滚，误擦除只能以新 principal 重新入职（§4）。

## 10. 待确认

- **E1** 已定为 A（负责人 2026-10-25）：用户名与邮箱一并假名化，列与 payload 同步，前缀 `erased-`。
- **E3**（L3）已由 ADR-039 R3 §8.1、§8.6、§8.7 定案（b1661a7d）；原列：`approval_open` 对 `erase_member` 接受 revoked 目标；审批冻结体对 `invite_owner` 用户名的定点改写例外；ADR §6.1/§6.5 修订（§5）。
- **E4**：保留期冻结是否覆盖员工登录事件（建议暂不，后续项）。
- **E5**（调度员）：两张员工表按 L6 I3 继续登记为保留，不做空写入（§3）。
- E2 已按 L6 建议定为“签发记录只清摘要、保留记录”。

## 11. 与片 D3 的依赖

§2.4 依赖片 D3 的 `accept_v3` 把 `approval_id` 记进接受审计或成员行（具体位置做 D3 时确定），以及 D3 的 owner 邀请；D3 又等 NX-056 B2 的执行期字段机制（ADR-039 R3 §8，L3 b1661a7d）。片 E 实现排在 D3 之后。

## 12. L6 复核关闭说明（第 1 版）

| 编号 | 处理 |
|---|---|
| C1 | 作用单位为 subject，前提是其全部 membership 已 revoked，锁内重查；台账记 principal 与 subject；`erased` 按 subject 判定不会误伤（§0、§1、§5；测试 2） |
| C2 | 删除台账新增 kind `workbench_member`，写一行（subject、approval_id）；重放函数幂等重做擦除；恢复演练测试（§2.3；测试 6） |
| C3 | `invite_owner` 冻结体用户名定点假名化（需 L3 例外），视图对 `erased-` 前缀输出 `erased`（ADR §6.1 修订）；E1 否决时改为有理由的保留（§2.4、§5；测试 5） |
| I1 | 锁内重查全部 revoked；与片 D 的规则关系写明；三种竞态测试（§1；测试 3） |
| I2 | 冷却与可批准条件适用；单 owner 租户无法擦除；运维通道不提供擦除（§0、§1；测试 4） |
| I3 | 两张表继续登记为保留，不做空写入；更正文档现状（§3） |
| I4 | verb、RetentionPort、run_once、summary 键接线；不碰 `result_snapshot`、`account_id`；清除记录另表只追加；并发与重放测试（§2.5；测试 7） |
| I5 | ADR §6.5 修订为台账判定；`erased-` 前缀在 HTTP 与 SQL 两层拒绝；列与 payload 同步、邮箱在 payload；重新入职的 `same_username_exists` 说明（§2.1、§5） |
| I6 | 不可回滚、误擦除重新邀请、关闭方式、与恢复重放的关系（§4） |
| S1–S7 | reason 枚举且不进审计（§0、§6）；删除会话行与盘点 evidence（§2.1、§2.6）；命令日志盘点与全库兜底（§2.6）；计数与自检（§7）；subject disabled 与 0198 重放测试（§2.1、§6）；E2 采纳（§2.2）；测试表述（§8 第 4 条） |
