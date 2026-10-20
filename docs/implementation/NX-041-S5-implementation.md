# NX-041 S5：模拟时钟

依据：`NX-041-design.md` §4、D5、§11 S5、迁移 D。迁移 `0263_nx041_sim_clock.sql`（编号是临时的，只出现在文件名和 catalog 里）。

## 实现

| 内容 | 位置 |
| --- | --- |
| 每个 sim/shadow 世界一个逻辑时钟 `control.nexloop_sim_clocks`：世界登记时随之创建，初始为**暂停**，停在世界的创建时间（D5：模拟世界默认不读墙钟）。时钟值 = 暂停时为 `sim_at`，运行时为 `sim_at + (墙钟 - anchor_at) × rate` | 0263 §1 |
| 只往前走：触发器只接受受治理 Action 打开的修改，每次 revision 加 1，锚点不后退，新时间不早于旧时钟在同一墙钟时刻的值。时钟只能随世界整体删除（S2 purge，世界处于 deleting）离开 | 0263 §1 |
| 每次变更写 `control.nexloop_sim_clock_events`（只追加，每个 revision 一行：操作、起止时间、速率、暂停、步长、操作人、请求号、原因）和一条 `runtime.audit_events` | 0263 §3 |
| 读取：`runtime.nexloop_world_now(tenant, world)`。real/test 直接返回 `clock_timestamp()`，行为不变；sim/shadow 读自己的时钟，找不到时钟就报错，不回退墙钟。`runtime.nexloop_world_wall_at(tenant, world, at)`：一个世界时间对应的墙钟时刻。real/test 原样返回；时钟暂停时为 infinity | 0263 §2 |
| 受治理 Action `nexloop.sim_clock.control` / `control_sim_clock`，subject_rule `human`，只给 owner。动作有 advance（秒，1 秒到 3650 天）、pause、resume、set_rate（大于 0 且不超过 100000）。目标是 real、test、`simulation` 字面值、未登记、他租户或非 active 世界时拒绝。同一请求号重试返回原结果 | 0263 §3；business-actions v32、capabilities、system-catalog-names、workbench-roles v28（slice `sim_worlds`）；`business_actions.py`、`goal_controls.py`、`workbench_actions_http.py`、`workbench_roles.py` |
| 工作队列（work feed）仍按墙钟调度：一个世界的到期时间先换算成墙钟时刻再入队；每次时钟变更后，该世界待处理的 Commitment 和计划条目立即重新到期，按新时钟重算 | 0263 §2、§3 |

## 时间逻辑清单

在 sim/shadow 世界里改读世界时钟的地方（real/test 下 `world_now` 就是 `clock_timestamp()`，`wall_at` 是恒等，所以行为不变）：

| 函数 | 改读的方式 |
| --- | --- |
| `authz.nexloop_commitment_command` | evaluate/settle 的判断时间 `v_now` 改为世界时间，覆盖 due_soon、breached（经 `commitment_target`）、condition_unresolved_at_due、no_due_date。下一阶段的队列时间换算成墙钟 |
| `runtime.nexloop_commitment_touch`、`authz.nexloop_plan_feed_touch` | 传入的世界到期时间换算成墙钟再入队 |
| `runtime.nexloop_plan_mark_due` | 重评到期时间：`min_reassess_seconds` 从世界时间算起；触发记录的 `at` 用世界时间 |
| `runtime.nexloop_outreach_accept` | 业务意图里的日期、审批到期时间 |
| `control.nexloop_governed_outreach_approve`、`runtime.nexloop_outreach_expire_approvals` | 审批到期判断，decided_at、finished_at |
| `authz.nexloop_outreach_command` | 闸门时间（勿扰时段、24 小时与 7 天频率、7 天营销额度），admitted_at、finished_at |
| `runtime.nexloop_outreach_gates` | 只改模型预算：预算是钱，在模拟世界里它的周期仍按墙钟判断 |
| `runtime.nexloop_outreach_metrics` | 各个 7 天窗口以世界时间为终点 |

有意不改，所有世界都用墙钟：

- 租约、队列重试与排序、updated_at；
- 审计与事件的记录时间；
- 凭据与签名的过期；
- 模拟世界自身的到期和 30 天摘要保留（D2：实验在现实中的生命周期）；
- 保留清理（模拟数据随整体删除离开，D2）；
- 成本预算及其周期（D6）；
- 计划自触发的限速窗口。

在模拟世界里目前到达不了（相关表限定 real：0046、0049、0079 的 `check(world='real')`）：

- 回复时限（reply-due 条目来自入站消费者消息）；
- 营销退订窗口；
- 由入站消息产生的联系限制。

这些按 D5 在模拟里禁用，由 S7 的消费者端口再决定。

## 测试

`tests/test_nx041_sim_clock_pg.py`：

- 时钟本身：
  - 每个世界各自一个时钟，创建后暂停；推进一个世界不影响另一个；
  - real/test 等于墙钟，`wall_at` 是恒等；
  - 他租户的世界读不到时钟，也不会回退墙钟。
- 只往前走与审计：
  - 重试幂等，请求号被另一种操作复用时拒绝；
  - real/test/`simulation`/未登记/他租户的世界都拒绝；
  - 0、负数、小数步长和越界速率拒绝；重复暂停拒绝；
  - 直接改表或带标志往回改都拒绝；事件不能改也不能删；
  - 每次变更各有一个事件和一条审计。
- 速率：运行时按速率前进，暂停后不动。
- 推进 30 天：
  - sim 世界里 Commitment 变为 breached，外联审批过期，7 天指标与 24 小时、7 天频率计数清零；
  - 同一时刻 real 世界的同类数据都不变。
- 暂停：墙钟越过到期时间后，sim 世界里 Commitment 和审批都不到期；推进 2 秒后才到期。
- 调度：
  - 暂停时，未来的世界时间在墙钟上为 infinity；速率 3600 时，1 个世界小时约等于 1 墙钟秒；
  - 时钟变更会让待处理条目重新到期，real 条目不动。
- 结构：被改的函数都读世界时钟；外联派发的租约仍按墙钟；整体删除世界时连同时钟和事件一起删除，摘要里有计数。

real/test 回归：被改函数原有的套件不做修改，全部运行，包括 commitments、plan re-evaluation、外联 send/runs/approval/metrics 等。

## 限制

- 模拟里的 effect intent 用列默认值，created_at 是墙钟（`outreach_contacts_since` 的第一部分会读到它），按计划由 S4 的替身执行器在写入时打上世界时间。
- 工作队列按墙钟调度。时钟暂停时，未来到期的条目停在 infinity，靠下一次时钟操作重新到期；模拟驱动（S7）应在每次推进后处理条目。
- 外联意图不可删除（0238 的守卫）。有外联意图的 sim 世界整体删除时会报告 `blocked`，属于 S2 已记录的限制。
- 外联：按调度员定案（AT-046 优先），模拟世界里的外联只走 S4 的替身（`aa_nx041_real_effect_world` 拒绝模拟世界写外联意图和 effect intent）。上表中外联函数的改读保留，在 real/test 下是恒等，在模拟世界里目前到达不了；原先给外联意图和审批打世界时间的 stamp 触发器已撤掉（S5 审查 4.1）。等 NX-042 接入孪生时，再按替身台账接上世界时间。

## 审查修复（S5 审查报告）

- 2.5：0263 为迁移前已登记、状态为 active 或 deleting 的世界补建时钟（暂停在创建时间，revision 1，并写 `created` 事件）。回填可重复执行。测试做法：先建世界并删掉它的时钟，执行迁移里的回填段，确认时钟可用。
- 4.1：撤掉外联表上的时间戳触发器，见上文“限制”。
- 1.2：守护测试 `test_the_clock_migration_changes_only_time_expressions_of_the_latest_definitions`。对 0263 重定义的 10 个函数，先把时间表达式还原，再与迁移目录中此前最后一次定义的函数体逐字比较；同时检查此后的迁移在重定义这些函数时仍读世界时钟。任何分支先改了这些函数，这条测试都会失败，届时在集成头重新导出函数体，再套用同样的替换规则。
- 墙钟回拨：墙钟早于锚点时，世界时间停在锚点（不后退）；Action 的新锚点取 `greatest(墙钟, 旧锚点)`，因此不会被守卫拒绝。
- 高倍速溢出：时钟最多走到 9999-01-01，到达后停住，不报错；advance 超过上界时拒绝（`out_of_range`）。

