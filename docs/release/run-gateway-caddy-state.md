# 部署单：gateway 的 Caddy 状态不再写只读根（S59）

适用分支 `l7/s59-gateway-caddy-state`。只改 `deploy/stage/config/Caddyfile.template` 与 `deploy/stage/compose.app.example.yaml` 的 gateway；无迁移、无镜像变化、无新变量、无持久卷。

## 现象与原因

gateway 每次启动都报两条 Caddy error：

- `unable to autosave config ... /config/caddy/autosave.json: read-only file system`
- `could not clean default/global storage ... /data/caddy/locks/storage_clean.lock: no such file or directory`

gateway 根文件系统只读（x-service 的 `read_only: true`），没有挂 `/config`、`/data`（镜像的 `XDG_CONFIG_HOME`、`XDG_DATA_HOME`）。服务本身正常，但每次看日志都像新错误。

## 选择：方案 1（关闭 autosave + /data 小 tmpfs）

- Caddyfile 全局块加 `persist_config off`：配置只来自只读挂载的 Caddyfile，不写 `/config`；`admin off` 已在，没有别的写入者，所以 `/config` 不必挂任何东西。
- `/data` 挂 tmpfs（`size=4m,mode=0700`）：`auto_https off`，证书与私钥来自 secrets（`/run/secrets/gateway_*`），不签发、不续期，没有需要持久化的证书或 ACME 状态；Caddy 只在这里写存储锁，重启丢失无妨。

不选方案 2（`/config` 也挂 tmpfs）：那样 autosave 照写一份不会被读取的配置，多一个可写目录而没有用处。只读根不变，不新增持久卷。

## 步骤（APP_HOST，配置员窗口）

1. 按常规渲染部署配置（Caddyfile 与 compose），核对渲染后的 Caddyfile 全局块含 `persist_config off`，compose 中 gateway 的 `tmpfs` 为 `[/tmp, "/data:size=4m,mode=0700"]`。
2. `docker compose ... up -d gateway`（只重建 gateway）。

## 验收

- gateway 重启后，日志中 `autosave` 与 `storage_clean.lock` 两条 error 计数都为 **0**：
  `docker logs --since <重启时刻> <gateway 容器> 2>&1 | grep -c -e 'unable to autosave config' -e 'could not clean default/global storage'` 输出 `0`。
- 工作台经 gateway 登录与访问正常（HTTPS 8443，HTTP 8080 跳转）。
- `docker inspect` 显示 gateway `ReadonlyRootfs: true`，`/data` 为 tmpfs。

## 回退

恢复上一版 Caddyfile 与 compose 后重建 gateway；两条日志重新出现，功能不受影响。
