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
- **范围**：online 阶段仍是 `online_not_available`，验收只覆盖 screening 和 shadow 两段。online 需要通过校准的孪生（NX-042 T5）和 X4 接线，列为后续项。

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
| 10 | 实验预算用尽即阻断新消耗 | D5、§9 建议 3 | gaps：`experiment_budget`（blocked） | `test_nx043_guard_wiring_pg` | **阻塞**：需要实验世界里的模拟 Run（AT-046 的 run 链路） |
| 11 | 分配：按消费者，会话聚类同组，对照组至少一半 | D2、AT-048 | gaps：`allocation`（blocked） | `test_nx043_allocation_pg` | **阻塞**：X4 还没接到任何服务入口 |
| 12 | 成熟窗口；窗口没到的样本不计；unknown 不算成功；样本不足不出结论 | D6、§6、§9 建议 4 | report：`experiment_result_reports_nothing_below_the_minimum` | `test_nx043_reports_pg`、`test_nx043_pooled_pg` | stage 只能验“不出结论”；按成熟窗口出统计需要 online 的分配 |
| 13 | 报告只含聚合数，冻结后不可修改，不能反推个人；只有 owner 能取 | §6、X3 4.x、报告 Action | report：`shadow_report_aggregates_only` | `test_nx043_report_action_pg` | stage 可验 |
| 14 | real 没有任何变化 | AT-046、§8 | probe-after：`real_side_unchanged`（每张带 world 列的表的 world-real 行摘要，外加 real 服务凭据和授权事实） | 本地测试 | stage 可验 |
| 15 | 结论不写成因果 | §6 | report（报告的 statement 字段） | `test_nx043_reports_pg` | 本地覆盖 |
| 17 | 胜出策略不自动推广；推广由负责人决定，作为新版本走正式发布链 | D8 | —（stage 上没有操作） | `test_no_path_from_a_report_or_the_guard_to_a_strategy_publication`：报告、报告 Action、护栏的函数体里，没有写策略版本或策略事件的语句，也没有调用任何发布 | 本地覆盖 |
| 16 | online 阶段 | §2 | gaps：`online_stage`（blocked） | — | **后续**：等孪生校准（NX-042 T5）和 X4 接线 |

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
10. `gaps`：阻塞项，由调度员登记。

## 审查（L6 `nx043-x7-review.md`）的处理

- **2.1（确认级）**：第 7 项按 AT-046 的拆法分成 shadow-keys（DATA_HOST）、decide（APP_HOST）、probe-after（DATA_HOST）三步，最后 merge。decide 不再需要探针。
- **3.1**：nonce 方案写成了共用函数，放在 `scripts/acceptance/s6_common.py`：
  - `Result(..., nonce=...)` 会在结果里写 `run_nonce`；没有设置时不写，所以其他脚本的输出不受影响；
  - `new_nonce()` 生成一个 nonce；
  - `load_chain(paths, prefix=, order=, nonce=)` 核对同一个 nonce、每步只出现一次、只包含这条链里的步骤、执行时间按链的顺序，返回合并后的步骤和阻塞项。

  AT-046 可以直接复用这套函数，由 L2 接入。
- **1.1**：清单新增第 17 项 D8，并补了一条结构测试。
- **3.2、4.1**：见上面的执行顺序第 4 步和第 7 步。
- **2.2（可选）**：`shadow_sends_nothing` 的 0 是由效应表的 CHECK 从结构上保证的。真正能说明决策只停在模拟里的，是新增的 `shadow_decisions_landed`：决策都在影子世界里，并且只有替身意图。

## 发现

- **X1 的“像真实内容”检查会拒绝带编号的假设文本**，比如 `NX-043` 会被当成会员号（字母-3 位以上数字）。工具里的假设文本因此不写编号，生成的 id 也只用字母。owner 自己写登记时也要避开这类写法，建议在工作台的提示里说明。
- **`allocation_seed` 不能超过 2147483647**，工具里取哈希的前 7 位十六进制。
