# NX-054 P4：读方切到正式关系（设计稿第 2 版）

依据：ADR-038 §2 第 3 点（归属只有一个定义来源；读守卫与 `object_fields` 经关系沿传递链走到 Consumer；purge 永久用“关系 ∪ 属性”）、§3 第 7 点（后备计数持续 7 天为 0 才移除后备）；设计稿 `relation-types-design.md` §6、§7 P4/P5；P1（`runtime.nexloop_relation_object_consumers`，0319）、P2（派生，0321 / 0344）、P3（回填，0349 / 0350）。基线 main 4426c5d0。**本文只是设计，不含实现。** 第 2 版处理 L6 对第 1 版（2845724b）的复核（`build/nx054-p4-design-review.md` 第 1–6 点），正文中以 `[R1]` 到 `[R6]` 标注。

## 0. 现状与结论

- **读方现状**：
  - 擦除期间的读守卫 `runtime.nexloop_assert_not_erasing`（0315）；
  - 工作台的 `authz.nexloop_workbench_object_fields`（0315）。
  
  两者都只在这个 tenant、这个 world 有擦除中的 Consumer 时，才调用 `runtime.nexloop_object_consumers`（0315 的属性规则）。没有擦除时只做一次索引探测，什么都不解析。
- **P4 的切法**：引入读侧解析函数 `runtime.nexloop_object_consumers_read`：结果是 **关系 ∪ 属性**，关系部分用 P1 的 `nexloop_relation_object_consumers`。两个读方改为调用它。
  - 为什么用并集，而不是“有关系时用关系、没有时用属性”：读守卫漏判一个 Consumer，就会在擦除期间漏读；并集在 P4 期间永远不会少于现在。
  - 关系只会让结果**更多**，例如经 link 写入的、属性规则不知道的关联。
  - P5 去掉并集中的属性部分，那一步是单向的，要以后备计数持续为 0 为前提。
- **“后备计数”不在读路径里写**，改用一个只读的核对函数，按需计算“属性能解析到、关系却解析不到”的对象数（下面称为**缺口**）。这正是 P5 移除后备之后会漏读的集合。
  - [R1] 理由更正：`authz.nexloop_workbench_object_fields` 是 STABLE（0315:65），在其中写计数会报错；`runtime.nexloop_assert_not_erasing` 是 VOLATILE（0315:43，与 0142:347 相同），技术上可以写，但它在每一次受守卫的读取中执行，计数写入会把读路径变成写路径（行锁、WAL、只读事务中的失败），而且两个读方只有一个能计数，口径不一致。所以两边都不写，统一用离线核对。
  - 由 `nexloop_metrics` 登录执行（0336 的模式）。[R1] 这个登录**没有**服务端强制只读（0131:439 只是一个只能执行少数函数的 login 角色）：只读由三层保证——核对函数声明为 STABLE（不能写），函数开头检查 `session_user='nexloop_metrics'`，调度脚本在调用前执行 `set transaction read only`；
  - 每天执行一次，见 §2.5；P5 的前提见 §1。
- **不变的东西**：
  - purge 永久用并集（ADR-038 §2.3），P4c 只是把关系那一半补进去；
  - 回填（0349）与预检继续用属性规则，因为它们的任务就是从属性推出关系；
  - RelationshipAssessment 的 `source_id` / `target_id` 首批没有关系（ADR-038 §3 第 6 点），这两个字段的归属在 P5 之后仍按属性解析，P5 只移除有关系覆盖的那部分属性规则。

## 1. 切片

| 片 | 内容 | 依赖 | 规模 | 上线条件 |
| --- | --- | --- | --- | --- |
| **P4a** | `nexloop_object_consumers_read`（关系 ∪ 属性）；读守卫与 `object_fields` 改用它（新迁移重定义，登记复制件守护）；只读核对函数 `authz.nexloop_relation_read_gaps(tenant)`，授权给 `nexloop_metrics`；`scripts/acceptance/relation_read_gaps.py` 与 DATA_HOST 的每日 timer（§2.5） | P1、P2；**stage 上线要等 P3 回填 verify 平衡**；[R4] **与 P4c 同一批上 real** | 1.5–2 天 | real 读路径的变更：ack-policy；stage 验收：0315 测试、全库擦除测试通过，非擦除部分的缺口为 0 |
| P4b | 世界模型邻居（0223 / `workbench_reads.object_neighbors`）改读关系：有方向、类型、基数；去掉“值像 id”的启发式分组。可见性仍是两端各自的成员读（ADR-025/035），25 上限与不可见计数不变 | P4a 的缺口为 0（否则邻居会少） | 1.5–2 天 | 工作台读路径：ack-policy；世界模型测试改写 |
| P4c | 擦除 purge 的“找这个 Consumer 的对象”改为关系 ∪ 属性（0138、0144、0145、0227、0307 的步骤）；只增不减 | P4a；[R4] **与 P4a 同一批上 real**（可以分开开发、分开复核，但不单独上线） | 1–1.5 天 | real 擦除路径：ack-policy；全库擦除测试 |
| P5 | 读侧去掉属性部分，只读关系（RelationshipAssessment 的 source/target 除外）；回退用新迁移恢复 | P4a 上线后**非擦除部分**的缺口（`gaps`）**连续 7 天为 0**（`gaps_erasing` 单独记录，不计入，见 §2.3）；stage 每日 verify 为 0；全库擦除测试在“只读关系”模式下通过（ADR-038 §3 第 7 点）；[R5] **缺口核对已接上告警**（§2.5），并在这 7 天中一直在线 | 0.5–1 天 | 负责人确认（单向一步）；[R5] P5 之后每日核对与告警**继续保留** |
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
  - [R2] 与 P3 verify 的口径一致（0349:458：verify 的 A、B 都不计入属于擦除中 Consumer 的对象）：缺口分两列输出，`gaps`（对象不属于任何擦除中的 Consumer）与 `gaps_erasing`（属于擦除中的 Consumer；回填按设计跳过它们，所以这一列不会因回填归零，只在擦除完成、对象被假名化或删除后消失）。P5 的前提只看 `gaps`。
  - [R6] 反方向也输出一列 `relation_only`：关系能解析到、属性规则解析不到的对象数。它对应 P3 verify 的 `extra`（多出来的派生关系）加上经 link 写入的关系，只报告，不作为前提；不为 0 时，说明 P4a 会比以前多拒读这些对象（§3）。
  - 只统计首批关系覆盖的类型（有带归属标记关系的源类型），以及 Message、Conversation（0315 经会话表解析的那两个类型，它们由 `message_in`、`conversation_of` 覆盖）；
  - RelationshipAssessment 只核对 `evidence_message_id` 那一半，`source_id` / `target_id` 不计入（首批没有关系）。
- 输出只有计数：`{world, type_name, objects, gaps, gaps_erasing, relation_only}`；不输出 id、属性值或业务内容。
- 函数声明为 STABLE，开头检查 `session_user='nexloop_metrics'`，否则 42501；只授权给 `nexloop_metrics`。脚本核对会话身份，执行 `set transaction read only`，设置 statement_timeout 60 s（[R1]）。
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

### 2.5 每日核对、证据与告警 [R3][R5]

- **谁、在哪、怎么调度**：DATA_HOST 上一个 systemd timer `nexloop-relation-read-gaps.timer`（每天 03:40，`Persistent=true`），服务单元以本机非特权用户执行 `relation_read_gaps.py`，经本机 socket 以 `nexloop_metrics` 登录（与 G-METRICS-DSN 相同的 DSN 文件，0600）。模板放在 `deploy/stage/systemd/`，由调度员在 P4a 上线时安装（与 host-health 的 timer 相同的安装方式），不需要负责人操作。
- **证据**：每次输出一个 JSON 文件，写到 DATA_HOST 的 `$NEXLOOP_ROOT/evidence/relation-read-gaps/<UTC 日期>.json`（0640，保留 30 天），只有计数。调度员在第 7 天把这 7 个文件的摘要（每天的 `gaps` 总和、`gaps_erasing` 总和、`relation_only` 总和）记入 NX-054 的证据，作为 P5 的前提依据。
- **证据写入失败也是失败**：证据文件写不成（目录不存在、磁盘满、权限），脚本以非零退出（退出码 2），不能只在 stdout 上打印结果后以 0 结束。
- **告警链路**（L6 复审第 1、2 点；不经 journal 转发）：
  1. `gaps > 0`（退出码 1）或证据写入失败（退出码 2）→ 服务单元以非零退出，systemd 记为 `Result` 不是 `success`；
  2. host-health 探针把这一轮记为 `result='failure'`（`host_health_probe.py` 按 `Result` 判断，与其他 timer 相同）；
  3. 0318 把它计入 `timer_scheduled_failures_24h`；
  4. v18 已有的 `timer_scheduled_failure` 规则（selector `*`，`gt 0`）触发告警；修复后下一轮成功，24 小时窗口过去后告警清除。
  - 前提：这个 timer 要加进 DATA_HOST 的 host-health 配置（`timers` 中一项 `relation-read-gaps`，timer 与 service 两个单元名），并且每一轮都要被探针认作**定时**运行：timer 拉起服务，运行开始时间在最近一次触发之后的 `grace_seconds` 之内。手工补跑（`systemctl start`）不算定时，不会刷新新鲜度，也不会抵消失败。
  - journal 中的 `<3>nexloop-relation-read-gaps gaps=<n> worlds=<n>` 只是给值守人员看的说明（`journalctl -p err`），**没有任何组件把它转发成告警**。
- **timer 新鲜度**：新增告警规则 **alert-rules v19 = v18 加一条 `timer_stale_relation_read_gaps`**（selector `data_host:relation-read-gaps`，阈值 93600 秒即 26 小时，`gt`），由 P4a 实现时加入，与 v18 中其他 `timer_stale_*` 的写法一致（v18 中这类规则都是逐个 selector 写的，没有 `*`）。timer 没有运行、没有被认作定时、或者一直失败，都会在 26 小时后触发。
- **stage 演练**（P4a 上线后，调度员执行）：
  1. 造出 `gaps = 1`：在 **stage 的 test world**（不碰 real）写入一个 Consumer 和一个指向它的 Goal，然后删掉这个 Goal 的派生关系。直接写 `ontology.objects` / `ontology.relations` 需要 owner 级身份，没有任何应用角色能做到，所以这一步由**调度员**在 **DATA_HOST** 上经 **bootstrap 窗口**（§0 的 `boot.dsn`，口令从负责人的 Keychain 经管道写入）执行一个只做这三条语句的事务，执行后关闭窗口；第 3 步同样经 bootstrap 窗口删除这两个测试对象（BEFORE DELETE 触发器会带走关系）。
  2. 让**这个 timer 自己**触发一次运行：用一个临时 drop-in 给 `nexloop-relation-read-gaps.timer` 加一个几分钟后的 `OnCalendar`，`daemon-reload` 后等它触发，演练结束删除 drop-in（`systemd-run --on-active` 建的是另一个临时 timer，`systemctl start` 服务是手工运行，这两种都不会被探针认作这个 timer 的定时运行）；确认这一轮 `Result` 不是 `success`，并在下一次告警评估后看到 `timer_scheduled_failure`（selector `data_host:relation-read-gaps`）触发；
  3. 删除测试对象或回填它，再用同样的方式触发一次定时运行，确认 `gaps = 0`、`Result=success`；24 小时窗口过去后告警清除（或者记录清除时刻）；
  4. 结果记入 NX-054 的证据。
- **P5 的前提之一就是这条链路已经在线**（两条规则都已发布，演练做过）；P5 之后 timer 与告警继续保留，作为“只读关系”之后的长期守护。

## 3. 风险

- **缺口不为 0 就切到 P5**，会在擦除期间漏读。所以 P5 的前提写死为“连续 7 天为 0”，并要求负责人确认。
- [R4] **拒读但不删除的窗口**：拒读条件包括擦除处于 `completed`（0132:48 的状态之一）。如果 P4a 单独上线、P4c 还没有上线，一个只经关系（属性规则不知道）挂在已擦除 Consumer 名下的对象，会被永久拒读，但 purge 不会处理它（purge 仍只按属性找对象），它的内容既不能读，也不会被擦掉。所以 **P4a 与 P4c 同一批上 real**（§1）；在那之前，stage 上 P4a 也不单独启用。
- **P4a 本身不会少判**：并集只会多出 Consumer。多出来的 Consumer，意味着某个对象经关系属于一个正在擦除的 Consumer，按 ADR-038 本来就应当拒读。如果 stage 上因此出现了新的拒读，那就说明以前漏读了，应当记录下来。
- **关系被经 unlink 删除、属性仍指向 Consumer**：属性规则仍会解析到，所以并集不受影响；缺口核对会把它报出来（P2 的规则是属性一变化就重新派生）。

## 4. 待 L6 复核的点

1. 后备计数改为离线的缺口核对，而不是在读路径里写计数（§0）。
2. P4a 的读侧用并集，而不是“有关系时用关系、没有时用属性”（§0）。
3. 告警走退出码 → host-health 探针 → `timer_scheduled_failure`；新鲜度由新增的 alert-rules v19 `timer_stale_relation_read_gaps`（26 h）负责（§2.5）。
4. P4a 与 P4c 同一批上 real（§3 [R4]）。
