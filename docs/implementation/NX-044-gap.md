# NX-044 差距检查：人类审核 Action 与 Schema 发布

基线：main `23332dc`（迁移到 0081）。检查对象：
- NX-012：受治理对象写入（`object_actions.py`、`object_edits.py`、`action_governor.py`、`action_definitions.py`、0007/0009/0010/0015/0054）
- NX-015：效应意图与执行链
- 0081：Role 策略外层包装
- vendored EIOS 的 ontology 注册与兼容性代码

## 1. 人类审核 Action 需要什么（ADR-019 §3.5）

| 需求 | 现状 | 差距 / 做法 |
|---|---|---|
| reviewer 由服务端会话解析，只能是人类 | 浏览器人类会话由 `authenticate_browser_business` 复核（0045，binding `subject_kind='human'`），`nexloop_assert_action_authority` 对浏览器摘要走人类分支（重新核对 12 类事实） | 没有任何 definer 要求调用方**必须**是人类。新 definer 要求：摘要存在于 `authz.nexloop_browser_token_realms`，且身份快照 binding 的 `subject_kind='human'`。service 与 agent（模型）凭据不在浏览器 realm 中，一律拒绝；reviewer_ref 由 SQL 从快照得出，请求体不能提供 |
| 权限 `ontology.schema.review` | NX-045/046 已把队列读取挂在 `eios:action:ontology.schema.review:1` EXECUTE 上 | 审核决定沿用同一个 resource，读与写使用同一权限 |
| 审计 | 对象写入的审计在 `runtime.nexloop_action_claims` 与 `authz.nexloop_action_decisions`，只适用于对象 create/edit 能力的 Action；审批端口 `UnavailableApprovalPort` 未接通 | 审核决定不是对象写入，不经 ActionGovernor。新增 `ontology.nexloop_review_decisions`：按 `review-decision` 契约持久化决定、reviewer、授权事实摘要（12 类事实的 record_hash）、发布结果和回流状态，同一事务内与候选状态转移一起提交 |
| 并发与幂等 | 候选状态机已有 revision CAS（`ontology.nexloop_candidate_transition`，NX-045） | 请求携带 `expected_revision`；`decision_id` 由服务端从 principal 和 Idempotency-Key 派生；同一 decision_id 重放返回已记录的结果，不会二次执行 |
| reject 冷却 | 0074/0078 触发器（单一时刻） | 直接复用：rejected 转移即触发 |
| merge_into | `ontology.nexloop_candidate_merge_effects`（NX-046）负责写别名并把 Claim 复位 | 直接复用；目标 ref 必须是最新定义中同类的现存定义 |

## 2. Schema 发布（approve）的现状

1. **NexLoop 目前不能发布任何对象类型的 v2**：0050 / 0077 的 `control.nexloop_configure_manifest` 遇到 `v_version<>1` 时报 'schema increment not supported'。vendored EIOS 的 `PostgresOntologyRegistry` 会写版本，但它不是 NexLoop 的受治理入口。
2. **EIOS 中可复用的部分**：
   - `ObjectTypeDefinition` / `PropertyDefinition` 模型校验
   - `eios.ontology.semantics.assert_object_compatible`：primary_key 不变；必填属性不可删除；类型与描述符不可变；enum 只能增加不能删除
   - `schema_contract_digest`：Action 绑定 Schema 时使用的摘要
3. **Action 与 Schema 的绑定**：Action 定义不可变（`on conflict do nothing`，按 jsonb 全等比较），并且按 `version` 和 `schema_digest` 绑定 Schema。0054 的编辑函数还要求 `a->'schema'` 与 `object_type_versions` 完全一致，且对象的 `schema_version` 等于 Action 绑定的版本。由此，在 Consumer v2 上写入新属性需要同时具备：
   - **后继 Action 版本**（如 `Consumer.edit:2`），对象类型引用改为 v2 及其摘要，其余与前一版本相同；
   - **已有对象的 `schema_version` 升级到 v2**：新增的属性都是可选的，enum 只增不减，因此旧对象的属性在 v2 下依然有效。
4. **授权**：Action 和属性都按版本或对象授权（`eios:action:Consumer.edit:2`、`eios:property:Consumer/<oid>/<新属性>`），都是新的 resource，发布本身不应该写入授权（门槛“权限不变”；ADR-020 §3 规定授权只经可信配置）。**已向调度员提问**；在答复前按方案 B 实现：发布不写授权，授权由可信配置授予之后再回流，等待中的 Claim 才会被应用；未授权时 reflow 结果明确显示仍在等待。

## 3. 发布门槛（docs/08 §5 与 ADR-019 §3.5 落到本任务）

| 门槛 | 落地方式 |
|---|---|
| 引用与结构有效 | owner 类型或属性存在且为最新版本；新名称合法且不重复；词表值所属属性是封闭 enum，值类型匹配且值不存在；新类型名不重复；值类型可映射 |
| 零越权 / 零跨租户 | 只有 real world 的候选能发布租户级 Schema；租户由会话推导；发布事务前后本租户 `authz.nexloop_authority_facts` 的摘要必须完全相同（发布不写任何授权） |
| 权限不变 | 同上，并且不改动任何 grant；后继 Action 与前一版本只允许在 version、对象类型引用、`contract_digest`、`previous_version` 上不同 |
| 迁移 / 索引兼容 | Python 侧由 EIOS `assert_object_compatible` 判定仅为增量变更；SQL 侧做结构核对：新定义等于旧定义加一个可选属性，或某个 enum 追加一个值，其余部分逐项相等；对象只升级 `schema_version`，不改属性；召回索引由回流时 `index_object_type` 用同一个 embedding profile 重建 |

门槛不通过时：候选**保持 pending_review**，记录 `status_reason` 与 `publication.gate_failures`，写入一条 outcome 为 `publication_failed` 的审核决定，不自动重试。

## 4. 0081 外层包装

0081 只对 `RoleExecutionCeiling` / `RoleAssignmentScope` 两个类型的 create/edit 追加 Role 策略检查，对 Consumer 等类型直接放行。本任务**不改写也不包装** 0081：
- 审核决定不经过 create/edit 函数；
- 回流应用仍走 0081 包装后的正常编辑路径。

## 5. 禁区与装配

- `backend.py` 在 L1 禁区内。审核决定端口放在新模块 `review_actions.py`，由 `http_api.py` 的工厂装配。工厂会读取 Backend 的 `_pool`、`_signer`、`_lock`，并在每次请求时用 `authenticate_browser_business` 复核人类会话；backend.py 不做任何修改。
- 回流（别名或类型重建索引、Claim 重匹配与应用）需要服务凭据，不在人类请求内执行：决定记录 `reflow_status='pending'`，由服务侧 `ReviewReflowWorker` 执行并回写状态。
