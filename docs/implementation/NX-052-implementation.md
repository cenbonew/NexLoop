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

- 一个迁移，临时号 0300 起，先 `not valid` 再逐表 `validate`。
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

## 7. 批次 3c 实现（0300，临时号）

### 7.1 裁定（调度员 2026-10-12）

- 采用方案 A：根表直接加租户外键；已经通过包含 `tenant_id` 的复合外键引用根表的子表，不再单独加。
- 端到端测量放到 1000 客户的整轮窗口，分三个状态：0162 之后、3c 之后、3c 之后并发配置发布（每 5 秒一次）。本机只做语句级测量。

### 7.2 迁移

0300 给 runtime 下 11 张表加 `nexloop_<t>_tenant_fk`，做法仍是先 NOT VALID 再 VALIDATE；回滚时新开迁移 `drop constraint if exists`。这 11 张表是：

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
