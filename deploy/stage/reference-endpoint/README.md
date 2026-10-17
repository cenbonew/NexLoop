# 参考企业交付端点：部署手册（NX-037）

**这是外部系统的替身，不属于 NexLoop。** 负责人目前没有企业自有的交付端点，试运行中由负责人扮演“企业”，内部员工在这个端点的员工页面上处理交付请求。

负责人定案（2026-10-13，ROADMAP §7）：

- **部署位置**：APP_HOST，作为独立的 Compose 项目运行（独立项目名、独立卷、不加入 NexLoop 的网络），与 NexLoop 应用栈同机但互不依赖；
- **交付语义**：服务工单。每个客户请求成为一张工单，员工在工作时间内处理，并标记“已处理”或“处理失败”。工作时间外到达的请求保持“待处理”，NexLoop 侧按查询结果等待，不重发。NexLoop 只通过服务效应 HTTP 协议（`nexloop-service-effect-http-v1`，[CONNECTORS.md §3](../../../docs/CONNECTORS.md)）访问它：

- 它不连接 NexLoop 的数据库，不使用 NexLoop 的任何凭据，也不加入 NexLoop 的 Compose 网络；
- 它用自己的 SQLite 文件（WAL、`synchronous=FULL`、独立卷）保存交付请求；“PostgreSQL 承载业务状态”是 NexLoop 自身的约束，不适用于这个外部替身；
- 程序：[`apps/reference-endpoint/reference_endpoint.py`](../../../apps/reference-endpoint/reference_endpoint.py)（只用标准库），镜像：[`apps/reference-endpoint/Dockerfile`](../../../apps/reference-endpoint/Dockerfile)（非 root，UID 10002）。

以下只写参数化步骤，取值都在负责人的私有文件里；每一步都属于 B 类，由负责人本人执行或逐步授权。

## 1. 准备材料（管理机）

| 材料 | 做法 |
|---|---|
| 服务凭据（NexLoop 调用时的 Bearer） | 随机生成 32 字节以上、只含 `[A-Za-z0-9._~-]`，写入私有文件 `service_token`；同一个值写进 NexLoop 效应 worker 的 provider 凭据文件 |
| 员工凭据（员工页面登录） | 另生成一个，写入 `staff_token`；必须与服务凭据不同（程序会拒绝相同的值） |
| TLS 证书与私钥 | 用内部 CA 签发服务证书，SAN 为端点的主机名或地址：`scripts/deploy/issue_certs.py server --ca-dir "$CA_DIR" --out "$CERT_DIR" --name reference-endpoint --san "DNS:$ENDPOINT_HOST_NAME" --san "IP:$APP_HOST_ADDR"`；链文件为 `server.chain.crt`，私钥为 `server.key` |
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
- 端点运行在 APP_HOST 上；`ENDPOINT_BIND_ADDR` 取 APP_HOST 上供效应 worker 访问的地址（`<APP_HOST_ADDR>`）。效应 worker 从 NexLoop 容器网络访问这个地址与端口，须在 APP_HOST 应用侧防火墙放行这一条出站规则，见 [NX-037 就绪缺口](../../../docs/implementation/NX-037-readiness-gaps.md) §1），并只对员工所在的受信网络开放员工页面。
- 撤销：`docker compose ... down`（不加 `-v`，数据卷保留）。

## 4. 一致性检查（启动后、接入 NexLoop 前）

```bash
python scripts/nx037/endpoint_conformance.py --origin https://<ENDPOINT_HOST_NAME>:<ENDPOINT_PORT> --credential-file <SERVICE_TOKEN_FILE> --ca-file <CA_FILE> --connect-address <APP_HOST_ADDR> --out <PRIVATE_REPORT_FILE>
```

全部检查项通过（`ok: true`）才接入。检查会在端点上留下 2 张摘要为“NexLoop conformance check …”的工单（其中一张的冲突变体被 409 拒绝，不会落库），员工可以把它们标记为“已处理”。端点对 POST 与 GET 都只返回 `fulfilled`，所以检查默认要求 `fulfilled`。

## 5. NexLoop 侧接入

效应 worker 的 provider 配置见 [`deploy/examples/nx037/enterprise-provider.example.json`](../../examples/nx037/enterprise-provider.example.json)：`origin` 为 `https://<ENDPOINT_HOST_NAME>:<ENDPOINT_PORT>`，`credential_file` 指向与 `service_token` 相同内容的私有文件，`ca_file` 为内部 CA，`connect_address` 为端点地址。其余装配见 [NX-037 真实租户业务装配手册](../../../docs/implementation/NX-037-tenant-assembly.md) §5。

## 6. 员工页面

浏览器打开 `https://<ENDPOINT_HOST_NAME>:<ENDPOINT_PORT>/staff`，用员工凭据登录（会话 8 小时，Cookie 为 HttpOnly、Secure、SameSite=Strict）。页面按工单列出最近 500 条请求，字段为：请求摘要（含详情与客户引用）、来源租户与意图 id、收到时间、处理人、状态（待处理 / 已处理 / 处理失败）、处理结果与备注。员工处理“待处理”工单时必须填写处理人（最多 64 字），可填备注（最多 500 字），再选“已处理”或“处理失败”；处理时间自动记录。标记一经写入不可更改。

- **视图**（2026-10-17）：默认只显示**待处理的客户工单**，按收到时间倒序；“全部客户工单”和“一致性检查工单”各有自己的视图，带计数。一致性检查工单按摘要识别：`NexLoop conformance check …`（`endpoint_conformance.py` 的默认摘要），或 `端点一致性检查`（命令单中的空操作参数）。时间按北京时间显示，鼠标悬停可看 UTC；存储仍为 UTC。
- 请求参数就是 `nexloop.enterprise.deliver:1` 的参数：`summary`（必填，最多 200 字）、`details`（可选，最多 2000 字）、`consumer_ref`（可选，NexLoop 的不透明客户引用，员工可凭它在工作台找到对应会话）。带其他键、类型或长度不对的请求一律返回 400 `parameters_invalid`，不落库。
- “来源租户”不来自请求：一个端点只服务一个企业，取部署配置里的租户标签 `ENDPOINT_TENANT_LABEL`。
- **效应边界**：效应是“工单已在企业系统中持久创建”。新请求落盘后 POST 即返回 `fulfilled`，GET 也始终返回 `fulfilled`（演练开关打开时除外）。员工标记的“已处理 / 处理失败”是企业内部的业务结果，不是效应状态：只保存在员工页面和端点自己的存储里，不经 GET 回传 NexLoop。

## 7. “结果暂时未知”演练（只能在本机用管理命令打开）

演练开关只能由端点所在主机上的管理命令写入数据目录中的私有控制文件，任何 HTTP 路由都不能打开它。在容器内执行：

```bash
docker compose -f deploy/stage/reference-endpoint/compose.example.yaml --env-file <PRIVATE_COMPOSE_ENV> exec endpoint python /opt/reference-endpoint/reference_endpoint.py drill --data-dir /var/lib/reference-endpoint --delay-next-posts 1 --delay-seconds 5
```

- `--delay-next-posts N`：接下来 N 个新的交付请求先持久化，再延迟 `--delay-seconds`（2.5–60 秒，默认 5，长于 NexLoop 的 2 秒截止）才应答。NexLoop 记为结果不明，随后应当用 GET 查询并对账，而不是重新提交。
- `--get-unavailable-seconds S`：接下来 S 秒内 GET 一律返回 503，用来检查 NexLoop 在查询不可用时保持结果不明、不判为完成、不重发。
- `--status` 查看当前状态，`--clear` 关闭演练。
- 每次 `unknown_reconciliation` 交互前打开一次；对第三方端点无法这样做时，改用 [`scripts/nx037/unknown_drill_relay.py`](../../../scripts/nx037/unknown_drill_relay.py) 中继。

## 8. 局限

- 员工的处理结果（已处理 / 处理失败及备注）不回传 NexLoop。NexLoop 只知道工单已创建；把处理结果回传（例如作为入站事件）是 v0.1 之后的后续项。
- 工作时间外到达的请求保持“待处理”，端点不提醒、不升级；这由员工的值班安排负责。
- 端点只服务一个企业（一个租户标签），也没有面向消费者的界面。
