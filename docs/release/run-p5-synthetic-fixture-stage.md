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
| P0.5 | **工具缺口 G1 已补上** | 见下 |

**G1（工具缺口，本单开始前必须先补）**：
- `s6_stage_schema.py` 只有 `r8 --publish` 能发布，而且只支持 `add_optional_property`；`at024` 里的 `add_object_type` 只演示被拒绝。
- 第 1 次发布（建类型）在 910c2b6d 上**没有 stage 工具入口**。F3 发布 `add_object_type` 同样要求 nx040-gates-1 的报告和 release permit。
- 需要另做一个小改动（建议加 `s6_stage_schema.py fixture-type`）：沿用 r8 的 G9–G13 链路，冻结 `add_object_type` 提案，改动内容固定为 `{"properties": []}`，并且只接受 `inputs.fixture.type_name`。改动另行送审。
- 补上之前，§1 第 1 步无法执行，本单整体为 blocked。

## 1. 发布夹具：7 次，都经提案流程，由负责人在 F3 发布

在 `$S6`（管理机）和 DATA_HOST 两份配置的 `inputs` 里加上下面这些，两份同步，各记一次 sha256：
- `fixture.type_name = "S6SyntheticFixture"`；
- `owner_directed_properties` 里加 6 条，每条形如 `{type_name: "S6SyntheticFixture", property_name, decision: "owner 2026-11-11 P5 路线 A"}`，属性是 `fx_a…fx_e` 和 `zz_t`。

| 步 | 执行者 | 命令（先 `--dry-run`，看过再去掉） | 预期 |
|---|---|---|---|
| 1.1 | 负责人 | `security find-generic-password -s nexloop/stage/approver3 -w \| uv run --frozen python scripts/acceptance/s6_stage_schema.py fixture-type --config "$S6" --publish`（G1 的入口） | published，version_after=1，successor_actions 为 [] |
| 1.2–1.7 | 调度员先在 DATA_HOST 容器里生成模板，负责人再执行 r8 | 每个属性两步。① 调度员：`s6_stage_evolution.py candidate-template --mode schema_only --property S6SyntheticFixture.<p>`。② 负责人：`security … -w \| … s6_stage_schema.py r8 --config "$S6" --publish`，配置里 `schema_type=S6SyntheticFixture`、`new_property=<p>`、`property_change={property_name:<p>, value_type:"string", display_name:<F 或 T>, description:"synthetic fixture (NX-038 P5)"}` | 每次都是 accepted、permit、published；version_after 依次为 2…7，successor_actions 都为 [] |

**核对（调度员，`ro`）**：
```
ro "select max(version) from ontology.object_type_versions where tenant_id='$TENANT_ID' and type_name='S6SyntheticFixture'"          -- 7
ro "select count(*) from ontology.objects where tenant_id='$TENANT_ID' and type_name='S6SyntheticFixture'"                             -- 0
ro "select count(*) from control.nexloop_action_definitions where tenant_id='$TENANT_ID' and definition::text like '%S6SyntheticFixture%'"  -- 0（A2）
```

## 2. 定义读规则（Q5-1）

1. **改清单**：这是一个单独的提交，走批次。在 `deploy/authorization/stage-principals.v1.json` 的 `definition_read_rules`，**只在 relay_source 那一条**的 `type_names` 里加 `S6SyntheticFixture`，并在 `purpose` 里写明“NX-038 P5 合成夹具，A4 退役时移除”。不加别的类型，不加别的主体。
2. **保存前快照（调度员，`ro`）**：
   ```
   ro "select entity_key, fact from authz.nexloop_authority_facts where tenant_id='$TENANT_ID' and fact_kind='definition_read_rule'"
   ```
   输出存档为 `q5-before`。
3. **register dry-run**（DATA_HOST，调度员，命令形态同 run-34 §1.5）：差异**只有**这一条规则多了 `S6SyntheticFixture`；没有新建凭据，没有轮换，没有其他差异。多出任何一项都停下。
4. **register 正式执行**，然后再取一次快照，存档为 `q5-after`。两份快照的 diff 应当只有 type_names 多了一项。

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
   并核对清单：在 main 910c2b6d 上只有 relay_source 一条（20 个类型）。工作台浏览器会话读定义走的是成员授权，不是 definition_read_rule，也不做 relay 的语义读取；本单没有核对它们的范围，只记录 definition_read_rule 这一类。把查询结果记入证据；出现其他主体就逐个写明，并在第 2 步一并处理。
2. **撤掉定义读权限**：再走一个批次提交，从 relay_source 的 `type_names` 删去 `S6SyntheticFixture`。register 先 dry-run，差异只有这一项，没有新建或轮换凭据；然后正式执行，取前后快照存档。这是让运行时抽不到 F 和 T 的唯一有效步骤（Q6）。
3. **隐藏属性（可选）**：负责人经 F3 的 hide，对 6 个夹具属性各提交一次，只是展示上的收尾。
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
