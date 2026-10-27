# 执行单（草稿）：NX-056 首批审批策略在 stage 上启用

负责人 2026-10-23 选定方案 A：先加第二位测试 owner，再发布策略 v1，然后马上做双人演练。本单把运行手册 `docs/implementation/approval-policies.md` §4 写成可以照着执行的命令序列。

- 决定：ADR-039；运行手册：`docs/implementation/approval-policies.md`（§2 发布、§4 启用顺序、§6 返回码、§7 应急）。
- **执行者与判定者分开记录**：
  - 第 1–7 步由调度员执行；
  - 第 8 步演练中，负责人账号的操作由负责人本人完成；第二位测试 owner 是**测试账号**（合成身份，账号由调度员生成并保管在钥匙串中，不属于任何真实的人），它的操作由调度员完成；
  - 判定由 L6 或负责人做（看证据，不参与操作）。
  - 调度员操作测试账号，不违反“不得代他人登录批准人账号”：这个账号本来就是为演练设立的测试身份，没有“他人”。真实成员的账号始终只由本人使用。
- **两天执行**（负责人 2026-10-25 选方案一，原因是 NX-058 片 D 的 D1：24 小时投票冷却）：
  - **第 0 天**：第 1–6 步（核对备份与迁移、发布 Action、加入第二位测试 owner、告警规则、策略 `--check`），末尾做 §6a 的只读核对，记下两位 owner 的 `vote_eligible_from` 和 D1 的部署时刻；
  - **第 1 天**：先做 §6b 的前提检查，全部满足才执行第 7 步（发布策略 v1）和第 8 步（双人演练）。
  - 两天之间至少隔 24 小时：第二位 owner 在第 0 天第 4 步才获得 owner 角色，冷却从那时起算；D1 本身也要部署满 24 小时。
- **时长**：第 0 天约 1 小时；第 1 天的演练（第 8 步）至少需要 2 小时，因为 8.6 要等待超过 1 小时才能看到告警，加上第 7 步，第 1 天约 2.5 小时。
- **镜像**：需要 94baecc3 或更新的发布（含 NX-056 B1 `0342_nx056_target_member` 与 NX-058 D1 `0343_vote_cooldown`），其中包含 `alert_rules_apply.py` 和 `approval_policies_apply.py`。
- 每一步标注 **A**（只读，可以随时执行）或 **B**（写入，按顺序执行，执行前确认上一步通过）。
- **按 94baecc3 核对过**：迁移尾 0343（NX-056 主体 0326–0332，跟进修正 0339，B1 为 0342，D1 投票冷却为 0343），角色清单 manifest 40，告警规则 v14（67 条），`alert_rules_apply.py` 与 `approval_policies_apply.py` 都在。执行前如果发布又前进了，再核对一次这几项。

## D1R. 第 1 天全部由调度员执行（负责人 2026-10-26 决定，先读这一节）

stage 没有真实上线，负责人决定第 1 天（6b、7、8）由调度员全程执行，**负责人本人的账号不参与**。为此新增了第三位合成测试 owner：

| 账号 | subject / principal | 角色 | 在第 1 天的作用 |
|---|---|---|---|
| 负责人 | （真人） | owner | **不做任何操作，也不能被选作批准人**：不登录、不批准、不拒绝、不撤回、不执行。它仍是有资格的批准人（策略的批准角色是 owner），所以 approver3 的请求也会出现在负责人的“待我审批”里，只是不处理；演练只由 approver2 决定 |
| approver3 | `stage-human-approver3` / `stage-principal-approver3`，用户名在钥匙串条目 `nexloop/stage/approver3` | owner，投票资格 2026-10-27 00:10Z 起 | **发起方**：原表中“负责人”做的 8.1、8.3–8.6 |
| approver2 | `stage-human-approver2` / `stage-principal-approver2` | owner | **批准方**：8.2、8.7 |

- 执行者：调度员（两个测试账号都由调度员操作；不代任何真人登录）。判定者：L6，只看证据。
- 两个测试账号都通过 `scripts/stage/approval_drill.py` 经工作台 HTTP 操作（见 §8 的命令）；也可以用浏览器，结果相同。
- **逐步核对过：8.x 没有哪一步只能由负责人本人完成。** 原文依赖负责人身份的只有三处，都已替换：
  - 6a/6b 的 `vote_eligible_from` 查询里的 `<负责人的 principal>` → `stage-principal-approver3`；
  - 第 7 步资格时间线 `eligible_count=2` → `3`（负责人、approver2、approver3 都是 owner）；
  - 审计核对中 `approval.consumed` 的 actor（负责人的 subject）→ `stage-human-approver3`。
  - 策略没有任何针对负责人 principal 的条件；真实派发开关是 owner 的 Action，approver3 是 owner，可以发起和执行。
- 负责人账号不参与带来的一个差别：原单 8.2 还验证了“负责人自己的待我审批里没有这条（禁止自批）”。现在改为验证 approver3 自己的“待我审批”里没有这条，并且 approver3 对自己的请求投票会被拒绝（§8 中 8.2a）。
- 第 9 节回退中由“负责人”做的动作（关闭开关、撤回）同样改由 approver3 做：关闭开关不需要审批，撤回是发起人自己的操作。

## R46. 按 release 46（main 4426c5d0）的修订（2026-10-26，先读这一节）

本单最初按 94baecc3 写成。之后发布又前进了，下面几项以本节为准，正文对应位置也已改过：

| 项 | 94baecc3 时 | release 46（4426c5d0） | 影响的步骤 |
|---|---|---|---|
| 迁移尾 | 0343 | 0355（L3 提取死信，接 L7 的 0354） | 第 2 步 |
| 角色清单 manifest | 40 | 46 | 第 4.2 步、6b Q5 |
| 告警规则 | v14（67 条） | 最高 v18（74 条），逐级超集：v14 ⊂ v15 ⊂ v16 ⊂ v17 ⊂ v18；v18 只改了提取死信那条规则的指标 | 第 5 步、6b Q5 |
| 审批策略 | `approval-policies.v1.json`，version 1，3 条 | 同名文件现在是 **version 4、`expected_revision: 3`、11 条**；version 1–3 在 `deploy/configuration/history/approval-policies.version{1,2,3}.json` | 第 6、7 步、回退 |
| 第 3 步待发布的 Action | 本批 4 个 | 以 release 46 组批记录为准（另含 NX-058 的成员类 Action、提取死信处理等） | 第 3 步 |

- **策略必须按版本依次发布**：stage 上还没有任何策略（`expected_revision` 当前为 0），直接发 `approval-policies.v1.json`（version 4，`expected_revision: 3`）会被拒为冲突。第 7 步改为依次发布 version 1 → 2 → 3 → 4 四个文件。
- **version 2–4 的内容**：NX-058 的 owner 成员类 Action（promote / demote / suspend / resume / revoke_owner，issue_owner_reset，invite_owner，erase_member），都需要另一位 owner 批准，被操作的成员不能批准，不适用单人模式。这些 Action 的 handler 在对应策略发布之前会拒绝执行，所以发布 version 2–4 之后它们才可用。
- **成员类策略的副作用**：发布后，对 owner 的降级、停用、撤销、签发重置码、邀请 owner、离职擦除都需要第二位 owner 批准。第二位测试 owner 必须一直保持可用；如果负责人以后要停用它，也要走这条审批（由负责人发起、第二位 owner 自己不能批准自己被停用的请求，见设计 §16 Z1），所以停用前要先有第三位成员，或者先发布放宽的策略版本。
- 第二位 owner 仍然用 `initial_identity --create` 和成员文件加入（第 4 步），不能用 `invite_owner`：后者本身需要另一位 owner 批准，此时还没有第二位。

## 0. 前提与变量

### 0.1 前提（不满足就不开始）

| # | 前提 | 怎么核对 |
|---|---|---|
| P0 | 第二十四批部署时已经做过**迁移前备份**：DATA_HOST 执行 `sudo -n systemctl start nexloop-pg-diff.service`，并记下备份标签（`BACKUP_LABEL`）和**备份开始时间**（第 1 步 `journalctl --since` 要用）。部署单必须记录这两项 | 第 1 步核对 |
| P1 | 94baecc3 或更新的发布已部署到 stage（含 NX-056 A0–A7、follow-up、B1） | `git log --oneline -1 $MAIN` 为部署单记录的提交；`/health/ready` 正常 |
| P2 | stage 的真实派发保持关闭 | 部署层 `REAL_DISPATCH_ENABLED` 不是 `true`；工作台“真实派发”面板中租户开关为关闭 |
| P3 | 保留期服务和告警评估服务都在运行 | `docker compose -p nexloop-app ps` 中 `retention-keeper`、`alert-evaluator` 都是 running |
| P4 | 负责人能登录工作台，并且是 owner | 负责人登录后，审批页可以打开 |
| P5 | 钥匙串里已经有第二位测试 owner 的口令条目（调度员生成，`nexloop/stage/approver2`） | `security find-generic-password -s nexloop/stage/approver2 >/dev/null`，只检查是否存在，不打印值 |
| P6 | **第 0 天开始之前**，D1（`0343_vote_cooldown`）已经部署到 stage。这样负责人的冷却从 D1 部署时刻起算，第二位 owner 的冷却从第 4 步起算 | `ro "select applied_at from control.schema_migrations where version='0343'"` 有值，并且早于第 0 天开始的时刻 |

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

# 第 0 天

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

**注意（B1 迁移）**：B1 的迁移会回溯校验已发布的审批策略：凡是声明了 `target_subject_field` 的策略，批准角色都必须恰好是 `{owner}`。只要有一条不合规，**整个迁移失败并回滚**，部署单在这一步停下。stage 上现有的策略（首批三条）都没有目标字段，不受影响；如果之后发布过带目标字段的策略，部署前先用第 7 步的只读查询核对。

迁移由第二十四批的部署单执行，本单只核对结果：

```
ro "select version from control.schema_migrations order by version desc limit 8"
ro "select to_regclass('control.nexloop_approval_policies') is not null, to_regclass('control.nexloop_approval_eligibility_events') is not null"
ro "select count(*) from control.nexloop_approval_policies"
```

- **预期**：迁移尾是 0355（release 46；其中 NX-056 为 0326–0332、0339 和 0342，D1 为 0343）；两张表都存在（`t|t`）；策略为 0 条，也就是此刻还没有任何能力需要审批。

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

2. 应用本批的角色清单（`workbench-roles.v1.json`，release 46 上为 manifest 46；94baecc3 时为 40）：

```
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/workbench_roles.py --roles /opt/release/deploy/authorization/workbench-roles.v1.json \
  --members /stage/workbench-members.json --apply --bootstrap-dsn-file /run/bootstrap-dsn --signing-key-file /stage/management/signing.key.hex --signing-key-id "$KEY_ID"
```

- **预期**：成功退出；再执行一次结果相同。
- **核对**：
  - `ro "select members ? 'stage-principal-approver2', members->>'stage-principal-approver2' from control.nexloop_workbench_configurations where tenant_id='$TENANT_ID' order by manifest_version desc limit 1"`，应为 `t|owner`；
  - 第二位 owner 用自己的账号登录工作台，审批页可以打开，三栏都为空，页面顶部没有单人模式提示。

## 5. 告警规则（DATA_HOST，配置员窗口，B）

release 46 起用 v18（74 条），它包含 v14 的全部审批规则。先用 doctor 看 stage 当前已发布的版本：已经是 v18 就跳过本步；低于 v18 就直接发布 v18（逐级超集，不必逐版发布；但一旦发布了 v18，就不能再发布更低的版本）。下面的命令把 v14 换成 v18，`--check` 预期为 `{"valid": true, "version": 18, "rules": 74}`。原文保留如下，供对照。

```
docker run $COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/alert_rules_apply.py --manifest /opt/release/deploy/configuration/alert-rules.v14.json --check
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/alert_rules_apply.py --manifest /opt/release/deploy/configuration/alert-rules.v14.json \
  --tenant "$TENANT_ID" --bootstrap-dsn-file /run/bootstrap-dsn
```

- **预期**：`--check` 输出 `{"valid": true, "version": 14, "rules": 67}`；应用后 `applied: true`。
- 下一轮告警评估之后（约 1 分钟），`nexloop-doctor --observability` 的 `alert_rules_current` 为 true。
- **前提**：镜像必须是 94baecc3 或更新的发布（含 `alert_rules_apply.py`）。没有就停下，不另找写入规则的办法。

## 6. 策略清单 `--check`（不连库，A）

release 46：四个文件依次校验（第 7 步也按这个顺序发布）。

```
for f in history/approval-policies.version1.json history/approval-policies.version2.json history/approval-policies.version3.json approval-policies.v1.json; do
  docker run $COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/approval_policies_apply.py --manifest /opt/release/deploy/configuration/$f --check
  sha256sum "$RUN/src/deploy/configuration/$f"
done
```

- **预期**（`version / expected_revision / policies`）：`1/0/3`、`2/1/8`、`3/2/10`、`4/3/11`，每个 `manifest_sha256` 与对应的 sha256sum 相同。四个 sha256 记入证据。

## 6a. 第 0 天末尾：只读核对并记录（A）

D1（NX-058 片 D 的投票冷却）规定：一位成员只有在“当前这段连续持有批准角色的起点”之后满 24 小时，投票才计入。起点取自片 C 的角色历史；读取函数为 `control.nexloop_workbench_vote_eligible_from(tenant, world, subject, roles)`，它返回起点加 24 小时。

```
ro "select now()"
ro "select m.principal_id, m.subject_id, control.nexloop_workbench_vote_eligible_from('$TENANT_ID','real',m.subject_id,array['owner']) \
    from control.nexloop_browser_memberships m where m.tenant_id='$TENANT_ID' and m.principal_id in ('stage-principal-approver2','stage-principal-approver3') order by 1"
ro "select min(recorded_at) from control.nexloop_workbench_role_history where tenant_id='$TENANT_ID' and source='deployment'"
```

**记入证据**（UTC 时间，不含口令和用户名）：

| 字段 | 来源 |
|---|---|
| 核对时刻 | `select now()` |
| approver3 的 `vote_eligible_from` | 第二条查询中 `stage-principal-approver3` 那一行（应为 2026-10-27 00:10Z 左右） |
| 第二位测试 owner 的 `vote_eligible_from` | 第二条查询中 `stage-principal-approver2` 那一行。以 P6 为前提（D1 在第 0 天之前已部署），它约等于第 4 步完成时刻加 24 小时；如果 D1 是在第 4 步之后才部署的，则为 D1 部署时刻加 24 小时 |
| D1 部署时刻 | 第三条查询：0343 部署时回填角色历史的那批行（`source='deployment'`）的 `recorded_at` |
| 第 1 天最早可开始的时刻 | 以上三个时刻（D1 部署时刻按加 24 小时计）中最晚的一个 |

- 任何一行的 `vote_eligible_from` 为空，说明这位成员在角色历史里没有当前的 owner 记录，回到第 4 步核对成员文件和角色清单是否已应用。
- 第 0 天到此结束，**不发布策略**。此时没有任何能力需要审批，stage 照常可用。

# 第 1 天

## 6b. 第 1 天开头：前提检查（A；任何一条不满足就停下）

```
ro "select now()"
ro "select m.principal_id, control.nexloop_workbench_vote_eligible_from('$TENANT_ID','real',m.subject_id,array['owner']) <= now() \
    from control.nexloop_browser_memberships m where m.tenant_id='$TENANT_ID' and m.principal_id in ('stage-principal-approver2','stage-principal-approver3') order by 1"
ro "select now()-min(recorded_at)>=interval '24 hours' from control.nexloop_workbench_role_history where tenant_id='$TENANT_ID' and source='deployment'"
ro "select count(*) from control.nexloop_approval_policies"
```

| # | 前提 | 预期 |
|---|---|---|
| Q1 | approver2 的投票资格已到（负责人不参与，不检查；approver3 只当发起方，发起时不检查冷却，它那一行只做记录） | 第二条查询中 `stage-principal-approver2` 那一行为 `t`；approver3 那一行的值照录 |
| Q2 | D1 部署已超过 24 小时 | 第三条查询为 `t` |
| Q3 | 第 0 天之后成员没有变化 | 两位都仍是 active 的 owner（§4.2 的核对查询）；`vote_eligible_from` 与第 0 天记录的一致。如果变了，说明角色有过中断，冷却已重新计时 |
| Q4 | 还没有发布任何策略 | 第四条查询为 0（如果不是 0，先查 `nexloop_approval_policy_publications` 已发到哪个版本，第 7 步从下一个版本接着发） |
| Q5 | 第 0 天的前提 P2、P3 仍然成立，并且第 0 天第 3–5 步的结果仍在 | 同 §0.1；第 3 步 Action 已发布（§3 的核对查询为 3）；告警规则为 v18、74 条（`nexloop-doctor --observability --alert-rules <v18 文件>` 的 `alert_rules_current` 为 true）；角色 manifest 为 46（§4.2 的核对） |

全部满足才继续第 7 步。

## 7. 发布策略 version 1 → 4（DATA_HOST，配置员窗口，B）

从这一步起，首批三个能力（打开真实派发、负责人发起的擦除、解除保全）和 NX-058 的 owner 成员类 Action 都需要另一位 owner 批准。

release 46：按顺序发布四个文件，每发一个先看输出再发下一个；某个文件被拒就停下，不跳版本：

```
for f in history/approval-policies.version1.json history/approval-policies.version2.json history/approval-policies.version3.json approval-policies.v1.json; do
  docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/approval_policies_apply.py \
    --manifest /opt/release/deploy/configuration/$f --tenant "$TENANT_ID" --bootstrap-dsn-file /run/bootstrap-dsn
done
# 最后对 approval-policies.v1.json 再执行一次（B，经配置员窗口，标为写入）：result.replayed 应为 true
```

- **预期**：每个都是 `applied: true`、`result.replayed: false`；`appended / unchanged` 依次为 `3/0`、`5/3`、`2/8`、`1/10`。
- 下面的核对按四次发布读：`nexloop_approval_policy_publications` 应有 4 行（version 1–4，`expected_revision` 0–3，sha256 与第 6 步一致）；策略共 11 条，首批三条的预期不变，成员类 8 条为 quorum 1、`{owner}`、86400、`not_applicable`。资格时间线首批三个能力的预期不变。
- **核对**：

```
ro "select manifest_version, manifest_sha256, expected_revision, appended from control.nexloop_approval_policy_publications"
ro "select capability, version, quorum, approver_roles, expires_after_seconds, solo_mode from control.nexloop_approval_policies order by 1"
ro "select capability, eligible_count from control.nexloop_approval_eligibility_events where tenant_id='$TENANT_ID' order by event_id"
```

- **预期**：
  - 第一行的 sha256 与第 6 步一致，`expected_revision=0`；
  - 三条策略都是 quorum 1、`{owner}`、86400、`allowed`；
  - 资格时间线每个能力一行，`eligible_count=3`（负责人、approver2、approver3）。

## 8. 双人演练（第 1 天，紧接第 7 步；工作台，B；至少 2 小时）

全部在合成租户进行，真实派发保持关闭（P2）。

**时间点**：8.1–8.5 在第 7 步之后立即连续进行（约 20 分钟）；8.6 紧接 8.5；8.7 在 8.6 发起超过 1 小时、告警出现之后。整段必须在第 1 天内完成，8.6 的请求不能拖过 24 小时（否则会过期，见 8.6）。

演练用的开关：action_type 层，`eios:action:nexloop.service.request:1`。租户开关和部署层开关都关着，所以即使这个开关打开，也不会有任何真实外发。

**原因文本写死**：判断“是不是同一请求”时只去掉 request_id，原因文本也算在内。所以三段演练各用一段固定的原因，彼此不同：

| 段 | 原因文本（原样输入） | 用途 |
|---|---|---|
| R1 | `演练一：批准与执行` | 8.1–8.4 |
| R2 | `演练二：撤回与冷却` | 8.5 |
| R3 | `演练三：等待告警` | 8.6–8.7 |
| W | `演练二撤回：不再需要` | 8.5 撤回时填写的原因（撤回必须写原因，1–500 字；不填会返回 `approval: invalid`） |

操作者一列（按 §D1R）：“approver3”是发起方，“approver2”是批准方，两者都是调度员操作的测试账号。**负责人账号全程不操作。**

**驱动脚本**（调度员，管理机；口令经钥匙串写入 0600 临时文件，由 trap 在退出时删除；`$WB` 是 stage 工作台的 https 源，只写 scheme 和主机）：

```
# 在仓库根目录执行；解释器用仓库的 .venv/bin/python（脚本需要 Python 3.11 及以上，只用标准库）
pw() { f=$(mktemp); chmod 600 "$f"; security find-generic-password -s "nexloop/stage/$1" -w > "$f"; echo "$f"; }
P3=$(pw approver3); P2=$(pw approver2)
trap 'rm -f "$P3" "$P2"' EXIT   # macOS 没有 shred；中途出错退出也会删掉两个口令文件
EV="${TMPDIR:-/tmp}/drill-evidence.jsonl"   # 证据文件不落进工作树；演练结束后作为证据归档
D3=(.venv/bin/python scripts/stage/approval_drill.py --base-url "$WB" --username "<approver3 用户名>" --account-label approver3 --password-file "$P3" --evidence-file "$EV")
D2=(.venv/bin/python scripts/stage/approval_drill.py --base-url "$WB" --username "<approver2 用户名>" --account-label approver2 --password-file "$P2" --evidence-file "$EV")
OPEN() { printf '{"scope_kind":"action_type","action_type":"eios:action:nexloop.service.request:1","enabled":%s,"data_mode":null,"reason":"%s"}' "$1" "$2"; }
# 先 dry-run 一次：不连网，只打印将要发的路径和字段名
"${D3[@]}" --dry-run request --operation set_real_dispatch --body-json "$(OPEN true '演练一：批准与执行')"
# …… 按下表逐步执行 ……
# 结束：退出 shell 时 trap 删除口令文件；也可以现在手动执行 rm -f "$P3" "$P2"
```

每一步输出一行 JSON（同时追加到 `$EV`，即 `$TMPDIR/drill-evidence.jsonl`，0600）：时间、步骤、账号标签、HTTP 状态、`approval_id`、`status`/`approval` 和固定原因码。不输出口令、用户名、Cookie、CSRF 或请求原因文本。退出码 0 = 2xx，1 = 被拒或做不成，2 = 参数错误。

| # | 命令 |
|---|---|
| 8.1 | `"${D3[@]}" request --operation set_real_dispatch --body-json "$(OPEN true '演练一：批准与执行')"` → 记下 `approval_id`（下称 A1） |
| 8.2a | `"${D3[@]}" list`（A1 只在 `mine`，不在 `to_decide`；`solo_mode.enabled` 为 `false`）；`"${D3[@]}" decide --approval-id A1 --decision approve --reason 自批` → 被拒：HTTP 409，`code` 为 `not_allowed_in_state`，`details_reason` 为 `self_approval`（见下方“原因码”说明） |
| 8.2 | `"${D2[@]}" list`（A1 在 `to_decide`）；`"${D2[@]}" decide --approval-id A1 --decision approve --reason '演练一：同意'` |
| 8.3 | `"${D3[@]}" execute --approval-id A1` → `approval: consumed` |
| 8.4 | `"${D3[@]}" request --operation set_real_dispatch --body-json "$(OPEN false '演练一：关闭')"` → 直接执行，没有 `approval_id` |
| 8.5 | `"${D3[@]}" request … "$(OPEN true '演练二：撤回与冷却')"` → A2；`"${D3[@]}" cancel --approval-id A2 --reason '演练二撤回：不再需要'`；再发同一 request → HTTP 409，`code` 为 `not_allowed_in_state`，`details_reason` 为 `cooldown`；同时 `mine` 里没有新的等待请求、审计里没有新的 `approval.requested` |
| 8.6 | `"${D3[@]}" request … "$(OPEN true '演练三：等待告警')"` → A3，等待超过 1 小时看告警 |
| 8.7 | `"${D2[@]}" decide --approval-id A3 --decision reject --reason '演练三：拒绝'` |

**原因码**（NX-056 follow-up，第四十批合入起）：工作台对审批路径的拒绝在原来的状态码之外带上固定原因码 `details.reason`，驱动脚本输出为 `details_reason`。状态码不变：
- 冷却：409 `not_allowed_in_state`，`details_reason=cooldown`；
- 自批：409 `not_allowed_in_state`，`details_reason=self_approval`；
- 被操作的成员参与批准：409，`target_cannot_approve`；单人模式下凑不够人数：409，`quorum_unreachable_in_solo_mode`；
- 非发起人撤回或执行：403 `forbidden`，`details_reason=not_requester`；不是有资格的批准人：403，`approver_unauthorized`。

**第 1 天（2026-10-27）实测**：自批和冷却都只返回 409 `not_allowed_in_state`，没有 `details_reason`。这是合入原因码**之前**的行为（当时 stage 的发布还没有这一片），当天按“409、`mine` 里没有新的等待请求、审计里没有新的 `approval.requested`”判定。第 2 天及以后、部署了含原因码的发布后的演练，按上面的原因码判定。

下表的“预期”不变；表中“负责人”一律读作 approver3，“测试 owner”读作 approver2。开关状态用只读查询核对，不用负责人登录看面板：`ro "select scope_kind, scope_ref, enabled from control.nexloop_real_dispatch_switches where tenant_id='$TENANT_ID' and world='real' order by 1,2"`。

| # | 谁 | 操作 | 预期 |
|---|---|---|---|
| 8.1 | 负责人 | 真实派发面板：打开 action_type `eios:action:nexloop.service.request:1`，原因 R1 | 页面提示“需要另一位成员批准”；审批页“我发起的”出现一条，状态“等待审批”，摘要显示 `scope_kind=action_type，enabled=true` |
| 8.2 | 测试 owner | 审批页“待我审批”：看到这条，写批准原因后批准 | 状态变为“已批准”；告警页出现 `approval_first_time_approver`（approver2 第一次批准，持续约 24 小时，属预期）；负责人自己的“待我审批”里没有这条（禁止自批） |
| 8.3 | 负责人 | 审批页“待我执行”：点“执行” | 提示“已按批准执行；这次批准已用掉”；真实派发面板中这个 Action 开关为打开 |
| 8.4 | 负责人 | 真实派发面板：关闭这个开关 | 立即生效，不需要审批（关闭不在策略条件内） |
| 8.5 | 负责人 | 8.4 之后立即：再次打开同一开关，原因 R2；在“我发起的”中撤回，撤回原因 W；然后立即再次以原因 R2 打开 | 第一次：状态变为“已撤回”。第二次：被拒绝，返回 `cooldown`（同一请求撤回后 1 小时冷却）。冷却只针对 R2，不影响下一步 |
| 8.6 | 负责人 | 8.5 之后立即：以原因 R3 打开同一开关，不审批 | 进入“等待审批”。超过 1 小时后，告警页出现 `approval_waiting`（“审批请求等待超过 1 小时”）。如果拖过 24 小时，请求会过期（`approval.expired`），这一段作废：换一段新的原因（例如 `演练三（重做）：等待告警`）从 8.6 重做 |
| 8.7 | 测试 owner | 告警出现后，拒绝 8.6 的请求 | 状态变为“已拒绝”；下一轮评估后 `approval_waiting` 解除 |

**审计核对**（8.3 之后，A）：

```
ro "select created_at, action, actor_id, payload->>'approval_id', payload->>'outcome', payload->>'actor', payload->'approved_by' from runtime.audit_events where tenant_id='$TENANT_ID' and action like 'approval.%' and created_at >= '<第 7 步完成时刻>' order by created_at"
```

操作者看 `actor_id` 列（`payload->>'actor'` 只有 `approval.consumed` 才有）。逐行预期：

| 步骤 | action | `actor_id` | 其他 |
|---|---|---|---|
| 8.1 | `approval.requested` | `stage-principal-approver3` | |
| 8.2 | `approval.decided` | `stage-principal-approver2` | outcome approved |
| 8.3 | `approval.consumed` | `stage-principal-approver3` | `payload.actor` 为 `stage-human-approver3`，`approved_by` 为 `["stage-human-approver2"]` |
| 8.5 | `approval.requested`、`approval.cancelled` | 都是 `stage-principal-approver3` | 冷却那一次是拒绝发起，不写审计 |
| 8.6 | `approval.requested` | `stage-principal-approver3` | |
| 8.7 | `approval.decided` | `stage-principal-approver2` | outcome rejected |

- 8.2a 的自批被拒不写 `approval.decided`。
- **负责人的 principal 和 subject 不得出现在任何一行**（`actor_id`、`payload.actor`、`approved_by` 都不行），出现就判失败。

**告警核对**（A）：

```
ro "select recorded_at, rule_id, kind, selector, value, rules_version from control.nexloop_alert_events where tenant_id='$TENANT_ID' and world='real' and rule_id in ('approval_waiting','approval_first_time_approver') and recorded_at >= '<第 7 步完成时刻>' order by recorded_at"
```

- **8.2 之后**：approver2 第一次投批准票，会出现 `approval_first_time_approver` 的 `firing`（v16 起有这条规则，统计最近 24 小时，所以约 24 小时后才 `resolved`）。这是预期内的，不是故障。
- **8.6**：A3 发起后约 61 分钟（等待超过 1 小时，加上一分钟一轮的评估），出现 `approval_waiting` 的 `firing`。
- **8.7 之后**：下一轮评估出现 `approval_waiting` 的 `resolved`。
- 两条告警的 `firing`、`resolved` 时间都从这条查询取，记入证据。

**证据**（记入 NX-056 任务证据，不含口令、DSN 和用户名；每条写明执行者和判定者）：

- 第 3、5、6、7 步命令的输出（JSON 行）；
- 第 7 步的 sha256；
- 8.1–8.7 每一步的页面状态截图或文字记录；
- 审计查询的结果；
- `approval_waiting` 告警的触发和解除时间；
- `$TMPDIR/drill-evidence.jsonl`（驱动脚本的输出）；
- 执行者：调度员（第 1–7 步、approver2 和 approver3 的全部操作）；负责人不参与；判定者：L6（只看证据）。

## 9. 回退

| 情况 | 做法 |
|---|---|
| 第 3–5 步失败 | 不继续往下。Action、角色和告警规则的发布都是幂等的，修正后重跑即可；在第 7 步之前，没有任何能力需要审批 |
| 第 7 步之后需要取消审批 | 按运行手册 §2“回退与停用”：发布下一个版本的清单（release 46 上为 version 5，`expected_revision: 4`），要停用的策略版本加 1、条件设成永远不会命中的常量，`decision` 中写明理由和批准人。命令与第 7 步相同。成员类策略没有条件字段，只能放宽（例如允许单人模式）而不能“设成不命中”，要停用先交调度员 |
| 第 1 天发现冷却未满（Q1 或 Q2 不满足） | 不发布策略，停下。此时没有任何能力需要审批，stage 不受影响。按 §6b 的查询算出最早可开始的时刻，到时再从 §6b 开始。如果 `vote_eligible_from` 比第 0 天记录的更晚（Q3），说明角色有过中断，先查清原因（成员文件是否被重新应用、是否有人降级后又提升），再按新的时刻等待。不要为了赶时间去改成员数据或角色历史 |
| 第二位 owner 不再需要 | 先停用策略（上一行），再按 run-assembly §2a `--revoke-member` 撤出。策略生效期间，不要让 owner 只剩一位（运行手册 §7） |
| 演练中真实派发开关状态异常 | 负责人关闭开关（关闭不需要审批），然后按 `docs/OPERATIONS.md` §8 核对 |
| 策略已发布、演练失败 | 按顺序：① 负责人关闭演练中打开过的开关（关闭不需要审批，立即生效）；② 测试 owner 拒绝、负责人撤回所有仍在等待的演练请求（如果测试 owner 的冷却还没满，他投的票不会计入，拒绝也无法生效，这时只能由负责人撤回）；③ 记录失败点和返回码（运行手册 §6），交 L6；④ 如果问题出在审批本身、短期修不好，按上面“取消审批”发布 v2。策略保持生效本身不影响业务，因为首批三个能力都是低频的 owner 操作 |
| 能不能撤回发布 | 第 3 步发布的 Action 和第 4 步写入的角色**不能撤回发布**：定义只追加（0050）。能做的只是撤销授权（`--revoke-member`）、停用策略（v2）。所以第 3、4 步执行前，要按 3.2 的 dry-run 和组批记录逐项核对 |

不允许的做法：直接写 SQL 改审批表、用 superuser 插入 approved 记录、代他人登录批准人账号（AGENTS.md 安全不变式）。
