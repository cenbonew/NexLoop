# NX-037 A 类前置：实现说明

分支 `nx037-prep`，从 main `9d01169` 切出。迁移 `0161_nx037_trial_readiness.sql`（合入 s5g 时由临时号 0280 重编号）。依据：`NX-037-design.md` §2（初始化脚本和交互执行器先在合成环境跑通）、§3.1（停止条件）、§3.2 与 14 §7（证据包），ROADMAP D-g / D-i 定案值，11_DEPLOYMENT §9 D4（真实租户独立于合成 demo 租户初始化）。

本次产物只在合成或测试环境中运行：不接真实渠道、不接真实模型，也不打开任何真实派发开关。

## 1. 试运行配置 `deploy/configuration/nx037-trial.v1.json`

- **D-g 费用上限**：
  - 累计 ¥100；
  - 单次 Run ¥2；
  - 租户月度 ¥300；
  - 先用 3 次交互校准。
- **D-i 计划**：
  - 共 30 条交互；
  - 五类场景配比：普通回复 15、真实交付 4、拒收后受限回复 4、人工接管 4、结果不明对账 3；
  - 两个工作日，10:00–18:00。
- **停止规则**：D-i 的 4 条，加上设计 §3.1 的跨租户读取、单次 Run 超限、critical 告警、负责人叫停。
- **暂停阈值**：连续 10 次中错误率超过 10%；费用达到上限的 80%；存在未了结的不明效应。
- **引用的配置**：告警规则 v3，效应类别 v2。

## 2. 真实租户初始化脚本 `scripts/nx037/real_tenant_init.py`

### 参数

- 租户 ID：必须是 uuid，并且不能是合成 demo 租户 `SANDBOX_TENANT`；
- 负责人主体：不透明引用；
- 价值目录文件：只记录它的 sha256；
- 渠道配置引用：不透明引用；
- D-g 三个值：可覆盖，但必须满足“单次 ≤ 累计 ≤ 月度”；
- 试运行期间。

### 三种模式

- **`plan`（默认 dry-run）**：只输出将要执行的受治理步骤，什么都不执行：
  1. `service_grants`（受信配置；走 0050 路径，同时创建租户和服务主体）；
  2. `effect_categories`（受信配置）；
  3. `alert_rules`（受信配置）；
  4. `model_budget`（负责人 Action `Budget.set`；输出完整的请求体，request_id 是稳定的业务意图 ID）；
  5. `value_catalog`、`channel_configuration`（只作为引用）；
  6. `verify`。
- **`execute`**：只调用受信配置的入口，也就是 `service_grants.apply`、`effect_categories.apply`、`observability.apply_rules`。它们都以 configurator 角色、经配置函数执行，可以重复执行。
  - **效应类别**：只为租户已经发布的 Action 声明，其余 Action 记为 `deferred`。未声明的 Action 按“触达客户、外部效应”处理（fail closed），所以推迟声明不会打开任何东西。
  - **预算**：必须由负责人在自己的会话中提交（工作台），脚本本身不提交。没有传入负责人的 `owner_actions` 时，这一步记为 `owner_pending`，并给出 request_id。
  - **不做的事**：不写任何表，不打开开关，不读取渠道密钥。
- **`verify`**：只读，以 metrics 角色调用 `authz.nexloop_nx037_tenant_state`（0161）。以下条件全部满足才算 ready：
  - 租户为 active；
  - 真实派发的三层开关都关闭：本进程的 `REAL_DISPATCH_ENABLED` 不是 `true`，租户开关没有打开，没有任何打开的 Action 开关；
  - model 预算等于 D-g 的累计上限，币种一致，并覆盖整个试运行期间；
  - 告警规则的生效版本等于所配置的版本。
  - 效应类别的声明数量只报告，不作为 ready 条件，因为未声明的 Action 本来就是外部效应、处于关闭状态。

### 限制：单次 Run 上限和月度上限没有对应的数据库预算

- **原因**：数据库里每个租户、每个 world 只有一个 model 预算（`control.nexloop_budget_limits` 的主键是 (tenant, world, kind)），无法同时表达三个上限。
- **当前做法**：
  - 数据库中只设置累计上限（试运行期间的 model 预算）；
  - 单次 Run 上限和月度上限由执行器的停止和暂停规则执行；
  - verify 在 `enforced_by_executor` 中列出这两个值。
- **提案（需要定）**：如果要在派发时强制执行这两个上限，需要扩展预算表，或者新增按 Run 的预留检查。这属于契约和迁移层面的改动，本次没有做。
- **第二道防线**：模型账号侧的花费上限，由负责人在 Provider 侧设置。

## 3. 受控交互执行器 `nexloop_eios/controlled_interactions.py`

### 调度与执行

- **调度 `schedule`**：按配比均匀交错排列（最大余数法），前 10 条就包含全部五类场景。每条交互有稳定的 ID（uuid5(trial_id, seq)），重试和重启后不变。
- **执行 `run`**：逐条调用 driver 的 `interact` 发起交互，然后由 collector 按 run_id 采集证据。每条记录在下一条交互开始之前写入 `interactions.jsonl` 并 fsync；结束时原子写入 `state.json`。
- **恢复**：
  - 已记录的交互不会再执行；
  - 暂停之后可以恢复，恢复后错误窗口重新计数（负责人已经复核过）；
  - 已停止（stopped）的试运行不能恢复，需要用新的 trial id 重新开始。

### 证据（每条交互一条记录）

- **耗时分解**：
  - 端到端：执行器自己计时；
  - 模型：各次调用 completed_at − requested_at 之和；
  - 工具：0133 记录的工具耗时；
  - 平台：端到端减去模型和工具。
  - 分解方法与 NX-031 §4.2 相同。
- **其他字段**：
  - 模型调用次数、失败次数、结果不明次数；
  - 按类别汇总的费用；
  - 意图的状态与 ID；
  - effect 路径的拒绝码；
  - 交付回执 ID；
  - 对账结果；
  - 规则判定。

### 规则（`evaluate`，停止优先于暂停）

- **出现 1 次即停止**：
  - `unauthorized_send`（越权发送）；
  - `wrong_recipient`（发错对象）；
  - `contact_after_refusal`（拒收后仍联系）；
  - `duplicate_external_effect`（同一意图被执行超过 1 次）；
  - `cross_tenant_read`（跨租户读取）；
  - `per_run_cost_exceeded`（单次 Run 超过 ¥2）；
  - `critical_alert`（critical 告警）；
  - `owner_stop`（负责人叫停）。
- **暂停**：
  - `error_rate`：最近 10 次中，错误数除以 10 大于 10%，即至少 2 次错误；
  - `cumulative_cost`：累计费用 ≥ 累计上限的 80%；
  - `monthly_cost`：本月已有费用加上累计费用 ≥ 月度上限的 80%；
  - `unresolved_unknown_effect`：对账窗口结束时仍有结果不明的效应。

### 采集器

- **`PgCollector`**：以 metrics 角色调用 `authz.nexloop_nx037_run_evidence`（0161）。返回结果只有计数、毫秒数、金额、币种、状态码和 ID。

### Driver

- **合成 driver**：`nexloop_eios/interaction_drivers.py` 的 `SyntheticDriver`（原 `tests/support/nx037_synthetic.py`，测试仍经它绑定合成租户），由确定性 provider 加回环渠道组成。
  - 停止规则依据的事实，都从回环渠道和 provider 实际留下的记录推出：发送记录、收件人、授权、每个意图的执行次数、上下文中读取的租户。
  - 因此注入的违规与真实违规一样，都是通过检查记录发现的。
- **真实 driver**：`RealDriver` 不发送任何内容，也不持有渠道凭据。它提示负责人安排的操作者在 WebChat 完成交互，再通过 metrics 角色观测结果并推出事实，见 §8。

## 4. 证据包 `scripts/nx037/evidence_pack.py`

### 证据项（14 §7 / 设计 §3.2）

- inventory_before / inventory_after
- release_manifest
- ci_result
- restore_drill
- effect_reconciliation
- load_report
- public_preflight
- third_party_notices
- known_risks
- interactions

### 处理规则

- **缺失项**：没有提供的项记为 `missing`，从不编造。
- **一般来源文件**：只记录 sha256 和字节数。这些文件可能包含主机名、路径或人名，所以不复制任何内容。
- **发布清单**：只抽取固定字段：commit、tree、release id、CI 结果 digest、catalog、契约 digest、锁文件哈希，以及镜像 digest 和归档哈希。
- **交互**：输出三个文件：
  - `interactions/summary.json`：按场景类别和结果的计数、耗时分解的 p50/p95、费用、停止或暂停的结论；
  - `interactions/reconciliation.json`：意图 ID 及其对账状态；
  - `interactions/records.json`。

### 值的限制

- **允许的值**：uuid、十六进制哈希或 commit、时间与日期、十进制数、布尔值、null、release id，以及固定词表中的代码（场景类别、规则、状态）。
- **校验**：写入时校验，`check(DIR)` 可以复查；不允许的值会被拒绝。
- **输出目录**：不允许写入已存在的目录。

## 5. 迁移 0161

- **新增函数**：两个只读、security definer 的 owner 函数，各自带一个 authz 包装函数，只授权给 `nexloop_metrics`：
  - `nexloop_nx037_tenant_state`（租户状态、开关、预算、告警规则版本和 digest、效应类别声明数）；
  - `nexloop_nx037_run_evidence`（单个 Run 的证据）。
- **search_path**：`pg_catalog,pg_temp`。
- **RLS**：函数读取 FORCE RLS 的表，所以先设置当前事务的 `eios.tenant_id`，读取后恢复。
- **不涉及的部分**：不写数据，也不改动任何已有的函数或表。

## 6. 测试

### `tests/test_nx037_controlled_interactions.py`（14 个，不需要 PG）

- **配置**：D-g / D-i 的值，以及非法配置被拒绝；
- **调度**：配比正确，ID 稳定；
- **合成环境完整运行**：30 条全部完成；耗时分解正确；对账全部了结；拒收和人工接管的交互不发送；每个意图只执行一次；对已完成的试运行重新执行时，不会再发起任何交互；
- **停止规则**：7 条停止规则各注入一次违规，都在出现的那一条交互上停止；停止前证据已经写入；停止后不能恢复；
- **负责人叫停**；
- **错误率**：10 次中 1 次错误时不暂停，2 次时暂停；恢复后不重复执行已记录的交互；
- **费用暂停**：累计费用达到 80% 时暂停；月度费用达到 80% 时暂停；
- **不明效应**：存在未了结的不明效应时暂停，且不会重发；
- **证据包**：只包含允许的值；缺失项如实记录；inventory 的内容不会被复制；自由文本会被 check 找出；拒绝写入已存在的目录。

### `tests/test_nx037_tenant_init_pg.py`（3 个，干净 catalog）

- **dry-run**：步骤与预算请求正确；拒绝 demo 租户和非法的费用上限；缺少受信配置文件时 execute 不执行。
- **execute 与 verify**：
  - 初始化前 verify 不通过；
  - execute 后只差预算一项；
  - 负责人经自己的会话提交 `Budget.set`（受治理路径）后，verify 通过；
  - 部署层开关打开、租户开关打开、预算值不符时，verify 都不通过；
  - 输出中不出现 DSN。
- **run_evidence**：只返回数字；metrics 角色不能直接调用 runtime 下的函数。

## 7. 运行入口 `scripts/nx037/run_interactions.py`（分支 `nx037-driver`，迁移 0163，合入 s5i 时由临时号 0310 重编号）

- **输入**：
  - trial 配置 `deploy/configuration/nx037-trial.v1.json`（D-g 上限、配比、规则）；
  - 交互计划 `deploy/configuration/nx037-interaction-plan.v1.json`，负责人可编辑：
    - `order`：30 次交互的类型与顺序，必须与配比 15/4/4/4/3 完全一致，否则拒绝；试运行开始后顺序不能再改（state 中记录顺序 digest）；
    - `real_dispatch`：真实模式下负责人计划打开的三层开关状态（部署层、租户层及其 data_mode、打开的 Action 列表）。
- **用法**：
  - 合成：`python scripts/nx037/run_interactions.py --driver synthetic --trial-id <uuid> --out <目录> [--plan-file F] [--inject 序号=违规] [--calibration-pause]`
  - 真实：`python scripts/nx037/run_interactions.py --driver real --trial-id <uuid> --out <私有证据目录> --plan-file F --tenant <uuid> --owner-principal <引用> --value-catalog F --channel-ref <引用> --period-start <ISO> --period-end <ISO> --metrics-dsn-file F --owner-session-file F`
  - 恢复暂停：在原命令后加 `--resume <负责人确认引用>`。
- **每次交互之后**：执行器采集证据并判定停止和暂停规则（§3）。
- **真实模式的前置条件**（任何交互之前检查，不满足即拒绝，退出码 3，列出拒绝原因代码）：
  1. `real_tenant_init.verify` 除三项开关以外的检查全部通过：租户 active，model 预算等于 D-g 累计上限并覆盖试运行期间，告警规则生效；
  2. 三层开关与计划完全一致：本进程的 `REAL_DISPATCH_ENABLED`；租户开关及其 data_mode、打开的 Action 开关列表，经 metrics 角色的 `authz.nexloop_nx037_open_switches`（0163）读取。多开或少开任何一层都会拒绝。
  - 前置条件满足时，真实模式开始运行（§8）。（早先版本在这里以退出码 4 `real_driver_unavailable` 退出，`nx037-real-driver` 已去掉。）
  - 真实模式在 3 次校准交互之后自动暂停（规则 `calibration`），由负责人确认单次费用后用 `--resume` 继续。
- **停止与暂停**：
  - 已停止的试运行不能恢复，需换新的 trial ID；
  - 暂停后不带 `--resume` 再次运行会被拒绝（`paused_needs_resume`）；`--resume` 必须是不透明引用，先追加到 `resumes.jsonl`（fsync）再继续；没有暂停时带 `--resume` 也会被拒绝。
- **退出码**：0 完成；1 暂停；2 停止；3 拒绝（输入错误、真实模式前置条件不满足、需要恢复确认、已停止）。
- **迁移 0163**：`runtime.nexloop_nx037_open_switches` 与 authz 包装函数，只读，只授权给 metrics 角色，返回租户开关（enabled、data_mode）和打开的 Action 资源 ID，不含原因、主体或意图。
- **测试**：
  - `tests/test_nx037_run_interactions.py`（10 个，合成）：随附计划符合配比，正常完成且按计划顺序；四类立即停止（越权发送、发错对象、拒收后仍联系、外部效应重复执行），停止后不能恢复；错误率暂停需要负责人确认引用才能恢复，并记录恢复；费用暂停恢复后再次暂停；不明效应暂停后恢复；校准暂停；计划顺序不能在试运行中途更改；非法计划和非法注入被拒绝；真实模式缺少配置、带注入、会话文件非私有时，在连接数据库之前就拒绝。
  - `tests/test_nx037_tenant_init_pg.py` 新增 1 个（干净 catalog）：预算未设、开关与计划不符时拒绝并列出原因；前置条件不满足时不会提示操作者；预算设好、开关一致时真实模式开始运行，第 1 次交互提示操作者，操作者没有完成时因 `interaction_timeout` 暂停（退出码 1，记录 1 条）；多开一个 Action 时拒绝。

## 8. 真实 driver（分支 `nx037-real-driver`，迁移 0172，合入 s5m 时由临时号 0172 重编号）

依据 NX-037 设计 D1：交互渠道是原生 WebChat，交付动作接企业自有的幂等端点，由知情的内部人员扮演消费者。

### 8.1 职责

- **driver 不扮演消费者，也不发送**：真实交互由负责人安排的内部人员在 WebChat 完成。每次交互，driver 做五件事：
  1. 读取服务端当前时间；
  2. 提示操作者执行第 N 次交互：类型和要点（`INSTRUCTIONS`），并给出时限；
  3. 以 metrics 只读角色轮询 `authz.nexloop_nx037_interaction_observation`（0172）。等待的条件是：提示之后的第一条消费者消息、它的 Run 已经结束（succeeded 或 failed）、Run 的效应没有未结束的（accepted、dispatching、observed_fulfilled）；
  4. 从记录推出停止规则所需的事实，交给执行器判定；
  5. 证据仍由 `PgCollector` 采集（0161）。
- **超时**：在 `--interaction-timeout`（默认 900 s）内没有完成，记为 `interaction_timeout`，按新增的暂停规则 `interaction_timeout` 暂停，不猜测结果。
- **不持有的东西**：任何发送代码路径、渠道凭据、写权限。`RealDriverConfig` 只保存渠道引用和负责人会话文件路径。
- **操作者**：
  - 实际试运行中是人：`ConsoleOperator` 把提示打到终端（stderr），并以 ID 和代码追加到试运行目录的 `prompts.jsonl`；
  - 合成栈测试中是自动操作者：Playwright 驱动 WebChat，`apps/web/test/browser/operator.browser.mjs`。

### 8.2 0172 观测函数（只读，只授权给 metrics 角色）

每条事实都来自这条触发消息和它的 Run 的记录：

| 事实 | 依据 | 对应规则 |
|---|---|---|
| `unauthorized_sends` | 触发消息的 Agent 外发记录，没有承载它的受治理出站行（0079） | 越权发送 |
| `wrong_recipient_sends` | 外发记录在别的对话中，或 Run 签发给了另一个消费者 | 发错对象 |
| `contacts_after_refusal` | 消费者已有生效的联系限制（0109）之后仍有 Agent 外发 | 拒收后仍联系 |
| `duplicate_deliveries` / `duplicate_fulfilled` | 同一触发的受治理出站行被投递或接受超过一次；同一消费者、同一业务摘要的已完成意图超过一个 | 外部效应重复执行 |
| `cross_consumer_reads` | Run 的模型上下文引用了别的消费者的 Consumer、对话或消息（NX-031 §4.4 的检查） | 跨租户/跨客户读取 |
| `critical_alerts` | 提示之后该租户 firing 的 critical 告警 | critical 告警 |
| 效应状态 | Run 的意图按状态计数；`unknown` 计为未了结 | 暂停：不明效应 |

- **意图与 Run 的关联**：意图在执行时才写入 `origin_run_id`，执行之前通过 Run 的派发控制快照（0097 `effect_dispatch_controls`）关联到 Run。
- **observed_fulfilled 视为未结束**：外部已经执行、受治理的最终确认还没完成。

### 8.3 运行入口的变化

- 真实模式在前置条件满足后正常运行。新增参数：`--interaction-timeout`、`--poll-seconds`。
- 去掉了退出码 4。前置条件不满足时仍是退出码 3，而且不会提示操作者。
- 暂停后仍需要 `--resume <负责人确认引用>` 才能继续。

### 8.4 测试

- **`tests/test_nx037_real_driver.py`**（单元测试，不需要数据库）：
  - 等到 Run 结束且效应结束才返回；
  - 7 种观测到的违规各自命中对应的停止规则；
  - 不明效应、Run 失败、超时（暂停）；
  - `ConsoleOperator` 只写 ID 和代码。
- **`tests/test_nx037_real_driver_pg.py`**（端到端，合成栈）：
  - 组件：真实 API（TLS、127.0.0.1、构建好的 WebChat），消息 relay，确定性 Agent Host 及其 runtime worker 和 guard，本地 JSON 投递，effect worker；
  - 自动操作者是 Chromium，在 WebChat 中发消息；
  - 5 次小计划（5 次都是 ordinary_reply）：
    1. 第 1 次完成；
    2. 第 2 次操作者不动作，因 `interaction_timeout` 暂停；没有恢复确认时拒绝继续；带负责人确认引用恢复；
    3. 第 3 次完成；
    4. 第 4 次交互中注入一条 firing 的 critical 告警（测试夹具直接写入告警事件），停止；
    5. 第 5 次不执行。
  - 断言：
    - 第 1、3、4 次各有自己的 Run，各有一个已完成的意图，对账结果为 settled，所有违规计数为 0；
    - 恢复记录正确；
    - 只产生 3 份 JSON 投递，库中只有 3 个意图；
    - 后台服务的输出中不含秘密值。
  - 标记为 `real_browser`，用 `-m real_browser` 运行（与 AT-015 同一条单独的浏览器测试线）。本机耗时约 70 s。
- **`tests/test_nx037_tenant_init_pg.py`**：真实模式在前置条件满足时运行并因超时暂停，见 §7。

### 8.5 限制

- **端到端测试绕过了运行入口**：测试直接以 `RealDriver` 调用执行器，没有经过 `run_interactions.py`。原因是合成栈的组装租户没有负责人预算与告警规则（D-g 前置条件）。运行入口在真实模式下的路径另由 `test_nx037_tenant_init_pg.py` 覆盖：前置条件检查、提示、超时暂停。
- **端到端计划只覆盖 ordinary_reply**：其余四类场景（真实交付、拒收后受限回复、人工接管、结果不明对账）的观测函数字段已经有了，但在合成栈上没有端到端覆盖，由真实试运行中的操作者完成。
- **人工接管等场景**：员工回复（sender_kind 为 human_takeover）不计为 Agent 外发。人工接管交互中 Run 的结束条件，需要在实际试运行中核对。
- **OPERATIONS §9.4**：由 L5 之后同步。
