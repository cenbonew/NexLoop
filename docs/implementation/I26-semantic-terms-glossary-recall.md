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
