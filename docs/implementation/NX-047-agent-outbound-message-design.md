# NX-047 设计稿：Agent / 人类接管外发消息持久化为 Message

依据：ADR-020 §2（负责人 2026-10-09）；ADR-020 §1 与 L1 设计稿 `NX-018-message-read-derivation.md`（分支 `claude/nx018-finish-997fcd`，§9 实现说明、§10 外发适配）。基线 main `3ad4061`（迁移至 0073）。本稿只做设计，不含产品代码；迁移编号均为临时占位。

## 1. 现状（只读核对）

- **入站**：顾客经浏览器 Human 会话，`authz.nexloop_conversation_command`（0046）在一个事务里写 `Message` 对象（受治理 `Message.create`）、`runtime.nexloop_conversation_messages`（会话内 `sequence`）、`nexloop_message_inbox`、`nexloop_message_outbox`（relay 消费）；0055 原生 WebChat 事件外包它；0069 触发器同事务写 Claim 提取 feed。`Message` 对象只有 `conversation_id/sequence/actor/body/accepted_at` 五个属性，`actor`=顾客 owner principal。
- **外发**：Agent 不写 Message。Run 通过 `nexloop.service.request:1` 提交外发意图（0040 `runtime.nexloop_effect_intents`，正文在 `frozen_request.parameters.message`；`state ∈ accepted/dispatching/unknown/fulfilled/failed/confirmed`）；独立 executor 执行（0042 `nexloop_effect_attempts.state ∈ dispatching/unknown/provider_accepted/observed_fulfilled/fulfilled`，`nexloop_effect_observations.provider_state ∈ accepted/fulfilled/not_found`）；未知结果先 QUERY/reconcile（0065）。`local_json_delivery` 是本地真实 JSON 导出 provider；浏览器只能经 `nexloop.conversation.service_receipt` Function（0048）看到“某条顾客消息对应的服务回执”，看不到 Agent 说了什么。
- **链接可推导**：intent → `nexloop_effect_submissions.run_id` → `authz.nexloop_message_run_issuances.message_id`（触发它的顾客 Message）→ 会话。
- **后果**：企业承诺（`commitment` Claim）在存储路径上无法提取；对话审计不完整；M18 履约无锚点。

## 2. 目标语义

1. **只有经 Action 核准的外发内容才成为 Message**。Pi 助手的中间文本、未提交的工具参数、被 governor 拒绝的请求都是草稿，不持久化为 Message，永不显示为“已发送”。人类接管者在界面输入但未提交的内容同理。
2. **已持久化 ≠ 已送达**。Message 对象不可变（正文、发送方、序号）；投递状态在独立的技术状态表中单调推进，来源只能是 effect 账本（attempt/observation/reconcile），不能由 Agent 或前端声明。
3. **发送方身份由服务端推导**：`sender_kind ∈ {agent, human_takeover}`，`sender_principal`（Agent 为 executor/Run 所属服务主体，人类为浏览器会话 principal），`role_ref`、`run_id`（Agent）或 `takeover_session`（人类）。企业一方统一为 speaker=`agent`（提取语义上的“企业侧”）。
4. **同一会话一条序号线**：外发 Message 与顾客消息共用 `nexloop_conversations.last_sequence`，在同一行锁下分配，保证回放顺序。
5. **不触发 relay 自环**：外发 Message 绝不写入 `nexloop_message_outbox`/`inbox`（那是“顾客消息待规划”队列），否则 relay 会为 Agent 自己的话再开 Run。

## 3. 数据模型（迁移形状，临时 `00xx_nx047_outbound_messages.sql`）

```
runtime.nexloop_outbound_messages (
  tenant_id text, world text check(world='real'), conversation_id text, message_id text,
  sequence bigint,                       -- 与 conversation_messages 同一序号
  direction text check(direction='outbound'),
  sender_kind text check(sender_kind in ('agent','human_takeover')),
  sender_principal text, role_ref text, run_id uuid null, takeover_session text null,
  intent_id uuid null unique references runtime.nexloop_effect_intents,  -- Agent 必填
  receipt_id uuid null,
  trigger_message_id text null,          -- 触发它的顾客 Message（issuance 链）
  delivery_state text check(delivery_state in ('persisted','dispatching','provider_accepted','delivered','unknown','failed','withdrawn')),
  delivery_changed_at timestamptz, persisted_at timestamptz,
  primary key(tenant_id,world,message_id),
  unique(tenant_id,world,conversation_id,sequence),
  foreign key(tenant_id,world,conversation_id,sequence) references runtime.nexloop_conversation_messages(tenant_id,world,conversation_id,sequence),
  check((sender_kind='agent')=(intent_id is not null and run_id is not null))
)
runtime.nexloop_outbound_delivery_events (tenant_id, world, message_id, seq, from_state, to_state, source text check(source in ('attempt','observation','reconcile','intent')), ref, recorded_at)  -- 追加审计
```

- FORCE RLS（`eios.tenant_id`），owner `nexloop_owner`，对 api/worker/scheduler/identity 撤销表权限。
- `nexloop_conversation_messages.record` 对外发行增加 `"direction":"outbound","sender_kind":…`（入站行保持现状，读取时缺省视为 inbound）；`Message` 对象类型 v1 不改（五属性），`actor`=`sender_principal`。不新增 Message 对象版本，避免触碰 0058/0060/0062 对 `actor/body` 的既有校验。
- **状态机（单调，只由 SQL 触发器/定义器推进）**：

| 来源事件 | delivery_state |
|---|---|
| intent `accepted`（与 Message 同事务） | `persisted` |
| attempt `dispatching` | `dispatching` |
| attempt/observation `provider_accepted` / provider_state `accepted` | `provider_accepted` |
| attempt `observed_fulfilled`/`fulfilled` 或 provider_state `fulfilled` | `delivered` |
| attempt `unknown`、intent `unknown` | `unknown`（只能经 QUERY/reconcile 离开，不重发） |
| intent `failed` 且无任何 provider 接受证据 | `failed` |
| reconcile 证明 `not_found` 且 intent 终止 | `failed` |
| 人类接管撤回未投递消息（受治理 Action） | `withdrawn`（仅 `persisted` 可达） |

  允许的转移：persisted→{dispatching,failed,withdrawn}；dispatching→{provider_accepted,delivered,unknown,failed}；provider_accepted→{delivered,unknown}；unknown→{provider_accepted,delivered,failed}；delivered/failed/withdrawn 为终态。逆向一律拒绝（触发器 raise）。`delivered` 只表示渠道接受/投递，**不表示**问题解决或承诺兑现（见 §7）。

## 4. 写入路径

### 4.1 Agent 外发
- 位置：包裹 0040 的 intent 受理定义器（`nexloop.service.request` 受理，rename 私有 alias + 新 public wrapper 的现有链式方式）。仅当 action 为会话回复类（`parameters.message` 存在，且 Run 由 `nexloop_message_run_issuances` 绑定到某条顾客 Message）时，在**同一事务**内：
  1. 锁 `nexloop_conversations` 行（与入站相同锁序），`sequence:=last_sequence+1`。
  2. 经 `authz.nexloop_create_object_action` 创建 `Message` 对象，使用新的受治理 Action `Message.agent_create:1`（低风险、approval none；其 permit 由 executor 主体持有，不是 Run 自己）——意图ID派生 `request_id='agent-message-'||intent_id`，重放幂等。
  3. 写 `conversation_messages`（record 含 direction/sender）、`outbound_messages(delivery_state='persisted')`，0069 触发器照常写提取 feed。
  4. **不写** `nexloop_message_outbox/inbox`。
- intent 受理重放（同 intent_id）→ 返回已有 message_id，不分配新序号。
- intent 被拒绝（scope/控制/预算/授权失败）→ 事务回滚，无 Message（草稿不持久化）。
- 投递推进：在 0042 attempt/observation 与 0065 reconcile 的写入点挂触发器（或在其定义器尾部调用私有函数）推进 `delivery_state` 并追加事件。

### 4.2 人类接管
- 前置（不在本任务实现，需负责人决定）：企业员工的 Human 身份与 `conversation.takeover` 权限模型；目前浏览器 Human 只代表顾客 owner。
- 设计：受治理人类 Action `Message.staff_send:1`，服务端从员工会话解析 principal，校验其对该 Consumer 的接管权限；同样经 effect intent（与 Agent 一致的外发账本），因此投递状态机复用。`sender_kind='human_takeover'`，`run_id` 为空。

## 5. 读取与显示

- 顾客视图（0046 `messages`/`events` 动词）：返回入站全部 + 外发中 `delivery_state ∈ {provider_accepted, delivered}` 的消息；`persisted/dispatching/unknown/failed/withdrawn` 不显示为已发送（WebChat 渠道下可显示为“处理中”占位但不含正文，需产品确认）。每条外发项带 `direction`、`sender_kind`、`delivery_state`。
- 员工/审计视图：全部外发消息及状态与事件历史。
- 浏览器端 `chat-api.ts` 增加外发项解析；现有 `ServiceReceipt` 继续表示 effect 回执，不与 Message 合并。

## 6. 纳入 L1 的 Message READ 派生

L1 已实现（NX-018 §9）：带 `derivation='accepted-message-v1'` 的类型化 READ 证明，由 `nexloop_assert_derived_message_read` 在 SQL 内复核：规则事实 `message_read_rule`、Message 对象存在、**`nexloop_message_outbox` 有受理记录**、`conversation_messages → conversations → Consumer` 一致、嵌套的 Consumer READ 当前有效。

外发适配（与 L1 §10 一致，不新增授权来源）：
- 条件“受理记录”扩为：`nexloop_message_outbox` 有该 message_id **或** `nexloop_outbound_messages` 有该 message_id（且 delivery_state ≠ `withdrawn`）。其余条件不变：同一会话→同一 Consumer、Source 对 Consumer 的当前 READ、规则有效、world=real、tenant 一致。
- 派生范围仍限 object + `actor` + `body`；`withdrawn` 的外发消息不派生（等价于删除）。
- 以新迁移 `create or replace` L1 的派生断言函数（或其内部“已受理”私有函数）实现；不改 L1 已发布迁移。
- 负向：他租户/他 world、未受理的 intent（事务回滚）、withdrawn、他 Consumer、Run 凭据请求派生——全部拒绝。

## 7. 对提取与 M18 的影响

- **提取（NX-019）**：0069 feed 触发器会收到外发 Message；`claim_store` 的 speaker 推导（actor≠owner → agent）与 0066 定义器（speaker 由服务端按 actor 推导）无需改语义，但应改为读 `outbound_messages.direction/sender_kind`，避免依赖 actor 比较。
  - **窗口过滤**：调度 `due` 与记录定义器只把 `delivery_state ∈ {provider_accepted, delivered}` 的外发消息纳入提取窗口；`persisted/dispatching/unknown` 延后（feed 行保留 pending，到达可提取状态后再入队）；`failed/withdrawn` 永不纳入。理由：没送达的话不是对顾客作出的承诺。
  - 守卫已有：agent 只能产出 `commitment`/`hypothesis`，顾客不能产出 `commitment`；Claim 契约已允许 `speaker=agent`。commitment 的 `valid_time`（如“明天下午前”→ambiguous deadline 窗口）直接成为 M18 的到期依据。
- **AT-040（履行证据）**：`delivery_state=delivered` 只证明消息送达，不得把 need_problem 标为已解决、不得把 commitment 标为已兑现。设计约束：履约状态（M18）只能来自独立证据（受治理 Action receipt、签名商业事件、顾客确认 Claim），不从 delivery_state 或 Agent 自述（“我已处理”）推导；NX-047 测试需断言 delivered 后 commitment Claim 的 `resolution_state` 不变、无履约记录产生。
- **AT-041（承诺到期）**：NX-047 提供带 deadline 的 commitment Claim 和其外发 Message/receipt 锚点；到期扫描与复评/异常（负责人可见）属 M18（S4），不在 NX-047 范围。ambiguous deadline 应以 `latest_bound_window` 的上界作为最晚到期，不能精确到分钟。

## 8. 迁移清单（临时编号，接在合并时的最高号之后）

1. `nx047_outbound_messages`：两张新表 + RLS + 撤权；`Message.agent_create:1` 的受治理定义由可信配置发布（ADR-020 §3 清单，不在迁移里直写）；intent 受理定义器包装（同事务写 Message/conversation_messages/outbound）；delivery 推进私有函数与 attempt/observation/reconcile 写入点挂接；`messages/events` 读取动词的外发投影与可见性过滤（create or replace 新版本）。
2. `nx047_outbound_read_derivation`：L1 派生“已受理”条件扩展（依赖 L1 迁移已合入）。
3. `nx047_outbound_extraction`：0069 feed `due` 窗口与记录定义器的外发投递过滤。
不改 0001..0073 及 L1 已发布迁移；全部 rename 私有 alias + 新 wrapper。

## 9. 测试清单（真实 PG，合成数据，关键不可 skip）

正向：
1. Run 提交 service.request（带 message）→ 同事务出现 Message 对象 + conversation_messages（序号紧接顾客消息）+ outbound(`persisted`)；不出现 message_outbox/inbox 行；relay `claim` 不会拿到它。
2. intent 重放同 intent_id → 同一 message_id、无新序号。
3. 投递推进：dispatching→provider_accepted→delivered，事件表完整；顾客视图在 provider_accepted 前看不到正文。
4. 并发：顾客入站与 Agent 外发同时写同一会话 → 序号唯一、无空洞、无死锁（固定锁序）。
5. 提取：delivered 的外发消息进入窗口，产出 `speaker=agent` 的 commitment，deadline ambiguous；persisted/unknown 时不进窗口，送达后进窗口。
6. L1 派生：Source 对 delivered 外发消息 READ 派生成功（object/actor/body）。

负向 / 恢复：
7. governor 拒绝（越 scope、控制撤销、预算不足、无授权）→ 无 Message、无 outbound、无 feed。
8. Pi 中间文本 / 未提交工具参数 → 无 Message（以真实 Pi 运行断言 sqlite 有 assistant 记录而 PG 无外发 Message）。
9. 状态逆转（delivered→dispatching、failed→delivered）被触发器拒绝；前端/Agent 无路径写 delivery_state（受限角色无表权限）。
10. unknown：不重发；只有 QUERY/reconcile 结果能推进；`not_found`+终止 → failed。
11. COMMIT 后 SIGKILL（intent 受理后、attempt 前）→ 恢复后同一 Message、状态 persisted→继续推进，无重复 Message。
12. 他租户/他 world/他 Consumer/withdrawn/Run 凭据 的派生 READ 均拒绝。
13. delivered 后断言：need_problem/commitment Claim 的 `resolution_state` 不变，无履约/已解决记录（AT-040 前置）。
14. 人类接管（若负责人批准身份模型）：非接管权限的 Human 发送被拒；顾客 owner 不能冒充员工。

## 10. 待负责人/调度员决定

- 人类接管的员工身份与 `conversation.takeover` 权限模型（ADR-020 §3 明确人类权限需负责人确认）。
- 顾客视图对“处理中”外发消息是否显示占位。
- `Message.agent_create:1` 的 Action 定义与 executor 授权进入 NX-048 可信配置清单。
- 是否允许 `withdrawn`（撤回未投递消息）进入 v0.1。
- 依赖：L1 READ 派生迁移合入 main 后再实现 §6/§8.2。

## 11. 实现说明（2026-10-09，与上文差异以本节为准）

分支 `nx047-outbound`（基线 `dispatch/integration-s3f` 90daa7b）。调度员推荐的产品口径：首版只做 Agent 外发（人类接管→NX-028）；顾客视图不显示“处理中”占位；v0.1 不支持撤回（`withdrawn` 状态未实现）。

1. **两阶段而不是“受理即建 Message 对象”**。外发意图受理时，同事务只写外发记录 `runtime.nexloop_outbound_messages`（`persisted`，含 intent/receipt/Run/触发消息/发送方/正文摘要），不分配会话序号、不建 Message 对象。渠道接受（`provider_accepted`/`delivered`）后，由外发记录服务以受治理 `Message.agent_create:1` 建 Message 对象，并以当时的下一个序号追加进会话流。原因：
   - 受治理对象创建需要服务端签名的 Action 许可，受理事务内的调用者是 Run 凭据（allowed_resources 受限），不应让 Run 自己持有 Message 创建权；
   - 若受理即占序号，顾客按序号游标增量读取时，迟到才可见的外发消息会落在游标之后而永远读不到。按“可见时分配序号”保证流顺序 = 顾客看到的顺序；
   - “已持久化≠已送达”由外发记录 + 投递状态承担；失败/未知的回复保留为外发记录可审计，但从不成为可见 Message。
2. **投递状态**只由账本触发器推进：`nexloop_effect_intents.state`、`nexloop_effect_attempts.state`、`nexloop_effect_observations.provider_state`（对账也写这些表）。逆向或非法转移在 `nexloop_outbound_advance` 中忽略、在表守卫触发器中拒绝；外发记录不可删除；受限角色无表与函数权限。
3. **防自环**：物化只写 `conversation_messages`（record 增加 `direction/sender_kind/intent_id/trigger_message_id`），不写 `nexloop_message_inbox/outbox`。
4. **READ 派生**（0079）：包装 0077 的 `nexloop_assert_derived_message_read`；非外发消息原样走 0077；外发消息要求已物化、渠道已接受、actor=外发记录发送方、序号与会话一致，其余规则（rule、Consumer READ、tenant/world、directory_hash、期限）同 0077。未改已发布迁移。
5. **提取**：设计稿 §8.3 的“提取过滤迁移”不再需要——只有渠道已接受的外发消息才进入会话流，0069 feed 触发器与 0066 的 speaker 推导（actor≠owner→agent）原样生效。
6. **授权清单**：`deploy/authorization/service-grants.v1.json` 新增 `outbound_message_recorder`（nexloop_api）与 `eios:action:Message.agent_create:1` execute。Action 定义本身随业务配置发布（测试中由可信配置 manifest 发布，复用 `Message.create` 的 Message 类型引用）。
7. **未实现**：人类接管（§4.2，NX-028）、撤回、顾客视图“处理中”占位、Web 前端对 `direction` 的展示（后端已返回字段，前端解析会忽略未知字段，显示为普通消息）、外发记录服务的 CLI/常驻进程入口。

## 12. 补完（调度员 2026-10-09 第二轮）

- **业务配置清单**：`deploy/configuration/business-actions.v1.json`（`nexloop-business-actions/1`）声明 `Message.agent_create:1`；`nexloop_eios.business_actions.compile_actions` 只接受低风险、无审批、无策略、`ontology.object.create` 的声明，按部署已发布的 Message 类型 schema 与显式 Capability 快照编译为可信配置 `actions` 行。测试从该清单与 `service-grants.v1.json`（`compile_principal`）发布，不再在测试里构造。
- **入口**：`nexloop-outbound-recorder`（`python -m nexloop_eios.outbound_messages`），风格同 effect worker：私有文件配置、要求 nexloop_api 角色、每轮重读凭据并重新认证、`--once` 输出 `{"recorded","replayed"}`、SIGTERM 退出。社区 compose 新增 `outbound-recorder`（profile `outbound`，默认不启动，只读私有卷 `outbound_config`）；容器入口新增 `outbound-recorder` 作业；`nexloop-doctor` 新增 `outbound_recorder_schema`（仅系统目录检查）。
- **前端**：`chat-api.message` 校验服务端 `direction/sender_kind/intent_id/trigger_message_id`（入站不得携带发送方字段，外发必须是 agent + 意图 UUID + 触发消息）；`messagePresentation` 把外发显示为“企业 Agent 回复 · 渠道已接受；不代表问题已解决或承诺已兑现”，不挂顾客回执组件。
- **§9 真实覆盖补齐**：#7 目录 scope 治理拒绝（Pi 两次提交均被拒，无意图/外发/Message；同一 scope 经 `assess_request_scope` 判 `outside_catalog_terms`，无折扣时 `within_catalog`）；#8 Pi 日志中的助手自由文本不出现在任何 Message/会话流；#10 真实超时（provider timeout 1s，导出已 fsync）→ 账本 dispatching，provider 停机时对账 QUERY 失败 → 账本 unknown（外发 `dispatching→unknown`），provider 恢复后 QUERY 观测 fulfilled → `delivered`，全程 1 个 attempt、1 个导出文件；#11 PG 行锁把记录服务 CLI 卡在事务中后 SIGKILL，无 Message，重启记录 1 条、再跑 0 条；#12 他租户主体走不到派生（basis=configured）且其配置 READ 也读不到该 Message。
- 账本意图 `observed_fulfilled` 映射为 `delivered`（0078 临时迁移内修订）。说明：本项目账本没有独立的 “reconciling” 状态；unknown 只能由对账 QUERY 的观测离开。
