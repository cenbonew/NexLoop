# 走查单：NX-058 成员与账号在 stage 上的真实浏览器走查（L6 P1）

- **执行者**：负责人，用自己的浏览器登录 stage 工作台，以 owner 账号操作。
- **判定者**：L6。依据是每一步的页面截图（截图里不能出现令牌、重置码或口令），加上调度员事后跑的只读读数。
- **只读核对**：调度员在 DATA_HOST 执行 `ro "…"`，`$T` 为租户。任何一步都不需要写 SQL，也不需要浏览器控制台。
- **没有 B 类步骤**：不改服务器、防火墙、配置或部署，不连主机；所有写入都经工作台页面上的受治理 Action 完成。新建的合成成员和它的 Keychain 条目，是本单唯一的持久产物。

## 先说结论：现有页面和 API 支持什么（main 77c1b924 核实）

| 需要的能力 | 现状 | 依据 |
|---|---|---|
| 邀请成员 | 支持。负责人在“管理 → 成员与账号”的“邀请”表单里填用户名、角色、有效期（1–72 小时），提交后**页面上只显示一次邀请链接**（`/workbench/invitation#token=…`） | `apps/web/src/workbench/pages/Members.tsx` 的 `InviteForm`；`actions.tsx` 的 `OneTimeSecret`；Action `workbench_member.invite` |
| 受邀人设口令、开通账号 | 支持。打开链接，设置口令（至少 12 个字符），然后去登录 | `AccountPages.tsx` 的 `InvitationPage`；`POST /api/v1/workbench/auth/invitation/accept`（`browser_http.py`） |
| 口令找回 | **没有自助找回，也不发邮件**。登录页的提示是“忘记密码？请负责人为你签发重置码”。负责人在成员行上选“签发重置码”，页面只显示一次重置码（30 分钟内有效）；本人在 `/workbench/reset` 输入重置码，设置新口令 | `WorkbenchApp.tsx:45`；`Members.tsx` 的 `IssueReset`；`AccountPages.tsx` 的 `ResetPage`；`POST /api/v1/workbench/auth/password/reset`；表 `control.nexloop_workbench_reset_codes`（0333） |
| 改角色、停用、恢复、撤销 | 支持，对象限于运营（operator）和审核人（reviewer），不需要审批。对象是负责人时要经 NX-056 审批，本单不覆盖 | `Members.tsx` 的 `memberControls`；0340 |
| 变更对已有会话的影响 | 改角色、停用、撤销、签发重置码、完成重置时，都会把账号的 `session_epoch` 加 1；已经登录的会话在下一次请求时失效，回到登录页 | 0340 `nexloop_workbench_member_settle`；0333 `nexloop_workbench_kick`、`nexloop_browser_password_write` |

**`.invalid` 邮箱不构成障碍**：stage 测试账号的邮箱是 `.invalid`，工作台账号也一律是 `<用户名>@workbench.invalid`（`browser_http.py` 接受邀请时写入）。邀请链接和重置码本来就不走邮件，只在签发人的页面上显示一次，由负责人线下交给本人。这就是受治理的现有途径：令牌和重置码不在任何读接口、列表或日志里，数据库里只有 HMAC。

**缺口**：没有“本人输入用户名或邮箱、收邮件找回口令”的自助流程。这是 account-self-service 设计 v7 的选择（不可枚举、不发邮件），不是漏做。本单不需要它。以后如果要做，最小方案是：在现有重置码表上加一个“邮件渠道”签发入口，只对已验证邮箱生效，按来源限速，返回统一响应；同时要有一个真实的邮件发送 Action。这需要 ADR，本单不实现。

## 0. 准备

1. **测试成员**：新邀请一个合成成员 `walk-op-<YYYYMMDD>`，初始角色为运营。不要拿 approver2 和 approver3 做停用或撤销的对象，因为它们可能被 NX-056 审批和 P5 演练用到。确实要用时，先只读查清它们当前的角色：
   ```bash
   ro "select c.members from control.nexloop_workbench_configurations c where c.tenant_id='$T' order by c.manifest_version desc limit 1"
   ```
2. **口令（K1）**：用仓库脚本生成，经 stdin 写入 Keychain。口令不出现在命令行参数、heredoc 或文件里，也不接 `| head`；脚本只打印 `{"stored": true, …}`：
   ```bash
   python3 -I scripts/dev/keychain_new_password.py --service nexloop/stage/walk-op-<YYYYMMDD> --account walk-op-<YYYYMMDD>
   ```
   - 条目已存在时，脚本拒绝执行（`exists`）。不要手动删除 Keychain 条目。
   - 浏览器里从 Keychain 复制、粘贴。
   - ② 需要新口令时，改用新的 account 名（加 `-r` 后缀），旧条目保留，并同步 vault-index：
     ```bash
     python3 -I scripts/dev/keychain_new_password.py --service nexloop/stage/walk-op-<YYYYMMDD> --account walk-op-<YYYYMMDD>-r
     ```
     登录用户名仍是 `walk-op-<YYYYMMDD>`，`-r` 只用来区分 Keychain 里的条目。
3. **剪贴板（K2）**：每次粘贴口令、邀请链接或重置码之后，立即清空剪贴板（例如复制一段无关文字覆盖）。截图前，先关掉显示链接或重置码的表单。
4. **两个浏览器上下文**：负责人用常规窗口登录 owner；受邀人用一个隐私窗口（或另一个浏览器配置）。会话 cookie 分开，才能看到“变更让对方下线”的效果。
5. **前置读数**（调度员）：
   ```bash
   ro "select count(*) from control.nexloop_browser_accounts where tenant_id='$T' and username like 'walk-op-%'"
   ```
   用户名必须未被使用，结果应为 0。
   限速策略必须齐全（stage 曾只有 `local_login`，接受邀请和完成重置因此失败关闭；补法见 `run-34-20261015.md` §2.3）：
   ```bash
   ro "select action, maximum_attempts, window_seconds, active from control.nexloop_browser_rate_policies where tenant_id='$T' and operator_principal_id='nexloop_identity' order by 1"
   ```
   必须有 `invitation_accept`、`local_login`、`oidc_start`、`password_reset_complete` 四行，并且都是 active。缺任何一行就停下，先按 §2.3 补齐。
6. **stage web 版本**（调度员，只读）：从 stage 当前发布清单（`release.json` 的 commit）确认，部署的 web 构建包含 77c1b924。在仓库里执行 `git merge-base --is-ancestor 77c1b924 <发布 commit>`，退出码必须为 0。不满足就停下：本单的页面文案和行为取自 77c1b924。

## ① 成员邀请

| # | 操作（负责人） | 页面预期 | 失败时的样子 |
|---|---|---|---|
| 1.1 | owner 窗口：管理 → 成员与账号 → 页面底部“邀请”。用户名填 `walk-op-<YYYYMMDD>`，角色选“运营”，有效期 24，登录方式保持默认（密码），提交 | 表单下方出现“邀请令牌（只显示这一次）”和一条完整链接；“未接受的邀请”表里多一行，显示用户名、运营、签发人、到期时间 | 按钮是灰的：用户名不符合规则，或者没选角色。提交后报错：看错误文案，例如没有权限，说明这个 owner 的角色没有 `workbench_member.invite` |
| 1.2 | 复制链接（只放进剪贴板，不存文件），关掉表单，再截表格 | 关掉表单后令牌不再显示，刷新页面也看不到 | — |
| 1.3 | 隐私窗口：粘贴并打开链接，随即清空剪贴板 | 标题“接受工作台邀请”；地址栏里的 `#token=…` 立即被清掉；有两个口令输入框 | “邀请链接不完整或已失效。请向邀请你的负责人重新索取（链接只能用一次）。” |
| 1.4 | 从 Keychain 粘贴口令，两次输入一致，点“接受邀请并设置密码” | “已接受邀请，账号已开通……”和“去登录”链接 | “未能完成：链接或重置码无效、已过期或已用过，或者新密码不符合要求。”（为了不泄露信息，各种原因的提示都一样）；“尝试太频繁”（来源限速 10 次 / 5 分钟） |
| 1.5 | 点“去登录”，用 `walk-op-<YYYYMMDD>` 和这个口令登录 | 进入工作台。导航有四组：运营、模型与资产、监控、管理。“管理”组里**没有**“词表”（只有带 `world_glossary_set` 的角色才显示），其余入口都在。点“管理 → 成员与账号”，页面显示“当前账号没有查看此内容的权限。”，没有成员表和邀请表单 | 登录失败；或者“成员与账号”显示了成员表；或者导航里有“词表”：判失败，记下所见 |
| 1.6 | 用同一条链接再打开一次，填任意口令（两次一致）提交（负谓词） | 页面能打开；提交后显示“未能完成：……” | 显示“已接受邀请”就是缺陷 |

**事后读数**（调度员）：
```bash
ro "select state, role, failures, accepted_at is not null as accepted, revoked_reason from control.nexloop_workbench_invitations where tenant_id='$T' and username='walk-op-<YYYYMMDD>' order by issued_at"
ro "select a.payload->>'status', a.payload->>'session_epoch', m.principal_id, m.payload->>'status' from control.nexloop_browser_accounts a join control.nexloop_browser_memberships m on m.tenant_id=a.tenant_id and m.subject_id=a.subject_id where a.tenant_id='$T' and a.username='walk-op-<YYYYMMDD>'"
ro "select principal_id, role, event, source from control.nexloop_workbench_role_history where tenant_id='$T' and principal_id='<上一条的 principal_id>' order by entry_id"
ro "select a.subject_id from control.nexloop_browser_accounts a where a.tenant_id='$T' and a.username='walk-op-<YYYYMMDD>'"
ro "select event_type, outcome, created_at from control.nexloop_browser_login_events where tenant_id='$T' and subject_id='<上一条的 subject_id>' order by created_at"
```
预期：
- 邀请一行，`state=accepted`，`role=operator`，`failures=0`（只有状态为 issued 时失败才计数，0335 `nexloop_workbench_invitation_failed`；1.6 发生在接受之后，不计数）；
- 账号和成员身份都是 active，`session_epoch` 为 1；
- 角色历史有一行 `operator|start`，`source` 以 `invitation:` 开头；
- 有登录成功的事件。

## ② 口令重置（负责人签发、本人完成）

| # | 操作 | 页面预期 | 失败时的样子 |
|---|---|---|---|
| 2.1 | 隐私窗口保持 `walk-op` 已登录，停在任意页 | — | — |
| 2.2 | owner 窗口：成员表里 `walk-op` 那一行 → “选择操作…” → “签发重置码”，提交；把重置码复制进剪贴板，**关掉表单后**再截“最近 90 天的重置码”表 | 表单下方出现“重置码（只显示这一次）”；“最近 90 天的重置码”表里多一行，状态为“未使用” | 菜单里没有这一项：这一行的账户不是 active，或者 owner 的角色没有 `workbench_member.issue_reset`。报“尝试太频繁”：同一签发人 15 分钟内超过 10 次 |
| 2.3 | 隐私窗口：随便点一个页面或刷新 | 回到登录页，因为签发重置码时会话就已失效 | 仍然能操作：缺陷，说明会话没有失效 |
| 2.4 | 负责人按第 0 步第 2 条，用 account `walk-op-<YYYYMMDD>-r` 生成新口令（不删除旧条目，同步 vault-index）。隐私窗口在登录页点“用重置码设置新密码”，进入 `/workbench/reset`，粘贴重置码，再从 Keychain 粘贴新口令两次，提交；随即清空剪贴板 | “密码已重置，之前的登录都已失效。请用新密码登录。” | “未能完成：……”：重置码过期（30 分钟）、已用过或输错；新口令和最近用过的口令相同也报这条 |
| 2.5 | 用新口令登录 | 进入工作台；顶部出现一条不能关闭的提示：“你的密码在 … 通过负责人签发的重置码重置……”；“管理 → 账号安全”里列出这次重置 | 没有提示：缺陷（V3 要求 7 天内每次登录都显示） |
| 2.6 | 用旧口令登录（负谓词）；再用同一个重置码提交一次（负谓词） | 两次都失败 | 任何一次成功都是缺陷 |

**事后读数**：
```bash
ro "select state, failures, completed_at is not null as completed, revoked_reason from control.nexloop_workbench_reset_codes where tenant_id='$T' and target_principal='<principal_id>' order by issued_at"
ro "select a.payload->>'session_epoch', a.payload->>'failed_attempts', array_length(a.password_history,1) from control.nexloop_browser_accounts a where a.tenant_id='$T' and a.username='walk-op-<YYYYMMDD>'"
```
预期：
- 重置码一行，`state=completed`；
- `session_epoch` 比 ① 的读数多 2（签发时加 1，完成时再加 1）；
- `password_history` 至少 1 条。

## ③ 成员变更

每一小步都先让隐私窗口的 `walk-op` 保持登录，再在 owner 窗口操作；之后回到隐私窗口点一下，确认会话已失效。

| # | 操作（owner 窗口，成员行 → 选择操作） | 页面预期 | 对已登录会话的影响 | 失败时的样子 |
|---|---|---|---|---|
| 3.1 | “改角色”：选“审核人”，提交 | 表单显示成功；刷新后，成员表里这一行的角色为“审核人”；表单写着“影响：……这位成员的会话立即失效” | 隐私窗口下一次请求回到登录页；用同一口令重新登录成功，导航同 1.5（审核人也看不到“词表”和成员表） | 判失败的情形：表单出现错误提示（记下文案）；刷新后角色仍为“运营”；或者隐私窗口仍能继续操作、没有回到登录页 |
| 3.2 | “停用”：填原因，提交 | 状态为“已停用”；操作菜单只剩“恢复”“撤销成员” | 会话失效；用原口令登录失败 | 能登录就是缺陷 |
| 3.3 | “恢复”：填原因，提交 | 状态为“在用”，角色为审核人（恢复后按角色当前的模板重新授权） | 恢复本身**不**让会话失效，`session_epoch` 不变（0340：只有恢复不调用 `member_settle`）；隐私窗口此时已在登录页（3.2 造成的），用原口令可以重新登录 | 登录失败，或者状态仍为“已停用”：判失败 |
| 3.4 | “撤销成员”：填原因，提交 | 状态为“已撤销”；操作菜单只剩“擦除这位成员的个人数据”（需要审批，本单不执行） | 会话失效；不能再登录；账号保留 | 能登录就是缺陷 |

**事后读数**：
```bash
ro "select role, event, source, recorded_at from control.nexloop_workbench_role_history where tenant_id='$T' and principal_id='<principal_id>' order by entry_id"
ro "select m.payload->>'status', a.payload->>'status', a.payload->>'session_epoch' from control.nexloop_browser_memberships m join control.nexloop_browser_accounts a on a.tenant_id=m.tenant_id and a.subject_id=m.subject_id where m.tenant_id='$T' and m.principal_id='<principal_id>'"
ro "select c.manifest_version, c.members ? '<principal_id>' as present, c.members->>'<principal_id>' as role from control.nexloop_workbench_configurations c where c.tenant_id='$T' order by c.manifest_version desc limit 3"
ro "select state, revoked_reason from control.nexloop_workbench_reset_codes where tenant_id='$T' and target_principal='<principal_id>' order by issued_at"
```
预期：
- 角色历史依次为：`operator start`（邀请）→ 3.1 `operator stop`、`reviewer start` → 3.2 `reviewer stop` → 3.3 `reviewer start` → 3.4 `reviewer stop`（0340 的停用、恢复、撤销各写一行 stop 或 start）；
- 成员身份最终是 `revoked`（3.2 之后、3.3 之前是 `suspended`）；
- `session_epoch` 在 3.1、3.2、3.4 各加 1，3.3 不变；最终值等于 ② 结束时的读数加 3；
- 最新配置版本里 `present=false`；
- 重置码仍只有 ② 那一行，`state=completed`（本单没有留下未使用的码，所以不会出现 `member_changed`）。

## 判定（L6）

- 通过的条件：①、②、③ 每一步的页面预期和读数全部一致，所有负谓词都失败。
- 每张截图要标出步骤号，并且确认里面没有链接、令牌、重置码或口令。
- 不覆盖：负责人对象的变更、邀请负责人、为负责人签发重置码、擦除，都要经 NX-056 审批；SSO（见 `run-nx058-oidc-enable.md`）。

## 收尾

- `walk-op-<YYYYMMDD>` 停在“已撤销”。账号不能删除；需要时另行按审批发起擦除。
- 负责人在 vault-index 里把两条 Keychain 条目（`walk-op-<YYYYMMDD>`、`walk-op-<YYYYMMDD>-r`）标为作废。
- 本单没有在 stage 上执行过。页面文案取自 main 77c1b924 的源码，实际文案以 stage 部署的版本为准。
