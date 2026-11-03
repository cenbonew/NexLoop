# Consumer v2：`delivery_windows` 属性草案与发布设计（Claim → Consumer 阶段二、三）

- **状态**：v2 草稿（v1 `8825eae7`/`f1cc53e6` 由 L6 判条件 PASS，本版写入条件 C1–C3 和规则表补充），待 L6 复核。作者：L4。基线：main `e0fa184d`。
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
{"kind": "array", "max_items": 40, "element": {"kind": "struct", "fields": [
  {"name": "weekday", "required": true, "descriptor": {"kind": "string", "enum": ["mon","tue","wed","thu","fri","sat","sun","any"]}},
  {"name": "part",    "required": true, "descriptor": {"kind": "string", "enum": ["morning","daytime","afternoon","evening","any"]}}
]}}
```

值示例：`[{"weekday":"fri","part":"daytime"}]`；“周末上午”是 `[{"weekday":"sat","part":"morning"},{"weekday":"sun","part":"morning"}]`；“随时都行”是 `[{"weekday":"any","part":"any"}]`。

值约束（在 EIOS 描述符之外，由 §3 的 SQL 校验函数和规范化函数共同保证）：

- 数组非空，元素按 (weekday, part) 去重，按星期顺序 mon…sun、any，再按 morning、daytime、afternoon、evening、any 排序后存储，长度不超过 40（8 × 5）；同一份数据只有一种写法，比较和合并都按集合；
- 不存钟点。以后要精确时间，按兼容扩展给元素加可选的 `from` / `to`（新增可选 struct 字段）。

**weekday 取字符串**（mon…sun / any）：调度员 2026-11-03 确认以计划 §10 的负责人决定为准，派工中的整数示例为笔误。

## 2. 发布路径（NX-040 / NX-044 schema 发布）

### 2.1 现状的阻碍

0254 `authz.nexloop_schema_proposal_publish` 对 `add_optional_property` 有两处不允许本属性：

1. `value_type` 只允许 `string / integer / number / boolean / datetime`，`json` 被判 `value_type_not_allowed`；
2. 期望的新属性固定为 `description ''`、`display_name ''`、`type_descriptor null`，提案带不进显示名、说明和描述符（`schema_not_the_proposal`）。

所以“直接走现有发布”做不到，需要一次窄的闸门扩展。

### 2.2 方案（推荐 A）

- **A. 扩展发布闸门，允许“封闭词表结构数组”这一种复合形态**（迁移 `schema_structured_property`，临时号 0416）。`add_optional_property` 的提案可以带 `type_descriptor`、`display_name`、`description`；期望的新属性由提案的这三项构造；其余门槛（`property_exists`、`property_security_bypass`、权限证明、`migration_over_limit`、后继 Action 一对一、`tool_signature_changed`）全部不变。

  **硬上限（L6 条件 C1）**：`value_type='json'` 只在描述符**同时**满足下列各条时允许，任何一条不满足都判 `value_type_not_allowed`：

  | 层 | 要求 |
  |---|---|
  | 顶层 | `kind='array'`；必须有 `max_items`，整数 1–64；只允许键 `kind`、`element`、`max_items` |
  | 元素 | `kind='struct'`；字段数 1–8；只允许键 `kind`、`fields` |
  | 字段 | 名字匹配 `^[a-z][a-z0-9_]{0,31}$` 且不重复；`required=true`；只允许键 `name`、`required`、`descriptor` |
  | 字段描述符 | 只能是 `{kind:'string', enum:[...]}`，只允许这两个键；`enum` 1–16 项，每项是匹配 `^[a-z][a-z0-9_]{0,31}$` 的字符串且不重复（因此超长、非 ASCII、空串都被拒绝） |

  - 出现任何未知键（包括 `format`、`unit`、`element` 出现在 struct 上等）都拒绝，不忽略。
  - `max_items` 是 EIOS `PropertyTypeDescriptor` 的**新增可选字段**（vendored 副本中按“附加字段默认值不进摘要”的既有约定加入，`semantics._prune_additive_defaults` 剪掉默认值，旧定义的摘要不变），`normalize_value` 校验数组长度。
  - 同一组表驱动用例（`tests/support/structured_descriptor_cases.py`）同时驱动 SQL 闸门和 F1 兼容库 `schema_compat.py`，两边判定逐条一致。
  - `display_name`、`description` 沿用 NX-040 提案的文本长度上限，见 §2.5。
- B. 不扩展闸门，Consumer v2 由可信配置（0050 manifest）发布：0050 遇到 v2 报 `schema increment not supported`，需要改的更多，而且绕开了人工审核这一层，不推荐。
- C. 把时间窗拆成 `string` 标量（如 `"fri:daytime,sat:any"`）：能走现有路径，但负责人决定的是结构化数组，且画像页、计划调度都要再解析一次，不推荐。

### 2.3 发布与改绑的顺序（L6 条件 C2）

核实结果：`follow_latest_version` **不会在发布时自动生效**。发布事务只生成后继 Action `Consumer.edit:2`，不写授权（权限证明要求前后摘要相同）；`review_actions.py:292` 的注释和 `OPERATIONS.md` §8a.11 都写明，要到下一次 `stage_secrets.py register`（service_grants apply）才解析成后继版本的授权。所以 v1 设计里“不用改授权文件”只对一半：授权**文件**不用改，但必须执行一次 register。

部署顺序：

1. **发布**：冻结一个 `add_optional_property` 提案（§1 的内容），审核员在知识工作台人工发布（D22）。同一事务内：Consumer v2 写入；后继 `Consumer.edit:2` 生成，只在版本和 Schema 引用上与 `:1` 不同；现有 Consumer 对象的 `schema_version` 升到 2，属性不变。
2. **register**：先 `stage_secrets.py register --dry-run`，再执行。部署单逐条列出预期新增的授权：
   - `claim_matcher → eios:action:Consumer.edit:2 execute`（由 `follow_latest_version` 解析，来自 service-grants v2）；
   - 工作台角色：`workbench-roles.v1.json` 在 `e0fa184d` 中没有 `Consumer.edit` 条目，预期不新增；实现时再核对一次。
   其余差异预期为 0（`OPERATIONS.md` §8a.11 第 3 步的核对方法）。属性级 EDIT 由类型规则按对象推导，新属性自动包含，不新增授权行。
3. **改绑**：私有 `config/claim-match.json` 的 `edit_actions` 改为 `{"Consumer": ["Consumer.edit", 2]}`。
4. **重启** claim-matcher（`up -d --no-deps claim-matcher`）。

**窗口期（1 之后、4 之前）不能丢 Claim**：对象已经是 v2，`Consumer.edit:1` 写入会被 0054 拒绝（`schema_version` 与 Action 绑定版本不符）。实现要求：

- 匹配器把这种拒绝识别为“Action 版本落后于对象 Schema”（单独的原因码 `edit_action_stale_schema`），提案保持 `proposed`，匹配任务进 `retry_wait`，**不**记为 `rejected` 或 `failed`，Claim 保持可再匹配；
- 改绑并重启后，同一提案按原 `request_id` 照常应用，幂等键保证只应用一次；
- 用例：发布后、改绑前跑一次匹配（提案 `proposed`、任务 `retry_wait`、Consumer 属性不变）；改绑后再跑一次（提案 `applied`，`receipt` 一条）；再跑第三次（不重复应用，proposal 和 receipt 数不变）。
- 更稳的做法——匹配器在运行时解析 `Consumer.edit` 当前生效的最新版本，不依赖配置里的版本号——留作后续，不在本次。

**`Consumer.edit:1` 的其他写入方**（实现阶段逐项定为“跟着改”或“保留为测试基线”，写成清单）：

| 位置 | 初步判断 |
|---|---|
| `deploy/stage/claim-match.example.json` | 跟着改为 2（部署单照它改私有文件） |
| `scripts/load/stack.py:206` | 负载测试栈只发布 v1 Schema，保留 1；如果它也发布 v2 再改 |
| `tests/test_background_services_pg.py:43` | 保留为 v1 基线（测的是后台服务装配，不涉及 v2） |
| `tests/test_business_actions.py:160` | 保留：测的是可信配置声明的初始版本，声明仍是 v1 |
| 工作台人工编辑 Consumer | 实现时核对是否写死版本；写死的改为取最新生效版本，否则 v2 对象在工作台也会被拒 |

### 2.4 安全与已有守卫

- 属性名不在敏感类（身份、联系方式、地址、支付）里：0410 拆驼峰后为 `delivery_windows`，不命中；它描述的是“方便收货的时段”，不是地址。
- 白名单 v1 为空，发布 v2 本身不会让任何值自动写入；只有白名单 v2（第 3 项）发布之后才会自动写入。
- 发布不改 owner 限制组（`owner-property-restrictions.json` 中 Consumer 的 `restricted_groups` 为空）。

### 2.5 显示名和说明是自由文本（L6 条件 C1）

- **来源**：NX-040 的 Schema 提案由 NX-038 的演进候选生成（`NX-040-design.md` §2.1，来源标 `evolution`），候选内容可以由模型产出；所以提案里的 `display_name`、`description` **可能是模型生成的文本**，是提示词注入的入口。本属性的这两个字段由 L4 按 §1 手写，经调度员、L6 审过的文本进入提案；但闸门不能假设所有提案都是这样来的。
- **长度**：沿用 NX-040 提案的文本上限，闸门复核。
- **审核**：发布页把这两个字段的原文完整展示给审核员（不截断、不渲染 Markdown），审核员批准的就是看到的全文。
- **进入模型提示词时**：Context 组装把它们放在标明“非指令数据”的结构化字段里（与 Claim 原文、消息正文同一类处理），不拼进系统指令。实现时核对 Context 组装现有的 schema 渲染路径，补一条用例：说明里写一句指令样式的文本，组装结果中它只出现在数据字段里。

## 3. 值校验（SQL 侧）

EIOS 的 Python 写入路径按描述符校验值（`validate_object_properties` → `normalize_value`），但 SQL 的编辑函数只检查 `properties` 是对象，不按描述符校验。为了让“自动写入的值一定是规范化后的值”在数据库里成立：

- 新增 `ontology.nexloop_property_value_valid(descriptor jsonb, value jsonb) returns boolean`（immutable，只认 §2.2 允许的形态，其余返回 false）；外加时间窗专用的“已排序、已去重、非空”检查。
- 0410 的自动写入判定（`ontology.nexloop_auto_write_allowed` 的调用处）在属性带描述符时先校验值，不合格的提案留在 `proposed`，原因 `value_not_normalized`，不写入。

## 4. 后续各项的接口（不在本次实现）

### 4.1 第 2 项：规范化

- 模块 `nexloop_eios/delivery_windows.py`：`normalize(text: str) -> list[dict] | None`，纯函数，规则表在代码里；返回 None 表示规范化不了。
- 星期：周一…周日、礼拜一…礼拜天、星期X、工作日（mon–fri）、周末（sat、sun）、每天、随时；英文 Monday…Sunday 及三字母缩写、weekdays、weekends、any day。
- 时段：上午、早上 → morning；白天 → daytime；下午 → afternoon；晚上、傍晚 → evening；未说时段、全天、都行 → any。英文 morning / during the day / afternoon / evening / tonight / anytime。
- **限定词“只 / 仅”**：表示限定时照常规范化（“只有周六在家” → sat/any）；“不仅仅”“不只”这类不表示限定，正则用 `(?<!不)(?<!不仅)(只|仅)`，后面列出的时间按普通陈述处理（“不只周六，周日也行” → sat、sun）。
- **模态说法“一般 / 通常 / 基本上 / usually / normally”**：**接受**，按普通陈述规范化（“一般周五白天在家” → fri/daytime）。理由：送货时间窗本身就是习惯性偏好，不是承诺；提取器已经把“可能、也许、maybe”这类不确定说法判为 tentative，被 0410 前的 modality 守卫挡住，不会走到这里。规则表把这几个词列为“可忽略的修饰词”，单测覆盖。
- 带钟点的说法（“9 点以后”“after 6pm”）按负责人决定**不换算**，返回 None，Claim 留在 `needs_resolution`。否定（“周五不在家”）、疑问句、假设句返回 None。
- 匹配器的值层（`_judge` Layer 4）对 `delivery_windows` 改为：模型选出的值必须等于 `normalize(claim value)` 的结果，否则判 `value_not_from_evidence`；模型不能自造。
- **替换顺序（L6 条件 C3）**：新值整体替换旧值（`latest_applied`，docs/04 §7.3），不与旧值求并集；但“新”按来源消息的 `accepted_at`（陈述时间）判断，**不按应用时间**。一条较早的陈述在较新的值之后才被应用时（例如 rematch 释放了旧 Claim），不替换：提案记为 `superseded_by_newer_statement`，Claim 结束为 `resolved`（有更新的陈述覆盖）；同一时刻的两条不同陈述留待审核。用例覆盖“先应用较新的、后到较旧的”这一顺序。

### 4.2 第 3 项：白名单 v2 草案

`deploy/configuration/auto-write.v2.json`：只含 `Consumer.delivery_windows`，`reason` 写明“低风险送货偏好，值只来自规范化规则表”，`approved_by_decision` 为 `2026-11-02#claim-consumer-plan-s10-1`。只交草案，正式发布由负责人确认。

### 4.3 第 4 项：画像页接口（L9 实现）

客户详情接口在 Consumer 属性旁加 `delivery_windows` 块：

```json
{"delivery_windows": {
  "value": [{"weekday": "fri", "part": "daytime"}],
  "display": "周五 白天",
  "source": {"claim_id": "…", "message_id": "…", "evidence_excerpt": "周五白天我在家"},
  "stated_at": "…",
  "effective_at": "…",
  "applied_by": "claim_match",
  "proposal_id": "…",
  "status": "applied"
}}
```

- **来源**：最新一条 `applied` 的 mutation proposal 对应的 Claim；`evidence_excerpt` 只取证据区间原文，权限沿用消息读取规则，读不到时只显示“来自对话”和时间。
- **两个时间**：`stated_at` 是来源消息的 `accepted_at`（顾客说这句话的时间），画像页“来自 X 月 X 日的对话”显示的是它；`effective_at` 是 proposal 的应用时间（receipt 时间），**只用于展示**（“X 月 X 日记入”），不参与替换顺序。
- **为空**：返回 `{"value": null, "status": "unknown"}`，页面显示“暂无（未从对话中获得）”。
- **撤回**：Claim 被更正（correction 链）后值随之清除或替换，`status` 为 `corrected`；不提供直接编辑按钮（人工修改走现有审核）。

### 4.4 第 5 项：部署单

stage 上的客户都是模拟客户（persona `simulated_agent`），rematch 会跳过它们。验收按 `run-contact-channel-demo.md` 的方式建一个 persona 为 `real` 的演示客户，让它说“周五白天我在家”，核对：提案 `applied`、Consumer 出现 `delivery_windows`、Claim `resolved`、画像页显示来源和生效时间。

## 5. 测试计划（实现阶段）

- 0416 闸门（表驱动，SQL 与 `schema_compat.py` 共用）：允许的形态通过；`array<string>`、无 enum 的 struct、深度 3、`json` 无描述符，以及 C1 的四个拒绝用例——多余键、超长或非 ASCII 的 enum 值、空 enum、缺 `max_items`——还有 `max_items` 超过 64、字段数 9、enum 17 项、`required=false` 的字段，都判 `value_type_not_allowed`；显示名、说明、描述符与提案不一致判 `schema_not_the_proposal`；权限证明前后摘要相同。
- 发布后：`Consumer.edit:2` 只在版本和 Schema 引用上不同；对象 `schema_version` 为 2 且属性未变；register 之后 `claim_matcher` 获得 `:2` 的执行授权（授权文件不改），register 之前执行 `:2` 被拒。
- 窗口期：发布后、改绑前匹配 → `proposed` + `retry_wait`；改绑后应用一次；再跑不重复（§2.3）。
- 替换顺序：先应用较新的陈述、再到较旧的陈述 → `superseded_by_newer_statement`，值不变（§4.1）。
- 规范化：`(?<!不)(?<!不仅)(只|仅)`、“一般 / 通常”的正反例。
- 自由文本：说明里写指令样式文本，Context 组装中只出现在数据字段（§2.5）。
- 值校验函数：乱序、重复、空数组、未知词、多余字段都被拒绝。
- 变异检查：去掉闸门的形态限制、去掉值校验，相关用例必须失败。

## 6. 实现记录（第 1、2 项，基线 main `50427429`）

- **迁移 `schema_structured_property`**（临时号 0416）：
  - `runtime.nexloop_structured_descriptor_valid` 按 C1 的硬上限判定；
  - `runtime.nexloop_structured_descriptor_expand` 把描述符展开成 EIOS 的导出形态，0054 比较的两边因此一致；
  - `runtime.nexloop_schema_change_valid`（0252 函数体）和 `ontology.nexloop_schema_publication_gates`（0257 函数体）只改了登记在 copy guard 里的几行；
  - `ontology.nexloop_property_value_valid` 加上提案表上的触发器：值不规范时，记录的提案原因为 `value_not_normalized`，进入 applying 时被拒绝。
- **vendored EIOS**：只改了 `ontology/models.py`。
  - `PropertyTypeDescriptor.max_items` 只允许数组使用，未设置时不出现在导出结果里，写入时校验数组长度。
  - `PROVENANCE.json` 已更新 `nexloop_sha256` 和 adaptations；上游哈希和 `versions.lock` 的 commit 都没动。
  - 摘要回归用例：已发布的 Consumer v1 的两个摘要与改动前逐字节相同。
  - SQL 侧没有按描述符重建或比较摘要的地方：`git grep type_descriptor` 列出的迁移逐一核对过，都只读 `enum`，或者用 `jsonb_set` 原样保留其余键。新增属性由 0416 的 expand 构造，`max_items` 只在设置时出现。
- **匹配器**：
  - 提示词升到 `nx020-match-prompt/2`。结构化属性在 schema 视图里带上 `value_shape`；payload 带上 `normalized_values`；模型的值必须与规范化结果完全相同。
  - 窗口期判定：Consumer 对象的 Schema 版本高于所配 edit Action 绑定的版本时，抛出 `EditActionStale`。这一步在召回和模型调用之前，所以什么都不记录；worker 用 0401 的 defer 把条目延后（每次 60 秒，不消耗尝试次数，最长一周）。已有提案在 apply 时返回 `edit_action_stale_schema`，保持原状态。
  - 结构化属性的 `effective_at` 取陈述时间（消息受理时间 anchor）。较旧的陈述记为 `superseded_by_newer_statement`，Claim 随现有 superseded 路径变为 `superseded`；同一时刻的两条不同陈述不做转移，留待审核。
- **`Consumer.edit:1` 的其他写入方**（C2 清单的结论）：
  - `deploy/stage/claim-match.example.json` 这次不改：在 Consumer v2 发布之前改成 2，会让匹配器指向一个还不存在的 Action。部署单（第 5 项）按“发布 → register → 改为 2 → 重启”同时修改样例和私有文件。
  - `scripts/load/stack.py:206`、`tests/test_background_services_pg.py:43`、`tests/test_business_actions.py:160` 保留为 v1 基线。
  - 工作台人工编辑没有写死 `Consumer.edit` 的版本（`git grep` 在前端和 `review_*` 中只找到测试数据里的 `:2`），由 `follow_latest` 授权覆盖。
- **创建 schema**（`consumer_profile.py`）这次不改：Consumer v1 由它发布，必须保持不变；`delivery_windows` 只由匹配器在 v2 上写入，新建客户时不需要它。

## 7. 实现记录（第 3、4 后端、5 项）

- **白名单 v2 草案**：`deploy/configuration/auto-write.v2.json` 只含 `Consumer.delivery_windows`，带 reason，`approved_by_decision` 为 `2026-11-02#claim-consumer-plan-s10-1`。正式发布要负责人确认，见部署单第 6 步。用例覆盖两种情况：Consumer v2 发布前应用这份清单被拒绝；发布后只打开这一个属性。
- **画像读取**：现有客户详情只给出属性值，没有来源和时间。
  - 新增迁移 `workbench_property_sources`（临时号 0417），函数为 `authz.nexloop_workbench_property_sources`，只有 owner 和 operator 能调用。
  - 客户详情新增 `property_sources` 块，契约为 `packages/contracts/workbench-consumer-property-sources.schema.json`，附 example。
  - 字段：`value`、`display`、`source`（claim、conversation、message 的 id）、`stated_at`、`effective_at`、`status`（applied、corrected、unknown）。
  - 不带引用原文：原文由证据图按 K5 的规则决定谁能读，前端用 `source` 跳过去看。
  - owner 限制组里的属性不出现在列表中。
  - 设计 §4.3 原来写的 `evidence_excerpt` 因此改为不提供，`applied_by` 和 `proposal_id` 也不提供。
- **提案工具**：`s6_stage_schema.py r8` 新增可选的 `inputs.property_change`，用来冻结 `delivery_windows` 这类带描述符和文字的提案；不填时行为与原来相同。
- **部署单**：`docs/release/run-claim-consumer-phase2.md`。
