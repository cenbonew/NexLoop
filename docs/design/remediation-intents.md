# NX-057 动作最终失败后的补救意图（设计稿，第 3 版）

- **状态**：设计稿与 ADR 草案，**不含实现**。
- **修订记录**：
  - 第 1 版 `8be2f9c4`。
  - 第 2 版按 L6 复核（`remediation-intent-review.md`）修订，逐条回应见 §18。主要改动：
    - **C-1**：去掉 `remediation.execute`，不做任何嵌套调用；
    - **C-2**：查明 0324 的窗口缺陷，在 R0 中修正；
    - **I-1**：生成条件改用 R0 的失败原因码；
    - **I-2 至 I-8**：链条与 origin 规则、自触发计数、熔断口径、两级紧急开关、definer 包装、审批可见性、与认领终态的边界；
    - **S-1 至 S-5**：一并写入。
  - 第 3 版按 L6 第二轮复核（针对 `2c9e7d81`，没有确认级问题，R0 至 R2 可以推进）修订：
    - **J-1**：R3 需要自己的摘要和调用回执（§6.3）；
    - **J-2**：如实写明 NX-024 自触发节流的实际行为，真正的上限由规划器自己执行（§6.2、§9.2）；
    - **建议**：统计口径统一按事件时间，删除 0324 中按意图计 failed 的分支（§8）；origin 不依赖 feed 中会被截断的 trigger 列表（§9.1）；写明 awaiting_approval 到期的处理（§4.2）；request_id 改为符合格式的 `rem.<id>.<n>`（§6.3）；
    - **R0 落地方式**：失败证据另建 `runtime.nexloop_effect_failures`，不改动效应事件表（§10）。
- **编号约定**：本文中的“0324”一律指**第二十二批的 `0324_outcome_classes`**（L7，NX-061 B5）。NX-056 分支上另有一个临时的 0324，组批时由调度员重新编号，与本文无关。
- **作者**：L7，2026-10-23；基点 main `76a9ab00`（第二十二批，含 NX-061 的 0324）。
- **依据**：
  - 负责人在 EIOS 能力缺口 Q2 中选了推荐项 A：v0.2 引入补救，先生成补救意图，再按规则决定直接执行还是先审批（L7 盘点 `6885a4fa` §A.5）；
  - 交接包 `docs/handoff/docs/06_ACTION_RUNTIME_AND_RECOVERY.md` §3：补偿是关联的另一项受治理 Action，可以标记 compensated，但不删除已发生的事实；
  - NX-056 统一审批（L3，`l3/unified-approvals-impl` 上的 `docs/design/unified-approvals.md` 与 0322，ADR-039 草案）。补救中凡需要人批准的步骤，**一律复用它**；
  - 上游冻结提交 `1e363db` 的补偿机制，只读对照（§2），不复制领域代码。

## 0. 先读这里：一页结论

1. **NexLoop 的失败很窄，预留在失败时已经原子退回**：
   - provider 协议没有“明确失败”的回答（只有 accepted、fulfilled、not_found，其余按 unknown 处理）；
   - 全库只有三条路径会把效应意图置为 `failed`（§1.1）；
   - 激励失败时，预算在同一个事务里就释放了。

   所以补救**不是逆操作**，而是最终失败之后的**业务跟进**。
2. **生成条件只看失败记录的原因码**（R0 新增的 `runtime.nexloop_effect_failures`）：`origin_expired`（来源 Run 过期且从未派发）和 `confirmed_not_delivered`（负责人带证据确认未送达）。
   - unknown 先对账；一次性回复被拒（`refused_one_reply:*`）以及一切按规则的拒绝都不生成补救；
   - 动作认领的 `permanent_failure`（例如 NX-056 的等待审批）也不生成补救（§3.4）；
   - 0324 的统计口径**只用于统计**，不作为生成条件（I-1）。
3. **三种补救**：
   - `reevaluate`：经 definer 包装函数唤醒原计划，触发原因为 `final_failure`，`cause='self'`；次数上限由规划器按补救表执行，NX-024 的自触发节流只起平滑作用（J-2）；
   - `escalate`：交给负责人；
   - `governed_action`：工作台用冻结参数**预填目标能力自己的请求**，由负责人本人直接提交这个目标能力。目标能力自己的权限检查和 NX-056 审批策略照常生效；补救意图按自己的预填摘要，经受治理入口写下的调用回执关联结果（J-1）。

   **不存在 `remediation.execute`，也没有任何嵌套调用处理函数的路径**（C-1）。
4. **防循环与刹车**：
   - 每个失败、每条规则最多一条补救；
   - 补救 Run 的 origin 由服务端派生，补救 Run 提出的动作再失败只升级（深度上限 1）；
   - 另有按 plan 和 consumer 计的链条上限，覆盖之后由别的 trigger 唤醒的 Run；
   - 熔断按“补救来源的失败”计数；
   - 有租户级和全局两级紧急开关。
5. **统计口径的缺陷**：0324 按意图的 `created_at` 取 1 小时窗口，路径③的失败（首条 unknown 必须已过 24 小时）**永远计不进失败率**，路径②大概率也计不进去。这是 NX-061 B5 的缺陷，**R0 中修正**：分子分母都改按事件时间计数，删除按意图计 failed 的那一支（C-2）。
6. **切片**：
   - R0：补齐失败记录，并修正 0324 的口径；
   - R1：只生成建议；
   - R2：自动唤醒计划；
   - R3：预填目标能力（依赖 NX-056 合入）；
   - R4：动作认领终态与统计联动。

   R0 到 R2 约 6–7 个开发日，不依赖 NX-056。

## 1. 现状（以基点代码为准）

### 1.1 失败从哪里来

| 路径 | 位置 | 条件 | 现在留下的证据 | R0 的原因码 |
|---|---|---|---|---|
| ① 一次性回复被拒 | `authz.nexloop_fail_refused_effect`（0152:68-95，置 failed 在 :91），调用方 `effect_execution.py:321-333` | 拒绝码 NXC05，原因为 already_answered、reply_window_closed 或 fallback_bound_elsewhere；意图是 accepted 且没有尝试 | 只有 `runtime.nexloop_dispatch_refusals` 里的码，**原因不落库** | `refused_one_reply:<reason>`（**不生成补救**） |
| ② 来源 Run 过期且从未派发 | `authz.nexloop_effect_origin_expired`（0174:14-50，置 failed 在 :36；先查派发控制 :30-32，被控制拒绝时返回 none，不置 failed），调用方 `settle_expired_origin` | 意图是 accepted 且没有尝试；已经在 dispatching 或 unknown 的只停止自动查询，不置 failed | **不写任何事件或原因** | `origin_expired` |
| ③ 负责人确认未送达 | `runtime.nexloop_governed_confirm_not_delivered`（0210:80-120，置 failed 在 :109） | 从 unknown 置为 failed；要求有 not_found 证据、首条 unknown 已过 24 小时、not_found 后再过 10 分钟（0214） | `runtime.nexloop_effect_determinations`（只追加） | `confirmed_not_delivered` |

还有一种常见情况（I-1）：路径②的意图在过期之前被 NXB02、NXC06 或 real 开关拒绝过，后来控制解除，来源凭据才过期，意图置为 failed。0324 因为这个意图“有过拒绝”而不把它计为 defect，**但它是真失败**。所以生成条件必须看原因码，而不是看 0324 的类别。

外联（0238）和激励（0145）各有独立台账和 `failed` 终态。激励的 failed 在同一事务中结算为 `released`，并写入负数的费用条目（0145:30、345-353）。

### 1.2 已有的“失败后补救”

- **回复保底**（`reply_fallback.py`、`reply-guarantee.v3.json`）：绑定回复失败后最多一次保底 Run，失败则升级（`control.nexloop_reply_escalations`，0109:111）。本设计不重复实现，也不改变它（§3.3）。
- **计划重评估**（NX-024，0106）：效应的观察写入时，触发器 `nexloop_plan_on_observation`（0106:170-184）以 `cause:'external'` 唤醒等待这个意图的计划。**②和③这两次最终失败都不会唤醒计划。**

### 1.3 已经存在但没有用起来的部分（I-8 补 file:line）

- `ActionCompensationMode`（`eios/ontology/definitions.py:533`）：所有 Action 都是默认值 none，全库没有地方读取。
- 动作认领表 `runtime.nexloop_action_claims`（0007:46），终态枚举为 `TerminalOutcomeStatus`（succeeded、permanent_failure、compensated，`eios/actions/models.py:372-375`）。
  - 基点中**只有两处终结效应认领，都用 SUCCEEDED**：`effect_execution.py:420-425`（finalize 命令）和 `effect_contexts.py:153`。
  - 三条失败路径都不碰认领：0152 没有任何认领语句；0174 只在注释中提到认领（:3、:23）；0210:3 写明 “never touches an Action claim”。
  - 所以失败意图的认领一直停在 active，直到它自己的有效期结束。
- NX-056（L3 分支的 0322）**会**把认领终结为 `permanent_failure`：在等待审批时（:12），以及处理函数拒绝时（:184）。§3.4 写明它与补救的边界。

### 1.4 会产生外部效应的 Action

依据 `action-effect-categories.v3.json`：

- `nexloop.service.request:1`：customer_contact；
- `nexloop.incentive.issue:1`：non_contact_service；
- `nexloop.enterprise.deliver:1`：customer_contact，external=true。

### 1.5 0324 的统计窗口缺陷（C-2）

- 0324:37-41 统计 `runtime.nexloop_effect_intents` 时，条件是 `created_at > now() - 1 hour`。
- 路径③要求首条 unknown 已过 24 小时，所以被确认失败的意图，创建时间必然早于 24 小时之前，**永远不进窗口**。
- 路径②的意图要等到来源 Run 凭据过期才会失败，也大多早于 1 小时之前创建。
- 结论：0324 的 `failure_rate` **低估**了这两类真失败。
- 修正放在 R0：重定义 0324 的快照包装函数，defect 按失败记录的 `recorded_at` 计，成功与 unknown 也改按各自事件的时间计（§8，第 3 版）。这是 L7 在 NX-061 B5 中留下的缺陷，记入本稿。

## 2. 上游的对照（只读，不复制）

`src/eios/composition/definition_runtime.py`（`_run_enterprise`、`_compensate`，约 790-910 行）：

- 只在 `PERMANENT_FAILURE`、`compensation_mode=AUTOMATIC`、外部写入结果不是未知时补偿；
- 恰好一次补偿尝试，走同一个受 fence 的端口（`EnterpriseAttemptKind.COMPENSATION`）；
- 补偿成功才变为 `COMPENSATED`；补偿失败保留如实的 `PERMANENT_FAILURE`；
- 补偿从不抛出异常。

| 上游原则 | 本设计 |
|---|---|
| 未知不补偿 | 保留 |
| 只尝试一次 | 保留：每个失败、每条规则一条补救；暂时性错误在同一条补救内最多重试 3 次 |
| 补救失败不改写原结局 | 保留：原意图的 `failed` 永远不改 |
| 补救不抛异常 | 保留 |
| 同一适配器做逆操作 | **不采用**：provider 协议不可靠，逆操作同样是一次没有幂等保证的外部写 |
| `compensation_mode` 写在 Action 定义上 | **不采用**：规则写在受信配置中（§5） |

## 3. 哪些失败生成补救意图

### 3.1 生成条件（I-1）

只有同时满足以下全部条件，才生成补救意图：

- R0 写入了一条 `failed` 效应事件，原因码是 `origin_expired` 或 `confirmed_not_delivered`；
- world 为 `real`；
- 有一条已发布的规则，与源 Action 的名称、**精确版本**和失败种类都匹配；
- 两级开关都处于开启状态（§9.4）。

### 3.2 不生成的情况

| 情况 | 原因 |
|---|---|
| unknown（包括 not_found 之后、确认之前） | 结果可能已经生效。先对账 |
| `refused_one_reply:*`，以及一切按规则的拒绝 | 系统按规则主动没有做 |
| authorization 类（NXB03、NXE01） | 先修配置。已有告警（如 `effect_route_unconfigured`） |
| transient 类（NXB02） | 派发路径自己会重试 |
| 由补救 Run 提出的意图再失败 | 深度上限 1：只升级（§9.1） |
| 链条上限已到（同一个 plan 或 consumer） | 只升级（§9.2） |
| world 不是 real | 模拟与影子世界不触达真实执行 |
| 动作认领的 `permanent_failure` | 见 §3.4 |

### 3.3 与回复保底的边界

绑定回复由回复保底负责。规则发布时，如果 source 是 `nexloop.service.request`，必须声明 `exclude_bound_replies=true`，否则拒绝发布。

### 3.4 与认领终态、审批等待的边界（I-8，写死为不变式）

1. 动作认领的 `permanent_failure`（NX-056 0322 的等待审批、处理函数拒绝，以及 R4 将来写入的失败认领）**不是**补救的生成条件，**不计入** 0324 的失败率，也**不计入**补救的熔断或速率统计。补救只看 R0 的失败记录。
2. `awaiting_approval` 和 `approval_required` 都不是失败，不计入熔断，也不计入速率上限中的失败计数。
3. `governed_action` 的目标能力被提交后，如果返回 `waiting_approval`，补救停在 `awaiting_approval`。**规划器从不重试，也从不重新提交**，人的提交是唯一入口（§6.3）。

每一条都配有测试（§16）。

## 4. 数据形态与状态机

### 4.1 补救意图

`runtime.nexloop_remediation_intents`：FORCE RLS；应用角色只能经 definer 函数访问。

| 列 | 含义 |
|---|---|
| `remediation_id` | uuid |
| `tenant_id`, `world` | world 恒为 `real` |
| `source_intent_id`, `source_action` | 失败的效应意图及其 Action，精确到版本 |
| `failure_kind`, `failure_id` | R0 失败记录（`runtime.nexloop_effect_failures`）的原因码与 id，作为证据 |
| `plan_id` | 等待这个意图的计划，由服务端查出；可以为空 |
| `rule_id`, `rule_version`, `rule_digest` | 生成时冻结 |
| `kind` | `reevaluate`、`escalate`、`governed_action` |
| `mode` | `automatic`，只用于 reevaluate 和 escalate；或 `manual`，即需要一个人来做。`governed_action` 恒为 manual，是否审批由目标能力自己的 NX-056 策略决定 |
| `state` | 见 4.2 |
| `attempts` | 只计暂时性错误的重试，最多 3 |
| `due_by` | 建议的有效期，默认 7 天 |
| `prefill_body`, `prefill_digest` | 只用于 `governed_action`：按规则的字段白名单从原意图冻结请求中逐字段复制的**目标能力请求体**；摘要是补救**自己定义的** `nexloop-remediation-prefill/1`（§6.3，J-1），不复用认领摘要或 NX-056 摘要 |
| `target_request_id`, `target_approval_id` | 由 `link` 动词从调用回执写入（§6.3） |
| `woken_plan_id`, `consumed_by_run` | 只用于 `reevaluate`：唤醒的 plan，以及消费这次唤醒的重评估 Run（§9.1） |
| `result_ref` | 结果引用：唤醒的 plan_id 与 feed 的 change_seq；或目标调用回执的 request_id（§6.3） |
| `last_code`, `created_at`, `updated_at` | |

- 约束 `unique(tenant_id, world, source_intent_id, rule_id)`。
- `runtime.nexloop_remediation_events` 只追加，每次状态变化一行：from、to、code、actor（service 或 human subject）、at。

### 4.2 状态机

```
proposed ──(reevaluate/escalate, automatic)──────────────────────▶ executing
proposed ──(governed_action：负责人提交预填的目标能力)──────────▶ awaiting_approval   （目标能力有 NX-056 策略时）
proposed ──(同上，目标能力没有审批策略)────────────────────────▶ executing
awaiting_approval ──目标审批被消费并执行─────────────────────────▶ executing
awaiting_approval ──目标审批被拒、过期或撤回─────────────────────▶ declined            （终态）
proposed ──(escalate，或 manual 中负责人选“标记已处理”)─────────▶ escalated           （终态：交给人）
executing ──完成，见 4.3────────────────────────────────────────▶ succeeded           （终态）
executing ──按规则被拒（conflict_protected）────────────────────▶ refused             （终态）
executing ──暂时性错误，attempts < 3────────────────────────────▶ proposed（退避 1、5、30 分钟）
executing ──其他失败，或 attempts 已达 3───────────────────────▶ failed → escalated   （终态）
executing ──结果 unknown 超过对账期限（48 小时，S-1）──────────▶ escalated            （原因 outcome_unknown_timeout，终态）
proposed / awaiting_approval ──到达 due_by，或不再适用──────────▶ expired / not_applicable（终态）
```

- **不适用**：consumer 删除中或已删除、计划已关闭或被取代、目标已结束、规则已撤下、开关关闭（§9.4，记为 `not_applicable:paused`）。进入 `executing` 之前每次都检查。
- 终态不可回退；原意图的 `failed` 不改。
- **awaiting_approval 的到期**（第 3 版）：
  - 补救停在 awaiting_approval 时，它的去向**只由目标审批决定**。规划器每轮按回执中的 `approval_id` 读取 NX-056 审批的状态（§6.3）：
    - 审批 approved 并被消费执行，补救进入 executing；
    - 审批 rejected、cancelled 或 expired，补救记为 `declined`，原因带上审批的终态。
  - NX-056 的审批有自己的 `due_by`（默认 24 小时），最终总会到达终态，所以 awaiting_approval 不会无限期挂着。
  - 补救自己的 `due_by` 在 awaiting_approval 期间**不生效**：审批一旦在进行中，就不能因为补救的期限而被跳过。
  - 规划器读到审批 expired 时，同样只记为 declined，**不会**替人重新发起审批。

### 4.3 “完成”的定义（I-4）

- **`reevaluate`**：
  - 计划的 feed 被成功触碰（`change_seq` 增加），就记为 `succeeded`。
  - 唤醒前发现计划已关闭等情况，记为 `not_applicable`。
  - 规划器自己的唤醒上限已到（§9.2），**不唤醒**，记为 `escalated`，原因为 `chain_capped`。
  - 第 2 版中的 `self_trigger_capped` 已删除：NX-024 的自触发节流不会拒绝，只会延后（J-2，§6.2），规划器观察不到这个状态。
  - “成功”只表示“已唤醒”。**熔断不看这个状态**，而是看补救来源的失败（§9.3），所以熔断对 reevaluate 规则一样能触发。
- **`escalate`**：写入升级记录，记为 `escalated`。
- **`governed_action`**：
  - 目标调用回执为成功，记为 `succeeded`；
  - 目标被处理函数拒绝（`execution_refused:*`），记为 `failed`，随后升级；
  - 按规则拒绝，记为 `refused`。

## 5. 规则（受信配置）

文件为 `deploy/configuration/remediation-rules.v1.json`，由 configurator 发布，在 SQL 中存成带版本、不可改的行（`control.nexloop_remediation_rules`）。

```json
{
  "schema_version": "nexloop-remediation-rules/1",
  "manifest_version": 1,
  "decision": "<负责人决定与日期>",
  "rules": [
    {
      "rule_id": "deliver_not_delivered",
      "source": {"stable_name": "nexloop.enterprise.deliver", "version": 1},
      "failure_kinds": ["confirmed_not_delivered"],
      "kind": "reevaluate",
      "mode": "automatic",
      "due_after_seconds": 604800,
      "purpose": "确认未送达后唤醒等待它的计划，由新的 Run 决定重试、告知客户还是升级"
    }
  ]
}
```

发布时的校验，一律失败关闭：

- source 必须是 `action-effect-categories` 中登记过的效应 Action，并且精确到版本。没有对应规则的版本不生成补救。
- `kind=governed_action` 时：
  - `target_capability` 必须是 `control.nexloop_governed_capabilities` 中 `subject_rule='human'` 的能力；不能是效应类 Action；
  - `mode` 必须是 `manual`；
  - 必须带 `prefill_map`：字段白名单，从原意图的冻结请求逐字段复制，不支持表达式；
  - 发布时用目标能力自己的请求校验来验证一份合成的预填请求体。
- `kind=reevaluate` 或 `escalate` 时，`mode` 可以是 `automatic` 或 `manual`。
- source 是 `nexloop.service.request` 时，必须 `exclude_bound_replies=true`。
- 同一个 source 版本和同一个 failure_kind，最多 1 条 reevaluate 规则加 1 条 governed_action 规则。

## 6. 由谁执行（只走受治理路径）

### 6.1 规划器（服务身份）

- 位于 domain worker 已有的定时 tick 中，**复用它现有的连接池**，没有新的池，也没有新的连接预算项（S-3）：
  - 每轮最多处理 50 条失败记录；
  - `statement_timeout` 为 5 秒，`lock_timeout` 为 1 秒；
  - tick 周期为 60 秒；
  - 用 `for update skip locked` 领取。
- 每个租户用该租户自己的服务身份 digest，不跨租户共用一个身份。
- 它只持有一项能力 `remediation.plan`，经受信配置授予（ADR-020），没有任何效应能力、渠道凭据或审批权。
- **它只调用 definer 包装函数**（I-6）：
  - `authz.nexloop_remediation_plan_command(p_digest, p_world, p_text, p_signature, p_payload)`；
  - 按 0174 的做法，用 `authz.nexloop_service_identity_snapshot(p_digest, p_world)` 在服务端得出租户和主体，**租户从不来自参数**；
  - 在行锁内重查：源意图确实是 failed；失败记录确实存在，且原因码在规则范围内；还没有这条补救；开关是开的；链条上限没到。
  - 动词：`propose`、`wake`（reevaluate）、`escalate`、`expire`。
- 现有的 `authz.nexloop_plan_feed_touch(p_tenant, …)`（0106:82）只供触发器和 definer 内部使用。0003:3、:7 已经把默认的 EXECUTE 从 PUBLIC 收回，基点中也没有任何 grant 授予它，所以服务角色不能直接调用。包装函数在内部调用它，R2 的测试要锁定“应用角色无权执行它”。

### 6.2 `reevaluate`

- 包装函数的 `wake` 动词调用 `plan_feed_touch`，trigger 为：

  ```
  {kind: 'final_failure', cause: 'self', intent_id, failure_kind, failure_id, remediation_id, at}
  ```

  `cause='self'`，因为失败源自这个计划自己的意图（I-3）。
- **NX-024 自触发节流的实际行为**（J-2，第 3 版如实改写）：
  - 0106:350-358 的节流只在**这个 feed 项合并后的所有 trigger 都是 `cause='self'`** 时才生效；只要合并进一条 `external`（例如 0106:179 的观测）或 `reassess_due`，节流就不看这一项。
  - 生效时预检返回 `throttled` 与 `retry_after_seconds`，这一项等窗口过去后再处理。它**只延后、从不拒绝或丢弃**，也不把任何状态告诉调用 `plan_feed_touch` 的一方。
  - 所以第 2 版写的“被自触发上限挡住、记为 `self_trigger_capped`”在基点代码里**观察不到**，这一状态已删除（§4.3）。
  - NX-024 的节流只是“别太密”的平滑，**不是**补救的上限。真正的上限由规划器自己在 `wake` 之前执行（§9.2），依据的是补救自己的表，而不是 feed。
- 之后由 NX-024 照常处理：确定性预检；需要判断时开一次有预算的重评估 Run。这个 Run 提出的每个动作都是新意图，**派发时重查**权限、联系约束（ADR-023）、real 开关（0153 三层）、控制版本、预算和一次性回复规则。

### 6.3 `governed_action`（C-1：没有嵌套调用）

1. 工作台“补救”一栏显示这条补救，并提供“按补救预填”按钮。按钮打开**目标能力自己的**提交表单（与工作台现有的受治理 Action 表单是同一个），请求体用 `prefill_body` 预填，人可以看到全部字段（I-7）。
2. 负责人确认后，**直接提交目标能力**，经 `authz.nexloop_goal_governed_action`，用自己当前的会话。于是：
   - 目标能力自己的 `assert_action_authority` 重查**请求人对目标能力**的现行权限；
   - 目标能力自己的 NX-056 审批策略（如果有）照常生效：等待审批、他人批准、请求人执行消费；
   - 处理函数以请求人本人的身份运行，`p_intent` 就是这次调用的 intent。

   与 NX-056 的不变式 1 一致。
3. **关联只读、只匹配摘要**（J-1，第 3 版重写）：
   - **为什么不能复用现有记录**：基点里没有可读的“受治理调用记录”。
     - 动作认领行（0007:46）的摘要**包含 request_id**，每次提交都不同，无法与预填比较；
     - NX-056 审批的 digest **包含策略版本**，策略一改就对不上 `prefill_digest`；
     - 无审批路径下，处理函数一旦拒绝，整个受治理调用的事务回滚，**不留任何记录**。
   - **补救自己的摘要** `nexloop-remediation-prefill/1`：对规范化 JSON（键排序、无空白）
     `{v:'nexloop-remediation-prefill/1', tenant, world, capability, action_version, body}` 取 sha256，其中 `body` 是去掉 `request_id` 的请求体。不含策略版本，也不含 request_id。生成补救时对 `prefill_body` 计算一次，存为 `prefill_digest`。
   - **request_id 格式**：`rem.<remediation_id>.<n>`。`remediation_id` 写成 32 位小写十六进制（不带连字符），`n` 是从 1 开始的提交序号，总长 38 至 40。
     - 它符合工作台 HTTP 层的幂等键格式 `[A-Za-z0-9._~-]{16,160}`（`review_http.py:123`、`web_chat_http.py:105` 等），也符合删除类处理函数自己的 `[A-Za-z0-9._:-]{1,190}`（0132:567）。
     - 第 2 版的 `remediation:…:n` 含冒号，**不符合**前一个格式，已改。
   - **调用回执** `runtime.nexloop_remediation_call_receipts`（R3 新表，FORCE RLS，只追加）：
     - 列：`request_id`（主键）、`remediation_id`、`tenant_id`、`world`、`capability`、`action_version`、`prefill_digest`（按上面的规则对**实际提交的**请求体计算）、`requester_subject`、`outcome`（`succeeded`、`refused`、`waiting_approval`）、`code`、`approval_id`、`recorded_at`；
     - **由受治理入口 `authz.nexloop_goal_governed_action` 写入**，而且只对 request_id 以 `rem.` 开头的调用写：
       - 处理函数成功，与业务写入同一事务写 `succeeded`；
       - NX-056 判定需要审批，写 `waiting_approval` 并带上 `approval_id`；
       - 处理函数拒绝时，入口在 **savepoint** 中调用处理函数，捕获拒绝后回滚到 savepoint，写 `refused` 与拒绝码，再把同一个拒绝原样抛给调用方。这是对 `rem.*` 调用的**唯一行为变化**：业务写入照样全部回滚，只多留一行回执。由于拒绝仍然抛出，外层事务若整体回滚，回执也会一起消失，所以入口要在**自己的**事务边界内完成“写回执、再抛出”，具体做法在 R3 立项时请 L6 专门复核；
     - 回执里的 `remediation_id` 只从 request_id 解析，入口不信任请求体中的任何补救字段；解析出的补救不存在或不属于本租户时，照常执行调用、**不写回执**。
   - 规划器的 `link` 动词只读回执，只有**目标能力与版本一致、回执的 `prefill_digest` 等于补救的 `prefill_digest`、租户与 world 一致、提交者是人类**时，才推进补救：
     - `succeeded`：按 §4.3 判定；
     - `waiting_approval`：进入 `awaiting_approval`，记下 `approval_id`，之后按 §4.2 跟随审批终态；
     - `refused`：留在 proposed，记下 `last_code`，人可以修正后再提交（n 加 1）。
   - 如果人改动了预填字段，摘要就不一致，补救不会被这次调用推进，仍停在 proposed，由人选择“标记已处理”；
   - request_id 由调用方提供，但伪造它不会带来任何权限：回执只记录真实发生的受治理调用，状态只依据回执推进。
4. 规划器**从不**代人提交，也不重试或重新提交（§3.4 第 3 条）。

### 6.4 明确禁止

- 规划器直接 insert 或 update 任何业务表或效应意图表；
- 重放原意图的冻结请求；
- 借用原意图来源 Run 的执行权（0042:306-318）；
- 由一个能力的处理函数调用另一个能力的处理函数（嵌套调用，C-1）。

## 7. 与 NX-056 统一审批的衔接

- 补救本身**没有**自己的审批策略。`governed_action` 是否需要审批，完全由**目标能力**在 NX-056 中的策略决定。例如目标是 `consumer.erase` 或 `retention.release_hold`，就走它们的首批策略。
- 审批人看到的是目标能力自己的 `summary_fields`（I-7）。补救一栏另外显示来源，即源 Action、失败种类和证据，供审批人对照。
- 状态对应：目标审批 waiting 时，补救是 `awaiting_approval`；目标审批被拒、过期或撤回时，补救是 `declined`；目标审批被消费执行后，补救按 4.3 判定。
- 依赖：NX-056 A4（执行与消费）合入后，R3 才能上线。本设计只依赖“审批通过后由请求人本人提交，目标能力自己的权限与策略生效”这一条，不改 NX-056 的任何语义。

## 8. 与 0324 运行结果分类的衔接

- **生成条件与 0324 解耦**（I-1）：补救只看 R0 的原因码。
- **R0 修正 0324 的效应计数**（C-2）。R0 的迁移 `create or replace` 0324 的快照包装函数（复制件守卫登记），**分子和分母一律按事件时间**（第 3 版）：
  - **succeeded**：1 小时内有 `fulfilled` 事件（`runtime.nexloop_effect_events.created_at`）的不同意图数；
  - **outcome_unknown**：**首条** `unknown` 事件落在 1 小时内的意图数；
  - **defect**：1 小时内 `runtime.nexloop_effect_failures` 中原因码为 `origin_expired` 或 `confirmed_not_delivered` 的行数，按 `recorded_at` 计；
  - `refused_one_reply:*` 的失败行**不计**，它已经按拒绝码计入拒绝类别；
  - **删除 0324 的 `intents` CTE**，也就是按意图 `created_at` 取窗口、按“是否有过拒绝”排除 failed 的那一支。不删的话，同一个失败会按意图计一次、又按失败行计一次。
  - 拒绝部分（`refusals` CTE）与死信部分不变。
  - 这样路径③的失败在被确认的那一小时内计入失败率；路径②曾被 NXB02 拒绝过的意图，过期时也计为 defect（I-1）。
- **失败率保持如实**：补救成功不抵扣原来的失败。R4 在 `outcomes` 中新增 `remediation` 子块：
  - 各状态的 1 小时计数；
  - `unremediated_defects`：1 小时内的 defect 事件中，既没有补救、也不在规则范围内的数量。依据修正后的口径，它不再恒为 0。
- 动作认领的 `permanent_failure` 不进入以上任何计数（§3.4）。
- 新的码（R0 的原因码）必须先在 0324 的分类表或新的事件分类中归类，守卫测试会报出来。

## 9. 上限、防循环与刹车

### 9.1 深度上限 1：origin 由服务端派生（I-2）

- 补救 Run 的判定**不读 feed 的 trigger 列表**（第 3 版）：0106:90 合并时只保留最新的 16 条 trigger，`final_failure` 可能被后来的观测挤掉。改为以补救表为准：
  - `wake` 成功时，补救记下 `woken_plan_id` 与 feed 的 `change_seq`，状态为 `succeeded`，`consumed_by_run` 为空；
  - NX-024 的启动器为一个 plan 开 Run 时，在同一事务里用 `for update` 取出这个 plan **所有 `consumed_by_run` 为空的已唤醒补救**，写上这个 Run 的 id。取到至少一条，这个 Run 就是补救 Run；
  - 这样无论 feed 项合并了多少 trigger，补救都不会丢；一次 Run 消费掉此前所有未消费的唤醒。
- 启动器在服务端把 `remediation_origin`（取其中最早的那个 `remediation_id`）写进这个 Run 的服务端记录（role run 与 Run 凭据的元数据），**不出现在任何工具参数中**，Run 和模型都无法自报或修改。
- 这个 Run 提出的**每一个**意图，在意图接收函数中由服务端从 Run 凭据继承 `remediation_origin`，写入意图的不可变列（R2 新增）。
- 带 origin 的意图失败时，规划器只生成 `escalate`。

### 9.2 链条上限（I-2）

- 有些意图是在补救 Run 设置了 `reassess_at` 之后，由 `reassess_due` 或 `commitment_due` 唤醒的 Run 提出的，它们不带 origin。为了覆盖这类情况，另设两个上限，**不看 origin**：
  - 同一个 plan：7 天内最多 2 次 `final_failure` 唤醒；
  - 同一个 consumer：24 小时内最多 3 条补救。
- **由谁执行**（J-2）：上限由规划器的 definer 包装函数在 `wake` 与 `propose` 之前、在行锁内执行。计数**只读补救自己的表**（按 plan 数 `woken_plan_id` 相同、7 天内 `wake` 成功的补救；按 consumer 数 24 小时内的补救行），不依赖 NX-024 的节流，也不读 feed。同一 plan 的并发规划经 `pg_advisory_xact_lock` 串行，两次计数不会同时读到旧值。
- 超出后**不唤醒**，只生成 `escalate`，原因为 `chain_capped`。这是可以观察到的状态（§4.3）。

### 9.3 熔断（I-4）

一条规则满足以下任一条件时进入 `held`：不再生成新的补救，并触发告警。

- 24 小时内带有这条规则 origin 的意图失败了 3 次；
- 24 小时内这条规则的补救因 `chain_capped` 升级了 3 次；
- 24 小时内这条规则的补救 failed 了 3 次（governed_action）。

解除用 `remediation.rule_release`，人类专用。

### 9.4 两级紧急开关（I-5）

- **全局（部署层）**：部署配置项 `remediation.enabled`，做法与 real 派发开关 0153 的部署层相同。默认关闭，上线时由调度员打开。
- **租户级**：人类受治理能力 `remediation.pause` 与 `remediation.resume`。
  - pause **立即生效、不需要审批**；
  - resume 是否需要审批，由负责人决定（§14 Q-6）。
- 规划器**在每一轮开始时，以及每次推进状态之前**都读取两级开关。关闭时：不生成新的补救；未执行的补救保持 proposed，到期则 expired；`executing` 中的补救不受影响，因为它们的动作已经交给受治理路径，各自受控制和派发开关约束。
- 租户的控制暂停（NXC01 的范围）同样使规划器跳过这个租户。

### 9.5 其他

- **速率上限**：每个租户每小时最多 20 条补救。超出后**每小时只写一条聚合升级**，带超出的数量（S-2），不逐条刷屏。
- **暂时性错误**：最多重试 3 次（1、5、30 分钟）。
- **unknown**：`governed_action` 的结果为 unknown 时只对账、不重试；48 小时仍未知就升级（S-1）。
- 所有数字都写在规则配置或部署配置中，不写死在代码里。

## 10. 审计与证据

- **R0**（第 3 版改为独立的表）：三条失败路径在原事务中各写一行 `runtime.nexloop_effect_failures`，**不改动** `runtime.nexloop_effect_events`。它的 `event_kind` 检查约束、`effect_fence` 和读取它的代码都保持原样。
  - 列：`failure_id`、`intent_id`（唯一，一个意图最多一行）、`tenant_id`、`world`、`reason_code`（`origin_expired`、`confirmed_not_delivered`、`refused_one_reply:<拒绝码>`）、`evidence`（determination id 或 not_found observation id、拒绝码、来源 Run id，只含 id 与码）、`recorded_at`；
  - FORCE RLS，属主 `nexloop_owner`，从应用角色收回全部权限，只由三个 definer 函数写入；
  - 需要重定义 0152、0174、0210 中的函数（新迁移，复制件守卫登记），同时修正 0324 的口径（§8）。
  - **没有失败行就不生成补救。**
- **每次状态变化**写一行 `runtime.nexloop_remediation_events`，同时写一条 `runtime.audit_events`（`nexloop.remediation.<to>`），只含 id、码和 digest。
- **证据链**：原意图 → 失败记录及其依据（determination、not_found observation）→ 规则版本与 digest → 唤醒的 plan 与重评估 Run（Context Manifest 能还原它看到了什么）或目标调用（request_id、审批 id、approved_by、回执）。
- **隐私**：正文与预填体不进审计和日志（AT-049）。`prefill_body` 只存在补救意图行中，受 RLS 保护，纳入 NX-029 的保留期与删除传播：consumer 被删除时，未终结的补救记为 not_applicable，已终结的替换为假名、只保留计数。
- **动作认领**（R4，按 Q-4 的推荐）：原意图 failed 时，认领终结为 `permanent_failure`；补救成功后也**不**推进为 `compensated`。§3.4 已写明认领的 permanent_failure 不进入任何补救或失败统计。

## 11. 工作台展示

- **“Action / 异常”页（`pages/Actions.tsx`）的“补救”一栏**：
  - 按状态分组：待处理、等待审批、执行中、已完成、已升级；
  - 每条显示源 Action、consumer（遵守 ADR-025 的成员读取规则；删除中的隐藏）、失败种类与时间、规则、kind、状态、`due_by`、尝试次数和最近的码。
- **操作**，均为人类受治理 Action：
  - `governed_action`：“按补救预填”，打开目标能力自己的表单，所有字段可见、可以修改（§6.3）；
  - “标记已处理”，记为 `escalated`，必须填写原因；
  - 待处理的补救可以“取消”，记为 `declined`，必须填写原因；
  - 租户开关 `remediation.pause` 与 `remediation.resume`。
- **证据抽屉**：按 §10 的证据链逐级展示 id、码和时间。
- **告警**（随 R1、R2 进入告警规则的新版本）：
  - `remediation_waiting`：proposed 超过 24 小时，warning；
  - `remediation_failed`：24 小时内大于 0，warning；
  - `remediation_rule_held`：大于 0，critical；
  - `remediation_rate_capped`：大于 0，warning；
  - `remediation_paused`：大于 0，info。
- 文案不用“已补偿”“已撤销”，统一叫“补救”。

## 12. 隐私、world 与租户

- 只有 world `real` 的失败生成补救。表 FORCE RLS，规划器包装函数从服务身份得出租户。
- 补救不能绕过“别再联系我”：面向客户的后续动作只能来自新 Run（派发时检查联系约束）或人类直接提交的目标能力（处理函数和派发路径照常检查）。

## 13. 最小可交付切片

| 切片 | 内容 | 依赖 | 估计 | 迁移 |
|---|---|---|---|---|
| **R0 证据与口径** | 新表 `runtime.nexloop_effect_failures`，三条失败路径写失败记录与原因码；**修正 0324 的效应计数**，分子分母都按事件时间，删除按意图计 failed 的分支（C-2）；复制件守卫登记 | 无 | 1.5–2 天 | 1 |
| **R1 只生成建议** | 规则配置与发布校验（先只开放 escalate）；补救意图表与事件表；规划器包装函数（propose、escalate、expire）；唯一约束、速率聚合、链条上限、不适用判定；两级开关；工作台“补救”一栏只读加“标记已处理”和取消；告警 | R0 | 2.5–3 天 | 1 |
| **R2 自动唤醒计划** | reevaluate 的 automatic；`wake` 动词与 `final_failure`（`cause='self'`）；规划器执行的唤醒上限；启动器按补救表消费唤醒并派生 origin，Run 凭据加意图的不可变列；熔断 | R1 | 2 天 | 1 |
| **R3 预填目标能力** | 规则的 governed_action（target、prefill_map）及发布校验；补救自己的预填摘要；受治理入口为 `rem.*` 写调用回执（含 savepoint 中捕获的拒绝，需 L6 专门复核）；工作台“按补救预填”；`link` 动词按回执关联；awaiting_approval 跟随审批终态；首批目标由负责人定（Q-2） | R1、**NX-056 A4 合入** | 2–2.5 天 | 1 |
| **R4 认领终态与统计联动** | 失败时认领终结为 permanent_failure；`outcomes.remediation` 与 `unremediated_defects`；证据抽屉 | R0、R1 | 1–1.5 天 | 1 |

合计约 9–11 天。

## 14. 需要负责人拍板

| 编号 | 问题 | 选项 | 推荐 |
|---|---|---|---|
| Q-1 | 首批覆盖范围 | A. 效应意图的 `origin_expired` 与 `confirmed_not_delivered`<br>B. A 加上外联和激励台账的 failed | **A** |
| Q-2 | R3 首批的目标能力 | 现有的人类能力中选，或者暂不设置 | **暂缓到有具体需求时** |
| Q-3 | reevaluate 能否默认自动执行 | A. 两种失败都自动<br>B. 只有 origin_expired 自动 | **A**：唤醒本身没有外部效应，并且受规划器的唤醒上限约束 |
| Q-4 | 补救成功后，原认领是否标为 compensated | A. 不标<br>B. 标 | **A** |
| Q-5 | 上限的默认值 | 租户每小时 20 条；plan 7 天 2 次唤醒；consumer 24 小时 3 条；熔断 24 小时 3 次；暂时性重试 3 次；unknown 对账期限 48 小时；建议有效期 7 天 | 按此执行，写在配置中 |
| Q-6 | 租户 resume 是否需要审批 | A. 不需要（与 pause 对称）<br>B. 挂 NX-056 策略，quorum 1 | **A**：pause 是刹车，resume 只是恢复到已经批准过的规则 |
| Q-7 | 全局开关的默认值 | 默认关闭，上线时打开 | 按此执行 |

## 15. ADR-040 草案

> **ADR-040｜最终失败后的补救意图**
>
> 状态：草案（L7，2026-10-23，第 3 版），待 L6 复核与负责人决定。
>
> 背景：负责人在 Q2 中决定引入补救。NexLoop 的效应只有三条路径会终结为 failed，预留类副作用在失败时已经原子退回；上游的“同一适配器逆操作”不适合 NexLoop 的 provider 协议。
>
> 决定：
> 1. 只有 R0 失败记录中原因码为 `origin_expired`（来源 Run 过期且从未派发）或 `confirmed_not_delivered`（负责人带证据确认未送达）、world 为 real、并且有精确匹配的规则时，才生成补救意图。unknown 先对账；一次性回复被拒、按规则的拒绝、配置缺失和动作认领的 permanent_failure 都不生成。
> 2. 补救有三种：
>    - `reevaluate`：经规划器的 definer 包装函数唤醒原计划，触发原因为 `final_failure`，`cause='self'`；
>    - `escalate`：交给负责人；
>    - `governed_action`：工作台用冻结参数预填**目标能力自己的请求**，由负责人本人直接提交，目标能力自己的权限检查与 NX-056 审批策略照常生效；补救意图按自己定义的预填摘要，经受治理入口为 `rem.*` 请求写下的调用回执关联结果。
>
>    不存在逆操作，不重放原意图，也不存在嵌套调用处理函数的入口。
> 3. 规划器只持有 `remediation.plan`，只调用 definer 包装函数，租户从服务身份得出；不新增直接 SQL，也不替换身份。
> 4. 每次失败、每条规则最多一条补救；补救 Run 的 origin 由启动器按补救表中未消费的唤醒在服务端派生（不读会被截断的 feed trigger 列表）并由意图继承，带 origin 的意图再失败只升级；按 plan 和 consumer 设链条上限，由规划器自己执行，不依赖 NX-024 的自触发节流；熔断按补救来源的失败计数；速率超限时每小时聚合一次升级；有全局与租户两级紧急开关。数值都在受信配置中。
> 5. 原意图的 failed 与失败率不因补救而改变；失败率的分子分母都按事件时间计入（修正 0324）；补救结果单独统计。
> 6. 补救规则是受信配置，精确到 Action 版本；没有规则的版本不生成补救。
>
> 后果：计划不再一直等待已经失败的意图；负责人能看到每一次真失败及其处理情况；面向客户的后续动作仍受联系约束、real 开关、预算和目标能力的审批约束。代价是新增四张表（失败记录、补救意图、补救事件、调用回执）、一份配置、一个规划器包装函数和一个 tick，受治理入口对 `rem.*` 请求多写一行回执；`governed_action` 依赖 NX-056。
>
> 被否决的方案：
> - 上游的同适配器自动逆操作；
> - `compensation_mode` 写在 Action 定义上；
> - 补救成功后把原失败从失败率中扣除；
> - 允许补救的补救；
> - **补救能力在自己的处理函数中嵌套调用目标能力的处理函数**（第 1 版的 `remediation.execute`）：会绕过请求人对目标能力的权限检查，以及目标能力自己的审批策略；
> - 以 0324 的 defect 类别作为生成条件：会漏掉曾被拒绝、后来才失败的真失败；
> - 用认领摘要或 NX-056 审批摘要关联预填：前者含 request_id，后者含策略版本，都对不上；
> - 依赖 NX-024 的自触发节流作为上限：它只在全部 trigger 为 self 时生效，而且只延后。

## 16. 测试清单（随各切片实现）

- **R0**：
  - 三条路径各写一行失败记录，原因码与证据正确；回滚的事务不留记录；同一意图最多一行；
  - 应用角色不能读写失败表；`runtime.nexloop_effect_events` 的约束不变；
  - **路径③的失败计入确认那一小时的失败率**（C-2）；路径②中曾被 NXB02 拒绝、后来过期的意图计为 defect（I-1）；`refused_one_reply:*` 不重复计数；
  - 两小时前创建、刚才 fulfilled 的意图计入 succeeded；一小时内创建、尚未 fulfilled 的不计入；首条 unknown 在窗口外的不再计入 outcome_unknown。
- **R1**：
  - `origin_expired` 与 `confirmed_not_delivered` 各生成恰好一条，重复检测返回同一条；
  - unknown、`refused_one_reply`、NXE01、NXB02、模拟世界、认领 permanent_failure（模拟 0322 的等待审批）都不生成；
  - 没有规则的 Action 版本不生成；
  - 速率超限时一小时只有一条聚合升级；
  - 链条上限生效；
  - **全局开关关闭、租户 pause 时，不生成也不推进**；pause 不需要审批；
  - 包装函数从服务身份得出租户，伪造参数中的租户无效；
  - 应用角色不能直接写补救表，也不能执行 `plan_feed_touch`；
  - 工作台读取遵守成员读取规则。
- **R2**：
  - `final_failure` 的 trigger 为 `cause='self'`；规划器的唤醒上限（plan 7 天 2 次、consumer 24 小时 3 条）达到时不唤醒、记为 `chain_capped`；并发规划不会超出上限；
  - 同一 feed 项先后合并 1 条 `final_failure` 和 16 条以上观测 trigger 后，启动的 Run 仍是补救 Run（按补救表消费，不读 trigger 列表）；一次 Run 消费掉此前所有未消费的唤醒；
  - 补救 Run 提出的所有意图都带 origin；origin 不能由工具参数设置；
  - 带 origin 的意图失败只升级；
  - **重评估 Run 想联系一个已经“别再联系我”的客户时，派发被 NXC05 拒绝**；
  - real 开关关闭时不产生外部效应；
  - 熔断对 reevaluate 规则能触发（带 origin 的失败达到 3 次）。
- **R3**：
  - 预填打开的是目标能力自己的表单；
  - **目标能力有首批 NX-056 策略时（如 `consumer.erase`），必须经目标能力的审批**；
  - 请求人对目标能力没有权限时被拒；
  - 改动预填字段后，摘要不一致，补救不被推进；改动 NX-056 策略版本后，未改字段的提交仍能关联；
  - request_id 为 `rem.<32 位十六进制>.<n>`，通过 HTTP 层幂等键校验；
  - 无审批路径下处理函数拒绝时，业务写入全部回滚，但留下一行 `refused` 回执，拒绝原样返回调用方；非 `rem.*` 请求的行为与基点完全一致；
  - 返回 `waiting_approval` 时补救停在 `awaiting_approval`，规划器不重试也不重提交；审批 rejected、cancelled、expired 时补救记为 declined；补救自己的 due_by 在等待审批期间不生效；
  - 效应类 Action 作为目标时发布被拒；
  - 伪造 request_id 不推进补救。
- **R4**：
  - 认领在失败时终结为 permanent_failure，补救成功后仍是 permanent_failure；
  - `outcomes.remediation` 与 `unremediated_defects` 正确，且不为恒 0。

## 17. 风险

- R2 会提高重评估 Run 的频率。受规划器的唤醒上限与链条上限、NX-024 的自触发平滑（`cause='self'`）与 Run 预算约束；上线后要观察重评估队列与成本。
- R0 是硬前置，同时修正已经上线的 0324 口径。修正后失败率**会升高**，因为以前漏计的真失败计进来了。上线说明里要写明这是口径修正，而不是退化。
- R3 依赖 NX-056 A4；如果消费语义变化，R3 只需要调整关联判定（§6.3）。
- 新增的人类能力：`remediation.pause`、`remediation.resume`、`remediation.rule_release`、标记已处理与取消。工作台成员读取要把补救表纳入 ADR-025、ADR-035 的派生规则。

## 18. 对 L6 复核的逐条回应

| 编号 | 处理 | 位置 |
|---|---|---|
| J-1（第 3 版） | 补救自己定义预填摘要 `nexloop-remediation-prefill/1`（不含 request_id 和策略版本）；R3 新增调用回执，由受治理入口为 `rem.*` 请求写入，拒绝在 savepoint 中捕获后留回执；request_id 改为 `rem.<id>.<n>` | §4.1、§6.3、§13 R3、§15、§16 R3 |
| J-2（第 3 版） | 如实写明 0106:350-358 只在全部 trigger 为 self 时生效、只延后；删除 `self_trigger_capped`；真正的上限由规划器按补救表执行，并发经咨询锁串行 | §4.3、§6.2、§9.2、§15、§16 R2 |
| 建议（第 3 版） | 分子分母统一按事件时间并删除 0324 的 `intents` 分支；origin 改按补救表中未消费的唤醒派生，不受 limit 16 影响；awaiting_approval 跟随审批终态；R0 另建失败表，不改效应事件表 | §8、§9.1、§4.2、§10 |
| C-1 | 去掉 `remediation.execute`；预填目标能力的表单，由请求人直接提交目标能力；关联只读、只按摘要匹配；ADR 被否决方案中加入“嵌套调用” | §0、§6.3、§7、§15、§16 R3 |
| C-2 | 查明 0324:37-41 按意图 `created_at` 取窗口的缺陷；R0 改为按失败事件时间计数；补测试 | §1.5、§8、§13 R0、§16 R0、§17 |
| I-1 | 生成条件改为 R0 原因码；0324 只用于统计；ADR 决定 1 改写 | §1.1、§3.1、§8、§15 |
| I-2 | origin 由服务端派生（第 3 版改为按补救表的未消费唤醒），经 Run 凭据由意图继承；按 plan 和 consumer 设链条上限 | §9.1、§9.2 |
| I-3 | `final_failure` 的 `cause='self'`；第 3 版写明 NX-024 节流只起平滑作用，上限见 J-2 | §6.2、§16 R2 |
| I-4 | reevaluate 的“成功”只表示已唤醒；熔断改为按补救来源的失败和链条升级计数 | §4.3、§9.3 |
| I-5 | 全局部署开关（默认关闭）加租户 pause/resume；规划器每轮、每次推进前都检查 | §9.4、§14 Q-6、Q-7 |
| I-6 | 核实：`plan_feed_touch` 没有 grant，0003:3、:7 已收回 PUBLIC 的默认 EXECUTE；规划器改用 definer 包装函数，按 0174 的做法从服务身份得出租户，在锁内重查；每个租户一个服务身份 | §6.1 |
| I-7 | 按 C-1 修改后，审批人看到的是目标能力自己的 summary_fields 与完整的预填表单 | §6.3、§7 |
| I-8 | 认领 permanent_failure 不触发补救、不计入任何统计；awaiting_approval 不算失败；返回 waiting_approval 时不重试也不重提交；§1.3 补上 file:line | §1.3、§3.4、§16 |
| S-1 | unknown 对账期限 48 小时，到期升级 | §4.2、§9.5 |
| S-2 | 速率超限后每小时一条聚合升级 | §9.5 |
| S-3 | 复用 domain worker 的池；每轮 50 条；语句超时 5 秒、锁超时 1 秒；skip locked | §6.1 |
| S-4 | ADR 决定 1 改写；被否决方案加入两条 | §15 |
| S-5 | 测试清单补齐 | §16 |
