# 运行单：结束过期 action claim（stage，2026-10-31）

适用分支 `l2/stale-claim-settle`（第 71 批：叠在 L4 I31 9a667c11 之上，L4 基于 5df334b0）。迁移为 `0385_action_claim_settle.sql`，接在 L4 的 0384 之后；告警 `alert-rules.v26.json` 以 L4 的 v25（89 条）为底，加两条，共 91 条。指标快照 `action_claims` 块包在 L4 的 `service_credentials` 块之上（rename-and-wrap），两块并存。

## 变更

| 项 | 内容 |
|---|---|
| 迁移 | 新表 `runtime.nexloop_action_claim_settlements`（追加写，只存 id、类别、证据 id、理由、执行人、意图）；检查函数 `runtime.nexloop_action_claim_settle_check`；受治理处理器 `runtime.nexloop_governed_settle_action_claim`（能力 `action_claim.settle`，操作 `settle_action_claim`）；指标快照新增 `action_claims` 块 |
| 业务 Action | `business-actions` v55：`nexloop.action_claim.settle:1`（human_owner、low、无审批）；编译计数 (20, 131, 2) |
| 能力快照 | `capabilities` v8：`action_claim.settle` |
| 工作台角色 | `workbench-roles` v51：owner 97 个 Action，新增 `eios:action:nexloop.action_claim.settle:1`，只给 owner |
| API 路由 | `api-routes` v7：`POST /api/v1/workbench/actions/settle_action_claim` |
| 告警 | v26：`action_claim_stale`（最老一条过期超过 1 小时，warning）、`action_claim_stale_effect`（effect 类 > 0 持续 15 分钟，critical） |
| 工具 | `scripts/deploy/action_claim_settle.py`：负责人登录工作台，逐条调用，重跑会重放；不连 PostgreSQL |

## 规则
- 只收 world=real、state=active、租约过期**超过 1 小时**的 claim。
- `uncommitted`：只接受白名单里的 6 个 Action：Goal.create、PlanStep.create、nexloop.experiment.start、nexloop.approvals.decide、nexloop.real_dispatch.set、nexloop.consumer_twin.calibrate。白名单的依据是测试已证明这几个 Action 的业务写入和终态 claim 在同一事务提交（0081 的创建入口，0348 的受治理入口）。此外 binding 必须是 adapter `nexloop.core` 且 target `postgres`，并且不存在 source_ref=intent 的对象。nexloop.core/postgres 的其他调用方（relation、两段式 context artifact、scope denial、identity link）一律返回 wrong_class（L6 I33）。
- `confirmed_not_delivered`：effect intent 已是 failed，有 0210 的 confirmed_not_delivered 裁定，且裁定依据的观察为 not_found。同时必须**没有任何迹象**表明 provider 可能收到过（L6 I32，正向白名单）：没有非 not_found 的观察；attempt 没有 provider_reference，且在裁定之后没有变动（updated_at）；该 intent 的 outbound 全部处于 persisted 或 failed，或者根本没有 outbound。
- 其他情况一律拒绝，并返回代码：not_found / not_active / not_stale / wrong_class / object_exists / intent_not_failed / no_determination / provider_observed / provider_reference / attempt_after_determination / outbound_unknown / already_settled。结果未知的 effect claim 不在这里处理，要先对账。
- stage 上那两条 service.request 按 Q4/Q6/Q7 的结果可以通过检查：没有 provider_ref；attempt 最后更新是 10-18 05:15Z，早于裁定时间 05:52Z；outbound 为 failed。
- 终态为 `permanent_failure`，outcome_id 填 settlement_id。同一 intent 之后再 reserve 只会重放，不会执行。**不写 effect_failures**，避免补救扫描重发回复和统计突增。

## 步骤（stage）
1. 发布前预演：`stage_secrets.py publish --dry-run`，预期只多出 `nexloop.action_claim.settle:1`，计数 (20, 131, 2)。
2. 部署迁移，执行 publish 和 `workbench_roles --apply`，owner 授权数应为 97。
3. 在 DATA_HOST 用 ro() 取完整 intent（只读）：
   ```sql
   select c.action_name,c.intent_id,c.claim->'binding'->>'target_system' target from runtime.nexloop_action_claims c
   where c.tenant_id=:'T' and c.world='real' and c.claim->>'state'='active'
     and (c.claim->>'lease_expires_at')::timestamptz<clock_timestamp()-interval '1 hour' order by 1,2;
   ```
   target 为 postgres 的填 `uncommitted`；两条 `nexloop.service.request` 填 `confirmed_not_delivered`。
4. 按 `deploy/examples/action-claim-settle.example.json` 写私有配置（0600），先加 `--dry-run` 检查，再正式执行：
   ```
   security find-generic-password -s <keychain service> -a <account> -w | \
     python3 scripts/deploy/action_claim_settle.py --origin https://<APP_HOST_NAME>:<PORT> --username-file F --password-stdin --config F
   ```
   预期 15 条全部 `settled`，退出码 0。重跑应全部为 `replayed`。
5. 验证：再跑一次第 3 步的查询，应为 0 行；指标快照的 `action_claims.stale_active` 应为 0。
6. 最后启用告警 v26（组批后的编号）。顺序不能反：先收尾、再上告警，否则现有 15 条会立刻触发。

## 负责人决定（负责人，2026-10-31，经调度员转达；全部按推荐）
- 可收的过期阈值为 **1 小时**，写死在检查函数里。claim 租约最长 5 分钟（0007），1 小时足以排除仍在执行的情况。
- **不做自动清扫**：由负责人用本工具一条命令执行；告警只负责发现问题。
- 顺序：**部署 → 收尾 15 条 → 启用告警**（即上面第 1–6 步）。
- **不补发回复**：53b78ed1 和 10-22 停摆的消息都不补发，和 S42 方案 A 一致。
