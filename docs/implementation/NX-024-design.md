# NX-024 策略 / 计划 / 复评调度：设计稿（待调度员审核，未实现）

分支 `nx046-close`（基于 main `50469bf`）。只有文档，没有代码和迁移。

依据：
- PRD M15“运营策略、计划与重规划”、M16“事件、任务与唤醒”；docs/05 §5–§8；docs/06。
- NX-022 文档（控制 revision、`goal_version_stale` 等拒绝码、0097 派发检查）。
- NX-023 文档（v6 上下文包、策略、目标与控制快照段）。
- 0069 / 0093 的 work feed 模式。

任务交付物（planning）：`no_action`、等待重评估、计划失效、有限 Run 预算。
相关验收：
- AT-029：当前无合理行动时正常输出 no_action；
- AT-007 的“重新评估”部分：判定已在 NX-022 完成，生成复评任务属 NX-024；
- M16 的不变量：同一事件不能无限触发自身，租约和栅栏有效。
- AT-041（承诺到期触发复评）的触发入口预留，承诺对象本身属 M18。

## 1. 现状（已核对代码）

| 已有 | 位置 | 对本任务的意义 |
|---|---|---|
| `PlanStep` 是受治理对象，字段为 consumer_id、goal_id、control_id、submitter_principals、action_name、state；由 relay 按消息创建 | `effect_contexts.py`、`message_relay.py`、0063 | 已有“步骤”实体，但缺 prerequisites、expected_result、stop_if、reassess_at、budget、intent_ref；也没有 Plan（版本链）和 Strategy |
| Role 激活链（0063/0075/0085）只读 PlanStep 的 consumer_id、state、submitter_principals | 0063 | **不改 PlanStep Schema**，规划元数据另建表，避免牵动 Role 激活与 v3/v5/v6 冻结核心 |
| NX-022 控制面：`control_heads.revision`、control_events；`authz.nexloop_control_snapshot`；拒绝码 control_paused / goal_version_stale / object_revision_stale / control_revision_stale / budget_exhausted | 0066/0068/0097、`goal_controls.py` | 复评的“失效判定”直接复用，不另造规则 |
| 派发拒绝点 | — | 恢复不是回放：旧任务、旧意图被拒后必须新建评估，这正是 NX-024 的入口 |
| ↳ 运行任务 | `runtime_dispatch.run_once` | 以 `RuntimeDispatchError(code)` 结束任务，状态 failed、code 为控制拒绝码 |
| ↳ 外发 | `effect_execution.prepare_effect_dispatch` | admit 前同事务断言，被拒时为 `admission_unavailable`，**不留可查询的拒绝记录** |
| Run 命令预算 | `runtime_activation._command` | 字段 maximum_model_turns（≤64）、maximum_tool_calls（≤128）、active_timeout_seconds（≤3600）、maximum_cost/currency；docs/05 §6 的初值是 8/12/300s |
| v6 包 | NX-023 | `strategy_ref`、`goal.control_snapshot`，`insufficient` 含 goal_not_current、control_paused、required_source_stale |
| work feed（0093） | `runtime.nexloop_work_feed` | 同事务登记、change_seq 栅栏租约、退避、死信、backlog；`available_at` 可直接表示 due_at |
| Run 输出 no_action 等 | 无契约，Host 也没有该输出 | 需要新增（见 §5、§8） |

## 2. 概念与边界
- **Strategy（运营策略）**：Agent 针对一个目标版本产出的阶段策略说明，带版本，可被 supersede。它与 Context Strategy（NX-023 的证据选择策略）是两回事；后者只作为 Run 的 `context_strategy` 引用。
- **Plan**：针对一个 (consumer, goal_version) 的短期行动计划，带版本，新版本 supersede 旧版本；旧版本的未执行步骤不再执行。
- **PlanStep 元数据**：沿用已有 PlanStep 对象 id，另存 prerequisites、expected_result、stop_if、reassess_at、budget、intent_ref。
- **Reevaluation（复评）**：一次有界评估，先做确定性预检，必要时再起一个有预算的 Run。
- **Run 结果**：no_action、needs_information、waiting_external、escalate、plan_update、action_intent。前四种是正常结果，不算失败。

这些都是执行与规划状态，不是业务事实：存于 `runtime` schema，不经 ontology-mutation；正式业务写入仍只走受治理 Action。

## 3. 触发源（同事务登记到 feed `plan-reevaluate`）

| # | 触发 | 登记位置（SQL，同事务） | 影响的 Plan |
|---|---|---|---|
| T1 | 目标改版 → `goal_version_stale` | 0066 goal_versions 插入新的 published 版本（旧版本 superseded）时触发 | goal_version_ref 指向旧版本或其后代的活动 Plan |
| T2 | 暂停恢复（resume）后旧意图或旧任务被拒 | control_events 中 scope 恢复事件触发：按 scope（tenant/role/consumer/strategy/action_type）匹配活动 Plan；另外 `runtime.jobs` 转为 failed 且 code ∈ 控制拒绝码时触发（与 finish 同事务） | 快照涉及该 scope 的 Plan；被拒任务所属 Plan |
| T3 | 控制 revision 推进（预算调整、目标链事件等） | control_events 插入时触发，与 0097 的 `control_revision_stale` 判定同口径 | 快照依赖该事件的 Plan，合并为一行 |
| T4 | 策略变更 | 运营 Strategy 新版本；ContextStrategy 新版本（0088） | 引用旧策略版本的活动 Plan |
| T5 | reassess_at 到期 | PlanStep 元数据写入或更新 reassess_at 时，登记 `available_at = reassess_at`（UTC due_at） | 该 Plan |
| T6 | 外部结果到达（waiting_external） | effect receipt 观察或对账写入时触发 | intent_ref 指向该意图的 Plan |
| T7 | 承诺到期（AT-041，预留） | M18 承诺表到期，接口预留，本任务不实现 | — |

规则：
- 登记全部在 SQL 触发器或定义器内完成，与引起变化的业务写入同一事务（先持久化后 ACK）。不依赖 Python 调用方，也不改 L4 文件（见 §9）。
- feed 键为 `plan:<plan_id>`。一个 Plan 在任意时刻至多有一行待办，多次触发合并：change_seq 递增，payload 累积触发原因，最多保留 16 条。
- **防自触发（M16）**：复评 Run 自身的写入（新的 Plan 版本、步骤元数据）产生的触发会带 `cause=self`。同一 Plan 在 `self_window`（默认 10 分钟）内的 self 触发超过 2 次，就推迟到 window 结束，并在 backlog 中标出 `self_trigger_throttled`。T5 的到期时间不得早于 now + 最小间隔（默认 60 秒）。

## 4. 入队与执行（复用 0093 work feed）
- 迁移把 `runtime.nexloop_work_feed.feed` 的检查约束扩为包含 `plan-reevaluate`。新增 `authz.nexloop_work_feed_touch_due(…, available_at)`：to-do 已存在时取 min(已有, 新)。
- 服务主体 `plan_reevaluator`（nexloop_scheduler 角色）只持有 `eios:action:NexLoop.feed.plan-reevaluate:1` 和“发起复评 Run”的授权；授权条目写进 service-grants 新版本。
- `PlanReevaluationWorker.run_once()`（新模块 `plan_reevaluation.py`）：
  1. claim 一批到期条目（租约，栅栏为 change_seq）。
  2. **确定性预检（不调模型）**：用新的 `control_snapshot` 加上 Plan 存储的快照，按 NX-022 的判定得到：
     - Plan 已被 supersede，或目标已关闭 → complete，不做任何事；
     - 控制暂停 → `waiting_external`，reassess_at = 恢复后（由 T2 唤醒），不起 Run；
     - 目标改版 → Plan 标记 `stale`，原因 goal_version_stale，需要重规划 → 进入第 3 步；
     - 客户对象 revision 变化或 stop_if 命中（例如已续费、问题已解决）→ Plan 标记 `invalidated`，未执行步骤取消；需要时进入第 3 步重规划，否则输出 `no_action`。
  3. 需要模型时，经现有 Run 入口 `accept_runtime_event` 入队一个 Role 或消息 Run。命令中的 goal_version_ref 取当前版本，`context_strategy` 取 Plan 的策略，预算取步骤 budget 与 docs/05 初值（8 轮模型、12 次工具、300 秒）中较小者，并受 NX-022 model 预算约束。
  4. 入队后 complete 该 feed 条目。Run 的结果经 §5 回写；若派发时被控制拒绝（快照又过期），T2 会再次登记，而且受防自触发限制。
- 失败退避：provider 或运行时不可用时 retry；授权缺失时 retry 直至死信。backlog 可观测。

## 5. Run 结果与计划版本
- 新增 SQL 定义器 `authz.nexloop_record_plan_outcome`。只接受**当前活动 Plan 版本**的复评 Run，Run 身份从激活记录推导；记录 no_action、needs_information、waiting_external（带 reassess_at）、escalate（进入负责人可见列表，NX-028 展示）、plan_update（写新版本并 supersede 旧版本，旧版本未执行步骤取消）。
- action_intent 仍走既有 `submit_effect_intent`。intent 的 `plan_step_ref` 指向步骤，派发前仍做 NX-022 检查。
- 一个 Run 只能记一次结果（幂等键为 run_id）。结果写入会触发 T5/T6 的登记。
- 契约提案（不改 packages/contracts，先提议）：
  - 新增 `run-outcome.schema.json`：kind ∈ 上述 6 种，以及 reasons、reassess_at、evidence_refs、plan_version_ref；
  - `action-intent.plan_step_ref` 的取值规则不变。

## 6. 与 v6 上下文策略、control_snapshot 的关系
- 复评 Run 的上下文必须用 v6：`strategy_ref` 来自 Plan；`goal.control_snapshot` 是入队时由 SQL 推导的快照（与 0097 的任务快照相同，不接受调用方提供）。
- 预检与 v6 的 `insufficient` 分工：goal_not_current、control_paused 在预检阶段就终止，不起 Run，避免浪费模型调用；required_source_stale 交给 Run 处理，Run 应输出 needs_information 或 no_action。
- 派发期间快照过期由 0097 拒绝，任务失败后进入 T2（恢复不是回放）。
- 策略版本变化（T4）不改付款事实和授权，只影响证据选择与计划。

## 7. 迁移形状（临时编号，实施时从 main 当前最大号之后起）
1. `runtime.nexloop_plans`：
   - 列：plan_id、tenant、world、consumer_id、goal_version_ref、strategy_ref、context_strategy_ref、version、status ∈ (active, superseded, stale, invalidated, closed)、supersedes、control_snapshot（服务端推导）、created_by_run、created_at；
   - 约束：唯一 (tenant, world, consumer_id, goal_version_ref) 且 status=active 的版本至多一个；只能向前转移。
2. `runtime.nexloop_plan_steps`：plan_id、plan_version、step_object_id（PlanStep 对象）、prerequisites、expected_result、stop_if、reassess_at、budget、intent_ref、state。
3. `runtime.nexloop_plan_outcomes`：run_id（主键）、plan_id、plan_version、kind、reasons、reassess_at、evidence_refs、recorded_at；只追加。
4. `runtime.nexloop_strategies`：带版本，可 supersede；内容为 Agent 产出的文本及引用。
5. work feed 约束扩展、`work_feed_touch_due`；T1–T6 触发器（各自只调用 touch，不做业务判断）。
6. 定义器：`authz.nexloop_record_plan_outcome`、`authz.nexloop_plan_reevaluation_feed`（复用 `authz.nexloop_work_feed`，按 feed 名授权）、`authz.nexloop_plan_precheck`（只读，返回预检结论）。
7. 全部表 FORCE RLS（tenant_boundary）；新函数的 search_path 为 `pg_catalog, pg_temp`；SECURITY DEFINER 函数一律设置 search_path。

## 8. 测试清单（实施时的 PG 测试，合成数据）
- AT-029：无合理行动时 Run 记 no_action，计划保持 active，无意图、无消息；KR 未完成也不硬发消息。
- AT-007（重评估部分）：目标改版、控制推进、客户状态变化后，旧计划不执行；feed 出现复评；新 Run 使用新版本和新快照；旧意图仍被拒（恢复≠回放）。
- 客户已续费或问题已解决（stop_if 命中），旧计划 invalidated，未执行步骤取消，预检不调模型（PRD M15 验收）。
- reassess_at 到期唤醒；进程重启或 Valkey 清空后到期任务不丢（M16，PG 是权威）。
- 暂停期间到期：预检输出 waiting_external，不起 Run；恢复（T2）后才评估。
- 防自触发：复评 Run 连续改计划，self_window 内超过上限就被推迟，backlog 可见。
- 租约栅栏：旧 worker 过期后提交被拒；合并触发时 change_seq 防止丢失后到的触发。
- 预算：Run 命令预算不超过步骤 budget 与初值；NX-022 model 预算不足时 budget_exhausted，不起 Run。
- 结果定义器：非当前版本 Plan 的 Run、重复结果、跨租户、跨 world、Agent 或人类伪造的调用均被拒；结果只记一次。
- 契约：run-outcome 合法与非法样例（提案通过后）。

## 9. 与 L4 正在修改的文件的重叠

| 文件 | 本设计是否需要改 | 说明 / 规避 |
|---|---|---|
| `runtime_activation.py` | **可能**：Run 结果的回写入口 | 方案 A：Host 经现有 `authorize_runtime_activation(operation=…)` 增加一个 `plan_outcome` 操作，需改此文件和 Host guard。方案 B（建议）：不改该文件，结果由新模块经独立 SQL 定义器回写，Run 身份由激活记录在 SQL 中推导，Host 侧只加一个工具调用。B 仍要改 Host，`apps/agent-host` 不在 L4 列表中，需确认 |
| `effect_execution.py` | **希望不改** | 外发被拒目前不留记录。T2 改用 SQL：在 0097 的意图快照表上，用“意图最近一次派发判定”视图或在 admit 失败后由 effect worker 调查询；最小方案是在 effect worker（`effect_worker.py`，不在 L4 列表）里拒绝后调用只读的 `nexloop_intent_dispatch_status` 再登记。若必须动 `prepare_effect_dispatch`，排在 L4 合入之后 |
| `backend.py` | **很可能**：AuthenticatedServices 门面新增 `plan_reevaluation_*`、`record_plan_outcome` | 只做纯追加，不改既有方法；合并顺序放在 L4 之后，冲突面只在文件末尾 |
| `runtime_dispatch.py` | 不改 | T2 由 `runtime.jobs` 的状态触发器登记 |
| `goal_controls.py` | 只读调用 | 复用 snapshot 与断言 |
| 新增文件 | `plan_reevaluation.py`、迁移、测试、service-grants 新版本、background_services 新入口 `nexloop-plan-reevaluator` | 与他线无重叠 |

## 10. 待调度员 / 负责人决定
1. Strategy、Plan、PlanStep 元数据放在 `runtime`（执行状态，本设计建议），还是做成受治理的 ontology 对象（需要 Schema 与 Action，更重）？
2. Run 结果回写选方案 A 还是 B（§9），以及是否接受新增 `run-outcome` 契约。
3. 防自触发参数初值（self_window 10 分钟、上限 2 次、最小间隔 60 秒）和复评 Run 的默认预算（8/12/300s）。
4. T6（外部结果）和 T7（承诺到期）是否纳入本任务。建议 T6 纳入，T7 留给 M18。
5. 实施顺序：等 L4 的 runtime_activation、effect_execution、backend 改动合入后，从新 main 开工。
