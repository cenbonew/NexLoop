# NX-054 P3：存量对象的关系回填（设计稿第 4 版）

依据：

- ADR-038 §2 第 5 点、§3 第 4 点（回填执行身份选 b）；
- 设计稿 `relation-types-design.md` §5、§7 P3；
- P1（`docs/implementation/NX-054-P1.md`）与 P2（`docs/implementation/NX-054-P2.md`，迁移 0321）的实现；
- 基线 main 53db8582。

**本文只是设计，不含实现。**

第 2 版处理了 L6 对第 1 版（6a226a9c）的复核：确认级 C-1、C-2，重要 I-1 到 I-6，建议 S-1 到 S-7。第 3 版处理 L6 对第 2 版（bea134c1）的复核：N-1 到 N-6，建议 S-8 到 S-10。第 4 版处理 L6 对第 3 版（7d04da0e）的复核：R-1 到 R-6；之后按 L6 对第 4 版（bcccdb5f）的复核改了 V-1 到 V-4，并按 L6 对实现第 1 片（f2021ae9）的复核与调度员的裁定（I-3）统一了状态口径。P2.1 已经实现（分支 l5/relation-derive-one）。每处修改在正文中用 `[C-1]`、`[N-1]` 这样的标记注明，§10 列出对照表。

## 0. 前提与结论摘要

- **前提**：
  - stage 上首批关系类型已经按发布手册由 configurator 发布（调度员执行），`control.nexloop_relation_derivations` 有 26 行；
  - P2.1 迁移已经上线（§2）。
  
  缺少任何一项时，preflight 会给出 blocking 项（§6）。
- **回填要补的对象**：P2 之后，新写入的对象和属性变化的对象都由触发器派生。回填只需要补两类：
  - P2 上线前已经存在的对象；
  - 以 `session_replication_role=replica` 写入的对象（恢复、种子数据）。
- **回填对存量对象执行 P2 的同一套派生规则**：
  - 0321 触发器函数的主体，在 P2.1 中抽成内部函数 `ontology.nexloop_relation_derive_one`；
  - 触发器与回填共用这个函数，派生规则只有一份。
- **与 relation-types-design.md §5 相比有三处修正**：
  1. 回填写入的关系，metadata 是 `{"derived": true, "backfill": "<run_id>"}`（§4.2）。
  2. 新建关系的清单、游标和被跳过的对象，记在专门的表里（§3.1）。
  3. 与擦除的互斥使用擦除表上的 SHARE 锁（§4.3）。

## 1. 范围与顺序

**范围**：

- 当前 tenant，由执行身份的服务端上下文得出，不接受参数；
- 这个 tenant 的每一个 world，包括 real、shadow、sim；
- `control.nexloop_relation_derivations` 中最新版本、并且带 `source_property` 的首批关系类型。

不在范围内的有：

- RelationshipAssessment 的 source/target、id 数组、`ceiling_ref`、`fulfillment_basis`（ADR-038 §3，首批没有这些关系类型）；
- 0016 link 写入的关系，回填既不读也不改。

**顺序**：先按关系类型，再按 world，最后在每个分组内按 `object_id` 走游标。

[I-1] **扫描的单位是源类型**。同一个源类型的所有关系类型在一次扫描中一起处理，例如 MessageAssignment 的 5 个关系类型。这样每个对象只扫描一次、只加锁一次，否则有几个关系类型就要扫几遍。

“按关系类型”的顺序落在源类型上，排列如下：

1. **带 `consumer_ownership` 标记的源类型在前**，因为 P4 切读依赖这些关系。其中：
   - 直接归属（`target`）的源类型在前；
   - 带传递归属的源类型在后，即 Message（`message_in`）和 RelationshipAssessment（`assessment_evidence`）；
   - 同一组内按类型名的字典序排列。
2. **其余源类型在后**。

计数仍然按关系类型分别记录（§3.1），verify 也按关系类型给出（§5）。

**world 的顺序**：先 real，再 shadow，最后 sim。

- sim world 可能在回填期间被整体删除（`nexloop_sim_world_purge`），这种并发属于正常情况，处理见 §4.1 [C-1]。
- [S-4] **写入 shadow 和 sim 的依据**：
  - 回填只在 `ontology.relations` 中写关系行，不调用任何 Action 执行器、不使用任何渠道凭据、不产生外部效果，所以不违反“仿真不得持有真实效果凭据”；
  - world 守卫（P1 的 `nexloop_relation_world_guard`）要求端点与关系在同一 world，并不限制写入方是谁；
  - 回填函数是 owner 所有的 security definer 函数，按对象所在的 world 写入；
  - 执行身份的 Action 授权只覆盖 `nexloop.relation.backfill:1`，其中的 world 参数是签名命令的 world，由服务端核对它属于这个 tenant。

**租户**：一次运行只处理一个 tenant。多租户部署按租户逐个运行，每个租户使用自己的执行身份。

## 2. 入口、执行身份与 P2.1

**P2.1 迁移**（[S-7]，先于 P3 单独上线）：

- 把 0321 `ontology.nexloop_relation_derive()` 的循环体抽成 `ontology.nexloop_relation_derive_one(o ontology.objects, p_relation text, p_marker jsonb)`；
- 触发器改为调用它，`p_marker` 固定为 `{"derived":true}`；
- 行为不变，P2 的全部测试（`test_relation_types_p2_pg.py`、`test_relation_derive_perf_pg.py`）作为回归；
- 这个函数不授权给任何角色。

**Action**：`nexloop.relation.backfill:1`。

- capability 为 `ontology.relation.backfill`，low 风险，无审批；
- change_scope 列出首批关系类型与端点类型。

**执行身份**：一个专门的低权限 service principal，只被授予这一个 Action（ADR-038 §3 第 4 点 b）。全部完成并验收后吊销。
  - 落地（调度员 2026-10-25）：`relation_backfiller`，登记在 `service-grants.v1.json` 第 19 版，数据库角色 `nexloop_domain_worker`，凭据在 `setup/` 下（`stage-principals.v1.json` 第 5 版）。**回填完成后撤销**：stage 上回填跑完、verify 核对平衡之后，用第 20 版去掉它。步骤见 `NX-054-stage-publish.md` §7。

**SQL 端**：`authz.nexloop_relation_backfill(digest, world, text, signature, payload)`，与 0016 / unlink 同一套签名命令模式。payload 的 `verb` 取以下之一：

| verb | 作用 |
| --- | --- |
| `preflight` | 只读，见 §6 |
| `start` | 登记一次运行 |
| `batch` | 执行一批 |
| `rescan` | 处理被跳过的对象，见 §4.1 |
| `sample` | 只读观测，见 §5 |
| `status` | 只读，返回进度 |
| `verify` | 只读，见 §5 |
| `abort` | 中止运行 |
| `revert_batch` | 回退一批，见 §7 |

**[I-3] 连接与角色**：

- `scripts/deploy/relation_backfill.py` 是一次性的运维工具，占用 ADR-037 中运维预留的 7 个连接里的 **1 个**：全程只有一条连接，不建连接池，也不并行。
- 不在 APP_HOST 应用的 60 个连接预算中登记，也不占用任何应用进程的连接池。
- 连接身份是 `nexloop_action_worker`（与 0016 link、P1 unlink 授权的角色相同）。回填函数只授权给这个角色。
- 采样（§5）通过同一条连接上的 `sample` 动作完成：由 security definer 函数在内部读取 `pg_locks`、`pg_stat_user_tables` 和 0323 的 API 指标，只返回计数。因此不需要另外的只读身份，也不需要给任何角色授予 `pg_monitor` 或 `pg_read_all_stats`。
  - [N-4] **为什么不用 pg_stat_activity**：没有 `pg_read_all_stats` 的角色（包括 nexloop_owner），在 `pg_stat_activity` 中看不到其他角色会话的 `wait_event_type`，等锁计数会一直是 0。
  - `pg_locks`（`pg_lock_status()`）对所有角色都返回全部锁，不受这个限制，所以改用 pg_locks，不扩大 owner 的权限。
  - `sample` 统计的是 objects、relations、擦除表这三个 relation oid 上 `granted = false` 的表级锁，作为**背景**：比如有其他运维操作或者长事务时，先不要开始下一批。
  - [R-5] 行锁等待**只统计正在等待回填事务的**：在每一批提交前于批内统计（见 §3.2“对 real 写入的影响”第 4 条），而不是在 `sample` 中按全库统计。全库的行锁等待大多与回填无关，用它做暂停条件会误判。
- 以后如果要按 world 并行，必须先修改连接预算并重新评审。
- `run` 运行在 CI 主机或 APP_HOST 上，私有配置与凭据走 stdin（与 `relation_types_publish.py` 相同）。

**[I-4] 签名、意图 id 与审计量**：

- **每一次调用都重新签名**：签名命令的 TTL 是 20 秒（release 24.1），`run` 是长时间运行的循环，因此不复用签名。
- **业务意图 id**：
  - batch 为 `run_id:source_type:world:after_object_id:batch`（含批大小，见 §3.2 [N-6]）；
  - rescan 为 `run_id:rescan:<轮次>`；
  - revert_batch 为 `run_id:revert:<序号>`。
  
  这一次调用的结果不明（网络中断）时，用同一个意图 id 重放，会得到 claim 中记录的结果，而不是悄悄跳到下一批。
- **审计量**：
  - 每次调用产生一行 Action claim 和一行治理审计；
  - 以 stage 的万级对象估算：按源类型扫描，每批 200 个对象，3 个 world，大约 100 到 300 次 batch 调用，再加上每 30 秒一次的 sample；
  - 合计在千行以内；
  - 这些行按治理审计的保留期（`governance_audit` 类）清理，不需要另设保留规则。

## 3. 分批、限速、断点续跑与幂等

### 3.1 运行记录

所有表都只有 owner 能写，不授权给任何应用角色。

- `control.nexloop_relation_backfill_runs`：
  - 列为 `backfill_run`（运行 id；不叫 `run_id`，因为模拟 world 的守护测试把没有 world 列的 `run_id` 当作 Agent Run，而回填运行属于租户、不属于任何 world）、`tenant_id`、`derivations_digest`（`start` 时派生映射的摘要）、`state`、`started_by`、时间戳；
  - state 取值为 `running`、`aborted`、`completed`、`reverting`、`reverted`；
  - [I-3，调度员裁定] **`aborted` 是终态，不续跑**：中止之后开一次新的运行。新运行从头扫描，已经有关系的对象计为 existing，结果幂等；
  - 没有 `paused` 状态：暂停（§5 的阈值、rescan 的退避）只是 `run` 进程在两批之间等待，数据库中的状态仍是 `running`；
  - [C-1] batch、rescan、complete 只接受 `running`；`reverting` 只由 revert_batch 处理；
  - [S-3] 部分唯一索引 `(tenant_id) where state in ('running','reverting')`，保证同一个 tenant 只有一个活动的运行。
- `control.nexloop_relation_backfill_cursors`：
  - 主键 `(backfill_run, source_type, world)`；
  - 记录 `last_object_id`、`done`、`batches`，以及最近一批的耗时和批大小。
- `control.nexloop_relation_backfill_counts`：
  - 主键 `(backfill_run, relation_name, world)`；
  - 计数包括 scanned、created、existing、dangling、skipped_erasure、skipped_vanished、skipped_world_purge。跨 world 的引用是预检的 blocking 项（§6），运行中不会遇到，所以不设 cross_world 列（L6 S-6）。
- `control.nexloop_relation_backfill_created`：`(backfill_run, relation_id)`，这次运行**新建**的关系。
  - [L6 B-1] 这里只记 relation_id（sha256），不是外键。sim 或 shadow world 被清除（`nexloop_sim_world_purge`）后，它的关系随对象一起删除，created 表中对应的 id 就成了悬空记录。revert 对这些 id 删除 0 行，并照常把它们从 created 表中移除，运行仍会走到 `reverted`，正确性不受影响；revert 输出的 `deleted` 会小于 created 的行数，差额就是这些已经随 world 删除的关系。
- [C-2] `control.nexloop_relation_backfill_skipped`：
  - 主键 `(backfill_run, source_type, world, object_id)`；
  - 记录 `reason`（`source_locked` 或 `target_locked`）、`attempts`、`first_seen_at`；
  - 与本批在同一个事务中写入。

**为什么不追加进 claim outcome**：每追加一批，jsonb 都要整行重写，写入量随清单长度平方增长。claim 只记录这次调用的结果摘要。

### 3.2 一批

一批是**一个短事务**，在 READ COMMITTED 下执行（P2 的基数检查依赖这一隔离级别，见 NX-054-P2 §1 I-4）。步骤如下：

1. 设置 `lock_timeout = 200ms`、`statement_timeout = 1s`。
   - **约定**：`statement_timeout` 由签名入口在调用引擎函数之前，以 `set local` 设置，因为函数无法改变它自己所在语句的超时。引擎的 batch、rescan、revert_batch 一进来就检查它在 (0, 1000 ms] 内，否则报 22023（L6 I-1）。签名入口漏设时直接失败，而不是悄悄失去这个上限。
   - `lock_timeout` 由引擎函数自己设置（它在每次等锁时读取，函数内设置有效）。
   - [R-1] **单批持锁的硬上限约 1 秒**：真实路径的连接默认 `lock_timeout` 是 3000 ms（`eios/persistence/settings.py`，`NEX_EIOS_DB_LOCK_TIMEOUT_MS`）。受治理编辑和身份关联遇到 55P03 会直接失败返回，没有重试；投递遇到 55P03 会重试。
   - 所以本批持有的任何锁都必须远短于 3 秒：statement_timeout 设为 1 s；第 5 步的循环每处理完一个源对象就检查一次已用时间，[V-3] 从**本批事务开始**（`transaction_timestamp()` 与 `clock_timestamp()` 之差）算起超过 750 ms 就停止处理，提前提交（见第 6 步）。
   - 这样，一条 real 路径因为回填而等待的时间，最长约 1 s，不会触发它自己的 3 s lock_timeout。
2. **检查运行状态**：
   - 读取并锁定 run 行和本游标行（`for update`）；
   - [S-5] 如果 state 不是 `running`（例如 `abort` 已经在 `run` 进程之外把它改成了 `aborted`），立即返回 `stopped`；
   - [I-2] 重新计算当前派生映射的摘要，与 `derivations_digest` 比较。不一致时，只把 state 改成 `aborted`、reason 为 `catalog_changed`，提交后返回，不做任何派生；
   - [N-2] world 是 sim world、并且 `control.nexloop_sim_worlds.status in ('deleting','deleted')` 时，把这个游标标为 done，计数记为 `skipped_world_purge`，提交后返回。verify 不对这个 world 计算 A 和 B。
   
   [N-5] 第 2 步的三项检查（运行状态、目录摘要、sim world 状态）同样在 `rescan` 和 `revert_batch` 的开头执行。revert 遇到目录摘要不一致时**不**中止，因为 revert 只删除带本次 run_id 标记的行，与映射无关；但会在输出中注明。
3. **擦除表加锁**：执行 `lock table control.nexloop_consumer_erasures in share mode`，然后读出擦除中的 Consumer 集合（§4.3）。
4. [I-1][N-3] **取一批源对象**，分两步：

   先用一条**不加锁**的查询确定本批的范围 `(after, hi]`：

   ```sql
   select object_id from ontology.objects
   where tenant_id = $t and type_name = $source_type and world = $w and object_id > $after
     and (properties->>'p1' ~ '^[a-f0-9]{64}$' or properties->>'p2' ~ '^[a-f0-9]{64}$' ...)
   order by object_id
   limit $batch
   ```

   - `hi` 是这条查询返回的最后一个 id；
   - 一个都没有返回时，这个游标标为 done。

   再在同一个范围内**加锁**，不带 limit：

   ```sql
   select * from ontology.objects
   where tenant_id = $t and type_name = $source_type and world = $w and object_id > $after and object_id <= $hi
     and (同上的 hex 条件)
     and not (这个对象的每一个 hex 属性对应的关系都已存在)   -- [S-8]，是否采用由 stage 的 EXPLAIN 决定
   for share skip locked
   ```

   - `p1`、`p2` 是这个源类型各个关系类型的 `source_property`。只有能派生出关系的对象才加锁；属性为空、已经是假名、或者不是 hex 的对象，既不锁也不写。
   - **被跳过的对象**：先算差集 = 第一条查询的 id 集合 − 第二条查询锁住的 id 集合。[R-3] 然后，在第二条查询**之后**，对差集中的每个 id 再求值一次“关系都已存在”（不加锁）：成立的计为 existing，不进 skipped 表；其余的写入 skipped 表。
     - 为什么要在之后求值：第二条查询里的 `not (关系都已存在)` 条件，与 skip locked 一样会把对象排除在结果之外。只看结果，无法分辨一个对象是“关系都在”还是“被锁住了”，所以必须在第二条查询之后单独判断。
     - 第二条查询之后才被删掉的关系，会让这个对象落进 skipped 表，由 rescan 处理，不会遗漏。
   - 两条查询的范围都是 `(after, hi]`，所以范围、跳过集合和游标三者一致：排在 hi 之前、被锁住的对象一定在差集里，即使它恰好是本批的最后一个；一批全部被跳过时，hi 仍然有定义。
   - 第一条查询之后才插入、并落在范围内的对象：它的 P2 触发器会派生，第二条查询看到也无妨（结果幂等）。
   - [S-8] 加了“关系都已存在”这个条件后，续跑、重跑，以及 revert 之后再跑时，绝大多数对象都不会再被加锁。代价是谓词更复杂（每个关系类型一个 `not exists`），是否采用，用 stage 的 EXPLAIN 决定。
5. **派生**：对每一个锁住的对象，以及它的每一个关系类型：
   - 跳过归属于擦除中 Consumer 的对象（§4.3）；
   - [C-1] 目标对象用 `for key share skip locked` 读取：
     - 读到了：持有 KEY SHARE 锁直到提交，并发删除只能等本批提交之后进行，插入关系时不会出现 23503；
     - 没读到，但不带锁能读到：[S-10] 说明目标正被删除，**或者正被其他事务以 FOR UPDATE 锁住**（投递、身份关联、受治理编辑都会这样做，见下文的 N-1），写入 skipped 表，reason 为 `target_locked`。运行手册中说明：rescan 的轮次主要来自这一项；
     - 两种都读不到：计为 dangling。
   - 读到目标之后调用 `derive_one(o, relation, {"derived":true,"backfill":run_id})`：
     - 新插入的行写入 created 表；
     - `on conflict do nothing` 没有插入时计为 existing，不打标记，也不进 created 表。
6. **写入跳过与游标**：
   - 被 skip locked 跳过的源对象写入 skipped 表，reason 为 `source_locked`；
   - 游标推进到 `hi`；
   - [R-1] 因为超过 750 ms 而提前停止时，用最后一个已处理的源对象 id 代替 `hi`：差集与 skipped 表只计算 `(after, 这个 id]` 范围内的对象，游标也推进到这个 id。还没处理的锁住的对象，在本批提交时随之释放，留给下一批；
   - 累加计数。
7. 提交。

**错误分类**（[C-1]）：

| 错误 | 处理 |
| --- | --- |
| `55P03` lock_not_available（lock_timeout） | 放弃本批，可以重试：`run` 等待 `pause_ms` 的 2 倍后，用同一个意图 id 重试 |
| `57014` query_canceled（statement_timeout，1 s） | 放弃本批；批大小减半之后重试，因为批大小变了，意图 id 也随之改变（见下文 [N-6]）。有 750 ms 的提前提交，正常情况下不会出现 |
| `40001`、`40P01`（序列化失败、死锁） | 同上 |
| `23503` foreign_key_violation | 按上面的设计不应出现，因为目标已经持有 KEY SHARE 锁；如果出现，放弃本批重试一次；同一批第二次仍然出现，才中止，reason 为 `unexpected_fk` |
| 其他（42501 基数冲突、world 守卫拒绝、签名失败等） | 立即中止 |

**为什么不用逐对象的异常块**：PL/pgSQL 的每一个 `begin … exception` 块都是一个子事务。一批 200 个对象就有 200 个子事务，超过每个后端 64 个子事务的缓存后，会拖慢**所有会话**的快照。用锁避免错误，而不是事后捕获错误。

**[N-6] 意图 id、批大小与 claim**：

- 意图 id 包含批大小：`run_id:source_type:world:after_object_id:batch`。payload 由意图 id 完全确定（批大小、after、源类型、world），所以同一个意图 id 永远携带同一个 payload，claim 不会因为 payload 摘要不一致而拒绝。
- **可重试的失败不写入 claim**：claim 与本批的写入在同一个事务内，55P03、57014、40001、40P01 会让整个事务回滚，claim 也随之回滚。因此：
  - 用同一个意图 id 重试（55P03 等），执行的是一次全新的调用，不会拿到记录下来的失败结果；
  - 57014 减半之后，意图 id 不同，旧的意图 id 从未提交过。
- 只有已提交的调用才有 claim：成功的批，以及以 `aborted` 收场的调用（目录变化、运行状态不是 running），这些调用的状态改写会在提交后返回。同一个意图 id 重放时，得到的是记录下来的结果。
- 重试不会跳过本批的范围：游标只在提交时推进，失败的批没有提交，游标仍然停在 `after`。

**断点续跑**：游标、计数、清单和跳过表都与本批的关系在同一个事务内提交，中断只会丢失没有提交的那一批。用同一个意图 id 重放，结果幂等（claim 中已有记录）；不同意图 id 重做同一范围也幂等：relation_id 是确定的，`on conflict do nothing`，各表都有主键。

**批大小与限速**（参照保留期 sweep 的 `batch (1,1000)` 等有上下界的部署配置）：

| 参数 | 默认 | 范围 | 作用 |
| --- | --- | --- | --- |
| `batch` | 200 | 50–1000 | 每批的源对象数 |
| `target_batch_ms` | 300 | 100–2000 | 一批超过这个时长，下一批减半（不低于 50）；连续 5 批都低于一半，增加 50%（不超过上限） |
| `pause_ms` | 200 | 50–10000 | 两批之间的间隔 |
| `max_batches` | 不限 | — | 分时段执行；stage 第一次运行用 `--max-batches 1` 校准默认参数（§9） |

**对 real 写入的影响**（第 2 版写的是“不会压住 real 写入”，这个结论不完整，[N-1] 修正如下）：

- **源对象**：
  - 回填只加 FOR SHARE，并且跳过已经被锁住的行；
  - 回填不修改 `ontology.objects`，也不触发 P2 的触发器；
  - 一个 real UPDATE 如果正好要改本批中的源对象，最多等待本批剩下的时间。
- **目标对象**：KEY SHARE 与普通 UPDATE（NO KEY UPDATE）兼容，但与 `select … for update` **冲突**。而现有代码有几条路径对对象行显式加 `for update`：
  - 0051、0175 的投递路径（锁 Consumer）；
  - 0061 的身份关联（锁 Consumer）；
  - 0015、0053、0054、0057、0058 的受治理编辑提交（锁被编辑的对象）。
  
  Consumer 正是归属类关系最主要的目标类型。后果有两个方向：
  - real 投递、编辑或身份关联遇到正被本批锁住的目标时，会等本批提交：典型值在 `target_batch_ms` 以内，[R-1] 最长约 1 s（750 ms 提前提交，statement_timeout 1 s），低于这些路径 3 s 的 lock_timeout，所以它们只会变慢，不会失败；
  - 反过来，这些路径持锁期间，回填会把目标记为 `target_locked`，所以这一项会频繁出现，rescan 的轮次也会增加。
- **控制办法**：
  1. **持锁时间的上限**：目标的 KEY SHARE 在派生这个对象的前一刻才加，但要持有到本批提交，所以上限就是本批的时长。
     - 硬上限是 [R-1] 的 750 ms 提前提交和 1 s statement_timeout；
     - 以目标为 Consumer 的源类型，另把批大小上限设为 100、`target_batch_ms` 设为 150，让典型的持锁时间远低于硬上限。
  2. [R-4] **按目标去重**：同一批中多个源对象指向同一个 Consumer 时，只在第一次加锁；后面再加同一把锁什么也不做，不再写 xmax 和 WAL。去重**不会缩短**持锁时间：这把锁从第一次加上一直持有到本批提交。它减少的只是锁记录和 multixact。
  3. **跳过最近写过的目标**：目标的 `updated_at` 在最近 10 秒内，就不加锁，直接写入 skipped 表（`target_locked`），留给 rescan。rescan 的退避（1 s 到 60 s）给写入留出了空隙。
     - [R-2] 这一条**避不开投递**：投递路径（0051、0175）只对 Consumer 加 `for update`，并不修改 Consumer，`updated_at` 不变。它只对受治理编辑（会修改对象，`updated_at` 随之更新）有效；对身份关联（0061）是否有效，取决于它是否修改了 Consumer，实现时核对。
     - 投递得到的保护来自第 1 条：它最多等约 1 s，而且投递遇到 55P03 本来就会重试。
  4. **观测**：
     - [R-5][V-2] 回填的每一批在提交前统计正在等待**本事务**的会话数：`pg_stat_activity` 中 `pg_backend_pid() = any(pg_blocking_pids(pid))` 的会话数。`pg_blocking_pids` 同时覆盖行锁（等待事务 id）和表级锁，也包括排在后面、被前面的等待者间接挡住的会话，比按 `transactionid` 过滤 `pg_locks` 更直接。它只需要会话的 pid，不读 `wait_event` 等受 `pg_read_all_stats` 限制的列（N-4），实现时用测试确认 owner 能看到其他角色的 pid。结果作为本批输出的 `waiters` 字段。`sample` 运行在两批之间，那时回填不持有任何锁，看不到这类等待，所以这一项必须在批内统计；
     - stage 校准时，单独记录投递路径（0323 的 relay_step 与写请求 p95）在回填期间的变化；
     - [R-6] stage 校准时，还要统计受治理编辑和身份关联两条路径上的 55P03：API 写请求的错误数（0323），以及这两条路径日志中的 lock_not_available 计数。预期为 0，不为 0 就暂停并缩小批大小。
  5. **后续选项（不在 P3 中）**：把这些路径改为 FOR NO KEY UPDATE（与 KEY SHARE 兼容）。这要改动已发布的函数，需要新迁移和复制件守护，另行立项。
- **咨询锁**：首批全是 many_to_one，派生不加按关系名的咨询锁。以后加入 one_to_one 类型时，它的批大小上限单独设为 50（§8 有测试）。
- **[I-1] WAL 与 vacuum**：
  - 写入量来自两部分：每条新关系约 300 字节的元组加索引；每个被 FOR SHARE 锁住的源对象，会在堆元组上写 xmax（heap_lock WAL），checkpoint 之后第一次修改这一页还要写整页镜像；
  - 目标对象的 KEY SHARE 也会写 xmax；目标已经被其他会话以 KEY SHARE 锁住时（例如外键检查），还会产生 multixact；
  - 估算：stage 上能派生的源对象是万级，乘以每个对象约 100 字节的锁记录，再加上整页镜像（最坏每页 8 KB，页数是对象数的约 1/30），总量在几十 MB 以内；
  - multixact 增长与 `n_dead_tup` 列入观测（§5）。
- **[S-1] 扫描谓词与索引**：
  - objects 的主键是 `(tenant_id, type_name, object_id)`，扫描用的是主键的范围扫描，按 `object_id` 有序，`world` 与 hex 条件在扫描时过滤；
  - 如果某个源类型中其他 world 的对象占多数（例如 sim 很大、real 很小），过滤的代价就不可忽略，那时再加索引 `(tenant_id, type_name, world, object_id)`；
  - 实现时在 stage 的数据规模上附上 EXPLAIN（ANALYZE, BUFFERS），由数据决定是否加索引。

## 4. 并发

### 4.1 与 P2 派生触发器

| 交错情况 | 结果 |
| --- | --- |
| real 事务 T 在回填读取之前已经提交了新属性 | 回填看到的是新值，结果正确 |
| T 在回填读取时持有这个源对象的行锁 | `skip locked` 跳过，写入 skipped 表 [C-2]，之后由 rescan 处理 |
| 回填已经对源对象加了 FOR SHARE，T 随后要修改它 | T 等回填提交之后才继续；T 提交时，触发器看得到回填写入的行，属性不再匹配时会删除它（带 `derived:true`），结果正确 |
| T 与回填插入同一个 relation_id | 后到的一方等待，然后 `on conflict do nothing`。如果后到的是回填，计为 existing，结果正确 |
| 目标对象正在被删除（对象删除、擦除 purge 删除端点），或者被 `for update` 锁住（[N-1]） | `for key share skip locked` 读不到，写入 skipped 表（`target_locked`）[C-1]；对方删除提交之后，rescan 时计为 dangling |
| sim world 已经处于 deleting 或 deleted 状态 | 第 2 步直接把这个 world 的游标标为 done（`skipped_world_purge`）[N-2] |
| 本批进行中才开始的 sim purge | purge 先删关系 R（未提交），回填对 R 的 relation_id 执行 `insert … on conflict do nothing` 时，要等待这一行“已删除、未提交”的冲突行；而 purge 接着删除源对象时，又要等待回填的 FOR SHARE，于是形成环。回填的 lock_timeout（200 ms）小于 deadlock_timeout（默认 1 s），所以回填会先以 55P03 放弃，purge 照常完成。下一批在第 2 步看到 deleting，就跳过这个 world [N-2] |

**[C-2] 为什么 skip locked 跳过的对象必须持久化**：行被锁住，不代表它的属性正在改变。可能的情况有：

- 对方只改了 properties 以外的列（0321 的 WHEN 子句不会排队派生）；
- 对方用 `select … for update` 做 CAS，但没有修改属性；
- 对方事务最后回滚。

这几种情况下都没有触发器替回填完成派生，所以第 1 版中“一定会被自己的事务派生”的说法不成立，已经删除。

**rescan**：

- 每个 (源类型, world) 的游标走完之后，`run` 调用 `rescan`；
- rescan 按 skipped 表逐个处理，同样使用 skip locked；处理成功的行在同一事务中删除，仍然被锁住的行 `attempts + 1`；
- 每一轮之间按指数退避（1 s、2 s、4 s…，最多 60 s）；
- 所有游标走完之后，skipped 表必须为空，运行才能变为 `completed`；
- 某个对象 10 轮之后仍然被锁住，`run` 停止循环并退出（运行仍是 `running`），在 status 中列出这些对象 id，由运维判断（通常是一个长事务），之后再执行 `run` 继续 rescan；
- [S-9] 源对象在 rescan 时已经不存在（被删除）：计为 `skipped_vanished`，并删除 skipped 表中的这一行；源对象所在的 sim world 已经进入 deleting：同样删除这一行，计为 `skipped_world_purge`。这样 skipped 表一定能清空，运行不会因为对象被删除而永远无法完成。

**[S-2] replica 模式写入**：

- 回填期间以 `session_replication_role=replica` 写入的对象，如果 object_id 落在游标之后，会被回填扫到；如果落在游标之前，则会遗漏，verify 会把它报出来；
- 运行手册规定：**回填期间不得以 replica 模式写入**（恢复、种子数据都等回填完成之后再做），已经写入的，回填完成后重新运行一次；
- preflight 还会检查是否有会话正处于 replica 模式（`pg_stat_activity` 上不可见，所以通过 `sample` 的提示告诉运维人工确认）。

### 4.2 来源标记

| 来源 | metadata |
| --- | --- |
| 派生（P2 触发器） | `{"derived":true}` |
| 回填新建 | `{"derived":true,"backfill":"<run_id>"}`，同时写入 created 表 |
| link（0016） | 由 link 指定，不含 `derived` |

- 回填遇到已经存在的关系，不改它的 metadata，也不把它记进清单。
- 触发器删除关系的条件是 `metadata @> '{"derived":true}'`，所以回填的行带着额外的 backfill 键，仍然会被触发器按属性维护（L6 已对照 0321 确认）。

### 4.3 与擦除

**风险**：purge 在完成之前，会先删除被擦除对象作为端点的关系（0138 第 211 行、第 267 行）；而这些对象的属性要到完成时才改为假名（0305–0307）。在这个窗口内，回填可能读到原来的 hex 值，把关系又写回去。

**做法**：

- 每一批在步骤 3 对 `control.nexloop_consumer_erasures` 加 SHARE 锁（同样受 200 ms lock_timeout 限制）；
- 然后读出 [I-6] state 在**白名单** `('blocking','settling','waiting_unknown_effects','purging','completed','completed_with_holds')` 中的擦除，得到它们的 Consumer 集合；
- 归属于这些 Consumer 的对象一律跳过，计为 skipped_erasure。归属判断沿用 0315 的属性规则，与 purge 同源。

**白名单**：

- 0132 的状态约束目前有 7 个状态，白名单是除 `pending_confirmation` 之外的全部 6 个；
- 以后新增状态时，preflight 检查约束中的状态集合是否与已知集合一致，不一致就作为 blocking 项（§6），由人决定新状态属于哪一边，不会被默认归入任何一边。

**[I-6] SHARE 锁的影响**：

- SHARE 锁与擦除表上的所有 INSERT 和 UPDATE 冲突；
- 写这张表的位置一共 30 处，分布在 0132、0138、0144、0145、0227、0307 中，都在擦除请求的创建，以及 keeper 的擦除步骤（`ErasurePort.step`：状态推进、`phase` 推进，各带 `updated_at`）之中；
- 没有心跳类的周期写入，也没有 real 对话路径上的写入；
- keeper 的擦除步骤和创建擦除请求的 Action，都**没有**设置 lock_timeout，遇到 SHARE 锁会**等待**本批提交，不会报错退出；
- 等待时长不超过本批的时长：典型值在 `target_batch_ms` 300 ms 以内，[R-1] 最长约 1 s；
- keeper 本身按周期运行，一次步骤晚几百毫秒，对擦除的进度没有实际影响；
- 反过来，擦除步骤正在执行时，回填拿不到 SHARE 锁，200 ms 后放弃本批，稍后重试；
- 实现时补一条测试：keeper 的步骤在回填持锁期间等待，然后成功（§8）。

**为什么不用咨询锁**：现有的擦除函数（0138 等）都不加 Consumer 级的咨询锁。要用咨询锁，就必须用新迁移重新定义 real 擦除路径上的状态推进函数。L6 已同意用表锁代替，前提是写明上面两点。

### 4.4 与 link/unlink

- 经 0016 link 写入的同名关系：relation_id 相同，`on conflict do nothing`，回填不改它。
- 经 unlink 删除的派生关系：如果属性仍然指向目标，回填会把它补回来。这与 P2 一致（属性是事实来源，I-2）。

## 5. 观测与中止

**每一批的输出**：`run` 向 stdout 写一行 JSON，`status` 也能读到。内容包括：

- run_id、源类型、world、批大小、耗时；
- 按关系类型分别给出 created、existing、dangling、skipped_erasure，以及 skipped 表新增的行数；
- 是否因为可重试的错误放弃（并给出错误码），以及下一批的批大小。

**`sample`**：`run` 每 30 秒调用一次，结果只含计数，见 §2 [I-3]。

| 指标 | 来源 | 暂停阈值（默认） |
| --- | --- | --- |
| API 写请求 p95（**后续项**，调度员 2026-10-25：第一版的 runner 不接入，靠等待者计数与锁等待暂停；stage 校准时人工对照 0323 的数字） | 0323 API 指标 | 比基线高出 25%，或者超过 500 ms |
| 等待回填的会话数 [N-4][R-5] | 每批输出的 `waiters`：提交前等待本事务的会话数 | 最近 5 批中有 2 批大于 3 |
| 三张表上的表级锁等待（背景） | `sample`：`pg_locks` 中这三个 relation oid 上 `granted = false` 的锁 | 大于 0 时，等它归零再开始下一批 |
| 编辑与身份关联的 55P03 [R-6] | API 写请求的错误数（0323）与这两条路径的日志计数 | 大于 0 |
| 可重试错误的放弃率 | 自身计数 | 最近 20 批中超过 20% |
| WAL 速率 | `pg_current_wal_lsn` 的差值 | 超过 20 MB/min |
| multixact 与死元组 [I-1] | `pg_stat_user_tables.n_dead_tup`（objects、relations）；`mxid_age(datminmxid)` | n_dead_tup 增长超过 objects 总行数的 20%；mxid_age 超过 autovacuum_multixact_freeze_max_age 的一半 |

**[S-5] 基线**：

- 取 `start` 之前 10 分钟的 API 指标，并且要求其中至少 200 个写请求；
- 样本不够时，延长到 60 分钟；
- 仍然不够（低峰期），改用固定上限 500 ms，并在输出中注明基线不足。

超过阈值时，`run` 暂停发起新的批（数据库中的状态不变），之后每 60 秒采样一次，连续 3 次正常再继续。

**中止**：

- 中止的原因有：§3.2 中“其他”类错误、第二次出现的 23503、目录摘要变化 [I-2]、执行身份被吊销或签名失败；
- 运维也可以执行 `relation_backfill.py abort`：
  - [S-5] 这是一次独立的签名 `abort` 调用，直接把 run 行改为 `aborted`，不依赖 `run` 进程；
  - 正在执行的 `run` 在下一批的步骤 2 读到这个状态后停止；
  - 吊销身份只作为最后手段。

**续跑规则**：

- **进程中断**（进程被杀、连接断开、主机重启）不是中止：运行仍是 `running`，再执行一次 `run`，从游标继续。
- [I-3] **中止的运行（任何原因）都不续跑**：修复后重新执行 preflight，开一次新的运行。
- 因为 `catalog_changed` 而中止时，还要注意：
  - 新版本可能去掉了某个 `source_property`，而触发器只在属性变化时删除关系，回填已经写入的、按旧映射建立的关系可能残留；
  - 必须先执行 verify：多余关系为 0 时，开一次新的运行；
  - 不为 0 时，先 revert 这次运行，再开新的运行。

**verify**（只读）：对每个 (关系类型, world) 给出：

- A：属性是 64 位 hex、目标在同一 world 中存在、并且不属于擦除中 Consumer 的源对象数；
- B：这些对象中实际已有这条关系的数量；
- 另外分别给出：悬空引用、跨 world 引用、因擦除跳过的数量、**多余的关系**（不在任何属性推导结果中，link 写入的单独列出），以及 skipped 表中剩余的行数。

验收条件：每一行 A = B；多余关系为 0；skipped 表为空 [C-2]。

## 6. 与 P1 §3 预检的关系

`preflight` 在数据库内执行 P1 §3 与 relation-types-design.md §2 的计数，按 (关系类型, world) 输出。

**blocking 项**：任意一项出现，`start` 就拒绝整次运行。L6 已同意整次拒绝。

1. `target_in_other_world > 0`（ADR-038 §2.4）。
2. one_to_one 或 one_to_many 类型的 `sharing_a_target > 0`。
3. RelationshipAssessment 已有的 `relation_name` 与首批重名（P1 §3 第 2 条）。
4. 关系目录没有发布，或者派生映射的摘要与 `start` 声明的不一致。
5. 有被禁用的派生触发器（`pg_trigger.tgenabled = 'D'`）。
6. [S-3] 0321 的派生触发器或 P2.1 的 `derive_one` 不存在。
7. [S-3] 本 tenant 已经有处于 running 或 reverting 状态的运行（部分唯一索引同样会拒绝）。
8. [I-6] 擦除状态约束中的状态集合与 §4.3 的已知集合不一致。

**只报告、不阻塞**：悬空引用、非 64 位 hex 的值、擦除中的 Consumer 数量。这些都不会建关系，原样进入 verify。

## 7. 回退

1. **revert**：`relation_backfill.py revert --run <run_id>`，由同一个执行身份执行。
   - 按批（每批 500 条）删除 created 表中、并且 `metadata @> {"backfill": run_id}` 的关系，同时从 created 表中删除这一行。
   - 同样是短事务，同样受 lock_timeout 约束。
   - 完成后 state 改为 `reverted`。
2. **[I-5] revert 之后 verify 不会平**：
   - 回填先插入、触发器随后 `on conflict do nothing` 的关系，会记在回填名下，revert 时被删除；
   - 属性仍然指向目标、但触发器不会再派生（属性没有变化），于是 A ≠ B；
   - 这符合“回到回填之前”的语义：revert 之后 verify 的缺口，等于这次运行的 created 数减去之后被触发器删除的数；
   - 要让关系重新完整，就再执行一次回填。
   - 触发器之后因为属性变化而删除、或者重新建立的关系，归属判断是：只要仍然带着本次 run_id 标记，就算回填的；触发器重新建立的行只带 `derived:true`，不受 revert 影响。
3. **[I-5] P4 之后执行 revert 的前提**：读守卫的属性后备仍然存在，也就是还在 ADR-038 §3 第 7 点的观察期内，P5 还没有移除后备。
   - `revert` 检查后备是否还在（P4 实现时提供一个可以查询的标记，例如派生映射或目录清单中的 `fallback_active`）；
   - 后备已经移除时，拒绝执行 revert。
4. **最坏情况**：中止之后 revert 也失败，这时吊销执行身份。已经写入的关系由 P2 的触发器随属性维护；P4 之前没有任何读方使用它们。
5. 回退**不**撤销 P2.1、P2、P1，它们各自通过新迁移回退。

## 8. 测试与演练（实现时）

**本地 PG**：`tests/test_relation_backfill_pg.py`，用例如下：

- **基本**：以 replica 模式写入一组存量对象，回填之后 verify 每一行 A = B，skipped 表为空。
- **续跑**：
  - 在两批之间中断进程，再执行 run，从游标继续，created 表与关系一一对应；
  - 用同一个意图 id 重放 batch，得到相同的结果，不推进游标 [S-6]。
- **跳过表** [C-2]：
  - 另开一个会话以 `select … for update` 锁住一个源对象（不改属性）；
  - 这一批把它写入 skipped 表，进程退出；
  - 续跑后 rescan 补上，verify 平。
- **并发删除** [C-1]：
  - 另开一个会话删除目标对象并挂起；
  - 回填写入 `target_locked`，不中止；
  - 对方提交之后，rescan 时计为 dangling；
  - [N-2] sim world 处于 deleting 状态时，第 2 步直接跳过（`skipped_world_purge`）；回填进行中才开始的 purge，回填以 55P03 放弃本批，purge 完成，下一批跳过这个 world；两种情况下运行都不中止。
- **目录变更也作用于 rescan** [N-5]：rescan 前发布新版本，rescan 返回 aborted。
- **属性并发修改**：经 Action 修改同一批对象的属性，不留下过期的关系。
- **批尾被锁** [N-3]：被锁住的对象恰好是本批范围内的最后一个，它会进入 skipped 表，游标停在 hi，rescan 之后 verify 平。
- **可重试错误** [N-6]：注入 55P03 后，同一个意图 id 重试成功；注入 57014 后，批大小减半、意图 id 改变，重试成功；两种情况都不会跳过本批的范围，claim 中没有失败记录。
- **投递路径的等待** [N-1]：另一个会话以投递路径的方式对 Consumer 执行 `for update`，在回填持有 KEY SHARE 期间等待，等待时长不超过本批时长，之后成功；回填持锁之前就被锁住的 Consumer 记为 `target_locked`。
- **反例：受治理编辑不能因回填失败** [R-1]：
  - 在 3000 ms 的 lock_timeout 下（与真实路径的默认值相同），经受治理编辑 Action 修改一个 Consumer，同时回填的一批正持有这个 Consumer 的 KEY SHARE；
  - 用一个注入的慢派生把这一批拉长，断言 750 ms 提前提交生效、编辑 Action 成功（不是 55P03）；
  - [V-4] 同时断言整批的耗时（从事务开始到提交）小于 1 s；
  - [V-1] **对照组**：同一测试再跑一次，这次（只在测试中）**同时**关掉提前提交、并把回填的 statement_timeout 放宽到 3 s 以上（例如 5 s），让这一批持锁超过 3 s。断言编辑在这种情况下以 55P03 失败。只关提前提交而保留 1 s 的 statement_timeout，这一批会在 1 s 时被取消，编辑本来就不会失败，对照组就证明不了任何东西。对照组成立，才说明实验组的通过是因为上限生效，而不是因为根本没有冲突。
- **提前提交** [R-1]：一批在 750 ms 停止时，游标、差集和 skipped 表都只覆盖已处理的范围，没处理的对象在下一批处理，verify 平。
- **批内等待计数** [R-5]：另一个会话等待回填持有的锁，这一批输出的 `waiters` 为 1；`sample` 不统计这类等待。
- **sample 的等锁计数** [N-4]：另开一个角色制造一次行锁等待，`sample` 能统计到。
- **rescan 时源对象已被删除** [S-9]：计为 skipped_vanished，skipped 表清空。
- **擦除** [I-6]：
  - 擦除推进到 purging 时，相关对象计入 skipped_erasure；
  - keeper 的擦除步骤在回填持锁期间等待，之后成功。
- **目录变更** [I-2]：运行中途发布 goal_of v2，下一批中止，reason 为 `catalog_changed`；中止的运行不再接受 batch 或 rescan。
- **abort** [S-5]：从另一条连接调用 `abort`，`run` 在下一批停止。
- **预检**：每一个 blocking 项都让 `start` 被拒，包括已经有活动运行的情况 [S-3]。
- **one_to_one** [S-6]：为测试发布一个 one_to_one 类型，回填走咨询锁路径；存量有冲突时，预检报 blocking。
- **revert**：只删除带本次 run_id 标记的关系；revert 之后 verify 的缺口与 §7 第 2 点的推算一致；后备已移除时拒绝 revert（P4 之后补充）。

**stage**（调度员执行，按顺序）：

1. preflight；
2. `run --max-batches 1`，附上 EXPLAIN；用这一批的耗时和 WAL 量校准 §3.2 的默认参数；
3. run；
4. verify；
5. revert 演练；
6. 再 run；
7. verify。

每批的耗时、等锁会话数、API p95 与基线的对比、WAL 与 multixact 的变化，作为 P3 的证据记录。

## 9. 待定点的结论（L6 第 1 版复核）

1. 用表锁代替咨询锁实现与擦除的互斥：**采用**，条件见 §4.3 [I-6]。
2. 默认参数（批 200、目标 300 ms、间隔 200 ms）：**暂定**，在 stage 上用 `--max-batches 1` 实测后校准；可重试错误放弃率的阈值，按实测的扫描量与锁写入量重新确定。
3. 预检有 blocking 项时拒绝整次运行：**采用**。
4. `derive_one` 的抽取单独拆成 P2.1 迁移先上线：**采用**（§2）。

## 10. 第 1 版复核意见对照

| 编号 | 处理位置 |
| --- | --- |
| C-1 外键 23503 | §3.2 第 5 步（目标加 KEY SHARE skip locked）、错误分类表；§4.1 最后一行；§8 |
| C-2 被跳过对象的持久化 | §3.1 skipped 表；§3.2 第 6 步；§4.1 rescan；§5 verify 条件 |
| I-1 FOR SHARE 的写放大 | §1 按源类型扫描；§3.2 第 4 步 hex 过滤；WAL 估算；§5 multixact 与死元组 |
| I-2 目录变更 | §3.2 第 2 步；§5 续跑规则 |
| 实现第 1 片复核 C-1、I-1、I-3、S-6 | §3.1 状态与计数；§3.2 第 1 步的约定；§4.1 rescan；§5 |
| I-3 连接预算与角色 | §2 |
| I-4 签名、意图 id、审计量 | §2 |
| I-5 revert 语义 | §7 第 2、3 点 |
| I-6 擦除表锁 | §4.3；§6 第 8 项 |
| S-1 索引与 EXPLAIN | §3.2 |
| S-2 replica 写入 | §4.1 |
| S-3 预检补充 | §3.1 部分唯一索引；§6 第 6、7 项 |
| S-4 world 依据 | §1 |
| S-5 进程外 abort、基线样本 | §3.2 第 2 步；§5 |
| S-6 测试补充 | §8 |
| S-7 P2.1 | §2、§9 |
| N-1 目标 KEY SHARE 与 `for update` 冲突 | §3.2“对 real 写入的影响”；§4.1 表；§8 |
| N-2 sim purge 的锁环 | §3.2 第 2 步；§4.1 表；§8 |
| N-3 跳过集合与游标 | §3.2 第 4、6 步；§8 |
| N-4 sample 的等锁信息 | §2；§5 表；§8 |
| N-5 rescan 与 revert 的检查 | §3.2 第 2 步末尾；§8 |
| N-6 意图 id 与重试 | §3.2 错误分类表与“意图 id、批大小与 claim”；§8 |
| S-8 已有关系的对象不加锁 | §3.2 第 4 步 |
| S-9 rescan 时源对象已被删除 | §4.1 rescan |
| S-10 target_locked 的措辞 | §3.2 第 5 步；§4.1 表 |
| R-1 单批持锁的硬上限 | §3.2 第 1、6 步与错误分类表；“对 real 写入的影响”；§4.3；§8 反例测试 |
| R-2 最近写过的目标避不开投递 | “对 real 写入的影响”第 3 条 |
| R-3 “关系已存在”的求值时机 | §3.2 第 4 步 |
| R-4 去重的措辞 | “对 real 写入的影响”第 2 条 |
| R-5 只统计等待回填的会话 | §2；“对 real 写入的影响”第 4 条；§5 表；§8 |
| R-6 编辑与身份关联的 55P03 | “对 real 写入的影响”第 4 条；§5 表 |
| V-1 反例测试的对照组 | §8 |
| V-2 等待者计数用 pg_blocking_pids | “对 real 写入的影响”第 4 条 |
| V-3 750 ms 从事务开始算起 | §3.2 第 1 步 |
| V-4 整批耗时小于 1 s | §8 |
