# NX-037 缺口 C：按 Action 路由 provider、NexLoop 投递服务、效应 worker 防火墙

- **分支**：`l3/nx037-provider-routing`，从 main `ef6c7fe` 切出。
- **依据**：`NX-037-readiness-gaps.md` §0 第 2、3 项与 §1。
- **不在本次范围**（归 L2，本次未改）：Action 定义、分类 v3、Agent 工具。测试中用常量引用新 Action 名 `nexloop.enterprise.deliver`。

## 1. 选择：单 worker 内路由表

**不采用的方案**：按 Action 过滤、为每个渠道各开一个效应 worker。不采用的原因：
- 认领（`claim`）在 SQL 中不区分 Action，按 Action 过滤需要新迁移改认领函数；
- 两个 worker 用同一个执行主体会互相认领对方的意图；
- 需要第二份执行凭据、第二个连接池（连接预算已到 57/60）。

**采用的方案**：一个效应 worker，持有一张路由表。

- **路由依据**：
  - 首次派发：认领结果中由 PG 给出的 Action 稳定名（`action_claim.key.action_stable_name`，`effect_execution.claim_effect` 现在把它作为 `action` 返回）。路由依据只来自受保护的账本，不来自请求参数。
  - 对账：查询认领没有新的预留，所以认领中不带 Action。对账时依靠 0044 的绑定：每个意图在派发时绑定到该 provider 的 profile digest，账本只为这个 digest 授权查询。worker 逐个尝试已配置的 provider，被账本接受的那个就是当初发送的 provider。
- **语义不变**：结果未知时，先用原 provider 查询对账，从不换 provider 或重发。
- **未配置路由的 Action**：不准入、不查询、不发送，返回状态 `unrouted`。租约到期后意图仍在队列里，配置补上之后才会派发。没有默认 provider。
- **ProviderRoutes 的约束**：
  - 至少一条路由；
  - 两条路由不能共用 origin、凭据文件或 profile digest，因此一个渠道的凭据不会被另一个渠道使用；
  - 每个 provider 的超时 ≤ 租约的 1/3。
- **CLI**：`nexloop-effect-worker` 新增 `--provider-routes-file`，与原 `--provider-config-file` 互斥且必须二选一。
  - 原参数保留，用于单渠道部署和现有测试，语义不变。
  - 路由文件格式：
    ```json
    {"schema": "nexloop-effect-provider-routes/1", "routes": {"<Action 稳定名>": "<provider 配置文件绝对路径>"}}
    ```
  - 每个 provider 配置文件的格式与 `--provider-config-file` 相同，同样按私有文件读取；每个 tick 重新读取。

## 2. stage Compose

- **action-worker**：离开应用命名空间，改在自己的网络 `effect_egress` 上（子网 `EFFECT_EGRESS_CIDR`）。
  - 改用 `--provider-routes-file`；
  - 挂载路由文件，以及每条路由的配置、Bearer 凭据、CA：`action_worker_webchat_provider*`、`action_worker_enterprise_provider*`。
  - **企业凭据只在这里**。
- **webchat-delivery（新增）**：`nexloop_eios.local_json_delivery`，是 `nexloop.service.request` 的 NexLoop 自有投递服务，协议与 HttpEffectProvider 相同。
  - 网络：共享 action-worker 的命名空间（`network_mode: service:action-worker`），只在回环上监听 `WEBCHAT_DELIVERY_PORT`。
  - 身份：以同一个执行主体认证（0051 要求），用它自己目录下的那份凭据副本。
  - 文件：TLS 证书、私钥和 provider 凭据都在 `secrets/webchat-delivery/` 下。
  - 连接池：`--pool-max 2`，为此新增 CLI 参数，默认 2。
- **连接预算**：`connection-budget.v1.json` 增加 `webchat-delivery`（1 进程，池 2），总量 57 → 59（上限 60）；`connection_budget.BASE_SERVICES` 把它列为必需服务。
- **私有文件的生成**：
  - 路由文件中的路径写容器内的 `/run/secrets/...`；
  - webchat 路由：origin 为 `https://127.0.0.1:<WEBCHAT_DELIVERY_PORT>`，CA 为投递服务的证书，凭据与投递服务的 provider 凭据是同一个值，但分属两个服务各自的文件；
  - enterprise 路由：origin 为企业端点，CA 与凭据由企业提供。

## 3. 防火墙模板

- **新增参数**（`stage_render.PARAMETERS` 白名单，并加入 `stage.env.example`，示例值都取自文档保留地址段）：
  - `EFFECT_EGRESS_CIDR`（CIDR）；
  - `ENTERPRISE_ENDPOINT_ADDR`（IP 地址）；
  - `ENTERPRISE_ENDPOINT_PORT`（远端端口 1–65535，允许 443）。本机服务端口仍要求 1024 以上。
- **`app-host.nft.template` 的 forward 链**：对效应 worker 命名空间依次是：
  1. 模型地址 drop；
  2. DATA_HOST 的 PostgreSQL accept；
  3. 企业端点 `ENTERPRISE_ENDPOINT_ADDR:ENTERPRISE_ENDPOINT_PORT` accept；
  4. 其余新连接 drop。
- **其他命名空间不变**：应用命名空间仍然到不了企业端点，也不放行 Valkey。

## 4. 测试

| 文件 | 内容 |
|---|---|
| `tests/test_effect_provider_routing.py`（新增，5 个） | 两个真实 HttpEffectProvider 分别连两台合成 provider 服务器：两个 Action 各自送到自己的 provider，另一台服务器没有收到任何请求；未配置的 Action（另一个 Action 名或无 Action）不准入、不发送；未知结果用发送它的 provider 查询，绑定不到任何 provider 时不查询；路由拒绝共用 origin、共用凭据和过长超时；路由文件格式严格，CLI 参数二选一 |
| `tests/test_deploy_render.py`（新增 1 个） | 三个参数的校验（企业端口 443 通过、PG 端口 443 仍拒绝、地址必须是 IP）；渲染出的效应 worker 规则恰好四条且顺序正确；应用命名空间规则不含企业端点；Compose 与 stage env 示例的 CIDR 一致 |
| `tests/test_stage_compose.py`、`tests/test_deploy_render.py`、`tests/test_connection_budget.py`（更新） | 两个服务的命名空间例外；每条路由的文件只属于 action-worker，投递服务不持有企业凭据；投递服务命令经其真实解析器校验；预算 59 |

## 5. 留给后续的事项

- 企业端点的地址、端口、CA、凭据和 Caddy 侧 SNI 由负责人提供（readiness §8），配置只放私有文件。
- `nexloop.enterprise.deliver` 的定义、分类与授权由 L2 完成后，才会有意图走企业路由。
- worker 遇到 `unrouted` 时只返回状态，没有告警规则。是否加告警由调度员决定。
