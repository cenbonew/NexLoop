# 参考端点员工令牌：存入钥匙串与取用（负责人本人，管理机）

参考端点员工页（`https://<ENDPOINT_HOST_NAME>:<ENDPOINT_PORT>/staff`）的员工令牌，目前只在负责人管理机上的一个私有文件里。下面两条命令把它存进 macOS 钥匙串，以后从钥匙串取用：
- 钥匙串条目：service 为 `nexloop/endpoint/staff`，account 为 `nexloop`；
- 令牌的值**不出现在任何进程的命令行参数中，也不打印到屏幕**；
- vault-index 只登记条目名，由调度员更新。

## 1. 存入（一次；令牌轮换后再执行一次）

```
F=~/.nexloop/stage/endpoint-20261015/secrets/staff_token
grep -Eqx '[A-Za-z0-9._~-]{16,4096}' "$F" && { printf 'add-generic-password -U -s nexloop/endpoint/staff -a nexloop -w '; tr -d '\n' < "$F"; printf '\n'; } | security -i >/dev/null && echo stored
```

- **为什么值不会进入 argv**：
  - `printf` 是 shell 内建命令，不会产生新进程；
  - `tr` 从文件读取令牌，写到管道；
  - `security -i` 从标准输入读取这一行命令，令牌只经过管道；
  - `>/dev/null` 丢掉 `security` 的提示符输出。
- **前面的 `grep` 检查**：文件必须恰好是一行令牌，只含 `[A-Za-z0-9._~-]`（参考端点的凭据格式，见 README §4）。这样令牌里不会有空格或引号，`security -i` 解析时不会被拆开或截断。检查不通过时不会写入，也不会输出任何内容。
- `-U`：条目已存在时更新它，用于令牌轮换。
- **预期**：只输出一行 `stored`。macOS 可能弹窗询问是否允许访问钥匙串，选择“允许”。
- **核对**（不打印值）：
  ```
  cmp -s <(security find-generic-password -s nexloop/endpoint/staff -a nexloop -w) <(tr -d '\n' < "$F"; echo) && echo same
  ```
  预期 `same`。
- **之后私有文件怎么处理**：由负责人决定。确认 `same` 后可以删除管理机上的这份副本；端点主机上的 `staff_token` 是服务本身要用的，不动。

## 2. 取用：复制到剪贴板

```
security find-generic-password -s nexloop/endpoint/staff -a nexloop -w | tr -d '\n' | pbcopy
```

- 在员工页的“员工凭据”框中粘贴，登录（会话 8 小时）。
- 登录后清空剪贴板：`pbcopy </dev/null`。
- 不要把令牌粘贴到聊天、文档或代理的指令里。员工侧的代理只使用负责人事先登录好的浏览器会话（NX-037-agent-players §7）。
