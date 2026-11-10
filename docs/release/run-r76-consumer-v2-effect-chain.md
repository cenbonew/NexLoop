# R76：stage 7a4dab53 → c58f723e 完整部署单（批次 152b–162，迁移 0434–0447；含 Consumer v2 效应链恢复，ADR-046）

- **范围**：stage 当前运行 7a4dab53；本次一次部署到 c58f723e，包含批次 152b、153b、154b、155、156、157、160、161、162（158/159 已被 160 取代）。
- **起因**：stage 2026-11-09 04:53Z 发布 Consumer v2 后，relay 对所有已有顾客的回复都失败在 effect_bind（`registrar schema scope unavailable`），customer-5（Consumer v1）正常。修复在批次 161/162（ADR-046 A′ 与 Q5′）。其余批次（NX-062 M2a–M5c、D23-B、NX-067）随同一次部署上线。
- **执行者与判定者**（每步表中另行标注）：
  - 迁移、publish、register、角色清单、配置发布、门禁、promote 和只读核对：**执行者**调度员（DATA_HOST / APP_HOST，文件凭据）。
  - 验证消息与 erase dry-run：**执行者**负责人（负责人终端）。
  - 每步结果：**判定者** L6（依据调度员提交的输出存档）。负责人确认整单后才开始。
- **通用约束**：
  - 所有 SQL 只读（下文 `ro` 指调度员现有的只读查询入口）；不手工改任何业务表。
  - 主机只写角色名（APP_HOST、DATA_HOST、CI 主机）。
  - 口令从 Keychain 经 stdin 传入，不进 argv。
  - **任一步结果与预期不符就停下**，不进入下一步，把输出交调度员和 L6。

## 0. 迁移清单与业务数据影响（第 2 步之前核对）

原单写“迁移不改任何业务数据”，**这一说法不对，以本表为准**：0447 在迁移时会删除业务数据（owner 行）并写标记和审计。其余 13 个迁移只建表、函数、触发器、策略和授权，或写系统登记行，不改已有业务数据。

| 迁移 | 批次 / 任务 | 迁移时写什么 | 是否改已有业务数据 |
|---|---|---|---|
| 0434 nx062_machine_access | 152b NX-062 M2a | 新表 machine clients/keys/capabilities/read_verbs（空）；service identity snapshot 改名并包一层 | 否 |
| 0435 nx062_machine_lifecycle | 152b M2b | `control.nexloop_governed_capabilities` 登记 6 个能力（系统登记行） | 否 |
| 0436 nx062_service_snapshot_grants | 152b M2a 修正 | 只移交函数 EXECUTE 授权 | 否 |
| 0437 nx062_machine_realm | 153b M3 | 新表速率策略、桶、计数（空）；**没有速率策略修订就是关闭**（见第 5 步） | 否 |
| 0438 action_input_tenant_narrow | 154b D23-B | 新表 shadow failures（空）；替换函数体 | 否 |
| 0439 nx062_machine_reads | 155 M4a | 新表 client grants、read counts（空）；登记 read verbs、machine capabilities、governed capabilities | 否 |
| 0440 nx062_machine_reads_b | 155 M4b | 登记两个 read verb 和 capability；函数包一层 | 否 |
| 0441 nx062_machine_authority | 156 M5a | 只有函数和授权 | 否 |
| 0442 nx062_machine_writes | 160 M5b | 先校验 0348 入口函数体的 sha256（不符即迁移失败）；新表 machine write capabilities、machine intents（空）；入口函数包一层 | 否 |
| 0443 nx062_object_notes | 160 M5c | 新表 object notes、note limits（空）；登记 `object.add_note` 能力 | 否 |
| 0444 nx062_tenant_hygiene | 160 | 只替换两个函数体 | 否 |
| 0445 nx067_oidc_gaps | 160 NX-067 | 新表 logout jti、group mappings、role ceiling events（空）；`workbench_external_identities` 加可空列 `role_ceiling`（不回填，null 即不设上限）；登记 `set_sso_group_mapping` 能力 | 否（加列，不改已有值） |
| 0446 effect_scope_successor_proof | 161 ADR-046 | 新表 `control.nexloop_effect_scope_proofs`（空）；`nexloop_real_effect_tables` 登记一行；替换 0179/0183 两个函数体 | 否 |
| **0447 erasure_owner_residue** | 161 | 新守卫触发器；**对每个租户执行一次 `control.nexloop_erasure_owner_residue_settle`**：对已完成、不在保留期、登录主体无非空授权的删除，补写 `control.nexloop_consumer_erasures.grants_cleared`（只补 null），**删除 `control.nexloop_consumer_owners` 中的残留 owner 行**（0392 的入场券假名化触发器照常执行），每个被清理或保留的 Consumer 写一条 `runtime.audit_events`，每个租户再写一条 run 审计和一条 NOTICE | **是**：stage 预期删除 customer-4 的 owner 行（1 行）、写 grants_cleared、写 2 条审计 |

- **执行者**：调度员。**判定者**：L6。
- 迁移前先确认 stage 的 `control.schema_migrations` 尾部是 7a4dab53 对应的最后一个迁移（0433）。尾部不同就停下。

## 1. 各批的部署期动作

依据：各批的 run sheet（`run-nx062-machine-access.md` §1、§4.1，`run-nx065-oidc-acceptance.md` §2），设计文档（`docs/design/nx067-oidc-group-roles-and-disable-sync.md`），以及 `git diff 7a4dab53 c58f723e -- deploy/` 的结果。

| 批次 / 任务 | 新 Action 需 publish | 新 compose 变量或 secret | configurator 发布的配置 | 角色清单 | 其他 |
|---|---|---|---|---|---|
| 152b NX-062 M2a/M2b | 6 个：`nexloop.machine_client.register/deactivate/activate`、`nexloop.machine_key.issue/rotate/revoke` | 无 | 无 | 涉及（owner 新增 6 个） | — |
| 153b NX-062 M3 | 无 | **有**：secret `api_machine_key_peppers`（`secrets/api/machine-key-peppers`，api-1/api-2 新参数 `--machine-key-peppers-file`） | **有**：`machine-rate-limits.v1.json`（不发布即关闭） | 无 | 告警 v35；Caddyfile 新增 `/machine/*` |
| 154b D23-B | 无 | 无 | 无 | 无 | 告警 v36（v35 加 shadow failure 规则） |
| 155 NX-062 M4a/M4b | 2 个：`nexloop.machine_client.grant`、`.revoke_grant` | 无 | 无 | 涉及 | — |
| 156 NX-062 M5a | 无 | 无 | 无 | 无 | 只有迁移 0441 |
| 157 NX-065 准备 | 无 | 无 | 无 | 无 | 只有文档 |
| 160 NX-062 M5b/M5c | 1 个：`nexloop.object.add_note` | 无 | **有**：`machine-note-limits.v1.json`（不发布时机器备注 429，人写不受影响） | 涉及 | — |
| 160 NX-067 | 1 个：`nexloop.workbench_member.set_sso_group_mapping` | 无 | 无 | 涉及（owner 新增 1 个） | provider 文件可选 `max_session_seconds`（缺省 28800）；本单不启用 OIDC，启用另按 run-nx065 |
| 161 ADR-046 | 无新 Action | 无 | 无 | 无 | stage-principals follow 声明（register 派生）；告警 v37 |
| 162 Q5′ | 无 | 无 | 无 | 无 | stage-principals 增加 `Consumer.create` follow 声明 |

**合计（以 c58f723e 的文件为准）**：
- business-actions v56 → v60，capabilities v9 → v13，新增 10 个 Action。
- 工作台角色清单 v53 → v57：owner 108 个 Action，operator 14 个。
- API 路由清单 v12 → v18：随镜像生效，不需要单独发布。
- 告警规则 v34 → v37：累积版本，只发布 v37。
- 一个新 secret，两份 configurator 配置（速率策略、备注上限），Caddyfile 一处新增。

**10 个新 Action 与 Consumer v2（L6 C1）**：
- 逐个核对 `business-actions.v1.json`：10 个 Action 的 `object_type` 和 change_scope **都**是 `Consumer:1`。这是业务 Action 的惯例写法，105 个业务 Action 里有 91 个这样写。
- stage 上 Consumer 已是 v2，而这 10 个 Action 是在 F3 发布之后才发布的，所以没有 `:2`，只能以 `:1` 执行。A′ 只覆盖效应链，不覆盖它们。
- 它们的处理函数不读写任何 Consumer 对象，Consumer 版本不进入校验：
  - 机器客户端、密钥、授权：写 `authz.nexloop_machine_*` 表；
  - SSO 组映射：写 `control.nexloop_workbench_oidc_group_mappings`；
  - 对象备注：只作用于 Commitment 和 CommercialRecord（0443 的 `type_name` check），备注所挂对象可以指向 v2 顾客。
- 本地证明：`tests/test_r76_catalog_publish_after_consumer_v2_pg.py`。在全部 Consumer 为 v2、库中只有这 10 个 Action 的 `:1` 时，owner 依次以 `:1` 执行了全部 10 个，每个都返回 outcome_id。执行顺序：register、issue、rotate、revoke key、grant、revoke_grant、deactivate、activate、set_sso_group_mapping，最后在指向 v2 顾客的 Commitment 上加备注。Consumer 的类型行、对象版本、`:2` 后继和 `:2` 授权前后不变。
- 不需要扩大 follow。

**system-catalog-names 与 world-model-glossary 的生效方式（L6 C2）**：
- **`deploy/configuration/system-catalog-names.v1.json`**（新增 10 个 Action 名）：不单独发布，也不由 configurator 写入数据库。
  - 它是系统目录的名单清单，`nexloop_eios.system_catalog` 与 `tests/test_catalog_manifests.py`、`test_system_catalog_pg.py`、`test_workbench_roles.py` 用它核对：目录、角色清单与 business-actions 三者的名单必须一致。
  - 新名字随第 6 步的 Action publish（定义写入数据库）和第 7 步的角色清单真正生效。
- **`deploy/configuration/world-model-glossary.v1.json`**（新增一条 `nexloop.workbench_member.set_sso_group_mapping` 的人工译名）：只影响显示，由 configurator 经 `scripts/deploy/world_glossary_apply.py` 发布，不随镜像生效。
  - 这次改动没有提升 `manifest_version`，仍为 1。0374 的 `control.nexloop_world_glossary_publish` 规定：同一版本号换了文本，就以“published with another text”拒绝（40001）。
  - 所以 stage 若已发布过词表 v1，**本单不发布词表**。这条 Action 的中文名由 `world_glossary_translator` 机翻补上，或暂时为空，不影响任何功能。
  - 要让人工译名上 stage，需要另做一个把 `manifest_version` 提到 2 的改动，另行组批。
  - 只读核对 stage 是否发布过：`select manifest_version, manifest_sha256 from control.nexloop_world_glossary_publications where tenant_id='<TENANT_ID>'`。

## 2. 步骤

| 步 | 内容 | 执行者 | 预期（不符即停） |
|---|---|---|---|
| 1 | **备份**：`sudo -n systemctl start nexloop-pg-diff.service` | 调度员 | 本次备份成功（unit 结果 success、备份时间晚于开始时间） |
| 2 | **pepper secret**（部署前）：已有密钥树用 `python scripts/deploy/stage_secrets.py machine-key-peppers --out <树>` 补加（不打印值，不覆盖已有文件）；按现有流程装到 APP_HOST（0600，属于 api）；按 run-nx062 §2 立即写入钥匙串备份 | 调度员 | 文件存在；摘要一致；钥匙串条目 `present`。缺这个文件，api 容器起不来 |
| 3 | **compose 与 Caddyfile**：按 release manifest 渲染 c58f723e 的 `compose.app.example.yaml`（api secret 列表新增 `api_machine_key_peppers`）和 `Caddyfile.template`（`/machine/*`）。compose 改动走 feature 分支和 PR，需要负责人 ack-policy | 调度员 | `deploy_render` 输出与 manifest 一致；网关仍没有 `log` 指令 |
| 4 | **部署镜像并执行 migrate**（0434–0447） | 调度员 | 见下方“第 4 步核对” |
| 5 | **配置发布**（configurator）：`python -m nexloop_eios.machine_access publish-rate-policy --policy deploy/configuration/machine-rate-limits.v1.json --database-url-file <configurator DSN 文件>`；`python -m nexloop_eios.machine_access publish-note-limits --policy deploy/configuration/machine-note-limits.v1.json --database-url-file <configurator DSN 文件>`；`alert_rules_apply.py --manifest deploy/configuration/alert-rules.v37.json` 先 `--check`，再正式执行 | 调度员 | 两条策略各有一个修订；`--check` 显示 version 37、116 条规则；下一次评估的 `rules_version` 为 37 |
| 6 | **Action publish**：`stage_secrets.py publish` 先 dry-run | 调度员 | 计划**只**新增第 1 节的 10 个 Action（business-actions v60、capabilities v13），同名同版本冲突 0。**另核**：stage 的 Consumer 已有 F3 发布的 v2（不在仓库清单里），dry-run 不得出现 Consumer 类型的新增或冲突；出现就停下。本地已用 `tests/test_r76_catalog_publish_after_consumer_v2_pg.py` 复现这一状态（不含这 10 个 Action 的目录 → 负责人 F3 发布 Consumer v2 → register），真实 `publish` 的 dry-run 只计划这 10 个，冲突为 0；正式 publish 前后 Consumer 的类型行、对象版本、`:2` 后继和 `:2` 授权逐字节不变；`workbench_roles --apply` 之后，owner 对 10 个 Action 都可达（已发布、已授权），并经受治理入口执行成功一个。确认后正式执行。未发布之前，工作台对这些 Action 返回 404 |
| 7 | **工作台角色**：`workbench_roles.py --roles deploy/authorization/workbench-roles.v1.json --members <私有成员文件> --apply …`（参数同 run-34 §2） | 调度员 | owner 授权 108 个，operator 14 个 |
| 8 | **register dry-run**：`stage_secrets.py register --dry-run --configurator-dsn-file …`（必须带 DSN，才会计算派生授权），输出存档 | 调度员 | 见下方“第 8 步预期” |
| 9 | **register**：去掉 `--dry-run` 后执行。执行前后各跑一次第 4 节的“非空授权事实”只读查询并存档 | 调度员 | 输出 `grants_added` 为 3，`grants_revoked` 为 0，`credentials_created` 为空，`extra_grants_outside_manifest` 与上次 register 相同（6，即 nexloop-sim-* 模拟主体的授权；L6 判第 9 步时修正）（register 的输出只给计数，没有名为 grants_removed 的字段，撤销计数即 `grants_revoked`）。前后两次查询的差集**恰好**是第 8 步的 3 条，没有消失的行。本地 `tests/test_consumer_v2_stage_path_pg.py` 用同一差集断言（测试库没有 R74，所以多一条 `claim_matcher → Consumer.edit:2`，共 4 条）。首次 register 若遇 `nexloop_tenants` 锁超时（`configuration_rejected`，无写入），可重试一次；仍失败则停 |
| 10 | **重启 api-1、api-2、gateway**，然后跑 **deploy gate**（与 R75 同款参数，`--before` 用当期阶段基线） | 调度员 | 见第 3 节。**gate 与 promote 分开执行，不串联** |
| 11 | gate 结果经 L6 判定之后，单独执行 **promote** | 调度员 | promote 成功；部署 SHA/digest 与测试过的 release manifest 一致 |
| 12 | **只读核对**（第 4 节） | 调度员执行，L6 判定 | 全部符合 |
| 13 | **0447 部署证据**（第 5 节） | 调度员、负责人执行，L6 判定 | 全部符合 |
| 14 | **三条验证消息**（第 6 节） | 负责人执行，L6 判定 | 每条 60 秒内收到回复 |

### 第 4 步核对

- `control.schema_migrations` 的尾部是 0447，共新增 14 行。
  - **编号说明（L6 C4）**：本单的迁移号是组批重编号后的名字。0442 是 `nx062_machine_writes`，0446 是 `effect_scope_successor_proof`；之前判词里的“0442（successor proof）”是 L7 分支上的临时号，已重编为 0446，二者不是同一个文件。
  - 按 c58f723e 的 `packages/eios-core/src/eios/migrations/catalog.json` 核对每一行的 checksum（只读）：`select version, checksum from control.schema_migrations where version>='0434' order by version`。预期与下表逐字相等，任一不等就停下：

    | 迁移 | sha256（c58f723e catalog） |
    |---|---|
    | 0434_nx062_machine_access | `7f59c87d5daae41771e73ad1f1828cd8261d67f69d77a1a643bce136f4f07a8e` |
    | 0435_nx062_machine_lifecycle | `58f0f491a8bd8e7205169fba4f75e848c4856fb60512c30daea21ed86aa371f3` |
    | 0436_nx062_service_snapshot_grants | `c8bad33a06c0c376b85a171adf6364ec79e0ba687a34ad0d5e68a66a52b489f8` |
    | 0437_nx062_machine_realm | `61f24649858187410e6926020585096fb2ca63c3ad8f6c0fc9d402b10eafb619` |
    | 0438_action_input_tenant_narrow | `696be73046bad147797a7d011ba4c6e1662af7d2d056e53181dc146a70901119` |
    | 0439_nx062_machine_reads | `daa6022e24e2f820afbfae4c8bc9302f66771d0bc156611e1b08402f5dc8bd54` |
    | 0440_nx062_machine_reads_b | `38369de8f57dbcbf429e83e98f9cd37945a3ba8dab15c862558da68052770e87` |
    | 0441_nx062_machine_authority | `1c7e864495d7112035e8473b64f0c723850577b2193181997567bddbecc15a95` |
    | 0442_nx062_machine_writes | `9184e995a97bf5ae05e2db26c46e8cf59c6f81406296f49d45ad18e99ed391e6` |
    | 0443_nx062_object_notes | `febf197456a6627f0449b20f6882d96bbb6cc158ea75dab6975790aac7300c01` |
    | 0444_nx062_tenant_hygiene | `7e1c6e08d0edc8b015754d4a840c5cf0fe050a7be63a2e8760609dbe0224cac0` |
    | 0445_nx067_oidc_gaps | `b947a23b07449622faed6d5c4127ce927a961baa53a3f18efb535c67a1f40425` |
    | 0446_effect_scope_successor_proof | `f02c7aef8278a383431cf22e2dde139ae10892dbc7d83d49b8c7de77306a6f47` |
    | 0447_erasure_owner_residue | `8bb7678984ec0d8a44ea5a4d7064ea0f63e554b4f834cddaa9b7b9ab3cc11e38` |
- `bootstrap.verify` 通过。
- 各服务 `/health/ready` 为 200，`reasons` 中没有 `schema_create_uncovered`。
- migrate 日志里 0447 输出每个租户一行 NOTICE：`erasure owner residue: tenant <T> settled <n> kept <m>`，存档。stage 租户预期 `settled 1 kept 0`。
- 0442 的入口函数体校验失败会使迁移整体回滚（错误码 55000）。出现就停下，不重试。

### 第 8 步预期

- `follow_latest.derived` 中本次新增恰好 3 条，来源都是 `stage-principals.v1.json` 中逐条的 follow 声明（FOLLOWABLE 白名单共 3 条）：
  - `relay_planner` 和 `role_planner` 的 `eios:action:nexloop.plan.bind_effect_context:2`（L6 Q5，registrar）；
  - `business_owner` 的 `eios:action:Consumer.create:2`（Q5′，负责人 2026-11-10 选定方案 1）。
  - 每条的 `scope_diff` 都是 `[{"type":"Consumer","from":1,"to":2}]`；purpose 都以 `(schema publication <id>)` 结尾。
- `refused` 为空。
- 还会列出 `claim_matcher → Consumer.edit:2`。它来自 `service-grants.v1.json` 的声明，R74 第 4 步已经写入，所以 dry-run 里会出现，但 register 不会重复新增。不算本次的新增派生。
- `business_owner → configure_control` 不派生：EffectControl 没有新版本，configure_control 没有 `:2`。
- 结果不同，或命令以 `follow_latest_refused` 退出，就停下。

## 3. 门禁

- **预期**：参照 R75，为 20/22。两项不通过，均属已知：
  - `real_dispatch_closed`：真实派发开关按 stage 现状开启；
  - `existing_services_unchanged`：CI 在 APP_HOST 上运行时会增加回环监听。
- 不通过的项不是这两项，或数量不是 2，就停下，不 promote。
- gate 与 promote 分两次执行，中间由 L6 判定 gate 输出。不写成 `gate && promote`。

## 4. 只读核对（调度员执行，L6 判定；把 `<TENANT_ID>` 换成实际值）

```sql
-- 迁移尾部与 checksum（与第 4 步核对表比对）
select version, checksum from control.schema_migrations where version>='0434' order by version;
-- 非空授权事实（第 9 步 register 前后各一次，比较差集）
select entity_key[1], entity_key[2] from authz.nexloop_authority_facts
 where tenant_id='<TENANT_ID>' and fact_kind='grants' and (payload->'grants') is distinct from '[]'::jsonb order by 1,2;
-- registrar :2 与 Consumer.create:2 的派生授权
select entity_key[1],entity_key[2] from authz.nexloop_authority_facts
 where tenant_id='<TENANT_ID>' and fact_kind='grants'
   and entity_key[2] in ('eios:action:nexloop.plan.bind_effect_context:2','eios:action:Consumer.create:2');
-- 对象版本分布（预期旧顾客为 2，customer-5 为 1；本单不改任何对象版本）
select schema_version,count(*) from ontology.objects where tenant_id='<TENANT_ID>' and world='real' and type_name='Consumer' group by 1;
-- 机器接入：速率策略和备注上限已发布；没有任何机器客户端（本单不登记）
select max(revision) from control.nexloop_machine_rate_policies;
select max(revision) from control.nexloop_machine_note_limits;
select count(*) from authz.nexloop_machine_clients where tenant_id='<TENANT_ID>';
-- 就绪自检与告警指标
select authz.nexloop_schema_create_coverage();
select runtime.nexloop_metrics_snapshot('<TENANT_ID>','real')->'relay_replies';
```

预期：派生授权共 3 行（relay_planner、role_planner、business_owner）；两条策略的修订号都为 1；机器客户端为 0；schema_create_coverage 没有未覆盖的类型。

## 5. 0447 部署证据（L6 R2）

1. **owner 行已清理、标记已写**（调度员执行，只读）：

   ```sql
   select e.consumer_id, e.grants_cleared,
          (select count(*) from control.nexloop_consumer_owners o where o.tenant_id=e.tenant_id and o.world=e.world and o.consumer_id=e.consumer_id) owners
     from control.nexloop_consumer_erasures e
    where e.tenant_id='<TENANT_ID>' and e.state='completed' order by e.completed_at;
   select count(*) from control.nexloop_consumer_owners where tenant_id='<TENANT_ID>' and consumer_id like '915ed71e%';
   ```

   - 预期：每行 `owners` 都为 0；customer-4（Consumer `915ed71e…`）的 `grants_cleared` 为 true；第二条查询结果为 0。
   - refusal-1 和 refusal-2 当初没有可追溯的 owner 行，`grants_cleared` 仍为 null，按 `run-erase-synthetic-consumer.md` 的事后核对处理。

2. **审计计数**（调度员执行，只读）：

   ```sql
   select action, payload->>'outcome' outcome, payload->>'settled' settled, payload->>'kept_grants_held' kept, created_at
     from runtime.audit_events
    where tenant_id='<TENANT_ID>' and action in ('nexloop.consumer_erasure.owner_residue','nexloop.consumer_erasure.owner_residue_run')
    order by created_at;
   ```

   - 预期：`owner_residue` 一条，outcome 为 `settled`（customer-4）；`owner_residue_run` 一条，settled 为 1，kept 为 0。与第 4 步存档的 NOTICE 一致。
   - kept 大于 0，说明还有顾客的登录主体持有授权：owner 行保留属于预期行为，但要停下报调度员，由负责人先撤销授权。

3. **erase dry-run**（负责人用 approver3 执行；完整命令在私有运行单里，这里只写公开部分）：

   ```bash
   security find-generic-password -s <keychain 服务名> -a <账号> -w | \
     python3 scripts/deploy/erase_synthetic_consumer.py erase --origin https://<APP_HOST_NAME>:<PORT> --ca-file <CA> \
     --username-file <approver3 用户名文件> --password-stdin --config <customer-4 删除配置> --dry-run
   ```

   - 预期：exit 0；customer-4 为 `already_erased`，`grants_held` 为 0，`grants_trace` 为 `cleared_at_erasure`（部署前是 `owner_link`）。

## 6. 三条验证消息（负责人执行，L6 判定），每条都要在 60 秒内收到回复

1. **s6c-1**（旧顾客，Consumer v2）：`control.nexloop_effect_scope_proofs` 新增一行，executed 为 `…service.request:1`，proof 为 `…service.request:2`。
2. **customer-5**（Consumer v1）：走 `:1`，不新增证明行。**注：customer-5 验证移到 R76.1 收尾（L6 有条件同意），本次不执行。**
3. **新建顾客**（按 `run-add-synthetic-customer.md`），**在 L6 复审 Q5′ 之后执行**（L6 已于 2026-11-10 给出 PASS）：
   - 负责人 2026-11-10 选定方案 1：business_owner → `Consumer.create:1` 声明 follow，经第 8、9 步派生 `Consumer.create:2`，校验与第 8 步相同。Q5′ 另要求后继的 scope 只变化该类型的版本。
   - 预期：新 Consumer 的 `schema_version=2`；回复成功；新增一行证明。
   - 本地 stage 路径测试 `tests/test_consumer_v2_stage_path_pg.py` 已覆盖正向和反向对照。

核对：

```sql
select executed_resource,scope_proof_resource,publication_id,consumer_version,recorded_at from control.nexloop_effect_scope_proofs
 where tenant_id='<TENANT_ID>' order by recorded_at desc limit 5;
select status,last_code,count(*) from runtime.nexloop_work_feed where tenant_id='<TENANT_ID>' and feed='claim-match' group by 1,2;
```

任一条失败，就停下，从 relay 日志取 `message relay attempt failed` 的 stage 和 reason，报调度员。

## 7. 失败时的处理

- **迁移失败**：整个迁移在事务里回滚，不重试，停下报调度员。DeadlockDetected 的情况：先确认已回滚，可以重试一次；仍失败就停掉 worker 后再重试。
- **第 14 步失败**：派生的 `:2` 授权保留，不撤销。
  - 这些授权只跟随到人工发布的等价后继，没有新增权限面。
  - 撤销后，旧顾客（v2）依然无法绑定，反而失去复现条件。
  - 确实要撤销时，从 `stage-principals.v1.json` 删掉对应的 follow 声明，然后重跑 register。派生授权会随声明一起撤销（Q5），不手工删授权事实。
- **迁移不回滚**：
  - **代码层**：回到上一个 release 的镜像。旧代码不调用新函数，按 `:1` 走，旧顾客的回复依然失败。
  - **数据层**：0447 删除的 owner 行和写入的 grants_cleared，以及迁移后产生的任何数据，只能从第 1 步的备份（`nexloop-pg-diff.service`）恢复，按 NX-035 恢复演练流程执行，不手工写 SQL。
  - **告警**：规则可以单独回退，用 configurator 重新发布 v36 或更早的版本。
- **机器接入的配置没有发布**：属于失败关闭，机器请求返回 503 或 429，不影响人和服务的路径；补发布即可。

## 8. 不做的事

- 不改 customer-5 的 v1 对象（L6 Q7）。以后要统一到 v2，走 0254 同款的受治理批量升级，另写运行单；`:1` 退役之前，先断言已经没有 v1 对象。
- 不停用任何 `:1` 定义，不手工改 SQL。
- 不登记机器客户端，不签发机器密钥，不执行 NX-062 验收演练（另按 run-nx062 §1 第 10 步和 §4.2 安排）；不启用 OIDC（另按 run-nx065）。
- 发布前门禁（Q8）和发布后冒烟（Q9）另立任务 NX-068。
