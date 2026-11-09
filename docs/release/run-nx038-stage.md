# 运行单：NX-038 剩余项的 stage 实测（AT-023、AT-024、AT-048，S6-R1、R5、R7）

- **执行人**：调度员（管理机）。**判定人**：负责人（按 `planning/` 中 AT-023/024/048 与 NX-038 的记录）。L6 只复核本单，不判定结果。
- **依据**：`docs/implementation/S6-stage-acceptance.md`（脚本、配置、断言、执行顺序）、`docs/implementation/evolution-stage-enrollment.md`（服务主体登记）、`run-nx056-approvals-enable.md`（合成 owner approver2/approver3）。本单不复述脚本的断言，只写在 stage 上怎么跑、看什么、失败怎么办。
- **结论**：六项的入口在 main 17aa74c1 上都有，都依赖同一个前提：租户的 evolution 预算。设预算的入口原来缺失，本分支补上（§6 G1：工作台路由 `set_budget`，由 approver3 用 `approval_drill.py` 提交，即 §2.0）。本单要在含 G1 的发布部署到 stage 之后执行。**NX-056 预算审批之后**（负责人 2026-10-28）：新设预算改走 `loosen_budget`，由 approver2 批准（§2.0 已改写），所以还要求部署含迁移 `budget_loosen` 的发布、发布 `nexloop.budget.loosen` 并应用审批策略 v5。S6-R2 无定义，按笔误处理（§6 G2）。
- **入口**：

| 项 | 入口 | 能否直接在 stage 跑 |
|---|---|---|
| AT-023 候选隔离 | `scripts/acceptance/s6_stage_evolution.py at023` | 入口有；要 P3（§2.0 设定） |
| AT-024 结构权限 | `scripts/acceptance/s6_stage_schema.py at024`（owner 用 approver3 的工作台会话） | 入口有；先跑 `s6_stage_evolution.py candidate-template`（开诊断，要 P3） |
| AT-048 验证泄漏 | `s6_stage_evolution.py at048` | 入口有；要 P3 |
| S6-R1 L0 复现 | `s6_stage_evolution.py r1` | 入口有；要 P3 |
| S6-R5 writer/evaluator 分离、test 评估凭据 | `s6_stage_evolution.py r5` | 入口有；要 P3 |
| S6-R7 删除传播 | `s6_stage_evolution.py r7-prepare` → `erase_synthetic_consumer.py erase`（approver3）→ `r7-verify` | 入口有；要 P3 |

- **身份**：人工步骤只用合成 owner **approver3**（口令在钥匙串 `nexloop/stage/approver3`；用户名的取法与 run-nx056-approvals-enable.md 相同）。负责人的账号不登录、不操作。evolution 的 writer/evaluator/reader 是服务凭据，不是人。
- 本单不写正式对象：演进只写候选、评估与留出集；`--publish`（r8）和 r6 **不在本单内**（属 NX-039/040 的运行单）。

## 0. 前提（不满足就不开始）

先跑一次只读预检：`uv run --frozen python scripts/acceptance/s6_preflight.py --config <S6 配置>`。它一次报告 P1–P5 各项的 ok、missing、expired 或 no_data，无数据不算通过，全部 ok 才退出 0。下表是各项的含义和手工核对办法。

| # | 前提 | 怎么核对 |
|---|---|---|
| P1 | stage 部署的是 main 17aa74c1 或之后、含迁移 0235/0237/0241/0248/0286（AT-048 修复）的发布 | 部署单；`ro "select version from control.schema_migrations where version in ('0235','0237','0241','0248')"` 返回 4 行（`ro` 见 run-nx057 §0.3）；0286 按集成后的实际编号核对 |
| P2 | 演进服务主体已登记：writer、evaluator、reader 三个服务凭据和一张只含 test 的评估凭据；三个 LOGIN 角色有口令、pg_hba 放行 | `python3 scripts/deploy/evolution_enroll.py --config <私有 enroll 配置> --check`，退出码 0，四项自检都为 true。没有登记过时，先按 `evolution-stage-enrollment.md` 执行（调度员；bootstrap 窗口） |
| P3 | 租户有 world real、kind `evolution` 的预算上限（单位 CNY，期间覆盖本次执行） | `ro "select unit, limit_amount, period_start, period_end from control.nexloop_budget_limits where tenant_id='$TENANT_ID' and world='real' and budget_kind='evolution'"` 有一行。**没有时本单全部判 blocked**：开诊断（`authz.nexloop_evo_diagnosis_open`，0235）在没有 evolution 上限时报 `budget evolution not configured`，每项检查都从开诊断开始；开评与 L1 预留也查这个上限（0241、0248）。没有时按 §2.0 设定。**有行但期间已结束时**（例如 10 月的 `2026-10-01`–`2026-11-01`），先按 §2.0 的同额续期（`set_budget`，不需审批），被拒再走放宽（核对清单 `docs/planning-drafts/s6-prereq-audit-20261107.md` §2.1） |
| P4 | approver3 是 stage 租户 active 的 owner 成员，持有 `nexloop.budget.loosen:1`（新设预算，NX-056 预算审批）、`Budget.set:1`、`nexloop.consumer.erase_synthetic:1` 等 owner Action 的授权，且这些 Action 在租户已发布；approver2 是 active owner，并已过 24 小时投票冷却（批准 §2.0 的放宽）；stage 已应用审批策略 v5（含 `goals.budget.loosen`） | **源码判断**：`deploy/authorization/workbench-roles.v1.json` 的 owner 角色含 `eios:action:Budget.set:1`（`existing_governed` 切片），owner 成员经 `workbench_roles.compile_members` 得到全部 owner 授权；run-nx056 §1 已把 approver3 登记为 owner。所以只要 approver3 是 active owner、`Budget.set` 已发布，就有权限。**stage 只读核对**（调度员执行，`ro` 见 run-nx057 §0.3）：`ro "select entity_key[2] from authz.nexloop_authority_facts where tenant_id='$TENANT_ID' and fact_kind='grants' and entity_key[1]='stage-principal-approver3' and entity_key[2] in ('eios:action:nexloop.budget.loosen:1','eios:action:Budget.set:1','eios:action:nexloop.consumer.erase_synthetic:1')"` 应有 3 行（授权事实的键是 `[principal_id, Action]`，见 `workbench_roles.py`）；`ro "select resource_id, active from control.nexloop_action_definitions where tenant_id='$TENANT_ID' and world='real' and resource_id in ('eios:action:Budget.set:1','eios:action:nexloop.budget.loosen:1')"` 应有两行，`active` 都为 t；`ro "select max(manifest_version) from control.nexloop_approval_policy_publications"` 不小于 5。没有授权或未发布时判 blocked，按 NX-056 运行单核对成员、按 `stage_secrets publish` 发布，不改用负责人账号 |
| P5 | 合成交互数据：窗口内有 persona 为 simulated_agent/synthetic 的 Consumer 的失败 Run，带检索未命中的语义读取；至少 4 个这样的 Consumer；AT-048 的 (b) 路线要一对近重复 Run | `uv run --frozen python scripts/acceptance/s6_stage_inputs.py --config <S6 配置>`，退出码 0（`semantic` 此时 blocked 是正常的）。不够时先用 `synthetic_customer.py` 跑一段合成交互（S6-stage-acceptance §合成交互） |
| P6 | 管理机有同一发布的检出，`uv` 可用；能经 HTTPS 访问 stage 工作台；能用 reader/writer/evaluator/inspector 四个 DSN 连库（与 NX-043 stage 脚本相同的走法） | `s6_stage_evolution.py all --dry-run` 不报连接或角色错误 |

## 1. 配置（只做一次）

复制 `deploy/examples/s6-stage.example.json` 到 `local-only/` 下（0600），按 S6-stage-acceptance 的“配置”一节填写。与本单有关的几处：

- `executor`：`dispatcher`；`release`：stage 当前发布号；`results_dir`：`local-only/` 下一个 0700 目录。
- `owner.username_file`：0600 文件，内容是 approver3 的用户名（取法同 run-nx056）。**不能填负责人的用户名**。
- `inputs.evolution`：用 P5 的 `s6_stage_inputs.py` 输出填 window、parent_revision、protected_refs、erasure_consumer_id；`label` 用新的，例如 `s6-stage-<日期>-1`；`created_at` 固定为本次执行开始的时间。
- R7 的删除另需 `deploy/examples/erase-synthetic-consumer.example.json` 的私有副本：`consumers` 只填 `inputs.evolution.erasure_consumer_id` 那一个，`confirmation_ref` 用 `s6-r7-erase:<日期>-1`。

## 2. 执行

所有命令在管理机的发布检出根目录执行。`S6=local-only/s6-stage.json`。口令只走钥匙串管道，不进参数、文件名或输出。

### 2.1 计划（只读）

```bash
uv run --frozen python scripts/acceptance/s6_stage_evolution.py all --config "$S6" --dry-run
```

```bash
security find-generic-password -s nexloop/stage/approver3 -w | uv run --frozen python scripts/acceptance/s6_stage_schema.py at024 --config "$S6" --dry-run
```

预期：每项一行 `nexloop-s6-stage-result/1`，`dry_run: true`，`blocked: false`，退出码 0。任何一项 blocked，按它的 `steps` 补前提后再来。

### 2.0 设 evolution 预算（P3 不满足时；approver3 发起和执行，approver2 批准，各一次）

NX-056 预算审批（负责人 2026-10-28，`docs/design/NX-056-budget-approval.md`）：新设预算属于放宽，走 `loosen_budget`（`nexloop.budget.loosen`），由另一位 owner 批准后才写入；`set_budget` 只接受收紧和到期后的同额续期，新设会以 409 `budget_loosen_requires_approval` 被拒。所以这一步分三条命令：approver3 发起，approver2 批准，approver3 执行。

照 run-nx056 §8 的驱动形态（口令经钥匙串写进 0600 临时文件，trap 删除；`$WB` 是 stage 工作台的 https 源，只写 scheme 和主机，与 run-nx056 相同）：

```bash
P3=$(f=$(mktemp); chmod 600 "$f"; security find-generic-password -s nexloop/stage/approver3 -w > "$f"; echo "$f")
P2=$(f=$(mktemp); chmod 600 "$f"; security find-generic-password -s nexloop/stage/approver2 -w > "$f"; echo "$f"); trap 'rm -f "$P3" "$P2"' EXIT
D3=(.venv/bin/python scripts/stage/approval_drill.py --base-url "$WB" --username "<approver3 用户名>" --account-label approver3 --password-file "$P3" --evidence-file local-only/nx038-budget.jsonl)
D2=(.venv/bin/python scripts/stage/approval_drill.py --base-url "$WB" --username "<approver2 用户名>" --account-label approver2 --password-file "$P2" --evidence-file local-only/nx038-budget.jsonl)
BUDGET='{"budget_kind":"evolution","unit":"CNY","limit_amount":"50.00","period_start":"<本月 1 日 00:00:00Z>","period_end":"<下月 1 日 00:00:00Z>","from_unit":null,"from_limit_amount":null,"from_period_start":null,"from_period_end":null,"expected_revision":0}'
"${D3[@]}" --dry-run request --operation loosen_budget --body-json "$BUDGET"
"${D3[@]}" request --operation loosen_budget --body-json "$BUDGET"      # 1. 发起：记下 approval_id
"${D2[@]}" decide --approval-id <approval_id> --decision approve --reason "S6 演进预算"   # 2. 批准
"${D3[@]}" execute --approval-id <approval_id>                          # 3. 执行
```

- **预期**：
  - dry-run 打印 `POST /api/v1/workbench/actions/loosen_budget` 和十个字段名，不连接；
  - 发起：HTTP 200，`status: waiting_approval`，有 `approval_id`；P3 的查询仍为空；
  - 批准：HTTP 200，`status: approved`；
  - 执行：HTTP 200，`approval: consumed`；P3 的查询有一行（`CNY|50.00|...`）。
- `from_*` 全为 null、`expected_revision` 为 0，表示“租户还没有 evolution 预算”。**租户已有这一行时**（例如上次执行留下的、期间已过的行），先用 P3 的查询加上 `revision` 列读出当前行，把 `from_unit`、`from_limit_amount`、`from_period_start`、`from_period_end`、`expected_revision` 填成读到的值（时间写成 UTC 的 `...Z`）；不符时发起会以 409 `budget_changed` 被拒，不会建审批。
- 旧期间已经结束、只需要同额续一个月时，不用审批：`"${D3[@]}" request --operation set_budget --body-json '<同额、新期间紧接旧期间、长度相同的五个字段>'`，HTTP 200（设计稿 Q4）。旧期间结束之前续期会以 409 `budget_loosen_requires_approval` 被拒。
- 字段要求（路由与 SQL 都会查）：`budget_kind` 取 `model|incentive|evolution`；evolution 的 `unit` 是三位大写币种；`limit_amount` 是十进制文本（不收浮点数）；两端时间都是 UTC，期间覆盖本次执行。50 元按 S6-stage-acceptance 的规模（每候选上限 10、L1 预留 0.01）足够，由调度员按需要调整。
- 发起重跑：同一请求体在等待期间返回同一条审批（`replayed: true`），不会重复建审批。执行重跑：1 小时内返回 `approval_consumed`，不会再写一次。
- 401/403：approver3 不是 active owner 或没有 `nexloop.budget.loosen` 授权（P4），或 approver2 不是有资格的批准人（投票冷却未过）；404 或 `Action not published`：`nexloop.budget.loosen` 未发布；409 `approval_unavailable`：stage 还没有应用审批策略 v5；409 `self_approval`：批准用了 approver3；422：字段不合法。都判 blocked，不改用负责人账号。

### 2.2 正式执行（顺序固定，原因见 S6-stage-acceptance §执行顺序）

| 步 | 命令 | 产出 |
|---|---|---|
| 1 | `uv run --frozen python scripts/acceptance/s6_stage_evolution.py r5 --config "$S6"` | S6-R5 |
| 2 | `uv run --frozen python scripts/acceptance/s6_stage_evolution.py at048 --config "$S6"` | AT-048 |
| 3 | `uv run --frozen python scripts/acceptance/s6_stage_evolution.py at023 --config "$S6"` | AT-023；结果里有 L0 候选的 id |
| 4 | `uv run --frozen python scripts/acceptance/s6_stage_evolution.py r1 --config "$S6"` | S6-R1 |
| 5 | `uv run --frozen python scripts/acceptance/s6_stage_evolution.py candidate-template --config "$S6"` | 写 `inputs.candidate_template`（0600） |
| 6 | `security find-generic-password -s nexloop/stage/approver3 -w \| uv run --frozen python scripts/acceptance/s6_stage_schema.py at024 --config "$S6"` | AT-024（不发布任何东西；没有 `--publish`） |
| 7 | `uv run --frozen python scripts/acceptance/s6_stage_evolution.py r7-prepare --config "$S6"` | R7 状态写进 `results_dir/state` |
| 8 | `security find-generic-password -s nexloop/stage/approver3 -w \| python3 scripts/deploy/erase_synthetic_consumer.py erase --origin https://<APP_HOST_NAME>:<PORT> --ca-file <CA> --username-file <approver3 用户名文件> --password-stdin --config <删除配置> --dry-run` | 预检：persona 不是 real，erasure 状态 none |
| 9 | 同第 8 步，去掉 `--dry-run` | 删除这一个合成 Consumer（不可逆；`consumer.erase_synthetic` 不在审批策略 v1 内，不经审批） |
| 10 | `uv run --frozen python scripts/acceptance/s6_stage_evolution.py r7-verify --config "$S6"` | S6-R7 |

- 第 9 步不可逆，必须放在最后两步：删除会让这个 Consumer 的 Run 离开窗口，前面各步的诊断输入会变。
- 第 1–4 步各用自己的留出集；同一个 `label` 重跑按重放处理，结果相同。换 `label` 就要换一批新的合成数据（P5）。
- 不在本单内：`at046`、`r4`、`r3`（NX-039 的项）、`r8`、`r10`、`r6`（NX-040/039）。`all` 会跑 at046/r4/r3，所以第 2.2 节逐条执行，不用 `all`。

### 2.2b 操作日执行单（R74 之后）

负责人 2026-11-09 定的顺序：**段 A customer-5 → 段 B 4 个顾客的 ADR-045 演练 → 段 C P5 判定 → 段 D r5 → 段 E at048 → 段 F at023 → 段 G r1 → 段 H r7**。前一段没有通过，后一段不开始。AT-024（§2.2 第 5、6 步）不在这一天。

**通用约定**
- **执行者**：凡是要口令登录的步骤，都在**负责人终端**执行，口令从 Keychain 经 stdin 传入，不进参数、文件或输出。调度员只用文件凭据（bootstrap DSN 文件、签名密钥文件、S6 配置里的服务 DSN 文件），不登录任何人的账号。
- **占位符**：`<APP_ORIGIN>` 是 stage 工作台的 https 源；`<CA>` 是 CA 文件；`<RUN>` 是 `local-only/` 下当天的私有目录（0700）；`$T` 是租户 ID；`ro "…"` 是 DATA_HOST 上的只读会话（run-nx057 §0.3）；`S6=<RUN>/s6-stage.json`。
- **证据**：两个窗口分开记录：`$S6` 的 window 用于段 D、F、G、H，`$S6_AT048` 的 window 用于段 E，并注明 at048 用的是合并后 3 个单元的划分。每段写清执行者和判定者。段 B 由调度员执行、L6 判定；段 C 由 S6 工具判定、L6 复核；段 D–H 由调度员执行、负责人判定（§4）。

**互斥（任何时刻只做一件）**
- ADR-045 开关只在段 B 打开。段 C 开始前必须已经关掉，段 D–H 期间也必须一直关着。
- NX-056 真实派发窗口：整天不开。每段开始前都跑一次下面的查询，结果必须是 0 行：

  ```bash
  ro "select r.status, r.action_resource from control.nexloop_approval_requests r left join control.nexloop_approval_consumptions c using (tenant_id, world, approval_id) where r.tenant_id='$T' and r.action_resource like 'eios:action:nexloop.real_dispatch.set:%' and (r.status='waiting_approval' or (r.status='approved' and c.approval_id is null))"
  ```

- X7（NX-043 整链重跑）和任何部署（gate、promote、迁移）都不和本单同时进行。开始前向调度员的部署队列确认当天没有排定的部署；如果中途要部署，等当前段结束再做。段 H 之后才能再部署。
- 每段开始前确认 ADR-045 开关的状态：

  ```bash
  ro "select runtime.nexloop_metrics_snapshot('$T','real')->'synthetic_fault'"
  ```

  段 B 第 2 步之前、以及段 C–H 期间，`active` 都必须是 0。

**耗时与负责人在场**

| 段 | 估计耗时 | 负责人在场 |
|---|---|---|
| A customer-5 | 60–90 分钟 | 开头约 20 分钟：口令写入 Keychain、mark；结尾约 10 分钟：预检发言 |
| B ADR-045 演练 | 约 50 分钟：预热 10 + 3 轮 × 4 人（每轮间隔 ≥ 1 分钟，加上约 5 分钟的兜底回复） | 全程：开关、发言、关开关都要登录 |
| C P5 判定 | 10 分钟 | 不需要 |
| D r5 | 10 分钟 | 不需要 |
| E at048 | 近重复那一对 15 分钟 + 运行 10 分钟 | 造近重复那一对时约 15 分钟（两个顾客各发一条） |
| F at023 | 15 分钟 | 不需要 |
| G r1 | 10 分钟 | 不需要 |
| H r7 | 准备 5 分钟 + 撤销 10 分钟 + 擦除 10 分钟 + 等 keeper 一轮 + 核对 10 分钟 | 擦除和核对（approver3 登录），约 20 分钟 |

合计约 4–5 小时，负责人要在场的时间约 2 小时，主要集中在上午的 A、B。

#### 段 A customer-5

- **前提**：R74 已经部署，含 0429（`ro "select version from control.schema_migrations where version='0429'"` 返回 1 行）；ADR-045 开关是关的。
- **执行**：按 `run-add-synthetic-customer.md` 的 §0–§9 执行，不在这里重复。负责人的步骤（口令写入 Keychain、`mark`）在负责人终端执行；授权发布（§4 `webchat_consumers.py --apply`）由调度员用文件凭据执行。
- **命令**（§9 的预检发言，负责人终端）：

  ```bash
  printf 'customer-5\t%s\n' "$(security find-generic-password -s nexloop/stage/customer-5 -w)" | uv run --frozen python scripts/acceptance/synthetic_customer.py --config <RUN>/synthetic-customer.json --plan <RUN>/plan-a-customer-5.json
  ```

  `synthetic_customer.py` 只有 `--config` 和 `--plan` 两个参数。口令从 stdin 传入，每行一个，格式是 `别名<TAB>口令`。它在发任何消息之前都会先做预检。`plan-a-customer-5.json` 只放 customer-5 的一轮普通问题，按 `nexloop-synthetic-customer-plan/1` 的格式写。
- **四人口令行**（段 B 和段 E 共用；在负责人终端，不落盘）：

  ```bash
  pw() { for a in s6c-1 s6c-2 s6c-3 customer-5; do printf '%s\t%s\n' "$a" "$(security find-generic-password -s nexloop/stage/$a -w)"; done; }
  ```

  Keychain 的服务名以 `~/.nexloop/vault-index.md` 里登记的为准。
- **预期**：预检通过，customer-5 的 persona 是 `simulated_agent`，会话的 consumer 和配置一致。
- **只读核对**：run-add-synthetic-customer §8 的三条（`n_grants` 5、`n_facts` 5；擦除登记 0 条；persona 是 `simulated_agent`，revision 1）。
- **失败时**：配额已满、账号状态不是 active，或者 §8 有任何一条不满足，就停下。段 B 退回用 3 个顾客做（`remaining` 9），P5 很可能会是 blocked，照实记录。

#### 段 B 4 个顾客的 ADR-045 演练

- **前提**：段 A 通过；`run-synthetic-model-fault.md` §2 的前提都满足（两个 relay 都是 `ontology_hybrid`，没有在运行的 NX-037 run 入口，模型预算和 evolution 预算都有效）；4 个顾客是 s6c-1、s6c-2、s6c-3、customer-5。
- **执行者**：负责人终端（开关、发言、关开关都要登录）；只读核对由调度员做。
- **命令**：
  1. 预热：开关关着，4 个顾客各发一轮普通问题：`pw | uv run --frozen python scripts/acceptance/synthetic_customer.py --config <RUN>/synthetic-customer.json --plan <RUN>/plan-b-warm.json`。
  2. 打开开关（approver3，驱动方式同 §2.0）：

     ```bash
     "${D3[@]}" request --operation set_synthetic_fault --body-json '{"state":"open","consumer_ids":["<4 个 consumer_id>"],"remaining":12,"duration_seconds":3600,"reason":"NX-038/039 S6 P5（ADR-045）"}'
     ```

     `set_synthetic_fault` 是 `deploy/authorization/api-routes.v1.json` 里这个能力对应的 operation。也可以改用工作台操作。
  3. `pw | uv run --frozen python scripts/acceptance/synthetic_customer.py --config <RUN>/synthetic-customer.json --plan <RUN>/plan-b-fault.json`：4 个顾客各发 3 轮，每轮间隔至少 1 分钟（间隔写在 plan 里）。每轮都带同一个未知词，每个顾客再各加一个**只有自己用**的词。这样每个顾客的词组都不一样，演练出来的 Run 不会被当成近重复；近重复那一对放到段 E 去造。
  4. 关开关：

     ```bash
     "${D3[@]}" request --operation set_synthetic_fault --body-json '{"state":"closed","consumer_ids":[],"remaining":0,"duration_seconds":0,"reason":"演练结束"}'
     ```

- **预期**：打开时返回 200，`opened: true`，`consumers: 4`；每个 Run 以 `runtime_task_failed` / `synthetic_model_fault` 结束；关掉后快照的 `active` 是 0。
- **只读核对**：
  - 每轮跑一次 run-synthetic-model-fault §2 第 4 步的查询（failure 和检索未命中数大于等于 1）。
  - 结束时：`ro "select kind, count(*) from runtime.nexloop_synthetic_fault_events where tenant_id='$T' group by 1"`，应该是 12 条 injected，加 1 条 closed 或 exhausted。
- **失败时**：
  - Run 没有失败：先查 guard 日志里的 `synthetic fault decision failed`（run-synthetic-model-fault §2 的说明）。
  - Run 失败了但没有检索未命中：多半是冷池，记下来，不在开关打开期间补轮次。
  - 不管哪种情况，最后都要先关开关，再进入段 C。

#### 段 C 先关开关，再跑 s6_stage_inputs，最后判 P5

- **前提**：段 B 第 4 步已经执行，快照的 `active` 是 0，兜底回复都已发出。
- **执行者**：调度员（文件凭据）。
- **命令**：

  ```bash
  uv run --frozen python scripts/acceptance/s6_stage_inputs.py --config "$S6"
  ```

- **预期**：`sources.window.status` 是 `ok`，`evidence.consumers_with_miss` 大于等于 4；`erasure_consumer_id` 是 `ok`。`semantic` 此时是 blocked，属于正常（§0 P5）。
- **P5 的判定**：以这个工具的输出为准（L6 2026-11-09）。带检索未命中的失败 Run 要覆盖 4 个不同的簇单元。不够的话，**照实判 blocked，不为了凑数加轮次**。演练证据和 P5 证据可以引用同一批 Run，但要分开记录。按 0418，这些 Run 不计入 B5 的失败率，这是预期。
- **只读核对**：把输出里的 `window` 和 `erasure_consumer_id` 填进 `$S6` 的 `inputs.evolution`（§1）。**`$S6` 的 window 从这里起固定不变**（L6 C1）：`until` 要在段 E 造近重复对之前，取工具给出的 `until` 和当前时间中较早的那个；段 D、F、G、H 都用这个窗口。`erasure_consumer_id` **必须是段 B 里 4 个顾客中的一个**，不能是 customer-1..4。工具只会挑 persona 不是 real 的顾客；如果它挑中的是 customer-1..4，就从 `candidates` 里换一个段 B 的顾客。
- **失败时**：`window` 是 blocked，就把 P5 判 blocked，段 D–H 当天不做，向负责人报告 `consumers_with_miss` 的实际值。

#### 段 D r5

- **前提**：段 C 的 P5 判定通过；§0 的 P1–P4、P6 通过（先跑一次 `s6_preflight.py`）。
- **执行者**：调度员。
- **命令**：

  ```bash
  uv run --frozen python scripts/acceptance/s6_stage_evolution.py r5 --config "$S6"
  ```

- **预期与失败处理**：预期见 §3 的 S6-R5 一行；失败按 §5 F2 处理。
- **只读核对**：结果行 `passed: true`、`blocked: false`，并记下结果文件的 sha256。

#### 段 E at048（包括 (b) 路线要的近重复那一对）

- **造近重复的一对**（负责人终端，开关关着）：AT-048 的 (b) 路线需要两个**不同顾客**的 Run，它们读取的词组完全相同（`s6_stage_evolution.py` 按 `term_hashes` 的集合是否相等来判定）。做法是从段 B 的 4 个顾客里挑两个（不要挑段 C 选出的 `erasure_consumer_id`），各发**一条**措辞完全相同、只含两个固定未知词的消息，例如“请介绍霁蓝釉陶片拼贴和鎏金錾花”，两条间隔至少 1 分钟，不要加别的词。命令：`pw | uv run --frozen python scripts/acceptance/synthetic_customer.py --config <RUN>/synthetic-customer.json --plan <RUN>/plan-e-pair.json`，plan 里只写这两个顾客的各一轮；其余两个顾客的口令行没有用到，工具会忽略它们，这一点以工具实际的处理为准，不行就只给这两行。发完后，复制一份 `$S6` 作为 `$S6_AT048`（`<RUN>/s6-stage-at048.json`，0600），只改两处：window 的 `since` 不变，`until` 延到近重复对的两条 Run 都结束之后；`label` 用一个新值（例如加上 `-at048` 后缀）。**`$S6` 本身不改。**
- **近重复对选哪两个顾客**（L6 C2）：不能选 `erasure_consumer_id`；也不能选段 B 里只有一条带未命中 Run 的顾客，因为合并之后它的单元可能撑不起留出集。只能从段 B 里有两条或以上带未命中 Run 的顾客中挑；可选的不到两个就判 blocked。
- **簇单元的合并**：诊断会把近重复的两个 Run 并成一个簇（`s6_stage_evolution.py` 的 `find`），连带它们各自顾客的簇也会合并，所以在 `$S6_AT048` 的窗口里只有 3 个单元。at048 用的是这 3 个单元的划分（L6：at048 的序号是 4，3 个单元能给出 6 个子集，够用）。其他段用 `$S6` 的窗口，那个窗口里没有近重复对，仍然是 4 个单元。at048 如果仍然报单元数不够，照实判 blocked，不再造数据。
- **命令**（调度员）：

  ```bash
  uv run --frozen python scripts/acceptance/s6_stage_evolution.py at048 --config "$S6_AT048"
  ```

- **预期**：(a) 和 (b) 都返回 `leakage_detected`；按顾客分组、把近重复并成一簇的候选开评通过（§3）。结果里 `near_duplicate_pairs` 至少为 1，不能出现 note `route near_duplicate not planned`。
- **失败时**：出现 `not planned` 说明那一对的词组不完全相同，判 blocked，不再重发；如果返回的不是 `leakage_detected`，按 §5 F6 判 failed。

#### 段 F at023

- **命令**（调度员）：

  ```bash
  uv run --frozen python scripts/acceptance/s6_stage_evolution.py at023 --config "$S6"
  ```

- **预期**：见 §3 的 AT-023 一行；结果里有 L0 候选的 id。
- **只读核对**：受保护表的 digest 前后不变（结果的 step 里已经比对）。
- **失败时**：按 §5 的 F1、F2、F7 处理。

#### 段 G r1

- **命令**（调度员）：

  ```bash
  uv run --frozen python scripts/acceptance/s6_stage_evolution.py r1 --config "$S6"
  ```

- **预期**：两次 L0 报告的 digest 相同（§3）。
- **失败时**：连接错误按 F9 重跑同一 `label`；digest 不同判 failed。

#### 段 H r7（顺序：撤销授权 → 擦除 → 核对）

- **对象**：只擦 `$S6` 的 `inputs.evolution.erasure_consumer_id`，它是段 B 的 4 个顾客之一。**customer-1..4 只作对照，不擦，也不发言。**下面用 `<C>` 表示这个顾客，`<P>` 表示它的登录主体。
- **执行者**：撤销由调度员执行（配置者窗口，用文件凭据）；擦除和核对在负责人终端执行（approver3，口令经 stdin）。
- **步骤**：
  1. **准备**（调度员）：

     ```bash
     uv run --frozen python scripts/acceptance/s6_stage_evolution.py r7-prepare --config "$S6"
     ```

  2. **撤销**（调度员）：在私有 webchat-consumers 配方里给 `<P>` 那一项加 `"revoke": true`，其余项不动；然后 `--check`，再 `--apply`，命令同 run-add-synthetic-customer §4。预期 `configured: true`，`revoked` 比上次多 1。核对：

     ```bash
     ro "select count(*) filter (where jsonb_array_length(coalesce(f.payload->'grants','[]'::jsonb))>0) n_grants, count(*) n_facts from authz.nexloop_authority_facts f where f.tenant_id='$T' and f.fact_kind='grants' and f.entity_key[1]='<P>'"
     ```

     `n_grants` 应为 0，`n_facts` 仍为 5。
  3. **擦除前预检**（负责人终端）：

     ```bash
     security find-generic-password -s nexloop/stage/approver3 -w | python3 scripts/deploy/erase_synthetic_consumer.py erase --origin <APP_ORIGIN> --ca-file <CA> --username-file <RUN>/approver3.username --password-stdin --config <RUN>/erase-r7.json --dry-run
     ```

     预期 persona 不是 real，`grants_held` 为 0，`grants_trace` 为 `owner_link`。如果结果是 `grants_held_revoke_first`，回到第 2 步。
  4. **擦除**（负责人终端）：同第 3 步的命令，去掉 `--dry-run`。这一步不可逆。
  5. **核对**（负责人终端，等 keeper 跑完一轮后）：再执行一次第 3 步的命令（带 `--dry-run`），应为 `already_erased`，`grants_trace` 为 `cleared_at_erasure`。然后调度员只读核对：

     ```bash
     ro "select grants_cleared from control.nexloop_consumer_erasures where tenant_id='$T' and consumer_id='<C>'"
     ```

     应为 `t`。最后跑：

     ```bash
     uv run --frozen python scripts/acceptance/s6_stage_evolution.py r7-verify --config "$S6"
     ```

- **预期**：S6-R7 一行见 §3。
- **失败时**：
  - 第 3 步显示 persona 是 real，或者对象是 customer-1..4：停下，按 §5 F5 处理，第 4 步不执行。
  - `grants_cleared` 不是 `t`：判 failed，报给负责人（说明擦除时授权没有核对清空）。
  - r7-verify 按 §5 F8 处理。

## 3. 预期输出与通过条件

每条命令输出一行 JSON 并写进 `results_dir`（0600）：`{check, acceptance, release, executor, executed_at, dry_run:false, world, passed, blocked, steps:[{name, expected, observed, passed, evidence}]}`。

| 项 | 通过条件（全部满足） |
|---|---|
| 通用 | `passed: true`、`blocked: false`、退出码 0；每个 step 的 `passed` 都为 true |
| S6-R5 | writer 凭据在 evaluator 端口开评被拒；writer 角色直接调用开评函数报 42501；evaluator 开评成功；非 test 凭据开 L1 报 `test-only evaluation credential`；登记的 test 凭据开 L1 成功 |
| AT-048 | (a) 按 Run 分组、同一消费者落两侧 → `leakage_detected`；(b) 近重复分两侧 → `leakage_detected`；按消费者分组、近重复并簇的候选开评通过 |
| AT-023 | L0 结论 accepted_for_release；L1 在 test world 开会话、结论 accepted、关闭并清快照；被拒候选（G7）状态 rejected；受保护表（正式定义与对象、Claims、候选定义、预算上限、授权事实、服务 grant）digest 前后不变 |
| S6-R1 | 两次 L0 报告 digest 相同，各自通过复现检查；第二次结论按重放处理，库里 digest 与第一次相同 |
| AT-024 | 候选写入（变更范围含 Action）被拒；proposal 冻结（携带 Action、类型越出候选范围）被拒；发布门槛：新类型不带 Action、无 accepted 报告时 `permit_missing`；没有发布任何东西 |
| S6-R7 | 留出集作废（missing_data）；候选 incomplete/missing_data；L1 会话以 candidate_ended 关闭、快照清空；该顾客的 Run 离开 case_refs |

判定时，负责人看结果文件和本单 §4 的证据摘要；任何 blocked 都不算通过（`passed: null`）。

## 4. 证据（写进 NX-038 与相关 AT；不含令牌、DSN、主机地址、用户名或正文）

- 每项的结果行（脚本已脱敏），以及 `results_dir` 下文件的 sha256；
- 执行人：调度员；判定人：负责人；分开写在每一行证据里，AT-023/024/048 的每个表格行都要更新；
- 第 9 步删除工具的输出（只有 id、persona、状态和计数）与 `confirmation_ref`；
- 发布号、`label`、执行时间段。

## 5. 失败时怎么处理

| 现象 | 原因 | 处理 |
|---|---|---|
| F1：任何检查 `blocked` 或失败，原因含 `budget evolution not configured` / `budget_exhausted` | 没有 evolution 上限行时报 `budget evolution not configured`；有上限行但 `limit_amount` 为 0、期间不覆盖现在或已用完时报 `budget_exhausted`（不是 not configured） | 按 §2.0 设预算（调高上限、延长或移动期间也按 §2.0 走放宽审批：`from_*` 填当前行）后重跑同一 `label` |
| F2：`r5`/`at023` 报评估凭据或角色错误 | P2 登记不全，或评估凭据不是 test-only | 跑 `evolution_enroll.py --check`，按其失败项修复；不要手工改登记表 |
| F3：`s6_stage_inputs.py` 有 blocked 的 window / erasure_consumer_id | 合成数据不够（P5） | 先跑合成交互，再查一次；不要放宽 persona 到 real |
| F4：`at024` 报 owner 登录失败或 403 | approver3 不是 active owner，或缺路由权限 | 判 blocked，按 NX-056 运行单核对 approver3 的成员状态；不改用负责人账号 |
| F5：第 8 步预检显示 persona real，或 erasure 已在进行 | 配置里的 Consumer 选错 | 停止，不执行第 9 步；重新用 `s6_stage_inputs.py` 取 erasure_consumer_id |
| F6：`at048` 返回的不是 `leakage_detected` | 泄漏检测失效（0286 未部署或被回退） | 判 failed，报负责人；先核对 P1 中的迁移 |
| F7：AT-023 受保护表 digest 变化 | 候选写进了正式对象，隔离失效 | 判 failed，保留结果文件，报负责人 |
| F8：`r7-verify` 某项没变 | 删除传播没到演进侧，或 keeper 还没跑完 | 等 keeper 一轮（删除工具 `--dry-run` 看 post-check）后重跑一次 `r7-verify`；仍失败判 failed |
| F9：任何步骤报连接、超时 | 网络或服务不可用 | 重跑同一命令（同一 `label` 按重放处理，不会重复写） |

## 6. 缺代码的项

| # | 缺口 | 影响 | 建议 | 估计 |
|---|---|---|---|---|
| G1 | **evolution 预算没有“配置文件加一条命令”的入口**（原状态）：工作台动作路由的字段表里没有 `set_budget`；`real_tenant_init.py budget` 把类型写死为 `model`，且要在 APP_HOST 上用应用端 DSN 和签名密钥 | P3 无法满足，本单全部 blocked | **本分支已补**：`workbench_actions_http.py` 的 `OPERATIONS` 加 `set_budget`（budget_kind 取 model/incentive/evolution，不写死；limit_amount 只收十进制文本；时间必须 UTC；字段严格），`api-routes.v1.json` 把 `goals.budget.set` 从 `capabilities_not_on_http` 移到 `operations`。不新增 Action，不需要迁移。测试：`test_workbench_actions_http.py`（owner 设 evolution 预算成功且重放、七种不合法的请求 422 且不写、operator 403）、`test_evolution_e4_pg.py`（未设预算时开诊断报 `budget evolution not configured`，阳性对照）、`test_api_route_inventory.py`（未上 HTTP 的能力 10 → 9）。NX-056 预算审批（2026-10-28）之后，`set_budget` 只收紧和到期后同额续期，新设和调高走 `loosen_budget`（另一位 owner 批准），见 §2.0 | 已完成 |
| G2 | **S6-R2 无定义**：`S6-stage-acceptance.md`、交接包文档和 `planning/` 中都没有 S6-R2 的定义；`s6_stage_evolution.py` 的 `ACCEPTANCE` 也没有 R2 | 无 | 按笔误处理（调度员 2026-10-28）：NX-038 那条 planning note 里的“R1、R2、R5、R7”应为“R1、R5、R7”，由 L4 更正 planning note。本单不补 R2 | 无 |

## 7. 局限

- L1 是 D16 的合成替身，只返回计数和标记，不跑 Run、不调模型；L0 的延迟等测量来自配置。
- 本单在 L2 本地只核对了入口与参数，**没有在 stage 上执行任何一步**；第 2.1 节的 dry-run 是第一次实际接触 stage。
- 第 9 步不可逆；重跑 R7 需要另一个合成 Consumer 和新的 `label`。
