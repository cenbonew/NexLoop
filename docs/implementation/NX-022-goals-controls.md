# NX-022 Goal/KR 与持续对齐（控制面）实现说明

开发线 L4，分支 `claude/nx022-goals-8a7828`，BASE `f58f3ee35ba0296271a5d0498693d474d220d801`。NX-022 正式依赖 NX-018；本线为先行开发，**不声明 done**，状态由调度员判定。证据数据见 `NX-022-goals-controls-evidence.json`。

## 1. 迁移（临时编号）

`packages/eios-core/src/eios/migrations/0066_nx022_goals_controls.sql`（catalog 0066、`versions.lock.json` bootstrap_revision 0066；revision 字符串只出现在这两处，测试经 `tests/support/release_metadata.py` 读锁）。不改任何已发布 0001..0065 文件或函数，只**调用**已有：

- `authz.nexloop_assert_action_authority`、`authz.nexloop_service_identity_snapshot`（含 0045 浏览器人类分支）、`authz.nexloop_authority_signing_keys`；
- `control.nexloop_action_definitions`、`runtime.nexloop_action_claims`（已发布 Action 与 claim/lease）；
- `ontology.objects.nexloop_revision`（计划时的客户对象版本）。

新增表（全部 `control` schema、`tenant_id+world` 主键前缀、owner `nexloop_owner`、ENABLE+FORCE RLS 按 `eios.tenant_id`、对所有应用角色 revoke）：

|表|用途|不可变规则|
|---|---|---|
|`nexloop_metric_definitions`|批准的 MetricDefinition：聚合（count/sum/ratio_of_sums 枚举）、单位、币种、成熟窗口、退款规则、分母口径|append-only，新口径=新版本|
|`nexloop_goals`|目标身份：long_term/stage/agent、父目标、owner|只允许 current_version +1，禁止删除|
|`nexloop_goal_versions`|O、周期、优先级、预算、约束、父版本、发布者种类、变更摘要、影响范围、发布时控制 revision|仅 published→superseded，内容不可原位改|
|`nexloop_key_results`|KR→(metric_id,version)、目标值、方向、窗口|append-only|
|`nexloop_metric_observations`|观察值、分子/分母、发生/成熟时刻、data_mode、来源与证据|append-only；real 世界只允许 real/test，非 real 世界不得标 real|
|`nexloop_control_heads` / `nexloop_control_events`|每 tenant/world 单调控制 revision 与事件史|事件 append-only|
|`nexloop_control_scopes`|tenant/role/consumer/strategy/action_type 暂停状态|禁止删除|
|`nexloop_budget_limits` / `nexloop_budget_consumption`|tenant 级 model/incentive 预算与消耗账本|消耗 append-only|
|`nexloop_run_goal_bindings`|Run→目标版本追溯（含绑定时控制 revision）|append-only|

## 2. 写路径与授权（复用 EIOS 链，不另造）

唯一写入口 `authz.nexloop_goal_governed_action`：与 0058 assessment 相同的受治理 Action 协议——已发布 Action 合同（`goals.metric.approve / goals.version.publish / goals.agent.propose / goals.control.set / goals.budget.set`）、`govern_published_action` claim/lease、Python 侧 `AuthorizationDecisionService` 对 `eios:action:<name>:<ver>` EXECUTE 的当前决策、HMAC 签名事实快照，SQL 侧再核验事实 record_hash、合同未变、claim 未被 fence，并在 commit tail 再验一次。capability↔operation 一一对应。

人类边界：除 `goals.agent.propose` 外全部要求 live identity 的 `subject_kind='human'`（只有真实浏览器 BrowserBusinessSession 产生）；有 EXECUTE grant 的 Agent 或 service 仍被拒。Agent proposal 只能新建/改版 `agent` 种类且 owner 为自己的目标，必须引用父目标**当前**版本、周期在父周期内，KR 只能引用已批准指标。

Python：`nexloop_eios/goal_controls.py`

- `GoalGovernedActions`：approve_metric / publish_goal / propose_agent_goal / set_control / set_budget；金额/目标值只接受十进制字符串/Decimal/int，拒绝二进制浮点。
- `ControlPlane`：snapshot / assert_dispatch / reserve_budget / budget_status / record_observation / compute_key_result / bind_run / read_goal；tenant、world 全部来自已认证会话。
- `goal_version_ref(goal_id,version)` → `goal:<id>@<version>`，满足 `packages/contracts/*` 中 `goal_version_ref` 的 pattern。

## 3. 控制 revision 与 dispatch 检查（AT-006/AT-007 控制面）

每次暂停/恢复、预算调整、目标发布、Agent 子目标、指标批准都在同事务内推进 `control_heads.revision` 并写事件。计划/入队时调用 `authz.nexloop_control_snapshot` 得到：

```json
{"control_revision":N,"scopes":[{"kind":"strategy","ref":"..."}],"goals":[{"goal_id":"...","version":v}],
 "objects":[{"type_name":"Consumer","object_id":"...","revision":r}],"budgets":["model"]}
```

dispatch 前调用 `authz.nexloop_assert_dispatch_controls(digest,world,snapshot)`（仅 root service 身份）。它对 control head 加 share 锁（与 owner 变更的 head 行更新互斥），依次拒绝：

|SQLSTATE|reason|条件|
|---|---|---|
|NXC01|control_paused|tenant 级或快照内任一 scope 当前暂停|
|NXC03|goal_version_stale|快照目标或其任一祖先不再是当前 published 版本（父目标改版→子目标需重新对齐）|
|NXC04|object_revision_stale|计划时客户对象的 `nexloop_revision` 已变|
|NXC02|control_revision_stale|快照之后出现影响 tenant、快照 scope、目标链或所用预算的控制事件（恢复≠回放，旧任务需重新评估）|

无关 scope 的变更不让其它任务失效。

### 给 L1 / 调度员的挂接点（本线**未修改** L1 文件）

1. 入队/建 intent 时保存 snapshot：需要在 `runtime.nexloop_effect_intents` 或队列任务旁持久化 snapshot（新列或旁表），属 L1/NX-024 与契约变更，需调度员决定；契约提案见 §6。
2. `nexloop_eios/effect_execution.py::prepare_effect_dispatch`（外发前）与 `runtime_dispatch.py::run_once`（提交 Pi 前）：在**同一事务**中调用 `ControlPlane.assert_dispatch(snapshot, connection=c)` 或直接 `select authz.nexloop_assert_dispatch_controls(...)`；捕获 `ControlDenied` 后转为 stale/blocked 并生成重新评估，而不是重试旧动作。
3. 模型调用网关与优惠类 Action 在消耗前调用 `ControlPlane.reserve_budget(...)`。
4. Run 创建时 `ControlPlane.bind_run(run_id=..., goal_ref=...)`。

## 4. 预算（AT-043 判定部分）

`authz.nexloop_reserve_budget`：锁 limit 行，周期内已用+本次>上限即 `NXB01 budget_exhausted`，不写任何行；未配置预算 `NXB03` fail-closed；同 consumption_id 同内容重放返回 `replayed`，不同内容 `NXB02`。owner 下调上限不删除/改写已记录的合法消耗。incentive 必须 ISO 币种整数最小单位；model 允许高精度 numeric。预算调整推进控制 revision，依赖该预算的旧快照失效。

## 5. KR 计算

`authz.nexloop_compute_key_result` 只按 MetricDefinition 的枚举聚合（count/sum/ratio_of_sums）在 KR 窗口内计算；未成熟观察（`matures_at>as_of`）单独计数不计入；real 世界只计 `data_mode='real'`，test 行计入 `excluded_non_real_observations`；模拟数据不能写入 real 世界。观察只能由 root service 身份写入（Agent Run 不能自报指标）。没有任何调用方 SQL 执行路径。

## 6. 未完成项与需要决定的事

- **契约提案（未改 packages/contracts）**：`run-command` 与 `action-intent` 增加可选 `control_snapshot`（上述结构）或 `control_revision`；`goal_version_ref` 采用 `goal:<goal_id>@<version>`。
- dispatch 检查与预算预留**尚未接入**真实 effect/runtime 派发与模型调用路径（见 §3 挂接点）；因此 AT-006/007/043 只完成控制面判定。
- AT-006 的“UI 状态一致”未做（负责人工作台属 NX-028）；可读接口 `read_goal`、`budget_status`、控制事件表已具备。
- AT-007 的“重新评估”只到判定层：检查返回 stale 原因，Plan/PlanStep 表与重评估任务生成属 NX-024。
- 读取函数（read_goal / compute_key_result / snapshot）只按已认证会话 tenant/world 隔离，未逐资源做 EIOS READ 决策；若需要属性级读授权应在 NX-023 Context 读取时补。
- 观察来源核验（商业系统证据→观察）与退款修正链未实现（M19/AT-042，属 NX-027）；结构已区分 data_mode/成熟窗口。
- 受治理 Action 被 SQL 拒绝后，其 claim 保持 active 至 lease 到期（与现有 Action 行为一致），同 request_id 不能被其它主体复用。

## 收口：派发路径接入（分支 `nx022-close`，BASE `7b831eff7e2505d1e6365a84c50468bb21a25da1` = main；临时迁移 0097）

### 实现
- **迁移 `0097_nx022_dispatch_controls.sql`**（只新增表、触发器、函数，并给 scheduler 增加 `reserve_budget` 执行权；不改已有迁移）：
  - `runtime.nexloop_effect_dispatch_controls`：在 `runtime.nexloop_effect_submissions` 上加 AFTER INSERT 触发器，由服务端从已存数据推导快照：
    - 消费者；
    - 动作类型；
    - 提交 Run 的 Role（`authz.nexloop_role_run_bindings.role_ref`）；
    - 该 Run 经 `bind_run` 绑定的 NX-022 目标版本；
    - 当时的控制 head。

    快照按每个提交 Run 各存一份，派发时取最新一份。不接受调用方提供的快照。
  - `runtime.nexloop_task_dispatch_controls`：在 `runtime.jobs` 上加 AFTER INSERT 触发器，对带 `run_command` 的运行任务记录入队时的控制 head。
  - `authz.nexloop_assert_intent_dispatch_controls` / `authz.nexloop_assert_task_dispatch_controls`：都复用 0068 的 `assert_dispatch_controls`。
    - 任务版的作用范围取自存储的 Run 命令：`consumer_ref`、`role_ref`、`goal_version_ref`。只有 `goal:<id>@<v>` 形式才是 NX-022 管理的目标，其他形式（例如 S2 本体 Goal 的 `goal:<id>:revision:..`）不加目标检查。
    - 任务快照的 budgets 含 `model`。
  - 两张表都是 tenant/world 隔离 + FORCE RLS，追加后不可改；快照缺失时拒绝派发（`control snapshot missing`）。
- **`effect_execution.prepare_effect_dispatch`**：在 **admit 的同一事务内** 先调用 `ControlPlane.assert_intent_dispatch`，后 admit。检查对 control head 加共享锁，与 owner 变更串行。被拒时沿用原有的 `EffectExecutionUnavailable` → `admission_unavailable`（L1 的诊断与重试语义未动）。
- **`runtime_dispatch.RuntimeDispatcher.run_once`**：在 `create_runtime_activation` 之前调用 `ControlPlane.prepare_run_dispatch`：
  1. 命令中的 NX-022 目标引用 `goal:<id>@<v>` 调用 `bind_run`，它幂等，已被取代的版本会被拒；
  2. 断言任务快照；
  3. 租户配置了 model 预算时，按 Run 命令声明的 `budget.maximum_cost / currency` 预留一次（consumption id 为 `run:<run_id>`）；未配置则不设门槛；
  4. 控制拒绝以 `RuntimeDispatchError(<reason>)` 结束任务（status=failed，code=`control_paused` / `goal_version_stale` / `control_revision_stale` / `budget_exhausted` 等），不会启动 Host。
- **Run 签发时绑定目标的位置**：Run 命令（含 `goal_version_ref`）首次被可信服务端处理的位置是 runtime dispatch。签发与入队在 L3 的 `runtime_activation.py`，按分工未改，所以 `bind_run` 放在派发激活之前执行，绑定的就是计划时的版本。
- **契约**：快照完全在服务端推导和存储，未改 `packages/contracts`。

### 端到端测试（`tests/test_nx022_dispatch_e2e.py`，8 passed）
| 场景 | 路径 | 结果 |
|---|---|---|
| 入队后暂停 consumer → 派发 | effect（独立 Worker 进程 + loopback provider） | `admission_unavailable`，provider 0 请求 |
| 恢复后旧意图再派发 | effect | 仍被拒（快照早于控制变更），0 请求：恢复不是回放 |
| 恢复后新 Run 重新提交同一业务意图（重评估）→ 派发 | effect | 正常派发，provider 恰好 1 次 POST |
| 目标改版（v1→v2）后派发旧计划 | effect | 被拒，`goal_version_stale` |
| 无关 consumer 被暂停 | effect | 正常派发 1 次 |
| 入队后暂停 role → 派发 | runtime（真实 TLS Host） | failed，`control_paused`，无 runtime 回执、无执行标记 |
| 目标改版后派发旧计划 | runtime | failed，`goal_version_stale`，无目标绑定 |
| 当前目标 | runtime | succeeded，Run 绑定到 `run-goal@1` |
| 模型预算 0.50 < Run 声明 1.0 | runtime | failed，`budget_exhausted`，无消耗记录 |
| 模型预算 5.00 | runtime | succeeded，记录一条 `run:<run_id>` 消耗 1.0 |

暂停/恢复、预算、目标改版这些 owner 变更，经由受治理 owner Action 使用的同一组内部状态函数写入（`control.nexloop_nx022_owner_change` / `_write_goal`）。仅限人类的授权链由 `tests/test_goal_controls.py` 覆盖。

### 回归
- 批次 1：94 passed、1 failed。失败的是 `test_effect_execution_sql::test_42_actual_bootstrap_and_publication_checksum`，它与 `test_bootstrap` 一样要求编号连续，而临时 0097 前有空号。本地临时改为连续的 0092（不提交）后，`test_bootstrap.py` + `test_effect_execution_sql.py` 9 passed。
- 批次 1 覆盖：nx022 e2e、goal_controls、effect_dispatch、runtime_dispatch、runtime_effect_bridge、effect_execution_sql、effect_intents、runtime_worker。
- 批次 2：69 passed。覆盖 offering_runtime_pg、role_policy_dispatch、role_policy_enforcement、runtime_effect_recovery、runtime_effect_tools、conversation_effect_receipts、local_json_delivery_pg、backend_lifecycle_capacity。

### 时延（13 例集合，本机串行，before = main 7b831ef 的派发代码且无 0097，交替各两轮）
| 项目 | before | after |
|---|---|---|
| 13 例结果 | 13/13；12/13（v4 失败） | 13/13；13/13 |
| v4[complete] | 通过 / **失败** | 通过 / 通过 |
| two_pi wall | 58.0 / 56.6 s | 56.8 / 61.0 s |
| authorize p50 / p95 / 每轮最慢 | 246 / 981 / 1235、1814 | 244 / 1016 / 1409、1513 |
| runtime_effect_tool p50 / p95 / 每轮最慢 | 503 / 1413 / 1511、2299 | 514 / 1503 / 1553、1641 |
| prepare_effect_dispatch p50 | 221 | 213 |
| 新增服务端开销（`track_functions`） | — | 提交时捕获 0.78 ms/次；任务入队 0.31 ms/次；effect admission 检查 1.9 ms/次；Run 派发检查 2.6 ms/次 |

- 2 s 门槛（NX-049）作用于 guard 的 authorize 与 effect_tool 请求：authorize 不经过新逻辑；effect_tool 只多一次提交时的快照触发器（约 0.8 ms）。两者的 p50 / p95 差异都在单轮噪声内，4 轮中 after 的最慢一次都低于 2 s，没有变差。
- before 第二轮中 v4 失败、effect_tool 达 2299 ms，那一轮期间 load avg 升到约 10，属于机器负载造成的边缘波动，与本改动无关。

### 验收结论（由调度员回填）
- **AT-005（目标不可变）：可回填 passed。** 依据 `tests/test_goal_controls.py`：
  - `test_at005_agent_cannot_change_parent_kr_metric_period_or_target`
  - `test_owner_versions_are_immutable_and_record_impact`
  - `test_owner_publish_requires_current_eios_grant_and_approved_metric`
- **AT-006（暂停队列）**：控制面与派发已落地并有端到端证据，见 `test_effect_paused_after_enqueue_is_denied_with_zero_effect_then_resume_needs_reevaluation` 和 `test_runtime_paused_after_enqueue_is_failed_before_host_start`。“UI 状态一致”部分归 **NX-028**。
- **AT-007（过期计划）**：目标改版或控制变更后，旧计划在派发时被拒，见 `test_effect_goal_revision_after_enqueue_denies_old_plan` 和 `test_runtime_goal_revision_after_enqueue_denies_old_plan_and_current_plan_runs`。客户对象版本过期的判定见 `test_goal_controls.py::test_at007_goal_revision_and_customer_change_invalidate_old_plan`。被拒后**生成重评估任务**归 **NX-024**。
- **AT-043（成本上限）**：tenant 模型预算在 Run 派发时预留，耗尽即拒绝且不写消耗，见 `test_runtime_model_budget_is_reserved_per_run_and_exhaustion_denies[0.50-budget_exhausted]`。预算判定和保留已有合法结果见 `test_goal_controls.py::test_at043_tenant_budget_blocks_new_consumption_and_keeps_existing`。
  - 尚未接入：按实际模型调用逐次扣减（需要模型网关回报实际费用），以及优惠类 Action 的 incentive 预算。
