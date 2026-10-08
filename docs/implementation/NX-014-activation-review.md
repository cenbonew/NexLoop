# NX-014 Run activation 只读审查

2026-10-08。仅记录审查事实和待验证项，不是完成验收；本次不修改源码或 planning，不读取凭据/生产环境。

## 审查边界

API 签发的 Run-bound credential 与队列 task lease 是两种不同权威。队列 Worker 能领取 task，并不意味着它能 lookup 任意 run_id 后取得别的 service/Agent 身份。正式恢复必须证明当前 Worker 持有原始 API 授权的任务绑定，且 tenant/world/source identity/RunID/reference/nonce 全部匹配；所有许可仍来自当前 EIOS 完整授权链，SQLite checkpoint 或恢复请求不能派生新 grant。

待审文件：`nexloop_eios/runtime_activation.py`、migration `0037` 及 `backend.py` 的装配入口。初读时这两个新增文件尚未写入；已与实现 Agent 沟通，待源码出现逐项复核。

现有 `0036_runtime_task_lease_guard.sql` 的 assert_lease 检查 tenant/world/queue、running status、credential-owned lease、fence 和时效，并通过完整 queue Action authority 的签名 facts 校验。现有 `runtime_authority.py` 在 Backend 内存持有 RunCredential，校验命令等于 task payload 后重新 authenticate_run；它没有跨进程/重启恢复凭据的持久绑定机制。不能以这个内存对象证明独立 Host/Worker restart 的完整恢复。

## 必须有的证据

- 恢复入口按可信 task binding 验证，只有当前 owner/fence 能激活；任意 run_id、他租户/世界、另一个 source 主体和错 credential_ref/nonce 均拒绝。
- 原始 source credential、Agent release、parent、grant、scope/control 撤销或 Run 到期后拒绝；新的 Worker credential 不能提高 source 许可。
- 新 token 只在受限 Backend 内存出现，持久记录不存明文或可公开重放的替代 credential；返回 Host 的 DTO/command 保持 reference-only。
- 每次激活/模型/工具 dispatch 仍实时核 EIOS 当前权限；lease 与 Run Action authority 保持独立。激活不修改正式业务对象、不创建 grant。
- 取得任务锁后的最终时效/签名 proof 校验不可缺少；fence 交接不能让旧 Host 覆盖新 Worker 的结果。

实际命令仅 `rg --files`、`rg -n`、`cat` 读取当前源码。未运行构建、测试、迁移或网络调用。

## 新实现首次审查

已读取 `runtime_activation.py` 与 `0037_runtime_activation_registry.sql`。API register 必须先真实 RunBearer authenticate；register SQL role 限 api；task payload command、tenant/world、Run digest/RunID、input 摘要与 owner epoch 持久不可变绑定。Worker create 不能给 token 或直接 lookup 任意 Run，只能匹配 enrollment 和自身 live task lease/fence。authorize 的 hint/resolve/Run proof/final authorize 各阶段仍需最终 SQL 复核，不能信任前一阶段的缓存。

发现并已通知实现 Agent：

1. 初版 `activation_hint` 调用 Root identity snapshot 后直接 join FORCE RLS 的 runtime.jobs，而 identity 函数不设置 tenant GUC。正常独立连接的查询会空而拒绝合法 activation；必须基于可信 identity binding 设置事务本地 tenant，不能接受请求 tenant。
2. register 在 advisory lock 后仅复核 queue proof 和 Run identity，没有最终 command.not_after 与全部 Run proofs 时效检查。授权锁等待超过有效期可能仍注册成功；应取得锁后再次校验。
3. Run proof 使用旧 assert_action_authority，旧函数不明确拒绝 NULL expiry。0037 必须对每个 Run proof 在本地拒绝缺 expiry、已过期和超25秒有效期，并在最终返回前复查。
4. 新 authz enrollment/activation 表只通过 revoke 和 security definer 显式tenant/world条件隔离，初读版本没有 FORCE RLS policy。若作为全局技术认证目录，应明确它与tenant业务表的不同保护机制，不能宣称已有tenant RLS。

Root 新增 Host/loopback control 端口也已读审：初版 activation ref 的 colon/underscore mismatch 已在 fresh files 确認统一；TLS listener handshake 原先可能在主 accept 阻塞，现已 do_handshake_on_connect=False，Handler.setup先3秒timeout再握手；Host guard请求已加 AbortSignal.timeout(2000) 总deadline。Root报告 slow TLS test 1 passed；本审查没有执行该测试。

Host只保存activation reference，发往CA验证的loopbackHTTPS；响应只authorized/run_id，不返回RawRun、source identity或DSN。当前仅deterministic-test profile，readiness仍不宣称businessAction/channel产品闭环。Pi submission done不能当作业务成功；Worker最终结果还需明确task outcome及fenced commit。

## 修复后源码复核

最新0037已看到：hint根据可信identity设置事务tenant GUC；每项Run proof明确拒绝NULL/过期/超25秒；register和authorize最终返回前重新逐项检查proof和完整EIOS Action authority；register最后校验command.not_after，Worker结果额外核lease时效。此前指出的三项确定缺陷已在源码修复，实际PG测试结果仍由执行Agent提供，本审查未自行运行。

私有 `_run_digest` 只在受限Backend内部构造同一个Run identity，public Python authorize返回过滤下划线字段、HTTPS控制端只返回authorized/run_id。它不是公开Host credential。当前Worker activation只委托原API持有的已签发Run，没有创建grant、扩展allowed_resources或在SQLite恢复时重新签发Run。

剩余范围：不可变Enrollment是技术授权目录（显式tenant/world条件、owner-only表与security definer），不是已启用tenant RLS的业务表；真实PG任意runId/source/ref/oldfence/撤权负例和独立Host进程恢复仍需终态证据。业务Action dispatch与外部效应的独立授权/lease及unknown reconcile属于后续闭环，不由activation或Pi done代替。
