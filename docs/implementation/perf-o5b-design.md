# O5b 设计稿：同一语句、同一检查阶段内的读断言去重

分支 `o5b-design`，BASE main `194e12b`。只写文档，不改代码和迁移。L1 与 L4 共同设计；L4 的“锁后尾验占比”测量（`perf-o5b-measure`）出来后，补全 §7 并定稿。

状态：**初稿（已含调度员裁定）**。§7 的数字待 L4 填入。裁定结果：只实现级别 1；级别 2 为候选、未采用；同意授予 TEMP 权限，但须补齐 §6.2 第 7 项的三种伪造负例；验收线同意。

## 0. 目标与硬约束

- **目标**：`authz.nexloop_assert_read_authority` 在同一条语句内重复率为 72–78%（v4[complete] 每条语句 30329→6553 次，两 Pi 17579→4863 次）。每条 activation 命令约调用 170 次，占 `runtime_activation_command` 约 88% 的耗时（`perf-o1-o4.md` s3m 复测 §2）。要在不改变判定的前提下去掉同一阶段内的重复。
- **硬约束**：
  - **H1** 任何锁等待之后的提交尾验必须完整重跑，不能用之前的结果代替。
  - **H2** L1 “锁等待后最终期限”系列测试照旧有效，见 §6.1。
  - **H3** 不放宽任何时限，不延长任何证明的有效期。
  - **H4** 已发布迁移及其校验和不动，新迁移使用临时编号。
  - **H5** 只缓存“允许”；拒绝每次都重新计算。
  - **H6** 不新增连接，不改变 LifecycleLock 和连接池的语义。

## 1. 现状：被去重的函数做了什么

最外层是 0084 的分派函数 `nexloop_assert_read_authority`，调用链如下：
- `type-property-v1` → `assert_derived_property_access`；
- 否则 → `_before_property_access_v0083`（即 0077 的分派函数）：
  - 带 `derivation` → `assert_derived_message_read`（0077 / 0080 / 0086）；
  - 否则 → `_before_message_read_v0072`（即 0011 的核心）。

0011 核心每次调用做以下几件事：
1. 对凭据行加 `FOR SHARE`，第一次取身份快照，校验 tenant / credential / principal / world / directory_hash / operation / `expires_at > clock_timestamp()` 以及 12 条事实。
2. 按固定顺序对每条事实行加 `FOR SHARE`，`load_authority_fact_snapshot` 的 record_hash 必须与 claims 中的一致。record_hash 是 payload 的 sha256，与时间无关。
3. 对租户行加 `FOR SHARE`，第二次取身份快照，再校验 directory_hash 与 `expires_at > clock_timestamp()`。
4. 返回 `binding`。

派生路径（0077 / 0084 / 0086）同样对规则事实（`message_read_rule`、`property_access_rule`、`property_group_restriction`）和被派生的对象行（Message / Conversation / outbox / object_type_versions 等）加 `FOR SHARE`，再递归断言内层的 Consumer READ / type READ。

**关键事实**：断言读到的每一项数据库输入都被本事务以 `FOR SHARE` 锁住，直到事务结束。因此在同一事务内，第一次断言成功之后，**其他事务不可能提交对这些输入的修改**：撤权方会阻塞在我们的共享锁上。剩下仍可能变化的只有三类：
- (a) 时钟：claims 及其嵌套 envelope 的 `expires_at`、规则的 `valid_until`、凭据的期限；
- (b) 本事务自己的写入；
- (c) 子事务回滚：回滚会释放该子事务取得的行锁。

§2 的去重正确性就建立在这三点之上。§8 要求实施前对“全部输入都已加锁”逐个函数审计。

## 2. 去重的作用域

| 作用域 | 键的组成 | 收益 | 风险 | 结论 |
|---|---|---|---|---|
| 语句 | txid + `statement_timestamp()` + 摘要 | 覆盖已测得的 72–78% | 最低：Python 侧的锁等待（例如 `_effect_tool` 的 Run 锁）都在独立语句中，自动失效 | **采用为上限** |
| 阶段（语句内被屏障切开的段） | 语句键 + 阶段号 | 语句级收益减去锁后尾验 | 需要在语句内的加锁点插入屏障 | **采用**：去重单位 = 语句 ∩ 阶段 |
| 事务 | txid + 摘要 | 再加上跨语句的重复（未测，预计较小：Python 每个请求的语句数不多） | Python 在两条语句之间做的事 SQL 看不到 | **不采用**；如需要另立项 |
| 跨事务 / 跨请求 | — | — | 破坏 H1 / H2，也违反 O1 的约定 | **禁止** |

**摘要键**为 `sha256(p_digest || p_world || p_claims::text)`。jsonb 的文本是规范化的；claims 包含 `expires_at`、directory_hash、12 条 record_hash 以及派生的 basis。claims 中任何字段变化都会落到不同的键上，不需要单独比较。

## 3. 失效条件

只有“命中”路径跳过昂贵的部分（事实加锁、事实快照、两次身份快照）。每次命中仍然执行以下两项：
- **时钟复核（每次命中必做）**：条目保存 `deadline = least(claims.expires_at, 所有嵌套 envelope 的 expires_at, 规则 valid_until, 凭据 expires_at)`。命中时要求 `deadline > clock_timestamp()`，否则视为未命中，回到完整断言（它会以原错误码 42501 拒绝）。错误信息与路径不变，所以期限类测试的断言不受影响。
- **事务与语句校验**：键中的 txid、`statement_timestamp()` 必须与当前一致。不一致等同于未命中。

### 3.1 撤权
- **其他事务的撤权**：被 §1 的 `FOR SHARE` 挡在本事务之后，语义与现在相同（现在也是阻塞）。本事务提交后，下一条语句或下一个请求重新断言。
- **本事务内的撤权或配置写入**：在断言读取的表上加语句级 `AFTER INSERT/UPDATE/DELETE` 触发器，执行时把本事务的 memo 世代加 1，作废全部条目。涉及的表：
  - `authz.nexloop_authority_facts`
  - `authz.nexloop_service_credentials`
  - `control.nexloop_tenants`
  - 派生输入表：`ontology.objects`、`ontology.object_type_versions`、`runtime.nexloop_conversations`、`runtime.nexloop_conversation_messages`、`runtime.nexloop_message_outbox`

  `ontology.objects` 写入较频繁，触发器只做一次计数加 1，代价可以忽略；作废后最多回到现在的成本。

### 3.2 过期
见上面的时钟复核。证明的有效期不会因为命中而延长（H3），因为 deadline 取自 claims 本身，而不是首次校验的时刻。

### 3.3 锁等待（H1）
语句内的每个“可能等待的加锁点”之后调用 `authz.nexloop_read_memo_barrier()`，阶段号加 1，此后的断言全部未命中、完整重跑。级别 1 必须覆盖的加锁点：

| 来源 | 加锁点 | 说明 |
|---|---|---|
| 0039 | `pg_advisory_xact_lock('nexloop-runtime-execution:'||run)` 之后；marker `insert … on conflict` 之后（可能等待并发插入的同一行） | 0039 的第 2、3 次 `_v0038` 调用因此都是完整尾验 |
| 0085 | `claims_tail` 中 `pg_advisory_xact_lock('nexloop-role-effect:'||run)` 之后 | Run 级串行之后的提交尾 |
| 0065 | `nexloop-action` advisory 锁之后；第 71 行 `FOR UPDATE` 之后 | 收据对账 |
| 0052 / 0053 / 0063 / 0081 | 第 58 / 138 / 120 / 192 行的 `FOR UPDATE` 之后 | context artifact、catalog、role runtime、role policy 绑定 |
| 0011 `read_object` | 对象行 `FOR SHARE` 之后（函数末尾第二次断言 `a` 是尾验） | 见 §3.4 |
| Python `_effect_tool` | `pg_advisory_xact_lock('nexloop-role-effect:'||run)` 是独立语句 | 语句作用域自动失效，不需要屏障 |

**屏障清单必须穷尽**：实施时用脚本列出 activation / effect / context 链上所有函数体中的 `for update`、`for share`（排除断言内部对授权输入的加锁）和 `pg_advisory_xact_lock`，与屏障清单逐项比对。比对作为测试固化，见 §6.3，函数体变化时自动报警。

### 3.4 级别 1（采用）与级别 2（候选，未采用）
- **级别 1（采用，满足 H1 的字面要求）**：每个加锁点之后都设屏障，所有尾验完整重跑。收益 = 阶段内重复。
- **级别 2（候选，未采用）**：只在 advisory 锁和 `FOR UPDATE` 之后设屏障。对非授权行的 `FOR SHARE`（例如 `read_object` 的对象行）之后不设屏障。论证：
  - 这类等待期间，授权输入都已被本事务锁住，不会变化；
  - 时钟复核照常执行；
  - 因此命中与完整重跑的判定逐项相同。

  代价是 H1 从“完整重跑”变为“时钟复核 + 不变量论证”，等于改变“派发时强制当前权限”的实现方式，触及 AGENTS.md 的安全不变量。调度员裁定：**本次不做**。即使 L4 的数据显示这部分占比很大，也必须单独写 ADR、由负责人本人确认后才能做。实施与测试只按级别 1。

### 3.5 REPEATABLE READ 快照
- Python 侧的 `repeatable read read only` 事务（`authorization.py:231`、`browser_authorization.py:72`）只做事实加载。按 PostgreSQL 的规则，只读事务中不能执行 `SELECT … FOR SHARE`，因此断言不会在这些事务中运行。
- 如果将来在 RR 读写事务中断言：快照固定，`FOR SHARE` 遇到快照之后被改过的行会报 40001（已归类为可重试，见 NX-018 L1 报告 s3n 一节）。命中路径不重新加锁，但锁本来就由首次断言持有，所以判定不变。
- 时钟复核用 `clock_timestamp()`，不是事务开始时间，RR 下同样有效。

### 3.6 子事务
memo 存放在表中，见 §4。plpgsql 的 `EXCEPTION` 块回滚子事务时，条目随之回滚，与被释放的行锁一致。在被回滚的子事务中校验过的 claims，之后必然完整重跑。

### 3.7 其他
- 不同的 digest（Source 会话与 Run 会话）、不同 world、不同租户，落到不同的键。
- 拒绝不缓存（H5）。
- `assert_edit_authority` 不纳入去重：写路径，次数少。

## 4. 存放与防伪造

- **采用：会话临时表**（调度员已同意授予 `nexloop_owner` TEMP 权限：它只出现在 NexLoop 自己的干净 bootstrap 迁移链中，不碰任何已有生产库。条件是 §6.2 第 7 项的三种伪造负例全部通过）
  ```sql
  pg_temp.nexloop_read_memo(stmt timestamptz, xid xid8, phase int, generation int, key text primary key, deadline timestamptz, binding jsonb)
  ```
  `ON COMMIT DELETE ROWS`。由 `nexloop_owner` 在会话中第一次使用时创建（security definer；需要授予 `nexloop_owner` 该库的 TEMP 权限）。另有单行的阶段 / 世代计数。
  - **防伪造**：会话用户可以抢先在自己的 `pg_temp` 中建同名表并写入伪造行。每次使用前校验 `to_regclass('pg_temp.nexloop_read_memo')` 的 `relowner` 是 `nexloop_owner`、`relnamespace = pg_my_temp_schema()`，并且没有授予其他角色任何权限。任一不符时**关闭 memo、全部完整断言**，正确性不受影响，只是不加速。会话用户无法把表的属主改成 `nexloop_owner`。
  - 只读事务允许写临时表，因此 RR 只读事务中调用也不会报错。
  - 连接池复用连接时，`ON COMMIT DELETE ROWS` 保证新事务从空表开始；即使连接被 `DISCARD ALL`，下次使用会重建。
- **不采用 GUC**：`set_config(..., true)` 的值会话用户可以随意改写，伪造需要额外的 HMAC。而且 6553 条记录（约 1 MB）每次都要整体解析，代价是 O(n)。
- **UNLOGGED 属主表（不采用）**：以 (backend pid, xid8, …) 为键，需要清理任务，并有跨后端的索引竞争。TEMP 权限已获同意，此方案只作记录。
- **开关**：读取 `nexloop_owner` 所属配置表中的一行（受治理配置），默认开启。会话 GUC 只能把它关掉（失败方向安全），不能打开。

## 5. 迁移形状（临时编号 `00xx_o5b_read_assert_memo.sql`）

1. `authz.nexloop_read_memo_ready()`（私有）：确保临时表存在且通过 §4 的校验，返回是否可用。
2. `authz.nexloop_read_memo_barrier()`（私有）：阶段号加 1。不授予会话角色执行权限，只在 owner 的函数体内调用。
3. 改名包装：
   ```sql
   alter function authz.nexloop_assert_read_authority rename to nexloop_assert_read_authority_before_read_memo_v00xx;
   ```
   新的外层函数：
   - 计算键；
   - 命中且时钟复核通过 → 返回缓存的 binding；
   - 否则调用内层，成功后写入条目（deadline 取自 claims 与嵌套 envelope 中的最小 `expires_at` / `valid_until`）。

   owner 与 revoke 写法沿用 0084 的 `do $grants$` 块。
4. 语句级触发器 `nexloop_read_memo_invalidate`，加在 §3.1 列出的表上。
5. 用 `create or replace` 重定义 §3.3 加锁点所在的函数体（0039 命令、0085 `claims_tail`、0065、0052、0053、0063、0081、0011 `read_object` 的当前最外层或私有体），只增加 `perform authz.nexloop_read_memo_barrier();` 一行。已发布的迁移文件与校验和不变。实施前须对齐当时最新的包装链：L3 的 0088–0090 和 L4 的 O2b 可能又包了一层。
6. 不涉及表结构变化和数据迁移；回滚就是把开关置为关闭。

## 6. 测试清单

### 6.1 必须照旧通过的“锁等待后最终期限”系列（H2）
- `tests/test_backend_lifecycle_capacity.py`：
  - `test_request_waiting_for_capacity_still_meets_final_deadline`
  - `test_lease_lapsing_mid_burst_fails_closed_with_operator_diagnosis`
  - `test_eight_concurrent_requests_complete_on_pool_of_four`
- `tests/test_role_policy_dispatch.py`：全部，包括 `binding_expired` 与 effect_units 并发两项。
- 0064 的 role 期限系列：`test_role_policy_enforcement`、`test_role_runtime_*` 中的 deadline 用例。
- effect intents 的“锁等待后租约过期回滚”用例：`test_runtime_effect_*`、`test_effect_intents*`。
- `test_runtime_activation*` 与 0039 marker 相关用例。

### 6.2 新增：去重正确性
1. **等价性**：activation / effect / context / v4 / 两 Pi 用例，在开关开与关两种状态下逐项比较：返回值、提交的行、拒绝的错误码。另比较命中与未命中计数，确认确实去重了。
2. **本事务内撤权**：同一语句中先断言，再（经受治理的配置函数）撤销 grant，然后同一 claims 再断言 → 世代失效 → 拒绝。
3. **并发撤权**：另一会话的撤权阻塞到本事务结束，随后的下一个请求被拒（行为不变）。
4. **语句内过期**：claims 在当前时间 +1 s 到期；首次断言后在同一语句内 `pg_sleep(1.1)`（测试专用 owner 函数）→ 命中路径拒绝，错误码 42501。
5. **屏障**：另一会话持有 `nexloop-runtime-execution:<run>`，本请求在等锁期间 claims 到期 → 等锁后的尾验拒绝。统计显示屏障之后全部未命中。0085 Run 锁同样测一遍。
6. **子事务回滚**：在 `EXCEPTION` 块内断言后回滚 → 条目消失、锁释放 → 并发撤权得以提交 → 下一次断言未命中并拒绝。
7. **伪造**（以下前三种是调度员要求的必备负例；每种都必须让 memo 关闭、回到完整断言，且伪造内容不产生任何允许）：
   - 会话角色预先在自己的 `pg_temp` 建同名表并写入伪造行；
   - 会话角色修改 `search_path`（例如把 `pg_temp` 放在最前，或指向自建 schema 中的同名表/函数）：实现中所有引用都写成全限定名，函数自带 `set search_path=pg_catalog`，属主与命名空间校验照常拒绝；
   - 在子事务中伪造条目（`SAVEPOINT` 内写入或改写条目后回滚或释放）：会话角色对表没有写权限；即使经 owner 函数写入后子事务回滚，条目也随之消失，不会残留；
   - 会话用户直接调用 `barrier`、`ready` 或内层函数 → 权限拒绝；
   - 会话用户用 GUC 打开开关 → 无效。
8. **隔离**：同一语句中两个 digest、两个 world、两个租户的同一目标，互不命中。
9. **拒绝不缓存**：首次拒绝，之后（配置修正后）再次断言不会读到旧的拒绝。
10. **连接复用**：提交后下一个事务的 memo 为空；`DISCARD ALL` 之后重建。
11. **只读 RR 事务**中调用外层函数不报错（写临时表允许）。
12. pool_depth 插件开启跑回归：单请求最多 2 个连接（H6）。

### 6.3 屏障穷尽性
脚本扫描当前函数体（`pg_proc.prosrc`）中的加锁语句，与 §3.3 的清单比对，多出或缺少都报错。测试库中运行。

## 7. 预期收益与验证

### 7.1 估算（待 L4 数据）
设 `d` = 语句内重复率（已测：0.72–0.78），`t` = 重复中“锁后尾验”所占比例（**待 L4 `perf-o5b-measure` 测出**），断言占 `runtime_activation_command` 的比例 a ≈ 0.88，命中的单次成本 c_hit / c_full 估计约为 0.03–0.05（一次索引查询加一次时钟比较，约 20–40 µs；完整断言 0.86 ms）。则

```
runtime_activation_command 降幅 ≈ a × d × (1 − t) × (1 − c_hit/c_full)
```

- t = 0.2 时约为 0.88 × 0.75 × 0.8 × 0.96 ≈ **51%**；
- t = 0.5 时约为 **32%**。

v4 慢调用 1392 ms，对应约降到 680–950 ms。authorize 的 max（1420 ms，O2a 之后）中 SQL 段同比下降。级别 2 未采用，因此 t 的三类都按完整重跑计入。

| L4 数据 | 填入值 |
|---|---|
| 语句内重复中锁后尾验占比 t（v4 / 两 Pi） | 待填 |
| 其中 advisory / `FOR UPDATE` 之后的部分（级别 1 必须重跑） | 待填 |
| 其中 `FOR SHARE` 之后的部分（级别 1 下仍完整重跑；仅作将来 ADR 的参考） | 待填 |

### 7.2 验证方法
1. **调用计数**：`track_functions=pl` 下，外层与核心 `_before_message_read_v0072` 的调用次数比即为实际去重率，应接近 d × (1 − t)。
2. **语句内重复复测**：`scripts/perf/read_assert_dup.py` 追踪（只在一次性库中），按阶段统计剩余重复，应接近 0；剩下的只能是屏障之后的尾验。
3. **延迟**：`scripts/perf/o1_o4_set.sh` 的 13 例集合，before / after 交替各两轮，比较以下指标：
   - `runtime_activation_command` 均值与 max；
   - authorize / effect_tool 的 p50 / p95 / max；
   - 慢化系数 k；
   - 以 k = 2 估算的越限次数。

   对照 O2a 表格的口径。
4. **验收线（调度员已同意）**：
   - 正确性 §6 全部通过；
   - `runtime_activation_command` 均值降幅不低于 §7.1 按实测 t 估算值的一半；
   - `relationship_context_v4[complete]` 在 sice 上连续 2 轮全量通过（ADR-021 的退出条件）。
5. **开销兜底**：开关关闭时，与 O2a 之后的基线相比无差异（±3%）。

## 8. 实施前置与分工建议
1. **L4**：给出 §7.1 的 t（区分 advisory / `FOR UPDATE` / `FOR SHARE` 三类之后的尾验）。级别 1 下这三类尾验都要完整重跑，按 t 整体估算收益；`FOR SHARE` 那一类的占比仅作为将来是否另写 ADR 的参考。
2. **L1**：
   - 审计 §1 的“全部输入都已加锁”，逐个函数列出断言读取的每一行及其锁；
   - 定稿屏障清单；
   - 写 §6.1 / §6.2 的测试；
   - 迁移由 L1 负责（加锁点函数大多属于 L1 已改过的链）。
3. **L4**：§7.2 的测量与对比。
4. **合并顺序**：在 O2b 和 NX-023 的 0088–0090 合入之后实施，按当时最新的包装链改名。

## 9. 未决问题
- 级别 2：已裁定本次不做，仅作为候选记录；将来要做须另写 ADR，并由负责人本人确认。
- TEMP 权限：已同意，不采用 UNLOGGED 属主表方案。
- 事务作用域是否值得另立项：需要先测量跨语句重复。
