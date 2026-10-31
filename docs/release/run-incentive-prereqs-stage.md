# B 档 #16 前置：激励派发开关、测试激励目录、incentive 预算（stage，受治理操作单草稿）

- **状态**：草稿，待负责人确认后执行。本单不改代码。
- **起因**：B 档运行单 #16“激励发放一次 + 联系限制拦截一次”被三项前置挡住（调度员只读核对，2026-10-31 21:4x）：
  - P5：`control.nexloop_real_dispatch_switches` 没有 `eios:action:nexloop.incentive.issue:1` 这一行；
  - P6：`ontology.objects` 里没有 `IncentiveOffering` / `ConsumerIncentiveOffering`；
  - P7：`control.nexloop_budget_limits` 没有 `budget_kind='incentive'` 的行。
- **角色**：
  - 执行者 approver3（owner，发起和执行）；需要审批的步骤由 approver2（owner）批准；
  - 判定者 L6；
  - 负责人账号全程不操作。
- **凭据**：口令只走钥匙串，经管道或 0600 临时文件传入，由 trap 删除；不进参数、文件名、输出或证据。
- **只读 SQL** 在 DATA_HOST 的只读窗口执行，下文写作 `ro "<SQL>"`，做法同 run-nx056 §6。
- **结论先行**（每步的依据见各节）：

| 项 | 能否按受治理路径做 | 是否需要审批 | 结论 |
|---|---|---|---|
| 1 开关 | 能：`set_real_dispatch`（`nexloop.real_dispatch.set:1`，owner） | **打开需要**（审批策略 v1：`enabled=true` 时须另一位 owner 批准；关闭不需要） | 先做 §1.0 核对，很可能**根本不需要这一行**（见 §1.0） |
| 2 目录对象 | **不能**：stage 上没有发布 `IncentiveOffering.create` / `ConsumerIncentiveOffering.create` 这两个 Action，仓库里也没有对应的 setup 工具 | — | **阻塞**，需要先补代码和 Action（§2.2），本单不绕过 |
| 3 预算 | 能：`loosen_budget`（`nexloop.budget.loosen:1`），新设预算不能走 `set_budget` | **需要**（审批策略 v5，可单人模式但本单不用） | 注意单位：incentive 预算按**最小货币单位**记，20 元写 `"2000"` |

## 0. 共用准备（管理机，发布检出根目录）

`$WB` 是 stage 工作台的 https 源，只写 scheme 和主机，同 run-nx056 §8。

```bash
pw() { f=$(mktemp); chmod 600 "$f"; security find-generic-password -s "nexloop/stage/$1" -w > "$f"; echo "$f"; }
P3=$(pw approver3); P2=$(pw approver2)
trap 'rm -f "$P3" "$P2"' EXIT
EV="${TMPDIR:-/tmp}/incentive-prereqs-evidence.jsonl"
D3=(.venv/bin/python scripts/stage/approval_drill.py --base-url "$WB" --username "<approver3 用户名>" --account-label approver3 --password-file "$P3" --evidence-file "$EV")
D2=(.venv/bin/python scripts/stage/approval_drill.py --base-url "$WB" --username "<approver2 用户名>" --account-label approver2 --password-file "$P2" --evidence-file "$EV")
```

`approval_drill.py` 每步输出一行 JSON：HTTP 状态、`approval_id`、`status`/`approval`、原因码。不输出口令、用户名、Cookie 或 CSRF。

前提核对（只读，不满足就停）：

| # | 核对 | 命令 | 预期 |
|---|---|---|---|
| Q1 | 三个 Action 都已发布 | `ro "select resource_id, active from control.nexloop_action_definitions where tenant_id='<TENANT_ID>' and world='real' and resource_id in ('eios:action:nexloop.incentive.issue:1','eios:action:nexloop.real_dispatch.set:1','eios:action:nexloop.budget.loosen:1') order by 1"` | 三行，都是 `t` |
| Q2 | 审批策略里有这两条能力 | `ro "select capability, version, quorum, solo_mode from control.nexloop_approval_policies where capability in ('real_dispatch.set','goals.budget.loosen') order by 1,2"` | 每个能力至少一行 |
| Q3 | approver2 的投票资格已到 | run-nx056 Q1 的那条查询 | `stage-principal-approver2` 一行为 `t` |
| Q4 | incentive-issuer 在跑，并且有执行授权 | APP_HOST：`docker ps --format '{{.Names}}' \| grep -c incentive-issuer`；库：`ro "select count(*) from authz.nexloop_service_credentials where credential_id='nexloop-service-incentive-issuer-key' and status='active' and expires_at>now()"` | 都 ≥1。不满足时 #16 即使三项前置都齐也发不出去，先回报调度员 |

## 1. 激励的 Action 层派发开关

### 1.0 先核对这一行是否真的需要（只读）

激励的准入（0153 里 `authz.nexloop_incentive_command` 的 `admit`）只在 `control.nexloop_effect_is_external(...)` 为真时才检查真实派发开关。`action-effect-categories.v3.json` 把 `nexloop.incentive.issue:1` 声明为 `non_contact_service`、`external_effect: false`，理由是回环账本不是外部效果（D9）。如果 stage 上应用的是 v2 或 v3，这个开关**不参与**激励准入；没有这一行就等于“关”，但激励不受影响。

```bash
ro "select x.external_effect, x.manifest_version, x.definition_digest = control.nexloop_definition_digest(d.definition) as current_definition
     from control.nexloop_action_effect_externality x join control.nexloop_action_definitions d
       on d.tenant_id=x.tenant_id and d.world=x.world and d.resource_id=x.resource_id and d.active
    where x.tenant_id='<TENANT_ID>' and x.world='real' and x.resource_id='eios:action:nexloop.incentive.issue:1'"
```

`control.nexloop_effect_is_external` 对 public 撤销了执行权，只读窗口不一定能调用，所以这里直接读它依据的表：只有 `external_effect=f` 且 `current_definition=t` 时才判为非外部。没有行，或 `current_definition=f`，都按外部处理（函数的默认）。如果只读角色连这两张表也读不到，就用 bootstrap 窗口执行，同 run-nx056 §6。

- 结果为一行 `f|…|t`：开关不影响 #16。**建议跳过 §1.1**，把 #16 的 P5 改成“effect_is_external 为 f”。由负责人确认。这样也不用为一个回环效果多开一个审批。
- 其他情况（`t`、无行或 `current_definition=f`）：按 §1.1 打开。

### 1.1 打开（需要审批：approver3 发起，approver2 批准，approver3 执行）

工具用 `approval_drill.py` 的 `set_real_dispatch`。`scripts/deploy/real_dispatch_switch.py` 不处理审批，打开时会遇到 `waiting_approval`，所以不用它。

```bash
OPEN() { printf '{"scope_kind":"action_type","action_type":"eios:action:nexloop.incentive.issue:1","enabled":%s,"data_mode":null,"reason":"%s"}' "$1" "$2"; }
"${D3[@]}" --dry-run request --operation set_real_dispatch --body-json "$(OPEN true 'B 档 #16：激励回环发放一次')"
"${D3[@]}" request --operation set_real_dispatch --body-json "$(OPEN true 'B 档 #16：激励回环发放一次')"   # 记下 approval_id = A1
"${D2[@]}" decide --approval-id A1 --decision approve --reason 'B 档 #16 激励开关'
"${D3[@]}" execute --approval-id A1
```

- **预期**：
  - dry-run 只打印路径 `POST /api/v1/workbench/actions/set_real_dispatch` 和字段名，不连接；
  - 发起：HTTP 200，`status: waiting_approval`，有 `approval_id`；
  - 批准：HTTP 200，`status: approved`；
  - 执行：HTTP 200，`approval: consumed`。
- **核对**（执行前后各一次）：

  ```bash
  ro "select scope_kind, scope_ref, enabled, data_mode, revision from control.nexloop_real_dispatch_switches where tenant_id='<TENANT_ID>' and world='real' order by 1,2"
  ```

  执行后多一行 `action_type|eios:action:nexloop.incentive.issue:1|t|...`，其余四行（tenant、enterprise.deliver、outreach.send、service.request）不变。
- **被拒的含义**：
  - 409 `self_approval`：批准用错了账号；
  - 409 `cooldown`：同一请求刚撤回过；
  - 403：approver3 不是 active owner；
  - 404：Action 未发布（Q1）。
  - 以上都判 blocked，不改用负责人账号。
- **回退**：关闭不需要审批，由 approver3 直接执行，没有 `approval_id`。

  ```bash
  "${D3[@]}" request --operation set_real_dispatch --body-json "$(OPEN false 'B 档 #16 结束：关闭激励开关')"
  ```

  核对：该行 `enabled=f`，`revision` 加 1。行不会被删除，关闭后的状态就是回退的终态。

## 2. 测试用 IncentiveOffering 与两条 ConsumerIncentiveOffering 绑定

### 2.1 现状：没有受治理的创建入口（阻塞）

- `IncentiveOffering`、`ConsumerIncentiveOffering` 两个**类型**由 `system-actions.v1.json` 发布（builder `incentive_offering_schemas`），`only_edit_via_actions=true`。
- 但仓库里**没有**它们的 CREATE Action：
  - `system-catalog-names.v1.json` 和 `system-actions.v1.json` 只有 `ServiceOffering.create` / `ConsumerServiceOffering.create` 等，`git grep "IncentiveOffering.create"` 没有结果；
  - 也没有像 `catalog_setup` / `enterprise_setup` 那样的 incentive setup 工具，也没有持有创建授权的服务主体。
- 现有测试（`tests/test_incentives_pg.py` 的 `seed_offering`）用 `session_replication_role=replica` 直接插 `ontology.objects`，那只是测试夹具，**不能**在 stage 上照做：直接 SQL、超级用户，都违反安全不变量。

结论：第 2 步在当前 main（51820ab6）上没有受治理的做法。本单**不提供**绕过办法。

### 2.2 补齐所需（交负责人决定，是另一项代码任务）

参照 `ServiceOffering` 的路径（`service_offerings.governed_create_catalog` + `catalog_setup`），需要：

1. 在 `system-actions` / `system-catalog-names` 中加 `IncentiveOffering.create:1`、`ConsumerIncentiveOffering.create:1`（`ontology.object.create`，postgres，按 `request_id`/`intent_id` 幂等），并用 `stage_secrets.py publish` 发布；
2. 给 `catalog_maintainer` 加这两个 Action 的 execute 授权（新的 service-grants 版本，经 register）；
3. 一个 `incentive_setup` 工具：配方是 `request_id`、`consumer_id` 列表、`valid_until`、金额。它用 `incentives.fixed_amount_example` 产生属性，`source_principal` 取 delivery Source 的 principal（同 `governed_create_catalog`）；
4. 测试：重放返回同一对象；Source 没有 CREATE；字段严格；准入能读到绑定。

补齐之后的执行草案（字段名以实现为准）：

```json
{"schema_version":"1.0","request_id":"<新 UUID，重放用同一个>","valid_until":"2026-12-01T00:00:00Z",
 "offering":{"incentive_code":"b16-test-credit","kind":"fixed_amount","max_amount_minor":"500","per_consumer_period_limit_minor":"1000",
             "currency":"CNY","eligibility":"bound_consumer","title":"测试优惠","active":true},
 "consumers":["<s6c-1 的 consumer_id>","<s6c-2 的 consumer_id>"]}
```

- 金额都是最小单位：单次最多 5 元，每人每期最多 10 元，两人合计不超过 §3 的 20 元预算。
- **预期**：输出 `offering_id` 和两个 `binding_id`；同一配方再执行一次，返回相同的对象。
- **核对**（只读，只看类型、数量和非个人字段）：

  ```bash
  ro "select type_name, count(*) from ontology.objects where tenant_id='<TENANT_ID>' and world='real' and type_name in ('IncentiveOffering','ConsumerIncentiveOffering') group by 1 order by 1"
  ro "select object_id, properties->>'currency', properties->>'max_amount_minor', properties->>'valid_until', properties->>'active' from ontology.objects where tenant_id='<TENANT_ID>' and world='real' and type_name='IncentiveOffering'"
  ro "select properties->>'offering_id', properties->>'offering_revision', properties->>'active' from ontology.objects where tenant_id='<TENANT_ID>' and world='real' and type_name='ConsumerIncentiveOffering'"
  ```

  预期计数 1 和 2；两条绑定都指向该 offering，`offering_revision=1`，`active=true`。
- **回退**：对象只能经受治理 Action 编辑（需要 `*.edit` Action，同属第 2.2 项），把 `active` 设为 false，不删除。在 edit Action 补上之前，可以让 `valid_until`（11-01 起约一个月）自然到期作为兜底；也可以关闭 §1 的开关并保留 §3 的预算上限，这样已有对象发不出任何激励。

## 3. incentive 预算：20 元，2026-11-01 至 2026-12-01

### 3.1 走哪条路径、是否免审批

- 新设预算属于放宽，按 NX-056 预算审批（`docs/design/NX-056-budget-approval.md`，审批策略 v5）走 `loosen_budget`（`nexloop.budget.loosen:1`），**需要另一位 owner 批准**。
  - 用 `set_budget` 新设会以 409 `budget_loosen_requires_approval` 被拒。`set_budget` 只接受收紧，以及旧期间结束后的同额续期。
  - 策略允许单人模式，但 stage 有 approver2，本单按两人做。
- **单位**：
  - incentive 预算的 `limit_amount` 必须是整数（0068 的检查约束 `limit_amount=trunc(limit_amount)`）；
  - 准入时预留的是 `amount_minor`（0145：`nexloop_reserve_budget(...,i.amount_minor::text,i.currency,...)`），所以单位是最小货币单位；
  - **20 元写成 `"2000"`**。写成 `"20.00"` 也能通过约束，但只有 20 分，#16 的发放会被预算拒绝。

### 3.2 命令（approver3 发起，approver2 批准，approver3 执行）

先读当前行（P7 应为空）：

```bash
ro "select budget_kind, unit, limit_amount, period_start, period_end, revision from control.nexloop_budget_limits where tenant_id='<TENANT_ID>' and world='real' and budget_kind='incentive'"
```

为空时：

```bash
BUDGET='{"budget_kind":"incentive","unit":"CNY","limit_amount":"2000","period_start":"2026-11-01T00:00:00Z","period_end":"2026-12-01T00:00:00Z","from_unit":null,"from_limit_amount":null,"from_period_start":null,"from_period_end":null,"expected_revision":0}'
"${D3[@]}" --dry-run request --operation loosen_budget --body-json "$BUDGET"
"${D3[@]}" request --operation loosen_budget --body-json "$BUDGET"            # 记下 approval_id = A2
"${D2[@]}" decide --approval-id A2 --decision approve --reason 'B 档 #16 激励预算 20 元'
"${D3[@]}" execute --approval-id A2
```

- 已有行时：把 `from_*` 和 `expected_revision` 填成读到的值，时间写成 UTC 的 `...Z`。不符时以 409 `budget_changed` 被拒，不会建审批。
- **预期**：
  - dry-run 打印 `POST /api/v1/workbench/actions/loosen_budget` 和十个字段名；
  - 发起：HTTP 200，`waiting_approval`，此时查询仍为空；
  - 批准：HTTP 200，`approved`；
  - 执行：HTTP 200，`approval: consumed`。
- **核对**：执行后上面的查询有一行 `incentive|CNY|2000|2026-11-01 00:00:00+00|2026-12-01 00:00:00+00|1`。
- **时机**：期间从 11-01 00:00Z 开始。在这之前执行 #16 会因为“没有覆盖当前时刻的预算”被拒，所以 #16 要放在 11-01 00:00Z 之后。
- **回退**：
  - 收紧不需要审批：`"${D3[@]}" request --operation set_budget --body-json '{"budget_kind":"incentive","unit":"CNY","limit_amount":"0","period_start":"2026-11-01T00:00:00Z","period_end":"2026-12-01T00:00:00Z"}'`，HTTP 200，`limit_amount` 变为 0，`revision` 加 1。0 是否被接受以路由校验为准；不接受时改用 `"1"`（1 分，小于任何一次发放）。
  - 已发出的激励不受影响，已预留的会按 0145 正常结算。
  - 也可以等 12-01 期间自然结束，不续期。

## 4. 执行顺序与证据

1. 0（Q1–Q4）
2. §1.0；结果为 `t` 时再做 §1.1
3. §3（可以先做，只要在 #16 之前即可）
4. §2：**阻塞**，等 §2.2 的代码任务合入、部署并发布 Action 后另行执行

- 证据：`$EV`（0600，不含口令、用户名、Cookie 和原因文本以外的内容），以及每步前后的只读查询结果。执行人写 approver3（由调度员操作），批准人写 approver2，判定人写 L6，分开记录。
- #16 结束后的回退：§1.1 关闭开关；§3.2 收紧预算或让它到期；§2 的对象停用（依赖 edit Action）。
