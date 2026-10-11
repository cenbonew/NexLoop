# NX-052 租户 ID 规范化：实施说明

依据：`docs/implementation/tenant-id-audit.md`（盘点与方案）、`planning/tasks.json` NX-052 deliverable。目标是让 tenant_id 统一为“小写规范 uuid 文本”，列类型仍为 `text`。本说明的迁移号 0230、0231 是临时号，合入时由调度员重新编号。

## 0. 设计

### 0.1 分批计划

分批的依据是两点：表的写入热度，以及外键检查落在哪条事务路径上。冷表先做；热点表单独成批，并且先测量再决定。

| 批次 | 内容 | 写入热度 | 状态 |
|---|---|---|---|
| 1a | Claim 契约 tenant_id 改为 uuid，增加 mode；Claim 投影补 mode（0230）；测试租户常量统一 | — | 本分支 |
| 1b | `control.nexloop_tenants` 加 uuid 格式 CHECK（0231，先 `NOT VALID` 再 `VALIDATE`） | 冷：租户只由可信配置 manifest 写入 | 本分支 |
| 2 | control、authz 配置类表补 `foreign key(tenant_id) references control.nexloop_tenants`：浏览器应用、成员、限流策略、服务凭据、告警与工作台配置等 | 冷：配置写入 | 只写计划 |
| 3 | ontology 业务表（除 `ontology.objects` 外）以及 Message、Claim、Conversation 族 | 中：每条消息或抽取会写几行 | 只写计划 |
| 4 | runtime 队列与运行族（除 `runtime.jobs` 外）：Run、Context、模型请求、效果、成本、可观测 | 中到高 | 只写计划 |
| 5 | 热点表：`ontology.objects`、`runtime.jobs`、`authz.nexloop_authority_facts` | 高 | 只写计划，以测量结果为前提 |

已有外键的 6 张 control 表（audit §1.1）不动。审计里统计的“92 张缺外键的表”以基线 0073 为准，第 2 批开工前要在当时的 main 上用 `pg_constraint` 重新列一遍。

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
   - 外键触发器耗时：开启 `track_functions='pl'` 后，`pg_stat_user_functions` 中 `RI_FKey_check_ins`、`RI_FKey_check_upd` 的 `total_time`；
   - 端到端：dispatch 时延的 p50 和 p95，沿用 NX-031 的口径；
   - 锁：`pg_locks` 采样中对 `control.nexloop_tenants` 的 `ROW SHARE` 和 `KEY SHARE` 等待。外键检查会对父行加 `FOR KEY SHARE`，因此要确认它不与租户行的 `authority_revision` 更新（`FOR UPDATE`）互相阻塞。
3. **判据**（建议值，由调度员确认）：热点表写入语句的中位耗时增加不超过 10%，端到端 p95 增加不超过 5%，并且出现与 authority_revision 更新的锁等待时不得超过 NX-031 的告警阈值。超出时，该表改用“触发器只校验格式”或继续依赖身份快照，并写入 ADR。
4. **重点关注租户行锁**：`update control.nexloop_tenants set authority_revision=…` 不改键列，取的是 `FOR NO KEY UPDATE`，与外键检查的 `FOR KEY SHARE` 不冲突。但 0050 中有显式的 `select … for update`（第 86 行、第 246 行），它取的是 `FOR UPDATE`，会与 `FOR KEY SHARE` 冲突：配置发布期间，带该租户外键的业务写入会排队等待。测量时必须并发跑“配置发布 + 业务写入”。如果等待超出判据，第 5 批之前需要先把这两处改成 `for no key update`，这需要新迁移包装，并另行评审。

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
- 交接副本 `docs/handoff/contracts/claim.schema.json` 及其示例同步修改，因为 `test_claim_contract` 要求这两份与 `packages/contracts` 中的版本逐字节一致。

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
