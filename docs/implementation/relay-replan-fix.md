# relay 卡死修复：无法完成的来信交给回复保障

依据：L4 在 NX-031 C4-low 重跑中发现的 relay 卡死（nx031-s3 `1131b31` 的实现说明），以及调度员 2026-10-11 的裁定：做终结处理，不沿用首次签发的命令。

- 分支 `relay-replan-fix`，从 main `7ee5e31` 切出。
- 临时迁移号 `0190_relay_handoff.sql`。

## 根因

1. **失败的尝试不释放**：`MessageRelay.run_once` 遇到任何异常都直接抛出，而 `release` 只接受已经路由的来信。所以失败的来信只能等 30 秒租约到期后再被认领。
2. **再次认领注定失败**：第一次尝试已经签发了 Run 凭据，`message_run_issuances` 把那次的 assignment 绑定了下来（vault 里也是）。再次认领时，recipe 按当前读取。客户的控制版本或 Consumer 版本一旦变化（例如拒收联系），新的 assignment 就与绑定的不一致：
   - vault 的 `prepare` 冲突，对应观察到的 ContextArtifactConflict 与 Unavailable；
   - 300 秒后凭据过期，每次都返回 `requires_governed_replan`。relay 对这个结果直接返回，不释放，也不升级。
3. **串行规则把问题放大**：`claim_next` 每个客户只取最早的一条 pending 来信（D2），所以这个客户之后的所有来信都被挡住。

## 修复

| 位置 | 做法 |
|---|---|
| SQL 新 verb `handoff` | 原因只能是以下之一：`requires_governed_replan`、`credential_expired`、`binding_conflict`、`attempts_exhausted`。执行后：<br>• 来信离开 pending（与人工接管结案的做法一致）；<br>• 释放租约；<br>• 吊销为它签发过的 Run 凭据（不沿用旧命令）；<br>• 把它的 `reply-due` 提前到现在，由回复保障按**当前**控制发兜底回复（ADR-023），不静默丢弃；<br>• 写一条只追加的 `runtime.nexloop_relay_handoffs` 记录和一条审计事件 `nexloop.relay.handoff`。<br>重复调用是幂等的：返回第一次的结果 |
| SQL 新 verb `yield` | 临时失败后交还来信：计一次尝试，租约在短暂退避（10 秒 × 次数，最多 60 秒）后结束；到第 5 次时提示应当终结。`attempts_exhausted` 只有在次数用完后才被接受 |
| `lookup` | 额外返回已绑定的 assignment digest |
| `MessageRelay` | 以下情况直接终结：<br>• recipe 已过期；<br>• 已签发的凭据已过期；<br>• 当前 assignment 与已绑定的（SQL 中或 vault 中）不一致；<br>• 签发结果为 replan。<br>其他异常先 `yield`，用完 5 次后终结。CLI 接受新的返回值 `handed_to_fallback` |
| 指标 | relay 段新增 `handoffs_last_hour` 和 `handoffs_total` |

- 实现方式：issuance command 再做一次“改名保留 + 新包装”；`runtime.nexloop_relay_metrics` 在 0143 的函数体上加两个计数。已发布的迁移都没有修改。
- 首次签发的命令不会被重用：终结时把它吊销，兜底 Run 由回复保障按当前约束另行签发。

## 测试（`tests/test_relay_handoff_pg.py`，3 个）

- **两次认领之间命令改变**（测试中注入 Consumer 版本变化，走的是与控制版本变化相同的路径）：
  - 第一次尝试在签发之后失败，交还来信，计 1 次；
  - 下一轮先服务从未被服务的客户 B，接着 A1 以 `binding_conflict` 终结：凭据吊销，来信不再 pending，`reply-due` 已到期，有审计事件，指标计数为 1；
  - 然后 A2 被正常路由。
- **凭据过期**：以 `credential_expired` 终结；重复调用 `handoff` 返回第一次的结果，审计事件只有一条。
- **有界重试与互不影响**：
  - 次数未用完时，`attempts_exhausted` 被拒绝；
  - 5 次 `yield` 之后可以终结；
  - 另一个客户 B 照常被服务；
  - 不是认领者、或 fence 不符时，不能终结。

## 和 NX-031 的关系

- 本分支只改 relay，不改压测脚本。修复合入后，由 L4 重跑 C4-low。
