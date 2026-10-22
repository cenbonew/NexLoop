# NX-061：EIOS 能力补齐第二批

- **来源**：`docs/design/eios-capability-gap.md` §A.2 第二批。C1（审计哈希链）由 L2 在 NX-059 中完成，本任务不含。调度员 2026-10-22 派工，L7 执行。
- **分支**：`l7/eios-gap-batch2`，基点 main `ab19278d`。三项分别提交：A1、E4、B5+B9。
- **迁移**：临时编号从 0319 起（main 最高为 0318），合并时由调度员重新编号。
- **移植**：ADR-006，逐文件手工移植，记录 provenance；不引入对 NEX-EIOS 的运行时依赖。

## A1：Schema 变更发布前列出受影响的查询、视图和动作

### 做了什么

- **vendor**：`eios/ontology/impact.py`，即上游 `src/eios/ontology/impact.py` 的 `DefinitionDependencyGraph` 与 `build_dependency_graph`，**逐字一致**，登记在 `PROVENANCE.json`。它只依赖 `eios/ontology/definitions.py`，而 vendored 的那份与上游逐字一致。
- **上游测试**：`tests/ontology/test_impact_analysis.py` 移植为 `tests/upstream/test_eios_impact_analysis.py`，登记在新建的 `UPSTREAM_ONTOLOGY_TESTS.json`，并把这份清单加入 `versions.lock.json` 的 `test_provenance`。唯一的改写：上游用的 `eios.composition.schema_contracts.object_type_reference` 没有 vendored，测试内用 vendored 的 `eios.ontology.semantics.schema_contract_digest` 定义了同样的函数。
- **迁移 0319** `authz.nexloop_read_schema_dependents`：只读，守卫与发布 basis（0254）相同，即 `authz.nexloop_assert_review_read`：需要 `ontology.schema.review` 的 EXECUTE、当前的 Human 会话、调用者自己的租户。它返回：
  - 本租户每个 Object Set、Object View 的最新版本；
  - 生效中的 Function 与 Action；
  - 只取定义文本中出现了该类型名的那些（粗筛），每类最多 500 条，超出时 `truncated` 为 true。

  不写任何数据。
- **`nexloop_eios/schema_impact.py`**：
  - 先把这些行用 vendored 的契约解析（JSON 模式，与其它读取方相同），解析失败的放进 `unreadable`，**不静默丢弃**；
  - 然后用 vendored 的图计算类型节点的下游。NexLoop 读取时取类型的最新版本，所以无论定义绑定的是哪个版本都算在内，并报告 `pinned_type_version`；
  - 每个受影响的定义同时标出它依赖了提案所改动的哪些属性：新增的可选属性，或增加词表值的属性，也就是以后 `hide` 会隐藏的那个属性；受属性影响的排在前面。
- **`GET /api/v1/workbench/schema-proposals/{id}/check`** 新增 `impact` 字段，取值来自 `SchemaPublicationPort.dependents()` 与 `schema_impact.impact()`。权限仍由 SQL 判定：operator 拿到 403，其它租户的提案为 404。`/check` 的响应不属于 `packages/contracts` 中的契约，没有改动任何契约。

### 测试

| 命令 | 结果 |
|---|---|
| `pytest tests/upstream/test_eios_impact_analysis.py` | 4 passed |
| `pytest tests/test_schema_impact.py`（新增，不需要 PG） | 3 passed：两种 `change` 形状；任意绑定版本；名称前缀相同的另一个类型（`ConsumerArchive`）不算；解析失败的定义列入 `unreadable`；未知提案返回 None |
| `pytest tests/test_schema_publication_http_pg.py`（新增 `test_check_lists_the_published_definitions_the_proposal_may_affect`） | 真实 PG 与真实工作台登录。发布 2 个 Consumer 的 Object Set、1 个 Store 的 Object Set、1 个 Consumer 的 Object View（都经受治理的发布函数）。`level` 词表提案的 `/check`：列出两个 Consumer Object Set、Consumer Object View 以及夹具里的 Consumer Action；Store 的定义不在其中；`consumer.by_level` 和 `consumer.card` 标出 `changed_properties=['level']`，`consumer.by_sport` 为空列表；绑定版本为 97。新增属性的提案：`affected_by_changed_properties=0`。operator 拿到 403。读取不写任何数据 |
| 同上，首次运行 | **首次失败**：所有定义都落进了 `unreadable`，因为用了 `model_validate`，而 vendored 契约是 strict 的，日期、枚举只能在 JSON 模式下解析。改为 `model_validate_json` 后通过。另外，开发机上 PG 启动需要 `LC_ALL=C.UTF-8` |
| `pytest -n 3 tests/test_bootstrap.py tests/test_schema_publication_http_pg.py tests/test_nx040_schema_publication_pg.py tests/test_object_sets_pg.py tests/test_object_views_pg.py tests/test_schema_impact.py tests/test_vendor_imports.py tests/upstream/test_eios_impact_analysis.py` | 44 passed；fresh catalog 包含 0319 |
| `LC_ALL=C.UTF-8 uv run --frozen python scripts/check_definer_search_path.py` | 0 problems |

### 限制

- 依赖图是**静态**的，来自定义的契约字段，不包括运行时的读写关系（运行时血缘 A7 已推迟）。
- 只列出受影响的定义，不阻止发布。是否把影响清单接进 F4 的发布许可门，需要改动 `evolution-evaluation-report` 契约，留给负责人决定。
- 工作台前端目前还没有调用 `/check`。页面展示另开任务。

## E4：每个接口的请求量、慢请求与错误数（验证“读取 0.5 秒内返回”）

### 做了什么

- **vendor**：`eios/api/metrics.py`，即上游 `src/eios/api/metrics.py` 的 `ApiMetricsRegistry`：按路由模板、方法和状态计数，带延迟直方图，序列数上限 512，标签做转义。有两处改写，都登记在 `PROVENANCE.json`：
  - 增加 **0.5 秒**的直方图桶，上游的桶是 5/25/100/250 ms、1 s、5 s，正好没有 PRD §8 的读取目标；
  - 指标前缀由 `nex_eios_` 改为 `nexloop_`。

  `eios/api/__init__.py` 是 NexLoop 自己的一行包标记，上游的 `eios.api`（它的 FastAPI 应用）没有 vendored。

  上游测试 `tests/api/test_metrics.py` 中的 registry 用例移植为 `tests/upstream/test_eios_api_metrics.py`（登记在 `UPSTREAM_API_TESTS.json`）；另外两个端点用例依赖上游应用，已删除，并在 adaptations 中写明。
- **中间件**（`http_api.create_app`）：每个请求按**路由模板**计数，未匹配的路径记为 `unmatched`，从不使用原始路径，因此标签有界，也不会带出 ID。
  - 同时记入两处：本进程的 registry（按路由的明细），以及 `observability.API`（按类汇总：GET/HEAD 记为 read，其它方法记为 write）。
  - 流式响应计到响应头发出为止；抛出异常的请求记为 500，然后重新抛出。
- **按类的进程样本**（迁移 **0320**，kind `api`，service 只能是 `read`/`write`）：
  - 每分钟写一次，内容是 calls、errors（5xx）、slow（≥500 ms）、p50/p95/max，以及与 guard 相同的对数直方图 `h_<i>`；只接受数字，其它键或其它 service 都被拒绝；
  - API 进程在后端打开后启动采样器，尽力而为，失败不影响请求；
  - 函数体沿用 0277，只加入 `api` 种类。
- **指标快照**（0320，按 0277/0318 的包装模式）新增 `api` 段：
  - 最近 5 分钟的进程汇总；
  - `classes.read` / `classes.write` 各自的 `calls`、`p50_ms`、`p95_ms`（把所有 API 进程的直方图合并后再取分位数）、`error_rate`、`slow_rate`。

  分位数函数 `runtime.nexloop_histogram_quantile` 与 `scripts/load/report.py` 的 `histogram_quantile` 算法一致，测试中用随机直方图逐一核对。现有的回环文本导出（`observability export`）自动带出 `nexloop_api_classes_*{key="read"|"write"}`。
- **按路由明细的可选回环导出**：`--api-metrics-port`，默认关闭，与 NX-030 D1 一致。开启后只在 127.0.0.1 上提供 `GET /internal/api-metrics`，内容是本进程的 registry；每个 API 进程一个端口，不得与 API 自身的端口相同。
- **负载报告**（`scripts/load/report.py`）每个 cell 新增 `api_requests`：按类给出请求数、错误数、慢请求数、合并后的 p50/p95；读请求的 p95 与 500 ms 比较（`within`/`over`/`missing`）。**这只是观测，不是门槛**：§7 不放宽也不新增门槛，是否把它设为门槛由负责人决定。

### 测试

| 命令 | 结果 |
|---|---|
| `pytest tests/upstream/test_eios_api_metrics.py` | 1 passed（上游 registry：序列数上限、标签转义） |
| `pytest tests/test_api_request_metrics.py`（新增，不需要 PG） | 4 passed：读/写分类，错误只算 5xx，404 不算；慢请求在 500 ms 处计入；中间件按模板 `/api/v1/artifacts/{artifact_id}` 计数，原始 ID 和未知路径都不出现在文本中，未知路径记为 `unmatched`；`le="0.5"` 是一个桶边界；回环端口提供明细，其它路径 404，应用关闭后端口也关闭；`--api-metrics-port` 与 `--port` 相同或小于 1024 时拒绝 |
| `pytest tests/test_observability_pg.py -k api_samples`（新增） | 真实 PG：两个“进程”的读样本，快照中 `classes.read` 的 calls=200，p50 和 p95 与 `report.histogram_quantile` 对合并直方图的结果相同（p95 ≥ 500）；slow_rate=0.25，error_rate=0.005；20 个随机直方图在 3 个分位上，SQL 与 Python 结果一致；文本导出带有 `nexloop_api_classes_p95_ms{key="read"…`；service 为 admin、带 `timeouts` 键、值为字符串、出现未知键时都被拒绝 |
| 同上，首次运行 | **首次失败**：测试中把 Python float 直接传给 `nexloop_histogram_quantile(jsonb,numeric)`，类型为 double precision，函数不匹配。测试里加 `::numeric` 后通过；快照内部的调用本来就没有问题 |
| `pytest tests/test_load_report.py`（新增 1 个） | 21 passed |
| `pytest -n 3`：observability、bootstrap、http_api、http_api_execution_profile、api_stage_ready_pg、browser_http、api_request_metrics、guard_op_samples、load_report、vendor_imports、upstream api_metrics、relay_step_timing、schema_publication_http_pg | 107 passed（中间件作用于所有请求，原有用例无回归） |
| `check_definer_search_path.py` | 0 problems |

### 限制与后续

- **告警规则没有改**。读请求 p95 超过 500 ms 是否要告警、阈值和持续时间多少，需要负责人决定；可以在 alert-rules v13 中引用 `api.classes.read.p95_ms`。
- 回环导出默认关闭。stage 的 compose/systemd 没有改，开不开启由调度员决定。
- 采样窗口是 1 分钟，快照取最近 5 分钟。进程刚启动时，最多要等 1 分钟才会出现第一个样本。
