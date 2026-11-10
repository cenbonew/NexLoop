# customer-5 回复控制加预算（ADR-045 演练恢复前、R76 第 7 步前）

起因：customer-5 的回复控制 EffectControl `d841e1f6…` 已用完（ledger `reserved_units = budget_units`），之后的回复在预留时被拒（`effect budget unavailable`，42501）。ADR-045 演练 r1/r2/r3 和 R76 第 7 步的验证消息都走 customer-5（Consumer v1），所以预算要在 R76 第 7 步之前加好。

## 1. 结论（按 main 66139a41 核实）

- **工作台 `Budget.set` 不适用**：它改的是租户级预算（`control.nexloop_budget_limits`，model、evolution、incentive），与某个 Consumer 的 EffectControl 账本无关。
- **受治理的路径只有一条：先 `EffectControl.edit:1`，再 `nexloop.effect.configure_control:1`。**
  1. `EffectControl.edit:1`（`system_catalog.py`，OBJECT_EDIT）：调用方是控制对象的 owner，即 `business_owner`。把对象属性 `budget_units` 改大，对象 revision 加 1。
  2. `nexloop.effect.configure_control:1`（0446 是最新定义）：调用方是 `business_owner`，同时出示 `effect_executor` 的令牌。账本执行 `on conflict(control_id) do update`：`budget_units`、`control_revision`、`valid_until` 改成对象的当前值，**`reserved_units` 保持不变**。剩余额度为新的 `budget_units` 减去已用的 `reserved_units`。`budget_units` 是累计上限，不是“再加多少”。
- **两步必须连着做**：
  - bind（0446 第 206–207 行）要求账本的 `control_revision` 等于对象当前的 revision。只做完第 1 步时，customer-5 的每条消息都会在 bind 时被拒，比预算用完更糟。
  - relay 的配方由 0141 按对象当前 revision 构建，所以不需要重新登记 relay。
- **命令**：`python -m nexloop_eios.effect_control_budget`（本分支新增，迁移 `effect_control_owner_read`，临时号 0448）。
  - 一份配方、一条命令。
  - 按顺序做三件事：owner 只读核对 → editor 执行 `EffectControl.edit:1` → owner 执行 `configure_control:1`。
  - 最后读回并核对对象与账本是否一致。
  - `--dry-run` 只读，打印计划。
- **三份凭据，都从文件读取**，不放进 argv，也不打印：
  - **owner**：`business_owner`，只做只读核对和 configure。必须是当初 configure 这个控制时用的同一个令牌。原因是 0446 的账本 upsert 要求 `owner_digest` 和 `executor_digest` 与已存的值相同；owner 或 executor 换过令牌，这个控制就永远不能再 configure。第 2 节 R5 用来核对这一点。
  - **editor**：持有 `EffectControl.edit:1` 的服务主体，并且对这个对象、对 `budget_units`（配方改 `valid_until` 时还要加上它）有 EDIT 权限。
    - 不要把这些授权加给 `business_owner`：会改变它的 requested_scopes，stage 上 register 会报 `credential_rotation_required`，轮换之后就触发上面那条 owner 令牌限制。
  - **executor**：`effect_executor`，与当初 configure 时是同一个令牌。
- **stage 授权（负责人 2026-11-10 选 A：按对象授权）**：新增专用主体 `control_budget_editor`，写在 `service-grants.v1.json` v25，setup 凭据文件是 `setup/control-budget-editor-credential`（`stage-principals.v1.json` v11）。
  - 授权内容：
    - `EffectControl.edit:1` execute；
    - `eios:object_type:EffectControl` read；
    - 对目标控制的对象 EDIT，以及它的 `budget_units`、`valid_until` 两个属性的 EDIT。
  - **控制 id 不进公开仓库**。公开 manifest 里只写占位符 `{control_budget_target}`。`stage_secrets.py register` 从管理树的私有文件 `management/control-budget-targets.json`（0600，内容为 `{"control_ids": ["<64 位 id>"]}`）逐个展开。没有这个文件时不授任何对象权限，输出里的 `private_object_grants` 为 0。dry-run 只打印个数，不打印 id。
  - 其他主体一律不能拿到对象或属性授权（`service_grants.OBJECT_GRANT_ROLES`，有测试锁定）。
  - 不选属性组规则的原因：EffectControl v1 没有属性组，0084 规定没有属性组的属性永远不派生，所以写了规则也派生不出任何 EDIT。
  - **不会引起轮换**：
    - `control_budget_editor` 的 requested_scopes 固定为 `action.execute`、`object_type.read`、`object.edit`、`property.edit`，与私有文件里有几个 id 无关，所以增减目标都不会要求它轮换；
    - 其他主体的 binding 逐个比对，与加新主体前完全相同；
    - `business_owner` 没有任何变化。
- **不可以做的事**：直接改 ledger 的 SQL、借用超级用户、为了预算新建一个 Consumer（会换 consumer_id，演练对照就断了）。

## 2.0 授权准备（调度员，管理树所在的机器，configurator；R76 部署之后、第 3 步之前）

1. 部署的版本要包含本分支的提交：迁移 `effect_control_owner_read`（临时号 0448，按合批后的编号）、`effect_control_budget` 模块、service-grants v25 和 stage-principals v11。
2. 写私有目标文件 `management/control-budget-targets.json`：权限 0600，内容为 `{"control_ids": ["<CTL 的完整 64 位 id>"]}`。这个文件只放在管理树里，不进仓库，不进证据。
3. `stage_secrets.py setup-credentials --out <树>`：只新增 `setup/control-budget-editor-credential`，输出里的 `added` 只有它这一个文件。
4. **（F1，必做）正式 register 之前，先对真实库跑一次 dry-run 并存档**：`stage_secrets.py register --dry-run`，用窗口模式 `--bootstrap-dsn-file`（L7 96775667 的写法，参数以那张单为准）。本步只读。预期如下，任何一项不符就停下，报调度员：
   - `principals` 比上一次存档多出且只多出 `control_budget_editor`（数目 +1）；
   - `control_budget_targets: 1`，`private_object_grants: 3`；
   - `grants` 里那 3 条对象和属性授权是占位符的形式，输出里不出现 id；
   - 轮换为 0。dry-run 本身不比对库里的凭据 binding，所以“轮换 0”分两层核对：一是本分支的测试已经锁定其他主体的 binding 不变；二是正式 register 在写入之前算出 `credential_rotation_required` 就会拒绝、什么都不写（`service_grants.apply` 的 blocking findings）。所以第 5 步只要成功执行，就说明轮换为 0。
5. `register`（去掉 `--dry-run`）。预期：
   - `credentials_created` 只有 `control_budget_editor`，`private_object_grants: 3`；
   - `grants_added` 以 dry-run 的计划为准：edit:1、类型 read，加上 3 条对象和属性授权；
   - 以 `credential_rotation_required` 拒绝，或者 `no token generated for principals: control_budget_editor`（说明第 3 步没做）时，什么都没写入。停下，报调度员。
6. 新凭据按惯例存入 Keychain（`nexloop/…`，同步 vault-index），之后照第 3 步写入 `/in/`。

## 2. 执行前只读核对（调度员在 stage 跑，`$T` 为租户）

`<CTL>` 是 `d841e1f6…` 的完整 64 位 id，`<C5>` 是 customer-5 的 consumer_id。

```sql
-- R1 账本：剩余额度 = budget_units - reserved_units（预期为 0），记下 control_revision 和 valid_until
select control_id, consumer_id, control_revision, budget_units, reserved_units, valid_until
  from control.nexloop_effect_control_ledger where tenant_id='$T' and world='real' and control_id='<CTL>';
-- R2 对象：revision 应等于 R1 的 control_revision；记下 owner_principal、executor_principal
select nexloop_revision, properties from ontology.objects
 where tenant_id='$T' and world='real' and type_name='EffectControl' and object_id='<CTL>';
-- R3 授权：owner 持有 configure；control_budget_editor 持有 edit:1，以及 <CTL> 的对象和两个属性的 EDIT。预期 5 行，每行的数都大于 0
select entity_key[1], entity_key[2], jsonb_array_length(coalesce(payload->'grants','[]'::jsonb)) from authz.nexloop_authority_facts
 where tenant_id='$T' and fact_kind='grants' and (entity_key[1], entity_key[2]) in
  (('<R2 owner_principal>','eios:action:nexloop.effect.configure_control:1'),
   ('nexloop-service-control-budget-editor-principal','eios:action:EffectControl.edit:1'),
   ('nexloop-service-control-budget-editor-principal','eios:object:EffectControl/<CTL>'),
   ('nexloop-service-control-budget-editor-principal','eios:property:EffectControl/<CTL>/budget_units'),
   ('nexloop-service-control-budget-editor-principal','eios:property:EffectControl/<CTL>/valid_until'));
-- R5 令牌没换过：账本记的 owner_digest 和 executor_digest，都应等于对应主体当前 active 凭据的 token_digest（两列都应为 t）
select l.owner_digest in (select token_digest from authz.nexloop_service_credentials c where c.tenant_id='$T' and c.status='active'
         and c.binding->>'subject_principal_id'=l.owner_principal),
       l.executor_digest in (select token_digest from authz.nexloop_service_credentials c where c.tenant_id='$T' and c.status='active'
         and c.binding->>'subject_principal_id'=l.executor_principal)
  from control.nexloop_effect_control_ledger l where l.tenant_id='$T' and l.world='real' and l.control_id='<CTL>';
-- R4 没有在途消息：customer-5 最近 10 分钟没有未结束的 relay 工作（revision 变化后，在途消息会以 binding_conflict 结束）
select status, count(*) from runtime.nexloop_work_feed
 where tenant_id='$T' and feed like 'relay%' and payload->>'consumer_id'='<C5>' and updated_at > now()-interval '10 minutes' group by 1;
```

- R4 的列名（`feed`、`payload`、`updated_at`）先在 stage 上用 `\d runtime.nexloop_work_feed` 确认。
- R3 或 R5 不满足就停下：
  - R3 缺授权，回到第 2.0 节，核对私有目标文件，然后重新 register；
  - R5 为 f，说明 owner 或 executor 的令牌已经换过，这个控制不能再 configure，报调度员。只有这种情况才考虑另建控制，需要另外设计。
- 只读的计划也可以用 CLI 的 `--dry-run` 打印（第 3 步的命令加上这个参数），输出 `phase`、`reserved_units`、`remaining` 和计划后的 `planned_remaining`。
- 如果 R1 的 `valid_until` 已经过期或快到期，第 3 步要同时把 `valid_until` 延后：同一次 edit 一起改，configure 会一起写进账本。

## 3. 执行（APP_HOST，负责人；owner、editor、executor 三份凭据）

- **建议额度**：新的 `budget_units` = R1 的 `reserved_units` + **30**。
  - 用量估计：r1/r2/r3 每轮 customer-5 最多 3 条回复，共 9 条；R76 第 7 步 3 条；合计 12 条。
  - 回落回复也会占用额度，重跑一轮再加 3 条，所以留出约 2.5 倍余量。
  - 取值范围是 1–1000000。
  - CLI 拒绝两种值：
    - 低于 `reserved_units` 的（`below_reserved`）；
    - 低于账本当前上限的（`below_current_cap`）。这样做是为了防止在读取和 configure 之间，`reserved_units` 增长后卡住账本的检查约束。
- **`valid_until`**：保持不变，除非 R1 显示 7 天内到期；需要改时，改到演练结束后 7 天以内。
- **配方** `/in/control-budget.json`（文件权限 0600，没有秘密）：

  ```json
  {"schema_version": "1.0", "control_id": "<CTL>", "from_revision": <R2 的 nexloop_revision>, "budget_units": <R1.reserved_units + 30>,
   "intent_id": "control-budget-customer5-20261110"}
  ```

  - 需要延期时，加 `"valid_until": "<UTC ISO，结尾 Z 或 +00:00>"`。
  - `from_revision` 固定写执行前的值，重跑时不要改。
  - `intent_id` 也固定，重跑用同一个。
- **凭据**：从 Keychain 用 stdin 写入一次性容器的 `/in/`（10001，0600），全部步骤结束后删除。
  - `business-owner-credential`：与 run-assembly §6 是同一份（R5 为 t）。
  - `control-budget-editor-credential`：第 2.0 节生成的 `setup/control-budget-editor-credential`。
  - `business-executor-credential`：与 run-assembly §6 是同一份。
- **命令**（`$APPTOOL`、`$BACKEND` 与 run-assembly §0 相同）：先加 `--dry-run` 看计划，确认 `phase` 为 `edit`、`planned_remaining` 约等于 30，再去掉 `--dry-run` 执行。

  ```
  docker run $APPTOOL "$BACKEND_IMAGE" -m nexloop_eios.effect_control_budget $BACKEND --owner-credential-file /in/business-owner-credential \
    --editor-credential-file /in/control-budget-editor-credential --executor-credential-file /in/business-executor-credential \
    --recipe-file /in/control-budget.json --dry-run
  ```

- **预期输出**：一行 JSON。
  - `phase` 为 `edit`，`replayed` 为 false；
  - `to_revision` = `from_revision` + 1 = `object_revision` = `ledger_revision`；
  - `budget_units` 为新值；
  - `reserved_units` 与 R1 相同；
  - `remaining` = `budget_units` − `reserved_units`。
- **重放**：同一份配方再执行一次，输出 `phase: done`、`replayed: true`，数值不变，不会再加一次。
- **失败**：stderr 只有一行 `Effect control budget unavailable: <reason>`，退出码为 1，`<reason>` 是固定代码之一，处理见第 5 节。

## 4. 执行后只读核对（调度员）

```sql
-- P1 账本：budget_units 是新值；reserved_units 与 R1 相同；control_revision = R1 + 1
select control_revision, budget_units, reserved_units, budget_units-reserved_units as remaining, valid_until
  from control.nexloop_effect_control_ledger where tenant_id='$T' and world='real' and control_id='<CTL>';
-- P2 对象和账本一致：nexloop_revision = P1.control_revision，properties->>'budget_units' = P1.budget_units
select nexloop_revision, properties->>'budget_units', properties->>'valid_until' from ontology.objects
 where tenant_id='$T' and world='real' and type_name='EffectControl' and object_id='<CTL>';
-- P3 relay 登记没变：仍为 active，control_id 仍为 <CTL>
select status, control_id from control.nexloop_relay_enrollments
 where tenant_id='$T' and consumer_id='<C5>' order by enrollment_revision desc limit 1;
```

- 冒烟：customer-5 发 1 条消息，60 秒内收到回复。
- 再读一次 P1，`reserved_units` 应该比 R1 多 1。
- 这 1 条已经算在建议额度里。

## 5. 失败时（按 `<reason>`）

| reason | 状态 | 处理 |
|---|---|---|
| `configure_unavailable` | edit 已提交，configure 没成功：对象是新 revision，账本还是旧的，customer-5 的每条回复都会在 bind 时被拒 | 重跑前先请调度员跑 R5：两列都为 t 才重跑；有 f 就停下，报调度员（换过的令牌不能再 configure，重跑也没用）。R5 通过后，立刻用**同一份配方**重跑：edit 是同一个 intent 的重放（claim 返回原结果，不会再改），configure 再执行一次，输出 `phase: configure`。重跑仍失败，先按 R5 查令牌，再报调度员。不要回退对象：回退本身也是一次 edit，会让 revision 再加 1 |
| `edit_unavailable` | 什么都没写 | editor 授权不全（R3），或者对象已经被另一个 intent 改过 |
| `below_reserved` / `below_current_cap` / `unchanged` | 什么都没写 | 改配方里的 `budget_units` |
| `revision_moved` / `ledger_behind` | 什么都没写 | 重读 R1、R2：`from_revision` 已经不是当前值，或者账本处在别的半成品状态。报调度员 |
| `control_owner_unavailable` | 什么都没写 | owner 凭据不是这个控制的 owner，或者 0448 还没部署 |
| `control_not_configured` | 什么都没写 | 这个控制从来没有 configure 过，不属于本单的情况 |
| `credential_unavailable` / `recipe_invalid` / `backend_unavailable` / `verify_failed` | 前三个什么都没写；`verify_failed` 在两步之后读回时不一致 | 报调度员，附上 P1、P2 |

## 5a. 收尾：撤销 control_budget_editor 的对象授权（必做）和凭据（由负责人决定）

1. **（F2，必做）加完预算、第 4 节核对通过后**，删除私有目标文件 `management/control-budget-targets.json`，然后 register。
   - 预期 `grants_revoked: 3`，`private_object_grants: 0`，`credentials_created: []`，不轮换。
   - 主体仍然保留 edit:1 和类型 read，但没有任何对象可改。
   - 跑下面第 4 步的第一条只读查询，确认这个主体的对象和属性授权已经空了。
2. **撤销凭据由负责人决定**，决定写进演练记录。撤销的做法：在 service-grants 下一个版本里把 `control_budget_editor` 移到 `retired_principals`，register 时会撤销它的凭据；同时在 stage-principals 里删掉对应的 setup 凭据文件。这是新的部署配置变更，另起一个提交，不在本单内做。
3. Keychain 里的这条凭据，在 vault-index 中标为作废。
4. 只读核对（调度员）：

```sql
-- 撤销对象授权后：对 <CTL> 的对象和属性授权都不在了（数为 0，或者没有行）
select entity_key[2], jsonb_array_length(coalesce(payload->'grants','[]'::jsonb)) from authz.nexloop_authority_facts
 where tenant_id='$T' and fact_kind='grants' and entity_key[1]='nexloop-service-control-budget-editor-principal' and entity_key[2] like 'eios:%:EffectControl/%';
-- 撤销凭据后（第 2 步做过时）：这个主体没有 active 凭据
select status, count(*) from authz.nexloop_service_credentials
 where tenant_id='$T' and binding->>'subject_principal_id'='nexloop-service-control-budget-editor-principal' group by 1;
```

## 6. 实现与测试（本分支）

- `packages/eios-core/src/nexloop_eios/effect_control_budget.py`：CLI。模块文档写明了顺序、半成品的补完方式和三份凭据的约束。
- 迁移 `0448_effect_control_owner_read.sql`（临时号）：`control.nexloop_effect_control_owner_read(digest, world, control_id)`。
  - 只有该控制的 owner（服务凭据，不带 Run 上下文）能读。
  - 返回对象的 revision、预算、到期时间，以及账本的 revision、`budget_units`、`reserved_units`、到期时间。
  - 其他情况一律拒绝，错误为 42501 `control owner unavailable`。
- 测试 `tests/test_effect_control_budget_pg.py`，在本地 PG 上：
  - 用完之后加额度，下一条回复成功，`reserved_units` 不变；
  - 同一份配方重放，不会再加；
  - 低于已用额度被拒，什么都没写；
  - 没有 edit 授权被拒；
  - 非 owner 凭据被拒；
  - configure 失败后，bind 在两个 revision 上都被拒，同一份配方重跑补完，之后回复成功；
  - 换一个 intent 不算重放；
  - `--dry-run` 不写；
  - CLI 输出一行，不含任何令牌；
  - `plan()` 覆盖全部拒绝原因和三个阶段。

### 6.1 本地证据（2026-11-10T07:55:16Z，`date -u`；只在本地跑，不是 stage 的证据）

- `scripts/check_definer_search_path.py`（`LC_ALL=C.UTF-8`）：1513 个例程都固定了 search_path，其中 SECURITY DEFINER 1205 个，0 处问题。
- `pytest -n 2 tests/test_bootstrap.py tests/test_published_migrations.py tests/test_function_copy_guard.py`：114 passed。这几项覆盖 bootstrap 到 catalog 尾部、已发布迁移的校验和、函数复制守卫。
- 测试没有写死迁移号：`git grep 0448 -- tests` 没有结果；新迁移只出现在 catalog 和 `versions.lock`，合批时由调度员重编。
- `tests/test_effect_control_budget_pg.py`：18 passed，此外还跑过一组守卫测试，198 passed（ae970304 的报告里列出了文件清单）。
- service-grants v25（E2）：`tests/test_control_budget_editor_grants.py` 锁定以下几点：
  - 公开 manifest 里没有控制 id；
  - 其他主体拿不到对象或属性授权，editor 也只能改 EffectControl 的这两个属性，操作只能是 edit；
  - 未解析的占位符不会被编译；
  - 增减目标不改变 editor 的 binding，其他主体的 binding 不变。
- `tests/test_stage_register_pg.py`：register 之后 editor 只对私有目标有 EDIT；dry-run 不打印 id；清空目标后再 register，撤销 3 条授权、不轮换；计数从 26 和 35 改为 27 和 36。

