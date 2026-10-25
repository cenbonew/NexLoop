# NX-056 B1（设计稿）：审批视图展示目标成员；首次投票告警（NX-058 D6）

> **接口以 ADR-039 §6（修订 R1）为准。** 本稿保留评估过程。其中的字段名 `target`、键集合、非 owner 裁剪、`invitee_username` 形状，以及“冷却由片 C 提供”的说法，都已被 ADR-039 §6 取代：字段名为 `target_member`，`kind` 分五种，请求人与批准人同一裁剪规则，投票冷却在片 D §2 实现。

起因：NX-058 片 D 的设计复核（`docs/design/nx058-owner-actions.md`）。owner 专用的成员类 Action（提升、降级、停用、撤销、签发重置码、恢复）接入 NX-056 审批后，批准人必须看得到“要改的是谁”。但 ADR-039 和 0326 的白名单规定 `summary_fields` 不能放身份标识。本稿评估调度员转来的提议，并给出 D6 的意见。

## 1. 结论

1. **提议可行，并且与 ADR-039、擦除、视图权限都一致**。前提是做成独立的“目标成员”块，而不是放进 `summary_fields`。理由见 §2。
2. **改动**：视图函数增加 `target` 块；HTTP 透传，不新增端点；前端增加一列和严格解析。不涉及新 Action、新策略字段，也不涉及迁移以外的写入。见 §3。
3. **切片**：作为 **NX-056 B1**，在 NX-058 片 D 之前或与它同批合入。它只依赖已有的 `target_subject_field`（Z1），不依赖片 D 的代码；片 D 的策略一发布就能用。见 §4。
4. **D6 首次投票告警：同意做**，放在 B1 一起做，用一个计数指标和一条告警规则。它针对的是 ADR-039 §4 中“owner 自造的第二身份”这项残余风险。见 §5。
5. **对片 D 设计的一处修正建议**：片 D 想把 `target_principal`（principal ID）和 `invite_owner` 的 `username` 放进 `summary_fields`。这与 0326 的白名单原则冲突：名字里带 principal 的字段会被禁止名单拒绝；username 是身份标识。建议改为：
   - `summary_fields` 只放 `new_role`；
   - 目标通过本稿的 `target` 块展示；
   - `invite_owner` 没有已存在的目标成员，被邀请人的 username 放进它的 `target` 块（§3.1 第 4 点）。

## 2. 一致性评估

| 约束 | 本提议如何满足 |
|---|---|
| ADR-039 §2.1、0329：`summary_fields` 只放能力白名单内、不含身份和自由文本的字段 | 不改。目标成员不经 `summary_fields`，白名单也不加任何身份字段 |
| 视图权限（0329）：每位成员只看到自己的行，别人的请求体不返回 | `target` 块只出现在已经可见的行里：<br>• “待我审批”：有资格的批准人，目标本人按 Z1 已排除；<br>• “我发起的”“待我执行”：请求人本人，目标就是他自己提交的。<br>不新增可见范围，也不返回冻结请求体中的其他字段 |
| 展示的是谁 | 由服务端用策略的 `target_subject_field`，从冻结请求体中取值，再按 Z1 同样的规则解析为本租户的成员（principal 或 subject）。展示**现在**的用户名、成员状态和当前工作台角色（读的时候实时解析，不冻结）。批准人看到的是目标此刻的状态：如果目标已被停用，或者角色已经变了，批准人能看出来 |
| 用户名属于身份标识，能不能给批准人看 | 批准人都是 owner（首批与片 D 的策略都是 `approver_roles: ["owner"]`）。owner 本来就能在工作台的成员读取中看到成员用户名（ADR-035），所以没有扩大谁能看到什么。如果将来某条策略允许 operator 或 reviewer 批准，`target` 块只给出目标的角色和状态，不给用户名（按批准人当前的角色判断） |
| 审计和导出不含身份正文 | 不改。审计只有 ID 和代码；`target` 块只是读时渲染，不写入任何表 |
| 擦除和假名化（0331） | 目标是工作台成员（员工），不是 Consumer。Consumer 擦除只替换请求体中等于该 Consumer ID 的完整字符串，不会碰到成员的 principal。万一解析不到成员，`target` 显示“目标已不在租户中”，不会回退去展示请求体原值。员工账号的停用与撤销走 NX-058 自己的路径，`target` 实时反映 |
| 冻结请求体与 digest | 不改。`target` 只读取请求体，不改写，digest 不受影响 |

## 3. 改动

### 3.1 SQL（新迁移，重新定义 `runtime.nexloop_approval_view_row`，登记复制件守护）

在行对象中增加 `target`：

- 策略没有 `target_subject_field`：`null`。
- 有，并且解析到本租户成员：

  ```
  {"principal_id": …, "username": …, "member_status": "active|invited|suspended|revoked", "role": "<当前工作台角色或 null>"}
  ```

  - `username` 取自 `control.nexloop_browser_accounts`（同租户、同 subject）。没有本地账号时为 `null`。
- 有，但解析不到：`{"unresolved": true}`，不带任何原值。
- `invite_owner`（没有已存在的目标成员）：片 D 为它设一个新的策略属性，或者就用 `target_subject_field = null`，同时由视图把请求体中的 `username` 作为 `{"invitee_username": …}` 展示。推荐后者，并且在视图中写死只对这一个能力这样做，不做成通用机制，避免以后被当作“展示任意字段”的入口。
- **批准人不是 owner 时**（将来的策略），`username` 和 `principal_id` 置为 `null`，只保留 `member_status` 和 `role`。判断依据是调用人当前的工作台角色。

视图函数的其余部分不变。所有读取都在已有的 owner 函数里进行，带 `row_security=on` 和租户设置，与 `nexloop_approval_subject_of` 的写法一致。

### 3.2 HTTP

`GET /api/v1/workbench/approvals` 的行增加 `target` 字段，没有新端点、新参数。`execution_inputs` 不受影响。

### 3.3 前端

- 解析器的行键集合加上 `target`（严格校验，与 §3.1 的几种形态逐一对应），未知形态一律拒绝。
- 页面的“内容摘要”列之前加“目标”列：
  - `用户名（当前角色，成员状态）`；
  - 解析不到时显示“目标已不在租户中”；
  - 邀请类显示“邀请：用户名”；
  - 非 owner 批准人只看到“角色、状态”。
- 测试：三种形态的解析和渲染；冻结请求体中的其他字段不出现。

### 3.4 测试（PG）

- 策略带 `target_subject_field` 时，“待我审批”行有 `target`，内容是现在的用户名和角色；
- 目标先被降级，再刷新视图，看到的是新角色；
- 目标不存在（解析不到）时，显示 `unresolved`，且不含原值；
- 没有目标字段的策略，`target` 为 `null`；
- 非 owner 的批准人（测试策略 `approver_roles` 含 operator）看不到用户名；
- 冻结请求体中的其他字段（例如 reason）不出现在 `target` 中。

## 4. 切片与排期

- **NX-056 B1**：§3 加上 §5，一个迁移（临时编号，组批时重编）加前端，约 0.5–1 天。
- **与片 D 的先后**：B1 可以先于片 D 合入。没有目标字段的策略不受影响，现有首批三项都没有目标字段。片 D 合入时，它的策略直接用 `target` 块展示；片 D 的设计按 §1 第 5 点调整 `summary_fields`。
- **B1 是否阻塞片 D 上 stage**：是。没有 B1，批准人在审批页上看不到目标，只能核对 digest。对成员类操作，这不足以支撑一个负责任的批准。建议两者同批上线。

## 5. D6 首次投票告警：意见

- **目的**：缓解 ADR-039 §4 的残余风险“owner 自造的第二身份当批准人”。一个以前从未投过批准票的成员第一次批准时提醒一次，便于事后把“加人、批准”这个序列串起来。它与 NX-058 片 C 的投票冷却（role_history）互补：冷却阻止刚加入的人立刻投票；告警让第一次投票被看见。
- **做法**（NX-056 的读模型，B1 一起做）：
  - 指标：`approvals` 块增加 `first_time_approvers_24h`，即在最近 24 小时内投出“本租户内第一次批准票”的不同 subject 数。只数 `outcome='approved'`；自批票也算。
  - 告警规则 `approval_first_time_approver`：`approvals.first_time_approvers_24h` 大于 0，warning，`for_seconds: 0`。告警规则版本加 1，成为 v15，即 v14 加上这一条。
  - 审计筛选：`approval.decided` 本身已经带 actor 和 `self_approved`，不需要新事件。
- **局限**：
  - 告警按指标评估，触发后在 24 小时内持续显示，不是“只发一次”的通知。
  - 新成员第一次正常批准也会触发，属于预期内的提醒：首批租户成员很少，噪声低。
  - 如果噪声成为问题，可以只对带 `target_subject_field` 的策略（成员类）计数。这是 B1 复核时可以选择的收窄方式，本稿默认不收窄。
- **不做**：不按“首票”阻止投票。阻止属于片 C 冷却的职责，告警只负责让事情被看见。

## 6. 不改 ADR-039 正文，只追加一条修订记录（草案）

在 ADR-039 §2 第 6 点（工作台）后追加：

> 6a.（B1，修订草案）策略声明了 `target_subject_field` 时，审批视图由服务端解析目标成员，在已有的可见范围内展示目标的当前用户名、角色和成员状态。批准人不是 owner 时，不展示用户名；解析不到时，只标明“不在租户中”。目标块是读时渲染，不写审计，也不进入 `summary_fields`；`summary_fields` 的白名单原则不变。

在 §4 残余风险“owner 自造的第二身份”后追加：

> 缓解（B1）：首次投票告警 `approval_first_time_approver`；投票冷却由 NX-058 片 C 提供。
