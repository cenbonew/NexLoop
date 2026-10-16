# unknown 意图的重查热循环与两条存量意图的对账（2026-10-16，release 5）

只用角色名。所有核对都是只读查询，在 DATA_HOST 一次性 backend 容器里以 bootstrap 只读事务执行，形态同 run-model-smoke §2.2。

**类别标注**：**B** 表示需负责人确认。

## 1. 结论

- **热循环的原因**：0174 的退避和“来源 Run 已过期就停止”在 effect worker 里**从来没有执行过**。
  - effect worker 把台账包在 `effect_worker._FreshExecutor` 里，它的 `__getattr__` 只放行固定几个方法，`back_off_unknown` 和 `settle_expired_origin` 不在名单里。
  - `EffectDispatcher._back_off` 和 `_origin_expired` 用 `getattr(ledger, ..., None)` 取这两个方法，结果都是 None，于是静默跳过。
  - 0174 的 PG 测试直接调用台账对象，没有经过 worker 的包装层，所以没发现。
- **每秒一条事件从哪来**：
  - 0042 的 `unknown` 动词每次都会写一条 `event_kind='unknown'` 事件，把 outbox 置为 `unknown`、清掉租约，并设 `available_at=now`；
  - worker 每 0.25 秒一轮（`--tick-seconds` 默认值）；
  - 每次 claim 把 fence 加 1，再 GET 一次，得到 503，又写一条 `unknown` 事件。
  - 加上认证、claim 和 HTTP 往返的耗时，大约每秒一次。
- **修复**（分支 `l2/unknown-query-backoff`）：
  1. `_FreshExecutor` 放行 `back_off_unknown` 和 `settle_expired_origin`。0174 的退避开始生效：第 n 次 unknown 之后，至少 min(60, 2^(n-1)) 秒才会再次领取。
  2. 新增临时迁移 0199 `effect_query_window`，重定义 `authz.nexloop_effect_query_backoff`，加上**最大自动查询窗口**：第一条 unknown 早于 6 小时后，`available_at` 设为 infinity，不再自动重查。
- **事件数量**：退避生效后不再“每次都写”。前 7 次的间隔依次为 1、2、4…32 秒，之后每分钟最多一条，6 小时窗口内大约 360 条，过了窗口就不再增加。我没有改动 0042 的 `unknown` 动词去合并计数：它是事件账本的核心路径，有上限就够了。
- **不变量不变**：unknown 先查询后对账，不会盲目重发；查询返回 not_found 时仍然不重投（`effect_dispatch.py` 中的 “no automatic resend”）。

## 2. 两条意图为什么结局不同

| 意图 | 现象 | 决定它的逻辑 |
|---|---|---|
| 1f6ac3bd（09:38） | 约 45 分钟 4015 条 unknown 事件，fence 一直增长，一直在被领取重查 | 每次查询都得到 503（`EffectProviderQueryUnavailable`），于是走 `record_effect_unknown`，`available_at=now`，退避没有执行，下一轮立即再领。它的来源 Run 在这段时间里看来一直没有被判为过期（或只是被撤销而没过期，0174 只认过期），所以没有别的东西让它停下 |
| d1dc3853（10:16） | 3 分钟 147 条后，outbox 停在 `unknown`、租约释放，不再增长 | 最可能的原因：它的来源 Run 凭据过期了。0174 头部注释写明，每次 claim 都会重新推导来源 Run 的身份；过期后 claim 被拒（`Run authentication denied`），于是不再领取、不再写事件，outbox 保持上一次 `unknown` 动词留下的 `unknown`、无租约。`settle_expired_origin` 本该把它设为 infinity，但它同样没有被调用，所以这一步也没发生 |

上面的判断依据是代码。需要用下面的只读查询在 stage 上确认，尤其是两条意图各自来源 Run 的 `expires_at`。

```
select i.intent_id, i.state, o.state, o.fence, o.available_at, o.lease_until,
       (select count(*) from runtime.nexloop_effect_events e where e.intent_id=i.intent_id and e.event_kind='unknown') as unknown_events,
       (select min(r.expires_at) from runtime.nexloop_effect_submissions s join authz.nexloop_run_credentials r on r.run_id=s.run_id
          where s.intent_id=i.intent_id) as origin_expires_at
  from runtime.nexloop_effect_intents i join runtime.nexloop_effect_outbox o using(intent_id)
 where i.intent_id::text like '1f6ac3bd%' or i.intent_id::text like 'd1dc3853%';
select intent_id, delivery_state, message_id, materialized_at from runtime.nexloop_outbound_messages
 where intent_id::text like '1f6ac3bd%' or intent_id::text like 'd1dc3853%';
```

## 3. 修好 provider profile 之后，这两条会怎样

- **重启方式**：修复 profile 后重启 action-worker。重启时**同时**重建 webchat-delivery，因为它用 `network_mode: service:action-worker`（OPERATIONS §8a.10）。worker 会在下一次可领取时，用绑定的 provider 发一次 GET。
- **GET 返回 fulfilled 或 accepted**：说明对方其实收到了。走查询对账路径（`record_effect_query_observation` → `observe_query`），或者 `nexloop.service.receipt_reconcile` 的 `recover`，意图变为 `fulfilled`。outbound 记录随触发器变为 `delivered` 或 `provider_accepted`，回复保证也就结清了。这是唯一能自动收尾的情况。
- **GET 返回 not_found**（503 发生在对方落库之前时，就会是这样）：
  - 仍记为 unknown 并退避，**不会自动重投**，系统里也没有“安全重投”的路径；
  - outbound 记录一直停在 `unknown`；
  - 回复保证（0152）到期后对未知回复对账一次：把 `available_at` 拉回 now，问一次；在 `reconcile_window_seconds`（默认 600 秒）内仍是 unknown，就以 `reply_unknown` 上报负责人，**不会**再起兜底回复；
  - 结论：顾客收不到这两条回复，只能由负责人处理，例如人工联系顾客，或请顾客重发一条消息，新消息会走新的 Run 和新的意图。
- **来源 Run 已过期（d1dc3853 很可能属于这种）**：即使 profile 修好，claim 也会被拒。修复合入后，worker 会调用 `settle_expired_origin`：意图如果已经发出过（`unknown` 或 `dispatching`），就停止自动重查（`available_at=infinity`）；之后只有回复保证的对账会再问一次。

## 4. stage 上处理两条存量意图的建议步骤（只读优先，B）

1. **只读确认**：运行 §2 的两条查询，记下每条意图的状态、unknown 事件数、来源 Run 是否过期，以及 outbound 的状态。
2. **只读查对方**：查 webchat-delivery 自己的投递记录里有没有这两个 `intent_id`，以 L4 的 profile 修复说明为准。这一步决定它们最后会走 fulfilled 还是 not_found。
3. **部署修复**：部署包含 `l2/unknown-query-backoff` 的发布（含迁移 0199，走迁移门槛和备份校验）。在这之前，热循环会继续写事件。如果需要先止血，可以只停 action-worker 和 webchat-delivery（B）。这只会暂停投递，不会丢失意图，outbox 都在 PG 里。
4. **修 profile 并重启**：修好 provider profile 后，按 §8a.10 同时重启 action-worker 和 webchat-delivery，然后看一轮结果：
   - fulfilled：自动收尾，核对 outbound 记录变为 `delivered`；
   - not_found：等回复保证上报 `reply_unknown`，由负责人联系顾客。**不要**手工改表重投，也不要删除事件。
5. **不需要清理事件**：4015 条事件是真实的审计记录，保留不删。如果事件量影响查询，另开任务评估归档，不在这次范围内。

## 5. 与 l2/reply-failfast（0199）的关系

- **迁移编号冲突**：两个分支都用了临时编号 0199（`message_run_reply_settle` 与 `effect_query_window`），内容互不相关，合入时其中一个要重编为 0200，`catalog.json` 和 `versions.lock.json` 的 `bootstrap_revision` 也要跟着改。本分支已把 `bootstrap_revision` 改为 0199；**l2/reply-failfast 漏改了这一项**，它的回归没覆盖 `test_effect_execution_sql`，合入时要一并修正。
- **功能上互补**：
  - reply-failfast 处理“Run 结束了却没提交回复”；
  - 本分支处理“提交了回复但投递结果 unknown 时，worker 怎么重查”。
  - 两者都会影响回复保证接手的时间点，但作用在不同的 outbound 状态上，不会互相改写对方的数据。
