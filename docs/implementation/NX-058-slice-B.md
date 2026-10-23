# NX-058 片 B：负责人签发重置码（不依赖邮件）

依据 `docs/design/account-self-service.md` 第 5 版的 §2、§1b、§4。迁移 `0333_workbench_reset_codes.sql` 用的是临时编号，组批时由调度员重排。

## 做了什么

| 部分 | 实现 |
|---|---|
| 签发 | 受治理 owner Action `nexloop.workbench_member.issue_reset`（capability `workbench_member.issue_reset`，operation `issue_workbench_reset`），只授权给 owner，并列入 owner-only 集合。工作台入口生成 128 位的码，在这次响应中显示一次；handler 只收到它的 `HMAC(identity_token_key, 'nexloop-reset/1'\|tenant\|码)` 和签发人来源摘要 |
| 签发时的重查 | 在租户成员锁（`nexloop-approval-eligibility:<tenant>`，与 NX-056 同一把）下重查：签发人仍是 active owner（`issuer_not_owner`，L6 S-1）；同一签发人 15 分钟内最多签发 10 张（`rate_limited`，L6 I-1：被盗的 owner 会话无法反复把成员踢下线）；目标是该租户工作台的 active 成员、不是 owner（否则返回 `target_role_changed`，owner 要用片 C 的 `issue_owner_reset`）、不是请求人本人；同一目标未用的旧码一律撤销；目标账号 `session_epoch+1`，所有会话立即失效；写审计（不含码） |
| 完成 | `POST /api/v1/workbench/auth/password/reset`，请求体只有 `code` 和 `password`，不需要会话，tenant 取服务端配置。限速：`password_reset_complete` 只按来源计数；口令规则复用 `identity_password_reset.password_problem`，单张码失败 5 次即作废；所有拒绝一律返回 400 `reset_invalid`，用 argon2 假哈希对齐时间；不进入登录锁定计数 |
| 完成时的重查 | 签发人仍是 active owner，目标仍是 active 非 owner 成员，账号 revision 未变；口令写入与 0198 **共用同一个 SQL 函数** `control.nexloop_browser_password_write`（新哈希、口令历史、`session_epoch+1`、清除锁定）；记一条 login event `owner_issued_password_reset`（含 `same_source_as_issuer`）和一条审计，审计关联签发人与完成人 |
| 0198 运维重置 | 迁移 `0334_operator_reset_revokes_codes.sql`（临时编号）用 create or replace 重定义 0198 的函数，签名和授权都不变，已登记到 copy guard：改为调用同一个口令写入函数，并撤销该账号所有未用的重置码（原因 `operator_reset`），紧急重置之后旧码立即失效（L6 I-2） |
| 告知 | 完成重置后 7 天内，`GET /api/v1/workbench/auth/session` 都带 `reset_notices`，不能关闭；`GET /api/v1/workbench/auth/reset-history` 返回本人最近 90 天的记录 |

## 部署（调度员 ack 部署路径并上机）

1. 迁移 0333（临时编号）。
2. 私有密钥 `identity-token.key`：
   - 新生成的密钥树由 `stage_secrets.py generate` 直接写出：`app/secrets/api/identity-token.key`，32 字节随机数写成 64 位十六进制，0600，属于 api；
   - 已有的密钥树用 `stage_secrets.py identity-token-key --out <树>` 补加，不覆盖已有文件，不打印值，并记入 manifest，`check` 通过；
   - 然后按现有流程安装到 APP_HOST。
   
   Compose（`deploy/stage/compose.app.example.yaml`）给 api 和 api-2 加 secret `api_identity_token_key`，以及参数 `--identity-token-key-file /run/secrets/api_identity_token_key`。**先装文件，再部署 compose**：Compose 找不到 secret 文件就不会启动 api。
   
   密钥只来自这个私有文件，不进镜像、日志和 fixture。文件存在但内容不合法时，签发和完成两端返回 503，其他功能照常。
3. 限速策略：`stage_secrets.py browser` 注册时，同时带上 `password_reset_complete`（每个来源 300 秒内 10 次，operator 为 nexloop_identity）。缺少这条策略时，完成端点返回 503（fail closed）。
4. 发布新 Action：stage_secrets publish（Action 数 102 → 103），以及工作台角色 manifest v37（owner 角色 69 个 Action）。

## 密钥轮换（L6 S-5）

轮换会让所有未用的重置码立即失效（片 A 上线后，未接受的邀请也一样），需要重新签发。两个 api 进程必须同时换成新密钥，否则会出现一端签发、另一端无法完成的情况。

1. 在管理机上对密钥树执行：
   ```bash
   stage_secrets.py identity-token-key --out <树> --rotate
   ```
   它写入新值并更新 manifest，不打印值，`check` 能通过。
2. 按现有流程把 `app/secrets/api/identity-token.key` 安装到 APP_HOST（0600，属于 api）。
3. 同时重建两个 api 进程：
   ```bash
   docker compose up -d --force-recreate api api-2
   ```
4. 告知 owner：轮换之前签发、还没使用的码已经失效，需要重新签发。

## 运行手册要点（L6 S-8）

- **只有两位 owner 时，重置另一位 owner 的口令**：能批准的另一位 owner 就是目标本人，审批无法完成，所以走 0198 运维通道（`identity_password_reset`）。0198 现在会同时撤销该账号所有未用的码。
- **负责人本人忘记口令**：同样走 0198。
- **签发失败后重试**（L6 S-7）：入口每次请求都会生成新码。用同一个 Idempotency-Key 重试，得到的是 `request_reused` 冲突或终态重放，都拿不到码。前端（片 D）在签发失败后要换一个新的 Idempotency-Key 重新签发，旧码自动撤销。
- **会话视图**：读取提示失败时不影响登录态，只是不返回 `reset_notices` 字段，并记一条日志（只有类型名，L6 S-4）。

## 不在片 B 内

- 对 owner 签发重置码（`issue_owner_reset`，需要审批）；失效条件全集里的停用、改角色、自行改口令（片 C）。**残余风险**：在片 C 之前，成员被 configurator 停用或改角色后，签发给他的未用码在 30 分钟内仍然存在。完成时会重查“目标仍是 active 非 owner 成员”，所以停用和升为 owner 的情形都会被拒绝；只有在 operator 和 reviewer 之间改角色时，旧码仍能完成。
- 签发时通知成员（L6 S-2）：放在片 D，成员页和登录后的提示一起做。
- 时间对齐（L6 S-3）：“签发人已失效”的拒绝，比“码不存在”多做几次 argon2 校验，两者耗时可以区分。码有 128 位熵，不能用于枚举，所以作为低风险接受。
- 前端：成员页的签发入口、owner 可见的最近 30 天重置记录、重置页、登录后的提示（片 D）。
- 邀请（片 A，等待 Q-D）。

## 测试

`tests/test_workbench_reset_codes_pg.py`：使用真实 app 和 PG，覆盖：
- 签发、完成，以及签发时踢掉会话；
- 码只以 HMAC 存储，审计里也没有码；
- 6 种拒绝情形都返回同一个答案；
- 一次性：同一张码不能用第二次；
- 重新签发会撤销旧码；
- 失败 5 次作废；
- 签发人不再是 owner 时无法完成；
- 没有密钥时两端都返回 503；
- operator 不能签发；
- 另一位 owner 作为目标时返回 `target_role_changed`；
- 7 天提示和 90 天记录；
- 审计关联签发人与完成人。
