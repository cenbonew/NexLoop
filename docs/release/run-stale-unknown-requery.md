# 存量 unknown 效应的受控重查（0205，2026-10-18）

只用角色名。目标是 stage 上 customer-1 的 2 条存量 unknown 回复意图：`1f6ac3bd…`、`d1dc3853…`。它们来自 10/16 冒烟（r5）。

**类别标注**：**B** 表示需负责人确认。

## 1. 机制

- **为什么卡住**：这两条意图的来源 Run 凭据早已过期。
  - 0174 因此停止了自动重查（outbox `available_at = infinity`）。
  - 0199 的 6 小时窗口又保证它们不会再被自动领取。
- **怎么重查**：负责人写一份订单文件，由 effect worker 自己的数据库角色和服务凭据执行一条命令 `nexloop_eios.effect_requery`。
  - 对每条意图调用受治理的定义器 `authz.nexloop_effect_requery_order`（0205）。
  - 每条意图只“武装”一次查询：把 outbox 设为立即可领。
  - 同时在 `runtime.nexloop_effect_requery_orders` 记下审计：哪个 executor、什么时间、负责人的确认引用。
- **前置条件**：以下全部满足才会武装，否则返回原因码，不做任何改动。（0206 起不再检查派发控制：订单只武装一次查询，不发送任何东西；0205 的这项检查会在 worker 事务之外把部署层开关读成关闭，导致发布 13 上两单都返回 `controls_refuse`。）

  | 条件 | 不满足时的原因码 |
  |---|---|
  | 调用者是这条意图自己的 executor | 报 unavailable |
  | 意图状态是 unknown | `not_unknown` |
  | outbox 已停止（infinity） | `not_stopped` |
  | 来源 Run 已过期 | `origin_current` |
  | 同一确认引用未用过 | `reference_used` |
  | 每条意图最多 3 次订单 | `orders_exhausted` |

- **查询本身**：由正在运行的 effect worker 沿原有的“先查询”路径完成：
  - **provider 有这次投递（fulfilled）**：记录观测；有 `nexloop.service.receipt_reconcile` 授权时，走 0065 收据对账，意图变为 fulfilled。stage 的 effect_executor 有这项授权（stage-principals grants[2]）。出站回复随之变为 delivered。
  - **not_found 或查询不可用**：记为 unknown。因为首次 unknown 早于 6 小时，0199 会再次停止它。**不重发，不新建意图。**
- **安全边界**：不用直接 SQL，不用 superuser，不冒用人类身份；命令本身不发送任何东西。

## 2. 只读预查（DATA_HOST，bootstrap 只读事务）

```
select i.intent_id,i.state,i.executor_principal,o.state outbox_state,o.available_at='infinity'::timestamptz stopped,
       (select r.expires_at<=clock_timestamp() from authz.nexloop_run_credentials r
         where r.run_id=coalesce(i.origin_run_id,(select s.run_id from runtime.nexloop_effect_submissions s where s.intent_id=i.intent_id order by s.created_at limit 1))) origin_expired,
       (select count(*) from runtime.nexloop_effect_events e where e.intent_id=i.intent_id and e.event_kind='unknown') unknown_events
  from runtime.nexloop_effect_intents i join runtime.nexloop_effect_outbox o using(intent_id)
 where i.intent_id::text like '1f6ac3bd%' or i.intent_id::text like 'd1dc3853%';
select intent_id,delivery_state from runtime.nexloop_outbound_messages where intent_id::text like '1f6ac3bd%' or intent_id::text like 'd1dc3853%';
```

预期结果：
- 两条都是 `state=unknown`、`stopped=true`、`origin_expired=true`，executor 是 effect_executor 的 principal；
- outbound 为 `unknown`。

如果 `stopped=false`，说明 worker 还在按自己的路径处理它，不要下单。

另外请在 APP_HOST 上只读查 webchat-delivery 自己的投递记录，看有没有这两个 intent_id。这一步能预判结果是 fulfilled 还是 not_found。

## 3. 执行（B）

1. 部署包含 0205 的发布：先做备份校验，迁移走迁移门槛；镜像 digest 要与测试清单一致。
2. 在 APP_HOST 上写订单文件，权限 0600，放在私有目录，不进 git：

   ```json
   {"schema": "nexloop-effect-requery-order/1", "confirmation_ref": "owner-requery-20261018-1",
    "intents": ["<1f6ac3bd 完整 uuid>", "<d1dc3853 完整 uuid>"]}
   ```

3. 用 action-worker 镜像、它自己的私有配置执行一次。参数和 effect worker 相同，只是多一个订单文件、不需要 provider 参数：

   ```bash
   python -m nexloop_eios.effect_requery --database-url-file <effect dsn> --signing-key-file <key> --signing-key-id <id> --service-credential-file <executor credential> --artifact-root <dir> --world real --order-file <order.json>
   ```

   - 每条意图输出一行 JSON，例如 `{"intent_id":"…","armed":true,"orders":1}`；
   - 退出码：全部武装为 0，有未武装的为 1（原因见输出），配置不可用为 2。

4. 正在运行的 effect worker 会在下一个 tick（默认 0.25 s，加上退避）领取并查询，不需要重启。

## 4. 验证（只读）

1. 再跑一遍 §2 的查询，并加上：

   ```
   select intent_id,confirmation_ref,ordered_by,ordered_at from runtime.nexloop_effect_requery_orders order by ordered_at;
   select intent_id,provider_state,observed_at from runtime.nexloop_effect_observations where intent_id::text like '1f6ac3bd%' or intent_id::text like 'd1dc3853%' order by observed_at;
   ```

2. 按结果分两种情况：
   - **fulfilled**：意图变为 `fulfilled`（或 `observed_fulfilled`），outbound 变为 `delivered`，有一条 `provider_state=fulfilled` 的观测；effect_attempts 仍只有 1 行（没有二次 POST）。这条回复就此收敛。
   - **not_found 或不可用**：意图仍是 `unknown`，`stopped=true`，有一条新的 unknown 事件；provider 回答 not_found 时还有一条 `provider_state=not_found` 的观测（0214 起；此前 not_found 不留观测，负责人的“确认未送达”因此缺依据）。不会有任何新的投递。这条按 ADR 只能由负责人处理，例如人工联系顾客。可以在 provider 恢复后用新的确认引用再下一次订单，最多 3 次。
3. 不要手工改表，也不要删除事件或订单记录。

### 4.1 `unknown_over_24h` 告警

- **指标口径**：`effects.unknown.over_24h` 统计的是 `state='unknown'` 且创建时间早于 24 小时的意图数（0131/0141）。这两条意图都是 10/16 创建的，所以只要它们还处于 unknown，告警就一直 firing。
- **对账成功**：两条都变成 `fulfilled` 或 `observed_fulfilled` 后，指标降为 0，下一次告警评估把它记为 resolved。只读确认：

  ```
  select count(*) from runtime.nexloop_effect_intents where state='unknown' and created_at<clock_timestamp()-interval '24 hours';
  select rule_id,kind,recorded_at from control.nexloop_alert_events where rule_id='unknown_over_24h' order by recorded_at desc limit 3;
  ```

  期望第一条返回 0，第二条最新一行的 kind 为 `resolved`。
- **查到 not_found**：意图按 ADR 继续保持 unknown（不重发），告警不会恢复。这时需要负责人决定后续处理，例如联系顾客。
  - 0210 起有受治理的“确认效应未送达”：负责人先确认 provider 的记录保留期覆盖投递时间，再在等待期满后按 [确认未送达手册](run-confirm-not-delivered.md) 判定。判定后意图变为 failed，告警随之恢复。
  - 不要为了消告警去改表。

## 5. 限制

- 本地 PG 测试的 fixture 没有给 executor `receipt_reconcile` 授权，所以测试里意图停在 `observed_fulfilled`。回复本身已经 delivered，且只有一次导出。
- stage 有这项授权，对账会把意图收尾为 fulfilled。部署后请按 §4 核对。
