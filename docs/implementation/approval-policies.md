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

3. **核对来源**（只读）。owner 会话的来源：DATA_HOST 上的 PG 容器里，用 bootstrap 角色经本地 socket 登录，然后 `set role nexloop_owner`。bootstrap 是唯一能切换到 owner 的登录角色；只读核对不需要配置员窗口。

   ```
   docker exec -i <PG 容器> psql -h /var/run/postgresql -U nexloop_bootstrap -d <库名> -v ON_ERROR_STOP=1 <<'SQL'
   begin read only;
   set local role nexloop_owner;
   select manifest_version, manifest_sha256, expected_revision, appended, unchanged, published_at from control.nexloop_approval_policy_publications order by 1;
   select capability, version, quorum, approver_roles, expires_after_seconds, solo_mode from control.nexloop_approval_policies order by 1, 2;
   rollback;
   SQL
   ```

   `manifest_sha256` 应等于发布 checkout 中 `sha256sum deploy/configuration/approval-policies.v1.json` 的结果。

4. **单人模式暂停 24 小时**：某个能力发布了新的策略版本后，资格时间线按新版本重新开始。所以该能力的单人模式要等新版本满 24 小时才会再次生效，这期间只能由他人批准。单人租户重发策略前要考虑这一点。
5. **生效范围**：发布之后，首批三个能力立即受审批约束，包括 stage 上的 owner。所以在发布**之前**，要按 A6 运行手册准备好第二位有资格的成员，或者在 A7 上线后开启单人模式。

**回退与停用**（与 ADR-039 §2.1 的口径一致）：策略只追加，没有删除路径。
- 要放宽，就发布一个新版本的清单，例如把期限改长、改批准角色。
- 要停用某个能力的审批，就发布一个新版本：该策略的版本加 1，条件设成永远不会命中的常量（例如 `{"field": "enabled", "equals": "never"}`），并在 `decision` 中写明停用理由和批准人。这是 ADR-039 认定的受治理停用方式，发布同样记录 sha256。恢复时，再发布一个新版本，把条件改回来。

## 3. 待接（不在 NX-056 范围内）

- **顾客本人的擦除请求入口**（NX-019 数据权利 Claim、WebChat）目前只生成 `pending_confirmation`。负责人确认时应调用 `nexloop.consumer.erase_on_request`，它不受审批约束。工作台还没有这个入口，需要另立任务接入。在接入之前，负责人如果改用 `consumer.erase` 确认，就会受审批约束。
- NX-058 的成员类 Action 接入时，要为它们设置 `target_subject_field`（Z1）和 `summary_fields` 白名单。

## 4. stage 启用顺序（负责人 2026-10-23 选 A：先准备第二位测试 owner，再发布策略 v1，然后马上做双人演练）

上机由调度员执行。变量和两种一次性容器（`$COMMON`、`$WINDOW`、`$APPTOOL`、`$BACKEND_IMAGE`、`$TENANT_ID`、`$KEY_ID`、`$RUN`、`$APP_RUN`）沿用 `docs/release/run-assembly-20261015.md` §0.2、§0.3；源码树按 §0.4 分发本批的发布 checkout。账号的用户名和口令都由调度员生成，存入钥匙串（`nexloop/…` 条目，值不进仓库、不进文档、不进证据）。

1. **备份**（DATA_HOST）：`sudo -n systemctl start nexloop-pg-diff.service`，确认这次运行成功（`systemctl status` 和备份目录的新时间戳）。没有经过验证的备份，不做迁移。
2. **迁移**：本批的迁移（NX-056 在 main 上为 0326–0332，随后的修正为临时 0337，组批时重新编号）随发布一起执行，按该批的部署单进行。迁移之后，审批存储、闸门、视图和单人模式的表都在，但还没有策略，任何能力都不需要审批。
3. **发布新的 Action**（DATA_HOST，配置员窗口；`docs/release/run-41-20261015.md` §3a.2 的形态）。本批新增 `nexloop.approvals.decide`、`nexloop.approvals.cancel`、`nexloop.consumer.erase_on_request`、`nexloop.approvals.solo_mode.set`。不发布的话，工作台调用会 404（`published action unavailable`）。

   ```
   docker run $COMMON -e PYTHONPATH=/opt/release/packages/eios-core/src "$BACKEND_IMAGE" /opt/release/scripts/deploy/stage_secrets.py publish \
     --out /stage --tenant "$TENANT_ID" --dry-run
   docker run $COMMON $WINDOW -e PYTHONPATH=/opt/release/packages/eios-core/src "$BACKEND_IMAGE" /opt/release/scripts/deploy/stage_secrets.py publish \
     --out /stage --tenant "$TENANT_ID" --bootstrap-dsn-file /run/bootstrap-dsn --signing-key-id $KEY_ID --dry-run
   # 核对 actions_to_publish 只包含上面 4 项、conflicts 为空，然后去掉 --dry-run 再执行一次；第三次执行应为 changed=false
   ```

4. **第二位测试 owner 和角色清单**：
   - 先建账号（APP_HOST，`initial_identity --create`，run-assembly §1 的形态）。清单由 `deploy/examples/nx037/identity-human.example.json` 填写，应用为 `$WORKBENCH_APPLICATION_ID`，ID 用合成值（例如 `stage-human-approver2`）。口令文件由调度员从钥匙串写入 `$APP_RUN/in/`，本步结束即删除。

     ```
     docker run $APPTOOL "$BACKEND_IMAGE" -m nexloop_eios.initial_identity --manifest /in/identity-approver2.json --create \
       --identity-database-url-file /run/secrets/api_identity_dsn --password-file /in/password-approver2 --idempotency-key-file /in/identity-seal
     ```

   - 再在成员文件 `$RUN/workbench-members.json` 中加一行 `{"principal_id": <第二位的 principal>, "subject_id": <第二位的 subject>, "role": "owner"}`，并应用本批的角色清单（`workbench-roles.v1.json`：以 main 为准，第二十四批合入后为 manifest v40）。这一步在 DATA_HOST 上经配置员窗口执行（run-assembly §2 的形态）：

     ```
     docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/workbench_roles.py --roles /opt/release/deploy/authorization/workbench-roles.v1.json \
       --members /stage/workbench-members.json --apply --bootstrap-dsn-file /run/bootstrap-dsn --signing-key-file /stage/management/signing.key.hex --signing-key-id $KEY_ID
     ```

   - 核对：两位 owner 各自登录工作台，审批页可以打开，三栏都为空。

5. **发布告警规则 v14**（DATA_HOST，配置员窗口；host-health §4.5 的形态）。v14 是 L7 的 v13（61 条）加上 NX-056 的 6 条（`alert-rules.v14.json`，以 L7 的 v13 为前缀）：`approval_waiting`、`approval_unconsumed`、`approval_sweep_stale`、`approval_solo_mode`、`approval_solo_self_approved`、`approval_solo_eligibility_reduced`。

   ```
   docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/alert_rules_apply.py --manifest /opt/release/deploy/configuration/alert-rules.v14.json \
     --tenant "$TENANT_ID" --bootstrap-dsn-file /run/bootstrap-dsn
   ```

6. **`--check`**（不连库）：

   ```
   docker run $COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/approval_policies_apply.py --manifest /opt/release/deploy/configuration/approval-policies.v1.json --check
   ```

   核对 `manifest_sha256` 与发布 checkout 中 `sha256sum` 的结果一致。
7. **发布策略 v1**：按 §2 第 2 步执行，再按 §2 第 3 步做只读核对。从这一刻起，首批三个能力都需要另一位 owner 批准。
8. **双人演练**，全部在合成租户、synthetic 数据上进行，真实派发保持关闭：
   - **批准**：负责人在工作台请求打开一个 test 模式的 action_type 真实派发开关，页面提示“需要另一位成员批准”。第二位 owner 在“待我审批”中写理由批准。
   - **执行**：负责人在“待我执行”中点“执行”，开关打开。核对审计里依次有 `approval.requested`、`approval.decided`、`approval.consumed`，并且 `approval.consumed` 中的 actor 是负责人、approved_by 是第二位 owner。
   - **关闭**：负责人关闭这个开关。关闭不需要审批，应立即生效。
   - **撤回**：负责人再请求一次，在“我发起的”中撤回，状态变为“已撤回”。同一请求 1 小时内再次发起，返回 `cooldown`。
   - **等待告警**：负责人再发起一条请求，不审批，等超过 1 小时后，告警页出现 `approval_waiting`。然后由第二位 owner 拒绝，或者等它过期，告警解除。
   - 证据：每一步的页面状态、审计事件 ID 和告警记录（不含口令和 DSN），记入任务证据。

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

## 7. 应急：批准人不可用

当唯一的另一位批准人不可用（休假、离职、账号锁定），而某个受审批的操作必须执行时，按以下顺序选择，越靠前越好：

1. **换一位批准人**：经成员文件和 `workbench_roles --apply`（§4 第 4 步的形态）给另一位可信成员 owner 角色。新成员立即可以批准（投票冷却等 NX-058 片 C 对接后才启用）。
2. **账号锁定**：按 run-assembly §1a 重置口令，或按 §2a 撤出原成员、换新账号。
3. **单人模式**（A7 上线后，只适合确实只有一个人的租户）：owner 在审批页开启，过去 24 小时内一直只有本人有资格时才生效。换了一个人、发布了新的策略版本，都要重新等满 24 小时。双人租户不能用它绕过另一个人：资格人数降到 1 之后要等满 24 小时，并且会触发 `approval_solo_eligibility_reduced` 告警。
4. **受治理的停用**（§2 回退与停用）：发布一个新版本的策略清单，把该能力的条件设成永远不会命中的常量，`decision` 中写明理由和批准人。操作完成后，再发布一个版本恢复。

不允许的做法：直接写 SQL 改审批表、用 superuser 插入 approved 记录、让别人代为登录批准人的账号。这些都违反 AGENTS.md 的安全不变式。
