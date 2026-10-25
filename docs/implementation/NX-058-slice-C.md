# NX-058 片 C（不需要审批的部分）：成员变更与 configurator 路径

依据 `docs/design/account-self-service.md` 第 7 版的 §1b、§1c、§4、§7。调度员 2026-10-23 决定：owner 专用 Action 等 NX-056 合入后再接，在此之前不开放；2026-10-25 定方案 1，解决受治理入口尾部复核的问题。迁移 `0337_workbench_member_changes.sql` 和 `0338_governed_authority_change.sql` 都是临时编号。

## 做了什么

| 部分 | 实现 |
|---|---|
| 成员变更 Action | `nexloop.workbench_member.change_role`（只在 operator 与 reviewer 之间切换）、`suspend`、`resume`、`revoke`，只授权给 owner。目标只取请求体的 `target_principal`（AA2），在成员锁内重查目标的角色和状态：目标是 owner 时返回 `target_role_changed`，目标是请求人本人时返回 `target_is_requester`；状态不符时返回 `target_not_active` 或 `target_not_suspended`；角色未变时返回 `role_unchanged` |
| 同一事务内的效果 | 先撤销旧角色的授权，再追加配置版本（停用和恢复改的是 membership 状态，不追加配置），然后授予新角色的模板（恢复时重新授予，AA1）。同时写角色历史，让成员的所有会话失效，撤销未用的重置码（原因 `member_changed`），并记一条审计 |
| 尾部复核（方案 1，0338） | handler 在第一次写授权前调用 `control.nexloop_authority_handler_mark`：它由 owner 持有、不授权给任何角色，先锁住租户行，再记下（txid、变化前的 revision）。0045 的 `nexloop_browser_identity_snapshot` 只改了一处：`directory_hash` 里的 tenant revision 改为 `authz.nexloop_effective_tenant_revision` 的返回值。只有存在本事务的标记、并且 revision 在标记之后有增加时，它才返回变化前的值；标记以 `FOR UPDATE` 锁住租户行，所以这些增量就是**本事务在 mark 之后的所有写入**（不只是 handler 自己的那几次写，L6 I-3）。一个事务只允许一个标记，第二次调用直接拒绝；判定不看 xmin，所以 handler 在 savepoint 里执行（NX-056 gate）也成立（I-1）；判定函数为 VOLATILE；一天前的标记在下一次 mark 时清理。I-2：mark 先取 NX-056 的 `control.nexloop_approval_tenant_lock`，再锁租户行；片 B 的成员锁 `control.nexloop_workbench_member_lock` 也改为直接调用这把锁（create or replace，已登记 copy guard），所以经 NX-056 gate 的变更与不需要审批的变更按同一顺序取锁。I-1 已在含 NX-056 gate 的代码上验证：在审批策略下，handler 在 gate 的 savepoint 里执行，复核通过；反向检查确认，恢复旧的 xmin 条件后同一用例返回 403。请求人的 session、subject、account（revision 和 session_epoch）、membership 仍按当前值复核。已登记 copy guard |
| X1 / Z2 | `control.nexloop_workbench_governed_drift`：自上一次 configurator 版本以来，由受治理变更改过角色或去留、而成员文件又会把它改成别的值的成员，apply 一律拒绝，返回 `members_changed_by_governed_actions: <principal>=<角色>`；`--revoke-member` 点名的 principal 例外 |
| configurator 路径的角色历史（S4） | `control.nexloop_workbench_role_history_sync`：apply 之后写入 configurator 变更造成的 start 和 stop；被移除或换角色的成员，会话失效、未用的重置码撤销 |
| 片 A、B 复核建议 | E-1：模板发布要求 `roles_manifest_version` 为正整数，否则返回 22023。E-2：CAS 不符改为 P0001 `expected_revision_mismatch`，不再用会被当作可重试的 40001。E-3：接受邀请时，ID 必须是 `workbench-<32hex>`、`workbench-subject-<同一 hex>`、`workbench-account-<同一 hex>`。E-4：并发接受测试用 advisory lock 做屏障。A-7：接受时校验时间戳不晚于当前时间，membership kind 为 home。A-8：每次实例化写一条 `grants_written` 审计并引用实例化记录（含模板换版），接受邀请另记一条 detail 审计，带 instance_id 和配置版本（审计行不更新，因为 NX-059 审计链持有它们的摘要）。A-10、B-1：签发类限速返回 429，`Retry-After: 900` |
| A-9（文档约束） | 模板携带角色 Action 的租户级 `resource_graph` 事实，内容与所有授权路径写入的相同；只有内容不同时才会被改写 |
| v2 包装 | 0333–0335（第二十四批，已发布），不再改动。接受邀请、模板发布、settle 改为调用 `*_v2`，旧入口收回授权 |

## 测试

- `tests/test_workbench_member_changes_pg.py`（真实 app 和 PG）：
  - 改角色、停用、恢复、撤销的完整流程：授权与 `compile_member` 逐字节一致，会话失效，重置码撤销，角色历史和审计都正确；
  - owner 作为目标、请求人本人作为目标、operator 调用，都被拒绝；
  - X1 拒绝撤销受治理的变更，点名撤销则放行；
  - configurator 移除成员时让会话失效，并写入角色历史。
- `tests/test_governed_authority_change_pg.py`：
  - 没有标记、标记之前已有增量、或者是另一个事务的标记时，都按当前 revision 复核；只有本事务在标记之后的增量才保留变化前的值；
  - 持有标记的事务会挡住其他事务的增量；
  - 任何应用角色都不能写标记、不能调用标记函数；设置 session 变量没有任何作用；
  - snapshot 中请求人自己的行仍按当前值复核。
- 模板测试和邀请测试补上了 E-1/E-2/E-4/A-8/A-10 的断言。

## 缓存核查（L6 S-6）

`directory_hash` 只存在于每次请求重新认证得到的 session 对象上（工作台每次调用都重新认证）。Valkey 只传递唤醒信号，不缓存认证或 inspection 结果。所以授权变更之后，没有跨请求缓存的旧 `directory_hash`。

## 不在本片内

- owner 专用 Action（`invite_owner`、`promote_owner`、`demote_owner`、`suspend_owner`、`resume_owner`、`revoke_owner`、`issue_owner_reset`）：等 NX-056 合入 main 后接审批，在此之前不开放。
- 账号自行改口令时撤销码（`password_changed`）：现有的改口令路径不在本片范围内，留作后续。
- 前端（片 D）。
