# 迁移 0400（企业交付 Action）对抗式审查

> 注：审查时该迁移的临时编号为 0400，合入 main 时重编号为 0178（处理见 `docs/implementation/NX-037-enterprise-deliver.md` §8）。

对象：分支 `l2/nx037-enterprise-deliver`，提交 `df80eb7`，`packages/eios-core/src/eios/migrations/0400_nx037_enterprise_deliver.sql`（570 行）。只读审查，不修改 L2 代码。审查人：L4，2026-10-13。

## 方法

1. **定义对比（实测，不靠阅读）**：把 `df80eb7^`（= main `ef6c7fe`，0400 之前）和 `df80eb7` 分别检出到临时工作树，各自在一次性 PostgreSQL 18 上执行完整 bootstrap。从 `pg_proc` 导出 `authz/runtime/control/ontology/eios` 下全部函数（646 个），逐项比较：`pg_get_functiondef` 函数体、`prosecdef`、`proconfig`（search_path、row_security）、owner、`proacl`、volatility。对照基准是“bootstrap 后库里的现行定义”，也就是最后一个定义该函数的迁移。
2. **允许表**：导出 `relacl`、RLS 与 force RLS 标志、`pg_policies`、各角色属性；在 0400 之后的库里，检查所有引用允许表的函数和所有使用动态 SQL 的 security definer 函数。
3. **绕过尝试（实测）**：在 `df80eb7` 的临时工作树里写临时测试（不提交），复用 L2 的 `effect_execution_fixture`（`enterprise-deliver`、`enterprise-mismatch`）。要绕过 Python 端校验、直接打到 SQL 层时，用 monkeypatch 改写签名前的载荷。同样的载荷也在 `df80eb7^` 上跑了一遍，作为基线。
4. **合并顺序**：检查其他 `l*/` 分支和 main（截至 `06b290d`）有没有迁移重定义这 8 个函数。

## 结论摘要

| # | 位置 | 严重度 | 结论 |
|---|---|---|---|
| F1 | 0400:259、352、395 | 低（纵深防御退化，已实测） | `action_version` 的精确等值比较被改成 `LIKE`：缺失或通配的 `action_version` 现在能通过 SQL 层的 `find` |
| F2 | 0400:13–23 | 低（加固建议） | 允许表没有 append-only 触发器；revoke 列表不全（实测 ACL 仍然只有 owner） |
| F3 | 0400:455，以及 `effect_catalog_metadata`/context pack | 中（功能，不是安全问题） | 绑定企业 offering 的消费者，Message Run 和回复意图全部被拒；一个消费者只能有一个 offering |
| F4 | 测试 `test_enterprise_deliver_pg.py:193` | 低（测试质量） | 不匹配测试用 `pytest.raises(Exception)`，没有断言拒绝原因 |

没有发现可以绕过 Action 与 slot 绑定、绕过 offering 一致性、写入允许表的路径，也没有引入新的锁。

## 1. 逐函数对比（核对项 1）

8 个函数的 `prosecdef`（全部是 security definer）、`proconfig`（`search_path=pg_catalog,pg_temp`、`row_security=on`）、owner（`nexloop_owner`）、`proacl`、volatility 在 0400 前后**完全一致**。其余 638 个函数没有任何变化，也没有增删函数。函数体只有下面这些差异行，其他行逐字相同：

| 函数 | 差异（前 → 后） | 判定 |
|---|---|---|
| `nexloop_assert_effect_plan` | `action_name='nexloop.service.request'` → 允许表里存在 (stable_name = step.action_name, slot = ctx.slot)；`ctx.slot='service.request:primary'` → `ctx.slot is not null` | 等价替换。两项合起来，仍然要求 PlanStep 的 Action 与上下文 slot 是允许表中的同一行 |
| `nexloop_effect_context_command` | executor_proof、run_proof 的 resource 从写死的回复 Action 改为“在允许表中”；step 的 Action 必须是允许表中、且 resource 同时等于 run_proof 与 executor_proof 的那一行；slot 取自该行 | 等价替换，而且更严：要求三者一致。0400:225 的子查询在 0400:201 已保证存在，不会出现 NULL slot |
| `nexloop_effect_intent_command_v0040` | resource 检查见 F1；新增 `v_action`（按 ctx.slot 和 c.resource_id 取 stable_name，取不到即拒绝）；`find`/upsert/insert 中的 action_name 改为 `v_action` | 绑定正确。对回复 Action 来说，`semantic` 与唯一键取值不变，已有意图的 business_digest 保持兼容。**F1** |
| `..._v0041`、`..._v0052` | 只改了 resource 检查 | **F1** |
| `nexloop_effect_catalog_hint` | `action_resource` = 回复 Action → “在允许表中，并且 = c.resource_id”；新增：Run 的 supply.delivery_action 必须等于 action_resource | 更严。之前的一致性靠“offering 只能是回复 Action”间接成立，现在显式检查 |
| `nexloop_assert_service_offering` | 原来对回复 offering 的固定检查，改为“回复分支或企业分支”二选一；`evidence_kind` 移进分支内；currency 仍然只允许 CNY；仍然要求 12 个键 | 回复分支与原条件完全等价；企业分支有格式约束（service_code、price_amount 正则） |
| `nexloop_service_catalog_scope` | `scope` 改为按 delivery_action 选择回复或企业模板 | 只影响返回给模型的描述，不影响授权 |

- **锁（核对项 4）**：新增的只有对 `control.nexloop_effect_actions` 的普通 `select`。它们不加行锁，表级只拿 AccessShareLock，只和 DDL 冲突。差异行里没有新的 `for update`、`for share` 或 advisory lock，原有加锁语句的位置和顺序逐字未变。0175 修复后的 ledger → context → object 顺序（v0052 注释所述）保持不变。
- **grant/revoke**：create or replace 保留了原 ACL，实测 `proacl` 前后相同。

## 2. 允许表的 RLS 与权限（核对项 2）

实测（0400 之后）：
- `relacl = {nexloop_owner=arwdDxtm/nexloop_owner}`，没有任何其他角色有权限；RLS 与 force RLS 都开启；唯一的策略是 `effect_actions_owner`，只对 `nexloop_owner` 生效。
- 运行时角色（api、domain_worker、action_worker、scheduler、identity、runtime、metrics、configurator、backup*）没有任何表权限。`nexloop_owner` 和 `nexloop_configurator` 不能登录。
- 0400 之后，引用这张表的函数只有本次重写的 6 个，全部只读，没有 insert、update 或 delete。
- 所有使用动态 SQL 的 security definer 函数一共 5 个（`workbench_prediction`、`goal_governed_action`、3 个 drill 函数）。它们执行的都是固定形状的 `select`，没有能指向这张表的写入路径。`goal_governed_action` 只调用已登记的 handler。

**F2（低）**：
- 0400:23 的 revoke 列表漏了 `nexloop_runtime`、`nexloop_configurator`、`nexloop_backup`、`nexloop_backup_recorder`。因为是新建表，这些角色本来就没有权限，实测 ACL 已证明，所以当前不构成问题。但如果以后某个迁移对 control schema 做批量授权，这张表不会被显式排除。
- 表上没有 append-only 触发器（同类部署配置 `control.nexloop_reply_policies` 有，见 0109:46）。改动 `slot_identity` 会让已登记的上下文悄悄换绑 Action。
- 建议：revoke 列出全部角色，并加上 `nexloop_nx022_append_only` 触发器（只能新增，不能修改或删除）。

## 3. Action 与 slot 的绑定能否被绕过（核对项 3）

| 构造的场景 | 实测结果 | 拦截点 |
|---|---|---|
| 回复上下文（默认 fixture）提交 `nexloop.enterprise.deliver` 意图 | 拒绝，0 条企业意图 | 授权层：Run 凭据没有企业 Action 的授权（`PostgreSQL authority facts unavailable`） |
| 企业上下文提交 `nexloop.service.request` 意图（参数合法） | 拒绝，0 条意图 | 同上 |
| offering 的 delivery_action 是回复 Action，提交企业意图（`enterprise-mismatch`） | 拒绝 | 0400:455 新增的 `outside catalog terms`（catalog_hint）；意图写入随事务回滚 |
| 同一个 Run 同时获得两个 Action 的授权、绑定到企业 step 后提交回复意图 | 没能构造出来：企业 fixture 中这个租户没有发布回复 Action（`published Run Action unavailable`） | 只做了代码核对：0400:273 用 ctx.slot 加 c.resource_id 取 `v_action`，slot 不符时 `effect Action outside context`；0400:53 和 0400:201 对计划和上下文也做同样的绑定 |
| offering 和意图不一致但 `action_resource ≠ resource_id` | 0400:445 拒绝 | — |

结论：绑定是多层的（Run 授权 → 上下文 slot → 意图 `v_action` → catalog_hint 的 delivery_action），实测找不到绕过路径。

### F1（低，已实测）：`action_version` 改用 LIKE 比较，SQL 层的精确校验退化

- **位置**：0400:259（v0040）、0400:352（v0041）、0400:395（v0052）。原条件是 `c->>'resource_id' is distinct from 'eios:action:nexloop.service.request:'||(p->>'action_version')`，新条件是 `not exists(...) or c->>'resource_id' not like '%:'||(p->>'action_version')`。
- **问题**：
  - `p->>'action_version'` 为 NULL 时，`not like NULL` 的结果是 NULL，整个 `or` 链的结果为 NULL，`if` 不会报错。原条件下 `is distinct from NULL` 为真，会拒绝。
  - `action_version` 为 `'%'` 或 `'_'` 时，被当成 LIKE 通配符，能匹配任意版本或任意一位数字的版本。
- **构造的失败场景（实测）**：把 Python 端（`effect_intents.py:87` 的 int 校验）签名前的 `find` 载荷改成缺少 `action_version`，或者 `action_version='%'`：
  - 在 `df80eb7` 上，两种载荷都被接受，返回意图状态 `accepted`；
  - 在 `df80eb7^` 上，同样的载荷被 `runtime effect context unavailable` 拒绝。
  - `submit` 不受影响：后续的 `::integer` 转换和 NOT NULL 约束会让它失败。
- **为什么只是低**：载荷由受信服务用 HMAC 签名，而且 Python 端先做了 int 校验。`find` 只读，只能查到同一上下文 slot 的意图。但这是 SQL 守卫这一层的纵深防御退化，而迁移的说明写的是“除替换外逐字未改”。
- **建议**：改成精确比较，例如 `not exists(select 1 from control.nexloop_effect_actions ea where ea.resource_id=c->>'resource_id' and ea.resource_id='eios:action:'||ea.stable_name||':'||(p->>'action_version'))`（NULL 时 `not exists` 为真，会拒绝）。并补一个 SQL 层测试：缺少 `action_version` 或为 `'%'` 时 `find` 被拒。

### F3（中，功能）：一个消费者不能同时使用回复 offering 和企业 offering

- **位置**：0400:455；`authz.nexloop_effect_catalog_metadata`（非 Message Run 分支要求恰好 1 个有效的 ConsumerServiceOffering，否则 `registration unavailable`）；`context_pack.py:42`（pack 的 supply 写死为回复 offering）。
- **场景**：
  - 某消费者绑定了企业 offering：它的 Message Run 无法构建 context pack；即使有 supply，回复意图也会被 0400:455 拒绝（delivery_action ≠ 回复 Action）。
  - 同时绑定两个 offering：非 Message Run 路径在 `n<>1` 处拒绝。
- **影响**：NX-037 的 30 次交互如果要求同一个消费者既收到普通回复又收到企业交付，会失败。这是安全上的正确拒绝（fail closed），不是绕过。L2 的说明文档把它列为 slice 2，建议在试运行计划里明确：企业交付使用独立的消费者，或者先完成 slice 2。

### F4（低，测试质量）

`test_enterprise_deliver_pg.py:193–197` 用 `pytest.raises(Exception)`。我实测过，当前的拒绝原因确实是 catalog_hint 的 `outside catalog terms`。但这个测试本身不能防止将来因为别的原因被拒、而绑定检查已经失效的情况。建议断言原因，并补上 F1 与第 3 节表中跨 slot 的 SQL 层用例。

## 4. 合并顺序

截至本审查，main（`06b290d`）在 `ef6c7fe` 之后没有新迁移，其他 `l*/` 分支也没有迁移重定义这 8 个函数。0400 按调度顺序先合并（L2 → L3 → L4 → L5）。要注意：今后任何迁移如果用 create or replace 重写这 8 个函数中的任何一个，必须基于 0400 之后的定义，否则会把写死的回复 Action 带回来，静默撤销允许表。建议在 0400 的测试里加一条断言：这 8 个函数的现行定义都引用 `control.nexloop_effect_actions`，并且不含写死的 `'nexloop.service.request'` 比较。

## 未覆盖

- 同一个 Run 同时获得两个 Action 授权的场景没能在 fixture 中构造出来，slot 绑定的最后一层只做了代码核对（见第 3 节）。
- 没有审查 Python 端（backend、effect_contexts、执行器）的改动，只审查了迁移和它的 SQL 行为。
- 临时测试和临时工作树只在审查时使用，没有提交，已清理。
