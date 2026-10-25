# C8：路由与能力的资源清单发布门（NX-062 第一步，设计稿第 1 版）

- **来源**：负责人 2026-10-22 Q3 按推荐 A：企业自有 Agent / 程序接入（API key、MCP、网关、SDK，差距分析中的 F1–F3）定为方向，放在 S7 之后；**先做“每个接口都登记了权限”的发布检查**，即差距分析第二轮中的 C8（`docs/design/eios-capability-gap.md` 第二轮 §C8、§Q3，分支 `claude/l7-eios-gap-288226` 提交 6885a4fa）。调度员 2026-10-26 派工：先写设计稿交 L6，不写代码。
- **分支**：`l7/nx062-c8`，基点 main 4426c5d0。
- **本稿只描述 C8**。F1 的接入通道需要威胁评审和新 ADR，不在本稿范围；本稿只说明 C8 给 F1 留的接口（§8）。
- 文中的数字都是在 4426c5d0 上实际枚举的结果（§2.1 的方法）。

## 0. 一页结论

- **要解决的问题**：`nexloop-api` 现在有 93 个（方法，路径）对外可达（经 Gateway，除 `/internal/*` 外全部转发）。每条路由的权限检查写在各自的处理函数和它调用的端口里（会话检查在 Python，Action 授权在 SQL），但**没有一份清单**说明每条路由由谁、按什么授权；新增路由时，漏了会话检查或授权，没有任何检查会失败。
- **做法**：
  1. 一份受审的清单 `deploy/authorization/api-routes.v1.json`，每条路由一行：所属领域（public、消费者会话、工作台会话、webhook 签名）和授权依据（哪个 Action、哪个读取动词、经哪个工作台操作分发）。
  2. 一组 CI 检查（本地 CI 权威，`unit-contract` 阶段），任何一项不满足，发布失败：
     - **G-1** 应用实际挂载的路由与清单**完全一致**，双向；
     - **G-2** 不需要会话的路由是一份**精确的白名单**；
     - **G-3** 清单引用的每个 Action、读取动词、工作台操作都真实存在；
     - **G-4** 每个受治理的人类能力，要么能从某条路由到达，要么在清单中写明“不经 HTTP”及理由；
     - **G-5** 真实 PG 上逐条打一遍：不带会话访问每条非公开路由，都必须被拒绝（401/403），不能是 2xx。
- **上游**：`src/eios/authz/inventory.py` 的思路（路由集合与清单精确相等、public 来源精确白名单、不自动绑定未审的新路由）照搬；它的一对一“路由 ↔ api_operation 资源”结构不搬，因为 NexLoop 的授权以 Action 为单位，多条路由共用一个 Action（例如 56 条工作台读取都经 `nexloop.workbench.read:1`）。按语义重写，不 vendor，来源写在模块文档字符串中。
- **调查中发现的一处问题**：`/openapi.json`（GET、HEAD）经 Gateway 对外可达。`create_app` 关掉了 `docs_url`、`redoc_url`，但没有关 `openapi_url`。建议在 C8-1 中关掉（§7 F-1）。
- **不需要迁移**。只读的 PG 检查用测试夹具完成。
- **切片**：C8-1 清单与静态检查（G-1 至 G-4，含 F-1），约 2 天；C8-2 无会话拒绝探测（G-5），约 1 天。

## 1. 范围

| 面 | 本稿 | 说明 |
|---|---|---|
| `nexloop-api` 进程的全部 HTTP 路由（经 Gateway 对外） | **在范围内** | 浏览器消费者、工作台、webhook、健康检查 |
| 工作台写入口 `POST /api/v1/workbench/actions/{operation}` 分发的 75 个操作 | **在范围内** | 一条路由背后是 75 个能力，单列为“操作清单”（§3.3） |
| 受治理能力注册表（`control.nexloop_governed_capabilities`，85 项） | **在范围内** | 与路由、操作交叉核对（G-4） |
| 服务主体的授权（service-grants、stage-principals） | 不改 | 已有清单和 `--doctor`；C8 只引用 |
| Agent Host 控制面（TypeScript，回环 + 客户端证书）、Gateway 本身、回环导出（`/internal/*`、指标导出、`--api-metrics-port`） | 不在本稿 | 都不对外：Caddy 对 `/internal/*` 直接 404，其余只监听 127.0.0.1。§9 列为后续 |
| 消费者通道 CLI、MCP（`consumer_mcp.py`） | 不在本稿 | 它们是 WebChat HTTP API 的客户端，不新增服务端路由，经 G-1 自动覆盖 |

## 2. 现状（4426c5d0）

### 2.1 路由枚举方法

用完整配置（消费者浏览器领域和工作台领域都开启）构造 `http_api.create_app`，递归展开 `app.routes`（含 `include_router` 的子路由和前缀），得到（方法，路径模板）对。这正是 G-1 要用的方法；它不连库，不启动服务。

| 类别 | 条数 | 例 |
|---|---|---|
| 健康检查 | 2 | `GET /health/live`、`GET /health/ready` |
| OpenAPI | 2 | `GET`、`HEAD /openapi.json`（§7 F-1） |
| 消费者领域登录 | 4 | `POST /api/v1/auth/login`、`csrf`、`logout`，`GET /api/v1/auth/session` |
| 消费者领域及共用读取 | 21 | 会话与消息、回执、Run、审核候选、上下文清单、成本、指标、商业观察、Artifact |
| webhook | 1 | `POST /api/v1/webhooks/commercial/{connector_id}`（连接器签名） |
| 工作台登录与账号自助 | 7 | `login`、`csrf`、`logout`、`session`、`invitation/accept`、`password/reset`、`reset-history` |
| 工作台读取与写入 | 56 | 51 个 GET：经 `nexloop.workbench.read:1` 与它的 20 个读取动词，以及审核、Schema 提案、导出等专用读取。5 个 POST：`actions/{operation}`、审核决定、Schema 提案的 publish 与 hide、迁移批次 |
| **合计** | **93** | 74 GET、1 HEAD、18 POST |

### 2.2 授权今天在哪里检查

- **会话**：每个路由器在处理函数前检查 Host/Origin、cookie 会话（`BrowserSessionService.inspect`）和 CSRF（写入）。消费者领域与工作台领域是两个应用 ID、两个 cookie。
- **Action**：会话通过后，处理函数调用端口，端口以用户会话签名请求，SQL 检查 `eios:action:…` 的 EXECUTE 和角色（例如 `authz.nexloop_workbench_read`、各受治理入口）。
- **工作台写入**：`OPERATIONS`（`workbench_actions_http.py`，75 个）→ 受治理入口按 `operation` 查注册表 → 能力 → Action。注册表本身已有精确性检查（`test_catalog_manifests_pg`：迁移目录中的静态清单 = 引导后的表），角色授予在 `workbench-roles.v1.json`。
- **缺的是路由这一层**：没有任何检查保证“每条路由都经过会话检查，并落到一个已登记的授权依据上”。现有测试逐路由写，新增路由时不会被提醒。

### 2.3 能力与操作的对照（G-4 的起点）

在 4426c5d0 上：

- `OPERATIONS` 的 75 个操作都在注册表中，没有悬空的操作；
- 注册表 85 项中有 10 项不经工作台写入口：`approve_metric`、`publish_goal`、`propose_agent_goal`、`set_budget`、`bind_commitment_commercial`、`record_cost`、`erase_message`、`export_consumer`、`hold_retention`、`release_retention_hold`。
  - 它们可能经别的路由（例如导出可能经 `data_export_http`），或者只经部署工具（business/catalog setup）、Agent 工具调用。逐项核实是 C8-1 的工作。
  - 今天这一点没有地方写明；C8 要求逐项写明到达路径或“不经 HTTP”的理由（G-4）。

## 3. 清单

### 3.1 文件与形状

`deploy/authorization/api-routes.v1.json`，与其他授权清单放在一起，同样受审、公开、版本化：

```json
{
  "schema_version": "nexloop-api-routes/1",
  "manifest_version": 1,
  "decision": "…",
  "routes": [
    {"method": "GET", "path": "/api/v1/workbench/overview", "realm": "workbench_session",
     "authorization": {"kind": "workbench_read", "verbs": ["overview"]},
     "purpose": "…", "source_task": "NX-030"}
  ],
  "operations": [
    {"operation": "set_control", "capability": "goals.control.set", "source_task": "NX-022"}
  ],
  "capabilities_not_on_http": [
    {"capability": "goals.budget.set", "reached_by": "scripts/deploy/business_setup.py", "reason": "…"}
  ],
  "public": [["GET", "/health/live"], ["GET", "/health/ready"]]
}
```

### 3.2 `realm` 与 `authorization.kind`

| realm | 含义 | 允许的 kind |
|---|---|---|
| `public` | 不需要会话 | `public`（只能出现在 `public` 白名单中，G-2） |
| `consumer_session` | 消费者领域 cookie 会话 | `action`、`session_only` |
| `workbench_session` | 工作台领域 cookie 会话 | `action`、`workbench_read`、`workbench_operation`、`session_only` |
| `webhook_signature` | 连接器签名，无会话 | `action`（签名通过后以连接器服务身份执行的 Action） |
| `pre_session` | 建立会话之前的入口：登录、CSRF、接受邀请、重置口令 | `pre_session`，并写明限速与一次性凭据的依据 |

| kind | 字段 | 核对（G-3） |
|---|---|---|
| `action` | `resource_id`（`eios:action:<name>:<version>`） | 在业务 Action 清单、系统目录名或迁移登记的 Action 中存在 |
| `workbench_read` | `verbs` | 每个都在 `workbench_reads.VERBS` 中，且 SQL 端接受（G-5 一并验证） |
| `workbench_operation` | 无（见 `operations`） | `operations` 与 `OPERATIONS` 完全相等 |
| `session_only` | `reason` | 只读本人会话信息（例如 `GET …/auth/session`），不读业务数据；数量受 L6 审 |
| `pre_session` | `reason` | 只能是登录、CSRF、邀请接受、口令重置及其历史；不得读业务数据 |

### 3.3 工作台操作清单

`POST /api/v1/workbench/actions/{operation}` 一条路由背后有 75 个能力，单靠路由清单看不见。所以清单另有 `operations`：每个操作对应一个能力，与 `OPERATIONS` 和注册表三方一致。新增工作台操作时，必须同时登记在这里。

## 4. 检查（发布门）

全部放在本地 CI 的 `unit-contract` 阶段（G-1 至 G-4，不需要 PG）和 PG 阶段（G-5），失败即阻止发布。发布前的 release 预检引用同一组测试，不另写一份。

| # | 检查 | 失败的例子 |
|---|---|---|
| **G-1** | 完整配置下的应用路由集合 = 清单 `routes` 的（方法，路径）集合，双向 | 新增路由没登记；路由删了清单没删；路径模板改名 |
| **G-2** | `realm` 为 `public` 的路由集合 = `public` 白名单，白名单本身由测试写死（当前为两个健康检查）。`webhook_signature`、`pre_session` 各自也有写死的精确集合 | 新增一条不检查会话的路由；把业务读取标成 public |
| **G-3** | 每个 `action` 资源存在；每个 `workbench_read` 动词在 `VERBS` 中；`operations` 与 `OPERATIONS` 完全相等，且每个能力都在注册表中（`catalog_manifests.governed_capabilities()`） | 引用了不存在或已改版本的 Action；新加读取动词没登记 |
| **G-4** | 注册表中的每个人类能力，要么出现在 `operations` 或某条路由的 `action` 中，要么出现在 `capabilities_not_on_http` 中（写明到达路径和理由）；三者不重叠 | 新增能力既没有入口也没有说明；一个能力同时声称“经 HTTP”和“不经 HTTP” |
| **G-5** | 真实 PG（工作台夹具）上，对每条非 `public`、非 `pre_session` 的路由，按路径模板填合法形状的占位参数，不带 cookie 请求一次：状态码必须是 401 或 403。webhook 路由不带签名请求：必须被拒绝。只断言“不是 2xx 且不是 5xx”和具体拒绝码，不断言正文 | 处理函数漏了会话检查；会话检查放在了数据库读取之后 |

- **G-1 的“完整配置”**：浏览器领域和工作台领域都开启，所有可选路由器都挂载。将来新增按配置挂载的路由器，必须加进这个“完整配置”，否则 G-1 看不见它。测试里同时断言“完整配置挂载的路由器数等于 `create_app` 中 `include_router` 的调用数”，防止漏挂。
- **G-5 的占位参数**：按路径参数的形状生成（UUID、64 位十六进制、整数），不用真实 ID。它不验证“有会话但无权限”的情况，那由各功能自己的测试负责；G-5 只保证“没有会话就进不去”。
- **为什么 G-5 用真实 PG**：工作台会话检查要读身份库。没有库时请求会以 503 失败，看起来也像被拒绝，会把漏检藏起来。用真实 PG 才能得到真实的 401。

## 5. 上游的对照（只读）

| 上游 | 取 | 不取 |
|---|---|---|
| `src/eios/authz/inventory.py` `AuthorizationResourceInventory` | 来源集合与清单精确相等（`require_exact_sources`）；public 来源是精确白名单；清单不可变；未审的新路由不自动绑定 | 路由与资源一对一（`reverse` 映射）：NexLoop 的授权单位是 Action，多条路由共用；`api_operation` 资源 ID（`METHOD/path`）作为授权资源（§8 留给 F1） |
| 同文件 `_CORE_ROUTE_KEYS`、`_CORE_CAPABILITY_KEYS` | 不取 | 全是上游路由和领域能力（含网球领域码） |
| `src/eios/authz/enforcement_inventory.py`（原子变更契约） | 不取 | 描述上游自己的表与执行者；NexLoop 的对应物是受治理入口与复制件守卫 |
| `tests/authz/test_resource_inventory.py` | 用例思路：路由集合相等、不自动绑定、public 精确、清单不可被篡改 | 具体用例（依赖上游的应用与 packs） |

按语义重写为 `nexloop_eios/api_route_inventory.py`（加载、校验清单，枚举路由），来源写在模块文档字符串中，做法与 B5 的 `run_outcome_taxonomy` 相同；不进 `PROVENANCE.json`，不 vendor。

## 6. 维护规则

- 新增、删除或修改路由，在同一个提交中修改清单，`manifest_version` 加 1，`decision` 写明原因；L6 复核时逐行看 `realm` 与 `authorization`。
- 清单不从代码自动生成。自动生成会把“漏了授权”的路由也照抄进清单，失去审查的意义。可以提供一个只打印差异的辅助命令（`python -m nexloop_eios.api_route_inventory --diff`），只给提示，不写文件。
- 改 `public`、`pre_session`、`webhook_signature` 的集合，除清单外还要改测试中写死的集合，即需要两处同时修改。

## 7. 调查中发现的问题

- **F-1 `/openapi.json` 对外可达**：
  - `create_app` 设置了 `docs_url=None`、`redoc_url=None`，可见本意是不对外提供文档；但 `openapi_url` 仍是默认值，GET、HEAD `/openapi.json` 返回完整的路由与模型。
  - Gateway（`deploy/stage/config/Caddyfile.template`）只拦 `/internal/*`，其余全部转发，所以经 stage 的公开地址可以拿到。
  - 风险低：仓库是公开的，模型本来就能从源码得到；但它暴露的是**这台部署实际开启的**路由与功能组合，没有理由公开。
  - **建议**：C8-1 中设 `openapi_url=None`。现有用法都不经这条路由，不受影响：`tests/test_commercial_observe_pg.py` 直接调用 `app.openapi()`；`scripts/generate_contracts.py` 从契约的 JSON Schema 生成 `openapi-components.json`，不经应用。关掉之后，G-2 的公开白名单就只有两个健康检查。
  - 如果 L6 认为应保留，就把它作为 `public` 登记，并在 `decision` 中写明理由。

## 8. 给 F1 留的接口

F1（API key、MCP、网关、SDK）将新增“机器凭据”领域。C8 让它必须从清单开始：

- 新增 `realm: machine_key`（F1 设计时定义），只能用于清单中逐条声明的路由，G-2 的精确集合同样适用；
- F1 的 API key 作用域可以按路由的 `api_operation` 标识（`eios:api_operation:<METHOD>/<path>`，与上游 `core_route_resource_id` 的形式相同）声明。C8 的清单可以先为每条路由算出这个标识（只计算，不作为授权资源），F1 再决定是否采用；
- 现有 MCP 只有消费者通道的客户端（`consumer_mcp.py`，stdio，走 WebChat HTTP API），不受影响。

## 9. 不做的事与后续

- Agent Host 控制面、回环导出：不对外，C8 不覆盖。后续可以给 Agent Host 做同样的清单（TypeScript 侧），另行立项。
- “有会话但无权限”的拒绝：各功能测试负责，C8 不重复。
- 不改任何授权语义、不改契约、不加迁移。

## 10. 切片与估计

| 切片 | 内容 | 估计 |
|---|---|---|
| **C8-1** | `api-routes.v1.json`（93 条路由逐条审定 realm 与授权依据、75 个操作、10 项不经 HTTP 的能力）；`api_route_inventory.py`；G-1 至 G-4；F-1 关闭 `openapi_url`；CI 接线 | 2 天 |
| **C8-2** | G-5 无会话拒绝探测（真实 PG，工作台与消费者两个领域、webhook 签名） | 1 天 |

C8-1 的大部分时间在逐条审定 93 条路由的授权依据。审定中如果发现某条路由的实际检查与清单应写的不一致，比如缺少会话检查，单独报告，不在 C8 中顺手修。

## 11. 需要 L6 与负责人确认的问题

| # | 问题 | 推荐 | 由谁定 |
|---|---|---|---|
| Q-a | `/openapi.json` 关闭还是登记为 public | 关闭（F-1） | L6 |
| Q-b | 清单用 JSON 文件（deploy/authorization）还是 Python 常量（上游做法） | JSON 文件：与其他授权清单一致，受审、公开，部署侧也能读取 | L6 |
| Q-c | G-5 是否进入每次 CI，还是只在发布前跑 | 每次 CI 的 PG 阶段：约 90 个请求，耗时在秒级 | L6 |
| Q-d | Agent Host 控制面的清单何时做 | F1 设计时一并考虑，C8 不做 | 负责人（随 F1） |
