# 草稿：NX-040 后续 ADR：停用前驱 Action，并让工作台角色跟随后继（未定，待负责人决定）

- **状态**：草稿，L7，2026-11-09。只是记录，不是决定，不改 planning。
- **背景**：stage R73 发布 Consumer v2 之后，98 个绑定 Consumer 的 Action 同时有 `:1` 和 `:2` 两个 active 版本。迁移 `schema_publication_successor_coexistence`（R74）是急修：工作台继续执行有授权的 `:1`，service-grants 通过 lineage 跟随到 `:2`。并存会一直保留下去，每次发布还会让 active 定义翻一倍。

## 待决定 1：发布时是否停用前驱（原方案 c）

- **做法**：F3 发布在同一个事务里，把被取代的前驱设为 `active=false`，同时加一个修复迁移，处理已经存在的并存对。
- **冲突**：F3 规定发布不写授权（§5，权限 digest 前后相等）。前驱停用后，所有只授权了 `:1` 的调用方会立刻失去执行权。所以停用的前提是，所有调用方在发布前已经声明跟随，或者在同一个事务里迁移授权；后者需要修订 §5。
- **影响面**：
  - 工作台 roles（精确版本）；
  - service-grants（只有声明了 follow 的才跟随）；
  - claim-match.json 等配置里写死的版本；
  - 进行中的 Run 绑定的旧版本（要定义宽限期，或者让旧版本只能读、不能执行）。

## 待决定 2：工作台角色支持 follow

- **做法**：`workbench-roles` 中的条目可以声明 `follow_latest_version`，`workbench_roles` 编译时，按 lineage 的同一套“纯 schema 重绑定”规则，为后继版本生成授权（规则见 `service_grants._rebinding_reason`）。
- **要求**：
  - 只跟随 human 发布、权限 digest 不变的后继；
  - 成员授权的变化，照旧经过 configurator 和审计；
  - 撤销时，各版本一起撤销。

## 建议顺序

先做“待决定 2”（roles follow），所有调用方都能自动跟到 `:2` 以后，再做“待决定 1”（停用前驱）。在此之前，并存是允许的状态，靠 R74 的规则保证正确：只选有授权的版本，执行时按精确 resource 检查。
