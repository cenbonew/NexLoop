# 运行单：NX-038 剩余项的 stage 实测（AT-023、AT-024、AT-048，S6-R1、R5、R7）

- **执行人**：调度员（管理机）。**判定人**：负责人（按 `planning/` 中 AT-023/024/048 与 NX-038 的记录）。L6 只复核本单，不判定结果。
- **依据**：`docs/implementation/S6-stage-acceptance.md`（脚本、配置、断言、执行顺序）、`docs/implementation/evolution-stage-enrollment.md`（服务主体登记）、`run-nx056-approvals-enable.md`（合成 owner approver2/approver3）。本单不复述脚本的断言，只写在 stage 上怎么跑、看什么、失败怎么办。
- **结论**：六项的入口在 main 17aa74c1 上都有，都依赖同一个前提：租户的 evolution 预算。设预算的入口原来缺失，本分支补上（§6 G1：工作台路由 `set_budget`，由 approver3 用 `approval_drill.py` 提交，即 §2.0）。本单要在含 G1 的发布部署到 stage 之后执行。S6-R2 无定义，按笔误处理（§6 G2）。
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

| # | 前提 | 怎么核对 |
|---|---|---|
| P1 | stage 部署的是 main 17aa74c1 或之后、含迁移 0235/0237/0241/0248/0286（AT-048 修复）的发布 | 部署单；`ro "select version from control.schema_migrations where version in ('0235','0237','0241','0248')"` 返回 4 行（`ro` 见 run-nx057 §0.3）；0286 按集成后的实际编号核对 |
| P2 | 演进服务主体已登记：writer、evaluator、reader 三个服务凭据和一张只含 test 的评估凭据；三个 LOGIN 角色有口令、pg_hba 放行 | `python3 scripts/deploy/evolution_enroll.py --config <私有 enroll 配置> --check`，退出码 0，四项自检都为 true。没有登记过时，先按 `evolution-stage-enrollment.md` 执行（调度员；bootstrap 窗口） |
| P3 | 租户有 world real、kind `evolution` 的预算上限（单位 CNY，期间覆盖本次执行） | `ro "select unit, limit_amount, period_start, period_end from control.nexloop_budget_limits where tenant_id='$TENANT_ID' and world='real' and budget_kind='evolution'"` 有一行。**没有时本单全部判 blocked**：开诊断（`authz.nexloop_evo_diagnosis_open`，0235）在没有 evolution 上限时报 `budget evolution not configured`，每项检查都从开诊断开始；开评与 L1 预留也查这个上限（0241、0248）。没有时按 §2.0 设定 |
| P4 | approver3 是 stage 租户 active 的 owner 成员，持有 `Budget.set:1`、`nexloop.consumer.erase_synthetic:1` 等 owner Action 的授权，且这些 Action 在租户已发布 | **源码判断**：`deploy/authorization/workbench-roles.v1.json` 的 owner 角色含 `eios:action:Budget.set:1`（`existing_governed` 切片），owner 成员经 `workbench_roles.compile_members` 得到全部 owner 授权；run-nx056 §1 已把 approver3 登记为 owner。所以只要 approver3 是 active owner、`Budget.set` 已发布，就有权限。**stage 只读核对**（调度员执行，`ro` 见 run-nx057 §0.3）：`ro "select entity_key[2] from authz.nexloop_authority_facts where tenant_id='$TENANT_ID' and fact_kind='grants' and entity_key[1]='stage-principal-approver3' and entity_key[2] in ('eios:action:Budget.set:1','eios:action:nexloop.consumer.erase_synthetic:1')"` 应有 2 行（授权事实的键是 `[principal_id, Action]`，见 `workbench_roles.py`）；`ro "select resource_id, active from control.nexloop_action_definitions where tenant_id='$TENANT_ID' and world='real' and resource_id='eios:action:Budget.set:1'"` 应有一行，`active` 为 t。没有授权或未发布时判 blocked，按 NX-056 运行单核对成员、按 `stage_secrets publish` 发布，不改用负责人账号 |
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

### 2.0 设 evolution 预算（P3 不满足时；approver3，一次）

照 run-nx056 §8 的驱动形态（口令经钥匙串写进 0600 临时文件，trap 删除；`$WB` 是 stage 工作台的 https 源，只写 scheme 和主机，与 run-nx056 相同）：

```bash
P3=$(f=$(mktemp); chmod 600 "$f"; security find-generic-password -s nexloop/stage/approver3 -w > "$f"; echo "$f"); trap 'rm -f "$P3"' EXIT
D3=(.venv/bin/python scripts/stage/approval_drill.py --base-url "$WB" --username "<approver3 用户名>" --account-label approver3 --password-file "$P3" --evidence-file local-only/nx038-budget.jsonl)
BUDGET='{"budget_kind":"evolution","unit":"CNY","limit_amount":"50.00","period_start":"<本月 1 日 00:00:00Z>","period_end":"<下月 1 日 00:00:00Z>"}'
"${D3[@]}" --dry-run request --operation set_budget --body-json "$BUDGET"
"${D3[@]}" request --operation set_budget --body-json "$BUDGET"
```

- 预期：dry-run 打印 `POST /api/v1/workbench/actions/set_budget` 和五个字段名；正式一行 JSON，HTTP 200，`operation: set_budget`，退出码 0。`set_budget` 不在审批策略 v1 内，不会返回 waiting_approval。
- 字段要求（路由与 SQL 都会查）：`budget_kind` 取 `model|incentive|evolution`；evolution 的 `unit` 是三位大写币种；`limit_amount` 是十进制文本（不收浮点数）；两端时间都是 UTC，期间覆盖本次执行。50 元按 S6-stage-acceptance 的规模（每候选上限 10、L1 预留 0.01）足够，由调度员按需要调整。
- 再执行一次会追加一个新的预算版本（每次新的 Idempotency-Key），值相同不影响结果；不需要重跑。
- 之后用 P3 的只读查询确认有一行。
- 401/403：approver3 不是 active owner 或没有 `Budget.set` 授权（P4）；404 或 `Action not published`：`Budget.set` 未发布；422：字段不合法。都判 blocked，不改用负责人账号。

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
| F1：任何检查 `blocked` 或失败，原因含 `budget evolution not configured` / `budget_exhausted` | P3 没有 evolution 预算，或已用完 | 按 §2.0 设预算（或调高上限）后重跑同一 `label` |
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
| G1 | **evolution 预算没有“配置文件加一条命令”的入口**（原状态）：工作台动作路由的字段表里没有 `set_budget`；`real_tenant_init.py budget` 把类型写死为 `model`，且要在 APP_HOST 上用应用端 DSN 和签名密钥 | P3 无法满足，本单全部 blocked | **本分支已补**：`workbench_actions_http.py` 的 `OPERATIONS` 加 `set_budget`（budget_kind 取 model/incentive/evolution，不写死；limit_amount 只收十进制文本；时间必须 UTC；字段严格），`api-routes.v1.json` 把 `goals.budget.set` 从 `capabilities_not_on_http` 移到 `operations`。不新增 Action，不需要迁移。测试：`test_workbench_actions_http.py`（owner 设 evolution 预算成功且重放、七种不合法的请求 422 且不写、operator 403）、`test_evolution_e4_pg.py`（未设预算时开诊断报 `budget evolution not configured`，阳性对照）、`test_api_route_inventory.py`（未上 HTTP 的能力 10 → 9） | 已完成 |
| G2 | **S6-R2 无定义**：`S6-stage-acceptance.md`、交接包文档和 `planning/` 中都没有 S6-R2 的定义；`s6_stage_evolution.py` 的 `ACCEPTANCE` 也没有 R2 | 无 | 按笔误处理（调度员 2026-10-28）：NX-038 那条 planning note 里的“R1、R2、R5、R7”应为“R1、R5、R7”，由 L4 更正 planning note。本单不补 R2 | 无 |

## 7. 局限

- L1 是 D16 的合成替身，只返回计数和标记，不跑 Run、不调模型；L0 的延迟等测量来自配置。
- 本单在 L2 本地只核对了入口与参数，**没有在 stage 上执行任何一步**；第 2.1 节的 dry-run 是第一次实际接触 stage。
- 第 9 步不可逆；重跑 R7 需要另一个合成 Consumer 和新的 `label`。
