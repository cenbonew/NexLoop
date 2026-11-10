# 草稿：方案 B——效应 Action 跟随 Consumer 的 schema 版本（含删除 A′）

- **状态**：L7 设计稿，2026-11-11，基于 main 910c2b6d。只是设计，不含实现，不改 planning、contracts 和迁移。送 L6 审。
- **来源**：负责人 2026-11-10 定 P0 方向 A′ 时把 B 另立任务（ADR-046 §3、§4）。验收含“删除 A′”：ADR-046 的过渡规则，以及 B 落地就会变红的守卫 `test_the_transitional_rule_goes_with_option_b`。
- **依据**：
  - `adr/ADR-046-effect-scope-successor-proof.md`；
  - `docs/planning-drafts/p0-consumer-v2-effect-chain-inventory-20261110.md`（下称“清单稿”）§1、§2.2、§5；
  - 迁移 0178（允许名单）、0179（registrar / effect context）、0183（intent 的精确版本）、0432（发布后继共存）、0446（A′）。
- **需要负责人或 L6 决定的点**：标为 **【待定 Bn】**，每项给了推荐。我不会自行定这些点。

## 0. 一句话

效应 Action 的版本不再写死 `:1`。规则如下：
1. **bind 时选定版本**。effect context 在 bind 时，从允许名单里选出“scope 精确包含 Consumer 当前版本的那一个已准入版本”，并把选中的精确 resource 存进 context。
2. **intent 只认这个版本**。intent 只接受 context 里存的那一个 resource，仍按原不变量逐项精确比对；不再有“由后继证明”的放宽。
3. **准入是一次性、受治理的**。后继进入允许名单要走 configurator 的准入步骤：A′ 原来在运行时做的等价性检查（A′-1、D1、D2），挪到准入时做一次并留下修订。
4. **授权**照 Q5′ 的 FOLLOWABLE 逐条派生。
5. **A′ 随后删除**：A′ 的函数、调用点和 proof 表都去掉，守卫测试翻转。

## 1. 现状：写死 `:1` 的位置（按层）

| 层 | 位置 | 现在怎么写 | B 的改法 |
|---|---|---|---|
| 允许名单 | `control.nexloop_effect_actions`（0178） | 主键是 stable_name，每个 Action 只有一行 version=1；slot_identity 唯一；resource_id 是生成列 | 主键改为 (stable_name, version)；slot_identity 按 stable_name 共用（不再唯一）；增加准入来源列。见 §2.1 |
| registrar / context | 0179 `authz.nexloop_effect_context_command` | context 只记 slot_identity，效应 Action 由 slot 反查允许名单唯一的一行 | bind 时选定效应 Action 的精确 resource，写进新列 `nexloop_effect_contexts.action_resource`。见 §2.2 |
| intent | 0183，A′ 后的当前定义在 0446 | 允许名单按 resource 匹配，`p->>'action_version'` 必须等于名单中的 version；Consumer 不在 scope 时走 A′ | `c->>'resource_id'` 必须等于 `ctx.action_resource`，scope 精确比对；A′ 分支删除。见 §2.3 |
| Python | `message_relay.py:18/20/155`、`reply_fallback.py:24`、`effect_execution.py:31/41/42/475`、`context_pack.py:28/29`、`service_offerings.py:13/31/54`、`contracts.py`（清单稿 §2.2） | 常量 `…:1` 或 `get(…, 1)` | 统一改为从 context 或 binding 取精确版本；只有“首次创建 context”这一处在 SQL 里选版本，Python 不自己选。见 §2.4 |
| Host | `apps/agent-host/src/context-input.ts:60/327`、`runtime-effect-tools.ts:563` | 硬比对 `'nexloop.service.request:1'`、`ENTERPRISE_ACTION='nexloop.enterprise.deliver:1'` | 拆成 stable_name（常量）和 version（来自 context pack 的正整数）。见 §2.5 |
| contracts | `context-pack-v6/v7.schema.json`，各约 7 处 `const`/`enum` | `"const": "nexloop.enterprise.deliver:1"` 等 | 新增 context-pack v8，用 `pattern` 表示（v6/v7 冻结不改）。**【待定 B1】** |
| 对象数据 | ServiceOffering 的 `delivery_action` 属性（企业交付是 `nexloop.enterprise.deliver:1`），Host 用它与 `constraints.action` 比对 | 带版本的字符串存在对象里 | 比对时只取 stable_name 部分，对象数据不迁移。见 §2.5 |
| 配置 | `action-effect-categories.v3.json`、`nx037-trial.v*`、`nx037-interaction-plan.v*`、`remediation-rules.v1.json`、`real_dispatch_switch.py:18` | `…:1` | 按“stable_name 级别的分类”读取，或者出新版本清单；旧版本清单冻结不改。见 §2.6 |
| 授权 | `stage-principals.v1.json` 中效应 Action 的 executor / run 授权 | 精确 `:1` | 给效应 Action 逐条声明 follow，FOLLOWABLE 增加两条。见 §3 |

## 2. 设计

### 2.1 允许名单：从“每个 Action 一行”改为“每个已准入版本一行”

- 新迁移：
  - 主键从 stable_name 改为 (stable_name, version)；
  - 去掉 slot_identity 上的 unique，改为“同一 stable_name 的所有行 slot_identity 相同”的约束（触发器或 exclusion）；
  - 新增 `admitted_by` 列：值为 `migration`（seed 的 `:1`）或 `publication:<uuid>`；
  - 新增 `admitted_at`、`consumer_version` 两列（`:1` 为 1）。
- **表仍然没有运行时角色可写**。新增的唯一写路径是 configurator 的定义者函数 `control.nexloop_admit_effect_action_successor(tenant, publication_id, stable_name, version)`（trusted configuration，与 0050 同款），以及“退役”函数（把一行标为 inactive，不删除，只追加）。
- **【待定 B2】允许名单的作用域**：0178 的允许名单是全局表，没有 tenant 列；而后继 `:N` 是租户级的（由某个租户的 F3 发布产生）。
  - 推荐：新建一张租户级的准入表 `control.nexloop_effect_action_admissions(tenant_id, world, stable_name, version, publication_id, …)`。全局表仍只列 stable_name 级别的 effect 身份（slot、类别），并去掉 version 列（迁移中保留为 1 的兼容视图，用到下一次大版本）。
  - 理由：不同租户的 Consumer 版本互不相同，全局一行无法表达“租户 A 已准入 `:2`，租户 B 还在 `:1`”。
  - 代价：0183 和 0179 的查询要改为按租户和 world 匹配准入表。

### 2.2 bind 时选定效应 Action 的精确版本（0179）

- 在 0446 的 registrar 版本选择（`authz.nexloop_effect_registrar_version`）旁边，新增 `authz.nexloop_effect_action_version(tenant, world, slot, consumer)`。它返回准入表中同时满足以下条件的那个 version：
  1. 同一 slot；
  2. active；
  3. 对应的 `control.nexloop_action_definitions` 行 active；
  4. change_scope 精确包含 Consumer 的 (type, schema_version)。
- **只能恰好一个**：零个时拒绝（`effect Action version unavailable`）；多于一个时也拒绝。按准入规则（§3.2，每个 Consumer 版本只准入一个后继），多于一个不应出现，出现即视为数据错误，不取最高。
  - 这与 registrar 的 Q2 不同：registrar 取“覆盖四个对象的最高版本”；效应 Action 只绑 Consumer，取唯一一个。
- 选中的结果写进 `nexloop_effect_contexts.action_resource`（新增的 not null 列；已有行在迁移中回填为 `…:1`），与 registrar 的 bind 在同一事务里完成。
- **Run proof 和 executor proof 的精确 resource**：改为 `ctx.action_resource`。授权仍按精确 resource 校验（`nexloop_assert_action_authority`），所以选错只会失败，不会扩权。
- **并发发布（Q3）**：
  - 如果 Consumer 在 bind 之后被升级，intent 时 scope 精确比对失败，报 `effect Consumer outside Action`，与今天相同。
  - context 不改写。relay 的下一次尝试会新建 context，选到新版本。
  - 这条失败计入 `relay_reply_failing`；在准入完成之前，这是预期内的短暂失败（见 §4 的顺序）。

### 2.3 intent 只认 context 的版本（0183）

- 允许名单那两个条件（resource 在名单中，且 version 等于 `action_version`）替换为：
  - `c->>'resource_id' = ctx.action_resource`；
  - `p->>'action_version' = split_part(ctx.action_resource, ':', 4)`。
- slot 改由 `ctx.slot_identity` 查。原来那句“`v_action` 必须在 slot 中”保留，用 stable_name 比对。
- `effect Consumer outside Action` 的精确比对保持不变，删除其后的 A′ 分支（§5）。
- `published.definition is distinct from c->'definition'` 等发布检查不变：定义按精确 resource 读取。

### 2.4 Python

- 所有 `get('nexloop.service.request', 1)` 或 `…:1` 常量，改为从以下来源取：
  - 已有 context：读 binding / context 中的 `action_resource`（新增只读函数，或扩展现有 context 读取）。
  - 尚未有 context（relay 首次组装）：由 SQL 在 bind 中选定，Python 不预先选。Run 的签发需要 Action 版本时，在 bind 之后再签发；如果现有顺序是先签发再 bind，就改为签发 stable_name 级别的 Run 能力，或在 bind 后补签。**【待定 B3】**这一点要先核实 0179 中 Run proof 和 bind 的先后顺序，实施前给出时序图。
- `reply_fallback.py:24` 遵循 ADR-023（唯一的回复工具）。工具名按 stable_name 识别，版本跟随 context。
- `service_offerings.py`、`contracts.py` 里的 `:1` 改为 stable_name 加“当前 context 版本”；生成的 schema 随 v8 一起更新。

### 2.5 Host 与 contracts

- **【待定 B1】contracts 是 NexLoop 的目标契约，按约束不能由实现线自行修改**。推荐的做法：
  - 新增 `context-pack-v8.schema.json`：`action` 改为 `pattern: ^nexloop\.service\.request:[1-9][0-9]*$`，企业交付同理；
  - 配套新增 `examples/context-pack-v8.valid.json`，并跑 `test_contracts`、`test_published_contracts`、`generate --check`；
  - Host 同时接受 v7（只有 `:1`）和 v8，一个版本周期之后再退役 v7。
- Host：
  - `ENTERPRISE_ACTION` 拆成 `ENTERPRISE_STABLE='nexloop.enterprise.deliver'`；
  - `constraints.action` 必须匹配 `^<stable>:[1-9][0-9]*$`；
  - 版本号只来自 context pack，Host 不自己推断。
- ServiceOffering 的 `delivery_action`：对象数据里仍是 `…:1`，不迁移（迁移对象属于 Consumer 以外的 schema 变更，不在本任务范围）。
  - Host 比对时，用 `stable_of(delivery_action) === stable_of(constraints.action)`；
  - SQL 侧对应的 catalog hint（0178 的“offering 的 delivery_action 必须是 intent 的 Action”）同样改为比对 stable_name。

### 2.6 配置清单

- `action-effect-categories` 出新版本（v4）：键改为 stable_name。读取方同时兼容 v3（`…:1` 键）一个周期。
- `nx037-trial.v*`、`nx037-interaction-plan.v*`、`remediation-rules.v1.json`：
  - 只有 runtime 真正读取 Action 精确版本的地方才需要改；
  - 只用于展示或验收脚本的不改；
  - 实施时逐个 `git grep` 列表核对（清单稿 §2.2 的“配置”行）。

## 3. 发布新 Consumer 版本时，效应 Action 后继的产生、准入与授权

### 3.1 产生（不变）

- F3 发布时已经为每个绑定 Consumer 的 Action 生成 `:N`，其中包括两个效应 Action（0432）。B 不改 F3。
- 发布本身仍然不写授权，也不写准入（NX-040 F3 §5）。

### 3.2 准入（新增，代替 A′ 的运行时证明）

- 发布之后由 configurator 执行准入：`scripts/deploy/effect_action_admit.py --tenant T --publication P --bootstrap-dsn-file F [--dry-run]`，窗口模式与 `machine_limits_publish.py` 相同。
- SQL 定义者函数检查下列条件，与 A′ 现在在运行时检查的完全相同，只是挪到准入时做一次。检查代码从 0446 原样复用：
  1. `:N` 与前一个已准入版本是同一 stable_name，active；
  2. `authz.nexloop_effect_definition_unversioned` 后逐字段相等，capability 也相等（A′-1）；
  3. `:N` 在该次发布的 `successor_actions` 中，且该发布 `published_by` 为 human，`permission_digest_before = permission_digest_after`（A′-2）；
  4. `:N` 的 scope 精确包含 Consumer:v；
  5. Consumer v 相对前一个已准入版本的 Consumer 版本累计只增不改（D1），且新增属性满足 D2。
- **任一条不满足，不准入，也不写任何东西**：
  - 这时该租户的效应链会在 intent 处报 `effect Consumer outside Action`，等同于 2026-11-09 的事故形态。
  - 所以 F3 发布前的就绪检查要增加一项：“发布会产生效应 Action 后继但不可准入”时，发布被拒绝或给出明确警告。**【待定 B4】**推荐拒绝发布：在 `nexloop_schema_proposal_publish` 中，对效应类型（Consumer）的 `add_vocabulary_value` 和 `hide` 操作直接拒绝，在发布前就挡住，而不是在运行时失败。
- **每个 Consumer 版本只准入一个效应 Action 版本**：用唯一约束 (tenant, world, stable_name, consumer_version) 保证。
- 准入写一条修订（谁、何时、哪次发布、检查摘要），留作审计。这是 A′-4 留痕的替代：A′ 是每次 intent 写一条，B 是每次准入写一条。

### 3.3 授权

- 效应 Action 的执行授权分两层：
  - Run 持有者（runtime planner 签发的 Run 能力）；
  - executor（effect worker）。
- 两者在 `stage-principals.v1.json` 里都是精确 `:1` 授权。
- **【待定 B5】推荐**：
  - FOLLOWABLE 增加 `eios:action:nexloop.service.request:1`、`eios:action:nexloop.enterprise.deliver:1` 两条，从 3 条变成 5 条，测试仍断言集合相等。
  - 派生条件沿用 Q5′ 的 `<Type>.create` 规则：scope 中只允许 Consumer 的版本变化（`create_scope_other_type` 改名为 `successor_scope_other_type`，两类共用）。
  - **另加一条：只派生已准入的后继**。lineage 里出现但准入表中没有的 `:N`，在 register 的 dry-run 中列为 `refused: not_admitted`，按现有规则以 `follow_latest_refused` 退出。
  - 结果是“准入”和“授权”两把钥匙都要有：只有准入没有授权会失败在授权；只有授权没有准入，`:N` 选不中。
- 与 `follow_latest_version` 的关系：B 不改 follow 的语义，只扩展白名单并增加 `not_admitted` 拒绝原因。发布仍不写授权，派生只发生在 register。

## 4. 迁移顺序与回退（扩展—收缩，A′ 在过渡期一直保留）

| 步 | 内容 | 能否独立回退 |
|---|---|---|
| M1 | 新迁移：准入表、允许名单兼容视图、`nexloop_effect_contexts.action_resource`（回填 `:1`）、选择函数、准入函数。intent 同时接受“`ctx.action_resource` 精确匹配”和旧条件；**A′ 保留** | 是：新列和新表都不被旧代码读取；回退即停用新函数（released migration 冻结，回退要写新迁移，只改函数体） |
| M2 | Python 和 Host 改为使用 context 的版本；contract v8 加入，Host 同时接受 v7/v8 | 是：回到上一个镜像。context 中的 `action_resource` 此时都是 `:1`，行为与以前相同 |
| M3 | stage：对 2026-11-09 那次 Consumer v2 发布执行准入（`effect_action_admit.py --dry-run`，然后正式执行），然后 register 派生效应 Action `:2` 的授权 | 是：把准入行标为 inactive（退役函数）。之后的 bind 又选回 `:1`，再由 A′ 证明 |
| M4 | 删除 A′（§5）。守卫翻转 | 否：删除之后回退需要新迁移重建 A′（0446 的函数体原样恢复）。所以 M4 必须在 M3 在 stage 上稳定运行 ≥ 7 天、没有 `effect Consumer outside Action` 之后再做。**【待定 B6】**观察期长度 |

- M1–M3 期间，旧顾客的 effect context 有两条路：
  - 新建的 context 选 `:2`，精确通过；
  - M3 之前已绑定、尚未过期的 context 仍是 `:1`，由 A′ 证明。

  context 的有效期以分钟计，M4 之前它们早已过期。M4 的迁移要断言：不存在 `action_resource` 不能精确覆盖其 Consumer 当前版本的未过期 context；有就拒绝迁移。

## 5. 删除 A′ 的步骤与守卫翻转（验收项）

1. **前置断言**（M4 迁移里先做，失败就整个迁移回滚）：
   - 每个“Consumer 版本 > 1 且存在 effect 使用”的租户，都有该版本的准入行；
   - 没有未过期、靠 A′ 才能通过的 context（§4 末尾）。
2. **intent 命令**：`create or replace` 去掉 `authz.nexloop_effect_scope_successor_proof(...)` 那一处调用，回到“scope 精确包含，否则报 `effect Consumer outside Action`”。
3. **删除函数**：
   - `drop function authz.nexloop_effect_scope_successor_proof(uuid,text,text,control.nexloop_action_definitions,integer)`；
   - `authz.nexloop_effect_definition_unversioned` 保留，供准入函数使用（§3.2）。
4. **处理 `control.nexloop_effect_scope_proofs`**：见 §6。
5. **守卫翻转**：
   - 删除 `test_the_transitional_rule_goes_with_option_b`；
   - 新增 `test_option_b_left_no_successor_proof`，断言以下各项：
     - `pg_proc` 中没有 `nexloop_effect_scope_successor_proof`；
     - `control.nexloop_effect_scope_proofs` 不存在（或按 §6 的推荐方案，只存在归档表）；
     - 允许名单或准入表的主键含 version；
     - intent 命令的函数体（`pg_get_functiondef`）中不含 `successor_proof`。
6. **其他文件**：
   - ADR-046 状态改为“已被 ADR-0xx（B）取代”，ADR 文件本身只追加状态行；
   - 函数复制守卫（`test_function_copy_guard`）、`system_catalog`、`real_effect_tables`（0446 登记了 proofs 表）、erasure 覆盖、db_boundary：各自的登记随删除一起更新，批次回归要跑这些守卫测试。
7. **测试清理**：A′ 的拒绝场景测试（`test_effect_scope_successor_proof_pg.py` 第 7 组）不是简单删除，而是改写为准入函数的拒绝测试（§7），保持每个条件都有变异覆盖。

## 6. stage 上现有 proof 行的处理

- 现状：R76 之后，stage 每一条旧顾客的效应 intent 都在 `control.nexloop_effect_scope_proofs` 写一行（context_id、`:1`、`:2`、publication_id、consumer_version）。行里没有个人信息，只有 id 和 resource。
- **【待定 B7】推荐**：M4 不直接删表，而是：
  1. 迁移中先把这张表改名为 `control.nexloop_effect_scope_proofs_archive`；
  2. 撤销所有写权限，加触发器拒绝 insert/update/delete，保留 RLS；
  3. 新增的守卫断言“只存在 archive、不能写入”。
- 理由：这些行是“哪段时间里哪些效应执行靠的是过渡规则”的唯一证据，对事故复盘和 R76 的验收证据有用；直接 drop 会让这段证据只剩审计日志里的间接痕迹。
- 备选：
  - 先导出计数和摘要（每个租户的行数、最早和最晚时间、publication_id 集合）存入运行单证据，然后 drop。这样更干净，但证据只剩摘要。
- erasure：表里没有 consumer_id，context_id 能经 `nexloop_effect_contexts` 关联到 Consumer。归档表要纳入成员 erasure 覆盖：
  - 删除顾客时，按 context 关联删除其行；
  - 或者确认 context 删除时级联删除。
  - 批次回归跑 erasure coverage。

## 7. 测试与变异清单

### 7.1 应该成功
1. v1 顾客：bind 选 `:1`，`ctx.action_resource = …:1`，intent 精确通过，**没有** proof 行。
2. v2 顾客（准入了 `:2`、派生了授权）：bind 选 `:2`，intent 精确通过，没有 proof 行。relay 全链（issue → effect_bind → context → runtime accept → effect intent → worker）端到端回复成功。
3. 新建顾客（Q6 写 v2）：同第 2 条。
4. 企业交付（`nexloop.enterprise.deliver`）：与第 1、2 条对称各一条。ServiceOffering 的 `delivery_action` 仍为 `…:1` 时，按 stable_name 比对通过。
5. Host 收到 v7 pack（`:1`）和 v8 pack（`:2`），都能正常组装 context-input。

### 7.2 应该拒绝（每条做变异检查）
1. 准入表中没有 Consumer:v 对应的版本 ⇒ bind 报 `effect Action version unavailable`，不产生部分 context。
2. 同一 Consumer 版本插入两个准入行 ⇒ 唯一约束拒绝；绕过约束直接构造时，选择函数也拒绝（不取最高）。
3. intent 的 `resource_id` 与 `ctx.action_resource` 不等（例如用 `:1` 处理一个绑定了 `:2` 的 context）⇒ `effect authority unavailable`。
4. bind 之后 Consumer 被升级（并发发布）⇒ intent 报 `effect Consumer outside Action`；下一次 relay 选到新版本（需要已准入）。
5. 准入函数，逐条对应 A′ 第 7 组：
   1. 后继不 active；
   2. scope 不含 Consumer:v；
   3. 不在发布的 successor_actions 中；
   4. 发布者不是 human；
   5. 权限摘要前后不等；
   6. 去掉版本号后定义或 capability 不等；
   7. 跨租户或跨 world；
   8. `:N` 不存在；
   9. scope 多出一个类型；
   10. D1：hide 之后再 add，做累计比较；
   11. D2：hidden 或有 default。

   以上各项都要拒绝，且不写任何东西。
6. register：
   - follow 到未准入的 `:N` ⇒ `refused: not_admitted`，以 `follow_latest_refused` 退出；
   - FOLLOWABLE 之外声明 follow ⇒ Refused；
   - 效应 Action 后继的 scope 有其他类型变化 ⇒ `successor_scope_other_type`。
7. Host：
   - v8 pack 的 action 是别的 stable_name、版本为 0 或非数字 ⇒ fail；
   - `delivery_action` 的 stable_name 不同 ⇒ fail。
8. 【B4】效应类型的 `add_vocabulary_value` 或 `hide` 发布 ⇒ 被拒绝（或给出警告，以负责人的选择为准）。
9. M4 前置断言：
   - 构造一个靠 A′ 才能通过的未过期 context ⇒ 迁移拒绝；
   - 构造一个缺准入行的 v2 租户 ⇒ 迁移拒绝。

### 7.3 守卫
- `test_option_b_left_no_successor_proof`（§5 第 5 步）。
- FOLLOWABLE 集合相等，共 5 条。
- 新迁移跑以下守卫：db_boundary、tenant_fk、system_catalog、function copy guard、erasure coverage、nx041 sim、real_effect_tables、`check_definer_search_path`。
- contract v8：test_contracts、test_published_contracts、`generate --check`，以及新的 example。
- `test_wheel_install`：准入脚本放在 `scripts/deploy/`，nexloop_eios 包内不得导入它（批次 164 的教训）。

### 7.4 变异要点（实现时逐项记录结果）
- 选择函数：去掉“恰好一个”、改成取最高、去掉“定义行 active”、去掉 Consumer 版本比对。
- intent：把 `= ctx.action_resource` 改回允许名单匹配；删去 version 比对。
- 准入：5.1–5.11 每个条件逐条删除。
- register：去掉 `not_admitted`；FOLLOWABLE 去掉或增加一条。
- Host：把 pattern 放宽为任意 stable_name；`delivery_action` 恢复为全字符串比对（v2 pack 下应该失败）。
- M4：去掉前置断言。

## 8. 不在本任务内
- ServiceOffering、ConsumerServiceOffering 等对象中 `delivery_action` 字符串的数据迁移。
- 其他仍写死 `action_version=1` 的 create（清单稿 §2.4 的后续项）。
- customer-5 的 v1 对象统一升到 v2（ADR-046 / R76 §8，走 0254 同款的受治理批量升级）。
- 除 Consumer 之外的类型绑定效应 Action 的情况：今天两个效应 Action 只绑 Consumer。如果以后绑定 Goal 等类型，选择函数要像 registrar 那样按多对象精确覆盖来选，需要另写 ADR。

## 9. 待定项汇总

| 编号 | 问题 | 推荐 |
|---|---|---|
| B1 | contracts 变更（context-pack v8） | 新增 v8 加 example，Host 同时接受 v7/v8，一个周期后退役 v7；需要负责人批准契约变更 |
| B2 | 允许名单是全局的，还是按租户准入 | 新建租户级准入表，全局表只留 stable_name 级别的身份 |
| B3 | Run 签发与 bind 的先后（Python 在哪里得到版本） | 实施前核实 0179 的时序并给出时序图；优先由 SQL 在 bind 中选定，Run 在 bind 后取版本 |
| B4 | 对效应类型的不可准入发布（vocabulary 或 hide） | 在 F3 发布时直接拒绝 |
| B5 | 效应 Action 的授权跟随 | FOLLOWABLE 增加 2 条，共 5 条；只派生已准入的后继（`not_admitted`） |
| B6 | M3 → M4 的观察期 | stage 上 ≥ 7 天没有 `effect Consumer outside Action` |
| B7 | proof 表处理 | 改名为只读归档表，并纳入 erasure 覆盖；备选为导出摘要后 drop |
