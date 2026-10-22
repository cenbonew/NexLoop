# NX-057 动作最终失败后的补救意图（设计稿，第 1 版）

- **状态**：设计稿与 ADR 草案，**不含实现**。待 L6 复核，之后交负责人决定首批规则与几个拍板点（§14）。
- **作者**：L7，2026-10-23；基点 main `76a9ab00`（第二十二批，含 NX-061 的 0324 运行结果分类）。
- **依据**：
  - 负责人在 EIOS 能力缺口 Q2 中选了推荐项 A：v0.2 引入补救，补救先生成一个“补救意图”，再按规则决定是直接执行还是先审批（L7 盘点 `6885a4fa` §A.5）；
  - 交接包 `docs/handoff/docs/06_ACTION_RUNTIME_AND_RECOVERY.md` §3：补偿是关联的另一项受治理 Action，可以标记 compensated，但不删除已发生的事实；
  - NX-056 统一审批（L3，`l3/unified-approvals-impl` 上的 `docs/design/unified-approvals.md` 第 5 版，ADR-039 草案）。补救中凡需要人批准的步骤，**一律复用它**；
  - 上游冻结提交 `1e363db` 的补偿机制，只读对照（§2），不复制领域代码。

## 0. 先读这里：一页结论

1. **NexLoop 的“失败”比上游少得多，也窄得多**：
   - provider 协议没有“明确失败”的回答，只有 accepted、fulfilled、not_found，其余一律按 unknown 处理；
   - 全库只有三条路径会把效应意图置为 `failed`（§1.1）；
   - 激励发放失败时，预算已经在同一个事务里**原子退回**了。

   因此补救意图**要处理的不是机械撤销**（撤销预留、退回权益在 NexLoop 中已经由各自的台账在失败的那一刻完成），而是**最终失败之后的业务跟进**：让计划知道失败并重新评估、把客户的事情接着办下去、或者把事情交给负责人。
2. **只有“真失败”生成补救意图**。按 0324 的结果分类，就是没有自带拒绝记录的 `failed`（类别为 defect）：
   - **unknown 先对账，不算失败**；
   - 按规则拒绝（例如联系受限、被人工接管）**不算失败**；
   - 配置缺失（authorization 类）要先修配置，不由补救来处理。
3. **三种补救**，执行都只走现有的受治理路径，**不新增直接 SQL，也不替换身份**：
   - `reevaluate`：唤醒原计划，重新评估。由新的 Run 按当前状态决定下一步，它提出的任何动作照常经过全部派发检查（权限、联系约束、real 开关、预算）；
   - `escalate`：只提醒负责人，由人处理；
   - `governed_action`：一个明确的人类受治理能力。由负责人在工作台发起，**经 NX-056 审批**后，由发起人本人提交。
4. **防循环**：
   - 每个失败意图、每条规则最多生成一条补救；
   - 补救产生的动作如果再失败，**只能升级给负责人**，不再生成补救（深度上限 1）；
   - 补救自身只在暂时性错误时重试，最多 3 次；
   - 每个租户每小时有补救数量上限；某条规则连续失败时会被暂停。
5. **最小切片**：
   - R0：补齐失败证据；
   - R1：只生成建议并在工作台展示；
   - R2：自动唤醒计划；
   - R3：人工补救加审批，依赖 NX-056 合入；
   - R4：动作认领终态与结果分类联动。

   R0+R1+R2 约 5–6 个开发日，可以独立上线，不依赖 NX-056。

## 1. 现状（以基点代码为准）

### 1.1 失败从哪里来

| 路径 | 位置 | 条件 | 现在留下的证据 | 0324 中的类别 |
|---|---|---|---|---|
| ① 一次性回复被拒 | `authz.nexloop_fail_refused_effect`（0152:68-95），调用方 `effect_execution.py:321-333` | 拒绝码 NXC05，原因为 already_answered、reply_window_closed 或 fallback_bound_elsewhere；意图是 accepted 且没有尝试 | 只有 `runtime.nexloop_dispatch_refusals` 里的码，**原因不落库** | conflict_protected（不是失败） |
| ② 来源 Run 过期且从未派发 | `authz.nexloop_effect_origin_expired`（0174:14-50），调用方 `settle_expired_origin` | 意图是 accepted 且没有尝试；已经在 dispatching 或 unknown 的只停止自动查询，不置 failed | **不写任何原因或事件** | defect |
| ③ 负责人确认未送达 | `runtime.nexloop_governed_confirm_not_delivered`（0210:80-120） | 从 unknown 置为 failed；要求有 not_found 证据、首条 unknown 已过 24 小时、not_found 后再过 10 分钟（0214） | `runtime.nexloop_effect_determinations`（只追加）：kind、observation_id、confirmation_ref、reason、decided_by、reply_followup | defect |

另有两套独立台账：外联（0238）和激励（0145），各自有 `failed` 终态。其中激励的 failed 在同一个事务里结算为 `released`，并写入负数的费用条目（0145:30、345-353），**预算退回已经内建**。

### 1.2 已有的“失败后补救”

- **回复保底**（`reply_fallback.py`、`reply-guarantee.v3.json`）：绑定回复明确失败后，**最多一次**保底 Run，失败则升级（`control.nexloop_reply_escalations`）。0210 的 `reply_followup=fallback_reply` 也能在确认失败后再触发一次。**这就是消息回复的补救**，本设计**不重复实现，也不改变它**（§3.3）。
- **计划重评估**（NX-024，0106）：效应的观察（accepted、fulfilled、not_found）写入时，触发器 `nexloop_plan_on_observation` 会唤醒等待这个意图的计划。**但②和③这两次“最终失败”的转换都不会唤醒计划**，计划可能一直在等一个已经确定失败的意图。这是本设计 R2 要补的缺口。

### 1.3 已经存在但没有用起来的部分

- vendored 的 `ActionCompensationMode`（none、automatic、manual，`eios/ontology/definitions.py:533`）：所有 Action 都是默认值 none，全库没有地方读取它。
- vendored 的 `TerminalOutcomeStatus`（succeeded、permanent_failure、compensated，`eios/actions/models.py:372-375`）：NexLoop 的每一处 finalize 都只用 SUCCEEDED，**没有任何地方把认领终结为 permanent_failure 或 compensated**；意图失败时，认领保持原样。

### 1.4 会产生外部效应的 Action

依据 `deploy/configuration/action-effect-categories.v3.json`：

- `nexloop.service.request:1`：customer_contact，WebChat 回复；
- `nexloop.incentive.issue:1`：non_contact_service；
- `nexloop.enterprise.deliver:1`：customer_contact，唯一一个 external=true 的 Action。

外联发送 `outreach.send` 走自己的台账。

## 2. 上游的对照（只读，不复制）

`src/eios/composition/definition_runtime.py`（`ActionRuntime._run_enterprise`、`_compensate`，约 790-910 行）：

- 只在 `PERMANENT_FAILURE`、`compensation_mode=AUTOMATIC`、并且外部写入结果**不是未知**时，才补偿；
- **恰好一次**补偿尝试，走同一个受 fence 的端口，尝试类型为 `EnterpriseAttemptKind.COMPENSATION`；
- 补偿成功，结局才变为 `COMPENSATED`；补偿失败或被拒，保留如实的 `PERMANENT_FAILURE`，回执仍作为证据追加；
- 补偿**从不抛出异常**：“补偿是补救，不是第二个失败来源”。

| 上游原则 | 本设计 |
|---|---|
| 未知不补偿 | 保留：unknown 先对账，不生成补救（§3.1） |
| 只尝试一次 | 保留：每个失败、每条规则只有一条补救；暂时性错误在同一条补救内最多重试 3 次（§9） |
| 补救失败不改写原结局 | 保留：原意图的 `failed` 永远不改；补救失败只会升级 |
| 补救不抛异常 | 保留：补救失败只更新补救自己的状态，不影响原意图或其他流程 |
| 由同一个适配器做逆操作（`execute_authorized(..., COMPENSATION)`） | **不采用**：NexLoop 的执行器没有“逆操作”，也不应该有。provider 协议不可靠，盲目逆操作同样是一次没有幂等保证的外部写。补救一律是**新的**受治理动作（§6） |
| `compensation_mode` 写在 Action 定义上 | **不采用**：规则写在受信配置中（§5），不改 Action 定义，避免发布 Action 新版本时连带改变补救语义 |

## 3. 哪些失败生成补救意图

### 3.1 规则

**生成条件**：效应意图进入 `failed`，**并且**在 0324 中的类别为 defect（没有自带拒绝记录）。具体是 §1.1 的路径②和路径③。外联、激励台账的 failed 是否纳入，首批不纳入，留给负责人决定（§14 Q-1）。

**不生成的情况**：

| 情况 | 原因 |
|---|---|
| unknown（包括 not_found 之后、确认之前） | 结果可能已经生效。先对账，确认失败（路径③）之后才进入补救 |
| 路径①，以及一切按规则的拒绝（conflict_protected、superseded） | 系统按规则主动没有做，不是失败 |
| authorization 类（NXB03 预算未配置、NXE01 路由未配置） | 要先修配置。由已有告警提示（`effect_route_unconfigured` 等），补救帮不上忙 |
| transient 类（NXB02） | 由派发路径自身的重试处理 |
| 由补救产生的动作再失败 | 深度上限 1：只升级，不再补救（§9） |
| world 不是 real | 模拟与影子世界的失败不生成补救；它们在自己的世界里观察，不触达真实执行 |

### 3.2 失败种类

`failure_kind` 只有两个值：

- `origin_expired`：路径②，从未离开过系统；
- `confirmed_not_delivered`：路径③，负责人带证据确认。

以后如果 provider 协议加入“明确的永久失败”，再加第三个值，届时需要新的 ADR。

### 3.3 与回复保底的边界

绑定回复（`nexloop.service.request` 中带 `reply_to_message_id` 的）由回复保底负责。**补救规则不能覆盖绑定回复**：规则发布时如果 source 是 `nexloop.service.request`，必须声明 `exclude_bound_replies=true`，否则拒绝发布。两套机制因此不会对同一次失败各补救一次。

## 4. 数据形态与状态机

### 4.1 补救意图

`runtime.nexloop_remediation_intents`：FORCE RLS，按 tenant 和 world 隔离，应用角色只能经 definer 函数访问。

| 列 | 含义 |
|---|---|
| `remediation_id` | uuid |
| `tenant_id`, `world` | world 恒为 `real`（§3.1） |
| `source_intent_id` | 失败的效应意图 |
| `source_action` | 如 `eios:action:nexloop.enterprise.deliver:1` |
| `failure_kind` | `origin_expired` 或 `confirmed_not_delivered` |
| `failure_evidence` | 证据引用：路径③是 `determination_id` 与 `observation_id`；路径②是 R0 新增的失败事件 id（§10） |
| `rule_id`, `rule_version`, `rule_digest` | 生成时冻结 |
| `kind` | `reevaluate`、`escalate`、`governed_action` |
| `mode` | `automatic`、`approval`、`manual` |
| `depth` | 恒为 0。由补救产生的意图记 `remediation_origin`，自身不能再生成补救 |
| `state` | 见 4.2 |
| `attempts` | 只计暂时性错误的重试，最多 3 |
| `due_by` | 建议的有效期，按规则配置；默认 7 天 |
| `approval_id` | NX-056 的审批 id，只用于 `governed_action` |
| `frozen_parameters` | 只用于 `governed_action`：按规则的字段白名单，从原意图的冻结请求中**逐字段复制**，不做计算 |
| `result_ref` | 结果引用：唤醒的 plan_id 与重评估 Run 的 id，或受治理调用的 request_id 与回执 |
| `last_code` | 最近一次失败或拒绝的码 |
| `created_at`, `updated_at` | |

约束：

- `unique(tenant_id, world, source_intent_id, rule_id)`：每个失败、每条规则只有一条补救；
- `source_intent_id` 引用 `runtime.nexloop_effect_intents`。

`runtime.nexloop_remediation_events` **只追加**，每次状态变化一行：remediation_id、from、to、code、actor（service 或 human subject）、at。

### 4.2 状态机

```
proposed ──(automatic)──────────────────────────────▶ executing
proposed ──(approval: 负责人点“发起补救”，经 NX-056)──▶ awaiting_approval ──approved──▶ executing
proposed ──(manual: escalate 或负责人决定)──────────▶ escalated            （终态：交给人）
awaiting_approval ──rejected / expired / cancelled──▶ declined             （终态）
executing ──成功──────────────────────────────────▶ succeeded              （终态）
executing ──按规则被拒（conflict_protected）──────▶ refused                （终态：例如联系受限、暂停中）
executing ──暂时性错误，attempts < 3──────────────▶ proposed（带退避，等下一轮）
executing ──其他失败，或 attempts 已达 3─────────▶ failed → 自动 escalated  （终态）
proposed ──到达 due_by，或不再适用────────────────▶ expired / not_applicable （终态）
```

- **不适用**的情况：consumer 正在删除或已删除、计划已关闭或被取代、源意图所在的目标已结束、规则已撤下。`executing` 之前每次都检查。
- **终态不可回退**。`succeeded` 只表示补救动作本身成功，原意图的 `failed` 不改（06 文档 §3）。
- **`reevaluate` 的“成功”**定义为“计划已被唤醒，并且重评估 Run 已经领取”。之后 Run 做什么、是否成功，属于新的意图，由它们自己的状态表达。

## 5. 规则（受信配置）

文件为 `deploy/configuration/remediation-rules.v1.json`，由 configurator 发布，在 SQL 中存成带版本、不可改的行（`control.nexloop_remediation_rules`），做法与效应类别、回复保底相同。

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

- source 必须是 `action-effect-categories` 中登记过的效应 Action，并且精确到版本。Action 发布新版本后，没有对应规则的版本**不生成补救**（提示缺口，不猜测）；
- `kind=governed_action` 时：
  - `remediation_action` 必须是 `control.nexloop_governed_capabilities` 中 `subject_rule='human'` 的能力，与 NX-056 的不变式 3 相同；
  - `mode` 只能是 `approval` 或 `manual`，**不能是 automatic**；
  - 必须带 `parameter_map`，取值为字段白名单，例如 `{"commitment_ref": "frozen_request.commitment_ref"}`，不支持表达式；
  - 选 `approval` 时，`remediation.execute` 必须已经有对应的 NX-056 审批策略；
- `kind=reevaluate` 或 `escalate` 时，`mode` 可以是 automatic，因为它们本身不产生外部效应（§6）；
- source 是 `nexloop.service.request` 时，必须 `exclude_bound_replies=true`（§3.3）；
- 同一个 source 版本和同一个 failure_kind，最多 1 条 `reevaluate` 规则加 1 条 `governed_action` 规则，避免多条规则对同一次失败同时唤醒。

## 6. 由谁执行（只走受治理路径）

| kind | 执行者 | 走的路径 | 为什么安全 |
|---|---|---|---|
| `reevaluate` | 补救规划器（retention keeper 或 domain worker 的定时 tick，服务身份），持有一项受信配置授予的能力 `remediation.plan`（ADR-020 的服务授权方式） | 调用现有的 `authz.nexloop_plan_feed_touch`，新增 kind `final_failure`，附带 intent_id、failure_kind 和证据引用。之后由 NX-024 照常处理：确定性预检；需要判断时开一次有预算的重评估 Run | **本身不写任何业务对象，也没有外部效应**，只是唤醒计划。新 Run 提出的每一个动作都是新意图，**派发时重查**权限、联系约束（ADR-023，没有例外）、real 派发开关（0153 三层）、控制版本、预算和一次性回复规则。新意图带 `remediation_origin`（§9） |
| `escalate` | 同上 | 写入一条升级记录（复用 `control.nexloop_reply_escalations` 的形态，新增原因 `final_failure`，或另建 `control.nexloop_remediation_escalations`，由 R1 实现时选定），并在工作台展示 | 只给人看 |
| `governed_action` | **工作台中的人类成员**，作为请求人 | 新增 human 能力 `remediation.execute`，请求体为 `{remediation_id}`。它的处理函数读取补救意图的冻结参数，在**同一个受治理入口、同一个请求人身份**下调用规则指定的人类能力的处理函数；整个调用受 NX-056 审批策略门控 | 与 NX-056 的不变式 1 一致：审批通过后，由请求人用自己当前的会话重新提交；不存在“代他人调用 handler”；处理函数自身的全部校验照常执行。效应类 Action **不能**作为 `governed_action` 的目标（§5） |

明确禁止：

- 补救规划器直接 `insert` 或 `update` 任何业务表或效应意图表。补救意图的写入只通过它自己的 definer 函数，状态迁移在 SQL 中校验；
- 重放原意图的冻结请求去“再发一次”。原意图的 `(goal, plan_step, slot, action)` 永远只对应一个意图（0040 的唯一约束）；要再发，只能由新 Run 用新的 slot 提出；
- 使用原意图的执行权。来源 Run 过期后不能借用（0042:306-318）。

## 7. 与 NX-056 统一审批的衔接

- 只有 `governed_action` 用到审批。挂接点是 `remediation.execute` 这一个 human 能力，审批策略写在 NX-056 的 `approval-policies` 中：
  - capability 为 `remediation.execute`，action_version 为 1；
  - quorum 为 1，approver_roles 为 owner，期限 24 小时；
  - summary_fields 为 `source_action`、`failure_kind`、`kind`、`remediation_action`。这些字段由服务端从补救意图渲染，**不展示冻结参数的正文**。
- 请求摘要按 NX-056 §6 的规则计算，请求体只有 `{remediation_id}`。冻结参数随补救意图一起冻结，所以审批批准的就是那一份参数。
- **单人模式**按 NX-056 §15，没有任何特殊处理。
- 状态对应：补救意图的 `awaiting_approval` 对应审批的 `waiting_approval`；审批 rejected、expired 或 cancelled 时，补救意图记为 `declined`；审批被消费并执行后，补救意图进入 `executing`，最终是 `succeeded`、`refused` 或 `failed`。
- 本设计**不改 NX-056 的任何语义**。只有一个依赖：NX-056 A4（执行与消费）合入之后，R3 才能上线。

## 8. 与 0324 运行结果分类的衔接

- **生成条件直接复用 0324 的口径**：只有类别为 defect 的失败生成补救。0324 已经保证“一次性回复被拒后终结为 failed”的意图只按其拒绝码计一次、不算 defect，补救因此**天然不会**为联系受限这类按规则的拒绝补救。
- **失败率保持如实**：原意图仍然是 defect，补救成功**不会**把它从失败率中抹掉；这与上游“补救不改写原结局”一致。快照的 `outcomes` 段新增一个子块 `remediation`，在 R4 实现：
  - `proposed`、`awaiting_approval`、`succeeded`、`refused`、`failed`、`escalated` 的 1 小时计数；
  - `unremediated_defects`：1 小时内的 defect 中，既没有补救、也没有被规则排除的数量。

  负责人看到的是“失败了多少，其中多少已经在补救”，而不是一个被补救冲淡的失败率。
- **补救自身产生的意图**（重评估 Run 提出的新效应）照常计入 `outcomes`。它们的拒绝按码归类；它们失败时也计为 defect，但只升级、不补救。
- **新的码**（例如 R0 新增的失败原因码）必须先在 0324 的分类表中归类，否则按 unresolved 计入失败（0324 的守卫测试会报出来）。

## 9. 上限与防循环

| 机制 | 规则 |
|---|---|
| 一次失败一条补救 | `unique(source_intent_id, rule_id)`；同一次失败重复检测到时直接返回已有的那一条 |
| 深度上限 1 | 重评估 Run 提出的新意图带 `remediation_origin=<remediation_id>`，记在意图的不可变列中，由 R2 新增，派发时冻结；这类意图失败时，规划器**只生成 escalate**，不生成其他补救 |
| 暂时性错误重试 | 同一条补救内最多 3 次，退避 1 分钟、5 分钟、30 分钟；第 4 次直接 failed，然后升级 |
| unknown 不重发 | `governed_action` 的处理函数如果产生了 unknown 结果，补救停在 `executing`，等结果对账，不重试 |
| 租户速率上限 | 每个租户每小时最多 20 条 proposed（按 real 计）。超出的记为 `escalated`，原因为 `rate_capped`，并触发告警 |
| 规则熔断 | 一条规则在 24 小时内有 3 条补救 failed，就自动进入 `held`：不再生成新的补救，并告警，由负责人复核后解除。解除是受治理 Action `remediation.rule_release`，人类专用 |
| 计划侧的上限 | `reevaluate` 唤醒的重评估仍然受 NX-024 自身的自触发窗口与上限约束（`plan-reevaluation.v1.json`）。补救不能绕过它，只是 feed 中多了一种原因 |

这些数字是建议的默认值，都写在规则配置中，不写死在代码里。

## 10. 审计与证据

- **R0 先补证据缺口**：
  - 路径②（0174）不写任何事件，路径①（0152）不记拒绝原因。R0 在 `runtime.nexloop_effect_events` 中新增事件种类 `failed`，带固定的原因码：`origin_expired`、`confirmed_not_delivered`、`refused_one_reply:<reason>`，三条路径各自在原事务中写入；
  - 这需要重定义 0152、0174、0210 中的函数，写一个新迁移，在复制件守卫中登记；
  - **没有这份证据，就不生成补救**（失败关闭）。
- **每次状态变化**写一行 `runtime.nexloop_remediation_events`，同时写一条 `runtime.audit_events`：action 为 `nexloop.remediation.<to>`，载荷只有 id、码和 digest。
- **证据链**（每条补救都能从工作台逐级点开）：
  - 原意图的 id 与 action；
  - 失败证据：determination 及其 not_found observation，或 R0 的失败事件；
  - 规则的版本与 digest；
  - 审批的 id 与 `approved_by`（NX-056）；
  - 执行结果：plan_id 与重评估 Run 的 id（Run 的 Context Manifest 能还原它看到了什么，M14、AT-027），或受治理调用的 request_id 与回执。
- **不记什么**：消息正文、冻结参数的正文都不进审计和日志（AT-049）。冻结参数只存在补救意图行中，受 RLS 保护，并纳入 NX-029 的保留期与删除传播：consumer 被删除时，未终结的补救记为 `not_applicable`，已终结的替换为假名、只保留计数。
- **动作认领**（R4，需要拍板 Q-4）：
  - 原意图 failed 时，把它的动作认领终结为 `permanent_failure`。目前从不终结，认领一直悬着；
  - 补救 `succeeded` 时，是否把原认领从 permanent_failure 推进为 `compensated`？vendored 的状态机允许这样做，上游也是这么做的。但 NexLoop 的补救是“跟进”而不是“逆操作”，这样标记容易被误读成“原来那次效应被撤销了”。**建议不推进**：只在补救意图上记录 succeeded，原认领保持 permanent_failure。

## 11. 工作台展示

- **“Action / 异常”页（`pages/Actions.tsx`）新增“补救”一栏**，按状态分组：待处理（proposed、awaiting_approval）、执行中、已完成、已升级。每条显示：
  - 原 Action 的显示名、consumer（按 ADR-025 的成员读取规则；删除中的隐藏）；
  - 失败种类与证据的时间；
  - 规则、kind、mode、状态、`due_by`、尝试次数、最近的码。
- **操作**：
  - `governed_action` 且 mode 为 approval：“发起补救”，即 `remediation.execute`，随后出现在 NX-056 的审批页中；
  - mode 为 manual：“标记已处理”，记为 `escalated`，必须填写原因；
  - 待处理的补救可以“取消”，记为 `declined`，必须填写原因。

  这三个都是人类受治理 Action。
- **证据抽屉**：按 §10 的证据链逐级展示，只显示 id、码和时间，正文在有权限时再按现有页面查看。
- **概览与可观测性**：Observability 页的 `outcomes` 旁边显示 `remediation` 子块（§8）。新增告警规则（随 R1、R2 进 alert-rules 新版本）：
  - `remediation_waiting`：proposed 超过 24 小时，warning；
  - `remediation_failed`：24 小时内大于 0，warning；
  - `remediation_rule_held`：大于 0，critical；
  - `remediation_rate_capped`：大于 0，warning。
- 文案中**不使用“已补偿”“已撤销”**这类词，统一叫“补救”。“已完成”只表示补救动作本身做完了。

## 12. 隐私、world 与租户

- 只有 world `real` 的失败生成补救（§3.1）。规划器的查询按 tenant 和 world 过滤；表 FORCE RLS。
- 补救规划器的服务身份**只**持有 `remediation.plan`，没有任何效应能力、渠道凭据或审批权。
- 所有面向客户的后续动作都只能来自新的 Run 或人类受治理调用，派发时重新检查联系约束。因此补救**不能**成为绕过“别再联系我”的通道。这一点要有专门的测试（§16）。

## 13. 最小可交付切片

| 切片 | 内容 | 依赖 | 估计 | 迁移 | 可独立上线 |
|---|---|---|---|---|---|
| **R0 证据补齐** | 三条失败路径在原事务中写 `failed` 事件和原因码；在 0324 中给新码归类；复制件守卫登记 | 无 | 1–1.5 天 | 1 | 是：失败可追溯，本身就有价值 |
| **R1 只生成建议** | 规则配置与发布校验（只允许 `escalate`）；补救意图与事件表；规划器生成 proposed 或 escalated，以及唯一、速率、熔断、不适用的判定；工作台“补救”一栏只读加“标记已处理”；告警规则 | R0 | 2–3 天 | 1 | 是：负责人能看到每一次真失败，以及它是否已经有人处理 |
| **R2 自动唤醒计划** | `reevaluate` 的 automatic 模式；`plan_feed_touch` 新增 `final_failure` 原因；意图新增 `remediation_origin`，深度上限 1 | R1 | 2 天 | 1 | 是：计划不再一直等待一个已经确定失败的意图 |
| **R3 人工补救加审批** | `remediation.execute` 能力及处理函数；`governed_action` 规则；NX-056 审批策略；工作台“发起补救”；首批补救动作由负责人定（§14 Q-2） | R1、**NX-056 A4 合入** | 2–3 天 | 1 | 是 |
| **R4 认领终态与结果分类联动** | 失败时认领终结为 permanent_failure；`outcomes.remediation` 子块；证据抽屉 | R1 | 1–1.5 天 | 1 | 是 |

合计约 8–11 天。**R0 到 R2 不依赖 NX-056**，可以先做。

## 14. 需要负责人拍板

| 编号 | 问题 | 选项 | 推荐 |
|---|---|---|---|
| Q-1 | 首批覆盖哪些失败 | A. 效应意图的路径②和③（`nexloop.enterprise.deliver`、`nexloop.incentive.issue`、非绑定回复的 `nexloop.service.request`）<br>B. A 加上外联和激励台账的 failed | **A**。外联失败通常不值得追；激励失败的退款已经内建 |
| Q-2 | R3 的首批 `governed_action` 补救动作 | 例如“把相关承诺标为受阻”（`commitment.mark_at_risk`，需要新建 human 能力），或者暂不设置 | **R3 暂缓到有具体需求时**。R1 和 R2 的升级与重评估已经覆盖主要场景，没有现成可用的补救动作时，不硬造 |
| Q-3 | `reevaluate` 能否默认自动执行 | A. 两种失败种类都自动<br>B. 只有 origin_expired 自动，confirmed_not_delivered 先要人确认 | **A**：唤醒本身没有外部效应，新 Run 的每个动作都要重新经过全部检查，而且确认未送达本来就是负责人做的决定 |
| Q-4 | 补救成功时，原动作认领是否标为 compensated | A. 不标，保持 permanent_failure，只在补救意图上记 succeeded<br>B. 标为 compensated | **A**：NexLoop 的补救是“跟进”，不是“撤销”，标为 compensated 容易误导 |
| Q-5 | 上限的默认值 | 每租户每小时 20 条；规则 24 小时内 3 次失败熔断；暂时性错误重试 3 次；建议有效期 7 天 | 按此执行，都写在配置中 |

## 15. ADR-040 草案

> **ADR-040｜最终失败后的补救意图**
>
> 状态：草案（L7，2026-10-23），待 L6 复核与负责人决定。
>
> 背景：负责人在 Q2 中决定引入补救：先生成补救意图，再按规则决定直接执行还是先审批。NexLoop 的效应只有三条路径会终结为 failed，预留类副作用在失败时已经原子退回；上游的“同一适配器逆操作”不适合 NexLoop 的 provider 协议。
>
> 决定：
> 1. 只有 0324 中类别为 defect 的效应失败（来源 Run 过期且从未派发；负责人带证据确认未送达）生成补救意图；unknown 先对账，按规则的拒绝和配置缺失不生成。
> 2. 补救有三种：`reevaluate`（唤醒原计划，由新 Run 重新评估）、`escalate`（交给负责人）、`governed_action`（一个人类受治理能力，经 NX-056 审批，由请求人本人提交）。不存在逆操作，也不重放原意图。
> 3. 所有执行都只走现有的受治理路径；规划器只持有 `remediation.plan`，没有效应能力；不新增直接 SQL，也不替换身份。
> 4. 每次失败、每条规则最多一条补救；补救产生的动作再失败时只升级（深度上限 1）；暂时性错误最多重试 3 次；有租户速率上限和规则熔断。数值都在受信配置中。
> 5. 原意图的 failed 与失败率不因补救而改变；补救的结果单独统计。
> 6. 补救规则是受信配置，精确到 Action 版本；没有规则的版本不生成补救。
>
> 后果：计划不再一直等待已经失败的意图；负责人能看到每一次真失败及其处理情况；面向客户的后续动作仍然受联系约束、real 开关和预算约束。代价是新增两张表、一份配置和一个规划器 tick；`governed_action` 依赖 NX-056。
>
> 被否决的方案：
> - 上游的同适配器自动逆操作：provider 协议不可靠，逆操作同样是一次没有幂等保证的外部写；
> - `compensation_mode` 写在 Action 定义上：发布 Action 新版本时会连带改变补救语义；
> - 补救成功后把原失败从失败率中扣除：会掩盖真实的失败；
> - 允许补救的补救：会形成循环。

## 16. 测试清单（随各切片实现）

- **R0**：三条路径各写一次 `failed` 事件，码正确；回滚的事务不留事件；新码都已在 0324 中归类（守卫测试）。
- **R1**：
  - 路径③生成恰好一条 proposed，重复检测返回同一条；
  - unknown、路径①、NXE01、NXB02、模拟世界都不生成补救；
  - 没有规则的 Action 版本不生成补救；
  - 速率上限、规则熔断、不适用（consumer 删除中）生效；
  - 应用角色不能直接写补救表；
  - 工作台的读取遵守成员读取规则，删除中的 consumer 隐藏。
- **R2**：
  - 唤醒计划后，重评估 Run 被领取；
  - **重评估 Run 想联系一个已经“别再联系我”的客户时，派发被 NXC05 拒绝**，补救记为 `refused`；
  - real 派发开关关闭时，补救不产生外部效应；
  - 补救产生的意图失败后只升级，不生成新的补救。
- **R3**：
  - 未批准不执行；自批被拒（单人模式除外）；
  - 批准后由请求人执行，处理函数以请求人身份运行；
  - 冻结参数改动后摘要不匹配；
  - 效应类 Action 作为目标时发布被拒；
  - mode 为 automatic 的 `governed_action` 发布被拒。
- **R4**：
  - 认领在失败时终结为 permanent_failure，补救成功后仍是 permanent_failure（按 Q-4 的推荐）；
  - `outcomes.remediation` 的计数与 `unremediated_defects` 正确。

## 17. 风险

- **R2 会提高重评估 Run 的频率**：每一次真失败都会唤醒一次。受 NX-024 的自触发上限与 Run 预算约束；R2 上线后要观察重评估队列与模型成本，即 0324 的 `outcomes` 与成本块。
- **证据缺口**：R0 之前，路径②没有任何事件，规划器无从判断。所以 R0 是硬前置，不能跳过。
- **NX-056 的进度**：R3 依赖 A4。如果 NX-056 的消费语义（例如“处理函数抛异常仍算已消费”）在 A4 中改变，R3 要跟着调整。本设计只依赖“审批通过后由请求人本人提交”这一条。
- **新增 human 能力**：`remediation.execute`、`remediation.rule_release`，以及取消、标记已处理。能力名与 NX-056 的首批能力不冲突。工作台成员读取需要把补救表纳入 ADR-025、ADR-035 的派生规则，R1 实现时要列出对应的读取函数。
