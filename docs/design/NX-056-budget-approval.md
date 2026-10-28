# 预算调整纳入 NX-056 审批：评估与设计（第 2 版）

- **状态**：已定（负责人 2026-10-28，Q1–Q8 均按推荐）。实现见同一分支的后续提交。
- **第 2 版**：按 L6 I1 修正期间的方向判定（2.2）与 Q4 的续期条件，并在测试清单里补上对应的两种情况。
- **起因**：L6 复核 L2 的 NX-038 G1（工作台路由 `set_budget`，分支 `l2/nx038-stage-runs` 1e56c3fa）时指出：model、incentive、evolution 三种预算，现在一位 owner 经 HTTP 就能调高，不经过第二个人。上 real 之前要决定是否纳入 NX-056 的审批策略。
- **按 main 17aa74c1 核对**。G1 在 1e56c3fa 上，尚未进这个 main；第 1 节按“G1 已合入”写。
- **依据**：`docs/design/unified-approvals.md`（第 3 节“条件只支持单字段等值”、第 10 节“`goals.budget.set` 移出首批，以后拆出 `goals.budget.raise`”、第 15 节单人模式）、`docs/design/nx058-owner-actions.md`（拆分 Action、执行时重查当前状态的做法）、`deploy/configuration/approval-policies.v1.json` version 4、迁移 0068（`control.nexloop_nx022_owner_change` 的 `set_budget` 分支）与 0235（`evolution` 类型）。

## 1. 现在能调预算的入口

所有人工入口最后都落到同一个受治理 Action `Budget.set:1`（能力 `goals.budget.set`，owner 角色、owner-only 集合），由 `control.nexloop_nx022_owner_change` 的 `set_budget` 分支写 `control.nexloop_budget_limits`（每个 `(tenant, world, budget_kind)` 一行，upsert，revision 递增）。

| # | 入口 | 谁 | 走到哪里 | 现在是否经审批 |
|---|---|---|---|---|
| E1 | HTTP：`POST /api/v1/workbench/actions/set_budget`（G1，`workbench_actions_http.OPERATIONS['set_budget']`，`budget_kind` ∈ model/incentive/evolution） | 任一 active owner 的工作台会话 | `Budget.set:1` | 否 |
| E2 | CLI：`scripts/stage/approval_drill.py request --operation set_budget`（NX-038 运行单 §2.0 用它，由 approver3 提交） | 同 E1（它就是 E1 的一个客户端） | E1 | 否 |
| E3 | CLI：`scripts/nx037/real_tenant_init.py`（第 4 步 `Budget.set`，D-g 的 model 累计上限；`budget_kind` 写死为 `model`） | 负责人本人的工作台会话（工具代为登录） | `GoalGovernedActions.set_budget` → `Budget.set:1` | 否 |
| E4 | Python：`nexloop_eios.goal_controls.GoalGovernedActions.set_budget` | 任何持有 owner 会话的调用方（E3 用它） | `Budget.set:1` | 否 |
| E5 | 系统：模拟世界建立时的触发器 `nx041_sim_world_model_budget`（0264）和一次性回填 0271 | 服务端，没有人 | 直接写 `nexloop_budget_limits`，只写 simulation/shadow 世界的 model 预算，`on conflict do nothing` | 不适用 |

- 恢复（0147）只会经 `owner_change` 写 `set_control`（暂停），不写预算，不是入口。
- 消耗（`nexloop_budget_consumption`）和调高无关，不在本稿范围。`incentive.issue` 按预算发放激励，预算本身没变，也不在范围。
- 前端（`apps/web`）没有设预算的表单；E1 只有 API 客户端在用。

## 2. 建议：按方向拆成两个 Action

### 2.1 为什么要拆

- 策略条件只能写“请求体某字段等于常量”（unified-approvals §3、§394），不能写“新上限大于当前上限”。
- 所以和 NX-058 一样拆 Action：**收紧**留在 `Budget.set`，不需要审批；**放宽**改走新 Action `Budget.loosen`，挂审批策略。是收紧还是放宽，由服务端在执行时对照当前行判定，不信任请求体。

### 2.2 方向的定义（服务端判定）

设当前行为 `cur`（可能不存在），请求为 `req`，同一 `(tenant, world, budget_kind)`：

| 情况 | 判定 | 走哪个 Action |
|---|---|---|
| 没有当前行（新设） | 放宽：从“未配置，什么都不能花”变成可以花 | `Budget.loosen` |
| `req.limit_amount > cur.limit_amount` | 放宽 | `Budget.loosen` |
| `req.period_start` 与 `cur.period_start` 不同（往前或往后都算） | 放宽：0068 只统计 `recorded_at` 落在 `[period_start, period_end)` 内的消耗，起点后移会把已经发生的消耗排除在外，相当于把已用额度清零；起点前移会扩大可用时间 | `Budget.loosen`（例外见 Q4 的续期） |
| `req.period_end > cur.period_end` | 放宽 | `Budget.loosen` |
| 改 `unit` | 放宽（无法比较大小；有消耗时 0068 本来就拒绝） | `Budget.loosen` |
| 以上都不是：上限不变或调低，`period_start` 不变，`period_end` 不变或提前，单位不变 | 收紧（包括“值完全相同”） | `Budget.set`，不经审批 |

- 收紧必须能立即生效，与 `real_dispatch.set` 关闭时不拦是同一个道理。调低到已消耗额以下也允许，0068 写明“不删除、不改写已记录的合法消耗”，之后的消耗会以 `budget_exhausted` 被拒。
- 一个请求只要有一项是放宽，整个请求就是放宽。
- 期间只有一种收紧：`period_end` 提前（起点不变）。起点的任何变化都按放宽处理，原因见上表（L6 I1 (a)）。

### 2.3 两个 Action 的行为

**`Budget.set:1`（收紧，原 Action，同一能力 `goals.budget.set`）**

- 处理函数先按 2.2 判方向：是放宽就拒绝，返回固定原因码 `budget_loosen_requires_approval`（409 `not_allowed_in_state`，与 NX-056 的原因码一致），请求体不写。
- 版本仍是 1：请求体字段不变，只多了一条拒绝路径。能力注册表与工作台路由不用改。
- E3 的“新设 model 预算”会因此被拒，改走 `Budget.loosen`（第 4 节）。

**`Budget.loosen:1`（放宽，新 Action，能力 `goals.budget.loosen`）**

- 请求体：`budget_kind`、`unit`、`limit_amount`、`period_start`、`period_end`（与 `set_budget` 相同），另加
  - `from_limit_amount`、`from_period_start`、`from_period_end`、`from_unit`：请求人看到的当前值，新设时都为 `null`；
  - `expected_revision`：当前行的 revision，新设时为 0。
- **发起时**和**执行时**，服务端都核对 `from_*` 和 `expected_revision` 与当前行一致：
  - 发起时不一致就拒绝，返回 `budget_changed`；
  - 执行时不一致，审批记为已消费，执行结果为 `execution_refused:budget_changed`（与 unified-approvals §4 第 3 步“处理函数拒绝时仍算已消费”一致），请求人只能重新发起。
- 这样批准人看到的“从多少调到多少”就是执行时真正的变化，不会出现“批准时是 50→60，执行时已经被别人调成 10，结果变成 10→60”。
- 请求如果按 2.2 其实是收紧，也照样执行。不在这里拒绝收紧，免得边界上来回拒绝；只是没有必要走这条路。
- **失败即关**（同 NX-058 的做法）：处理函数在执行前核对 `goals.budget.loosen` 当前确实有一条形态正确的审批策略（quorum ≥ 1，批准角色 `{owner}`，没有 condition）。没有就拒绝（`approval_unavailable`）。所以在策略文件应用之前，任何放宽都做不了，而不是绕过审批直接生效。
- world：只在 real 世界经审批。模拟世界的 model 预算由 E5 的触发器建立，不经人工入口。工作台路由只对 real 世界开放，与现状相同。

### 2.4 审批策略（建议）

| 能力 | 条件 | quorum | 批准角色 | 期限 | 单人模式 | 摘要字段 |
|---|---|---|---|---|---|---|
| `goals.budget.loosen` | 无 | 1 | owner | 86400 秒 | `allowed` | `budget_kind`、`unit`、`from_limit_amount`、`limit_amount`、`from_period_start`、`from_period_end`、`period_start`、`period_end` |

- 批准人不能是请求人（按 subject 比较），这是 NX-056 的通用规则。
- **单人模式 `allowed`**：与 NX-058 的 owner 成员 Action（`not_applicable`）不同。只有一个人的公司也必须能设预算，否则上线第一天就无法设 D-g 上限；单人模式本身要求过去 24 小时内只有一人有审批资格，每次自批都有审计和告警（第 15 节）。
- 摘要字段都是金额、币种、时间和类型，没有顾客标识，也没有自由文本，符合 v1 的摘要规则。

## 3. 与审批策略 v4 的衔接

- **策略文件**：`approval-policies.v1.json` 的 `version` 4 → **5**，`expected_revision` 3 → 4；`policies` 追加一条 `goals.budget.loosen` version 1、`action_version` 1；`decision` 追加 v5 一段。策略只追加，前四版不变。
- **Action 和能力**：
  - `capabilities.v1.json` 新增能力 `goals.budget.loosen`（governed_operation `loosen_budget`，human only）；
  - `system-actions.v1.json`、`system-catalog-names.v1.json` 新增 `Budget.loosen`；
  - `workbench-roles.v1.json` 的 owner 角色加 `eios:action:Budget.loosen:1`，放进 owner-only 集合，manifest 版本加 1；
  - `api-routes.v1.json` 的 `operations` 加 `loosen_budget`；
  - `workbench_actions_http.OPERATIONS` 加 `loosen_budget` 的字段表（含 `from_*`、`expected_revision`）。
- **部署顺序**（与 v2–v4 相同，“先发布 Action，再应用策略文件”）：
  1. 迁移（`Budget.set` 变为只收紧，`Budget.loosen` 的处理函数）；
  2. `stage_secrets publish` 发布 `Budget.loosen:1`；
  3. 应用角色清单；
  4. 应用策略 v5。
  - 第 1 步到第 4 步之间，放宽一律被拒（`budget_loosen_requires_approval`，或 `approval_unavailable`），收紧不受影响。stage 是开发/测试环境，可以接受。real 上线前应一次做完。
- **新 Action 版本没有策略就拒绝**（v1 的规则）对 `Budget.set` 没有影响：它不挂策略。

## 4. 对各入口的影响

| 入口 | 改动 |
|---|---|
| E1 HTTP | 保留 `set_budget`（收紧）；新增 `loosen_budget`，返回 `waiting_approval` 和 `approval_id`。之后批准人在“审批”页批准，请求人在“待我执行”里执行（NX-056 工作台已有） |
| E2 approval_drill | 已经支持 `request`、`decide`、`execute`。NX-038/NX-039 运行单 §2.0 改为三步：approver3 发起 `loosen_budget`，approver2 批准，approver3 执行。运行单要相应更新 |
| E3 real_tenant_init | 第 4 步改为 `Budget.loosen`（新设，`from_*` 为 null，`expected_revision` 0），输出 `approval_id`。由第二位 owner 批准，或在单人模式下由负责人自批，然后执行。工具加一个 `execute` 子步骤，或者在说明里指向工作台的“待我执行” |
| E4 GoalGovernedActions | 新增 `loosen_budget(...)`；`set_budget` 保留 |
| E5 模拟世界触发器 | 不变 |

## 5. 迁移与测试清单

### 5.1 迁移（一个新迁移，临时编号）

1. 重定义 `control.nexloop_nx022_owner_change`：
   - `set_budget` 分支先判方向，放宽就以 `budget_loosen_requires_approval` 拒绝；
   - 新增 `loosen_budget` 分支：核对 `from_*` 和 `expected_revision`，核对策略形态（失败即关），然后写与原来相同的 upsert。
   - 这个函数已发布，按“已发布迁移冻结”的规则在新迁移里重定义，并在 `tests/test_function_copy_guard.py` 登记 `Allowed(...)`。
2. 在受治理入口的操作白名单里登记 `loosen_budget` → `goals.budget.loosen`（0124 的入口按能力路由）。
3. 不改 `nexloop_budget_limits` 的表结构。
4. 重算 catalog 的 sha，更新 `versions.lock.json` 的 `bootstrap_revision`。

### 5.2 测试

| 测试 | 断言 |
|---|---|
| 方向判定（PG） | 新设、调高、延长期间、提前起点、**后移起点**、改单位：`Budget.set` 都以 `budget_loosen_requires_approval` 拒绝，库里不变；调低、`period_end` 提前、值相同：直接生效，不产生审批 |
| 起点后移不能清零（PG） | 期间内已有消耗时，用 `Budget.set` 把 `period_start` 往后移：以 `budget_loosen_requires_approval` 拒绝，预算行和可用额度都不变（L6 I1 (a)） |
| 同额续期的时间条件（PG，Q4 选 B 时） | `now < cur.period_end` 时提交同额续期：`Budget.set` 以 `budget_loosen_requires_approval` 拒绝，旧期间的消耗仍然计入；`now ≥ cur.period_end` 时同一请求直接生效（L6 I1 (b)） |
| 调低到已消耗额以下（PG） | 允许；已记录的消耗不变；之后的消耗报 `budget_exhausted` |
| 放宽需要审批（PG） | `Budget.loosen` 返回 `waiting_approval`，预算行不变；另一位 owner 批准后执行，预算行为请求值，revision 加 1 |
| 自批（PG） | 请求人自己批准被拒；单人模式下（满足 24 小时条件）可以自批，有审计和告警；单人模式关闭后自批票不计入 |
| 执行时重查（PG） | 批准后、执行前用 `Budget.set` 调低：执行得到 `execution_refused:budget_changed`，审批已消费，预算保持调低后的值 |
| 发起时核对（PG） | `from_*` 或 `expected_revision` 与当前行不符：发起即被拒，不建审批 |
| 失败即关（PG） | 没有 `goals.budget.loosen` 策略，或策略形态不对：`Budget.loosen` 以 `approval_unavailable` 拒绝，预算不变 |
| 策略文件（Python） | v5 校验通过：version 5、expected_revision 4、只追加；摘要字段都在请求体字段内 |
| 路由清单 | `test_api_route_inventory.py`：`loosen_budget` 在 operations 中；能力计数相应变化 |
| 工作台路由 | `loosen_budget` 字段严格（缺 `from_*` 或 `expected_revision` 为 422）；operator 403 |
| 注册计数 | 新增业务 Action：`test_stage_register_pg` 中的计数加 1 |
| 复制件守护 | `owner_change` 的重定义已在 `test_function_copy_guard.py` 登记 |
| 前端（若加表单） | 预算页显示当前值、请求值和审批状态；`Approvals.tsx` 的 `APPROVAL_ACTION_TEXT` 加 `loosen_budget: '放宽预算'` |
| real_tenant_init | 第 4 步在没有审批时输出 `waiting_approval`；批准后执行得到预算行 |

## 6. 负责人决定（2026-10-28：Q1–Q8 均按推荐）

| # | 决定 |
|---|---|
| Q1 | A：拆成 `Budget.set`（收紧）和 `Budget.loosen`（放宽），不扩展条件语法 |
| Q2 | A：model、incentive、evolution 三种都要审批 |
| Q3 | A：新设预算算放宽，要审批 |
| Q4 | B：旧期间结束后（`now ≥ cur.period_end`）的同额续期免审批，其余期间变化都要审批 |
| Q5 | A：单人模式 `allowed` |
| Q6 | quorum 1 |
| Q7 | A：小额调高不免审批 |
| Q8 | A：作为 real 上线的前置 |

下表是提交给负责人时的问题和推荐，保留作记录。


| # | 问题 | 选项 | 推荐 |
|---|---|---|---|
| Q1 | 用什么方式只拦“放宽” | A：拆成 `Budget.set`（收紧）和 `Budget.loosen`（放宽）两个 Action；B：扩展策略条件语法，支持与当前值比较 | **A**。unified-approvals §3、§394 已经定了不扩展条件语法；NX-058 用的也是拆分 |
| Q2 | 哪些预算类型要审批 | A：model、incentive、evolution 全部；B：只拦 incentive（直接花钱给顾客）；C：incentive 和 model | **A**。三种都以真实币种计费；一个策略覆盖全部，更简单。B 也可以做：策略加条件 `budget_kind = incentive`，但 model 预算是 D-g 的累计上限，调高它同样会扩大真实花费 |
| Q3 | 新设预算（之前没有这一行）算不算放宽 | A：算，要审批；B：不算，第一次设可以直接生效 | **A**。没有预算时什么都不能花，新设就是从 0 放开；只有一个人时用单人模式即可 |
| Q4 | 每月续期（期间整体后移）要不要每次审批 | A：要；B：“同额续期”免审批，条件全部满足：服务端时间 `now ≥ cur.period_end`（旧期间已经结束）；新期间的起点等于旧期间的终点；长度相同；上限和单位都不变。由 `Budget.set` 直接执行 | **B**。旧期间结束后的同额续期不提高花费速度，每月都要第二个人批准只会增加操作负担。**旧期间结束之前续期必须审批**：那时续期等于把旧期间的消耗清零，提前拿到一整份新额度（L6 I1 (b)）。其余的期间变化（延长、移动起点、续期时同时调高）都要审批 |
| Q5 | 单人模式是否适用 | A：`allowed`；B：`not_applicable`（只有一人时就无法放宽预算） | **A**。只有一个人的公司必须能设预算；单人模式已有 24 小时资格历史、审计和告警 |
| Q6 | quorum | 1 或 2 | **1**，与其他策略一致 |
| Q7 | 小额调高是否免审批（例如 10% 以内） | A：不免；B：按比例或金额免审批 | **A**。条件语法不支持；在处理函数里加阈值，会让“分多次小幅调高”绕开审批 |
| Q8 | 上 real 之前是否必须完成 | A：必须，作为 real 上线前置；B：上线后补 | **A**。上线后改，需要在有真实花费时迁移语义 |

## 7. 工作量（估计）

| 片 | 内容 | 估计 |
|---|---|---|
| B1 | 迁移（方向判定、`loosen_budget` 分支、失败即关）、能力和 Action 注册、PG 测试 | 1 天 |
| B2 | 工作台路由、`approval_drill` 用法、策略 v5、路由清单与注册计数 | 0.5 天 |
| B3 | `real_tenant_init` 第 4 步、NX-038/NX-039 运行单 §2.0、可选的前端表单 | 0.5 天 |

## 8. 不做的事

- 不改消耗记账和 `budget_exhausted` 的语义。
- 不给模拟世界的预算加审批（E5，系统建立，不花真实的钱）。
- 不扩展策略条件语法（Q1）。

## 9. 实现说明（与上文的差异和细化）

- **Action 名**：放宽用业务 Action `nexloop.budget.loosen`（能力 `goals.budget.loosen`，operation `loosen_budget`，`business-actions.v1.json`），与 NX-058 的 owner Action 同一种登记方式；上文写的 `Budget.loosen` 指的就是它。`Budget.set` 名字和版本不变。
- **迁移** `budget_loosen`（临时编号 0369）：
  - `control.nexloop_budget_direction`：对照当前行（加行锁）判定 tighten、renewal 或 loosen，规则即 2.2 和 Q4；
  - `Budget.set` 的处理函数 `control.nexloop_governed_owner_change` 先过 `control.nexloop_budget_set_tighten_only`，放宽就以 `budget: budget_loosen_requires_approval` 拒绝，不写；
  - `control.nexloop_governed_loosen_budget`：失败即关的两层检查（策略形态；正在执行的审批就是这一条请求），执行时在行锁下核对 `from_*` 和 `expected_revision`，然后调用 0068 原样的 set_budget 写入，并写一条审计事件（方向、前后值、approval_id）；
  - **发起时核对**放在 `control.nexloop_approval_open` 里：能力为 `goals.budget.loosen` 时先核对当前行，不符就以 `budget: budget_changed` 拒绝，不建审批。
  - `control.nexloop_approval_summary_allowed` 增加本能力的八个摘要字段。三处重定义都在 `tests/test_function_copy_guard.py` 登记。
- **Q4 的“长度相同”**：旧期间是整数个日历月（例如 10-01 到 11-01）时，按月数比较，新期间也必须是同样多的整月（11-01 到 12-01 虽然少一天，也算同长）；否则按精确时长比较。
- **world**：处理函数不限制 world；工作台路由只在 real 世界。
- **real_tenant_init**：第 4 步改为 `loosen_budget`（第一笔 model 预算，`from_*` 为空、`expected_revision` 0）。`budget` 子命令第一次运行打开审批（`approval_pending`），另一位 owner 批准后加 `--execute` 再运行一次即写入；审批处理函数的拒绝按固定代码输出（例如 `budget: approval_unavailable`）。
- **工作台**：`loosen_budget` 在“审批”页显示为“放宽预算”，可以从“待我执行”执行；三个新拒绝代码有中文说明。
