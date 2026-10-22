# 登录限速的键（L6 C11，调度员 2026-10-22）

## 问题

浏览器登录的限速键只用 `request.client.host`，每次尝试都会扣一次。stage 的网关（Caddy）先删掉 `X-Forwarded-For`，再反向代理到回环上的 API，所以经网关的所有登录，来源都是同一个回环地址：**整个租户共用一个限速桶**。任意一个来源的大量尝试，都会让所有成员和 owner 收到 429，可以被滥用成全租户的登录拒绝服务。

顾客 WebChat 登录（`/api/v1/auth/login`）和工作台登录（`/api/v1/workbench/auth/login`）用的是同一个 `browser_http.router`，两者都受影响，这次一起修了。代码里没有别的地方用 `request.client` 做限速或审计。

## 修复

1. **来源地址只采信受信代理覆盖写入的头**：
   - 网关用 `header_up X-NexLoop-Client-IP {remote_host}` 覆盖写入这个头，客户端自带的同名头会被替换；`X-Forwarded-For` 仍然删掉。
   - API 只有在请求来自 `--trusted-proxy-peer` 配置的地址（stage 是 `127.0.0.1`，网关和 API 在同一个网络命名空间）时，才采信这个头，而且这个头必须恰好有一个、取值必须是规范的 IP。头缺失、有多个、取值不规范，或者请求不是来自受信代理，都返回 503，不会改用别的值。
   - 不配置受信代理时，和改动前一样只看 socket 对端（伪造不了），所有转发头都忽略。
   - 解析器取自上游 NEX-EIOS 冻结提交的 `src/eios/api/identity_client_ip.py`，vendored 到 `packages/eios-core/src/eios/identity/client_ip.py`。头名改成 `X-NexLoop-Client-IP`，provenance 记在 `PROVENANCE.json`（ADR-006）。
2. **新增按用户名限速的维度**，与按来源地址的维度并存：
   - 两个维度都用租户的 `local_login` 策略和同一张计数表，靠摘要的域区分：来源地址用 `ip:<地址>`，用户名用 `user:<sha256(规范化用户名)>`。规范化方法是 NFKC、去掉首尾空白、case-fold。表里只存 HMAC 摘要。不需要迁移。
   - 每次尝试两个维度各扣一次，任一超限即返回 429，并带上 `Retry-After`。地址维度先扣，请求体格式不对也照样扣；用户名维度在请求体合法之后才扣。
   - **失败计数与成功登录**：和原有策略一样，成功登录也计入次数，不会重置任何一个桶。
   - **已知取舍**：攻击者可以从多个来源反复尝试同一个用户名，把这个账户在窗口期内锁住。这是按用户名限速的固有代价，影响范围只到这一个账户，其他用户不受影响；窗口结束后自动恢复。

## 对今晚登录高峰复测（负载车道 C4-peak）的影响

- 压测车道**不经过网关**：驱动给每个顾客绑定一个不同的回环源地址（`scripts/load/driver.py` 的 `source_address`），直接连 API。所以修复前后都是按 socket 对端分桶，每个顾客一个桶，压测的来源地址口径没有变化。
- 新增的用户名维度下，每个顾客在 `login_all` 中只登录一次，不会触发。
- 结论：这次修复**不影响**复测的口径和结果。反过来，修复前那个“经网关后全租户共用一个桶”的问题，压测车道本来就测不到；它只出现在 stage 上经网关的真实登录。

## 部署

- `deploy/stage/config/Caddyfile.template`：新增 `header_up X-NexLoop-Client-IP {remote_host}`。
- `deploy/stage/compose.app.example.yaml`：两个 API 进程都加上 `--trusted-proxy-peer 127.0.0.1`。
- API 和网关要一起上线。只上 API、网关还是旧版时，经网关的登录会因为缺少这个头而返回 503。
