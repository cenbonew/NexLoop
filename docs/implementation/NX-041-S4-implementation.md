# NX-041 S4：Action 替身、模拟台账与不出网的执行器

依据：`NX-041-design.md` §3、D4、D11、D12、§11 S4；AT-046。迁移 `0262_nx041_stand_ins.sql`（编号是临时的，只出现在文件名和 catalog 里）。

## 实现

| 内容 | 位置 |
| --- | --- |
| 替身目录 `control.nexloop_stand_ins`（D11）：服务请求走 HTTP provider、服务请求走本地 JSON 投递、优惠发放、外联发送，各一个替身，版本 1.0.0。没有替身的效应在模拟世界中不可用（fail closed）。目录只能追加 | 0262 §1 |
| 行为配置 `control.nexloop_stand_in_configs`（D12）：没有配置时默认全部成功、零延迟；失败率、结果不明比例、延迟区间和失败码必须显式写入，写入后不能修改。写入函数 `runtime.nexloop_stand_in_configure` 不授予任何角色，由可信配置或 S3 的实验冻结输入调用 | 0262 §2 |
| 模拟台账与 real 台账在物理上分开：`runtime.nexloop_sim_effect_intents`、`runtime.nexloop_sim_effect_receipts`。world 只能是 `sim:`/`shadow:`，`data_mode='simulation'`；回执要求 `simulated=true`、`output_kind='simulated_output'`，并带替身版本。只允许追加，回执上只能更新重复执行次数；整体删除（S2）通过 erasure scope 清掉这个 world 的行 | 0262 §3 |
| 入口：`authz.nexloop_sim_effect_submit`（api/domain_worker 调用）、`_execute`、`_pending`（action_worker 调用）、`_query`（按意图查询，语义同 `GET /v1/effects/<intent_id>`）。只接受已登记且 active 的 sim/shadow 世界，凭据必须是这个世界的那一个凭据（S2 D10）；real、test 在查凭据之前就被拒绝（`real_world_refused`） | 0262 §4 |
| 确定性：回执由（替身版本, world, intent_id, 模拟时钟）和该世界的配置唯一决定；对同一意图重复执行，返回同一张回执，`executions` 加一，`repeated=true` | 0262 §4 |
| real 效应路径拒绝模拟世界：effect intents、outbox、attempts、observations、incentive issues、outreach sends、outbound messages、real dispatch switches、effect contexts 上加了 CHECK 约束 `nx041_real_effect_world`（审查 1.1 之前是 `aa_` 触发器，已改；外发表清单见下文审查修复）。sim/shadow 世界的行在第一次写入时就被拒绝，所以真实执行器永远领不到模拟世界的意图 | 0262 §5 |
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

- 部署编排里还没有替身执行器进程（compose/systemd），这一步留给模拟世界上线时补。

## 审查修复（L6 审查 `nx041-s4-review.md`）

0262 还没有发布，以下改动都直接在 0262 里修改。

| 编号 | 修复 |
| --- | --- |
| 1.1 | guard 从 `aa_` 触发器改成 CHECK 约束 `nx041_real_effect_world`，迁移时直接 validate。CHECK 检查的是所有 BEFORE 触发器执行完、最终写入的那一行；replica 模式也绕不过去，只有执行 DDL 才能去掉。测试覆盖了两种情况：一个排在后面、设成 enable always 的触发器把 world 改写成 sim；以及 replica 模式。两种都被拒绝。另有阳性对照：同样的行写 real 可以通过。 |
| 1.3 | 新增声明表 `control.nexloop_real_effect_tables`。加 guard 的有 20 张：原来的 9 张；审查列出的 message_outbox、relay_handoffs/claims/attempts、data_exports、outbound_delivery_events、local_delivery_recoveries；守护测试又查出的 relay_enrollments、relay_consumer_cursors、message_relay_leases、effect_dispatch_controls。不加的 3 张都写了理由：`runtime.nexloop_outbox` 是内部 job 交接，job 要对外只能经过受治理的效应意图，而效应意图已加 guard；`dispatch_refusals` 只记录拒绝；`task_dispatch_controls` 是内部 job 租约。守护测试：凡是带 world 列、名字匹配 outbox、relay、deliver、export、send、dispatch、outbound 或效应台账的表，都必须登记在声明表里。 |
| 4.1 / 4.2 | 意图里的 `sim_clock` 改由服务端取 `runtime.nexloop_world_now(tenant, world)`（S5 提供），payload 里出现 `sim_clock` 直接拒绝。每个意图在提交时由服务端生成随机盐 `outcome_salt`，存在意图行里，回执哈希包含这个盐。重复执行和查询时用的是落库的值，所以结果不变。测试：客户端按旧公式（版本、world、intent、时钟）预测结果，至少有一次预测错；两种结果都出现；重复执行时回执不变。 |
| 3.1 | 新增角色 `nexloop_stand_in_worker`（login，没有任何特权），只授予 `sim_effect_pending`、`sim_effect_execute`、`sim_effect_query` 的执行权限。`nexloop_action_worker` 不再能调用替身入口。测试断言：这个角色能执行的 security definer 函数，只有这 3 个入口，外加对 public 开放的 `runtime.nexloop_erasure_permits`（只对 owner 当前事务返回布尔值）；它调用 `real_dispatch_summary` 会被拒绝。 |
| 2.2 | 白名单测试扩展到以下几项：所有应用角色（包括新角色）对 4 张台账表都没有权限；视图、规则和 RLS 策略中都不出现台账表名；引用台账的函数只有 S4 自己的；动态枚举 world 表的函数只有 S2 的 snapshot、purge 和 targets，`targets` 是 S2 修复合入后才有的。 |
| 5.2 | 评估结论：不需要新约束，已经用守护测试固定下来。理由如下：Claim 必须挂在会话上（`conversation_id` 不能为空，并有外键），而会话表的 CHECK 是 `world='real'`，所以 sim 或 shadow world 里根本不会有 Claim；Claim 的 epistemic_kind 里没有 fact 类，`verified_fact` 是刻意不提供的；Commitment 的「已兑现」只能由效应意图推进，而效应意图表已加 guard。S1 对 Claim 投影出的 mode 为 simulation，只在以后放开 sim 会话时才会用到；放开会话属于 S7 的模拟端口，届时需要重新评估。 |
| 6 | 回执外键没有设成 cascade，所以 S2 的 purge 要多跑一轮才能删完。这次没有改，结果是正确的；S2 修复里，单表失败只记为 blocked、删除继续往下走，下一轮就能删完。 |

## 审查修复二（复核 `nx041-s4-fix-verify.md` 的余项）

0262 仍未发布，以下改动直接在 0262 中修改；catalog 中 0262 的校验和随之更新。

| 编号 | 修复 |
| --- | --- |
| 1.3 余项：按结构识别外发表 | 守护测试不再只按表名匹配。凡是带 world 列、并被“派发、relay、执行器类”函数写入的表，都必须在 `control.nexloop_real_effect_tables` 中登记，或者加上 guard，或者写明不加的理由。<br>“派发、relay、执行器类”函数按函数名识别，名字中含以下片段之一即算：dispatch、relay、execut、deliver、effect、outreach、outbound、incentive、export、send、lease、webhook、notif、mail；测试解析这些函数体中带 schema 前缀的 insert/update/delete 目标表。<br>表名兜底匹配增加了 webhook、notif、mail、push、callback、channel。<br>这次扫描新找出 23 张表：<br>• 加了 guard 的 11 张：effect_control_ledger、effect_plan_bindings、effect_catalog_bindings、effect_events、effect_provider_bindings、effect_requery_orders、effect_determinations、message_routes、outreach_approvals、reply_escalations、restore_channels；<br>• 不加 guard、写明理由的 12 张：run_credentials、work_feed、budget_consumption、budget_settlements、两张效应类别配置表、claims、definition_aliases、conversations、conversation_messages、commitment_evidence、human_requests；<br>• 另外 sim_effect_receipts 是模拟台账本身，一并登记，不加 guard。<br>阳性对照：结构扫描必须能找到 effect_outbox、outbound_messages、outreach_sends 这几张表。 |
| 1.3 余项：job outbox | 新增守护测试，确认 `runtime.nexloop_outbox` 只有一个写入方 `authz.nexloop_queue_command`（包括它重命名后保留的旧版本 `_before_*`）。该写入方不写任何已加 guard 的外发表。outbox 的列与登记的完全一致，没有目的地、地址或请求体之类的列。<br>Python 侧：既从 durable queue 取任务、又持有网络客户端的模块，必须与登记清单完全一致（目前只有 `runtime_dispatch.py`，它访问的是 Agent Host 的内部 API）。 |
| 2.2 余项：角色清单 | 台账权限检查不再用写死的角色清单，改为遍历 `pg_roles` 中所有 `nexloop%` 角色，只排除 owner 和超级用户；同时检查 PUBLIC 对台账表没有 ACL 项。检查的权限扩展为 select、insert、update、delete、truncate、references、trigger。 |
| 2.2 余项：动态 SQL 的边界 | 测试只能检查“函数源码里写出了台账表名”的读取。对于自己枚举系统目录（pg_class、pg_attribute、pg_tables、information_schema 的 tables/columns）、再用 format 拼出表名读取的函数，测试看不出它读不读台账。<br>因此新增一条规则：所有这类函数必须在测试的 `CATALOG_ENUMERATORS` 中登记，并写明读取什么。目前登记的有：read_memo_table_ok、sim_world_has_tenant、sim_world_tables，以及 S2 的 targets、snapshot、purge。新增这类函数而不登记，测试就会失败。 |
| N2 | 迁移在创建 `nexloop_stand_in_worker` 之后，会把已存在的同名角色校正成规定的形态：<br>• 属性设为 login、noinherit、nocreatedb、nocreaterole、nosuperuser、noreplication、nobypassrls；<br>• 撤销它的所有角色成员关系；<br>• 撤销它在 authz、control、ontology、runtime、extensions、public 中原有的表、序列、函数和 schema 权限，然后再重新授予三个入口函数。<br>口令不改。<br>测试：先手工建一个同名角色（nologin、inherit、createdb、bypassrls，并加入 pg_read_all_data），执行 bootstrap 后断言属性正确、没有成员关系、只能执行这三个入口。 |
| N4 | 同一个 sim world 中，同一个 `business_digest` 只能有一个未终结的意图。“未终结”指：还没有回执，或者回执是 unknown（unknown 不算终结，应先查询）。<br>在此期间换一个 intent_id 再提交会报 `stand-in: business_intent_open`；同一个 intent_id 重试仍可接受。<br>判断按 `(tenant, world, business_digest)` 取 advisory lock，串行执行。<br>一旦回执为 fulfilled 或 failed，再提交会生成一条新意图，与原来的意图并列保存，所以每一次尝试都留在账里。<br>测试：重试、换 intent_id 被拒、另一笔业务和另一个 world 不受影响、unknown 仍被拒、终结后可再提交、两个连接并发提交。 |

剩余边界：结果为 failed 之后，提交方仍可以针对同一笔业务再提交，这属于正当重试，每次尝试都记在账里。实验报告应按 business_digest 统计重试次数，而不是只看最后一次的结果。

## 部署说明（由调度员安排，本次没有改 deploy/）

- 替身执行器使用独立角色 `nexloop_stand_in_worker` 连接数据库，需要单独的 DSN 文件，不能用 action_worker 的口令。
- 这个角色由迁移创建，不带口令。部署时需要以下两种做法之一：
  - 按 stage 的做法为它生成口令，存入 Keychain，并写入它自己的 DSN 文件；
  - 在 pg_hba 中为它加一行本地 peer 或证书认证。
- 不要把这个 DSN 放进 action-worker 的服务目录。
