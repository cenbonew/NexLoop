
## 8. 实现记录（L6 条件 PASS，D1–D5）

迁移 `alert_evaluate_split`（临时号 0452），测试 `tests/test_alert_evaluate_split_pg.py`，执行单 `docs/release/run-alert-evaluate-split.md`，告警规则 v39。与设计稿不同的地方：

- **函数所在 schema**：T1 和 T2 的函数放在 `authz`（`authz.nexloop_alert_maintain_unbound`、`authz.nexloop_alert_snapshot_take`），因为 `nexloop_domain_worker` 没有 `runtime` schema 的 USAGE。
- **D1**：
  - 快照多了 `alert_evaluation{maintenance_failed_1h, skipped_1h, minutes_since_success}`，跳过原因记在 `runtime.nexloop_alert_evaluation_runs`；
  - v39 加三条规则：`alert_maintenance_failed`（warning）、`alert_evaluation_skipped`（warning）、`alert_evaluation_stalled`（≥5 分钟没有成功评估，critical）；
  - stalled 由下一次成功评估发现，因为它的快照在记成功之前拍摄。评估器完全停摆时，靠 0425 通知器的新鲜度检查。
- **D2**：快照函数只接受 active 状态的租户；120 秒内有未消费的快照就复用，复用时会保留维护失败的标记；函数只返回 uuid；两张表对所有服务角色都不可读。
- **D3**：快照入表前去掉自带的 `tenant_id` 标签（这一行的 tenant 列已经限定了租户）。字段清单由测试逐项检查：每个键和值都是计数、代码或时间，不含任何 uuid、长十六进制、邮箱、电话、租户或主体 id。擦除分类是“不含个人数据，保留 1 天，不擦除”，依据就是这个测试，`real_effect_tables` 的 reason 里也写了。
- **D4**：同一个 snapshot_id 第二次提交返回 `snapshot_consumed`，不改任何状态，并计为一次跳过。
- **D5**：本地（空库）实验守卫 `guard_experiments` 平均 5.0 ms、最长 5.7 ms；整个 run_once 平均 20.5 ms、最长 26.1 ms。stage 上线前后都按执行单 §1 和 §3 记录 pg_stat_statements 里的平均和最长耗时；守卫平均超过 500 ms 或最长超过 3 秒，就另立任务拆分。
- **兼容路径（新增）**：签名载荷里没有 snapshot_id 时（上一版评估器，用于滚动部署或回退），T3 在同一事务里先维护、再拍快照，然后评估，告警不会被跳过；只有带了 snapshot_id 但格式不对，才算 `snapshot_missing`。
- **tick 中途授权变化（新增的行为）**：发布不再等维护和快照，所以一次发布或 register 可能落在评估 tick 的中间。那一 tick 的 T3 授权会过期被拒（ContextDenied），快照保持未消费；下一 tick 重新认证，在有效期内复用这份快照，测试覆盖了这一点。代价是这种 tick 晚一分钟评估。
