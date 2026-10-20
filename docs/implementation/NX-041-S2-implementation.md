# NX-041 S2：模拟世界的创建与删除，按 world 发放凭据

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
