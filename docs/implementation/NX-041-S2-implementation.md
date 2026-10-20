# NX-041 S2：模拟世界的创建与删除，按 world 发放凭据

> **D10 状态（2026-10-21 更新）**：D10 已定案（负责人 2026-10-21 放宽为三凭据：api、worker、agent，每个只绑定这一个模拟世界，删除世界时一并吊销），实现中。下文中“D10 待定”“等 D10 定下来”“D10 定案时”等说法，均指这一定案。

依据：`NX-041-design.md` D2、D10、§11 S2、迁移 B。迁移 `0260_nx041_sim_worlds.sql`（编号只出现在文件名和 catalog 里）。

## 实现

| 内容 | 位置 |
| --- | --- |
| 登记表 `control.nexloop_sim_worlds`：world（只接受 S1 校验下的 `sim:`/`shadow:`，`real`/`test`/`simulation` 都登记不了）、租户、创建者、用途、状态 active/deleting/deleted、创建/到期/删除时间、请求号。行不能删除，状态只能往前走 | 0260 §1 |
| 受治理 Action `nexloop.sim_world.create` / `create_sim_world`、`nexloop.sim_world.delete` / `delete_sim_world`，subject_rule `human`，只给 owner（D10）。world id 由租户和 request_id 推导，重试得到同一个 world | 0260 §3；business-actions v+1、capabilities、system-catalog-names、workbench-roles v+1（slice `sim_worlds`）；`business_actions.py`、`goal_controls.py`、`workbench_actions_http.py`、`workbench_roles.py` |
| 按 world 发凭据（D10，AT-046）：`authz.nexloop_service_credentials` 上的触发器。sim/shadow 凭据恰好只含一个 world；该 world 已为同一租户登记且为 active；每个 world 至多一个 active 凭据；worlds 不能修改；吊销后不能恢复；sim 凭据的 principal 不能同时是 real/test 凭据的 principal（两个方向都检查）。删除 world 时同时吊销它的服务凭据和 Run 凭据 | 0260 §2、§3 |
| 整体删除（D2）：`runtime.nexloop_sim_world_purge(tenant, world, limit)` 遍历每张带 world 列的表（运行时从系统目录取，新表自动纳入），只删 `world=<该 sim world>`（有 tenant_id 的表同时限定租户）。每次调用是一个事务，最多删 limit 行，可以中断后续跑；状态为 deleted 后再调用也不改任何东西（幂等）。删第一行之前，`control.nexloop_sim_world_summaries` 记下每张表的计数和摘要哈希（行摘要排序后的 sha256，不含原文），记下后不能修改 | 0260 §4 |
| 保留：类别 `sim_world_summary` 30 天（settings v8 / retention.v1.json v8）。保留轮次先续跑处于 deleting 的 world，再删除 30 天前已完成删除的摘要；登记行保留为 deleted | 0260 保留段 |
| 指标导出：在 real、test 之外，遍历已登记且为 active 的 sim/shadow world（S1 留下的那一步） | 0260 §5 |

## 测试

`tests/test_nx041_sim_worlds_pg.py`：
- 登记、重试幂等，real/test 不能登记；
- Action 只给 owner；
- 凭据负例：sim A 访问 real 和 test、sim A 访问 sim B、real 凭据进入 sim A、多个 world、未登记、跨租户、第二个凭据、principal 复用、改 worlds、吊销后恢复；
- 分批删除：每批之后 real、test 和其他 sim world 的行数都不变；
- 摘要不含原文，不能改也不能删；
- 保留续跑，30 天后清掉摘要；
- 指标导出。

测试夹具：`tests/support/sim_worlds.py` 在发 sim 凭据前先登记 world，并吊销这个 world 原有的凭据，相当于重新签发。

## 限制

- 自带 append-only 守卫、又不认 erasure scope、也没有专用开关的表，如果存有某个 sim world 的行，会被报告为 `blocked`。这时 world 停在 deleting，不会假装已经删完。按计划由 S3/S4 给这些表补删除路径；目前只有 semantic access 记录（0234 的开关）和读 erasure scope 的表能删。
- 每次删除会对所有带 world 列的表各做一次计数，没有 world 索引的表会全表扫描。sim world 的数据量小，目前可以接受。

## 审查修复（L6 审查 `nx041-s2-review.md`）

0260 还没有发布，以下改动都直接在 0260 里修改。

| 编号 | 修复 |
| --- | --- |
| 4.1 | 过期摘要清理时，limit 改为作用在“已过期的摘要”上，不再作用在 deleted 的登记行上。登记行永远保留，旧写法下删除过的 world 超过 batch 个之后，后面的摘要就永远不会过期。测试：删除 3 个 world，batch=2，分两轮清完。 |
| 2.1 | 新增部分唯一索引 `nexloop_service_credentials_one_active_sim`。触发器看不到别的事务里还没提交的行，并发插入时第二个插入会先等待，第一个提交后报 UniqueViolation。测试用两个连接交错执行。 |
| 2.2 | UPDATE 时，如果 `tenant_id` 或 `binding` 变了，按新建凭据的规则重新检查，两个方向都检查：把 sim 凭据的主体改成 real 的主体，以及把 real 凭据的主体改成 sim 的主体，都会被拒绝。 |
| 1.1 | 新增声明表 `control.nexloop_sim_world_dependents`，登记 9 个外键：自身没有 world 列、但通过外键挂在带 world 列的表上的子表。其中 8 个为 `delete`，按 position 在 world 表之前删除；1 个为 `cascade`（recall_embeddings）。删除条件根据 catalog 中的外键列自动生成。守护测试：catalog 里实际存在的此类外键集合必须与声明完全一致，以后加了新表而没有声明，测试就会失败。会话里通过 run_id、intent_id 关联的无 world 列的表，都已经在外键清单中。 |
| 1.4 / 5.1 | 每张表的删除和计数都放在同一个子事务里。某张表失败时，记入 `blocked: {表: sqlstate}`，计入 remaining（无法计数时按 1 计），然后继续处理后面的表，world 保持 deleting。测试：建一张 nexloop_owner 没有权限的表，第一轮删除后，返回值报告这张表 42501，其他表已删完；授权之后再跑一轮，删除完成，real 数据不受影响。 |
| 1.5 | 快照改为分批：`snapshot(tenant, world, p_rows)` 每次读到约 p_rows 行就停（按整张表计，不在表内切分），中间结果写入 `control.nexloop_sim_world_snapshot_parts`。所有表做完后才写摘要，并清空中间结果。读不了的表在摘要里记为 `unreadable:<sqlstate>`。purge 在快照未完成时只推进快照，不删任何数据。 |
| 1.7 | 0129 的 erasure scope 只接受符合 `^[a-z_]+\.[a-z_]+$` 的表名。原实现对表名带数字的表（例如 `evo_l1_*`）也去打开 scope，每次都报 23514；以前之所以没出问题，只是因为这类表正好没有数据。现在只对符合命名的表打开 scope。 |
| 1.8 | 删除开始时（world 变为 deleting）写一条删除台账：kind `sim_world`，subject 为该 world，world 记为 real。NX-035 的恢复日志会照常带上这条台账。`runtime.nexloop_replay_deletions`（0135 的函数体，原地替换，只多一个 kind）在重放时，把恢复后仍为 active 的 world 置回 deleting，并吊销它的服务凭据和 Run 凭据，剩下的由 keeper 续删。测试模拟了“从删除前的备份恢复”：world 和凭据回到 active、数据也回来，重放后状态为 deleting、凭据被拒，keeper 跑完后为 deleted，零残留；重复重放时返回 already_applied。 |
| 1.2 | 守护测试：所有删除目标表上的策略都是放宽型；凡是启用了 RLS 的表，至少有一条按 tenant 放行（或无条件放行）的策略，对 nexloop_owner 生效。现状满足这一点，所以 purge 不会因为 RLS 少删数据。以后有人加了收紧型策略或按主体过滤的策略，这条测试就会失败。没有引入绕过 RLS 的角色，那样需要 ADR。 |
| 2.4 | 测试：Run 凭据在删除前能通过认证（阳性对照），删除后报 `Run authentication denied`。认证路径 0034 会检查 `status='active'` 和有效期，不经过缓存。 |
| 5.2 | 凭据负例全部改成用 `match` 指明触发的是哪条规则。这样查出一个问题：原测试里“real 主体带进 sim world”这个负例，其实触发的是“一个 world 只能有一个 active 凭据”。现在改为用一个还没有凭据的已登记 world 来测。 |

## 仍不处理的部分及理由

- **库外数据（1.1）**：本地 Artifact 的对象文件和 Agent Host 上的 Pi Run 目录不在数据库删除范围内。数据库里的行（Artifact 记录、Run 凭据和绑定）会被删除，对应的文件则交给 S6。做法预计沿用 retention v3 的 run_files 队列和 Host 确认机制，即删除 world 时写入清理队列，由 Host 确认后才算完成。在 S6 之前，sim world 的 Run 目录按 run_files 的保留期清理，最长 30 天。
- **服务凭据行**：world 删除后，服务凭据行仍然保留，状态为 revoked。凭据表的列名是 `worlds`，不在删除目标里；保留下来是为了审计。
- **4.4 导出数量上限**：本批不改，原因如下：
  - world 只能由 owner 通过受治理 Action 创建，数量很少，基数受人工操作约束；
  - 是否要给 active world 设数量上限、`expires_at` 到期后是否自动转为 deleting，属于 D2 的产品决定，应当另外确认，不能由实现自行决定。

  如果要做，需要在 keeper 中加一个到期转 deleting 的步骤，并设置租户级上限。
- **分区父表**：分区父表整表删除，不受 limit 约束，已在代码注释中写明。
- **world id 可推导**：world id 由 tenant 和 request_id 推导而来。它只是一个标识，不是凭据，能推导出来没有安全问题。

## 完整性补齐（L6 复核 1.1 余项、N1、N2、N5）

| 编号 | 处理 |
| --- | --- |
| 1.1 递归 | 守护测试改成递归检查：从有 world 列的表出发，沿外键往下逐层找所有没有 world 列的表，不限层数；其中每个外键都必须登记在依赖声明里。另外，凡是有 `run_id` 列（包括 `xxx_run_id`）、但没有 world 列的表，都必须登记，或者标记为 `not_applicable`。声明表新增了 `not_applicable` 模式和 `reason` 列，这个模式必须写明理由。目前没有孙表；有 run_id 列的两张表（effect_run_contexts、effect_submissions）已通过外键声明，不需要 `not_applicable`。 |
| N1 | world 表的删除顺序改为按依赖拓扑排序：如果某张 world 表被其他 world 表的外键引用，它一定排在那些表之后（链长上限 40，用来终止环）。这样一轮就能先删子表、后删父表。测试断言除环上的边以外，所有外键边都满足这个顺序。S2 和 S3 的联合测试：同一个 world 里先做 synthetic 生成、再做 structural 生成，owner 的删除 Action 一轮删完，没有 blocked，也没有残留。外键环（goals 和 goal_versions 互相引用）不受这个保证，仍然靠多轮删除。 |
| N2 | 已确认：`evo_l1_definitions` 和 `evo_l1_sessions` 都有 `CHECK (world = 'real')`，sim 行写不进去，所以不会卡住删除。新增守护测试：表名带数字、不能被 erasure scope 登记，又有拒绝删除触发器的删除目标，必须带只允许 real 的 world 约束。以后新加这类表而没有这个约束，测试就会失败。 |
| N5 | key 的形式一致，0261 不需要改。生产路径 `service_grants.compile_owner_restrictions` 生成的 key 是 `[type_name]`，`configure_manifest`（0084:134）也强制 key 等于 type_name，与 0261 中 `entity_key=array[type_name]` 的匹配方式相同。新增测试：用 owner 的限制配置文件，经 `validate_owner_restrictions` 和 `compile_owner_restrictions` 生成事实，验证受限组的属性不会进入 sim world。 |
