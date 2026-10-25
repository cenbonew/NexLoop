# 设计增补｜NX-058 片 E：员工离职擦除

第 1 版，2026-10-25，L2。负责人 Q-A 定为「A+90天」：员工离职擦除纳入 v0.2，作为 NX-058 片 E，排在片 D 之后。上位设计是 `docs/design/account-self-service.md` 第 7 版 §4（S3 擦除传播），审批接法照片 D 设计（`docs/design/nx058-owner-actions.md` v2.8），目标展示照 ADR-039 R1 §6.5。本稿先送 L6；实现等片 D 合入 main 后开始，迁移用临时编号。

## 0. 范围

一个 owner 专用 Action `nexloop.workbench_member.erase_member`（能力 `workbench_member.erase_member`，handler 操作名 `erase_workbench_member`），请求体 `target_principal`、`reason`。走 NX-056 统一审批，策略形态与片 D 相同：无条件、quorum 1、批准人恰好是 `{owner}`、`target_subject_field: target_principal`、`solo_mode: not_applicable`、24 小时；`summary_fields` 为空，目标经审批视图 `target_member` 展示。**被擦除的人不能参与批准**：NX-056 已按 `target_subject_field` 在决定时拒收、消费时不计目标本人的票（AA2）；另外擦除前提要求目标已撤销（§1），撤销者不是 active 成员，本来也没有投票资格。

不在范围：Consumer 擦除（NX-029，另有路径）；owner 的自我擦除（请求人不能是目标）；把 principal 本身假名化（负责人口径：保留 principal）。

## 1. 前提与时机

- **目标必须已经 revoked**（经片 C 的 `revoke`、片 D 的 `revoke_owner`，或 configurator 的 `--revoke-member`）。擦除只清离职者的残留数据，不兼做撤销：撤销牵涉授权、配置和最后一位 owner 的判断，已有各自的审批口径。目标仍是 invited/active/suspended 时拒绝 `target_not_revoked`。
  - 这要求 `approval_open` 的目标检查对**这一个能力**接受 revoked 成员（片 D2 起只接受 invited/active/suspended）。按能力写死，与 B1 对 `invite_owner` 的写法同理，copy guard 登记，需 L3 同意。
- 同一成员只擦除一次：handler 以 `control.nexloop_workbench_member_erasures`（新表，只追加）判定，已擦除返回 `already_erased`。
- 期限：离职后的 90 天由**保留期服务**执行登录事件中两项摘要的清除（§2.3）；擦除 Action 本身随时可做，不等 90 天。

## 2. 擦除内容（S3，按负责人原话）

handler 在同一事务内，按片 D 的顺序（租户设置 → 成员锁 → 策略自查与审批回查 → 目标重查 → 写入），执行：

### 2.1 账号

- `control.nexloop_browser_accounts`：状态改为 `disabled`；`password_hash` 置空，`password_history` 置空数组；`session_epoch+1`、`revision+1`。
- `username` 列有唯一约束且非空，原样保留会留下可识别的登录名：改为 `erased-` 加 `sha256(tenant|local_account_id)` 前 16 位（不可逆，且在租户内唯一），`verified_email` 改为同一假名加 `@erased.invalid`。**这一条超出负责人原话**（原话是“停用账号、清除口令哈希和口令历史”），作为待确认项 E1 列出；不做的话，账号表会长期保留离职者的登录名和邮箱。
- 账号行本身保留（principal、subject 的外键和审计追溯需要）。

### 2.2 邀请与重置记录

- `control.nexloop_workbench_invitations`：删除该成员**被邀请**的记录（`accepted_principal` 等于目标，或 `accepting_subject` 等于目标 subject），以及其中的 `username`（上位设计称 `username_key`，实际列名 `username`）、`token_hmac`、`acceptor_source`、`issuer_source`。目标**签发**的邀请：撤销时已全部终结；删除其中目标为签发人的已终结记录，或只清 `issuer_source`（E2，见 §8）。
- `control.nexloop_workbench_reset_codes`：删除目标为 `target_subject` 的全部记录；目标签发的记录同 E2。
- 这两张表的删除写入 `control.nexloop_deletion_ledger`（计数，不含值），与 Consumer 擦除同一口径。

### 2.3 登录事件的两项摘要（保留 90 天）

- `control.nexloop_browser_login_events` 的 `details` 中，**来源摘要**和**用户名摘要**两类键（实现前逐条核对 0023、0024、0050、0162、0198 以及工作台 HTTP 写入的键名，盘点结果写进实现说明），离职擦除后**保留 90 天**再清除，由保留期服务按期执行：
  - 擦除时在 `control.nexloop_workbench_member_erasures` 记下 `login_digests_due_at = erased_at + 90 days`；
  - 保留期服务（`nexloop_eios.retention`）新增一个步骤：对到期且未清除的记录，把该 subject 的登录事件 `details` 中这两类键置为 `null`（只改这两类键，事件行和其余字段保留），记 `login_digests_cleared_at`。
  - 保留期冻结（retention hold）对员工数据不适用（holds 针对 Consumer），不做联动；如需要，作为后续项。
- 理由：90 天内保留来源摘要，供安全调查（例如离职前的异常登录）；之后清除。

### 2.4 保留的部分

- principal、subject、membership 行（状态已是 revoked）、授权实例与角色历史（只含 principal）、审批记录（冻结请求体中的目标 principal）、`runtime.audit_events` 与 NX-059 审计链。审计链里本来就只有 principal 和动作，不含用户名或其摘要（上位设计 §4）。

## 3. 两张员工表接入员工擦除（erasure-coverage）

`docs/implementation/erasure-coverage.md` 与 `tests/test_consumer_erasure_coverage_pg.py` 中，`control.nexloop_workbench_grant_instances` 与 `control.nexloop_workbench_role_history` 目前登记为“保留的员工身份”（有理由的例外）。改为**接入员工擦除**：

- 新增员工擦除的覆盖测试 `tests/test_member_erasure_coverage_pg.py`，口径照 Consumer 覆盖测试：凡带员工身份列（`principal_id`、`subject_id`、`issued_by_*`、`target_subject`、`accepted_principal`、`username`、`*_source` 等）的员工相关表，要么员工擦除函数中有对它的写入，要么登记理由；新表默认要求登记。
- 两张表的处理：
  - `grant_instances`：擦除时为目标写一条撤销实例（幂等，片 C 的 S-b 口径），保证不留现行授权；历史实例只含 principal 与摘要，保留，理由登记为“授权审计”。
  - `role_history`：若仍有未结束的持有，补 `stop`（来源 `erasure`）；历史行保留，理由“投票资格与审计”。
  - 覆盖测试把这两张表列在“员工擦除有写入”一侧，Consumer 覆盖测试里的登记理由同步改为“员工擦除见 NX-058 片 E”。
- 登记新表 `control.nexloop_workbench_member_erasures`（只追加、只含 principal、时间和计数）为保留，理由“擦除台账”。

## 4. 审批视图：`erased` 占位

ADR-039 §6.5 约定：成员身份被擦除时 `target_member` 显示 `{"kind":"erased"}`，不回退到请求体原值。判定以 `control.nexloop_workbench_member_erasures` 中有该 subject 的记录为准（比“username 以 pseudonym: 开头”更直接，不依赖 E1 的结论）。这需要 B1 的 `runtime.nexloop_approval_target_member` 读这张表——NX-056 读模型的改动，需 L3 同意；判定顺序仍按 §6.1（非 owner 先得到 `hidden`，不暴露是否被擦除）。

## 5. 与 0198 运维通道、NX-059 审计链的关系

- **0198 运维重置**：被擦除的账号已停用、口令哈希为空，0198 重置对它拒绝（重置要求账号存在且可用）；实现时补测试，确认 0198 不能把已擦除的账号“复活”。运维若确需恢复，只能重新邀请一个新账号（新的 principal）。
- **configurator**：成员文件里出现已擦除的 principal 时，apply 拒绝（`member_erased`），防止把已擦除的人重新配置进来；X1 的 drift 检查不受影响。
- **NX-059 审计链**：擦除 Action 写一条审计事件（principal、动作、`approval_id`、删除计数，不含用户名或摘要），自然进链。擦除**不改、不删**任何既有审计事件，所以链的哈希不受影响；这正是上位设计把用户名摘要排除在审计之外的原因。保留期服务清除登录事件摘要时也写一条审计事件（计数）。

## 6. 清单与计数

- 新能力 1 个：business-actions、capabilities、catalog names 各 +1；workbench-roles owner +1（片 D 后的计数再 +1）、owner-only 集合。
- 审批策略：`approval-policies` 下一版（片 D 的 v2 之后为 v3）加 `workbench_member.erase_member`。
- 发布顺序同片 D：先发布 Action，再 apply 策略；B1 的 `erased` 判定先上线。

## 7. 测试

1. 撤销后的成员经审批擦除：账号 disabled、口令与历史为空、会话失效；邀请与重置记录删除、台账计数正确；principal、审计、审批记录保留；`target_member` 对 owner 显示 `erased`，对非 owner 仍是 `hidden`。
2. 目标未撤销：`target_not_revoked`；已擦除：`already_erased`；请求人即目标：拒绝。
3. 被擦除的人（ripe owner 被撤销后）不能批准任何请求；目标本人的票不计（AA2）。
4. 保留期服务：未到 90 天不清；到期后只清两类摘要键，其余字段与行保留，记录清除时间与审计。
5. 0198 对已擦除账号拒绝；configurator apply 含已擦除 principal 被拒。
6. 覆盖测试：员工擦除覆盖全部员工身份表；Consumer 覆盖测试的登记同步。
7. 回归：片 C/D、NX-056（含 B1 视图）、Consumer 擦除覆盖、copy guard、definer。

## 8. 待确认

- **E1**（负责人）：用户名与邮箱是否一并假名化（建议是；否则账号表长期保留离职者登录名和邮箱）。
- **E2**（负责人/L6）：目标**签发**的已终结邀请和重置记录，是删除，还是只清 `issuer_source`（建议只清摘要、保留记录：它们记录的是对他人的操作，属于他人的审计上下文）。
- **E3**（L3）：`approval_open` 对 `erase_member` 接受 revoked 目标；`target_member` 以擦除台账判定 `erased`。
- **E4**：保留期冻结是否需要覆盖员工登录事件（建议暂不，作为后续项）。
