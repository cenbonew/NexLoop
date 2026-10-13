# 参考企业交付端点：部署手册（NX-037）

**这是外部系统的替身，不属于 NexLoop。** 负责人目前没有企业自有的交付端点，试运行中由负责人扮演“企业”，内部员工在这个端点的员工页面上处理交付请求。NexLoop 只通过服务效应 HTTP 协议（`nexloop-service-effect-http-v1`，[CONNECTORS.md §3](../../../docs/CONNECTORS.md)）访问它：

- 它不连接 NexLoop 的数据库，不使用 NexLoop 的任何凭据，也不加入 NexLoop 的 Compose 网络；
- 它用自己的 SQLite 文件（WAL、`synchronous=FULL`、独立卷）保存交付请求；“PostgreSQL 承载业务状态”是 NexLoop 自身的约束，不适用于这个外部替身；
- 程序：[`apps/reference-endpoint/reference_endpoint.py`](../../../apps/reference-endpoint/reference_endpoint.py)（只用标准库），镜像：[`apps/reference-endpoint/Dockerfile`](../../../apps/reference-endpoint/Dockerfile)（非 root，UID 10002）。

以下只写参数化步骤，取值都在负责人的私有文件里；每一步都属于 B 类，由负责人本人执行或逐步授权。

## 1. 准备材料（管理机）

| 材料 | 做法 |
|---|---|
| 服务凭据（NexLoop 调用时的 Bearer） | 随机生成 32 字节以上、只含 `[A-Za-z0-9._~-]`，写入私有文件 `service_token`；同一个值写进 NexLoop 效应 worker 的 provider 凭据文件 |
| 员工凭据（员工页面登录） | 另生成一个，写入 `staff_token`；必须与服务凭据不同（程序会拒绝相同的值） |
| TLS 证书与私钥 | 用内部 CA 签发服务证书，SAN 为端点的主机名或地址：`scripts/deploy/issue_certs.py server --ca-dir "$CA_DIR" --out "$CERT_DIR" --name reference-endpoint --san "DNS:$ENDPOINT_HOST_NAME" --san "IP:$ENDPOINT_HOST_ADDR"`；链文件为 `server.chain.crt`，私钥为 `server.key` |
| 私有 Compose env | 复制 [`compose.env.example`](compose.env.example) 到私有目录，填写项目名、按 digest 的镜像引用、绑定地址与端口、秘密目录 |

秘密目录中的四个文件都必须是 **0600、属主为 UID 10002** 的普通文件（程序在容器内以该用户读取，并拒绝非私有文件）。

## 2. 构建镜像

构建上下文只包含 `apps/reference-endpoint/` 目录；`PYTHON_IMAGE` 取 `versions.lock.json` 中按 digest 固定的 Python 基础镜像：

```bash
docker build --pull=false --build-arg PYTHON_IMAGE=<PYTHON_IMAGE_BY_DIGEST> -t <ENDPOINT_IMAGE_TAG> apps/reference-endpoint
```

部署时用 `docker image inspect` 得到的 digest 写入 `ENDPOINT_IMAGE`。

## 3. 启动

```bash
docker compose -f deploy/stage/reference-endpoint/compose.example.yaml --env-file <PRIVATE_COMPOSE_ENV> config -q
```

```bash
docker compose -f deploy/stage/reference-endpoint/compose.example.yaml --env-file <PRIVATE_COMPOSE_ENV> up -d
```

- 容器根文件系统只读、去掉全部 capability、`no-new-privileges`，只发布一个 TLS 端口到 `ENDPOINT_BIND_ADDR`；
- 端点所在主机与端口须对 NexLoop 的效应 worker 开放（APP_HOST 应用侧防火墙的出站规则，见 [NX-037 就绪缺口](../../../docs/implementation/NX-037-readiness-gaps.md) §1），并只对员工所在的受信网络开放员工页面。
- 撤销：`docker compose ... down`（不加 `-v`，数据卷保留）。

## 4. 一致性检查（启动后、接入 NexLoop 前）

```bash
python scripts/nx037/endpoint_conformance.py --origin https://<ENDPOINT_HOST_NAME>:<ENDPOINT_PORT> --credential-file <SERVICE_TOKEN_FILE> --ca-file <CA_FILE> --connect-address <ENDPOINT_HOST_ADDR> --out <PRIVATE_REPORT_FILE>
```

全部检查项通过（`ok: true`）才接入。检查会在端点上留下 3 条带 `nexloop_conformance_check` 标记的交付请求，员工可以把它们标记为“已处理”。

## 5. NexLoop 侧接入

效应 worker 的 provider 配置见 [`deploy/examples/nx037/enterprise-provider.example.json`](../../examples/nx037/enterprise-provider.example.json)：`origin` 为 `https://<ENDPOINT_HOST_NAME>:<ENDPOINT_PORT>`，`credential_file` 指向与 `service_token` 相同内容的私有文件，`ca_file` 为内部 CA，`connect_address` 为端点地址。其余装配见 [NX-037 真实租户业务装配手册](../../../docs/implementation/NX-037-tenant-assembly.md) §5。

## 6. 员工页面

浏览器打开 `https://<ENDPOINT_HOST_NAME>:<ENDPOINT_PORT>/staff`，用员工凭据登录（会话 8 小时，Cookie 为 HttpOnly、Secure、SameSite=Strict）。页面列出最近 500 条交付请求；对“待处理”的请求可以“标记已处理”（GET 状态变为 `fulfilled`）或“标记处理失败”（GET 状态变为 `failed`）。标记一经写入不可更改。

> **已知协议缺口**：当前 NexLoop 的客户端只认 `accepted`、`fulfilled`、`not_found` 三种状态。GET 返回 `failed` 时，NexLoop 把这次查询当作“不可用”，意图保持结果不明并按回复保障与对账流程升级给负责人，绝不会当作已完成或重发。是否在协议中加入 `failed`，由调度员另行决定。

## 7. “结果暂时未知”演练（只能在本机用管理命令打开）

演练开关只能由端点所在主机上的管理命令写入数据目录中的私有控制文件，任何 HTTP 路由都不能打开它。在容器内执行：

```bash
docker compose -f deploy/stage/reference-endpoint/compose.example.yaml --env-file <PRIVATE_COMPOSE_ENV> exec endpoint python /opt/reference-endpoint/reference_endpoint.py drill --data-dir /var/lib/reference-endpoint --delay-next-posts 1 --delay-seconds 5
```

- `--delay-next-posts N`：接下来 N 个新的交付请求先持久化，再延迟 `--delay-seconds`（2.5–60 秒，默认 5，长于 NexLoop 的 2 秒截止）才应答。NexLoop 记为结果不明，随后应当用 GET 查询并对账，而不是重新提交。
- `--get-unavailable-seconds S`：接下来 S 秒内 GET 一律返回 503，用来检查 NexLoop 在查询不可用时保持结果不明、不判为完成、不重发。
- `--status` 查看当前状态，`--clear` 关闭演练。
- 每次 `unknown_reconciliation` 交互前打开一次；对第三方端点无法这样做时，改用 [`scripts/nx037/unknown_drill_relay.py`](../../../scripts/nx037/unknown_drill_relay.py) 中继。
