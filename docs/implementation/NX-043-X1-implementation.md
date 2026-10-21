# NX-043 X1：策略版本与实验预登记

- **依据**：NX-043 设计稿 §3、§4、§10（D1–D9，负责人 2026-10-20 按推荐定案）；负责人 2026-10-21 定：NX-043 可以先做不依赖校准的切片（D9 局部提前），凡是选用孪生、进入线上验证的环节，仍然要等校准通过。
- **起点**：第八批预演头 `a3214ccd`，里面已有 NX-042 T1–T7。
- **迁移**：`0285_nx043_strategies_experiments.sql`。这是临时编号，以合入后的 catalog 为准。临时编号和前一个迁移之间空了号，所以 `test_bootstrap.py` 的两项计数断言（catalog 条数等于 revision 号）在重新编号之前会失败，迁移本身可以正常应用。
- **契约**：`packages/contracts/strategy-version.schema.json`（`nexloop-strategy-version/1`）、`packages/contracts/experiment-registration.schema.json`（`nexloop-experiment-registration/1`），以及示例和生成的 TS、OpenAPI、Pydantic。

## 1. 策略版本（D1）

- **表**：
  - `control.nexloop_strategy_versions`：一个策略版本一行，版本号由服务端分配，定义和 digest 冻结；
  - `control.nexloop_strategy_events`：状态 published / retired，作废要写原因。
- 两张表都只能追加：update、delete、truncate 一律以 `append-only` 拒绝；租户边界用 RLS，并有租户外键。
- **杠杆只有 D1 的三种**：

  | 杠杆 | 字段 | 检查 |
  |---|---|---|
  | 跟进时机 | `follow_up_delay_hours` | 1–336 小时 |
  | 人设版本选择 | `persona`（`persona_id`、`version`） | 该人设版本当前是已发布状态，否则 `persona_not_published` |
  | 非营销外联目的开关 | `outreach_purposes`（目的 → 开或关） | 必须在最新的目的清单里，并且不是营销类、不需要同意，否则 `purpose_not_allowed` |

- 其他任何杠杆（价格、优惠、优惠金额、营销同意、同意方式、真实派发等）一律以 `lever_not_allowed` 拒绝。描述文本过“像真实内容”的检查，与孪生用同一个函数。
- **Action**：
  - `nexloop.strategy.publish` / `publish_strategy`：同一请求重放得到同一版本，同一请求改了定义被拒（`request_reused`）；
  - `nexloop.strategy.retire` / `retire_strategy`：重复作废返回 `already_retired`，未知版本返回 `not_found`。

## 2. 实验预登记（D3、D4、D6）

- **表**：
  - `control.nexloop_experiments`：预登记冻结后的全文和 digest；
  - `control.nexloop_experiment_events`：状态 started、stopped，停止要写原因。没有事件即为 registered。
- 两张表同样只能追加。改实验只能另登记一个新实验，原 experiment_id 再登记返回 `experiment_exists`。
- **必填字段**：`stage`、`world`、`hypothesis`、`candidate`、`control`、`twin`、`primary_metric`、`guardrails`、`sample_size`、`maturity`、`budget`、`ends_at`、`allocation_seed`。
  - 少了任何一项都以 `registration_incomplete` 拒绝。
  - `control`、`twin` 也必须写出来，没有时写 null。
- **默认值写进冻结的登记**（`runtime.nexloop_experiment_defaults()`），填写的值只能更严：

  | 项 | 默认（定案） | 允许的范围 | 放宽时的拒绝码 |
  |---|---|---|---|
  | 拒绝联系率或退订率比对照组高（D4） | 0.02 | 大于 0，且不超过 0.02 | `guardrail_looser_than_default` |
  | 对账异常（D4） | 0（出现任何一次就停止） | 只能是 0 | 同上 |
  | 预算用尽即停（D4） | true | 只能是 true | 同上 |
  | 成熟窗口（D6） | 14 天 | 14–90 天 | `maturity_below_default` |
  | 每组最少满足成熟窗口的样本（D6） | 30 | 至少 30 | 同上 |

- **样本与预算**：
  - 候选组 1–20 人，否则 `candidate_group_too_large`（D2）；
  - 对照组不少于候选组，否则 `control_group_too_small`（D2，对照组至少占一半）；
  - 模型预算 0–100 元（D5）；
  - 优惠预算只能是 0，否则 `incentive_not_allowed`（D1、D5：首批不做优惠类实验）。
- **结束时间**：必须晚于现在，并且在 180 天以内，否则 `ends_at_out_of_range`。
- **阶段决定世界**：
  - screening 只在 `sim:` 世界，并且必须选用孪生，否则 `twin_required`；
  - shadow 只在 `shadow:` 世界；
  - online 只在 real；
  - 阶段和世界不一致时返回 `world_mismatch`。
- 登记时还检查：
  - 候选与对照策略都是已发布状态，否则 `strategy_not_published`；
  - 选用的孪生版本存在，否则 `twin_unknown`。
- 孪生是否已校准**在开始时检查**，因为登记可以早于校准。

## 3. 开始与停止（D3、D9）

- `nexloop.experiment.start` / `start_experiment`：只能从 registered 开始，否则 `not_registered_state`；停止后不能再开始。开始时把预登记重新核对一遍：
  - 已过结束时间：`ended`；
  - online 阶段：`online_not_available`。X1 还没有分配（X4）和停止规则（X5），线上验证要等这两个切片；
  - **选用的孪生必须当前可选用**（已发布、未作废、最近的校准结论为通过，即 `runtime.nexloop_consumer_twin_selectable`），否则 `twin_not_calibrated`。这是 D9 剩下的约束；登记之后校准被收回，也会被拒；
  - 候选与对照策略仍然是已发布状态，人设杠杆指向的人设版本仍然是已发布状态；
  - 模拟世界或影子世界仍然是 active 且没有到期，否则 `world_not_active`。
- `nexloop.experiment.stop` / `stop_experiment`：从 registered 或 started 停止，要写原因；重复停止返回 `already_stopped`。停止只追加一个事件，不发布任何东西，不撤回、不重发任何效应。
- 两个 Action 都按请求重放：同一请求再执行一次返回原结果（`started` 或 `stopped` 为 false）。

## 4. 不打开真实派发（设计 §4）

- 预登记里没有任何字段能指定开关；登记里只要出现 “real dispatch” 字样，就以 `real_dispatch_not_allowed` 拒绝，这一步在其他检查之前。
- 本切片的函数都不写 `control.nexloop_real_dispatch_switches`，测试会检查函数体，并核对登记、开始、停止前后开关表完全一致。

## 5. 权限与清单

- 五个 Action 都是 `human` 主体、负责人专属，属于工作台角色清单 v33 的 `experiments` 范围，并在负责人专属集合中。operator 或 reviewer 持有其中任何一个，角色清单都会被拒。
- **同步的清单**：
  - `business-actions.v1.json` v37、`capabilities.v1.json`（schema_hash 由 `write_hashes` 计算）、`system-catalog-names.v1.json`；
  - `business_actions.PROFILES`、`goal_controls`（映射和提交方法）、`workbench_actions_http.OPERATIONS`，以及拒绝码前缀 `strategy:`、`experiment:`；
  - 测试映射：`commercial_fixture`、`test_business_actions`、`test_governed_entry_pg`；
  - register 计数 94 → 99（`test_stage_register_pg`）。
- **角色清单的上限**：每个角色最多 64 个 Action 的检查改为 128。负责人角色加上 experiments 范围后有 65 个 Action。这个上限只是防误写的检查，与授权事实没有关系。
- 本切片没有重新定义已有函数，所以复制件守护不需要登记。

## 6. 测试

`tests/test_nx043_experiments_pg.py`（10 项）：

| 测试 | 覆盖 |
|---|---|
| `test_the_contract_examples_are_valid_and_sql_accepts_them` | 契约示例有效，SQL 接受 |
| `test_strategy_versions_are_frozen_and_retired_by_an_appended_event` | 版本冻结、重放、作废事件、只能在 real 发起 |
| `test_only_the_low_risk_levers_of_d1_are_accepted` | 禁用杠杆被拒；人设与目的检查 |
| `test_a_registration_with_a_missing_field_is_refused` | 预登记缺任何字段都被拒（SQL 和契约都拒） |
| `test_the_defaults_of_d4_and_d6_are_written_in_and_can_only_be_tightened` | 默认值写入、放宽被拒；样本、预算、阶段与世界的检查 |
| `test_a_registration_is_frozen_and_a_changed_experiment_is_a_new_one` | 登记后修改被拒（SQL 触发器与 Action 两条路径） |
| `test_an_uncalibrated_twin_cannot_be_started_d9` | 未校准或被收回校准的孪生不能开始 |
| `test_start_and_stop_append_events_and_recheck_the_registration` | 状态机、重放、策略作废后不能开始、online 暂不开放 |
| `test_an_experiment_never_opens_real_dispatch` | 开关表前后不变；提到真实派发的登记被拒 |
| `test_the_five_actions_are_owner_only_governed_human_actions` | 负责人专属、角色清单伪造被拒、租户边界 |

## 7. 未完成项

1. X2：在模拟世界中做筛选。X3：影子世界。X4：线上分配。X5：停止规则与预算阻断。X6：结果统计。X7：验收。
2. online 阶段在 X4、X5 之前不能开始。
3. `primary_metric.key` 目前只检查格式，与已登记指标的对应由 X6 接上。
4. 负责人用的命令行工具（配置文件加一条命令）还没有实现，目前只有工作台写入口。
