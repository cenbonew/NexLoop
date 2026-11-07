# NX-054 P5：读方只读关系（迁移草案）

依据：`relation-read-p4.md` §1 的 P5 行；ADR-038 §2 第 3 点、§3 第 6、7 点。迁移 `relation_read_only`（临时号 0421）。观察期已满 7 天（起算日 10-28）；负责人 2026-11-07 确认（“按推荐”）。L6 对草案判了条件 PASS，C1、C2 在本版中处理。

## 1. 改什么

- **三个读方**都只调用 `runtime.nexloop_object_consumers_read`，P5 只换掉这一个函数，三个读方的函数体都不动，所以不需要登记复制件守护：
  - 擦除期间的读守卫 `runtime.nexloop_assert_not_erasing`（0357）；
  - 工作台的 `authz.nexloop_workbench_object_fields`（0357）；
  - 工作台成员读取的权限判断 `authz.nexloop_workbench_member_may_read`（0387:98，L6 C2）。
- 新的解析规则：
  - 对象本身是 Consumer：结果就是它自己。
  - tenant **没有已发布的关系目录**：仍按 0315 的属性规则解析。从未发布过关系的 tenant 无关系可读，如果此时去掉属性，读守卫就什么也拒绝不了。
  - 已发布关系目录：读归属关系（0319 的 `nexloop_relation_object_consumers`，按传递链走到 Consumer）。
  - **回填覆盖之前不去掉属性（L6 C1）**：对象的归属链上（它的源类型，以及从它出发经归属派生能到的类型）只要有一个带源属性的归属派生，还没有被一次已完成的回填覆盖，就仍然并上属性规则。新发布的归属类型（例如目录 v2 的 `assessment_evidence`）因此在它自己的回填完成之前不会有空窗。
    - 覆盖记录在新表 `control.nexloop_relation_backfill_coverage`（按关系类型版本）。回填 run 进入 `completed` 时由触发器写入：这一刻派生映射正是 run 开始时的那一份（门控在 `catalog_changed` 时会停下 run），而且所有 cursor 都已完成、没有跳过的对象。run 进入 `reverting` 或 `reverted` 时，删除这个 run 的覆盖记录。
    - 迁移时，对派生映射仍是其 digest 的已完成 run（stage 的 p3-2）补记覆盖。
    - verify 的平衡不在读路径里重算（每次受守卫的读取都要全表扫描）。代码层面的条件是：run 完成，并且重扫后没有跳过的对象；告警靠每日缺口核对。唯一的例外是 RelationshipAssessment 的 `source_id` / `target_id`：首批没有对应的关系（ADR-038 §3 第 6 点），这两个字段继续按属性解析。它的 `evidence_message_id` 走关系；但在 tenant 还没有发布 `assessment_evidence` 时，也按属性规则解析。stage 发布时推迟了这个类型（NX-054-stage-publish.md §8 第 4 步），所以 P5 不依赖目录版本 2。
- 新增 `runtime.nexloop_object_ownership_uncovered(tenant, type)`，判断上面这一条。
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

- 已发布目录、并且归属链上的类型都已回填覆盖时，只靠属性归属于擦除中 Consumer 的对象**不再被拒读**。这正是缺口核对所计的 `gaps`，前提 1 保证上线时这个集合为空。还没有覆盖时，行为与 P4a 相同，所以 `test_relation_read_union_pg` 不用改。
- 新类型已发布但还没回填：擦除中客户的对象仍被拒读；回填完成后才改为只读关系；覆盖撤销（出了新版本，或 run 被 revert）后，属性后备回来（`test_a_published_type_keeps_the_property_rule_until_its_backfill_completes`）。
- 工作台成员读取：在只读关系模式下，仍读不到经关系归属于擦除中客户的对象（`test_the_workbench_member_read_follows_the_relations_only_mode`）。
- 只靠关系归属的对象：照旧拒读。
- RelationshipAssessment：经 `source_id` 归属的照旧拒读。
- 没有发布目录的 tenant：行为与 P4a 完全相同。`test_erasing_indirect_read_pg` 的那两个用例不变，并且照样通过。

## 4. 回退

- 回退时新开一个迁移，内容是 `docs/design/relation-read-p5-rollback.sql`，不改 `relation_read_only` 本身：删除只读关系的函数和 `nexloop_object_ownership_uncovered`，把 `_p4union` 改回原名；覆盖表和触发器保留，它们只记录已完成的回填，不影响行为。三个读方随即回到 P4a 的并集。
- `test_relation_read_only_pg` 的回退用例会执行这份 SQL，并核对属性后备已经回来。
- 回退后观察：verify 差异与 `relation_dangling` 计数（relation-types-design.md S-5），以及每日缺口核对。
- 回退无需迁移数据：P5 不写任何行。

## 5. 否决的做法

| 做法 | 否决理由 |
|---|---|
| 各读方分别重定义，把调用换成只读关系 | 三个函数体都要复制一遍，还要在复制件守护里登记；而它们共用的解析函数只有一个，改它一处就够了 |
| 原地 `create or replace` 解析函数 | 回退就得把 P4a 的函数体再抄一遍；改名保留后，回退只需要改名，不会抄错 |
| 不论是否已发布目录，一律只读关系 | 未发布关系的 tenant 没有任何关系，读守卫会什么也拒绝不了 |
| RelationshipAssessment 的 source/target 也只读关系 | 首批没有这两个字段的关系（ADR-038 §3 第 6 点），去掉属性就是确定的漏读 |
