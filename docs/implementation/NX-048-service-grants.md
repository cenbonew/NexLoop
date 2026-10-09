# NX-048 服务主体授权清单（ADR-020 §3）

分支 `nx048-grants`，BASE `b8d5d514da73ea523099383236cab5763d6ad912`（dispatch/integration-s3b）。不声明 done。

## 交付
- `deploy/authorization/service-grants.v1.json`：版本化清单。`principals` 写明角色、service 主体/凭据/应用标识、运行所用 DB 角色、用途和来源任务；`grants` 每项写明主体、资源、操作、用途、来源任务；`deferred` 列出盘点到但**不**由本路径授予的授权及原因；`excluded_by_policy` 列出策略排除项。
- `nexloop_eios/service_grants.py`：`--check`（只校验）、`--doctor`（只读差异，存在漂移时 exit 1）、`--apply`（幂等应用并输出变更报告）。
- 迁移 `0071_nx048_service_grant_inventory.sql`（临时编号）：仅增加给 `nexloop_configurator` 用的只读函数 `control.nexloop_service_grant_inventory(tenant)`。它返回非人类授权事实、凭据元数据（不含 token digest）和人类标识列表，用于差异计算和人类主体拒绝。
- `trusted_configuration.py` 小幅重构：拆出 `apply_manifest_with_secrets`（内存中的秘密子集）和 `configurator_connection`；原 `apply_manifest` 行为不变。

## 写入路径
清单被编译成确定性的 EIOS authority facts（subject/membership/actor/authentication/application/subject_authority/resource_graph/grants/scope/controls/policies），只通过 0050 `control.nexloop_configure_manifest` 写入：会话必须是 `nexloop_configurator`，不能是超级用户或 BYPASSRLS，也不能是 owner 成员。
- 每次写入都在 `control.nexloop_configuration_publications` 留下包含完整公开清单的审计记录，并推进 tenant authority_revision。
- 凭据只以 sha256 digest 入库。
- 清单未变时，apply 检测到无差异，不产生任何写入。
- 从清单中移除的 grant，在下次 apply 时写成空 grant 集（撤权）。
- 清单外主体的授权不会被 apply 修改，只由 doctor 报告；外来事实的处理需要负责人决定。
- 已存在凭据的 binding 若变化（例如 requested_scopes 变化），apply 失败关闭（`credential_rotation_required`）。

## v1 盘点结论
- **授予**：
  - NX-019 claim_extraction_scheduler：`nexloop.claim.extract:1`、`NexLoop.queue.claim-extraction:1`
  - NX-019 claim_extraction_worker：同上两项
  - NX-020 claim_matcher：`nexloop.claim.match:1`、`Consumer.edit:1`、`Product.create:1`、`object_type` Consumer/Product READ
  - NX-013/014 runtime_worker：`NexLoop.queue.operations:1`
  - NX-016 message_relay：`nexloop.conversation.route:1`
- **暂缓**（见清单 `deferred`）：
  - 真实外发效应 `nexloop.service.request/query/receipt_reconcile`：需负责人确认渠道开通。
  - 每条 Message 的 READ：按 ADR-020 §1 由 NX-047 派生。
  - matcher 的逐对象读写：动态授权，需要受治理派生。
  - Run-bound 的 context bind：每个 Run 单独发凭据。
  - 规划/owner 控制/身份链接：属 Agent 或人类授权。
  - scope_denial.record：随外发渠道一起决定。
- **策略排除**（整份清单拒绝）：
  - 人类主体：principal 不允许出现 kind 字段，主体/principal 与人类目录重名也拒绝。
  - `ontology.schema.review`。
  - 真实外发效应 Action。
  - `REAL_DISPATCH_ENABLED`。
  - 非 action/execute 与 object_type/read 的资源类型或操作。

## 部署命令
```
python -m nexloop_eios.service_grants --manifest deploy/authorization/service-grants.v1.json --tenant <tenant> --apply \
  --database-url-file <configurator-dsn-file> --signing-key-file <backend-signing-key-hex> --signing-key-id <id> \
  --service-secrets-file <private {credential_reference: token}> --credential-expires-at <UTC ISO>
python -m nexloop_eios.service_grants --manifest ... --tenant <tenant> --doctor --database-url-file <configurator-dsn-file>
```
`nexloop_configurator` 默认 NOLOGIN，部署时由负责人开启登录（测试中由 admin 执行同样的部署步骤）。

## 限制
- 清单只覆盖静态的 Action/队列/类型级授权。NX-019 worker 要真正完成提取，仍需逐 Message READ（NX-047）；NX-020 实际写入仍需逐对象 EDIT。
- 对于事实不存在的资源，EIOS 决策返回 fail-closed 的 `AuthorizationUnavailable`，测试中视为“未授权”。
- `resource_graph` 和 `revision` 事实在 tenant 内共享，按确定性内容 upsert：若其它可信配置写入了不同内容，doctor 会报 fact_drift，apply 会改写。
