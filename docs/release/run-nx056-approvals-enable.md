# 执行单（草稿）：NX-056 首批审批策略在 stage 上启用

负责人 2026-10-23 选定方案 A：先加第二位测试 owner，再发布策略 v1，然后马上做双人演练。本单把运行手册 `docs/implementation/approval-policies.md` §4 写成可以照着执行的命令序列。

- 决定：ADR-039；运行手册：`docs/implementation/approval-policies.md`（§2 发布、§4 启用顺序、§6 返回码、§7 应急）。
- **执行者与判定者分开记录**：
  - 第 1–7 步由调度员执行；
  - 第 8 步演练中，负责人账号的操作由负责人本人完成；第二位测试 owner 是**测试账号**（合成身份，账号由调度员生成并保管在钥匙串中，不属于任何真实的人），它的操作由调度员完成；
  - 判定由 L6 或负责人做（看证据，不参与操作）。
  - 调度员操作测试账号，不违反“不得代他人登录批准人账号”：这个账号本来就是为演练设立的测试身份，没有“他人”。真实成员的账号始终只由本人使用。
- **时长**：演练（第 8 步）至少需要 2 小时，因为 8.6 要等待超过 1 小时才能看到告警；加上第 1–7 步，整单约 3 小时。
- **镜像**：需要 main 9779abb6（第二十四批）或之后的 backend 镜像，其中包含 `alert_rules_apply.py` 和 `approval_policies_apply.py`。
- 每一步标注 **A**（只读，可以随时执行）或 **B**（写入，按顺序执行，执行前确认上一步通过）。
- **按 main 9779abb6 核对过**：NX-056 的迁移是 0326–0332（跟进修正 0337 随下一批），角色清单 manifest 40，告警规则 v14（67 条，最新版本），`alert_rules_apply.py` 与 `approval_policies_apply.py` 都在。执行前如果 main 又前进了，再核对一次这几项。

## 0. 前提与变量

### 0.1 前提（不满足就不开始）

| # | 前提 | 怎么核对 |
|---|---|---|
| P0 | 第二十四批部署时已经做过**迁移前备份**：DATA_HOST 执行 `sudo -n systemctl start nexloop-pg-diff.service`，并记下备份标签（`BACKUP_LABEL`）和**备份开始时间**（第 1 步 `journalctl --since` 要用）。部署单必须记录这两项 | 第 1 步核对 |
| P1 | 第二十四批（含 NX-056 A0–A7 及 review 11）已合入 main，并且已按该批的部署单部署到 stage | `git log --oneline -1 $MAIN` 为部署单记录的提交；`/health/ready` 正常 |
| P2 | stage 的真实派发保持关闭 | 部署层 `REAL_DISPATCH_ENABLED` 不是 `true`；工作台“真实派发”面板中租户开关为关闭 |
| P3 | 保留期服务和告警评估服务都在运行 | `docker compose -p nexloop-app ps` 中 `retention-keeper`、`alert-evaluator` 都是 running |
| P4 | 负责人能登录工作台，并且是 owner | 负责人登录后，审批页可以打开 |
| P5 | 钥匙串里已经有第二位测试 owner 的口令条目（调度员生成，`nexloop/stage/approver2`） | `security find-generic-password -s nexloop/stage/approver2 >/dev/null`，只检查是否存在，不打印值 |

### 0.2 变量

与 `docs/release/run-assembly-20261015.md` §0.2 相同：`TENANT_ID`、`WORKBENCH_APPLICATION_ID`、`MAIN`、`RUN`、`APP_RUN`、`NEXLOOP_ROOT`、`KEY_ID`。另外增加：

| 变量 | 含义 |
|---|---|
| `BACKEND_IMAGE` | 本批部署单记录的 backend 镜像（digest 形式） |
| `PG_CONTAINER` | DATA_HOST 上 PG 容器的名字（`docker compose -p nexloop-data ps -q postgres`） |
| `DB` | 业务库名（部署单 §0 中的值） |

变量值只在 shell 中设置，不进仓库、不进证据。

### 0.3 一次性容器与只读会话

- `$COMMON`、`$WINDOW`（DATA_HOST）、`$APPTOOL`（APP_HOST）：与 run-assembly §0.3 相同。
- **只读 owner 会话**：在 DATA_HOST 上，用 bootstrap 角色经 PG 容器内的本地 socket 登录，然后在只读事务里 `set local role nexloop_owner`，事务结束时 `rollback`。本单的所有核对查询都用这个形态：

```
ro() { docker exec -i "$PG_CONTAINER" psql -h /var/run/postgresql -U nexloop_bootstrap -d "$DB" -v ON_ERROR_STOP=1 -At \
  -c "begin read only" -c "set local role nexloop_owner" -c "select set_config('eios.tenant_id','$TENANT_ID',true)" -c "$1" -c "rollback"; }
```

  每次调用的输出里，第一行是 `set_config` 返回的租户 ID（为了满足表上的行级安全策略而设置），不是查询结果，核对时跳过这一行即可。

### 0.4 源码树

与 run-assembly §0.4 相同。用 `$MAIN` 的 `git archive` 打包 `scripts/deploy deploy/authorization deploy/configuration deploy/examples/nx037 packages/eios-core/src`，分发到 `$RUN/src`（DATA_HOST，属主 999）和 `$APP_RUN/src`（APP_HOST，属主 10001）。

## 1. 核对迁移前备份（DATA_HOST，A）

备份必须在迁移之前完成。本单不重新备份，只核对第二十四批部署时做的那一次（P0）：

```
sudo journalctl -u nexloop-pg-diff.service -o cat --since "<部署单记录的备份开始时间>" | tail -n 5
sudo docker exec -u postgres "$PG_CONTAINER" pgbackrest --stanza=nexloop info --output=json \
  | python3 -c 'import json,sys; [print(b["label"], b["type"], b["timestamp"]["stop"]) for s in json.load(sys.stdin) for b in s["backup"]]'
```

- **预期**：
  - journal 最后的 JSON 行为 `ok: true`、`verify_status: verified`，repo1 与 repo2 各一次；
  - `info` 中出现标签 `$BACKUP_LABEL`，它的结束时间早于本批迁移开始的时间（迁移时间以部署单记录为准）。
- 核对不通过就停下，不往下执行：没有经过验证的迁移前备份，就不能把这次部署视为可回退。

## 2. 迁移（随本批部署单，A 核对）

迁移由第二十四批的部署单执行，本单只核对结果：

```
ro "select version from control.schema_migrations order by version desc limit 8"
ro "select to_regclass('control.nexloop_approval_policies') is not null, to_regclass('control.nexloop_approval_eligibility_events') is not null"
ro "select count(*) from control.nexloop_approval_policies"
```

- **预期**：迁移尾是 0336（main 9779abb6 的 catalog 尾，其中 NX-056 为 0326–0332；跟进修正 0337 随下一批部署时才会出现）；两张表都存在（`t|t`）；策略为 0 条，也就是此刻还没有任何能力需要审批。

## 3. 发布新的 Action（DATA_HOST，配置员窗口，B）

本批新增 4 个 Action：`nexloop.approvals.decide`、`nexloop.approvals.cancel`、`nexloop.consumer.erase_on_request`、`nexloop.approvals.solo_mode.set`。

```
# 3.1 只编译，不连库（A）
docker run $COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/stage_secrets.py publish --out /stage --tenant "$TENANT_ID" --dry-run
# 3.2 与已发布内容比对（经配置员窗口，只读，A）
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/stage_secrets.py publish --out /stage --tenant "$TENANT_ID" \
  --bootstrap-dsn-file /run/bootstrap-dsn --signing-key-id "$KEY_ID" --dry-run
# 3.3 发布（B）：同 3.2，去掉 --dry-run
# 3.4 再执行一次 3.3（B，经配置员窗口，标为写入）：应为 changed=false
```

- **3.2 预期**：`actions_to_publish` 的总数等于第二十四批组批记录中“待发布 Action”的总数，并且包含上面 4 项；`conflicts` 为空。数目不符就停下，对照组批记录核对。
- **3.3 预期**：`changed: true`，`authority_revision` 加 1。
- **核对**：`ro "select count(*) from control.nexloop_action_definitions where active and resource_id like 'eios:action:nexloop.approvals.%'"`，应为 3（decide、cancel、solo_mode.set）。

## 4. 第二位测试 owner（B）

### 4.1 建账号（`initial_identity --create`）

1. **管理机**：按 `deploy/examples/nx037/identity-human.example.json` 填写 `$APP_RUN/in/identity-approver2.json`，应用为 `$WORKBENCH_APPLICATION_ID`：
   - `SUBJECT_ID=stage-human-approver2`；
   - `PRINCIPAL_ID=stage-principal-approver2`；
   - `USERNAME`：调度员生成，存入钥匙串，填入清单；
   - `EMAIL` 用 `.invalid` 域；
   - `NOW_UTC_ISO` 取当前时刻减几秒。

   然后复制到 APP_HOST 的 `$APP_RUN/in/`（0600，属主 10001）。
2. **管理机 → APP_HOST**：口令从钥匙串经管道写入，不出现在 argv 和终端输出中：

```
umask 077; security find-generic-password -s nexloop/stage/approver2 -w | ssh <APP_HOST> "sudo sh -c 'umask 077; cat > $APP_RUN/in/password-approver2 && chown 10001:10001 $APP_RUN/in/password-approver2'"
```

3. **APP_HOST**：建号，然后删掉口令文件：

```
docker run $APPTOOL "$BACKEND_IMAGE" -m nexloop_eios.initial_identity --manifest /in/identity-approver2.json --create \
  --identity-database-url-file /run/secrets/api_identity_dsn --password-file /in/password-approver2 --idempotency-key-file /in/identity-seal
sudo shred -u "$APP_RUN/in/password-approver2"
```

- **预期**：`created: true`。再执行一次是幂等的，不会再建。
- **配额**：工作台配额是 5 个（run-34 §2.2），已用 1 个，这里用掉第 2 个。

### 4.2 成员文件与角色清单（DATA_HOST，配置员窗口）

1. 在 `$RUN/workbench-members.json`（0600，属主 999）的 `members` 里加一行，原有成员保持不变：

```
{"principal_id": "stage-principal-approver2", "subject_id": "stage-human-approver2", "role": "owner"}
```

2. 应用本批的角色清单（`workbench-roles.v1.json`，main 9779abb6 上为 manifest 40（含 NX-056 A7 和 NX-058 片 A、B 的角色））：

```
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/workbench_roles.py --roles /opt/release/deploy/authorization/workbench-roles.v1.json \
  --members /stage/workbench-members.json --apply --bootstrap-dsn-file /run/bootstrap-dsn --signing-key-file /stage/management/signing.key.hex --signing-key-id "$KEY_ID"
```

- **预期**：成功退出；再执行一次结果相同。
- **核对**：
  - `ro "select members ? 'stage-principal-approver2', members->>'stage-principal-approver2' from control.nexloop_workbench_configurations where tenant_id='$TENANT_ID' order by manifest_version desc limit 1"`，应为 `t|owner`；
  - 第二位 owner 用自己的账号登录工作台，审批页可以打开，三栏都为空，页面顶部没有单人模式提示。

## 5. 告警规则 v14（DATA_HOST，配置员窗口，B）

```
docker run $COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/alert_rules_apply.py --manifest /opt/release/deploy/configuration/alert-rules.v14.json --check
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/alert_rules_apply.py --manifest /opt/release/deploy/configuration/alert-rules.v14.json \
  --tenant "$TENANT_ID" --bootstrap-dsn-file /run/bootstrap-dsn
```

- **预期**：`--check` 输出 `{"valid": true, "version": 14, "rules": 67}`；应用后 `applied: true`。
- 下一轮告警评估之后（约 1 分钟），`nexloop-doctor --observability` 的 `alert_rules_current` 为 true。
- **前提**：镜像必须是 main 9779abb6 或之后的版本（含 `alert_rules_apply.py`）。没有就停下，不另找写入规则的办法。

## 6. 策略清单 `--check`（不连库，A）

```
docker run $COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/approval_policies_apply.py --manifest /opt/release/deploy/configuration/approval-policies.v1.json --check
sha256sum "$RUN/src/deploy/configuration/approval-policies.v1.json"
```

- **预期**：`{"valid": true, "version": 1, "expected_revision": 0, "policies": 3, "manifest_sha256": "<与 sha256sum 相同>"}`。把 sha256 记入证据。

## 7. 发布策略 v1（DATA_HOST，配置员窗口，B）

从这一步起，首批三个能力（打开真实派发、负责人发起的擦除、解除保全）都需要另一位 owner 批准。

```
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/approval_policies_apply.py \
  --manifest /opt/release/deploy/configuration/approval-policies.v1.json --tenant "$TENANT_ID" --bootstrap-dsn-file /run/bootstrap-dsn
# 再执行一次（B，经配置员窗口，标为写入）：result.replayed 应为 true
```

- **预期**：`applied: true`，`result.appended: 3`，`result.unchanged: 0`，`result.replayed: false`。
- **核对**：

```
ro "select manifest_version, manifest_sha256, expected_revision, appended from control.nexloop_approval_policy_publications"
ro "select capability, version, quorum, approver_roles, expires_after_seconds, solo_mode from control.nexloop_approval_policies order by 1"
ro "select capability, eligible_count from control.nexloop_approval_eligibility_events where tenant_id='$TENANT_ID' order by event_id"
```

- **预期**：
  - 第一行的 sha256 与第 6 步一致，`expected_revision=0`；
  - 三条策略都是 quorum 1、`{owner}`、86400、`allowed`；
  - 资格时间线每个能力一行，`eligible_count=2`（负责人和第二位 owner）。

## 8. 双人演练（工作台，B；至少 2 小时）

全部在合成租户进行，真实派发保持关闭（P2）。

演练用的开关：action_type 层，`eios:action:nexloop.service.request:1`。租户开关和部署层开关都关着，所以即使这个开关打开，也不会有任何真实外发。

**原因文本写死**：判断“是不是同一请求”时只去掉 request_id，原因文本也算在内。所以三段演练各用一段固定的原因，彼此不同：

| 段 | 原因文本（原样输入） | 用途 |
|---|---|---|
| R1 | `演练一：批准与执行` | 8.1–8.4 |
| R2 | `演练二：撤回与冷却` | 8.5 |
| R3 | `演练三：等待告警` | 8.6–8.7 |
| W | `演练二撤回：不再需要` | 8.5 撤回时填写的原因（撤回必须写原因，1–500 字；不填会返回 `approval: invalid`） |

操作者一列：“负责人”指负责人本人用自己的账号操作；“测试 owner”指调度员用第二位测试 owner 的测试账号操作。

| # | 谁 | 操作 | 预期 |
|---|---|---|---|
| 8.1 | 负责人 | 真实派发面板：打开 action_type `eios:action:nexloop.service.request:1`，原因 R1 | 页面提示“需要另一位成员批准”；审批页“我发起的”出现一条，状态“等待审批”，摘要显示 `scope_kind=action_type，enabled=true` |
| 8.2 | 测试 owner | 审批页“待我审批”：看到这条，写批准原因后批准 | 状态变为“已批准”；负责人自己的“待我审批”里没有这条（禁止自批） |
| 8.3 | 负责人 | 审批页“待我执行”：点“执行” | 提示“已按批准执行；这次批准已用掉”；真实派发面板中这个 Action 开关为打开 |
| 8.4 | 负责人 | 真实派发面板：关闭这个开关 | 立即生效，不需要审批（关闭不在策略条件内） |
| 8.5 | 负责人 | 8.4 之后立即：再次打开同一开关，原因 R2；在“我发起的”中撤回，撤回原因 W；然后立即再次以原因 R2 打开 | 第一次：状态变为“已撤回”。第二次：被拒绝，返回 `cooldown`（同一请求撤回后 1 小时冷却）。冷却只针对 R2，不影响下一步 |
| 8.6 | 负责人 | 8.5 之后立即：以原因 R3 打开同一开关，不审批 | 进入“等待审批”。超过 1 小时后，告警页出现 `approval_waiting`（“审批请求等待超过 1 小时”）。如果拖过 24 小时，请求会过期（`approval.expired`），这一段作废：换一段新的原因（例如 `演练三（重做）：等待告警`）从 8.6 重做 |
| 8.7 | 测试 owner | 告警出现后，拒绝 8.6 的请求 | 状态变为“已拒绝”；下一轮评估后 `approval_waiting` 解除 |

**审计核对**（8.3 之后，A）：

```
ro "select action, payload->>'approval_id', payload->>'outcome', payload->>'actor', payload->'approved_by' from runtime.audit_events where tenant_id='$TENANT_ID' and action like 'approval.%' order by created_at"
```

- **预期**：依次有 `approval.requested`、`approval.decided`、`approval.consumed`。其中 `approval.consumed` 的 actor 是负责人的 subject，`approved_by` 是 `["stage-human-approver2"]`。
- 8.5 之后多一条 `approval.cancelled`（撤回）；8.6 之后多一条 `approval.requested`；8.7 之后多一条 `approval.decided`（outcome rejected）。`cooldown` 是拒绝发起，不写审计。

**证据**（记入 NX-056 任务证据，不含口令、DSN 和用户名；每条写明执行者和判定者）：

- 第 3、5、6、7 步命令的输出（JSON 行）；
- 第 7 步的 sha256；
- 8.1–8.7 每一步的页面状态截图或文字记录；
- 审计查询的结果；
- `approval_waiting` 告警的触发和解除时间；
- 执行者：调度员（第 1–7 步、测试 owner 的操作）和负责人（负责人账号的操作）；判定者：L6 或负责人。

## 9. 回退

| 情况 | 做法 |
|---|---|
| 第 3–5 步失败 | 不继续往下。Action、角色和告警规则的发布都是幂等的，修正后重跑即可；在第 7 步之前，没有任何能力需要审批 |
| 第 7 步之后需要取消审批 | 按运行手册 §2“回退与停用”：发布 `approval-policies.v2.json`，`expected_revision: 1`，三条策略的版本都加 1，条件设成永远不会命中的常量，`decision` 中写明理由和批准人。命令与第 7 步相同 |
| 第二位 owner 不再需要 | 先停用策略（上一行），再按 run-assembly §2a `--revoke-member` 撤出。策略生效期间，不要让 owner 只剩一位（运行手册 §7） |
| 演练中真实派发开关状态异常 | 负责人关闭开关（关闭不需要审批），然后按 `docs/OPERATIONS.md` §8 核对 |
| 策略已发布、演练失败 | 按顺序：① 负责人关闭演练中打开过的开关（关闭不需要审批，立即生效）；② 测试 owner 拒绝、负责人撤回所有仍在等待的演练请求；③ 记录失败点和返回码（运行手册 §6），交 L6；④ 如果问题出在审批本身、短期修不好，按上面“取消审批”发布 v2。策略保持生效本身不影响业务，因为首批三个能力都是低频的 owner 操作 |
| 能不能撤回发布 | 第 3 步发布的 Action 和第 4 步写入的角色**不能撤回发布**：定义只追加（0050）。能做的只是撤销授权（`--revoke-member`）、停用策略（v2）。所以第 3、4 步执行前，要按 3.2 的 dry-run 和组批记录逐项核对 |

不允许的做法：直接写 SQL 改审批表、用 superuser 插入 approved 记录、代他人登录批准人账号（AGENTS.md 安全不变式）。
