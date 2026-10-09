# NX-021 混合召回：实现与证据（开发线 L2）

状态：实现与定向测试完成，**未标 done**。NX-021 在任务图上依赖 NX-018，任务状态由调度员判定。迁移编号 0066 为临时编号，合并时由调度员重编号（revision 字符串只出现在 `catalog.json`、`versions.lock.json`，测试经 `support.release_metadata.BOOTSTRAP_REVISION` 读取）。

依据：ADR-019 §3.2、§3.6；docs/handoff/docs/03 §6（先结构化过滤再召回、来源版本校验、按 profile 隔离向量）；ADR-011（同库检索，先精确召回再考虑 ANN）。第一步端点实测见 `NX-021-embedding-probe.md`。

## 1. 交付内容

| 文件 | 内容 |
|---|---|
| `packages/eios-core/src/eios/migrations/0066_nx021_recall_index.sql` | 召回索引表、分词函数、授权入口函数 |
| `packages/eios-core/src/nexloop_eios/recall.py` | `OntologyRecall`（召回）、`RecallIndexer`（回流）、`EiosRecallAuthorizer`、`RecallConfig`、`vocabulary_ref` |
| `packages/eios-core/src/nexloop_eios/embedding_provider.py` | `DeterministicTestEmbeddingProvider`（CI 默认）、`ArkMultimodalEmbeddingProvider`（真实，只能由显式 profile 构造） |
| `tests/test_recall_pg.py` | 9 项真实 PG + 真实 EIOS READ 判定测试 |
| `tests/test_embedding_provider.py` | 5 项无网络 provider 测试（假 transport） |
| `tests/verification_real_embedding.py` | 显式 opt-in 的真实端点 + 真实 PG 召回验证（默认 CI 不收集） |

## 2. 设计

### 2.1 索引（派生、可重建层）
- `ontology.nexloop_recall_entries`：每行一段可召回文本，带 `tenant_id`、`world`、`index_kind`（definition / instance）、`ref`、`target_kind`、权限闸门 `gate_type`/`gate_property`、`source_key`/`source_revision`、`tokenizer_version`、`tsv`、`ngrams`。
- `ontology.nexloop_recall_embeddings`：`(entry_id, profile_id)` → `extensions.vector`；触发器保证 `vector_dims = profile.dimension`、非零向量、租户一致。
- `ontology.nexloop_embedding_profiles` / `nexloop_recall_settings`：按租户登记 `model@dimension` 与当前活动 profile。切换必须显式点名被替换的 profile（compare-and-set），否则 `40001`；旧 profile 向量不与新 profile 混用。
- 四张表 owner 为 `nexloop_owner`，启用并强制 RLS（`tenant_boundary`），对应用角色无表权限。
- **索引文本只来自数据库**：定义行由 `ontology.object_type_versions` 最新版本在 SQL 内展开（类型名、显示名、复数显示名、描述；属性名、显示名、描述、所属 property_group；`type_descriptor.enum` 封闭词表值）；实例行由 `ontology.objects` 当前行与类型定义展开（`title_property`/指定名称字段、别名字段、`primary_key`/指定强标识字段）。调用方只提供向量，SQL 校验向量与 SQL 展开出的行一一对应，无法注入外来文本。
- 别名（NX-045/046 回流）：canonical ref 必须是最新定义中存在的类型 / 属性 / 词表值；别名继承其权限闸门。

### 2.2 中文分词（实测驱动）
测试 PG（与默认部署一样 `--no-locale`，C locale）下实测：`show_trgm('关注防水功能')` 返回空集，`to_tsvector('simple', …)` 把整段中文当一个词。因此：
- FTS：`nexloop-cjk-bigram-v1`，中文连续段转为重叠字二元组（单字保留单字），其余走 `simple` 解析器；查询词元 OR 连接，分数为查询词元覆盖率（0–1）。
- trgm 路：`extensions.show_trgm`（拉丁 / 数字三元组）∪ 中文字二元组，同一 Jaccard 相似度；GIN `&&` 预选。只用于名称、显示名、别名、词表值、实例标题，不做描述全文模糊扫描（docs/03 §6）。

### 2.3 三路 + 强标识融合
- 每路（vector / fts / trgm）在预过滤后的行中各取 `candidate_limit`（默认 20）。同一 ref 每路取最高分。
- 融合分 = Σ w_m·s_m / Σ w_m（只计启用的路）；默认权重 vector 0.5、fts 0.2、trgm 0.3；`method` 为贡献最大的那一路。
- 实例召回先做强标识精确匹配：`strong_ids`（键 + 规范化值）或 `verified_refs`（服务端已验证的会话消费者 ref）。命中即 `method=strong_id, score=1.0`，排在最前；之后才是名称 / 别名 / 向量。
- 输出 `RecallHit.contract()` = `{ref, method, score}`，测试用 `candidate-definition.schema.json` 的 `recall` 子 schema 校验。`top_k` 默认 5、`candidate_limit`、权重、`min_score` 均为 `RecallConfig` 配置（`config_version=nx021-recall-v1`）。结果只含 ref、分数、命中的字段类别，**不返回被索引的文本**。
- ref 约定：`eios:object_type:T`、`eios:property:T/p`、`nexloop:vocabulary:T/p/<值，空白与 % 百分号编码>`、`eios:object:T/<object_id>`。

### 2.4 权限与隔离（AT-025 / AT-003 召回部分）
1. **tenant**：所有入口函数第一步 `ontology.nexloop_recall_tenant(digest, world)` 通过 `authz.nexloop_service_identity_snapshot` 由凭据推导 tenant，并检查租户 active、设置 `eios.tenant_id`。调用方无法传 tenant；凭据不允许的 world 直接 `42501 service authentication denied`。
2. **world**：实例行必须 `world = 会话 world`；定义行是租户级 Schema（world 为空），别名行带 world。
3. **属性权限预过滤**：`OntologyRecall` 先取闸门名单（`control.nexloop_recall_gates`，仅名称），对每个闸门做完整 EIOS READ 判定：类型 `eios:object_type:T`，属性定义 `eios:property:T/p`。只有可读闸门进入 SQL，SQL 在算任何分数之前过滤，无权属性的文本不参与向量、FTS、trgm 打分。
4. **来源版本校验**：定义行要求 `source_revision = 最新版本`；实例行要求对象仍存在且 `nexloop_revision` 相同；别名行要求 canonical ref 仍在最新定义中。过期或删除的行直接不可见，等回流重建。
5. **返回校验**：Python 侧逐行核对 tenant、world、闸门、类型范围，任何不符都抛 `PermissionError`（不静默丢弃）。
6. **实例后置校验**：每个实例命中还需当前对象 READ（`eios:object:T/id`）与所有命中字段的属性 READ（`eios:property:T/id/p`，与 `AuthorizedObjectReader` 同一资源约定）。
7. **会话新鲜度**：每批 EIOS 判定前后检查身份快照的 `directory_hash`。会话过期（例如 Schema 发布改变 directory）时抛 `RecallUnavailable`，**不会表现为“无匹配”**，避免下游误生成重复候选。单个资源判定失败（如没有 grant 事实）按拒绝处理。

### 2.5 Embedding 与维度
- `OntologyRecall` / `RecallIndexer` 构造时要求 `provider.dimension == expected_dimension`（来自 `EMBEDDING_DIMENSION`），否则 `EmbeddingDimensionMismatch`。
- SQL 侧向量查询与回流都要求 `profile_id`、维度与租户活动 profile 一致，否则 `22023 embedding profile or dimension mismatch`。**不降级为纯 FTS**。
- 未配置 provider 时是显式的 FTS + trgm 模式，结果 `methods=('fts','trgm')`、`profile_id=None`。
- `ArkMultimodalEmbeddingProvider`：每次请求一条文本（端点会把多个 input 融合为一个向量），显式发送 `dimensions`，校验返回长度。只对传输错误做有界重试，不重试 HTTP 错误。异常信息只含 HTTP 状态和清洗后的错误码，不含 key 或 message。维度未实测（`EMBEDDING_DIMENSION` 为空）或 `text` 模式（未验证）时拒绝构造。

### 2.6 回流接口（供 NX-045/046）
`RecallIndexer` 提供以下方法，SQL 入口仅授予 `nexloop_domain_worker`：
- `activate_profile(replacing=None)`
- `index_object_type(type_name)`：发布后调用
- `index_alias(alias_id, canonical_ref, alias_text)`：合并后调用
- `index_instance(type_name, object_id, spec=None)`：新实例或更新后调用
- `remove_source(source_key)`

召回入口授予 `nexloop_api` 与 `nexloop_domain_worker`。

## 3. 迁移 0066（临时编号）与依赖

- 新建：扩展 `pg_trgm`、`vector`（均 `with schema extensions`，与 0005 的 pgcrypto 做法一致），并对 `nexloop_owner` 授予 `show_trgm`、`vector_dims`、`vector_norm`、`cosine_distance` 的执行权限。
- 新建表：`ontology.nexloop_embedding_profiles`、`nexloop_recall_settings`、`nexloop_recall_entries`、`nexloop_recall_embeddings`。
- 新建函数：
  - `ontology.*`：内部辅助函数，无应用角色权限。
  - `control.nexloop_recall_*`：9 个入口函数。放在 `control` 是因为应用角色只对 `authz`/`control` 有 schema usage。
- 依赖的已有对象：
  - 角色 `nexloop_owner/api/domain_worker`（0002）
  - schema `extensions`（0005）
  - `authz.nexloop_service_identity_snapshot`（0034/0045 版本）
  - `control.nexloop_tenants`
  - `ontology.object_type_versions`（0001）
  - `ontology.objects`，含 `world`（0010）与 `nexloop_revision`（0015）
- 0001..0065 文件与 checksum 未改；`catalog.json` 只追加 0066；`versions.lock.json` 的 bootstrap_revision 由 0065 改为 0066。

### 部署影响（需调度员 / 负责人决定）
- **pgvector 是硬依赖**。本机 Homebrew PostgreSQL 18.4 + pgvector 0.8.5 可用，CI 不受影响。但 `versions.lock.json` 锁定的社区 compose 镜像 `docker.io/library/postgres:18.4-bookworm` 不含 pgvector（`pg_trgm` 在 contrib 内可用），在该镜像上 clean bootstrap 会停在 0066。上线前需要把 compose / 部署镜像换成带 pgvector 0.8.x 的 PG18 镜像，并在 versions.lock 登记 digest。这属于部署配置变更，本线未改。docs/11 中 DATA_HOST 本来就规定 PostgreSQL 18 + pgvector + pg_trgm。
- `CREATE EXTENSION` 需要 bootstrap 超级用户身份，与 0005 相同。
- 首版不建 HNSW/IVFFlat：向量列为无类型 `vector`，维度由 profile + 触发器固定，按 ADR-011 先在预过滤后做精确余弦检索。若之后建 ANN 索引：1024 维可用 `vector`，2048 维需 `halfvec`。

## 4. 实际执行的命令与结果

环境：`source ~/.nvm/nvm.sh && nvm use 24`（Node v24.13.0），`PYTHONPATH=packages/eios-core/src:tests`，uv Python 3.12.10，Homebrew PostgreSQL 18.4，pgvector 0.8.5，pg_trgm 1.6。

1. 端点实测（第一步，scratchpad 脚本，未入库）：
   - 首个请求 200 / 2048 维；第 2 个请求首次失败 TLS `UNEXPECTED_EOF_WHILE_READING`。
   - 加传输错误捕获后完成全部探测；第二轮 27 次尝试中 3 次传输错误，有界重试后成功。详见 probe 报告。
2. `uv run --frozen pytest -q tests/test_bootstrap.py`：
   - **首次 2 passed / 2 errors**：测试 PG 无法启动，postmaster 报 “became multithreaded during startup / Set LC_ALL”。原因是本 shell 未设置任何 `LANG/LC_*`，与代码无关。
   - 加 `LANG=en_US.UTF-8` 后 4 passed，2.05s。之后所有 PG 测试都带该变量。
3. `tests/test_recall_pg.py` 迭代中的失败（均为首次出现，按原样保留记录）：
   - fixture 的 primary_key 属性未设 required（测试数据错误）
   - `permission denied for schema ontology`：入口函数改放 `control`
   - `permission denied for function show_trgm`：参照 0005 授权给 owner
   - 租户 B 的 spec 引用了未声明字段（被函数正确拒绝，测试数据修正）
   - 2 failed：
     - ① “防水达人”在隐藏昵称后仍命中实例。该命中来自可读姓名行的低分向量近邻，断言改为证明隐藏文本对任何路都没有贡献。
     - ② 发布 v2 后召回为空。根因是 Schema 发布使会话 directory 过期，EIOS 判定异常被当作拒绝吞掉。这是**设计缺陷**，已改为 `RecallUnavailable` 加新鲜度检查。
   - 随后 9 failed：未授予资源（无 grant 事实）也以 `AuthorizationUnavailable` 出现，无法按异常类型区分，改为“批前、批后会话新鲜度检查 + 单资源失败即拒绝”。
   - 1 failed：测试中途新建凭据再次改变 directory，属预期的 fail closed，测试改为重新认证。
   - 最终 9 passed，10.76s。
4. 自审发现：无租户的 profile 表会违反 `test_db_boundary` 的“ontology/runtime 全表强制 RLS”不变量，在运行该测试前已改为按租户登记。
5. 最终定向测试：

   ```bash
   LANG=en_US.UTF-8 uv run --frozen pytest -q tests/test_recall_pg.py tests/test_embedding_provider.py tests/test_embedding_profile.py tests/test_bootstrap.py tests/test_db_boundary.py tests/test_doctor.py tests/test_core_sandbox.py tests/test_compose_bootstrap.py tests/test_wheel_install.py --tb=short --junitxml=.ci-results/nx021-recall-final.xml
   ```

   → **61 passed，33.67s**，0 skip。junit sha256 `4addc5fe49c4ec5398eaba3cdaa919c93a2e275e00915b1e0aa57f32f301b92b`。
6. 真实端点 + 真实 PG（显式 opt-in，合成短语）：

   ```bash
   LANG=en_US.UTF-8 EMBEDDING_DIMENSION=1024 NEXLOOP_REAL_EMBEDDING_ENV_FILE=<负责人私有 .env> uv run --frozen pytest -q tests/verification_real_embedding.py -s --junitxml=.ci-results/nx021-real-embedding.xml
   ```

   - **首次 1 failed / 1 passed**：断言 1024 维重复请求逐位一致，实测不成立。已改为余弦断言，并更正 probe 报告。
   - 最终 **2 passed，27.76s**：
     - 重复余弦 0.999142
     - “在意是否防水”的定义召回首位为 `eios:property:Consumer/waterproof_concern`，vector 分 0.7545，融合分 0.540116
     - 敏感属性 `monthly_income` 不出现
     - 期间 1 次 TLS 传输错误被重试吸收
   - junit sha256 `3b055d0eaaf26605f2b9133f429985b2e65fda981b612ed84bd876af1c9db9ef`。这是**真实**证据，其余 PG 测试是**测试证据**（deterministic provider、合成数据）。
7. 进程：测试结束后 `pgrep -fl nexloop-pg` 中没有本线遗留进程。仍在运行的 PG 是并行的 NX-019 线和交接文档记录的 49624 实例。

## 5. 验收结论（建议，由调度员判定）

| AT | 建议 | 依据 |
|---|---|---|
| AT-025 检索隔离 | **passed（召回索引范围，测试证据）** | 见下方说明 1 |
| AT-069 发布回流 | **not_run（召回侧已验证，整体未完成）** | 见下方说明 2 |
| AT-003 属性权限（召回部分） | 召回部分有证据，整体仍 not_run | 见下方说明 3 |

1. AT-025 的依据是 `test_cross_tenant_and_world_isolation` 与 `test_return_check_rejects_foreign_rows`：
   - 租户 B 的同名类型 / 属性 / 同强标识实例对租户 A 不可见；模拟 world 实例对 real 不可见，反之亦然。
   - 凭据不允许的 world 返回 42501；应用角色直接查索引表时权限被拒。
   - 篡改返回行的 tenant 时抛 `PermissionError`。
   - 预过滤在 SQL 内（tenant 由凭据推导），返回校验在 Python 内。
   - 未覆盖：docs/03 的记忆 / 证据检索（`memory_items`、`search_chunks`）尚未实现，不在本实现内。
2. AT-069 召回侧的依据是 `test_reflow_alias_new_definition_and_new_instance`：
   - 别名回流后“防泼水”首位命中 `waterproof_concern`（score > 0.9）。
   - v2 发布并回流后，新属性 `sport_preference` 首位命中。
   - 新实例回流后可召回；对象修订后旧文本不可见，回流后新文本可见。
   - “走自动应用、不生成重复候选”依赖 NX-020（分层匹配）与 NX-045（候选去重），尚未实现。
3. AT-003 的依据是 `test_sensitive_property_invisible_including_vector_route` 与 `test_instance_post_check_requires_current_object_and_property_read`：无属性 READ 时任何路都召回不到，向量分数可证明隐藏文本没有贡献，结果不含文本。详情 / 导出路径不在本任务内。

## 6. 局限与未完成

- §3.2 第三路“该消费者已有属性值作为上下文”未在本模块实现，应由 NX-020 直接用现有 `AuthorizedObjectReader` 读取。
- 别名的真实来源表要到 NX-045 才有。目前 `index_alias` 只校验 canonical ref 有效，别名本身的合法性依赖受治理的 domain worker 调用方。
- 权限判定成本：每次召回为每个闸门、每个实例候选做完整 EIOS 判定，外加批前、批后两次新鲜度查询，没有缓存。规模变大后需要评估。
- 新的资源约定 `eios:object_type:T` 与定义级 `eios:property:T/p` 需要显式 grant；现有授权数据中还没有这类 grant，默认全部不可见（fail closed）。
- 切换 profile 后旧向量保留但不再使用，没有清理任务。没有 ANN 索引，没有批量 / 并发 embedding。
- 中文是字二元组，不做分词；排序参数（权重、`candidate_limit`）未用真实数据校准。
- `text` 模式 embedding 端点未验证，适配器拒绝使用。

## 7. 需要决定的事

1. 负责人在私有 `.env` 填 `EMBEDDING_DIMENSION=1024`，或明确选 2048（需改用 halfvec 索引方案）。
2. 部署 / 社区 compose 的 PostgreSQL 镜像需要换成带 pgvector 的版本（见 §3）。
3. 确认定义级权限资源约定：类型 `eios:object_type:T`，属性定义 `eios:property:T/p`。
