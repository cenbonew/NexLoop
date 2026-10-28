# NX-043 X7：验收清单与 stage 验收工具

- **依据**：
  - NX-043 设计稿：§2 三个阶段，§3–§6，§8，§9 S7 验收覆盖及建议新增的五项，§10 中的 D1–D9；
  - X1、X3、X4、X5、X6 和报告 Action 的实现文档。
- **基准**：第九批预演 `79b0e1eb`。
- **工具**：`scripts/acceptance/nx043_stage.py`，示例配置 `deploy/examples/nx043-stage.example.json`，本地测试 `tests/test_nx043_stage_pg.py`。
- **工具约定**：
  - 配置、结果、阻塞语义、脱敏与 S6 和 AT-046 的 stage 脚本相同（`s6_common`，结果格式 `nexloop-s6-stage-result/1`）。
  - 写入只走 owner 的受治理工作台 Action（HTTPS，口令从 stdin 读取），或影子世界 worker 凭据的模拟 worker 入口；探针只读。
  - 世界、实验、策略的 id 只存在 `results_dir` 下的私有状态文件里（0600），输出只有代码、计数和摘要。
- **范围**：screening、shadow 和 online 三段。负责人 2026-10-21：测试阶段放开 online，D9 校准暂不作为前置，online 段可验（`online`、`online-probe`、`online-stop` 三个阶段）。

## 验收清单

| # | 要求 | 来源 | stage 验收（阶段：步骤） | 本地覆盖 | 当前 |
| --- | --- | --- | --- | --- | --- |
| 1 | 策略版本只用 D1 允许的杠杆，由负责人发布，不可修改 | D1 | prepare：`candidate_strategy_published`、`control_strategy_published` | `test_nx043_experiments_pg` | stage 可验 |
| 2 | 实验先预登记，登记后冻结 | D3 | prepare：`shadow_experiment_registered` | 同上 | stage 可验 |
| 3 | 阶段决定世界：screening 在 sim，shadow 在 shadow | §2 | prepare：两种登记 | 同上 | stage 可验 |
| 4 | 选用的孪生未校准时，screening 不能开始 | D9 | run：`screening_start_needs_a_calibrated_twin`（期望 409 `twin_not_calibrated`） | 同上 | stage 可验，需要 `inputs.screening` |
| 5 | 实验开始不打开真实派发，三层开关不变 | §4、§9 建议 1、D3 | run：`shadow_experiment_started`；probe-after：`real_dispatch_switches_unchanged` | `test_nx043_experiments_pg`、guard 测试 | stage 可验 |
| 6 | 影子复制按租户开关，默认关闭，只复制入站事件的哈希和 Claim 类型 | D7 | run：`shadow_copy_on`；probe-after：`shadow_copy_counts`；report：`shadow_copy_off` | `test_nx043_shadow_pg` | stage 可验，需要 real 入站流量 |
| 7 | 影子决策只记录、不发送；影子世界零 provider 请求 | D7、§9 建议 5、AT-046 | 拆成跨主机的几步（按 AT-046 的做法）：shadow-keys（DATA_HOST 探针）：`shadow_events_copied`；decide（APP_HOST，只用 sim_worker）：`shadow_decisions_recorded`；probe-after（DATA_HOST）：`shadow_decisions_landed`、`shadow_sends_nothing`；merge：`nx043-shadow` | 同上 | stage 可验 |
| 8 | 停止规则：到结束时间由护栏自动停止 | D4、§5、§9 建议 2 | probe-after：`guard_stops_at_the_end`（等待告警评估服务的 tick，reason 为 `guardrail: ended`） | `test_nx043_guardrails_pg`、`test_nx043_guard_wiring_pg` | stage 可验 |
| 9 | 其他护栏：拒绝率、对账异常、孪生被撤回 | D4 | — | 同上 | 只有本地覆盖（stage 上需要注入真实越界） |
| 10 | 实验预算用尽即阻断新消耗 | D5、§9 建议 3 | 预算演练（负责人 2026-10-28 方案 A，迁移 0366）：budget-start：`budget_drill_registered`、`budget_drill_started`；budget-before；budget-runs：`first_run_accepted`、`second_run_accepted`；budget-probe：`budget_exhausted_stops_the_drill`、`first_run_kept`、`new_spend_refused_at_dispatch`、`budget_drill_visible`、`drill_runs_never_real` | `test_nx043_budget_drill_pg`、`test_nx043_guard_wiring_pg`、`test_nx043_stage_pg` | **stage 可验**（预算演练，孪生未校准，结论不代表实验效果） |
| 11 | 分配：按消费者，会话聚类同组，对照组至少一半 | D2、AT-048 | online-probe：`online_allocated` | `test_nx043_allocation_pg`、`test_nx043_x4_wiring_pg` | **可验**：X4 接线（0304）后 real 入站首次分配，经 online 段验证 |
| 12 | 成熟窗口；窗口没到的样本不计；unknown 不算成功；样本不足不出结论 | D6、§6、§9 建议 4 | report：`experiment_result_reports_nothing_below_the_minimum` | `test_nx043_reports_pg`、`test_nx043_pooled_pg` | stage 只能验“不出结论”；按成熟窗口出统计需要 online 的分配 |
| 13 | 报告只含聚合数，冻结后不可修改，不能反推个人；只有 owner 能取 | §6、X3 4.x、报告 Action | report：`shadow_report_aggregates_only` | `test_nx043_report_action_pg` | stage 可验 |
| 14 | real 没有任何变化 | AT-046、§8 | probe-after：`real_side_unchanged`（每张带 world 列的表的 world-real 行摘要，外加 real 服务凭据和授权事实；口径见下文“real 摘要的排除口径”） | 本地测试 | stage 可验 |
| 15 | 结论不写成因果 | §6 | report（报告的 statement 字段） | `test_nx043_reports_pg` | 本地覆盖 |
| 17 | 胜出策略不自动推广；推广由负责人决定，作为新版本走正式发布链 | D8 | —（stage 上没有操作） | `test_no_path_from_a_report_or_the_guard_to_a_strategy_publication`：报告、报告 Action、护栏的函数体里，没有写策略版本或策略事件的语句，也没有调用任何发布 | 本地覆盖 |
| 16 | online 阶段：登记、开始、入站首次分配、候选组外发审计带策略版本、停止 | §2、D2、D3、D4 | online：`online_experiment_registered`、`online_experiment_started`；online-probe：`online_allocated`、`online_strategy_audited`；online-stop：`online_experiment_stopped` | `test_nx043_online_open_pg`、`test_nx043_stage_pg` | **可验**（负责人 2026-10-21：测试阶段放开 online，D9 校准暂不作为前置） |

## stage 上的执行顺序

每一步的结果都带同一个 `run_nonce`，取自私有状态文件里的 run 编号。`probe-after` 和 `merge` 用 `s6_common.load_chain` 核对 `--from` 给的各步：属于同一次运行（nonce 相同）、每步只出现一次、执行时间按链的顺序。私有状态文件和 key 文件需要由调度员在 DATA_HOST 和 APP_HOST 之间拷贝，两者都是 0600，里面只有 id 和哈希。

1. `prepare`（owner）。
2. `run`（owner）：screening 开始被拒；shadow 实验开始；影子复制打开。
3. 由调度员让 real 入站流量进来，例如合成 webchat 消费者。
4. `probe-before`（DATA_HOST，只读）：先等 real 侧安静下来，即该租户没有未结束的 real job，并且在 `quiet_seconds` 内 world-real 摘要不再变化，然后才取基准（L6 审查 3.2）。这批入站消息引起的 real 自动回复因此都包含在基准里，不会被误报为“real 变了”。选择等待而不是改用不触发回复的注入方式，理由是：webchat 入站会经过 relay 和 Run，如果用 synthetic persona 规避回复，与真实流量路径不一致，也取决于 relay 的行为；等待则不改变真实路径。
5. `shadow-keys`（DATA_HOST，只读）：把已复制事件的 key 写进私有文件。key 是哈希，不含个人信息。
6. `decide`（APP_HOST，只用 sim_worker）：读取拷贝过来的 key 文件，对每个 key 记录候选策略的决策。
7. `report`（owner）：影子报告取**昨天**这一整天（L6 审查 4.1）。报告第一次读取就会冻结，所以不能取还没过完的当天。最后关掉影子复制。
8. 等到 `ends_in_minutes` 之后，执行 `probe-after --from <probe-before> --from <shadow-keys> --from <decide>`（DATA_HOST）：等待护栏停止，核对决策已落库，并比较摘要。
9. `merge --from` 以上四步：没有连接，合成一份 `nx043-shadow` 结果。
10. `online`（owner）：在 world real 登记一个 online 实验（不选孪生）并开始。必须在 `probe-after` **之后**执行：online 会写 real 侧的分配和审计，否则会被 real 摘要比较当成变化。
11. 由调度员再让 real 入站流量进来，然后执行 `online-probe`（DATA_HOST，只读）：各组分配人数（候选组不超过上限）和 `strategy_applied` 审计数，只输出计数。
12. `online-stop`（owner）：停止 online 实验，所有人回到当前策略。
13. 预算演练（第 10 项）：`budget-start`（owner）→ `budget-before`（DATA_HOST）→ `budget-runs`（APP_HOST，演练 world 的 api 凭据）→ `budget-probe`（DATA_HOST）。见下文“预算演练”。
14. `gaps`：已经没有阻塞项（第 9 项按设计只做本地覆盖）。

## 审查（L6 `nx043-x7-review.md`）的处理

- **2.1（确认级）**：第 7 项按 AT-046 的拆法分成 shadow-keys（DATA_HOST）、decide（APP_HOST）、probe-after（DATA_HOST）三步，最后 merge。decide 不再需要探针。
- **3.1**：nonce 方案写成了共用函数，放在 `scripts/acceptance/s6_common.py`：
  - `Result(..., nonce=...)` 会在结果里写 `run_nonce`；没有设置时不写，所以其他脚本的输出不受影响；
  - `new_nonce()` 生成一个 nonce；
  - `load_chain(paths, prefix=, order=, nonce=)` 核对同一个 nonce、每步只出现一次、执行时间按顺序，返回合并后的步骤和阻塞项。`order` 由调用方传入它实际需要的那一段前序步骤，必须恰好是这几步：不能多出这段之外的步骤，也不能缺少任何一步（L6 复核 fca51f2d）。例如 probe-after 只接受 probe-before、shadow-keys、decide，把一份 probe-after 的结果当作前序传进来会被拒绝。

  AT-046 可以直接复用这套函数，由 L2 接入。
- **1.1**：清单新增第 17 项 D8，并补了一条结构测试。
- **3.2、4.1**：见上面的执行顺序第 4 步和第 7 步。
- **2.2（可选）**：`shadow_sends_nothing` 的 0 是由效应表的 CHECK 从结构上保证的。真正能说明决策只停在模拟里的，是新增的 `shadow_decisions_landed`：决策都在影子世界里，并且只有替身意图。

## 发现

- **X1 的“像真实内容”检查会拒绝带编号的假设文本**，比如 `NX-043` 会被当成会员号（字母-3 位以上数字）。工具里的假设文本因此不写编号，生成的 id 也只用字母。owner 自己写登记时也要避开这类写法，建议在工作台的提示里说明。
- **`allocation_seed` 不能超过 2147483647**，工具里取哈希的前 7 位十六进制。

## stage 执行单（release 32，调度员 2026-10-22）

对齐的 stage 现状：
- 模拟进程组已启用，4 个服务在运行；
- 已有一个 sim world（3 个合成 Consumer，替身 Action 目录已发布）；
- 真实外发三层开关全开（租户层、enterprise.deliver、outreach.send）；
- online 已放开（0309）。

**real 外发是开着的。** online 段里候选组的外联会**真实执行**。目前只会落到参考企业端点这个替身，以及合成 webchat。所以收尾时必须停止实验，并确认没有在途的外发（第 O4、O5 步）。

### 主机、角色和配置

| 位置 | 配置文件 | 角色 | 连接 |
|---|---|---|---|
| 调度员机器 | `PRIVATE.json` | owner：`owner {origin, ca_file, username_file}`，口令从 Keychain 经 stdin 传入 | 工作台 HTTPS |
| DATA_HOST | `DATA.json` | `inspector` 和 `probe`：bootstrap DSN，`default_transaction_read_only=on` | 本机 socket（local pg_hba），只读；脚本连接后再设一次只读并 `show` 核对，不是只读就拒绝（L6 复核 1.1） |
| APP_HOST | `APP.json` | `sim_worker {dsn_file, token_file}`：shadow world 的 worker 凭据 | hostssl |

- 三份配置的 `tenant_id`、`inputs`、`results_dir` 相同。
- `inputs.screening.world` 填已有的 sim world，`inputs.screening.twin` 填它的孪生版本。
- 私有状态文件 `results_dir/nx043-state.json`（0600，只有 id）由 owner 阶段写出。每次 owner 阶段之后，调度员把它拷到 DATA_HOST 和 APP_HOST。
- key 文件 `nx043-shadow-keys.json` 由 DATA_HOST 拷到 APP_HOST。
- 各步结果（JSON）汇总到调度员机器，用于 merge。

### 顺序约束

1. **整个 X7 都排在 AT-046 的 `probe-after` 之后。** X7 第 S3 步注入的 real 入站流量会改变 real 侧数据，AT-046 的 real 摘要比较会把它当成变化。
2. **online 段（O1–O5）排在 X7 的 `probe-after`（S8）之后。** online 会写 real 侧的分配、外联和审计。
3. **O1 之前，就绪检查 R1 必须全部 ready。** 只有 `real_dispatch_state` 是信息项：它报告 open 或 closed，不构成阻塞。

### 步骤

| 步 | 主机 | 角色/连接 | 命令 | 产出 |
|---|---|---|---|---|
| R1 | DATA_HOST | inspector，socket 只读 | `nx043_stage.py readiness --config DATA.json` | `nx043-readiness`：sim world、screening 孪生未校准、两个策略可发布、shadow 复制关、real 入站分配触发器、real 派发层状态（tenant 层和 action 层只读取开关表；部署层在数据库里看不到，报 `not_observable_from_db`，可在 `inputs.deployment_real_dispatch` 填入操作员观察到的容器环境变量 `REAL_DISPATCH_ENABLED`）、无冲突实验，每项 ready 或 blocked 加原因 |
| S1 | 调度员机器 | owner，HTTPS | `security find-generic-password … -w \| nx043_stage.py prepare --config PRIVATE.json` | shadow world、两个策略版本、shadow（和 screening）登记；私有状态文件 |
| S2 | 调度员机器 | owner，HTTPS | `… \| nx043_stage.py run --config PRIVATE.json` | screening 被拒 `twin_not_calibrated`；shadow 开始；影子复制打开 |
| S3 | 调度员机器 | 合成顾客，HTTPS（WebChat 顾客路径） | `security find-generic-password … -w`（每个账号一行 `别名<TAB>口令`）`\| uv run --frozen python scripts/acceptance/synthetic_customer.py --config CUSTOMERS.json --plan PLAN.json`：由合成 Consumer 注入 real 入站流量（见下文“合成顾客发言工具”） | 拷贝到 shadow world 的事件 |
| S4 | DATA_HOST | probe，socket 只读 | `nx043_stage.py probe-before --config DATA.json` | 等 real 侧安静，取 real 摘要和开关摘要 |
| S5 | DATA_HOST | probe，socket 只读 | `nx043_stage.py shadow-keys --config DATA.json` | key 文件（拷到 APP_HOST） |
| S6 | APP_HOST | sim_worker，hostssl | `nx043_stage.py decide --config APP.json` | 影子决策，只记录 |
| S7 | 调度员机器 | owner，HTTPS | `… \| nx043_stage.py report --config PRIVATE.json` | 影子报告（昨天）、实验结果；影子复制关闭 |
| S8 | DATA_HOST | probe，socket 只读 | 等 `ends_in_minutes` 之后：`nx043_stage.py probe-after --config DATA.json --from S4 --from S5 --from S6` | 护栏停止、决策落库、real 摘要不变 |
| S9 | 调度员机器 | 无连接 | `nx043_stage.py merge --config PRIVATE.json --from S4 --from S5 --from S6 --from S8` | `nx043-shadow` |
| O1 | 调度员机器 | owner，HTTPS | `… \| nx043_stage.py online --config PRIVATE.json` | world real 的 online 实验，登记并开始；状态文件里记下开始时间（拷到 DATA_HOST） |
| O2 | 调度员机器 | 合成顾客，HTTPS（WebChat 顾客路径） | 同 S3，用新的 `plan_id`：再注入 real 入站流量 | 首次分配；候选组的外联**真实发出**（替身端点、合成 webchat） |
| O3 | DATA_HOST | probe，socket 只读 | `nx043_stage.py online-probe --config DATA.json` | 各组分配人数、`strategy_applied` 审计数 |
| O4 | 调度员机器 | owner，HTTPS | `… \| nx043_stage.py online-stop --config PRIVATE.json` | 实验停止，所有人回到当前策略 |
| O5 | DATA_HOST | probe，socket 只读 | `nx043_stage.py online-drain --config DATA.json` | 开始以来**实验成员**（有组别的分配）的 real 外联和效应没有在途的（L6 复核 1.3：其他 real 外联不影响判断）（`pending_approval`、`accepted`、`dispatching`、`unknown`、`observed_fulfilled` 都为 0）；默认等 900 秒 |
| B1 | 调度员机器 | owner，HTTPS | `… \| nx043_stage.py budget-start --config PRIVATE.json`（在 S1 之后；用 S1 的两个策略） | screening 登记带 `budget_drill: true`、上限 1 CNY、1 小时内结束、孪生已发布未校准；开始 |
| B2 | DATA_HOST | probe，socket 只读 | `nx043_stage.py budget-before --config DATA.json` | 演练 world 的合成 Consumer、Run Action 已发布、演练已开始 |
| B3 | APP_HOST | sim_api（演练 world 的 api 凭据），hostssl | `nx043_stage.py budget-runs --config APP.json --from B2` | 两个模拟 Run 依次进入（相隔 `gap_seconds`），各自声明最大花费 1.0 CNY |
| B4 | DATA_HOST | probe，socket 只读 | `nx043_stage.py budget-probe --config DATA.json --from B2 --from B3` | 第 1 个 Run 成功、预留和台账保留；演练以 `budget_exhausted` 停止；第 2 个 Run 在派发时被拒（`budget_exhausted`），没有预留；登记标记与审计可见；real 没有预留 |
| G | 任意 | 无连接 | `nx043_stage.py gaps --config ANY.json` | 没有阻塞项 |

### 预算演练（第 10 项，负责人 2026-10-28 方案 A）

- **证据口径**：预算演练，孪生未校准，结论不代表实验效果。登记记录、审计事件 `nexloop.experiment.budget_drill`、登记返回的 notice 和实验报告都带这一标记（报告的 `budget_drill: true` 与 notice 计入报告摘要）。
- **前提**：
  - AT-046 的 sim world 仍然有效：有合成 Consumer，Run Action 已发布（AT-046 prepare），模拟进程组在运行；
  - 这个 world 的 api 凭据（`roles.sim_api`，与 AT-046 相同）；
  - 一个已发布、未退役、**未校准**的孪生版本（例如 AT-046 publish-twin 发布的合成孪生）。
- **豁免的范围（0366）**：只有登记显式带 `budget_drill: true`，并且同时满足以下条件时，才豁免“孪生已校准”（D9）：
  - stage 为 screening，world 为 sim；
  - 模型上限大于 0、不超过 1 CNY；
  - 结束时间在 1 小时内；
  - 孪生已发布未校准。
  
  任何一条不满足，登记被拒 `budget_drill_not_allowed`。不带标记的 screening 仍然在开始时被拒 `twin_not_calibrated`（第 4 项，S2 照常验证）。
- **派发时的复核（D5）**：某个 sim 或 shadow world 中最新登记的实验已经被护栏以 `budget_exhausted` 停止，且该 world 没有进行中的实验时，新的 Run 模型预留以 NXB01 拒绝。模拟 runtime worker 的派发控制把它报为 `budget_exhausted`，任务失败。
  - 达到或越过上限的那一次预留仍然放行：停止事件要和它一起提交；
  - 此前的预留、实验台账和已完成的 Run 都保留；
  - real 和 test 不受影响；
  - 不新增也不改动任何凭据，影子世界照旧只有 worker 凭据。
- **配置**：`inputs.budget_drill` = `{world, twin {twin_id, version}, ends_in_minutes（默认 45，最多 55）, gap_seconds（默认 30）, run_timeout_seconds（默认 900）}`；APP.json 另加 `roles.sim_api`。私有状态文件照旧在主机之间拷贝。
- **收尾**：演练实验已经由护栏停止，不需要 owner 再停。到结束时间后，护栏不会再改动它。

### 收尾

- O4 之后实验必须是 stopped。O5 如果在超时内没有清零，**不要**重试外发。按“未知结果先查询或对账”的原则处理：在工作台查看对应 intent 的对账状态，由对账流程收口，再重跑 O5。
- 三层派发开关是否关回，由负责人决定，这份执行单不改开关。
- 用 R1 再跑一次就绪检查，确认 `no_conflicting_experiment` 为 ready（没有留下运行中的实验），并且 `shadow_copy_off` 为 ready。

### 合成顾客发言工具（S3 / O2）

`scripts/acceptance/synthetic_customer.py`，与 S6 共用，用法见 `docs/implementation/S6-stage-acceptance.md`“合成交互数据执行单”第 2 步。S3 与 O2 各用一份计划（不同的 `plan_id`），每轮 `wait_reply_seconds` 可设为 0：X7 只需要入站流量，回复与外联由 S4–S8 / O3–O5 观察。


## real 摘要的排除口径（第三轮 S8 之后，release 38）

第三轮 S8 的 `real_side_unchanged` 报告 `control.nexloop_alert_events`、`runtime.nexloop_action_claims`、`runtime.nexloop_retention_sweeps` 有变化。窗口是 S4（09:56Z）到 S8（11:56Z）。口径实现在 `scripts/acceptance/at046_stage.py` 的 `EXCLUDED` 和 `FILTERED` 中，AT-046 与 X7 共用；结果文档会列出被排除的表、被过滤的条件以及各自的理由。测试：`tests/test_at046_stage_pg.py::test_x7_bookkeeping_and_the_owners_own_governance_claims_leave_the_digest_and_nothing_else`。

| 表 | 口径 | 理由 |
|---|---|---|
| `control.nexloop_alert_state`、`runtime.nexloop_orphan_sweeps` | 整表排除（AT-046 已有） | 告警评估器和孤儿清理器按时写入，与实验无关 |
| `control.nexloop_alert_events` | 整表排除 | 告警评估器每个周期都会追加 firing/resolved 事件（NX-030），不论实验是否在运行 |
| `runtime.nexloop_retention_sweeps` | 整表排除 | retention-keeper 每清理一个类别记一行（0129/0140），不论实验是否在运行 |
| `runtime.nexloop_action_claims` | **不整表排除**，只去掉一类行：动作属于 `OWNER_GOVERNANCE_ACTIONS`（策略、实验、报告、影子复制开关、模拟世界、孪生），**并且**认领人是该租户的工作台成员 | 这些是验收步骤中负责人自己的治理动作（例如 S7 的报告读取、影子复制开关），不是模拟对 real 的影响。其余认领一律比较，包括服务或 Run 认领人、任何其他动作、非成员发起的同名动作；测试证明这几类变化仍会被报告 |

调度员归因的结果（release 39，窗口 09:56:00Z–11:56:30Z）是：没有任何变化可以归因到 shadow world。仍被比较的认领，即 3 条 Message.create、6 条 Goal.create、2 条 Commitment.create、1 条 PlanStep.create，全部来自 S3 注入的 real 入站，以及处理这些入站的 real Run。问题出在 S4：取基准时，这些 Run 的认领还没有收尾。所以对这类行的处理方式是改 S4 的安静条件，不是排除它们：

- **S4 安静条件**：同时满足三条：该租户 real 世界没有未结束的 job；没有 active 且租约未过期的 action_claim；world-real 摘要在 `quiet_seconds` 内不再变化。满足后才取基准，并记下取基准的时刻 `taken_at`。超时则 `real_side_quiet` 记为 blocked，并列出未收尾的 Action 名称与计数，此时不取基准。租约已过期的 active 认领不会再变化，所以不阻塞取基准；它们是否为孤儿租约，用归因 SQL 的第 5 段检查。
- **告警证据**：告警事件虽然被排除在摘要之外，probe-after 仍会输出 `real_alerts_in_window`，列出从 `taken_at` 到当前时刻触发或恢复过的告警规则（rule_id、kind、次数）。

归因方法：调度员在 DATA_HOST 上只读运行归因 SQL，见 L2 的交付说明，输出只有计数、动作名、时间桶和认领人类别。如果归因发现窗口中有不属于上表的行，例如服务认领人发起的 `nexloop.service.request`，则不能用排除来消化，必须当作 real 变化处理。
