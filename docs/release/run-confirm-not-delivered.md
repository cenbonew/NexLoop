# 负责人确认“效应未发生”（0210，2026-10-18）

只用角色名。适用于查询结果为 not_found、结果不明（unknown）已超过 24 小时的效应意图，例如 10/16 冒烟留下的 `1f6ac3bd…`、`d1dc3853…`。设计稿见 `docs/design/confirm-effect-not-delivered.md`，负责人 10/18 对 5 项全部按推荐拍板。

**类别标注**：**B** 表示需负责人本人操作或确认。

## 1. 机制

- **入口**：受治理的人类 Action `nexloop.effect.confirm_not_delivered:1`（能力 `service.confirm_not_delivered`，操作 `confirm_effect_not_delivered`），只授予工作台 owner；处理函数还会再核对当前工作台角色必须是 owner。
- **负责人的工具**：`scripts/deploy/confirm_not_delivered.py`。读一份私有配置文件，用负责人自己的工作台登录（密码从钥匙串管道输入），对每条意图先做只读预查，再调用上述 Action。不连 PostgreSQL，不经过浏览器 console。
- **判定前的检查**（SQL 里检查，任一不满足都拒绝并给出固定原因码，不做任何改动）：

  | 条件 | 原因码 |
  |---|---|
  | 意图属于本租户 | `intent_unknown` |
  | 还没有判定过（每条意图只能判定一次） | `already_determined` |
  | 意图状态是 unknown | `not_unknown` |
  | effect worker 没有持有它的租约 | `effect_busy`（稍后再试） |
  | 有一次投递尝试 | `no_attempt` |
  | 当前尝试的**最新**观测是 `not_found`、晚于投递，且从未有 accepted / fulfilled 观测 | `no_not_found_evidence` |
  | 首次 unknown 已满 24 小时，且该 not_found 观测已满 10 分钟 | `wait_period` |
  | 配置中 `provider_retention_checked` 为 true | `retention_unconfirmed` |
  | 选择兜底回复时，这条消息尚未升级、也未被回复 | `fallback_unavailable` |

- **判定成功后**（同一事务）：
  - outbox 和意图都变为 `failed`（意图沿用现有终态，不新增状态）；出站回复随之由 unknown 变为 failed，转移事件来源为 `intent`；
  - `runtime.nexloop_effect_determinations` 追加一条判定记录：类型 `confirmed_not_delivered`、作为依据的 not_found 观测、确认引用、理由、保留期确认、回复跟进方式、负责人 principal；
  - **不发送任何东西，不新建意图，不改 Action claim**；投递尝试行保持原状。
- **回复保证**（每条意图在配置里选择）：
  - `owner_follow_up`（默认）：把这条回复所对应的顾客消息记为升级，原因 `owner_follow_up`。回复保证 worker 据此结清它，不会自动发兜底回复，由负责人跟进。
  - `fallback_reply`：不升级，失败的回复交给回复保证，按 0152 启动这条消息唯一的一次兜底回复。消息已升级（例如之前的 `reply_unknown`）或已被回复时拒绝（`fallback_unavailable`），此时请改用 `owner_follow_up`。
  - 10/16 的两条存量意图之前已被回复保证升级为 `reply_unknown`，只能选 `owner_follow_up`。

## 2. 只读预查（B）

### 2.1 provider 记录保留期

先在 APP_HOST 上只读确认 provider（webchat-delivery）记录的最早时间，确认它覆盖这两条意图的投递时间。如果 provider 只保留了某个时间之后的记录，那么 not_found 只能说明“记录不在了”，**不能**证明没有送达。这种情况请不要判定，改由负责人人工联系顾客核实。只有确认保留期覆盖投递时间后，才在配置里写 `"provider_retention_checked": true`。

### 2.2 工具预查（不写任何东西）

在 APP_HOST 上写配置文件，权限 0600，放在私有目录，不进 git（样例见 `deploy/examples/confirm-not-delivered.example.json`）：

```json
{"schema": "nexloop-confirm-not-delivered/1", "confirmation_ref": "owner-confirm-20261018-1",
 "reason": "重查结果为 not_found；已核对 provider 记录保留期覆盖投递时间。",
 "provider_retention_checked": true,
 "intents": [{"intent_id": "<1f6ac3bd 完整 uuid>", "reply_followup": "owner_follow_up"},
             {"intent_id": "<d1dc3853 完整 uuid>", "reply_followup": "owner_follow_up"}]}
```

然后加 `--dry-run` 执行：

```bash
security find-generic-password -s <keychain service> -a <owner account> -w | python3 scripts/deploy/confirm_not_delivered.py --origin https://<APP_HOST_NAME>:<PORT> --ca-file <CA> --username-file <owner username file> --password-stdin --config <confirm.json> --dry-run
```

- 输出一个 JSON 对象，每条意图有 `before`：原因码 `code`、意图与 outbox 状态、出站状态、首次 unknown 时间、最新观测（状态与时间）、最早可判定时间 `wait_until`、消息是否已升级。不含用户名、密码、cookie 或理由。
- `result` 为 `eligible` 表示可以判定；`not_eligible` 时看 `before.code`：
  - `no_not_found_evidence`：还没有 not_found 查询结果。先按 `docs/release/run-stale-unknown-requery.md` 下重查订单；provider 不可用时没有观测，不能判定。
  - `wait_period`：等到 `wait_until` 之后再来。
  - `effect_busy`：worker 正在处理，几秒后重试。
- 预查走工作台只读接口 `GET /api/v1/workbench/effect-determination/<intent_id>`（仅 owner），不会写任何东西。

可选的数据库只读核对（DATA_HOST，bootstrap 只读事务）：

```
select i.intent_id,i.state,o.state outbox_state,o.lease_until,
       (select min(e.created_at) from runtime.nexloop_effect_events e where e.intent_id=i.intent_id and e.event_kind='unknown') first_unknown,
       (select jsonb_agg(jsonb_build_object('state',x.provider_state,'at',x.observed_at) order by x.observed_at) from runtime.nexloop_effect_observations x where x.intent_id=i.intent_id) observations
  from runtime.nexloop_effect_intents i join runtime.nexloop_effect_outbox o using(intent_id)
 where i.intent_id::text like '1f6ac3bd%' or i.intent_id::text like 'd1dc3853%';
```

## 3. 判定（B）

1. 部署包含 0210 的发布：先做备份校验，迁移走迁移门槛；发布 business-actions v18 和 workbench-roles v12（owner 角色获得 `nexloop.effect.confirm_not_delivered:1`），镜像 digest 要与测试清单一致。
2. 预查全部为 `eligible` 后，去掉 `--dry-run` 再执行同一条命令。
   - 每条意图的 `result` 为 `determined`，并给出 `determination_id`、`reply_followup`、`outbound_state`（应为 `failed`），`after.code` 为 `already_determined`。
   - 退出码：全部判定为 0；有不合格或被拒绝的为 1（`code` / `reason` 说明原因）；配置不可用为 2。
   - 每条意图的 Idempotency-Key 由意图、确认引用和跟进方式推导，重跑同一份配置只会重放，不会重复判定；已判定的意图显示 `already_determined`。

## 4. 验证 `unknown_over_24h` 恢复（只读）

1. 再跑一次 `--dry-run`：两条都应为 `already_determined`。
2. DATA_HOST 只读：

   ```
   select intent_id,state from runtime.nexloop_effect_intents where intent_id::text like '1f6ac3bd%' or intent_id::text like 'd1dc3853%';
   select intent_id,delivery_state from runtime.nexloop_outbound_messages where intent_id::text like '1f6ac3bd%' or intent_id::text like 'd1dc3853%';
   select intent_id,kind,confirmation_ref,reply_followup,decided_at from runtime.nexloop_effect_determinations order by decided_at;
   select count(*) from runtime.nexloop_effect_intents where state='unknown' and created_at<clock_timestamp()-interval '24 hours';
   select rule_id,kind,recorded_at from control.nexloop_alert_events where rule_id='unknown_over_24h' order by recorded_at desc limit 3;
   ```

   期望：意图为 `failed`；出站为 `failed`；两条判定记录；第四条查询返回 0（没有其他长期 unknown 时）；告警评估器下一次评估后，最新一行的 kind 为 `resolved`。
3. 工作台“告警”页面上 `unknown_over_24h` 不再 firing；回复升级列表里这两条消息的原因是原来的 `reply_unknown`（已升级的消息不会新增 `owner_follow_up`）。
4. effect_attempts 每条意图仍只有 1 行：判定没有发送任何东西。
5. 不要手工改表，也不要删除判定、事件或观测记录。判定记录只追加、不可修改。

## 5. 限制

- 等待期（24 小时、10 分钟）是 SQL 里的固定值，与负责人 10/18 的决定一致；要改需要新迁移。
- 判定理由只写在判定表里，工作台预查、工具输出和人工操作审计页都不展示它。
- 意图一旦判定为 failed 就不可撤回。如果之后发现顾客其实收到了回复，按 ADR 只追加新的事实，不改这条判定。
