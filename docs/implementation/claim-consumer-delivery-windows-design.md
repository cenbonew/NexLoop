# Consumer v2：`delivery_windows` 属性草案与发布设计（Claim → Consumer 阶段二、三）

- **状态**：草稿，待 L6 判定。作者：L4。基线：main `e0fa184d`。
- **依据**：`claim-to-consumer-property-plan.md` §2、§3、§10（负责人 2026-11-02 决定）、§10.1 第 6、7 项；NX-040 设计（`NX-040-design.md`）与 0254 发布事务；NX-044 差距检查 §2。
- **范围**：本文先交第 1 项（属性草案 + 发布路径），第 2–5 项只写接口，L6 通过后再实现。迁移临时号从 0416 起。

## 1. 属性草案

| 项 | 值 |
|---|---|
| 类型 / 版本 | `Consumer` v1 → v2，兼容扩展：只加一个可选属性，v1 的属性逐项不变 |
| `property_name` | `delivery_windows` |
| `value_type` | `json`（结构化值在 jsonb 中的存储形态） |
| `type_descriptor` | `array`，元素为 `struct`，见下 |
| `required` / `nullable` | `false` / `true` |
| `default_value` | `null`（不写默认值；“没有获得”和“空数组”不同，见 §4） |
| `display_name` | `送货时间` |
| `description` | `顾客说过方便收货的星期与时段（如“周五白天”），由对话中的陈述经规范化规则表得出；不含具体钟点。` |
| `render_hint` / `visibility` | `plain` / `normal` |

元素结构（两个字段都必填，都是封闭词表）：

```json
{"kind": "array", "element": {"kind": "struct", "fields": [
  {"name": "weekday", "required": true, "descriptor": {"kind": "string", "enum": ["mon","tue","wed","thu","fri","sat","sun","any"]}},
  {"name": "part",    "required": true, "descriptor": {"kind": "string", "enum": ["morning","daytime","afternoon","evening","any"]}}
]}}
```

值示例：`[{"weekday":"fri","part":"daytime"}]`；“周末上午”是 `[{"weekday":"sat","part":"morning"},{"weekday":"sun","part":"morning"}]`；“随时都行”是 `[{"weekday":"any","part":"any"}]`。

值约束（在 EIOS 描述符之外，由 §3 的 SQL 校验函数和规范化函数共同保证）：

- 数组非空，元素按 (weekday, part) 去重，按星期顺序 mon…sun、any，再按 morning、daytime、afternoon、evening、any 排序后存储，长度不超过 40（8 × 5）；同一份数据只有一种写法，比较和合并都按集合；
- 不存钟点。以后要精确时间，按兼容扩展给元素加可选的 `from` / `to`（新增可选 struct 字段）。

**需要调度员确认的一处不一致**：派工示例写的是 `{weekday:5, part:'daytime'}`（整数），计划 §10 第 2 项（负责人决定）写的是 `weekday ∈ mon…sun / any`（字符串）。本草案按 §10 用字符串：`any` 无法用 1–7 自然表示，字符串在画像页和审核里可直接读。如负责人要整数，只改词表，其余设计不变。

## 2. 发布路径（NX-040 / NX-044 schema 发布）

### 2.1 现状的阻碍

0254 `authz.nexloop_schema_proposal_publish` 对 `add_optional_property` 有两处不允许本属性：

1. `value_type` 只允许 `string / integer / number / boolean / datetime`，`json` 被判 `value_type_not_allowed`；
2. 期望的新属性固定为 `description ''`、`display_name ''`、`type_descriptor null`，提案带不进显示名、说明和描述符（`schema_not_the_proposal`）。

所以“直接走现有发布”做不到，需要一次窄的闸门扩展。

### 2.2 方案（推荐 A）

- **A. 扩展发布闸门，允许“封闭词表结构数组”这一种复合形态**（迁移 `schema_structured_property`，临时号 0416）：
  - `add_optional_property` 的提案可以带 `type_descriptor`、`display_name`、`description`；
  - `value_type='json'` 只在描述符是 `array<struct>` 且每个字段都是带非空 `enum` 的 `string`、深度为 2 时允许，其他复合形态仍判 `value_type_not_allowed`；
  - 期望的新属性改为由提案的这三项构造，其余门槛（`property_exists`、`property_security_bypass`、权限证明、`migration_over_limit`、后继 Action 一对一、`tool_signature_changed`）全部不变；
  - F1 兼容库 `schema_compat.py` 同步放开同一形态，前后端判定一致；
  - 显示名、说明长度上限沿用 NX-040 提案的文本限制。
- B. 不扩展闸门，Consumer v2 由可信配置（0050 manifest）发布：0050 遇到 v2 报 `schema increment not supported`，需要改的更多，而且绕开了人工审核这一层，不推荐。
- C. 把时间窗拆成 `string` 标量（如 `"fri:daytime,sat:any"`）：能走现有路径，但负责人决定的是结构化数组，且画像页、计划调度都要再解析一次，不推荐。

### 2.3 发布与改绑的顺序

1. 冻结一个 `add_optional_property` 提案（§1 的内容），审核员在知识工作台人工发布（D22：只能人工）。
2. 同一发布事务内：Consumer v2 写入；后继 `Consumer.edit:2` 由 0254 生成，只在版本和绑定的 Schema 引用上与 `:1` 不同；现有 Consumer 对象的 `schema_version` 升到 2，属性不变（stage 共 7 个真实 + 模拟客户，远低于 10 000 的上限）。
3. 授权：`claim_matcher` 的 `Consumer.edit` 授权已带 `follow_latest_version`（service-grants v2），不需要改授权文件；属性级 EDIT 由类型规则（property_access_rules）按对象推导，新属性自动包含在内。发布事务的权限证明保证发布本身不写任何授权。
4. `business-actions.v1.json` 中 `Consumer.edit` 的声明保留 v1（Action 定义不可变，0050 不发布 v2）。仓库里声明的是“初始版本”，后继版本由发布事务产生；在声明的 `purpose` 里补一句说明，不改编译出的定义。
5. `claim-match.json` 的 `edit_actions` 改为 `{"Consumer": ["Consumer.edit", 2]}`，只重启 claim-matcher。实现时先核对匹配器在 `:1` 仍有效时对 v2 对象的行为：0054 要求对象的 `schema_version` 等于 Action 绑定的版本，所以对象升级之后 `Consumer.edit:1` 不能再写这些对象，这一步必须紧跟发布。
6. `consumer_profile.py` 的创建 schema 加上可选的 `delivery_windows`，规则与 §1 相同（创建时可以不填）。新建 Consumer 的版本取已发布的最新版本，不写死 1。

### 2.4 安全与已有守卫

- 属性名不在敏感类（身份、联系方式、地址、支付）里：0410 拆驼峰后为 `delivery_windows`，不命中；它描述的是“方便收货的时段”，不是地址。
- 白名单 v1 为空，发布 v2 本身不会让任何值自动写入；只有白名单 v2（第 3 项）发布之后才会自动写入。
- 发布不改 owner 限制组（`owner-property-restrictions.json` 中 Consumer 的 `restricted_groups` 为空）。

## 3. 值校验（SQL 侧）

EIOS 的 Python 写入路径按描述符校验值（`validate_object_properties` → `normalize_value`），但 SQL 的编辑函数只检查 `properties` 是对象，不按描述符校验。为了让“自动写入的值一定是规范化后的值”在数据库里成立：

- 新增 `ontology.nexloop_property_value_valid(descriptor jsonb, value jsonb) returns boolean`（immutable，只认 §2.2 允许的形态，其余返回 false）；外加时间窗专用的“已排序、已去重、非空”检查。
- 0410 的自动写入判定（`ontology.nexloop_auto_write_allowed` 的调用处）在属性带描述符时先校验值，不合格的提案留在 `proposed`，原因 `value_not_normalized`，不写入。

## 4. 后续各项的接口（不在本次实现）

### 4.1 第 2 项：规范化

- 模块 `nexloop_eios/delivery_windows.py`：`normalize(text: str) -> list[dict] | None`，纯函数，规则表在代码里；返回 None 表示规范化不了。
- 星期：周一…周日、礼拜一…礼拜天、星期X、工作日（mon–fri）、周末（sat、sun）、每天、随时；英文 Monday…Sunday 及三字母缩写、weekdays、weekends、any day。
- 时段：上午、早上 → morning；白天 → daytime；下午 → afternoon；晚上、傍晚 → evening；未说时段、全天、都行 → any。英文 morning / during the day / afternoon / evening / tonight / anytime。
- 带钟点的说法（“9 点以后”“after 6pm”）按负责人决定**不换算**，返回 None，Claim 留在 `needs_resolution`。否定（“周五不在家”）、疑问句、假设句返回 None。
- 匹配器的值层（`_judge` Layer 4）对 `delivery_windows` 改为：模型选出的值必须等于 `normalize(claim value)` 的结果，否则判 `value_not_from_evidence`；模型不能自造。
- 合并：新的已应用值整体替换旧值（`latest_applied`，docs/04 §7.3），不与旧值求并集；来源是最新的那条 Claim。

### 4.2 第 3 项：白名单 v2 草案

`deploy/configuration/auto-write.v2.json`：只含 `Consumer.delivery_windows`，`reason` 写明“低风险送货偏好，值只来自规范化规则表”，`approved_by_decision` 为 `2026-11-02#claim-consumer-plan-s10-1`。只交草案，正式发布由负责人确认。

### 4.3 第 4 项：画像页接口（L9 实现）

客户详情接口在 Consumer 属性旁加 `delivery_windows` 块：

```json
{"delivery_windows": {
  "value": [{"weekday": "fri", "part": "daytime"}],
  "display": "周五 白天",
  "source": {"claim_id": "…", "message_id": "…", "evidence_excerpt": "周五白天我在家", "recorded_at": "…"},
  "effective_at": "…",
  "applied_by": "claim_match",
  "proposal_id": "…",
  "status": "applied"
}}
```

- **来源**：最新一条 `applied` 的 mutation proposal 对应的 Claim；`evidence_excerpt` 只取证据区间原文，权限沿用消息读取规则，读不到时只显示“来自对话”和时间。
- **生效时间**：`effective_at` 取 proposal 的应用时间（receipt 时间），不是消息时间；画像页同时显示“来自 X 月 X 日的对话”。
- **为空**：返回 `{"value": null, "status": "unknown"}`，页面显示“暂无（未从对话中获得）”。
- **撤回**：Claim 被更正（correction 链）后值随之清除或替换，`status` 为 `corrected`；不提供直接编辑按钮（人工修改走现有审核）。

### 4.4 第 5 项：部署单

stage 上的客户都是模拟客户（persona `simulated_agent`），rematch 会跳过它们。验收按 `run-contact-channel-demo.md` 的方式建一个 persona 为 `real` 的演示客户，让它说“周五白天我在家”，核对：提案 `applied`、Consumer 出现 `delivery_windows`、Claim `resolved`、画像页显示来源和生效时间。

## 5. 测试计划（实现阶段）

- 0416 闸门：允许的形态通过；`array<string>`、无 enum 的 struct、深度 3、`json` 无描述符都判 `value_type_not_allowed`；显示名、说明、描述符与提案不一致判 `schema_not_the_proposal`；权限证明前后摘要相同。
- 发布后：`Consumer.edit:2` 只在版本和 Schema 引用上不同；对象 `schema_version` 为 2 且属性未变；`claim_matcher` 不改授权即可执行 `:2`。
- 值校验函数：乱序、重复、空数组、未知词、多余字段都被拒绝。
- 变异检查：去掉闸门的形态限制、去掉值校验，相关用例必须失败。
