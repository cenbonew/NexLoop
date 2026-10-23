# NX-058 片 A：负责人邀请成员，以及角色模板实例化（Q-D = C）

依据 `docs/design/account-self-service.md` 第 7 版的 §1、§1c，以及 ADR-020 §3 修订稿。迁移 `0327_workbench_invitations.sql` 用的是临时编号，组批时由调度员重排。

## 做了什么

| 部分 | 实现 |
|---|---|
| 角色模板 | `workbench_roles.role_templates`：用 `compile_member` 和 `revoke_member` 为占位主体 `nexloop-template-principal` / `nexloop-template-subject` 生成每个角色的授权事实和撤销形式。`apply_members` 在 configure 之后，于同一个 configurator 事务里调用 `control.nexloop_publish_workbench_role_templates`，只有 `nexloop_configurator` 的 session 能调用。模板只追加，带版本号和摘要；内容不变时不新增版本 |
| 模板换版（AA4） | 角色内容一变，同一次发布就新增一个版本，并把所有当前经实例化持有该角色的成员，按新版本重写授权（实例化记录的来源为 `template:<版本>`） |
| 实例化 | 内部函数 `control.nexloop_workbench_grants_write`，不授权给任何角色。它把占位符换成成员的 principal 和 subject，再重算依赖它们的摘要：每条 grant 的 digest，以及内容中含有该成员的那些事实的 `snapshot_digest`，都按 `eios.authz.facts` 的规范 JSON 形式计算。内容不含成员的事实（resource graph、policies）原样使用模板的字节。写入方式与 0050 相同；每次写入另记一条只追加的实例化记录 |
| 与设计的一处出入 | 设计 §1c 写的是“重算依赖 principal 的那一个摘要”。实际上，依赖成员的摘要有每条 grant 的 digest 和每条相关事实的 snapshot_digest，都已在 SQL 中按规范形式重算，并由黄金测试逐字节核对 |
| 邀请 | 受治理 owner Action `nexloop.workbench_member.invite`（只能邀请 operator 或 reviewer；邀请 owner 用片 C 的 `invite_owner`，需要审批）和 `nexloop.workbench_member.revoke_invitation`，只授权给 owner，并列入 owner-only 集合。工作台入口生成 256 位的令牌，只在这次响应中显示一次；库里只存 `HMAC(identity_token_key, 'nexloop-invite/1'\|tenant\|令牌)`。在成员锁下重查：签发人是 active owner；同一签发人 15 分钟内最多 10 次；用户名未被占用；同一用户名未用的旧邀请会被撤销；有效期 1–72 小时，默认由前端传 24 |
| 与设计的一处出入 | 设计 §1 写的是“签发时先以 invited 状态写账号和成员”。实现中**签发时不写身份目录**，到接受时才一次性写入，与 0050 的 initial identity 相同。这样不会留下悬空的 invited 账号，撤销邀请也不需要清理 |
| 接受 | `POST /api/v1/workbench/auth/invitation/accept`，请求体只有 `token` 和 `password`，不需要会话，tenant 取服务端配置。只按来源限速（`invitation_accept`）；所有拒绝都返回 400 `invitation_invalid`；口令规则复用 `password_problem`，同一令牌失败 5 次即作废 |
| 接受时写入的内容 | 身份进程用身份目录的模型构造 Human（subject、membership、account），SQL 按 initial identity 的口径校验后写入。主体插入受一条窄 RLS 策略约束：只允许正在被接受的那份邀请所记录的 subject。之后在同一事务内，依次追加一版工作台配置（`recorded_by=invitation:<id>`，configurator 自己的入口不变）、实例化角色授权、写入角色历史的 `start`、记 login event 和审计（审计关联签发人与接受人，并记 `same_source_as_issuer`） |

## 部署（调度员接手）

1. 迁移 0327（临时编号）。
2. `stage_secrets.py browser` 注册时，同时带上 `invitation_accept` 限速策略（每个来源 300 秒内 10 次）。
3. 再执行一次工作台 apply（`workbench_roles.py apply`）：它会在 configure 之后发布三份角色模板。没有模板时，接受邀请会因为 `role_template_missing` 而失败。
4. 发布新 Action：Action 数 103 → 105；工作台角色 manifest v38，owner 角色 71 个 Action。
5. 密钥 `identity-token.key` 与片 B 共用，没有新增文件。

## 测试

- `tests/test_workbench_role_templates_pg.py`：
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
  - 用不含受邀成员的成员文件做 configurator apply 会被拒绝（0116 的授权检查；片 C 的 X1 会给出明确的错误码）。

## 不在片 A 内

- `invite_owner`（需要审批）、X1 的 `members_changed_by_governed_actions`、角色变更、停用、恢复、撤销，以及 configurator 路径写入角色历史，都在片 C。
- 前端（片 D）。
- 员工离职擦除（待 Q-A）。
