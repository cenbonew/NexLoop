# NX-052 租户 ID 规范化：实施说明

依据：`docs/implementation/tenant-id-audit.md`（盘点与方案）、`planning/tasks.json` NX-052 deliverable。目标是让 tenant_id 统一为“小写规范 uuid 文本”，列类型仍为 `text`。本说明的迁移号 0230、0231 是临时号，合入时由调度员重新编号。

## 0. 设计

### 0.1 分批计划

分批的依据是两点：表的写入热度，以及外键检查落在哪条事务路径上。冷表先做；热点表单独成批，并且先测量再决定。

| 批次 | 内容 | 写入热度 | 状态 |
|---|---|---|---|
| 1a | Claim 契约 tenant_id 改为 uuid，增加 mode；Claim 投影补 mode（0230）；测试租户常量统一 | — | 本分支 |
| 1b | `control.nexloop_tenants` 加 uuid 格式 CHECK（0157，先 `NOT VALID` 再 `VALIDATE`） | 冷：租户只由可信配置 manifest 写入 | 本分支 |
| 2 | control 配置类 26 张表补 `foreign key(tenant_id) references control.nexloop_tenants`（0158，见第 3 节） | 冷：可信配置、治理设置或负责人操作写入 | 本分支 nx052-batch2 |
| 3 | ontology 业务表（除 `ontology.objects` 外）以及 Message、Claim、Conversation 族 | 中：每条消息或抽取会写几行 | 只写计划 |
| 4 | runtime 队列与运行族（除 `runtime.jobs` 外）：Run、Context、模型请求、效果、成本、可观测 | 中到高 | 只写计划 |
| 5 | 热点表：`ontology.objects`、`runtime.jobs`。`authz.nexloop_authority_facts` 自 0004 起已有租户外键（盘点时漏计），它作为现有成本的对照基线 | 高 | 只写计划，以测量结果为前提 |

已有租户外键的 8 张表不动：0004 的 `authz.nexloop_authority_facts`、`authz.nexloop_service_credentials`，0008 的 action_definitions，0040 的 effect_contexts，0048 的效果回执表，以及 0050 的三张身份与配置表（audit §1.1 只列了 6 张 control 表）。在 s5c 之前的分支上，带 `tenant_id` 的基表共 175 张，其中 167 张缺外键：runtime 78 张、control 54 张、ontology 25 张、authz 10 张。下一批开工前仍需在当时的 main 上重新列一遍。

### 0.2 每批的迁移与回滚

- **迁移**：每批一个新迁移。先 `alter table … add constraint … foreign key (tenant_id) references control.nexloop_tenants(tenant_id) not valid`，这一步只持有短暂的 `SHARE ROW EXCLUSIVE` 锁，不扫描旧数据，新写入立即受约束。然后在同一迁移里 `validate constraint`，它只持有 `SHARE UPDATE EXCLUSIVE` 锁，不阻塞读写。
- **大表**：如果部署数据量使 VALIDATE 在迁移窗口内跑不完，就把该表的 VALIDATE 拆成一个单独的迁移，排在下一次发布中。在 VALIDATE 之前，约束对新行已经生效。
- **回滚**：回滚迁移只做 `alter table … drop constraint …`（加 `if exists`）。数据不变，也不需要备份恢复。已发布迁移的校验和不改；回滚以新的迁移号前滚。
- **预检**：迁移前在只读连接上跑孤儿查询：`select count(*) from <t> where tenant_id not in (select tenant_id from control.nexloop_tenants)`。若有孤儿，VALIDATE 会失败，此时由负责人决定处置方式，迁移不自行删除或改写数据。
- **1b 的特例**：CHECK 写在租户根表上，回滚方式是 `drop constraint nexloop_tenants_tenant_id_uuid`。真实部署中若已存在非 uuid 租户，VALIDATE 会失败，整个迁移事务回滚，这正是期望看到的发现（audit §4）；改名方案需要负责人决定。

### 0.3 热点表写入成本的测量

1. **基准**：使用 NX-031 压测模板（`tests/load_template.py`、`scripts/load/`）在一次性集群上跑 Run 或消息主路径的固定矩阵。分别在“加外键前”和“加外键后、已 VALIDATE”两个状态下各跑 3 轮，取中位数。
2. **指标**：
   - 每张热点表的写入语句耗时：`pg_stat_statements` 的 `mean_exec_time` 和 `calls`，按语句归到表；
   - 外键触发器耗时：外键检查由内部 C 触发器 `RI_FKey_check_ins` 执行，`pg_stat_user_functions` 统计不到（原计划写错，已更正）。改用 `explain (analyze)` 输出中 `RI_ConstraintTrigger` 的 `Time` 和 `Calls`；
   - 端到端：dispatch 时延的 p50 和 p95，沿用 NX-031 的口径；
   - 锁：`pg_locks` 采样中对 `control.nexloop_tenants` 的 `ROW SHARE` 和 `KEY SHARE` 等待。外键检查会对父行加 `FOR KEY SHARE`，因此要确认它不与租户行的 `authority_revision` 更新（`FOR UPDATE`）互相阻塞。
3. **判据**（第 5 批的验收门槛，调度员 2026-10-11 确认）：热点表写入语句的中位耗时增加不超过 10%，端到端 p95 增加不超过 5%，并且出现与 authority_revision 更新的锁等待时不得超过 NX-031 的告警阈值。超出时，该表改用“触发器只校验格式”或继续依赖身份快照，并写入 ADR。
4. **重点关注租户行锁**：`update control.nexloop_tenants set authority_revision=…` 不改键列，取的是 `FOR NO KEY UPDATE`，与外键检查的 `FOR KEY SHARE` 不冲突。但 0050 中有显式的 `select … for update`（第 86 行、第 246 行），它取的是 `FOR UPDATE`，会与 `FOR KEY SHARE` 冲突：配置发布期间，带该租户外键的业务写入会排队等待。测量时必须并发跑“配置发布 + 业务写入”。方案：第 5 批之前单独评审一个新迁移，用 `create or replace` 包装这两个函数，只把这两处锁改成 `for no key update`。它们只更新 status 和 authority_revision，都不是键列，因此 `FOR NO KEY UPDATE` 足以串行化同租户的配置发布，又不阻塞外键检查。本批不改。

### 0.4 与 0050 manifest 顺序的关系

0050 的 `apply_trusted_configuration` 先插入或更新 `control.nexloop_tenants`（第 89 行和第 92 行），再写授权事实、应用与身份。所以外键的“先有租户、后写事实”与 manifest 顺序一致，第 2 至 5 批不需要改 manifest。

需要逐一核对的是那些不经过 manifest、直接插入租户的路径。这些路径在 1b 之后也必须写入 uuid：

- 迁移 0077、0084、0086、0102、0142 中在函数体内插入租户的代码；
- `sandbox_profile` 与 `compose_bootstrap`（通过 `SANDBOX_TENANT`）；
- 各测试 fixture。

1b 的 CHECK 对这些路径同样生效：违规写入会在插入时直接失败，而不是等到 Run 或契约边界。

## 1. 批次 1a

### 1.1 Claim 契约（单独的 Schema 提交）

- `tenant_id` 由 `minLength 1` 改为 `format: uuid`，与另外 8 个契约一致。
- 新增必填字段 `mode`，枚举值为 real、simulation、shadow、test，与其他契约一致。它由服务端根据 world 推出：

  | world | mode |
  |---|---|
  | real | real |
  | simulation 或 shadow | simulation |
  | 其他 | test |

  推导规则与 `runtime.nexloop_cost_data_mode` 相同。因此 Claim 只会出现 real、test、simulation 三种 mode。
- 在 `allOf` 中增加两条约束：
  - `mode` 不得为 shadow。契约能够表达这一点，Schema 测试 `shadow_mode` 是对应的负例；
  - 与 run-command 相同的条件：mode=real 当且仅当 world_id=real。负例为 `real_world_test_mode`、`simulation_mode_in_real_world`、`real_mode_outside_real_world`。另有负例 `tenant_not_uuid` 和 `missing_mode`。
- **兼容性**：这是不向后兼容的收紧。缺少 mode 或 tenant_id 不是 uuid 的旧 Claim 将不再通过校验。影响范围如下：
  - 唯一的生产者是 SQL 投影 `authz.nexloop_claim_contract`，由 0230 补上 mode；
  - 唯一的消费方校验点是 `ClaimStore.read`；
  - 没有外部调用方，契约仍为 v1（目标契约，尚未对外发布）。
  - 若真实部署中存在非 uuid 租户的 Claim，读取时会被拒绝，而不会被放行，该问题与 1b 一起暴露。
- `docs/handoff/` 冻结不改；`packages/contracts` 是唯一的活副本，生成物和示例以它为准。偏离登记在 `packages/contracts/handoff-divergence.json`（文件、原因、任务、日期），`test_claim_contract` 只放行“与冻结副本一致或已登记”的情况。

### 1.2 0230：Claim 投影补 mode

用 `create or replace` 替换 0073 的 `authz.nexloop_claim_contract`，只增加 `mode` 字段，推导规则同上。存储、RLS、记录器和读取授权都不变。文件名带 `_nx019_`，原因是它复制的是 NX-019 的投影，因此仍在 AT-064 白名单的既有标签内。

### 1.3 测试租户常量

- `tests/support/tenants.py` 定义三个常量：
  - `TENANT_A`，取代 `synthetic-a`；
  - `TENANT_B`，取代 `synthetic-b`；
  - `TENANT_OTHER`，取代隔离测试中作为“外租户”的 `synthetic-other`（`synthetic-other-owner` 等 owner 和 intent 名不是租户，保留）；
  - `SANDBOX_TENANT`，取代 `nexloop-sandbox`。它从 `nexloop_eios.sandbox_profile` 导入，`browser_test_profile.TENANT`、`worker_test_profile` 与 `compose_bootstrap` 使用的也是同一个值。
- 替换规则：
  - Python 字符串字面量恰好等于旧值的，改为引用常量；
  - 以旧值开头、由租户派生的标识，改为 f-string，例如 `f'{TENANT_A}-principal'`。`authority_fixture` 用 `tenant+identity_suffix` 派生 principal 和 subject，所以硬编码的 `synthetic-a-*-principal` 必须跟着变。
  - SQL 文本内的旧值直接替换为对应的 uuid 字面量。
- `scripts/load/collect.py`、`scripts/load/synthetic.py` 以及 `apps/web/test/review-workbench.test.ts` 改为 uuid 字面量，并注明来源。`tests/load_template.py` 只改了租户常量，压测逻辑不变，因此压测模板的 digest 会变化，整轮测试前需要重建模板。
- 守护测试 `tests/test_tenant_constants.py` 覆盖三点：
  - 三个常量都是规范 uuid，且与 browser profile 一致；
  - 代码和测试中不再出现旧字面量；
  - SQL 中 tenant_id 位置上出现的 uuid 只能是这三个常量之一。
- 判断为“不是租户”而保留的字符串类别：
  - 以 `synthetic-` 开头、但不是 `synthetic-a`/`synthetic-b` 加连字符形式的应用、主体和资源名，例如 `synthetic-browser-app`、`synthetic-owner`、`synthetic-customer`、`synthetic-authority`、`synthetic-api`、`synthetic-absent`、`synthetic-backend-*`、`synthetic-approve-*`、`synthetic-agent-*`、`synthetic-account-a`；
  - 测试载荷文本，例如 `synthetic-community-worker-orphan`；
  - 冻结数据集 `tests/data/nx019_extraction_cases.json` 中的 `tenants`。这些值只作为纯函数抽取器的输入，不会写入数据库，并且数据集已冻结；
  - `test_claim_contract` 中故意构造的非 uuid 负例；
  - `docs/` 中的历史说明。

## 2. 批次 1b

0231 在 `control.nexloop_tenants` 上增加约束 `nexloop_tenants_tenant_id_uuid check (tenant_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')`：先 `NOT VALID`，再 `VALIDATE`。

- 约束只接受小写规范形式，因为三个契约生成器都输出小写。
- `NOT VALID` 的约束对新插入的行立即生效；`VALIDATE` 负责检查已有的行。

## 3. 批次 2：control 配置类表的租户外键（0158，临时号）

### 3.1 范围

本批共 26 张表，均在 `control.nexloop_` 前缀下：

- 浏览器配置：`browser_applications`、`browser_business_applications`、`browser_accounts`、`browser_memberships`、`browser_rate_policies`；
- 告警与工作台：`alert_rules`、`alert_silences`、`workbench_configurations`；
- 预算与上下文：`budget_limits`、`context_strategies`；
- 目标与指标：`metric_definitions`、`metric_sources`、`goals`、`goal_versions`、`key_results`；
- 恢复与保留：`restore_channels`、`restore_scopes`、`retention_holds`；
- 动作配置：`relation_action_bindings`、`action_effect_categories`；
- 消费者与转发：`erasure_salts`、`consumer_owners`、`relay_enrollments`；
- 控制面：`control_heads`、`control_scopes`；
- 商务：`commercial_connectors`。

这些表都由可信配置、治理设置或负责人操作写入，不按消息或 Run 写入。

control 中下列表留到第 4 批，因为它们按消息、登录或事件写入：

- 事件与日志类：`alert_events`、`alert_state`、`browser_login_events`、`browser_evidence`、`control_events`；
- 会话与限流计数：`browser_sessions`、`browser_rate_counters`；
- 预算消耗：`budget_consumption`；
- 指标观测：`metric_observations`、`metric_cohorts`、`metric_cohort_heads`；
- 回复与接管：`reply_escalations`、`takeover_*`、`contact_*`；
- 其他：`deletion_ledger`、`data_exports`、`consumer_*` 的其余表、`effect_*`、`run_goal_bindings`、`relationship_context_recipes`、`erased_contact_keys`。

authz 中缺外键的 10 张表都是 Run 或消息的绑定表，也归入第 4 批。

迁移先对每张表 `add constraint nexloop_<t>_tenant_fk … not valid`，再逐表 `validate constraint`。

**回滚**：新开一个迁移，逐表执行 `alter table control.nexloop_<t> drop constraint if exists nexloop_<t>_tenant_fk`。数据不变。

### 3.2 写入路径核对

| 写入方 | 表 | 租户是否必然已存在 |
|---|---|---|
| 0050 / 0142 `configure_manifest`、`initial_identity_create` | 浏览器应用、业务应用、账户、成员、限流策略 | 同一函数先锁定或插入 `nexloop_tenants` |
| `commercial_configure_connector`（0118）、`restore_reconcile` / `restore_verified`（0147） | commercial_connectors、restore_channels、restore_scopes | 函数内读取 `nexloop_tenants` |
| `configure_workbench`（0116）、`context_strategy_publish`（0088）、`erasure_step`（0145） | workbench_configurations、context_strategies、erasure_salts | 调用前经身份快照校验，快照要求租户为 active |
| `conversation_command`（0138）、`relay_enrollment_command`（0141） | consumer_owners、relay_enrollments | 先经签名授权校验，授权要求租户存在 |
| nx022 系列（0068）、`configure_alert_rules`（0131）、`governed_alert_silence`（0133）、`governed_retention_hold`（0132）、`configure_effect_categories`（0112）、`metric_source_declare`（0119） | 目标、指标、预算、控制面、告警、保留、效果类别 | 都由已认证的服务或负责人会话调用，租户来自签名声明，并由 `nexloop_service_identity_snapshot` 复核 |
| `browser_test_profile` | 浏览器配置 | 写入前检查租户为 active（第 40 行） |
| 无 SQL 写入方 | relation_action_bindings | 只有测试直接写入 |

- **没有删除租户的路径**：仓库中没有 `delete from control.nexloop_tenants`，也没有改租户 id 的语句。
- **备份恢复不受影响**：`pg_dump` 先装载数据再建约束，所以恢复演练不受影响，相关测试在本批的测试集中已通过。
- **受影响的只有测试捷径**：`test_browser_identity_reads`、`test_browser_rate_limits` 和 `test_browser_tenant_rotation` 在没有租户行的情况下直接写浏览器配置。现已在写入前先插入租户行，与生产顺序一致。
- **错误码**：违反外键时报 23503，属于拒绝，不是可重试的成功。

### 3.3 冷表的测量方法验证

工具是 `scripts/load/tenant_fk_cost.py`，只在一次性集群上运行。它的做法：

- 对一张表交替删除和重建其 `_tenant_fk`；
- 每个变体逐行 autocommit 插入，记录客户端测得的中位数和 p95；
- 用一次 `explain (analyze)` 采样外键触发器的耗时；
- 做一次锁探针：另一会话按 0050 的方式对租户行持有 `FOR UPDATE`，同时执行插入。

测量表选用无触发器的 `control.nexloop_commercial_connectors`。`test_load_matrix` 禁止 `scripts/load` 写入登录限流策略，所以不用 `browser_rate_policies`。

本机结果（2026-10-11）：2000 行 × 3 轮，取各轮的中位数；测量时开发机负载约为 6。

| 变体 | 中位耗时 | p95 耗时 |
|---|---|---|
| 无外键 | 68.9 µs | 84.4 µs |
| 有外键 | 71.6 µs | 87.1 µs |
| 比值 | 1.040 | 1.032 |

- **判据**：中位耗时不超过 +10%、p95 不超过 +5%，本批**通过**。
- **触发器采样**：1 次调用，耗时 0.01 ms。
- **锁探针**：无外键时插入等待 0.2 ms；有外键时等待 0.307 s，正好等于 `FOR UPDATE` 的持有时长。这在本地复现了 §0.3 第 4 点中的冲突。冷表的配置写入与配置发布本来就在同一个运维窗口里，影响可以接受；但第 5 批之前，“改为 `for no key update`”的评审是必需的。
- **端到端 p95**：本批的表不在 dispatch 主路径上，端到端 p95 按定义不受影响。第 3 至 5 批要用 NX-031 的矩阵测量端到端 p95，而不是本节的语句级 p95。

## 4. 批次 3 设计（3a、3b 已实现，见第 5 节；3c 待锁评审）

### 4.1 范围与分组

第 3 批覆盖 ontology 中除 `ontology.objects` 外的 24 张表，以及会话、消息、Claim 族。热点表 `ontology.objects` 和 `runtime.jobs` 不在本批。表按写入频率和来源分为四组，每组一个迁移，按 3a → 3d 的顺序合入：

| 组 | 表 | 写入频率 | 说明 |
|---|---|---|---|
| 3a 定义与配置 | `ontology.nexloop_merge_configurations`、`nexloop_recall_settings`、`nexloop_embedding_profiles`、`nexloop_definition_aliases`、`nexloop_candidate_definitions`、`nexloop_candidate_events`、`nexloop_candidate_rejections`、`nexloop_review_decisions`、`nexloop_instance_approvals`、`nexloop_assessment_revisions` | 冷：评审或治理动作 | 与第 2 批同类，可直接沿用第 2 批的做法 |
| 3b 抽取与 Claim | `ontology.nexloop_claims`、`nexloop_claim_matches`、`nexloop_claim_rematch`、`nexloop_conversation_topics`、`nexloop_extraction_runs`、`nexloop_extraction_run_claims`、`nexloop_mutation_proposals`、`runtime.nexloop_claim_extraction_feed` | 中：每条消息的抽取会写几行到几十行 | 一次抽取在一个事务里多行写入，外键检查会对同一租户行加多次 `FOR KEY SHARE`（同一事务内只锁一次，代价主要是索引查找） |
| 3c 会话与消息 | `runtime.nexloop_conversations`、`nexloop_conversation_messages`、`nexloop_message_inbox`、`nexloop_message_outbox`、`nexloop_message_routes`、`nexloop_message_provider_facts`、`nexloop_message_reply_links`、`nexloop_message_relay_leases`、`nexloop_relay_claims`、`nexloop_relay_consumer_cursors`、`nexloop_outbound_messages`、`nexloop_native_web_events`、`nexloop_native_web_transports`、`nexloop_staff_replies` | 中到高：每条入站或出站消息都会写入 | 位于消息受理的 ACK 前路径，端到端时延必须实测 |
| 3d 上游 EIOS 核心表 | `ontology.events`、`relations`、`object_type_versions`、`event_type_versions`、`relation_type_versions`，以及 `nexloop_recall_entries`、`nexloop_recall_embeddings` | `events` 随对象变更写入，接近热点；其余为冷表 | 需要先决定，见 4.4 |

### 4.2 迁移与回滚

- 做法与第 2 批相同：每组一个迁移，先 `not valid`，再逐表 `validate`；回滚时新开迁移 `drop constraint if exists`。
- 3c 中的表在真实部署里可能较大。如果预检发现单表 VALIDATE 超过迁移窗口（建议上限 60 秒，可在部署副本上实测），就把该表的 VALIDATE 拆到下一次发布。
- 擦除路径（NX-029）删除或改写的是子表的行，外键不会影响子行的删除。

### 4.3 测量

- **3a**：只做语句级测量，方法同 §3.3。
- **3b、3c**：用 NX-031 压测矩阵测量端到端时延。在 1b 之后的同一模板上，分别跑加外键前和加外键后两个状态，各 3 轮，比较 dispatch 时延的 p50 和 p95，判据为 p95 增加不超过 5%。语句级采样同时用 `tenant_fk_cost.py` 覆盖 `nexloop_conversation_messages` 和 `nexloop_claims`。需要在 TABLES 中加入这两张表；它们带触发器，清理语句要走测试专用路径。
- **锁**：3c 位于消息受理的事务里，必须并发跑“配置发布（0050 的 `FOR UPDATE`）+ 消息受理”的锁探针。如果“改为 `for no key update`”的评审尚未完成，并且锁等待超出判据，那么 3c 要等这项评审完成后再合入。

### 4.4 待决定

1. **3d 是否加外键**：`ontology.events`、`relations` 等 5 张表来自 vendored EIOS 核心（0001）。给它们加 NexLoop 的 `control.nexloop_tenants` 外键，会让核心 schema 依赖 NexLoop 的控制面表。建议的处理方式二选一：
   - 只给 NexLoop 自己的 recall 表加外键，核心表继续依赖身份快照；
   - 以 ADR 记录这一偏离，然后再加外键。
2. **`ontology.events` 的归类**：它随对象变更写入，写入量接近 `ontology.objects`。建议把它归入第 5 批，与 `objects` 一起测量。
3. **3c 与 0050 锁评审的先后**：建议先完成“改为 `for no key update`”的评审，再合入 3c。

## 5. 批次 3a、3b 实现（0159、0160）

### 5.1 裁定（调度员 2026-10-12）

1. vendored EIOS 核心表（0001 的 `ontology.events`、`relations`、`object_type_versions`、`event_type_versions`、`relation_type_versions`）**不加** NexLoop 的租户外键，继续依赖身份快照。
   - **理由**：AGENTS 要求 vendored 内核与上游的偏离尽量小，而且核心不应依赖 NexLoop 的控制面表。
   - **边界**：只有 NexLoop 自己建的表（`nexloop_` 前缀）才引用 `control.nexloop_tenants`；recall 表 `nexloop_recall_entries`、`nexloop_recall_embeddings` 属于 NexLoop，归入 3b。
   - 不另立 ADR。`tests/test_tenant_fk_pg.py` 断言这 5 张核心表没有指向租户的外键。
2. `ontology.events` 归入第 5 批，与 `ontology.objects` 一起测量。
3. 0050 两处锁的改动先评审，评审稿见 `NX-052-tenant-lock-review.md`；3c 等评审通过后再做。

### 5.2 迁移

- **0159（3a）**：ontology 中 10 张定义、评审与配置类表，包括合并配置、recall 设置、embedding 配置、定义别名、候选定义及其事件和拒绝记录、评审决定、实例批准、评估修订。
- **0160（3b）**：抽取与 Claim 族 8 张表（claims、matches、rematch、主题、抽取运行及其 Claim 关联、变更提议、抽取队列），加上 recall 的两张表。
  - 0160 涉及 `nexloop_claims`，已在 AT-064 白名单登记：本迁移只加约束，不读也不投影 Claim 值。
- 两个迁移的做法相同：先 `not valid`，再逐表 `validate`；回滚时新开迁移 `drop constraint if exists <表>_tenant_fk`。

3b 的多数表原本已经通过复合外键引用父表，链条最终指向 `runtime.nexloop_conversations`。但会话表本身属于 3c，现在还没有租户外键。所以在 3c 之前，3b 直接加的租户外键是这条链上唯一的租户存在性保证。3c 之后它与链上的检查有些重复，代价已经计入下面的测量。

### 5.3 写入路径

| 组 | 写入方 | 租户是否必然已存在 |
|---|---|---|
| 3a | 评审、候选和合并配置的治理函数（NX-045、NX-046、NX-050、NX-021 的配置） | 先经签名授权和身份快照校验，租户必然存在 |
| 3b | 抽取记录器（NX-019）、匹配器（NX-020）、NX-044 回流、recall 索引器 | 同上；而且这些写入都晚于会话的创建，会话本身也已经过授权 |

测试侧直接写入这些表的地方都在租户行存在之后。本批受影响的 53 个测试文件全部通过（见报告）。

### 5.4 测量（2026-10-12，本机负载约 5–6，2000 行 × 3 轮）

| 表 | 无外键 中位 / p95 | 有外键 中位 / p95 | 比值 中位 / p95 | 判据 |
|---|---|---|---|---|
| `ontology.nexloop_merge_configurations`（3a） | 71.8 / 84.2 µs | 75.1 / 87.1 µs | 1.046 / 1.035 | 通过 |
| `ontology.nexloop_extraction_runs`（3b，另有一个指向会话的外键） | 50.3 / 61.0 µs | 52.6 / 63.2 µs | 1.046 / 1.035 | 通过 |

锁探针：在 `FOR UPDATE` 下，两张表的插入都被阻塞约 0.3 秒；在 `FOR NO KEY UPDATE` 下不再阻塞（0.6–0.7 ms）。这与评审稿的预期一致。

3b 每次抽取会写几行到几十行，按上表的单行增量（约 2–3 µs）估算，一次抽取增加 0.1 ms 以内。端到端 p95 要等 3c 时用 NX-031 矩阵一起实测。

## 6. 批次 3c 设计：会话与消息表（已实现，见第 7 节）

前置条件：0162（租户行锁改为 `FOR NO KEY UPDATE`）已经实现，等待合入。3c 要在 0162 合入 main 之后再合入。

### 6.1 范围

下面 13 张表在 runtime 下，都位于消息受理、转发或回复的路径上：

| 表 | 已有的复合外键 | 3c 的处理 |
|---|---|---|
| `nexloop_conversations` | 无（会话族的根表） | **直接加租户外键** |
| `nexloop_conversation_messages` | `(tenant_id,world,conversation_id)` 引用会话 | 见 6.2 |
| `nexloop_outbound_messages` | 引用会话、`effect_intents` | 见 6.2 |
| `nexloop_message_inbox`、`nexloop_message_outbox`、`nexloop_message_routes`、`nexloop_message_provider_facts`、`nexloop_message_reply_links`、`nexloop_message_relay_leases` | 无 | 直接加 |
| `nexloop_relay_claims`、`nexloop_relay_consumer_cursors`、`nexloop_staff_replies` | 无 | 直接加 |
| `nexloop_native_web_events` | 无（原生入站的根表） | 直接加 |
| `nexloop_native_web_transports` | 引用 `native_web_events`，含 tenant_id | 见 6.2 |

`nexloop_inbox`、`nexloop_outbound_delivery_events`、`nexloop_context_artifact_bindings` 和 `nexloop_fallback_context_bindings` 属于 Run 与效果族，留在第 4 批。

### 6.2 是否只给根表加（需要你决定）

子表的复合外键里已经包含 `tenant_id`，并且引用的是根表的主键。一旦根表有了租户外键，“子行的租户必然存在”就可以沿外键链传递得到。所以对这类子表，可以只给根表加，不给子表加：

- **方案 A（建议）**：只给根表 `nexloop_conversations` 和 `nexloop_native_web_events`，以及其余没有复合外键的表加租户外键；`conversation_messages`、`outbound_messages`、`native_web_transports` 依赖外键链。每条消息少做一到两次外键检查。测试断言这条外键链完整：每个子表都有一个外键，其列以 `tenant_id` 开头，并且引用的父表已有租户外键。
- **方案 B**：每张表都直接加，规则统一，但每条消息多做一到两次检查。

3b 中 `extraction_runs`、`claims` 等表已经直接加了外键，它们同样可以只依赖会话链，但已合入的部分不回退。

### 6.3 迁移与回滚

- 一个迁移（0164，合入时由临时号 0300 重编号），先 `not valid` 再逐表 `validate`。
- `nexloop_conversation_messages` 是本批最大的表：先在部署副本上实测 VALIDATE 的时长，超过 60 秒就把它的 VALIDATE 拆到下一次发布。如果采用方案 A，这张表不加约束，问题就不存在。
- **回滚**：新开迁移，`drop constraint if exists`。

### 6.4 写入路径

所有入站路径都先经过签名授权，或者原生入站的身份校验（取 `FOR SHARE` 或身份快照），之后才写入。所以写入时租户行必然存在。

出站、转发和人工回复路径的写入都晚于会话创建。会话表本身由 0046 的受理函数在授权之后写入。

### 6.5 测量（判据：端到端 p95 增加不超过 5%）

1. **语句级**：用 `tenant_fk_cost.py` 测 `nexloop_conversations` 和 `nexloop_message_inbox`。需要在 TABLES 中加入这两张表；会话表带触发器，清理语句要走测试专用的路径。
2. **端到端**：在 NX-031 压测模板上跑消息主路径矩阵，分为三个状态，每个状态 3 轮，比较 dispatch 时延的 p50 和 p95：
   - 0162 之后、3c 之前；
   - 3c 之后；
   - 3c 之后，同时并发跑配置发布，每 5 秒发布一次 manifest，验证 0162 的效果。

   这一项需要 L4 车道的整轮窗口，由调度员安排；本机只能跑语句级。

### 6.6 待决定

1. 采用方案 A 还是方案 B。
2. 端到端测量排在哪个车道窗口。

## 7. 批次 3c 实现（0164）

### 7.1 裁定（调度员 2026-10-12）

- 采用方案 A：根表直接加租户外键；已经通过包含 `tenant_id` 的复合外键引用根表的子表，不再单独加。
- 端到端测量放到 1000 客户的整轮窗口，分三个状态：0162 之后、3c 之后、3c 之后并发配置发布（每 5 秒一次）。本机只做语句级测量。

### 7.2 迁移

0164 给 runtime 下 11 张表加 `nexloop_<t>_tenant_fk`，做法仍是先 NOT VALID 再 VALIDATE；回滚时新开迁移 `drop constraint if exists`。这 11 张表是：

- 根表：`nexloop_conversations`、`nexloop_native_web_events`；
- 没有复合外键的表：`message_inbox`、`message_outbox`、`message_routes`、`message_provider_facts`、`message_reply_links`、`message_relay_leases`、`relay_claims`、`relay_consumer_cursors`、`staff_replies`。

下面 3 张子表靠外键链传递，不单独加：

| 子表 | 复合外键 | 父表 |
|---|---|---|
| `nexloop_conversation_messages` | `(tenant_id,world,conversation_id)` | `nexloop_conversations` |
| `nexloop_outbound_messages` | `(tenant_id,world,conversation_id)` | `nexloop_conversations` |
| `nexloop_native_web_transports` | `(tenant_id,world,provider_namespace,principal_id,provider_event_id)` | `nexloop_native_web_events` |

### 7.3 链式传递的判据（以后新增的子表也必须满足）

一张表算作“有租户路径”，需要满足下面两条之一：

1. 它有一个已 VALIDATE 的外键，引用 `control.nexloop_tenants(tenant_id)`；
2. 它有一个已 VALIDATE 的外键，其列映射中包含“子表的 `tenant_id` → 父表的 `tenant_id`”，并且父表本身有租户路径（递归判断）。

只看列名不够，必须是 `tenant_id` 映射到 `tenant_id`。

`tests/test_tenant_fk_pg.py` 用一条递归 SQL（`TENANT_PATH_SQL`）计算所有有租户路径的表，并断言三点：

- 3c 的 11 张根表都已直接加外键并 VALIDATE，3 张子表没有自己的租户外键，而且这 14 张表都在租户路径集合里；
- 引用 3c 根表或子表的**所有**表（包括以后新增的表）都在租户路径集合里；
- 反例：在同一事务里临时建两张表，一张的复合外键用另一列去对应父表的 `tenant_id`，另一张用 `tenant_id` 对应 `tenant_id`。前者不算有租户路径，后者算。

### 7.4 写入路径

- 入站：先经过签名授权或原生入站的身份校验，再写会话、收件箱、路由等表，租户行必然已存在。
- 出站、转发、人工回复：都晚于会话创建。
- 0162 之后，配置发布和初始身份创建取的是 `FOR NO KEY UPDATE`，不再阻塞这些外键检查。锁探针在 `FOR UPDATE` 下等待约 0.31 秒，在 `FOR NO KEY UPDATE` 下只等 0.2–0.4 ms。

### 7.5 语句级测量（2026-10-12，2000 行 × 3 轮）

| 表 | 无外键 中位 / p95 | 有外键 中位 / p95 | 比值 中位 / p95 | 判据 |
|---|---|---|---|---|
| `runtime.nexloop_conversations` | 59.2 / 108.4 µs | 60.0 / 99.9 µs | 1.014 / 0.921 | 通过 |
| `runtime.nexloop_message_inbox` | 51.0 / 94.0 µs | 50.9 / 89.8 µs | 0.999 / 0.955 | 通过 |

测量期间本机负载约 6.7，L4 正在跑 1000 客户模板，所以 p95 低于 1 是噪声。中位比值与前几批的 1.04–1.05 处在同一量级。

端到端 p95 等整轮窗口按三个状态测量，判据是增加不超过 5%。

## 8. 批次 4：Run、Context、效应与计划族（0166）

### 8.1 设计

- **范围的确定方法**：在 s5j 上用 3c 的递归判据 `TENANT_PATH_SQL`，列出所有带 `tenant_id`、但还没有租户路径的表，共 106 张。本批只处理其中 NexLoop 自己的 Run、Context、效应和计划族，共 48 张：
  - 32 张直接加外键，其中 authz 9 张、runtime 23 张；
  - 16 张靠外键链覆盖。
- **哪些表要直接加**：这些表引用父表时用的是 `run_id`、`intent_id`、`context_id` 等单列外键，列中不含 `tenant_id`，所以不满足链式判据，必须直接加。例如 `effect_intents` 引用 `control.nexloop_effect_contexts(context_id)`，虽然父表有租户外键，但这条外键不传递租户。
- **哪些表靠链覆盖**：外键的列包含 `tenant_id`，并且父表有租户路径。

  | 链上的表 | 父表 |
  |---|---|
  | `context_sources` | `context_packs` |
  | `model_requests` | `context_packs` |
  | `model_request_prompts` | `model_requests` |
  | `context_artifact_bindings`、`fallback_context_bindings` | `local_artifacts` |
  | `effect_attempts`、`catalog_bindings`、`provider_bindings`、`events`、`local_delivery_recoveries` | `effect_intents` |
  | `effect_observations`、`query_admissions` | `effect_attempts` |
  | `plan_events`、`plan_outcomes`、`plan_runs`、`plan_steps` | `plans` |

- **不在本批**：
  1. **vendored EIOS 核心**：0001 中的 `runtime.jobs`、`invocations`、`job_events`、`audit_events`、`idempotency_records`、`artifact_references`。按 3d 的裁定不加外键。
  2. **`runtime.nexloop_inbox` 和 `runtime.nexloop_outbox`**：它们带租户的外键指向 `runtime.jobs`。`jobs` 是核心表，没有租户路径，所以这两张表要么直接加，要么等第 5 批的定性结果，见 8.5。
  3. **控制面事件、会话、台账**，以及商务、承诺、擦除和审计族（约 50 张）：列为第 4b 批，只做计划，见 8.5。
- **与 ACK 的关系**：这些表都在消息受理之后写入，由 Run 签发、上下文组装、效应派发和计划执行产生，不在 ACK 之前的路径上。
- **与 0162 锁的关系**：外键检查对租户行取 `FOR KEY SHARE`，它与授权路径的 `FOR SHARE`、0162 之后配置发布的 `FOR NO KEY UPDATE` 都不冲突。锁探针结果：持 `FOR UPDATE` 时插入等 0.30 秒，持 `FOR NO KEY UPDATE` 时只等 0.2 ms。
- **写入路径**：这些表都由带签名的 Run 凭据、或服务身份快照校验之后的函数写入。Run 凭据本身要求租户存在，所以写入时租户行必然存在。

### 8.2 迁移与回滚

- 0166 给 32 张表加外键，先 NOT VALID，再逐表 VALIDATE。
- **回滚**：新开迁移，`drop constraint if exists <表>_tenant_fk`。
- **大表**：`cost_entries`、`model_requests` 一类表在部署中可能较大。在部署副本上先预检 VALIDATE 的时长，超过 60 秒就把该表的 VALIDATE 拆到下一次发布。`model_requests` 本身靠外键链覆盖，没有自己的约束。

### 8.3 测试

`tests/test_tenant_fk_pg.py` 断言三点：

- 32 张直接加外键的表都已 VALIDATE；
- 16 张链上的表没有自己的租户外键，vendored 的 runtime 核心表也没有，而且 48 张都在租户路径集合里；
- 3c 的“所有子表都满足链式判据”断言照常生效。

本批唯一受影响的测试捷径是 `test_context_engine_pg::test_storage_invariants…`：它在没有租户行的情况下直接插入 `context_packs`，现已在插入前先插入租户行。

### 8.4 语句级测量（2026-10-12，2000 行）

| 表 | 轮数与负载 | 中位比值 | p95 比值 | 判据 |
|---|---|---|---|---|
| `runtime.nexloop_run_tool_timings`（每个 Run 一行） | 5 轮，负载约 6 | 1.071 | 0.913 | 通过 |
| `runtime.nexloop_work_feed`（每个工作项一行） | 5 轮，负载约 6 | 1.054 | 1.040 | 通过 |

- 每轮比值波动很大：
  - `run_tool_timings` 的中位比值在 1.03 到 1.11 之间，p95 比值在 0.85 到 1.30 之间；
  - `work_feed` 的中位比值在 0.99 到 1.11 之间，p95 比值在 0.85 到 1.29 之间。
- 负载约 9 时先跑过一次 3 轮，`work_feed` 得到 1.147 / 1.174，超出判据，判为噪声后重测。
- 在本机共享负载下，单轮结果不能作为判据，只有多轮中位数的结论才有意义。精确数字以整轮窗口的端到端测量为准。

### 8.5 待决定

1. **第 5 批要重新定性**：`ontology.objects`、`runtime.jobs` 和 `ontology.events` 都来自 vendored EIOS 核心（0001）。按 3d 的裁定，“核心表不加 NexLoop 外键，继续依赖身份快照”，第 5 批就不应该加外键。有两种做法：
   - 维持裁定：第 5 批改为“只做测量和文档化，不加约束”；
   - 写一份 ADR，作为例外给这三张核心表加外键。
2. **`runtime.nexloop_inbox` 和 `nexloop_outbox`**：它们是 NexLoop 自己的 durable 队列表，带租户的外键指向 `runtime.jobs`。如果第 5 批不给 `jobs` 加外键，这两张表需要直接加，写入频率与 `jobs` 相当，建议归到第 5 批一起测量。
3. **第 4b 批**：剩余约 50 张 control 和 runtime 表，包括控制面事件、浏览器会话与登录事件、限流计数、台账、商务、承诺、擦除和审计。按写入频率再分两组实现，是否继续由你安排。
## 9. 批次 4b：控制面事件与台账；商务、承诺、擦除与审计（0167、0168）

裁定（调度员 2026-10-12）：先做写入频率低、风险小的控制面表，再做商务、承诺、擦除和审计。擦除表与审计表要注意删除传播和只追加约束，外键不得阻碍擦除的顺序。

### 9.1 范围

- **0167（4b-1，22 张）**：
  - 控制面事件：`action_effect_externality`、`alert_events`、`alert_state`、`control_events`；
  - 浏览器会话与登录：`browser_evidence`、`browser_login_events`、`browser_sessions`；
  - 限流计数：`browser_rate_counters`；
  - 身份关联回执：`consumer_identity_link_receipts`；
  - 联系限制：`contact_refusal_hits`、`contact_restrictions`；
  - 效应控制台账与计划绑定：`effect_control_ledger`、`effect_plan_bindings`；
  - 真实派发开关：`real_dispatch_switches`；
  - 关系上下文配方：`relationship_context_recipes`；
  - 回复升级与对账：`reply_escalations`、`reply_reconciles`；
  - 接管：`takeovers`、`takeover_escalations`、`takeover_settlements`；
  - 清扫记录：`runtime.nexloop_orphan_sweeps`、`retention_sweeps`。
- **0168（4b-2，20 张）**：
  - 商务：`commercial_customer_links`，以及 runtime 中的 6 张 `commercial_*` 表；
  - 承诺：4 张 `commitment*` 表；
  - 激励：`incentive_issues`；
  - 擦除：`consumer_erasures`、`data_exports`、`deletion_ledger`、`erased_contact_keys`、`erasure_artifacts`、`erasure_tombstones`；
  - 审计：`read_audit_daily`、`workbench_read_audit`。
- **全部直接加**：这些表现有的外键（如 `takeover_id`、`connector_id`、`context_id`、`control_id`）都不带 `tenant_id`，不满足链式判据。

### 9.2 删除传播与只追加约束

- **没有 ON DELETE 冲突**：仓库里对这些表的删除全部是删除子行，来自擦除步骤、保留到期清扫和恢复日志回放；子表指向租户的外键不约束子行的删除。仓库中没有删除租户行或修改 `tenant_id` 的代码，所以不需要任何 `ON DELETE` 动作，擦除顺序不受影响。
- **只追加触发器不受影响**：`*_append_only`、`nx029_no_delete`、`nx029_ledger` 等触发器保护的是这些行自身的 update 和 delete；外键只在插入时检查租户是否存在，两者互不影响。
- **恢复不受影响**：恢复演练用 `pg_restore`，它先装载数据、后建约束。

### 9.3 写入路径与测试捷径

生产写入方都在签名授权或身份快照校验之后写入，租户必然存在。

唯一受影响的测试捷径是 `tests/test_journal_retention_pg.py`：它在没有租户行的情况下直接写入 `deletion_ledger`，现已在 fixture 中先插入两个租户。

## 10. 批次 5：只测量与文档，以及 durable 队列表（0169）

### 10.1 裁定（调度员 2026-10-12）

1. **vendored 核心表不加 NexLoop 外键**：涉及 0001 中的 `ontology.objects`、`events`、`relations`、三张类型版本表，以及 `runtime.jobs`、`invocations`、`job_events`、`audit_events`、`idempotency_records`、`artifact_references`。第 5 批对它们只做测量和文档，不写 ADR。
2. **`runtime.nexloop_inbox`、`nexloop_outbox` 是 NexLoop 自有表**：直接加租户外键，并入本批实现和测量。

### 10.2 核心表的租户一致性由谁保证

- **RLS 策略**：0002 给每张核心表打开并强制 RLS（`enable` 加 `force row level security`），策略 `tenant_boundary` 要求 `tenant_id = current_setting('eios.tenant_id')`，`using` 和 `with check` 两处都有。所以属主角色也只能读写当前 GUC 指定的租户。
- **GUC 的来源**：这个 GUC 只由 SECURITY DEFINER 写入函数设置，例如 0010 的受治理对象创建、0035 和 0036 的队列，以及 Run 和动作入口。这些函数设置 GUC 之前，都先经过签名授权或服务身份快照校验，而这两种校验都要求租户行存在且为 active。
- **测试**：`tests/test_tenant_fk_pg.py` 把上述 12 张表登记为 `KNOWN_EXCEPTIONS`，理由引用本裁定，并断言三点：
  - 它们没有指向租户的外键；
  - 它们开启并强制了 RLS；
  - `tenant_boundary` 策略的 `using` 和 `with check` 都按 `eios.tenant_id` 比较 `tenant_id`。

### 10.3 durable 队列表

- **为什么要直接加**：`nexloop_inbox` 和 `nexloop_outbox` 带租户的复合外键 `(tenant_id, job_id)` 指向 `runtime.jobs`，而 `jobs` 属于核心表，没有租户路径，所以这两张表必须直接加。
- **在 ACK 前路径上**：durable inbox 按“先持久化、再 ACK”的方式在受理时写入，所以外键检查在 ACK 前路径上。
- **测量结果**（2000 至 3000 行，负载约 5–6）：

| 表 | 测量 | 中位比值 | p95 比值 | 判据 |
|---|---|---|---|---|
| `inbox` | 5 轮 | 1.064 | 1.039 | 通过 |
| `outbox` | 第 1 次，5 轮 | 1.059 | 1.121 | 未通过 |
| `outbox` | 第 2 次，7 轮，3000 行 | 1.017 | 0.977 | 通过 |
| 对照表 `commercial_connectors`（第 2 批已通过） | 与 `outbox` 第 1 次同时 | 1.038 | 1.054 | 未通过 |
| 对照表 `commercial_connectors` | 与 `outbox` 第 2 次同时 | 1.089 | 1.115 | 未通过 |

- **结论**：在本机共享负载下，单轮 p95 比值在 0.49 到 1.33 之间跳动，语句级测量分辨不出 5% 的 p95 差异；中位比值稳定在 1.02 到 1.09，说明单行外键检查的代价约为几微秒。是否放行以 1000 客户整轮窗口的端到端 p95 为准（判据：增加不超过 5%）。
- **整轮窗口的建议**：在“0162 之后、3c 之后、并发配置发布”这三个状态之外，加一个“0169 之后”的状态，单独比较入站受理时延。
- **锁探针**：持 `FOR NO KEY UPDATE` 时，两张表的插入等待都只有约 0.3 ms。

## 11. 端到端测量方案 v2：200 客户模板、C4-low 类格（在 CI 主机执行）

背景：D-a（2026-10-13）把 v0.1 定为 200 客户、持续来信每秒不超过 0.1 条；AT-060 的门槛按矩阵 v2 判定，即 C4 用 0.1/s、C4-low 用 0.05/s。1000 客户整轮不再作为门槛。原来按 1000 客户整轮窗口设计的四状态方案作废，改为下面的方案。

### 11.1 “有外键”与“无外键”的对照：同一模板，在单元的一次性克隆上删约束

- **做法**：每个状态都用合入后的同一个提交、同一个 200 客户模板，只跑一格。“无外键”状态设 `NEXLOOP_LOAD_TENANT_FK_DROP_CELL=all`。`stack.py` 在这一格的模板克隆上、任何服务启动之前，删掉 NX-052 全部批次的租户外键：第 2、3a、3b、3c、4、4b-1、4b-2、5 批，以及 token realms 那一条。删掉的约束数记在单元目录的 `tenant-fk-state.json` 里。
  - 模板本身和其他格都保留外键。
  - NX-052 之前就有的租户外键（0004、0008、0040、0048、0050）不动。
  - 克隆是一次性集群，格结束后即删除，所以不会碰任何持久库。
- **为什么不对比“0158 之前的检出”和当前 main**：这两个检出之间还隔着大量与外键无关的提交，例如回复保证、派发、协议等改动，比出来的差别不只是外键的代价。同一模板、同一提交下，唯一的变量就是这些约束；1b 的 CHECK 和 0162 的锁改动在两个状态下都相同。
- **为什么不在模板构建时删**：那样每个状态都要重建一次模板。模板构建本身会让数据和缓存的物理布局产生差异，会混进对照里。

### 11.2 并发配置发布的注入方式：按 0162 的锁做租户行锁脉冲

- **注入**：设 `NEXLOOP_LOAD_TENANT_LOCK_PULSE=5,0.2`。`stack.py` 在这一格里另起一个进程，每 5 秒对本租户行持有 0.2 秒 `FOR NO KEY UPDATE`，这正是 0162 之后 `configure_manifest` 和 `initial_identity_create` 所取的锁。0.2 秒是本机配置发布事务时长的上限。
- **为什么不真的每 5 秒发布一次 manifest**：
  1. 单元里拿不到配置员密钥和模板的完整 manifest；
  2. 真发布会递增 `authority_revision`，使授权缓存失效，带来与外键无关的时延变化。

  NX-052 要验证的是“租户行锁不阻塞外键检查”，锁脉冲正好隔离出这一点。授权缓存失效的代价属于配置发布本身，不在 NX-052 的范围内。

### 11.3 状态、指标与判据

只跑矩阵中的 C4-low 格。并发 4，速率取 v2 中 C4 的 0.1/s，这是 D-a 的上限，对外键代价最敏感。时序沿用矩阵：预热 120 秒、稳态 600 秒、排空最长 600 秒，每格约 15 到 22 分钟。

| 状态 | 单元环境 | 用途 |
|---|---|---|
| N | `NEXLOOP_LOAD_TENANT_FK_DROP_CELL=all` | 无外键基线 |
| K | 不设 | 全部外键 |
| P | `NEXLOOP_LOAD_TENANT_LOCK_PULSE=5,0.2` | 全部外键，加并发配置发布 |

| 比较 | 指标（`report.json` 中 C4-low 格） | 判据 |
|---|---|---|
| K 对 N | `metrics.ack_p95_ms`（入站受理到 ACK）、`decomposition.e2e_ms.p95`（触发到回复）、`metrics.idle_dispatch_p95_ms`（受理到 Run 任务被认领） | 三项 p95 增加均不超过 5% |
| P 对 N | 同上 | 同上 |

另外两条要求：

- K 和 P 两次运行都必须通过 `report.py` 自己的门槛判定（格的 `gate.result` 为 `passed`）。
- 如果某项比值落在 1.05 到 1.06 之间，就把该状态和 N 各再跑一次，取两次比值的中位数。

如果 v2 矩阵已经合入（`deploy/load/matrix.v2.json`），`nx052_e2e.py matrix` 会自动以它为基础；否则以 v1 为基础，并只改 C4-low 这一格的速率和并发。

### 11.4 在 CI 主机上的执行命令

在 CI 主机上用车道账户、在 main 的检出中执行。三个状态各调用一次 `run_on_lane.sh`，每次都会重建一次 200 客户模板（种子固定，内容相同）。

```bash
uv run --frozen python scripts/load/nx052_e2e.py matrix --rate 0.1 --out .ci-results/load/matrix.nx052.json
export LOAD_CONSUMERS=200 LOAD_MATRIX=.ci-results/load/matrix.nx052.json LOAD_RUNNER_CMD=scripts/load/matrix_cells.py NEXLOOP_LOAD_CELLS=C4-low
NEXLOOP_LOAD_TENANT_FK_DROP_CELL=all scripts/load/run_on_lane.sh                 # N
scripts/load/run_on_lane.sh                                                      # K
NEXLOOP_LOAD_TENANT_LOCK_PULSE=5,0.2 scripts/load/run_on_lane.sh                 # P
uv run --frozen python scripts/load/nx052_e2e.py compare --baseline .ci-results/load/<N>/report.json --candidate .ci-results/load/<K>/report.json
uv run --frozen python scripts/load/nx052_e2e.py compare --baseline .ci-results/load/<N>/report.json --candidate .ci-results/load/<P>/report.json
```

- `compare` 输出 JSON，内容包括每项指标的基线值、候选值、比值，以及两次运行的门槛判定。全部满足时退出码为 0，否则为 1。
- **核对状态是否生效**：
  - N 的单元目录 `lane/.../tenant-fk-state.json` 中，`dropped_constraints` 应大于 0；
  - K 和 P 的单元目录里不应有这个文件；
  - P 的单元目录里应有 `work/tenant-lock-pulse/process.log`。
- **写入 evidence**：把两次 `compare` 的输出、三个 `report.json`，以及运行 ID 和提交号，作为 NX-052 的 evidence 写入，类型为 `test_report`。
- 如果 `LOAD_WINDOW` 的夜间窗口不适合，可按车道运行手册设置窗口变量。三格合计约 1 小时，再加上三次模板构建的时间。

### 11.5 改动的脚本

| 脚本 | 改动 |
|---|---|
| `scripts/load/tenant_fk_state.py` | 登记 NX-052 的全部外键批次，并加上 `all` |
| `scripts/load/stack.py` | 单元级的 `NEXLOOP_LOAD_TENANT_FK_DROP_CELL` 钩子，默认关闭 |
| `scripts/load/nx052_e2e.py`（新增） | 生成状态矩阵；比较两次运行的报告 |

原有的模板级 `NEXLOOP_LOAD_TENANT_FK_DROP` 钩子和锁脉冲钩子保留，默认关闭。

测试在 `tests/test_nx052_e2e.py`，覆盖以下几点：

- 生成的状态矩阵能通过 `matrix.py` 的校验，并且只改了 C4-low 这一格；
- 比较逻辑：比值、门槛判定，以及缺少某项指标时判为不通过；
- 命令行的退出码；
- 每个 NX-052 外键迁移都登记成了批次；
- `all` 在一次性 PG 上删掉的正好是 NX-052 的全部租户外键，更早的外键保留；
- 只有设置了环境变量时，`stack.py` 才删约束。

## 12. NX-052 的完成定义与剩余项

### 12.1 完成定义

1. **契约**：Claim 的 `tenant_id` 是 uuid，并且有 `mode` 以及 mode↔world 条件（1a）。已合入。
2. **租户根**：`control.nexloop_tenants` 有 uuid 格式的 CHECK（1b，0157）。已合入。
3. **测试租户**：统一使用 `tests/support/tenants.py` 中的常量，守护测试 `test_tenant_constants` 防止旧字面量回来。已合入。
4. **租户外键**：每张带 `tenant_id` 的表，必须满足以下三者之一：
   - 有已 VALIDATE 的租户外键；
   - 满足链式判据（§7.3）；
   - 登记在 `EXCEPTION_REASONS` 中，并写明理由。
   
   由 `tests/test_tenant_fk_pg.py::test_every_table_with_tenant_id_has_a_tenant_path_or_a_recorded_exception` 断言。这项断言要求 s5l（第 4 批）、s5m（第 4b 批、第 5 批和收尾提交）都合入后才成立。只看 `nx052-closure` 这条分支时，它缺第 4 批，会失败。
5. **租户行锁**：配置发布和初始身份创建取 `FOR NO KEY UPDATE`（0162）。已合入。
6. **语句级测量**：每批在本机按 §0.3 的方法测量，结果记在各节中。
7. **端到端测量（v2）**：按 §11 在 200 客户模板的 C4-low 格（0.1/s）执行，K 对 N、P 对 N 两组比较都在判据内，且门槛判定通过。

### 12.2 剩余项

- **只剩第 7 项**，即 §11 的 v2 端到端测量（三格，约 1 小时加三次模板构建），由调度员在 CI 主机执行。执行完并把 evidence 写入 NX-052 后，NX-052 即可标为 done。
- 如果某项比较超出判据，处理方式是把该批中最频繁写入的表改成靠外键链或例外覆盖，或者拆出这张表单独处理，再重新测量。这要另行决定，不在本任务内预设。

### 12.3 以后新增表的规则

- 新增一张带 `tenant_id` 的表时，要么加租户外键，要么用 `tenant_id → tenant_id` 的复合外键引用一张已有租户路径的表。
- 如果它属于 vendored 核心，就把它加入 `EXCEPTION_REASONS` 并写明理由。
- 三者都不满足的，完整性断言会失败。

### §11.4 N 状态失败的排查与修复（2026-10-13）

- **现象**：CI 主机上的 N 状态（`NEXLOOP_LOAD_TENANT_FK_DROP_CELL=all`，检出 e5e7ebd，catalog 0186，C4-low，200 客户）在格开始后约 0.8 s 结束，`aborted=runner_error`，没有任何异常信息。
  - `cell.json` 里没有 `login_rate_policy`，说明失败发生在 `Stack(...)` 构造期间，即 `stack.py` 的外键删除这一步或它之前。
  - 原因是 `scripts/load/matrix.py` 的 `Cell.execute` 用 `except Exception` 吞掉了异常，只留下 `runner_error`。
- **核对批次清单**：在当前 catalog（main 22ff9c3）bootstrap 出的库上：
  - 9 个 NX-052 批次文件列出的 134 个约束全部存在，没有重名；
  - `tenant_fk_state.drop(['all'])` 删除 134 个，无报错；
  - 指向租户表的外键共 142 个，其余 8 个是 NX-052 之前就有的（批次本来就不含）。
  
  0178–0186 没有改动这些约束。用 5 个客户的模板、单格、`--timing-scale 0.05` 在本机复现 N 状态：格正常完成，删除 134 个。相关文件（stack.py、tenant_fk_state.py、matrix.py、load_template.py、迁移目录）在 e5e7ebd 与 22ff9c3 之间没有差异。**CI 上的失败在本机无法复现，原因还没有定位。**
- **修复（让下一次失败自带证据）**：
  - `matrix.py`：格中止时在 `cell.json` 写 `error`，内容为异常类型链（含 PostgreSQL 的 SQLSTATE）和 `文件:行:函数` 形式的调用栈，不写异常消息、原文和 ID（D8）；`matrix.log` 同时打印一行。另写 `tenant_fk_state`，记录删除了哪些批次、多少个约束，以及缺失的约束。
  - `tenant_fk_state.drop`：先核对所有约束都存在，缺任何一个就抛 `TenantFkMismatch`，一个都不删；然后在一个事务里全部删除，不会删到一半。
  - `stack.py` 把缺失约束的标识（`表.约束名`，只有模式名，没有数据），或"未知批次名"，写入 `tenant-fk-state.json`。
- **测试**：
  - `tests/test_load_matrix.py::test_an_aborted_cell_records_the_error_types_and_frames_without_text`；
  - `tests/test_nx052_e2e.py::test_a_mismatched_batch_drops_nothing_and_the_cell_records_what_is_missing`。
- **下一步**：在 CI 主机上用本修复重跑 N。新的 `cell.json` 的 `error` 与 `tenant_fk_state` 会直接给出失败的类型和位置。

### §11.4（续）"全部缺失"的原因与修复（2026-10-14）

- **CI 现象**：run 20261013T161753Z-fa9a71c（main ec5ffc4，200 客户，C4-low，relay 2）。
  - `cell.json` 的 `error` 是 `tenant_fk_state.TenantFkMismatch`，调用栈 `matrix.py:137 → stack.py:63 → tenant_fk_state.py:60`。
  - `tenant_fk_state.missing` 列出了全部 134 个约束。
- **原因（b，已在本机复现）**：
  - 核对查询按 `conrelid::regclass::text` 取表名，而 regclass 转成文本时，只要表所在的 schema 在当前会话的 `search_path` 上，就会省略 schema。
  - 批次清单里是带 schema 的名字（`control.…`、`ontology.…`），于是在 `search_path` 含这些 schema 的会话里，一个也对不上。约束本身都在。
  - 仓库里没有任何地方设置角色级或库级的 search_path，CI 主机的会话很可能从环境里拿到了它，例如 `PGOPTIONS`、角色或服务器设置。
  - 删除语句本身用带 schema 的名字，不受影响。
- **复现**（与 CI 主机相同的路径）：`build_template.sh --consumers 3`，然后 `matrix.py --cells C4-low --timing-scale 0.05 --relay-processes 2`，设 `NEXLOOP_LOAD_TENANT_FK_DROP_CELL=all`。
  - 正常环境下格完成，删除 134 个约束。
  - 加上 `PGOPTIONS='-c search_path=control,ontology,runtime,authz,public'` 后，得到与 CI 完全相同的错误、调用栈和缺失清单。
- **排除的另外两种可能**：
  - (a) 连到别的库：克隆的连接串固定为 `dbname=postgres user=nexloop_bootstrap`（`tests/load_template.py` 的 `Clone`），本机的同路径运行与 CI 用的是同一个连接串。
  - (c) 模板没有外键：同一模板在不设 search_path 时 134 个都在。
- **修复**：
  - `scripts/load/tenant_fk_state.py` 改从 `pg_constraint` 关联 `pg_class`、`pg_namespace` 取带 schema 的名字，不再依赖 search_path。
  - 核对失败时，在 `tenant-fk-state.json` 和 `cell.json` 的 `tenant_fk_state.cluster` 里额外记录：当前库名、会话 search_path、服务器版本号，以及指向租户表的外键数量。只有标识和数字。
  - 修复后用上面会失败的条件重跑：格完成，删除 134 个。
- **测试**：`tests/test_nx052_e2e.py::test_the_key_check_does_not_depend_on_the_session_search_path`。在把这些 schema 放进 search_path 的会话里：
  - 先断言 regclass 文本确实省略了 schema（陷阱本身）；
  - 再断言核对能找到全部约束、删除成功；
  - 真实缺失时，仍报出缺失项和 cluster 信息。
- **未解释的部分**：第一次 CI 失败（e5e7ebd，修复前的代码直接执行删除，没有核对步骤）不会受 search_path 影响，原因仍未知。下次在 CI 主机上重跑 N 时，如果再次失败，`cell.json` 的 `error` 与 `tenant_fk_state.cluster` 会给出位置和环境。

### §11.4（再续）根因：模板已经没有 NX-052 外键（2026-10-14）

- **CI 现象**：run 20261013T191340Z-2199b39（fa9a71c + 3495aed）。`TenantFkMismatch`，出错位置 `tenant_fk_state.py:76`，缺 134 个；cluster 为 `database='postgres'`、`search_path="$user", public`、`tenant_foreign_keys=8`。
- **"连错了库"的可能可以排除**：每一格的克隆是**独立的 PostgreSQL 集群**（`tests/load_template.py` 的 `Clone`：复制模板 PGDATA，用自己的 socket 目录和端口启动），里面唯一使用的库就是 `postgres`。模板本身也是在测试夹具的 `postgres` 库里建的（`tests/conftest.py:25`），整条路径上没有别的库名。所以 `database='postgres'` 就是这一格的克隆库。
- **根因（已在本机按 CI 签名完整复现）**：`tenant_foreign_keys=8` 恰好是 NX-052 之前就有的租户外键数（142 − 134）。也就是说，这次的模板在构建时已经执行过模板级删除 `NEXLOOP_LOAD_TENANT_FK_DROP`（`build_template_hold.py:156`），或者复用了这样构建的模板，克隆里自然一个 NX-052 外键都没有。
  - 本机用 `NEXLOOP_LOAD_TENANT_FK_DROP=all build_template.sh` 建模板，再设 `NEXLOOP_LOAD_TENANT_FK_DROP_CELL=all` 跑格，得到与 CI 逐项相同的结果：错误类型、调用栈、134 个缺失、库名、search_path、8 个外键。
  - 这也解释了第一次失败（e5e7ebd）：那时的代码直接执行 `alter table … drop constraint`，约束不存在时立即报错。
  - search_path 那一条（3495aed）是真实存在的隐患，但不是 CI 上这次的原因。
- **修复**：
  - `stack.py`：格级删除之前读取模板的 `template.json`。如果 `tenant_fk_dropped` 存在，就以 `TemplateKeysDropped` 拒绝，不碰克隆；`tenant-fk-state.json` 和 `cell.json` 记 `refused: template_keys_already_dropped` 以及模板删过的批次。
  - `run_on_lane.sh`：预检时如果 `NEXLOOP_LOAD_TENANT_FK_DROP` 与 `NEXLOOP_LOAD_TENANT_FK_DROP_CELL` 同时设置就拒绝。两者是不同的测量方式，不能叠加。
  - `TenantFkMismatch` 的 cluster 信息中带有库名（3495aed 已有），连到没有这些外键的库时会直接显示出来。
- **验证（本机，CI 同路径）**：
  - 用模板级删除过的模板跑 N：格以 `TemplateKeysDropped` 拒绝，记录 `template_keys_already_dropped`。
  - 用正常模板跑 N：格完成，删除 134 个。
- **测试**：
  - `test_a_template_built_without_the_keys_is_refused_with_that_cause`；
  - `test_a_database_without_the_keys_reports_its_name_and_the_right_one_drops_all`：连到另一个库时报出该库名与 0 个租户外键；连到克隆库时删除 134 个。
- **CI 主机上的处理**：
  - 重跑 N 时只设 `NEXLOOP_LOAD_TENANT_FK_DROP_CELL=all`，并确认环境里没有 `NEXLOOP_LOAD_TENANT_FK_DROP`；
  - 不要用 `--resume` 复用模板级删过外键的模板，检查方法是看模板目录的 `template.json` 有没有 `tenant_fk_dropped`。
