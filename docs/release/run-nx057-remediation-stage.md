# 执行单：NX-057 补救意图在 stage 上启用（授权 v19/v20、补救规则 v1、告警规则 v15–v18、开关）

NX-057 的 R0–R4 与 G1、G2 都随 release 46（main 4426c5d0）上了 stage。部署单只做迁移、Action 发布和角色清单；本单写的是部署之后的四件事，按顺序执行：

1. 告警规则发布到 v18（stage 已经是 v18 或更高就跳过；2026-10-27 stage 为 v19，本步只核对）；
2. 服务授权到 v20：v19 给补救规划器授权，v20 是 NX-054 P3 的 `relation_backfiller`，同一次 register 一起应用；
3. 补救规则 v1；
4. 打开部署层开关 `REMEDIATION_ENABLED`，核对租户开关。

最后一节是回退。

第 1 步（告警规则）和第 2 步（授权）互不依赖，可以互换顺序：新增告警读的指标在规划器生效前都是 0（§1.1）。第 3 步必须在第 4 步之前（§3、§4.1 的停止条件）。

- **依据**：
  - 设计稿 `docs/design/remediation-intents.md` 第 4 版（§6.1 规划器、§9.4 开关、§14 负责人决定 Q-1..Q-7）；
  - 实现说明 `docs/implementation/NX-057-R1-remediation-suggestions.md`、`NX-057-R2-plan-wake.md`；
  - 命令形态沿用 `run-assembly-20261015.md` §0.3、`run-release7-deploy-20261016.md` §3、`run-nx056-approvals-enable.md` §0.3。
- **按 main 575859f7（release 50）核对过**（2026-10-27 刷新；上一版按 main 4426c5d0（release 46），第一版按 main 94baecc3）。4426c5d0→575859f7 之间，下面这些项目、`scripts/deploy/` 中本单用到的三个脚本的参数、`configurator_window.py`、compose 和 env 示例都没有变化；只多了告警规则 v19–v21 和 ADR-040（文档）：
  - 迁移（main 上的正式编号）：`remediation_intents` 0337、`remediation_wake` 0338、`remediation_claims` 0345（R4）、`remediation_rule_publications` 0351（G1）；
  - 授权：`service-grants.v1.json` manifest 20，`stage-principals.v1.json` manifest 5；角色清单 `workbench-roles.v1.json` manifest 46；
  - 规则：`remediation-rules.v1.json` manifest 1，只有一条规则 `deliver_final_failure`；
  - 告警规则：v14 67 条，v15 71 条，v16 73 条，v17 74 条，v18 74 条；main 上另有 v19 76 条、v20 77 条、v21 78 条，stage 当前为 v19（第 1 步跳过，见 §1.0）；
  - G1：`scripts/deploy/remediation_rules_apply.py`；G2：`deploy/stage/compose.app.example.yaml` 中 `plan-reevaluator` 的 `REMEDIATION_ENABLED`，env 示例 `deploy/stage/compose.app.env.example` 默认 `false`。
  - 执行前如果 main 又前进了，重新核对这几项。
- 每一步标 **A**（只读，随时可以执行）或 **B**（写入，按顺序执行，执行前确认上一步通过）。
- **G1、G2 已在 release 46 中**（§0.4）：四步都可以用 release 46 或之后的 release 执行，本次按 release 50。下文的“release 46”指这些部署件首次进入的 release，release 50 同样满足。
- **谁执行**：见 §0.5。

## 0. 前提、变量、G1/G2

### 0.1 前提（不满足就不开始）

| # | 前提 | 怎么核对 |
|---|---|---|
| P1 | release 46 已经按部署单部署，部署单记录了迁移前备份 | 部署单；`ro "select version from control.schema_migrations where version in ('0337','0338','0345','0351') order by 1"` 为 4 行 |
| P2 | 本批的 Action 已经发布（先在只读会话里试跑一次这条查询，确认能执行，再看结果）：`nexloop.remediation.resolve`、`nexloop.remediation.switch`、`nexloop.remediation.rule_release` | `ro "select resource_id from control.nexloop_action_definitions where active and resource_id like 'eios:action:nexloop.remediation.%' order by 1"`，应为 3 行。规划器的 `nexloop.remediation.plan:1` 是服务端口，不在业务 Action 清单里，这里查不到是正常的 |
| P3 | （先按 §0.5 确认 approver3 是 active owner）角色清单（`workbench-roles.v1.json` manifest 42 起含补救的三个 Action；release 46 为 manifest 46）已经应用，owner 能看到工作台“运营 → Action / 异常”页的“补救”一栏 | 负责人登录后打开该页，“补救”一栏显示“当前没有待处理的补救” |
| P4 | 效应类别 v3 已经在本租户生效：`nexloop.enterprise.deliver:1` 被声明为外部效应 | `ro "select external_effect from control.nexloop_action_effect_externality where tenant_id='$TENANT_ID' and world='real' and resource_id='eios:action:nexloop.enterprise.deliver:1'"`，至少一行 `t`。没有就停下：第 3 步会以 `source_not_effect` 被拒 |
| P5 | 真实派发三层开关的实际状态已读出并记入证据（2026-10-28 调度员核实：stage 上三层都开着，release 53+54 的门禁按此预期 20/21） | 读法同 run-outreach-slice5-stage P8：部署层只记 `REAL_DISPATCH_ENABLED` 是 `true` 还是 `false`，租户层与 Action 层看 `control.nexloop_real_dispatch_switches`。补救规则 v1 只有一条手动升级规则（`deliver_final_failure`），补救本身不发任何东西，所以开关开着不是停止条件；开着时最终失败会比关着时多，4.4 的查询可能不为空 |
| P6 | `plan-reevaluator` 在运行 | `docker compose -p nexloop-app ps plan-reevaluator` 为 running |

### 0.2 变量

与 `run-assembly-20261015.md` §0.2、`run-nx056-approvals-enable.md` §0.2 相同：`TENANT_ID`、`MAIN`、`RUN`、`NEXLOOP_ROOT`、`KEY_ID`、`BACKEND_IMAGE`、`PG_CONTAINER`、`DB`。另外：

| 变量 | 含义 |
|---|---|
| `CREDENTIAL_EXPIRES_AT` | 上一次 register 用的值（部署单记录）。只在需要新建凭据时使用，本单预期不建凭据 |
| `LAST_REGISTER` | 上一次在 stage 执行 `stage_secrets.py register` 时用的 main 提交（部署单记录） |
| `RELEASE_CHECKOUT` | 管理机上的 release 检出（run-release7 §3.1 的同名变量），用于 2.1 的 diff |

变量值只在 shell 中设置，不进仓库、不进证据。例外：`$LAST_REGISTER` 是一个公开的 main 提交哈希，要记进证据（2.1 的 diff 预期依赖它不早于第二十四批）。

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

### 0.4 G1、G2（已在 release 46 中）

main 94baecc3 上缺这两个部署件，第二十九批补齐（分支 `l7/nx057-g1-g2`），已在 release 46 中。下表留作记录。

| # | 原来的缺口 | 影响 | 补件（已就绪） |
|---|---|---|---|
| G1 | **补救规则没有配置员窗口入口**。`python -m nexloop_eios.remediation rules --apply` 只接受 `--database-url-file`（配置员 DSN）。stage 上没有常驻的配置员登录，窗口只能由工具自己打开；`scripts/deploy/` 里没有补救规则的包装脚本 | 没有它，第 3 步不能执行 | `scripts/deploy/remediation_rules_apply.py`，与 `alert_rules_apply.py` 同形：`--bootstrap-dsn-file` 开窗口，`--check` 不连库，输出一行 JSON，不打印 DSN。迁移 `remediation_rule_publications`（main 上为 0351）：按文件原文发布，库里记录文件的 sha256 和 `expected_revision`（发布前本租户的规则版本，第一次为 0；不符就拒绝，什么也不写）；配置员不能再绕过这条记录直接写规则。`python -m nexloop_eios.remediation rules --apply` 也改走这条路径，需要 `--expected-revision` |
| G2 | **`REMEDIATION_ENABLED` 还没有接入 compose**。`plan-reevaluator` 的环境变量只有公共锚点里的 `REAL_DISPATCH_ENABLED`，进程里读不到 `REMEDIATION_ENABLED`，部署层开关恒为关 | 没有它，第 4 步不能执行 | `deploy/stage/compose.app.example.yaml` 的 `plan-reevaluator` 增加 `REMEDIATION_ENABLED: "${REMEDIATION_ENABLED:-false}"`，只给这一个服务，公共锚点里的 `REAL_DISPATCH_ENABLED` 照旧；env 示例 `deploy/stage/compose.app.env.example` 增加 `REMEDIATION_ENABLED=false`。默认值 false 由 `tests/test_stage_compose.py::test_the_remediation_switch_is_one_compose_variable_of_the_plan_reevaluator_closed_by_default` 锁住：租户开关默认是开的，部署层必须默认关（L6 I-3）。属于部署配置变更，合并时按 NX-033 走 ack-policy |

**服务授权 `--doctor` 仍然没有窗口入口**（不在 G1 范围内）：`python -m nexloop_eios.service_grants --doctor` 读库也要配置员 DSN（`configurator_connection`）。所以本单的只读核对用 `ro` 查询代替 `--doctor`，见 2.1。

### 0.5 执行者（release 50 时的分工）

| 步骤 | 执行者 | 账号 |
|---|---|---|
| P1、P2、P4、P5、P6，第 1–3 步，2.1/2.3/4.1/4.4 的查询，4.2 部署层开关 | 调度员 | 管理机、DATA_HOST、APP_HOST 的运维账号；配置员窗口由工具自己打开，不需要工作台账号 |
| P3（看“补救”一栏）、4.3（只在租户开关被暂停时恢复）、§5 中工作台上的回退动作（暂停/恢复补救、标记已处理/取消、解除熔断） | 合成 owner **approver3**（`stage-principal-approver3`）；判定者是调度员 | 工作台账号，口令在钥匙串 `nexloop/stage/approver3`。负责人本人不需要操作，也可以代替 approver3 |

- **不需要第二位 owner 批准**：已发布的审批策略（`approval-policies.v1.json` version 4）覆盖的是 `real_dispatch.set`（只在打开时）、`consumer.erase`、`retention.release_hold` 和 NX-058 的 `workbench_member.*` 全部 8 个能力（promote_owner、demote_owner、suspend_owner、resume_owner、revoke_owner、issue_owner_reset、invite_owner、erase_member），不包括 `nexloop.remediation.resolve`、`nexloop.remediation.switch`、`nexloop.remediation.rule_release`。所以本单的工作台动作提交后立即执行，不进入“审批”页，approver2 不需要参与。
- **为什么 approver3 可以**：角色清单 manifest 46 只把这三个补救 Action 给了 `owner` 角色，approver3 是 owner（run-nx056 §0 的账号表）；补救 Action 没有针对负责人 principal 的条件。
- 本单所有写“负责人在工作台……”的地方，都按这张表由 approver3 执行。
- **以 stage 库里已应用的策略为准**（不看 main 上的文件）。执行前核对（A，参照 run-nx056 §7 的核对查询）：

```
ro "select max(manifest_version) from control.nexloop_approval_policy_publications"                                   # 5（release 53 的 S8 之后；之前为 4）
ro "select count(*) from control.nexloop_approval_policies where capability like 'remediation%' or capability like '%remediation%'"   # 0
```

  - 预期第一条为 `5`（release 53 的 S8 应用了 version 5，只新增 `goals.budget.loosen`；在那之前为 `4`），第二条为 `0`。
  - **停止条件**：第一条大于 5，或者第二条不是 0，说明 stage 已应用的策略可能覆盖补救动作。停下，按 stage 上的策略补上 approver2 的批准步骤，再继续。
- **approver3 的状态**（A，在 P3 之前执行）：确认 approver3 仍是 active owner，没有被暂停、撤销或降级。

```
ro "select m.payload->>'status', (select string_agg(distinct a.payload->>'status',',') from control.nexloop_browser_accounts a
      where a.tenant_id=m.tenant_id and a.subject_id=m.subject_id),
    (select c.members->>m.principal_id from control.nexloop_workbench_configurations c where c.tenant_id=m.tenant_id order by c.manifest_version desc limit 1)
    from control.nexloop_browser_memberships m where m.tenant_id='$TENANT_ID' and m.principal_id='stage-principal-approver3'"
```

  - 预期为一行：`active|active|owner`。查询只输出状态和角色，不输出用户名。
  - 其他结果（`suspended`、`revoked`、账户 `disabled`、角色不是 `owner`，或者没有这一行）：不要用 approver3，改由负责人本人执行工作台步骤，并在证据中写明原因。

## 1. 告警规则到 v18（DATA_HOST，配置员窗口，B；stage 已是 v18 或更高就跳过）

### 1.0 release 50 时的情况：跳过

- stage 当前是 **v19**（v18 的超集，另加 2 条），高于 v18，按 1.1 的规则**本步跳过**，不发布任何版本。
- 只执行 1.2 的第一条查询（预期 `19|<digest>`），以及“核对”中的 doctor，`--alert-rules` 传 `alert-rules.v19.json`（doctor 比对的是传入文件的版本）。
- main 上的 v20、v21 不属于本单，由各自的执行单发布；本单不要顺带发布。

### 1.1 各版本的关系

| 版本 | 来源 | 条数 | 与上一版的差别 |
|---|---|---|---|
| v14 | L3，NX-056 | 67 | v13 加 6 条审批告警 |
| v15 | L7，NX-057 | 71 | v14 原样保留，加 4 条：`remediation_waiting`、`remediation_rate_capped`、`remediation_paused`（warning），`remediation_rule_held`（critical） |
| v16 | L3，第二十七批 | 73 | v15 原样保留，加 2 条：`approval_first_time_approver`、`approval_same_person_flag`（warning） |
| v17 | L7，第三十一批（feed 积压修复） | 74 | v16 原样保留，加 1 条：`feed_attempts_over_ceiling`（critical：pending 的 feed 项被领取超过 20 次，崩溃循环） |
| v18 | L3，第三十一批（提取死信） | 74 | v17 的全部规则都在；只有 `data_health_extraction_dead_letters` 的指标改为 `data_health.extraction.unresolved_dead_letters`（未处理、也没有被后来的成功提取覆盖的死信）。没有删除规则，其他规则不变 |

- 逐级超集：每一版都保留上一版的全部规则（按 `rule_id`，已用脚本逐条比对 v14→v18）。v15–v17 只加规则；v18 只改了上面那一条的指标，没有增删。
- **版本只能增加**：`control.nexloop_configure_alert_rules` 拒绝不大于当前版本的新版本（`alert rules version must increase`）。同版本同内容重放返回 `replay: true`。
  - 所以一旦发布了较高的版本，就再也不能发布较低的版本。
  - 中间版本可以跳过：从当前版本直接发布 v18，结果与逐版发布相同。
- **L3 的 NX-056 第 0 天也发布 v18**。执行前先看第一条查询：
  - 当前已是 v18：**本步跳过**，直接核对 doctor（下面最后一条）；
  - 当前大于 v18（之后又有新版本）：跳过，不要回退；
  - 当前低于 v18：直接发布 v18，一条命令即可。
- 新增规则读的指标（`remediation.*`、`approvals.*`、`feeds.*.max_pending_attempts`、`data_health.extraction.unresolved_dead_letters`）都由 release 46 的迁移提供。规划器和规则还没有生效时，补救的指标都是 0，不会产生告警。

### 1.2 命令

```
ro "select version, digest from control.nexloop_alert_rules where tenant_id='$TENANT_ID' order by version desc limit 1"     # A：当前版本
docker run $COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/alert_rules_apply.py \
  --manifest /opt/release/deploy/configuration/alert-rules.v18.json --check                                                # A
# 当前版本低于 18 时才执行：
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/alert_rules_apply.py \
  --manifest /opt/release/deploy/configuration/alert-rules.v18.json --tenant "$TENANT_ID" --bootstrap-dsn-file /run/bootstrap-dsn   # B
```

- **`--check` 预期**：`{"valid": true, "version": 18, "rules": 74}`。
- **应用预期**：`applied: true`，`result.replay: false`。重跑是 `replay: true`。stage 已经是同内容的 v18 时，执行也只是 `replay: true`，不会写入。
- **核对**：下一轮评估之后，`nexloop-doctor --observability --alert-rules <stage 当前版本的文件，release 50 时为 v19> --metrics-database-url-file <metrics DSN>` 的 `alert_rules_current` 为 true（doctor 比对的是传入文件的版本，所以要传与库里相同的版本）。
- **失败时**：`alert rules version must increase` 说明库里已经是更高版本，核对第一条查询的结果，不要改版本号重发。

## 2. 服务授权到 v20（DATA_HOST，配置员窗口，B）

### 2.00 2026-10-28 的现状：按情况 A，授权已生效，本步只核对

- `plan_reevaluator` 的 `nexloop.remediation.plan:1` 已在 2026-10-28 生效（authority_revision 3197）；v20 的 `relation_backfiller` 此前已经 register 过。所以按 2.0a 的**情况 A** 处理，不走情况 B，不需要 `setup-credentials`。
- 本步不再写入。只执行：
  1. 2.1 的授权查询：plan_reevaluator 有三行（`NexLoop.feed.plan-reevaluate:1`、`nexloop.plan.reevaluate:1`、`nexloop.remediation.plan:1`），长度都是 1；scope 仍为 `["action.execute"]`。
  2. 2.1 的最后一条（还没扫描过的最终失败数），以及 2.3 的三条查询：授权生效后规划器已经在跑，积压应已按 `switch_closed` 扫完。
- `relation_backfiller` 和 register 的计数**以 OPERATIONS §8a.11a 的只读差异为准**，本单不写死预期：NX-054 第 14 步（2026-10-28）会撤销 `relation_backfiller` 的授权，之前之后它的查询结果和 register 的 `grants_revoked` 都不同。（§8a.11a 在本分支所基于的 main 17aa74c1 上还没有，执行时按当时 main 上的这一节。）
- 可选（B，经配置员窗口）：执行一次 2.2 的 register 作为幂等核对，预期 `changed: false`、`grants_added: 0`、`credentials_created: []`；其余计数与 §8a.11a 的只读差异一致。不一致就停下，报给出这份执行单的人。
- 授权查询结果与上面不符（例如缺 `nexloop.remediation.plan:1`）：停下报告，不要按情况 B 补做。
- 证据写 authority_revision 3197 与核对查询的结果；2.0a–2.2 保留作记录。

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

### 2.0a 这次 register 会带上什么：v19 与 v20

release 46 的清单是 service-grants v20、stage-principals v5。stage 上一次 register 用的是哪一版，决定了这次的预期：

| 情况 | 怎么判断 | 这次会写入什么 |
|---|---|---|
| **A**：v20 已经在 stage 上 register 过（NX-054 P3 回填前做过，见 `docs/implementation/NX-054-stage-publish.md` §7） | 2.1 中 `relation_backfiller` 的授权查询有一行，长度为 1 | 只有 v19：给 `plan_reevaluator` 加 `nexloop.remediation.plan:1` |
| **B**：v20 还没有 register 过 | 那条查询为空 | v19 加 v20：除了上面一条，还要新建 `relation_backfiller` 主体、它的凭据，以及它唯一的一条授权 `nexloop.relation.backfill:1` |

- **情况 B 的前提**：管理机先执行 `stage_secrets.py setup-credentials --out <树>`（NX-054-stage-publish §7 第 1.1 步）。它会生成 `setup/relation-backfiller-credential`，并把令牌加进 `management/service-secrets.json`。然后把这两个文件同步到 `$RUN`（属主 999，0600）。缺了它们，register 会以 `no token generated for principals: relation_backfiller` 拒绝，什么也不写。
- **v21**：v20 的 `relation_backfiller` 是一次性的主体，回填核对平衡后，由 v21 撤销。如果执行时 main 已经有 v21，2.1 的 diff 会多出这一段；2.2 中 `grants_revoked` 为 1，并且不再新建它的凭据。这时先停下，按 v21 的说明核对，再继续。

### 2.1 只读核对（A，代替 `--doctor`）

```
git -C "$RELEASE_CHECKOUT" diff "$LAST_REGISTER" "$MAIN" -- deploy/authorization/service-grants.v1.json deploy/authorization/stage-principals.v1.json deploy/authorization/owner-property-restrictions.json
docker run $COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/stage_secrets.py register --out /stage --tenant "$TENANT_ID" --dry-run
ro "select entity_key, jsonb_array_length(payload->'grants') from authz.nexloop_authority_facts where tenant_id='$TENANT_ID' and fact_kind='grants'
    and entity_key[1]='nexloop-service-plan-reevaluator-principal' order by entity_key"
ro "select binding->'requested_scopes' from authz.nexloop_service_credentials where tenant_id='$TENANT_ID'
    and binding->>'subject_principal_id'='nexloop-service-plan-reevaluator-principal'"
ro "select entity_key, jsonb_array_length(payload->'grants') from authz.nexloop_authority_facts where tenant_id='$TENANT_ID' and fact_kind='grants'
    and entity_key[1]='nexloop-service-relation-backfiller-principal'"                                                     # 2.0a 的情况 A/B
ro "select count(*) from runtime.nexloop_effect_failures where tenant_id='$TENANT_ID' and world='real'
    and failure_id not in (select failure_id from runtime.nexloop_remediation_scans)"
```

- **diff 预期**：`$LAST_REGISTER` 是第二十四批（main 9779abb6）或之后的提交时，`deploy/authorization/` 下只能有这两段（release 46 中）：
  - v18→v19：`service-grants.v1.json` 的 `manifest_version` 改为 19，`decision` 末尾加 v19 一段，`grants` 加一行 `plan_reevaluator action eios:action:nexloop.remediation.plan:1 execute`；
  - v19→v20（情况 A 时 `$LAST_REGISTER` 已经包含这一段，diff 里不会出现）：`manifest_version` 改为 20，`principals` 加 `relation_backfiller`，`grants` 加一行 `relation_backfiller action eios:action:nexloop.relation.backfill:1 execute`；`stage-principals.v1.json` 的 `manifest_version` 4→5，`credential_files` 加 `setup/relation-backfiller-credential`。
  - 还有别的差异就停下，报给出这份执行单的人。
- **dry-run 预期**：`grants` 里有 `plan_reevaluator action eios:action:nexloop.remediation.plan:1 execute` 和 `relation_backfiller action eios:action:nexloop.relation.backfill:1 execute`。情况 A：主体数和凭据文件数与上次相同；情况 B：各多 1。dry-run 不连库，看不出与库的差异。
- **授权查询预期**：plan_reevaluator 现在有 `NexLoop.feed.plan-reevaluate:1`、`nexloop.plan.reevaluate:1` 两行，长度都是 1；还没有 `nexloop.remediation.plan:1`。
- **scope 预期**：`["action.execute"]`。新授权也是 `action.execute`，所以 scope 集合不变，不会出现 `credential_rotation_required`。
- **最后一条**：现在还没有扫描过的最终失败数，记下来，2.3 要用。

### 2.2 正式执行（B）

```
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/stage_secrets.py register --out /stage --tenant "$TENANT_ID" \
  --bootstrap-dsn-file /run/bootstrap-dsn --signing-key-id "$KEY_ID" --credential-expires-at "$CREDENTIAL_EXPIRES_AT"
```

- **预期**：

| 字段 | 情况 A | 情况 B |
|---|---|---|
| `changed` | true | true |
| `grants_added` | 1 | 2 |
| `credentials_created` | `[]` | `["relation_backfiller"]` |
| `grants_revoked`、`extra_grants_outside_manifest` | 0 | 0 |
| `message_read_rules_written`、`role_source_read_rules_written` | 0 | 0 |

- 情况 B 新建的凭据，到期时间取 `$CREDENTIAL_EXPIRES_AT`。
- 数字不符就停下：说明库里的授权与清单在这次之前就有偏差。记下输出，报给出这份执行单的人。
- **被拒时**：`credential_rotation_required` 说明有凭据的 scope 变了，不要重试，停下报告。
- **再执行一次**：`changed: false`，`grants_added: 0`。
- **核对**：重跑 2.1 的授权查询，多出 `{nexloop-service-plan-reevaluator-principal,eios:action:nexloop.remediation.plan:1}`，长度为 1。情况 B 下，`relation_backfiller` 那条查询也有一行，长度为 1。

### 2.3 授权生效之后会发生什么（A，观察）

- 规划器和重评估在同一个进程里，每分钟最多一轮，每轮最多处理 50 条失败。
- 部署层开关此时还是关的（release 46 的 compose 中默认 `false`），所以每一条还没扫描过的最终失败都会被记为 `switch_closed`，**包括授权之前就存在的历史失败**。
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

## 3. 补救规则 v1（DATA_HOST，配置员窗口，B）

- **内容**：
  - 一条规则 `deliver_final_failure`：源 Action `nexloop.enterprise.deliver:1`；两种失败（`origin_expired`、`confirmed_not_delivered`）；方式 `reevaluate`，`automatic`；期限 7 天。
  - 限额：每租户每小时 20 条；每客户 24 小时 3 条；每个计划 7 天唤醒 2 次；24 小时 3 次触发熔断。
  - 负责人 2026-10-25 决定：Q-1..Q-7 全部按推荐。
- **前提**：P4（效应类别 v3）。源 Action 必须是本租户声明的外部效应，否则返回 `source_not_effect`。`nexloop.service.request:1` 被声明为本地效应，所以不在 v1 里。
- **为什么必须在第 4 步之前**：规划器打开以后，没有规则时，每条失败都会被**永久**记为 `no_rule`，规则发布后也不会再处理。

```
ro "select coalesce(max(manifest_version),0) from control.nexloop_remediation_rules where tenant_id='$TENANT_ID' and world='real'"   # A：当前版本，第一次为 0
docker run $COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/remediation_rules_apply.py \
  --manifest /opt/release/deploy/configuration/remediation-rules.v1.json --check                                                     # A
sha256sum "$RUN/src/deploy/configuration/remediation-rules.v1.json"                                                                  # A
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/remediation_rules_apply.py \
  --manifest /opt/release/deploy/configuration/remediation-rules.v1.json --tenant "$TENANT_ID" --expected-revision 0 \
  --bootstrap-dsn-file /run/bootstrap-dsn                                                                                            # B
# 再执行一次（B，经配置员窗口，标为写入）：result.replay 应为 true
```

- `--expected-revision` 填第一条查询的结果。stage 第一次发布时为 0。
- **`--check` 预期**：`{"valid": true, "manifest_version": 1, "rules": 1, "manifest_sha256": "<与 sha256sum 相同>"}`。把 sha256 记入证据。
- **应用预期**：
  - 外层：`applied: true`，`manifest_version: 1`，`expected_revision_passed: 0`（本次命令传入的值），`expected_revision_recorded: 0`（库里为这个版本记录的值）；
  - `result`：`configured: true`，`replay: false`，`rules: 1`，`manifest_sha256` 与 sha256sum 相同，`expected_revision: 0`（与 `expected_revision_recorded` 相同）。
- **重跑预期**：`result.replay: true`，什么也不写。`expected_revision_recorded` 仍是第一次记录的 0；重跑时 `--expected-revision` 传了别的值，也只体现在 `expected_revision_passed` 上。证据以 `expected_revision_recorded` 为准。
- **规则表已有 v1，发布记录却为空**：说明 v1 是在迁移 `remediation_rule_publications` 之前直接写入的（L6 S-1；stage 尚未发布过规则，预期不会出现）。这时重跑命令会得到 `replay: true`、`expected_revision_recorded: null`。先用下面第一条查询核对 `digest`，再决定是否发布 v2，不要补写记录。
- **被拒时**（输出 `applied: false`，`reason` 是数据库的拒绝原文；不打印 DSN）：
  - `expected revision 0, current N`：库里已经有版本 N，用第一条查询核对，弄清楚是谁发布的，不要直接改成 N 重发；
  - `version 1 published with another text`：库里的 v1 来自另一份文件，对照 sha256 核对，不要改版本号重发；
  - `source_not_effect`：回到 P4；
  - `version_must_increase`：文件版本不大于库里的当前版本；
  - `remediation rules are trusted configuration only`：连接身份不是配置员，检查是否经窗口执行。
- **核对**：

```
ro "select manifest_version, digest, configured_by from control.nexloop_remediation_rules where tenant_id='$TENANT_ID' and world='real'"
ro "select manifest_version, manifest_sha256, expected_revision, published_by from control.nexloop_remediation_rule_publications where tenant_id='$TENANT_ID' and world='real'"
```

  - 第一条预期为一行：`1|<digest>|nexloop_configurator`。`digest` 是库对规范化 JSON 求的值，与文件的 sha256 不同；
  - 第二条预期为一行：`1|<与 sha256sum 相同>|0|nexloop_configurator`。

## 4. 打开开关（B）

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
- **停止条件**（L6 I-2）：
  - 第一条不是 0：积压还没清空，等 2.3 完成；
  - 第二条不是恰好一行 `1`：规则 v1 没有发布（或不是本单的版本）。
  - 这两种情况都**不打开**部署层开关，回到第 2 步或第 3 步。开关打开之后，没有规则的失败会被永久记为 `no_rule`，以后发布规则也不会补生成。

### 4.2 部署层开关（APP_HOST，B）

0. **前提**：APP_HOST 上的 `$NEXLOOP_ROOT/compose.app.yaml` 来自 release 46 或之后（按部署单更新）。核对：`grep -n 'REMEDIATION_ENABLED:' $NEXLOOP_ROOT/compose.app.yaml` 只有一行，在 `plan-reevaluator` 下。旧文件里没有这一行，改 env 也不会生效。
1. 在 APP_HOST 的私有 compose env（`$NEXLOOP_ROOT/config/compose.app.env`，由仓库中的示例 `deploy/stage/compose.app.env.example` 填写而来）中，把 `REMEDIATION_ENABLED=false` 改为 `REMEDIATION_ENABLED=true`。G2 的 env 示例有这一行；部署时如果没有加，就在这里加。值只能是小写的 `true`，其他值都等于关。
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

- **执行者**：approver3（合成 owner，§0.5），用自己的工作台账号操作；负责人本人也可以代替。调度员不用运维手段代操作。这个动作不在审批策略内，提交后立即生效。
- **判定者**：调度员，用 4.1 的第三条查询和 4.4 的查询判定，不参与操作。
- 4.1 显示租户开关是开的：什么都不用做，判定者记下查询结果即可。
- 显示是暂停的：approver3 在“运营 → Action / 异常 → 补救”点“恢复补救”，填原因后提交。页面摘要不再显示“补救已暂停”，下一轮评估后 `remediation_paused` 告警解除。
- 判定：4.1 的第三条查询，最新一条为 `paused=f`，`revision` 加 1。

### 4.4 打开后的核对（A）

打开之后的下一个最终失败，扫描结果不再是 `switch_closed`：

```
ro "select decision, count(*) from runtime.nexloop_remediation_scans where tenant_id='$TENANT_ID' and scanned_at > '<4.2 的时间>' group by 1"
ro "select state, kind, last_code, count(*) from runtime.nexloop_remediation_intents where tenant_id='$TENANT_ID' group by 1,2,3"
```

- `nexloop.enterprise.deliver:1` 的最终失败预期为 `generated`，其他 Action 的失败为 `no_rule` 或 `not_eligible`。
- 最终失败多少取决于真实派发开关（P5 读出的状态）。失败很少时，打开之后一段时间内两条查询都为空是正常的。本单不制造失败；演练另行安排。
- 工作台“补救”一栏显示待处理条数；告警：
  - 有补救等待超过 24 小时：`remediation_waiting`；
  - 超过每小时上限：`remediation_rate_capped`；
  - 规则被熔断：`remediation_rule_held`（critical），由负责人在同一栏“解除熔断”。
- R4（下一批，`l7/nx057-r4`）会补上认领终态、`outcomes.remediation` 计数和证据抽屉。在那之前打开也可以，只是工作台还没有单条补救的证据页。

**证据**（记入 NX-057 任务证据，不含 DSN、令牌和口令；每条写明执行者和判定者）：

- 第 1–3 步每条命令的输出（JSON 行）；
- 第 1 步前的告警规则版本（以及是否因为已是 v18 或更高而跳过；release 50 时预期 v19，跳过）；
- 工作台动作由哪个账号执行（approver3 或负责人）；
- 2.00：情况 A，authority_revision 3197，2.1/2.3 查询的结果；做了 2.2 幂等核对时写它的输出（之前的写法：`$LAST_REGISTER` 的实际值、2.0a 判定的情况、2.1 的 diff 摘要、2.2 的计数）；
- 第 3 步规则文件的 sha256；
- 2.3、4.1、4.4 查询的结果；
- 4.2 的时间。

## 5. 回退

| 情况 | 做法 | 说明 |
|---|---|---|
| 要马上停止生成补救 | approver3（或负责人）在工作台“补救”点“暂停补救”，填原因 | 最快的办法，下一轮生效，不需要重建任何服务。会触发 `remediation_paused` 告警（warning），这是预期的。暂停也会挡住负责人手动唤醒 |
| 关掉部署层 | 私有 compose env 改回 `REMEDIATION_ENABLED=false`，执行 4.2 的两条命令，`grep` 应为 `=false` | 只停规划器；负责人手动唤醒仍然可以用（它只看租户开关和控制暂停） |
| 关着期间的失败 | 不处理 | 被记为 `switch_closed`，重新打开后**不会补生成**。需要跟进的，由负责人按失败记录手动处理 |
| 已经生成的补救 | 负责人在“补救”一栏逐条“标记已处理”或“取消” | 关开关不会改变已有补救。`proposed` 状态的补救到期（7 天）后自动变为 `expired` |
| 某条规则不该生效 | 发布 `remediation-rules.v2.json`：`manifest_version: 2`，去掉这条规则（`rules` 可以为空），`decision` 写明理由。命令同第 3 步，`--expected-revision 1` | 规则只追加不删除，版本只能增加；v1 的记录保留。需要先评审 v2 文件 |
| 规则被熔断 | 负责人确认原因后，在“补救”一栏“解除熔断” | 解除后，从下一个失败开始重新生成 |
| 撤销规划器的授权 | 不在本单范围 | 要改清单（在当时最新版本的下一版里去掉这条授权；main 575859f7 上最新仍是 v20），然后重跑 register。plan 会把这条授权清空。开关已经足够停用，一般不需要撤销 |
| 告警规则 | 不回退 | 告警只写告警状态，没有外部效应。版本只能增加；如果确实要去掉某条，发布当时最新版本的下一版（main 575859f7 上最新是 v21，stage 为 v19），并另行评审 |

**不允许的做法**：

- 直接写 SQL 改补救表、扫描表或开关表；
- 用 superuser 或 bootstrap 冒充配置员；
- 手工改 `nexloop_configurator` 的登录属性；
- 在 stage 上用 `python -m nexloop_eios.service_grants --apply` 代替 register（§2.0）。
