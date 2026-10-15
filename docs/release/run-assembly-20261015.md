# 合成租户组装逐条命令单（stage，2026-10-15 下午）

本单把本地演练 `tests/test_assembly_dryrun_pg.py`（夹具 `tests/assembly_dryrun_fixture.py`）的全链路改写成 stage 上的命令。演练中每个工具都按这里的顺序、以同样的输入跑通过；本单只把路径、身份和主机换成 stage 的。

- 只用角色名与参数，不写主机名、地址、口令或令牌。
- 约定沿用 [deploy-days-20261015](deploy-days-20261015.md)：
  - **B** 表示改动服务器、凭据或数据，每一步都要负责人确认；
  - **sudo-A** 表示 APP_HOST 上由负责人本人输入 sudo；
  - **sudo-D** 表示 DATA_HOST 上由负责人本人输入 sudo。

## 0. 现状、变量与源码树

### 0.1 已完成（不再重复）

- DATA_HOST 上已执行：`register`（含 setup 主体 `business_owner`、`catalog_maintainer`、`role_assembler`，stage-principals v3）、`publish`、`real_tenant_init configure`；
- signing key id 是 `nexloop-stage-20261015`（management/signing.key.hex 为 ASCII 新钥，run-34 之后登记）；
- APP_HOST 的 `config/recall-types.json` 已放好。

### 0.2 变量（管理机与两台主机的 shell 中设置；值不进仓库、不进证据）

| 变量 | 含义 |
|---|---|
| `TENANT_ID` | 合成租户 UUID |
| `BROWSER_APPLICATION_ID`、`WORKBENCH_APPLICATION_ID` | run-34 §2.2 登记的两个应用 |
| `MAIN` | 本单使用的 main 提交，即包含本单的提交 |
| `RUN` | DATA_HOST 上的 stage 管理树（run-34 §1.5 的 `$RUN`，含 `management/`、`setup/`、`boot.dsn`、`identity-seal`） |
| `APP_RUN` | APP_HOST 上本单专用的私有目录，0700，属主 10001，用完删除 |
| `NEXLOOP_ROOT` | 两台主机上的部署根 |
| `KEY_ID` | `nexloop-stage-20261015` |

### 0.3 两种一次性容器

**DATA_HOST**（配置者窗口与只读读取；同 run-34 §1.5 的 `$COMMON`，加 PYTHONPATH）：

```
COMMON="--rm --network none --read-only --user 999:999 --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  -v $RUN/src:/opt/release:ro -v $RUN:/stage:ro -e PYTHONPATH=/opt/release/packages/eios-core/src --entrypoint python"
WINDOW="-v $NEXLOOP_ROOT/run/postgresql:/var/run/postgresql -v $RUN/boot.dsn:/run/bootstrap-dsn:ro"
```

**APP_HOST**（以 `nexloop_api`、`nexloop_identity` 连库的工具）：

- **调用方式更正**：business、catalog、enterprise setup、`context_strategy_setup`、`role_assembly`、`relay_enrollment` 和 Budget.set **都不调用 API 的 HTTP 接口**，而是用 API 自己的 PG 客户端身份直接连 DATA_HOST 的 PG（经 TLS 客户端证书）。
  - 业务 Action 以 `nexloop_api` 执行，登录与会话以 `nexloop_identity` 执行。
  - 所以它们要在 APP_HOST 上、在 api 容器的同一网络命名空间里运行，即 `network_mode: service:app-net`。
  - 网络形态同 run-34 §4.7 的例外：`--network container:<app-net 容器>`。
  - 不经 Gateway。
- **挂载**：只读挂 api 的 DSN 与证书，路径与 compose 中 api-1 的 `/run/secrets/<secret>` 完全一致，因为 DSN 文本里写的就是这些路径。
- **身份**：容器以 10001 运行，因为私有文件要求属主等于进程用户。

```
APP_NET=$(docker compose -p nexloop-app ps -q app-net)
S=$NEXLOOP_ROOT/secrets
APPTOOL="--rm --network container:$APP_NET --read-only --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m --tmpfs /work:rw,noexec,nosuid,size=16m,uid=10001,gid=10001 \
  -v $APP_RUN/src:/opt/release:ro -v $APP_RUN/in:/in:ro -e PYTHONPATH=/opt/release/packages/eios-core/src \
  -v $S/api/dsn:/run/secrets/api_dsn:ro -v $S/api/pg-client.crt:/run/secrets/api_pg_client_cert:ro -v $S/api/pg-client.key:/run/secrets/api_pg_client_key:ro \
  -v $S/api/identity-dsn:/run/secrets/api_identity_dsn:ro -v $S/api/identity-pg-client.crt:/run/secrets/api_identity_pg_client_cert:ro \
  -v $S/api/identity-pg-client.key:/run/secrets/api_identity_pg_client_key:ro -v $S/api/browser-rate.key:/run/secrets/api_browser_rate_key:ro \
  -v $S/shared/db-ca.crt:/run/secrets/pg_ca:ro -v $S/shared/signing.key:/run/secrets/signing_key:ro --entrypoint python"
BACKEND="--database-url-file /run/secrets/api_dsn --signing-key-file /run/secrets/signing_key --signing-key-id $KEY_ID --artifact-root /work/artifacts"
```

`/in` 放本单的私有输入：配方、setup 凭据副本、口令文件。属主 10001，0600；目录 0700。

### 0.4 源码树（管理机，然后分发；B）

发布镜像不含 `role_assembly`、`context_strategy_setup`、`webchat_consumers` 的新代码，所以两台主机都把 `$MAIN` 的源码树只读挂入，PYTHONPATH 优先，同 run-41 §3a.1。

```
git archive $MAIN scripts/deploy scripts/nx037 deploy/authorization deploy/configuration deploy/ontology deploy/examples/nx037 \
  deploy/stage/compose.app.example.yaml deploy/stage/compose.data.example.yaml packages/eios-core/src > assembly-src.tar
# DATA_HOST：tar -x -C $RUN/src（属主 999）；APP_HOST：tar -x -C $APP_RUN/src（属主 10001）
```

- **核对**：`git diff --stat <发布镜像提交> $MAIN -- packages/eios-core/src/eios` 必须为空，即迁移无差异。
- **撤销**：删除这两个目录。

## 1. 账号：owner 与 WebChat 消费者（APP_HOST，`initial_identity --create`；B，sudo-A）

- **身份**：`nexloop_identity`，经 api 的 identity DSN。
- **配额**：run-34 §2.2 发布的初始身份配额——WebChat 10 个，工作台 5 个，`operator_label=stage-owner`，种子 `sha256` 必须匹配。
- **种子**：`$RUN/identity-seal` 在管理机/DATA_HOST，复制一份到 `$APP_RUN/in/identity-seal`（0600，10001），本步结束即删除。
- **要建的账号**：

| 账号 | 应用 | 用途 |
|---|---|---|
| owner | `$WORKBENCH_APPLICATION_ID` | 工作台 owner：发布 Context 策略、Budget.set、接管回复 |
| CUSTOMER_1..4、REFUSAL_1..2 | `$BROWSER_APPLICATION_ID` | 交互计划 v3 的 6 个消费者（由负责人操作的 AI agent 扮演，`trial-participants.example.json`） |

- **每个账号**：
  - 由 `deploy/examples/nx037/identity-human.example.json` 填一份清单，放 `/in/identity-<名>.json`。占位符换成合成值，例如 `SUBJECT_ID=stage-human-<名>`、`PRINCIPAL_ID=stage-principal-<名>`、`USERNAME` 用 `<名>`、`EMAIL` 用 `.invalid` 域，`NOW_UTC_ISO` 取当前时刻减几秒；
  - 口令文件 `/in/password-<名>`，至少 12 个字符，由负责人生成后存入密码管理器。

```
docker run $APPTOOL "$BACKEND_IMAGE" -m nexloop_eios.initial_identity --manifest /in/identity-<名>.json --create \
  --identity-database-url-file /run/secrets/api_identity_dsn --password-file /in/password-<名> --idempotency-key-file /in/identity-seal
```

- **预期**：`created: true`。同一清单再执行一次是幂等的，不会再建。
- **核对**（bootstrap，只读）：`select application_id, created_accounts from control.nexloop_initial_identity_allowances where tenant_id='$TENANT_ID'`：工作台 1，WebChat 6。
- **记下**：每个账号的 `principal_id` 与 `subject_id`（取自清单），后面几步要用。
- **撤销**：账号不能删除；账号可以停用（NX-029 路径）。配额只增不减，所以清单要先核对再执行。
- **演练出处**：`create_human`。

## 2. 工作台授权：`workbench_roles --apply`（DATA_HOST，配置者窗口；B，sudo-D）

成员文件 `$RUN/workbench-members.json`（0600，999），格式见 `workbench_roles` 模块注释：
`{"schema_version":"nexloop-workbench-members/1","tenant_id":…,"application_id":"$WORKBENCH_APPLICATION_ID","caller_application_id":"eios:application:$WORKBENCH_APPLICATION_ID","members":[{"principal_id":<owner>,"subject_id":<owner>,"role":"owner"}]}`。

```
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/workbench_roles.py --roles /opt/release/deploy/authorization/workbench-roles.v1.json \
  --members /stage/workbench-members.json --apply --bootstrap-dsn-file /run/bootstrap-dsn --signing-key-file /stage/management/signing.key.hex --signing-key-id $KEY_ID
```

- **入口**：`workbench_roles.main(window=…)`。执行顺序按 0140：先成员，再 grant 事实（0050），最后工作台的 0045 业务应用行。
- **预期**：成功退出；再执行一次结果相同。
- **核对**：
  - `select caller_application_id from control.nexloop_browser_business_applications where tenant_id='$TENANT_ID' and application_id='$WORKBENCH_APPLICATION_ID'`：一行；
  - 配置者 NOLOGIN 且无口令。
- **撤销**：以空成员列表重新 apply（按模块注释的顺序，先撤销 grant 再缩小成员）。
- **演练出处**：`owner_workbench`。

### 2a. 撤出工作台成员：`--revoke-member`（DATA_HOST，配置者窗口；B，sudo-D）

**适用**：成员不再可用，例如 owner 账号建号时口令输错、已无人知道口令。改由另一个账号担任 owner。该账号要先按 §1 建好，并已加入成员文件。

**步骤**：
1. 新成员文件 `$RUN/workbench-members.json`：去掉被撤成员，保留其余成员，并加上新 owner。被撤成员**不得**仍在文件中，否则工具拒绝且不写任何东西。
2. 执行（`--revoke-member` 可重复）：

```
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/workbench_roles.py --roles /opt/release/deploy/authorization/workbench-roles.v1.json \
  --members /stage/workbench-members.json --apply --revoke-member <被撤成员的 principal_id> \
  --bootstrap-dsn-file /run/bootstrap-dsn --signing-key-file /stage/management/signing.key.hex --signing-key-id $KEY_ID
```

- **顺序**（`workbench_roles.apply_revocations`，按模块注释与 0140）：
  1. 先经 0050 为被撤成员的每个工作台 Action 写入**空授权集合**；撤销是一次写入，不删除；
  2. 再按新成员文件 apply，即应用事实与 0045 行、成员列表、其余成员的授权。
- **预期**：`revoked: [<principal>]`，`revoked_grant_sets` 等于工作台角色清单中的 Action 数，`members` 为剩余人数。再执行一次结果相同。
- **核对**（bootstrap，只读）：
  - `select payload->'grants' from authz.nexloop_authority_facts where tenant_id='$TENANT_ID' and fact_kind='grants' and entity_key[1]='<被撤成员>'`：全部为 `[]`；
  - 配置者 NOLOGIN 且无口令；
  - 被撤成员仍能登录（账号未停用），但执行 Budget.set、发布 Context 策略等工作台 Action 时都返回 `ActionAuthorizationDenied`。
- **新 owner 接手**：
  - §4 的 Context 策略：若还未发布，由新 owner 执行。已发布的策略不受影响，`published_by` 仍记录原发布者；
  - §5 的 Budget.set：由新 owner 执行。用户名与口令文件换成新 owner 的。
- **撤销**：把该成员重新加入成员文件，再普通 `--apply`，授权会按角色重新写入。
- **演练出处**：`test_a_revoked_workbench_owner_is_refused_and_the_remaining_owner_is_not`。

## 3. WebChat 消费者授权：`webchat_consumers --apply`（DATA_HOST；B，sudo-D）

配方 `$RUN/webchat-consumers.json`：由 `deploy/examples/nx037/webchat-consumers.example.json` 填出，`consumers` 列 6 个账号的 `principal_id`、`subject_id`，`persona` 均为 `simulated_agent`。

```
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/webchat_consumers.py --role /opt/release/deploy/authorization/webchat-consumer-role.v1.json \
  --recipe /stage/webchat-consumers.json --apply --bootstrap-dsn-file /run/bootstrap-dsn --signing-key-file /stage/management/signing.key.hex --signing-key-id $KEY_ID
```

- **预期**：`configured: true`，`granted: 6`。再执行一次结果相同，`authority_revision` 仍会加 1。
- **核对**：
  - `control.nexloop_browser_business_applications` 中 `$BROWSER_APPLICATION_ID` 一行，`caller_application_id = eios:application:nexloop-webchat`，`requested_scopes = {action.execute,function.execute}`；
  - 每个消费者的 grant 事实恰好是 `webchat-consumer-role.v1.json` 的 5 个资源。
- **撤销**：在配方中对该消费者写 `revoke: true` 后重新 apply（授权集合置空，不删除）。
- **演练出处**：`apply_webchat_consumer_role`；`test_the_webchat_consumer_opens_a_conversation_and_writes_after_the_consumer_role_is_applied`。

## 4. Context 策略：`context_strategy_setup`（APP_HOST，owner 本人；B，sudo-A）

- **身份**：owner 本人（工作台登录，0088 只允许人发布），经 workbench roles v11 持有 `nexloop.context.strategy.publish`。
- **私有输入**：
  - `/in/owner-username`、`/in/owner-password`：owner 的工作台用户名与口令，只放文件，不作参数，不打印；
  - 配方 `/in/context-strategy-setup.json`：由 `context-strategy-setup.example.json` 填 `TENANT_ID` 与 `WORKBENCH_APPLICATION_ID`。`request_id` 保持示例值，它是稳定的业务意图 ID。

```
docker run $APPTOOL "$BACKEND_IMAGE" -m nexloop_eios.context_strategy_setup --recipe /in/context-strategy-setup.json \
  --strategies-file /opt/release/deploy/configuration/context-strategies.v1.json $BACKEND --identity-database-url-file /run/secrets/api_identity_dsn \
  --username-file /in/owner-username --password-file /in/owner-password --browser-rate-key-file /run/secrets/api_browser_rate_key \
  --browser-origin "$BROWSER_ORIGIN"
```

- `$BROWSER_ORIGIN` 取 compose 环境文件中的同名值，即 Gateway 的 https origin。登录速率限制按它计数，每次执行消耗一次 `local_login`。
- **预期**：`strategy_ref = context-strategy:recent_plus_required@1`，`replayed: false`；再执行一次 `replayed: true`，`outcome_id` 相同。输出不含用户名与口令。
- **核对**：`select strategy_id, version, published_by from control.nexloop_context_strategies where tenant_id='$TENANT_ID'`：`recent_plus_required|1|<owner principal>`。
- **撤销**：策略只能发布新版本，不能删除。
- **演练出处**：`publish_strategy`；`test_the_owner_publishes_the_relay_strategy_and_the_relay_issues_the_run`。

## 5. 试运行预算 Budget.set：`real_tenant_init.py budget`（APP_HOST，owner 本人的会话；B，sudo-A）

- **请求内容**：与 `real_tenant_init.py plan` 打印的 `model_budget` 步相同（`budget_request`），即累计上限 100（trial 的 `cost_caps.cumulative`），币种与期间同 configure。
- **身份**：owner 本人。子命令按工作台登录的方式登录：`context_strategy_setup.owner_session`，使用私有的用户名与口令文件和登录速率限制。然后以 owner 的浏览器业务授权（0045）提交 `Budget.set`，结束后撤销会话。没有 owner 成员身份（即没有 `Budget.set` 授权）的人会被拒绝。
- **私有输入**（`/in`，10001，0600）：
  - `owner-username`、`owner-password`：同 §4；
  - 值目录 `value-catalog.json`：同 configure；
  - 配方 `budget-recipe.json`：`{"schema_version":"nexloop-budget-setup/1","tenant_id":"$TENANT_ID","workbench_application_id":"$WORKBENCH_APPLICATION_ID","browser_origin":"$BROWSER_ORIGIN"}`。配方里没有秘密。

```
docker run $APPTOOL "$BACKEND_IMAGE" /opt/release/scripts/nx037/real_tenant_init.py budget --tenant "$TENANT_ID" \
  --owner-principal "$OWNER_PRINCIPAL_REF" --value-catalog /in/value-catalog.json --channel-ref "$CHANNEL_REF" \
  --period-start "$PERIOD_START" --period-end "$PERIOD_END" --recipe /in/budget-recipe.json \
  --username-file /in/owner-username --password-file /in/owner-password $BACKEND \
  --identity-database-url-file /run/secrets/api_identity_dsn --browser-rate-key-file /run/secrets/api_browser_rate_key
```

- 参数与 run-34 / run-41 §4.2 的 configure 完全相同：`$OWNER_PRINCIPAL_REF`、`$CHANNEL_REF`、试运行期间。`$BACKEND` 见 §0.3。
- **预期**：
  - `{"mode":"budget","executed":true,"budget_kind":"model","limit_amount":"100",…,"replayed":false}`；
  - 再执行一次 `replayed: true`，`request_id` 相同。`request_id` 由租户与期间开始时间决定，是稳定的业务意图 ID；
  - 输出不含用户名与口令。
- **失败**：输出 `{"ok":false,"detail":…}`，退出码 2，不写任何东西。口令文件不是 0600 或属主不对时，输出 `PermissionError`。
- **核对**：
  - `real_tenant_init.py verify --metrics-dsn-file …`：`ready: true`，模型预算等于累计上限并覆盖试运行期间；
  - 或者 `select authz.nexloop_nx037_tenant_state('$TENANT_ID','real')` 中 `budgets.model.limit` 为 100。
- **撤销**：用新的 Budget.set 改值（换期间，即换意图 ID）；预算只有当前值。
- **演练出处**：`set_trial_budget`；负例见 `test_budget_cli_refuses_a_non_owner_and_a_password_file_that_is_not_private`。

## 6. 每个消费者的业务组装（APP_HOST，6 次；B，sudo-A）

对每个消费者 `<C>`（CUSTOMER_1..4、REFUSAL_1..2）依次执行 6.1–6.3，记下各自的输出 JSON（只含 ID）。

**setup 凭据**：`setup/` 下的副本复制到 `/in`（10001，0600），全部步骤结束后删除：

| `/in` 文件 | 来源（管理树 `setup/`） | 主体 |
|---|---|---|
| `business-owner-credential` | `business-owner-credential` | `business_owner`（EffectControl owner，也负责 relay 登记） |
| `business-executor-credential` | `business-executor-credential` | `effect_executor` 的同一令牌 |
| `catalog-maintainer-credential` | `catalog-maintainer-credential` | `catalog_maintainer` |
| `catalog-source-credential` | `catalog-source-credential` | `relay_source` 的同一令牌 |
| `role-assembler-credential` | `role-assembler-credential` | `role_assembler`（`ontology.roles.manage`） |

**配方**：都来自 `deploy/examples/nx037/`，下表列出各自要填的私有值。

| 步骤 | 示例 | 要填的值 |
|---|---|---|
| 6.1 business | `business-recipe.example.json` | `NEW_UUID`（每个消费者一个新 UUID，固定后重放用同一个）、`CONSUMER_PRINCIPAL_ID`（§1 中该账号的 principal）、`TRIAL_PERIOD_END_UTC_ISO` |
| 6.2 catalog | `catalog-recipe.example.json` | `NEW_UUID`、`CONSUMER_ID_FROM_BUSINESS_SETUP`（6.1 输出的 `consumer_id`）、期末 |
| 6.3 enterprise | `enterprise-setup-recipe.example.json` | `NEW_UUID`、consumer_id、`SERVICE_CODE_SUFFIX`、`OFFERING_TITLE`、`CNY_PRICE_AMOUNT`、期末 |

```
docker run $APPTOOL "$BACKEND_IMAGE" -m nexloop_eios.business_setup $BACKEND --owner-credential-file /in/business-owner-credential \
  --executor-credential-file /in/business-executor-credential --recipe-file /in/business-<C>.json
docker run $APPTOOL "$BACKEND_IMAGE" -m nexloop_eios.catalog_setup $BACKEND --maintainer-credential-file /in/catalog-maintainer-credential \
  --source-credential-file /in/catalog-source-credential --recipe-file /in/catalog-<C>.json
docker run $APPTOOL "$BACKEND_IMAGE" -m nexloop_eios.enterprise_setup $BACKEND --owner-credential-file /in/business-owner-credential \
  --executor-credential-file /in/business-executor-credential --maintainer-credential-file /in/catalog-maintainer-credential \
  --source-credential-file /in/catalog-source-credential --recipe-file /in/enterprise-<C>.json
```

- **预期**：
  - business：`configured: true`，`owner_ref = principal:nexloop-service-business-owner-principal`，`executor_ref = principal:nexloop-service-effect-executor-principal`，并输出 `consumer_id`、`control_id`；
  - catalog：`source_ref = principal:nexloop-service-relay-source-principal`，并输出 `offering_id`、`binding_id`；
  - enterprise：输出 `enterprise_control_id`、`enterprise_offering_id`、`enterprise_binding_id`；
  - 同一配方再执行一次，返回相同的对象。
- **核对**（bootstrap，只读）：每个消费者有 1 个 Consumer、2 个 EffectControl、2 个 ServiceOffering、2 个 ConsumerServiceOffering、1 个 ConsumerOwnership。
- **撤销**：对象只能经受治理 Action 编辑（`allow_effect=false`、`active=false`），不删除；配方错误时换新的 `request_id` 重建，旧对象停用。
- **交互计划**：`nx037-interaction-plan.v3.json` 中的 `consumer:<CUSTOMER_n>` 换成 `consumer:<该消费者的 consumer_id>`。
- **演练出处**：`assemble`。

## 7. 角色组装：`role_assembly`（APP_HOST，每个消费者一次；B，sudo-A）

配方由 `role-assembly.example.json` 填出：`consumer_id`、`control_id`（6.1 的输出），`NOW_UTC_ISO`（固定的 `valid_from`，重放时不改），期末，`ROLE_NAME`、`ROLE_RESPONSIBILITY`（合成描述），`PER_RUN_COST_CAP`、`TRIAL_CURRENCY`（与 trial v3 一致：2、CNY）。`source_principal_id` 保持示例值 `nexloop-service-role-source-principal`。

```
docker run $APPTOOL "$BACKEND_IMAGE" -m nexloop_eios.role_assembly $BACKEND --assembler-credential-file /in/role-assembler-credential \
  --recipe-file /in/role-<C>.json
```

- **身份**：`role_assembler`。它的凭据带 `ontology.roles.manage`，0057/0081 要求调用者有这个 scope。
- **预期**：输出 `goal_id`、`step_id`、`ceiling_id`、`role_id`、`scope_id`、`link_id`、`role_ref = role:<role_id>`。同一配方再执行一次，返回相同的 ID。
- **核对**：
  - RoleDefinition 的 `ceiling_ref` 等于 ceiling；
  - link 的 `consumer_id`、`role_id`、`scope` 一致，`active=true`；
  - PlanStep 的 `control_id` 等于该消费者的 EffectControl，`submitter_principals` 为 role source。
- **撤销**：`ConsumerRoleLink.edit active=false`（需要另行授予 edit；本单不授予）。
- **演练出处**：`assemble_role`；`test_the_assembled_role_is_what_a_role_run_binds`。

## 8. relay 登记（APP_HOST，每个消费者一次；B，sudo-A）

登记文件由 `relay-enrollment-enterprise.example.json` 填出：6.1–6.3 的 ID；`relay_principal` 用 `nexloop-service-message-relay-principal`（stage 授权清单中 `message_relay` 的 principal_id）；`intent_id = relay-enroll-<新 UUID>`。再加上 §7 的 `role_id`、`link_id`、`step_id` 三个键，三者必须同时出现。

```
docker run $APPTOOL "$BACKEND_IMAGE" -m nexloop_eios.relay_enrollment $BACKEND --owner-credential-file /in/business-owner-credential \
  --enrollment-file /in/enroll-<C>.json
```

- **身份**：`business_owner`，即 EffectControl 的 owner。0160 会复核 owner、control、offering、binding、企业槽位和角色三件是否都属于该消费者（0180 第 129–135 行）。
- **预期**：`{"enrolled":true,...}`。同一意图再执行是重放；换新意图且内容相同也是重放。
- **核对**：`select role_id, link_id, step_id, enterprise_control_id from control.nexloop_relay_enrollments where tenant_id='$TENANT_ID' and consumer_id='<consumer_id>' order by enrollment_revision desc limit 1`。
- **撤销**：`relay_enrollment` 的 `withdraw`（`nexloop.relay.withdraw:1`，`business_owner` 持有授权）。
- **演练出处**：`assemble` 的登记段，以及 `assemble_role` 的重新登记段。

## 9. relay-run-profile.json（DATA_HOST 生成，APP_HOST 放置；B，sudo-D、sudo-A）

**生成**（DATA_HOST，bootstrap 只读事务；只写新文件）：取 CUSTOMER_1 的 `role_id`。消息 Run 里 `role_ref` 只是一个标签，见判定与 0063，所以部署级 profile 用一个已发布的 Role 即可。

```
docker run $COMMON -v "$NEXLOOP_ROOT/run/postgresql:/var/run/postgresql" -v "$RUN/boot.dsn:/run/bootstrap-dsn:ro" -v "$RUN/out:/out" \
  "$BACKEND_IMAGE" /opt/release/scripts/deploy/tenant_config.py relay-run-profile --tenant "$TENANT_ID" --bootstrap-dsn-file /run/bootstrap-dsn \
  --out /out/relay-run-profile.json --role-id <CUSTOMER_1 的 role_id> --maximum-cost 2 --currency CNY
```

（`$RUN/out` 是新建的 0700 目录，属主 999；`--read-only` 下只有它可写。）

- **预期**：`{"ok":true,"command":"relay-run-profile","queue":"operations","runtime_profile":"deepseek-flash"}`。
- 文件内容：
  - `role_ref = role:<role_id>`；
  - `context_manifest_ref = artifact:context-bind-pending`；
  - budget 8/8/120/2/CNY；
  - `runtime_owner_epoch` 1；
  - queue `operations`。

> **提醒**：`role_ref` 不得改成 `role:<id>:mapping:<id>` 形式。0063 对没有 Role 绑定、却带这种 `role_ref` 的 Run 一律拒绝（`bound role mandatory`），那样每条消息的 Run 都会失败。只用 `tenant_config` 生成的 `role:<id>`。

**放置**（APP_HOST，sudo-A）：上次启动时宿主机上没有这个文件，Docker 自建了一个空**目录** `config/relay-run-profile.json`。所以要先停掉挂载它的服务，再删目录、放文件：

```
docker compose -p nexloop-app stop message-relay message-relay-2 reply-guarantor
sudo test -d $NEXLOOP_ROOT/config/relay-run-profile.json && sudo rmdir $NEXLOOP_ROOT/config/relay-run-profile.json   # 只删空目录；非空则停下核对
sudo install -o 10001 -g 10001 -m 0600 relay-run-profile.json $NEXLOOP_ROOT/config/relay-run-profile.json
```

- **核对**：
  - `sudo stat -c '%U %a %F' $NEXLOOP_ROOT/config/relay-run-profile.json`：`10001 600 regular file`；
  - 内容中 `role_ref` 以 `role:` 开头，且不含 `:mapping:`。
- **撤销**：stop 三个服务，删除文件。
- **演练出处**：`tenant_configuration`。

## 10. provider routes（管理机生成，APP_HOST 替换占位；B，sudo-A）

```
scripts/deploy/provider_routes.py --out-dir <新私有目录> --webchat-delivery-port <compose 环境中的 WEBCHAT_DELIVERY_PORT> --enterprise-placeholders
```

- **输出**：`provider-routes`、`webchat-provider-config`、`enterprise-provider-config`（`{}`）、`enterprise-provider-credential`（随机）。
  - 文件里写的是容器路径 `/run/secrets/...`；
  - 只路由 `nexloop.service.request`，企业 Action 不路由（没有企业端点，属阶段 C）。
- **替换**：APP_HOST 上 `secrets/action-worker/` 下的同名占位文件逐个替换，`install -o 10001 -g 10001 -m 0600`。`enterprise-provider-ca.crt` 保留现有内容。
- **核对**：替换后 `stage_secrets.py check --out …` 不再报 `pending` 中的这几项（manual）。
- **撤销**：恢复原占位文件，然后 stop action-worker。

> **对 36 次交互的影响**：`real_delivery`（6 次）的企业交付在没有企业端点时是 `unrouted`，不派发、不准入，意图留在队列里，直到补上路由。其余 4 类不受影响。

## 11. 重建 worker 与总核对（APP_HOST，sudo-A；B）

```
docker compose -p nexloop-app up -d --force-recreate action-worker webchat-delivery message-relay message-relay-2 reply-guarantor recall-indexer
docker compose -p nexloop-app ps
```

**核对**（只读）：
- **进程**：上述服务处于 running，不再重启；`docker compose logs --tail 20 <服务>` 只有 ready 行，没有 `unavailable`。
- **v6 上下文**：WebChat 消费者登录并发出第一条消息后，relay 的一轮应是 `queued`，`runtime.nexloop_message_routes` 中有该消息。前提是 §4 的策略已发布，否则会停在 v6 上下文准备。
- **激活**：runtime-worker 认领 `operations` 队列，见 D-QUEUE 修复。Agent Host 与模型 key 属阶段 C；没有它们时 Run 停在 runtime 派发。
- **租户状态**：`real_tenant_init.py verify --metrics-dsn-file …` 输出 `ready: true`，三层真实派发开关全部关闭（D9）。
- **relay Source**：`nexloop-doctor --relay-source …` 通过，因为已有登记的消费者。

**收尾**：删除 `$APP_RUN`，包括 `/in` 中的凭据副本、口令、种子和配方。管理树中的配方与输出 JSON 按私有证据保存，不进仓库。

## 12. 剩余缺口与限制

| ID | 内容 | 影响 | 建议 |
|---|---|---|---|
| G-BUDGET-CLI | **已解决**：`real_tenant_init.py budget`（§5） | — | — |
| G-ENTERPRISE | 企业端点属阶段 C；`--enterprise-placeholders` 不路由企业 Action | `real_delivery` 的企业交付为 `unrouted` | 阶段 C 提供端点后，用 `--enterprise-origin`、`--enterprise-connect-address` 重新生成路由 |
| G-PLAN / G-ROLE-READ | 已判定不阻塞，放到试运行之后 | plan-reevaluator 空转 | — |
| 未在 stage 验证 | 本单的 APP_HOST 一次性容器形态（`--network container:` 加 api secrets 挂载）只在演练中以等价方式验证：同一角色、同一 DSN 形态，用本地 trust DSN | 第一次执行时可能遇到 PG 防火墙或证书路径问题 | §1 第一个账号（owner）就是连通性试跑：它失败时不写任何东西，按报错核对防火墙与证书路径后再继续 |
