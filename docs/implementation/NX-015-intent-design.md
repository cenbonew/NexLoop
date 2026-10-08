# NX-015：稳定 business intent 最小入口设计

本文件是基于当前源码的实施建议，不是已实现接口或验收证据。本次只读公开源码/契约并新增本文；没有读凭据、执行迁移、调用 provider、更新 planning。NX-015 的真实外部效应验收仍需独立实现与执行。

## 现有边界与可复用点

| 实际源码 | 可复用行为 | 设计约束 |
|---|---|---|
| `nexloop_eios/postgres_action_claims.py:PostgresActionClaimPort._execute` | 真实 EIOS facts resolver、当前 session tenant/world、完整 fact vector、25 秒以内 HMAC、受限 definer、claim fence/revision | 当前方法自己开事务；原子组合需要私有 prepare/execute 分拆，不能套两个独立提交冒充 UoW |
| `eios/migrations/0007_action_claims.sql:nexloop_action_claim_command` | tenant/world/action/intent 唯一键、锁后重验、terminal replay | 原第 93 行同时比较 `principal_id` 和完整 binding；不能删除主体比较让两个角色共享 claim |
| `eios/actions/models.py:ActionReservationKey/ClaimBindingPayload` | 稳定 reservation key 与 invocation/action/reference/request digest/capability/adapter/target binding | invocation、binding 必须由意图服务持久生成；不得把新 Run/tool_call 放入稳定 claim binding |
| `nexloop_eios/action_governor.py:govern_published_action` | 从真实发布定义/capability 获得 Governor permit；读取 PG 时钟 | approval authority 未接入；非空 governance.policy_refs 当前明确 fail closed，不得改成假批准 |
| `nexloop_eios/object_actions.py:GovernedObjectCreator.create` | 真实 Action schema/governance；对象写与最终 claim 结果经 protected SQL | 当前调用 governor 可能已独立 reserve；此模式不能直接证明 intent/outbox/reserve 三者原子 |
| `nexloop_eios/runtime_activation.py:RuntimeActivationPort.accept` | 同一受限连接/事务调用两个真实 signed definer，commit 后 ACK | 可借鉴私有组合方法，不借用 queue 权限授 effect 权限 |
| `eios/actions/write_attempts.py` | 既有 attempt 状态与 CAS 语义 | 当前没有装配通用 PG store/provider；旧领域 migration 不可整体搬入 |

依据 `docs/handoff/docs/06_ACTION_RUNTIME_AND_RECOVERY.md` §2–5/§8–9：business_intent 跨 Run/tool call 稳定；不同规范化 payload 返回 409；accepted 仅为持久受理；未知外发必须查询；Run lease 与 Action lease 分离。`packages/contracts/action-intent.schema.json` 是目标契约，其 identity/digest/version/timestamps 字段必须由可信后端计算或核验，不是 runtime 可以指定权限的参数。

## 推荐身份协议：共享 intent，固定真实执行主体

采用一个具有独立 EIOS Action grant 的**真实服务/Agent 执行主体**，凭自己的凭据持有同一 intent 的 Action claim。它不是把提交者换成一个假的身份，也不沿用别人的 Run bearer。固定 executor principal 的选择由服务端配置/治理登记决定，第一次受理后冻结；SQL 必须证明当前 Worker 的实际认证 principal 与登记值相符。

两个真实提交主体 A/B 可以申请同一可信业务键，但分别进行当前 Run/source/Action 权限与目标范围核验。共享键不授读取或执行权：

1. 服务端从认证 session 得 tenant/world/actor；从可验证的计划步骤/业务对象找回稳定业务键。`consumer_ref/goal_version_ref/plan_step_ref` 必须存在并可访问，不能把用户传入相同字符串当成同一业务事实。
2. A/B 各自获准向相同消费者/计划步骤提出这个 Action，且拥有治理定义允许的 intent 访问范围，才可连接同一个 receipt。其他主体、租户、world 返回统一拒绝，不泄露“意图是否存在”、payload hash 或 provider reference。
3. A/B 的提交来源、Run 与可观察 rationale/evidence 作为独立关联记录追加；不会更改已冻结 executor、payload 或 claim binding。提交关联不是永久 grant。
4. 执行主体仅使用自己的 Action authority reserve/finalize，同 key 的 principal 始终一致，保留 0007 的检查。A/B 不直接 reserve 固定 executor 的 claim；访问共同 receipt 经独立的受治理读取端口，不能调用原 claim replay 来绕过主体检查。
5. 外发前同时重验 executor 的真实当前 Action 权限，以及原受理的有效授权来源/业务约束。来源凭据只保存不可逆摘要/安全引用，通过真实身份/事实链解析；不得伪造 Run identity。Run TTL 到期不会被延长：若要求新的运行许可，新的合法 Run 经受控再授权关联**同一 intent**；未获得新许可前保持 blocked，不自动重发。

源 Run 当前授权是否必须持续有效到外发，还是可在受理时明确移交至独立长期服务权限，是需主 Agent 确认的治理策略。默认采用更严格的“来源当前有效 + executor 当前有效”，不能把 accepted 当无限期授权。两种策略都要求明示委托关系与范围；仅队列 Worker grant 不够。

## 稳定键与冲突

建议私有后端入口（尚未实现）：

```python
service.submit_action_intent(
    action_name, contract_version,
    consumer_ref, goal_version_ref, plan_step_ref,
    parameters, expected_versions, evidence_refs, decision_rationale,
)
service.read_action_receipt(intent_ref)
```

caller 不提交 tenant/actor/executor/任意 intent_id。计划服务在可信的 `(tenant, world, plan_step_identity, action_semantic_slot)` 唯一索引下建立 UUID intent_id 与 receipt_id；新 tool_call 或重新规划同一步查回原 key。不同合法意图可以携带相同内容。新意图须显式 supersedes 并处理旧 unknown，不能靠增加 action version 或改变 tool_call 绕过去重。

`payload_digest` 覆盖 effect-affecting 规范化参数、目标/渠道、冻结 Action/capability 版本与语义，不含新 Run/tool_call、请求时间或 rationale 等审计噪声。expected_versions 属于持久前置条件：改变版本不能静默替换旧请求；返回 409/需显式再治理。完整冻结请求与 digest 都存 PG，不能只信 caller digest。Action contract/version/capability/adapter/target 变化也按冲突处理；旧请求保留原值。

先检查当前身份与目标访问，再锁稳定键并比较 payload/binding。相同且获准访问返回同 receipt；不同返回公开固定 `intent_payload_conflict`（HTTP 409），不覆盖、不生成 outbox、不暴露旧 payload。拒绝访问与 payload 冲突必须分开，不能把授权失败伪装成可重试冲突。

## 原子受理与迁移边界

建议新增 append-only bootstrap migration，具体编号由主 Agent 在当前 head 后分配；原 0007 与 1–39 的已发布字节不改。

- `runtime.nexloop_action_intents`：可信稳定键、UUID intent/receipt、冻结请求/digest/Action binding、真实 executor principal、来源引用、治理状态。它是受治理 Action 技术登记，不以直接表写实现正式业务对象更新。
- `runtime.nexloop_intent_submissions`：A/B 的真实主体与 Run/来源审计关联，读取仍需 current access checks。
- `runtime.nexloop_effect_outbox`：每 intent 唯一待办、可用时间/lease/fence、执行阶段，受限 definer-only。不能直接复用通用 queue grant 外发。
- `runtime.nexloop_effect_attempts`：稳定 provider key/ref、dispatch_started、unknown/reconcile 证据、attempt revision/fence；无 raw credential、无 provider 敏感诊断原文。

表应有 trusted tenant/world 边界及受限 grants；全局 auth 技术例外需明确说明，不能声称不存在的 RLS。应用直接 SELECT/INSERT/UPDATE 均拒绝；签名命令由 actual session 的完整 EIOS proofs 驱动，锁等待后/返回前再验时钟、版本与控制条件。锁按统一顺序 stable intent → 业务资源排序 → claim/attempt，不能与 claim 自身 advisory/row 锁反向组合。

**最薄选择：入口事务只治理并持久化 intent + source association + receipt accepted + effect outbox；Action claim 在 Worker 后续领取时由固定真实 executor reserve。** 此时不虚称入口已经持有 Action execution permit。上述四项在一个连接/事务提交后才返回 accepted。若需求要求 accept 同时 reserve，则必须把 existing claim proof preparation 与 execution 私有拆分、同一事务实际调用原 claim definer；不能把两个 `PostgresActionClaimPort.reserve()` 独立提交叫原子。固定 executor 与 submitter 两个真实身份均需独立证明，不能在 definer 中任意代签主体。

建议新模块 `action_intents.py` 提交/读取；`effect_dispatch.py` 独立 Worker；`effect_provider.py` 明确 dispatch/query capability；小幅私有 `postgres_action_claims.py` prepare/execute 仅在确需 UoW 时进行。Backend 增窄端口，Runtime tool 只提交/查询同 intent，不具 provider key/DB 权限。target Action 名需正式注册发布并符合契约 `nexloop.*`，不可用未发布字符串或现有 Consumer.create 伪装外部服务效应。

## Worker 与 unknown/query-first

Worker 领取 effect outbox，绑定独立 Action fence；从已发布 Action definition 得 Governor permit，并重验双方授权策略、目标/计划有效性、消费者最新约束、资源版本与预算。现有 policy/approval 无权威来源的分支继续 blocked；不能用空 policy_refs 规避业务需要的约束。原 claim 只管执行 ownership/replay，不能替代 contact/consent/control revision 审核。

外发前短事务持久化 attempt 的 dispatch_started、固定 provider idempotency key、Action claim revision/fence；commit 后才 HTTP，不持有长业务事务。首次 key 来源于 intent+provider namespace，跨重试不变。任何 transport timeout/进程死后 dispatch_started/orphan 都视 unknown，不先 mark_retryable 再发送。

恢复只查询相同 key/ref：

- provider 已受理/完成：按该 Action 定义的 accepted/delivered/fulfilled 语义更新原 receipt；只有业务成功定义满足才 finalize existing claim succeeded。
- 查询结果不明/失败：保持 unknown 或 still_unknown，记录脱敏证据；不得 permanent_failure 伪装已确定，也不得新发。
- 可证明未受理：仅 provider 协议明确允许安全重试、当前所有权限/约束/预算仍有效时，以同 key 在新 attempt fence 下再发。
- 无查询/幂等能力：禁止自动重发，挂人工核对；不承诺通用 exactly-once。

query/reconcile 需要独立明确的当前读取许可；撤去发送许可不应通过“查询”再次发送。若连查询也失权，保持 unknown/blocked，不绕过 EIOS。旧 fence 结果提交拒绝；有效 receipt/attempt/outbox/claim finalize 在同一短事务更新，不出现 terminal claim 已成功而 receipt 还 unknown 的独立提交窗口。补偿是另一项受治理 Action，保留原效应事实。

## 主 Agent 需确定的最小接口

1. 第一项真实注册 Action/服务效应及 provider；dispatch/query 的确定状态语义、幂等与查询能力。受控持久 HTTP 故障 provider 可验协议，但不得记为真实渠道交付。
2. 可信稳定计划步骤/业务键的具体权威来源与 intent read/associate 权限资源；未装配权威来源不能接受任意 caller plan_step_ref。
3. 固定真实 executor 配置与受理后的授权连续性策略；两提交者不能各自持有同一原 claim。
4. accept 是否必须同时 reserve；推荐先原子 intent/outbox accepted，再 Worker reserve，避免不必要的 claim UoW 改动。
5. 目标契约当前只有 ActionIntent，没有 receipt/provider protocol schema：需定义受限公开 receipt 的状态与引用字段，区分技术 accepted 和业务成功。

验收至少覆盖 AT-033～037：真实 PG + 两个真实授权主体、第三主体拒绝、同 intent 一次外发、不同 payload 409、原子 rollback/commit 未知重放、provider 受理后 SIGKILL query-first 零新发送、撤权 dispatch 请求为零、旧 fence 不得提交。没有外部凭据仅阻塞相关真实渠道验证。

## 本次实际读取

执行 `cat docs/implementation/NX-015-effect-source-audit.md`、`cat packages/contracts/action-intent.schema.json`、`cat/rg/sed` 读取 docs/06、`postgres_action_claims.py`、`object_actions.py`、`action_governor.py`、`eios/actions/models.py`、`eios/authz/operations.py`、`0007_action_claims.sql`。只新增本设计文件；没有运行测试或声称接口已实现。
