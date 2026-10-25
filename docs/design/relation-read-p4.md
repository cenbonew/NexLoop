# NX-054 P4：读方切到正式关系（设计稿）

依据：ADR-038 §2 第 3 点（归属只有一个定义来源；读守卫与 `object_fields` 经关系沿传递链走到 Consumer；purge 永久用“关系 ∪ 属性”）、§3 第 7 点（后备计数持续 7 天为 0 才移除后备）；设计稿 `relation-types-design.md` §6、§7 P4/P5；P1（`runtime.nexloop_relation_object_consumers`，0319）、P2（派生，0321 / 0344）、P3（回填，0349 / 0350）。基线 main 4426c5d0。**本文只是设计，不含实现。**

## 0. 现状与结论

- **读方现状**：
  - 擦除期间的读守卫 `runtime.nexloop_assert_not_erasing`（0315）；
  - 工作台的 `authz.nexloop_workbench_object_fields`（0315）。
  
  两者都只在这个 tenant、这个 world 有擦除中的 Consumer 时，才调用 `runtime.nexloop_object_consumers`（0315 的属性规则）。没有擦除时只做一次索引探测，什么都不解析。
- **P4 的切法**：引入读侧解析函数 `runtime.nexloop_object_consumers_read`：结果是 **关系 ∪ 属性**，关系部分用 P1 的 `nexloop_relation_object_consumers`。两个读方改为调用它。
  - 为什么用并集，而不是“有关系时用关系、没有时用属性”：读守卫漏判一个 Consumer，就会在擦除期间漏读；并集在 P4 期间永远不会少于现在。
  - 关系只会让结果**更多**，例如经 link 写入的、属性规则不知道的关联。
  - P5 去掉并集中的属性部分，那一步是单向的，要以后备计数持续为 0 为前提。
- **“后备计数”不在读路径里写**：读守卫运行在只读事务和 STABLE 函数中，不能也不应该写计数。改用一个只读的核对函数，按需计算“属性能解析到、关系却解析不到”的对象数（下面称为**缺口**）。这正是 P5 移除后备之后会漏读的集合。
  - 由只读的 metrics 登录执行，与 0336 相同；
  - stage 上每天执行一次；
  - P5 的前提是连续 7 天为 0。
- **不变的东西**：
  - purge 永久用并集（ADR-038 §2.3），P4c 只是把关系那一半补进去；
  - 回填（0349）与预检继续用属性规则，因为它们的任务就是从属性推出关系；
  - RelationshipAssessment 的 `source_id` / `target_id` 首批没有关系（ADR-038 §3 第 6 点），这两个字段的归属在 P5 之后仍按属性解析，P5 只移除有关系覆盖的那部分属性规则。

## 1. 切片

| 片 | 内容 | 依赖 | 规模 | 上线条件 |
| --- | --- | --- | --- | --- |
| **P4a** | `nexloop_object_consumers_read`（关系 ∪ 属性）；读守卫与 `object_fields` 改用它（新迁移重定义，登记复制件守护）；只读核对函数 `authz.nexloop_relation_read_gaps(tenant)`，授权给 `nexloop_metrics`；`scripts/acceptance/relation_read_gaps.py` | P1、P2；**stage 上线要等 P3 回填 verify 平衡** | 1.5–2 天 | real 读路径的变更：ack-policy；stage 验收：0315 测试、全库擦除测试通过，缺口为 0 |
| P4b | 世界模型邻居（0223 / `workbench_reads.object_neighbors`）改读关系：有方向、类型、基数；去掉“值像 id”的启发式分组。可见性仍是两端各自的成员读（ADR-025/035），25 上限与不可见计数不变 | P4a 的缺口为 0（否则邻居会少） | 1.5–2 天 | 工作台读路径：ack-policy；世界模型测试改写 |
| P4c | 擦除 purge 的“找这个 Consumer 的对象”改为关系 ∪ 属性（0138、0144、0145、0227、0307 的步骤）；只增不减 | P4a | 1–1.5 天 | real 擦除路径：ack-policy；全库擦除测试 |
| P5 | 读侧去掉属性部分，只读关系（RelationshipAssessment 的 source/target 除外）；回退用新迁移恢复 | P4a 上线后缺口**连续 7 天为 0**；stage 每日 verify 为 0；全库擦除测试在“只读关系”模式下通过（ADR-038 §3 第 7 点） | 0.5–1 天 | 负责人确认（单向一步） |
| 撤销回填主体 | `service-grants` v21 去掉 `relation_backfiller` | P3 在 stage 上 verify 平衡 | 0.2 天 | 部署配置：调度员 ack |

## 2. P4a 细节

### 2.1 `runtime.nexloop_object_consumers_read(tenant, world, type, id) returns text[]`

- 语义：`nexloop_relation_object_consumers(...)` 与 `nexloop_object_consumers(...)` 的并集，去重、排序。
  - 同一个对象，两边的结果只会是“相同”或“关系多出几个”；不会出现“关系少了”，除非还没有回填（这就是缺口）。
- STABLE、security definer、owner 所有、不授权给任何角色；调用方已经设置了 `eios.tenant_id`（与 0315 相同）。
- 代价：
  - 只在“这个 world 有擦除中的 Consumer”时才会被调用（两个读方的快速路径不变）；
  - 关系一侧是一个最多 L 跳（首批 L = 3）的递归查询，每跳一次索引探测。

### 2.2 两个读方

- `runtime.nexloop_assert_not_erasing` 与 `authz.nexloop_workbench_object_fields`：用新迁移重定义，**唯一的差别**是把 `runtime.nexloop_object_consumers(` 换成 `runtime.nexloop_object_consumers_read(`。
- 在 `tests/test_function_copy_guard.py` 登记这一处替换（revert：`_read(` → `(`），复制件守护保证没有别的差别。
- 函数体从当前的集成头复制（0315 是最新的定义；实现时再核对一次 main 上有没有更新的重定义）。

### 2.3 缺口核对 `authz.nexloop_relation_read_gaps(tenant) returns jsonb`

- 对每个 world、每个对象类型，统计满足以下条件的对象数：`nexloop_object_consumers` 中至少有一个 Consumer **不在** `nexloop_relation_object_consumers` 的结果中。
  - 只统计首批关系覆盖的类型（有带归属标记关系的源类型），以及 Message、Conversation（0315 经会话表解析的那两个类型，它们由 `message_in`、`conversation_of` 覆盖）；
  - RelationshipAssessment 只核对 `evidence_message_id` 那一半，`source_id` / `target_id` 不计入（首批没有关系）。
- 输出只有计数：`{world, type_name, objects, gaps}`；不输出 id、属性值或业务内容。
- 只读的 metrics 登录 `nexloop_metrics` 执行（0336 的模式）：服务端强制只读、只能执行函数；脚本核对会话身份，设置 statement_timeout 60 s。
- 规模：stage 上是万级对象，每个对象两次解析；放在低峰期每天执行一次，几十秒以内。实现时在 stage 数据上实测。
- `scripts/acceptance/relation_read_gaps.py --dsn-file <metrics DSN> --tenant <TENANT_ID>`：输出 JSON，有缺口时退出码 1。

### 2.4 测试（本地 PG）

- **并集**：
  - 只有属性、没有关系的对象（以 replica 模式写入）：读守卫照样拒绝，结果与 P4 之前相同；
  - 只有关系、属性规则解析不到的对象（经 link 写入，或者只在关系里出现的链）：读守卫现在也拒绝；
  - 两者都有：结果相同。
- **0315 的测试**（`test_erasing_indirect_read_pg`）与全库擦除测试（`test_erasure_whole_database_pg`）原样通过，并在设计稿 §6 要求的模式下加一条断言：发布并回填之后，缺口为 0。
- **缺口核对**：
  - replica 写入一个不派生的对象，缺口为 1；回填之后为 0；
  - 输出不含 id；
  - 只有 `nexloop_metrics` 能执行。
- **复制件守护**：登记的替换必须恰好出现一次，没有其他差别。
- **性能**：没有擦除时，读守卫的路径不变（一次探测）；有擦除时，比较 P4a 前后单次读守卫的耗时，作为证据（本地 PG，N = 1000 次）。

## 3. 风险

- **缺口不为 0 就切到 P5**，会在擦除期间漏读。所以 P5 的前提写死为“连续 7 天为 0”，并要求负责人确认。
- **P4a 本身不会少判**：并集只会多出 Consumer。多出来的 Consumer，意味着某个对象经关系属于一个正在擦除的 Consumer，按 ADR-038 本来就应当拒读。如果 stage 上因此出现了新的拒读，那就说明以前漏读了，应当记录下来。
- **关系被经 unlink 删除、属性仍指向 Consumer**：属性规则仍会解析到，所以并集不受影响；缺口核对会把它报出来（P2 的规则是属性一变化就重新派生）。

## 4. 待 L6 复核的点

1. 后备计数改为离线的缺口核对，而不是在读路径里写计数（§0）。
2. P4a 的读侧用并集，而不是“有关系时用关系、没有时用属性”（§0）。
3. 缺口核对只按 metrics 登录每天执行一次，不接入告警规则；需要告警时，P4a 之后再加一条 v14 规则。
