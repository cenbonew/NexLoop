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

## 执行包（R77 复核，2026-11-11）

**对照 stage 现状的复核**：
- 部署：R77 = main 4deeb61c。0449 和 0450 都在线，所以：NX-068 门禁会判夹具提案；新类型没有 Action，successor 为空，预期 pass。§8 第 3 步的 hide 是有效的退役手段（R3 条件已满足）。
- 开关：仍是 `REMAINING=8`，即 4 人 × 2 轮。名单 `fault-consumers.txt` 4 行。`DURATION=3600` 是脚本上限；如果 r1 到 r2 预计超过 50 分钟，就不要开。
- 窗口和 label：since 取 r1 的 round start，不含预热。label 按 §7 第 1 条。parent_revision 仍按 L6 段 C 第 3 条（无已发布语义版本时用 `sem-0`；如果 11-11 之后已有发布，就用最新版本，以 `ro` 查到的为准）。
- 告警规则 v38、authority_revision 6441（4 条 follow）：§2 的 register 会让 authority_revision 增加；这是预期内的，§4 起必须不再变化（A3）。在 §4 预热前记一次，§6 后再记一次，两次必须相等。
- readiness：04239b2a 的 pass 只说明 readiness 可用，P5 不用它发布（V1）。

**逐段执行者与核对**（“前”“后”都是调度员的只读核对；任何一项不符合就停在那一段）：

| 段 | 执行者 | 执行前核对 | 执行后核对 |
|---|---|---|---|
| §0 前提 | 调度员 | — | P0.1–P0.8 全部符合 |
| §1.1 夹具类型 | 负责人终端（`fixture-type --publish`，approver3 口令从 Keychain 经 stdin） | P0.4、P0.8 为 0；预算（R4） | V2 为 0/0；published、version_after=1、successor_actions=[]；max(version)=1 |
| §1.2–1.7 六个属性 | 调度员生成模板（DATA_HOST 容器），负责人终端执行 r8 `--publish` | R4 预算；上一步 max(version)=n | V2 为 n/n；version_after=n+1；全部完成后执行 §1 的三条核对（7、0、0） |
| §2 定义读规则 | L3 交清单提交，经批次合入；调度员执行 register | `q5-before` 快照；dry-run 差异只有 1 项 | `q5-after` diff 只有这 1 项；R1 索引行为 v7 |
| §3 A2 | 调度员 | §4 的选词已定 | 三条都为 0 |
| §4 选词与预热 | 调度员 `p5-probe.dh.sh check`；负责人终端 `p5-speak.sh warm` | 开关关闭（P0.2）；记下 authority_revision、策略版本和 top_k；W2 before | reads：4 条都是 terms，coverage<1，key=hash(T)，集合两两不同；W2 after 无变化 |
| §5 开开关 | 负责人终端 | §4 判定通过 | opened、consumers 4、remaining 8 |
| §6 两轮 | 负责人终端 r1、r2；调度员核对 | 开关为 open | status：remaining 0、active 0；8 条 reads 满足 P1；authority_revision、策略版本和 top_k 与 §4 相同 |
| §7 判定 | 调度员（DATA_HOST 容器） | 两份配置的 window、label 和 parent 已同步，记下 sha256 | inputs 退出码 0，consumers_with_miss ≥ 4；r5 dry-run units ≥ 4、pairs = 0 → 交 L6 |
| §8 退役 | 负责人终端（hide）；L3 交清单撤回提交，调度员执行 register | Q5-2 主体清单 | 规则事实中不再有夹具；退役后合成读取不含 hash(F) 和 hash(T) |

## 0. 前提

| # | 前提 | 核对 |
|---|---|---|
| P0.1 | stage 的发布是 R77 = main 4deeb61c（批次 166d，release `20261111-4deeb61c80e5-1`），迁移目录到 0450：含 NX-068 发布门禁 0449、召回 hidden 0450、G1 的 `fixture-type` | 部署单；`ro "select max(version) from control.schema_migrations"`，预期 0450 |
| P0.2 | ADR-045 开关已关 | `fault-switch.sh status` 打印的 SQL：active 为 0 |
| P0.3 | evolution 预算在期内，并且够 7 次 r8 各 0.01 CNY 的 reflow | run-nx038 §0 P3 的查询 |
| P0.4 | `S6SyntheticFixture` 不存在 | `ro "select count(*) from ontology.object_type_versions where tenant_id='$TENANT_ID' and type_name='S6SyntheticFixture'"`：0 |
| P0.5 | stage 的发布包含 G1 的 `s6_stage_schema.py fixture-type` | 管理机上执行 `s6_stage_schema.py fixture-type --config "$S6" --dry-run`，退出码 0 |
| P0.6 | 部署冻结：§4 预热到 §6 r2 之间不部署（调度员已确认；批次 167 只改测试和文档，不部署） | 调度员记录冻结开始和结束的 `date` |
| P0.7 | 不复用测试提案 04239b2a（L6 V1）：P5 只发布 §1 自己冻结的夹具提案 | §1 每步的 proposal_id 都是本次新冻结的，类型都是 `S6SyntheticFixture` |
| P0.8 | 没有遗留的夹具提案：`ro "select count(*) from ontology.nexloop_schema_proposals where tenant_id='$TENANT_ID' and type_name='S6SyntheticFixture'"` | 0 |
| P0.8a | 对照（L6 P2，防止 `$TENANT_ID` 为空或写错导致 count=0 被误判成“没有遗留”）：`ro "select count(*) from ontology.nexloop_schema_proposals where tenant_id='$TENANT_ID' and type_name='Consumer'"`；证据里记 `printf %s "$TENANT_ID" \| wc -c`（只记长度，不记值） | ≥ 2（11-11 读到 2 个 frozen）；小于 2 就说明租户变量不对，P0.4、P0.8 和 V2 的结果都不能采信，停下 |

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

**V2（stale-parent，每次发布之前，调度员，`ro`）**：0254 只发布“冻结时的父版本仍是最新版本”的提案，父版本移动了就拒绝。为了让失败发生在发布之前，每次 `--dry-run` 冻结出提案之后、去掉 `--dry-run` 之前，都核对一次：
```
ro "select p.parent_schema_version, (select coalesce(max(version),0) from ontology.object_type_versions v where v.tenant_id=p.tenant_id and v.type_name=p.type_name) from ontology.nexloop_schema_proposals p where p.tenant_id='$TENANT_ID' and p.proposal_id='<本步 proposal_id>'"
```
两列必须相等：1.1 是 0 和 0，1.2–1.7 依次是 1…6。不相等就停，不要发布。

**发布后（L6 P1，每一步都做，调度员）**：对本步的 proposal_id 再跑一次上面的查询，记录 `parent_schema_version`。它必须等于发布前的最新版本 n；第二列此时应为 n+1。证据写 proposal_id、parent_schema_version=n、发布后 max(version)=n+1，不能只写“已发布”。如果 dry-run 不写提案、发布前查不到，证据就写三项：`published=true`、`version_after=n+1`、发布后 `max(version)=n+1`，再加上发布后查到的 parent_schema_version=n。V2 查询同样依赖 `$TENANT_ID`，先过 P0.8a。注意：Consumer 的 04239b2a 的父版本是 v1，当前已是 v2，属于 stale，不得发布（V1、V2），与本单无关，只是不要误用。

**R4（每次 r8 之前，调度员）**：先用 run-nx038 §0 P3 的查询确认 evolution 预算在期内并且还有余量，不足就不开始这一次。7 次发布中**任何一次失败都停下，不要从中间续跑**：把已发布的版本号和失败那一步的结果文件报给 L6，由 L6 决定怎么接续。半成品的夹具（少几个属性）只要没有 definition_read_rule 就不会被运行时读到，不必回滚。

## 2. 定义读规则（Q5-1）

1. **改清单**：这是一个单独的提交，走批次。在 `deploy/authorization/stage-principals.v1.json` 的 `definition_read_rules`，**只在 relay_source 那一条**的 `type_names` 里加 `S6SyntheticFixture`，并在 `purpose` 里写明“NX-038 P5 合成夹具，A4 退役时移除”。不加别的类型，不加别的主体。规则是按类型给的：类型下全部非受限属性的定义都可读，标记属性 `fixture_marker` 也包括在内（M2）。它没有显示名，中文句子抽不到它，可读也不会影响抽词。
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

预期：只有 source_revision 7 一个版本（1.1 建出 v1，1.2–1.7 各加一版，所以是 7）；display_name 行共 6 条（5 条 F、1 条 T），另有 name 行，其中包括没有显示名的标记属性 `fixture_marker`。不是 7，或者行数不对，就停下，等下一个 reflow tick 再查；仍然不对就报 L6。

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
   - label 用新的：执行当天如果是 2026-11-11，就用 `s6-stage-20261111-2`，因为 `-1` 已被 11-11 第一次的窗口（since 2026-11-10T22:06Z）占用；其他日期用 `s6-stage-<执行日期>-1`。执行前在两份配置里 grep 一遍，确认 label 没用过；
   - parent_revision 按 L6 段 C 第 3 条处理（没有已发布版本时用 `sem-0`）。
2. 执行 `local-only/s6-stage-20261111/run-inputs.sh`（s6_stage_inputs）：退出码 0，consumers_with_miss ≥ 4。
3. 执行 `s6_stage_evolution.py r5 --config <容器内配置> --dry-run`（U1）：units ≥ 4，pairs = 0。草案的 target_refs 应落在夹具定义上（A1）。
4. 判定交 L6。段 D–H 按 run-nx038 执行，证据里写明数据源是合成夹具。

## 8. 退役（A4 加 Q5-2；R7 之后的固定步骤）

1. **列出其他有全类型定义读权限的主体（Q5-2，调度员，`ro`）**：
   ```
   ro "select entity_key, fact->'type_names' from authz.nexloop_authority_facts where tenant_id='$TENANT_ID' and fact_kind='definition_read_rule'"
   ```
   并核对清单：在 main 910c2b6d 上只有 relay_source 一条（20 个类型），4deeb61c 上未变；§2 之后应为 21 个，其中含夹具。**R2（代码出处，main 910c2b6d）**：在 Python 代码里，读 definition_read_rule 的只有服务凭据的语义读取路径：召回门控 `nexloop_eios/recall.py:116`，以及定义详情 `context_engine/authority.py:67`（由 `context_engine/semantic.py:170` 调用）。两者都调用 0381 的 `authz.nexloop_definition_read`，参数是服务凭据的 digest。工作台的代码里没有任何地方引用 definition_read_rule（`git grep definition_read_rule` 的全部结果）。所以工作台浏览器会话不经这条规则，夹具规则对它们没有影响。把查询结果记入证据；出现其他主体就逐个写明，并在第 2 步一并处理。
2. **撤掉定义读权限**：再走一个批次提交，从 relay_source 的 `type_names` 删去 `S6SyntheticFixture`（按类型撤，标记属性一并撤掉）。register 先 dry-run，差异只有这一项，没有新建或轮换凭据；然后正式执行，取前后快照存档。这是让运行时抽不到 F 和 T 的有效步骤之一（Q6）。
3. **隐藏属性**：负责人经 F3 的 hide，对 6 个夹具属性各提交一次。召回 hidden 迁移（0450）已在批次 166 合入：hidden 属性不再进召回，所以 hide 本身就让召回停止抽 F 和 T，是有效的退役手段（Q6 测试断言：hide 之后即使仍有定义读权限，recall_terms 也为空）。第 2 步和第 3 步都做，任一步都足以停止抽词，两步一起做是双保险；第 4 步确认抽词已经停止。**R3**：0450 不得在 §4 预热到 §6 r2 之间才部署到 stage；执行前核对 stage 的迁移目录已含 0450，如果没有，这一步只是展示收尾，只有第 2 步有效。
4. **只读确认（调度员）**：
   - definition_read_rule 事实里已经没有夹具类型；
   - 退役之后，任意一次合成读取的 term_hashes 不含 hash(F) 和 hash(T)。stage 上不能扮演 relay_source 直接调 recall_terms，所以以读取记录为准。
5. **候选（A1）**：target_refs 落在夹具上的候选，不得晋升到影响真实读取的语义版本；已经晋升的，经语义发布回退，随夹具一起撤回。
6. **召回索引里留下的夹具行不删**：读不到，也没有删除入口，记入证据。

## 9. 证据（执行者和判定者分开写，不含口令、DSN、用户名和主机名）

- 7 次发布的 publication_id、proposal_id、发布前后的 V2 结果（parent_schema_version=n，发布后 max(version)=n+1）和 version_after（L6 P1）；
- `$TENANT_ID` 的长度（不记值）和 P0.8a 的 Consumer 提案数（L6 P2）；
- q5-before、q5-after 两份快照，以及 register dry-run 的差异；
- A2 的三条结果；
- 策略版本和 top_k；
- 预热和正式两次 reads 的输出；
- 开关的 open 和 status 输出；
- s6_stage_inputs 的输出、r5 dry-run 的 units 和 pairs；
- 退役的前后快照和确认结果；
- 时间一律取自 `date`。
