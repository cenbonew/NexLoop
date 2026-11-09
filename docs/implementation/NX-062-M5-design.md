# NX-062 M5 设计：机器写入（prepare / commit）、0124 机器分支、机器授权事实

- **状态**：草案，送 L6 审。审过之后再切片实现，本文不含迁移。
- **基线**：批次 155（bbe5175b），M4a/M4b 已合入（迁移 0439、0440）。M5 迁移暂用临时号 0441 起。
- **约束来源**：设计稿 v2 §3.1–§3.3、§6 M5、§10 C5–C8、Q-A/Q-C/Q-G 的负责人决定；ADR-041 §2.4、§6 与修订 A1（授权表 `authz.nexloop_machine_client_grants` 是机器授权的唯一权威）。
- **调研事实**（以 bbe5175b 为准）：
  - F-1：0124 统一入口 `authz.nexloop_goal_governed_action(p_digest,p_world,p_text,p_signature,p_payload)` 的现行定义在 0348。它依次检查签名许可、`nexloop_assert_action_authority`、Action 定义与能力注册行、请求摘要、`nexloop_service_identity_snapshot` 的主体类型与 `subject_rule`、`runtime.nexloop_action_claims` 认领行，再过 0327 审批门，调用 handler，写终态结果，提交前重新断言一次授权。
  - F-2：`nexloop_service_identity_snapshot` 和 `nexloop_assert_action_authority` 都按 `p_digest` 分流。0045 为浏览器 realm 加过同样的分流：先查摘要定位表，命中走浏览器快照，否则走原函数。机器摘要由 C1 明确拒绝，不能进这两个函数。
  - F-3：授权事实存在 `authz.nexloop_authority_facts(tenant_id, fact_kind, entity_key, payload)`。浏览器路径用 12 类事实，每类带 `record_hash`，断言时逐条重算比对。
  - F-4：**目前没有任何“对象备注”的 Action，也没有备注表**，人类入口和机器入口都没有。Q-C 的决定（写入只开放对象备注）以为已有人类入口，这个前提不成立，见 §1。
  - F-5：审批决定由受治理 handler `control.nexloop_governed_approval_decide`（0326）执行，注册的 `subject_rule` 是 `human`。solo 判定 `control.nexloop_approval_solo_effective`（0332）只看负责人角色里能审批的主体，机器主体当前进不了这个集合，但这只是间接结果，没有显式排除。

## 1. 首批写能力：对象备注（需要负责人确认 Q-M5-1）

F-4 说明对象备注要新建，不是给已有能力开机器入口。推荐的形状：

- 新受治理 Action `nexloop.object.add_note:1`，operation `object_note_add`，`subject_rule` 为 `agent_or_human`（0124 的 check 约束只允许这两种取值，不改约束），handler `runtime.nexloop_governed_object_note`。
- 新表 `runtime.nexloop_object_notes(tenant_id, world, note_id, type_name, object_id, author_kind human|machine, author_principal_id, client_id null, text, created_at, intent_ref)`，append-only，RLS、强制 RLS、租户外键。
- **备注是陈述，不是事实**：不写进对象属性，不进 ontology，不改变对象的 revision。读取时标明作者类型，机器写的备注在工作台上显示为“机器客户端 <client_id> 的备注”。
- 正文 1–2000 字符，去掉控制字符。不解析链接，不触发任何通知或派发，所以没有外部效应。
- 目标对象：必须是机器授权里给了 `object.add_note` 的类型（首批只有 Commitment、CommercialRecord），并且按 M4 的 `authz.nexloop_machine_object_visible` 可见。不可见、不存在、他人租户的对象一律 404。
- **擦除**：正文可能写进消费者信息。消费者擦除时，按对象关联的消费者把正文改写为固定标记，作者为该消费者的备注同样处理；成员擦除时改写作者为该成员的备注。两张覆盖表都登记这张表。
- **人类入口**：同一个 Action 在工作台对象页加一个“添加备注”按钮和备注列表（只读列表加一个输入框）。不加人类入口的话，备注只有机器能写、没有人能看，没有意义。

需要负责人确认：
- Q-M5-1a：按上面新建备注 Action 和表，人类和机器共用（**推荐**）；或者 M5 只交付写入的基础设施，用测试里登记的能力验证，等备注的业务形态定了再开。
- Q-M5-1b：备注保留期。推荐跟随对象：对象删除时备注一起删除，不单独设保留期。

## 2. 机器授权事实：从授权表在快照时派生（L6 审 D-2）

C7 要求机器分支的 `assert_action_authority` 通过。按 F-2，机器需要自己的快照：`authz.nexloop_machine_identity_snapshot`（M2a 已有）和新的 `authz.nexloop_machine_authority_fact_snapshot`，与浏览器路径同一结构。

事实从哪里来，两种做法：

| 方案 | 做法 | 问题 |
|---|---|---|
| A. 存储事实行 | grant / revoke 时由触发器写 `authz.nexloop_authority_facts`，再加一致性检查（doctor 与测试）比对授权表和事实行 | 两份数据，必然要处理漂移；撤销时必须在同一事务里删除事实行，否则会有窗口 |
| **B. 快照时派生（推荐）** | 不写事实行。机器事实快照按 (kind, key) 现场从授权表、主体表、密钥表、允许表计算 payload 和 `record_hash` | 每次调用要算一遍，但只是几行主键查询 |

推荐 B，理由是 ADR-041 A1 已经规定授权表是唯一权威，B 不产生第二份数据，也就没有漂移。“一致性检查”在 B 下落成两条测试：
- `authz.nexloop_authority_facts` 里不存在任何机器主体的行（触发器拒绝写入，测试断言）；
- 同一授权状态派生出的事实与 `record_hash` 是确定的，授权一变（撤销、类型变化、允许表退役、密钥 scope 变化）`record_hash` 就变，0124 末尾的二次断言因此会拒绝提交。

机器事实的种类（与浏览器同一套 kind，内容按机器改写）：

| kind | 派生自 |
|---|---|
| subject、actor、subject_authority | 机器主体表（状态 active，actor kind 固定为 `machine`） |
| machine_authentication | 密钥行（prefix、world、scope、到期时间、pepper 版本；不含摘要） |
| grants | 授权表中该能力的最新一行（未撤销，带类型列表） |
| scope | 密钥 scope 包含该能力 |
| policies | 允许表当前行（active，kind write） |
| controls、resource_graph、revision | 与浏览器路径相同的租户级事实，直接读现有事实行（不是机器专属） |

## 3. 0124 机器分支（C7，L6 审 D-3）

不能让机器摘要进入 `nexloop_service_identity_snapshot` 或 `nexloop_assert_action_authority`（C1）。也不能用 GUC 或会话临时表给这两个函数打开机器例外：`nexloop_api` 能自己设置自定义 GUC，也能抢先建同名临时表，这样会绕开 C1。

推荐做法：
1. 0441 把 `authz.nexloop_goal_governed_action` 改名为 `nexloop_goal_governed_action_before_machine`，按 0436 的做法动态复制 EXECUTE 授权对象，新建同签名的包装函数：
   - `p_digest` 不是机器形状（`nxm:` 前缀）时，原样委托给改名后的函数，非机器路径行为完全不变，原有回归测试全部保留；
   - 是机器形状时，调用 `authz.nexloop_machine_governed_core(...)`。
2. `nexloop_machine_governed_core` 是 0348 入口主体的副本，只改四处：
   - 身份用机器快照；
   - 授权断言用 `authz.nexloop_machine_assert_action_authority`（§2）；
   - `subject_rule` 判定改为“能力为 `agent_or_human`，且在机器允许表的当前行里，且在密钥 scope 里”，`human` 能力一律 42501；
   - 认领行由本函数在同一事务内创建（见 §4），不需要租约。
3. **副本一致性测试**：把两份函数体做规范化（去掉注释和空白），断言差异只出现在登记的四段里。0348 之后若有人改了原入口而没改副本，这个测试会失败。

变异检查（C7）：分别删掉允许表检查、scope 检查、授权断言、`human` 拒绝，每一项都要有测试变红。

## 4. prepare / commit / 查询（C5、C6）

- **意图表** `runtime.nexloop_machine_intents(tenant_id, client_id, intent_id, capability, world, prepare_digest, expires_at, phase prepare|committed|refused, result_code, action_outcome_id, created_at, committed_at)`，主键 (tenant_id, client_id, intent_id)。不存参数，不存正文，result 只存结果码和 ID（C6、I-7）。进入两张擦除覆盖表（没有可改写的列，登记为“无个人数据”）。
- **prepare**：`authz.nexloop_machine_prepare(prefix, digest, capability, intent_id, params)`。
  - 跑完 §3.1 的 2.1–2.6，参数用能力的参数 Schema 校验，规范化。
  - `prepare_digest = sha256(canonical[tenant, client, world, capability, intent_id, 规范化参数, expires_at])`，有效期 10 分钟。
  - 写意图行，返回 `{prepare_digest, expires_at, normalized}`；`normalized` 只回给客户端，服务端不存。
  - 同一意图再次 prepare：已 committed 返回 409 `intent_committed`；仍在 prepare 阶段且未过期，参数相同返回同一摘要，参数不同 409。
- **commit**：`authz.nexloop_machine_commit(prefix, digest, capability, intent_id, params, prepare_digest)`，一个事务完成：
  1. 重跑 2.1–2.6，不沿用 prepare 的判定（C5）；
  2. 锁意图行，过期返回 410，重算摘要不一致返回 409；
  3. 已 committed：摘要相同返回首次结果（同一个 outcome_id），不同返回 409；
  4. 业务意图 ID 为 `sha256(canonical[client_id, intent_id])`（C5），以它为 `idempotency_key` 建认领行，进 §3 的机器核心；
  5. 核心返回后更新意图行为 committed（或 refused 并记结果码），与 0124 调用在同一事务里。先持久化后返回（I-3）。
- **崩溃与 unknown**：commit 只有一个 PG 事务，要么全部提交，要么全部没有。客户端拿不到响应时调 `GET /machine/v1/intents/{intent_id}`，结果与首次一致；重启后同样（测试：提交后断开连接、重建应用再查）。
- **签名许可**：0124 需要签名命令。机器路径由 API 进程用现有签名密钥构造（与 goal_controls 相同），事实 payload 来自 §2 的机器快照；SQL 侧照常验签。

## 5. 审批（C8）

- 审批决定 handler `control.nexloop_governed_approval_decide` 改名加包装：决定人是机器主体时，在 handler 内抛 42501。这样即使有人把审批能力错登进机器允许表，SQL 也会拒绝（允许表 trigger 本来就拒绝审批能力，这是第二道）。
- `control.nexloop_approval_solo_effective` 改名加包装：请求人是机器主体时返回 false，机器提交的请求不能 solo 自批。
- `control.nexloop_approval_eligible_subjects` 的结果里剔除机器主体，机器主体不计入法定人数。
- 测试：登记一条测试用的需审批能力，机器请求进入待审批；solo 模式开着也不会自批；机器调用审批决定得到 42501；把这三处分别删掉，各有测试变红。
- 有外部效应但没有审批策略的能力：机器 prepare 返回 `approval_policy_required`（用测试能力验证，M5 没有真实的效应能力）。

## 6. HTTP、契约与清单

- 路由：`POST /machine/v1/prepare`、`POST /machine/v1/commit`、`GET /machine/v1/intents/{intent_id}`。都在 machine_key realm，都先过 M3 的准入和限速。api-routes 清单 +3，manifest_version +1。
- 契约（已获授权新增 machine 契约）：`machine-prepare`、`machine-commit`、`machine-intent`，统一错误体复用 `machine-error`。
- 状态码：参数或身份字段 422；对象不可见 404；不在 scope、没有授权、`human` 能力 403；摘要不符或同意图内容不同 409；prepare 过期 410；限速 429；待审批 202。
- 审计：`runtime.audit_events` 记 actor（机器主体）、key_prefix、intent_id、capability、结果码，进入 NX-059 哈希链，不记参数和正文。
- 新 Action 要更新 `test_stage_register_pg` 的计数；部署说明写明先 publish 再验证（新 Action 发布前 404）。

## 7. 切片

| 片 | 内容 | 迁移 |
|---|---|---|
| M5a | 机器事实快照与断言（§2）；审批三处排除（§5）；事实行禁止写入的触发器 | 0441 |
| M5b | 意图表；prepare / commit / 查询的 SQL；0124 改名包装与机器核心（§3）；副本一致性测试；备注 Action 与表（视 Q-M5-1） | 0442 |
| M5c | HTTP 路由、契约、清单、审计；工作台备注入口；stage test world 跑一次 prepare → commit → query；运行单 | 0443（如需） |

每片都带 PG 测试、真实 HTTP 测试、变异检查，不用 xfail 或 skip，新表进擦除覆盖，跑 db_boundary、tenant_fk、system_catalog、copy guard 和擦除覆盖。

## 8. 请 L6 判定的取舍

- D-1：首批写能力需要新建备注 Action 和表（F-4），还是 M5 先只交付基础设施。
- D-2：机器事实采用快照时派生（方案 B），一致性检查落成“无事实行 + 授权变化即 hash 变化”两条测试。
- D-3：0124 机器分支用“改名包装 + 机器核心副本 + 副本一致性测试”，不用 GUC 或临时表打开 C1 例外。
- D-4：认领行由机器核心在同一事务内创建，不走租约。
- D-5：审批的三处排除都在 SQL 内实现。

## 9. L6 条件 PASS 的补充（63bb2c85，R1–R6）

L6 同意 D-2 至 D-5 的方向。以下各条覆盖前文中与之冲突的说法。

### R1 授权事实的 hash 与并发撤销
- `grants` 事实的 payload 包含最新授权行的 `grant_id` 和 `revision`，不只是类型列表。撤销后再用相同类型重新授权，hash 也会改变。
- 机器核心末尾的二次断言对机器主体行、密钥行、该能力的最新授权行加 `FOR SHARE`。授权与撤销的 handler 本来就要对同一行做 `FOR UPDATE`，两者因此串行：撤销一旦提交，之后开始的 commit 必然被拒；与撤销并发的 commit 要么先完成，要么等撤销提交后被拒。测试用两个连接：一个连接持有撤销事务不提交，另一个发起 commit，断言它阻塞，撤销提交后得到 42501。
- 禁止写事实行的触发器按 `authz.nexloop_machine_clients` 判定机器主体（principal 在主体表里即拒绝），不看 payload 的 actor 字段。

### R2 改名包装的前置核对与双重防线
- 0441 改名前用 `pg_proc` 核对 `authz.nexloop_goal_governed_action` 当前函数体的 sha256 等于 0348 版本的值，不一致就中止迁移，让迁移作者先合入新改动。
- 调用链核对：`authz.nexloop_workbench_action_for`（0430、0433）只用来查能力对应的 Action 版本，不调用入口。入口的调用方是 API 进程里的 SQL 文本。测试遍历 `pg_proc`，断言除新包装外，没有任何函数体引用 `_before_machine`。
- 改名后的 `_before_machine` 保留 C1：它内部调用的 service 快照对机器摘要本来就抛 42501。测试断言“用机器摘要直调 `_before_machine` 得到 42501”（以 owner 身份直调，因为 `nexloop_api` 没有它的执行权）。
- 副本一致性测试锁定**基准函数体的哈希**：`_before_machine` 的规范化函数体哈希写在测试里，原入口有任何改动都会变红；同时比对机器核心与基准的差异只在登记的四段里。

### R3 业务意图 ID 与认领行
- 业务意图 ID 为 `sha256(canonical[tenant_id, client_id, intent_id])`。
- 机器核心写 `runtime.nexloop_action_claims`。实现时核对 AT-064 的边界白名单（`test_claim_store_pg`），新函数若出现在白名单检查的范围里，就逐条登记并写明理由。

### R4 commit 顺序与待审批
- commit 的顺序改为：
  1. 重跑 2.1–2.6；
  2. 锁意图行；
  3. 已 committed 或 refused：摘要相同返回首次结果，不同返回 409；
  4. 再判过期：phase 为 prepare 且已过期，返回 410；
  5. 比对摘要，进入机器核心。
  这样客户端在 10 分钟之后重试，或在 unknown 后补查，拿到的都是首次结果，不会是 410。
- phase 增加 `pending_approval`。审批门返回“等待审批”时，意图行记为 `pending_approval` 并记下 `approval_id`，HTTP 返回 202。
- 后续写回：负责人批准后，客户端用同一 intent 和同一 prepare_digest 再次 commit。pending_approval 不再判 prepare 过期，因为摘要在第一次 commit 时已核对过；能否执行由审批本身的有效期决定，审批门会消费这次批准。执行结果把意图行写成 committed 或 refused。审批被拒或过期时，审批门返回不可执行，意图行写成 refused 并记结果码。`GET /machine/v1/intents/{id}` 返回 phase 和 approval 状态，不返回审批人。

- **审批通过后的执行重跑全部检查**（L6 复核 R2 时的附加要求）：
  - 调研结果（bbe5175b）：受治理 handler 只由统一入口调用，0124、0327、0347、0348 各版入口都是如此。审批通过后的执行，也是请求人再次经入口提交、由审批门消费这次批准。没有 domain_worker 或其他路径绕开入口直接调用 handler。M5b 加一条测试锁定这一点：遍历 `pg_proc`，除入口与机器核心外，没有函数体按注册表的 handler 动态调用。
  - 机器请求的执行（pending_approval 之后再次 commit）走机器核心，重跑 C5 的全部检查：身份、密钥 scope、允许表、授权行（机器授权断言，带 FOR SHARE）、`subject_rule`，然后才由审批门消费批准。prepare 阶段和第一次 commit 的判定都不沿用。
  - 测试：机器请求进入待审批，负责人批准；批准之后、执行之前撤销授权，执行被拒（42501），handler 没有被调用，意图行记为 refused。从 scope 里去掉该能力（轮换为窄 scope 的密钥）的情况同样测一次。

### R5 API 代签
- 机器路径的签名命令由 API 进程用现有签名密钥构造。签名内容在原有字段之外，绑定 `client_id`、`intent_id`、`prepare_digest`、`world`；机器核心逐项核对它们与本次 commit 的参数一致。
- 有效期沿用 20 秒。签名密钥只在 API 进程里，不进 Runtime（Pi 没有这个密钥）。现有的部署检查已经保证这一点，测试再断言一次。

### R6 审批排除的判定依据
- 审批决定、solo 判定、可审批人员名单三处，都按 `authz.nexloop_machine_clients`（服务端身份）判定机器主体，不读请求 payload。
- 变异检查：三处分别删掉，各有一个测试变红。

### 写能力的范围（负责人未定备注之前）
- 对象备注的两种做法负责人还没定（L6 的安全对比见判词），M5a/M5b 先只做与备注无关的机制。
- 验证用的写能力 `test.machine.write_probe` 只在测试夹具里登记（注册行、handler、允许表行都由夹具在测试 PG 中写入），迁移、发布目录和 allowlist 里都没有。
- 守护测试：从空库跑完全部迁移后，发布目录和 allowlist 里不存在任何 `agent_or_human`、kind 为 write 的机器能力；读取部署目录里的 manifest 时也断言没有这个能力。M5b 合入后，生产中的机器写入实际上是关闭的。
- 如果负责人选做法一（人机共用备注），另开一片，L6 列的条件作为验收项：纯文本转义渲染、标明“机器客户端备注 / 未核实陈述”、不进 agent 提示的指令区、不作为事实证据、擦除覆盖消费者与成员、人类入口的 CSRF 和权限检查。

## 10. 实现时的调整（M5a）

- 审批决定的排除改为在 `control.nexloop_approval_decisions` 上加触发器（见 [NX-062-M5a-machine-authority.md](NX-062-M5a-machine-authority.md)）。原因：handler 在注册表里按 oid 登记，改名包装不生效，而整段重写原函数又过不了 copy guard。
- 机器事实采用 §2 表中的六类机器专属事实；controls、resource_graph、revision 这三类租户级事实不纳入机器断言。租户级的控制在 handler 和派发时照常检查。

## 11. 对象备注（负责人 2026-11-10 选 A，送 L6 审）

负责人原话“A，按推荐”：新建人和机器共用的对象备注。本节取代 §1 中“需要负责人确认”的部分，L6 列的四条作为验收项（V-1 至 V-4）。

### 11.1 数据与 Action
- 表 `runtime.nexloop_object_notes`：
  - 列：tenant_id、world、note_id、type_name、object_id、author_kind（human|machine）、author_principal_id、author_subject_id、client_id（机器才有）、text、created_at、request_ref。
  - 主键 (tenant_id, world, note_id)，租户外键，RLS 和强制 RLS，只有 nexloop_owner 的策略。所有应用角色都没有直接读写权限，读写都经 SECURITY DEFINER 函数。
  - append-only，只有擦除改写和随对象删除两条例外，都经专用函数，函数里核对调用来源。
- 受治理 Action `nexloop.object.add_note:1`：operation `object_note_add`，`subject_rule` 为 `agent_or_human`，handler `runtime.nexloop_governed_object_note`。
  - 参数只有 type_name、object_id、text、request_id，未知字段 422。
  - 目标必须是 Commitment 或 CommercialRecord，并且对作者可见。人类按工作台对对象的读取权限判断，机器按授权类型和 M4 的可见性函数判断。不可见、不存在、他人租户一律 404。
  - 正文 1–2000 字符，去掉控制字符和双向控制符。
  - 不改对象、不改 revision，不触发通知、派发或任何外部效应，所以不需要审批策略。
- 机器写入走 M5b 的 prepare / commit。授权守卫扩展后，授权类型限定为上面两类。

### 11.2 V-1 纯文本显示与标注
- 工作台对象页新增备注列表和输入框。正文按纯文本渲染：只用文本节点，不用 innerHTML 或 Markdown，不把链接转成可点击。
- 每条备注显示作者类型。机器写的标为“机器客户端 <client_id> 的备注 · 未核实陈述”，人写的标为“<成员显示名> 的备注 · 未核实陈述”。
- 测试：
  - 前端单测：正文为 `<img src=x onerror=…>`、`javascript:` 链接、Markdown 链接时，DOM 里只有文本节点；
  - HTTP 测试：读取接口返回的正文原样，不做 HTML 转换，`Content-Type` 为 JSON；
  - 标注文本出现在页面上。

### 11.3 V-2 不进 agent 提示的指令区，不作为事实证据
- 读取函数只授权给工作台读取路径（`nexloop_api` 经工作台会话）。不授权给 `nexloop_runtime`、`nexloop_domain_worker`、`nexloop_action_worker`、`nexloop_scheduler`，Pi Runtime 的上下文构造读不到备注。
- 备注不是 ontology 对象，不能被 Claim、Commitment 的 evidence 或 fulfillment_evidence 引用。证据引用的校验函数不接受 note_id 形状的引用。
- 守护测试：
  - 遍历 pg_proc，断言只有登记的几个函数引用这张表；
  - Runtime 角色直读这张表、调用读取函数都是 42501；
  - 用 note_id 构造证据引用，被拒。
- 以后如果要让 agent 看到备注，必须另立任务，放进上下文的“数据区”并标注来源和未核实，不在本期。

### 11.4 V-3 擦除
- 消费者擦除：
  - 目标对象关联到被擦除消费者的备注，正文改写为固定标记，作者字段保留；
  - 判定复用 NX-054 的对象到消费者关联函数（`runtime.nexloop_object_consumers_read`）；
  - 正文里写了消费者 ID 但对象没有关联的情况，按擦除时对正文做该消费者 ID 精确匹配，也改写。
- 成员擦除：作者是被擦除成员的备注，作者的 principal 和 subject 改写为擦除占位，正文改写为固定标记。
- 两张覆盖表都登记这张表，整库擦除测试覆盖。
- 保留期：对象删除时，同一事务里删除它的备注，包括保留期到期的删除。legal hold 期间对象不删，备注也不删。
- 测试：两种擦除各一条（擦除前后正文和作者），对象删除带走备注，legal hold 期间不删。

### 11.5 V-4 人类入口
- 工作台走现有的受治理 Action 通道（`/workbench/actions/...`），CSRF 头、会话、Origin 检查沿用现有中间件。
- 权限：工作台角色清单新增 `object.add_note`，给 owner 和 operator（reviewer 只读）。SQL 侧 handler 再核对一次当前角色和对象可见性。
- 测试：
  - 没有 CSRF、CSRF 错误、跨 Origin 都返回 403；
  - reviewer 返回 403；
  - 看不到的对象返回 404；
  - 正常提交 200，并写一条审计。

### 11.6 切片
- M5b：意图表、prepare / commit / 查询、0124 机器分支、授权守卫扩展到写能力、机器请求的审批请求人，加上备注表和 Action 的 SQL 与擦除（V-3）、V-2 的守护。
- M5c：HTTP 路由与契约、工作台备注入口（V-1、V-4）、stage test world 演练、运行单。
- 新 Action 要更新 `test_stage_register_pg` 的计数，部署说明写明先 publish 再验证。
