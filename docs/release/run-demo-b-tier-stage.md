# 运行单：演示页 B 档的 stage 补验证（S74，负责人 2026-10-31）

依据：L3 S74 现状清单（notes `l3-s74-demo-capability-status.md`）里的 #16、#17、#18、#19。基线 main 80b1009b。本单只写步骤和核对方法，不改代码。

## 0. 先读：这张单能证明什么、不能证明什么（源码核对，80b1009b）

1. **测试人设先于勿扰和频率**。外联执行器的准入（0240 `authz.nexloop_outreach_command` admit）按 `runtime.nexloop_outreach_gates`（0243 定义）的顺序，取**第一道**拒绝作为 `refusal_code`，但会把**每一道闸**的结论写进 `refusal_gates`。顺序是：
   1. `contact_restricted`
   2. `control_paused`
   3. `purpose`
   4. `marketing`
   5. **`persona_excluded`**（人设不是 `real` 就拒）
   6. `quiet_hours`
   7. `frequency`
   8. `consumer_preference`
   9. `real_dispatch`
   10. `budget`

   s6c-1/2/3 的人设都是 `simulated_agent`。所以给它们的外联：
   - `refusal_code` 一定是 `persona_excluded`，不可能是 `quiet_hours` 或 `frequency_capped`；
   - 勿扰时段的拒绝只能从 `refusal_gates` 里 `quiet_hours` 这一道的 `result='refuse'` 看到，这是“部分证明”；
   - **频率永远到不了 refuse**：频率只数已准入的外联（`runtime.nexloop_outreach_contacts_since`，0238），以及未绑定来信、属于触达类的 effect intent。s6c 的外联全被人设拦下，从不准入，计数一直是 0；激励发放走自己的账本，也不计入；
   - **#17 的 `fulfilled` 同样做不到**。

   OUT 第 5 片单把 s6c-1 预期为 `fulfilled`，按源码会是 `persona_excluded`。
2. **完整证明需要负责人决定（方案 R）**：用受治理 Action `set_consumer_persona` 把 **s6c-1** 的人设临时改为 `real`，做完再改回 `simulated_agent`。影响：
   - 改动期间，s6c-1 被当作真实顾客，进入指标、演进和评估的人设筛选；
   - 人设修订有审计；
   - S6 补跑已 PASS，window 已固定，不影响已有证据；但如果之后还有 S6 补跑，window 要避开这段时间。
   - 不选 R 时，按方案 S（只用 simulated_agent）执行：#18 只拿部分证明，#17 只证明“受理 → 准入 → 逐道闸记录”。
3. **激励没有人设闸**（0145 / 0153 的 `authz.nexloop_incentive_command` admit）。准入时依次检查：顾客不在擦除中、负责人控制、联系限制（带通知的发放对受限顾客整单拒绝）、D7（受限顾客只收负责人发的或回复本人来信的）、目录、每顾客周期上限、租户激励预算、真实派发开关。所以 #16 用 s6c 账号就能完整做。
4. **都是回环**：外联执行器只写 `LocalOutreachOutbox`，激励发放器只用回环 provider。`fulfilled` 只表示写进了本地回环，**不会有任何消息送达真实客户**。

## 1. 角色与授权

- **执行者**：调度员（命令和只读查询）。
- **人工步骤**：只用合成 owner `approver3`（工作台 Action）和合成顾客 s6c-1/2/3；负责人账号不参与。
- **判定者**：L6。证据里三者分开写。
- **需要负责人授权的**：
  - 方案 R 的人设变更（§0.2）；
  - B6 的计划复评 Run：真实模型调用，单 Run 上限 2 CNY，本单最多触发 1 次，合计不超过 2 CNY，计入租户月度模型预算；
  - 第 3 节若改租户勿扰设置（`set_outreach_controls`），属于配置变更。不改的话就在勿扰时段内执行，推荐这样做。
- **其余步骤不调用模型、不产生模型费用**：owner 发起的外联和激励都不经过模型；激励金额计入租户激励预算（回环，不出钱）。

## 2. 前提（全部只读；任一不满足就停，交回调度员）

`ro()` 用 DATA_HOST 上 `day1_ro.sh` 前 16 行的定义。`$T` 是租户 id；`$C1`、`$C2`、`$C3` 是 s6c-1/2/3 的 consumer_id（私有 `local-only/s6c-20261030/index.json`）。

| # | 前提 | 只读核对 |
|---|---|---|
| P1 | 外联执行器在运行 | APP_HOST：`docker inspect -f '{{.State.Status}} {{.RestartCount}}' nexloop-app-outreach-sender-1` 为 `running`；`docker logs --since 10m nexloop-app-outreach-sender-1 2>&1 \| grep -c 'background tick failed'` 为 0（I31 续期后） |
| P2 | 外联执行器只有回环 provider（OUT P9） | 沿用 OUT 单 P9 四项核对，或 `scripts/deploy/outreach_sender_p9.py` 的结论 |
| P3 | **激励发放器已部署** | APP_HOST：`docker inspect -f '{{.State.Status}}' nexloop-app-incentive-issuer-1` 为 `running`。不存在则 #16 判 blocked：要先按 compose 里的 `incentive-issuer` 服务（`deploy/stage/compose.app.example.yaml`）部署，部署件走 ack-policy |
| P4 | 激励执行者主体已登记 | `ro "select credential_id,status from authz.nexloop_service_credentials where credential_id like '%incentive-issuer%'"` 一行 `active`；`ro "select count(*) from authz.nexloop_authority_facts where fact_kind='grants' and entity_key[2] like '%nexloop.incentive.execute%'"` ≥ 1 |
| P5 | 真实派发开关（只记录，不改） | `ro "select scope_kind,scope_ref,enabled,data_mode,revision from control.nexloop_real_dispatch_switches where tenant_id='$T' and world='real' order by 1,2"`。看租户层和 `eios:action:nexloop.outreach.send:1`、`eios:action:nexloop.incentive.issue:1` 两行。激励那一行没有或关着时，发放会以 `real_dispatch_closed` 拒绝：这也是派发前拦截，但不是 #16 要的那一种，判 blocked 交负责人决定是否打开（回环） |
| P6 | 激励目录与绑定 | `ro "select type_name,object_id,schema_version,properties->>'status',properties->>'currency' from ontology.objects where tenant_id='$T' and type_name in ('IncentiveOffering','ConsumerIncentiveOffering') order by 1,2"`：至少一个有效的 IncentiveOffering，以及它和 `$C1`、`$C2` 的 ConsumerIncentiveOffering 绑定。没有就停，目录只能经受治理对象 Action 写入（D4），属于负责人操作 |
| P7 | 租户激励预算 | `ro "select unit,limit_amount,period_start,period_end,revision from control.nexloop_budget_limits where tenant_id='$T' and world='real' and budget_kind='incentive'"` 一行，`now()` 在期间内且有余额 |
| P8 | 统一审批（NX-056）是否要求激励审批 | `ro "select capability,version,action_version,quorum,approver_roles,condition from control.nexloop_approval_policies where capability like '%incentive%' order by version desc"`（0326；策略按能力而非租户）。要求审批时，§5 先产生 pending，由另一位成员批准；`approver3` 是请求方，不能自己批 |
| P9 | 外联目的 `commitment_followup` 已列出、已启用、不要审批 | `ro "select runtime.nexloop_outreach_purpose('$T','commitment_followup')"`：`listed` 和 `enabled` 都为 true，`definition.requires_approval` 为 false |
| P10 | 租户外联设置（勿扰、频率） | `ro "select runtime.nexloop_outreach_settings('$T')"`：记下 `time_zone`、`quiet_hours`、`per_24h`、`per_7d`。默认 Asia/Shanghai、21:00–09:00、1、2 |
| P11 | 人设 | `ro "select consumer_id,persona,revision from control.nexloop_consumer_personas where tenant_id='$T' and consumer_id in ('$C1','$C2','$C3')"`：三行都是 `simulated_agent` |
| P12 | 联系限制 | `ro "select consumer_id,active,rule_id from control.nexloop_contact_restrictions where tenant_id='$T' and world='real' and consumer_id in ('$C1','$C2','$C3')"`：`$C1` 无生效限制。`$C2` 有限制时直接用于 §5.2，没有就按 OUT §3.1 加一条 |

## 3. owner Action 脚本（私有，`local-only/b-tier/owner_action.py`，0700）

调用工作台受治理 Action `POST /api/v1/workbench/actions/<operation>`。登录、CSRF 和登出复用发布检出里的 `scripts/deploy/real_dispatch_switch.Workbench`，与 `confirm_not_delivered.py` 同一做法。口令只走 stdin，脚本必须是文件。Idempotency-Key 由配置内容派生，重跑即重放。

```python
#!/usr/bin/env python3
"""B-tier owner Action (S74): one governed workbench Action from a private JSON {"operation": ..., "body": {...}}.
    security find-generic-password -s nexloop/stage/approver3 -w | owner_action.py <release checkout> <origin> <ca-file> <username-file> <config.json>
Output: the HTTP status and the response's ids/state/code only (never the body text, username, password, cookie or token)."""
import hashlib, json, sys
repo, origin, ca, user_file, cfg = sys.argv[1:6]
sys.path.insert(0, repo + '/scripts/deploy')
from real_dispatch_switch import HttpsTransport, Workbench  # noqa: E402
config = json.load(open(cfg)); op = config['operation']; body = config['body']
assert op in ('send_outreach', 'issue_incentive', 'set_consumer_persona'), op
password = sys.stdin.readline().rstrip('\n'); username = open(user_file).read().strip()
assert username and len(password) >= 8, 'credentials'
wb = Workbench(HttpsTransport(origin, ca), origin); wb.login(username, password)
try:
    key = 'b-tier-' + hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:48]
    status, out = wb._call('POST', '/api/v1/workbench/actions/' + op, body, {'X-CSRF-Token': wb.csrf, 'Idempotency-Key': key})
    keep = ('intent_id', 'state', 'code', 'replayed', 'revision', 'refusal_code', 'approval_id')
    print(json.dumps({'operation': op, 'status': status, **({k: out[k] for k in keep if isinstance(out, dict) and k in out}),
                      'reason': (out.get('details') or {}).get('reason') if isinstance(out, dict) else None}, sort_keys=True))
finally:
    wb.logout()
```

配置文件（0600）每步一个，下文写为 `cfg-<步骤>.json`。

## 4. #18 勿扰时段与频率上限的拒绝路径

**执行时间**：B1 在租户勿扰时段内（默认北京时间 21:00–09:00，以 P10 读出的为准）；B2、B3 在勿扰时段外。不改租户设置。

### 4.1 方案 S（不改人设；部分证明）

**B1 勿扰时段内触发一次（s6c-1）**

```json
{"operation": "send_outreach", "body": {"consumer_id": "<C1>", "purpose": "commitment_followup",
 "origin_ref": "owner:b-tier-20261031-quiet", "body": "（合成）您预约的服务明天开始，如需调整请回复。", "channel": "native.webchat"}}
```

- 预期：受理（`state` 为 `accepted`）；执行器准入后为 `refused`，`refusal_code=persona_excluded`；`refusal_gates` 里 `quiet_hours` 是 `refuse`，detail 为勿扰区间。
- 只读核对见 §4.3 的 Q1、Q2，看 `quiet_hours` 那一道。
- 判定：`quiet_hours=refuse` 且回环 outbox 没有这条（Q3），记为“勿扰闸在派发时生效（被人设闸先拦）”。

**B2 同一顾客 24 小时内第 2 次**：方案 S 下**不做**。频率计数一直是 0（§0.1），做了只会再得到 `persona_excluded`，`frequency=pass`。证据里写“不可证明，原因见 §0.1”。

### 4.2 方案 R（负责人授权后；完整证明）

顺序：R0 改人设 → B1 → B2 → B3 → R9 改回。B1 在勿扰时段内，B2、B3 在次日勿扰时段外，B2 和 B3 之间不超过 24 小时。

- **R0**：`cfg-R0.json`，`{"operation":"set_consumer_persona","body":{"consumer_id":"<C1>","persona":"real","reason":"S74 B 档 stage 验证，临时；R9 改回"}}`。核对：P11 的查询中 `$C1` 为 `real`，revision 加 1。
- **B1 勿扰时段内**：配置同 4.1。
  - 预期 `refused`，`refusal_code=quiet_hours`。
  - 人设闸这时通过；勿扰排在频率之前，所以即使频率也拒，代码仍是 `quiet_hours`。
- **B2 勿扰时段外第 1 次（阳性，同时是 #17 的阳性对照）**：`origin_ref` 改为 `owner:b-tier-20261031-first`。
  - 预期 `fulfilled`，`admitted_at` 非空，`refusal_gates` 为空，`provider_reference` 非空。
  - 回环 outbox 有一条（Q3）。
  - 计入频率：Q4 的 `last_24h` 为 1。
- **B3 24 小时内第 2 次**：`origin_ref` 改为 `owner:b-tier-20261031-second`。
  - business intent id 含 origin_ref，所以这是新意图，不是重放。
  - 预期 `refused`，`refusal_code=frequency_capped`；`refusal_gates.frequency.detail.last_24h=1`。
  - 回环 outbox 没有这条。
- **R9**：`cfg-R9.json` 把 `persona` 改回 `simulated_agent`，理由“S74 B 档验证结束”。核对同 R0。**无论前面成败都要做**。

### 4.3 只读核对 SQL

```sql
-- Q1 本单的外联行（ids、状态、代码；不读正文）
select intent_id, origin_ref, initiator, state, refusal_code, created_at, admitted_at, finished_at, provider_reference is not null as has_provider_ref
  from runtime.nexloop_outreach_sends where tenant_id='$T' and world='real' and origin_ref like 'owner:b-tier-%' order by created_at;
-- Q2 逐道闸结论
select s.origin_ref, g->>'gate' gate, g->>'result' result, g->'detail' detail
  from runtime.nexloop_outreach_sends s, jsonb_array_elements(coalesce(s.refusal_gates,'[]'::jsonb)) g
 where s.tenant_id='$T' and s.world='real' and s.origin_ref like 'owner:b-tier-%' order by s.created_at;
-- Q4 当前频率计数
select runtime.nexloop_outreach_contacts_since('$T','real','$C1', now()-interval '24 hours') last_24h,
       runtime.nexloop_outreach_contacts_since('$T','real','$C1', now()-interval '7 days') last_7d;
```

Q3（APP_HOST，只数不读）：`sudo ls <outreach-sender 的 outbox 目录> | grep -c <intent_id 的哈希部分>`，拒绝的意图为 0，B2 为 1。目录以 OUT P9 读出的挂载为准。

## 5. #16 激励发放一次 + 联系限制拦截一次

前提 P3–P8 全部满足。下文中 `<O>`、`<rev>`、`<B1>`、`<B2>` 分别是 P6 读出的 offering_id、revision，以及对 `$C1`、`$C2` 的 binding_id；金额取目录单次上限以内的最小值，例如 100（分）。

### 5.1 发放一次（s6c-1，没有联系限制）

```json
{"operation": "issue_incentive", "body": {"consumer_id": "<C1>", "incentive_offering_id": "<O>", "offering_revision": <rev>,
 "binding_id": "<B1>", "amount_minor": "100", "currency": "CNY", "notification_text": "（合成）续卡券已发放。"}}
```

- 预期：受理为 `accepted`。P8 要求审批时为 pending，由另一位成员批准。
- 发放器准入后为 `fulfilled`，`admitted_at` 非空，`provider_reference` 非空；激励预算预留后结算（`fully_used`）。
- 方案 R 下如果在 B2 之前做，这一步不影响外联的频率计数：激励不计入 `nexloop_outreach_contacts_since`，见 §0.1。

### 5.2 联系限制拦截一次（s6c-2，有联系限制）

配置同上，`consumer_id` 换成 `<C2>`，`binding_id` 换成 `<B2>`，`notification_text` 非空。

- 预期：`refused`，`refusal_code=contact_restricted`。带通知的发放对受限顾客整单拒绝（0145 §4 admit）。
- provider 收到 0 个请求；激励预算没有预留。
- **注意**：`notification_text` 为空的 owner 发放，按 D7 对受限顾客是**允许**的；本步必须带通知。

### 5.3 只读核对

```sql
select intent_id, consumer_id like '$C1%' c1, consumer_id like '$C2%' c2, initiator, origin, state, refusal_code, amount_minor, currency,
       admitted_at, finished_at, provider_reference is not null as has_provider_ref
  from runtime.nexloop_incentive_issues where tenant_id='$T' and world='real' and created_at > '<本节开始时间>' order by created_at;
select budget_kind, sum(amount) from control.nexloop_budget_consumption
 where tenant_id='$T' and world='real' and budget_kind='incentive' and recorded_at > '<本节开始时间>' group by 1;
select action, request_id, created_at from runtime.audit_events
 where tenant_id='$T' and action like 'nexloop.incentive.issue.%' and created_at > '<本节开始时间>' order by created_at;
```

预期：C1 一行 `fulfilled`；C2 一行 `refused / contact_restricted`；预算消耗只有 C1 那一笔；审计里有 `nexloop.incentive.issue.refused` 一条。表和列已对照 0145、0068、0238 核过；执行前仍按 §8 在一次性库上跑一遍。

## 6. #17 外联第 5 片阳性对照

- **方案 R**：§4.2 的 B2（owner 发起的 `fulfilled`）就是外联发送和执行器准入的阳性对照。Agent 发起这一路再做 B6。
- **B6（Agent 发起；真实模型调用，需负责人授权，≤ 2 CNY）**：按 OUT 单 §3.2 给 s6c-1 建一个计划步骤，`reassess_at` 设在约 5 分钟后，并且在勿扰时段外、距 B2 超过 24 小时（否则会以 `frequency_capped` 拒绝，那就是又一次频率证明，不是阳性）。常驻 plan-reevaluator 认领后，Run 调用 `nexloop.outreach.send`。
  - 预期：`initiator='agent'`，带 `run_id`；方案 R 下为 `fulfilled`。
  - 前提：OUT 单的 G-1 到 G-3 已满足（Role 上限含发送，见 OUT §2.2、§2.3），P10（L6 S49）中 `plan.outcome` 已生效。
  - 核对：Q1（`origin_ref like 'plan:%'`、`initiator='agent'`），以及 OUT 单 §4 的查询。
- **方案 S**：B6 预期 `refused / persona_excluded`，只证明“Run → 受理 → 准入 → 逐道闸”这条链。

## 7. #19 客户画像属性修订（只读）

```sql
-- 合成顾客的 Consumer 对象：版本、更新时间、属性名（不读值）
select o.object_id like '$C1%' c1, o.object_id like '$C2%' c2, o.object_id like '$C3%' c3, o.schema_version, o.updated_at,
       (select array_agg(k order by k) from jsonb_object_keys(o.properties) k) property_names
  from ontology.objects o where o.tenant_id='$T' and o.type_name='Consumer' and o.object_id in ('$C1','$C2','$C3');
-- 每个对象的事件：类型、序号、时间、是谁（不读 payload）
select e.object_id like '$C1%' c1, e.event_type, e.sequence, e.actor_id, e.created_at
  from ontology.events e where e.tenant_id='$T' and e.object_type='Consumer' and e.object_id in ('$C1','$C2','$C3')
 order by e.object_id, e.sequence;
-- 由 Claim 匹配自动应用的属性修订（来源可追溯到 Claim）：事件里引用 claim 的条数
select e.object_id like '$C1%' c1, count(*) filter (where e.payload ? 'claim_id' or e.payload ? 'source_claims') from_claims, count(*) total
  from ontology.events e where e.tenant_id='$T' and e.object_type='Consumer' and e.object_id in ('$C1','$C2','$C3') group by 1;
```

- 预期：至少一个 s6c 顾客的 Consumer 对象有 S6 发言之后的属性修订事件，并且能追溯到 Claim。演示里的“客户画像 · 修订 N”就对应这些事件。
- 如果 `from_claims` 为 0，说明 stage 上画像更新没有走 Claim 自动应用这条路，#19 仍留在 B 档，并在证据里写明。
- payload 的键名（`claim_id` / `source_claims`）以 claim 应用代码为准，执行前在一次性库上核对（§8）。

## 8. 执行前的一次性库核对

§2、§4.3、§5.3、§7 的 SQL 先在一次性 PostgreSQL 上用与 `ro` 相同的会话（只读事务、`set local role nexloop_owner`、设置 `eios.tenant_id`）逐条执行，确认列名和函数签名，与 OUT 单 L6 S2 的做法相同。这只证明 SQL 可执行，不代表 stage 的数据。

## 9. 证据

- **执行者（调度员）**：
  - 执行时间（UTC，取自 `date`）；
  - P1–P12 的输出，`consumer_id` 只写前 12 位；
  - 每一步 `owner_action.py` 的输出行；
  - Q1–Q4、§5.3、§7 的输出；
  - 方案 R 的 R0、R9 修订号。
- **判定者（L6）**：
  - B1：`quiet_hours` 在派发时拒绝。方案 S 下作为 `refusal_gates` 的结论，方案 R 下作为 `refusal_code`。
  - B3：`frequency_capped`（只有方案 R）。
  - B2 / B6：阳性。
  - 5.1：`fulfilled`；5.2：`contact_restricted`，provider 0 请求。
  - #19：有可追溯修订。
  - 每项判 passed、failed、不可证明（写明原因）。
- 所有 `fulfilled` 都只表示写进了回环，不是送达真实客户。

## 10. 局限

- 方案 S 不能证明频率拒绝和外联 `fulfilled`，这是测试人设闸的设计结果（ADR-034），不是缺陷。
- 本单不打开、不关闭任何真实派发开关（P5 只记录），不启用营销目的，不给 s6c-1/2/3 以外的任何顾客触发。
- 真实渠道送达不在本单范围内（负责人 S74 决定：暂不新建任务）。
