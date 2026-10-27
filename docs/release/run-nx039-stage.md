# 运行单：NX-039 剩余项的 stage 实测（AT-046 回归的 S6 部分、S6-R3、S6-R4）

- **执行人**：调度员（管理机）。**判定人**：L6。每条证据里两者分开写。
- **依据**：`docs/implementation/S6-stage-acceptance.md`（脚本、配置、断言、执行顺序），以及 NX-038 的运行单 `docs/release/run-nx038-stage.md`（L2，分支 `l2/nx038-stage-runs`，ad55eb36）。本单不复述脚本的断言，只写在 stage 上怎么跑、看什么、失败怎么办，以及和 NX-038 那张单怎么排。
- **入口**（main 17aa74c1 上都有，`scripts/acceptance/s6_stage_evolution.py`）：

| 项 | 命令 | 要什么 |
|---|---|---|
| AT-046 回归（S6 部分） | `s6_stage_evolution.py at046` | 只要角色和凭据登记（P2）；不读诊断输入，不开诊断，不用留出集 |
| S6-R4 留出集用尽 | `s6_stage_evolution.py r4` | P2–P5；用**自己那份**留出集，故意用完（使用序号 1–5，第 6 个被拒） |
| S6-R3 G5 硬门槛 | `s6_stage_evolution.py r3` | P2–P5，另加 P7（某次读取超过 10 条结果并含受保护定义） |

- **身份**：本单三项都不需要人工操作。唯一的人工步骤是 NX-038 §2.0 的 evolution 预算设置，只由合成 owner **approver3** 执行（口令在钥匙串 `nexloop/stage/approver3`）。负责人的账号不登录、不操作。writer、evaluator、reader 是服务凭据，不是人。
- 本单不写正式对象，只写候选、评估和留出集使用记录。`r6`（发布语义版本再回滚）、`r8 --publish` 不在本单内。

## 0. 前提

与 NX-038 运行单 §0 的 **P1–P6 相同**，逐条照那里核对，本单不重复。另加：

| # | 前提 | 怎么核对 |
|---|---|---|
| P7 | R3 的输入：窗口内某次语义读取的结果超过 10 条（占满浏览上限），并且包含配置的 `protected_refs` | `s6_stage_inputs.py` 输出中 `evolution.window` 的 `evidence` 里“占满浏览上限的读取数”≥ 1；`evolution.protected_refs` 的 `status` 为找到，并且来源是这类读取。不满足时，`r3` 会以 `no scoped definition whose alias pushes a configured protected ref past the browse limit` 判 blocked |
| P9 | 合成 Consumer 至少覆盖 **4 个簇单元**。留出集是簇单元的非空真子集：4 个单元有 14 个，够分给 8 项；3 个单元只有 6 个，排在后面的项（其中有 at048、r5、r7、template）会以 `cluster units give … holdouts; 8 are needed` 判 blocked | 只读：`uv run --frozen python scripts/acceptance/s6_stage_evolution.py r4 --config "$S6" --dry-run`，结果中 `inputs` 一步的 `units` ≥ 4（同一步还给出 `runs`、`clusters`、`excluded`）。`s6_stage_inputs.py` 的 `evolution.window` 证据中 Consumer 数 ≥ 4 只是必要条件：近重复的 Run 会并簇，所以以 `units` 为准。不足时，先跑一段涉及更多合成 Consumer 的合成交互，然后两张单一起换 `label` |
| P8 | 本单和 NX-038 的单**用同一份私有配置**（`local-only/s6-stage.json`，同一个 `label`、同一个 `window`），并且 NX-038 的第 7 步（`r7-prepare`）还没有开始 | 配置文件的 sha256 与 NX-038 执行时记录的相同；NX-038 的结果目录里没有 `r7-prepare` 的结果 |

预算（P3）不满足时，按 NX-038 §2.0 由 approver3 设定，**两张单共用这一次设定**，不要再设一次。

## 1. 两张单怎么排：同一次诊断里连续跑

### 1.1 留出集不会互相用掉

- 脚本按 `HOLDOUT_ORDER = ('at023','r1','r3','r4','at048','r5','r7','template')` 给每项分配一份留出集：把诊断的簇单元列成所有非空真子集（按大小、再按组合顺序），第 i 项取第 i 个子集。
  - 不同项的子集不同，所以留出集的 digest 也不同。
  - 每个留出集最多判 5 个候选（D39-3），使用记录按留出集的 digest 计。
- 所以 R4 用掉的“使用序号 1–5”是它自己那份留出集（第 4 个子集）上的 1–5，不占 at023、r1、r3、at048、r5、r7、template 的序号。反过来，NX-038 的各项也用不到 R4 那份。
- `at046` 不用留出集。
- **前提**：两张单看到的簇单元必须完全相同。也就是同一份配置（同一个 `label`、`window`、`personas`），并且中间没有删除任何 Consumer。只要输入变了，第 i 个子集就可能落到别的簇上，可能和另一项已经用过的留出集重合。

### 1.2 什么会打乱分配

| 事件 | 影响 | 所以 |
|---|---|---|
| NX-038 第 9 步删除合成 Consumer（R7） | 这个 Consumer 的 Run 离开窗口，簇单元改变；含它的留出集被作废（`missing_data`） | 本单三项必须在 NX-038 第 7 步之前跑完 |
| 换 `label` 或 `window` | 诊断和候选 id 都变，子集重新分配 | 不换。要换就两张单一起换，并换一批新的合成数据（S6-stage-acceptance：旧留出集已经用过） |
| `s6_stage_inputs.py --only semantic`（NX-038 在 at023 之后执行） | 只取 `semantic`，`evolution` 的各项保持原值 | 不影响，照做 |
| `r6` 发布语义版本 | 移动语义版本指针 | 不在两张单内；如有，放在本单之后 |

### 1.3 合并后的执行顺序（一次诊断、一份配置）

按 S6-stage-acceptance §执行顺序穿插。“038-n”是 NX-038 运行单 §2.2 的第 n 步。

| 顺序 | 单 | 步 | 说明 |
|---|---|---|---|
| 0 | 038 | §2.1 dry-run（`all --dry-run`） | `all` 的 dry-run 也覆盖 at046、r4、r3，一次看全部计划；只读 |
| 1 | **039** | `at046` | 只依赖凭据登记 |
| 2 | 038 | 038-1 `r5`、038-2 `at048` | |
| 3 | **039** | `r4` | 用完它自己的留出集 |
| 4 | 038 | 038-3 `at023`、038-4 `r1` | at023 之后按 NX-038 跑 `--only semantic` |
| 5 | **039** | `r3` | 依赖 `protected_refs`；放在 r1 之后 |
| 6 | 038 | 038-5 `candidate-template` 到 038-10 `r7-verify` | 删除（038-9）在最后 |

- 两张单都逐条执行，**不用 `all`**：`all` 不含 r7，也不能把 NX-038 和 NX-039 的结果分开归档。
- 第 1、3、5 步的先后可以挪动，但都要在顺序 6 之前完成。

## 2. 执行

所有命令在管理机的发布检出根目录执行，`S6=local-only/s6-stage.json`，与 NX-038 相同。

### 2.1 计划（只读；可以和 NX-038 §2.1 合成一次 `all --dry-run`）

```bash
uv run --frozen python scripts/acceptance/s6_stage_evolution.py at046 --config "$S6" --dry-run
uv run --frozen python scripts/acceptance/s6_stage_evolution.py r4 --config "$S6" --dry-run
uv run --frozen python scripts/acceptance/s6_stage_evolution.py r3 --config "$S6" --dry-run
```

预期：每项一行 `nexloop-s6-stage-result/1`，`dry_run: true`，`blocked: false`，退出码 0。并且：

- `r4` 的 `plan` 一步：`holdout_uses` 为 0，`candidates` 为 6，`open_dimensions` 列出已开放的维度（当前 main 上是 `ontology_schema`、`semantic_content`）。`holdout_uses` 不是 0，说明这份留出集已经用过（重跑或输入变了），见 §4 F3。
- `r3` 的 `plan` 一步：`target` 不为空，`protected_refs` ≥ 1。

### 2.2 正式执行（位置见 §1.3）

| 步 | 命令 | 产出 |
|---|---|---|
| 1 | `uv run --frozen python scripts/acceptance/s6_stage_evolution.py at046 --config "$S6"` | AT-046（S6 部分） |
| 3 | `uv run --frozen python scripts/acceptance/s6_stage_evolution.py r4 --config "$S6"` | S6-R4 |
| 5 | `uv run --frozen python scripts/acceptance/s6_stage_evolution.py r3 --config "$S6"` | S6-R3 |

- 同一个 `label` 重跑按重放处理：`r4` 第二次执行时 `holdout_uses` 已是 5，`plan` 一步不通过，结果里写明“需要新的 label 和新的合成数据”。所以 `r4` 只跑一次。

## 3. 预期输出与通过条件

每条命令输出一行 JSON，写进 `results_dir`（0600），形态同 NX-038 §3。

| 项 | 通过条件（全部满足；通用条件同 NX-038：`passed: true`、`blocked: false`、退出码 0、每个 step 都 passed） |
|---|---|
| AT-046（S6 部分） | 评估凭据的 worlds 只有 `test`，并且已登记；它在 real 认证被拒（`refused`）；writer 和 evaluator 的凭据是 real 的服务凭据；两个角色能执行的 security definer 函数只有各自的演进入口（writer：`authz.nexloop_evo_*`、`authz.nexloop_schema_proposal_*`；evaluator：`authz.nexloop_evo_*`；两者另有 `runtime.nexloop_erasure_permits`），没有任何效应入口；world test 的真实派发在部署开关关闭时关闭，强制打开时仍由 world 层关闭 |
| S6-R4 | 同一留出集上的 6 个候选：前 5 个开评成功，使用序号 1–5；第 6 个返回 `holdout_exhausted`，候选仍为 `proposed`；用未开放的维度 `semantic_tool` 提出候选，被拒为 `evolution dimension semantic_tool not open` |
| S6-R3 | G5 候选的验证集得分最高；它的 `failed_gates` 含 `no_forbidden_regression`；结论为 `reject`、原因 `hard_gate_failed`，候选状态 `rejected` |

- AT-046 的世界 test 派发探针只在 world test 上执行，并且在事务里临时打开部署开关，事务结束即恢复，不会影响 real（脚本注释写明了不能对 real 这样做）。
- R4 的 `dimension_closed` 拒绝码在当前 main 上无法触发（两个维度都已开放），按 S6-stage-acceptance 的约定改为验证 propose 时就被拒。判定时以此为准，不算缺项。
- 任何 blocked 都不算通过（`passed: null`）。

## 4. 失败时怎么处理

| 现象 | 原因 | 处理 |
|---|---|---|
| F1：`budget evolution not configured` / `budget_exhausted` | 同 NX-038 F1 | 按 NX-038 §2.0 由 approver3 设定或调高，两张单共用；然后重跑同一 `label` |
| F2：`at046` 的评估凭据 worlds 不是 `["test"]`、未登记，或在 real 认证成功 | 登记错误，或评估凭据被换成了 real 凭据 | 判 failed，停止全部演进检查（R5、L1 都依赖它），报 L6；不要手工改登记表，按 `evolution-stage-enrollment.md` 重新登记 |
| F3：`r4` 的 `plan` 显示 `holdout_uses` 不为 0 | 这份留出集已经用过：重跑，或者输入变了（NX-038 的删除已经执行、换了 `window`） | 不执行。要重做 R4 需要新的 `label` 和新的合成数据；两张单一起换 |
| F4：`r4` 前 5 个中有开评失败（预算以外的原因） | 留出集被其他检查共用，或者开评门槛变化 | 判 failed，保留结果文件，报 L6 |
| F5：`r3` blocked，原因是找不到会把受保护定义挤出浏览上限的定义 | P7 不满足 | 先跑一段浏览范围更宽的合成交互，再用 `s6_stage_inputs.py` 重取；`window` 一旦改变，两张单都要换 `label` 重来 |
| F6：`r3` 的 G5 候选没有被拒 | G5 门槛失效 | 判 failed，报 L6 |
| F7：连接、超时 | 网络或服务不可用 | 重跑同一命令（同一 `label` 按重放处理）；`r4` 重跑前先看 dry-run 的 `holdout_uses` |

## 5. 证据（写进 NX-039 与 AT-046；不含令牌、DSN、主机地址、用户名或正文）

- 每项的结果行（脚本已脱敏），以及 `results_dir` 下文件的 sha256；
- 执行人：调度员；判定人：L6，分开写；AT-046 的表格行写明这次只覆盖 S6 部分；
- 配置文件的 sha256、`label`、发布号、执行时间段，并注明与 NX-038 是同一次诊断，以及本单三项在 NX-038 第 7 步之前完成；
- 若 §2.0 的预算由本次设定：写 approver3 提交的那一行（不含用户名）。

## 6. 局限

- L1 是 D16 的合成替身，只返回计数和标记，不跑 Run、不调模型。
- 本单只在本地核对了入口、参数和留出集的分配方式，**没有在 stage 上执行任何一步**；§2.1 的 dry-run 是第一次实际接触 stage。
- R4 用完的留出集不能再用；重做 R4 需要新的 `label` 和新的合成数据。
