# 草稿：NX-068 Schema 发布前门禁（Q8）与发布后冒烟（Q9）

- **状态**：L4 设计稿，2026-11-11，基于 main 910c2b6d。只是设计，不含实现，不改 planning。L6 2026-11-11 判定为条件 PASS（N1–N4），本稿已按 N1–N4 修改，见 §9。与设计稿 B 合并实施时，NX-068 是第 1 步。
- **来源**：P0 复盘（Consumer v2 发布后效应链中断，`docs/planning-drafts/p0-consumer-v2-effect-chain-inventory-20261110.md`）的 Q8、Q9。修复本身已经由 `adr/ADR-046-effect-scope-successor-proof.md`（A′、Q4/Q5/Q5′、Q6、Q10）和 `docs/release/run-r76-consumer-v2-effect-chain.md` 交付。NX-068 不改那些规则，只做两件事：
  - 发布**之前**，用这些已有规则预判一次。预判失败就拒绝发布，并列出必须先做的步骤。
  - 发布**之后**，自动跑一次只读冒烟。冒烟失败就告警。
- **目标**：负责人在工作台 F3 发布对象类型 T 的 v(N+1) 时，能提前看到这次 P0 那样的断链，而不是等 20 小时后才由顾客回复失败暴露出来。

## 1. 入口

| 入口 | 位置 | 作用 |
|---|---|---|
| E1 预检读（新） | `SchemaPublicationPort.readiness(proposal_id)`；工作台 F3 的发布对话框打开时调用一个只读路由（暂定 `GET /api/v1/workbench/schema/proposals/{id}/publish-readiness`，沿用 F3 现有的 reviewer 授权和读 Action） | 给出判定结果和步骤清单（见 §3），不写任何东西 |
| E2 发布时复检（权威） | SQL `nexloop_schema_proposal_publish` 外包一层同名 wrapper，按 0437/0438 的做法：wrapper 继承原函数的 EXECUTE 授权对象，原函数改名为 `_before_publish_gate` 并撤销全部授权 | 在发布事务内、写入任何对象和 Action **之前**重新判定。有 `refuse` 级的项就 raise（`P0001`，错误信息里带门禁 code 列表），整个发布回滚 |
| E3 发布后冒烟（新） | 发布事务提交后写一条 `runtime` 冒烟任务（同一事务内入队，先持久化再 ACK）；scheduler 取出后执行 §4 的只读检查，结果写入冒烟结果表 | 失败时进入 metrics，由告警规则触发（§4.3） |

- E1 只是给人看的，E2 才是判定依据。E1 和 E2 调用同一个 SQL 判定函数 `authz.nexloop_schema_publish_gate(p_tenant, p_world, p_publication jsonb)`。所以“预检通过，发布被拒”只会因为两次调用之间状态变了（并发发布或授权变化），不会因为两套规则不一致。
- E2 是在 Python `build()` 生成 publication 之后判定的，判定对象就是这次真正要发布的 `actions`（后继列表）和 `compatibility_report.op`，而不是重新推算一遍。
- `batch=True`（D25 分批）和 `hide` 都走同一个 `_publish`，所以同样经过门禁。`migrate_batch` 不再判定，因为 Action 后继在第一批时就已经发布了。

## 2. 判定规则

记被发布的类型为 T，当前最新版本为 b，本次发布 b+1，操作为 op（`add_optional_property | add_vocabulary_value | hide | …`）。设 **受影响集合 S** = 真实世界中 active，且 `governance.change_scope.object_types` 含 (T, b) 的 Action 版本。F3 对 S 中每个 Action 都生成后继 `:k+1`（`build()` 里的 `successors()`），后继随这次发布一起 active。

对 S 中每个 Action `a:k` 按角色分类。分类读的是数据库里已有的事实，不需要额外配置：

| 类 | 怎么识别 | 发布后谁在用 `:k` | 判定 | code（refuse 级） |
|---|---|---|---|---|
| G1 效应 Action | 效应 Action 的识别交给 `effect_successor_admissible`：B 之前看允许名单 `control.nexloop_effect_actions`，B 之后看准入表。门禁不自己写识别条件 | B 之前：0183 仍执行 `:1`，靠 A′ 证明。B 之后：执行已准入的 `:N` | 只调用 §2.2 的单一函数 `effect_successor_admissible(...)`：B 之前判 A′ 能否证明（A′-1 等价、A′-2 由人工发布、A′-3 只追加可选属性），B 之后判后继能否准入。T 不在任何效应 Action 的 scope 中时，这一类为空 | `effect_successor_unprovable:<op>` / `effect_successor_not_equivalent` |
| G2 registrar Action | `nexloop.plan.bind_effect_context`、`nexloop.effect.configure_control`（0179 的两个 verb） | Python 用 `authz.nexloop_effect_registrar_version` 选出覆盖四个对象的版本（Q2）。持有者必须对 `:k+1` 有授权，否则选不到 | 每个持有 `a:k` 精确授权的主体，必须有 follow 声明（Q5，见 §2.1）。没有声明的列为“必须先做的步骤” | `registrar_holder_without_follow:<principal>` |
| G3 `<T>.create` | stable_name = `T.create` | Q6 `create_current` 只用覆盖最新版本的 create，不回落到 v1 | 每个持有 `T.create:k` 的主体：有 follow 声明（Q5′，且后继 scope 只有 T 的版本变化，`create_scope_other_type` 规则），或者在发布前已经被显式授予 `:k+1`（不可能，后继还不存在），所以实际上只看 follow | `create_holder_without_follow:<principal>` |
| G4 工作台 Action | 角色模板（`workbench-roles`）引用的 Action | `nexloop_workbench_action_for` 按 0432 选当前可用的最高版本，后继与前任并存 | 0432 已经让工作台对 `:k` 与 `:k+1` 并存做了处理，所以不拒绝。但如果某个角色模板只授予了 `:k`，而 `:k` 的 scope 不再覆盖对象新版本，就列为 `warn`，提示“发布后需要应用角色清单 vX” | （warn）`workbench_role_needs_manifest` |
| G5 其他 | 上面都不是 | 由各自的调用方处理 | 只列出来，不拒绝 | （info） |

- **只降不升**：门禁只会拒绝发布或者给出提示，不会写授权，也不会自动加 follow 声明。“发布本身不写授权”（NX-040 F3 §5、ADR-046 §4）保持不变。
- **拒绝必须带步骤**：每个 refuse 项都带 `remedy`，见 §3。没有 remedy 的 code 不允许出现（测试断言）。
- **空集不等于通过**：如果 S 为空，但存在 `T.create` 或效应 Action 引用 T 的其他版本，就报 `scope_lineage_gap`（warn）。这对应 review-silent-pass 的教训：门禁没东西可查时要说出来，不能静默放行。

### 2.2 单一判定函数 `effect_successor_admissible`（N1）

- 签名（暂定）：`authz.nexloop_effect_successor_admissible(p_tenant text, p_world text, p_executed jsonb, p_successor jsonb, p_type text, p_from_version int, p_to_version int, p_operation text) returns jsonb`。返回 `{admissible: bool, code: text|null}`，code 用 0446 已有的拒绝码（`successor_policy_differs`、`publisher_not_human` 等）。
- 它是 stable 的纯判定：不写 `control.nexloop_effect_scope_proofs`，也不写别的表。
- 四处共用这一个函数，不再各自写判定条件：
  1. 0183 的 A′ 证明函数：改为先调用它，判定通过后再写 proofs。写 proofs 仍然只发生在这里。
  2. 设计稿 B 的准入函数：B 之后准入前也调用它。
  3. 门禁 G1（E1/E2）。
  4. 冒烟 S3（N4）。
- 本迁移从 0446 拆出时就用这个名字。0446 的 proof 函数改为调用它，行为不变，由 0446 现有的全部测试（含变异检查表里的各项）证明。
- B 之前的分支判 A′ 能否证明。B 之后（B M2）同一个函数改判能否准入；B M4 删除 A′ 分支，NX-068 加的断言在那时翻转（§5）。

### 2.1 follow 声明在数据库里看不看得到（V1，实现前必须核实）

- 目前 follow 声明写在部署清单（`stage-principals.v1.json`）里，由 `scripts/deploy/stage_secrets.py` 的 FOLLOWABLE 白名单校验，register 时派生 `:N` 授权（0083、0432）。派生出的授权在 purpose 里带 `follow_latest_version of …`。
- 发布前门禁要判断“这个主体发布后会不会跟随”，需要在**还没有后继的时候**就知道声明是否存在。API 运行时读不到部署清单（也不应该读）。
- 需要核实：register 写入 `:k` 授权事实时，是否把 `follow_latest_version: true` 一起放进了 grants payload。
  - **如果写入了**：门禁直接读授权事实，不需要迁移。
  - **如果没写入**（预计如此）：一个迁移，register 在写授权事实时一并把 follow 标记写进 payload（只追加一个键，不改已有键）。门禁只信这个标记。旧数据要等下一次 register 才会补上标记，在那之前门禁会报“没有声明”，这是安全的方向（拒绝或给出步骤，而不是放行）。
- **FOLLOWABLE 只在 Python 里（N2）**。SQL 门禁看不到这份白名单，因此做不了测试 7。两种做法：
  - **方案 1（推荐）**：register 只给 FOLLOWABLE 内的授权写 follow 标记，白名单外的声明在 register 时就被拒绝（现状如此），不写标记。门禁只信标记。另加测试：任何授权事实里，白名单外的 Action 都没有 follow 标记（遍历 FOLLOWABLE 的补集，断言为空）。好处是白名单仍然只有一处，改它仍然只动 Python 加负责人决定，不需要迁移和发布。
  - 方案 2：把白名单写进数据库（新表，随部署发布），门禁直接读。代价是多了一个需要发布和对账的配置，Python 和数据库两份白名单要做一致性测试。
  - 两种做法下，白名单都仍然是唯一的放宽口径，门禁不扩大它。门禁的判断与 register 的 `_rebinding_reason` 用同一套规则（V2：把 Python 的 scope 差异判定在 SQL 里复刻一份，或者在 E1 的 Python 侧调用 `service_grants` 的纯函数。推荐后者，因为 Q4 的“scope 扩大即拒绝”只在 Python 里实现过一次；E2 在 SQL 里只核对 follow 标记存在、后继 scope 的差异只含 T 的版本这两件事）。

## 3. 失败时是什么样子

E1 返回的结果（E2 拒绝时，错误信息里也是同样的 code 列表）：

```json
{"verdict": "refuse",
 "type": "Consumer", "from_version": 2, "to_version": 3, "op": "add_vocabulary_value",
 "items": [
  {"level": "refuse", "code": "effect_successor_unprovable:add_vocabulary_value",
   "action": "eios:action:nexloop.effect.relay_reply:1",
   "remedy": "效应 Action 只能用 A′ 跨版本证明，而 A′ 只接受 add_optional_property。可以把这次变更改为追加可选属性，或者先交付方案 B（ADR-046 §3）"},
  {"level": "refuse", "code": "registrar_holder_without_follow:relay_planner",
   "action": "eios:action:nexloop.plan.bind_effect_context:2",
   "remedy": "在 stage-principals 里为 relay_planner 声明 follow_latest_version（须在 FOLLOWABLE 内），然后 register，再发布"},
  {"level": "warn", "code": "workbench_role_needs_manifest", "action": "eios:action:Consumer.edit:2",
   "remedy": "发布后应用角色清单（版本号随清单）"}],
 "steps": ["…按 refuse 项的顺序列出…"]}
```

- 工作台 F3 对话框：有 refuse 项时，“发布”按钮置灰，旁边列出步骤；只有 warn 项时可以发布，但需要勾选“已知晓”。
- 结果里不包含主体以外的身份信息，不包含对象数据。
- **E2 是权威拒绝（N3）**：raise 或返回结构化拒绝都可以，但任何路径都不能写入部分发布，也不能消耗 release permit（F4）。门禁在 `_before_publish_gate` 之前执行。
- **V3 定下之前不实现拒绝审计**。如果以后要保留审计，用 E1 侧的独立记录，不放进发布事务。

## 4. 发布后冒烟（Q9）

### 4.1 检查项（全部只读，以发布者所在租户和 real 世界为范围）

| 项 | 怎么查 | 通过条件 |
|---|---|---|
| S1 registrar 选版 | 对 T 的一个 v(b+1) 对象（存在时），以及每个持有 registrar 授权的主体，调用 `authz.nexloop_effect_registrar_version`，构造 step/goal/control 的代表组合（取各类型的最新版本） | 能选到版本，并且选到的版本有授权。如果 T 还没有 v(b+1) 对象（分批发布中），就用类型版本代替对象版本 |
| S2 create 覆盖 | `authz.nexloop_schema_create_coverage()`，并按 T 过滤（需要一个带 type 参数的重载，或者在冒烟函数里内联同一个查询） | `uncovered = 0`，并且 T 的最新版本有某个持有者可用的 create |
| S3 效应 A′ 可证 | 对 G1 每个效应 Action 调用 `effect_successor_admissible`（纯判定，不写 proofs；N4）。不直接调用 0446 的 proof 函数 | 每个都可证，或者 T 不在任何效应 scope 中 |
| S4 relay 指标基线 | `runtime.nexloop_metrics_snapshot(tenant,'real')->'relay_replies'` | 只记录基线，不在发布时判定（发布那一刻没有流量） |

- 冒烟在发布后 1 分钟（让分批发布的第一批落地）执行一次，15 分钟后再执行一次。第二次只看 S4：发布后 15 分钟内 `failing_without_success_15m > 0`，并且基线时为 0，就判失败。
- 冒烟本身不跑任何 Action，不发消息，不创建对象。S1 和 S3 只是调用 stable 函数。

### 4.2 结果存储

- 新表 `runtime.nexloop_schema_publish_smokes`（tenant_id、world、publication_id、round、checks jsonb、outcome、ran_at）：append-only，租户 RLS（force），带 tenant FK，要登记 erasure coverage（不含个人数据，登记为 `not_personal`）。
- 冒烟任务用 publication_id + round 作为稳定 ID，重试时幂等。scheduler 重启后会重新取出未完成的任务。

### 4.3 告警

- metrics 快照加 `schema_publish_smoke`：最近 24 小时内失败的冒烟次数，以及最近一次失败的 publication_id。
- 告警规则新版本 `schema_publish_smoke_failed`（critical）：大于 0 就触发。
- 如果告警配置为空，或者没有这条规则，覆盖率为 0，测试判失败（review-silent-pass）。
- 冒烟任务 30 分钟内没有执行（scheduler 停了）也算失败：`smoke_overdue`，由同一条规则覆盖，避免“冒烟没跑就等于通过”。

## 5. 与 Q4/Q5′ 的关系

- **Q4**（register 遇到 scope 扩大的后继就拒绝，不派生）和 **Q5/Q5′**（只有 FOLLOWABLE 白名单里逐条声明的 follow 才派生，共 3 条）都是**发布之后、register 的时候**生效。门禁把同样的判断提前到**发布之前**：
  - 发布后 register 会因为 Q4 拒绝的后继（scope 扩大），门禁在发布前就报出来；
  - 发布后 Q5 不会派生的持有者（没有声明，或者不在白名单），门禁在发布前就列为步骤；
  - 门禁不扩大白名单，不新增 follow 来源，不改变 Q4 的判定。新增一个需要跟随的 Action 仍然要由负责人决定，再经 L6 复审（与 Q5′ 的流程一样）。
- **Q6**（create 只写最新版本，不回落）：G3 和 S2 保证发布之后 Q6 有可用的 create，不会出现“发布后新建顾客被拒”（run-r76 的发现）。
- **A′**：G1 和 S3 是 A′-1 至 A′-3 的预判，用的是 0446 同一套 SQL，不是另写一份。B 交付以后，A′ 删除，G1 改为检查 B 的允许名单（A′-6 的守护测试会提醒这一点；NX-068 增加一条断言：A′ 删除时 G1 的分支也必须改）。
- **Q10**（relay 告警）：S4 复用 Q10 的指标，告警的时间窗口从“持续 15 分钟”缩短为“发布后 15 分钟”，范围限定在这一次发布。

## 6. 是否需要迁移

需要，一个迁移（临时编号，合入时由调度员重新编号）：

1. `authz.nexloop_schema_publish_gate(text,text,jsonb)`：stable、security definer、`search_path=pg_catalog,pg_temp`、`row_security=on`、租户上下文从参数设置。G1 只调用 `effect_successor_admissible`。
1a. 从 0446 拆出 `authz.nexloop_effect_successor_admissible`（§2.2）：0446 的 proof 函数改为调用它，行为不变，由 0446 的测试证明。
2. `nexloop_schema_proposal_publish` 的门禁 wrapper（授权对象从原函数复制过来，原函数撤销）。
3. （V1 不成立时）register 写授权事实时附带 follow 标记：只改 Python 和授权 payload 的写入，不改已发布迁移，可能不需要 SQL。
4. `runtime.nexloop_schema_publish_smokes` 表、冒烟入队与执行函数、metrics wrapper（叠在 0446 的 relay_replies wrapper 之上，同样的做法）。
5. 不改已发布的迁移，不改 Action 定义。不新增业务 Action：E1 是读，冒烟由 scheduler 执行。如果实现时发现 E1 必须作为新的读 Action 发布，要同步更新 `stage_register` 计数和 business-actions 版本。

## 7. 测试清单

每条拒绝或放宽条件都要做变异检查。不允许 xfail 或 skip。

**门禁（E1/E2 同一个函数）**

1. 复现 P0：Consumer v1→v2 `add_optional_property`，registrar 持有者没有 follow 声明 ⇒ refuse `registrar_holder_without_follow`，发布没有写入（对象版本、Action 后继、permit 都不变）。
2. 同一场景补上声明并 register ⇒ pass，发布成功，之后 relay 回复端到端成功（复用 `test_consumer_v2_stage_path_pg` 的路径）。
3. `add_vocabulary_value`、`hide` 作用在效应 scope 内的类型上 ⇒ refuse `effect_successor_unprovable:<op>`；作用在不在任何效应 scope 中的类型上 ⇒ 不因 G1 拒绝。
4. 后继与 `:1` 不等价（人为让 policy 或 receipt 不同）⇒ refuse `effect_successor_not_equivalent`。
5. `T.create` 持有者没有声明 ⇒ refuse `create_holder_without_follow`；有声明 ⇒ pass。
6. 后继 scope 扩大（多一个类型）⇒ refuse，code 与 register 的 Q4 拒绝一致（参数化，与 `test_dry_run_stops_on_a_refused_successor[scope_widened]` 用同一组输入）。
7. 按 N2 方案 1：register 遇到白名单外的声明时拒绝，并且不写 follow 标记；遍历所有授权事实，白名单外的 Action 都没有标记；门禁对这样的持有者报 refuse。
8. S 为空，但 create 或效应 Action 引用 T 的其他版本 ⇒ warn `scope_lineage_gap`（不能静默通过）。
9. 并发：E1 通过，然后另一个会话撤销 follow 授权，再执行 E2 ⇒ E2 拒绝。
10. 每个 refuse code 都有 remedy（遍历 code 表断言）。
11. 拒绝时 release permit 没有被消耗，也没有写入部分发布（对象版本、后继 Action、publication 行都不变）。不测审计（V3 定下前不实现）。
12. 分批发布（D25）和 `hide` 经过门禁；`migrate_batch` 不经过门禁。
13. wrapper：原函数改名后没有任何授权对象，wrapper 的授权对象等于原函数原来的授权对象；definer search_path 检查通过。

7a. `effect_successor_admissible` 是纯判定：调用前后 `nexloop_effect_scope_proofs` 和其他表的行数都不变。0446 的全部测试在拆分之后通过。
7b. 一致性：门禁 G1、冒烟 S3 和 0183 的 A′ 证明对同一组输入给出相同判定（参数化，覆盖 0446 变异检查表里的每个拒绝码）。另有结构断言：这三处的函数体都调用 `effect_successor_admissible`，而且都没有自己写的等价比较。
7c. 守护：A′ 分支存在时，G1 的 refuse 码含 `effect_successor_unprovable`；B M4 删除 A′ 时这条断言翻转。

**冒烟（E3）**

14. 发布后入队一条任务；先持久化再 ACK；scheduler 重启后仍然执行，并且对同一 publication_id + round 只执行一次。
15. S1–S3 正常 ⇒ outcome passed；人为撤销 registrar 的 `:2` 授权 ⇒ S1 失败，告警触发。
16. 人为让 create 覆盖缺失 ⇒ S2 失败。
17. 第二轮：基线为 0，注入 relay 失败、没有成功 intent ⇒ 失败；基线已经不为 0 ⇒ 不归咎于这次发布（只记录）。
18. 任务逾期（30 分钟没执行）⇒ `smoke_overdue` 触发同一条告警。
19. 告警配置为空，或缺少 `schema_publish_smoke_failed` 规则 ⇒ 告警覆盖测试失败。
20. 冒烟只读：执行前后对象数、Action 数、intent 数、授权事实全部不变。

**全局守护**（新增表必须跑）

21. db_boundary、tenant_fk、system_catalog、copy guard、erasure coverage、nx041 sim、claim_store、published_migrations、bootstrap/catalog head；`check_definer_search_path`；告警规则的 schema 校验和告警覆盖；api-routes inventory（如果增加了 E1 路由）。

## 8. 待核实与待决

- **V1**：follow 标记是否已经在授权 payload 中（决定是否需要改 register）。
- **V2**：E2 是在 SQL 里复刻 Q4 的 scope 差异判定，还是只核对“后继 scope 的差异只含 T 的版本”。推荐后者，完整的 Q4 判定留在 E1 的 Python 侧。
- **V3**：F3 对拒绝的约定是 raise 还是返回结构化结果。这决定拒绝审计怎么落地；定下之前不实现审计（N3）。
- **N2**：推荐方案 1（register 只给白名单内的授权写标记，门禁只信标记），待负责人或 L6 确认。
- **待负责人决定**：warn 项是否也拒绝发布（推荐：不拒绝，需要勾选“已知晓”）；冒烟第二轮的窗口取 15 分钟还是 30 分钟（推荐 15 分钟，与 Q10 一致）。

## 9. L6 判定后的修改（2026-11-11）

- **N1**：G1 改为只调用单一函数 `effect_successor_admissible`（§2.2）。它从 0446 拆出，行为不变；A′ 证明、B 的准入、门禁、冒烟 S3 四处共用。
- **N2**：§2.1 写明两种做法，推荐方案 1；测试 7 按方案 1 改写。
- **N3**：E2 是权威拒绝，任何路径都不写部分发布，也不消耗 permit；V3 定下之前不实现审计（§3、测试 11）。
- **N4**：S3 用纯判定，不调用 0446 的 proof 函数，也不写 proofs（§4.1、测试 7a）。
- **合并顺序**：NX-068 是第 1 步，内容是拆出函数、门禁和外包函数（G1 走 A′ 分支）、冒烟。B 的 M2 把 G1 和 S3 切到“能否准入”，M4 删除 A′ 分支。
