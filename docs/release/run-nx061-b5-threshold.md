# 部署单：NX-061 B5 失败率阈值与告警 v31（stage）

- **状态**：草稿。执行者：调度员。判定者：L6。证据只记计数、规则数和快照字段值，不记消息或参数。
- **决定**：负责人 2026-11-07（按推荐，经调度员转达）。
  - 1 小时内已判定的结果（`outcomes.decided`）≥ 10，且失败率（`outcomes.failure_rate`）> 0.2：报 warning。
  - decided < 10：记"数据不足"，不判通过，也不报警。
  - 正式上线后按真实数据再调。
- **适用**：分支 `l7/nx061-b5-threshold`。
  - 迁移 `outcome_failure_rate_gate`：临时号 0418，接在 0417 之后，以组批后的编号为准。
  - 告警规则 v30 → v31，只追加一条。
  - 镜像和 compose 都没有改动，只有迁移和规则文件。

## 1. 设计：没有数据时规则给出什么

告警评估器（0131）只拿一个 JSON 数字和一个阈值比较，表达不了"decided ≥ 10 且 rate > 0.2"这种复合条件。所以 0418 在快照的 `outcomes` 块里加了几个派生字段：

| 字段 | 含义 |
|---|---|
| `min_decided` | 10，负责人定的样本门槛 |
| `failure_rate_gated` | decided ≥ 10 时等于 `failure_rate`，否则为 JSON null |
| `gate` | `measured` 或 `insufficient_data` |
| `insufficient_data` | 1 或 0（数字，与 data_health 的 no_data 写法相同） |

v31 新增的规则 `outcomes_failure_rate_high` 读 `outcomes.failure_rate_gated`，条件是 gt 0.2，severity 为 warning。

| 情况 | 快照 | 规则 | 判读（`run_outcome_taxonomy.failure_rate_gate`） |
|---|---|---|---|
| decided < 10（含 0） | gated = null，gate = insufficient_data，insufficient_data = 1 | 不触发 | `insufficient_data`：**未判定，不算通过** |
| decided ≥ 10，rate ≤ 0.2 | gated = rate，gate = measured | 不触发 | `within` |
| decided ≥ 10，rate > 0.2 | gated = rate，gate = measured | **warning** | `warning` |
| 没有快照，或快照里没有 outcomes 块 | — | 不触发 | `no_data`：**不算通过** |

- **数据不足不报警**，这是负责人的选择。告警通路不支持 info 级别（v15 已注明），所以不另加 info 规则。数据不足只体现在快照字段 `outcomes.gate` 和 `outcomes.insufficient_data` 上。
- 看到规则没有触发时，先看 `gate`：只有 `measured` 才说明"在阈值内"。

## 2. 上线

1. 部署前备份，按现行流程执行 `sudo -n systemctl start nexloop-pg-diff.service`。
2. 部署本批并迁移。迁移尾为组批后的编号，`versions.lock.json` 的 `bootstrap_revision` 与之相同。
3. `post_deploy_check.py` 全部通过。
4. 告警规则 v31（DATA_HOST，配置员窗口，沿用现行告警版本发布流程，与 v29、v30 相同）：

```bash
docker run $CHECK_COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/alert_rules_apply.py --manifest /opt/release/deploy/configuration/alert-rules.v31.json --check
```

去掉 `--check` 正式应用，其余参数与 v30 的发布相同。预期 106 条：v30 的 105 条，加上 `outcomes_failure_rate_high`。

## 3. 只读核对

```sql
select runtime.nexloop_metrics_snapshot('<TENANT_ID>','real')->'outcomes';
```

- 预期字段：`min_decided` 为 10；有 `gate` 和 `insufficient_data`。
- stage 流量低时，大概率是 `gate: insufficient_data`、`failure_rate_gated: null`。这是预期，证据里如实写"数据不足，未判定"，**不要写成"失败率在阈值内"**。

## 4. 回退

- 告警规则回到 v30：用同一个工具发布 `alert-rules.v30.json`。
- 迁移不回退：新增的字段只是额外的键，旧规则和旧代码都不读它们。
