# I26：语义读取按术语检索、词表进入召回（S6 后续观察项）

起因：S6 第三次运行 fdd261cf 的 relay semantic access 全部 0 命中（L6 I26）。三个原因叠加：
1. 整条用户语句作为一个 term 去 browse；
2. 世界模型词表（0374）不参与召回，中文问句对不上纯英文标识；
3. 定义索引落后（schema 发布不触发重建）。

## 改动（迁移 I26-a / I26-b，合批时排在相邻两个编号）

- **I26-a（抽词、重建、未命中原因）**
  - `control.nexloop_recall_terms`：在调用方可读范围内，从语句里抽出定义的名称、显示名、别名、枚举值。
  - relay 的 `_semantic_read` 改为用这些术语检索；访问记录每个术语记一个 hash。
  - `control.nexloop_recall_stale_definitions`：recall-indexer 每个 tick 先把过期类型重新索引。
  - `miss_reason`：取值 `no_readable_types` / `denied` / `no_match`。
- **I26-b（方案 A，负责人 2026-10-30）**
  - 词表译名（manual 和 machine 都包括）作为 real 世界的 alias 写入召回索引，`gloss_source` 区分来源。
  - 映射范围：
    - object_type 映射到类型 ref；
    - property 映射到所有拥有该属性的类型；
    - section、relation、event_type、function、action 没有召回 ref，跳过。
  - 同步：每次词表写入和每次新增类型版本，都由触发器对整个租户重写 gloss 行。
  - 访问记录 `gloss_hits` 统计 `{manual, machine}`。
  - manifest 发布和负责人更正各开一个 7 天观察窗（0250）。
  - S55：stale 判定只读一个 tokenizer 常量，并拒绝不一致的调用方；重建后仍判为过期的类型会打 warning 并计数。

- **I26-c（第三轮，L2 审查 side-session-notes/I26-review-l2.md）**
  - B2：每个术语单独做词法检索，再按 ref 取最高分合并；ambiguity 只在同一术语的候选之间比较。原先把术语拼接成一个查询，每个定义只能拿到“自己那一个词 / 全部词”的分数，还会误报 ambiguity。最多检索 8 个术语。配置了 vector 时，向量仍对整句计算，作为一组额外候选。
  - property 译名扇出：一个 property 术语如果命中多个类型的同名属性，而别的术语已经点名了其中某个类型，就只保留被点名的类型。property 译名不足 3 个字的（例如“状态”“名称”）不作为术语抽取。
  - tokenizer：`nexloop_recall_insert`、`nexloop_recall_scope` 改为读 `nexloop_recall_tokenizer()`，0067 原文不改，在新迁移里用 create or replace 重定义。
  - B1：gloss 只走词法，由 `test_gloss_rows_stay_lexical_only` 固定下来。
  - 后续（未做）：词表 reflow 目前是整租户删除后重建，复杂度为词条数×类型数，在写事务内完成；L6 判 stage 规模可以接受。租户变大时，改为按变化的 identifier 增量处理，或交给 recall-indexer 的 tick。另外，gloss 行只写 world=real，shadow 和 simulation 的评估对比需要考虑这一点。

## 分数阈值与检索次数（L6，第三轮之后）

- **含义变化**：`min_score` 和 `candidate_limit` 的含义都变了。
  - 分数现在按单个术语计算：一个定义命中自己那个词，就接近满分。因此同一个 `min_score` 实际上比整句检索时宽松。stage 当前配置为 0.0，不受影响；以后调高时，要按单个术语的分数来定阈值。
  - `candidate_limit` 现在是“每个术语、每种方法”各一个上限；`top_k` 在合并之后才截断。
- **检索次数**：一次 access 最多 8 个术语（`MAX_TERMS`）。每个术语跑 fts 和 trgm 两种词法方法，最多 16 次检索。另外还有 1 次抽词查询、1 次可读范围查询；配置了 vector 时，再加 1 次整句检索。
  - L6 估的是 8 × 3 = 24 次，按的是三种方法；vector 不参与逐词检索，所以实际上限是 16 次。

## S57 上线观察：语义读取耗时

- relay 的语义读取池只有 1 条连接，上面这些查询是顺序执行的，会拉长 access 耗时。
- 现有超时 `SEMANTIC_READ_TIMEOUT_SECONDS` 为 0.5 秒；超时只计数，不会让 Run 失败。
- 上线后要对照两项：relay 的 semantic `timeout` 计数，以及 access 的耗时。
- 如果出现 timeout，有两个处理办法：
  - 调小 `MAX_TERMS`；
  - 把多个术语合进同一条 SQL，一次返回各个术语各自的候选。

## Embedding（I27）

- gloss 行不会被 embedding：
  - `nexloop_recall_insert` 不附带向量；
  - 0219 的 backfill 只挑 `source_key='type:*'` 的定义和实例；
  - `index_object_type` 只重写 `type:*` 行。
- 因此整租户重写不会触发模型调用，换 entry_id 也没有成本。
- 代价是 vector 方法看不到 gloss 行，只有 fts 和 trgm 能命中。relay 的语义读取本来就只用全文检索（不调用 embedding），所以不受影响。
- 如果以后要让 gloss 行参与 vector 检索，需要先把同步改成差量（保留未变化行的 entry_id），再接入 backfill。

## 世界隔离证据

- gloss 行写在 world=real。`recall_scope` 对 definition 的过滤条件是 `world is null or world = 调用世界`。
- 对应测试：`tests/test_semantic_terms_pg.py::test_a_correction_clearing_or_restore_takes_the_alias_away_and_simulation_never_sees_it`。
  - 在 simulation 世界，有 Consumer READ 的会话召回“客户”，结果不含 Consumer；real 世界能命中。
  - `…::test_gloss_rows_are_invisible_outside_world_real` 逐个世界（simulation、test）断言看不到 gloss 行。

## S6 后续观察项

1. **上线核对**
   - 定义索引 20/20，`stale_definitions` 为空；
   - recall-indexer 日志里没有 `still stale after reindex`；
   - gloss 行的 embedding 数为 0。
2. **补跑**（新 TRIAL_ID，窗口起点在上线之后）
   - 已知概念的中文变体（contact_preference、unsubscribe、refuse_contact，以及直接说“客户”的问句）：命中数大于 0，term_hashes 多于 1，`miss_reason` 为 null，`gloss_hits` 不为空。
   - 英文对照：同样能命中，`gloss_hits` 为空。
3. **S56（机翻改变召回但不开观察窗）**
   - 机翻写入是持续的，不开 release watch，它对召回的影响要另外从 `gloss_hits.machine` 去观察：
     - 按日统计 `gloss_hits.machine > 0` 的访问占比；
     - 这些访问的 ambiguity 比例；
     - 这些 Run 的回复 fallback 比例。
   - 和只经 manual 命中的访问对比。机翻命中的歧义率或失败率明显偏高时，先由负责人把有问题的译名更正成 manual；如果需要，再把 2 个字的 machine 译名排除出抽词。
4. **2 个字的中文术语**（S55-2 保留 2 个字的下限）
   - 观察 ambiguity 和 7 天观察窗的歧义率回归告警（`evolution_release_regressing`）。
