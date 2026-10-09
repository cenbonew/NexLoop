# NX-018：Source 对已受理 Message 的受治理 READ 派生（方案 B 设计稿）

状态：**设计稿，未实现、未执行任何测试**。等待负责人决定（调度员转达）。L1，2026-10-09。

## 1. 问题与根因

main f58f3ee 的 21 个真实回归（test_message_relay 7、test_offering_runtime_pg 6、投递/effect worker/scope_denials/context_mode 等）全部可归到 `c6e5065`（0062）。实测：`tests/test_message_relay.py::test_independent_cli_governed_assignment_and_persist_before_ack` 在 `045c93f`（0062 前）1 passed / 5.08s，在 `c6e5065` 上 failed / 3.66s。

0062 令 `ContextArtifactProducer.prepare` 与 Context 复制件 READ 必须携带 Source 对 `eios:object:Message/<id>` 及 `actor`/`body` 属性的**当前** READ 证明（`postgres_artifacts.context_message_read_envelope` → `AuthorizedObjectReader._authority`）。授权解析按精确 resource_id 加载 `resource_graph`/`grants`/`scope`/`controls`/`policies` 事实；而 `authz.nexloop_authority_facts` **只由可信配置（0050 `apply_manifest`）写入**，受理新 Message 时没有任何路径产生这些事实 → `ResourceGraphFacts is unavailable`，被 4 层 `except … from None` 收敛为 "Message relay unavailable" / "PostgreSQL authority facts unavailable"。

因此不是夹具遗漏，而是产品缺口：真实部署中每条新 Message 都需要运维重新发布配置，Message→Context 链路实际不可用。只有 `test_context_artifacts` 通过 `configure_message_read`（每条消息重发可信配置）规避了它。

约束：不能在受理时把 per-Message 事实物化写入 `authz.nexloop_authority_facts`——该表的 `authority_epoch_guard` 触发器会推进 authority epoch，改变 `directory_hash`，使所有现有 ServiceSession 失效（每条消息一次全局失效，不可接受）。

## 2. 派生规则（只从已有受治理事实派生，不新增授权来源）

Source 主体 P 对 Message M 的 READ（object + `actor` + `body` 两个属性，且仅此三个 resource）成立，当且仅当在同一短事务中**全部**满足：

1. **已受理**：`runtime.nexloop_conversation_messages` 存在 (tenant T, world W, M) 行，且 `runtime.nexloop_message_inbox`/`nexloop_message_outbox` 中有同一 (T, W, message_id) 的受理记录（outbox 由 0046 受理事务写入；不接受 pending prepare 或未提交事务）。
2. **租户/世界**：T、W 取自 P 的 server-side 身份快照（`nexloop_service_identity_snapshot`），只在 RLS `eios.tenant_id=T` 下查行；W 必须为 `real`；消息行的 T/W 必须相同。
3. **Consumer 边界**：M 所属 `runtime.nexloop_conversations.consumer_id = C`，且 P 当前对 `eios:object:Consumer/C` 持有 READ（走现有可信配置事实与 `nexloop_assert_read_authority`，不新造）。
4. **显式启用规则**：可信配置中存在一条新的物化事实 `message_read_rule`（kind 新增，key `[P]`），payload 声明 `{"type_name":"Message","fields":["actor","body"],"requires":"consumer_read","valid_until":…}`。没有此规则的 Source 不派生任何 Message READ（默认关闭、fail closed）。规则变更走 manifest 发布，会正常推进 epoch——这是低频的运维动作，与"每条消息"无关。
5. **Run 凭据**：派生只对 Source 服务身份（`subject_kind='service'`，`run_context` 为 null）生效；Run 凭据仍受 0034 `allowed_resources` 限制，不派生。

派生结果严格限定在：P 已能 READ 的 Consumer 名下、已受理的 Message、其 object + `actor` + `body`。不覆盖 Conversation、其它字段、未受理消息或其它 Consumer；不产生 EDIT/CREATE/EXECUTE。

## 3. 迁移形状（临时编号，接在本线 0066/0067 之后，例如 0068）

- `authz.nexloop_authority_facts.fact_kind` 检查约束与 0050 manifest 校验加入 `message_read_rule`（只由可信配置写）。
- 新私有函数 `authz.nexloop_derived_message_fact(p_digest,p_world,p_kind,p_key) returns jsonb`：仅当 key 精确匹配 `eios:object:Message/<64hex>` 或其 `/actor`、`/body` 属性且第 2 节条件全部满足时，返回确定性 payload（不含 `snapshot_digest`，由 EIOS 模型校验时计算——facts.py `_RepositoryFacts` 在 digest 为 None 时自行计算）：
  - `resource_graph`：root=该 Message 资源，parent=`Consumer/C` 资源，`registry_revision`/`registry_digest` 取规则事实与消息行的确定性组合。
  - `grants`：principal P、仅 `read`、`valid_until` = min(规则 valid_until, Consumer READ grant valid_until)、`revision` 取规则事实 revision。
  - `scope`/`controls`/`policies`：复用 P 对 `Consumer/C` 的对应已配置事实内容，只把 target 替换为 Message 资源（保持同等 scope/控制/策略约束，不放宽）。
- 包裹 `authz.nexloop_load_authority_fact`（0034 最新版本）：物化表无行时才调用派生；物化事实优先（保留现有可信配置测试路径）。
- 包裹 `authz.nexloop_assert_read_authority`（及 `nexloop_fact_coverage` 依赖处）：对 Message 派生条目，"for share 锁行"改为锁 规则事实行 + Consumer READ 事实行 + `conversation_messages` 行 + `conversations` 行，再重算派生 payload 的 record_hash 与证明中的比较；任何一项变化即 `read authority revision changed`。
- 不修改 0001..0067 任何已发布文件；全部以 rename 私有 alias + 新 public wrapper 的现有链式方式追加。

## 4. 撤权与删除传播

| 变化 | 传播 |
|---|---|
| 撤销 P 对 Consumer C 的 READ（manifest） | Consumer 事实 hash 变化/缺失 → 所有派生 Message READ 在下次使用时 SQL 拒绝；epoch 推进同时使旧 session 失效 |
| 删除/停用 `message_read_rule` | 派生为 null → `read authority dependency missing` |
| Message 删除或更正（governed Message.edit / 删除） | 消息行缺失或 revision 变化 → 派生 hash 变化 → 拒绝；0062 既有"当前 body 必须等于不可变 outbox/Context"检查继续生效 |
| Conversation 迁移到其它 Consumer | parent 变化 → hash 变化 → 拒绝 |
| 规则/Consumer grant 到期 | `valid_until` 进入证明 expires_at 最小值，0064 期限检查生效 |

## 5. 测试迁移

改为走派生（删去 per-Message 可信配置写入，只配置一次 `message_read_rule` + Consumer READ）：
- 21 个回归：test_message_relay（7）、test_offering_runtime_pg（6）、test_local_message_delivery_assembly、test_message_driven_delivery、test_native_web_delivery、test_effect_dispatch、test_effect_worker_cli、test_local_effect_worker_kill、test_scope_denials、test_context_mode_configuration[extra2]。它们需要在共享夹具（`local_message_assembly_fixture` / `receipt_plan` / offering fixture）的初始 manifest 中加入规则。
- test_context_artifacts 的正向 READ 用例改走派生。

保留可信配置路径（物化事实优先，验证两条路径语义一致且互不放宽）：
- `configure_message_read(allow=False, withdraw=...)` 的撤权负例（八种 READ 撤销、body/actor/object 单独撤销）——改为撤销 Consumer READ 或规则，另保留一例物化 grant 为空时显式拒绝（物化事实优先于派生，空 grant 不得被派生"补权"）。
- 0062 的 test_context_source_read_reproduction 复现例。

## 6. 必须新增的负向测试（全部真实 PG，fail closed，0 Artifact/0 job/0 binding）

1. 他租户：同 message_id 存在于另一 tenant → 拒绝（RLS + 身份快照）。
2. 他 world：world≠real 或证明中 world 被改 → 拒绝。
3. 未受理：仅 prepare、事务回滚、只在 inbox 无 outbox → 拒绝。
4. 已撤权：撤 Consumer READ / 删规则 / 规则过期 → 下一次 prepare/bind/Artifact READ 拒绝；已签发证明在 SQL 尾检被拒。
5. 已删除/已更正消息 → 拒绝。
6. 其它 Consumer 的消息（P 无该 Consumer READ）→ 拒绝。
7. 越界字段：请求 Message 其它属性或 Conversation → 不派生、拒绝。
8. Run 凭据请求派生 → 拒绝（allowed_resources 不变）。
9. 物化空 grant 优先：存在显式空 grants 事实时不得被派生覆盖。
10. epoch：受理新消息不改变 `directory_hash`（断言旧 session 继续可用）——这是相对"物化"方案的核心性质。

## 7. 风险与待确认

- `registry_digest`/`revision` 的确定性构造需与 EIOS 决策服务对 ResourceGraph 完整性（`closure_complete`）的要求逐项核对，先写一条 SQL 派生 payload ↔ Python 模型校验一致性测试。
- Consumer 作为 Message 的 parent 是否符合 EIOS 资源层级语义，需要负责人确认；备选是要求 Conversation READ（但 Conversation 同样是动态对象，会回到同一问题）。
- `message_read_rule` 是新的 fact kind，改变授权模型——这是本方案需要负责人批准的核心点。
- 不涉及 packages/contracts JSON Schema。

## 8. 与本线其它工作

本线已提交第 1 步（0066，`e272d50`），不含回归修复。第 2 步（0067 v4 关系 Context）及 backend 锁/证明复用改动在工作区待定向回归结果后单独提交；按调度员指示，在负责人决定前暂停第 2/3 步之外的新工作。
