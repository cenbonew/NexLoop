# ADR-046｜效应 Action 版本固定期间，Consumer scope 可由已发布的后继证明（过渡规则 A′）

- **状态**：已采纳（负责人 2026-11-10 选定 A′；L6 2026-11-10 判定条件 A′-1 至 A′-6 及 D1/D2）。过渡规则，方案 B 交付后删除。
- **依据**：
  - 设计清单：`docs/planning-drafts/p0-consumer-v2-effect-chain-inventory-20261110.md`。
  - 迁移：`effect_scope_successor_proof`（0446）。
  - 测试：`tests/test_effect_scope_successor_proof_pg.py`、`tests/test_create_versions_pg.py`、`tests/test_stage_follow_registrar.py`、`tests/test_consumer_v2_stage_path_pg.py`、`tests/test_relay_reply_alert_pg.py`。
  - 关联：NX-040 F3（发布不写授权，§5）、ADR-030（效应 slot）。
  - 运行单：`docs/release/run-r76-consumer-v2-effect-chain.md`。

## 1. 背景

- 效应 Action（`nexloop.service.request`、`nexloop.enterprise.deliver`）的版本在三处写死为 `:1`：
  - 全局允许名单 `control.nexloop_effect_actions`（stable_name 是主键，version 固定为 1）；
  - Agent Host（`context-input.ts`）；
  - context-pack contracts。
- NX-040 F3 发布 Consumer 新版本时会做两件事：
  - 把真实世界里全部 Consumer 对象升到新版本；
  - 为每个绑定 Consumer 的 Action 生成 `:N`。
- 原有安全不变量要求：执行某个 Action 版本时，该版本的 change_scope 必须精确包含对象当前的 (type, version)。
- stage 2026-11-09 发布 Consumer v2 之后，有两处被拒，旧顾客的回复因此全部失败约 20 小时：
  - registrar 写死 `:1`，其 scope 只含 Consumer:1（0179）；
  - 效应 Action `:1` 的 scope 不含 Consumer:2（0183）。

## 2. 决定

1. **registrar（0179）**：
   - registrar 的 resource 是该 verb 的 stable_name 的某个 active 精确版本，不再写死 `:1`。
   - 调用方在 bind 自己的事务里选版本：取 scope 精确覆盖 step、goal、control、Consumer 四个对象各自 (type, version) 的版本中最高的一个，且调用方须持有当前有效的 execute 授权。
   - SQL 仍按精确 resource 校验授权，并逐对象精确比对 scope 和快照，所以选错只会失败，不会扩权。
2. **效应 intent（0183），仅此一处放宽**：
   - 效应 Action `:1` 的 scope 不含 Consumer 的当前版本 v 时，允许由同一谱系的后继 `:N` 证明，条件全部在 SQL 中判定，缺一即拒绝：
     1. `:N` 是 active 的，与 `:1` 同一 stable_name；
     2. 两者去掉版本号（version、contract_digest，以及类型引用的 version 和 schema_digest）后定义逐字段相等，capability 也相等；
     3. `:N` 出自一次人工 F3 发布：在 `ontology.nexloop_schema_publications.successor_actions` 中，`published_by` 为 human，`permission_digest_before = permission_digest_after`；
     4. `:N` 的 scope 精确包含 Consumer:v；
     5. Consumer v 相对 `:1` scope 中的版本 b 累计只增不改（D1）：b 的属性是 v 属性的前缀，逐项相等；之后新增的属性非必填、可空、非 hidden、无 default（D2）；其余键相同。
3. **留痕（A′-4）**：`control.nexloop_effect_scope_proofs` 按 effect context 记录：
   - 执行的资源（`:1`）；
   - 证明的资源（`:N`）；
   - publication_id；
   - Consumer 版本。
4. **授权跟随**：
   - registrar 的两个 Action 在 `stage-principals.v1.json` 中逐条声明 `follow_latest_version`。
   - register 经 lineage 派生 `:N` 授权，dry-run 列出每条派生授权的 scope 差异；遇到被拒的后继时退出，从不派生 scope 扩大的后继（Q4/Q5）。
   - 发布本身仍不写授权。
   - **Q5′（负责人 2026-11-10 “按推荐”）**：follow 另放宽到 business_owner → `Consumer.create:1`，不推广到其他效应或 create Action。FOLLOWABLE 白名单逐条列出，共 3 条，测试断言集合相等。`<Type>.create` 的后继除上述等价条件（去掉版本号后定义相等、人工发布且权限摘要不变）外，scope 中只允许 `<Type>` 的版本变化，否则拒绝（`create_scope_other_type`），register 不写任何授权。
5. **新对象写最新已发布版本（Q6）**：
   - 新建对象由覆盖最新已发布版本、且调用方有授权的 `<Type>.create` 写入；不满足时抛错，不回落到 v1。
   - 就绪检查报告“最新版本没有 create Action 覆盖”的类型（`schema_create_uncovered`）。
6. **告警（Q10）**：alert rules v37 `relay_reply_failing`（critical）：15 分钟内 relay 尝试失败或用尽，且没有任何回复 intent 时触发。

## 3. 理由与代价

- 选 A′ 而不是 B：B 要改效应 Action 的允许名单、Host 和 contracts，需要发版，P0 来不及。A′ 只放宽一处，而且这条证明来自同一份已审查的定义谱系：去掉版本号后与 `:1` 完全相等，并经人工发布。
- 代价：
  - 安全不变量从“执行版本的 scope 精确包含对象版本”扩展为“执行版本，或其人工发布的等价后继的 scope 精确包含”。
  - 每次使用都会留痕。
  - 只适用于“只追加可选属性”的类型版本；词表扩展和 hide 不适用。
- 否决的替代：
  - A（任一后继都可证明 scope，不比对定义等价）——等于用 `:2` 的 scope 执行 `:1`，L6 否决（2026-11-10）。
  - C（把对象降回 v1 或撤回发布）——没有经过测试的受治理回退路径，而且会丢失 v2 数据（2026-11-10）。

## 4. 影响

- **期限**：本规则是过渡规则，B 交付后删除。守护测试 `test_the_transitional_rule_goes_with_option_b`：允许名单一旦接受 version 不为 1 的效应 Action（B 合入），测试就变红，要求同时删除以下三样：
  - `authz.nexloop_effect_scope_successor_proof`；
  - 它在 intent 命令中的调用；
  - `control.nexloop_effect_scope_proofs`。
- “删除 A′”列为 B 的验收项。
- 不在其他入口使用后继证明：工作台 Action 和队列已复现，不受影响（设计清单 §4）。
