# NX-043 X3：影子世界

- **依据**：NX-043 设计稿 §2、§4、§6、§8、§9 和 D7；NX-041 D7、S2（世界登记与整体删除）、S4（Action 替身）。
- **起点**：L5 的 `l5/nx043-x1`（c11313b9）。
- **迁移**：`0288_nx043_shadow_world.sql`（集成时由临时编号 0292 重排，接在 0287 之后）。临时编号前面有空号，所以 `test_bootstrap.py` 的计数断言在重排之前会失败，这与 X1 的情况相同。
- **契约**：没有新增契约。报告用 `schema: nexloop-shadow-report/1` 自描述；正式契约等 X6 的报告切片统一处理。

## 1. 复制开关（D7，默认关闭）

- 表 `control.nexloop_shadow_copy_events` 只能追加，以租户最新的一条事件为准。如果没有任何事件，或者最新事件指向的 shadow 世界已经不是 active（比如被删除或过期），就不复制（`runtime.nexloop_shadow_copy_target`）。
- 新增受治理 Action `nexloop.shadow_copy.set` / `set_shadow_copy`：
  - human 类，只授予 owner，只能从 real 发起，带审计；
  - 同一请求重放返回原结果，同一请求换了内容报 `request_reused`；
  - 只接受同租户、已登记且 active 的 `shadow:<uuid>` 世界，否则报 `world_not_active`；sim 世界和 real 都报 `invalid`。
- **清单同步**：
  - business-actions（manifest_version +1）、capabilities（hash 由 `write_hashes` 生成）、system-catalog-names、workbench-roles（owner，`experiments` 分组，版本 +1）；
  - `PROFILES`、`goal_controls`（映射和方法）、`workbench_actions_http`（操作和错误前缀 `shadow copy`）、`workbench_roles`（owner 专用）；
  - `commercial_fixture`、`test_business_actions`、`test_governed_entry_pg`；
  - register 计数从 99 改为 100。

## 2. 入站事件的脱敏复制

- real 的入站会话消息（direction 为 inbound）写入时，如果开关开着，触发器 `nx043_shadow_copy` 在目标 shadow 世界写一行 `runtime.nexloop_shadow_events`，只包含：
  - `event_key = sha256(world|message_id)`；
  - `content_hash = sha256(world|payload_digest)`；
  - 复制的整点时间（`copied_hour`）；
  - `data_mode` 为 simulation，`origin` 为 shadow_copy。

  不复制原文，也不复制 message、conversation、Consumer 的 id；出站消息不复制。
- 从这条消息抽取出的 real Claim，触发器 `nx043_shadow_copy_claim` 只把 Claim 类型（epistemic_kind）记到 `runtime.nexloop_shadow_event_claims`。
- 两个哈希都带上 world，所以一个 shadow 世界的键拿到别的世界没有用，也不能直接和 real 的 digest 对上。
- 复制和 real 的写入在同一个事务里（实时复制，设计稿已说明这会增加负载）。开关默认关闭，关闭时触发器只做一次开关查询就返回。

## 3. 影子决策只记录、不发送

- `authz.nexloop_shadow_decision_record(digest, world, decision)` 只授予 `nexloop_sim_worker`，并且要求它在 shadow 世界里（S6c 的 app_role）。real 和 test 在任何查询之前就被 `sim_effect_tenant` 拒绝，用别的世界的凭据也被拒。
- 决策取值为 reply、none、follow_up、outreach，并要求策略版本处于已发布状态（`strategy_not_published`）。同一事件、同一策略版本重放返回原结果，同一请求换了决策报 `decision_reused`。
- follow_up 和 outreach 走 S4 的替身（`authz.nexloop_sim_effect_submit`，`nexloop.outreach.send` / `outreach_outbox`，origin 为 `replay`），得到模拟回执，什么都不发送。
- reply 只记录：影子世界里没有会话，也没有对应的替身。
- **负例**：
  - sim worker 调用 real 的外发入口，没有权限；
  - shadow 世界往 real 效应表写行，被 NX-041 的 CHECK `nx041_real_effect_world` 拒绝；
  - real 的效应意图表和外发消息表前后行数不变。

## 4. 差异报告（聚合、最小样本、冻结）

- `runtime.nexloop_shadow_report_freeze(tenant, world, strategy, version, since, until, by)` 只有 owner 能调用，没有授予任何应用角色。
- **对比方式**：对窗口内该策略版本的每个影子决策，按复制时同样的键找到对应的 real 入站消息。real 的实际决策这样判定：同一会话的下一条消息是 outbound，记为 reply；否则记为 none。
- **报告内容**：
  - 单元格 `(影子决策, real 决策)`，每格给出事件数和不同 Consumer 数；
  - 某格不同 Consumer 少于 `runtime.nexloop_shadow_minimum_consumers()`（30，即 NX-042 D3）就不输出，只计入 `suppressed_cells`；
  - 总计和一致率也要不少于 30 个 Consumer 才输出；
  - 另有 `unmatched_events`（对不上 real 消息的事件数）。
- id 只在函数内部的计算中用到，报告里不出现任何 Consumer、会话或消息 id。
- **冻结**：报告写进 `runtime.nexloop_shadow_reports`，只能追加。同一窗口、同一策略版本再次调用，返回已冻结的那份。
- 报告写明结论范围：只是影子世界中的观察，不是真实效应，也不是因果估计（设计稿 §6）。

## 5. 删除与隔离

- 五张表都有 world 列，只接受 shadow 世界，全部挂 `sim_ledger_guard`（只能追加；只有 S2 的整体删除能删）。删除 shadow 世界后，这五张表和替身意图在该世界都是 0 行，开关指向的世界不再 active，之后的入站消息不再复制。
- `tests/test_nx041_sim_roles_pg.py` 的 WHITELIST 里，`nexloop_sim_worker` 加入了 `authz.nexloop_shadow_decision_record`。

## 6. 范围外与后续

- “是否触碰护栏”（例如在勿扰时段想发消息）由 X5 的停止规则和护栏统计接入；X3 只记录决策。
- 谁来产生影子决策（某个策略在影子世界里运行 Agent）属于 X4、X6 的运行编排；X3 提供的是记录入口。
- 报告目前由 owner 函数生成；如果需要放进工作台，由 X6 加读取入口。

## 测试（`tests/test_nx043_shadow_pg.py`）

- 开关关闭时零复制；只有 owner 能操作，只能从 real 发起；非 shadow 世界、其他租户的世界都被拒；重放；关闭后不再复制。
- 复制的内容只有哈希和 Claim 类型，表里查不到原文、message、conversation、Consumer 的 id、payload digest、topic 和引文；出站消息和其他租户的消息不复制。
- 影子决策：reply 只记录；outreach 走替身，得到模拟回执；重放；`decision_reused`、`event_unknown`、`strategy_not_published`；real、test、其他世界、其他角色都被拒；real 外发入口和效应表的 CHECK 都拒绝 shadow；real 效应表行数不变。
- 报告：40 个 Consumer 中 32 个在 real 被回复。(reply, reply) 这一格有 32 个 Consumer，输出；(reply, none) 只有 8 个，被抑制；总计的一致率为 0.8。报告里没有任何 id；冻结后不能修改，重复调用得到同一 digest。总数不足 30 的世界，单元格和总计都不输出。
- 删除 shadow 世界：五张表和替身意图零残留，之后不再复制。

## L6 审查修复（`nx043-x3-review.md`）

| 编号 | 处理 |
| --- | --- |
| 1.1 确认：删除目标 world 后，复制悄悄回到旧 world | 两项都做了：(1) 开关账本改名为 `target_world`，不再有 world 列，因此不是 S2 整体删除的目标，不会随 world 一起删除；(2) 在 `control.nexloop_sim_worlds` 上加触发器 `nx043_shadow_world_left`：shadow 世界离开 active（deleting 或 deleted）时，如果租户最新的开关事件正指向它并且是开启的，就自动追加一条关闭事件（`changed_by` 为 `system:sim_world_<状态>`）。补了负例：先开 A、再开 B、删除 B。账本依次是 A 开、B 开、B 关，`copy_target` 返回 null，之后的入站消息不会复制到 A。 |
| 1.2 复制失败连带真实消息写入失败 | 选用 **savepoint**，不用 outbox。理由：D7 要求实时复制；用 outbox 要多一个后台复制进程，还要处理积压和顺序。PL/pgSQL 的 `begin … exception` 本身就是一个 savepoint：复制里出任何错，只回滚复制这部分，真实消息照常写入。失败按（租户、整点、SQLSTATE）计数写入 `runtime.nexloop_shadow_copy_failures`，只记计数，不记消息或 id；计数本身也放在一个 savepoint 里，尽力而为。Claim 的复制触发器做了同样处理。补了测试：用一个会报错的触发器让复制失败，真实消息和 Claim 都写入了，失败计数为 1；拿掉这个触发器后复制恢复。 |
| 1.3 缺少 direction 的消息被当成 inbound | 只复制 `direction = 'inbound'` 的消息；缺少 direction 的消息不复制。报告里 real 入站消息的判定也改成同一规则。补了测试。 |
| 4.1 多份报告可以做差分 | 按 NX-042 T5 的做法：报告窗口只能是整 UTC 天，否则报 `window_not_on_grid`；同一窗口只出一份（冻结，重复调用返回同一份）。少于 30 个 Consumer 的格子既不输出，也不给具体数：`suppressed_cells` 只写 `none` 或 `some`。 |
| 4.2 两个附带计数没有设门槛 | `suppressed_cells` 和 `unmatched_events` 都只写 `none` 或 `some`。 |
| 4.3 报告没有受治理入口 | 这次没做，调度员的修复清单里没有这一项。报告函数仍然只有 owner 能调用；如果需要，由 X6 或后续切片包装成 owner 专用的受治理 Action。 |
| 合并 | X3 和 X4 都用了 0292 这个临时编号，集成时要重排。 |

## L6 复核（`nx043-x3-fix-review.md`）2.2：real 写入路径

- **开关关闭时不开子事务**：复制触发器先在 savepoint 外用一次只读查询读取开关（`runtime.nexloop_shadow_copy_target`），开关关闭（D7 的默认状态）时直接返回，不进入 `begin … exception` 块。这样批量导入不会累积子事务；一个事务里超过 64 个子事务，pg_subtrans 就会溢出。Claim 的复制触发器做了同样的处理。
- **锁等待可以被捕获**：块内先设 `lock_timeout = 200ms`。锁等不到时报 55P03，能被异常处理捕获并计数，不会一直等到 statement_timeout；后者报 57014，`when others` 捕获不到，会导致真实消息写入失败。复制成功后恢复真实事务原来的 lock_timeout；复制失败时，块内的设置随子事务一起回滚。
- **测试**：开关关闭时，一个事务里批量插入 100 条 inbound，事务的子事务数为 0，lock_timeout 仍是原来的值，说明没有进入复制块。开关打开后，每条复制各有一个子事务，lock_timeout 同样恢复。
- **开关前的这次读取本身失败怎么办**：这只是一条简单的 select，L6 认为这个风险可以接受。真实写入的风险只剩这一条读取。
- **可选项没做**：失败计数接入 metrics 和告警，可以在后续单独做。
