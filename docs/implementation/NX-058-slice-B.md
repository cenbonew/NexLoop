# NX-058 片 B：负责人签发重置码（不依赖邮件）

依据 `docs/design/account-self-service.md` 第 5 版的 §2、§1b、§4。迁移 `0325_workbench_reset_codes.sql` 用的是临时编号，组批时由调度员重排。

## 做了什么

| 部分 | 实现 |
|---|---|
| 签发 | 受治理 owner Action `nexloop.workbench_member.issue_reset`（capability `workbench_member.issue_reset`，operation `issue_workbench_reset`），只授权给 owner，并列入 owner-only 集合。工作台入口生成 128 位的码，在这次响应中显示一次；handler 只收到它的 `HMAC(identity_token_key, 'nexloop-reset/1'\|tenant\|码)` 和签发人来源摘要 |
| 签发时的重查 | 在租户成员锁（`nexloop-approval-eligibility:<tenant>`，与 NX-056 同一把）下重查：目标是该租户工作台的 active 成员、不是 owner（否则返回 `target_role_changed`，owner 要用片 C 的 `issue_owner_reset`）、不是请求人本人；同一目标未用的旧码一律撤销；目标账号 `session_epoch+1`，所有会话立即失效；写审计（不含码） |
| 完成 | `POST /api/v1/workbench/auth/password/reset`，请求体只有 `code` 和 `password`，不需要会话，tenant 取服务端配置。限速：`password_reset_complete` 只按来源计数；口令规则复用 `identity_password_reset.password_problem`，单张码失败 5 次即作废；所有拒绝一律返回 400 `reset_invalid`，用 argon2 假哈希对齐时间；不进入登录锁定计数 |
| 完成时的重查 | 签发人仍是 active owner，目标仍是 active 非 owner 成员，账号 revision 未变；效果与 0198 相同：新哈希、口令历史、`session_epoch+1`、清除锁定；记一条 login event `owner_issued_password_reset`（含 `same_source_as_issuer`）和一条审计，审计关联签发人与完成人 |
| 告知 | 完成重置后 7 天内，`GET /api/v1/workbench/auth/session` 都带 `reset_notices`，不能关闭；`GET /api/v1/workbench/auth/reset-history` 返回本人最近 90 天的记录 |

## 部署（调度员 ack 部署路径并上机）

1. 迁移 0325（临时编号）。
2. 私有密钥 `identity-token.key`：
   - 新生成的密钥树由 `stage_secrets.py generate` 直接写出：`app/secrets/api/identity-token.key`，32 字节随机数写成 64 位十六进制，0600，属于 api；
   - 已有的密钥树用 `stage_secrets.py identity-token-key --out <树>` 补加，不覆盖已有文件，不打印值，并记入 manifest，`check` 通过；
   - 然后按现有流程安装到 APP_HOST。
   
   Compose（`deploy/stage/compose.app.example.yaml`）给 api 和 api-2 加 secret `api_identity_token_key`，以及参数 `--identity-token-key-file /run/secrets/api_identity_token_key`。**先装文件，再部署 compose**：Compose 找不到 secret 文件就不会启动 api。
   
   密钥只来自这个私有文件，不进镜像、日志和 fixture。文件存在但内容不合法时，签发和完成两端返回 503，其他功能照常。
3. 限速策略：`stage_secrets.py browser` 注册时，同时带上 `password_reset_complete`（每个来源 300 秒内 10 次，operator 为 nexloop_identity）。缺少这条策略时，完成端点返回 503（fail closed）。
4. 发布新 Action：stage_secrets publish（Action 数 102 → 103），以及工作台角色 manifest v37（owner 角色 69 个 Action）。

## 不在片 B 内

- 对 owner 签发重置码（`issue_owner_reset`，需要审批）、失效条件全集里的停用、改角色、自行改口令（片 C）。
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
