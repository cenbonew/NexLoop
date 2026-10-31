# 运行单：激励目录对象的受治理创建与撤下（S77，stage）

- 适用分支：`l2/s77-incentive-catalog`，基于 main 13dbcf71。
- 迁移：0389 激励目录守卫，0390 只增属性组的类型版本递增（ADR-042）。两个都是临时号。
- 用途：解除 B 档 #16 的 P6，即 stage 上没有 `IncentiveOffering` / `ConsumerIncentiveOffering`。
- 执行者：调度员，持 catalog_maintainer 凭据。判定者：L6。
- **L6 PASS 之前不组批**：0390 改的是信任根，负责人在合入前可以叫停。

## 变更

| 项 | 内容 |
|---|---|
| 类型 | `IncentiveOffering` / `ConsumerIncentiveOffering` 发布 **v2**：只给已有属性 `active` 加一个属性组 `incentive_lifecycle`，其余与 v1 完全相同。已发布的 v1 不动 |
| Action | `IncentiveOffering.create:1`、`ConsumerIncentiveOffering.create:1`、`IncentiveOffering.edit:1`、`ConsumerIncentiveOffering.edit:1`；setup 组，target 为 postgres，按 `request_id` 幂等；绑定 v2 |
| 授权 | `service-grants.v1.json` 文件内版本号 24：catalog_maintainer 从 stage-principals 迁入，主体、凭据、setup 凭据文件都不变；增加四个 Action 的 execute、两个类型的 READ，以及两条 `property_access_rule`（edit+read，组 `incentive_lifecycle`，basis v2）。owner-property-restrictions 增加两个类型，不限制任何组 |
| 守卫 0389 | 只允许 world=real。创建：形状必须与 0145 的准入一致；绑定必须指向一个有效 offering 的当前 revision，客户必须存在且不在擦除中（服务端判断）。修改：只允许 active 从 true 改为 false，其余一律拒绝 |
| 信任根 0390 | 新增 `control.nexloop_configure_group_increment`，只有 configurator 可以调用；`configure_manifest` 不变（ADR-042 §2.1） |
| 工具 | `python -m nexloop_eios.incentive_setup setup\|withdraw`：私有配方（0600），带 `--dry-run`；护栏为 `--max-amount-minor`（默认 10000 分）和 `--max-validity-days`（默认 90），具体数值待负责人确认；金额一律用最小货币单位 |
| 译名 | 四个 Action 的中文名**不在**本批：world-model-glossary v1 已按原文发布，留待 v2 |

## 步骤

### 0. 前提（只读）

```bash
ro "select type_name, version from ontology.object_type_versions where tenant_id='<TENANT_ID>' and type_name in ('IncentiveOffering','ConsumerIncentiveOffering') order by 1,2"
ro "select type_name, schema_version, count(*) from ontology.objects where tenant_id='<TENANT_ID>' and type_name in ('IncentiveOffering','ConsumerIncentiveOffering') group by 1,2"
```

- 第一条：预期两个类型各有一行 v1，还没有 v2。
- 第二条：预期 **0 行**，即没有 v1 实例。
  - 如果有 v1 实例，**停下报告**：v1 对象没有属性组，catalog_maintainer 撤不了它们；它们仍可以靠 valid_until 到期。是否保留由负责人决定。
  - v2 发布以后新建的对象都是 v2。

### 1. 部署本批并迁移

- 迁移前先备份（按部署单）。
- 迁移 0389、0390。

### 2. publish（顺序：类型 → 只增属性组 → Action）

```bash
python3 scripts/deploy/stage_secrets.py publish ... --dry-run
python3 scripts/deploy/stage_secrets.py publish ...
```

- dry-run 预期：`actions_to_publish` 恰好是上面四个 Action，`conflicts` 为空，`object_types` 为 22（20 个类型，其中两个各有 v1、v2）。
- 正式执行时，`catalog_publish.publish` 按下面的顺序走，失败了整步回滚：
  1. 一份只含 v1 类型的清单；
  2. `apply_group_increments`，即只增属性组这一步；
  3. 完整清单，包括四个 Action。
- 核对：
  - `ro "select type_name, version from ontology.object_type_versions where tenant_id='<TENANT_ID>' and type_name like '%IncentiveOffering' order by 1,2"` 两个类型各有 v1 和 v2；
  - `ro "select count(*) from control.nexloop_configuration_publications where tenant_id='<TENANT_ID>' and public_manifest->>'kind'='group_increment'"` 结果为 1。

### 3. register（授权与规则）

```bash
python3 scripts/deploy/stage_secrets.py register ... --dry-run
python3 scripts/deploy/stage_secrets.py register ...
```

- dry-run 预期：principals 里仍然有 catalog_maintainer，grants 多出四个 Action 和两个类型 READ。
- 正式执行后核对 `ro "select entity_key, payload->>'active' from authz.nexloop_authority_facts where tenant_id='<TENANT_ID>' and fact_kind in ('property_access_rule','property_group_restriction') and entity_key::text like '%IncentiveOffering%'"`，预期 4 行：两条规则、两条限制，都是 active。
- 没有新凭据，也没有轮换。

### 4. setup（调度员，catalog_maintainer 凭据）

配方用 `deploy/examples/incentive-setup.example.json` 的格式，私有文件 0600：

- `request_id` 用新的 UUID，重放时用同一个；
- 金额用分：单次 500，每人每期 1000；
- `valid_until` 是 UTC，在 90 天以内；
- `consumers` 填两个 s6c 测试客户的 id。

```bash
python -m nexloop_eios.incentive_setup setup --dry-run --database-url-file F --signing-key-file F --maintainer-credential-file F --recipe-file F --artifact-root D
python -m nexloop_eios.incentive_setup setup ...（同上，去掉 --dry-run）
```

- 预期输出：`configured: true`、`offering_id`、两条 `bindings`、`dispatch_authorized: false`；同一配方重跑，输出完全相同。
- 拒绝时只输出 `{code, step, index}`：
  - `step=binding index=i` 表示第 i 个客户不存在或在擦除中，被服务端拒绝；
  - `amount_above_ceiling` / `validity_above_ceiling` 表示超出护栏。
  - **不要改库绕过**。

### 5. 只读核对

```bash
ro "select type_name, schema_version, count(*) from ontology.objects where tenant_id='<TENANT_ID>' and world='real' and type_name like '%IncentiveOffering' group by 1,2"
ro "select properties->>'offering_revision', properties->>'active', properties->>'source_principal' from ontology.objects where tenant_id='<TENANT_ID>' and world='real' and type_name='ConsumerIncentiveOffering'"
```

- 预期 offering 1 个、绑定 2 个，`schema_version=2`。
- 绑定的 `offering_revision=1`、`active=true`，`source_principal` 为 catalog_maintainer 的 principal。

### 6. 撤下（回退）

配方用 `deploy/examples/incentive-withdraw.example.json` 的格式：`target` 为 `offering` 或 `binding`，填 `expected_revision`。

```bash
python -m nexloop_eios.incentive_setup withdraw ...
```

- 预期 `withdrawn: true`，`revision` 加 1。之后准入对这个对象报 `incentive outside catalog`。
- **不能重新启用**：要恢复就新建 offering 或绑定。
- 改期限、改金额，同样是新建 offering。

## 证据

- 第 0 步两条查询的结果；
- publish、register 的 dry-run 和正式执行的计数；
- setup 两次的输出：只记 id 和计数，不记凭据、DSN、主机或用户名；
- 第 5 步的查询结果。
