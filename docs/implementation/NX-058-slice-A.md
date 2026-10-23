# NX-058 片 A：负责人邀请成员，以及角色模板实例化（Q-D = C）

依据 `docs/design/account-self-service.md` 第 7 版的 §1、§1c，以及 ADR-020 §3 修订稿。迁移 `0335_workbench_invitations.sql` 用的是临时编号，组批时由调度员重排。

## 做了什么

| 部分 | 实现 |
|---|---|
| 角色模板 | `workbench_roles.role_templates`：用 `compile_member` 和 `revoke_member` 为占位主体 `nexloop-template-principal` / `nexloop-template-subject` 生成每个角色的授权事实和撤销形式。`apply_members` 在一个 configurator 事务里分三步：①发布模板；②configure 成员；③settle。只有 `nexloop_configurator` 的 session 能调用。模板只追加，带版本号和摘要；内容不变时不新增版本 |
| 发布记录（L6 D-1） | 与 0050、NX-056 策略发布的做法一致：发送模板文本，SQL 计算并记录它的 sha256、roles manifest 版本、`expected_revision`（上一次发布的 roles manifest 版本，首次为 0），写入只追加表 `control.nexloop_workbench_template_publications`。同一版本、同一 sha256 再发一次属于重放，什么都不改；`expected_revision` 不符返回 `expected_revision_mismatch`；同一版本换了内容返回 `manifest_conflict` |
| 模板换版（AA4，L6 A-3） | 角色内容一变就新增版本。对所有当前经实例化持有该角色的成员：第①步先写旧版本的撤销形式（来源 `template-pending:<新版本>`），这样收窄后的角色能通过第②步的 grant 检查；第③步再按新版本授予（来源 `template:<新版本>`）。收窄和放宽都由测试走 configurator 的正式路径验证，测试不直接写授权表 |
| 实例化 | 内部函数 `control.nexloop_workbench_grants_write`，不授权给任何角色。它按 **JSON 值**把占位符换成成员的 principal 和 subject（只替换完全等于占位符的字符串值，不做文本拼接）；principal 和 subject 必须是身份目录的 ID 字符集，不能含引号、反斜杠、空格或 JSON 片段，否则在写入任何内容之前就拒绝（L6 A-1）。再重算依赖它们的摘要：每条 grant 的 digest，以及内容中含有该成员的那些事实的 `snapshot_digest`，都按 `eios.authz.facts` 的规范 JSON 形式计算。内容不含成员的事实（resource graph、policies）原样使用模板的字节。写入方式与 0050 相同；每次写入另记一条只追加的实例化记录 |
| 与设计的一处出入 | 设计 §1c 写的是“重算依赖 principal 的那一个摘要”。实际上，依赖成员的摘要有每条 grant 的 digest 和每条相关事实的 snapshot_digest，都已在 SQL 中按规范形式重算，并由黄金测试逐字节核对 |
| 邀请 | 受治理 owner Action `nexloop.workbench_member.invite`（只能邀请 operator 或 reviewer；邀请 owner 用片 C 的 `invite_owner`，需要审批）和 `nexloop.workbench_member.revoke_invitation`，只授权给 owner，并列入 owner-only 集合。工作台入口生成 256 位的令牌，只在这次响应中显示一次；库里只存 `HMAC(identity_token_key, 'nexloop-invite/1'\|tenant\|令牌)`。在成员锁下重查：签发人是 active owner；同一签发人 15 分钟内最多 10 次；用户名未被占用；同一用户名未用的旧邀请会被撤销；有效期 1–72 小时，默认由前端传 24 |
| 与设计的一处出入 | 设计 §1 写的是“签发时先以 invited 状态写账号和成员”。实现中**签发时不写身份目录**，到接受时才一次性写入，与 0050 的 initial identity 相同。这样不会留下悬空的 invited 账号，撤销邀请也不需要清理 |
| 接受 | `POST /api/v1/workbench/auth/invitation/accept`，请求体只有 `token` 和 `password`，不需要会话，tenant 取服务端配置。只按来源限速（`invitation_accept`）；所有拒绝都返回 400 `invitation_invalid`；口令规则复用 `password_problem`，同一令牌失败 5 次即作废 |
| 接受时写入的内容 | 身份进程用身份目录的模型构造 Human（subject、membership、account），SQL 按 initial identity 的口径校验后写入。主体插入受一条窄 RLS 策略约束：只允许正在被接受的那份邀请所记录的 subject。之后在同一事务内，依次追加一版工作台配置（`recorded_by=invitation:<id>`，configurator 自己的入口不变）、实例化角色授权、写入角色历史的 `start`、记 login event 和审计（审计关联签发人与接受人，并记 `same_source_as_issuer`） |

## 部署（调度员接手）

1. 迁移 0335（临时编号）。
2. `stage_secrets.py browser` 注册时，同时带上 `invitation_accept` 限速策略（每个来源 300 秒内 10 次）。
3. 再执行一次工作台 apply（`workbench_roles.py apply`）：它会在 configure 之后发布三份角色模板。没有模板时，接受邀请会因为 `role_template_missing` 而失败。
4. 发布新 Action：Action 数 103 → 105；工作台角色 manifest v38，owner 角色 71 个 Action。
5. 密钥 `identity-token.key` 与片 B 共用，没有新增文件。

## 测试

- `tests/test_workbench_role_templates_pg.py`：
  - 带引号、反斜杠、空格、JSON 片段、空串、占位符本身或超长的 principal 和 subject 都被拒绝，且什么都没写（A-1）；实例与模板相比，除摘要外只有成员的两个值不同；
  - 发布记录、重放、`expected_revision` 不符、同一版本换内容（D-1）；
  - 收窄和放宽两种换版都走 configurator 的正式路径（A-3）；
  - configurator 重跑并列出该成员时，事实字节不变，租户的 `authority_revision` 不变，也不新增实例化记录（A-5）；
  - 三个角色的 SQL 实例化结果与 `compile_member` 逐字节相同，撤销形式与 `revoke_member` 逐字节相同；
  - 模板换版后，重写的结果与按新角色 `compile_member` 的输出相同；
  - 只有 configurator 能发布模板；api 和 identity 角色都调用不了实例化函数；
  - 重复 apply 不会新增模板版本。
- `tests/test_workbench_invitations_pg.py`（真实 app 和 PG）：
  - 邀请、接受、登录，新成员的授权与 `compile_member` 相同；
  - 令牌只以 HMAC 存储，审计里没有令牌；
  - 5 种拒绝情形都返回同一个答案；令牌是一次性的；
  - 重新签发会撤销旧邀请；owner 可以撤销邀请；失败 5 次作废；
  - `invite` 不能邀请 owner；用户名已被占用时拒绝；签发人限速；签发人被停用后无法接受；
  - operator 无权邀请；没有密钥时两端都返回 503；
  - 用不含受邀成员的成员文件做 configurator apply 会被拒绝，断言具体错误 `a current Human grant is outside the workbench role of <principal>`（A-4；片 C 的 X1 会给出 `members_changed_by_governed_actions`）；
  - 过期的令牌被拒；签发人被降为非 owner（不只是停用）后被拒；同一令牌并发接受时只成功一次（A-6）。

## 不在片 A 内

- `invite_owner`（需要审批）、X1 的 `members_changed_by_governed_actions`、角色变更、停用、恢复、撤销，以及 configurator 路径写入角色历史，都在片 C。
- 前端（片 D）。
- 员工离职擦除（待 Q-A）。
