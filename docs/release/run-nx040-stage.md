# 运行单：NX-040 剩余项的 stage 实测（AT-024 / S6-R9，S6-R10，S6-R8，S6-R6）

- **执行人**：调度员（管理机）。**判定人**：L6（按 `planning/` 中 AT-024 与 NX-040 的记录）。人工步骤只用合成 owner **approver3**；负责人的账号不登录、不操作。证据里执行人与判定人分开写。
- **依据**：
  - `docs/implementation/S6-stage-acceptance.md`（脚本、配置、断言、执行顺序；第 121 行起的执行顺序是本单的顺序来源）；
  - `scripts/acceptance/s6_stage_schema.py`（`at024`、`r10`、`r8`、`r6`）与 `scripts/acceptance/s6_stage_evolution.py candidate-template`；
  - `scripts/deploy/schema_proposal.py`（owner 的 Schema 提案复核与发布 CLI）；
  - L2 的 NX-038 运行单 `docs/release/run-nx038-stage.md`（分支 `l2/nx038-stage-runs` ad55eb36）：本单与它**共用配置、前提与 evolution 预算**，并且**插在它的第 6 步与第 7 步之间**执行（§1）。
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

**与 NX-038 运行单共用**：P1（发布与迁移）、P2（演进服务主体登记）、P3（evolution 预算，NX-038 §2.0 由 approver3 设定）、P4（approver3 是 active owner）、P5（合成交互数据）、P6（管理机检出与连接）。按 NX-038 运行单 §0 核对，本单不重复。另需：

| # | 前提 | 怎么核对 |
|---|---|---|
| Q1 | 迁移 0252（NX-040 Schema 提案）在库里 | `ro "select 1 from control.schema_migrations where version='0252'"` 返回 1（`ro` 见 run-nx057 §0.3） |
| Q2 | approver3 有 Schema 复核权限（`ontology.schema.review`：owner 与 reviewer 有，operator 403），且 `publish_semantic_release`、`rollback_semantic_release` 已发布 | `security find-generic-password -s nexloop/stage/approver3 -w \| python3 scripts/deploy/schema_proposal.py basis --config <私有 schema 配置> --origin … --username-file … --password-stdin` 对 r8 产出的提案返回 200（§3.3 之后才有提案；之前用 at024 的 dry-run 看路由是否可达）。语义 Action 未发布时 r6 返回 404 / `Action not published`，按 `stage_secrets publish` 发布 |
| Q3 | `inputs.schema_type`（例如 `Consumer`）在 stage 租户存在且**至少有一个属性**；`inputs.new_property` 不是它已有的属性；`inputs.new_type` 不存在 | r10 在 Parent 没有属性时报 blocked（`the Parent of … has no property to break`）；`at024 --dry-run` 不报 `inputs.* required` |
| Q4 | 租户已有一个已发布的语义版本（`parent_revision`） | `s6_stage_inputs.py` 输出的 `parent_revision` 不是 blocked。没有时 r6 在回退一步报 blocked（`no earlier release version to roll back to`），本单 r6 判 blocked，交回调度员 |
| Q5 | 调度员确认是否执行 r8 的正式发布（§3.3）：它会在 stage 租户的正式 Schema 上新增一个合成可选属性，之后用 `hide` 回退为隐藏（新版本，不删除） | 调度员在证据中记“执行 / 不执行” |

## 1. 与 NX-038 运行单的先后顺序

S6-stage-acceptance 规定的总顺序（第 121–126 行）：at023 → 刷新 `semantic` → r1/r3 → candidate-template → **at024、r10、r8** → **r6** → r7。r6 会移动语义版本指针，必须在所有用到 `parent_revision` 的演进检查之后；r7 的删除会改变窗口，必须在最后。所以两张运行单合起来按下表执行：

| 总序 | 运行单 | 步 | 说明 |
|---|---|---|---|
| 1–4 | NX-038 | r5、at048、at023、r1 | at023 的结果里有 L0 语义候选的 id |
| 5 | NX-038 | candidate-template | 写 `inputs.candidate_template` |
| 6 | NX-038 | **at024** | AT-024 同时属于 NX-038 与 NX-040 的剩余项，只执行一次；NX-038 第 6 步已执行时，本单 §3.1 不再执行，直接引用其结果 |
| 7 | **本单 §3.2** | r10 | |
| 8 | **本单 §3.3** | r8（不带 `--publish`），然后可选的正式发布与 `hide` | |
| 9 | **本单 §3.4** | 刷新 `semantic`，然后 r6 | |
| 10–13 | NX-038 | r7-prepare、删除预检、删除、r7-verify | 必须最后 |

NX-039 的 at046、r4、r3 不在两张运行单内；若同一窗口执行，按 S6-stage-acceptance 的顺序插在第 4 步附近，且都在 r6 之前。

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

记下结果里的提案 id。Q5 为“执行”时，用路 2 发布（approver3）：

```bash
python3 scripts/deploy/schema_proposal.py check   --config local-only/s6-r8-proposal.json --dry-run
security find-generic-password -s nexloop/stage/approver3 -w | python3 scripts/deploy/schema_proposal.py check   --config local-only/s6-r8-proposal.json --origin https://<APP_HOST_NAME>:<PORT> --ca-file <CA> --username-file <approver3 用户名文件> --password-stdin
security find-generic-password -s nexloop/stage/approver3 -w | python3 scripts/deploy/schema_proposal.py publish --config local-only/s6-r8-proposal.json --origin https://<APP_HOST_NAME>:<PORT> --ca-file <CA> --username-file <approver3 用户名文件> --password-stdin
```

- `check` 预期：F1 允许，类别 minor，影响清单只有 `inputs.schema_type`；
- `publish` 预期：`published: true`，新的 Schema 版本号；发布前后由 `verify` 核对迁移目录与 `bootstrap.verify` 不变——用路 1 时脚本自动核对，用路 2 时由调度员执行一次 `s6_stage_schema.py r8`（同一 `label`，按重放处理）取 `catalog_and_verify_unchanged` 一步作为发布后的核对；
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
- 发布号、`label`、执行时间段，以及与 NX-038 运行单合并执行的总序号（§1）。

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

- 路 2 发布后取“迁移目录与 verify 不变”的证据，要再跑一次 `s6_stage_schema.py r8`（重放）；如果希望 `schema_proposal.py publish` 自己输出 verify 摘要，可另立一个小改动（约 0.5 天），不阻塞本单。

## 8. 局限

- L1 是 D16 的合成替身；G9–G13 中的测量输入来自配置。
- r8 的正式发布在 stage 租户的正式 Schema 上留下一个隐藏的合成属性版本（`hide` 之后仍有版本记录），这是有意保留的审计痕迹。
- r6 发布与回退各产生一个语义版本；之后 7 天的观察结果不在本单判定范围内。
- 本单没有在 stage 上执行任何一步；§3.0 的 dry-run 是第一次实际接触 stage。
