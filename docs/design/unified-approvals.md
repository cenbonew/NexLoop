# NX-056 统一审批机制（设计稿，第 5 版）

- **状态**：设计稿第 3 版。
  - 第 2 版：按 L6 复核（`unified-approvals-review.md`，针对 611cc8f7）修订，逐条回应见第 14 节；
  - 第 3 版：写入负责人 2026-10-22 的决定（首批 Action、默认期限、**单人模式**，见第 15 节），并处理 L6 第 2 版复核的 N-1、N-2（第 4 节第 3 步）。ADR 编号改为 **ADR-039**（ADR-038 已被正式关系类型占用）。
  - 第 5 版：L6 追加 3 的 T-C1（时间线按**策略**记录批准角色并集的人数）和 T-I1（时间线表只追加、只有 nexloop_owner 可写）。
  - 第 4 版：负责人 2026-10-22 对 S-C1 选修法 **A**（资格人数在过去 24 小时内一直不超过 1）；同时处理 S-I1（关闭单人模式后自批票不再计入）、N-2a（暂时性数据库错误不消费审批），并写死成员计数的口径（第 15 节）。
- **依据**：
  - 负责人 2026-10-22 Q1 选 A：引入 EIOS 通用审批（多人中至少 N 人批准、超时作废、禁止自批），新的高风险动作统一使用；现有外联审批（0240 系列）和候选审批暂时保留，之后再评估是否并入；
  - L7 盘点 `docs/design/eios-capability-gap.md` B1（L7 分支 6885a4fa）；
  - 上游冻结提交 `1e363db` 的实现，按 ADR-006 移植并记录 provenance。
- **基准**：main 1892c9ac。

## 1. 现状

**已 vendored，但没有接上：**

- `eios/approvals/{models,ports}.py`：审批主体、决定、记录、状态 `waiting_approval → approved | rejected | cancelled | expired`；
- `eios/composition/action_approval.py`：把一条审批记录解析成调用级证据；
- `eios/actions/governance.py:671-800`：`approval_mode=REQUIRED` 或风险等级不是 low 的 Action 必须带证据。Python governor 会核对审批主体、quorum 和 `require_distinct_requester`，并用 `assert_fresh` 重查批准人；同时它**认可 `is_policy_auto_approved`**，也就是不需要人类批准也能通过的证据。

**NexLoop 把“无审批”写死在两层：**

- **Python**：
  - `business_actions.py:155`、`catalog_publish.py:77` 把 `approval_mode` 写死为 NONE；
  - `business_actions.py:115` 拒绝风险等级不是 low、或 `approval_mode` 不是 none 的 Action；
  - 运行时用 `UnavailableApprovalPort`：`action_governor.py:13/32`（`create_action_governor`）、`effect_execution.py:218/499`、`conversation_messages.py:131`。
- **SQL**：
  - 受治理入口 `authz.nexloop_goal_governed_action`（0124）要求 `approval_mode='none'`；
  - 效应意图和注册器的入口（0010:18、0015:63、0016:113、0040:137、0041:104）要求 `approval_mode='none'` 并且 `risk_level='low'`。

**已有的两套专用审批：**0240 外联审批，0103 实例候选审批。

**上游还没有搬过来的部分：**

- `approvals/{state_machine,expiry,in_memory}.py`；
- `adapters/postgres/{approvals,approval_human_authority}.py`、`composition/{approval_expiry,approval_reads}.py`；
- 迁移 0009、0014、0015、0023、0073、0089。

上游状态机的语义：一票拒绝即终结；不同批准人的批准数达到 quorum 时通过；同一个人只能批准一次；决定时间不能早于请求时间。

## 2. 关键决定：只走 SQL 路径，并且不替换身份（回应 C1、C2、C4）

**接合点只有一个：SQL 受治理入口 `authz.nexloop_goal_governed_action`。**这个入口会按 `control.nexloop_governed_capabilities` 分发到 human 能力的处理函数，首批要接审批的 Action 全都走这里，例如 `real_dispatch.set`，处理函数是 `control.nexloop_governed_set_real_dispatch`（0153:141）。

**Python governor 和效应路径完全不参与：**

- 目录中的 `approval_mode` **保持 NONE**，风险等级保持 low；
- SQL 的效应门（0010、0015、0016、0040、0041）和 0124 对 `approval_mode='none'` 的要求**一条都不放宽**，所以不存在“对 REQUIRED 的普遍豁免”；
- Python 的 `ApprovalPort` 仍然是 `UnavailableApprovalPort`，所以 governor 那条认可 `is_policy_auto_approved` 的路径**根本不会被触达**。

**是否需要审批，只看审批策略表** `control.nexloop_approval_policies`。受治理入口在验完 permit、权限和身份之后，在调用处理函数之前查这张表。

由此成立的不变式：

1. **不新增身份替换入口**：审批通过之后，**由请求人用自己当前的会话把同一个请求再提交一次**，正常走完受治理入口：签名 permit、`authz.nexloop_assert_action_authority` 重查请求人的现行权限、human 身份检查、请求摘要一致性检查。入口发现有一条“已批准、未消费、摘要相同、策略版本仍有效”的审批，就**一次性消费**它，然后调用处理函数。处理函数始终以**提交者本人**的身份执行，与今天完全相同；不存在任何“代他人调用 handler”的函数。
2. **没有自动批准**：SQL 只有一条批准登记路径，就是 `approvals.decide`，它要求人类会话，并且不能是请求人。没有任何 auto-approve 分支。策略表的约束 `quorum between 1 and 5` 本身也排除了“0 人批准”。这些都用测试锁定。
3. **效应类 Action 不能挂审批策略**：发布策略时校验，目标能力必须是登记在 `control.nexloop_governed_capabilities` 中、`subject_rule='human'` 的能力，否则拒绝。Run 和 Agent 发起的效应（例如 `outreach.send`）不在本期范围，它们的挂起点在效应意图上（第 9 节）。

## 3. 审批策略模型（回应 I2）

审批策略是受信配置：`deploy/configuration/approval-policies.v1.json`，在 SQL 中存成带版本、不可改的行，表为 `control.nexloop_approval_policies`。

| 字段 | 含义 |
|---|---|
| `capability` | 受治理能力名，例如 `real_dispatch.set`，必须是 human 能力 |
| `action_version` | Action 的版本，精确匹配。Action 发布新版本后，没有对应策略的新版本**拒绝执行**（fail closed），直到策略也随之更新 |
| `condition` | 可选，形式只能是“请求体顶层的某个字段等于某个常量”，例如 `{"field": "enabled", "equals": true}`。**不支持**与当前值比较，也不支持按发起来源判断。凡是需要这类条件的需求，一律通过拆分 Action 来表达，以免引入表达式攻击面 |
| `quorum` | 至少需要几位不同的批准人，范围 1–5。不允许 0：单人租户只做事后审计的方案，已在 ADR 中否决 |
| `approver_roles` | 可以批准的工作台角色，批准人必须**现行**持有其中之一 |
| `expires_after_seconds` | 期限，范围 900 秒到 7 天 |
| `summary_fields` | 工作台上可以展示的请求体字段白名单，由服务端渲染 |
| `version` | 策略版本 |

固定不可配置的规则：

- 禁止自批，按 **subject** 比较；
- 一票拒绝即终结；
- 不允许自动批准。

## 4. 流程

1. **请求**：请求人调用一个有审批策略、并且命中条件的 Action：
   - 入口照常验证 permit、权限和 human 身份，然后计算 `request_digest`（第 6 节）；
   - **如果已经有同一个 digest 的 waiting 审批**，直接返回那一条，不新建，以免刷批；
   - 否则写入 `control.nexloop_approval_requests`：冻结的请求体、`request_digest`、请求人的 subject 和 principal（来自服务端身份）、策略版本、`due_by`；
   - 返回 `{approval_id, status: "waiting_approval", due_by}`，**不调用处理函数**，没有任何业务写入或效应意图。
2. **决定**：受治理 Action `approvals.decide`，只能由人执行，在**批准人自己的会话**中：
   - 入口照常重查批准人的现行权限：持有 `approvals.decide`、当前是成员、持有策略中的角色、是人类；
   - 批准人不能是请求人（按 subject 比较），也不能对同一个审批批准两次；
   - 用数据库时钟判断，过了 `due_by` 就拒绝本次决定，并把审批记为 `expired`；
   - 决定只追加一行到 `control.nexloop_approval_decisions`；状态由上游状态机推导（一票拒绝即终结；凑够 quorum 就变成 `approved`）；
   - **决定时只记录，不执行**。
3. **执行（消费）**：请求人在工作台点“执行”，工作台用冻结的请求体和一个新的 `request_id` 重新提交（首次提交的动作认领已终结；摘要不含 `request_id`，所以仍能匹配）（第 8 节）：
   - 受治理入口照常重查**请求人**的现行权限，接着在消费审批之前依次检查：
     - 审批状态为 `approved`，并且**尚未消费**；
     - 重新计算的 `request_digest` 与审批记录一致，策略版本仍是当前版本，否则 `policy_changed`；
     - **重查全部批准人的现行资格**（回应 C3）：每一位投了批准票的人，当前仍是成员、仍持有策略角色、仍是人类、仍不是请求人。不再满足的票不计入；剩下的有效票达不到 quorum 时，审批退回 `waiting_approval` 并记录原因，本次不执行；
     - 租户的控制暂停：如果相关范围处于暂停状态，就拒绝执行，原因是 `control_paused`（回应 I1）；
     - 审批未过期：执行也必须在 `due_by` 之前，过期就不能再消费。
   - 全部通过后：把审批标记为已消费（只能消费一次；消费后 1 小时内，同一请求人再次提交同一请求体，返回 `approval_consumed` 和执行结果；超过 1 小时，再提交视为新的请求），然后调用处理函数。
     - 已批准但已过 `due_by` 的审批在执行时记为 expired，同一请求体随即打开新的请求，不会被卡住；清扫同样终结过期未消费的 approved 审批。
     - 未执行（等待、过期、已消费、`policy_changed`）或被处理函数拒绝时，动作认领的终态记为 `permanent_failure`，不记为 `succeeded`。
     - **并发（L6 N-1）**：消费开始时，对审批行执行 `select … for update`；消费记录表 `control.nexloop_approval_consumptions` 上另有 `unique(approval_id)`。两个标签页同时点“执行”时，第二个事务要么被阻塞，等到它拿到锁时已经看到“已消费”；要么在唯一约束上失败。处理函数最多执行一次。
     - **处理函数抛出异常（L6 N-2，选择“仍算已消费”）**：处理函数的调用放在一个保存点里（`begin … exception when others … end`）。如果处理函数抛出异常，只回滚它自己的写入；“已消费”的标记和执行结果 `execution_refused:<sqlstate>:<固定消息代码>` 照常写入并提交。审批因此不能再被消费，请求人只能重新发起一个新请求。
     - 选这种语义，而不是“抛异常时不消费、改正后可以再执行”，理由是：后一种做法下，同一条批准可以被反复尝试，直到某个请求体或者时机碰巧成功，这让批准的含义变得含糊。
     - **暂时性数据库错误除外（L6 N-2a）**：SQLSTATE 为 40001（序列化失败）、40P01（死锁）、55P03（锁超时）、57014（语句超时）时，异常**原样重新抛出**，整个事务回滚，审批不被消费，请求人可以重试执行。只有业务拒绝才记为 `execution_refused`。
   - 处理函数自身的全部校验照常进行；它产生的效应意图在**派发时**照常重查权限、联系约束（联系约束优先）、real 派发开关、控制和预算（ADR-023、0153、0263）。
   - 如果处理函数拒绝（例如请求体中的 `expected_revision` 已经过期），审批仍然记为**已消费**，执行结果记为 `execution_refused:<code>`，**不会自动重新发起**。请求人只能重新发一个请求，从而得到一个新的审批。
4. **撤回**：请求人可以撤回自己发起、仍在等待中的审批（`approvals.cancel`），审批记为 `cancelled`。

## 5. 审批人推导（回应 C3、C5）

- 批准人只能是浏览器会话中的人类成员，身份由服务端推导：会话 → subject 和 principal → 当前的成员关系和工作台角色（ADR-025、ADR-035）。请求体里的任何身份字段一律忽略。
- 禁止自批：**按 subject 比较**。同一个人换会话、换 principal，仍然算是本人。
- 批准人的资格在两个时点按**现行**状态检查：决定时（在其本人的会话中）和执行消费时（在 SQL 中按当前的成员与角色状态检查，不用提交时的快照）。
- 批准人只能是人：Agent、Run 和服务凭据都不能批准，SQL 中会再检查一次 subject 类型。
- 审批不授予任何权限：执行始终是提交者本人在其现行权限内的一次正常调用，不会超出他的上限（ceiling）。
- **0240 的自批口径**：0240 按 principal 比较（0240:29，`decided_by<>requested_by`；:314）。同一个人用两个 principal 就可以自批。**A1 中同时新增一个迁移**，重定义 0240 的决定函数，改为按 subject 比较，复制件守护会登记这项重定义，并同时补上测试。所以两套审批并存期间不存在这个缺口（回应 C5）。

## 6. 请求摘要（回应 I3）

```
request_digest = sha256(canonical_v1({
  "digest_version": "nexloop-approval-digest/1",
  "tenant_id", "world",
  "action": "eios:action:<name>:<version>",
  "policy": "<capability>@<policy version>",
  "body": 去掉 request_id 之后的请求体
}))
```

- `canonical_v1` 就是现有的 `canonical_payload` 规则：键排序、紧凑分隔符、UTF-8。版本号写进 digest，以后要改规范化规则只能用新版本。
- 请求体只要有任何一处改变，digest 就会改变：既不能消费原来的审批，也不会复用它，只能新建审批。
- 消费是一次性的：每个审批最多被消费一次，消费记录和执行结果一起写入。

## 7. 超时作废（回应 I6）

- `due_by = requested_at + expires_after_seconds`，在请求时冻结。
- 过期处理有三条路径：
  - 决定时，数据库时钟超过 `due_by`，拒绝本次决定并记为 `expired`；
  - 消费时，同样检查，过期就不能执行；
  - 周期清扫：复用 retention-keeper 的定时任务，逻辑沿用上游 `ApprovalExpirySweeper`，**只把 waiting 改为 expired**，绝不触发执行。
- 过期的请求永远不会执行。

## 8. 工作台、审计、隐私

- **观察视图 `approvals`**，分三栏：
  - “待我审批”：我有资格批准、不是我发起、仍在等待中的审批；
  - “我发起的”；
  - “待我执行”：我发起、已批准、尚未消费的审批。
  - 每条显示：Action 的显示名、请求人、`summary_fields` 中各字段的值（由服务端渲染）、digest 的前 12 位、期限、批准数和 quorum。
- **操作**：
  - `approvals.decide`：批准或拒绝，必须填原因；
  - `approvals.cancel`；
  - “执行”：用冻结的请求体重新提交原来的 Action。
- **限流与冷却**（回应 S3）：
  - 每个请求人对同一个能力最多同时有 3 个 waiting 审批；
  - 同一个 digest 在被拒绝或撤回后 1 小时内不能再次发起，以免反复发起来挑选批准人；
  - 被拒的计数写入审计。
- **审计事件**：`approval.requested`、`approval.decided`、`approval.expired`、`approval.cancelled`、`approval.consumed`（含执行结果代码）。效应意图和受治理调用的审计载荷同时记录 actor（请求人）和 `approved_by`（批准人 subject 集合）。审计里只有 ID、代码和 digest，没有请求体正文。
- **冻结的请求体**：只存在 `control.nexloop_approval_requests` 中，受 RLS 保护，并纳入 NX-029 的保留期。涉及 Consumer 的请求，按现有删除传播规则处理（替换为假名，只保留计数）。决定表只追加。
  - 实现（A5）：保留期沿用 `governance_audit` 类（默认 365 天），从请求的 `due_by` 起算，到期后请求体替换为 `{operation, redacted: true}`，ID、digest、状态、决定和消费记录保留，由保留期服务在审批清扫之后执行并留下墓碑；Consumer 擦除完成时（有保全则跳过，同 0307），所有请求体中作为完整 JSON 字符串出现的该 Consumer ID 替换为本次步骤的假名，墓碑记数。两者都只改请求体，并且只能在本事务为这张表开启的擦除作用域内进行。
- **提醒**：新增告警 `approval_waiting`。

## 9. 与现有两套审批的关系（回应 I5）

| 现有机制 | 处理 |
|---|---|
| 0240 外联审批 | **门控一次效应**，挂起点在效应意图上（Run 也会发起）。本期保留不动，只在 A1 中把自批比较改为按 subject（第 5 节）。<br>**第二步合并时的唯一事实来源规则**：<br>• 每个 purpose 在任一时刻只受**一套**机制门控，切换是按 purpose 发布新的 purpose 列表版本时原子完成的；<br>• 切换后，这个 purpose 的新请求只写统一审批；<br>• `outreach_approvals` 中已有的记录**迁移为只读历史**，不再写入，也不从统一记录反向派生。<br>合并约 2 天，另行立项 |
| 0103 实例候选审批 | 这是审核流程中的决定，不门控某一次调用，**不并入**。最多在工作台上合并成一个“待处理”入口 |

## 10. 首批接入（建议，交负责人决定）

| 建议 | 能力 | 条件 | quorum | 说明 |
|---|---|---|---|---|
| ✔ 首批 | `real_dispatch.set` | `enabled = true`（只在打开时需要审批；关闭不需要，必须能立即生效） | 1 | 对外影响最大的一步 |
| ✔ 首批 | `consumer.erase`（工作台 owner 发起的擦除，0132:358） | 无 | 1 | 不可逆。顾客本人请求的擦除走的是另一条入口（source `consumer_request`），不经过这个能力，所以不受影响。实现时会核实；如果两者共用同一个能力，就先拆出 `consumer.erase_on_request` |

**A5 核实结果**：负责人确认顾客本人请求时，原来也走 `consumer.erase`（同一个处理函数，`confirm_request` 字段被忽略），请求体无法区分两者。因此已拆出 `consumer.erase_on_request`（只能确认该顾客处于 `pending_confirmation` 的本人请求，否则拒绝），它不挂审批策略；`consumer.erase` 的策略只拦负责人发起的擦除（以及仍经由它的确认）。
| ✔ 首批 | `retention.release_hold` | 无 | 1 | 解除保全之后数据会被清理 |
| 移出首批 | `goals.budget.set` | — | — | “只在提高上限时”需要与当前值比较，第 3 节不支持；以后如有需要，就拆分出 `goals.budget.raise` |
| 可选 | `semantic_release.publish`、`strategy.publish` | 无 | 1 | 已有评估门 |

**负责人决定（2026-10-22）：**

- **首批**就是上表中标“✔ 首批”的三项：`real_dispatch.set`（只在 `enabled=true` 时）、`consumer.erase`（工作台发起）、`retention.release_hold`。quorum 都是 1，禁止自批。
- `goals.budget.set` 移出首批；`semantic_release.publish` 和 `strategy.publish` 暂时不加。
- **默认期限 24 小时**。
- **单人租户**：负责人新增**单人模式**，“公司就一个人在用这个系统时，允许管理员开启不需要第二个人审批，自己批就行了”。设计见第 15 节。“quorum 为 0”的方案仍然否决：单人模式放宽的只是“禁止自批”这一条，审批本身并没有被跳过。

## 11. ADR-039 草案

```
# ADR-039｜统一 Action 审批（人类受治理能力）

状态：草案（负责人 2026-10-22 Q1 选 A）

决定：
1. 需要审批的范围由受信配置中的审批策略决定，只能针对 human 受治理能力。
   策略包括：Action 版本、可选的单字段等值条件、quorum（1–5）、批准角色、期限、摘要字段白名单。
   禁止自批（按 subject 比较），一票拒绝即终结，超期作废，不允许自动批准，quorum 不能为 0。
2. 审批只决定“这次调用能否执行”，不授予任何权限。
   执行时由请求人以自己当前的会话再提交一次同一请求，正常走完受治理入口，然后一次性消费已通过的审批。
   执行时会重查请求人和全部批准人的现行资格、策略版本和控制暂停；之后效应派发时的各项重查照常进行。
   系统中不存在“代他人执行”的函数。
3. 接合点只在 SQL 受治理入口。目录中的 approval_mode 保持 none；
   效应门、Python governor 和 ApprovalPort 都不改，也不放宽。
4. 上游的状态机和到期逻辑按 ADR-006 vendored；持久化层按 NexLoop 的 authz 签名、world、RLS 和只追加规则重写。
5. 0240 外联审批暂时保留，自批比较先改为按 subject；之后作为效应级审批按 purpose 切换并入。
   0103 候选审批属于审核流程，不并入。
6. 单人模式（负责人 2026-10-22）：owner 经受治理 Action 开启或关闭，必须写理由，默认关闭。
   只有在该租户该 world 中有审批资格的 active 人类成员当前恰好 1 人，并且过去 24 小时内一直不超过 1 人时才生效；
   人数按可审计的时间线和服务端时间，在每次批准和每次消费时实时计算。关闭单人模式后，已有的自批票不再计入。
   它只放宽“禁止自批”这一条：自批仍然分“申请”和“批准”两步，必须写理由，记录上标明 self_approved；
   quorum 大于 1 的策略在单人模式下不可满足，直接报错，不降级。

备选方案与否决理由：
- 批准人在决定时直接执行：执行身份会变成批准人，或者需要一个替换身份的函数，形成提权面（L6 C2）。
- 由异步服务主体执行：需要一个能代表任意请求人执行的服务凭据，同样是提权面；而且请求人的现行权限难以等价重查。
- 先统一到效应级挂起：首批都是不产生效应意图的 human 控制，放在效应级做不到；留到合并 0240 时再做。
- 通过 Python governor 和 ApprovalEvidence：首批的能力不经过 governor；接上之后，会连带要求放宽效应门对 REQUIRED 的拒绝，
  并且引入 governor 对 policy 自动批准的认可。

风险：
- 审批流程本身成为社会工程或刷批的入口：用限流、冷却，以及不同批准人的现行资格重查来缓解。
- 审计里没有正文，复核困难：工作台只显示服务端渲染的白名单字段和 digest。
- 单人租户无法执行需要审批的 Action：这是禁止自批的本意。
- Action 发布新版本后，没有对应策略版本时拒绝执行（fail closed），所以发布流程中必须同步更新策略。

- 单人模式被用来绕开第二个人：只有 owner 能开启；资格人数必须**在过去 24 小时内一直不超过 1**（负责人 2026-10-22 选 S-C1 修法 A），
  所以“先移除另一人的批准资格、再开启单人模式自批”至少要等 24 小时，期间被移除的人可以发现；只要出现第二个有资格的成员就自动失效；
  关闭单人模式后自批票不再计入；工作台常驻提示；开启、关闭、每次自批、批准角色的移除都有审计和告警，并且可以单独筛出。
- **修法 A 的残余风险**：如果 owner 账户被盗用，而租户本来就长期只有一人（满足 24 小时条件），攻击者可以开启单人模式并自批。
  这时审批只能留下审计和告警，不能阻止；双人控制在单人租户中本来就不存在，这是负责人知情接受的取舍。

后果：请求人在审批通过后需要回来点一次“执行”。审批不会自动产生任何写入。
```

## 12. 测试清单（回应 S5）

- 自批被拒：同一个 subject 用另一个 principal 自批，统一审批和 0240 各测一次；
- quorum 为 2 时，一个人重复批准被拒；
- 一票拒绝即终结；
- 期限：决定时过期、消费时过期、清扫只做终结而不执行；
- 请求体或 digest 改变后不能消费；策略版本改变后执行被拒；
- 批准人在执行前被撤销角色或移除：这一票不再计入，审批退回 waiting；
- 请求人在执行前失去权限：正常入口拒绝，审批不被消费；
- 已通过的审批再次消费被拒（`approval_consumed`）；
- 两个事务并发消费同一条审批：处理函数只执行一次（L6 N-1）；
- 处理函数抛出异常：审批仍算已消费，结果记为 `execution_refused:<sqlstate>`，再次提交时返回 `approval_consumed`（L6 N-2）；
- 处理函数遇到暂时性错误（例如锁超时）：事务回滚，审批不被消费，可以重试执行（L6 N-2a）；
- 处理函数拒绝时记为 `execution_refused`，并且不会自动重新发起；
- 控制暂停时执行被拒；
- 不存在自动批准：没有任何 SQL 路径能在没有人类决定的情况下得到 approved，quorum 为 0 的策略被拒；
- 效应类能力不能挂策略；
- 同一个 digest 在 waiting 时重复发起，返回同一条审批；冷却期内再次发起被拒；
- 派发重查不被绕过：在 real 派发开关关闭的情况下，审批通过并执行之后，效应仍然被开关拒绝；
- 删除传播：Consumer 被擦除后，审批请求中的 consumer_id 替换为假名。

## 13. 实现切片与工作量（回应 S4）

| 切片 | 内容 | 估计 |
|---|---|---|
| A0 | vendored `state_machine.py`、`expiry.py` 和上游测试，记录 provenance | 0.5 天 |
| A1 | 迁移：策略、请求、决定、消费四张表，RLS，只追加；`approvals.decide`、`approvals.cancel` 两个能力和审计；0240 自批比较改为按 subject（新迁移，登记复制件守护） | 2 天 |
| A2 | 重定义受治理入口 `authz.nexloop_goal_governed_action`（0124，登记复制件守护）：策略匹配、挂起、同 digest 去重、消费时的全部重查、一次性消费、执行结果；限流与冷却 | 2.5–3 天 |
| A3 | 过期清扫和告警 `approval_waiting` | 0.5 天 |
| A4 | 工作台：三栏观察视图、批准、拒绝、撤回、执行 | 1.5 天 |
| A5 | 首批三个能力的策略、第 12 节的测试、保留期与删除传播 | 1.5 天 |
| A6 | ADR、运行手册；在 stage 上启用策略之前，先决定用单人模式（由 owner 开启）还是准备第二个测试成员 | 0.5 天 |
| A7 | 单人模式（第 15 节）：策略属性 `solo_mode`（`allowed` 默认 / `not_applicable`）；设置表和受治理能力 `approvals.solo_mode.set`；资格人数时间线（两处触发器、初始记录、advisory lock）；在决定和消费时实时判断（当前人数、24 小时窗口）；`self_approved` 标记，关闭后不计入；工作台常驻提示；告警；测试 | 1.5 天 |

合计约 **10.5–11 天**：第 2 版是 9–9.5 天，单人模式（含 24 小时资格人数时间线）1.5 天。A1 和 A2 改动的是受治理入口这条公共路径，需要 L6 深审。把 0240 并入统一审批另计约 2 天。

## 14. 对 L6 复核（611cc8f7）的逐条回应

| 条目 | 回应 |
|---|---|
| 一（遗漏） | 第 1 节补上了 SQL 层写死 none 的位置（0010、0015、0016、0040、0041、0124）。第 2 节决定这些门**都不改**：目录中的 `approval_mode` 保持 none，需要审批与否只由策略表决定 |
| C1 | 只走 SQL 受治理入口（第 2 节）；Python governor、ApprovalPort 和效应门都不参与，也不放宽，所以没有普遍豁免 |
| C2 | 取消“以请求人身份执行”的设计。改为请求人用自己的会话再提交一次，消费审批后执行（第 2、4 节），系统中不存在身份替换函数；审计同时记录 actor 和 `approved_by`（第 8 节） |
| C3 | 消费时重查全部批准人的现行资格，无效票不计入，达不到 quorum 时审批退回 waiting（第 4.3、5 节）；决定时在批准人本人的会话中重查 |
| C4 | 不允许自动批准：SQL 没有这条路径，quorum 不能为 0，governor 路径根本不会被触达，并用测试锁定（第 2 节第 2 条、第 12 节） |
| C5 | A1 中新增迁移，把 0240 的自批比较改为按 subject，并补测试（第 5 节） |
| I1 | 消费时重查策略版本和控制暂停（第 4.3 节）；Action 发布新版本后，没有对应策略时拒绝执行（第 3 节） |
| I2 | 条件只支持单字段等值；`goals.budget.set` 移出首批，以后用拆分 Action 表达；`consumer.erase` 只针对工作台 owner 发起的能力，顾客本人请求的擦除走另一条入口，实现时核实，必要时拆分（第 3、10 节） |
| I3 | digest 写入算法版本，绑定 Action 版本和策略版本；同 digest 在 waiting 时去重；执行被拒后审批仍算已消费，不会自动重新发起；只能消费一次（第 4、6 节） |
| I4 | 第二个测试成员必须在启用策略之前就位（第 10、13 节）；“quorum 为 0”已在 ADR 中否决 |
| I5 | 写明了按 purpose 原子切换、旧记录迁移为只读历史的规则（第 9 节） |
| I6 | 清扫只做 waiting→expired，绝不执行；决定和消费都以数据库时钟为准（第 7 节） |
| S1 | ADR 草案补上了“备选方案与否决理由”和“风险”两段（第 11 节） |
| S2 | 现状清单补上了 `action_governor.py`、`conversation_messages.py` 和 SQL 层的硬约束（第 1 节） |
| S3 | 限流与冷却：每个请求人对同一能力最多 3 个 waiting 审批；被拒绝或撤回后，同一 digest 冷却 1 小时（第 8 节） |
| S4 | A2 调整为 2.5–3 天，A1 调整为 2 天，合计约 9–9.5 天（第 13 节） |
| S5 | 测试清单补齐（第 12 节） |

## 15. 单人模式（self-approval，负责人 2026-10-22）

负责人的原话：“公司就一个人在用这个系统时，允许管理员开启不需要第二个人审批，自己批就行了。”下面按调度员建议的五条护栏设计；对不妥的地方，在最后一小节给出替代方案。

**1. 开启和关闭**
- 只有 owner 能开启或关闭，经受治理能力 `approvals.solo_mode.set`。它本身是 human 能力，不挂审批策略，否则单人租户永远无法开启。
- 必须写理由，审计留痕。
- 默认关闭。
- 设置存成按 (tenant, world) 只追加的事件：`control.nexloop_approval_solo_mode_events`，最新一行决定当前状态。

**2. 生效条件，实时判断（负责人 2026-10-22 选 S-C1 修法 A）**
- 单人模式只有同时满足三点时才生效：
  - 已经开启；
  - 在该租户该 world 中，有资格的人类成员**当前恰好 1 人**，并且这个人就是请求人；
  - **过去 24 小时内，有资格的人数一直不超过 1**。窗口内任何时刻人数超过 1，都会让窗口从那一刻重新开始计算。
- **为什么要看历史**：如果只看当前人数，owner 可以先去掉另一位成员的批准角色，或者停用他，把人数降到 1，再开启单人模式自批，然后恢复对方的角色。这样，一个有两个人的公司里，双人控制就被一个 owner 会话绕过了（L6 S-C1）。有了 24 小时的历史条件，这么做之后要等满 24 小时才能自批，而这期间被移除的人有机会发现。
- **资格人数的口径**（写死）：
  - 成员状态 `control.nexloop_browser_memberships.payload.status='active'`；
  - subject 必须是人类：服务主体、Agent、Run 凭据都排除；
  - 在**最新一版**工作台配置（`control.nexloop_workbench_configurations` 的 `members`）中，持有该策略任一批准角色；
  - **按 subject 去重**：一个人有两个 principal 只算一人，与禁止自批的口径一致；
  - 只计算该租户该 world，不跨 world。
  - 成员的有效期（`valid_from`、`valid_until`）**不扣减人数**：将来才生效、或者已经过期但仍是 active 的成员都计入。这样只会让人数偏大，也就是让单人模式更难生效，是安全的方向；而且时间到点生效这类变化不会产生写入事件，不计入反而会漏记。
- **资格人数时间线按策略记录（L6 T-C1）**：只追加的表 `control.nexloop_approval_eligibility_events`，每行记录 (tenant, world, **策略能力与策略版本**, 资格人数, 资格 subject 集合的摘要, 记录时间)。
  - 资格人数是该策略**全部批准角色的并集**中、按 subject 去重之后的人数，与判定口径完全一致。如果按每个角色分别计数，那么当策略有两个批准角色、A 持有其中一个、B 持有另一个时，每个角色都只有 1 人，单人模式就会被误判为生效，A 可以自批。
  - 资格 subject 集合的摘要是 sha256(排序后的 subject 列表)。它只用于审计：人数不变但换了人，也能在时间线上看出来。
  - 写入点是同一个数据库里的触发器：
    - `control.nexloop_browser_memberships` 插入、更新、删除时；
    - `control.nexloop_workbench_configurations` 插入新版本时；
    - `control.nexloop_approval_policies` 插入新的策略版本时（批准角色可能变了）。
  - 任何一次变更，都会重新计算受影响租户**所有**相关策略的并集人数，只有人数或集合摘要发生变化时才追加一行；记录时间取服务端时钟 `clock_timestamp()`。
  - **只追加（L6 T-I1）**：表属于 `nexloop_owner`，启用并强制 RLS；只有触发器使用的 owner 函数能 INSERT；任何角色（包括 owner 自己）都不能 UPDATE、DELETE、TRUNCATE，由触发器拒绝，沿用现有只追加表的写法；应用角色只能经由只读函数读取。
  - 部署时为每个租户写一行初始记录。在此之前没有时间线，所以单人模式最早在部署 24 小时之后才能生效（时间线必须完整覆盖整个窗口；覆盖不全时按“不生效”处理）。
- **判定**：在每次自批的决定时和每次消费时，**按该策略**（批准角色并集），用服务端时间实时计算以下三项，不使用快照：
  - 当前人数（按上面的口径重新计算）；
  - 窗口起点之前最近一条记录的人数；
  - 窗口内全部记录的最大人数。
  三项都不超过 1，单人模式才算生效。
- **竞态**：成员变更事务和消费事务对同一个租户使用同一把 advisory lock，所以不存在“第二个人的加入还没提交、消费已经计完人数”的瞬间窗口。
- 一旦出现第二个有资格的成员，自批就立即失效，不需要人工关闭：
  - 已经挂起、还没有批准的请求，需要由他人批准；
  - 已经自批、还没有消费的审批，在消费时那一票不再计入，审批退回 waiting，需要他人批准。
- **关闭单人模式之后同样如此（L6 S-I1）**：带 `self_approved=true` 的票，只有在**消费时**单人模式仍然生效（已开启，并且满足上面三点）时才计入。否则这一票不计入，审批退回 waiting。

**3. 自批仍然是完整的审批**
- 申请和批准分两步，并没有跳过审批：请求人先提交，再在“待我审批”里自己批准，批准时必须写理由。
- 决定记录上标 `self_approved=true`；执行时同样一次性消费，同样重查现行资格。
- 审计事件 `approval.decided` 带 `self_approved`，工作台和导出都可以单独筛出全部自批。

**4. 只放宽“禁止自批”这一条**
- quorum、期限、一票拒绝、限流与冷却、控制暂停、消费时的各项重查，以及派发时的权限和联系约束重查，**全部不变**。
- quorum 大于 1 的策略在单人模式下**不可满足**：发起这类请求时直接返回 `quorum_unreachable_in_solo_mode`，不降级成 1。
- 单人模式不改变谁有资格批准，只是允许这唯一一位有资格的人批准自己的请求。

**5. 工作台提示**
- 单人模式开启期间，工作台顶部常驻提示“单人模式已开启：审批可由本人批准”。
- 如果开启了单人模式，但有资格的成员已经多于 1 人，提示改为“单人模式未生效：已有其他成员可审批”。

**护栏的补充与替代**

- **补充：告警。** 新增告警 `approval_solo_mode`：单人模式开启时，以及发生自批时，各提醒一次，写入工作台的告警列表。理由：owner 账户一旦被盗用，攻击者可以先开启单人模式再自批；告警能让人事后发现这个序列。
- **补充：告警也覆盖批准角色的移除。** “批准角色被移除”和“拥有批准角色的成员被停用”也纳入 `approval_solo_mode` 告警和审计筛选视图，便于事后把整个序列串起来。
- **不再需要单独的开启冷却期**：24 小时历史条件已经覆盖了“盗用会话后立即开启并自批”的情况。
- **不采用：按租户而不是按 world 判断人数。** 成员和角色本来就按 (tenant, world) 管理，单人模式也按 (tenant, world) 判断，口径一致。

**测试**
- 单人租户在时间线满 24 小时并开启单人模式后，可以自批；记录上 `self_approved=true`，执行时正常消费；
- **S-C1**：先去掉另一人的批准角色，再开启单人模式并立即自批 → 被拒；满 24 小时后 → 可以自批；
- **S-C1**：窗口内第二人出现后又被移除 → 需要从移除之时起再等 24 小时；
- **S-I1**：自批之后关闭单人模式，执行时审批退回 waiting；
- 时间线不完整（部署不满 24 小时）时，单人模式不生效；
- **T-C1**：策略有两个批准角色，A 持有一个、B 持有另一个 → 并集人数为 2，单人模式不生效，A 不能自批；任一批准角色的变更都会触发该策略重新计数；
- **T-I1**：时间线表只追加：应用角色和 owner 都不能 UPDATE、DELETE、TRUNCATE，应用角色不能直接 INSERT；
- 第二个有资格的成员加入后：
  - 新的自批被拒；
  - 已经自批、未消费的审批，在消费时退回 waiting；
- 关闭单人模式后，自批立即被拒；
- 非 owner 不能开启或关闭单人模式；
- 在单人模式下，quorum 为 2 的策略返回 `quorum_unreachable_in_solo_mode`；
- 审计和导出可以筛出全部自批；
- 单人模式下照常生效：一票拒绝、期限、控制暂停，以及派发时的重查（real 派发开关关闭时，效应仍然被拒）。

- **补充：策略属性 `solo_mode`（L6 复核 NX-058 时提出）。** 每条审批策略带属性 `solo_mode`，取值 `allowed`（默认）或 `not_applicable`。
  - `not_applicable` 的策略在单人模式下**也不能自批**：自批票不计入，quorum 达不到时返回 `quorum_unreachable_in_solo_mode`，只能等第二个合格成员加入后再批。
  - 适用于会扩大或转移 owner 权力的动作，例如邀请或提升 owner、对 owner 签发重置码（NX-058 接入时设置）。
  - 这个属性在 A7 中随单人模式一起实现（策略表加一列，A1 的策略插入守护同时校验取值）；首批三项策略取 `allowed`。

## 16. 补充（L6 追加 7；调度员 2026-10-23）

- **加锁顺序**：凡是同时涉及成员变更和审批的事务，一律**先取租户级 advisory lock**（与第 15 节同一把，按租户、不按 world），**再锁成员行或审批行**（`select … for update`）。决定、消费、成员变更、单人模式设置都按这个顺序，避免两个事务交叉等待。A7 实现时把这一点写进每个函数的注释，并加一条并发测试。
- **投票资格起点（NX-058 N-1 口径）**：一票是否计入，除了现行资格（第 5 节），还要看批准人**首次进入批准角色的时间**。
  - 数据来源是 NX-058 的 `control.nexloop_workbench_role_history`（长期保留）。不用成员的 `valid_from`，因为它可以被回填。
  - 首次进入批准角色的时间**跨策略版本**计算：策略升版本不重新计时。
  - 离开批准角色后再加入，**重新计时**。
  - NX-056 的资格时间线（第 15 节）仍然只记人数和 subject 集合摘要，不为此加字段。
  - **接口约定**：NX-056 读取 `role_history` 中某个 subject 在某个 **(tenant, world)** 下当前这段连续持有批准角色（策略 `approver_roles` 中任一）的起点。参数为 `(tenant, world, subject, approver_roles)`，返回起点时间，从未持有或当前不持有时返回 null。与 role_history 的 (tenant, world) 划分一致（L6：原文缺 world）。NX-058 片 C 落地后对接。在此之前不启用投票冷却，现行资格检查不变。
- **策略条件语法不扩展**：只支持“请求体字段 = 常量”。需要“与当前值比较”的场景，按 NX-058 的做法拆出专用 Action，由服务端在执行时重查当前状态；如果确实需要扩展条件语法，先交调度员，不直接做。
- **summary_fields 不得包含 Consumer 标识或自由文本**：所有有资格的批准人都看得到摘要。Python 校验和 0321 的策略守护都会拒绝名字里带 consumer、principal、subject、customer、identity，或 reason、basis、text、body、message、note、description、quote、comment、name、email、phone 的字段。批准人靠 digest 和这些字段判断，自由文本留在请求人自己的“我发起的”记录里。
- **执行入口**：工作台只能执行它自己能构造的请求体。也就是说，操作必须在工作台的 OPERATIONS 中，而且冻结请求体的键不能超出这个操作的字段。不满足时，视图返回 `inputs: null`，页面提示改用发起时的入口执行，不会出现反复 422 的情况。例如 `retention.release_hold` 不是工作台操作，它的请求从受治理 API 客户端发起，执行也在那里完成（同一请求体，新的 request_id）。
- **擦除和保留期改写请求体**：两者都只能在本事务为请求表开启的擦除作用域内进行，并且只改请求体。0321 的守护先判断这条路径，再检查“已消费不可改”，所以已消费的行同样可以被假名化或删去正文。

- **N-1 补充（调度员 2026-10-23）**：消费事务也先取租户 advisory lock，再对审批行执行 `select … for update`，与上面的加锁顺序一致（A7 随单人模式一起加上这把锁；在此之前，行锁加消费表主键已能保证只执行一次）。
- **目标成员不参与批准（NX-058 Z1）**：审批的对象是某位成员时（例如对 owner 降级、停用、撤销、签发重置码、恢复），这位成员不能参与批准。
  - 策略可以声明 `target_subject_field`：请求体中指向目标成员的字段，取值可以是该租户的 principal，也可以是 subject ID。按 subject 比较，同一个人用另一个 principal 也一样。
  - 决定时拒收这位成员的票（`target_cannot_approve`）；消费时重新计票也不算他；工作台“待我审批”里不出现这条请求，批准数也不算他的票。不限于三位及以上 owner 的情形。
  - 首批三项策略都不涉及成员，不设这个字段；NX-058 的成员类 Action 接入时设置。

## 17. 补充（L6 追加 8）

- **发布与来源**：发布策略用 `scripts/deploy/approval_policies_apply.py`（带 `--check`、`--tenant`、`--bootstrap-dsn-file`），发送文件原文。SQL 记录清单版本、原文 sha256 和 `expected_revision`，与 0050 的做法一致。步骤见 `docs/implementation/approval-policies.md`。
- **summary_fields 改为按能力设白名单**（`control.nexloop_approval_summary_allowed`），取代第 16 节的禁止名单（禁止名单仍保留，作为第二道检查）。白名单之外的字段（例如 `scope_ref`、联系方式、地址）一律拒绝，不在白名单中的能力不显示任何摘要。SQL 同时拒绝重复的批准角色。
- **擦除分支收窄**：只有 owner 的函数可以改冻结请求体，而且只允许两种形式：整体删去正文（`{operation, redacted: true}`），或者结构不变、只把某些完整字符串值替换为 `pseudonym:<32 位十六进制>`。
- **Z1 fail closed**：策略声明了 `target_subject_field` 时，发起审批时这个字段必须指向本租户的一位成员（principal 或 subject 均可），否则以 `target_unresolved` 拒绝。
- **顾客擦除开始时**：这位顾客仍在等待或已批准、尚未消费的审批（请求体中出现其 ID 的）直接终结，记为 expired，原因 `consumer_erased`。正在执行的那条审批除外：例如经批准执行的 `consumer.erase` 本身，由 `nexloop.approval_in_flight` 标记。擦除完成时再按第 16 节把请求体中的 ID 替换为假名。
- **顾客请求入口接入 `erase_on_request`** 不在本任务内，记为待接（实施文档第 3 节）。
