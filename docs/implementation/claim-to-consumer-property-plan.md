# 让真实客户属性写进本体：实施计划（Claim → Consumer 属性）

状态：计划草案，只读诊断后编写，未实现。基线 main 2bad63f9。

目标（负责人）：客户说“周五白天我在家”→ 提取 Claim → 匹配本体 → 受治理地更新 Consumer 属性（送货时间窗）→ 客户画像页可见。

## 0. 现状（stage，tenant stage，world real，调度员 11-02 只读）

- 抽取在跑：`ontology.nexloop_claims` 约 1480 条；匹配器 `nx020-matcher/1` 共 1384 条结果，最近 11-02 01:10Z。
- `ontology.nexloop_mutation_proposals` 0 行；7 个 Consumer 的 properties 全空。
- APP_HOST `config/claim-match.json` 为 `{}`（3 字节）。claim-matcher、claim-extraction-worker、claim-extraction-scheduler 三个容器在跑。
- 匹配结果按原因：

| 原因 | 数量 | 判断 |
|---|---|---|
| `kind_not_formal`（intent 487，commitment 175） | 662 | 设计如此：intent/commitment 不写属性（`claim_matching.py:52` `WRITABLE_KINDS`） |
| `modality_not_asserted`（need_problem 314，constraint 22，preference 21，user_statement 3） | 360 | 见 §4 |
| `type_not_writable`（preference 29，need_problem 28，user_statement 18，constraint 3） | 78 | **配置缺口**：`edit_actions` 为空，`_schema('Consumer')` 为 None（`claim_matching.py:201-210,302`） |
| `no_match`（user_statement 72，need_problem 32，constraint 21，preference 1） | 126 | 已暂存候选定义；object_type 候选 24 条 pending_review、8 条 superseded，无人审核 |
| `negated_requires_resolution` | 18 | 设计如此（否定需要人确认） |
| hypothesis / claim already resolved / type_undecided | 61 / 78 / 1 | 正常 |

即使配置补上，Consumer v1 只有 `display_name`、`locale`、`timezone`、`lifecycle_status`、`external_id_refs`、`contact_preferences`（`consumer_profile.py:11-15`，后两项只允许空数组），没有送货时间窗，目标链路走不通。

## 1. 阶段一：只改配置与部署（约 1 人日，含核对）

### 1.1 改动

1. APP_HOST 私有 `config/claim-match.json`：

   ```json
   {"edit_actions": {"Consumer": ["Consumer.edit", 1]}}
   ```

   形状由 `background_services.load_match_configuration`（`background_services.py:171-176`）校验，只允许 `edit_actions` / `create_actions`。仓库示例 `deploy/stage/claim-match.example.json` 同步改成这一份（这是唯一的仓库改动，走正常批次）。
2. 重启 `claim-matcher`（只此一个容器）。

### 1.2 前置核对（只读）

- `Consumer.edit:1` 已发布、`claim_matcher` 已授权：

  ```sql
  select * from control.nexloop_action_definitions where tenant_id='stage' and stable_name='Consumer.edit';
  ```

  （列以 0008 及后续迁移为准；也可用 `stage_secrets.py publish --dry-run` 与 `register --dry-run` 期望“无变化”代替。）授权来源：`deploy/authorization/service-grants.v1.json:209-219,323,354`。
- 服务凭据有效：claim-matcher 日志最近一轮没有 `PermissionError` / `unavailable`（`grep -c`）。

### 1.3 78 条 type_not_writable 的重新匹配

现状：同一 Claim、同一 `matcher_version` 的结果会重放（`claim_matching.py:250-251`），改配置后它们**不会**自动重匹配。现有释放机制 `ontology.nexloop_claim_rematch`（0101/0104）只在审核决定回流时、按候选的 `cause` 释放，不覆盖“配置变化”。

三种触发方式（推荐 a）：

- a. **新增运维工具 `claim_rematch.py`（小产品改动，约 1 人日）**：以 claim_matcher 的服务身份调用一个新的 definer 函数，参数为 `reason=type_not_writable` 与 `matcher_version`。函数只释放 `resolution_state='needs_resolution'` 且最新结果为该原因的 Claim：置 `unresolved`、`nexloop_claim_rematch` 的 generation+1，`cause='config:edit_actions'`，再 `nexloop_work_feed_touch('claim-match', conversation)`。可重复执行；dry-run 只报数量。
- b. 把 `MATCHER_VERSION` 升为 `nx020-matcher/2`：所有 Claim 都会重匹配，调模型约 900 次，范围太大，并且会改变已 resolved 的语义。不推荐。
- c. 直接改表：违反“只经受治理路径”的约束，不允许。

结论：阶段一的“配置”半天可完成；78 条重匹配需要 a 这一小段产品改动，可与阶段二同批。

### 1.4 验收（只读 SQL）

```sql
-- 重匹配后不再出现新的 type_not_writable
select split_part(reason,':',1), outcome, count(*) from ontology.nexloop_claim_matches
 where tenant_id='stage' and world='real' and recorded_at > '<重启时间>' group by 1,2 order by 3 desc;
-- 这 78 条现在的去向（多数应变成 no_match→property 候选，因为 v1 没有合适属性）
select m.outcome, split_part(m.reason,':',1), count(*) from ontology.nexloop_claim_matches m
 join ontology.nexloop_claim_rematch r using(tenant_id,world,claim_id)
 where m.tenant_id='stage' and m.world='real' and r.cause='config:edit_actions' and m.matcher_version like '%rematch%' group by 1,2;
```

预期：阶段一本身**不会**让 Consumer 属性出现值（v1 没有可写的业务属性）。它把 78 条从“不可写”推进到“属性候选”或 `noop`，为阶段二提供证据。

## 2. 阶段二：Consumer schema（约 2–3 人日）

### 2.1 属性草案

| 属性 | 类型 | 取值 | 来源示例 |
|---|---|---|---|
| `delivery_windows` | json，结构化数组 | `[{"weekday":"fri","part":"daytime"}]`；`weekday` ∈ mon…sun / any，`part` ∈ morning/daytime/afternoon/evening/any | “周五白天我在家” |
| `preferred_contact_time` | string，受控词表 | `weekday_daytime` / `weekday_evening` / `weekend` / `any` | “工作日晚上联系我” |
| `delivery_notes` | string，≤200 字 | 自由文本，只作备注 | “放门卫” |

推荐只先做 `delivery_windows`。值的形态见 §6 决策 2。

### 2.2 发布路径（两条，推荐 A）

- A. **NX-044 schema 发布**：Consumer v2 = v1 + 新属性，作为兼容扩展（只增属性，NX-040 兼容规则）；`Consumer.edit` 绑定到 v2（`business-actions.v1.json` 的 `object_type.version`）。同时改 `consumer_profile.py` 的创建 schema（新属性可选），`claim-match.json` 不变。
- B. **候选定义回流**：审核员在知识工作台批准一个 `property` 候选（kind=property，Consumer 下），按 0082 的属性回流把依赖 Claim 重新指向新属性。依赖现有 24 条候选里恰好有这个属性；而它们都是 object_type 候选，不一定合适。适合长尾属性，不适合我们明确想要的核心属性。

### 2.3 依赖

阶段一完成；NX-044 发布通道在 stage 可用（`Consumer.edit` 已发布）；workbench_roles、register 计数如有新 Action 需同步（本方案只改版本，不新增 Action）。

### 2.4 验收

```sql
select count(*) from ontology.nexloop_mutation_proposals where tenant_id='stage' and world='real' and type_name='Consumer' and property_name='delivery_windows';
select status, count(*) from ontology.nexloop_mutation_proposals where tenant_id='stage' and world='real' group by 1;
select object_id, properties ? 'delivery_windows' from ontology.objects where tenant_id='stage' and type_name='Consumer';
```

预期：一条合成测试客户的“周五白天我在家”产生 1 条 proposal，状态 `applied`，对应 Consumer 出现 `delivery_windows`；Claim 变为 `resolved`。

## 3. 阶段三：规范化与画像页（约 2 人日）

- **规范化**（约 1 人日）：匹配器的值层（`_judge` Layer 4，`claim_matching.py:331-349`）目前要求 `chosen == _claim_value(claim)`（`value_not_from_evidence`）。结构化时间窗需要一个确定性的规范化函数：输入 Claim 的 `value` / `time_expression`，输出 `{weekday, part}`。规则表放在代码里并做单测，模型只能从规范化结果中选择，不能自造。多个窗口按集合合并；新证据覆盖旧证据的规则沿用 docs/04 §7.3（`latest_applied`）。
- **画像页**（约 1 人日）：工作台客户详情增加“送货时间”一栏，显示值、来源 Claim 引用、生效时间；为空时显示“暂无（未从对话中获得）”，不显示空白。
- **验收**：浏览器走查合成测试客户；只读 SQL 同 §2.4，另查 proposal 的 `receipt` 非空。

## 4. modality_not_asserted：守卫还是提取器？

守卫（`claim_matching.py:267`）只接受 `modality='asserted'`。提取器的 modality 取值为 asserted / conditional / tentative / requested（`conversation_extraction.py:23`），并会强制改写：句中有条件词改为 conditional（`:625-626`），有“可能/想/考虑”一类词改为 tentative（`:629`）。

初判：need_problem 的高频 predicate 是“询问最近的课程安排”“询问能否指定上门时间”，这是客户**提问**，被标为 `requested` 是对的；把提问写成 Consumer 属性才是错的。所以这 314 条的大部分是**守卫正确**，问题在于提取器把提问也抽成了 need_problem（它更接近 intent/question）。少数可能是 TENTATIVE 正则误伤（例如“我想要周五送”是偏好，被强制改为 tentative）。

请跑以下只读 SQL，取 2–3 条真实样本再定：

```sql
-- 分布：kind × modality × 是否被强制改写
select epistemic_kind, modality, (guard_flags ? 'modality_forced_tentative') forced_tentative, (guard_flags ? 'modality_forced_conditional') forced_conditional, count(*)
 from ontology.nexloop_claims where tenant_id='stage' and world='real' and epistemic_kind in ('need_problem','preference','constraint') and modality<>'asserted'
 group by 1,2,3,4 order by 5 desc;
-- 样本（引用原文 ≤60 字；只给调度员看，不入仓库）
select left(claim_id,12), epistemic_kind, modality, guard_flags, predicate, left(quote,60) from ontology.nexloop_claims
 where tenant_id='stage' and world='real' and epistemic_kind='need_problem' and modality<>'asserted' order by recorded_at desc limit 6;
```

（列名取自 0066：`epistemic_kind`、`guard_flags`、`quote`。）

判定规则：`requested` 且 predicate 以“询问/咨询/能否”开头，算守卫正确，提取器归类偏差；`tentative` 并且命中强制改写，原文又是明确偏好，算提取器误伤。后者应改 TENTATIVE 规则（小改动，约 0.5 人日，带回归样本）；前者可以把提问改为 intent，属于提取器提示词和版本改动，不进本计划的关键路径。

## 5. 24 条 pending_review 候选：入口与审核人

- 入口：工作台“知识工作台”页（`apps/web/src/workbench/pages/Knowledge.tsx`），列表为“待审核候选”。“待处理”页也有“去审核 / 审核最早一项”的直达链接（`pages/Pending.tsx:25`）。
- 权限：审核动作是 `ontology.schema.review:1`，属于 `reviewer` 角色（`deploy/authorization/workbench-roles.v1.json:121`）。看证据原文也只限审核人（`REVIEWER_ONLY_TEXT`）。负责人要审核，需要在“成员”页给自己或指定的人加上“审核人”角色。
- 负责人点击路径：工作台 → 成员 → 给审核人账号加“审核人”角色 → 用该账号登录 → 知识工作台 → 待审核候选 → 点候选 → 审核证据（看引用原文）→ 批准 / 拒绝 / 合并 → 回到列表看回流状态（“数据健康”页的 `reflow_stale` 告警会提示超过 1 小时未回流的决定）。
- 建议：24 条都是 object_type 候选，大多是对话里提到的新事物，例如课程、服务项。先抽 5 条看质量；与 Consumer 属性无关的直接拒绝，拒绝会进入冷却（0073），不会反复排队。

## 6. 需要负责人拍板

1. **对话推出的属性：免审批自动写入，还是先进审核？**
   推荐：**低风险属性**（送货时间窗、联系时间）继续用 `Consumer.edit` 免审批自动写入，但必须满足 asserted、affirmed、有原文证据、值来自规范化表；画像页显示来源，可撤回（correction 链）。**敏感属性**（身份、支付、地址）一律先进审核。理由：演示目标要求“说完就能看到”；现有守卫已经挡住非陈述句、否定句和无证据的情况，被挡住的这些都会留在 needs_resolution 给人看。
2. **送货时间窗的取值形态。**
   推荐：结构化数组 `[{weekday, part}]`，part 取 5 档，不存具体钟点。理由：可比较、可合并，计划调度能直接用；“白天”这类模糊说法不必硬换算成钟点；以后要精确时可以加可选的 `from` / `to`，属于兼容扩展。
3. **历史 Claim 要不要回放？**
   推荐：**只回放 preference / constraint / user_statement 中 asserted 且 affirmed 的部分**（约 50–80 条），用 §1.3a 的工具按原因释放，先 dry-run 报数量；intent、commitment、提问类不回放。理由：成本小（每条一次模型调用），能让 7 个真实 Consumer 立刻出现属性；全量回放调用多、收益低，还会让 resolved 的语义漂移。

## 7. 工作量汇总

| 阶段 | 内容 | 工作量 | 依赖 |
|---|---|---|---|
| 一 | claim-match.json、重启、核对 | 0.5 人日 | 无 |
| 一 + | `claim_rematch.py` 与 definer 函数（新迁移） | 1 人日 | 阶段一 |
| 二 | Consumer v2（delivery_windows）发布、创建 schema | 2–3 人日 | 阶段一，决策 2 |
| 三 | 规范化与画像页 | 2 人日 | 阶段二 |
| 可选 | TENTATIVE 误伤修正 | 0.5 人日 | §4 SQL 结果 |

合计约 6–7 人日，外加一次 stage 部署（新迁移、发布与注册的 dry-run 期望变化、重启 claim-matcher 与 api）。
