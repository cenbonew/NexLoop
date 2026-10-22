# NX-056 统一审批机制（设计稿）

- **状态**：设计稿，待 L6 复核，复核后再实现。
- **依据**：
  - 负责人 2026-10-22 Q1 选 A：引入 EIOS 通用审批（多人中至少 N 人批准、超时作废、禁止自批），新的高风险动作统一使用；现有外联审批（0240 系列）和候选审批暂时保留，之后再评估是否并入。
  - L7 盘点 `docs/design/eios-capability-gap.md` B1（L7 分支 6885a4fa）。
  - 上游冻结提交 `1e363db` 的实现，按 ADR-006 移植并记录 provenance。
- **基准**：main 1892c9ac。

## 1. 现状

**已 vendored、但没有接上：**

- `eios/approvals/{models,ports}.py`：审批主体 `ApprovalSubject`（`request_digest`、`requester_id`、`quorum`、`eligible_approver_scopes`、`require_distinct_requester`、`due_by`）、决定、记录和状态（`waiting_approval → approved | rejected | cancelled | expired`）；
- `eios/composition/action_approval.py`：把一条已终结的审批记录解析成调用级的 `ApprovalEvidence`；
- `eios/actions/governance.py`（`_validate_approval_evidence`）：`approval_mode=REQUIRED` 或风险等级不是 low 的 Action，必须带审批证据，Action governor 会对着存储再校验一遍。

**NexLoop 的实际情况：**

- `approval_mode` 被写死为 `NONE`（`business_actions.py`、`catalog_publish.py`）；
- 运行时用的是 `UnavailableApprovalPort`（`effect_execution.py`）；
- 每个功能各自实现一套审批：0240 外联审批，以及 0103 实例候选审批。

**上游还没有搬过来的部分：**

- `approvals/{state_machine,expiry,in_memory}.py`：纯逻辑，没有领域代码；
- `adapters/postgres/approvals.py` 和 `approval_human_authority.py`；
- `composition/{approval_expiry,approval_reads}.py`；
- 迁移 0009、0014、0015、0023、0073、0089 等，其中有 `control.approvals` 表。

上游的状态机语义（`state_machine.apply_decision`）：

- 一票拒绝就直接终结；
- 不同批准人的批准数达到 quorum 时通过；
- 同一个人只能批准一次；
- 决定时间不能早于请求时间。

## 2. 移植范围（ADR-006）

| 上游 | 处理 |
|---|---|
| `approvals/state_machine.py`、`approvals/expiry.py` | **原样 vendored**，记录 provenance；上游的相关单测搬进 `tests/upstream/`，并记入 `UPSTREAM_*_TESTS.json` |
| `approvals/in_memory.py` | 只给测试用（参考实现），不进运行路径 |
| `adapters/postgres/approvals.py`、`approval_human_authority.py` 和上游迁移 | **不搬代码，按 NexLoop 的规则重写**：authz 签名入口、`world` 列、RLS、只追加、`nexloop_owner` 拥有、应用角色只拿 execute。只借用表结构和状态迁移的语义 |
| `composition/approval_reads.py` | 参照它来实现工作台的只读视图 |

## 3. 审批策略模型

审批策略是**受信配置**，随发布版本下发，不在运行时由租户自行修改：

- 配置文件：`deploy/configuration/approval-policies.v1.json`；
- 在 SQL 中存成带版本、不可改的行：`control.nexloop_approval_policies`。

每条策略：

| 字段 | 含义 |
|---|---|
| `action` | `eios:action:<name>:<version>`，精确到版本 |
| `condition` | 可选，只在请求体满足条件时才需要审批，例如 `real_dispatch.set` 只在 `enabled=true` 时需要。只允许“某个顶层字段等于某个常量”，不允许任意表达式 |
| `quorum` | 至少需要几位**不同的**批准人，范围 1–5 |
| `approver_roles` | 哪些工作台角色可以批准，例如 `owner`、`admin`。批准人必须**当前**还持有这个角色 |
| `expires_after_seconds` | 从请求起算的期限，范围 15 分钟到 7 天；过期即作废 |
| `risk_level` | 写回 Action 定义的 `risk_level`（medium 或 high），同时 `approval_mode=REQUIRED` |

固定不可配置的规则：

- 请求人不能批准自己的请求（`require_distinct_requester` 恒为 true），按服务端推导出的 subject 判断；
- 一票拒绝即终结。

策略一旦变化，就成为一个新版本。审批主体会冻结请求那一刻的策略版本，之后修改策略不会影响已经发起的审批。

## 4. 与受治理 Action 的接合点

流程：Action 声明需要审批 → 请求挂起 → 审批通过后，**同一个请求**继续执行 → 执行时照常重查一切。

1. **请求**：工作台通过受治理入口（`control.nexloop_governed_<op>`）调用一个需要审批的 Action，并且命中策略条件：
   - 服务端计算 `request_digest`：`sha256(canonical({tenant, world, action 引用, 规范化请求体}))`；请求体去掉 `request_id`，`operation` 保留；
   - 写入 `control.nexloop_approval_requests`：冻结的请求体、`request_digest`、请求人（服务端身份，不取请求体里的任何值）、策略版本、`due_by`；
   - 返回 202：`{approval_id, status: waiting_approval, due_by}`。**此时什么都不执行**，不产生任何业务写入或效应意图。
   - 同一个 `request_id` 重放时返回同一个审批；同一个 `request_id` 换了请求体，按现有规则判为 `request_reused`。
2. **决定**：受治理 Action `approvals.decide`（只能由人执行）：
   - 批准人必须是浏览器会话中的人类成员，**当前**持有策略中的某个角色，不是请求人，并且没有批准过这个审批；
   - 用数据库时钟判断，过了 `due_by` 一律拒绝，并把审批记为 `expired`；
   - 决定只追加一行（`control.nexloop_approval_decisions`），审批的状态由状态机推导。
3. **继续执行**：促成 quorum 的那一次批准，**在同一个事务里**执行冻结的请求：
   - 先重新校验**请求人**：仍然是有效成员，**当前**仍然持有这个 Action（工作台角色）；租户和 world 仍然一致。批准人的权限只用来批准，不会借给执行；
   - 再用冻结请求体重新计算 `request_digest`，必须与审批主体一致。请求体以任何方式变化都会使审批失效；
   - 然后按请求人的身份调用 Action 原来的处理函数。这一步只改变“何时执行”，不提供任何绕过：
     - 处理函数自己的全部校验照常进行；
     - 产生的效应意图在**派发时**照常重查权限、联系约束（联系限制优先）、real 派发开关、控制暂停和预算（ADR-023、0153、0263）。
   - 执行失败（例如请求人已失去权限，或校验不通过）时，审批记为 `approved`，执行结果记为 `execution_refused:<code>`；不会自动重试，也不会改由批准人执行。
   - **为什么选在同一个事务里**：这样不需要额外的服务主体，也就没有“谁来代替请求人执行”的问题；批准和执行要么一起提交，要么一起回滚；执行用的是请求人身份，不会扩大权限。
4. **撤回**：请求人可以撤回自己发起、仍在等待中的请求（`approvals.cancel`），审批记为 `cancelled`。
5. **Action 定义**：发布目录时（`catalog_publish`、`business_actions`），有策略的 Action 写入 `approval_mode=REQUIRED` 和对应的 `risk_level`。Action governor 的审批证据校验会因此生效：执行时由 `action_approval.ApprovalEvidenceResolver` 从已通过的审批记录生成证据，governor 再对着存储核对一遍。
6. **Run 和 Agent 发起的效应**（例如 `outreach.send` 由 Run 提交）**不在第一批**。它们的挂起点在效应意图上（像 0240 那样增加 `pending_approval` 状态），属于第 8 节第二步的合并工作。

## 5. 审批人推导

- 批准人身份只来自服务端：浏览器会话 → subject 和 principal → 当前的工作台成员关系和角色（ADR-025、ADR-035）。请求体里的任何身份字段一律忽略。
- 禁止自批：请求人和批准人按 subject 比较（同一个人换会话或换 principal 也算本人）。
- 批准人必须是人：Agent、Run 凭据和服务凭据都不能批准（`approvals.decide` 是只给人类的 Action，在 SQL 中再校验一遍 subject 的类型）。
- 批准不授予任何额外权限：审批只放行“请求人本来就有权做的事”（第 4 节第 3 点，执行时重查），不会超出请求人的上限（ceiling）。
- **单人租户**：如果租户里只有 owner 一个人，quorum ≥ 1 且禁止自批意味着 owner 自己无法完成需要审批的 Action。这是这项规则的本意，但需要负责人确认首批 Action 是否接受这一点（见第 10 节 Q-A1）。

## 6. 超时作废

- `due_by = requested_at + expires_after_seconds`，在请求时冻结。
- 过期处理有两条路径：
  - 决定时用数据库时钟判断，过期就拒绝本次决定，并把审批记为 `expired`；
  - 一个周期性的清扫：把到期仍在等待的审批记为 `expired`，并写审计。复用现有的 retention-keeper 定时任务，或另加一个检查，实现时再定；逻辑沿用上游 `expiry.py` 的 `ApprovalExpirySweeper`。
- 过期之后冻结的请求永远不会执行。请求人只能重新发起，得到一个新的审批和新的期限。

## 7. 审计、隐私与删除

- 审计事件写入 `runtime.audit_events`：`approval.requested`、`approval.decided`（含结果和原因代码）、`approval.expired`、`approval.cancelled`、`approval.executed`（含执行结果代码）。审计里只有 ID、代码、digest 和时间，**没有请求体正文**。
- 冻结的请求体只存在 `control.nexloop_approval_requests` 里，受 RLS 保护，并纳入 NX-029 的保留期：执行或作废后按保留期删除，删除时留 tombstone。
- 涉及 Consumer 的请求（例如 `consumer.erase`），在该 Consumer 被擦除时，按现有的删除传播规则处理：把 consumer_id 替换为假名，只保留计数。
- 决定表只追加：任何角色都不能 UPDATE 或 DELETE，只有保留期删除这一条路径例外。

## 8. 工作台入口

- **观察视图 `approvals`**：
  - “待我审批”：我有资格批准、不是我发起、仍在等待中的审批；
  - “我发起的”：我发起的全部审批。
  - 每条显示：Action 的显示名、请求人（显示名）、变更范围摘要（策略里白名单字段的值，不显示完整请求体）、`request_digest` 的前 12 位、期限、已有的批准数和 quorum。
- **操作**：`approvals.decide`（批准或拒绝，必须填原因）；`approvals.cancel`（只有请求人可以撤回）。
- **提醒**：沿用告警规则，新增一条 `approval_waiting`：有待审批项接近期限时提醒。

## 9. 与现有两套审批的关系和迁移路径

| 现有机制 | 语义 | 处理 |
|---|---|---|
| 0240 外联审批（`outreach_sends.state = pending_approval`，24 小时窗口，owner 角色，单人，不能是请求人） | **门控一次效应**（一次外发），挂起点在效应意图上 | 第一批保留不动。第二步合并：把 `outreach.send` 中 `requires_approval` 的 purpose 改成统一审批主体（策略 quorum 1、期限 24 小时、`approver_roles=[owner]`），`pending_approval` 状态改由统一审批记录驱动；过渡期两套并存，迁移完成前不改现有行为。约 2 天，另行立项 |
| 0103 实例候选审批（`ontology.nexloop_instance_approvals`） | **审核一个候选是否成为正式对象**，是审核流程的决定，不门控某一次 Action 调用 | **不建议并入**。语义不同，硬并会让统一审批承担审核队列的职责。最多在工作台上合并成一个“待处理”入口 |

## 10. 首批接入哪些 Action（建议，交负责人决定）

| 建议 | Action | 条件 | quorum | 理由 |
|---|---|---|---|---|
| ✔ 首批 | `real_dispatch.set` | 只在 `enabled=true` 时（打开真实外发）；关闭不需要审批，必须能立即生效 | 1 | 打开 real 外发是对外影响最大的一步 |
| ✔ 首批 | `consumer.erase` | 只限 owner 主动发起的擦除；顾客本人请求的擦除不经审批，不能被拖延 | 1 | 不可逆 |
| ✔ 首批 | `retention.release_hold` | 无 | 1 | 解除法律或业务保全后，数据会被清理 |
| 可选 | `goals.budget.set` | 只在提高上限时 | 1 | 涉及花费 |
| 可选 | `semantic_release.publish`、`strategy.publish` | 无 | 1 | 改变线上行为；已有评估门，审批是多出来的一层 |
| 不建议 | `experiment.*`、`outreach_purposes.set` 等 | — | — | 已有各自的护栏，加审批只会增加摩擦 |

**需要负责人决定：**

- **Q-A1（单人租户）**：首批 Action 在只有 owner 一个人的租户里将无法执行。选项如下：
  - (a) 接受这一点，这些租户要先添加第二位成员；
  - (b) 允许策略声明 `quorum: 0`，即只做事后审计，用于单人租户；
  - (c) 首批只做 `real_dispatch.set`。

  建议选 (a)，这正是禁止自批的意义；stage 上需要先准备第二个测试成员。
- **Q-A2**：首批的 Action 清单和条件，上表“✔ 首批”三项，是否照此执行。
- **Q-A3**：默认期限，建议 24 小时，与 0240 一致。

## 11. ADR 草案

```
# ADR-038｜统一 Action 审批（修订 0240 的定位）

状态：草案（负责人 2026-10-22 Q1 选 A）

决定：
1. 高风险的受治理 Action 统一使用一套审批：受信配置中的策略（Action 版本、可选的单字段条件、quorum、批准角色、期限）；
   请求人不能批准自己的请求；一票拒绝即终结；超期作废；审批绑定请求摘要，请求体一变就失效。
2. 审批只延后执行，不授予权限：最后一次批准在同一事务里，以请求人当时的身份执行冻结的请求；
   执行时请求人的权限、Action 自身的校验，以及效应派发时的联系约束、real 派发开关、控制和预算都照常重查。
3. 批准人只能是当前持有策略角色的人类成员；Agent、Run 和服务凭据都不能批准。
4. 上游的状态机和到期逻辑按 ADR-006 vendored；持久化层按 NexLoop 的 authz 签名、world、RLS 和只追加规则重写。
5. 现有的 0240 外联审批暂时保留，之后作为效应级审批并入；0103 候选审批属于审核流程，不并入。
后果：需要审批的 Action 在单人租户中无法执行（Q-A1）；审计只记录 ID、代码和 digest。
```

## 12. 实现切片与工作量

| 切片 | 内容 | 估计 |
|---|---|---|
| A0 | vendored `state_machine.py`、`expiry.py` 和上游测试，记录 provenance | 0.5 天 |
| A1 | 迁移：策略、请求、决定三张表，RLS，只追加，签名入口 `approvals.request`、`approvals.decide`、`approvals.cancel`，审计 | 1.5 天 |
| A2 | 受治理入口的接合：策略匹配、202 挂起、在同一事务中继续执行并重查请求人、执行结果记录；目录中的 `approval_mode` 和风险等级；接上 governor 的审批证据校验 | 1.5 天 |
| A3 | 过期清扫（复用上游 sweeper）和告警 `approval_waiting` | 0.5 天 |
| A4 | 工作台：观察视图、两个操作、前端列表和详情 | 1.5 天 |
| A5 | 首批 Action 接入，加测试（禁止自批、quorum、期限、请求体变化后失效、执行时请求人失去权限、派发重查不被绕过、单人租户），保留与删除传播 | 1 天 |
| A6 | ADR、运行手册、stage 验证（第二个测试成员） | 0.5 天 |

合计约 **7 天**，与 L7 估计的 6–8 天一致。把 0240 并入统一审批另计约 2 天。A1、A2 会改动受治理入口这条公共路径，需要 L6 深审。
