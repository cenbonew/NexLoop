# ADR-039｜统一 Action 审批（人类受治理能力）

状态：**采用**。范围与首批由负责人 2026-10-22 定为 Q1 方案 A（首批三项，quorum 1，禁止自批，默认期限 24 小时）；单人模式由负责人 2026-10-22 新增，S-C1 选修法 A（24 小时窗口），S-I1（关闭后自批票不计入）。实施任务 NX-056（切片 A0–A7）。 **修订草案 R1**（§6，2026-10-25，审批视图的目标成员接口）待 L6 复核，复核通过后由 NX-056 B1 实现、NX-058 片 D 同步。

关联：

- 设计稿 `docs/design/unified-approvals.md` 第 5 版及其补充（第 15–17 节）；L6 复核 `build/unified-approvals-review.md` 追加 1–9，以及 A2 的深审；
- 安全不变式“只有受治理的 EIOS Action 可改正式业务对象”“权限在派发时按现行状态重查”（AGENTS.md）；
- ADR-006（上游 vendored）、ADR-020（受信配置发布 Action）、NX-022（受治理入口 0124）、NX-029（保留期与擦除）、NX-058（成员与角色历史：Z1、`role_history`）；
- 迁移（临时编号，组批时重编；第二十四批合入后为 0326–0332，随后的修正为临时 0339）：审批存储、受治理入口的审批闸门、过期清扫与指标、工作台视图、首批策略的前提与受信配置、保留期与擦除、单人模式；
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
   - 审批的对象是某位成员时，这位成员不参与批准（Z1）。发起审批时，目标必须是本租户的成员，并且成员状态在该能力声明的可接受状态之内，默认只接受 active（修订草案 R3 §8.1）。

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

6a. **目标成员块 `target_member`（修订草案 R1，2026-10-25，NX-056 B1；接口以 §6 为准）**：策略声明了 `target_subject_field`（或者能力是 `workbench_member.invite_owner`）时，审批视图由服务端在读取时解析目标，在已有的可见范围内按调用人的角色裁剪后展示。它不进 `summary_fields`，不写审计，不改冻结请求体和 digest。

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
  - 策略没有 `target_subject_field`，并且能力不是 `workbench_member.invite_owner` 时，取值为 `null`；
  - 否则取值为下面五种对象之一，用 `kind` 区分。
- 前端严格解析：键集合固定，每种 `kind` 的键集合也固定，出现未知形态一律拒绝。
- **判定顺序（写死）**，依次检查，命中即停：
  1. 策略没有 `target_subject_field`，并且能力不是 `workbench_member.invite_owner` → `null`；
  2. 调用人当前的工作台角色不是 owner → `hidden`，**不做任何解析**。这样非 owner 看不出目标存在不存在、是否被擦除：所有有目标的行对他都一样；
  3. 审批记录的能力是 `workbench_member.invite_owner` → `invitee`（§6.4；出错时为 `unresolved`）；
  4. 目标身份已被擦除 → `erased`；
  5. 解析不到本租户成员 → `unresolved`；
  6. 否则 → `member`。

| `kind` | 键（全部必有） | 何时出现 |
|---|---|---|
| `member` | `kind`、`label`、`username`、`account_status`、`member_status`、`role` | 目标解析到本租户成员，并且调用人当前的工作台角色是 owner |
| `hidden` | `kind` | 有目标，但调用人当前不是 owner（无论是批准人还是请求人），这时不做任何解析，所以目标存在、不存在、已擦除对他看起来都一样；`workbench_member.invite_owner` 对非 owner 同样是 `hidden` |
| `unresolved` | `kind` | 有目标字段，但解析不到本租户成员，包括请求体中的值不是任何成员的 principal 或 subject |
| `erased` | `kind` | 目标成员的身份已被擦除（§6.5） |
| `invitee` | `kind`、`username`、`same_username_exists` | 能力为 `workbench_member.invite_owner`，并且调用人是 owner（D7，负责人 2026-10-25 定为 A：展示） |

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

### 6.4 `invite_owner`（D7：负责人 2026-10-25 定为 A，展示）

被邀请人还不是成员，`target_subject_field` 为 `null`。负责人 2026-10-25 定 D7 为 **A：展示**，即对 owner 调用人输出 `invitee` 变体；非 owner 仍为 `hidden`。下面保留“不展示”分支的说明，仅作记录。

- （已不适用）定下来之前的默认是不展示，即 `invite_owner` 的行对所有调用人都是 `{"kind":"hidden"}`。
- （记录）**不展示时，owner 批准人怎么核对**（线下）：
  1. 批准前，通过系统以外的渠道（当面或电话）向请求人确认被邀请人的用户名；
  2. 在工作台的成员列表里确认没有同名或近似名的现有成员；
  3. 执行并接受之后，在成员列表中核对新成员的用户名与线下确认的一致；不一致时，按 NX-058 的路径立即停用或撤销这个成员，并留下记录。
  - 局限：批准时无法在系统里看到用户名，只能靠线下确认，以及事后核对和撤销。
- **展示（现行）**：视图对**这一个能力**写死（按审批记录的能力 `workbench_member.invite_owner` 识别，**不看**请求体的 `operation`：请求体由请求人填写，按它判断会让任何能力的请求伪装成邀请，从而遮住真实的目标），从请求体取 `username`（不是字符串时为 `unresolved`），输出 `{"kind":"invitee","username":…,"same_username_exists":…}`。
  - `same_username_exists` 由服务端判断：同租户 `nexloop_browser_accounts` 中是否已有**完全相同**的 username（`unique(tenant_id, username)`）。
  - **HTTP 层**（NX-058 片 D 实现）拒绝与本租户现有账户同名的邀请，所以正常流程下它为 `false`；为 `true` 时，说明请求绕过了 HTTP 层，批准人应当拒绝。
  - 这不是通用的“展示任意字段”机制，其他能力不得仿照。
- **测试点**：
  - ~~默认（不展示）：owner 与非 owner 调用人看到的都是 `{"kind":"hidden"}`~~（作废：D7 已定为展示）。
  - 伪造：其他能力的请求体即使写了 `operation: invite_workbench_owner`，也不会得到 `invitee`。
  - 展示：owner 看到 `invitee` 和正确的用户名；同名时 `same_username_exists` 为 `true`；非 owner 看到 `hidden`；HTTP 层拒绝同名邀请。
- **残余风险**：近似名冒充（例如与现有成员只差一个字符）既拦不住，也不会被 `same_username_exists` 标出。靠批准人在成员列表中核对，以及接受之后的核对。

### 6.5 擦除

- 目标是工作台成员（员工），Consumer 擦除（0331）不会触及成员的 principal。
- 目前仓库没有员工身份的擦除路径。将来成员身份被擦除或假名化（username 或 principal 换成 `pseudonym:` 开头的值，或者 subject 带擦除标记）时，块显示 `{"kind":"erased"}`，不回退到请求体的原值。
- **登记 erasure coverage**（`docs/implementation/erasure-coverage.md`）：“审批视图的目标成员块是读时渲染，不保存副本；冻结请求体中的目标 principal 属于保留的员工身份（原因：审批的审计需要），与 NX-058 片 D 的员工身份保留口径相同。”

### 6.6 告警规则 v16（NX-056 B1 发布，两边统一）

v16 等于 v15（v14 加上 NX-057 的修复类规则）再加上两条规则；指标都在 `approvals` 块中，由 B1 实现：

| 规则 | 指标 | 条件 | 来源 |
|---|---|---|---|
| `approval_first_time_approver` | `approvals.first_time_approvers_24h` | 大于 0，warning，`for_seconds: 0` | 最近 24 小时内投出本租户**第一张批准票**的不同 subject 数（自批票也算；只数 `outcome='approved'`） |
| `approval_same_person_flag` | `approvals.same_person_flags_24h` | 大于 0，warning，`for_seconds: 0` | 最近 24 小时内审计事件 `approval.same_person_flagged` 的条数。这个事件由 NX-058 片 D 写入：邀请的接受者与签发者为同一个人（S1，`same_source_as_issuer`）。payload 只有 ID 和原因码。片 D 合入之前，这个指标恒为 0 |

### 6.7 其他口径

- **有目标的策略，批准角色只能是 owner**：声明了 `target_subject_field` 的策略，`approver_roles` 必须恰好是 `{owner}`。B1 在策略守护（0326 的重定义，新迁移）和 Python 校验中都加上这一条。所以在现有规则下，“非 owner 批准人”不会出现在有目标的行上；§6.3 的 `hidden` 只针对非 owner 的请求人。
- **首票计数与 S-I1 对齐**：`first_time_approvers_24h` 按**决定事件**计数：只要写入了一条 `outcome='approved'` 的决定，就算一次投票，包括自批票，不管它在执行时是否计入。S-I1 管的是执行时这一票算不算数，告警关心的是“有人第一次投了票”这件事本身，两者口径不同，但互不矛盾；`self_approvals_24h` 也按同一口径计数。

- **投票冷却**由 NX-058 **片 D §2** 实现（读取 role_history 的接口 `(tenant, world, subject, approver_roles)`，§2 第 10 点），不是片 C。片 C 提供的是 role_history 本身。
- **先后**：B1 可以先于片 D 合入。片 D 的 owner 成员类 Action 在 stage 上启用之前，B1 必须已经合入（否则批准人看不到目标）。片 D 的设计应把这一条列为启用前置条件。

## 7. 修订草案 R2：执行期字段、发起时的检查、终结原因与拒绝原因码（2026-10-25，NX-056 B2；NX-058 片 D2/D3 照此对接）

本节是这几项接口唯一的权威定义，待 L6 复核。实现（NX-056 B2）叠在 NX-058 片 D2 的 0344 之后：它也重新定义 `control.nexloop_approval_open`，两条线不能各自改同一个函数，见 §7.6。

### 7.1 执行期字段（调度员定的方向：秘密不进 digest）

- **问题**：`issue_owner_reset` 和 `invite_owner` 的码或 token 由 HTTP 层在每次提交时生成，它的 HMAC 现在放在请求体里，也就进了冻结请求体和 digest。请求人重新提交执行时会生成新的码，digest 对不上，审批永远消费不了；而且秘密的派生值也不应该进冻结请求体。
- **定义**：一个能力可以声明一组**执行期字段**，登记在新表 `control.nexloop_approval_execution_fields(capability primary key, fields text[])` 中。这张表只追加、只有 owner 可写，由该能力的迁移登记（`fields` 是数组，一个能力可以登记多个字段。NX-058 片 D3 登记：`workbench_member.issue_owner_reset` 为 `{code_hmac, issuer_source}`，`workbench_member.invite_owner` 为 `{token_hmac, issuer_source}`。`issuer_source` 是来源地址的 HMAC：同一个人换了设备或网络，重提时它会变，所以也必须是执行期字段）。
- **规则**：
  1. **不进 digest**：`control.nexloop_approval_digest` 在去掉 `request_id` 的同时，去掉该能力的执行期字段（函数由 immutable 改为 stable，因为要读这张表）。gate、`policy_changed` 的旧版本比对、“1 小时内已消费”的查找，以及片 D handler 对“正在执行的审批”的 digest 回查，都经过这个函数，所以自然一致。
  2. **不进冻结请求体**：`control.nexloop_approval_open` 冻结的是去掉执行期字段之后的请求体。执行期字段是**可选的多余键**：挂起的那次提交可以不带，`approval_open` 不要求它们出现，也不校验它们的值。审批视图、“待我执行”的 `inputs`、保留期和擦除看到的都是没有这些字段的请求体。工作台执行时，由 HTTP 层重新生成码，再加上这些字段。
  3. **只在执行事务里用**：执行那一次重新提交时，完整请求体（含执行期字段）照常交给 handler。handler 自己校验这些字段的格式（例如 64 位十六进制的 HMAC）。它们不写入消费记录、审批记录或执行结果，审计也不记它们的值。码本身只出现在这一次 HTTP 响应里（片 D 的 I6）。
  4. **动作认领**（claim）里只有整个请求的 `request_digest`，即完整请求体的哈希，不含字段值；这是受治理入口的既有机制，不受这一节影响。
  5. **第一次提交**（挂起）时生成的码不会被使用：请求挂起后，这个码随之作废，HTTP 层也不返回它。
- **校验**：执行期字段不得同时出现在策略的 `summary_fields`、`target_subject_field` 或条件字段中。策略守护和 Python 校验都拒绝这种组合。

### 7.2 发起时的检查（确认 NX-058 片 D2 的 0344）

- **`operation_mismatch`**：请求体的 `operation` 必须是该能力在 `control.nexloop_governed_capabilities` 中登记的 operation，否则拒绝（22023）。这正是 L6 对 B1 的建议，与 ADR-039 一致（视图也已按审批记录的能力判断类型，§6.4）。
- **目标的成员状态**：invited、active、suspended 可以发起，revoked 拒绝（`target_unresolved`）。§2 第 1 点已同步修改。
- **`quorum_unreachable`**（D8 的一部分）：对声明了 `target_subject_field` 的策略，以及 `solo_mode = not_applicable` 的策略，发起时计算“除请求人和目标本人之外，现在有资格、并且投票冷却已满（`control.nexloop_workbench_vote_eligible_from(tenant, world, subject, approver_roles) <= now()`）的人数”。少于 quorum 就当场拒绝 `approval: quorum_unreachable`（22023，HTTP 409），不挂起等到 24 小时过期。
  - 如果只是冷却未满导致不够，错误的 detail 带 `eligible_from`，即最早能凑够 quorum 的时刻：把除请求人和目标外的各候选人的 `vote_eligible_from` 升序排列，取第 quorum 个；返回 null（当前不持有批准角色）的人不计入。凑不够 quorum 个候选人时不带 `eligible_from`。NX-058 片 D3 的 HTTP 层把它映射成 409，并带上 `eligible_from`。
  - 已有的 `quorum_unreachable_in_solo_mode` 保持不变，只用于单人模式。
- 这几项都在 `control.nexloop_approval_open` 中，由 B2 统一实现（§7.6）。

### 7.3 终结原因：`requester_no_longer_eligible`（D8）

- **新增终结原因** `requester_no_longer_eligible`，与 `due_by_passed`、`policy_changed`、`superseded`、`consumer_erased` 并列：请求人已经不能执行自己的请求了（被降级、停用或撤销），这时他名下仍在等待，或者已批准但尚未消费的请求，立即终结（记为 expired，写 `approval.expired`，payload 的 reason 为这个原因）。
- **入口**（**已被 §8.5 取代**：现行签名的第三个参数是 requester_principal，按 principal 终结，由触发器接线）：新函数 `control.nexloop_approval_end_for_requester(tenant, world, requester_subject, actor_principal, intent)`，只有 owner 函数能调用。
  - 由 NX-058 片 D 的 handler 在降级、停用、撤销成员之后，在同一事务里调用；
  - configurator 的成员 apply（`role_history_sync` 把某个 owner 移出或降级时）同样调用，经同一个 owner 函数，在 configurator 会话中也可以调用（L2 在 D3 中接线）；
  - 跳过正在执行的那条审批（`nexloop.approval_in_flight`）；
  - 遵守加锁顺序：handler 在 gate 之内，已经持有租户锁。
- **为什么不自动按授权判断**：请求人能否执行，取决于他在执行那一刻的授权，受治理入口本来就会拒绝。终结只是为了让这些请求不再占用批准人的注意力和 3 条等待的配额，所以由成员变更的 handler 显式调用，不在每次读取时推断。

### 7.4 执行被拒的原因码（D8）

- 消费结果的形式保持为 `execution_refused:<sqlstate>(:<code>)`（0326 的约束早已允许可选的 code）。
- **code 的来源**：handler 抛出的异常消息，形如 `<前缀>: <code>`，其中 code 匹配 `^[a-z_]{1,64}$`（例如 `workbench member: target_not_active`）。gate 只取这个 code，不取消息的其余部分，也不取任何值。消息不是这个形式时，只记 sqlstate。
- 入口返回 `{"status":"execution_refused","code":<sqlstate>,"reason":<code 或 null>}`，工作台据此显示。
- 这样，片 D 的 handler 只要按这个约定写异常消息（现有的 `workbench member: …` 已经符合），不需要另外的接口。

### 7.5 原因码总表（D8 定案）

| 码 | 出现在 | 含义 |
|---|---|---|
| `quorum_unreachable` | 发起时拒绝 | 除请求人和目标外，有资格并且冷却已满的人数不够 quorum；可能带 `eligible_from` |
| `quorum_unreachable_in_solo_mode` | 发起时拒绝 | 单人模式下，自批永远无法满足这条策略 |
| `operation_mismatch` | 发起时拒绝 | 请求体的 operation 与能力不符 |
| `target_unresolved` | 发起时拒绝 | 目标不是本租户 invited、active 或 suspended 的成员 |
| `requester_no_longer_eligible` | 终结原因（`approval.expired` 的 reason） | 请求人已被降级、停用或撤销 |
| `execution_refused:<sqlstate>:<code>` | 消费结果 | 已按批准执行，handler 拒绝了；code 取自 handler 的异常消息 |

### 7.6 由谁实现（与 L2 的分工建议）

- **NX-056 B2（L3）**：执行期字段表、digest 与冻结请求体的处理、`approval_open` 中的 `quorum_unreachable`、`requester_no_longer_eligible` 及其函数、gate 的原因码，以及相应的测试与复制件守护。
  - B2 叠在片 D2 的 0344 之后：`approval_open` 以 0344 的版本为基础，保留其中的 `operation_mismatch` 和 suspended 放宽，只增加本节的检查。
  - 0344 中这两项改动与本 ADR 一致，L3 确认，不需要 L2 再改。
- **NX-058 片 D3（L2）**：为 `issue_owner_reset` 和 `invite_owner` 登记执行期字段；HTTP 层每次提交都重新生成码，并且只在执行成功的那次响应中返回；handler 降级、停用、撤销成员后，调用 `control.nexloop_approval_end_for_requester`；handler 的异常消息遵守 §7.4 的形式。
- **合入顺序**：片 D2（0344）先于 B2，B2 先于片 D3。

### 7.7 NX-058 片 E（员工擦除，E3）需要的三处配合（预留，片 E 实现时启用）

片 E 设计 v2（`l2/nx058-slice-e-design` c1e3b1d2，§1、§2.4、§5）。片 E 排在 D3 之后；本节先把口径定下，实现随片 E。

1. **revoked 目标只对擦除开放**：`approval_open` 只对 `workbench_member.erase_member` 这一个能力接受 revoked 目标（擦除的前提是这个 subject 的全部 membership 都已 revoked），其余能力仍按 §7.2 拒绝 revoked。B2 在 `approval_open` 中按能力名预留这个分支；`erase_member` 未登记之前，它不会被走到。
2. **对冻结请求体的定点改写**：员工擦除时，经 `invite_owner` 入职者的审批冻结体中，`username` 改写为 `erased-` 开头的假名。
   - 这需要审批请求表的守护为这一类改写开一个例外：只经 owner 函数、在本事务为请求表开启的擦除作用域内，并且只允许把 `username` 这一个字符串整串换成 `erased-` 开头的值。口径同 0331 的 Consumer 假名化（§2 第 5 点）。
   - 改写后请求体的 digest 与记录不再一致，这是有意的：这些审批都已终结。
   - 由片 E 的迁移实现（重定义 0326 的守护，登记复制件守护）。
3. **`erased` 的判定**：
   - §6.5 的 `erased` 改为“员工擦除台账中有这个 subject”（台账由片 E 定义），不再按 `pseudonym:` 前缀推断；
   - §6.1 第 3 步中，`invite_owner` 的 `username` 以 `erased-` 开头时输出 `{"kind":"erased"}`。仍在第 2 步之后：非 owner 一律 `hidden`。
   - 由片 E 的迁移重定义 `runtime.nexloop_approval_target_member`。

## 8. 修订草案 R3：取代 R2 中与片 E 冲突的部分（2026-10-25；L6 联审 R2 与片 E v2）

本节取代 §7 中与之冲突的内容：§7.1 的表结构、§7.2 的目标状态、§7.3 的终结范围、§7.7 的 erased 判定。§7 其余部分仍然有效。B2 按本节实现；片 E 按本节同步。

### 8.1 能力审批设置：执行期字段与可接受的目标状态（C-R1；取代 §7.1 的表与 §7.2 的状态规则）

- **新表** `control.nexloop_approval_capability_settings(capability primary key, execution_fields text[] not null default '{}', target_statuses text[] not null default '{active}')`。它只追加、只有 owner 可写，由能力自己的迁移登记。没有登记的能力，执行期字段为空，可接受的目标状态为 `{active}`。
- **目标状态**：发起时，目标成员的状态必须在 `target_statuses` 之内，否则为 `target_unresolved`。不再全局写死“invited、active 或 suspended，revoked 一律拒绝”。可选值为 `active`、`suspended`、`revoked`（不含 `invited`：还没接受邀请的人不是处置对象；邀请走 `invite_owner`，没有目标成员）。
- **登记**：
  - NX-058 片 D2 的五个能力由 B2 的迁移登记，因为 B2 引入这张表；没有这些登记，B2 一合入，resume_owner 等操作就会被拒（I-R4）：

    | 能力 | `target_statuses` |
    |---|---|
    | `workbench_member.promote_owner` | `{active}` |
    | `workbench_member.demote_owner` | `{active,suspended}` |
    | `workbench_member.suspend_owner` | `{active}` |
    | `workbench_member.resume_owner` | `{suspended}` |
    | `workbench_member.revoke_owner` | `{active,suspended}` |

  - 片 D3 登记 `issue_owner_reset`（`{active}`）和 `invite_owner`（没有目标成员）的执行期字段。
  - 片 E 登记 `erase_member`（`{revoked}`）。
- **为什么由 B2 为 D2 的五个能力登记，以及为什么不含 `invited`**（S-B3）：
  1. 设置表由 B2 引入，而“登记必须先于请求”（§8.3）。D2 的五个能力在 B2 合入时可能已经发布、但还没有请求；如果等 D3 再登记，B2 与 D3 之间这段时间里，`resume_owner` 这类以 suspended 成员为目标的操作会被默认的 `{active}` 拒绝，所以由 B2 在同一个迁移里登记；
  2. `invited` 不在可选状态里：还没有接受邀请的人既没有角色也没有权限，不是处置对象；要撤回一份邀请，走撤销邀请，邀请本身也没有目标成员（`invite_owner` 用 §6.4 的 invitee 变体）。
- **为什么允许 suspended 是安全的**（I-R4）：
  - 被停用的 owner 什么都不持有：没有授权，也不能登录或投票。对他降级或撤销，只会进一步减少权限；
  - 恢复（resume）本身就需要另一位 owner 批准；
  - 只在这三个能力上放开；其他能力，包括首批三项和以后新增的能力，默认只接受 active。

### 8.2 执行期字段只能是 HMAC（I-R1；补充 §7.1）

- 字段名必须匹配 `^[a-z][a-z0-9_]{0,59}_hmac$`。所以片 D3 要把 `issuer_source` 改名为 `issuer_source_hmac`：它本来就是来源地址的 HMAC，改名之后才能登记。
- 值的格式严格校验：请求体中出现这些字段时，值必须是 64 位小写十六进制。`approval_open` 在冻结前校验，不符合就拒绝 `approval: execution_field_invalid`；gate 在执行时统一再校验一次（I-B3）；handler 自己也校验。字段可以不出现（挂起的那次提交可以不带）。
- **gate 只检查请求体中出现了的执行期字段（S-C3）**：字段缺失时 gate 不拒绝，因为挂起和执行都走同一个 gate，而挂起的那次可以不带。执行时字段必须齐全，这一点由 handler 负责：handler 发现缺失就拒绝（消费结果为 `execution_refused`）。

### 8.3 登记先于请求（I-R3）

- 一个能力的设置必须在它有任何审批请求之前登记。设置表的守护会拒绝为已有审批请求的能力登记设置，因为那些请求冻结时没有去掉这些字段，digest 的口径也就不一致了。
- **发布顺序**：
  1. 在同一个迁移里登记能力和它的审批设置；
  2. 发布 Action；
  3. 发布审批策略。
- 要改设置，就为新的能力版本登记新的能力（只追加），不改已有的设置。

### 8.4 执行后的响应丢失（I-R2）

- 请求人执行时，如果响应丢了（网络中断），而执行其实已经成功，那么码或 token 只出现在那次响应里，已经无法找回。
- **同一个 request_id 重试**：受治理入口按动作认领的终态重放，返回 `replayed: true` 和 outcome，不会再执行一次，也不会再生成码。审批已经在第一次执行时消费掉了。
- **处理**：先查询或对账，不盲目重试：
  1. 在“我发起的”中，这条审批的结果是 `executed`；
  2. 撤销那次生成的码或 token（重置码由下一次签发自动作废，或者按 NX-058 的撤销路径处理；邀请撤销邀请）；
  3. 重新发起一次审批。
- 这与 AGENTS.md“结果不明时先查询、对账，不盲目重试”的原则一致。
- **实测：同一个 request_id、带新的 HMAC 重试**（`tests/test_nx056_first_policies_pg.py::test_a_retry_with_the_same_request_id_and_a_new_hmac_is_measured`，L6 对 B2 的复核要求实测）：
  - **既不是重放，也不是新的审批请求**：Python 治理层抛出 `ActionGovernanceError`（“Action reservation conflicts with immutable binding”）。原因是动作认领（claim）的请求摘要覆盖整个请求体，包括 HMAC；同一个幂等键配上不同的请求体，与已保留的绑定冲突。审批层没有第二条请求，也没有任何写入。
  - **工作台 HTTP 层目前没有映射这个错误**，落到兜底分支，返回 **503 `unavailable`**（可重试）。工作台的 HMAC 是服务端每次请求都重新生成的，所以 `issue_owner_reset` 和 `invite_owner` 在响应丢失后，用同一个 Idempotency-Key 重试，一定会走到这里。
  - **交 NX-058 片 D3 选一**（推荐 a）：
    a. 让动作认领的请求摘要也去掉该能力的执行期字段（`goal_controls._submit` 计算 `canonical_request_digest` 时按 `execution_fields_of` 去掉），这样同一个 request_id 的重试按认领终态重放（`replayed: true`，不再生成码），符合上面“同一个 request_id 重放”的口径；
    b. 至少在 HTTP 层把这个冲突映射为 409 `conflict`（不可重试），并提示按本节先对账、撤销后重新审批。
  - 在 D3 落地之前，这两个能力的响应丢失按上面的对账流程处理，不要重试。

### 8.5 终结请求按 principal（C-R2；取代 §7.3 的范围）

- `control.nexloop_approval_end_for_requester(tenant, world, requester_principal, actor, intent)` 按**请求人的 principal** 终结请求，而不是按 subject。同一个人（subject）可能有另一个仍然有效的 owner principal，那个 principal 发起的请求不应被连带终结。`actor` 和 `intent` 写进一条审计事件 `approval.requester_ended`（谁、因为哪次成员变更，终结了几条）。
- **接线（按现状，L6 对 B2 的 C-B1）**：不靠各调用方自己调用，而由 B2 的触发器统一接上：
  - 角色历史中某个角色的 `stop`：片 D2 handler 的降级和撤销、configurator 的成员 apply 都经片 C 写这一条；
  - 成员状态离开 active：停用、撤销。
  - **范围（S-C1）**：任何一个角色写入 `stop`（不论 owner、operator 还是 reviewer，也不论这个角色是否是该请求能力的批准或执行角色），都会终结这个 principal 名下**所有**待处理的请求（等待中，以及已批准尚未消费的）。这是有意从严：角色变化之后，旧的请求一律重新发起，不去逐条判断它是否仍然可以执行。
  - 所以 D2、D3 的 handler 和 configurator 都不需要再调用；正在执行的那条审批（成员 Action 本身）由函数跳过。端到端测试见 `test_a_requester_who_loses_the_role_has_old_approvals_ended_without_any_explicit_call`。

### 8.6 员工擦除台账与 erased 判定（取代 §6.5 的判定与 §7.7 第 3 点）

- **台账**由片 E 定义：单独一张表，长期保留，不借用会在 365 天后删除的 `nexloop_deletion_ledger`。审批视图需要的接口是：按 (tenant, subject) 查询这个 subject 是否已被擦除。
- **erased 的判定**：
  - 目标成员的 subject 在擦除台账中 → `erased`；
  - `invite_owner` 的被邀请人用户名以 `erased-` 开头 → `erased`；
  - 都在 §6.1 第 2 步（非 owner 一律 `hidden`）之后判断。
  - 片 E 合入之前，B1 的 `pseudonym:` 前缀判断作为过渡保留；片 E 的迁移重新定义 `runtime.nexloop_approval_target_member`，改成按台账判断，并去掉这个前缀判断。
- 用户名的假名前缀统一为 `erased-`（取代 §6.5 中“`pseudonym:` 开头”的说法）。

### 8.7 冻结请求体的定点改写（确认 §7.7 第 2 点）

- 只允许员工擦除流程，经 owner 函数、在本事务为请求表开启的擦除作用域内，把 `invite_owner` 请求体中的被邀请人 `username` 这一个字符串整串改写为 `erased-` 开头的假名。其他字段、其他能力一律不许改。
- 改写之后，请求体与 digest 不再一致，这是有意的：这些审批都已终结，digest 只作为审计记录保留。口径同 0331 的 Consumer 假名化。
- 由片 E 的迁移实现（重定义请求表守护，登记复制件守护）。

### 8.8 对片 D2 改动的书面确认，以及 operation 一致性的核对（调度员 2026-10-25）

- **确认**：0344 在 `approval_open` 中加的 `operation_mismatch` 与本 ADR 一致。它对 suspended 目标的放宽由本节 §8.1 的按能力声明取代：B2 以 0344 的 `approval_open` 为基础，把写死的状态列表换成 `target_statuses`，并为 D2 的五个能力登记 §8.1 表中的状态。
- **核对 operation**（防止 `operation_mismatch` 误拒已有能力）：现有接入审批的能力，注册表登记的 operation 与 `goal_controls` 构造请求体时写入的 operation 逐一一致：

  | 能力 | operation |
  |---|---|
  | `real_dispatch.set` | `set_real_dispatch` |
  | `consumer.erase` | `erase_consumer` |
  | `retention.release_hold` | `release_retention_hold` |
  | `consumer.erase_on_request` | `erase_consumer_on_request` |
  | `approvals.solo_mode.set` | `set_approval_solo_mode` |
  | `incentive.issue` | `issue_incentive` |

  更根本的是，受治理入口本身（0124 起）就要求请求体的 operation 等于能力登记的 operation，否则拒绝。所以经入口发起的请求不会被 `operation_mismatch` 误拒；这个检查只会挡住绕过入口、直接构造的调用。

### 8.9 B2 实现说明（L3，叠在 0344 之上，临时迁移 `0345_nx056_execution_fields.sql`）

- 按 §7 与本节实现：
  - `control.nexloop_approval_capability_settings` 及其守护（只许 `*_hmac` 字段；状态只能取 active、suspended、revoked；能力已有请求时拒绝登记）；
  - 两个读取函数 `execution_fields_of` 和 `target_statuses_of`；
  - 为 D2 的五个能力登记目标状态；
  - digest 去掉执行期字段（函数改为 stable）；
  - 策略守护拒绝把执行期字段用作摘要、目标或条件；
  - `approval_open`：按能力的目标状态检查目标、校验执行期字段的值（`execution_field_invalid`）、`quorum_unreachable`（放在单人模式检查之后，所以单人模式下仍然返回更具体的 `quorum_unreachable_in_solo_mode`）、冻结请求体去掉执行期字段；
  - 终结原因 `requester_no_longer_eligible`，以及按 principal 终结的 `end_for_requester`，由角色历史和成员表上的触发器接线（C-B1）；
  - 设置表守护还检查已发布的策略（I-B1），并且 `revoked` 不能与其他状态混用（I-B2）；gate 在执行时统一校验执行期字段的 HMAC 格式（I-B3）；
  - 入口的执行被拒原因码。
- 重定义的函数都登记了复制件守护。
- **动到的 L2 测试**（B2 改变了行为，测试随 B2 一起改）：
  - `tests/test_owner_member_actions_pg.py` 的消费结果，现在带上 handler 的原因码 `:target_role_changed`；
  - `tests/test_nx056_approvals_store_pg.py` 中没有登记设置的能力，现在拒绝 suspended 目标。
- **交给 L2（片 D3、片 E）**：
  - 执行期字段改名为 `issuer_source_hmac`，登记在设置表中（不是 §7.1 原先写的 `execution_fields` 表）；
  - `end_for_requester` 已由触发器接线，D3 不需要调用；
  - 响应丢失后的同 request_id 重试目前返回 503，请按 §8.4 选 a 或 b；
  - `erase_member` 登记 `{revoked}`。
