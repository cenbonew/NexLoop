# ADR-039｜统一 Action 审批（人类受治理能力）

状态：**采用**。范围与首批由负责人 2026-10-22 定为 Q1 方案 A（首批三项，quorum 1，禁止自批，默认期限 24 小时）；单人模式由负责人 2026-10-22 新增，S-C1 选修法 A（24 小时窗口），S-I1（关闭后自批票不计入）。实施任务 NX-056（切片 A0–A7）。 **修订草案 R1**（§6，2026-10-25，审批视图的目标成员接口）待 L6 复核，复核通过后由 NX-056 B1 实现、NX-058 片 D 同步。

关联：

- 设计稿 `docs/design/unified-approvals.md` 第 5 版及其补充（第 15–17 节）；L6 复核 `build/unified-approvals-review.md` 追加 1–9，以及 A2 的深审；
- 安全不变式“只有受治理的 EIOS Action 可改正式业务对象”“权限在派发时按现行状态重查”（AGENTS.md）；
- ADR-006（上游 vendored）、ADR-020（受信配置发布 Action）、NX-022（受治理入口 0124）、NX-029（保留期与擦除）、NX-058（成员与角色历史：Z1、`role_history`）；
- 迁移（临时编号，组批时重编；第二十四批合入后为 0326–0332，随后的修正为临时 0337）：审批存储、受治理入口的审批闸门、过期清扫与指标、工作台视图、首批策略的前提与受信配置、保留期与擦除、单人模式；
- 受信配置 `deploy/configuration/approval-policies.v1.json`，发布命令 `scripts/deploy/approval_policies_apply.py`，运行手册 `docs/implementation/approval-policies.md`。

## 1. 背景

NexLoop 已有两套局部审批：0240 外联审批（门控一次效应）、0103 实例候选审批（审核流程）。负责人的高风险控制，例如打开真实派发、负责人发起的擦除、解除保全，以前一人即可执行。EIOS 目录里有 `approval_mode`，但它连带效应门和 Python governor 的 REQUIRED 语义，首批能力又都不经过 governor，所以不能直接打开。

## 2. 决定

1. **范围由受信配置的审批策略决定，只针对 human 受治理能力。** 策略（`control.nexloop_approval_policies`，只追加）包括：
   - 能力和 Action 版本，精确匹配；Action 发布新版本而没有对应策略版本时，拒绝执行（fail closed）；
   - 可选的单字段等值条件，只支持“请求体顶层字段 = 常量”，字段缺失时同样需要审批；
   - quorum（1–5）和批准角色，角色不重复；
   - 期限（900 秒到 7 天）；
   - 摘要字段：按能力设白名单，不得包含 Consumer 标识或自由文本；
   - 可选的 `target_subject_field`（Z1）。

   固定规则：
   - 禁止自批，按 subject 比较；
   - 一票拒绝即终结；
   - 超期作废；
   - 不允许自动批准，quorum 不能为 0；
   - 审批的对象是某位成员时，这位成员不参与批准（Z1）。发起审批时，目标必须是本租户 invited 或 active 状态的成员。

   策略由技术配置员用文件原文发布。数据库记录清单版本、原文 sha256 和 `expected_revision`（0050 的做法），事后可以证明策略来自哪个文件。

   **放宽与停用的口径**：策略不能删除。发布新版本时，可以把条件设成永远不会命中的常量（例如 `{"field": "enabled", "equals": "never"}`），效果等于取消该能力的审批。本 ADR 把这种做法**明确认定为受治理的停用方式**，不由校验禁止。理由：
   - 它只能经受信配置发布，而受信配置需要配置员窗口，并且每次发布都记录原文 sha256 和 revision，可追溯；
   - 它在策略文件中可见，评审时一眼可以看出；
   - 禁止它，只会逼着停用者另找更不透明的办法。

   发布这样的版本时，`decision` 里必须写明停用的理由和批准人；运行手册给出了步骤。

2. **审批只决定“这次调用能否执行”，不授予任何权限。**
   - 第一次提交时请求挂起，冻结请求体并计算 digest：租户、world、Action、策略版本，以及去掉 request_id 之后的请求体。
   - 批准人经受治理的 `approvals.decide` 决定，请求人可以经 `approvals.cancel` 撤回。
   - 执行由请求人本人完成：用自己当前的会话、一个新的 request_id，再提交同一请求体。这次提交照常走完受治理入口，然后一次性消费已通过的审批。
   - 执行时重查：请求人和全部批准人的现行资格（失去资格的票不计入，审批退回等待）、期限、策略版本（旧版本的审批作废，返回 `policy_changed`）、控制暂停。
   - 之后效应派发时的各项重查照常进行，包括 real 派发开关、联系约束和预算。
   - 系统中不存在“代他人执行”的函数。

3. **接合点只在 SQL 受治理入口（0124 → 0327）。** 目录中的 `approval_mode` 保持 none；效应门、Python governor 和 ApprovalPort 都不改，也不放宽。

4. **执行语义**：
   - 处理函数在保存点中执行。处理函数拒绝时审批仍算已消费，结果记为 `execution_refused:<sqlstate>`，不自动重新发起。暂时性错误（40001、40P01、55P03、57014、53xxx、08xxx、57P0x）重新抛出，审批不被消费。
   - 行锁加消费表主键，保证并发执行时处理函数只运行一次。
   - 消费后 1 小时内，同一请求人再提交同一请求体，返回 `approval_consumed` 和结果。
   - 未执行或被拒时，动作认领的终态记为 `permanent_failure`。

5. **生命周期**：
   - 过期清扫并入保留期服务，只终结、不执行，同时终结已批准但过期未消费的审批；
   - 指标 `approvals` 块和告警 `approval_waiting`、`approval_unconsumed`、`approval_sweep_stale`；
   - 冻结请求体按 `governance_audit` 保留期删去正文；
   - Consumer 擦除开始时，终结请求体中出现该顾客 ID 的未完成审批（正在执行的那条除外）；擦除完成时，把请求体中的 ID 替换为假名；
   - 只有 owner 的函数可以在擦除作用域内改写请求体，并且只允许删去正文或整串替换为假名两种形式。

6. **工作台**：审批页分“待我审批”“待我执行”“我发起的”三栏，每位成员只看到自己的行，别人的请求体不会返回。执行就是把冻结的请求体原样再提交一次；工作台无法构造的请求体不提供执行按钮。

6a. **目标成员块 `target_member`（修订草案 R1，2026-10-25，NX-056 B1；接口以 §6 为准）**：策略声明了 `target_subject_field`（或者能力是 `invite_owner`）时，审批视图由服务端在读取时解析目标，在已有的可见范围内按调用人的角色裁剪后展示。它不进 `summary_fields`，不写审计，不改冻结请求体和 digest。

7. **上游**：状态机和到期逻辑按 ADR-006 vendored；持久化层按 NexLoop 的 authz 签名、world、RLS 和只追加规则重写。

8. **与现有审批的关系**：
   - 0240 外联审批暂时保留，自批比较已改为按 subject。之后作为效应级审批，按 purpose 原子切换并入（另行立项）。
   - 0103 候选审批属于审核流程，不并入。

9. **首批**（负责人 2026-10-22）：
   - `real_dispatch.set`，只在 `enabled=true` 时需要审批，关闭不需要，保证能立即生效；
   - `consumer.erase`，负责人发起的擦除。顾客本人请求的确认拆为 `consumer.erase_on_request`，不受审批约束；
   - `retention.release_hold`。

   三项都是 quorum 1、owner 批准、24 小时。

10. **单人模式**（将由 A7 实现；**A7 上线之前不生效**，在此之前审批一律需要另一位成员）：
    - owner 将经受治理 Action `approvals.solo_mode.set` 开启或关闭，必须写理由，默认关闭。
    - 生效条件：该租户该 world 中有资格的 active 人类成员当前恰好 1 人，并且过去 24 小时内一直不超过 1 人。人数来自按策略记录的、只追加的资格时间线和服务端时间，在每次决定和每次消费时实时计算。
    - 关闭后，已有的自批票不再计入。
    - 自批仍然分“申请”和“批准”两步，必须写理由，记录上标明 `self_approved`。
    - quorum 大于 1 的策略在单人模式下不可满足，直接报错。
    - 策略属性 `solo_mode = not_applicable` 的能力在单人模式下也不能自批，例如对 owner 的提升或重置。
    - 加锁顺序：先取租户 advisory lock，再锁成员或审批行。
    - 投票资格起点读取 NX-058 的 `role_history`（片 C 提供），按 (tenant, world)；投票冷却由 NX-058 片 D §2 实现。

## 3. 备选方案与否决理由

- **批准人在决定时直接执行**：执行身份会变成批准人，或者需要一个替换身份的函数，形成提权面（L6 C2）。
- **由异步服务主体执行**：需要一个能代表任意请求人执行的服务凭据，同样是提权面；请求人的现行权限也难以等价重查。
- **先统一到效应级挂起**：首批都是不产生效应意图的 human 控制，在效应级做不到；留到合并 0240 时再做。
- **通过 Python governor 和 ApprovalEvidence**：首批能力不经过 governor；接上之后，会连带要求放宽效应门对 REQUIRED 的拒绝，并引入 governor 对 policy 自动批准的认可。
- **条件里支持“与当前值比较”**：会引入表达式攻击面。这类需求一律拆出专用 Action，由服务端在执行时重查当前状态（NX-058 的做法）。
- **单人租户用 quorum 0 只做事后审计**：否决。单人模式放宽的只是“禁止自批”这一条，审批本身并没有被跳过。

## 4. 风险与残余

- **刷批和社会工程**：每位请求人每个能力最多 3 条等待中的审批；被拒绝或撤回后，同一 digest 冷却 1 小时；每位批准人的现行资格都会重查。
- **审计里没有正文**：工作台只显示服务端渲染的白名单字段和 digest；自由文本只出现在请求人自己的记录里。
- **单人租户在 A7 上线前无法执行受审批的 Action**：这是禁止自批的本意。stage 启用前，要先准备第二位有资格的成员（运行手册 §4）。
- **发布 Action 新版本时必须同步发布策略**：否则按 fail closed 拒绝执行。
- **策略不能删除**：要放宽就发布新版本；整体停用需要新增迁移。
- **owner 自造的第二身份**（缓解：首次投票告警 `approval_first_time_approver`，以及投票冷却，后者由 NX-058 片 D §2 实现，均见 §6）：owner 可以经成员配置再加入一个自己控制的身份。它满 24 小时后（以及在单人模式之外随时）都可以作为“另一位”批准人。这在 NexLoop 内无法区分；靠成员变更的审计和告警（`approval_solo_eligibility_reduced` 以及成员配置的发布记录）事后发现。
- **假名化之后 digest 对不上**：擦除把冻结请求体中的顾客 ID 换成假名后，请求体重新计算的 digest 与记录中的不再一致。这是有意的：被擦除顾客的请求已经在擦除开始时终结，不会再被执行；digest 只作为审计记录保留。
- **过期清扫依赖保留期服务**：保留期服务停止运行时，过期的审批不会被终结。消费时仍会检查期限，过期的审批照样不能执行。指标 `overdue` 和告警 `approval_sweep_stale` 用来发现这种情况。
- **`permanent_failure` 不只代表一种结果**：动作认领的终态 `permanent_failure` 同时覆盖“未执行”（等待、过期、已消费、策略已变）和“处理函数拒绝”。下游要分辨时，看受治理入口返回的 `status`、`approval` 字段，以及消费记录的 `result`，不能只看 outcome。
- **SQL 里还没有带 world 参数的投票资格接口**：读取 NX-058 `role_history` 的接口约定为 `(tenant, world, subject, approver_roles)`，由 NX-058 片 D §2 在 SQL 实现（数据来自片 C 的 role_history）。片 D 合入之前不启用投票冷却。
- **单人模式的残余风险（修法 A）**：如果 owner 账户被盗用，而租户本来就长期只有一人，攻击者可以开启单人模式并自批。这时审批只能留下审计和告警，不能阻止。这是负责人知情接受的取舍。

## 5. 后果

- 请求人在审批通过后，需要回来点一次“执行”。审批不会自动产生任何写入。
- 新增或调整需要审批的能力时：更新受信配置文件（必要时随迁移扩展摘要白名单），用发布命令发布，并在发布之前确认有可用的批准人。
- 顾客本人的擦除请求入口（NX-019、WebChat）接入 `erase_on_request` 另立任务。在此之前，负责人确认顾客请求时如果走 `consumer.erase`，就会受审批约束。

## 6. 修订草案 R1：审批视图的目标成员接口（权威版本，NX-056 B1 实现，NX-058 片 D 照此同步）

本节是这个接口唯一的权威定义；设计稿 `docs/design/approval-target-display.md` 和 NX-058 片 D 设计中与本节不一致的地方，一律以本节为准。

### 6.1 字段与固定键集合

- 字段名 **`target_member`**。审批视图（`runtime.nexloop_approval_view`）三栏中的**每一行都有这个键**：
  - 策略没有 `target_subject_field`，并且能力不是 `invite_owner` 时，取值为 `null`；
  - 否则取值为下面五种对象之一，用 `kind` 区分。
- 前端严格解析：键集合固定，每种 `kind` 的键集合也固定，出现未知形态一律拒绝。
- **判定顺序（写死）**，依次检查，命中即停：
  1. 策略没有 `target_subject_field`，并且能力不是 `invite_owner` → `null`；
  2. 调用人当前的工作台角色不是 owner → `hidden`，**不做任何解析**。这样非 owner 看不出目标存在不存在、是否被擦除：所有有目标的行对他都一样；
  3. 能力是 `invite_owner` → 按 §6.4（D7 未定时为 `hidden`）；
  4. 目标身份已被擦除 → `erased`；
  5. 解析不到本租户成员 → `unresolved`；
  6. 否则 → `member`。

| `kind` | 键（全部必有） | 何时出现 |
|---|---|---|
| `member` | `kind`、`label`、`username`、`account_status`、`member_status`、`role` | 目标解析到本租户成员，并且调用人当前的工作台角色是 owner |
| `hidden` | `kind` | 有目标，但调用人当前不是 owner（无论是批准人还是请求人），这时不做任何解析，所以目标存在、不存在、已擦除对他看起来都一样；以及 D7 未定或选“不展示”时的 `invite_owner`（这时对 owner 也是 `hidden`） |
| `unresolved` | `kind` | 有目标字段，但解析不到本租户成员，包括请求体中的值不是任何成员的 principal 或 subject |
| `erased` | `kind` | 目标成员的身份已被擦除（§6.5） |
| `invitee` | `kind`、`username`、`same_username_exists` | 能力为 `invite_owner`，调用人是 owner，**并且负责人 D7 选择“展示”**。D7 未定时不出现 |

### 6.2 解析规则（只用实际存在的列）

1. **取目标**：用 `control.nexloop_approval_target_subject(tenant, frozen_body, target_subject_field)`（0326）得到候选 subject：请求体的值如果是本租户的 principal，就取它的 subject；否则把原值当作 subject。
2. **确认是成员**：候选 subject 在本租户的 `control.nexloop_browser_memberships` 中至少有一行，否则为 `unresolved`。这一步不可省略：`target_subject` 的回退路径会原样返回请求体中的字符串，**不得回显**。
3. **用于展示的成员行**：
   - 请求体给的是本租户的 **principal**：就用这个 principal 的 membership 行，不换成同一个 subject 下的其他 principal（例如另一个 active 的）。批准人看到的必须正是请求要处置的那个成员身份。
   - 请求体给的是 **subject ID**：在这个 subject 本租户的 membership 中取一行，按状态排序 `active`、`invited`、`suspended`、`revoked`，再按 `principal_id` 升序，取第一行。
   - `member_status` 是这一行的状态；`role` 是最新工作台配置中这一行 principal 的角色，没有时为 `null`。
4. **`label`**：上一步那个 principal 的标签，形式为 `p-` 加 `sha256(principal_id)` 十六进制的前 8 位。
   - 不输出完整的 principal，也不输出 subject。
   - 不截取开头：principal 是部署时人工填写的字符串，常见同一前缀（例如 `stage-principal-…`），截取开头几位没有区分度；哈希前缀稳定，并且能区分。
   - 0021 中没有“显示名”列，所以不存在显示名；批准人靠用户名和标签认人。
5. **`username`**：在 `control.nexloop_browser_accounts` 中取同租户、同 subject 的一行，按账户状态排序 `active`、`invited`、`disabled`，再按 `local_account_id` 升序，取第一行的 `username`；没有账户时为 `null`。`account_status` 是这一行的账户状态（没有账户时为 `null`）。成员状态与账户状态是两个独立字段。
6. **revoked 成员**的 membership 行仍在，所以仍然解析为 `member`，并显示用户名。这是为了审计：批准人要看清楚处置的是谁。
7. **fail closed**：读取中出现任何异常或空结果（包括行级安全返回空），一律为 `unresolved`。

### 6.3 可见范围与裁剪（批准人和请求人同一规则）

- 行本身只出现在调用人自己的三栏里（“待我审批”为有资格的批准人，目标本人按 Z1 已排除；“我发起的”“待我执行”为请求人本人），不新增可见范围。
- **裁剪按调用人当前的工作台角色**：
  - 是 owner（有成员读取权，ADR-035）：给出 `member` 或 `invitee` 的完整键；
  - 不是 owner：一律 `hidden`，不给用户名、标签、角色或状态。
  - 请求人与批准人适用同一规则。理由：请求体由请求人任意填写，只按“自己提交的”放行，就能借构造的请求体把 principal 或 subject 换成用户名、角色和状态，从而枚举成员（L6 B-I1）。
- NX-058 片 D 的“非 owner 读者”口径与本节一致：取 `hidden`，而不是整块 `null`。`null` 只表示“这条策略没有目标”。

### 6.4 `invite_owner`（D7：待负责人定）

被邀请人还不是成员，`target_subject_field` 为 `null`。是否在审批行里展示被邀请人的用户名，**待负责人定（D7）**。

- **定下来之前，实现默认不展示**：`invite_owner` 的行对所有调用人都是 `{"kind":"hidden"}`。
- **不展示时，owner 批准人怎么核对**（线下）：
  1. 批准前，通过系统以外的渠道（当面或电话）向请求人确认被邀请人的用户名；
  2. 在工作台的成员列表里确认没有同名或近似名的现有成员；
  3. 执行并接受之后，在成员列表中核对新成员的用户名与线下确认的一致；不一致时，按 NX-058 的路径立即停用或撤销这个成员，并留下记录。
  - 局限：批准时无法在系统里看到用户名，只能靠线下确认，以及事后核对和撤销。
- **选“展示”时**：视图对**这一个能力**写死，从请求体取 `username`，输出 `{"kind":"invitee","username":…,"same_username_exists":…}`。
  - `same_username_exists` 由服务端判断：同租户 `nexloop_browser_accounts` 中是否已有**完全相同**的 username（`unique(tenant_id, username)`）。
  - **HTTP 层**（NX-058 片 D 实现）拒绝与本租户现有账户同名的邀请，所以正常流程下它为 `false`；为 `true` 时，说明请求绕过了 HTTP 层，批准人应当拒绝。
  - 这不是通用的“展示任意字段”机制，其他能力不得仿照。
- **测试点**：
  - 默认（不展示）：owner 与非 owner 调用人看到的都是 `{"kind":"hidden"}`；行的其他字段与请求体中都不出现 username。
  - 展示：owner 看到 `invitee` 和正确的用户名；同名时 `same_username_exists` 为 `true`；非 owner 看到 `hidden`；HTTP 层拒绝同名邀请。
- **残余风险**：近似名冒充（例如与现有成员只差一个字符）既拦不住，也不会被 `same_username_exists` 标出。靠批准人在成员列表中核对，以及接受之后的核对。

### 6.5 擦除

- 目标是工作台成员（员工），Consumer 擦除（0331）不会触及成员的 principal。
- 目前仓库没有员工身份的擦除路径。将来成员身份被擦除或假名化（username 或 principal 换成 `pseudonym:` 开头的值，或者 subject 带擦除标记）时，块显示 `{"kind":"erased"}`，不回退到请求体的原值。
- **登记 erasure coverage**（`docs/implementation/erasure-coverage.md`）：“审批视图的目标成员块是读时渲染，不保存副本；冻结请求体中的目标 principal 属于保留的员工身份（原因：审批的审计需要），与 NX-058 片 D 的员工身份保留口径相同。”

### 6.6 告警规则 v15（NX-056 B1 发布，两边统一）

v15 等于 v14 加上两条规则；指标都在 `approvals` 块中，由 B1 实现：

| 规则 | 指标 | 条件 | 来源 |
|---|---|---|---|
| `approval_first_time_approver` | `approvals.first_time_approvers_24h` | 大于 0，warning，`for_seconds: 0` | 最近 24 小时内投出本租户**第一张批准票**的不同 subject 数（自批票也算；只数 `outcome='approved'`） |
| `approval_same_person_flag` | `approvals.same_person_flags_24h` | 大于 0，warning，`for_seconds: 0` | 最近 24 小时内审计事件 `approval.same_person_flagged` 的条数。这个事件由 NX-058 片 D 写入：邀请的接受者与签发者为同一个人（S1，`same_source_as_issuer`）。payload 只有 ID 和原因码。片 D 合入之前，这个指标恒为 0 |

### 6.7 其他口径

- **有目标的策略，批准角色只能是 owner**：声明了 `target_subject_field` 的策略，`approver_roles` 必须恰好是 `{owner}`。B1 在策略守护（0326 的重定义，新迁移）和 Python 校验中都加上这一条。所以在现有规则下，“非 owner 批准人”不会出现在有目标的行上；§6.3 的 `hidden` 只针对非 owner 的请求人。
- **首票计数与 S-I1 对齐**：`first_time_approvers_24h` 按**决定事件**计数：只要写入了一条 `outcome='approved'` 的决定，就算一次投票，包括自批票，不管它在执行时是否计入。S-I1 管的是执行时这一票算不算数，告警关心的是“有人第一次投了票”这件事本身，两者口径不同，但互不矛盾；`self_approvals_24h` 也按同一口径计数。

- **投票冷却**由 NX-058 **片 D §2** 实现（读取 role_history 的接口 `(tenant, world, subject, approver_roles)`，§2 第 10 点），不是片 C。片 C 提供的是 role_history 本身。
- **先后**：B1 可以先于片 D 合入。片 D 的 owner 成员类 Action 在 stage 上启用之前，B1 必须已经合入（否则批准人看不到目标）。片 D 的设计应把这一条列为启用前置条件。
