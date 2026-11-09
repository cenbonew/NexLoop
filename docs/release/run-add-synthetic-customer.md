# 运行单：在 stage 新建一个能发言的合成顾客（customer-5）

用途：NX-038 P5 需要至少 4 个合成 Consumer 有“失败且带检索未命中”的 Run（ADR-045 演练，见 `docs/release/run-synthetic-model-fault.md`）。customer-1..4 用不了：customer-1/2/3 的 WebChat 授权已在 10-30 撤销，customer-4 已擦除。所以新建第 4 个：customer-5。只有 3 个顾客时，演练按 3×3=9 次进行，不需要本单。

本单只把已有的几份运行单串起来，每一步写明引用哪一节、customer-5 与那一节有什么不同：
- `docs/release/run-assembly-20261015.md`（下称“组装单”）的 §1、§3、§6–§8；
- `docs/release/run-s6-stage-prep.md` §2.2a（口令、开会话、标记的脚本）。

所有命令用角色名（管理机、DATA_HOST、APP_HOST），变量沿用组装单 §0.2。私有文件 0600，放在 `local-only/customer-5-<日期>/`，不进仓库。执行者是调度员；带“负责人在终端执行”的步骤由负责人执行。

## 0. 前提核对（只读，DATA_HOST，`ro "…"`，`$T` 为租户）

1. **WebChat 初始身份配额还有空位**（组装单 §1：WebChat 共 10 个，只增不减）：

   ```bash
   ro "select application_id, created_accounts from control.nexloop_initial_identity_allowances where tenant_id='$T'"
   ```

   WebChat 那一行要同时满足：`enabled` 为 true，并且 `created_accounts` 小于 `maximum_accounts`。不满足时，走下面的 0a。2026-11-09 stage 读数：WebChat `enabled=false`，10/10，已满且已关闭，所以要走 0a。

### 0a. 名额不足时：负责人发布一个新的 WebChat 应用 id，带 5 个名额（负责人 2026-11-09 决定 5A）

**为什么不能把 10 改成 15**：
- 配额以 (tenant, application_id) 为主键，每个应用只有一行（`0050_trusted_configuration.sql:32-37`）。
- 0050 清单的写入只插入新行（`on conflict do nothing`）。行已存在时，只要 `maximum_accounts`、`operator_label`、种子摘要有任何一项不同，或者给已关闭的行发送 `enabled: true`，就拒绝（`configuration conflict`，40001）。当前定义在 `0381_definition_read_rule.sql:236-247`，注释原话是 “never reset/reopened, nor quota replenished by revision”。
- 建账号时，计数加 1 后达到上限就自动关闭（`0352_member_erasure.sql:501-502`）；已关闭或已满的配额拒绝 `initial identity closed`（同文件 480 行）。
- `stage_secrets.py browser` 在发送前也会拒绝：`identity allowance exists with other values (never reset)`（`_browser_pending`、`apply_browser`）。
- 所以在现有的 WebChat 应用上加名额，没有受治理的路径；要加，只能改迁移。本次不改。

**可行的受治理路径：新应用 id、新配额行**：
- 0050 的同一入口（配置者签名清单）登记一个新的浏览器应用 `<WebChat 应用 id>-b`（active），同时给它一行配额：enabled，`maximum_accounts` 5，`operator_label` 和种子摘要与 run-34 §2.2 相同。这是新的主键，不触发上面的冲突。原来两行不动：WebChat 仍为 false、10/10，工作台不变。
- 新账号能从现有的 WebChat 登录：登录只要求“登录用的那个应用” active，按 tenant 加 username 找账号，账号本身不带应用 id（`0021_browser_identity_canonical_reads.sql` 的 `control.nexloop_read_browser_identity`）。WebChat 前端和 api 的 `BROWSER_APPLICATION_ID` 不用改。新应用 id 只在 `initial_identity --create` 时用来扣名额，并记在 receipt 里。
- 不发送限速策略：限速按租户加 action 计，已经登记过。
- 证据：
  - `tests/test_trusted_configuration_pg.py::test_a_full_allowance_is_never_raised_or_reopened_and_a_new_application_adds_accounts_that_sign_in_through_the_first`，在真实 PG 上验证四件事：
    - 已满的配额改为更大的上限，或者重新 enabled，都被拒绝，原行不变；
    - 新应用加 5 个名额被接受；
    - 新应用下建出的账号计数为 1；
    - 这个账号经第一个应用登录、发会话成功。
  - `tests/test_stage_secrets.py`：新参数只发送新应用这一行；重跑不发布；把 WebChat 改成 15 会被拒绝。

**工具**：`stage_secrets.py browser` 新增 `--additional-application-id`、`--additional-accounts`（默认 5）。带这两个参数时，只发布新应用及其配额，其余部分与 run-34 §2.2 完全相同：同一个配置者窗口、同一个 identity-seal、同一个签名密钥。

**步骤**（负责人，DATA_HOST 配置者窗口，与 run-34 §2.2 同一个一次性容器形态）：

1. 填私有值：复制 `deploy/examples/webchat-allowance-additional.example.env` 到 `local-only/`，0600，填入占位符，然后 `source`。文件里没有秘密，种子和 bootstrap DSN 仍是文件（`$RUN/identity-seal`、`$RUN/boot.dsn`）。新 id 必须是没有登记过的，例如 `<WebChat 应用 id>-b`。
2. dry-run（不连库，只打印将要发布的内容）：

   ```bash
   docker run $COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/stage_secrets.py browser --out /stage --tenant "$TENANT_ID" \
     --browser-application-id "$BROWSER_APPLICATION_ID" --workbench-application-id "$WORKBENCH_APPLICATION_ID" \
     --additional-application-id "$ADDITIONAL_APPLICATION_ID" --additional-accounts "$ADDITIONAL_ACCOUNTS" --operator-label "$OPERATOR_LABEL" \
     --identity-seal-file /stage/identity-seal --dry-run
   ```

   预期：`dry_run: true`，`applications` 只有新 id，`rate_policies` 为空，`allowances` 为 `{新 id: 5}`。

3. 正式发布（写入只经配置者窗口；当前值经 bootstrap socket 在只读事务里读取）：

   ```bash
   docker run $COMMON -v "$NEXLOOP_ROOT/run/postgresql:/var/run/postgresql" -v "$RUN/boot.dsn:/run/bootstrap-dsn:ro" \
     "$BACKEND_IMAGE" /opt/release/scripts/deploy/stage_secrets.py browser --out /stage --tenant "$TENANT_ID" \
     --browser-application-id "$BROWSER_APPLICATION_ID" --workbench-application-id "$WORKBENCH_APPLICATION_ID" \
     --additional-application-id "$ADDITIONAL_APPLICATION_ID" --additional-accounts "$ADDITIONAL_ACCOUNTS" --operator-label "$OPERATOR_LABEL" \
     --identity-seal-file /stage/identity-seal --bootstrap-dsn-file /run/bootstrap-dsn --signing-key-id "$SIGNING_KEY_ID"
   ```

   预期：第一次 `changed: true`，`authority_revision` 加 1；重跑 `changed: false`。如果报 `never reset`，说明新 id 已经登记过、但值不同：停下，不要换参数重试，先只读查清楚。

4. 只读核对（`ro "…"`）：

   ```bash
   ro "select application_id, enabled, maximum_accounts, created_accounts from control.nexloop_initial_identity_allowances where tenant_id='$T' order by 1"
   ro "select application_id, active from control.nexloop_browser_applications where tenant_id='$T' order by 1"
   ```

   - 新 id：`enabled` true，`maximum_accounts` 5，`created_accounts` 0；在应用表里 active。
   - 原 WebChat：仍为 false、10/10；工作台不变；配置者恢复为 NOLOGIN。
   - 合计 WebChat 名额 10+5=15，已用 10。这就是“maximum 15、created 10、enabled true”的落地形式：分成两行，不是一行。

5. 后续第 3 步建账号时，`identity-customer-5.json` 的 `application_id` 填新 id，不填 `$BROWSER_APPLICATION_ID`。`operator_label` 和 identity-seal 不变。建成后，新 id 那一行 `created_accounts` 为 1；登录仍走现有 WebChat。

**备选（不做，只列出）**：如果一定要单行 15，需要新迁移重定义 `control.nexloop_configure_manifest`，允许“提高上限并重新启用”。这会改变 0050 “never reset” 的不变量，要有 ADR 和负责人批准。最小方案是：只允许 `maximum_accounts` 增大，同时带新的种子摘要，并写一条审计记录。目前不需要。

2. **customer-5 是否已经存在**：钥匙串里已有同名旧条目 `nexloop/stage/customer-5`，可能是 11-07 按 §2.2a 第 1 步生成的。先查账号是否已经建过：

   ```bash
   ro "select a.payload->>'status', m.principal_id from control.nexloop_browser_accounts a join control.nexloop_browser_memberships m on m.tenant_id=a.tenant_id and m.subject_id=a.subject_id where a.tenant_id='$T' and a.username='customer-5'"
   ```

   - **没有行**：账号未建。旧钥匙串条目没有对应的账号，可以覆盖（见第 2 步）。
   - **有行，状态 active**：账号已建，**不要**再执行第 3 步的 `--create`，也不要覆盖钥匙串。记下 `principal_id`，从第 4 步开始。如果口令无人知道，按组装单 §1a 用 `identity_password_reset` 重置，新口令直接写入钥匙串。
   - **有行，状态不是 active**：停下，报告负责人。

3. **ADR-045 窗口外**：没有打开的故障开关，也没有在运行的 NX-037 run 入口（同 run-synthetic-model-fault 的前提）。

## 1. 私有值（调度员先填好，后面各步共用）

| 值 | 取法 |
|---|---|
| `SUBJECT_ID` | `stage-human-customer-5` |
| `PRINCIPAL_ID` | `stage-principal-customer-5-<12 位随机 hex>`（与已有账号同一形式） |
| `LOCAL_ACCOUNT_ID` | 新 UUID |
| `USERNAME` | `customer-5` |
| `EMAIL` | `customer-5@<合成域>.invalid` |
| 三个配方的 `NEW_UUID` | 每个配方各一个，固定后重放用同一个 |
| 期末 `TRIAL_PERIOD_END_UTC_ISO` | `2026-12-01T00:00:00Z`，与 s6c-1/2/3 的装配一致（run-outreach-slice5 §开头） |

**授权有效期**：WebChat 的 5 个授权没有到期时间。`webchat_consumers` 经 `workbench_roles.human_facts` 写入，`valid_until` 为空，一直有效到撤销为止。按期失效的是装配：第 5 步三个配方和第 6 步角色配方里的期末，到期后 relay 不再为它服务。所以这里只有装配期末一个日期需要定。

## 2. 口令（负责人在终端执行）

用 run-s6-stage-prep §2.2a 脚本的 `--apply password`：口令直接生成进钥匙串 `nexloop/stage/customer-5`，不打印、不落文件。

- 脚本发现条目已存在时拒绝执行（`exists`）。只有第 0 步第 2 条判定“没有行”时，才由负责人先删除旧条目，再执行 `--apply password`。删除和生成都按负责人自己的钥匙串规则（vault-index 同步更新）。
- 需要的输入只有一项：钥匙串服务名 `nexloop/stage/customer-5`，账户名 `customer-5`。

## 3. 浏览器账号（负责人在终端执行，APP_HOST）

1. 调度员用第 1 步的值，由 `deploy/examples/nx037/identity-human.example.json` 填出 `identity-customer-5.json`：`application_id` 为 `$BROWSER_APPLICATION_ID`（走了 0a 时，填 0a 的新 id），`operator_label` 与组装单 §1 相同，`NOW_UTC_ISO` 取当前时刻减几秒。放到 APP_HOST 的 `/in/identity-customer-5.json`（10001，0600），同时放一份 `identity-seal`（组装单 §1，本步结束后删除）。
2. 负责人先执行 §2.2a 脚本的 dry-run（不带参数），清单 `--check` 通过后，再执行 `--apply identity`。口令经 ssh 的 stdin 进入容器内的 tmpfs，不落盘。
3. 预期 `created: true`。重跑同一清单是幂等的。
4. 核对：第 0 步第 2 条的 SQL 应返回一行，状态 active，`principal_id` 等于清单里的值。

## 4. WebChat 授权（DATA_HOST，配置者窗口）

在私有配方 `$RUN/webchat-consumers.json` 的 `consumers` 里**追加**一项，其余项不动：

```json
{"principal_id": "<PRINCIPAL_ID>", "subject_id": "stage-human-customer-5", "persona": "simulated_agent"}
```

先检查，再发布：

```bash
docker run $COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/webchat_consumers.py --role /opt/release/deploy/authorization/webchat-consumer-role.v1.json \
  --recipe /stage/webchat-consumers.json --check
```

```bash
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/webchat_consumers.py --role /opt/release/deploy/authorization/webchat-consumer-role.v1.json \
  --recipe /stage/webchat-consumers.json --apply --bootstrap-dsn-file /run/bootstrap-dsn --signing-key-file /stage/management/signing.key.hex --signing-key-id $KEY_ID
```

- `webchat_consumers.py` 没有单独的 dry-run 模式。`--check` 只校验 role 和配方；`--compile-facts`（把 `--check` 换成它）会在本地打印将要写入的授权事实，不连数据库，可以用来核对 customer-5 恰好是那 5 个资源。
- 预期 `configured: true`。`granted` 等于配方中没有 `revoke` 的项数，`revoked` 等于写了 `revoke: true` 的项数。配方里已撤销的 customer-1/2/3（以及按 run-erase-synthetic-consumer“存量顾客”撤销的 customer-4、refusal-1/2）要保持 `revoke: true`，不要删掉这些项：删掉不会恢复它们的授权，但以后核对时就看不出它们是撤销过的。

## 5. 业务组装：Consumer 与 owner 登记（APP_HOST，service 路径）

按组装单 §6.1–§6.3，只对 customer-5 执行一次。`CONSUMER_PRINCIPAL_ID` 填 `<PRINCIPAL_ID>`，期末用第 1 步的值。

- 6.1 `business_setup` 以 `business_owner` 身份创建 Consumer，并登记 ConsumerOwnership（owner 为 customer-5 的 principal），输出 `consumer_id`、`control_id`。这就是 service 路径的 Consumer 创建和 owner 登记，不需要另外调用别的工具。
- 核对（组装单 §6）：这个 consumer 有 1 个 Consumer、2 个 EffectControl、2 个 ServiceOffering、2 个 ConsumerServiceOffering、1 个 ConsumerOwnership。

## 6. 角色组装与 relay 登记（APP_HOST）

按组装单 §7（`role_assembly`）和 §8（`relay_enrollment`）各执行一次，期末与第 5 步相同。relay-run-profile 是部署级的（组装单 §9），不需要改。

核对 relay 登记，最新一次应为 active，并且带 role、link、step：

```bash
ro "select status, role_id is not null, link_id is not null, step_id is not null from control.nexloop_relay_enrollments where tenant_id='$T' and consumer_id='<consumer_id>' order by enrollment_revision desc limit 1"
```

`status` 列名以 stage 上 `\d control.nexloop_relay_enrollments` 为准。

## 7. 开一个会话并标记 persona（负责人在终端执行）

1. **开会话**：§2.2a 脚本的 `--apply open`。customer-5 登录后开一个会话，不发消息。`synthetic_customer.py` 的预检要求账号已有会话，会话的 consumer_id 必须等于配置里的 consumer_id。
2. **标记**：调度员由 `deploy/examples/consumer-persona.example.json` 填出 `persona-customer-5.json`，填 `consumer_id` 和 `principal_id`（与配方一致）。负责人以 approver3 身份先执行 `--apply mark-dry`，再执行 `--apply mark`（即 `erase_synthetic_consumer.py mark`，只接受配方中 persona 为 `simulated_agent` 且未撤销的 principal）。预期 `marked`，revision 1。

## 8. 只读核对（DATA_HOST）

三条都要满足，才能发言：

```bash
ro "select count(*) filter (where jsonb_array_length(coalesce(f.payload->'grants','[]'::jsonb))>0) n_grants, count(*) n_facts from authz.nexloop_authority_facts f where f.tenant_id='$T' and f.fact_kind='grants' and f.entity_key[1]='<PRINCIPAL_ID>'"
```

预期 `n_grants` 5，`n_facts` 5；`nexloop.conversation.read:1` 必须在其中（`entity_key[2]` 可以加进 select 逐行看）。

```bash
ro "select count(*) from control.nexloop_consumer_erasures where tenant_id='$T' and consumer_id='<consumer_id>'"
```

预期 0。

```bash
ro "select persona, revision from control.nexloop_consumer_personas where tenant_id='$T' and world='real' and consumer_id='<consumer_id>'"
```

预期 `simulated_agent`，revision 1。

再用第 4 个顾客的候选查询（`docs/design/synthetic-model-fault.md`“第 4 个测试顾客”）跑一遍：customer-5 的 consumer_id 应出现在结果里。

## 9. 发言前的预检配置

在 `synthetic_customer.py` 的私有配置（`nexloop-synthetic-customer/1`）里加两处：

- `accounts`：`"customer-5": "customer-5"`（alias → 用户名）；
- `persona_check.consumers`：`"customer-5": "<consumer_id>"`。

口令按 S6-stage-acceptance 的方式经 stdin 一行一个传入（`customer-5<TAB><口令>`，口令取自钥匙串 `nexloop/stage/customer-5`）。预检通过时，customer-5 的 persona 显示为 simulated_agent，会话的 consumer 与配置一致。之后按 run-synthetic-model-fault 的 4 个顾客路径执行（`remaining` 12）。

## 10. 以后撤销或擦除 customer-5

顺序固定为 **撤销 → 擦除 → 核对**（`docs/release/run-erase-synthetic-consumer.md`；`grants_held` 和 `grants_trace` 来自迁移 0429，它在批次 144 里，部署之前预检里没有这两项，擦除前改用第 8 步的第一条 SQL 核对授权已清空）：

1. **撤销**：配方里 customer-5 那一项加 `"revoke": true`，按第 4 步重新 `--apply`；再跑第 8 步的第一条 SQL，`n_grants` 应为 0，`n_facts` 仍为 5。
2. **只停用、不擦除时**：再用 `relay_enrollment` 的 `withdraw` 撤销 relay 登记（组装单 §8），到此为止。
3. **擦除**：`erase_synthetic_consumer.py erase --dry-run`，确认 `grants_held` 为 0、`grants_trace` 为 `owner_link`；然后正式 `erase`。授权还没撤销时，工具不会发送（`grants_held_revoke_first`），服务端也会返回 409 拒绝。
4. **核对**：keeper 跑完后再执行一次 `erase --dry-run`，结果应为 `already_erased`，`grants_trace` 为 `cleared_at_erasure`。

## 限制

- 账号不能删除，配额只增不减（组装单 §1）；已有配额不能提高，只能按 0a 新增应用 id。第 3 步执行前，务必先核对清单。
- 本单没有在 stage 上实际执行过，命令都取自组装单和 §2.2a 已经执行过的版本。第 6 步 `relay_enrollments` 的列名，要在 stage 上 `\d` 确认。
