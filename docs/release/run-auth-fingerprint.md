# 运行单：浏览器授权指纹（AUTH-FP，stage）

- 适用分支：`l2/auth-fingerprint`，基于 main e862dc41。迁移 0403（临时号，调度员合批时重编）。
- 内容：浏览器 `directory_hash` 只对决定授权的字段求哈希；`browser_authentication` 事实的 `record_hash` 不再包含随每次请求变化的 `expires_at`。所有检查不变。
- 执行者：调度员。判定者：L6。

## 上线影响

- 迁移生效的那一刻，所有用旧哈希签发、还在途中的浏览器 claim 会被拒一次（42501，接口上表现为 403/503，SSE 读取失败）。
- 兜底：前端会重读；SSE 连续 3 次读取失败才关流（SSE-DROP），关流后 5 秒自动重连。不需要用户操作，也不需要重启。
- 工作台会话和服务凭据的指纹不在本单范围，不受影响；已登录的浏览器会话本身不会被登出。

## 步骤

1. 部署前（只读）记基线：取部署前 30 分钟的计数，方法见下文"计数方法"。
2. 按部署单备份、迁移 0403，重建 api。
3. 部署后 5 分钟、30 分钟各计一次。

## 计数方法（只读，在 APP_HOST 上执行）

```bash
docker logs --since 30m <api 容器> 2>&1 | grep -c 'events read failed'
```
```bash
docker logs --since 30m <api 容器> 2>&1 | grep -oE "events read failed \{'reason': '[A-Za-z]+'" | sort | uniq -c
```
```bash
docker logs --since 30m <PostgreSQL 容器> 2>&1 | grep -cE 'browser (action authority (stale|changed)|business (account|session|membership|subject) (stale|expired))'
```

- 第一条：SSE 计数失败的次数（只记异常类名，SSE-DROP 加的 warning）。第二条按异常类名分组。
- 第三条：PostgreSQL 日志里浏览器授权被拒（42501）的次数；PostgreSQL 只在 `log_min_error_statement` 覆盖 ERROR 时才记，记不到时如实写"日志不含"，只用前两条。
- 预期：部署后 5 分钟内可能有一个尖峰（旧 claim 被拒一次），30 分钟时回落到基线或更低（同一会话并发请求互相作废的那部分消失）。
- 30 分钟时仍明显高于基线：停下，把三条计数报调度员，不要回滚迁移（回滚需要新迁移）。

## 局限

- 没有单独的 membership 版本用例，membership 整行仍在指纹里。
- 他人输错密码会让账户 revision 加 1（0023），该账户所有会话随即被判为 stale，这与指纹无关；AUTH-DOS（0404，`run-auth-session-epoch.md`）修复。

## 证据

- 部署前后三次计数（只记数字和异常类名）；迁移版本号。
