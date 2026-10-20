# 内部发布说明：发布 24、发布 25（v0.1.0 之后第二、三批）

- **读者**：在 stage 上部署这两个发布的运维（调度员与负责人）。面向用户的变化见 [CHANGELOG](../../CHANGELOG.md) 的“第二批”“第三批”两节。
- **写法**：只用角色名和占位符。迁移号、发布号和镜像 digest **以最终集成为准**；下面标 `<…>` 的地方部署前回填。
- **通用步骤**：每次部署都按 [OPERATIONS §8a.11](../OPERATIONS.md#8a11-例行发布部署的固定步骤发布-1618-起) 的固定步骤执行：备份核验与迁移门槛 → Action 发布预演 → 角色清单升版本时应用 → 镜像与重建 → 租户配置下发 → 部署后核对。本说明只列这两个发布各自的不同之处。

## 1. 概览

| | 发布 24（第二批） | 发布 25（第三批） |
|---|---|---|
| 发布标识 | `<RELEASE_24_ID>`（`<SHA_24>`） | `<RELEASE_25_ID>`（`<SHA_25>`） |
| 迁移范围 | `0235`–`0241`（以集成为准：`<MIG_24_FROM>`–`<MIG_24_TO>`） | `0242`–`0247`（以集成为准：`<MIG_25_FROM>`–`<MIG_25_TO>`） |
| 工作台角色清单 | 升到 v20（基线发布 23 为 v17） | 升到 v23 |
| 新的业务 Action | `nexloop.outreach_purposes.set:1`、`nexloop.outreach.send:1`、`nexloop.outreach.approve:1` | `nexloop.marketing_consent.record:1`、`nexloop.object_set.publish:1` |
| 新的数据库角色 | `nexloop_evo_writer`、`nexloop_evo_evaluator`（由迁移建立） | — |
| 新的可信配置 | `outreach-purposes.v1` | `contact-refusal-rules.v2`、`unsubscribe-rules.v1`；告警规则 v6 |

两个发布可以连续部署，也可以分开部署；如果要跳过发布 24 直接部署发布 25，迁移会一次跑完 `0235`–`0247`，其余步骤合并执行。

## 2. 迁移

| 号（暂定） | 内容 | 批次 |
|---|---|---|
| 0235 | NX-038 E4：归因与演进候选；预算类别 `evolution`；`nexloop_evo_writer` | 发布 24 |
| 0236 | 外联目的清单（内含 `outreach-purposes.v1`） | 发布 24 |
| 0237 | NX-038 E5：评估凭据隔离（只限 test world）、叠加读取与交接 | 发布 24 |
| 0238 | 外联：受治理的外发 Action 与外发账本（只有回环渠道） | 发布 24 |
| 0239 | 外联：计划复评 Run 的外联入口 | 发布 24 |
| 0240 | 外联：可选审批（四眼原则，24 小时时限） | 发布 24 |
| 0241 | NX-039 V1：`nexloop_evo_evaluator` 与开评、开放维度表、留出集登记 | 发布 24 |
| 0242 | NX-039 V2：冻结的门槛清单 `nx039-gates-1`、评估器写报告并结束评估 | 发布 25 |
| 0243 | 外联切片 7：退订与营销同意记录（内含 `unsubscribe-rules.v1`） | 发布 25 |
| 0244 | 拒绝规则 v2（内含 `contact-refusal-rules.v2`，ADR-023 §4） | 发布 25 |
| 0245 | 数据健康：指标快照中的数据流指标（告警规则 v6 所需） | 发布 25 |
| 0246 | 工作台 Object 的 Property 值读取 | 发布 25 |
| 0247 | Object Set（ADR-036） | 发布 25 |

- 迁移前先做差异备份并校验，迁移只经 `nexloop-migrate` 的门槛（没有豁免）。
- **全部是只新增的迁移**：新增表、函数、角色、检查约束的放宽，以及对已有函数的原位替换；没有删除列或删除数据。

## 3. 需要发布的 Action 与角色清单

1. **Action 发布预演**：

   ```bash
   python scripts/deploy/stage_secrets.py publish --out <管理机秘密树> --tenant <TENANT_ID> --bootstrap-dsn-file <BOOTSTRAP_DSN_FILE> --signing-key-id <SIGNING_KEY_ID> --dry-run
   ```

   比较结果里应当只出现 §1 表中新增的 Action（发布 24 新增 3 个，发布 25 新增 2 个），以及随之更新的 Object Type 版本。出现其他差异就停下来核对。确认后去掉 `--dry-run` 正式发布。
2. **应用工作台角色清单**（发布 24 升到 v20，发布 25 升到 v23）：

   ```bash
   python scripts/deploy/workbench_roles.py --roles deploy/authorization/workbench-roles.v1.json --members <私有成员文件> --apply --bootstrap-dsn-file <BOOTSTRAP_DSN_FILE> --signing-key-id <SIGNING_KEY_ID> --signing-key-file <SIGNING_KEY_FILE>
   ```

   - v20：负责人获得外联目的、外发与审批的授权。
   - v23：负责人获得营销同意记录、Object Set 发布的授权，并获得知识审核 `ontology.schema.review`（负责人 2026-10-20 决定）；审核员角色保持不变。
   - 只发布 Action 而不应用角色时，新 Action 没有任何人持有授权。
3. **演进的服务主体**（发布 24 起）：写候选的主体与评估器主体是两个不同的服务主体，评估凭据只能是 test world。登记方式见 NX-038 与 NX-039 设计稿。stage 上是否现在就登记，由负责人决定；**不登记时演进功能不可用，不影响其他功能**。

## 4. 可信配置与告警规则

- **随迁移生效、不需要另外下发**的配置：`outreach-purposes.v1`（0236）、`unsubscribe-rules.v1`（0243）、`contact-refusal-rules.v2`（0244）。仓库中 `deploy/configuration/` 下同名文件是这些迁移所用内容的记录，修改要出新版本，不能原地改。
- **拒绝规则 v2 是行为变化**：部署发布 25 之后，营销后 30 天内的“退订”等说法只停营销。规则切换写入切换记录表，可以审计。部署后用一条合成消息核对一次，或者在部署单上明确记录“未核对”。
- **告警规则需要指向 v6**（数据健康）：
  - 告警规则 v6 新增 10 条数据健康规则，依赖 0245 的指标，所以要在发布 25 的迁移之后下发。
  - **待解决**：当前的试运行配置 `nx037-trial.v4.json` 仍指向 `alert-rules.v5.json`，而 `real_tenant_init.py configure` 按试运行配置下发，直接重跑只会下发 v5。部署发布 25 之前，需要新增一份指向 v6 的试运行配置版本（可信配置只增版本，不改旧版本），或者另行确定下发 v6 的方式。在这件事定下来之前，数据健康页可以正常显示，但对应的告警不会触发。

## 5. 部署后核对

| 项 | 发布 24 | 发布 25 |
|---|---|---|
| 迁移 | catalog 尾号等于 `<MIG_24_TO>` | catalog 尾号等于 `<MIG_25_TO>` |
| 工作台 | 企业世界模型为可展开的 EIOS 导航；运营人设的“外联控制”中出现“外联目的”与“待审批外联” | 出现“数据健康”页；Object 详情显示 Property 值；企业世界模型下出现 Object Set；知识工作台对负责人显示审核决定 |
| 真实派发 | 三层开关保持部署前的状态；**外联不会真实外发** | 同左 |
| 告警 | 告警评估在更新，没有新的 firing | 告警规则生效版本为 v6（见 §4 待解决项）；数据健康页没有“需关注”，或者每一项都能解释 |
| doctor | `nexloop-doctor` 全部通过 | 同左 |

## 6. 回滚要点

- **迁移只新增，回滚只换镜像**：出现问题时，用上一份已知可用发布的镜像重建（`deploy.py rollback-check` 会如实报告：数据库已经在更新的迁移上，提示向前修复），**不运行任何 down 迁移**，也不删除新增的表或数据。
- 回滚到旧镜像之后：
  - 新增的工作台页面和 Action 不再可用，但已经写入的记录（审批、同意、Object Set、审核决定）都保留，重新部署新镜像后继续有效；
  - 拒绝规则 v2 由数据库中的规则决定，**换镜像不会退回 v1**。如果要退回 v1，需要负责人决定，并出一个新的迁移或新的规则版本。
- 角色清单不需要回退：多出来的授权对应的 Action 在旧镜像中没有入口。
- 回滚后按 §5 再核对一遍，并在部署单上记录回滚原因。
