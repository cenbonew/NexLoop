# NX-026 承诺与价值履行：设计稿（调度员已审，D3/D6 负责人已定；未实现）

分支 `nx026-design`，从 main `9016021` 切出。本稿只有文档：没有代码，没有迁移；文中迁移号都是占位。NX-026 依赖 NX-025（L2 收尾中），实现等审核后再定。

依据：
- PRD M18；`03_DOMAIN_DATA_MODEL.md` §4 的 Commitment 字段与状态；`04_CONVERSATION_ONTOLOGY_PIPELINE.md` §4、§8；`09_API_AND_EVENT_CONTRACTS.md` §3、§5、§6；`10_UX_AND_WORKBENCH.md`（承诺/交付视图）。
- ADR-020 §2（commitment Claim 只来自企业外发消息）、ADR-023（明确拒绝联系的硬性停发）。
- NX-022、NX-024、NX-047、NX-023 的实现说明；L2 分支 `nx025-impl` 中的 0108–0110（只读核对，尚未合入 main）。

交付物（planning）：条件 / 期限 / 责任 / 证据，发送与完成分开。
验收：
- AT-040：消息发送成功但问题未解决，不得标为客户问题已解决或承诺已兑现。
- AT-041：承诺到期未完成，触发复评或异常，并且负责人可见。
- 保持 AT-020（承诺层面不重复）、AT-010（目录外承诺）已有的结论。

## 1. 现状（已核对代码）

| 已有 | 位置 | 对本任务的意义 |
|---|---|---|
| `commitment` Claim | 0066，`conversation_extraction.py` | 只能 `speaker=agent` 产出，顾客说出的承诺会改判为 intent。字段齐全：`modality`/`condition`、`valid_time`（kind、status、start/end、`latest_bound_window`、timezone、anchor）、`source`（message_id、span、content_hash、quote）、`correlation_key` |
| 企业外发持久化 | NX-047（0077–0079） | 只有渠道已接受的外发才物化为 Message 并进入会话流，因此 commitment Claim 的来源一定是**已送达**的企业消息。外发记录 `runtime.nexloop_outbound_messages` 带 `intent_id`、`run_id`、`trigger_message_id`、`delivery_state` |
| 匹配器对 commitment | `claim_matching.py` | 直接判为 `needs_resolution`，不调模型、不写正式对象。**目前没有任何路径把承诺变成可跟踪的对象** |
| Commitment / Problem 对象类型 | — | **不存在**。部署清单只发布了 `ContextStrategy`、`ContextManifest`（system-object-types）和 `Consumer.edit`、`Message.agent_create` 等 Action（business-actions v4） |
| 到期触发接口 T7 | 0106 `runtime.nexloop_plan_commitment_due(tenant,world,plan,commitment_ref,due)` | 只有接口：把计划登记进 `plan-reevaluate` feed，cause=external。没有调用方，也没有授予任何角色 |
| 计划 | 0106 | 一份 Plan 对应一个 (consumer, goal_version)；可以按 consumer 找到 active 计划 |
| 控制面 | NX-022（0066/0068/0097） | 暂停、目标版本、控制 revision；`bind_run` 记录 Run 绑定的目标版本，可作为 `related_goal_ref` 的来源 |
| 联系限制 | `nx025-impl` 0109（未合入） | `control.nexloop_contact_assert_intent` 对受限 consumer 的**所有** effect intent 生效，只放行绑定入站来信、处于时间窗内、每条来信至多一条的回复；其余返回 NXC05 |
| v6 open_work | NX-023 设计，`nx025-impl` 0108 | 设计里“未履行承诺”来自正式 Commitment 对象。0108 已加入 plan 子段，做法是 SQL 重新导出内容并钉住 |
| 目录范围判定 | NX-047 #7 `assess_request_scope` | 派发前已拦截目录外的折扣/承诺请求（AT-010），所以已送达的外发里通常不会再有目录外承诺 |

结论：NX-026 要补的是从 commitment Claim 开始的整条后续链路：
1. Claim 到正式承诺对象的登记；
2. 承诺的状态机与证据；
3. 到期扫描；
4. 违约与异常；
5. 复评触发；
6. 只读投影。

## 2. 概念与边界

- **Commitment（正式对象，EIOS 本体）**：记录“企业已经对这位顾客作出了这项承诺”这个事实。正式写入只走受治理 Action，工作台和分析只读取投影（03 §1）。
  - 依据：commitment Claim 来源的外发已经送达，承诺已经说出口。登记不是批准承诺，而是记录这个事实。
- **四件事分开记录（M18“输入与产出”）**：
  1. **请求创建**：为履行承诺提交了某个 effect intent；
  2. **已交付**：effect 账本 fulfilled/confirmed，或绑定外发已送达；
  3. **客户确认**：顾客陈述的 Claim；
  4. **问题解决**：Problem 的解决证据。

  它们是证据账本里不同 `kind` 的行，不会合并成一个状态。承诺状态 `fulfilled` 只根据**合格证据**推进（§4.3）。
- **不在本任务内**：
  - Problem 对象类型及 `Commitment–ADDRESSES→Problem` 关系。v0.1 先只把相关的 need_problem Claim 作为证据引用，见 D5。
  - 负责人工作台 UI（NX-028）。
  - 商业事件核验（NX-027）。
  - 人类接管员工身份（NX-028）。

## 3. 从 commitment Claim 到 Commitment 对象（登记）

### 3.1 触发与幂等
- **同事务入队**：`ontology.nexloop_claims` 插入 `epistemic_kind='commitment'` 的行时，用 AFTER INSERT 触发器在同一事务里登记 work feed `commitment-register`，键为 `claim:<claim_id>`，做法同 0093 和 0109。
- **去重键**：`dedupe_key = sha256(tenant | world | source.message_id | correlation_key)`。
  - 同一条消息、同一谓词，在重跑提取或换 extractor 版本后仍落到同一个承诺（AT-020）。
  - 同一条消息里的不同谓词是不同的承诺。
- **重申**：另一条外发消息里出现同一个 `correlation_key`，而且该 consumer 已有 open/conditional/in_progress 的同键承诺时，**不新建对象**，只在事件账本追加 `reaffirmed`，引用新的 Claim。
  - 新的期限与原期限不同时，按 §4.4 的“改期”处理：新话已经送达顾客，所以这是事实。如果旧承诺在新话之前已经违约，违约事件保留不变。
- **对象 id 与重放**：`object_id = sha256('commitment:' || dedupe_key)`，受治理创建的 `request_id = 'commitment-' || dedupe_key`。worker 在“受治理创建成功、账本未写”时崩溃，重放会得到同一对象，不会重复。

### 3.2 字段推导（全部服务端确定性推导，不调模型）

| Commitment 属性（03 §4） | 来源 |
|---|---|
| `made_to` | Claim 的 `consumer_id`（NX-019 由服务端从已验证会话推导） |
| `made_by` | 外发记录的 `sender_kind`、`sender_principal`、`role_ref`、`run_id`；Message 的 `actor` |
| `content_ref` | `claim:<id>`、`message:<id>#<span_start>-<span_end>`，以及 `content_hash`。原文只留在 Message 和 Claim 中，Commitment 不复制正文 |
| `promised_at` | 外发 Message 的 `accepted_at`，即渠道接受、物化的时刻 |
| `due_at` | 来自 `valid_time`：status=resolved 时取 `end`（point 取 `start`）；ambiguous 时取 `latest_bound_window[1]`，即最晚界（NX-047 §7），不精确到分钟；`absent`/`unresolved`/`unparsed` 时为空，`due_state='unspecified'`；`past_reference` 时为空，并记一条异常 `due_in_past` |
| `due_precision` | `exact`、`latest_bound` 或 `unspecified`。工作台必须显示这是推导出的最晚界 |
| `condition` | `modality='conditional'` 时取 Claim 的 `condition` 原文，初始状态为 `conditional`；否则为空，初始状态为 `open` |
| `status` | §4 |
| `fulfillment_evidence` | 证据账本中合格证据的引用列表（§4.3），由状态转移同时写入 |
| `related_goal_ref` | 外发 Run 经 `bind_run` 绑定的 NX-022 目标版本（`goal:<id>@<v>`），没有则为空 |
| `fulfillment_basis` | 新增字段，取值 `undetermined`（登记时的默认值）或 `communication`。只能由人类负责人或运营经受治理 Action `nexloop.commitment.mark_communication` 逐条改为 `communication`，留证（D3 裁定） |

- `modality='tentative'` 的 commitment（如“我尽量……”）**不登记**为承诺。它保持 `needs_resolution`，在事件账本里留 `skipped_tentative`。
- 登记前做两项检查。检查结果只用来标记和升级，不阻止登记，因为话已经送达，不登记反而会让承诺从跟踪里消失：
  1. **目录检查**：沿用 `assess_request_scope`。结果为 `outside_catalog_terms` 时记异常 `outside_catalog`，负责人可见。PRD 要求“不允许无能力的承诺”，这一要求的预防点在派发前（AT-010 已通过）；这里是兜底发现。
  2. **受限期间作出的承诺**：consumer 正处于 ADR-023 联系限制时，记异常 `made_under_contact_restriction`。依据是 ADR-023 §7：兜底回复不得约定后续联系。
- 登记完成后，Claim 的 `resolution_state` 置为 `resolved`，并在匹配日志写入 `commitment:<object_id>`；之后 `ClaimMatcher` 不再处理这条 Claim。
- **缺少 Commitment 类型**：租户没有发布 Commitment 类型时，Claim 保持 `needs_resolution`，feed 行以 `type_unavailable` 退避，并记异常 `schema_gap`。不丢弃 Claim，也不建临时对象（04 §8）。

### 3.3 更正与删除
- **更正**：correction Claim 把 commitment Claim 标为 superseded 时，**不自动取消**承诺，因为话已经送达。系统只记异常 `source_superseded`，由负责人决定取消或改期。
- **删除**：来源 Message 被删除（数据权利请求）时，NX-026 只保证两点：承诺不再暴露原文（`content_ref` 只剩引用，读取端口与 v6 不再返回引文）；状态、证据与事件审计保留。同时记异常 `source_deleted`。保留期、删除传播与导出的完整语义归 NX-029（D7 裁定）。

## 4. 状态机与证据

### 4.1 状态（03 §4 的六个值，不另造）

```
          condition_met
conditional ───────────► open ──start──► in_progress
    │                      │                 │
    │ cancel(human)        │ fulfill(evid.)  │ fulfill(evid.)
    ▼                      ▼                 ▼
 cancelled ◄──cancel── open/in_progress   fulfilled
                           │
                           │ due elapsed, not fulfilled
                           ▼
                        breached ──fulfill(evid., late)──► fulfilled(late=true)
```

| 转移 | 谁 | 前提（SQL 强制） |
|---|---|---|
| 登记 → `open` / `conditional` | 登记服务主体 `commitment_registrar`（受治理 `Commitment.create:1`） | §3 |
| `conditional → open` | 人类负责人或运营（`nexloop.commitment.condition_met`），或可核验的证据（D4） | 证据账本中有一行 `condition_met` |
| `open → in_progress` | 监控服务 `commitment_monitor` | 提交了一个引用该承诺的 effect intent（证据 `requested`） |
| `→ fulfilled` | `commitment_monitor` | 证据账本中至少有一行**合格**证据（§4.3），且该证据的发生时刻 ≥ `promised_at`；到期之后发生的记 `late=true` |
| `open/in_progress → breached` | `commitment_monitor` 到期处理（§5） | `due_at ≤ now`，且没有合格证据 |
| `conditional` 到期 | 不转为违约 | 记异常 `condition_unresolved_at_due`，同时触发复评（§5） |
| `→ cancelled` | 只能由人类负责人（`nexloop.commitment.cancel`，需填写原因），或由改期自动产生（§4.4） | Agent、Run、服务主体都不能取消，否则可以借取消逃避违约 |
| `fulfilled`、`cancelled` | 终态 | `breached` 只能转为 `fulfilled(late)`，违约事件永久保留 |

- **强制方式**：在 `ontology.objects` 上为 `type_name='Commitment'` 加 BEFORE UPDATE 守卫触发器，按上表校验 status 转移，并到证据账本核对前提条件。
  - 通用的 `Commitment.edit` 即使被授权，也无法绕过这个触发器把状态改成 fulfilled。
  - 内容字段（made_to、made_by、content_ref、promised_at）创建后不可改。
  - due_at 只能经 §4.4 的改期变更。

### 4.2 证据账本（runtime，append-only）

`runtime.nexloop_commitment_evidence`：
- 字段：(tenant, world, commitment_id, seq, kind, ref, verification, occurred_at, recorded_by, detail)。
- `kind` 的取值：

| kind | 写入者 | ref | 是否合格（可推进 fulfilled） |
|---|---|---|---|
| `requested` | intent 提交触发器 | `intent:<id>` | 否 |
| `effect_fulfilled` | effect 账本触发器 | `intent:<id>`，attempt 或 observation | **是**：intent 引用了本承诺、属于同一 consumer，账本状态为 `fulfilled`/`confirmed` |
| `delivered_message` | 外发投递触发器 | `message:<id>` | 默认否，只在 `fulfillment_basis='communication'` 时合格（D3） |
| `consumer_confirmation` | Claim 登记 | `claim:<id>` | 否。顾客陈述只作展示，即“客户确认”这一栏 |
| `problem_resolution` | Problem 证据（D5：Problem 不进 NX-026，另开任务） | — | 否，单独一栏；v0.1 显示为“不可用” |
| `commercial_event` | NX-027 的核验通道 | `commercial:<id>` | 是，限已核验的签名事件 |
| `operator_attestation` | 人类 Action `nexloop.commitment.attest` | 人类主体与理由 | 是 |
| `condition_met` | 人类 Action 或已核验证据 | — | 只用于 conditional → open |
| `agent_report` | Agent 工具 | Run 与文本摘要 | **永不合格**（04 §7 第 4 条：Agent 说“我已处理”不是证据） |

- **Agent 怎样参与履约**：Run 在提交 effect intent 时带上 `commitment_ref`（新的可选参数，由服务端校验该承诺属于该 Run 的 consumer、且状态 open/in_progress）。
  - 履约证据由 effect 账本自动产生，Agent 不能直接写合格证据。
  - Agent 可以经 Host 工具 `nexloop.commitment.report` 写一条 `agent_report`，只供展示。
- **AT-040 的结构保证**：外发 `delivered` 只会产生 `delivered_message` 证据。在默认的 `fulfillment_basis='undetermined'` 下它不合格，顾客口头说“好了”也不合格。

### 4.3 读取口径
工作台和 v6 显示四栏：请求创建 / 已交付 / 客户确认 / 问题解决。每栏列出各自的证据和时间，与 `status` 分开显示（UX 10 中“消息发送即已履行”是明确禁止的展示方式）。

### 4.4 改期与“保留旧承诺”
03 §4 要求“修改期限保留旧承诺”。改期不改写原对象：
- 新建一个 Commitment，记 `supersedes=<old>`；
- 旧对象转为 `cancelled`，原因 `superseded:<new>`；
- 旧对象的证据和事件全部保留。

改期有两个来源：
1. 人类负责人或运营的受治理延期（`nexloop.commitment.extend`，UX 10 中的“受治理延期”）；
2. 送达的外发消息重申了同一承诺，但期限不同（§3.1）。

如果旧承诺已经 `breached`，它保持 breached，不会因改期变成 cancelled。

## 5. 到期、提醒、违约与复评（AT-041，NX-024 T7）

- **feed**：新增 `commitment-due`，每个承诺一行，键为 `commitment:<id>`，有两个阶段：
  1. **临近**：时刻为 `due_at − lead_seconds`（配置项）。调用 T7 `runtime.nexloop_plan_commitment_due`，为该 consumer 的每个 active 计划登记复评，让 Agent 在到期前有机会履约。
  2. **到期**：时刻为 `due_at`。在承诺行锁下复核：
     - 已经 fulfilled 或 cancelled：完成 feed 行；
     - 没有合格证据：受治理转为 `breached`，写 `due_elapsed` 事件和异常 `breached`，再调用 T7（cause=external）。
- **T7 的调用方式**：由 `commitment_monitor` 经签名端口 `authz.nexloop_commitment_command`（verb `due`），在定义器内部调用 T7。T7 本身仍然**不授予任何应用角色**。计划按 consumer 查找；没有 active 计划时只记异常 `no_active_plan`。
- **conditional 到期**：不转为违约，记异常 `condition_unresolved_at_due`，并触发复评。
- **没有期限的承诺**：登记时就触发一次复评（业务规则：澄清期限，04 §8）。超过 `unspecified_max_open_seconds`（配置项，默认 7 天）仍未履约时，记异常 `no_due_date`。
- **异常的可见性**：`runtime.nexloop_commitment_exceptions` 为 append-only，每个（承诺, reason）至多一行。查询端口 `nexloop.commitment.read` 返回未完成承诺、证据四栏、异常和事件史，权限做法同 0109 的 `nexloop.contact.read`。NX-028 之前就以这个端口作为负责人可见的依据；运维指标“承诺逾期”（17 §监控）也从这里统计。
- **与暂停的关系**：consumer 或 tenant 暂停时，到期处理**照常**转为违约并记异常，因为暂停不能把违约藏起来。复评由 NX-024 的 precheck 判定为 paused，不启动 Run。

## 6. 与相关任务的关系

### 6.1 NX-024（计划复评）
- T7 只经承诺定义器调用，复用 feed 合并与自触发限流。复评结果（no_action、action_intent 等）不直接改承诺状态；状态只看证据。
- v6 的 open_work 新增 `commitment` 子段：
  - 内容是该 consumer 的 open/conditional/in_progress/breached 承诺及证据四栏摘要；
  - 由 SQL 重新导出并逐项比对，钉住、不裁剪，做法同 0108 的 plan 条目；
  - `evidence_kind=formal`，因为它是正式对象。
- 只读工具 `read_open_commitments`（09 §5）就是这个投影。

### 6.2 NX-022（控制面）
- `related_goal_ref` 取自 `run_goal_bindings`。
- 带 `commitment_ref` 的 intent 照常经过 0097 派发检查：暂停、目标过期、revision 过期都会拒绝；被拒后由 NX-024 生成复评，不会重放旧的履约意图。
- 预算：涉及优惠或金额的履约 intent 照常调用 `reserve_budget`（incentive），超出返回 NXB01。PRD 要求“不允许无预算的承诺”，预防点在派发前；登记时只用目录检查兜底发现（§3.2）。
- 新增控制事件类型：**不新增**。承诺事件不推进控制 revision，否则每次登记都会让无关的计划快照过期。

### 6.3 ADR-023（联系限制下的履约外发）
- **按 ADR-023 §3 调整联系限制的范围（D6 裁定，main `8bcd4c1`）**：受限期间只挡会触达该客户的 effect；不触达客户的服务交付照常派发；附带的客户通知仍受限制。0109 现在对受限客户的全部 effect 都拒绝，NX-026 用新迁移调整，不改 0109：
  - **类别声明**：business-actions 清单的每个 effect Action 新增 `effect_category ∈ {customer_contact, non_contact_service}`。非触达类还要声明 `notification_parameters`，即哪些参数是给客户的通知（例如 `message`）。
    - 声明经可信配置编译进一张旁表 `control.nexloop_action_effect_categories(action_name, action_version, definition_digest, category, notification_parameters)`。
    - 不改 EIOS `ActionDefinition` 模型，避免已发布定义的 digest 变化。旁表行和 intent 冻结的 `action_definition` 按 digest 对应；对不上就视为未声明。
  - **派发判定**：用新迁移包装 0109 的 `control.nexloop_contact_assert_intent`，rename 为私有 alias，再加新 wrapper。受限 consumer 的 intent 满足以下**全部**条件时直接放行：
    1. 旁表把该 Action 声明为 `non_contact_service`，且 digest 一致；
    2. intent 没有 NX-047 外发记录；
    3. `frozen_request.parameters` 中声明的通知参数全部为空或缺省。

    其余情况交给 0109 原逻辑处理，即只放行绑定来信的回复，否则 NXC05。所以未声明类别、触达类、带附带通知的 intent 都会被拒。
  - **附带通知不拆分**：一个 intent 对应一次 provider 请求，派发时无法只发服务、不发通知。带通知的服务交付整条被拒（NXC05，原因 `attached_notification`），Run 可以去掉通知后重新提交。
- 履约外发（触达类）在受限期间仍然被拒：只有绑定入站来信的回复放行，主动的履约外发返回 NXC05，provider 收到零请求。
- **承诺侧的处理**：
  - 履约 intent 被拒（NXC05）时，记异常 `blocked_by_contact_restriction`，负责人可见。承诺**不自动取消**，也不改期。
  - 到期仍按 §5 转为 `breached`，异常带上限制原因，负责人可以据此人工取消或延期。
  - 顾客主动来信时，绑定回复可以带 `commitment_ref`。这条回复送达后产生 `delivered_message` 证据，是否合格仍看 `fulfillment_basis`。回复不解除限制。
  - 负责人解除限制（`contact_released`）会推进控制 revision；NX-024 T3 会让该 consumer 的计划复评，复评可以重新提交履约 intent。被拒的旧 intent 不重放。
- 不触达客户的履约 intent（例如后台退款），在受限期间按上面的判定照常派发，并照常产生 `effect_fulfilled` 合格证据。
- 兜底回复 Run（0110）只开放一个发送工具，**不开放** `commitment_ref` 与 `nexloop.commitment.report`。它作出的承诺按 §3.2 标记为 `made_under_contact_restriction`。

### 6.4 NX-047 / NX-019 / NX-020
- 承诺的来源只有已送达的外发（NX-047 两阶段物化）。
- NX-019 的 Claim 守卫不变。
- NX-020 匹配器继续对 commitment 返回 `needs_resolution`。登记服务把 Claim 置为 `resolved` 后，匹配器不再处理它。`claim_matching.py` 只需要在注释里写明这一点，不改逻辑。

## 7. 写入口与授权

| 入口 | 主体 | 授权 | 说明 |
|---|---|---|---|
| `Commitment.create:1` | `commitment_registrar`（service） | business-actions 清单 + service-grants | 受治理 `ontology.object.create`；对象类型需先发布（D1） |
| `Commitment.edit:1` | `commitment_monitor`（service） | 同上 | 只用于 in_progress、fulfilled、breached 三种状态转移；守卫触发器强制状态机 |
| `nexloop.commitment.cancel` / `extend` / `attest` / `condition_met` / `mark_communication` | 人类负责人或运营 | `human_owner`，走 NX-022 governed 入口 | business_actions 的 `PROFILES` 需要新增这些 human_owner profile；Agent 和服务主体即使有 grant 也会被 SQL 拒绝 |
| `authz.nexloop_commitment_command` | registrar / monitor | 签名 + 当前 EXECUTE（`eios:action:nexloop.commitment.register:1` / `.monitor:1`） | verb：register、evidence、due、exception |
| `authz.nexloop_commitment_read` | 服务主体（NX-028 之前）及负责人 | `nexloop.commitment.read` | 只读 |
| intent 参数 `commitment_ref` | Run | 现有 `nexloop.service.request` 链 | SQL 校验同 consumer、承诺状态为 open/in_progress；不合法时拒绝整个 intent |
| Host 工具 `nexloop.commitment.report` | Run | 配置开关，默认关闭 | 只写 `agent_report`，不合格 |

- 所有新函数的 `search_path` 为 `pg_catalog, pg_temp`，SECURITY DEFINER，owner 为 `nexloop_owner`，并撤销 PUBLIC 和所有应用角色的直接权限。
- 新表启用 FORCE RLS（`eios.tenant_id`），应用角色没有表权限。

## 8. 迁移形状（占位编号，接在合并时的最高号之后）

1. `nx026_commitments`：
   - 表：
     - `runtime.nexloop_commitment_sources`：dedupe_key 唯一，记录 claim、message、object_id；
     - `nexloop_commitment_events`：append-only，记 registered、reaffirmed、skipped_tentative、in_progress、fulfilled、breached、cancelled、superseded、due_soon；
     - `nexloop_commitment_evidence`：append-only；
     - `nexloop_commitment_exceptions`：append-only，每个（承诺, reason）唯一。
   - work feed 的 feed CHECK 增加 `commitment-register`、`commitment-due`。这里与 0109 的同一约束重叠，见 §11。
   - 触发器：
     - claims 插入 commitment 时登记 feed；
     - effect 账本状态变化写 `requested` / `effect_fulfilled` 证据；
     - 外发 `delivery_state` 变为 delivered 时写 `delivered_message` 证据；
     - `ontology.objects` 上的 Commitment 守卫。
   - 端口：`nexloop_commitment_command` 与 `nexloop_commitment_read`；`authz.nexloop_work_feed` 改为 create or replace 版本，加入两个新 feed。
   - intent 受理链接受 `commitment_ref`：采用 rename 私有 alias 加新 wrapper 的方式，不改已发布函数体。
2. `nx026_contact_effect_categories`（D6）：
   - 旁表 `control.nexloop_action_effect_categories`：append-only，FORCE RLS，只能由可信配置写入；
   - 包装 `control.nexloop_contact_assert_intent`。
3. `nx026_commitment_context`：v6 open_work 的 commitment 子段（SQL 重导出与比对）。0108 合入后再写，并依赖它。
4. 对象类型 `Commitment v1` 和 Action 定义**不写在迁移里**，经可信配置清单 `deploy/ontology/business-object-types.v1.json` 与 business-actions 清单发布（ADR-020 §3，D1 裁定）。

已发布迁移不改。所有替换都采用 rename 私有 alias 加新 wrapper，或者 create or replace 新版本。

## 9. 配置

`deploy/configuration/commitments.v1.json`：
- `lead_seconds`：到期前多久触发复评，默认 3600；
- `unspecified_max_open_seconds`：默认 604800；
- 登记 worker 与 monitor 的批量大小；
- 是否开放 `nexloop.commitment.mark_communication`（D3，默认开放）。

Python 用严格加载器校验这份配置。每个承诺在登记时冻结一份 settings。

## 10. 测试清单（真实 PG，合成数据，关键用例不得 skip；E2E 走真实 NX-047 链路和真实 Host/Pi，用确定性 provider）

正向：
1. Agent 回复“我会在明天下午前给你处理进展”并送达，依次经过提取、登记，得到 Commitment：
   - made_to、made_by、promised_at 正确；
   - due_at 为 ambiguous 窗口的最晚界，`due_precision='latest_bound'`；
   - Claim 置为 resolved。
2. 带条件的承诺（“确认后我们就给你补发”）登记为 `conditional`，condition 为原文；人类执行 `condition_met` 后转为 `open`。
3. 履约 intent 带 `commitment_ref`：
   - 提交后产生 `requested` 证据，承诺转为 in_progress；
   - 执行器真实派发并观测到 fulfilled 后产生 `effect_fulfilled` 证据，承诺转为 fulfilled，`fulfillment_evidence` 引用该 intent。
4. 到期前 lead 时刻，该 consumer 的 active 计划进入 `plan-reevaluate`（T7，cause=external）。
5. 人类延期：新对象 supersedes 旧对象，旧对象变为 cancelled(superseded)，证据与事件保留。
6. 查询端口返回证据四栏、异常和事件史。v6 open_work 的 commitment 子段被钉住，内容与 SQL 重导出一致。

AT-040 / AT-041：
7. **AT-040**：外发送达后，Problem/Claim 不变；承诺仍为 open，只多一行 `delivered_message` 证据。顾客说“好了”产生 `consumer_confirmation`，承诺仍为 open。Agent 的 `agent_report` 同样不推进状态。
8. **AT-041**：到期未完成时：
   - 承诺转为 breached，写异常 `breached`；
   - 计划被登记复评；
   - 查询端口可以看到该承诺；
   - 到期后的合格证据把它转为 fulfilled(late)，breached 事件仍在。
9. conditional 到期时不违约，写异常 `condition_unresolved_at_due` 并触发复评；没有期限的承诺超过上限时写 `no_due_date`。

幂等与并发：
10. **AT-020（承诺层）**：同一输入重跑提取、换 extractor 版本重跑、登记 worker 重放，都只有一个对象和一行 sources。
11. 同一承诺在另一条外发中重申，得到 `reaffirmed`；期限不同则走改期。
12. 登记 worker 在受治理创建之后、写账本之前被 SIGKILL：恢复后得到同一对象，账本补齐。
13. 到期违约与履约证据并发：在承诺行锁下串行，两种顺序各断言一次。结果要么 fulfilled（证据先到），要么 breached 之后 fulfilled(late)，不会出现别的组合。

负向：
14. 通用 `Commitment.edit` 直接把状态改为 fulfilled、改内容字段、跳过状态：都被守卫触发器拒绝。
15. Agent、Run、服务主体执行 cancel / extend / attest / condition_met：都被拒绝，即使持有 grant 也拒绝。
16. `commitment_ref` 指向他 consumer、他租户、他 world，或已经 fulfilled/cancelled 的承诺：intent 被拒，不产生证据。
17. Run 凭据、浏览器会话、其他租户的服务主体调用 `nexloop_commitment_command` 或 `_read`：拒绝。
18. 顾客说出的“承诺”（已改判为 intent）、`tentative` 的 commitment、未送达的外发：都不登记。
19. 租户没有发布 Commitment 类型：Claim 保持 `needs_resolution`，记异常 `schema_gap`，不建对象。
20. 来源 Claim 被更正，记 `source_superseded`；来源消息被删除，记 `source_deleted`。两种情况承诺都不自动取消。
21. 目录外承诺（直接构造已送达外发）：登记，同时记异常 `outside_catalog`。

ADR-023：
22. 受限客户的履约 intent 被拒（NXC05），provider 零请求，记异常 `blocked_by_contact_restriction`；到期转为 breached，异常中带限制原因。
23. 受限客户主动来信：绑定回复带 `commitment_ref` 可以派发，并产生 `delivered_message` 证据；限制不解除；不绑定来信的履约外发仍被拒。
24. 兜底回复 Run 不能带 `commitment_ref`；它作出的承诺被标记 `made_under_contact_restriction`。
25. 解除限制后，计划复评；旧的被拒 intent 不重放。
25a. **D6**：受限客户的 `non_contact_service` effect（无通知参数）能派发，provider 恰好 1 次请求，并产生 `effect_fulfilled` 证据。以下情况都被拒（NXC05），provider 零请求：
   - `customer_contact` 类 effect；
   - 未声明类别的 effect；
   - 旁表 digest 与 intent 冻结定义不一致的 effect；
   - 声明通知参数非空的 `non_contact_service` effect（`attached_notification`）。

   同一服务去掉通知后重新提交，可以派发。旁表不能被应用角色写入。非受限客户的派发不受这些判定影响（回归）。

D3：
26a. 默认 `undetermined` 时，带 `commitment_ref` 的已送达外发只产生不合格的 `delivered_message` 证据。人类执行 `mark_communication` 之后，下一条这样的已送达外发即为合格证据，承诺转为 fulfilled；标记之前已经送达的消息是否追认为合格证据，按标记时刻之后的证据判定（不追认，避免事后改口径）。Agent、服务主体执行 `mark_communication` 被拒；标记写入事件账本，留证。

与 NX-022：
26. 暂停 consumer 时：到期照常违约并记异常，复评 precheck 判为 paused，不启动 Run，履约 intent 派发被拒。

## 11. 与现有分支的文件重叠

| 文件 / 对象 | 重叠分支 | 处理 |
|---|---|---|
| `runtime.nexloop_work_feed` 的 feed CHECK；`authz.nexloop_work_feed`（create or replace） | `nx025-impl` 0109 已加入 `reply-due` 并重写端口；0110 可能也涉及 | NX-026 迁移必须基于 0109/0110 合入后的最新函数体，CHECK 取并集。**在 NX-025 合入 main 之后实现** |
| `authz.nexloop_assert_intent_dispatch_controls`；`control.nexloop_contact_assert_intent` | 0109 已重写或新建 | 前者不改；后者 rename 为私有 alias，再加新 wrapper（D6），必须基于 0109 合入后的函数体。NXC05 的结果同时用来写 `blocked_by_contact_restriction` 异常 |
| `authz.nexloop_goal_governed_action`（human Action 入口） | 0109 为 `goals.contact.release` 重写 | 新增的 commitment human Action 要在它的最新版本上扩展，与 0109 冲突的风险最高 |
| `control.nexloop_control_events` 的 event_kind CHECK | 0109 | 不改（§6.2） |
| `runtime.nexloop_outbound_messages` 上的触发器 | 0109 `reply_settle`；NX-047 0078 投递推进 | 新增独立触发器，不改已有的，但要确认触发顺序不影响结清 |
| intent 受理链（0040 包装链） | NX-047 0078、0110 兜底的按 Run 查找复制 | 用 rename + wrapper 接 `commitment_ref`，基于合入后的最新链 |
| v6 open_work：`context_engine/sections.py`、`context_artifacts.py`、0108 导出函数 | `nx025-impl` | 第 2 个迁移依赖 0108，放在 NX-025 合入之后 |
| `background_services.py`、`container_entrypoint.py`、`pyproject.toml` 脚本、compose profile | `nx025-impl`（reply-guarantor、plan-reevaluator） | 新增 `commitment-registrar` / `commitment-monitor` 入口，同类追加，会有文本冲突，但语义上不冲突 |
| `deploy/configuration/business-actions.v1.json`、`deploy/authorization/service-grants.v1.json`、`business_actions.py` PROFILES | 无未合入分支；`nx049-deploy-config` 改 `doctor.py` | 清单 manifest_version 递增；`business_actions.py` 的 validate/compile 接受 `effect_category`、`notification_parameters`（D6），并编译旁表行；doctor 新增 schema 检查时注意与 nx049-deploy-config 的冲突 |
| `claim_matching.py` | 无 | 只改注释 |
| `planning/*` | 调度员 | 本线不改 |

linked 会话 nx019-extract、nx021-recall 的分支相对 main 没有提交，看不到未提交的改动。它们如果改动 Claim 插入路径（0066 定义器）或 `correlation_key` 的计算方式，会影响 §3.1 的去重键，需要调度员确认。

## 12. 裁定（调度员 2026-10-10）与仍待决事项

已裁定：
- **D1 采用**：Commitment v1 作为 M18 核心类型，经可信配置在部署时发布，不走人工审核候选路径。**选择新建 `deploy/ontology/business-object-types.v1.json`，不并入 system-object-types**。理由：
  - system-object-types 的清单决定写明其中类型是 NexLoop 系统元数据（ContextStrategy、ContextManifest），“不授予任何服务主体”；Commitment 是业务对象，需要 `commitment_registrar`、`commitment_monitor` 两个服务主体经 `Commitment.create:1` / `Commitment.edit:1` 写入，与该清单的约束相反；
  - 业务类型（今后的 Problem、Offering 等）与系统元数据分开版本化，变更评审范围不互相牵连；
  - 发布方式与 system-object-types 相同（同一可信配置编译器、同一引用一致性测试），只是另一份清单。
- **D2 采用**：沿用 03 的六个状态值；`late` 是标志，改期取消的原因记 `superseded:<new>`，不新增状态值。
- **D4 采用**：v0.1 的 `condition_met` 只允许人类推进（`nexloop.commitment.condition_met`），已核验证据不自动推进。§4.1 表中“或可核验的证据”一项在 v0.1 不实现。
- **D5 采用**：Problem 类型与 `ADDRESSES` 关系不进 NX-026，以后另开任务；证据栏“问题解决”先显示为不可用。
- **D7 采用**：归 NX-029。NX-026 只保证删除来源消息后承诺不再暴露原文，状态与审计保留（§3.3）。
- **D8 采用**：NX-028 之前，cancel / extend / attest / condition_met 只提供受治理端口和测试。

负责人已定（2026-10-10，经调度员转交，原话“D3 和 D6 都同意你的建议”）：
- **D3**：v0.1 默认 `fulfillment_basis='undetermined'`，需要 effect 回执或人类 attest；开放可选项，由负责人或运营经受治理人类 Action `nexloop.commitment.mark_communication` 逐条标记为 `communication`，标记留证。标记之后，带 `commitment_ref` 且已送达的外发即为合格证据。落点见 §3.2、§4.2、测试 26a。
- **D6**：已写入 ADR-023 §3（main `8bcd4c1`）。联系限制只挡触达该客户的外发 effect；不触达客户的服务交付照常派发，附带的客户通知仍受限制；类别由 Action 定义声明，未声明按“触达客户”处理。NX-026 用新迁移调整 0109 的判定（不改 0109），落点见 §6.3、§8 第 2 项、测试 25a。

实现前提：NX-025 合入 main（调度员通知），临时迁移号届时再定。

## 13. 实现切片建议（NX-025 合入后）

1. **切片一**：
   - `business-object-types.v1.json`（Commitment v1）与 Action 清单（D1）；
   - 登记 feed、worker 与去重；
   - 守卫触发器与证据账本；
   - 查询端口；
   - 测试 1、2、7、10–12、14–21。
2. **切片二**：
   - 到期 feed、违约、T7 与异常；
   - `commitment_ref` 与 effect 证据；
   - ADR-023 与 NX-022 交互，含 D6 的 effect 类别旁表与联系判定包装；
   - D3 的 `mark_communication`；
   - 测试 3–5、8、9、13、22–26，以及 25a、26a。
3. **切片三**：v6 open_work commitment 子段与 `read_open_commitments`，测试 6，并附 E2E（真实 Host/Pi）。
