# tenant_id / world_id 类型盘点

基线：main `3ad4061`（迁移至 0073）。方法：在测试 PG 上 fresh bootstrap 后查 `information_schema`/`pg_constraint`/`pg_proc`；静态检索 Python、TypeScript 与 `packages/contracts`。只读盘点，未改任何代码。

## 1. 现状

### 1.1 PostgreSQL（权威存储）

| 项 | 结果 |
|---|---|
| 含 `tenant_id` 的基表 | 98 张，全部 `text` |
| 含 `world` 的基表 | 67 张，全部 `text`；没有任何表叫 `world_id` |
| 根表 | `control.nexloop_tenants(tenant_id text, status, authority_revision)` |
| tenant 外键 | 仅 6 张 control 表引用 `nexloop_tenants`：action_definitions、effect_contexts、function_definitions、configuration_publications、initial_identity_allowances、initial_identity_receipts。其余 92 张（含 ontology.objects、Message、Claim、runtime.jobs、authz 事实）只靠 RLS（`tenant_id=current_setting('eios.tenant_id')`）和定义器内的身份快照约束 |
| tenant 格式约束 | **无**：没有任何 CHECK 限制 tenant_id 为 uuid 或其它格式 |
| world 约束 | 14 条 CHECK，其中 10 条为 `world='real'`（会话/消息/原生入站/效果等只支持 real），其余为 data_mode 组合、索引种类相关 |
| SQL 函数参数 | 165 个 authz/control/runtime/ontology 函数参数名含 tenant/world，全部 `text`；租户实际取自签名声明并由 `nexloop_service_identity_snapshot` / 浏览器身份快照复核，不信任参数 |

### 1.2 Python

- 上游 vendored EIOS：`tenant_id: str` 约 330 处；授权事实模型用 `CanonicalIdentifier`（有界规范标识，非 uuid）20 处，策略注册表用 `RegistryIdentifier` 10 处；身份模型只做非空校验。**EIOS 内核不要求 uuid。**
- NexLoop 层：
  - `runtime_activation._command` / `context_pack`（Context Pack `bindings.tenant_id` format uuid）/ agent-host `runtime-adapter.ts`、`context-input.ts`：**强制 uuid**。即 Run/Context/Runtime 链只接受 uuid 租户。
  - `claim_matching.py` 生成 `ontology-mutation` / `candidate-definition` 契约对象时直接用会话租户，契约要求 uuid → NX-020/045 实际只能在 uuid 租户下运行（其测试用 `uuid4()` 租户）。
  - `browser_test_profile.TENANT='nexloop-sandbox'`、`sandbox_profile` 与 54 个测试文件使用 `synthetic-a` 等非 uuid 租户；31 个测试文件使用 uuid 租户。两类租户在同一 DB 结构下都能工作，直到触碰 Run/契约边界。
- 生成的契约模型 `nexloop_eios/contracts.py`：8 个契约 `tenant_id` 为 uuid，`Claim` 为非空字符串。

### 1.3 packages/contracts

| 契约 | tenant_id | world_id |
|---|---|---|
| action-intent、candidate-definition、context-manifest、event-envelope、ontology-mutation、review-decision、run-command、evolution-candidate | `string, format: uuid` | `string, minLength 1`（evolution-candidate 无 world_id），配合 mode↔world 条件 |
| claim（NX-019） | `string, minLength 1` | `string, minLength 1`，无 mode |

## 2. 不一致点

1. **存储接受任意文本，Run/契约边界要求 uuid。** 非 uuid 租户能受理消息、提取 Claim，但无法发起 Run（`runtime_activation` 拒绝），也无法产出 ontology-mutation / candidate-definition（契约拒绝）。失败发生在链路中段而不是租户创建时。
2. **Claim 契约是 text，其它 8 个契约是 uuid。** 同一租户的 Claim 与其派生的 mutation/candidate 在契约层类型不同；Claim 契约能通过的 `synthetic-a` 在下游契约中非法。
3. **world 命名不一致**：DB 列叫 `world`，契约叫 `world_id`；DB 对会话族只允许 `real`，契约允许任意非空字符串并用 `mode` 约束。Claim 契约没有 `mode`。
4. **外键覆盖不全**：92 张表的 tenant_id 不引用 `nexloop_tenants`；删除/停用租户无法由约束保证级联语义（目前依赖“租户不删除、只停用”和身份快照的 status 检查）。
5. **测试租户双轨**：`synthetic-a`/`nexloop-sandbox` 与 uuid 并存，掩盖了问题 1（部分 PG 测试永远走不到 uuid 边界）。

## 3. 统一方案（建议）

**目标：tenant_id 统一为“小写规范 uuid 文本”，类型保持 `text`，不改列类型。**

理由：列改 `uuid` 类型要重写 98 张表、165 个函数签名、全部 RLS 策略与 `current_setting` 比较（`text` GUC 与 `uuid` 列比较需显式转换），以及上游 EIOS 的 `str` 模型；收益仅是存储紧凑。用 CHECK + 契约 pattern 达到同样的格式保证，代价低一个数量级，并且与 EIOS 内核的 `str` 模型兼容。

步骤：
1. **契约**：Claim 契约 `tenant_id` 改为 `format: uuid`（与其它 8 个一致），增加 `mode` 与 mode↔world 条件，保持 `world_id` 命名。重新生成类型。
2. **租户根**：`control.nexloop_tenants` 加 `check(tenant_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')`（新迁移，`NOT VALID` 后 `VALIDATE`，以便已有部署先审计）。租户创建入口（compose_bootstrap / 初始身份 / 可信配置）在 Python 侧同样校验。
3. **其余表**：不逐表加 CHECK；给 92 张缺外键的表补 `foreign key(tenant_id) references control.nexloop_tenants` 即可继承格式保证（大表用 `NOT VALID` + 后台 `VALIDATE CONSTRAINT`）。可按 schema 分批，每批一个迁移。
4. **测试**：`synthetic-a`/`synthetic-b`/`nexloop-sandbox` 改为固定 uuid 常量（如 `tests/support/tenants.py` 中 `TENANT_A/TENANT_B`），一次机械替换；`browser_test_profile.TENANT` 同步。
5. **world**：保持 DB 列名 `world`、契约名 `world_id`，在文档中固定映射；不做重命名。

## 4. 迁移代价估计

| 项 | 规模 | 风险 |
|---|---|---|
| Claim 契约改 uuid + mode | 1 schema、1 示例、生成物、claim_store 投影加 mode | 低；需同步 0073 投影函数（新迁移 create or replace） |
| nexloop_tenants CHECK | 1 迁移 | 低；已有非 uuid 租户的部署会在 VALIDATE 失败——这是期望的发现 |
| 92 张表补 tenant 外键 | 3–5 个迁移（按 schema） | 中：外键检查增加写入成本；`ontology.objects`、`runtime.jobs`、`authz.nexloop_authority_facts` 是热点表，需要在测试中量化；外键也会阻止“先写事实后建租户”的配置顺序，需核对 0050 manifest 顺序 |
| 测试租户替换 | 54 个测试文件 + 若干 fixture | 低但面广；纯机械，可一次提交；需全量 CI |
| 列类型改 uuid（不建议） | 98 表、165 函数、全部 RLS、上游模型 | 高；不建议 |

已发布迁移 0001..0073 不改；全部为新增迁移。真实部署（hengce/sice）若存在非 uuid 租户，需要负责人决定改名方案后才能 VALIDATE。

## 5. 待决定

- 是否采纳“uuid 文本 + CHECK/外键”而不是列类型迁移。
- Claim 契约改 uuid 是否与第 4 步测试租户替换同批进行（建议同批，否则 Claim 相关 PG 测试需改用 uuid 租户）。
- 补外键是否放在 S4 之前。
