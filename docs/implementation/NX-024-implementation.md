# NX-024 策略 / 计划 / 复评调度：实现说明

设计稿见 `NX-024-design.md`；本文件记录按调度员五点裁定落地的实现、测试与限制。迁移号 `0106` 为临时编号，合并时由调度员统一重编号。

## 裁定落地

1. **存放位置**：Plan、Strategy、步骤元数据和 Run 结果放在 runtime schema 的新表中，不属于本体：
   - 表：`runtime.nexloop_plans`、`nexloop_plan_events`、`nexloop_plan_steps`、`nexloop_strategies`、`nexloop_plan_runs`、`nexloop_plan_outcomes`。
   - 全部只追加（append-only trigger），启用 FORCE RLS（tenant_boundary），并撤销应用角色的直接表权限。
   - 写入只走两个签名端口：
     - `authz.nexloop_plan_command`：协议 `nexloop-plan-command-v1`，资源 `eios:action:nexloop.plan.reevaluate:1`。
     - `authz.nexloop_record_plan_outcome`：协议 `nexloop-plan-outcome-v1`，资源 `eios:action:nexloop.plan.outcome:1`。
   - NX-028 的可见性留待以后做成只读投影。
2. **方案 B（新增 run-outcome 契约，经 Host 回写）**：
   - 契约：新增 `packages/contracts/run-outcome.schema.json`（1.0），并重新生成 TS、OpenAPI 和 Python 的 `RunOutcome`。
   - Host 侧：
     - 新增工具 `nexloop.plan.outcome`，由配置 `plan_outcome_tool` 开启，且要求 `effect_tools` 同时开启。
     - 客户端 `recordOutcome` 调用 `/internal/v1/runtime/outcomes/record`。
     - 固定错误码：`plan_version_not_current`、`invalid_run_outcome`、`run_outcome_unavailable`。
   - Python 侧：
     - guard 路由调用 `AuthenticatedServices.record_plan_outcome`，该方法走 `_invoke`，因此处于请求级连接作用域内。
     - 先用 `operation='tool'` 重新授权活动中的 activation，再调用签名端口。
   - SQL 侧再次校验以下几点，任一不满足即拒绝：
     - activation 仍然存在；
     - 调用者持有该任务租约，且 fence 一致；
     - command digest 与 binding 一致；
     - Run 已关联到当前计划版本；
     - 结果形状严格匹配；
     - `intent_ref` 由本 Run 提交。
   - `runtime_activation.py` 未改动。
3. **可配置**：自触发窗口与上限（600 s / 2 次）、最小复评间隔 60 s、复评 Run 默认预算（8 轮模型 / 12 次工具调用 / 300 s）等全部写在 `deploy/configuration/plan-reevaluation.v1.json`。
   - Python 侧用 `load_settings` 严格校验该配置。
   - 每份计划在建立时冻结一份 `settings`。
   - Run 的实际预算取两者中较小者：配置上限，以及各步骤预算的最大值。
4. **触发源**：
   - 控制事件：暂停、恢复、目标版本过期、控制修订（T1 / T3）。
   - 派发被控制规则拒绝（T2）。
   - 策略或上下文策略的新版本（T4）。
   - 计划自身设定的 `reassess_at`（T5）。
   - 外部结果到达（T6，`runtime.nexloop_effect_observations`），本期纳入范围。
   - 承诺到期（T7）只提供接口 `runtime.nexloop_plan_commitment_due`，未授予任何调用权限，留给 M18。
5. 实现从 `35c790e` 开始，即 nx049-roundtrips 合并之后。

## 调度

- 复用 0093 work feed，新增 feed `plan-reevaluate`：
  - 每份计划一个 item，`available_at` 即到期时间；
  - 多次触发时保留最早的到期时间，并累积最近 16 条触发记录。
- `PlanReevaluationWorker.run_once()` 只做一轮，不常驻循环。流程如下：
  1. 领取到期 item；
  2. 执行 `precheck`，按结果分别处理：
     - `closed` / `paused`：完成该 item；
     - `invalidated`：标记计划失效；
     - `throttled`：自触发超限，按 `retry_after` 延迟后重试；
     - `reevaluate`：签发一个受预算约束的 Run，`link_run`，再用 `context_strategy=recent_plus_required` 激活。
- `RoleRunLauncher` 负责 Run 的签发与激活：
  - 签发：通过 source 调用 `issue_run_credential`；
  - 激活：Planner 调用 `bind_effect_context`，再调用 `activate_role_plan`。
- 新函数的 `search_path` 一律为 `pg_catalog, pg_temp`。`scripts/check_definer_search_path.py` 报告 0 个问题。

## 测试（合成数据，干净的 catalog PostgreSQL）

`tests/test_plan_reevaluation_pg.py` 覆盖：
- 建立计划后派生控制快照，并登记到期时间；
- 恢复后不是重放，而是重新复评；
- 目标出新版本后计划过期；
- `stop_if` 使计划失效，且不启动 Run；
- 自触发限流；
- 策略变化和派发被拒触发复评（非控制类错误码不触发）；
- 端口拒绝与表权限。

`tests/test_plan_outcome_pg.py` 使用真实的受治理 Run（经 Source、Planner、accept、claim、activation 和 start 授权），覆盖：
- 正常结果的记录、重放和冲突；
- `plan_update` 追加 v2，之后关联到 v1 的 Run 无法写入；
- `action_intent` 必须由本 Run 提交；
- 各类身份错误都不写入，包括：错误的 activation、改动过的 command、未关联计划的 Run、没有租约的 worker、没有 outcome 授权的 worker、结果形状错误；
- `link_run` 要求 Run 凭证真实存在；
- 外部结果真实经过执行器派发、查询并落库观测后，计划进入 feed（T6）。

`apps/agent-host/test/runtime-plan-outcome.test.ts` 覆盖传输、9 种非法形状和固定错误码。

## 限制

- 生产环境 Role Run 的签发目前只在测试 fixture 里有策略绑定。因此 `RoleRunLauncher` 留了 `prepare_run` 钩子；端到端的 Role 激活路径还没有专门的 PG 测试，复评 worker 的测试用的是 FakeLauncher。
- `background_services` 尚未登记 `plan-reevaluator` 条目，部署时需要单独接入。
- Host 的 `dist/` 是被忽略的构建产物；改动 TS 后需要先 `pnpm run build`，读取 `dist` 的 vitest 才会用到新代码。
- 承诺到期（T7）只有接口，没有调用方和授权。
