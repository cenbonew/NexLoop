# 草稿：ADR 附注——效应 Action 版本固定期间的 scope 证明过渡规则（A′）

- **状态**：L7 草稿，2026-11-10，未生效。需要负责人确认后，才能实施迁移和代码。不改 planning。
- **关联**：NX-040 F3（发布不写授权，§5）；ADR-030（效应 slot）；0179 effect_context_command；0183 effect_intent_command_v0040；设计清单 `docs/planning-drafts/p0-consumer-v2-effect-chain-inventory-20261110.md` §5。

## 背景
效应 Action（`nexloop.service.request`、`nexloop.enterprise.deliver`）的版本固定为 `:1`，固定在以下位置：
- 全局允许名单 `control.nexloop_effect_actions`；
- Host；
- context-pack contracts。

F3 发布 Consumer 的新版本时，会把真实世界中的全部 Consumer 对象升版，并为每个绑定 Consumer 的 Action 生成 `:N`。原有的安全不变量是：“执行某个 Action 版本时，该版本的 change_scope 必须精确包含对象当前的 (type, version)”。在这个不变量下，`:1` 不能再处理已升版的 Consumer，效应链因此中断（stage 2026-11-09 至 11-10）。

## 决定（过渡规则）
在 0183 的 intent 检查和 0179 的 registrar 检查这两处，且只在这两处，scope 证明可以由同一定义谱系中的后继提供。条件如下，全部在 SQL 中判定，缺一即拒绝：
1. 后继 `:N` active；与被执行的 `:1` 是同一 stable_name；去掉版本号后两者的定义逐字段相等（operation、handler、input_schema、target_systems、policy_refs、approval_mode、capability，以及 change_scope 的 type 集合）。
2. `:N` 出自一次人工的 F3 发布：在 `ontology.nexloop_schema_publications.successor_actions` 中，`published_by` 为 human，`permission_digest_before = permission_digest_after`。
3. `:N` 的 scope 精确包含对象的当前版本 v。
4. 类型 v 相对于 v−1 只增不改：前缀属性逐项相等，新增属性可空、非必填。所以 add_vocabulary_value 和 hide 产生的版本不适用。
5. 留痕：执行的资源（`:1`）和证明的资源（`:N`，含 publication_id）都要写入 intent/context 的 claims 和审计。

授权不变：执行者仍须持有 `:1` 的有效授权；registrar 自身按 L6 Q1/Q2 精确选版本，SQL 精确比对。

## 期限与移除
- 本规则是过渡规则。方案 B 交付后删除（B：允许名单按 (stable_name, version) 存储，Host 和 contracts 改为版本白名单）。
- 守护测试：一旦允许名单接受 version>1 的效应 Action，测试就变红，要求同时删除本规则和对应的 SQL 分支。
- “删除 A′”列为 B 的验收项。

## 不做
- 不在其他入口使用后继证明。§4 已复现：工作台 Action 和队列不受影响。
- 不放宽“精确版本”：v 必须被某个后继精确包含。
- 不改授权：发布仍不写授权，F3 §5 不变。
