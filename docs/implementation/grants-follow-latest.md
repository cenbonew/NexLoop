# 声明式授权沿用 follow_latest_version（ADR-020 §3，开发线 L2）

分支 `grants-follow-latest`，BASE `a8ff5959d24963c7cb7935c89ea8e672705aa269`（dispatch/integration-s3j）。临时迁移 `0083_grants_follow_latest.sql`。未声明 done。

## 1. 行为
- **声明**：`deploy/authorization/service-grants.v1.json` 中的 action 授权项可以带 `"follow_latest_version": true`，只能是 `true`，且只允许用在 `resource_type=action` 上。
  - v2 清单（`manifest_version: 2`）为 `claim_matcher` 的 `eios:action:Consumer.edit:1` 加了该声明。
  - 原有的策略排除照旧对整份清单生效：人类主体、`ontology.schema.review`、真实外发效应、REAL_DISPATCH。
- **展开**：`service_grants --doctor/--apply` 在比对之前，把清单展开成“有效清单”（`effective_manifest`）。版本链的数据来自新的只读函数 `control.nexloop_service_grant_action_lineage(tenant)`（仅 `nexloop_configurator`），它列出全部 Action 版本、定义、能力，以及是否由 NX-044 审核决定发布。从声明的版本开始逐个检查 v+1，每一步都必须满足以下全部条件才沿用：
  1. 同名 Action，版本号恰为上一版本 +1，且有效（active），同一 world；
  2. 由人类审核决定发布（`review_decision_id` 不为空）；
  3. capability 完全相同；
  4. 定义只在 `version`、对象类型引用的 `version/schema_digest`、`contract_digest/previous_version/created_at` 上不同；governance（含 risk、审批、策略）与 change_scope 的其他部分完全相同；
  5. 对象类型集合不变（个数、名称、类型都一致）。

  任一步不满足，该版本**拒绝沿用**、链条在此停止，并在报告中记录原因。可能的原因：`successor_inactive`、`not_published_by_review_decision`、`world_changed`、`capability_changed`、`definition_changed`、`governance_changed`、`change_scope_changed`、`resource_scope_changed`。
- **写入**：派生的授权（同一主体、相同操作集合、purpose 写明“沿用自哪个版本、来自哪个审核决定”）与清单中列出的授权走完全相同的 0050 可信配置路径：`nexloop_configurator`，写入 `control.nexloop_configuration_publications` 审计，事实是确定性的，所以重复 apply 不产生任何写入。
  - 如果后来某个后继版本不再满足条件（例如被停用），下次 apply 会按现有规则把对应授权撤销（写成空授权集）。
- **只作用于清单中显式声明的服务主体与 Action**：不会扩展到其他服务主体、其他 Action（即使绑定的是同一个类型）、人类主体或审核权限。人类主体本来就不能出现在清单里（`excluded_by_policy:human_principal`）。
- **报告**：
  - `doctor` 输出 `follow_latest: {derived, refused}`；存在 refused 时 `in_sync=false`。
  - `apply` 的报告同样带 `follow_latest`。
  - `--check` 列出所有声明了沿用的授权。
- **uncovered-actions**（NX-044 的只读检查）：被沿用覆盖的版本列入 `will_be_auto_covered`（下次 apply 生效），不再算作缺口；被拒绝沿用的版本仍列在 `uncovered` 中，并附 `follow_latest_refused` 原因。

## 2. 测试与结果
环境：`source ~/.nvm/nvm.sh && nvm use 24`、`LANG=en_US.UTF-8`、`PYTHONPATH=packages/eios-core/src:tests`。

- `tests/test_grants_follow_latest.py`（纯函数，11 项）：
  - 纯 Schema 重绑定的版本链会被沿用；
  - 以下 8 种变化都被拒绝且链条停止：能力、定义、governance、change_scope、对象类型增加、对象类型更换、版本停用、非审核发布；
  - 未声明的 Action、跨 Action（Product.create、Consumer.create）、版本链断裂（缺 :2 时 :3 不沿用）都不派生；
  - 声明格式与策略排除照常生效。
- `tests/test_grants_follow_latest_pg.py`（真实 PG，2 项）：
  - 真实人类 approve 发布 `Consumer.edit:2` 后，`uncovered-actions` 显示它“将自动覆盖”，doctor 给出派生项；
  - apply 后 `claim_matcher` 获得 :2 的 EXECUTE，其他服务主体（message_relay）没有；
  - 第二次 apply 写入为空（`changed=false`）；
  - 持有 :1 的浏览器人类不会获得 :2；
  - 清单中混入人类 principal 时整份拒绝。
- 调整了两个已有测试：
  - `tests/test_review_decisions_pg.py`：v2 清单下的 uncovered-actions 期望改为 `will_be_auto_covered`；去掉声明后仍报告缺口。
  - 新增 `MANIFEST_PATH` 常量。
- 首次失败（均保留）：
  1. PG 测试中断言“apply 后 doctor `in_sync`”失败：测试夹具中还有清单以外的服务主体授权，被报告为 extra_grants，这是正确行为。断言改为针对本功能的字段（缺失授权、事实漂移、拒绝沿用均为空，且 extra 都不属于清单主体）。
  2. 目标集中 NX-044 用例失败：它假设清单没有沿用声明，按上面方式调整。
- 最终目标集（follow 纯函数与 PG、service_grants_pg、review_decisions、review_http、trusted_configuration 两组、bootstrap、db_boundary、doctor、core_sandbox、compose_bootstrap、wheel_install）：**94 passed，67.16s**，junit sha256 `45e95494eee98bad3ade1dedc50686223955e8243c88966b0ad491dc8e317235`。
- 本线没有遗留进程。

## 3. 局限
- 只沿用 Action 的 EXECUTE。新属性的逐对象 READ/EDIT 仍需要受治理派生（与 NX-047 同类），尚未实现，所以发布后的 Claim 应用仍可能处于“等待授权”。
- 沿用以“由审核决定发布”为前提。手工或其他途径产生的后继版本一律不沿用，需要在清单中显式列出。
