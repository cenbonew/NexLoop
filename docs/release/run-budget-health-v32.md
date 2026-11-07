# 运行单：预算健康快照与告警规则 v32（stage）

- 起因：stage 的模型预算在 2026-11-05 16:00Z 到期，两天里没有任何告警。`budget_near_limit` 只看本周期已用的比例，看不到到期；`refusal_budget_exhausted` 只有出现实时拒绝时才会触发。
- 内容：
  - 迁移 `budget_health`（临时号 0422，调度员合批时重编）在 metrics snapshot 里新增 `budget_health` 块。
  - `alert-rules.v32.json` 是 v31 原样加 4 条规则：`budget_expiring`（warning）、`budget_expired`、`budget_unconfigured`、`budget_exhausted_recent`（critical）。
- 必需的预算：model 永远必需；evolution 只在租户参与演化时才必需。判断依据是数据：`authz.nexloop_evo_principals` 里有该租户的 principal，或者已经有 evolution 预算行。快照的 `budget_health.required` 列出按这条规则算出的实际列表。
- 执行者：调度员。判定者：L6。

## 步骤

1. 按部署单备份、迁移，重建 api。
2. 切换告警规则：用 `alert_rules_apply.py --manifest .../alert-rules.v32.json`，先 `--check`，再应用（命令和窗口照最近一次部署单的告警切换步骤）。
3. 只读核对快照：`select runtime.nexloop_metrics_snapshot('<tenant>','real')->'budget_health';`
   - `required` 至少包含 `model`；租户参与演化时还要有 `evolution`；
   - `kinds.model.period_end` 与预算行一致，`expires_in_seconds` 的正负与是否到期一致。

## 阳性对照（切换后至少看一轮评估，每分钟一次）

| 情况 | 预期 |
|---|---|
| 模型预算到 **2026-11-12 16:00Z** 仍未续 | 之后的第一轮评估，`budget_expired` 触发（critical），工作台显示 |
| 模型预算在 7 天内到期，并且还没过期 | `budget_expiring` 触发（warning）。按 11-12 16:00Z 到期算，切换后立即就满足这个条件，应当能看到 |
| 必需的 kind 没有预算行（例如参与演化的租户没有 evolution 预算） | `budget_unconfigured` 触发（critical） |
| 近 1 小时出现 NXB01 拒绝 | `budget_exhausted_recent` 触发（critical） |
| 预算续上（period_end 至少在 7 天以后），1 小时内没有拒绝 | 以上 4 条都不触发；已触发的在下一轮评估时消除 |

核对方法：
```sql
select rule_id,firing,last_evaluated_at from control.nexloop_alert_state
where tenant_id='<tenant>' and rule_id like 'budget_%';
```
只记录规则名、是否触发和时间，不导出其他内容。

## 回退

用 `alert_rules_apply.py` 重新应用 v31。迁移不回退：快照多出来的 `budget_health` 块不影响任何已有规则。

## 证据

- 迁移版本号；
- v32 切换的输出；
- 阳性对照里实际看到的那几行（规则名、firing、时间）。
