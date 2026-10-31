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
- **负责人已授权（2026-10-31 会话确认，经调度员转达）**：
  - **方案 R**：s6c-1 人设临时改为 `real`，验证完改回（§4.0）；
  - **B6**：Agent 发起一次主动外联，真实模型调用，单 Run 上限 2 CNY，沿用 100 CNY / 7 天预算；外联只走回环（§6）；
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

**重跑即重放**：同一个 cfg 重跑会重放上一次的结果（Idempotency-Key 由配置内容派生）。不要为了“再试一次”去改 `reason` 或其他字段，否则会变成一次新的请求；对 `set_consumer_persona` 来说，就是一次新的人设修订。

## 4. #18 勿扰时段与频率上限的拒绝路径

**执行时间**：B1 在租户勿扰时段内（默认北京时间 21:00–09:00，以 P10 读出的为准）；B2、B3 在勿扰时段外。不改租户设置。

### 4.0 人设切换窗口（方案 R，负责人已授权）

`R0`（改为 real）和 `R9`（改回）由 **approver3** 在负责人终端执行，用 §3 的 `owner_action.py`，Action 是 `set_consumer_persona`。

```json
// cfg-R0.json
{"operation": "set_consumer_persona", "body": {"consumer_id": "<C1>", "persona": "real", "reason": "S74 B 档 stage 验证，临时（负责人 2026-10-31 会话确认）；R9 改回"}}
// cfg-R9.json
{"operation": "set_consumer_persona", "body": {"consumer_id": "<C1>", "persona": "simulated_agent", "reason": "S74 B 档 stage 验证结束，改回"}}
```

```bash
security find-generic-password -s nexloop/stage/approver3 -w | python3 local-only/b-tier/owner_action.py <release 检出> https://<APP_HOST_NAME>:<PORT> "$HOME/.nexloop/stage/endpoint-20261015/ca-bundle.crt" local-only/b-tier/approver3.username local-only/b-tier/cfg-R0.json
# 验证结束后（无论 B1-B6 成败）同样执行 cfg-R9.json
```

**前后只读核对**（R0 之前、R0 之后、R9 之后各一次）：

```sql
-- 当前人设
select consumer_id like '$C1%' c1, persona, revision, updated_at from control.nexloop_consumer_personas where tenant_id='$T' and world='real' and consumer_id='$C1';
-- 人设事件：窗口的起止时间以这里为准
select previous, persona, revision, set_by, recorded_at from control.nexloop_consumer_persona_events where tenant_id='$T' and world='real' and consumer_id='$C1' order by revision;
```

- 预期：R0 之前是 `simulated_agent`；R0 之后是 `real`，revision 加 1，新增一条 `simulated_agent → real` 事件；R9 之后回到 `simulated_agent`，再加一条 `real → simulated_agent` 事件。
- **时间窗** `[W0, W1)`：W0 是 R0 事件的 `recorded_at`，W1 是 R9 事件的 `recorded_at`。两个时间都写进证据，取自上面的查询，不要估计。

**窗内的影响**：
- 人设在读取时才求值（`runtime.nexloop_consumer_persona` 取当前值，0220）。所以窗内任何按人设筛选的读取都把 s6c-1 当作 `real`，包括它在窗口之前的历史记录。涉及的读取有：
  - 演进轨迹读取 `authz.nexloop_evo_trace_runs`，以及它的 `personas` 参数（0233）；
  - 外联证据（0251）；
  - NX-043 的分配和比较（0289 起）；
  - 运营指标里与人设相关的部分。
- 窗内不发起任何演进或评估读取，也不跑 S6 / NX-043 的阶段脚本。调度员在 R0 之前确认没有排定这类任务。

**窗后的排除方法**：R9 之后，s6c-1 的全部记录又标为 `simulated_agent`，窗内产生的 Run（B2、B6 等）也一样，会进入以后 `personas` 含 `simulated_agent` 的演进读取。排除办法：
1. **首选**：以后演进或评估的 `window` 不与 `[W0, W1)` 重叠，`since` 取 W1 或之后；
2. 必须覆盖这段时间时，把窗内 s6c-1 的 Run 列成排除清单，从读取结果中剔除，并在证据里写明剔除了多少。清单由下面的只读 SQL 生成：

```sql
-- 演进读取所用的同一个内部函数（0233，0313 重定义），只取 s6c-1 在窗内的 run_id
select x->>'run_id' run_id, x->>'issued_at' issued_at
  from jsonb_array_elements(runtime.nexloop_evo_trace_runs('$T','real','<W0>'::timestamptz,'<W1>'::timestamptz,null,null,1000,
       array['real','simulated_agent','synthetic'])->'runs') x
 where x->>'consumer_id'='$C1' order by 2;
```

（输出一页最多 1000 条；超出时用上一页最后一条的 issued_at 和 run_id 作为 `p_after_at`、`p_after_run` 翻页。）
3. 窗内留下的外联和激励行（Q1、§5.3）是本单的证据，按 `origin_ref like 'owner:b-tier-%'` 或 `plan:` 加时间窗识别，不属于业务数据。

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

**L6 的四个条件（负责人已选 R）**：

1. **压缩时间窗，全程约 1 小时，不隔夜**（以租户勿扰 21:00–09:00、北京时间为例，按 P10 读出的设置换算）：

   | 约北京时间 | 步骤 |
   |---|---|
   | 08:30 | R0（勿扰时段快结束时） |
   | 紧接着 | B1（仍在勿扰时段内） |
   | 09:00 之后马上 | B2 |
   | B2 之后 | B3 |
   | 随即 | R9 |

2. **R0 之前的只读确认**（任一不满足就不开始）：
   - 没有自动发起方会在窗内触达 s6c-1，SQL 见 §4.2a；
   - I36（未结束承诺作废）已完成；
   - **B6 不放进这个窗口**（见 §6）。
3. **证据**：
   - 记 R0、R9 的执行时间（取自 `date -u`）和人设修订号（§4.0 的事件查询），作为指标和演进必须排除的时间段；
   - 同一时间段写进 S6 运行单的注意事项（`docs/release/run-s6-stage-prep.md` §6 之后的“排除时段”一节，本提交已加上该节；W0、W1 的实际值由调度员在执行后填进证据）。
4. **B2 的 `fulfilled` 只表示写进回环**：证据照抄 §0.4，不写“已送达”。

- **R0**：见 §4.0。
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
- **R9**：见 §4.0。B3 之后随即执行，**无论前面成败都要做**。

#### 4.2a R0 之前：窗内没有自动触达 s6c-1 的来源（只读）

```sql
-- 进行中的 Goal / PlanStep（指向 s6c-1）
select type_name, count(*) from ontology.objects where tenant_id='$T' and type_name in ('Goal','PlanStep','Plan')
   and properties::text like '%'||'$C1'||'%' and coalesce(properties->>'status','') not in ('completed','cancelled','closed','done','superseded') group by 1;
-- 会触发跟进的未结束承诺（made_to = s6c-1）
select properties->>'status' status, count(*), min(properties->>'due_at') from ontology.objects where tenant_id='$T' and type_name='Commitment'
   and properties->>'made_to' like '%'||'$C1'||'%' and properties->>'status' in ('conditional','open','in_progress','breached') group by 1;
-- 排队或运行中的计划复评任务（plan-reevaluator 的作业）
select capability_name, status, count(*) from runtime.jobs where tenant_id='$T' and status not in ('succeeded','failed','dead_lettered')
   and normalized_input::text like '%'||'$C1'||'%' group by 1,2;
-- 未完成的外联与激励意图
select 'outreach', state, count(*) from runtime.nexloop_outreach_sends where tenant_id='$T' and world='real' and consumer_id='$C1' and state in ('accepted','pending_approval','dispatching','unknown') group by 2
union all select 'incentive', state, count(*) from runtime.nexloop_incentive_issues where tenant_id='$T' and world='real' and consumer_id='$C1' and state in ('accepted','dispatching','unknown') group by 2;
```

- 预期：四个查询都没有行。有行就不开始，交回调度员处理：例如先按 I36 作废承诺，或等作业结束。
- 四个查询已在本地一次性库上试跑，执行无错（见 §8）。`runtime.jobs` 的终态取值以 stage 读出的为准：先跑一次 `select status, count(*) from runtime.jobs where tenant_id='$T' group by 1`，再决定排除哪些。

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
- **B6（Agent 发起；真实模型调用；不在方案 R 的时间窗内，L6 条件 ②）**：在 R9 之后执行，此时 s6c-1 已改回 `simulated_agent`，预期 `refused / persona_excluded`，证明“计划复评 Run → 受理 → 准入 → 逐道闸”这条链。如果要 Agent 发起这一路也拿到 `fulfilled`，需要另开一个满足同样条件的窗口，由负责人另行决定。
  - **授权依据**：负责人 2026-10-31 会话确认（经调度员转达）。
  - **成本上限**：单 Run ≤ 2 CNY（计划复评 Run 预算 4 轮模型、12 次工具、2 CNY），本单只触发 1 次；沿用 100 CNY / 7 天预算。执行前后各读一次租户模型预算消耗，差额写进证据：`ro "select sum(amount) from control.nexloop_budget_consumption where tenant_id='$T' and world='real' and budget_kind='model' and recorded_at > now()-interval '7 days'"`。
  - **回环约束**：外联执行器的 provider 只有 `LocalOutreachOutbox`（P2）；不满足 P2 就不做 B6。
  - 步骤：按 OUT 单 §3.2 给 s6c-1 建一个计划步骤，`reassess_at` 设在约 5 分钟后，并且在勿扰时段外、距 B2 超过 24 小时（否则会以 `frequency_capped` 拒绝，那就是又一次频率证明，不是阳性）。常驻 plan-reevaluator 认领后，Run 调用 `nexloop.outreach.send`。
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
-- 由 Claim 匹配产生的属性修订（可追溯到 Claim：nexloop_mutation_proposals，0070）
select p.target_ref like '%'||'$C1'||'%' c1, p.property_name, p.status, count(*), max(p.updated_at)
  from ontology.nexloop_mutation_proposals p where p.tenant_id='$T' and p.world='real' and p.type_name='Consumer'
   and (p.target_ref like '%'||'$C1'||'%' or p.target_ref like '%'||'$C2'||'%' or p.target_ref like '%'||'$C3'||'%')
 group by 1,2,3 order by 1,2,3;
```

- 预期：至少一个 s6c 顾客的 Consumer 对象有 S6 发言之后的属性修订事件，并且能追溯到 Claim。演示里的“客户画像 · 修订 N”就对应这些事件。
- 如果第三个查询没有 `status='applied'` 的行，说明 stage 上画像更新没有走 Claim 自动应用这条路，#19 仍留在 B 档，并在证据里写明。

## 8. 执行前的一次性库核对

§2、§4.3、§5.3、§7 的 SQL 先在一次性 PostgreSQL 上用与 `ro` 相同的会话（只读事务、`set local role nexloop_owner`、设置 `eios.tenant_id`）逐条执行，确认列名和函数签名，与 OUT 单 L6 S2 的做法相同。这只证明 SQL 可执行，不代表 stage 的数据。

**L3 本地试跑（2026-10-31，main 80b1009b 的 schema）**：
- 夹具：`tests/test_semantic_access_record_pg.py` 的 `semantic`，即全量 bootstrap 加合成租户 A。consumer_id 用占位哈希。
- 会话：只读事务，`set local role nexloop_owner`，设置 `eios.tenant_id`。
- 结果：20 条全部执行无错。

| 查询 | 结果 |
|---|---|
| P4a | 0 行 |
| P4b | 1 行（0）|
| P5 / P6 / P7 / P8 / P11 / P12 | 各 0 行 |
| P9 | 1 行：`listed=true`、`enabled=true`、`commitment_followup` |
| P10 | 1 行：`per_24h=1`、`per_7d=2`、`source=default`、marketing 关 |
| Q1 / Q2 | 各 0 行 |
| Q4 | 1 行（0, 0）|
| §5.3 三条 | 各 0 行 |
| §7 前两条 | 各 0 行 |
| §4.0 人设事件 | 0 行 |

- §7 第三条最初按 `ontology.events.payload` 的键名写，键名无法从源码确认，已改为 `ontology.nexloop_mutation_proposals`（0070），重新试跑执行无错，0 行。
- 夹具里没有激励目录、预算、人设和外联数据，所以只验证了列名和函数签名。
- §4.0 的排除清单 SQL（`runtime.nexloop_evo_trace_runs`）也已本地试跑，执行无错，0 行。
- P4b 的匹配方式（`entity_key[2] like '%nexloop.incentive.execute%'`）只证明能执行。stage 上读出 0 时，改用 `ro "select entity_key from authz.nexloop_authority_facts where fact_kind='grants' and entity_key::text like '%incentive%'"` 再看一次。

## 9. 证据

- **执行者（调度员）**：
  - **P1–P12 每一条的输出**（逐条原样记录，`consumer_id` 只写前 12 位；B 档 L6 补充 b）；
  - §4.2a 四个查询的输出；
  - R0、R9 的 `date -u` 时间和人设修订号（排除时段）；
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
