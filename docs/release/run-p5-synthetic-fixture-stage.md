# 运行单：NX-038 P5 路线 A，stage 合成夹具（发布、预热、ADR-045 两轮、P5 判定、退役）

- **依据**：
  - `docs/planning-drafts/p5-coverage-design-20261111.md`、`p5-fixture-q1q6-20261111.md`（Q1–Q6 本地通过，933f8f82）；
  - 负责人 2026-11-11 的决定：“都按推荐”，即 K2 加路线 A，Q5-a，附条件 Q5-1、Q5-2；
  - L6 的条件 A1–A5、P1–P4；
  - 合成交互计划沿用 `local-only/opday/p5-*`（措辞重新选，见 §4）。
- **证据定性（A1）**：本单证明的是“演进机制在 stage 上能端到端走通”，不是发现了真实目录缺口。段 D–H 的证据一律写明数据源是合成夹具 `S6SyntheticFixture`。“stage 目录对合成问题全覆盖”另作事实记录。
- **执行者**：
  - 调度员：只读核对、DATA_HOST 容器内的工具、register；
  - 负责人：所有要登录工作台的步骤，包括 F3 发布、ADR-045 开关和合成顾客发言，口令只从 Keychain 经 stdin 传入；
  - 判定者：L6。
- **失败即停**：任何一步不符合预期，都停在那一步报调度员，不要跳步，不要重试写操作。
- **全程不变（A3/P1）**：从 §4 预热到 §6 r2 结束，不得有部署、目录、词表、策略或 top_k 的任何变更。

## 0. 前提

| # | 前提 | 核对 |
|---|---|---|
| P0.1 | stage 的发布包含 910c2b6d 或之后的代码 | 部署单 |
| P0.2 | ADR-045 开关已关 | `fault-switch.sh status` 打印的 SQL：active 为 0 |
| P0.3 | evolution 预算在期内，并且够 7 次 r8 各 0.01 CNY 的 reflow | run-nx038 §0 P3 的查询 |
| P0.4 | `S6SyntheticFixture` 不存在 | `ro "select count(*) from ontology.object_type_versions where tenant_id='$TENANT_ID' and type_name='S6SyntheticFixture'"`：0 |
| P0.5 | stage 的发布包含 G1 的 `s6_stage_schema.py fixture-type` | 部署单；管理机上执行 `s6_stage_schema.py fixture-type --config "$S6" --dry-run`，退出码 0 |

**G1（已补，L6 判词 G1）**：`s6_stage_schema.py fixture-type` 和 r8 走同一条 G9–G13 链路：提案、gates、release permit，加上 `--publish` 时由负责人在 F3 发布。
- 只接受 `inputs.fixture.type_name`，而且必须匹配 `^S6Synthetic[A-Za-z]*$`；dry-run 也先校验，不符合就直接拒绝，什么都不写。
- schema_only 模板必须是同一负责人决定里、列在 `owner_directed_properties` 中的夹具属性。
- **与 L6 原条件不同的一处**：L6 要求改动固定为 `{"properties": []}`，但 0416 的 `runtime.nexloop_schema_change_valid` 只接受 1–64 个属性的 `add_object_type`，空类型在冻结时就会报 `schema proposal invalid`（本地测试复现）。所以改动固定为唯一的标记属性 `{"properties": [{"property_name": "fixture_marker", "value_type": "string"}]}`。它没有显示名；ASCII 名字只在整词出现时才算词，中文句子抽不到它。其他任何改动，包括空改动，都会被拒绝。请 L6 确认。
- 测试：`tests/test_s6_stage_schema_pg.py` 中 fixture-type 的三组用例（正向发布；其他类型名、模式外名称、真实类型名、其他改动、空改动全部拒绝且不写；at024 的拒绝演示不变），本地 25 项全过。NX-068 的门禁不在 910c2b6d 上，组批时要在含 0449 的基线上再跑一次，预期 pass：新类型没有 Action，门禁没有可判的后继。

## 1. 发布夹具：7 次，都经提案流程，由负责人在 F3 发布

在 `$S6`（管理机）和 DATA_HOST 两份配置的 `inputs` 里加上下面这些，两份同步，各记一次 sha256：
- `fixture.type_name = "S6SyntheticFixture"`；
- `owner_directed_properties` 里加 6 条，每条形如 `{type_name: "S6SyntheticFixture", property_name, decision: "owner 2026-11-11 P5 路线 A"}`，属性是 `fx_a…fx_e` 和 `zz_t`。

| 步 | 执行者 | 命令（先 `--dry-run`，看过再去掉） | 预期 |
|---|---|---|---|
| 1.1 | 负责人 | `security find-generic-password -s nexloop/stage/approver3 -w \| uv run --frozen python scripts/acceptance/s6_stage_schema.py fixture-type --config "$S6" --publish`（先 `--dry-run`） | published，version_after=1，successor_actions 为 []，schema 只有 `fixture_marker` |
| 1.2–1.7 | 调度员先在 DATA_HOST 容器里生成模板，负责人再执行 r8；每次之前按 R4 核对预算 | 每个属性两步。① 调度员：`s6_stage_evolution.py candidate-template --mode schema_only --property S6SyntheticFixture.<p>`。② 负责人：`security … -w \| … s6_stage_schema.py r8 --config "$S6" --publish`，配置里 `schema_type=S6SyntheticFixture`、`new_property=<p>`、`property_change={property_name:<p>, value_type:"string", display_name:<F 或 T>, description:"synthetic fixture (NX-038 P5)"}` | 每次都是 accepted、permit、published；version_after 依次为 2…7，successor_actions 都为 [] |

**核对（调度员，`ro`）**：
```
ro "select max(version) from ontology.object_type_versions where tenant_id='$TENANT_ID' and type_name='S6SyntheticFixture'"          -- 7
ro "select count(*) from ontology.objects where tenant_id='$TENANT_ID' and type_name='S6SyntheticFixture'"                             -- 0
ro "select count(*) from control.nexloop_action_definitions where tenant_id='$TENANT_ID' and definition::text like '%S6SyntheticFixture%'"  -- 0（A2）
```

**R4（每次 r8 之前，调度员）**：先用 run-nx038 §0 P3 的查询确认 evolution 预算在期内并且还有余量，不足就不开始这一次。7 次发布中**任何一次失败都停下，不要从中间续跑**：把已发布的版本号和失败那一步的结果文件报给 L6，由 L6 决定怎么接续。半成品的夹具（少几个属性）只要没有 definition_read_rule 就不会被运行时读到，不必回滚。

## 2. 定义读规则（Q5-1）

1. **改清单**：这是一个单独的提交，走批次。在 `deploy/authorization/stage-principals.v1.json` 的 `definition_read_rules`，**只在 relay_source 那一条**的 `type_names` 里加 `S6SyntheticFixture`，并在 `purpose` 里写明“NX-038 P5 合成夹具，A4 退役时移除”。不加别的类型，不加别的主体。
2. **保存前快照（调度员，`ro`）**：
   ```
   ro "select entity_key, fact from authz.nexloop_authority_facts where tenant_id='$TENANT_ID' and fact_kind='definition_read_rule'"
   ```
   输出存档为 `q5-before`。
3. **register dry-run**（DATA_HOST，调度员，命令形态同 run-34 §1.5）：差异**只有**这一条规则多了 `S6SyntheticFixture`；没有新建凭据，没有轮换，没有其他差异。多出任何一项都停下。
4. **register 正式执行**，然后再取一次快照，存档为 `q5-after`。两份快照的 diff 应当只有 type_names 多了一项。

**R1（§2 之后、§4 之前，调度员，`ro`）**：确认召回索引已经为夹具 v7 建好行。索引由 recall-index 服务的 reflow 补齐（每个 tick 先 reflow）：

```
ro "select source_revision, field, count(*) from ontology.nexloop_recall_entries where tenant_id='$TENANT_ID' and source_key='type:S6SyntheticFixture' group by 1,2 order by 1,2"
```

预期：只有 source_revision 7 一个版本；display_name 行共 6 条（5 条 F、1 条 T），另有 name 行。不是 7，或者行数不对，就停下，等下一个 reflow tick 再查；仍然不对就报 L6。

## 3. A2：上线前的只读核对（调度员）

措辞和 F、T 在 §4 选定之后执行，任何一条不满足都停：
- 近 30 天 world real 的全部 Message 正文里，按子串规则不含 F 也不含 T：`p5-fixture-q1q6-20261111.md` §2 的 SQL，预期 0；
- F、T 与 `local-only/opday/p5-catalog.tsv` 中的任何目录词互不包含；
- §1 的两条核对（没有对象，没有 Action）。

## 4. 措辞与预热（开关关着）

- **选词**：
  - F 和 T 是业务上不会出现的组合词；
  - T 的 sha256 要小于 F 和全部 U_i（离线算）；
  - 各顾客的 U_i 可以沿用 11-11 的四个词：服务项目、联系偏好、送货时间、激励方案。11-11 实测它们都能命中，读出数已知；
  - 句子全部用询问句（L6 W1）。

  每句的形态是“F 和 T……，另外 U_i ……？”，写进 `p5-messages.txt`，然后调度员跑 `p5-probe.dh.sh check`：四行都是 terms，key 都等于 hash(T)，集合两两不同。
- **记录 A3 的配置（调度员，`ro`）**：当前 context 策略的版本和 `semantic.top_k`（预期 5）。
- **预热**（负责人）：`local-only/opday/p5-speak.sh warm`，记下打印的 round start。
- **判定**（调度员，约 5 分钟后）：`p5-probe.dh.sh reads <warm round start>`，四条读取都要满足：extraction=terms，coverage=0.5 或至少小于 1，最小 hash 相同且等于 hash(T)，集合两两不同。同时按 L6 W2 核对四位顾客的 Consumer 修订、联系约束和待审提案都没有变化。**任何一条不满足就停，不开开关**。

## 5. 开开关（负责人）

```bash
REMAINING=8 DURATION=3600 REASON='NX-038 P5 synthetic fixture' local-only/opday/fault-switch.sh open local-only/opday/fault-consumers.txt --dry-run
```

看过再去掉 `--dry-run`。预期 opened true，consumers 4，remaining 8。

## 6. 两轮（负责人），之后再读一次（调度员）

- 负责人执行 `p5-speak.sh r1`，记下 round start（**新窗口的 since**）；等兜底回复后执行 `p5-speak.sh r2`。
- 调度员：
  - `fault-switch.sh status`：remaining 0，active 0；
  - `p5-probe.dh.sh reads <r1 round start>`：8 个失败 Run 都是 extraction=terms、coverage<1，键相同，四位顾客的集合两两不同（P1）。不满足就停。

## 7. P5 判定（调度员，DATA_HOST 容器）

1. 两份配置的 `inputs.evolution`：
   - `window.since` 取 r1 的 round start，until 取 r2 之后；
   - label 用新的，例如 `s6-stage-<日期>-2`；
   - parent_revision 按 L6 段 C 第 3 条处理（没有已发布版本时用 `sem-0`）。
2. 执行 `local-only/s6-stage-20261111/run-inputs.sh`（s6_stage_inputs）：退出码 0，consumers_with_miss ≥ 4。
3. 执行 `s6_stage_evolution.py r5 --config <容器内配置> --dry-run`（U1）：units ≥ 4，pairs = 0。草案的 target_refs 应落在夹具定义上（A1）。
4. 判定交 L6。段 D–H 按 run-nx038 执行，证据里写明数据源是合成夹具。

## 8. 退役（A4 加 Q5-2；R7 之后的固定步骤）

1. **列出其他有全类型定义读权限的主体（Q5-2，调度员，`ro`）**：
   ```
   ro "select entity_key, fact->'type_names' from authz.nexloop_authority_facts where tenant_id='$TENANT_ID' and fact_kind='definition_read_rule'"
   ```
   并核对清单：在 main 910c2b6d 上只有 relay_source 一条（20 个类型）。**R2（代码出处，main 910c2b6d）**：在 Python 代码里，读 definition_read_rule 的只有服务凭据的语义读取路径：召回门控 `nexloop_eios/recall.py:116`，以及定义详情 `context_engine/authority.py:67`（由 `context_engine/semantic.py:170` 调用）。两者都调用 0381 的 `authz.nexloop_definition_read`，参数是服务凭据的 digest。工作台的代码里没有任何地方引用 definition_read_rule（`git grep definition_read_rule` 的全部结果）。所以工作台浏览器会话不经这条规则，夹具规则对它们没有影响。把查询结果记入证据；出现其他主体就逐个写明，并在第 2 步一并处理。
2. **撤掉定义读权限**：再走一个批次提交，从 relay_source 的 `type_names` 删去 `S6SyntheticFixture`。register 先 dry-run，差异只有这一项，没有新建或轮换凭据；然后正式执行，取前后快照存档。这是让运行时抽不到 F 和 T 的唯一有效步骤（Q6）。
3. **隐藏属性**：负责人经 F3 的 hide，对 6 个夹具属性各提交一次。**R3（与召回 hidden 迁移的时序，按 H-d）**：那个迁移不得在 §4 预热到 §6 r2 之间部署。如果它在退役之前已经上线，hide 本身就会让召回停止抽这些词，成为有效的退役手段：届时第 2 步和第 3 步都做，并在第 4 步核对里确认抽词已经停止；同时按那份设计稿 §5 更新 Q6 测试和本节文字。如果它还没上线，这一步只是展示上的收尾，只有第 2 步有效。
4. **只读确认（调度员）**：
   - definition_read_rule 事实里已经没有夹具类型；
   - 退役之后，任意一次合成读取的 term_hashes 不含 hash(F) 和 hash(T)。stage 上不能扮演 relay_source 直接调 recall_terms，所以以读取记录为准。
5. **候选（A1）**：target_refs 落在夹具上的候选，不得晋升到影响真实读取的语义版本；已经晋升的，经语义发布回退，随夹具一起撤回。
6. **召回索引里留下的夹具行不删**：读不到，也没有删除入口，记入证据。

## 9. 证据（执行者和判定者分开写，不含口令、DSN、用户名和主机名）

- 7 次发布的 publication_id 和 version_after；
- q5-before、q5-after 两份快照，以及 register dry-run 的差异；
- A2 的三条结果；
- 策略版本和 top_k；
- 预热和正式两次 reads 的输出；
- 开关的 open 和 status 输出；
- s6_stage_inputs 的输出、r5 dry-run 的 units 和 pairs；
- 退役的前后快照和确认结果；
- 时间一律取自 `date`。
