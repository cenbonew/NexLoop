# 运行单：NX-040 剩余项的 stage 实测（AT-024 / S6-R9，S6-R10，S6-R8，S6-R6）

- **执行人**：调度员（管理机）。**判定人**：L6（按 `planning/` 中 AT-024 与 NX-040 的记录）。人工步骤只用合成 owner **approver3**；负责人的账号不登录、不操作。证据里执行人与判定人分开写。
- **依据**：
  - `docs/implementation/S6-stage-acceptance.md`（脚本、配置、断言、执行顺序；第 121 行起的执行顺序是本单的顺序来源）；
  - `scripts/acceptance/s6_stage_schema.py`（`at024`、`r10`、`r8`、`r6`）与 `scripts/acceptance/s6_stage_evolution.py candidate-template`；
  - `scripts/deploy/schema_proposal.py`（owner 的 Schema 提案复核与发布 CLI）；
  - L2 的 NX-038 运行单 `docs/release/run-nx038-stage.md`（分支 `l2/nx038-stage-runs` ad55eb36）与 L3 的 NX-039 运行单 `docs/release/run-nx039-stage.md`（分支 `l3/nx039-stage-runs` 06948eb4）：三张单**共用配置、前提与 evolution 预算**；本单的 r10、r8 插在 038-6（at024）之后、038-7（r7-prepare）之前，r6 放在 038-10（r7-verify）之后，r8 的正式发布放在最后（§1）。
- 本单不复述脚本的断言，只写在 stage 上怎么跑、看什么、失败怎么办。本单在 L4 本地只核对了 main 17aa74c1 上的入口与参数，**没有连接 stage**。
- **入口**（main 17aa74c1）：

| 项 | 入口 | 写入什么 | 能否直接在 stage 跑 |
|---|---|---|---|
| AT-024 / S6-R9 结构权限 | `s6_stage_schema.py at024` | 候选与提案冻结（演进侧）；**不发布** | 入口有；前置是 `s6_stage_evolution.py candidate-template` |
| S6-R10 破坏性变更关闭 | `s6_stage_schema.py r10` | 一个候选与提案冻结；三种破坏性变更只在 F1 判定中构造，**不发布** | 入口有；同上 |
| S6-R8 Schema 兼容 | `s6_stage_schema.py r8`；`--publish` 为 B 类 | 候选、提案、bundle、开评、reflow 预留（计入 evolution 预算）、结论与发布许可；加 `--publish` 时在 stage 租户的正式 Schema 上新增一个合成可选属性 | 入口有；`--publish` 需调度员确认（§3.3） |
| S6-R6 语义版本发布与回退 | `s6_stage_schema.py r6` | 发布一个语义版本，再回退为更高的新版本；开启 7 天观察 | 入口有；依赖 at023 的 L0 语义候选（§3.4） |

- **Schema 发布的两条路**（同一组工作台路由，都经 `SchemaPublicationPort`，SQL 复核 F4 发布许可、F1、后继 Action 与权限证明）：
  1. `s6_stage_schema.py r8 --publish`：脚本以 approver3 的工作台会话调用 `POST /api/v1/workbench/schema-proposals/{id}/publish`；
  2. owner CLI `scripts/deploy/schema_proposal.py publish`（同一路由，配置文件加一条命令）：适合在 r8 不带 `--publish` 跑完后，由 approver3 单独发布；回退用同一 CLI 的 `hide`。
  两条路二选一，**不要两条都执行**（同一提案第二次发布会被拒或按重放返回，徒增一条审计）。本单推荐路 2，因为它把 B 类发布与验收脚本分开，`check` 可以先看 F1 结论与影响清单。
- 语义版本的发布与回退经工作台 Action `publish_semantic_release` / `rollback_semantic_release`（`s6_stage_schema.py r6` 内部调用），不经 Schema 发布路由。
- 这几个 Action 都不在 stage 已应用的审批策略（approval-policies v4：真实派发、擦除、保留、owner 成员管理）里，不会返回 waiting_approval。

## 0. 前提（不满足就不开始）

**与 NX-038 运行单共用**：P1（发布与迁移）、P2（演进服务主体登记）、P3（evolution 预算：按 0369 新设预算算放宽，要走审批——approver3 发起和执行，approver2 批准，按 NX-038 §2.0）、P4（approver3 是 active owner）、P5（合成交互数据）、P6（管理机检出与连接）。按 NX-038 运行单 §0 核对，本单不重复。另需：

| # | 前提 | 怎么核对 |
|---|---|---|
| Q1 | 迁移 0252（NX-040 Schema 提案）在库里 | `ro "select 1 from control.schema_migrations where version='0252'"` 返回 1（`ro` 见 run-nx057 §0.3） |
| Q2 | approver3 有 Schema 复核权限（`ontology.schema.review`：owner 与 reviewer 有，operator 403），且 `publish_semantic_release`、`rollback_semantic_release` 已发布 | `security find-generic-password -s nexloop/stage/approver3 -w \| python3 scripts/deploy/schema_proposal.py basis --config <私有 schema 配置> --origin … --username-file … --password-stdin` 对 r8 产出的提案返回 200（§3.3 之后才有提案；之前用 at024 的 dry-run 看路由是否可达）。语义 Action 未发布时 r6 返回 404 / `Action not published`，按 `stage_secrets publish` 发布 |
| Q3 | `inputs.schema_type`（例如 `Consumer`）在 stage 租户存在且**至少有一个属性**；`inputs.new_property` 不是它已有的属性；`inputs.new_type` 不存在 | r10 在 Parent 没有属性时报 blocked（`the Parent of … has no property to break`）；`at024 --dry-run` 不报 `inputs.* required` |
| Q4 | 租户已有一个已发布的语义版本（`parent_revision`） | `s6_stage_inputs.py` 输出的 `parent_revision` 不是 blocked。没有时 r6 在回退一步报 blocked（`no earlier release version to roll back to`），本单 r6 判 blocked，交回调度员 |
| Q5 | 调度员确认是否执行 r8 的正式发布（§3.3）：它会在 stage 租户的正式 Schema 上新增一个合成可选属性，之后用 `hide` 回退为隐藏（新版本，不删除） | 调度员在证据中记“执行 / 不执行” |

## 1. 与 NX-038、NX-039 运行单的先后顺序

三张单共用一次诊断、一份配置（同一 `label`、`window`、`personas`）和一次 evolution 预算设定。L3 的 NX-039 运行单（`l3/nx039-stage-runs` 06948eb4，§1.3）已排好 NX-038 与 NX-039 的合并顺序，删除顾客（038-9）在最后。本单在它的基础上插入 NX-040 的四项；“038-n”是 NX-038 运行单 §2.2 的第 n 步，“039 顺序 n”是 NX-039 运行单 §1.3 的顺序号。

| 总序 | 单 | 步 | 说明 |
|---|---|---|---|
| 0 | 038 | §2.1 `all --dry-run`，加本单 §3.0 的四个 dry-run | 只读 |
| 1–5 | 039 / 038 | 039 顺序 1–5：`at046`；038-1 `r5`、038-2 `at048`；`r4`；038-3 `at023`、038-4 `r1`（之后 `--only semantic`）；`r3` | 不变，照 NX-039 §1.3 |
| 6 | 038 | 038-5 `candidate-template` | 写 `inputs.candidate_template` |
| 7 | 038 | 038-6 **`at024`**（AT-024 / S6-R9） | NX-038 与 NX-040 共用，只执行一次（本单 §3.1 只在 038-6 未执行时补跑） |
| 8 | **040** | §3.2 **`r10`** | |
| 9 | **040** | §3.3 **`r8`**（不带 `--publish`） | 开评一次 |
| 10–13 | 038 | 038-7 `r7-prepare` → 038-8 删除预检 → 038-9 删除 → 038-10 `r7-verify` | 删除在所有用到诊断输入的步骤之后 |
| 14 | **040** | §3.4 核对 `semantic`，然后 **`r6`** | 移动语义指针，放在 r7 全部完成之后（L6 S1） |
| 15 | **040** | （可选，Q5）§3.3 路 2 正式发布 r8 的提案，再 `hide` | 唯一写正式 Schema 的一步，放在全部验收之后 |

**为什么插在这里**：

- **at024、r10、r8 放在 candidate-template（总序 6）之后**：三项都从 `inputs.candidate_template` 起步（`s6_stage_schema.py` 的 `candidate_body`），没有它就报缺失；candidate-template 本身在 038-5，所以三项只能在它之后。at024 与 NX-038 共用，跟在 038-5 后面正是 038-6。
- **at024、r10、r8 放在删除（总序 10–13）之前**：candidate-template 的候选与 r8 的开评用的是诊断里分给 `template` 的那份留出集（`s6_stage_evolution.py` 的 `HOLDOUT_ORDER` 最后一项）。删除合成 Consumer 会改变簇单元，含它的留出集被作废（`missing_data`），r8 的开评就会失败或落到别的簇上。这与 NX-039 把 r3、r4 放在删除之前是同一个理由。
- **留出集不冲突**：at024、r10 只做候选写入与提案冻结，不开评；r8 开评一次，用的是 `template` 那份，不占 at023、r1、r3、r4、at048、r5、r7 的留出集，也远低于每份 5 个候选的上限（D39-3）。
- **r6 放在 at023（总序 4）之后**：r6 发布的是 at023 产出的 L0 accepted_for_release 语义候选（S6-stage-acceptance 第 121 行），`--only semantic` 也在 at023 之后执行。
- **r6 放在所有用到 `parent_revision` 的演进检查之后**：r6 发布语义版本、再回退为更高的新版本，会移动语义版本指针。at023、r1、r3、r4、at048、r5、candidate-template 和 r7-prepare 都以配置里的 `parent_revision` 为候选的父版本；NX-039 §1.2 也要求 r6 放在 NX-039 之后。总序 14 时这些都已完成。§3.4 在删除之后执行的 `--only semantic` 只取 `semantic` 一项，不重新取窗口，不受删除影响。
- **r6 放在 r7-verify 之后（L6 S1，与 S6-stage-acceptance 第 125–126 行的顺序不同）**：r7-prepare 仍以配置里旧的 `parent_revision` 起草候选。SQL 里只有语义发布有 `parent_moved` 检查，候选写入和开评都没有，所以 r6 先移动指针大概率不影响 r7，但没有验证过。r6 不依赖窗口，也不依赖被删除的 Consumer（它发布的是 at023 的候选，`--only semantic` 在总序 4 之后已经取好），所以把它移到 r7 全部完成之后，r7 的四步就不受指针移动的影响。删除仍是所有用到诊断输入的步骤中的最后一步。
- **r8 的正式发布放在最后（总序 15，r6 之后）**：它是唯一写正式 Schema 的一步（B 类，Q5），与验收无关的 `hide` 回退也跟着它。放在全部验收之后，发布或回退出问题不会影响任何一项验收的输入，验收结果也不依赖它。r8 的验收本身（总序 9）不带 `--publish`。

## 2. 配置

沿用 NX-038 运行单 §1 的私有配置 `S6=local-only/s6-stage.json`（0600），本单另填：

- `inputs.schema_type`、`inputs.new_property`、`inputs.new_type`：例如 `Consumer`、`s6_preferred_store`、`S6Voucher`（`deploy/examples/s6-stage.example.json`）；名字用合成的、带 `s6_` 前缀，便于事后辨认；
- 可选 `inputs.restricted`：一个负责人受限属性组中的 `{type_name, property_name}`，用于 AT-024 的再暴露检查；
- `inputs.semantic`：§3.4 由 `s6_stage_inputs.py --only semantic` 填写；
- 路 2 发布另需 `deploy/examples/schema-proposal.example.json` 的私有副本：`proposal_id` 填 r8 结果中的提案 id，`rationale` 写“S6-R8 add_optional_property（合成）”。

## 3. 执行

所有命令在管理机的发布检出根目录执行。口令只走钥匙串管道，不进参数、文件名或输出。

### 3.0 计划（只读）

```bash
for C in at024 r10 r8 r6; do
  security find-generic-password -s nexloop/stage/approver3 -w | uv run --frozen python scripts/acceptance/s6_stage_schema.py "$C" --config "$S6" --dry-run
done
```

预期：每项一行 `nexloop-s6-stage-result/1`，`dry_run: true`，`plan` 一步列出要调用的受治理入口，退出码 0。

### 3.1 AT-024 / S6-R9（若 NX-038 第 6 步未执行）

```bash
security find-generic-password -s nexloop/stage/approver3 -w | uv run --frozen python scripts/acceptance/s6_stage_schema.py at024 --config "$S6"
```

### 3.2 S6-R10

```bash
security find-generic-password -s nexloop/stage/approver3 -w | uv run --frozen python scripts/acceptance/s6_stage_schema.py r10 --config "$S6"
```

### 3.3 S6-R8

```bash
security find-generic-password -s nexloop/stage/approver3 -w | uv run --frozen python scripts/acceptance/s6_stage_schema.py r8 --config "$S6"
```

记下结果里的提案 id。Q5 为“执行”时，**在总序 15（NX-038 的 r7-verify 之后）**用路 2 发布（approver3）：

```bash
python3 scripts/deploy/schema_proposal.py check   --config local-only/s6-r8-proposal.json --dry-run
security find-generic-password -s nexloop/stage/approver3 -w | python3 scripts/deploy/schema_proposal.py check   --config local-only/s6-r8-proposal.json --origin https://<APP_HOST_NAME>:<PORT> --ca-file <CA> --username-file <approver3 用户名文件> --password-stdin
security find-generic-password -s nexloop/stage/approver3 -w | python3 scripts/deploy/schema_proposal.py publish --config local-only/s6-r8-proposal.json --origin https://<APP_HOST_NAME>:<PORT> --ca-file <CA> --username-file <approver3 用户名文件> --password-stdin
```

- `check` 预期：F1 允许，类别 minor，影响清单只有 `inputs.schema_type`；
- `publish` 预期：`published: true`，新的 Schema 版本号；
- **发布后的核对（L6 S2，方法：直接执行 `bootstrap.verify`）**：用路 1 时脚本自动核对（`owner_publication_changes_no_catalog`）。用路 2 时，**不**靠再跑一次 r8 的重放（重放会先重新提案、冻结和开评，不保证能走到 verify 那一步），而是直接执行：

  ```bash
  uv run --frozen python scripts/acceptance/s6_bootstrap_verify.py --config "$S6"
  ```

  它用 S6 配置里 r8 同一个 verify 连接（`roles.verify.dsn_file`，只读事务）执行 `bootstrap.verify`，输出一行 `{revision, migration_count, digest}`，与 r8 的 `catalog_and_verify_unchanged` 同一摘要。通过条件：等于总序 9 的 r8 结果中 `catalog_and_verify_unchanged.observed.before`。本地测试 `tests/test_s6_stage_schema_pg.py::test_r8_goes_through_g9_g13_and_the_catalog_stays` 在 owner 发布之后执行它，结果与 r8 的 before 相同；没有配置 verify 连接时以退出码 2 拒绝，不算通过；
- 验收后用同一 CLI 的 `hide` 把这个合成属性回退为隐藏（新 Schema 版本，不删除对象数据），输出记入证据。

### 3.4 S6-R6

先刷新 `semantic`（只取这一项，其他输入保持第一次的值）：

```bash
uv run --frozen python scripts/acceptance/s6_stage_inputs.py --config "$S6" --only semantic
```

`semantic.candidate_id` 应等于 NX-038 第 3 步 at023 结果中的 L0 候选 id（S6-stage-acceptance 第 121 行）；不一致时以 at023 的结果为准，手工改写配置并在证据中注明。然后：

```bash
security find-generic-password -s nexloop/stage/approver3 -w | uv run --frozen python scripts/acceptance/s6_stage_schema.py r6 --config "$S6"
```

r6 发布一个语义版本、立即回退为更高的新版本，并开启 7 天观察。之后 7 天内语义观察告警若触发，按 NX-039 的观察流程处理，不在本单内。

## 4. 预期输出与通过条件

每条命令输出一行 JSON 并写进 `results_dir`（0600），格式同 NX-038 运行单 §3。

| 项 | 通过条件（全部满足） |
|---|---|
| 通用 | `passed: true`、`blocked: false`、退出码 0；每个 step 的 `passed` 都为 true |
| AT-024 / S6-R9 | 候选写入（变更范围含 Action）被拒；提案冻结（携带 Action、类型越出候选范围）被拒；发布门：新类型不带 Action、无 accepted 报告时 `permit_missing`；配置了 `restricted` 时再暴露被拒（`property_security_bypass`）；没有发布任何东西 |
| S6-R10 | 删除属性、改值类型、改为必填三种破坏性变更，各自以 `breaking_remove_property`、`breaking_change_value_type`、`breaking_required_property` 被 F1 拒绝，类别 major |
| S6-R8 | bundle 的 `hard_gates_version` 为 `nx040-gates-1`；test world 开评；G9–G13 结论 accept、类别 minor、无失败门；`accepted_for_release` 且有发布许可；迁移目录修订、迁移数与 `bootstrap.verify` 摘要前后相同。执行正式发布时：`published: true`，迁移目录与 verify 仍不变 |
| S6-R6 | 发布得到新版本；回退得到比发布更高的新版本，`claims_changed = 0`；语义复核读取中观察 `active ≥ 1` |

任何 blocked 都不算通过（`passed: null`）。

## 5. 证据（写进 NX-040 与 AT-024；不含令牌、DSN、主机地址、用户名或正文）

- 每项的结果行（脚本已脱敏）及 `results_dir` 下文件的 sha256；
- 执行人：调度员；人工步骤：approver3（工作台会话，写明时间与操作名）；判定人：L6。三者分开写，AT-024 的每个表格行都要更新；
- r8 的提案 id、发布与 `hide` 后的 Schema 版本号（执行正式发布时）；r6 的发布版本、回退版本与观察状态；
- 发布号、`label`、执行时间段，以及与 NX-038、NX-039 运行单合并执行的总序号（§1）。

## 6. 失败时怎么处理

| 现象 | 原因 | 处理 |
|---|---|---|
| F1：`blocked`，原因含 `budget evolution not configured` / `budget_exhausted` | evolution 预算缺失或用完（r8 的 reflow 预留计入 evolution） | 按 NX-038 运行单 §2.0 设定或调高后，同一 `label` 重跑 |
| F2：`inputs.* required` 或 r10 报 Parent 没有属性 | Q3 不满足 | 改 `inputs.schema_type` 为有属性的类型；不改脚本 |
| F3：at024 / r8 / r10 报 `candidate_template` 缺失 | NX-038 第 5 步没有执行 | 先执行 candidate-template，不跳过 |
| F4：r8 的 G9–G13 有失败门，或不是 minor | 工具回归集、Context 体量或 reflow 预留不满足 | 判 failed，保留结果文件，报 L6；不放宽门槛 |
| F5：`catalog_and_verify_unchanged` 失败 | 发布改动了迁移目录或 bootstrap 校验 | 判 failed，立即停止后续发布步骤，报调度员 |
| F6：r6 报 `no earlier release version to roll back to` | 租户还没有已发布的语义版本（Q4） | 判 blocked，交回调度员；不为凑条件先发一个版本 |
| F7：r6 报 404 或 `Action not published` | 语义发布 Action 未发布（Q2） | `stage_secrets publish` 后重跑 |
| F8：工作台 401/403 | approver3 不是 active owner，或缺 `ontology.schema.review` | 判 blocked，按 NX-056 运行单核对成员状态；不改用负责人账号 |
| F9：连接、超时 | 网络或服务不可用 | 重跑同一命令（同一 `label` 按重放处理） |

## 7. 缺代码的项

在 main 17aa74c1 上，本单的四项都有入口，evolution 预算的入口由 L2 的 G1 补上（L6 PASS），**没有新的代码缺口**。需要注意：

- 路 2 发布后的“迁移目录与 verify 不变”由新增的 `scripts/acceptance/s6_bootstrap_verify.py` 直接核对（§3.3），不再依赖 r8 的重放。

## 8. 局限

- L1 是 D16 的合成替身；G9–G13 中的测量输入来自配置。
- r8 的正式发布在 stage 租户的正式 Schema 上留下一个隐藏的合成属性版本（`hide` 之后仍有版本记录），这是有意保留的审计痕迹。
- r6 发布与回退各产生一个语义版本；之后 7 天的观察结果不在本单判定范围内。
- 本单没有在 stage 上执行任何一步；§3.0 的 dry-run 是第一次实际接触 stage。
