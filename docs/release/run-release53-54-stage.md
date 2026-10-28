# 执行单：release 53 + 54 的 stage 一次部署（第 47–50 批）

本单把 release 53（第 47、48、49 批，`run-release53-stage.md`）与 release 54（第 50 批，`run-release54-stage.md`）合成一次部署：检出 **ad8f42dc**（第 49、50 批与 L8 修复合成的一批，第一父 c7f00079，合入后即 main；迁移 0364–0371）。stage 从 release 52（迁移尾 0363）直接到 ad8f42dc。两份原单只作依据，执行以本单为准。每一步写明预期结果和怎么撤回。

- **执行者**：调度员。**判定者**：L6。证据里两者分开写，并分开记录测试、演练和真实结果。
- **人工步骤**：只有调度员在管理机和两台主机上执行的命令；负责人账号不参与。新 Action 的使用演练（预算放宽审批、SSO）不在本单，按各自的执行单做。
- **按 ad8f42dc 核对过**（只读核对源码，没有连接主机）：与 f56cd5af（release 54 单核对的提交）相比只多了两个测试文件的修复（9d97ff66），部署件和代码相同；下表的清单版本在 ad8f42dc 上逐项读过。

| 项 | ad8f42dc 上的值 | 来源 |
|---|---|---|
| 新迁移 | 0364 `nx043_budget_drill`、0365 `nx058_oidc`、0366 `ui_approval_requester_labels`、0367 `ui_consumer_last_conversation`、0368 `ui_conversation_times`、0369 `budget_loosen`、0370 `ui_experiments_view`（实验空间只读视图，`observe_read` 加动词 `experiments`，只给 owner）、0371 `nx058_invite_sso`（邀请可指定仅限 SSO；成员视图加 `sso_pending`）；迁移尾为 0371 | `packages/eios-core/src/eios/migrations/catalog.json` |
| 服务授权 | `service-grants` v22：v21 让 `relation_backfiller` 退役（进入 `retired_principals`，授权撤销）；v22 新增 `outreach_sender`，只持有 `eios:action:nexloop.outreach.execute:1`。清单共 15 个主体 | `deploy/authorization/service-grants.v1.json` |
| stage 主体 | `stage-principals` v7：v6 去掉 `setup/relation-backfiller-credential`；v7 增加 `outreach-sender/service-credential` → `outreach_sender`。凭据文件 34 个 | `deploy/authorization/stage-principals.v1.json` |
| 业务 Action | `business-actions` v52；编译结果 object_types 20、actions **128**、functions 2。比 main 17aa74c1（125）多三个：`nexloop.workbench_member.link_external`、`nexloop.workbench_member.unlink_external`、`nexloop.budget.loosen` | `stage_secrets.py publish --dry-run`（不带连接文件，只编译计数）实测 |
| 工作台角色 | `workbench-roles` v48；owner 角色 94 个 Action（17aa74c1 上为 91），增加 link_external、unlink_external、`Budget.loosen` | `deploy/authorization/workbench-roles.v1.json` |
| 审批策略 | `approval-policies` version 5、`expected_revision: 4`、12 条；新增 `goals.budget.loosen`（quorum 1、批准角色 owner、24 小时过期、单人模式 allowed）；`manifest_sha256` = `258bd631b83dc1f0d84bf4e44a06b77266d372771635c101ef5bbf0342ce66f3` | `approval_policies_apply.py --check` 实测 |
| compose | 新服务 `outreach-sender`（外联执行器，outreach 第 5 片 G-1）；app-net 上的服务从 17 个变为 18 个 | `deploy/stage/compose.app.example.yaml` |
| 连接预算 | 应用总数 59 → **60**（等于上限 60，没有余量了） | `deploy/stage/connection-budget.v1.json` |
| Host 运行配置 | 示例打开 `plan_outcome_tool`、`outreach_tool`（G-2，行为变化，§S10） | `deploy/stage/agent-host.runtime-config.example.json` |
| host-health | APP_HOST 期望容器增加 `nexloop-app-outreach-sender-1`；DATA_HOST 不变 | `deploy/examples/host-health-install.app-host.example.json` |
| API 路由清单 | `api-routes` v4（v3 加 SSO 三条，v4 加 `GET /api/v1/workbench/experiments`）；仓库内由 CI 核对，随镜像生效，stage 上没有要应用的步骤 | `deploy/authorization/api-routes.v1.json` |
| 前端 | release 53 的工作台 UX X1–X4；release 54 的实验空间开放、Object Type 关系图、运营人设页、确认面板、成员页操作选择器与“待 SSO 关联”、客户详情先列最近 5 次对话 | `apps/web` |
| 告警规则 | **无变化**：仓库最新仍为 v21，与 main 17aa74c1 相同 | `deploy/configuration/alert-rules.v21.json` |
| 浏览器限速策略 | 增加 `oidc_start`（NX-058 片 F）。stage 不配置 SSO provider，两个 SSO 端点在限速之前就返回 404，所以本单**不**重跑 `stage_secrets.py browser`，记录“未配置 SSO，不需要” | `scripts/deploy/stage_secrets.py` `OIDC_RATE`；`docs/implementation/NX-058-slice-F.md` |

本单新增的工具：

- `scripts/deploy/stage_secrets.py add-service`：给已有的秘密树添加一个 compose 里新出现的后台服务的文件（DSN、PG 客户端证书、服务凭据）。只生成这几类文件；服务挂了渠道凭据、provider 文件或密钥就拒绝；树里已有该服务的任何文件也拒绝。测试 `tests/test_stage_secrets.py::test_add_service_*`。
- `scripts/deploy/outreach_sender_p9.py`：只读，读 `docker compose config --format json`，核对外联执行器只用回环 provider、不挂任何渠道凭据（P9）。测试 `tests/test_outreach_sender_p9.py`。

## 0. 记号

- `$NEXLOOP_ROOT`：APP_HOST 上的部署根目录；`$RUN`：DATA_HOST 上的运行目录（同 run-nx054-s8-backfill）；`<树>`：管理机上的秘密树（`stage_secrets.py generate` 的输出目录）；`<检出>`：管理机上 ad8f42dc 的检出。
- `ro "<SQL>"`：DATA_HOST 上的只读会话（bootstrap socket、`begin read only`、`set local role nexloop_owner`、设置 `eios.tenant_id`，最后 rollback），同 `docs/release/run-nx057-remediation-stage.md` §0.3。
- `stage_release.py`：例行发布的分步执行器（`scripts/deploy/stage_release.py`），私有配置的形状见 `deploy/examples/stage-release.example.json`。本单用到它的 `backup`、`precheck`、`migrate`、`publish`、`roles`、`rebuild`、`gate`、`promote`、`post` 各步；`register`、秘密树、审批策略、Host 运行配置和 host-health 不在它里面，单独执行。
- 每一步先 `--dry-run` 看命令，再正式执行。任何一步结果与预期不符就停下，交调度员判断，不往下做。

## 1. 前提（不满足就不开始）

| # | 前提 | 怎么核对 |
|---|---|---|
| P1 | ad8f42dc 的 CI（seq 186）通过，release 已构建、签名、封存 | `stage_release.py --step build`、`--step sign` 的 JSON 都是 ok；签名前它会核对提交在 main 上（ad8f42dc 合入 main 之后才执行本单） |
| P2 | stage 当前是 release 52（迁移尾 0363），known_good 记录在案 | `ro "select max(version) from control.schema_migrations"` 返回 0363 |
| P3 | 最近一次 pgBackRest 备份成功 | OPERATIONS §8a.2 |
| P4 | stage 上已应用的版本：service-grants v20（v21 尚未 register）、stage-principals v5、workbench-roles v46、approval-policies revision 4、告警规则 v21 | register 前的 §8a.11a 差异核对会再核一次授权；审批策略看 `control.nexloop_approval_policy_publications` 的最大 `manifest_version`（4）；角色清单与告警规则以上次部署记录为准。任一项不符就停下，按实际版本重写受影响的步骤 |
| P5 | NX-054 §8 的关系回填已完成，verify 平衡（v21 让 `relation_backfiller` 退役的前提） | `run-nx054-s8-backfill.md` 第 13 步的记录；第 14 步第 1 条（删除 APP_HOST 上的 `$BF`）已经做过 |
| P6 | 管理机上的 `<树>` 是 stage 正在用的那份，`stage_secrets.py check --out <树>` 为 ok | 执行一次 |
| P7 | 两台主机的部署门禁参照：`$NEXLOOP_ROOT/ops/baseline-r36/baseline.json` 存在 | `test -f` |
| P8 | 真实派发三层开关的现状记录在案（门禁预期 20/21，唯一不通过的是 `real_dispatch_closed`） | 上次部署的门禁记录 |

## 2. 步骤

### S1 备份核验与迁移前差异备份

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step backup
```

- 预期：`nexloop-pg-diff.service` 退出 0，日志里有本次差异备份完成的记录。
- **撤回**：不需要（只多一份备份）。

### S2 只读预检

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step precheck
```

预检 SQL 清单（私有 JSON）至少包含：

| name | sql | expect |
|---|---|---|
| migration_head | `select max(version) from control.schema_migrations` | `0363` |
| approval_policy_revision | `select max(manifest_version) from control.nexloop_approval_policy_publications` | `4` |
| outreach_agent_rows | `select count(*) from runtime.nexloop_outreach_sends where initiator='agent'` | `0` |

另记一条数值（`precheck` 只记录输出的 sha256，看不到数值，所以不放进预检清单）：

```bash
ro "select count(*) from control.nexloop_workbench_invitations where state='issued'"
```

把这个数写进证据。这是迁移前**全部**未用的邀请：迁移前还没有 `sso_provider` 列，SSO 邀请无从计数，所以不设固定预期值；S13 拿它对照。

- **撤回**：不需要（只读）。

### S3 迁移 0364–0371

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step migrate
```

- 迁移门槛必须回答 `allowed: true`；迁移输出 ok；之后的 definer 检查必须是 `0 problems`（三项都由 `migrate` 一步做完，任一不符就停）。
- 8 个迁移一次执行。预期迁移后 `ro "select max(version) from control.schema_migrations"` 返回 0371。
- **迁移时遇到 `DeadlockDetected`**：先确认迁移尾没有变（仍是 0363，迁移整体回滚），重试一次；再遇到就停掉 APP_HOST 的 worker 服务（`docker compose -p nexloop-app stop <worker 服务>`）再迁移。
- **迁移成功后再做一次差异备份**（重跑 S1）。
- **0369 的影响**：迁移后 `Budget.set` 只接受收紧，放宽一律以 `budget_loosen_requires_approval` 拒绝；`Budget.loosen` 要等 S4、S5、S8 都做完才可用。在此之间放宽被拒、收紧不受影响（`docs/design/NX-056-budget-approval.md` §部署顺序）。stage 是开发/测试环境，可以接受。
- 0365（SSO 表和函数）、0366–0368（工作台读取的新字段）、0364（NX-043 预算演练）都只增加对象，不改已有行为；不配置 SSO provider 时 SSO 端点返回 404。
- 0370：只增加 owner 的只读视图，其他动词不变；改名保留的旧函数 `nexloop_workbench_observe_read_before_experiments` 对 `nexloop_api` 已撤销执行权限。0371：已有邀请的 `sso_provider` 为空，行为不变；stage 不配置 SSO provider，指定 `sso_provider` 的邀请被拒为 `provider_unknown`（409）。两者共涉及 10 个 `security definer` 函数，都在 definer 检查范围内。
- **撤回**：迁移只能前进。真要退回 0363，只能用 S1 的迁移前备份恢复（pgBackRest 恢复，会丢失恢复点之后的数据），需要调度员决定，并与镜像一起退回 release 52。不要手工删除迁移建的对象。

### S4 Action 发布：先预演，再发布

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step publish
```

`publish` 一步先带连接文件 `--dry-run`（与库里已发布的状态比较，什么都不写），再正式发布。

- 预演预期：有变化，变化只有三个新 Action：`nexloop.workbench_member.link_external:1`、`nexloop.workbench_member.unlink_external:1`、`nexloop.budget.loosen:1`；编译计数 object_types 20、actions 128、functions 2。预演结果出现别的变化就停下。
- 发布预期：ok；发布前后各记录一次计数。
- **不发布的后果**：新 Action 一律 404 或 `Action not published`，S5 应用角色也没有意义。
- **撤回**：目录发布只追加，没有删除路径。要让新 Action 不可用，撤回 S5（没有人持有它们的授权），或者发布一个新版本的 business-actions。

### S5 应用工作台角色 v48

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step roles
```

- 预期：ok；owner 角色 94 个 Action（v46 为 91），新增 link_external、unlink_external、`Budget.loosen`。
- 核对：`roles` 的 JSON 输出里 owner 的授权数为 94；放宽预算要等 S8 之后才能经审批执行。
- **撤回**：用 17aa74c1 的 `deploy/authorization/workbench-roles.v1.json`（v46）按 OPERATIONS §8a.11 第 3 步重新应用，会撤销这三个 Action 的成员授权。

### S6 秘密树：删除回填凭据、给外联执行器生成文件（管理机）

先复制一份 `<树>` 作为本步的撤回点：`cp -a <树> <树>.before-r53`（0700，不进仓库）。

1. **先删**回填主体的 setup 凭据（`run-nx054-s8-backfill.md` 第 14 步第 3 条）：
   - 从树中删除 `setup/relation-backfiller-credential`；
   - 删除 `management/service-secrets.json` 中的 `setup:setup/relation-backfiller-credential` 条目：`jq 'del(."setup:setup/relation-backfiller-credential")'` 写到同目录的临时文件，0600，再 `mv` 覆盖。不打印内容。
   - 必须在 register 之前删：v6 起 register 遇到没有主体的条目会拒绝（`a generated credential file has no principal: setup/relation-backfiller-credential`），什么也不写。
   - **删完立即另存第二份快照**：`cp -a <树> <树>.after-s6-1`（0700，不进仓库）。它既没有回填凭据，也还没有外联执行器的条目，是 v21 / v6 清单唯一能接受的树，S7 的撤回只能用它（L6 I1）。
2. **再生成**外联执行器的文件：

   ```bash
   python3 <检出>/scripts/deploy/stage_secrets.py add-service --out <树> --ca-dir <中间 CA 目录> --params <私有 params> --service outreach-sender
   ```

   预期输出：`{"added": ["outreach-sender/dsn", "outreach-sender/pg-client.crt", "outreach-sender/pg-client.key", "outreach-sender/service-credential"], "credentials_added": ["app:outreach-sender/service-credential"], "service": "outreach-sender"}`。
   - DSN 用 `nexloop_domain_worker` 已有的口令（从树里同一角色的另一个 DSN 读出），所以**不需要**改任何角色口令，也不需要重跑 `role-passwords.sql`；
   - 客户端证书的 CN 是 `nexloop_domain_worker`，由同一个中间 CA 签发；
   - 服务凭据是新令牌，同时写进 `service-secrets.json`，S7 的 register 用它登记 `outreach_sender` 的凭据；
   - 签名密钥和 PG CA 是已有的共用文件，不重新生成。
3. `python3 <检出>/scripts/deploy/stage_secrets.py check --out <树>`：预期 ok。
4. **装到 APP_HOST**：`$NEXLOOP_ROOT/secrets/outreach-sender/` 目录 0700、属 10001；四个文件用 `install -o 10001 -g 10001 -m 0600` 逐个装入（与其他服务的文件相同，OPERATIONS §8a.5：源文件缺失时 Docker 会自建同名空目录，所以必须在 S9 之前装好）。
5. **outbox 目录**：`install -d -o 10001 -g 10001 -m 0700 "$NEXLOOP_ROOT/data/outreach-outbox"`。若由 Docker 自动创建，目录属 root，执行器会拒绝启动（它要求目录属本用户、0700）。
6. 同步到 DATA_HOST 的 `$RUN`（register 读的是 `$RUN` 下的树），做法同 run-nx054-s8-backfill 第 14 步第 3 条。

- **撤回**：
  - 只撤外联执行器的文件（保留回填凭据的删除）：管理机用 `<树>.after-s6-1` 覆盖回去；
  - `<树>.before-r53` 只用于连 S6.1 一起撤回、并且 stage 仍停在 v20 / v5 的情况（它还含回填凭据，v6 起的 register 会拒绝它）；
  - APP_HOST 删除 `$NEXLOOP_ROOT/secrets/outreach-sender/` 和 `$NEXLOOP_ROOT/data/outreach-outbox`（S9 之前，或撤回 S9 之后）。回填凭据删掉之后不需要恢复：v21 起它没有主体。

### S7 register：service-grants v21 + v22、stage-principals v6 + v7（DATA_HOST）

1. **dry-run**（不连库，只列清单）：

   ```bash
   python3 <检出>/scripts/deploy/stage_secrets.py register --out <树> --tenant <TENANT_ID> --dry-run
   ```

   预期：`principals` 25 个，`credential_files` 34；`grants` 中没有 `relation_backfiller`，有一条 `outreach_sender action eios:action:nexloop.outreach.execute:1 execute`。
2. **差异核对**（OPERATIONS §8a.11a，只读）：
   - 第 2、3 步得到 `comm -23 manifest-grants.txt db-grants.txt`（清单有、库里没有，register 会新增的），预期**恰好一行**：`nexloop-service-outreach-sender-principal eios:action:nexloop.outreach.execute:1`；
   - 反方向 `comm -13 manifest-grants.txt db-grants.txt`（库里有、清单没有，register 会撤销的），预期**恰好一行**：`nexloop-service-relation-backfiller-principal eios:action:nexloop.relation.backfill:1`；
   - 另外出现的行按 §8a.11a 逐条查来源，交调度员确认后才 register。`follow_latest_version` 的授权可能因为后继版本出现在差异里，按 §8a.11a 的说明判断。
3. **register**：用含 v22 的 `$RUN/src`，命令同 run-nx054-s8-backfill 第 6 步第 3 条。
   - 预期：`grants_revoked` 1（relation_backfiller）、`grants_added` 1（outreach_sender 的 outreach.execute）、`credentials_created` 只有 `outreach_sender` 的一条；`extra_grants_outside_manifest` 只有 `nexloop-sim-*`。
4. **核对**：run-nx054-s8-backfill 第 6 步第 1 条的授权查询（relation_backfiller）返回空；`ro "select entity_key[1]||' '||entity_key[2] from authz.nexloop_authority_facts where tenant_id='<TENANT_ID>' and fact_kind='grants' and entity_key[1]='nexloop-service-outreach-sender-principal'"` 返回一行，资源是 `eios:action:nexloop.outreach.execute:1`。
5. 清理 Keychain 中回填凭据的条目（如有），更新 `~/.nexloop/vault-index.md`。

- **撤回**：
  - 撤销外联执行器的授权：先撤回 S9 的执行器（停掉并删除容器）。然后用 `<树>.after-s6-1`（没有 `app:outreach-sender/service-credential` 条目）配 v21 / v6 的清单（git 中 31884a17 的两个文件）再 register 一次，`grants_revoked` 应为 1（outreach_sender）。
  - **不能用当前的 `<树>`**：S6 之后它的 `service-secrets.json` 里有 `app:outreach-sender/service-credential`，v6 没有对应的主体，register 会拒绝（`a generated credential file has no principal`，`stage_secrets.py` 第 742 行），什么也不写。也不能用 `<树>.before-r53`：它还含回填凭据，同样被拒绝。没有 `after-s6-1` 快照时，先从树里删掉 `app/secrets/outreach-sender/` 和 `service-secrets.json` 的 `app:outreach-sender/service-credential` 条目（jq 写临时文件，0600，再 `mv` 覆盖，不打印内容），再 register；
  - 回填主体已退役，不恢复。

### S8 审批策略 v5（预算放宽）

1. **校验**（不连库，任意机器）：

   ```bash
   python3 <检出>/scripts/deploy/approval_policies_apply.py --manifest deploy/configuration/approval-policies.v1.json --check
   ```

   预期：`{"valid": true, "version": 5, "expected_revision": 4, "policies": 12, "manifest_sha256": "258bd631b83dc1f0d84bf4e44a06b77266d372771635c101ef5bbf0342ce66f3"}`。
2. **发布**：在 DATA_HOST 上用一次性 backend 容器执行，形态同 `docs/implementation/approval-policies.md` §2 第 2 步。预期 `"applied": true, "version": 5`，`result` 里 `appended` 1、`unchanged` 11；再执行一次得到 `"replayed": true`。
3. **核对来源**（只读，同 approval-policies.md §2 第 3 步）：`control.nexloop_approval_policy_publications` 新增 `manifest_version` 5 的一行，`manifest_sha256` 等于上面的值；`control.nexloop_approval_policies` 有 `goals.budget.loosen` version 1、quorum 1、批准角色 `{owner}`、`expires_after_seconds` 86400、`solo_mode` allowed。
4. **影响**：
   - 从此预算放宽要另一位 owner 批准；收紧照旧立即生效。
   - `goals.budget.loosen` 是新能力，它的单人模式要等新策略满 24 小时才生效（approval-policies.md §2 第 4 步）。这期间放宽只能由他人批准。
   - S3 到本步之间放宽一律被拒，本步完成后恢复为“经审批放宽”。
- **撤回**：策略只追加，没有删除路径。要放宽或停用，发布一个新版本的清单（approval-policies.md §2 回退一节）。

### S9 镜像与重建（含新服务 outreach-sender）

1. **P9：外联执行器只用回环 provider、不挂任何渠道凭据**（`up` 之前，APP_HOST，只读）：

   ```bash
   docker compose -p nexloop-app -f "$NEXLOOP_ROOT/compose.app.yaml" --env-file "$NEXLOOP_ROOT/config/compose.app.env" \
     --env-file "$NEXLOOP_ROOT/config/compose.images.env" config --format json | python3 <检出>/scripts/deploy/outreach_sender_p9.py
   ```

   预期 `{"ok": true, "problems": []}`。它核对：
   - 入口是 `nexloop-outreach-sender`，命令里没有 `--provider` 和 `--real-channel-enabled`，所以 provider 只能是回环 outbox（执行器对非回环 provider 一律拒绝启动，先于读取任何凭据）；
   - secret 恰好是它自己的 4 个文件加 `signing_key`、`pg_ca`，没有任何渠道凭据；
   - 卷只有 Artifact 目录和 outbox；只读根文件系统、`cap_drop ALL`、`no-new-privileges`、`network_mode: service:app-net`；环境变量只有 `REAL_DISPATCH_ENABLED`。

   **不是 ok 就停**，不执行下面的重建。三层真实派发开关都打开时，这是挡住外联对外发送的唯一一道。
2. **重建**：

   ```bash
   python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step rebuild
   ```

   - 镜像只按签名清单里的 digest 载入（`rebuild` 会比对）；
   - OPERATIONS §8a.10：app-net 现在被 18 个服务引用（多了 outreach-sender），重建 app-net 时它们一起处理；
   - 预期：`docker compose ps` 中有 `nexloop-app-outreach-sender-1` 且在运行；它的日志第一行是 `Outreach Sender ready`，此后没有 `background tick failed`。日志里只有计数和结构化的失败行，不会有正文。
- **撤回**：
  - 只撤外联执行器：`docker compose -p nexloop-app stop outreach-sender && docker compose -p nexloop-app rm -f outreach-sender`，再用不含它的 compose 文件（release 52）启动；已受理的外联意图停在 `accepted`，不会被发送；
  - 整体：按 known_good（release 52）的镜像和 compose 重建，同 §8a.10 一起处理引用方。迁移不随镜像退回（见 S3）。

### S10 G-2：Host 运行配置打开 `plan_outcome_tool` 与 `outreach_tool`（行为变化，单独一步，调度员按 ack-policy 确认后执行）

**影响**（`docs/implementation/outreach-slice5-executor.md` §G-2）：

- `plan_outcome_tool`：Host 给 Run 提供 `nexloop.plan.outcome`。stage 的计划复评 Run 从此每次复评可以记录一次结论：
  - `plan_update` 用新步骤替换计划，计划版本前进；
  - `reassess_at` 设定下次复评时间，仍受 `min_reassess_seconds`（60 秒）和 `self_trigger_cap`（2）约束；
  - `escalate` 升级给负责人；
  - 写入只经 guard 和 SQL，SQL 校验 Run、计划和当前计划版本；不改业务对象，不发送任何东西。
- `outreach_tool`：只给计划复评 Run 提供 `nexloop.outreach.send`。受理不等于发送：SQL 仍要求计划 Run、Run 凭据和 Role 上限都含 `nexloop.outreach.send:1`、目的已启用；是否发出只由 S9 的执行器在派发时决定，三层真实派发开关、联系限制、频率、测试人设在那时再查一次。外联目前只有回环 outbox，不会到达任何真实渠道。

**执行**（APP_HOST）：

1. 备份：`cp -p "$NEXLOOP_ROOT/secrets/agent-host/runtime-config.json" <私有备份目录>/runtime-config.json.before-r53`。
2. 生成新文件：`jq '.plan_outcome_tool=true | .outreach_tool=true' <原文件> > <同目录临时文件>`，0600、属 10020（Host 的用户），再 `mv` 覆盖。这会换 inode（OPERATIONS §8a.6），容器看不到新内容，所以必须重建容器。
3. 只看键，不贴文件：`jq -r '[.effect_tools,.plan_outcome_tool,.outreach_tool]|@csv' <文件>` 预期 `true,true,true`。
4. 重建 Host 及引用它的服务（§8a.10）：`docker compose -p nexloop-app up -d --force-recreate agent-host agent-link-relay`。
5. 预期：Host 日志里没有 `runtime configuration refused`；`agent-link-relay` 的网络命名空间指向新的 Host 容器（§8a.10 的核对方法）。

- **撤回**：用备份覆盖回去（同样 0600、属 10020），再执行第 4 步。两项必须一起删：只留 `outreach_tool` 时 Host 拒绝启动。

### S11 host-health 与告警规则

1. **APP_HOST 的 host-health**：把私有安装配置 `probe.containers` 按新示例加上 `{"name": "outreach-sender", "container": "nexloop-app-outreach-sender-1"}`，然后按 `docs/implementation/host-health.md` §4 重跑安装：

   ```bash
   printf 'app_host\t%s\n' "$APP_PW" | sudo bash <检出>/scripts/deploy/host_health_install.sh <私有 app-host 配置>
   ```

   口令从 Keychain 经管道传入（不写进命令行、不进 python heredoc）。预期：安装 ok，`/etc/nexloop/host-health.json` 的容器列表含 `outreach-sender`；下一轮探针之后，`host_unit_missing` 与 `crash_loop` 都不报它。
2. **DATA_HOST 的 host-health**：无变化，不执行，记录“无变化”。
3. **告警规则**：无变化（仓库最新 v21，与上次部署相同），不下发，记录“无变化”。
- **撤回**：私有配置去掉这一项，重跑安装。

### S12 部署门禁

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step gate
```

- 私有配置：`expected_gate_failures: ["real_dispatch_closed"]`；`gate_dir` 的 `--before` 用 `$NEXLOOP_ROOT/ops/baseline-r36/baseline.json`（不要用上一次门禁的 after.json）。
- 预期 **20/21**，唯一不通过的是 `real_dispatch_closed`（真实派发开着，P8）。`secret_distribution` 必须通过：S6 装入的 `secrets/outreach-sender/` 是目录 0700、文件 0600、属 10001。
- 出现别的失败就停，不 promote。
- **撤回**：不需要（只检查）。

### S13 promote 与部署后核对

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step promote
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step post
```

- `promote` 只在本 release 最近一次门禁只因预期项失败时执行；`post` 的 `post_deploy_check` 为 `pass: true`，日志错误统计不超过配置的上限。
- **连接数实测**（L6 建议：连接预算已到 60 / 60，没有余量）：所有服务运行至少 5 分钟后，只读记录一次各登录角色的实际连接数：

  ```bash
  ro "select usename, count(*) from pg_stat_activity where datname=current_database() group by 1 order by 1"
  ro "select count(*) from pg_stat_activity where datname=current_database()"
  ```

  证据里写各角色的数目和总数，与预算对照：应用服务的总数不应超过 60（模拟世界、运维与超级用户的连接另算，见 `deploy/stage/connection-budget.v1.json`）。超过就停下交调度员，不再增加任何服务，并在下次加服务之前先调整预算。
- 只读核对：
  - `ro "select max(version) from control.schema_migrations"` 为 0371；
  - 邀请对照（release 54）：`ro "select count(*) from control.nexloop_workbench_invitations where state='issued'"` 与 S2 记下的数相同（0371 不改变已有邀请；期间有人新发或用掉邀请时，在证据里说明差额来源）；`ro "select count(*) from control.nexloop_workbench_invitations where sso_provider is not null"` 为 0；
  - 实验视图（release 54）由 S14 的 W4 在页面上核对；
  - `ro "select initiator, state, count(*) from runtime.nexloop_outreach_sends group by 1,2 order by 1,2"`：与 S2 相比没有新的 `agent` 行（外联第 5 片的验收按它自己的执行单做）；已有的 `accepted` 行此时会被执行器准入，按各道闸进入终态，这是 G-1 的预期效果；
  - `nexloop-doctor` 全部通过（OPERATIONS §8a.11 第 6 步）；工作台“告警”页没有新的 firing。
- **撤回**：按 known_good 退回 release 52（S9 的整体撤回），并恢复 S10 的 Host 配置。

### S14 浏览器走查（调度员，合成账号 approver3，S13 之后）

stage 工作台，用 approver3（合成 owner）登录；口令从 Keychain 取，只在浏览器的登录框里输入，不进命令行、不进截图。**走查只看不改**：不点任何“确认”提交，打开的编辑器和确认面板都用“取消”或“关闭”退出。每页记录：页面能打开、没有整页空白、没有错误边界的告警框、浏览器控制台没有报错；截图不含口令和地址。

**负责人报过的三处（W1–W3），再加实验空间（W4）**：

| # | 页面（侧栏） | 怎么看 | 预期 |
|---|---|---|---|
| W1 | 运营 › **Action / 异常** | 打开页面，等加载完成；再从别的页面切回来一次 | 页面内容正常显示，**整个工作台不白屏**；加载中和不可用时显示空状态与“重试”；侧栏和顶栏始终在 |
| W2 | 模型与资产 › 企业世界模型 › **Object Type**，切到关系图 | 看默认视图，再点“适应画布”；点一个类型（如 Consumer），再用 Tab/Enter、方向键、+/− 操作 | **每个节点都在画布内**，不再只看到 Consumer 一个和从画布外伸进来的箭头；标签不小于 12 px，可读；“适应画布”后仍框住全部节点；同一对类型间的多条关系分开画；放不下的关系名在选中一端类型时显示。类型超过 64 个时按名称排成网格并提示去看表格视图（stage 目录不到这个数，应是正常的关系图） |
| W3 | **运营人设** | 看列表；点“新建人设”打开编辑器再“关闭”；在某一行点“发布新版本…”再“关闭” | 租户的**每个人设单独一行**，有当前版本、状态和分配位置（“租户默认”、命名它的分配规则或“未分配”）；没有人设时空状态说明可以建多个人设；编辑器在列表下方打开，焦点在标题，关闭后焦点回到原按钮；新人设从版本 1 开始，已占用的标识不能再用；“版本记录”列出每个版本 |
| W4 | **实验空间** | 打开列表和一条实验的详情（没有实验就看空状态） | 侧栏不再标“未启用”；列表只有计数，不出现任何客户；预算演练的实验带“预算演练”标记和提示“预算演练，孪生未校准，结论不代表实验效果”；结束时间的输入提示“按北京时间填写，提交时换算为 UTC”。登记、开始、停止只打开确认面板看效果说明，然后“取消”；未提交时 Escape 和“取消”都能关闭确认面板（“请求进行中不能关闭”走查不提交、看不到，由 `workbench-confirm` 测试覆盖） |

- 任一项不符：截图（不含敏感信息）、记录控制台报错，交调度员；不在 stage 上改任何东西。W1–W3 任一失败就不判本 release 通过。
- **撤回**：不需要（只看）。


## 3. 证据与判定

| 步骤 | 执行者记录 | 判定者（L6）核对 |
|---|---|---|
| S1–S5 | 各步 `stage_release.py` 日志行（已脱敏）、预演与发布前后的计数、S2 的未用邀请数 | 迁移尾 0371、definer 0 problems；预演只有三个新 Action |
| S6–S7 | `add-service` 与 `check` 的输出、§8a.11a 两个方向的差异（各恰好一行）、register 的 `grants_revoked`、`grants_added`、`credentials_created` | 差异与清单 v21、v22 的变化一致；没有额外授权 |
| S8 | `--check` 的 `manifest_sha256`、发布与重放的输出、来源查询结果 | sha256 与 ad8f42dc 的文件一致 |
| S9 | P9 的输出、`docker compose ps`、执行器日志的前几行（只有计数和固定行） | P9 为 ok；日志里没有正文 |
| S10 | 调度员的确认记录、`@csv` 的输出、Host 日志中没有 refused | 确认先于执行 |
| S11 | host-health 安装输出、探针一轮后的结果 | 期望容器包含执行器 |
| S12–S13 | 门禁 JSON 的失败项名单、promote 与 post 的输出、连接数、邀请对照 | 失败项只有 `real_dispatch_closed`；未用邀请数不变，SSO 邀请为 0 |
| S14 | W1–W4 每项的结论、截图、控制台记录 | W1–W3 全部符合预期 |

证据里不贴 DSN、口令、令牌、地址或主机名，只写角色名（APP_HOST、DATA_HOST、CI 主机）。

## 4. 回退总表

| 步骤 | 怎么撤回 | 代价 |
|---|---|---|
| S3 迁移 | 只能用迁移前备份恢复，与镜像一起退回 release 52 | 恢复点之后的数据丢失；需要调度员决定 |
| S4 Action 发布 | 不能删除；撤回 S5 让无人持有授权 | 无 |
| S5 角色 | 重新应用 v46 | 三个新 Action 失去成员授权 |
| S6 秘密树 | 只撤执行器的文件：`<树>.after-s6-1` 覆盖（`before-r53` 只用于 stage 仍是 v20 / v5 时）；删除 APP_HOST 上的新目录 | 无 |
| S7 register | 先撤回 S9 的执行器；再用 `<树>.after-s6-1`（或删掉 outreach-sender 条目的树）配 v21 / v6 清单 register，撤销 outreach_sender 的授权 | 执行器无法认证 |
| S8 审批策略 | 只能发布新版本放宽或停用 | 无删除路径 |
| S9 重建 | 停掉并删除 outreach-sender；或整体退回 release 52 | 已受理的外联意图停在 `accepted` |
| S10 Host 配置 | 备份覆盖回去，两项一起删，重建 agent-host 与 agent-link-relay | 计划复评 Run 失去结论工具和外联工具 |
| S11 host-health | 私有配置去掉执行器，重跑安装 | 无 |
| S12 门禁、S14 走查 | 不需要（只检查、只看） | 无 |
| S13 promote | 同 S9 的整体撤回（known_good 指回 release 52），并恢复 S10 的 Host 配置；迁移不随镜像退回（见 S3） | 同 S3、S9、S10 各行；实验空间与前端修复一起失效 |
