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
- **缺口：没有现成的命令。** 66139a41 上能用的入口只有 Python 后端 API：`Backend` 服务会话的 `edit_object(action_name='EffectControl.edit', …)` 和 `configure_effect_control(control_id, control_revision, executor_token)`。
  - `business_setup`、`enterprise_setup` 只在新建时调用 configure，不能改已有控制对象。
  - 工作台前端也没有 EffectControl 页面。
  - 负责人的操作只用“配置文件 + 一条命令”，不手敲 Python 片段。**需要先补一个小 CLI，见第 6 节；补好之前本单第 3 步不能执行。**
- **不可以做的事**：直接改 ledger 的 SQL、借用超级用户、为了预算新建一个 Consumer（会换 consumer_id，演练对照就断了）。

## 2. 执行前只读核对（调度员在 stage 跑，`$T` 为租户）

`<CTL>` 是 `d841e1f6…` 的完整 64 位 id，`<C5>` 是 customer-5 的 consumer_id。

```sql
-- R1 账本：剩余额度 = budget_units - reserved_units（预期为 0），记下 control_revision 和 valid_until
select control_id, consumer_id, control_revision, budget_units, reserved_units, valid_until
  from control.nexloop_effect_control_ledger where tenant_id='$T' and world='real' and control_id='<CTL>';
-- R2 对象：revision 应等于 R1 的 control_revision；记下 owner_principal、executor_principal
select nexloop_revision, properties from ontology.objects
 where tenant_id='$T' and world='real' and type_name='EffectControl' and object_id='<CTL>';
-- R3 授权：business_owner（R2 的 owner_principal）同时持有 edit 和 configure；缺任何一条都停下，报调度员（补授权要走 register 和负责人确认）
select entity_key[2], jsonb_array_length(coalesce(payload->'grants','[]'::jsonb)) from authz.nexloop_authority_facts
 where tenant_id='$T' and fact_kind='grants' and entity_key[1]='<R2 owner_principal>'
   and entity_key[2] in ('eios:action:EffectControl.edit:1','eios:action:nexloop.effect.configure_control:1');
-- R4 没有在途消息：customer-5 最近 10 分钟没有未结束的 relay 工作（revision 变化后，在途消息会以 binding_conflict 结束）
select status, count(*) from runtime.nexloop_work_feed
 where tenant_id='$T' and feed like 'relay%' and payload->>'consumer_id'='<C5>' and updated_at > now()-interval '10 minutes' group by 1;
```

- R4 的列名（`feed`、`payload`、`updated_at`）先在 stage 上用 `\d runtime.nexloop_work_feed` 确认。
- 如果 R1 的 `valid_until` 已经过期或快到期，第 3 步要同时把 `valid_until` 延后：同一次 edit 一起改，configure 会一起写进账本。

## 3. 执行（APP_HOST，负责人，`business_owner` 和 `effect_executor` 的凭据）

- **建议额度**：新的 `budget_units` = R1 的 `reserved_units` + **30**。
  - 用量估计：r1/r2/r3 每轮 customer-5 最多 3 条回复，共 9 条；R76 第 7 步 3 条；合计 12 条。
  - 回落回复也会占用额度，重跑一轮再加 3 条，所以留出约 2.5 倍余量。
  - 上限是 1–1000000（`business_setup` 配方的取值范围）。
- **`valid_until`**：保持不变，除非 R1 显示 7 天内到期；需要改时，改到演练结束后 7 天以内。
- **凭据**：与 opday2 单 §0.3 相同的两个文件，`business-owner-credential` 和 `business-executor-credential`。
  - 从 Keychain 用 stdin 写入一次性容器的 `/in/`，不放在命令行，也不写进 heredoc。
  - 用完删除。
- **命令**：第 6 节的 CLI 补好后，命令填在这里。CLI 一次完成 edit 和 configure，并输出：
  - `control_id`；
  - `from_revision` / `to_revision`；
  - `budget_units`；
  - `reserved_units`；
  - `remaining`。
  - 同一个 `--intent-id` 重复执行是重放，不会再加一次。

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

## 5. 失败时

- **edit 成功、configure 失败**：账本 revision 落后于对象，customer-5 的回复会在 bind 时被拒。
  - 用同一个 intent 重跑 CLI：edit 是重放，configure 会再试一次。
  - 重跑仍失败，就停下，把 CLI 的错误码报给调度员。
  - 不要回退对象：回退本身也是一次 edit，会让 revision 再加 1。
- **R3 缺授权**：停下。补授权要走 `stage_secrets.py register`，并由负责人确认，不在本单处理。

## 6. 前置：补 CLI（未做，等派工）

建议新增 `nexloop_eios.effect_control_budget`，形式与 `business_setup` 一致：

- 参数：`--owner-credential-file`、`--executor-credential-file`、`--recipe-file`。
- 配方字段：`{control_id, budget_units, valid_until?, intent_id}`。
- 执行流程：
  1. 用 owner 会话读取当前对象；
  2. 新值必须大于等于账本的 `reserved_units`，否则拒绝；
  3. 调 `edit_object('EffectControl.edit', 1, …, expected_revision=当前 revision)`；
  4. 调 `configure_effect_control(control_id, 新 revision, executor_token)`；
  5. 读回对象，与账本核对。
- 测试：在 PG 上覆盖四种情况：
  - 预算用完之后加额度，下一次预留成功；
  - 只 edit、未 configure 时 bind 被拒；
  - 同一个 intent 重放；
  - 新值小于已用额度时被拒。
