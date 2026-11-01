# 运行单：S91 召回预算耗尽与接管未到期改为 defer（stage）

- 适用分支：`l2/s91-feed-defer`，基于第 96 批 e4cbc5f3。**没有迁移**：使用 S90（0401）已有的 defer 动词。
- 改动只在两个 worker：
  - **召回索引**（recall-instance）：向量预算耗尽（`EmbeddingBudgetDenied`）时，条目推迟 900 秒，不消耗重试次数，连续推迟最长 7 天；超过后回到计数重试（最多 20 次，最终进 dead letter）。
  - **接管到期**（takeover-expiry）：接管还没到期时，条目推迟到 `expires_at`（每次最多 6 小时），连续推迟最长 7 天，覆盖 SQL 允许的最长接管 604800 秒。接管是否结束本来就不依赖这个 worker：SQL 直接比较 `expires_at`（`control.nexloop_takeover_active`）。
  - 未知异常和权限被拒仍然走计数重试，不会被推迟。
  - 两个 worker 的 summary 都新增 `deferred` 计数。
- 部署：换成本批镜像，重启 recall-indexer 和 takeover-expiry 的服务（或等它们下一次被 timer 拉起）。没有开关。

## 前提：预算周期不长于 7 天（负责人 D2）

- 召回推迟的上限是 7 天。只有当 model 预算周期（`set_budget` 的 period_start 到 period_end）**不长于 7 天**时，下一个周期一开始，推迟中的条目就会被处理掉。
- 如果周期更长（例如按月）、而预算在周期中途用完：推迟 7 天后条目会回到计数重试，最终进 dead letter，这时要人工决定：提高预算，或者等新周期开始后重放 dead letter。
- **执行前请调度员只读核实 stage 当前的预算周期**（L2 没有连接主机，无法核实）：

  ```bash
  ro "select world, budget_kind, unit, limit_amount, period_start, period_end, period_end-period_start as length, revision from control.nexloop_budget_limits where tenant_id='<TENANT_ID>' and budget_kind='model' and period_end>now() order by period_start"
  ```

  预期：当前及以后的 model 预算周期，长度都不超过 7 天。超过时先报负责人，可选办法：缩短预算周期，或接受上面的人工处理。

## 判读 feed_backlog_age 告警（照 S90 Q1）

- 推迟不会更新 `changed_at`，所以预算长期耗尽时，`feed_backlog_age` 会持续上升并告警。**这是预期行为**：让长时间推迟能被看到。
- 判读：

  ```bash
  ro "select feed, last_code, count(*), min(changed_at), min(deferred_since) from runtime.nexloop_work_feed where tenant_id='<TENANT_ID>' and status='pending' and feed in ('recall-instance','takeover-expiry') group by 1,2 order by 3 desc"
  ```

  - recall-instance 的 `last_code = embedding_budget` 占绝大多数：预期内，原因是预算耗尽。等预算周期或调整预算，**不要重放，也不要改库**；预算恢复后积压会自动消化。
  - `deferred_since` 早于 7 天前：这些条目下一次处理时会转为计数重试，接近进 dead letter，需要人工处理（见上节）。
  - takeover-expiry 的 `last_code = takeover_active`：接管还没到期，预期内；条目会在 `expires_at` 被处理。
  - 其他 `last_code` 占多数：照常排查。

## 回退

换回上一批镜像即可，没有数据变更。已推迟的条目照常在到期时被处理。
