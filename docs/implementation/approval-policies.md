# NX-056 统一审批：策略发布与运行手册

决定：ADR-039；设计：`docs/design/unified-approvals.md` 第 3、10、15–17 节。本文写四件事：怎样把 `deploy/configuration/approval-policies.v<N>.json` 发布到某个环境、怎样证明库里的策略来自哪个文件、stage 上的启用顺序、日常运行和问题定位。

## 1. 文件与规则

- 一个文件就是一份完整的策略清单：
  - `version`：清单版本；
  - `expected_revision`：发布前库里最近一次已发布清单的版本，首次发布为 0；
  - `policies`：每个能力一条。
- 策略本身只追加：
  - 改一条策略，要给它一个新的策略 `version`；
  - 同一策略版本再次出现时，内容必须完全相同；
  - 清单里没有出现的能力，保留原有策略，本版本不支持删除。
- 策略是**部署级**的：所有租户共用一套，没有租户维度。命令里的 `--tenant` 只用来确认目标租户存在，不会写进策略。
- `summary_fields` 只能取该能力白名单里的字段（`control.nexloop_approval_summary_allowed`，Python 中的 `SUMMARY_ALLOWED` 与之一致）。白名单之外的能力不显示任何摘要。给新能力加白名单，要随该能力的接入一起新增迁移。
- 数据库为每次发布写一行 `control.nexloop_approval_policy_publications`，内容是清单版本、文件原文的 sha256、`expected_revision`、新增条数、未变条数、发布者和时间：
  - 同一版本、同一 sha256 重放，返回 `replayed: true`；
  - 同一版本、不同 sha256，冲突；
  - `expected_revision` 与库里当前值不符，冲突。

## 2. stage 发布步骤（调度员；与 host-health §4.5 的告警规则发布形态相同）

1. **校验**，不连库，可以在任意机器上执行：

   ```
   python3 scripts/deploy/approval_policies_apply.py --manifest deploy/configuration/approval-policies.v1.json --check
   ```

   预期输出：`{"valid": true, "version": 1, "expected_revision": 0, "policies": 3, "manifest_sha256": "<与 sha256sum 相同>"}`。

2. **发布**：在 DATA_HOST 上用一次性 backend 容器执行，与 run-41 §4.2 的形态相同。bootstrap socket DSN 文件只读挂载，只在本次调用期间打开配置员登录窗口：

   ```
   ... "$BACKEND_IMAGE" /opt/release/scripts/deploy/approval_policies_apply.py \
       --manifest /opt/release/deploy/configuration/approval-policies.v1.json \
       --tenant "$TENANT_ID" --bootstrap-dsn-file /run/bootstrap-dsn
   ```

   预期输出：`{"applied": true, "version": 1, ..., "result": {"configured": true, "appended": 3, "unchanged": 0, "replayed": false, ...}}`。再执行一次应得到 `"replayed": true`。输出中不会出现 DSN。

3. **核对来源**：以 owner 身份执行：

   ```sql
   select manifest_version, manifest_sha256, expected_revision from control.nexloop_approval_policy_publications;
   ```

   `manifest_sha256` 应等于发布 checkout 中 `sha256sum deploy/configuration/approval-policies.v1.json` 的结果。

4. **生效范围**：发布之后，首批三个能力立即受审批约束，包括 stage 上的 owner。所以在发布**之前**，要按 A6 运行手册准备好第二位有资格的成员，或者在 A7 上线后开启单人模式。

**回退**：策略只追加，没有删除路径。要放宽，就发布一个新版本的清单，例如把期限改长、改批准角色。要整体停用，需要新增迁移（ADR-039 待定），本版本不提供。

## 3. 待接（不在 NX-056 范围内）

- **顾客本人的擦除请求入口**（NX-019 数据权利 Claim、WebChat）目前只生成 `pending_confirmation`。负责人确认时应调用 `nexloop.consumer.erase_on_request`，它不受审批约束。工作台还没有这个入口，需要另立任务接入。在接入之前，负责人如果改用 `consumer.erase` 确认，就会受审批约束。
- NX-058 的成员类 Action 接入时，要为它们设置 `target_subject_field`（Z1）和 `summary_fields` 白名单。

## 4. stage 启用顺序（A6 决定点：交调度员与负责人确认）

发布策略的那一刻起，首批三个能力就需要第二位有资格的成员批准。stage 当前只有负责人一位 owner。启用前二选一：

| 方案 | 做法 | 能否立即使用 | 取舍 |
|---|---|---|---|
| **A（推荐）：先准备第二位测试成员** | 用现有成员配置（受信配置的 members 文件加 `apply_members`），在合成租户里加一位测试 owner，账号口令存钥匙串。然后发布策略 v1 | 能，而且发布后立即可以做一次完整的双人演练 | 多维护一个测试账号；双人控制从第一天起就是真的 |
| B：等 A7 后用单人模式 | 先上线 A7，负责人开启单人模式。时间线从部署起满 24 小时后自批才生效，然后发布策略 | 部署后至少 24 小时 | 单人模式本来就是给单人租户的退路；拿它来上线等于从一开始就不演练双人流程 |

**推荐 A**。理由：

- 它不依赖 A7；
- 它可以完整验证三栏视图、执行往返和告警；
- 单人模式保留给真实的单人租户使用。

确认后的执行步骤（方案 A）：

1. 准备第二位测试 owner：members 文件加一行，用 `apply_members` 写入，写 authority facts；口令存入钥匙串的 `nexloop/…` 条目。
2. 按 §2 发布策略 v1。
3. 演练，全部在合成租户、synthetic 数据上进行：
   - 负责人请求打开一个 test 模式的 action_type 开关；
   - 测试 owner 在“待我审批”中批准；
   - 负责人在“待我执行”中执行；
   - 核对审计 `approval.requested`、`approval.decided`、`approval.consumed`，其中 actor 和 approved_by 都要正确；
   - 再请求一次后撤回；
   - 让一条请求等待超过 1 小时，确认 `approval_waiting` 告警出现，并在过期清扫后自动解除。
4. 关闭开关：关闭不需要审批，应立即生效。

## 5. 日常运行

- **发起**：在工作台照常操作，例如打开真实派发开关。返回“需要另一位成员批准”，审批出现在“我发起的”。
- **审批**：有资格的成员在审批页“待我审批”中批准或拒绝，必须写原因。发起人自己，以及审批对象本人（Z1），都看不到这一行。
- **执行**：批准后，发起人在“待我执行”中点“执行”。如果显示“工作台不能执行这条请求”，说明这条请求是从受治理 API 客户端发起的（例如 `retention.release_hold`），要回到发起时的入口，用同一请求体再提交一次。
- **期限**：默认 24 小时，过期作废，不会执行。已批准但没有执行的审批，同样在过期后作废。
- **告警**：
  - `approval_waiting`：有审批等待超过 1 小时；
  - `approval_unconsumed`：已批准超过 1 小时没有执行；
  - `approval_sweep_stale`：过期审批 15 分钟内没有被终结，说明保留期服务没有在运行。

## 6. 问题定位（返回码）

| 返回 | 含义 | 处理 |
|---|---|---|
| `waiting_approval` / `approval: required` | 已挂起，等待批准 | 找有资格的成员批准 |
| `waiting_approval` + `approver_no_longer_eligible` | 批准人已失去资格，这一票不再计入 | 请另一位有资格的成员重新批准 |
| `approval_consumed` | 1 小时内已经执行过同一请求 | 不会重复执行；确需再做，等 1 小时后重新发起 |
| `policy_changed` | 审批是在旧策略版本下发起的，已作废 | 重新提交，按新策略审批 |
| `execution_refused:<sqlstate>` | 已按批准执行，但处理函数拒绝了，审批已用掉 | 修正请求后重新发起 |
| `self_approval` / `target_cannot_approve` | 发起人本人，或者审批对象本人，不能批准 | 换一位批准人 |
| `too_many_waiting` / `cooldown` | 同一能力已有 3 条等待中的审批 / 同一请求被拒绝或撤回后的 1 小时冷却期内 | 处理已有的审批 / 等冷却结束 |
| `target_unresolved` | 策略声明了目标成员字段，但字段不指向本租户 invited 或 active 状态的成员 | 核对请求 |
| `policy_unavailable` | Action 发布了新版本，但没有对应的策略版本 | 发布新版本的策略清单（§2） |
| HTTP 503（暂停） | 执行时控制处于暂停状态 | 恢复控制后再执行，审批不会被消费 |
