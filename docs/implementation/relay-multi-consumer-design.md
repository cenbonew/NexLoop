# message relay 多客户：设计稿

状态：**设计稿，未实现**。分支 `relay-multi-design`，从 main `7f5823f` 切出。这是 NX-031 的前置产品修复：L4 在装载 1000 个消费者时发现，relay 一个进程只能服务一个客户。设计审核通过后在同一分支实现，临时迁移号用 0160–0169。契约改动列在 §7，先经调度员批准再改。

**负责人确认**：2026-10-11，负责人确认 relay 多客户修复按方案 A 执行，原话“同意，按你的方案修”（经调度员转达）。§7 的契约改动和 §9 的 D 项仍由调度员逐项批准。

## 1. 现状（代码核对，main 7f5823f）

### 1.1 recipe 的来源与每一项的用途

relay 进程 `message_relay_cli` 启动时读 `--recipe-file`（私有 JSON，`validate_recipe` 严格校验 13 个键），每轮重读一次，**内容不能变**（变了就判失败，“轮换不能就地改动稳定的业务分派”）。容器入口里文件名是 `relay-recipe.json`（`container_entrypoint.py:122`）。各键的来源与用途：

| 键 | 来源 | 用途（`message_relay.py`） | 性质 |
|---|---|---|---|
| `consumer_id` | business setup 输出（`business_setup.setup_business` 的 `consumer_id`） | `claim(consumer_id=…)` 只认领这个客户的消息；Goal、PlanStep、MessageAssignment 的 consumer；`consumer_ref` | **每个客户不同** |
| `control_id` | business setup 输出（EffectControl） | PlanStep 和 MessageAssignment 的 control；关系与 v6 上下文的 formal | 每个客户不同 |
| `control_revision` | business setup 的 `expected_control_revision`（=1） | MessageAssignment；`bind_effect_context`；`goal_version_ref` | 每个客户不同，**会过期** |
| `consumer_revision` | business setup 的 `expected_consumer_revision`（=1） | MessageAssignment；`bind_effect_context` | 每个客户不同，会过期 |
| `valid_until` | 由部署人员手填，应与 EffectControl 的 `valid_until` 一致 | Goal 与 MessageAssignment 的有效期；过期后返回 `requires_governed_replan` | 每个客户不同 |
| `offering_id` / `offering_binding_id` | catalog setup 输出（`catalog_setup.setup_catalog`：每个客户各建一个 ServiceOffering 和一个 ConsumerServiceOffering） | 上下文中的供给与目录信封（`prepare_message_context` / v6 / 关系上下文） | 每个客户不同 |
| `role_ref`、`runtime_profile`、`budget` | 部署配置 | RunCommand | **部署级**，与客户无关 |
| `queue` | 部署配置 | 运行时队列（`accept_runtime_event`、`bind_message`） | 部署级 |
| `runtime_owner_epoch` | 部署配置（Host owner epoch） | RunCommand | 部署级 |
| `context_manifest_ref` | 部署配置 | 只在 `validate_recipe` 里做语法校验；实际命令用 `artifact:context-bind-pending`，绑定后替换 | 部署级（实际未使用） |

`FallbackReplyLauncher`（reply-guarantor 的兜底回复，`reply_fallback.py`）读**同一份** recipe：如果 `state['consumer_id']` 与 recipe 不同，直接报错 `recipe is for another consumer`。所以兜底回复同样只能服务一个客户。

对照：计划复评的 Role Run（`plan_reevaluation.py`）已经是多客户的。offering、binding、control 和各项 revision 都从 PG 中的计划和当前对象读取，没有 recipe 文件。本设计让 relay 走同样的路线。

### 1.2 SQL 侧（0049，0126 包装）

- 入口 `authz.nexloop_message_run_issuance_command`，有 `claim`、`issue`、`own_route`、`release`、`lookup` 几个动词。调用方必须是 `nexloop_api`，并用 route 凭据签名 `nexloop.conversation.route:1`。
- **`claim` 必须带 `consumer_id`**。它选该客户 `status='pending'`、且这条消息本身没有有效 lease 的消息，`order by conversation_id, sequence limit 1`。核对项包括：会话归属、ConsumerOwnership、Consumer 和 Conversation 的形式对象。
- **授权范围**：claim 对租户内**任何**已登记归属的客户都会成功。现在 relay 只服务 recipe 中的那一个客户，是 recipe 文件限定的，SQL 并没有限定。
- **顺序**：同一会话按 sequence 处理，但只检查“这条消息”有没有 lease，不检查同一会话里更早的消息是否还在处理。单 relay 单客户时不会出问题；多个 relay 同时工作时，同一会话的第 2 条可能在第 1 条之前被处理。
- **`issue`**：
  - 逐项复核 MessageAssignment、Goal、PlanStep 和 EffectControl 的当前 revision、`allow_effect`、执行者和有效期；
  - 0126 包装后，如果会话或客户处于接管中，抛出 NXC06 并回滚。
  - 但 claim 并不跳过接管中的客户。多客户下会出现 relay 反复认领同一条被接管的消息、每次都回滚，从而**堵住队头**。

### 1.3 进程形态

- 一个 relay 进程，持有 4 个凭据：route、source、planner、executor；
- 一个 vault 目录，用 `relay.owner` 文件锁保证单 owner；
- 一个后端池（L4 实测最小可用池为 2）；
- 每轮串行处理一条消息。

## 2. 目标

1. **一个 relay 进程服务多个客户**：按消息所属客户，在 SQL 里取该客户的配置（control、offering、binding），不再依赖每个客户一份 recipe 文件。
2. **每个客户的配置在受治理的建立过程中生成，登记到 PG**：由受治理的 Action 写入，relay 自己不能写。
3. **revision 不再写进文件**：认领时由 SQL 返回当前 revision，签发时 SQL 照常复核。这样就不存在“recipe 中的 revision 过期”的问题。
4. **部署级的 Run 配置**（role_ref、runtime_profile、budget、queue、owner epoch）保留为部署配置文件，与客户无关。
5. **公平**：一个客户积压，不能饿死其他客户。**顺序**：同一客户内按顺序处理。
6. **授权**：relay 只能认领登记给它的客户；每次签发都在 SQL 里复核。撤销登记、删除中（NX-029 erasing）或接管中的客户立即停止。
7. 兜底回复使用同一套登记。

**不做**：
- 并行处理同一客户的多条消息；
- 跨租户的 relay；
- 修改 Run 签发的核对项（0049 的核对全部保留）。

## 3. 客户登记（relay enrollment）

### 3.1 登记表 `control.nexloop_relay_enrollments`

- 只追加；FORCE RLS（按 `eios.tenant_id`）；应用角色没有任何表权限；只能通过下面的 definer 函数读写。

| 列 | 说明 |
|---|---|
| tenant_id, world | world 只允许 `real`（与 0049 一致） |
| consumer_id | 客户 |
| enrollment_revision | 同一客户单调递增；最新一行就是当前状态 |
| state | `active` 或 `withdrawn` |
| relay_principal | 被授权服务这个客户的 relay 服务主体（route 凭据的主体，默认 `message_relay`） |
| control_id | EffectControl；必须属于这个客户，`allow_effect=true`，未过期 |
| offering_id, binding_id | ServiceOffering 和 ConsumerServiceOffering；binding 必须属于这个客户、引用这个 offering 且为 active |
| enrolled_by, intent_id, recorded_at | 登记人（服务主体或人类）、受治理请求 id、时间 |

- 唯一键：`(tenant_id, world, consumer_id, enrollment_revision)` 和 `(tenant_id, world, intent_id)`，后者用于重放。

### 3.2 谁来登记

- **受治理的服务 Action `nexloop.relay.enroll:1` / `nexloop.relay.withdraw:1`（D1）**：
  - 执行者是**这个 EffectControl 的 `owner_principal`**，也就是 business setup 中创建 Consumer 和 EffectControl 的那个 owner 服务主体。
  - handler 在 SQL 中核对：调用者就是 control 的 owner；control、offering 和 binding 三者属于同一客户、同一租户、world 为 real；control 未过期且 `allow_effect`；binding 为 active。全部满足才写入一行。
  - 重放（同一 intent）得到同一结果。
- **建立流程**：business setup（Consumer、EffectControl、归属）→ catalog setup（offering、binding）→ **relay enroll**。
  - 新增一个 `relay_enrollment.py` 入口（或在 catalog setup 之后串联），只接受上面两个 setup 的输出 id。
  - NX-031 的装载（L4）对每个合成客户调用一次，代替“每个客户一份 recipe 文件”。
- **人类撤销**：owner 可以在工作台撤销某个客户的 relay 登记（`withdraw`）。v0.1 先只给服务 Action，人类入口作为后续项（D1 的一部分）。
- **relay 自己永远不能登记或撤销**：relay 主体没有这两个 Action，handler 也会拒绝 `relay_principal` 等于调用者的情况。

### 3.3 登记的有效性随源对象变化

登记只是“授权并指明用哪组对象”。以下情况都不需要人去改登记，因为认领和签发时 SQL 读的都是当前值：
- EffectControl 被暂停（`allow_effect=false`）或过期：claim 跳过这个客户，issue 照常拒绝；
- binding 被停用，或 offering 不再 active：同样跳过；
- 对象 revision 前进：claim 返回当前 revision，issue 按当前 revision 复核。

## 4. 认领、公平与顺序

### 4.1 新动词 `claim_next`（不带 consumer_id）

在包装后的 `nexloop_message_run_issuance_command` 中新增 `claim_next`。原来的 `claim(consumer_id)` 保留给单客户兼容模式（D4）。`claim_next` 的流程：

1. **候选客户**：同时满足以下条件：
   - 有当前 `active` 登记，且 `relay_principal` 等于调用者；
   - 不在删除中（`runtime.nexloop_consumer_erasing`，NX-029）；
   - 没有处于接管中的整客户接管（`control.nexloop_takeover_active`）；
   - control、binding 当前有效；
   - **当前没有被任何 relay 持有 lease 的消息**（同一客户串行，D2）；
   - 至少有一条 pending 消息。
2. **每个候选客户的“队头消息”**：该客户 pending 消息中最早的一条，按 `accepted_at`（outbox `record->>'accepted_at'`，0046）、`conversation_id`、`sequence` 排序；并且它所在的会话没有处于会话级接管中。
   - 如果队头所在会话被接管，跳过这个客户。这条消息由接管流程处理（0126 的 D5 结算），不会被重放。
3. **公平（D3）**：在候选客户中，按 `last_served_at`（该客户上一次被认领的时间，没有就算最早）升序，再按队头消息的 `accepted_at` 升序，选第一个。
   - 效果是按客户轮转：一个积压 100 条的客户，每轮只占一个名额。
   - 表 `runtime.nexloop_relay_consumer_cursors(tenant, world, consumer_id, last_served_at)` 在 claim 的同一事务里更新。
4. **并发**：
   - 候选客户的 cursor 行用 `for update skip locked` 锁定；
   - 消息上沿用 0049 的 per-message advisory lock 和 lease fence。
   - 多个 relay 进程同时 claim 时，各自拿到不同的客户，不会互相等待。
5. **返回**：比 `claim` 多一个 `profile`：`{consumer_id, control_id, control_revision, consumer_revision, offering_id, binding_id, valid_until(取 control 的有效期), enrollment_revision}`，revision 都是当前值。其余字段（message_id、fence、route_fence 等）与 `claim` 相同。

**查询代价**：以 `runtime.nexloop_message_outbox` 中 `status='pending'` 的行为驱动，按客户分组。需要新增部分索引 `(tenant_id, world, status) where status='pending'`。1000 个客户、几十条 pending 时是毫秒级；NX-031 的 C4 / C4-peak 会实测。

### 4.2 签发时的复核（`issue` 包装）

在现有核对之外，新增以下复核。任何一项不满足都抛异常并回滚，签发不会发生：
- 这条消息的客户**当前**有 active 登记，`relay_principal` 是调用者，并且登记中的 control 等于 MessageAssignment 的 `control_id`；
- 不在删除中；
- 不在接管中（0126 已有）。

offering 和 binding 由上下文准备阶段使用，签发时核对 `profile.enrollment_revision` 仍是最新一行，因此登记一变，旧的认领就失效。

### 4.3 relay 侧（`message_relay.py`）

- `MessageRelay` 改为接收“部署级 Run 配置”（`run_profile`：role_ref、runtime_profile、budget、queue、owner epoch），不再接收 recipe。
- `run_once` 调用 `claim_next`，用返回的 `profile` 组装 Goal、PlanStep、MessageAssignment 和上下文，其他流程不变。
- `recipe_digest` 改为对 `{run_profile, profile}` 求摘要（只有语义变化，字段不变）。
- 幂等 intent 现在由 `[message_id, type_name, recipe]` 生成；改为 `[message_id, type_name, run_profile, profile]`。同一条消息重试时 profile 相同，intent 也就相同。
- **兼容模式（D4）**：CLI 仍接受 `--recipe-file`，走原来的 `claim(consumer_id)`，供现有单客户测试和部署过渡使用；新增 `--run-profile-file` 进入多客户模式。两者互斥。

## 5. 与其他机制的衔接

| 机制 | 衔接方式 |
|---|---|
| **ADR-023 必回** | 必回由 0109 在入站事务中登记，reply-guarantor 按 reply-due 推进，与 relay 无关。被拒绝联系（受限）的客户仍然会收到回复：claim 不按联系限制跳过，联系限制在派发时执行（NXC05）。 |
| **ADR-023 兜底回复** | `FallbackReplyLauncher` 不再读 recipe：用 relay 身份调用新动词 `profile`（`{consumer_id}` → 当前登记的 profile，只返回给该客户的 `relay_principal`），再按 profile 组装。客户没有 active 登记时，兜底无法启动，按现有路径升级给 owner，不会静默。reply-guarantor 的 CLI 去掉 `--recipe-file`，改用 `--run-profile-file`。 |
| **NX-028 接管** | claim 跳过接管中的客户或会话（新增），issue 照常拒绝（0126）。接管结束时由 0126 的 D5 结算处理未回复的消息。这样一个被接管的客户不会堵住其他客户。 |
| **NX-029 删除** | erasing 期间 claim 跳过，issue 拒绝；删除完成后，登记作为该客户数据的一部分进入删除清单（只留不含原文的 tombstone）。 |
| **NX-022 暂停与预算** | 不变：派发准入照常检查 NXC01 / NXB01。claim 不读暂停状态，避免和准入重复判断，代价是暂停期间会签发 Run 然后在准入时被拒。**另一个选择**是在 claim 中也跳过暂停的 scope，以节省 Run（D5）。 |
| **NX-030 指标** | 快照新增 `relay` 区块：pending 消息数、有 pending 的客户数、最老 pending 年龄，**以及“有 pending 但没有 active 登记的客户数”**（这些消息没有任何 relay 会处理）。告警规则新增 `relay_unenrolled_pending > 0`（critical）和 `relay_oldest_pending_seconds`（沿用 queue 阈值）。 |

## 6. 连接池与进程数

- **每个 relay 进程的池仍是 2**（D7；L4 实测 relay 最小可用池为 2）。多客户不改变“每轮串行处理一条消息”的连接用法：claim 多了按客户分组的查询，但仍在同一个连接、同一个事务里。所以**不需要更多连接**。
- **吞吐**：每条消息的 relay 处理包含多次受治理调用。单进程是否跟得上名义速率（1 条/秒），由 NX-031 的 C4 和 C4-peak 实测。
  - 跟不上时增加 relay 进程数（每个池 2）。各进程各自持有 vault 和 owner 锁（每个进程一个 vault 目录），claim 用 `skip locked`，彼此不冲突。
  - 进程数写进连接预算，每多一个进程加 2。默认 1 个（D6）。

## 7. 迁移与契约改动清单（需调度员批准）

**迁移**（临时号 0160 起，一个文件）：
1. `control.nexloop_relay_enrollments`、`runtime.nexloop_relay_consumer_cursors`，以及 outbox 的 pending 部分索引；
2. `nexloop_relay_enroll` / `withdraw` 的 handler（definer 函数，`search_path pg_catalog,pg_temp`）；
3. `nexloop_message_run_issuance_command`：改名保留为 `_before_relay_multi_v0160`，新包装加入 `claim_next`、`profile` 两个动词，并在 `issue` 中做登记、删除中、接管的复核。0049 和 0126 不修改；
4. `runtime.nexloop_metrics_snapshot`：`create or replace`，增加 `relay` 区块（与 NX-030 的快照函数重叠，见下）。

**契约和配置改动**（列出待批，未修改）：
| # | 改动 | 影响 |
|---|---|---|
| C1 | relay 签名协议 `nexloop-message-run-issuance-v1` 的 payload 新增两个动词：`claim_next`（无参数，只有 `lease_seconds`）和 `profile`（`consumer_id`）；返回值新增 `profile` 对象 | 内部协议，没有公开 JSON Schema；单客户的 `claim` 不变 |
| C2 | business-actions 新增 `nexloop.relay.enroll:1`、`nexloop.relay.withdraw:1`（object_type 为 Consumer v1，service authority） | 清单新版本 |
| C3 | service-grants：owner 服务主体（business setup 的 owner 凭据）获得 enroll / withdraw；`message_relay` 不变 | 清单新版本 |
| C4 | `message_relay_cli`：新增 `--run-profile-file`（部署级 Run 配置，5 个键），与 `--recipe-file` 互斥；reply-guarantor 同样改 | 部署配置；容器入口 `relay-recipe.json` 改为 `relay-run-profile.json` |
| C5 | 新的建立入口 `nexloop-relay-enroll`（读 business setup 和 catalog setup 的输出 id） | 部署步骤增加一步 |
| C6 | NX-030 快照新增 `relay` 区块，告警规则新版本加 2 条 | 工作台运行状态页可以显示（前端另做） |
| C7 | MessageAssignment 的 `recipe_digest` 改为对 `{run_profile, profile}` 求摘要 | 字段不变，语义变化 |

## 8. 测试清单

- **多客户正确路由**：一个 relay 进程、3 个已登记客户，交替入站。每条消息的 Run 中，`consumer_ref`、control、offering 和 binding 都是该消息所属客户的；MessageAssignment 的 consumer 与会话的 consumer 一致。
- **无串号的身份核对**：沿用 NX-031 §4.4 的第 1–4 项：Run 绑定、外发归属、Claim 归属、模型请求上下文中的对象 ID 只属于该客户。
- **公平性**：客户 A 积压 20 条，B、C 各 1 条。B 和 C 在 A 的前 2 条之内被认领；连续认领序列按客户轮转。
- **同一客户内的顺序**：同一客户两个会话、各 3 条，处理顺序等于 `accepted_at` 顺序；同一客户任何时刻最多 1 条处于 lease 中。两个 relay 进程并发时，同一客户不会同时被两边认领。
- **撤权即时生效**：
  - withdraw 之后，下一次 claim 不再返回这个客户；
  - 已经认领、尚未签发的消息在 issue 时被拒绝（登记 revision 已变）；
  - erasing、整客户接管和会话接管分别验证：只跳过对应客户或会话，其他客户照常处理；
  - relay 主体自己调用 enroll 会被拒绝；
  - 登记给另一个 relay 主体的客户，本 relay 认领不到；
  - control 暂停时跳过。
- **登记 handler**：非 control owner 被拒；control、offering、binding 不属于同一客户被拒；重放得到同一结果；表只追加；应用角色读不到表。
- **兜底**：多客户下兜底回复用登记的 profile；没有登记时升级给 owner。
- **原有单客户测试回归**：`test_message_relay`、`test_message_relay_configuration`、`test_outbound_messages_pg`、`test_reply_fallback_pg`、`test_takeover_e2e_pg` 等不改断言（兼容模式）；`test_runtime_dispatch` 和 NX-049 时限门槛串行回归。
- **真实 Pi 端到端**：1 个 relay 进程、2 个客户，各自经原生 WebChat 入站，Host 和 Pi 跑真实 Run 并完成回复。两个客户各得到自己的回复，外发归属正确。串行运行。
- **指标**：快照的 `relay` 区块；一个未登记但有 pending 的客户会触发 `relay_unenrolled_pending`。

## 9. 待决定的事项（D 项，附推荐）

- **D1 登记由谁写**：
  - 推荐：受治理的服务 Action `nexloop.relay.enroll/withdraw`，执行者必须是该 EffectControl 的 owner 服务主体，在建立流程中自动完成；人类撤销入口作为后续项。
  - 替代：由可信配置（configurator）按文件批量登记。缺点是文件里要写 1000 组 id，而且绕开了“control owner 才能授权”这条对象级核对。
- **D2 同一客户内的并行度**：
  - 推荐：**同一客户串行**，任何时刻最多 1 条消息在处理，按 `accepted_at` 排序。这样同一客户的 Run 不会在联系限制、承诺和上下文上互相竞争，也符合 NX-051 的会话顺序。
  - 替代：按会话串行、同一客户的多个会话并行（吞吐更高，但同一客户的并发 Run 会读到彼此未完成的状态）。
- **D3 公平策略**：
  - 推荐：按客户轮转，`last_served_at` 最早者优先，相同时取最老消息。
  - 替代：全局 FIFO，按 `accepted_at`。实现最简单，但一个客户积压时会让其他客户排在它整批消息之后。
- **D4 单客户 recipe 模式**：
  - 推荐：保留一个版本作为兼容（两种 CLI 参数互斥），等部署和 NX-031 切换完成后另开任务删除。
  - 替代：直接删除（现有单客户测试要一并改写，改动面大）。
- **D5 claim 是否跳过暂停的 scope**：
  - 推荐：**不跳过**，由派发准入统一拒绝（单一判断点，与 NX-022 一致）。
  - 替代：claim 也跳过，节省暂停期间注定被拒绝的 Run。
- **D6 relay 进程数**：
  - 推荐：默认 1 个、池 2；按 NX-031 的 C4 实测吞吐决定是否增加，每加一个进程预算加 2。

## 10. 实现说明（调度员 2026-10-11 批准 D1–D6、C1–C7 后实现）

临时迁移号 `0160_relay_multi_consumer`，并入了 main `ec272dd`。

### 10.1 迁移 0160

- **登记表与辅助表**：
  - `control.nexloop_relay_enrollments`：只追加，FORCE RLS，应用角色没有任何权限；可选的 `role_id` / `link_id` / `step_id` 三者必须同时出现或同时缺省（L4 约束 3）；
  - `runtime.nexloop_relay_consumer_cursors`：公平轮转用的游标；
  - `runtime.nexloop_relay_claims`：记录经 `claim_next` 认领的消息，签发时据此复核登记；
  - 新增 outbox pending 的部分索引。
- **`runtime.nexloop_relay_profile`**：当前登记为 active、EffectControl（对象和账本）、binding、offering 都有效时，返回 profile（revision 取当前值）；否则返回 null。
- **`authz.nexloop_relay_enrollment_command`**：签名服务端口，协议 `nexloop-relay-enrollment-v1`，动词 `enroll` / `withdraw` / `read`。认证沿用 `nexloop_plan_port_tenant`。调用者必须是该 EffectControl 账本中的 owner，并且不能把自己登记为 relay。
- **`authz.nexloop_relay_multi_command`**：实现 `claim_next` / `profile`。认证与 0049 相同。`claim_next` 逐个检查候选客户：
  - 同时只有一个 relay 在为某个客户做选择（客户级 advisory lock）；
  - 该客户没有处理中的消息；
  - 不在删除中，没有处于接管中；
  - profile 有效；
  - 会话归属与形式对象核对、消息的规范形式核对（与 0049 相同）。

  全部满足后才写入 lease、route fence、游标和认领记录。
- **`authz.nexloop_message_run_issuance_command`**：改名保留为 `_before_relay_multi_v0160`，0049 和 0126 都没有修改。新包装做两件事：
  - `claim_next` / `profile` 转给上面的新函数；
  - `issue` 在原有核对之后，再核对“该客户不在删除中”（所有模式都核对）；经 `claim_next` 认领的消息，还要核对登记 revision、relay 主体和 control 都没变。任何一项不符就回滚。
- **`runtime.nexloop_metrics_snapshot`**：`create or replace`。函数体**取自 0131**，这是该函数唯一的定义，0133 没有替换它；生成时原样复制 0131 的函数体，只在 `return` 之前追加一行 `relay` 区块。`relay` 区块由 `runtime.nexloop_relay_metrics` 计算。

### 10.2 代码

- **`message_relay.py`**：
  - `MessageRelay` 的 `recipe` 和 `run_profile` 二选一（D4），两种模式互斥；
  - 多客户模式调用 `claim_next`，用返回的 profile 组装 recipe（`recipe_from_profile`）；后续 Goal、PlanStep、MessageAssignment、上下文和签发的流程不变；
  - 单客户模式的 intent 计算不变。
- **`message_relay_cli`**：`--recipe-file` 与 `--run-profile-file` 互斥，必须给其一。
- **`reply_fallback.FallbackReplyLauncher`**：同样二选一。run profile 模式下，用 relay 的 route 身份调用 `profile` 读取登记；客户未登记时抛出 `FallbackUnavailable`，由 guarantor 按 `fallback_failed` 升级给 owner。
- **reply-guarantor**：参数与 CLI 一致，两者互斥。
- **容器入口**：`relay-run-profile.json` 存在时传入 `--run-profile-file`；它和 `relay-recipe.json` 同时存在时，参数冲突，进程不启动。
- **新模块 `relay_enrollment.py`**：
  - 进程内函数 `enroll`、`withdraw`、`read`，供 NX-031 构建模板时调用（L4 约束 2）；
  - 命令行入口 `nexloop-relay-enroll`；
  - `read` 按 consumer_id 只读返回 control、offering、binding、role、link、step 和状态（L4 约束 3）。
- **`deploy/configuration/alert-rules.v2.json`**：按“新版本是新文件”的约定新建，v1 保持不变。在 v1 的 21 条基础上增加：
  - `relay_unenrolled_pending`：critical，持续 300 s；
  - `relay_backlog_age`：warning，超过 600 s。

### 10.3 与批准清单的差异（需调度员确认）

- **C2（business-actions）没有新增条目**：`nexloop.relay.enroll` / `withdraw` 走的是签名服务端口，与 `nexloop.plan.reevaluate`、`nexloop.reply.fallback` 一样，只需要授权事实，不需要发布 Action 定义。
- **C3（service-grants）改为在 `excluded_by_policy` 中登记，不新增授权**：business owner 不在这份清单的主体之列，它的权限与 `EffectControl.create` 一样，属于 owner 自己的控制配置；SQL 还要求调用者就是 control 的账本 owner。`message_relay` 和清单中的任何常驻服务都得不到这两个 Action。

### 10.4 L4 接口约束

1. **每个格子 1 个 relay、池 2**：多客户模式下，relay 只需要部署级 run profile 和 relay 主体（service-grants 中的 `message_relay`）的凭据，不再需要每个客户的 recipe。连接池用法不变。
2. **登记**：可以在进程内调用，同一 intent 重放、同一 profile 换 intent 都是幂等的。实测每个客户 5.6 ms（30 个客户，本机负载约 6），推算 1000 个客户约 6 s（`test_enrollment_throughput_in_process`）。
3. **只读拿 id**：`relay_enrollment.read(owner, consumer_id=…)` 返回各项 id。role、link、step 在登记时可选；不填时为 null。
4. **`tool_timing`**：由 Dispatcher 在每个 Run 结束时写入，relay 签发的 Run 也一样。计数的端到端验证见 `test_run_tool_timing_pg`（生产 runtime worker 加真实 Pi）；多客户的 Pi 测试使用的 guard 测试对象不记录计数，只核对字段存在。

### 10.5 未完成项

- ~~Source 的逐对象 READ 随客户数增长~~：调度员 2026-10-11 裁定采用方案 A，已实现，见 §11。
- **登记记录未接入 NX-029 的删除传播**：登记行只含 id，删除中的客户已经被 claim 和 issue 拒绝；把登记行加入删除清单与 tombstone，作为后续项。
- **兜底签发的 SQL（0110）没有复核登记**：撤销登记之后，launcher 在 Python 侧读取 profile 时就会失败；但从 `profile` 调用到兜底签发之间存在毫秒级的窗口，SQL 侧在这段窗口内不会复核登记。

## 11. Source 读派生（ADR-020 §1 的扩展；迁移 0161）

### 11.1 裁定与原因

- **问题**：relay 准备消息上下文时，Source 要读取每个客户的 Consumer、ServiceOffering 和 ConsumerServiceOffering。如果用静态的逐对象授权，每个客户约 20 条 target，1000 个客户就是约 2 万条，都写进同一份授权事实。每次请求都要对这份事实做快照摘要，而且每写一条授权都会推进 authority epoch，使所有会话失效。所以这条路走不通。
- **调度员 2026-10-11 裁定采用方案 A**，必须随本次 relay 多客户实现一起交付：仿照 0077 和 0099 的 `message_read_rules`，每次读取时在 SQL 中派生，不写任何逐对象授权。方案 B（静态授权）只允许在单元测试中过渡使用，现已全部改为派生。

### 11.2 规则与发布

- **事实类型 `relay_source_read_rule`**：key 为 `[Source 主体]`，payload 恰好包含 `tenant_id`、`principal_id`、`relay_principal`、`active`、`valid_until` 五个字段。
  - 只能经可信配置写入。`control.nexloop_configure_manifest` 的写法是原样复制 0102 的函数体，只追加这一类事实的形状校验：
    - key 与 `principal_id` 必须一致；`tenant_id` 必须是本租户；
    - `relay_principal` 必须是一个已登记的服务主体，并且不等于 Source 本身。
- **service-grants v16 新增 `relay_source_read_rules` 段**：每条写明 `relay`（清单中的角色名，必须持有 `nexloop.conversation.route:1`）和 `source_principal_id`。
  - `source_principal_id` 必须**不是**本清单管理的主体。relay 的 Source 持有 `nexloop.service.request`，这个 Action 在 `excluded_by_policy` 里属于 owner 专属，所以 Source 不由本清单管理。
  - `service_grants --apply` 把这一段编译成上面的事实，与 `message_read_rules` 走同一条可信配置路径，有审计、可重放。
  - 从清单中删掉某条规则时，下一次 apply 会把对应事实置为 `active=false`，不删除。
- **v16 的取值**：`message_relay` → `nexloop-service-relay-source-principal`。部署时，relay 的 Source 凭据必须使用这个主体 ID（**待调度员确认**；它的其他授权仍按原方式由 owner 配置）。

### 11.3 派生条件（每次读取时在 SQL 中重算，不缓存）

Source 主体 P 读取资源 R，**全部**满足以下条件才派生 READ：

1. **调用者**：P 是 real world 中、没有 Run 上下文的服务凭据（`subject_kind='service'`），身份快照有效，凭据未撤销、未过期。
   - Run 凭据、浏览器会话、Agent 一律走已配置路径，什么都派生不到。
2. **只派生 READ**，而且 R 只能是以下之一：
   - `eios:object:Consumer/<c>`；
   - `eios:object:ServiceOffering/<o>`，以及它的 12 个属性（`OFFERING_FIELDS`，写死在规则中）；
   - `eios:object:ConsumerServiceOffering/<b>`，以及它的 5 个属性（`BINDING_FIELDS`，写死在规则中）。

   Consumer 的属性不派生。
3. **规则**：`relay_source_read_rule[P]` 存在、`active`、未过期。
4. **登记**：存在一个客户 c，它的**当前**登记（0160）为 active，`relay_principal` 等于规则中的 relay，并且 R 就是 c 本身，或者 c 的登记中的 offering 或 binding。
5. **binding 指向这个 Source**：登记中的 binding 对象，`source_principal` 是 P，`consumer_id` 和 `offering_id` 与登记一致。
6. **c 不在删除中**（NX-029）。
7. **属性**：属性在对象当前的 schema 中有声明；如果它所属的属性组在负责人的 `property_group_restriction`（ADR-019 §2）中被标为受限，就不派生。
8. **已配置优先**：只要 P 对 R 自己有已配置的 `grants` 事实，就走已配置路径，派生被拒（标记为 `superseded`）。

### 11.4 机制

- **`authz.nexloop_relay_source_derivation`**：计算派生依据，本身不授予任何权限。
- **`authz.nexloop_property_access_basis`**（0084 版本改名保留后再包一层）：按顺序选择路径：已配置 → 0084 的类型属性派生 → 本派生。
- **`authz.nexloop_assert_relay_source_read`**：在使用时校验派生证明：
  - 证明外形与 0084 相同，带 `derivation='relay-source-v1'`，`facts` 为空；
  - 逐项重新派生，并比对规则 hash、客户、登记 revision、类型和受限组 hash，任何一项变了都拒绝。
- **公开的 `assert_read_authority`，以及内层的 `_before_message_read_v0072`**，都改名保留后再包一层：带本派生标记的证明，先检查是否在删除中，再交给上面的校验函数。
  - 消息读取派生（0077）在内部调用内层入口来校验 Source 嵌套的 Consumer READ，所以内层入口也必须能分派这一派生。
- **Python**：
  - `object_reads.AuthorizedObjectReader._derived` 在 basis 为 `relay-source-v1` 时，构造 READ 证明（`property_access.relay_source_claims`），不需要嵌套类型 READ；
  - `trusted_configuration.FACT_MODELS` 新增 `RelaySourceReadRule`。

### 11.5 失效（都在下一次读取时生效，没有缓存窗口）

撤销登记、客户进入删除流程、规则被置为 inactive 或过期、Source 凭据被撤销、binding 改指其他 Source、对象被删除：任何一项发生后，下一次读取时基础查询就派生不出结果，校验时也因为依据变化而拒绝。这类证明带有派生标记，0096 的读取缓存不会记住它，每次都走完整校验。

### 11.6 测试清单（已实现）

- **`test_relay_multi_consumer_pg.py`**（夹具中 Source **没有任何**逐对象授权，读取全部来自派生）：
  - 多客户路由、轮转、串行、撤权、签发前撤权回滚、真实 Pi 双客户端到端：在只有派生授权的条件下全部通过；
  - `test_source_read_is_derived_only_for_enrolled_consumers_and_ends_at_the_next_read`：
    - 未登记的客户读不到；不在固定清单中的属性读不到；
    - 撤销登记后下一次读取失败，重新登记后恢复；
    - 进入删除流程后失败；
    - 没有规则的另一个 Source 读不到；规则置为 inactive 后失败；
  - `test_source_credential_revocation_run_credentials_and_browser_sessions_get_nothing`：
    - EDIT 从不派生；
    - 该 Source 签发的 Run 凭据走已配置路径，读不到；
    - 顾客的浏览器会话走已配置路径；
    - Source 凭据撤销后下一次读取失败；
  - `test_an_agent_with_a_rule_and_a_binding_naming_it_still_derives_nothing`：给 Agent 配上规则和指向它的 binding，基础派生函数直接调用时会返回 derived，但 Agent 用自己的凭据时只走已配置路径，读不到。
- **`test_relay_source_read_rules_pg.py`**：
  - v16 经 `service_grants.apply` 写入；重放时不写入；doctor 显示一致；删除规则后置为 inactive；
  - Python 校验器拒绝 6 种错误清单；
  - `configure_manifest` 拒绝 8 种畸形事实，接受精确的形状。
- **受限属性组**：ServiceOffering 和 ConsumerServiceOffering 的 schema 目前没有属性组，所以受限组排除逻辑已实现，但没有能触发它的样例。Consumer 的属性完全不在派生范围内。

## 12. 合入前补充（调度员 2026-10-11）

### 12.1 兜底签发在 SQL 中复核登记（迁移 0162）

- **做法**：`authz.nexloop_reply_fallback_command` 的公开入口（0126 的接管检查包装）改名保留为 `_before_relay_enrollment_v0162`，在外面再包一层。动词为 `issue` 时，在原有核对之后再做两项复核：
  - 消息所属客户不在删除中：所有兜底签发都复核；
  - 该客户只要**曾经登记过**，当前登记就必须为 active，并且 relay 主体就是调用者。从未登记过的客户属于单客户 recipe 模式（D4），行为不变。

  任一项不符就抛异常，整个签发事务回滚。launcher 读取 profile 之后、签发之前撤销登记，签发必然被拒，不存在时间窗口。
- **负例**：`test_fallback_issuance_rechecks_the_enrollment_in_sql_no_window`：
  - A 在 launcher 读取 profile、创建 Goal 之后被撤销登记，签发被拒，不留签发记录；
  - 对照组 B 保持登记，走同一条路径能正常签发。

### 12.2 Source 主体固定与 doctor 核对

- **部署说明**：`deploy/stage/README.md` §6 写明，relay 的 Source 凭据必须使用主体 ID `nexloop-service-relay-source-principal`。
- **指标**：0162 用 `create or replace` 替换 `runtime.nexloop_relay_metrics`，`relay` 区块新增两项：
  - `enrolled_consumers`：已登记的客户数；
  - `source_rules`：每条规则的 Source 主体、relay 主体、是否 active、该主体是否有 active 的服务凭据。只含 ID 和布尔值。
- **doctor**：新增 `nexloop-doctor --relay-source`，只通过只读的 `nexloop_metrics` 角色读取。对每个有已登记客户的 real 租户核对：
  - 存在针对固定主体、处于 active 的规则；
  - 该主体有 active 的服务凭据；
  - 没有其他 Source 主体的 active 规则。

  测试：`test_doctor_relay_source_requires_the_fixed_source_principal_and_its_credential`。

### 12.3 受限属性组

ServiceOffering 和 ConsumerServiceOffering 的 schema 目前没有属性组，所以 0161 的受限组排除逻辑已实现，但没有可触发它的样例（调度员已接受）。Consumer 的属性不在派生范围内。

### 12.4 登记记录接入 NX-029 的删除传播（迁移 0163）

- **新增删除阶段 `relay`**，排在 `references` 之后：
  - 在删除范围内（`erasure_open`）清除该客户的全部登记记录；
  - 同时清除轮转游标和认领记录；
  - 留一条 tombstone：`item_class='relay_enrollment'`，`subject_kind='control.nexloop_relay_enrollments'`，只含计数。subject_kind 不用 `consumer`，以免与删除凭据（Consumer 本身的 tombstone）同键。
- **为什么只需清除**：删除请求一进入删除中状态，relay 就已经停止服务该客户（claim、profile、签发、兜底签发、Source 读派生都会跳过或拒绝）。这一阶段只是把残留的记录清掉。
- **函数体来源**：`runtime.nexloop_erasure_step` 取自 **0138_nx029_erasure_propagation.sql**，它是 main `ec272dd` 和 `dispatch/integration-s4p` 上该函数的最新定义（0139、0140 都没有替换它），原样复制后只加了这一个阶段。
- **测试**：L3 的 `test_erasure_propagation_pg.py::test_consumer_erasure_reaches_plans_formal_objects_and_the_effect_ledger`。删除前补入一条登记、一个游标和一条认领记录；删除后断言三者都已清空，tombstone 只有一条且只含计数。
