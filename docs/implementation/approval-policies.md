# NX-056 审批策略的发布（受信配置）

设计：`docs/design/unified-approvals.md` 第 3、10、16 节。本文只写怎样把 `deploy/configuration/approval-policies.v<N>.json` 发布到某个环境，以及怎样证明库里的策略来自哪个文件。启用的先后顺序（先准备第二位测试成员，或者先开单人模式）属于 A6 运行手册。

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
