# S6 stage 实测前提核对（NX-038 / NX-040，2026-11-07）

- **依据**：main 50427429 上的这几份文件：
  - `docs/release/run-nx038-stage.md` §0（P1–P6）
  - `docs/release/run-nx040-stage.md` §0（Q1–Q5）
  - `docs/implementation/S6-stage-acceptance.md`「stage 前提」
  - `docs/release/run-s6-stage-prep.md`（C1–C7、A1–A6）
  - 脚本 `scripts/acceptance/s6_stage_evolution.py`、`s6_stage_schema.py`、`s6_stage_inputs.py`、`scripts/deploy/evolution_enroll.py`
- **只读文档核对**：没有连接任何主机，没有碰 stage。下文的"现状"取自 run-s6-stage-prep 记录的调度员 10-29/10-30 说明，未经只读复核。
- **结论先说**：两件事会让执行直接被拦下（判 blocked），开跑前必须先解决。
  1. **P3 的 evolution 预算已经过期**：C1 记录的期间是 10-01 到 11-01，今天是 11-07。
  2. **P5 的合成 Consumer 不够**：只剩 customer-1/2/3 能发言，而 at048、r5、r7、candidate-template 都要 4 个簇单元。
- 其余前提有命令能只读核对，但大多**不在** `s6_stage_evolution.py --dry-run` 里。现在可以用 `s6_preflight.py` 一次报完 P1–P5（见 §1 末）。

## 1. 逐项：能否由脚本只读报告

| # | 前提 | 能报告它的脚本或命令 | 在 `s6_stage_evolution.py all --dry-run` 里？ | 缺口 |
|---|---|---|---|---|
| P1 | 发布与迁移（0235/0237/0241/0248/0286；NX-040 另要 0252） | 运行单 §0 的 `ro` 查询，手工跑 | **否**。dry-run 不读 `control.schema_migrations`；`s6_stage_schema.py` 只在 r8 前后记录目录摘要 | 没有一条命令能一次报出"所需迁移都在" |
| P2 | 演进主体登记：writer、evaluator、reader 三个 LOGIN 角色和服务凭据，加一张只含 test 的评估凭据 | `evolution_enroll.py --config … --check`，四项自检 | **部分**。dry-run 的 `roles` 步骤会按各 DSN 连库并核对 `current_user`，inspector 只读；但不核对凭据令牌是否有效、评估凭据是否只含 test | 以 `--check` 为准 |
| P3 | 租户有 world real、kind evolution 的预算上限，期间覆盖本次执行 | 运行单 §0 的 `ro` 查询，手工跑 | **否**。dry-run 不开诊断；`control.nexloop_budget_limits` 只出现在 AT-023 受保护表的摘要里，不检查有没有这一行 | 正式执行时，第一次开诊断（0235）报 `budget evolution not configured`，那时才发现 |
| P4 | approver3 是 active owner，持有 `Budget.set`、`nexloop.budget.loosen`、`nexloop.consumer.erase_synthetic` 授权且这些 Action 已发布；approver2 已过投票冷却；审批策略 ≥ v5 | 运行单 §0 的三条 `ro` 查询；`approval_drill.py --dry-run` 只打印请求，不连接 | **否**。演进脚本不用 owner 会话；只有 `s6_stage_schema.py at024 --dry-run` 和 r7 的擦除用到 approver3 | 冷却是否已过，只能在批准那一步看到（401/403） |
| P5 | 窗口内有 ≥ 4 个合成 Consumer、带检索未命中的失败 Run；AT-048 (b) 要一对近重复 Run；R3 要一次占满 10 条的读取 | `s6_stage_inputs.py`：报 window（含 Consumer 数，< 4 判 blocked）、erasure_consumer_id、protected_refs、parent_revision、semantic、schema_type、new_property | **部分**。dry-run 会在内存中规划：没有草稿时报 Precondition；at048 没有近重复对时只写 note（`route near_duplicate not planned`），不判 blocked | inputs 脚本不报近重复对，要看 at048 dry-run 的 notes |
| P6 | 管理机检出、`uv`、HTTPS 与四个 DSN | `s6_stage_evolution.py all --dry-run` | **是** | — |
| Q1 | 迁移 0252 | `ro` 查询 | 否 | 同 P1 |
| Q2 | approver3 有 Schema 复核权限，语义发布与回退 Action 已发布 | `schema_proposal.py basis`（要先有 r8 的提案）；之前用 `at024 --dry-run` 看路由可达 | 否 | 语义 Action 未发布，要到 r6 才报 404 |
| Q3 | `inputs.schema_type` 存在且有属性，`new_property`、`new_type` 未被占用 | `s6_stage_inputs.py`（schema_type、new_property、new_type）；`at024 --dry-run` | 否（在 schema 脚本里） | — |
| Q4 | 已有一个已发布的语义版本 | `s6_stage_inputs.py` 的 parent_revision | 否 | 没有时 r6 判 blocked |
| Q5 | 调度员决定是否执行 r8 的正式发布 | 决定，没有命令 | — | — |

### 建议的开跑前只读顺序（调度员，全部只读）

1. `python3 scripts/deploy/evolution_enroll.py --config <私有 enroll 配置> --check`：P2。
2. run-nx038 §0 的 `ro` 查询：P1（加上 0252 即 Q1）、P3（加 `revision` 列）、P4 的三条。
3. `uv run --frozen python scripts/acceptance/s6_stage_inputs.py --config <S6 配置>`：P5、Q3、Q4。
4. `uv run --frozen python scripts/acceptance/s6_stage_evolution.py all --dry-run --config <S6 配置>`：P6，以及每项的规划；看 at048 的 notes 有没有 `near_duplicate not planned`。
5. `uv run --frozen python scripts/acceptance/s6_stage_schema.py at024 --dry-run --config <S6 配置>`：Q2 的路由与 Q3。

任何一步不满足，相关项判 blocked，不开始正式执行。

### 一条命令：`s6_preflight.py`（已补）

`uv run --frozen python scripts/acceptance/s6_preflight.py --config <S6 配置> [--owner-principal stage-principal-approver3]`

- 只用 `roles.inspector`，连接后先设只读；读同一配置里的令牌文件，只比较摘要。
- 一次报告 P1–P5，每项给出 ok、missing、expired 或 no_data，只给数量，不含令牌、DSN、主机、路径、用户名或正文。
  - **P1**：所需迁移是否都在（0235、0237、0241、0248、0252、0286）。
  - **P2**：与 `evolution_enroll.py --check` 同样的三项检查，加三个 LOGIN 角色。
  - **P3**：预算行的金额与期间。期间已结束判 expired，并给出续期办法；没有这一行，或期间还没开始，判 missing。
  - **P4**：approver3 的三项授权、两个预算 Action 已发布且启用、审批策略版本 ≥ 5。approver2 的投票冷却仍只能在批准那一步看到。
  - **P5**：有检索未命中的合成 Consumer 数（需要 ≥ 4），以及跨 Consumer 的近重复对数。近重复对为 0 时判 no_data：AT-048 的 (b) 路线不规划。
- **无数据不算通过**：只有 ok 计入通过；全部 ok 才退出 0，否则退出 1，配置被拒退出 2。
- 测试：`tests/test_s6_preflight_pg.py`，覆盖预算过期、预算缺失或未开始、只有 3 个合成 Consumer、缺迁移、令牌不可读，以及只读、不泄露。

它取代了上面顺序中的第 1、2 步，以及第 3 步里与 P5 有关的部分。window、protected_refs 等输入值仍由 `s6_stage_inputs.py` 给出。

## 2. 负责人和调度员要准备什么

### 2.1 P3 evolution 预算：先续期，不需要审批

- **现状**：C1 记录的行是 `CNY 50.00，2026-10-01T00:00:00Z – 2026-11-01T00:00:00Z`，已经结束。
- **最省事的做法：同额续期**。旧期间已结束，同额、新期间紧接旧期间、长度相同的续期不需要审批（NX-056 设计 Q4；run-nx038 §2.0 倒数第三条）。
  - 由 **approver3** 执行 `set_budget`，新期间是 `2026-11-01T00:00:00Z – 2026-12-01T00:00:00Z`。
  - 先用 P3 的查询加 `revision` 列读出当前行，把 `from_*` 和 `expected_revision` 填成读到的值。
  - 注意：10 月是 31 天，11 月是 30 天，"长度相同"是否要求天数一致，按 SQL 的判定为准。被拒（409）时改走下面的放宽路径，不要猜。
- **需要换金额，或续期被拒时：放宽，要审批**（run-nx038 §2.0 的三条命令）：
  1. approver3 用 `approval_drill.py request --operation loosen_budget` 发起，记下 `approval_id`；
  2. approver2 用 `decide --decision approve` 批准（不能是 approver3，否则 409 `self_approval`）；
  3. approver3 用 `execute` 执行。
  - `from_*` 和 `expected_revision` 填当前行的值。
- **负责人要做的**：确认金额（50 CNY 按 S6-stage-acceptance 的规模足够）。上面的人工步骤都只用合成 owner approver2/approver3，**负责人的账号不登录**。
- **到期时间表**：12-01 再次到期。本轮 S6 实测要在 12-01 前跑完，否则再续一次。

### 2.2 P5 合成交互数据：需要第 4 个合成 Consumer

- **现状**（run-s6-stage-prep A4、§2.1a）：customer-4 和 refusal-1 的 Consumer 已擦除，10-30 起只有 customer-1/2/3 能发言。按 §2.1a 的 3 账号路径，at048、r5、r7、candidate-template 照实判 blocked。
  - candidate-template 被拦，NX-040 的 at024、r10、r8、r6 也跟着走不了，它们都从 candidate-template 开始。
- **要拿到完整结果，必须补第 4 个**。路径是 run-s6-stage-prep §2.2a 的 customer-5：
  1. **负责人在终端执行**：`--apply password`，口令直接生成进钥匙串 `nexloop/stage/customer-5`；
  2. **负责人在终端执行**：`--apply identity`，在 APP_HOST 上 `initial_identity --create`，口令经 ssh stdin 进入容器内的 tmpfs；
  3. 调度员：在 DATA_HOST 把 customer-5 加进私有配方，执行 `webchat_consumers.py --apply`；
  4. **负责人在终端执行**：`--apply open`，customer-5 开一个会话、不发消息，于是有了 Consumer；
  5. 调度员：只读查出 consumer_id，填 `persona-customer-5.json`；
  6. approver3：先 `--apply mark-dry`，再 `--apply mark`，把它标为 simulated_agent。
  - 用之前先做 run-assembly §6.1–6.3。
- **再造数据**：按 run-s6-stage-prep §2.2，用 customer-1/2/3/5 逐轮发言（`synthetic_customer.py` 发言，`run_interactions.py --driver real` 观察）。发言要登录，所以这一步是**负责人在终端执行**。
  - 前提 A1：`ontology_hybrid` 已启用，负责人 10-29 已决定；
  - 前提 A5、A6：没有 online 实验，外发三层开关与计划一致，OUT 已结束或避开。
- **AT-048 (b) 的近重复对**：发言计划里要有两个不同的合成顾客问同一组词的一轮（相同的关键术语）。没有时，at048 的 (b) 路线不规划，只做 (a)，结果里会有 note。
- **R3**：负责人 10-29 已决定不发布 top_k ≥ 10 的策略，r3 照实判 blocked（A2），不必为它造数据。
- **新 label 要新数据**：用新的 `label` 重跑时，旧留出集已用过，要再造一批（S6-stage-acceptance「stage 前提」末条）。

### 2.3 其他

- **P2**：DATA_HOST 上已有四个登记目录（C5），开跑前用 `--check` 确认。如果口令或 pg_hba 失效，按 `evolution-stage-enrollment.md` 的操作单处理，其中 pg_hba 一步由**负责人执行**（调度员的免密 sudo 只能启动备份单元）。
- **Q5**：r8 是否带 `--publish`，由调度员决定，在证据中记"执行 / 不执行"。

## 3. 本次顺带修正的文档

- `S6-stage-acceptance.md`「stage 前提」第一条原文写"负责人此前定了'stage 暂不注册演进服务主体'，调度员会另行请示"，已过时：负责人 2026-10-21 已定案登记（`evolution-stage-enrollment.md` 首节）。改为指向该文档和 `--check`，并写明 dry-run 不报告预算和迁移。
- `run-nx038-stage.md` P3：补一句"期间已结束时先按 §2.0 的同额续期，不需审批"，指向本核对文档。
