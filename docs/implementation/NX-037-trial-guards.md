# NX-037 真实试运行开跑守卫与期间费用（L5 S5 核对第 6、7 项）

依据：调度员 2026-10-14 的派工。分支 `l3/nx037-trial-guards`，从 main 327907c 切出。

## 1. 开跑与恢复守卫（`scripts/nx037/trial_guards.py`）

real 模式在开始时和每次 `--resume` 时检查以下各项。任何一项不通过，退出码为 3，并列出原因代码。

| 守卫 | 原因代码 | 判定 |
|---|---|---|
| D3 知情同意 | `consent_missing` | 交互计划 `consumers` 里的每个消费者，都要在试运行配置的 `participants` 里登记 sha256；`--consent-files`（私有 JSON）里有对应的同意书路径；同意书存在、是普通私有文件（0600、属于本用户、不是符号链接），sha256 与登记一致。试运行目录的 `consent.json` 和证据包的 `interactions/participants.json` 只写 consumer id 与 sha256。 |
| 试运行窗口 | `outside_trial_window` | 当前时间落在 `plan.window` 的 start ≤ 本地时间 < end 内，时区默认 Asia/Shanghai，可在配置里写 `timezone`。没有配置窗口时一律拒绝。 |
| 企业端点 | `enterprise_endpoint_nonconformant` | 计划打开 `nexloop.enterprise.deliver:1` 时，用 effect worker 的企业 provider 文件运行 `endpoint_conformance.py`，报告写到 `<试运行目录>/preflight/endpoint-conformance.json`。 |
| WebChat 投递 | `webchat_delivery_unavailable` | `local_json_delivery` 新增 `GET /v1/health`（需要 Bearer）：执行者凭据能在当前权威下认证、导出存储可用时返回 200 `{state: ok}`，否则返回 503。守卫通过回复 provider 文件请求这个接口。 |
| 出站录入器 | `outbound_recorder_stalled` | 新增迁移 0195 `authz.nexloop_nx037_outbound_backlog`（metrics 角色，只返回数字）：已被渠道接受或已投递、但还没写入会话、且等待超过 60 s 的回复数必须为 0。 |

其余前置检查不变：租户就绪、三层开关、币种与 Host。测试栈（`test_stack`）同样经过全部守卫。

## 2. 期间实际费用（最小方案）

- `controlled_interactions.run(..., month_cost=...)` 在每次交互前后各读一次租户本月 model 费用（0185）。
- 记录的 `cost` 保留三个数：
  - `total`：触发 Run 自身的费用，仍用于单 Run 上限；
  - `period_increase`：交互期间的实际增量，包括兜底、复评、角色 Run，以及抽取、匹配等写入 model 费用记录的花费；
  - `counted`：两者取大。
- 累计上限和月度上限按 `counted` 计算。恢复时，`month_cost_before` 扣除的也是本试运行已计入的 `counted`。证据包的 summary 同时给出 `cost.total` 和 `cost.counted`。
- **局限**：同一时段内与本次交互无关的花费（例如其他客户的后台抽取）也会计入这一次交互。计数只会偏高，因此只可能提前暂停，不会漏停。

### 按交互精确归属（未实现，留作后续）

- 在 cost_entries 上记录"起因"，即触发这笔花费的来信或 Run；兜底、复评、角色 Run 在签发时继承原来信的引用。
- `nexloop_nx037_run_evidence` 改为按"起因"汇总，不再只按 `run_id`。
- 抽取、匹配类花费按会话归属到最近一次来信。
- 规模约 M：涉及 cost_entries 的写入方、0161 和 Run 签发链路，需要和 L2、L4 协调。

## 3. 测试

- `tests/test_nx037_trial_guards.py`：
  - 同意：1 个通过用例，9 个反例；
  - 窗口：本地时间、边界、时区，以及没有配置窗口；
  - 企业端点：通过；凭据错误、配置文件不存在、端点已停，都拒绝；
  - WebChat 投递：真实投递服务通过；凭据错误、配置文件不私有、服务已停、没有后端，都拒绝；
  - 录入器：积压 30 s 时通过，90 s 时拒绝；
  - 期间费用：触发 Run 之外的花费让累计上限在第 4 次交互时暂停。
- `tests/test_nx037_assembly_examples.py`：同意相关的示例填入合成值后能通过守卫。
- `tests/test_nx037_synthetic_five_pg.py`：五类场景在全部守卫下仍然跑通。另在运行入口上验证两个反例：不给同意映射时拒绝（`consent_missing`），窗口外拒绝（`outside_trial_window`）。证据包里有参与者条目，每条记录都有 `period_increase` 和 `counted`。

## 4. 开跑清单与人工补偿

见 [OPERATIONS.md](../OPERATIONS.md) §9.4a。仍需人工补偿的项：单 Run 成本人工查看、告警值守（没有外部通知渠道）、录入器存活（开跑时没有积压就无法判断）、负责人会话的有效性。

## 5. 负责人 2026-10-14 变更：客户由 AI 代理扮演

- **D3 改为两部分**：
  - 负责人一次性操作授权：`operator_authorization.authorization_sha256`，加上私有授权文件 `--operator-authorization-file`；不满足时为 `authorization_missing`。
  - 参与者登记：负责人是唯一的员工（调度员补充，2026-10-14）。计划的每个消费者都必须是 `kind: agent` 的参与者（`consumer_ref` 加工具名）；`kind: human` 只用于负责人本人的员工角色（`role: staff`，不带消费者账号）。其他情况一律为 `consent_missing`。原来的真人同意书核对和 `--consent-files` 已经去掉。
- **persona 标记**：全部参与者都是代理时，每条交互记录、state 和证据包 summary 都标 `persona: simulated_agent`；`participants.json` 写 consumer id、kind、persona。
- **系统内的测试人设标记：v0.1 不做**（调度员审定，2026-10-14）。
  - v0.1 靠三样东西区分测试账号：交互记录与证据包里的 `persona: simulated_agent`、证据包的 `participants.json`、试运行配置的 `participants`。后续分析按 participants 清单排除这些账号。
  - 局限：系统内（数据库、工作台、导出）没有标记，这些账号产生的会话、Claim、承诺等数据与真实客户的数据混在一起，只能事后按清单排除。
  - **后续项（v0.1 之后，需要 ADR）**：
    - 新增控制面表 `control.nexloop_consumer_personas`，字段为 tenant_id、world、consumer_id、persona（只允许 simulated_agent）、trial_id、set_by、set_at、reason；租户外键遵循 NX-052。
    - 写入只走可信配置（configurator）或负责人的受治理 Action；运行时、Agent 和服务主体不能写。
    - 读取：metrics 角色的只读函数；分析视图、导出（NX-029）和评测数据集在源头按它排除。
    - ADR 需要决定：标记能否撤销、撤销是否留痕；被标记账号的数据保留期；标记是否影响派发（建议不影响，只影响分析）。
- 开跑清单写明"客户为模拟、系统为真实"（OPERATIONS §9.4a）。验收报告模板：`docs/release/nx037-acceptance-report.template.md`。
- 交互计划 v3 草案：`docs/implementation/NX-037-plan-v3-draft.md`，待审定。

## 6. 计划 v3 与多轮判定（调度员审定，2026-10-14）

- **新文件**：`nx037-interaction-plan.v3.json`、`nx037-trial.v3.json`。共 36 次，配比 16/6/5/5/4，前 3 次为校准（普通回复、交付、结果未知）；上限与规则字段和 v2 相同。
- **加载器同时接受 v2 和 v3**：
  - v3 的 order 项写成 `{kind, variant, consumer}`，配比仍按 kind 计数；
  - consumers 取自 order；
  - 拒收类账号不得出现在交付类和结果未知类里，否则拒绝（`account groups`）；
  - order 摘要对 v3 包含变体和账号，对 v2 不变。
- **多轮判定**：
  - 新增迁移 0196 `authz.nexloop_nx037_interaction_messages`（metrics 角色，只返回 id 和时间），列出本次交互提示之后受理的全部来信；
  - RealDriver 对每条来信按其受理时间走 0172 观测。全部了结才算完成：Run 已终结且没有未结效应；或者没有 Run（会话被接管），但已经有员工回复；
  - 任何一轮的 Run 失败，本次交互都判为 error；拒收后联系等停止事实按各轮累加；
  - 执行器收集每一轮 Run 的证据并合并（`merge_evidence`）；单 Run 上限按最大的那个 Run（`cost.max_run`）判定；
  - 记录里增加 `run_ids`、`messages`、`variant`。
- **完成标记**：ConsoleOperator（real 模式）提示后等待 `<试运行目录>/operator-done/<序号>`，代理发完全部消息后创建这个文件；到时限仍未创建，由驱动按超时处理。
- **测试**：
  - v3 加载与配比；坏配比、坏变体、缺账号、v3 用了字符串项、拒收账号做交付、consumers 与 order 不一致，都拒绝；
  - 多轮：三轮都成功；中间一轮失败判为 error；后一轮拒收后联系触发停止；某一轮未了结时等到超时；接管消息凭员工回复了结；
  - 多 Run 证据合并与按最大 Run 判定单 Run 上限；变体文本；完成标记的等待；
  - 五类场景测试：接管交互的两条来信都被计入（`messages` 为 2，Run 为 1）。
