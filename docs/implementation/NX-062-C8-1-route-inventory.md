# NX-062 C8-1：API 路由清单与发布门

- **依据**：设计稿 `docs/design/c8-route-capability-inventory.md` 第 2 版，L6 复审 PASS（73a74a41）。调度员 2026-10-26 派工，范围是：清单与它的 JSON Schema，G-1 到 G-6，以及 `openapi_url=None` 和对应测试。
- **分支**：`l7/nx062-c8`，接在 73a74a41 之后。**没有迁移**。
- **改到 deploy/ 和 scripts/ci/ 下**：新增 `deploy/authorization/api-routes.v1.json` 和 `api-routes.schema.json`；`scripts/ci/stages/build-artifacts.sh` 多了一行检查。合并时按 NX-033 走 ack-policy。

## 做了什么

| 文件 | 内容 |
|---|---|
| `nexloop_eios/http_api.py` | `FastAPI(..., openapi_url=None)`（F-1，Q-a）。只改了这一行，并加了一行注释 |
| `deploy/authorization/api-routes.v1.json` | 93 条路由加 1 个 Mount，逐条写明 realm、处理函数和授权依据；75 个工作台操作；10 项不经 HTTP 的受治理能力；1 个在后端允许表中、却没有路由到达的操作 |
| `deploy/authorization/api-routes.schema.json` | 清单的 JSON Schema（draft 2020-12），放在清单旁边，原因见设计稿 §3.2 |
| `nexloop_eios/api_route_inventory.py` | 加载并校验清单（Schema 加跨字段规则）；完整配置的 `create_app`；枚举路由（Mount 记为一条）；后端允许表（AST）；端口是否存在；处理函数是否绑定到它的端口；`python -m nexloop_eios.api_route_inventory --diff` |
| `scripts/check_web_build.py` | G-6。`scripts/prepare_community_build.py`（发布镜像的构建上下文）改为调用它，原先内联的后缀检查因此只剩这一份；CI 的 `build-artifacts` 阶段在 web 构建之后也调用它 |
| `tests/test_api_route_inventory.py` | G-1 至 G-4、G-6、F-1，不需要 PG |
| `tests/test_api_route_inventory_pg.py` | G-5，真实 PG |

两份测试都会被 `integration-parallel` 阶段的全量 pytest 收进去，所以每次 CI 都会运行，G-5 也在其中（Q-c）。

## 与设计稿第 2 版的差异

- **授权依据改为“端口”**：
  - 设计稿 §3.2 的 kind 中有 `action`（写 Action 资源 ID）和 `workbench_read`（写读取动词）。实现时我逐条看了处理函数，发现能可靠核对的那一层并不是 Action，而是处理函数委派给的**端口**：
    - `backend.workbench:<操作>`、`backend.browser:<操作>`：后端的封闭允许表，`Backend._invoke_workbench` 和 `_invoke_browser` 中的集合字面量；
    - `sql:<函数>`：会话接管状态直接调用 SQL；
    - 可导入的 `nexloop_eios.<模块>.<类>.<方法>`，例如 `CommercialObserver.records`、`SchemaPublicationPorts.publish`。
  - Action 授权在这些端口下面由 SQL 检查，一个端口可能检查多个 Action。如果在清单里写 Action ID，只是抄一遍代码，而且没法核对。
  - 所以 kind 改为 `port`；`service_bearer`、`webhook_signature` 也各带一个 port。
  - G-3 核对两件事：
    - 端口存在：在允许表中，并且由对应的服务类提供；或者 SQL 函数由某个迁移定义；或者可以导入并调用；
    - **处理函数绑定到这个端口**：处理函数，或者它直接调用的同模块函数，以标识符、属性或整词字符串的形式提到端口名。
  - 后端两张允许表与路由之间也做了双向核对：允许表中的每个操作，要么有路由到达，要么列在 `port_operations_not_on_http`。
- **G-6 依据的构建清单**（L6 建议，复审时要求写明）：用**固定的允许后缀表** `{.html, .js, .css, .png}`，不用 Vite manifest。
  - 理由：这次构建没有开启 `build.manifest`，没有 manifest 可比；而这张后缀表就是发布镜像构建上下文原本使用的规则（`prepare_community_build.py`），现在两处共用一份。
  - 另外还检查：没有 `*.map`；`.js`、`.css`、`.html` 中没有 `sourceMappingURL` 注释（`.html` 由 L6 S-1 加入）；没有点文件和符号链接；顶层有 `index.html`。
- **工作台页面与 Mount**：kind 为 `static`（`source: web_root`），realm 为 `public_static`；Mount 的 handler 写作 `static`。这与设计稿一致，只是把写法定了下来。

## 枚举结果（完整配置，关闭 OpenAPI 之后）

| realm | 条数 |
|---|---|
| `workbench_session` | 59（含工作台读取 45、写入分发 1、审核 4、Schema 发布 5、导出 1、会话 3） |
| `consumer_session` | 22 |
| `pre_session` | 6 |
| `public_static` | 3（两条页面路由和 `MOUNT /`） |
| `public` | 2（健康检查） |
| `service_bearer` | 1（Artifact 读取，world `test`） |
| `webhook_signature` | 1 |
| **合计** | **94**：93 条路由加 1 个 Mount |

## 测试

| 命令 | 结果 |
|---|---|
| `pytest tests/test_api_route_inventory.py` | 9 passed |
| 第一次运行 | 1 failed：G-3 发现我写的端口 `CommercialObserver.entries` 不存在，处理函数实际调用的是 `cost_entries`。这是门本身在起作用：清单改正后通过。另外，处理函数绑定检查原来只认完全相等的字符串，SQL 函数名嵌在 `select …(…)` 里，于是改为按整词匹配字符串 |
| `pytest tests/test_api_route_inventory_pg.py` | 1 passed，探测了 83 条路由（94 条减去 public、public_static、pre_session 共 11 条），全部返回 401 或 403；Artifact 读取不带令牌和带无效令牌都是 401；webhook 用一个已配置的连接器、不带签名，得到 401 |
| 同上，第一次运行 | 1 failed：9 个 POST 用空正文得到 422，webhook 用未配置的连接器得到 404。这些回答来自输入检查，不是凭据检查。改为按各处理函数接受的形状构造正文和 Idempotency-Key，并配置一个连接器之后，全部是 401/403 |
| 变异：把 `/health/live` 改成 consumer_session | G-2 与 G-5 都失败，已恢复 |

## 调查中发现、另行报告的事项（C8 不修）

- **F-2**：Artifact 读取写死了 `world='test'`，并且对外可达。已按调度员选定的方案 1 修复，见下一节。
- **7 项受治理能力没有生产调用方**：
  - 能力：`commitment.bind_commercial`、`consumer.export`、`cost.record`、`goals.agent.propose`、`message.erase`、`retention.hold`、`retention.release_hold`；
  - 它们都登记在注册表里，只经 `GoalGovernedActions` 的 Python 方法调用，而这些方法在 packages、scripts、apps 中，除测试外都没有调用方。清单的 `capabilities_not_on_http` 如实写明了这一点；
  - 另外 3 项能力由部署工具调用：`goal_setup`、`real_tenant_init`。
- **`backend.browser:link_authenticated_identity`**：在浏览器允许表中（0061，消费者身份关联），但没有任何路由调用它。

## 维护

- 新增、删除或修改路由，在同一提交中修改清单并把 `manifest_version` 加 1；`--diff` 只打印差异，不写文件。
- 改不检查 cookie 会话的 realm 集合时，要同时改 `tests/test_api_route_inventory.py` 中的 `FIXED`。

## F-2 修复：test world 的 Artifact 读取只在 `--mode test` 下挂载（调度员 2026-10-26，方案 1）

- **`http_api.create_app`**：`GET /api/v1/artifacts/{artifact_id}` 只在 `config.mode == 'test'` 时注册。stage 以及以后的任何其他模式都不挂载，请求落到 404（`not_found`），不会做任何认证。社区镜像的冒烟检查跑在 test 模式下，不受影响。
- **清单 v2**：
  - 这一条加上 `modes: ["test"]`；没有 `modes` 的路由在每种模式下都挂载；
  - Schema 增加可选的 `modes` 字段。
- **检查按模式区分**：
  - G-1 对 test 和 stage 两种完整配置分别比对：test 为 94 项，stage 为 93 项；
  - G-2 的写死集合也按模式区分：stage 下 `service_bearer` 为空集；
  - G-5 的工作台夹具是 test 模式，照旧探测这条路由；
  - 新增测试：stage 模式下请求这条路由，返回 404 `not_found`；
  - 漏挂检查用 test 模式，因为只有在 test 模式下，每一处路由注册都会执行。
- **stage 上那个 test world 凭据**（`nexloop-service-evolution-evaluation-key`，活动状态，requested_scopes 为 `nexloop.evolution`）：
  - **结论：经这条路由读不到任何 Artifact**；
  - 读取要先拿到 `eios:artifact:local_test` 的 READ 许可（`PostgresArtifactRepository.issue_permit`），这需要该主体在这个资源上的授权事实，以及对应的作用域事实；
  - 这个凭据由 `scripts/deploy/evolution_enroll.py` 登记，清单中 `grants: []`，作用域只有 `nexloop.evolution`；登记工具的自检 `no_effect_authority` 还要求这些主体没有任何 grants、scope、policies、controls 事实；
  - 所以许可在查找 Artifact 之前就失败关闭；路由把所有读取失败都映射为 404，与未知 id 的回答相同，不能用来探测 Artifact 是否存在；
  - 证据：`tests/test_evolution_enroll_pg.py::test_the_test_world_evaluation_credential_gets_no_artifact_read`。用真实 PG 和真实的登记工具登记之后，这个凭据能在 test world 通过认证，但许可失败关闭（缺授权事实），并且库中没有该主体的任何 grants、scope、policies、controls 事实；
  - 因此，在修复上线之前，没有经这条路由的实际数据暴露面；
  - 残余情况：持有这个令牌的人请求时，会得到 404 而不是 401，可以借此判断令牌有效，但前提是已经拿到了令牌。另外，每次请求都会触发一次认证查询。方案 1 上线后，这两点都不存在了。

## L6 对 103d4fb8 的小复核（追加提交）

- **S-4**：`ApiConfiguration.mode` 不再有默认值，改为必填的关键字参数，并且只接受 `http_api.MODES` 中的值（`__post_init__`）。所有调用方都显式传入：
  - `container_entrypoint.py`、`scripts/dev/restore_drill_serve.py` 传 `mode='test'`；
  - 各测试和测试夹具也传 `mode='test'`，包括 `test_web_chat_frontend.py` 中子进程脚本字符串里的那一处；
  - `http_api.parse` 本来就传 `mode=a.mode`。
- **S-6**：`http_api.MODES` 是唯一的一份模式列表。`--mode` 的 choices 由它生成，`api_route_inventory` 也直接导入它。新增测试 `test_the_modes_are_one_list_in_the_api_the_schema_and_the_cli`，断言它与 Schema 的 `modes` 枚举、CLI 的 choices 一致，并断言配置没有默认模式、拒绝未知模式。
- **S-7**：清单内容升到 `manifest_version` 2，但 `schema_version` 有意保持 `nexloop-api-routes/1`。新增的 `modes` 是可选字段：没有 `modes` 的路由在每种模式下都挂载，按 /1 写成的旧清单仍然有效、含义不变。只有不向后兼容的格式变化，才需要升到 /2。
- **S-5（已做）**：G-5 改为按模式参数化。用工作台夹具自己的配置，分别以 test 和 stage 模式构造应用，各自探测本模式挂载的全部路由：test 探测 83 条，stage 探测 82 条，因为 stage 下没有 Artifact 读取。

