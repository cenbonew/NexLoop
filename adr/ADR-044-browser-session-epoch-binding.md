# ADR-044｜浏览器会话绑定 session_epoch，不再绑定账户行 revision

- **状态**：已采纳（2026-11-02，L6 选定修法；§4 锁定策略由负责人 2026-11-02 决定）
- **依据**：AUTH-DOS（L2 在 AUTH-FP 时发现）；迁移 0404（临时号）`browser_session_epoch`；0023、0024、0031、0333、0352、0403

## 1. 背景

两处会话检查都要求账户当前的 `revision` 等于会话登录时记下的 `credential_revision`，不等就拒绝：
- `control.nexloop_read_browser_session`（0031）；
- `authz.nexloop_browser_identity_snapshot`（0403）。

但 `account.revision` 是账户行的乐观并发版本，很多与会话无关的写入也会把它加 1：
- 每次登录失败（0023 `nexloop_record_browser_account_failure`）；
- 登录成功后清零失败计数，或重新哈希密码（0024 `nexloop_save_browser_account_password`）。

结果是：任何知道用户名的人输错一次密码，该账户的全部会话就永久失效，被用户名就能触发的拒绝服务。另外，0403 的账户指纹含 `revision`，失败一次还会让持有中的 claim 作废一次。

## 2. 决定

1. 会话不再比较账户 `revision`。两处检查都去掉 `revision = credential_revision` 这一条，其余检查全部保留：
   - `session_epoch`；
   - `status = active`、subject 一致；
   - `must_change_password` / `restricted`；
   - membership 与会话期限。

   锁定条件（`locked_until`）也从这两处移出，见 §4。
2. 账户指纹白名单（`authz.nexloop_browser_fingerprint_account_keys`）去掉 `revision`、`lockout_level`、`locked_until`。
3. 会话自身记录的 `credential_revision` 不变，仍随会话一起计入哈希。
4. 凡是应当结束会话的写入，都必须把 `session_epoch` 加 1。现有路径已全部满足，本次没有补写。核对结果：

| 写入 | 函数 | session_epoch |
|---|---|---|
| 管理员重置密码（0198，0334 起） | `nexloop_browser_password_write` | +1 |
| owner 签发的重置码兑换（0333 / 0348） | `nexloop_browser_password_write` | +1 |
| 签发重置码、成员变更（暂停 / 撤销 / 降级等，0333 / 0340 / 0346 / 0348） | `nexloop_workbench_kick` | +1 |
| 成员擦除（0352，同时停用账户、删除会话） | `nexloop_workbench_member_erase` | +1（已是停用且无口令时为 0，此时 status 已拒绝） |
| 登录失败（0023） | `nexloop_record_browser_account_failure` | 不变（不应结束会话） |
| 重新哈希、成功登录后清零失败计数（0024） | `nexloop_save_browser_account_password` | 不变（口令与历史不能变，不应结束会话） |

## 3. 理由与代价

L6 的论证：
- 会话失效与行版本是两件事。`session_epoch` 是专门的"结束全部会话"计数器，所有该踢会话的写入都已经升它。
- `revision` 继续承担乐观并发和对账的职责。0023 的失败回执（`account_revision`）和 0024 的 `p_expected` 都靠它，所以不能让失败不升 revision。

否决的替代：
- **登录失败不升 `revision`**：破坏 0023 回执与 0024 预期版本的对账（L6，2026-11-02）。
- **只把失败字段排除在比较之外**：`revision` 是整行版本，无法只排除部分字段，等于重新定义行版本（L6，2026-11-02）。

代价与边界：
- 今后若新增"应结束会话"的账户写入，必须升 `session_epoch`。只升 `revision` 不会再结束会话。
- `test_browser_session_epoch_pg` 覆盖了现有四类写入（重置、踢出、停用、擦除），以及锁定期间会话保持有效。

## 4. 锁定只拦新的密码登录（负责人 2026-11-02 决定）

负责人 2026-11-02 决定：**锁定不影响已经建立的会话**。

- 锁定（`locked_until > now`，第 5 次失败触发）只拒绝新的密码登录，正确口令也会被拒。拦截点在 `eios.identity.local_accounts` 和登录凭证 `control.nexloop_browser_evidence`（0025），本次没有改动。
- 已有会话和持有中的 claim 照常有效。两处会话检查都不再看 `locked_until`。否则他人连续输错 5 次，仍能让用户掉线 15–60 分钟。
- 需要踢掉会话时，走改密码或重置（`session_epoch` + 1）。管理员重置、owner 签发重置码、成员变更都会升 `session_epoch`，见 §2 表。
- 账户指纹去掉 `lockout_level` 和 `locked_until`。按新语义，这两个字段不再决定任何已建立会话的授权，留在指纹里只会让第 5 次失败把持有中的 claim 作废一次，又回到同类问题。
- 指纹仍保留 `status`、`session_epoch`、`must_change_password`，它们仍决定授权。
- 否决的替代：锁定继续拒绝已有会话（L6 与负责人，2026-11-02），理由就是上面说的掉线问题。

## 5. 影响

- 部署时，持有旧账户指纹的浏览器 claim 会被拒绝一次（哈希变了），客户端重读即可。
- 契约不变。
