# 执行单：NX-042 校准工具链在 stage 上用合成数据走一遍（不是真实校准）

负责人 2026-10-28 决定拆分 NX-042：先用 stage 的合成数据把校准流程走一遍，NX-042 据此判 done；真实数据的 D9 校准（至少 28 天真实数据）拆成上线后的新任务（本单末尾 §6 是拆分建议）。

> **证据口径（每一条证据都要带上）**：合成数据，只验证工具链，不代表真实校准。本单不产生任何校准结论事件，孪生的校准状态保持不变（`uncalibrated`），NX-043 不能因此选用任何孪生。

- **执行者**：调度员（第 1、2、4 步，只读）；合成 owner approver3（第 3 步，工作台账号）。**判定者**：L6。证据里两者分开写；负责人不需要操作。
- **按 main 17aa74c1 核对过**：
  - 校准 Action `nexloop.consumer_twin.calibrate:1`（operation `calibrate_consumer_twin`），owner 角色、owner-only 集合（`workbench-roles.v1.json`），不在审批策略（`approval-policies.v1.json` version 4）内，approver3 可以直接执行；
  - 命令行 `scripts/deploy/twin_calibrate.py`（`--config`、`--dry-run`、`--origin`、`--ca-file`、`--username-file`、`--password-stdin`），配置示例 `deploy/examples/twin-calibrate.example.json`；
  - 评估函数 `runtime.nexloop_twin_calibration_evaluate(definition jsonb, windows jsonb)`（0282，immutable，纯函数，不读表、不写表）；聚合读取 `runtime.nexloop_twin_calibration_aggregates(tenant, since, until, exclude_before)`（0281/0282，stable）；
  - stage 上的合成孪生 `at046-synthetic-stage` v1，定义来自 `packages/contracts/examples/consumer-twin.valid.json`：行为 `reply_within_24h`、`refuse_contact`、`payment_intent`。
  - main 前进后，执行前重新核对这几项。

## 0. 为什么不能“用合成顾客跑出一份校准报告”

调度单要求用 stage 合成顾客（customer-1..4、perf/refusal 样本、`at046-synthetic-stage` 孪生）跑 calibration report。按 main 上的实现，这样做**得不到 passed/failed 报告**，原因都是有意的设计（T5 实现说明，D3/D4/D9）：

| # | 设计 | 对合成数据的影响 |
|---|---|---|
| L1 | 聚合只读 real 世界、real 人设的消费者，每组至少 30 人；非 real 人设计入 `excluded.non_real_persona` | 合成顾客要么被排除，要么不满 30 人被抑制。没有一个单元，结论最多是 `insufficient_sample` |
| L2 | 窗口必须在版本发布之后开始、并且已经结束（28/56/84 天） | `at046-synthetic-stage` v1 在 release 33（2026-10-22 前后）发布，最早的窗口要到 2026-11-19 才结束；现在提交会被拒（`window_open`） |
| L3 | 用过的窗口不能再用（`window_used`），报告冻结、不能删除 | 一旦用合成期写出报告，这段窗口就永久用掉了 |
| L4 | 含没有真实聚合数据的行为（`payment_intent`）的孪生判 `not_calibratable`（D4） | `at046-synthetic-stage` 本身就永远是 `not_calibratable` |

因此本单把“校准流程走一遍”拆成能在 stage 上**不写任何校准数据**完成的四段，合起来覆盖工具链的每一环：

| 段 | 覆盖的环节 | 写入 |
|---|---|---|
| 1 | 孪生状态读取 | 无 |
| 2 | 聚合读取：在 stage 的真实库上读一个窗口，确认合成顾客被排除或抑制（L1） | 无（只读事务） |
| 3 | 命令行 → 工作台登录 → 受治理 Action → 服务端独立性检查（预期被拒，不写报告） | 只有 Action 的拒绝记录，没有报告、没有事件 |
| 4 | 评估：stage 上部署的评估函数，输入 stage 孪生的定义和合成的窗口聚合，四种结论各跑一次 | 无（只读事务，函数是纯函数） |

## 1. 前提（不满足就不开始）

| # | 前提 | 怎么核对 |
|---|---|---|
| P1 | stage 的迁移包含 0281、0282 | `ro "select count(*) from control.schema_migrations where version in ('0281','0282')"`（`ro` 见 run-nx057 §0.3）为 2 |
| P2 | `at046-synthetic-stage` v1 在库里 | 第 1 步第一条查询有一行 |
| P3 | approver3 是 active owner | run-nx057 §0.5 的 approver3 状态查询，预期 `active|active|owner` |
| P4 | 管理机有 `$RELEASE_CHECKOUT`（stage 当前部署的 main 提交）的检出，`python3` ≥ 3.12 | 同 run-nx059 P5 |

## 2. 执行

### 第 1 步：孪生状态（调度员，只读）

```
ro "select twin_id, version, published_at, definition_digest from control.nexloop_consumer_twins where tenant_id='$TENANT_ID' and twin_id='at046-synthetic-stage'"
ro "select runtime.nexloop_consumer_twin_status('$TENANT_ID','at046-synthetic-stage',1), runtime.nexloop_consumer_twin_calibration('$TENANT_ID','at046-synthetic-stage',1)"
ro "select count(*) from control.nexloop_twin_calibration_reports where tenant_id='$TENANT_ID'"
ro "select count(*) from control.nexloop_consumer_twin_calibrations where tenant_id='$TENANT_ID'"
```

- **预期**：一行 v1；状态 `published` 或 `retired`（AT-046 收尾会作废它）；校准状态 `uncalibrated`；报告数和校准事件数都是 0。
- 记下 `published_at`、报告数和事件数，第 5 步要再查一次比对。

### 第 2 步：聚合读取（调度员，只读）

读最近 28 天（合成顾客的活动都在这段时间里）：

```
ro "select runtime.nexloop_twin_calibration_aggregates('$TENANT_ID', date_trunc('day',now())-interval '28 days', date_trunc('day',now()), null)"
```

- **预期**：`groups` 为 `[]`，`suppressed` 为 `["all","daypart"]`；`excluded.non_real_persona` 为合成顾客中这段时间被联系过的人数（≥ 0）。
- 如果 `groups` 不为空：说明有 ≥ 30 个 real 人设的消费者被联系过。stage 上不应出现，停下报告，不要继续第 3 步。
- 输出只有计数，不含消费者标识。

### 第 3 步：受治理 Action，预期被拒（approver3，工作台）

配置 `local-only/nx042/twin-calibrate.json`（0600），窗口从孪生发布当天（UTC 零点）开始：

```
{"schema": "nexloop-twin-calibrate/1", "twin_id": "at046-synthetic-stage", "version": 1,
 "windows": [{"split_at": "<第 1 步 published_at 的 UTC 日期>", "span_days": 28}]}
```

```
python3 "$RELEASE_CHECKOUT/scripts/deploy/twin_calibrate.py" --config local-only/nx042/twin-calibrate.json --dry-run
security find-generic-password -s nexloop/stage/approver3 -w | python3 "$RELEASE_CHECKOUT/scripts/deploy/twin_calibrate.py" \
  --config local-only/nx042/twin-calibrate.json --origin https://<APP_HOST_NAME>:<PORT> [--ca-file <CA>] \
  --username-file <approver3 用户名文件，0600> --password-stdin
```

- **dry-run 预期**：退出码 0，显示请求体（twin_id、version、windows）和 Idempotency-Key，不读口令、不发送。
- **正式执行预期**：退出码 1，被拒，原因是下面之一（都在写报告之前）：
  - `window_before_publication`：零点早于发布时刻；
  - `not_published`：孪生已被 AT-046 收尾作废；
  - `window_open`：窗口还没结束。
- **停止条件**：执行前确认今天早于 `split_at + 28 天`，并且 `split_at` 是发布当天。如果算出的窗口已经结束、又在发布之后，**不要执行**：服务端会写出一份冻结报告并永久用掉这段窗口（L3）。
- 输出不含用户名、口令、cookie。

### 第 4 步：评估函数，四种结论（调度员，只读）

输入：stage 孪生的定义（第 1 步那一行），以及合成的两窗口聚合：每窗口 4 个时段，每段 30 人，回复 14 人、拒绝 1 人。“有偏差”的版本在第 2 窗口改三处：18–24 时段回复 5 人，12–18 和 18–24 时段拒绝 0 人。

```
FIX="(select jsonb_agg(jsonb_build_object('window',win,'aggregates',jsonb_build_object('groups',
 (select jsonb_agg(jsonb_build_object('group',g,'consumers',30,
   'replied_24h',case when MISS and win=2 and g='daypart_utc_18_24' then 5 else 14 end,
   'refused',case when MISS and win=2 and g in ('daypart_utc_12_18','daypart_utc_18_24') then 0 else 1 end))
  from unnest(array['daypart_utc_00_06','daypart_utc_06_12','daypart_utc_12_18','daypart_utc_18_24']) g))))
 from generate_series(1,NWIN) win)"
DEF="(select definition from control.nexloop_consumer_twins where tenant_id='$TENANT_ID' and twin_id='at046-synthetic-stage' and version=1)"
ev() { ro "select x->>'conclusion', x->>'cell_count', x->>'covered_cells', x->>'coverage', x->>'reason'
           from runtime.nexloop_twin_calibration_evaluate($1, $(printf '%s' "$FIX" | sed "s/MISS/$2/g; s/NWIN/$3/")) x"; }
ev "$DEF #- '{parameters,behaviors,payment_intent}'" true  2    # A
ev "$DEF #- '{parameters,behaviors,payment_intent}'" false 2    # B
ev "$DEF #- '{parameters,behaviors,payment_intent}'" true  1    # C
ev "$DEF"                                             true  2    # D
```

- `#- '{parameters,behaviors,payment_intent}'` 只改函数的输入，不改库里的孪生。
- **预期**（已在本地一次性集群上用同一定义和同一段 SQL 跑过）：

| # | 输入 | 结论 | 单元 / 覆盖 | 说明 |
|---|---|---|---|---|
| A | 两窗口，有偏差，去掉 `payment_intent` | `passed` | 16 / 13，0.8125 | 落在 0.70–0.90 内 |
| B | 两窗口，全部覆盖 | `failed` | 16 / 16，1.0000 | `intervals too wide (over-coverage)` |
| C | 一个窗口 | `insufficient_sample` | 8 / 8 | 不足 16 个单元 |
| D | stage 孪生原定义 | `not_calibratable` | 16 / 13 | `behaviours without real aggregate data stay uncalibrated: payment_intent` |

- 这四个结论**都不写库**：评估函数是纯函数，`ro` 在只读事务里执行，最后回滚。

### 第 5 步：确认什么也没写（调度员，只读）

重跑第 1 步的四条查询：校准状态仍为 `uncalibrated`，报告数和事件数与第 1 步相同（都是 0）。

## 3. 通过条件（NX-042 工具链验收）

1. 第 1 步：孪生在库里，校准状态 `uncalibrated`；
2. 第 2 步：合成顾客没有进入任何单元（`groups` 为空，被排除或被抑制）；
3. 第 3 步：dry-run 退出码 0；正式执行被服务端以独立性检查之一拒绝（`window_before_publication`、`not_published` 或 `window_open`），没有写报告；
4. 第 4 步：A–D 四个结论与上表完全一致；
5. 第 5 步：报告数、事件数没有变化，校准状态仍为 `uncalibrated`。

五条都满足，判定者记“工具链验证通过（合成数据，不代表真实校准）”，NX-042 可以判 done。

## 4. 失败处理

| 现象 | 处理 |
|---|---|
| 第 2 步 `groups` 不为空 | 停下报告：stage 上出现了 ≥ 30 个 real 人设的被联系者，需要先弄清来源 |
| 第 3 步返回 200、写出了报告 | 不能撤回（报告冻结）。停下，记录报告引用和结论，报给 L6；这段窗口已经用掉。由于孪生含 `payment_intent`，结论只会是 `not_calibratable`，不写事件，校准状态不变 |
| 第 3 步登录失败 | 先核对 P3；不要换负责人账号重试，报给调度员 |
| 第 4 步结论与上表不符 | stage 上部署的评估函数与 main 不同，或输入写错。对照 0282 的函数体和本单的 SQL，报给 L6，不要改函数 |

## 5. 证据（记入 NX-042，不含口令、用户名、DSN）

- 每条证据开头写：**合成数据，只验证工具链，不代表真实校准**。
- 执行者（调度员；第 3 步为 approver3）和判定者（L6）分开写。
- 第 1、5 步的查询结果；第 2 步的 `groups`、`suppressed`、`excluded`；第 3 步 dry-run 的 Idempotency-Key 和正式执行的拒绝原因；第 4 步 A–D 的结论行。

## 6. 拆分建议（由 L4 落 planning）

- **新任务标题**：消费者孪生真实数据校准（D9）
- **依赖**：正式上线（真实派发打开、有真实 Consumer），并且上线后积累至少 28 天真实数据。按 T5，一个版本要有两个都在发布之后开始、并且已经结束的 28 天窗口才能下结论，实际至少约 56 天。
- **前置工作**：
  - 发布一个用于校准的孪生版本：只含有真实聚合数据的两种行为（`reply_within_24h`、`refuse_contact`）。`payment_intent` 要等 T4 有了它的真实聚合后再加入；
  - 发布日期要早于第一个校准窗口的起点。
- **验收**：
  1. 用 `twin_calibrate.py` 对该版本跑一次校准，两个 28 天窗口，至少 16 个单元，每组至少 30 个 real 人设的 Consumer；
  2. 冻结报告，结论为 `passed` 或 `failed`，校准事件与报告一致；
  3. 结论为 `passed` 时，NX-043 能选用该版本；为 `failed` 时，报告写明原因，并按 K=3 的规则处理；
  4. 执行者与判定者分开记录，证据写明使用的是真实数据。
- **NX-042 的证据**链接到本单的执行记录，注明“真实校准见新任务”。
