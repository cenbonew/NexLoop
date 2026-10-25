# 执行单：NX-057 补救意图在 stage 上启用（授权 v19、补救规则 v1、告警规则 v15/v16、开关）

NX-057 R1/R2 随 release 44（main 94baecc3）上 stage。部署单只做迁移、Action 发布和角色清单；本单写的是部署之后的四件事，按顺序执行：

1. 告警规则 v15 → v16；
2. 服务授权 v19（给补救规划器的授权）；
3. 补救规则 v1；
4. 打开部署层开关 `REMEDIATION_ENABLED`，核对租户开关。

最后一节是回退。

- **依据**：
  - 设计稿 `docs/design/remediation-intents.md` 第 4 版（§6.1 规划器、§9.4 开关、§14 负责人决定 Q-1..Q-7）；
  - 实现说明 `docs/implementation/NX-057-R1-remediation-suggestions.md`、`NX-057-R2-plan-wake.md`；
  - 命令形态沿用 `run-assembly-20261015.md` §0.3、`run-release7-deploy-20261016.md` §3、`run-nx056-approvals-enable.md` §0.3。
- **按 main 94baecc3 核对过**：
  - 迁移：`remediation_intents`（0337）、`remediation_wake`（0338）；
  - 授权：`service-grants.v1.json` manifest 19；
  - 规则：`remediation-rules.v1.json` manifest 1，只有一条规则 `deliver_final_failure`；
  - 告警规则：v14 67 条，v15 71 条，v16 73 条。
  - 执行前如果 main 又前进了，重新核对这几项。
- 每一步标 **A**（只读，随时可以执行）或 **B**（写入，按顺序执行，执行前确认上一步通过）。
- **两个缺口**（§0.4）：本单第 3 步和第 4 步各缺一个部署件，main 94baecc3 上还没有。这两步写成补齐之后的形态，补齐之前在那里停下。第 1、2 步现在就能执行。

## 0. 前提、变量、缺口

### 0.1 前提（不满足就不开始）

| # | 前提 | 怎么核对 |
|---|---|---|
| P1 | release 44 已经按部署单部署，部署单记录了迁移前备份 | 部署单；`ro "select version from control.schema_migrations order by version desc limit 12"` 中有 0337、0338 |
| P2 | 本批的 Action 已经发布：`nexloop.remediation.resolve`、`nexloop.remediation.switch`、`nexloop.remediation.rule_release` | `ro "select resource_id from control.nexloop_action_definitions where active and resource_id like 'eios:action:nexloop.remediation.%' order by 1"`，应为 3 行。规划器的 `nexloop.remediation.plan:1` 是服务端口，不在业务 Action 清单里，这里查不到是正常的 |
| P3 | 本批的角色清单（`workbench-roles.v1.json` manifest 42）已经应用，owner 能看到工作台“运营 → Action / 异常”页的“补救”一栏 | 负责人登录后打开该页，“补救”一栏显示“当前没有待处理的补救” |
| P4 | 效应类别 v3 已经在本租户生效：`nexloop.enterprise.deliver:1` 被声明为外部效应 | `ro "select external_effect from control.nexloop_action_effect_externality where tenant_id='$TENANT_ID' and world='real' and resource_id='eios:action:nexloop.enterprise.deliver:1'"`，至少一行 `t`。没有就停下：第 3 步会以 `source_not_effect` 被拒 |
| P5 | stage 的真实派发保持关闭 | 部署层 `REAL_DISPATCH_ENABLED` 不是 `true`；工作台“真实派发”面板中租户开关为关闭 |
| P6 | `plan-reevaluator` 在运行 | `docker compose -p nexloop-app ps plan-reevaluator` 为 running |

### 0.2 变量

与 `run-assembly-20261015.md` §0.2、`run-nx056-approvals-enable.md` §0.2 相同：`TENANT_ID`、`MAIN`、`RUN`、`NEXLOOP_ROOT`、`KEY_ID`、`BACKEND_IMAGE`、`PG_CONTAINER`、`DB`。另外：

| 变量 | 含义 |
|---|---|
| `CREDENTIAL_EXPIRES_AT` | 上一次 register 用的值（部署单记录）。只在需要新建凭据时使用，本单预期不建凭据 |
| `LAST_REGISTER` | 上一次在 stage 执行 `stage_secrets.py register` 时用的 main 提交（部署单记录） |
| `RELEASE_CHECKOUT` | 管理机上的 release 检出（run-release7 §3.1 的同名变量），用于 2.1 的 diff |

变量值只在 shell 中设置，不进仓库、不进证据。

### 0.3 一次性容器、配置员窗口、只读会话

- **`$COMMON`、`$WINDOW`**：与 run-assembly §0.3 相同（DATA_HOST）。

```
COMMON="--rm --network none --read-only --user 999:999 --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  -v $RUN/src:/opt/release:ro -v $RUN:/stage:ro -e PYTHONPATH=/opt/release/packages/eios-core/src --entrypoint python"
WINDOW="-v $NEXLOOP_ROOT/run/postgresql:/var/run/postgresql -v $RUN/boot.dsn:/run/bootstrap-dsn:ro"
```

- **源码树**：`$RUN/src` 用 `$MAIN` 的 `git archive` 重新打包（`scripts/deploy deploy/authorization deploy/configuration packages/eios-core/src`），属主 999，做法同 run-assembly §0.4。
- **配置员窗口怎么开**：不需要手工开。
  - 本单的写入工具（`stage_secrets.py register`、`alert_rules_apply.py`）接受 `--bootstrap-dsn-file /run/bootstrap-dsn`，在自己的进程里开窗口（`scripts/deploy/configurator_window.py`）：
    1. 用 `$RUN/boot.dsn`（`nexloop_bootstrap`，经 PG 容器的本地 socket）登录；
    2. 给 `nexloop_configurator` 设一次性口令（只把 SCRAM 校验值发给 PG），有效期 10 分钟（`VALID UNTIL`）；
    3. 把配置员 DSN 写进容器内的私有临时文件，交给工具；
    4. 工具结束时，不论成功、被拒还是出错，都恢复成 NOLOGIN、无口令。
  - 同一时间只能开一个窗口。如果返回 `another configurator window is in use` 或 `a configurator window is open`，等上一个窗口关闭（最多 10 分钟）再重试，不要手工改 `nexloop_configurator`。
  - **连接身份**：写入时连库的是 `nexloop_configurator` 本身。受信配置拒绝 `SET ROLE` 和提权会话，所以不能用 bootstrap 或 superuser 代替。
- **只读 owner 会话 `ro`**：与 run-nx056 §0.3 相同。输出的第一行是 `set_config` 返回的租户 ID，不是查询结果。

```
ro() { docker exec -i "$PG_CONTAINER" psql -h /var/run/postgresql -U nexloop_bootstrap -d "$DB" -v ON_ERROR_STOP=1 -At \
  -c "begin read only" -c "set local role nexloop_owner" -c "select set_config('eios.tenant_id','$TENANT_ID',true)" -c "$1" -c "rollback"; }
```

### 0.4 两个缺口（main 94baecc3）

| # | 缺口 | 影响 | 需要的补件 |
|---|---|---|---|
| G1 | **补救规则没有配置员窗口入口**。`python -m nexloop_eios.remediation rules --apply` 只接受 `--database-url-file`（配置员 DSN）。stage 上没有常驻的配置员登录，窗口只能由工具自己打开；`scripts/deploy/` 里没有补救规则的包装脚本 | 第 3 步不能执行 | 一个与 `alert_rules_apply.py` 同形的 `scripts/deploy/remediation_rules_apply.py`：`--bootstrap-dsn-file` 开窗口，调用 `nexloop_eios.remediation.apply_rules`，加 `--check`，输出一行 JSON，不打印 DSN。属于代码改动，另行派工 |
| G2 | **`REMEDIATION_ENABLED` 还没有接入 compose**。`plan-reevaluator` 的环境变量只有公共锚点里的 `REAL_DISPATCH_ENABLED`，进程里读不到 `REMEDIATION_ENABLED`，部署层开关恒为关 | 第 4 步不能执行 | `deploy/stage/compose.app.example.yaml` 的 `plan-reevaluator` 增加 `REMEDIATION_ENABLED: "${REMEDIATION_ENABLED:-false}"`（只给这一个服务）；`compose.app.env.example` 增加 `REMEDIATION_ENABLED=false`；同时跑所有涉及 compose 文件的测试（含 `test_stage_compose`）。属于部署配置变更，按特性分支走，需要负责人确认 |

**补救规则 `--doctor`、服务授权 `--doctor` 也受 G1 同样的限制**：`python -m nexloop_eios.service_grants --doctor` 读库也要配置员 DSN（`configurator_connection`）。所以本单的只读核对用 `ro` 查询代替 `--doctor`，见 2.1。

## 1. 告警规则 v15 → v16（DATA_HOST，配置员窗口，B）

### 1.1 三个版本的关系

| 版本 | 来源 | 条数 | 与上一版的差别 |
|---|---|---|---|
| v14 | L3，NX-056 | 67 | v13 加 6 条审批告警 |
| v15 | L7，NX-057 | 71 | v14 原样保留，加 4 条：`remediation_waiting`、`remediation_rate_capped`、`remediation_paused`（warning），`remediation_rule_held`（critical） |
| v16 | L3，第二十七批 | 73 | v15 原样保留，加 2 条：`approval_first_time_approver`、`approval_same_person_flag`（warning）。L3 起草时也叫 v15，组批时改为 v16 |

- 三个版本是逐级超集：没有删除，也没有修改任何一条已有规则（已用脚本逐条比对 v14→v15→v16）。
- **版本只能增加**：`control.nexloop_configure_alert_rules` 拒绝不大于当前版本的新版本（`alert rules version must increase`）。同版本同内容重放返回 `replay: true`。
  - 所以一旦发布了 v16，就再也不能发布 v15。
  - 也可以从 v14 直接发布 v16，结果与先 v15 后 v16 相同。
- **建议顺序**：先 v15，看一轮评估，再 v16。两步之间不需要等其他步骤。
  - 如果 stage 当前还停在 v13（run-nx056 第 5 步没有做），可以跳过 v14 直接 v15、v16。v14 的规则都包含在里面。
- 新增的 6 条规则读的指标（`remediation.*`、`approvals.first_time_approvers_24h`、`approvals.same_person_flags_24h`）都由 release 44 的迁移提供。规划器和规则还没有生效时，这些值都是 0，不会产生告警。

### 1.2 命令

```
ro "select version, digest from control.nexloop_alert_rules where tenant_id='$TENANT_ID' order by version desc limit 1"     # A：当前版本
for V in 15 16; do docker run $COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/alert_rules_apply.py \
  --manifest /opt/release/deploy/configuration/alert-rules.v$V.json --check; done                                          # A
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/alert_rules_apply.py \
  --manifest /opt/release/deploy/configuration/alert-rules.v15.json --tenant "$TENANT_ID" --bootstrap-dsn-file /run/bootstrap-dsn   # B
# 约 1 分钟后（一轮评估），再执行：
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/alert_rules_apply.py \
  --manifest /opt/release/deploy/configuration/alert-rules.v16.json --tenant "$TENANT_ID" --bootstrap-dsn-file /run/bootstrap-dsn   # B
```

- **`--check` 预期**：`{"valid": true, "version": 15, "rules": 71}`，`{"valid": true, "version": 16, "rules": 73}`。
- **应用预期**：`applied: true`，`result.replay: false`。重跑同一版本是 `replay: true`。
- **核对**：下一轮评估之后，`nexloop-doctor --observability --alert-rules <v16 文件> --metrics-database-url-file <metrics DSN>` 的 `alert_rules_current` 为 true（doctor 比对的是传入文件的版本，所以要传 v16）。
- **失败时**：`alert rules version must increase` 说明库里已经是更高版本，核对第一条查询的结果，不要改版本号重发。

## 2. 服务授权 v19（DATA_HOST，配置员窗口，B）

### 2.0 为什么在 stage 上用 `register`，不直接用 `service_grants --apply`

- stage 的服务授权由 `stage_secrets.py register` 管理。它把 `service-grants.v1.json` 与 `stage-principals.v1.json` 合并，加上 `owner-property-restrictions.json`，再调用同一个 `nexloop_eios.service_grants.apply`（run-34 §1、run-release7 §3）。
- **不要在 stage 上执行 `python -m nexloop_eios.service_grants --apply`**：
  - 它只看 `service-grants.v1.json`；
  - 对清单里有的主体，凡是库里有、清单里没有的授权，都会撤销（`plan` 中的 revocation）；
  - stage 清单给 `message_relay` 加了一条 `eios:action:NexLoop.queue.operations:1`，不在 service-grants 里，直接 `--apply` 会把它撤掉；
  - 另外，它不带 `--owner-restrictions` 时，属性规则的核对也与 register 不同。
- 两条路径的连接身份、签名密钥和 service-secrets 都一样：

| 输入 | 来源 | 在容器里的位置 |
|---|---|---|
| 连接身份 | `nexloop_configurator`，由窗口用 `$RUN/boot.dsn`（`nexloop_bootstrap`，本地 socket）临时打开（§0.3） | `--bootstrap-dsn-file /run/bootstrap-dsn` |
| 签名密钥 | `$RUN/management/signing.key.hex`：管理机上 `stage_secrets.py generate` 生成的管理树中，配置员用的共享签名密钥 hex 副本，经 scp 放到 `$RUN`（0600，属主 999）。签名密钥 ID 是 `$KEY_ID`，与上次 register、publish 用的相同 | register 自己读 `/stage/management/signing.key.hex`；直接用 CLI 时是 `--signing-key-file` 和 `--signing-key-id` |
| service-secrets | `$RUN/management/service-secrets.json`：同一棵管理树里，按凭据引用名存放的服务令牌（0600，属主 999）。只在需要**新建**凭据时读取其中的值；register 还会核对每个主体都有令牌 | register 自己读 `/stage/management/service-secrets.json`；直接用 CLI 时是 `--service-secrets-file` |

- 管理树和 `$RUN` 必须一致。先按 run-release7 §3.0 核对，`service-secrets.json` 和 `setup/` 下的文件必须来自管理机的同一棵树。

### 2.1 只读核对（A，代替 `--doctor`）

```
git -C "$RELEASE_CHECKOUT" diff "$LAST_REGISTER" "$MAIN" -- deploy/authorization/service-grants.v1.json deploy/authorization/stage-principals.v1.json deploy/authorization/owner-property-restrictions.json
docker run $COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/stage_secrets.py register --out /stage --tenant "$TENANT_ID" --dry-run
ro "select entity_key, jsonb_array_length(payload->'grants') from authz.nexloop_authority_facts where tenant_id='$TENANT_ID' and fact_kind='grants'
    and entity_key[1]='nexloop-service-plan-reevaluator-principal' order by entity_key"
ro "select binding->'requested_scopes' from authz.nexloop_service_credentials where tenant_id='$TENANT_ID'
    and binding->>'subject_principal_id'='nexloop-service-plan-reevaluator-principal'"
ro "select count(*) from runtime.nexloop_effect_failures where tenant_id='$TENANT_ID' and world='real'
    and failure_id not in (select failure_id from runtime.nexloop_remediation_scans)"
```

- **diff 预期**：如果 `$LAST_REGISTER` 是第二十四批或之后的提交，只有 `service-grants.v1.json` 的 v18→v19：`manifest_version` 改为 19，`decision` 末尾加 v19 一段，`grants` 加一行 `plan_reevaluator action eios:action:nexloop.remediation.plan:1 execute`。还有别的差异就停下，报给出这份执行单的人。
- **dry-run 预期**：`grants` 里有 `plan_reevaluator action eios:action:nexloop.remediation.plan:1 execute`；主体数和凭据文件数与上次相同。dry-run 不连库，看不出与库的差异。
- **授权查询预期**：plan_reevaluator 现在有 `NexLoop.feed.plan-reevaluate:1`、`nexloop.plan.reevaluate:1` 两行，长度都是 1；还没有 `nexloop.remediation.plan:1`。
- **scope 预期**：`["action.execute"]`。新授权也是 `action.execute`，所以 scope 集合不变，不会出现 `credential_rotation_required`。
- **最后一条**：现在还没有扫描过的最终失败数，记下来，2.3 要用。

### 2.2 正式执行（B）

```
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/stage_secrets.py register --out /stage --tenant "$TENANT_ID" \
  --bootstrap-dsn-file /run/bootstrap-dsn --signing-key-id "$KEY_ID" --credential-expires-at "$CREDENTIAL_EXPIRES_AT"
```

- **预期**：
  - `changed: true`，`grants_added: 1`；
  - `grants_revoked: 0`，`credentials_created: []`，`extra_grants_outside_manifest: 0`；
  - `message_read_rules_written: 0`，`role_source_read_rules_written: 0`。
- 数字不符就停下：说明库里的授权与清单在这次之前就有偏差。记下输出，报给出这份执行单的人。
- **被拒时**：`credential_rotation_required` 说明有凭据的 scope 变了，不要重试，停下报告。
- **再执行一次**：`changed: false`，`grants_added: 0`。
- **核对**：重跑 2.1 的授权查询，多出 `{nexloop-service-plan-reevaluator-principal,eios:action:nexloop.remediation.plan:1}`，长度为 1。

### 2.3 授权生效之后会发生什么（A，观察）

- 规划器和重评估在同一个进程里，每分钟最多一轮，每轮最多处理 50 条失败。
- 部署层开关此时还是关的（G2 补齐前恒为关），所以每一条还没扫描过的最终失败都会被记为 `switch_closed`，**包括授权之前就存在的历史失败**。
  - 这是有意的设计（设计稿 §9.4，L6 M-5）：开关关着期间的失败，以后开了也不补生成。
  - 打开开关之前先把积压清空，打开之后只处理新的失败，不会一下子为历史失败生成一批补救。
- **核对**：等待（2.1 记下的数 ÷ 50）分钟以上，然后：

```
ro "select decision, count(*) from runtime.nexloop_remediation_scans where tenant_id='$TENANT_ID' group by 1 order by 1"     # 只有 switch_closed
ro "select count(*) from runtime.nexloop_effect_failures where tenant_id='$TENANT_ID' and world='real'
    and failure_id not in (select failure_id from runtime.nexloop_remediation_scans)"                                    # 0
ro "select count(*) from runtime.nexloop_remediation_intents where tenant_id='$TENANT_ID'"                                   # 0
```

- 没有失败记录时，第一条查询为空，也是正常的。
- 授权之前，规划器每轮返回 `unavailable`，什么也不写；重评估照常进行。后台服务的日志只打印整数和字符串字段，看不到规划器的结果，所以用上面的查询判断。

## 3. 补救规则 v1（DATA_HOST，配置员窗口，B；**G1 补齐后才能执行**）

- **内容**：
  - 一条规则 `deliver_final_failure`：源 Action `nexloop.enterprise.deliver:1`；两种失败（`origin_expired`、`confirmed_not_delivered`）；方式 `reevaluate`，`automatic`；期限 7 天。
  - 限额：每租户每小时 20 条；每客户 24 小时 3 条；每个计划 7 天唤醒 2 次；24 小时 3 次触发熔断。
  - 负责人 2026-10-25 决定：Q-1..Q-7 全部按推荐。
- **前提**：P4（效应类别 v3）。源 Action 必须是本租户声明的外部效应，否则返回 `source_not_effect`。`nexloop.service.request:1` 被声明为本地效应，所以不在 v1 里。
- **为什么必须在第 4 步之前**：规划器打开以后，没有规则时，每条失败都会被**永久**记为 `no_rule`，规则发布后也不会再处理。

```
docker run $COMMON "$BACKEND_IMAGE" -m nexloop_eios.remediation rules --manifest /opt/release/deploy/configuration/remediation-rules.v1.json --check   # A
sha256sum "$RUN/src/deploy/configuration/remediation-rules.v1.json"                                                                                    # A
# G1 补齐后（形态同 alert_rules_apply.py）：
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/remediation_rules_apply.py \
  --manifest /opt/release/deploy/configuration/remediation-rules.v1.json --tenant "$TENANT_ID" --bootstrap-dsn-file /run/bootstrap-dsn          # B
```

- `$COMMON` 的 entrypoint 是 `python`，所以 `-m` 可以直接跟在镜像后面。
- **`--check` 预期**：`{"valid": true, "manifest_version": 1, "rules": 1}`。把 sha256 记入证据。
- **应用预期**：`configured: true`，`manifest_version: 1`，`replay: false`，`rules: 1`。重跑是 `replay: true`。
- **被拒时**：
  - `source_not_effect`：回到 P4；
  - `version_must_increase`：库里已有不同内容的 v1 或更高版本，用下面的查询核对，不要改版本号重发；
  - `remediation rules are trusted configuration only`：连接身份不是配置员，检查是否经窗口执行。
- **核对**：

```
ro "select manifest_version, digest, configured_by from control.nexloop_remediation_rules where tenant_id='$TENANT_ID' and world='real'"
```

  预期为一行：`1|<digest>|nexloop_configurator`。

## 4. 打开开关（B；部署层开关**在 G2 补齐后才能执行**）

一共有三道开关，全部打开时规划器才生成补救（设计稿 §9.4）：

| 开关 | 默认 | 谁控制 | 生效时间 |
|---|---|---|---|
| 部署层 `REMEDIATION_ENABLED` | 关（只有精确的 `true` 才算打开） | 调度员，私有 compose env | 重建 `plan-reevaluator` 后的下一轮 |
| 租户开关 `remediation.switch` | **开**（没有任何记录时为开；最新一条记录是暂停时才关） | owner，工作台“运营 → Action / 异常 → 补救” | 下一轮（≤1 分钟） |
| 租户控制暂停（NXC01，租户范围 `*`） | 不暂停 | owner，工作台暂停/恢复 | 下一轮 |

### 4.1 打开前的核对（A）

```
ro "select count(*) from runtime.nexloop_effect_failures where tenant_id='$TENANT_ID' and world='real'
    and failure_id not in (select failure_id from runtime.nexloop_remediation_scans)"                                  # 0（2.3）
ro "select manifest_version from control.nexloop_remediation_rules where tenant_id='$TENANT_ID' and world='real'"     # 1（第 3 步）
ro "select revision, paused, recorded_at from control.nexloop_remediation_switches where tenant_id='$TENANT_ID' and world='real' order by revision desc limit 1"
ro "select paused from control.nexloop_control_scopes where tenant_id='$TENANT_ID' and world='real' and scope_kind='tenant' and scope_ref='*'"
```

- 第三条为空，或者最新一条 `paused=f`：租户开关是开的。最新一条是 `paused=t`：在 4.3 恢复。
- 第四条为空，或者为 `f`：没有租户级控制暂停。如果是 `t`，说明整个租户被暂停了，先弄清楚原因，不要为了补救去解除。

### 4.2 部署层开关（APP_HOST，B）

1. 在 APP_HOST 的私有 compose env 中，把 `REMEDIATION_ENABLED=false` 改为 `REMEDIATION_ENABLED=true`。这一行由 G2 加入，值只能是小写的 `true`。
2. 只重建这一个服务：

```
sudo docker compose -p nexloop-app -f $NEXLOOP_ROOT/compose.app.yaml --env-file $NEXLOOP_ROOT/config/compose.app.env \
  --env-file $NEXLOOP_ROOT/config/compose.images.env up -d --no-deps plan-reevaluator
sudo docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$(sudo docker compose -p nexloop-app ps -q plan-reevaluator)" | grep '^REMEDIATION_ENABLED='
```

- compose 文件和两个 env 文件的位置与 run-42 §7.2 相同；私有 compose env 就是其中的 `compose.app.env`。

- **预期**：`REMEDIATION_ENABLED=true`。这个变量不是秘密，可以打印。
- 其他服务读不到这个变量，所以不需要重建：负责人手动唤醒走 API，本来就不看部署层开关。

### 4.3 租户开关（工作台，owner，B）

- 4.1 显示租户开关是开的：什么都不用做。
- 显示是暂停的：负责人在“运营 → Action / 异常 → 补救”点“恢复补救”，填原因后提交。页面摘要不再显示“补救已暂停”，下一轮评估后 `remediation_paused` 告警解除。

### 4.4 打开后的核对（A）

打开之后的下一个最终失败，扫描结果不再是 `switch_closed`：

```
ro "select decision, count(*) from runtime.nexloop_remediation_scans where tenant_id='$TENANT_ID' and scanned_at > '<4.2 的时间>' group by 1"
ro "select state, kind, last_code, count(*) from runtime.nexloop_remediation_intents where tenant_id='$TENANT_ID' group by 1,2,3"
```

- `nexloop.enterprise.deliver:1` 的最终失败预期为 `generated`，其他 Action 的失败为 `no_rule` 或 `not_eligible`。
- stage 的真实派发是关的，最终失败很少，打开之后一段时间内两条查询都为空是正常的。本单不制造失败；演练另行安排。
- 工作台“补救”一栏显示待处理条数；告警：
  - 有补救等待超过 24 小时：`remediation_waiting`；
  - 超过每小时上限：`remediation_rate_capped`；
  - 规则被熔断：`remediation_rule_held`（critical），由负责人在同一栏“解除熔断”。
- R4（下一批，`l7/nx057-r4`）会补上认领终态、`outcomes.remediation` 计数和证据抽屉。在那之前打开也可以，只是工作台还没有单条补救的证据页。

**证据**（记入 NX-057 任务证据，不含 DSN、令牌和口令；每条写明执行者和判定者）：

- 第 1–3 步每条命令的输出（JSON 行）；
- 2.1 的 diff 摘要、2.2 的计数；
- 第 3 步规则文件的 sha256；
- 2.3、4.1、4.4 查询的结果；
- 4.2 的时间。

## 5. 回退

| 情况 | 做法 | 说明 |
|---|---|---|
| 要马上停止生成补救 | 负责人在工作台“补救”点“暂停补救”，填原因 | 最快的办法，下一轮生效，不需要重建任何服务。会触发 `remediation_paused` 告警（warning），这是预期的。暂停也会挡住负责人手动唤醒 |
| 关掉部署层 | 私有 compose env 改回 `REMEDIATION_ENABLED=false`，执行 4.2 的两条命令，`grep` 应为 `=false` | 只停规划器；负责人手动唤醒仍然可以用（它只看租户开关和控制暂停） |
| 关着期间的失败 | 不处理 | 被记为 `switch_closed`，重新打开后**不会补生成**。需要跟进的，由负责人按失败记录手动处理 |
| 已经生成的补救 | 负责人在“补救”一栏逐条“标记已处理”或“取消” | 关开关不会改变已有补救。`proposed` 状态的补救到期（7 天）后自动变为 `expired` |
| 某条规则不该生效 | 发布 `remediation-rules.v2.json`：`manifest_version: 2`，去掉这条规则（`rules` 可以为空），`decision` 写明理由。命令同第 3 步 | 规则只追加不删除，版本只能增加；v1 的记录保留。需要先评审 v2 文件 |
| 规则被熔断 | 负责人确认原因后，在“补救”一栏“解除熔断” | 解除后，从下一个失败开始重新生成 |
| 撤销规划器的授权 | 不在本单范围 | 要改清单（v20 去掉这条授权），然后重跑 register。plan 会把这条授权清空。开关已经足够停用，一般不需要撤销 |
| 告警规则 | 不回退 | 告警只写告警状态，没有外部效应。版本只能增加；如果确实要去掉某条，发布 v17 并另行评审 |

**不允许的做法**：

- 直接写 SQL 改补救表、扫描表或开关表；
- 用 superuser 或 bootstrap 冒充配置员；
- 手工改 `nexloop_configurator` 的登录属性；
- 在 stage 上用 `python -m nexloop_eios.service_grants --apply` 代替 register（§2.0）。
