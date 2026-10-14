# NX-032 切片 4：AT-058 双机隔离模拟（rootless Docker）

依据：调度员 2026-10-11 的裁定。CI 主机的 AppArmor 限制了非特权 user namespace，`unshare` 路线不可行；改 AppArmor 设置是 B 类操作，而且会削弱主机防护，所以不走这条路。改用 CI 用户已有的 rootless Docker。

## 拓扑

| 容器 | 网络 | 模拟对象 |
|---|---|---|
| `data`（PostgreSQL）、`data-valkey`（与 `data` 共用网络命名空间） | data_net | DATA_HOST |
| `app` | data_net、mgmt_net | APP_HOST：内部 API 端口只绑 127.0.0.1，Gateway 端口对外 |
| `neighbour` | data_net | 能在 TCP 层连到 DATA_HOST 的另一台 LAN 主机 |
| `outsider` | mgmt_net | 非 APP_HOST 的管理来源 |
| CI 用户本身 | 宿主机 | CI 主机上的 CI 用户 |

- 服务端配置由 `deploy/stage/config/` 的模板经白名单渲染器生成，与真实部署是同一套模板。
- 证书由 `scripts/deploy/issue_certs.py` 生成在私有临时目录中。
- 探测脚本是 `scripts/deploy/probe_isolation.py`，切片 5 的真实部署也用它。

## 哪些是真实判定，哪些只是拓扑等价

| 结论 | 性质 |
|---|---|
| TLS 1.3、客户端证书、客户端的 verify-full（错的 CA、SAN 不符、没有客户端证书、证书来自别的 CA、证书属于另一个角色） | 真实：服务端是真实的 PostgreSQL 和 Valkey |
| pg_hba：只放行 APP_HOST_ADDR，明文一律拒绝 | 真实：`neighbour` 带着合法证书和正确密码，仍然被拒 |
| Valkey ACL（default 用户关闭、密码错误） | 真实 |
| 内部端口只绑回环 | 真实：`outsider` 连 app 的 8000 端口被拒 |
| 管理网和 CI 用户都连不到 DATA_HOST | **拓扑等价**：`outsider` 不在 data_net 上，CI 用户无法路由进 rootless bridge。模拟中**没有加载 nft 规则** |
| LAN 邻居连 Valkey | **拓扑缺口**：Valkey 自身没有来源限制，真实部署靠防火墙拦住；模拟中邻居能连上 TLS 端口，探测如实记为 `reachable` |

## 在 CI 主机上运行（CI 用户，只读仓库检出）

```bash
python3 -I scripts/deploy/netns_sim/run.py --out "$HOME/nexloop-sim-reports/$(date -u +%Y%m%dT%H%M%SZ)"
```

- **镜像**：默认使用 `versions.lock.json` 中的三个 digest：`pgvector/pgvector`、`valkey/valkey`、`python-build-base`（python:3.12.15-slim-bookworm）。
  - 任一镜像本地没有时，脚本不会拉取，而是报 `{"passed": false, "error": "image_not_present_locally", "images": [...]}`，退出码 2。
  - 本地镜像如果只有 tag、没有这个 digest，可以用 `--postgres-image`、`--valkey-image`、`--python-image` 传入本地已有的引用。报告中会记录实际使用的引用。
- **预期输出**：一行 JSON，退出码 0。示意如下（各位置的 `failed` 都为空列表）：
  ```
  {"evidence": {...}, "images": {...}, "locations": {"app": {"failed": [], "passed": true}, "host": {"failed": [], "passed": true},
   "neighbour": {"failed": [], "passed": true}, "outsider": {"failed": [], "passed": true}}, "passed": true, "rootless": true}
  ```
- **报告**：`--out` 目录中有每个位置的报告 `report-<位置>.json`，以及汇总 `summary.json`，权限都是 0600。报告中只有位置代号、探测代号、目标代号、预期类别、实际类别和原因代码，不含任何地址或路径。
- **清理**：结束时执行 `docker compose -p nexloop-sim down -v`，并删除临时工作目录。加 `--keep` 会保留现场，用于排查。
- **只看计划、不启动**：加 `--prepare-only`。

## 注意

- 各容器的地址是 RFC 5737 文档保留段（192.0.2.0/24、198.51.100.0/24），只存在于模拟自建的 bridge 中。
- CI 用户本身的探测会向 192.0.2.20 发起 TCP 连接，预期为不可达（超时或无路由）。这个地址是保留段，不对应任何真实主机，最多会有一个 SYN 走到默认路由。
- 不连接任何其他主机，不改任何系统设置，不拉取镜像。
