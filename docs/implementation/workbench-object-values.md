# 工作台 Object 属性值（D-G1，实现说明，2026-10-20）

依据：L4 差距分析 `docs/implementation/eios-capability-gap.md`（分支 `l4/eios-gap-analysis`）中的 D-G1，负责人 2026-10-20 按推荐决定；ADR-025、ADR-035；NX-029。分支 `l2/object-property-values`，起点 c359f865。

## 读取路径（只读，不改写任何业务对象）

1. **迁移 0242（临时编号）**：
   - 新增工作台读取 verb `object_fields`，归 `nexloop.workbench.read:1`，包装方式与 0230 相同：原函数改名后照原样调用，新包装只为这个 verb 重做签名声明和成员身份核对。
   - 输入一个对象，按它的 schema 列出每个属性：是否有值、负责人是否限制了它所在的属性组、值里有没有正在擦除的 Consumer，以及当前成员的角色能否读它（用现有的成员派生 `authz.nexloop_workbench_member_may_read`）。
   - **不返回任何值，也不写任何表。**
   - 成员读不了的对象（包括 Consumer 正在擦除的对象）返回 `readable=false`，不列字段。
2. **取值**：只读取可读且有值的属性，走现有的审计读取 `authz.nexloop_read_object`（派生方式 `workbench-member-v1`，用途 `world_model`）。这次读取会在 SQL 里把对象和每个请求的属性再核对一遍，每个对象写一条审计记录。
3. **输出**：每个属性的结果是下面三种之一：
   - `ok`：带值。值是文本，或规范化的 JSON 文本，最多 2000 字，超出时截断并标 `truncated`；
   - `withheld`：只给名称。原因是 `restricted`（负责人限制的属性组）、`not_permitted`（角色不派生这项读取）或 `erasing`（值里有正在擦除的 Consumer）；
   - `missing`：没有值。
4. **接口**：
   - `GET /api/v1/workbench/objects/{type}/{id}`：抽屉用，新契约 `workbench-object-values`；
   - `GET /api/v1/workbench/objects/{type}`：列表页的每个对象多一个可选字段 `properties`（契约只新增），用的是同一次审计读取。原本判断对象是否可见就要读一次，所以审计条数不变，仍然是每个列出的对象一条。

## 工作台

- Object 页的表格：属性名作为列，夹在对象标识和版本列之间；表格外加 `.table-scroll`，只在表格内部横向滚动。
- 长值超过 48 字时截断，点开可看全文（`details`）。
- withheld 统一显示“受限”徽标（`StatusBadge`，`data-code="withheld"`），悬停提示说明原因。
- 对象抽屉新增“属性”一节，列出全部属性，长值默认展开。
- 页面导语从“只列出标识”改为说明会显示属性值。

## 测试

- `tests/test_workbench_object_values_pg.py`（4 项）：
  - 有 READ 时显示值，抽屉一次读取写一条审计，列表页每个对象一条，业务对象的 digest 不变；
  - 没有 READ 时（成员角色不派生的类型、非成员、顾客）为 403 且看不到值；其他租户的对象为 404，带 tenant 参数为 422；
  - 负责人限制的属性组只给名称（`restricted`），值不出现；
  - Consumer 正在擦除时：它本身为 403，从列表中消失；其他对象里引用它的属性显示为 withheld（`erasing`）。
- `tests/test_workbench_objects_pg.py`：列表条目现在多一个 `properties` 键。
- web：契约解析（伪造的 withheld 值被丢弃，未知状态被拒）、列与徽标、截断与展开、抽屉。
