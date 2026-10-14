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
| 出站录入器 | `outbound_recorder_stalled` | 新增迁移 0480 `authz.nexloop_nx037_outbound_backlog`（metrics 角色，只返回数字）：已被渠道接受或已投递、但还没写入会话、且等待超过 60 s 的回复数必须为 0。 |

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
