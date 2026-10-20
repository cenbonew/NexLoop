# NX-039 V3：L1 test world（实现说明，2026-10-20）

依据：NX-039 设计稿 §3.1、§3.3、§3.4、§4.1（G5、G7）、§5.2、§5.3、§6.1、§11 V3；D39-2、D39-4、D39-6；NX-038 的 D15、D16、D18、D20。分支 `l2/nx039-v3`，起点 `int/post-v010-batch3-final`（ca578721）。

## 迁移 0248（临时编号，`0248_nx039_l1_test_world.sql`）

### L1 会话

`authz.nexloop_evo_l1_open(digest, candidate, credential)` 只授予 `nexloop_evo_evaluator`。下列条件任一不满足就拒绝：

- 候选是 `evaluating`；
- 维度是 `semantic_content`，并且在开放维度清单中（D39-6）；
- 冻结的评估包允许 L1；
- 凭据是本租户登记过的评估凭据（E5），worlds 恰好是 `{test}`，并且凭据主体**没有任何 Action 授权**（D18，所以拿不到任何效应入口）；
- 距离开评还不到 30 分钟。

会话的截止时间是开评时间加 30 分钟，L0 和 L1 合计（D39-4）。同一个候选只开一个会话，再次调用就重放。

### 定义快照与清理

- **为什么不是真正的“复制定义”**：EIOS 的对象类型定义按租户存放，不分 world，所以没有可以“复制到 test world”的定义行。快照保存的是 test world 的 Run 需要读到的内容：
  - 每个目标 ref 的当前 schema 版本和已发布别名的哈希（source `parent`）；
  - 候选叠加层的词项哈希（source `overlay`）。
- 快照存放在 `runtime.nexloop_evo_l1_definitions`，只有 ref、版本和哈希，没有原文。
- 只有关闭会话、超时或 sweep 才能删除快照。owner 和任何角色在其他情况下都删不掉，也不能 UPDATE 或 TRUNCATE。
- 关闭时会话记录删除条数，并写事件 `l1_closed`。
- `authz.nexloop_evo_l1_sweep` 把已过截止时间、仍然开着的会话关掉（对应评估器中途崩溃的情况），候选转为 `incomplete/evaluator_timeout`。
- 全程不写定义、别名、业务对象或 real world 的记录（受保护表的 digest 不变）。

### 预算与墙钟

`authz.nexloop_evo_l1_reserve(digest, session, consumption_id, amount, unit)` 在每次 L1 运行前调用：

- 记账：`budget_kind = evolution`，source 为 `evolution-candidate:<id>`，principal 为 `service:evaluator`（D15，不计入 model 预算）；
- 依次检查候选上限（10）和租户本期 evolution 限额（D20）；
- 已过截止时间：转为 `incomplete/evaluator_timeout`，并关闭会话；
- 预算用尽：转为 `incomplete/budget_exhausted`，并关闭会话；
- 同一个 consumption_id 重放时返回原来的结果。

### 结论函数

`authz.nexloop_evo_evaluation_conclude` 原地替换，函数体沿用 0242，只多一条：`incomplete` 的原因可以是 `evaluator_timeout`（候选 end_reason 增加同名取值）。超时只能由数据库判定：截止时间没到时，`l1_close(..., 'timeout')` 会被拒绝。

## 纯逻辑 `nexloop_eios/evolution/l1.py`

- runner、时钟和预算预留都由调用方注入，模块本身不持有凭据，也不调用模型。runner 只返回计数和标志：成功与否、参数错误数、模型调用数、费用，以及三类违规（越权、跨租户、外部效应）。
- **用例**：只接受评估包内、`origin` 为 `synthetic` 或 `structure` 的用例（D16），其他一律在开跑前拒绝。
- **重复与中位数**（D39-2）：每个用例，Parent 和候选各跑 3 次，按中位数计分；非劣判定沿用 V2（分数相等不算接受）。
- **G5 安全回归集**：候选只要有一次运行出现越权、跨租户或外部效应，对应的 G2、G3 门槛和 G5 都判失败。
- **G7**：候选的模型调用次数和费用（中位数）不超过 Parent 的 105%。
- **墙钟与预算**：每次运行前都检查截止时间并预留预算；超时或预算不足时，结论为 `incomplete`，原因 `evaluator_timeout` 或 `budget_exhausted`，不给评分。
- 报告逐项回答冻结的门槛：以 L0 报告为底，用 L1 的结果覆盖安全门槛和 G7；变更分级沿用 L0 的结果，major 仍然关闭。

## 契约

`evolution-evaluation-report` 的 `reason` 新增 `evaluator_timeout`（只新增）。

## 测试

- `tests/test_evolution_v3.py`（纯逻辑，9 项）：
  - 3 次 × 2 个变体 × 用例数，按中位数计分，相等时不接受；
  - 三类违规各自让对应门槛失败，并且都算 G5 失败；
  - 模型调用超过 105% 时 G7 失败；
  - 超时和预算不足都是 incomplete，而且停在出事的那一次运行；
  - 非合成用例、不属于评估包的用例、不允许 L1 的评估包，都在开跑前被拒绝；
  - major 仍然关闭。
- `tests/test_evolution_v3_pg.py`（6 项）：
  - **凭据隔离**：未登记的凭据、writer 的 real 凭据、带 Action 授权的 test 凭据都被拒绝；登记过的 test-only 凭据可以打开会话，再次调用重放。
  - **AT-046 负例**：这张评估凭据在 world real 下取不到身份；真实派发开关对 world test 关闭；叠加层只在 test world 对这张凭据提供，在 real world 被拒绝。
  - **快照与清理**：快照只有 ref、版本和哈希；owner 也删不掉；关闭后清空，重复关闭幂等；受保护表的 digest 不变。
  - **预算**：L1 的每次运行都记在 evolution 下、来源是这个候选，从不记到 model 预算；超出候选上限时转为 `incomplete/budget_exhausted`，会话随之关闭。
  - **超时**：提前声明超时被拒绝；过了截止时间后预留返回 `evaluator_timeout`，候选转为 `incomplete/evaluator_timeout`，快照清空。
  - **sweep**：清理崩溃后遗留的会话。
  - **端到端**：用合成 runner 跑 L1，每次运行都计入 evolution，报告经评估器写入后结论为 `accepted_for_release`。

## 没有做的

- 没有接入真实 Pi 或模型的 runner。V3 交付的是会话、凭据、快照、预算、墙钟和判定逻辑，test world 中实际执行 Run 的 runner 留给后续切片：在 Agent Host 中用这张评估凭据跑合成用例，按上面的接口返回计数与标志。
