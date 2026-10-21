# NX-043 X4 接线：strategy_for 进入外联决策和 Context 组装

调度员 2026-10-21 指派。迁移 `0304_nx043_x4_wiring.sql`，编号是临时的，只出现在文件名和 catalog 中。

## 生效条件

只有同时满足以下条件，才使用候选策略：

- 实验 `started`，`stage=online`，`world=real`，并且 `ends_at` 还没到；
- 这个 Consumer 已被分配到候选组；
- 候选策略仍是 `published`；
- 如果登记了孪生，孪生仍可选；
- 这个 Consumer 不在擦除中。

否则一律回退到当前策略：对照组、被排除（erasing、`in_same_comparison` 等，arm 为空）、策略已作废、实验已停止、其他 world。

负责人 2026-10-21：测试阶段放开 online，D9 校准暂不作为前置（0309）：start 不再以 `online_not_available` 拒绝 online，接线随实验 started 立即生效。real 派发开关只读不写，也没有新增任何外发路径；开关关着时外发仍被挡住（`real_dispatch` 门）。

## 改动

| 位置 | 改动 |
|---|---|
| `runtime.nexloop_experiment_levers` | 新增，只读，不分配。返回 `{experiment_id, strategy_id, version, levers}`，不满足条件时返回 null |
| `runtime.nexloop_experiment_online_running` | 新增，只读预检，判断这个租户有没有正在运行的 online 实验 |
| 入站触发器 `nx043_experiment_allocate` | real 入站消息（record 中没有 `direction`）触发首次分配。cluster 键由服务端从会话中取出，就是 Consumer 本身。先在子事务之外读开关，再在保存点中执行，lock_timeout 为 200 ms。失败只计入 `accounting_failures`（`allocation:inbound`），消息本身照常写入 |
| `runtime.nexloop_operator_persona_section`（复制自 0228） | 候选组在**已有当前 persona** 时，改用策略指定的 persona 版本（`persona_ref`、`version`），`source` 保持当前取值（owner_assign、rule、default）。没有当前 persona 时不会因为实验而多出 persona 段。版本作废后回到当前 persona。persona 的结算和事件不变 |
| `runtime.nexloop_outreach_gates`（复制自 0263） | 候选组的 `outreach_purposes` 杠杆**只能关闭**已登记、非营销、无需审批、无需同意的 purpose（`enabled and 杠杆`），不能重新打开租户已关闭的 purpose（L6 复核 3.1，调度员 2026-10-21），并附加一个 `experiment_strategy` 门条目（`strategy:<id>@<version>`）。营销 purpose 永远不受影响 |
| `runtime.nexloop_outreach_candidates`（复制自 0232） | 候选组的 service_followup 在 `follow_up_delay_hours`（1–336 小时）之后才成为候选；其他人保持 24 小时窗口。预过滤放宽到 336 小时 |
| `runtime.nexloop_outreach_accept`（复制自 0263） | 在受理时把本次决策使用的策略 `{experiment_id, strategy_id, version}` 冻结到 intent 的新列 `experiment_strategy` |
| 外联发送触发器 `nx043_experiment_strategy_evidence` | 从 `experiment_strategy` 列读取决策时的值，追加审计事件 `nexloop.experiment.strategy_applied`（X6 的依据），插入时不再重新查询杠杆，因此决策之后策略被作废也不会改变记录。审计写入失败不会让发送失败，但会计入 `accounting_failures`（`evidence:outreach_send`）（L6 复核 4.1） |
| `packages/contracts/published.json` 和 `tests/test_published_contracts.py` | 记录已发布契约的 checksum（release 28，13087064），目前收录 `context-pack-v6.schema.json` = 9f50b585；文件字节变化即测试失败 |

四处函数的重定义都已登记在 `tests/test_function_copy_guard.py`（`nx043_x4_wiring`）。

## 测试

`tests/test_nx043_x4_wiring_pg.py` 覆盖以下内容：

- 只有候选组拿到杠杆；
- 候选组的 persona 是策略版本、source 不变，persona 作废后回退；没有当前 persona 时不新增 persona 段；
- purpose 杠杆只对候选组生效，营销 purpose 不受影响；
- 擦除、策略作废、实验停止、`in_same_comparison` 都会回退；
- 入站消息触发首次分配，失败被隔离；
- 外联发送写入审计；
- real 派发开关不变，新函数没有对应用角色授权。


## 限制

- 跟进时间的杠杆**没有**单独的 PG 测试，因为构造 service_followup 候选需要完整的 effect 执行夹具。这部分逻辑由 copy-guard 锁定改动范围，并由现有的 outreach candidates 测试做回归。
- 测试中直接写入 started 事件构造 online 实验，与 X4 测试的做法相同；负责人 2026-10-21 放开 online 后，经受理 Action 开始的测试见 `test_nx043_online_open_pg`。

## 为什么不扩 context-pack v6（调度员 2026-10-21，方案 B）

- context-pack v6 已经发布：v0.1.0 tag 中包含它，release 28（13087064）记录的 checksum 是 9f50b585。已发布的契约字节不能再改。
- 所以 pack 中不带实验来源。实验来源记录在三处：allocation（组别）、intent 的 `experiment_strategy` 列（决策时的策略版本）、审计 `nexloop.experiment.strategy_applied`。统计和追溯都从这三处取。
- 如果以后确实要让 pack 带上实验来源，只能新建 context-pack v7，并作为独立任务来做。`nexloop.context-pack.v6` 写死在约 20 个已发布的 SQL 函数（bind、校验、取回）和 Host 配置中，v7 需要重定义整条链路。
- 这样一来，Host 与迁移之间也没有上线顺序的约束了：Host 不需要任何改动。
