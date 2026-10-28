# UI 后端 X1–X4：接口说明（给 L9）

来源：L9 工作台 UX 审查（`claude/ui-ux-05f384` 3dc04c11 的 `docs/design/workbench-ux-audit.md` §X）。分支 `l2/ui-backend-x1-x4`，基于 main 17aa74c1。迁移 0366–0368，临时编号，由调度员重排。只改后端；前端由 L9 接。

**契约**：四处都是给已有响应**追加只读字段**，没有删改旧字段。这几处响应（工作台审批视图、消费者列表、顾客对话列表）在 `packages/contracts/` 里都没有发布过 JSON Schema，所以不出新的契约版本；旧客户端忽略新字段即可。前端解析器若用白名单校验字段，要先放行新字段。

**权限**：都在原读取函数之后做（改名包装，原函数的签名校验、成员、角色、Action 授权检查都不变）。能看到这一行的人，才看到新字段。

## X1、X2：审批视图

`GET /api/v1/workbench/approvals`，`to_decide`、`mine`、`to_execute` 三栏的每一行新增：

| 字段 | 类型 | 含义 |
|---|---|---|
| `requested_by_username` | string 或 null | 发起人在本租户的工作台用户名。以下情况为 null：该成员已被擦除（NX-058 slice E）、没有账号、principal 不是本租户的成员。不会返回擦除占位名，也不会返回其他租户的账号 |
| `summary_labels` | object | `summary` 每个键对应的中文标签，例如 `{"scope_kind":"范围","enabled":"开关"}`。没有标签的字段，标签就是字段名本身 |

- 原有的 `requested_by`（principal）不变。显示建议：有用户名时显示用户名，principal 作为次要文字；null 时显示“已删除的成员”或成员别名。
- 标签表（`control.nexloop_approval_summary_label`）：`scope_kind` 范围、`enabled` 开关、`data_mode` 数据模式、`action_type` Action 类型、`amount_minor` 金额（最小货币单位）、`currency` 币种、`offering_revision` 激励目录版本、`new_role` 新角色。值本身（如 `tenant`、`true`）仍是原码，值的中文映射由前端负责。

## X3：消费者列表

`GET /api/v1/workbench/consumers` 的每一项新增：

| 字段 | 类型 | 含义 |
|---|---|---|
| `last_conversation_at` | string 或 null | 该客户所有对话中最新一条消息的受理时间，UTC，格式 `YYYY-MM-DDTHH:MM:SS.ffffffZ`。没有消息、或客户正在被擦除/已擦除时为 null |

- 新排序键：`sort=last_conversation_at`，配合 `order=asc|desc`。为 null 的客户不参与时间排序，**无论升序降序都排在最后**（按 id）。
- 不带任何筛选和排序的原始分页（`after`）也带这个字段。
- 前端 `route.ts` 的 `consumers.sort` 白名单需要加上 `last_conversation_at`。

## X4：顾客端对话列表

`GET /api/v1/conversations`（顾客自己的对话）的每一项新增：

| 字段 | 类型 | 含义 |
|---|---|---|
| `started_at` | string 或 null | 第一条消息的受理时间（与消息的 `accepted_at` 同一格式）；还没有消息时为 null |
| `last_message_at` | string 或 null | 最新一条消息的受理时间 |
| `message_count` | integer | 消息条数 |

- 没有标题字段：库里不存标题，让顾客写标题是新的写入路径，不在本次范围。建议用时间命名，例如“10月28日 10:30 开始 · 共 5 条”。
- 不读消息正文。

## 测试

`tests/test_ui_backend_x1_x4_pg.py`：
- 审批人看到发起人用户名与标签，发起人自己的一栏同样有；
- 成员擦除记录写入后，用户名为 null（principal 仍在）；其他租户查询不到；
- 消费者列表带最新消息时间，降序排序可用；擦除客户后它的时间为 null 并排在最后；
- 排序在两个方向都把 null 放最后（纯函数测试）；
- 顾客对话列表的三个字段，在 0 条和 2 条消息时都正确，且不含正文；
- 新增的辅助函数与被包装的原函数，`nexloop_api` 都不能直接执行。
