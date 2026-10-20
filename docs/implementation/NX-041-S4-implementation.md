# NX-041 S4：Action 替身、模拟台账与不出网的执行器

依据：`NX-041-design.md` §3、D4、D11、D12、§11 S4；AT-046。迁移 `0261_nx041_stand_ins.sql`（编号是临时的，只出现在文件名和 catalog 里）。

## 实现

| 内容 | 位置 |
| --- | --- |
| 替身目录 `control.nexloop_stand_ins`（D11）：服务请求走 HTTP provider、服务请求走本地 JSON 投递、优惠发放、外联发送，各一个替身，版本 1.0.0。没有替身的效应在模拟世界中不可用（fail closed）。目录只能追加 | 0261 §1 |
| 行为配置 `control.nexloop_stand_in_configs`（D12）：没有配置时默认全部成功、零延迟；失败率、结果不明比例、延迟区间和失败码必须显式写入，写入后不能修改。写入函数 `runtime.nexloop_stand_in_configure` 不授予任何角色，由可信配置或 S3 的实验冻结输入调用 | 0261 §2 |
| 模拟台账与 real 台账在物理上分开：`runtime.nexloop_sim_effect_intents`、`runtime.nexloop_sim_effect_receipts`。world 只能是 `sim:`/`shadow:`，`data_mode='simulation'`；回执要求 `simulated=true`、`output_kind='simulated_output'`，并带替身版本。只允许追加，回执上只能更新重复执行次数；整体删除（S2）通过 erasure scope 清掉这个 world 的行 | 0261 §3 |
| 入口：`authz.nexloop_sim_effect_submit`（api/domain_worker 调用）、`_execute`、`_pending`（action_worker 调用）、`_query`（按意图查询，语义同 `GET /v1/effects/<intent_id>`）。只接受已登记且 active 的 sim/shadow 世界，凭据必须是这个世界的那一个凭据（S2 D10）；real、test 在查凭据之前就被拒绝（`real_world_refused`） | 0261 §4 |
| 确定性：回执由（替身版本, world, intent_id, 模拟时钟）和该世界的配置唯一决定；对同一意图重复执行，返回同一张回执，`executions` 加一，`repeated=true` | 0261 §4 |
| real 效应路径拒绝模拟世界：effect intents、outbox、attempts、observations、incentive issues、outreach sends、outbound messages、real dispatch switches、effect contexts 上加了 `aa_nx041_real_effect_world` 触发器，按名字排序最先触发。sim/shadow 世界的行在第一次写入时就被拒绝，所以真实执行器永远领不到模拟世界的意图 | 0261 §5 |
| 执行器 `nexloop_eios/stand_in_executor.py`（D4）：只接收数据库 URL 文件、该世界的服务凭据文件和一个 sim/shadow world；没有 provider 配置，没有路由文件，没有通道凭据，也不导入任何网络库；只调用上面的 SQL 入口。`StandInReceipt` 与 `EffectProviderReceipt` 同形，另带显式的 `simulated`、`stand_in_version`、`output_kind` | Python |

## 与设计稿的差异

- 设计稿 §7 的处置表曾打算把模拟意图记在 `runtime.nexloop_effect_intents` 里，“只由替身执行器领取”。这里改为**完全不进入** real 效应台账：模拟意图和回执写在自己的两张表里，real 效应表对 sim/shadow 一律拒绝写入。这样更严格，real 的对账、成本和指标在物理上就读不到模拟记录。测试会检查只有 S4 自己的函数引用这两张表。
- 外联在模拟世界里不走 `authz.nexloop_outreach_command`（它的发送行写在 `runtime.nexloop_outreach_sends`，现在拒绝 sim world），而是作为 `nexloop.outreach.send` 的替身提交。外联闸门在模拟世界中如何照常执行（§3.4），留给接入孪生时（NX-042）处理。
- 替身输出不能成为事实：回执的 `output_kind` 约束为 `simulated_output`。Claim 只能来自消息或其他 Claim（0066 的约束），不存在从回执派生 Claim 的路径。模拟世界的 Claim 投影 mode 为 simulation（S1）。

## 测试

`tests/test_nx041_stand_ins_pg.py`：
- 目录覆盖 D11 的全部效应；
- 回执带模拟标记、结果确定，重复执行时计数；
- 台账约束拒绝 simulated=false、事实类输出和 real 数据模式；
- D12 配置的校验、冻结、失败码和结果不明；
- real、test 和不合法的 world 调用替身被拒绝；sim A 和 shadow B 互不相通；没有替身的效应 fail closed；删除世界后凭据失效、台账清空；
- real 效应表拒绝 sim/shadow 行；真实派发开关对模拟世界关闭在 world 层；
- AT-046 端到端：所有真实派发开关打开时，sim 凭据在 7 个 real 效应入口全部被拒；同一凭据经替身完成派发；real 效应表行数不变；只有 S4 的函数读取模拟台账；
- 执行器不导入网络库，`--world` 只接受 sim/shadow，也不接受 provider 参数。

## 限制

- 替身执行器复用 `nexloop_action_worker` 数据库角色（与真实 effect worker 是同一个角色），隔离依靠 world 和凭据，没有新建数据库角色。
- 部署编排里还没有替身执行器进程（compose/systemd），这一步留给模拟世界上线时补。
