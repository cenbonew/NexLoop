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
