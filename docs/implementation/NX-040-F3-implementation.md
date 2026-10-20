# NX-040 F3：发布门槛与权限证明（实现说明，2026-10-20）

依据：NX-040 设计稿（e4dee217）§4、§5、§6、§7、§11 F3；D21–D27 已定案，分歧 1–3 已定案。分支 `l2/nx040-f3`，从 `l3/nx040-f2`（7ceeb388）切出，并合入了 `l2/nx040-f1`（f6f9f8b4）。

## 迁移 0254（临时编号，`0254_nx040_schema_publication.sql`）

### 发布函数

`authz.nexloop_schema_proposal_publish`：

- 只能由人发起（D22）：调用方必须是真实的浏览器人工会话，当前授权包含 `eios:action:ontology.schema.review:1`。签名声明按审核决定（0082）的方式核验，协议为 `nexloop-schema-publication-v1`。服务、Run 和 Agent 都被拒绝。
- 有两种发布：
  - `publish`：发布一个冻结的 proposal（F2）；
  - `hide`：回退一个已发布的可选属性，即发布下一个 Schema 版本，把该属性设为 hidden（分歧 1 的定案：回退是新版本，不生成 SQL 迁移）。

### 门槛

`ontology.nexloop_schema_publication_gates`，码与 F1 共用：

- SQL 不信任后端交来的结果，而是按 proposal 自己重新推出期望的下一版本，与后端构造的逐字比较。不一致时返回 `schema_not_the_proposal`。
- 其他码：
  - `parent_moved`：Parent 已经不是最新版本。proposal 转为 withdrawn，需要重新冻结；
  - `value_type_not_allowed`、`property_exists`、`not_closed_vocabulary`、`vocabulary_value_exists`；
  - `breaking_primary_key`、`edit_mode_open`：新类型不能有主键，必须 `only_edit_via_actions`；
  - `authority_content`：新类型附带了 Action，或后继 Action 找不到前驱；
  - `successor_not_one_to_one`；
  - `tool_signature_changed`：后继 Action 除版本号和绑定的 Schema 引用之外，还改了其他内容（capability、scope、风险、输入等）；
  - `property_security_bypass`：新属性或新类型的属性，与负责人限制的属性组中的属性同名；
  - `migration_over_limit`：一次迁移超过 10 000 个对象（D25）；
  - `not_published_property`：hide 只能针对本 proposal 已发布的属性。

### 权限证明（§5）

在同一个事务里，发布前后各计算一次 `authz.nexloop_schema_permission_digest`，任何差异都会让整个发布失败。digest 覆盖：

- 租户的 authority 指纹；
- 当前工作台配置中的角色与成员；
- 联系约束；
- 发布前已经存在的全部 Action 定义。

另外 0082 的 authority 指纹仍单独核对。两个 digest 都写入发布记录，表上的 check 要求两者相等。

### 发布记录

- 新表 `ontology.nexloop_schema_publications`：只追加，带租户外键和行级安全。
- proposal 通过 F2 的 `mark_published` 转为 published，permit 引用写作 `publication:<id>`。NX-039 V4 的发布许可表到位后，改为核对并使用那份许可。

### 只读依据

`authz.nexloop_read_schema_publication_basis`：给审核员的后端读取 proposal、该类型的最新版本、绑定在这个版本上的 Action、受限属性名和对象数，权限要求与审核读取相同。

## Python：`nexloop_eios/schema_publication.py`

- `SchemaPublicationPort`：只接受人工会话。提供 `basis`、`publish`、`hide` 三个方法。
- `build(basis, kind)`：
  - 先用 F1（`judge`）判定，只有允许时才构造后继 Action（复用审核发布的重新绑定方式）；
  - 下一版本在最新版本已存的 JSON 上做加法得到，所以与 SQL 推出的期望版本逐字一致。
- F1 新增码 `property_security_bypass`（受限属性名，算作 major），parent 新增 `restricted_names`。

## 测试 `tests/test_nx040_schema_publication_pg.py`（14 项）

- 白名单正例：
  - 可选属性：版本 2、后继 `Consumer.edit:2`、对象迁移到新版本、authority 指纹不变；发布前外部计算的、事务内前后记录的、发布后重算的权限 digest 四者一致；proposal 转为 published；重放；发布记录只追加。
  - 词表值；
  - 无主键、无 Action 的新类型。
- 每类拒绝都不写入任何东西：json 类型、已有属性、已有词表值、开放词表、带主键的新类型；Parent 被移动时 proposal 转为 withdrawn。
- AT-024 三处负例，都用伪造的后端提交，证明 SQL 独立拦截：
  1. 新类型附带提权 Action：`authority_content`；
  2. 改已有 Action 的权限：capability 或 scope 被改为 `tool_signature_changed`；多出一个 Action 为 `authority_content` 加 `successor_not_one_to_one`；缺少后继为 `successor_not_one_to_one`；
  3. 借新类型或新属性绕过属性安全：后端的 F1 和 SQL 都返回 `property_security_bypass`。
- 回退是新版本：版本 3 中该属性为 hidden，后继 `Consumer.edit:3`；hide 一个不属于本 proposal 的属性被拒。
- 只能由人发布：服务凭据即使持有审核授权也被拒，proposal 保持 frozen。

## 端到端（rebase 到 F2 修复 a73773d4 之后补）

`test_end_to_end_f2_freezes_f1_judges_and_f3_publishes`：
- 用 evolution writer 从 ontology_schema 候选冻结两个 proposal，一个新类型，一个可选属性（F2）；
- 冻结前先用 F1 判定，冻结的 compatibility_report_digest 就是这次判定报告的 digest；
- 发布时后端再用 F1 判定一次，然后由 F3 发布成功；
- Consumer 升到版本 2，Warranty 为版本 1，两条发布记录的权限 digest 前后都相等。

## F2 不一致（已由 L3 在 a73773d4 修复）

F2 冻结时现在拒绝带主键的新类型和非标量类型，与设计稿 §4.1、F1 一致。F3 的 SQL 仍然独立拦截这两类变更：“带主键的新类型”测试直接插入数据库，绕过 F2，验证 SQL 也会拒绝。
