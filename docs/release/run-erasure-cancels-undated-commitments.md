# 运行单：擦除时作废无期限承诺（I36 第二步，迁移 0388 临时号）

适用分支 `l2/i36-erasure-cancel`（在第一步 `l2/i36-commitment-cancel` 之上）。L6 同意方案 A，附条件 a–d（2026-10-31，经调度员转达）。

## 为什么
- 设计 D4（0132）：已擦除 consumer 的承诺保持原状态，直到自然到期。没有 due_at 的承诺永远不会到期。
- 擦除完成时，0306 的触发器会删除该 consumer 的承诺登记行（`runtime.nexloop_commitments`）。此后 keeper 处理这些承诺会报 `commitment unavailable`，所以取消请求永远推进不到 cancelled。

## 变更（迁移 0388）
| 项 | 内容 |
|---|---|
| a 共用转移 | `runtime.nexloop_commitment_cancel_apply(tenant, world, commitment, source)`：用 keeper evaluate 所用的同一个 `runtime.nexloop_commitment_target` 推出补丁，对象守卫会再校验一次；修订号 +1；status 事件由 `runtime.nexloop_commitment_status_event` 写入。keeper 的 settle 现在也调用这个函数（`authz.nexloop_commitment_command` 是 0263 的函数体，只替换了这一处，已登记 copy guard）。擦除写的事件带 `source: consumer_erasure`，keeper 写的不带，和原来一样 |
| b 范围与幂等 | 只处理 conditional / open / in_progress 中满足"没有期限"或"已有取消请求"的承诺；fulfilled、breached、cancelled 不动。取消请求每条最多一条；status 事件只在最后记录的状态不同时才写，所以 keeper 之后醒来补丁为空、事件不重复 |
| c 位置 | 在完成触发器 `control.nexloop_consumer_reference_erasure` 内（0306 函数体加这一段，已登记 copy guard），与删除登记行同一事务，并在删除之前。retention hold 仍在两者之前返回 |
| I36-c 有期限 | 负责人方案一：有期限且未结束的承诺不删登记行，`consumer_id` 改为本次擦除的 pseudonym，`properties` 清为 `{"erased":true}`（登记后不再使用）；登记表约束放宽接受 pseudonym，登记守卫放行这一次擦除更新。keeper 照常推进：到期为 breached，异常记在 pseudonym 名下，没有计划可标记、不联系任何人；结束后由 `commitment_closed` 保留期整条删除。计数 `commitments_kept`。L6 条件：a 保留行只剩 pseudonym 和 keeper 所需字段（claim/message/correlation 引用为 null，dedupe_key 为 `erased:<承诺 id>`，properties 为 `{"erased":true}`），约束只多接受这一种形状；b `runtime.nexloop_commitment_mark_plans` 对擦除中或已擦除的 consumer 直接返回，不生成任何计划标记或跟进；c（I38）keeper 读不到登记行时 SQL 报 P0002，keeper 把该项完成并计入 `orphaned`，不再重试 |
| d 存量 | 迁移末尾的一次性块：made_to 为 `erasure:<request id>` 且登记行仍在的承诺（0306 之前完成的擦除），按同一规则处理；取消请求的 ref 和 principal 都记为该 made_to 值 |

## 和 L6 条件 a 的差别（请知悉）
擦除写对象不是 keeper 的签名动作 `Commitment.edit`，因为擦除没有 keeper 的许可，所以不会产生 action claim 或 outcome 行。这和擦除自己改 made_to（0132）是同一种做法。账本事件、修订号和补丁与 keeper 推进的结果相同。

## 步骤（stage，部署第二步所在批次时）
1. 迁移前先做备份（按部署单）。
2. 迁移 0388 会自动处理存量：如果第一步之后那 44 条已经被 keeper 推进为 cancelled，存量块什么都不做；如果还剩着，就在这里把它们变成 cancelled。
3. 核对：
   - 第一步运行单里的 C7 应为 0 行；
   - 卡片未结束 0，四个标签都不再显示；
   - `select count(*) from runtime.nexloop_commitment_events where kind='status' and detail->>'source'='consumer_erasure'`，等于存量块处理的条数（第一步若已全部推进则为 0）。
4. 以后再做擦除：擦除记录 `consumer_references` 的计数里会多一项 `commitments_cancelled`。
