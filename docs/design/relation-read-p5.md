# NX-054 P5：读方只读关系（迁移草案）

依据：`relation-read-p4.md` §1 的 P5 行；ADR-038 §2 第 3 点、§3 第 6、7 点。**本文与迁移 `relation_read_only`（临时号 0416）都是草案。** 组批有两个前提：P4a 之后的观察期满 7 天，且负责人确认。在此之前，草案只留在单独的分支上。

## 1. 改什么

- 擦除期间的读守卫 `runtime.nexloop_assert_not_erasing` 和工作台的 `authz.nexloop_workbench_object_fields` 都只调用 `runtime.nexloop_object_consumers_read`（0357）。P5 只换掉这一个函数，两个读方的函数体都不动，所以不需要登记复制件守护。
- 新的解析规则：
  - 对象本身是 Consumer：结果就是它自己。
  - tenant **没有已发布的关系目录**：仍按 0315 的属性规则解析。从未发布过关系的 tenant 无关系可读，如果此时去掉属性，读守卫就什么也拒绝不了。
  - 已发布关系目录：只读归属关系（0319 的 `nexloop_relation_object_consumers`，按传递链走到 Consumer）。唯一的例外是 RelationshipAssessment 的 `source_id` / `target_id`：首批没有对应的关系（ADR-038 §3 第 6 点），这两个字段继续按属性解析。它的 `evidence_message_id` 走关系。
- P4a 的并集函数改名为 `runtime.nexloop_object_consumers_read_p4union` 保留下来，供回退使用。
- **不变的东西**：
  - purge 永久使用“关系 ∪ 属性”（0361，ADR-038 §2.3）；
  - 回填和预检仍按属性规则；
  - 每日缺口核对（`authz.nexloop_relation_read_gaps`）、timer 和告警都继续保留（`relation-read-p4.md` §2.5）。

## 2. 组批前提（单向的一步）

| # | 前提 | 证据 |
|---|---|---|
| 1 | P4a 上线后，非擦除部分的 `gaps` **连续 7 天为 0** | 7 份每日证据文件的摘要（`relation-read-p4.md` §2.5），由调度员记入 NX-054 证据 |
| 2 | stage 每日的 verify 为 0 | 同上 |
| 3 | 全库擦除测试在只读关系模式下通过 | 组批时跑 `test_erasure_whole_database_pg` 和本草案的测试 |
| 4 | 负责人确认 | 调度员记录确认的时间 |

## 3. 行为变化（测试 `tests/test_relation_read_only_pg.py`）

- 已发布目录时，只靠属性归属于擦除中 Consumer 的对象，**不再被拒读**。这正是缺口核对所计的 `gaps`；前提 1 保证上线时这个集合为空。`test_relation_read_union_pg` 中 P4a 的对应断言已相应改写。
- 只靠关系归属的对象：照旧拒读。
- RelationshipAssessment：经 `source_id` 归属的照旧拒读。
- 没有发布目录的 tenant：行为与 P4a 完全相同。`test_erasing_indirect_read_pg` 的那两个用例不变，并且照样通过。

## 4. 回退

- 回退时新开一个迁移，内容是 `docs/design/relation-read-p5-rollback.sql`，不改 `relation_read_only` 本身：删除只读关系的函数，把 `_p4union` 改回原名。两个读方随即回到 P4a 的并集。
- `test_relation_read_only_pg` 的回退用例会执行这份 SQL，并核对属性后备已经回来。
- 回退后观察：verify 差异与 `relation_dangling` 计数（relation-types-design.md S-5），以及每日缺口核对。
- 回退无需迁移数据：P5 不写任何行。

## 5. 否决的做法

| 做法 | 否决理由 |
|---|---|
| 两个读方各自重定义，把调用换成只读关系 | 两个函数体都要复制一遍，还要在复制件守护里登记；而它们共用的解析函数只有一个，改它一处就够了 |
| 原地 `create or replace` 解析函数 | 回退就得把 P4a 的函数体再抄一遍；改名保留后，回退只需要改名，不会抄错 |
| 不论是否已发布目录，一律只读关系 | 未发布关系的 tenant 没有任何关系，读守卫会什么也拒绝不了 |
| RelationshipAssessment 的 source/target 也只读关系 | 首批没有这两个字段的关系（ADR-038 §3 第 6 点），去掉属性就是确定的漏读 |
