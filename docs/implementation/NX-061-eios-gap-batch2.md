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
