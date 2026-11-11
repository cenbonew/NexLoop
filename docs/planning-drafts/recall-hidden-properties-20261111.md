# 召回索引对 hidden 属性停止抽词：核实与设计（2026-11-11，草稿，送 L6）

- **来源**：L3 在 P5 夹具验证中发现的问题（933f8f82，`p5-fixture-q1q6-20261111.md` 的 Q6）。负责人 11-11 按 L6 的推荐把它另立为一项任务，倾向于让召回索引对 hidden 属性也停止抽词。
- **基线**：main 910c2b6d。只在本地测试库运行，stage 上没有做任何操作。
- **本稿交付**：
  - 复现测试 `tests/test_recall_hidden_property_pg.py`（2 项，记录**当前**行为，全部通过）；
  - 本设计稿。

  迁移本身不在本稿，等 L6 裁定之后再写。

## 1. 现象核实

**复现**：v1 有 `budget_level`（带 3 个枚举值）和 `waterproof_concern`。发布 v2，把这两个属性标为 `visibility: "hidden"`（与 0254 F3 hide 写入的形状相同），然后按 v2 重新建索引。复现测试断言了以下情况，每一条都是**当前**行为：

| 面 | 现象 | 断言 |
|---|---|---|
| 定义行 | `nexloop_recall_definition_rows(t,'Consumer',2)` 仍返回 hidden 属性的 name、display_name、description 和枚举值 | `('…/waterproof_concern','display_name','关注防水') in rows` |
| 索引 | 按 v2 重建后，`type:Consumer` 的行里，这两个属性还有至少 7 条 | `indexed>=7` |
| recall_terms | 句子“我很关注防水，预算两千元以内”抽出 `关注防水` 和 `两千元以内` | `result.terms` |
| 检索 | `recall_search` 返回 `eios:property:Consumer/waterproof_concern` | `refs(result.definitions)` |
| 已有别名 | hide 之前写入的别名“防泼水”仍然有效，并且能召回这个属性 | `'alias' in fields` |
| 新别名 | `index_alias` 仍接受指向 hidden 属性的新别名 | `==1` |

命令：

```
LC_ALL=C.UTF-8 PYTHONPATH=packages/eios-core/src .venv/bin/python -m pytest tests/test_recall_hidden_property_pg.py -q -n 2   # 2 passed
```

**涉及的迁移和函数**（main 910c2b6d）：

- **根源**：`ontology.nexloop_recall_definition_rows(tenant,type,version)`，在 **0067** 中定义，之后没有任何迁移重定义过。它遍历 `definition->'properties'` 时不看 `visibility`。
- **以它为唯一来源的地方**（改它一处，下面全部跟着变）：

| 调用方 | 迁移 | 用途 |
|---|---|---|
| `control.nexloop_recall_definition_texts` | 0067 | 给 Python 索引器算向量的文本 |
| `control.nexloop_recall_index_definition` | 0067 | 写 `type:<T>` 行（先删后插） |
| `control.nexloop_recall_index_alias` | 0067 | 别名的 canonical ref 必须在定义行里 |
| `ontology.nexloop_recall_scope`，alias 分支 | 0379（替换了 0067 的版本） | 查询时判断别名、词表释义、发布别名是否有效：canonical ref 必须在**最新版本**的定义行里 |
| `runtime.nexloop_semantic_release_reindex` | 0249 | 语义发布里的别名行，找不到 ref 就跳过 |
| `ontology.nexloop_recall_glossary_reflow` | 0378 | 世界模型词表的释义行 |
| `authz.nexloop_review_decide`，merge_into 分支 | 0082 | 候选合并的目标必须是当前定义里同类、同范围的元素 |

- **消费方**：`control.nexloop_recall_terms`（0379）和 `control.nexloop_recall_search`（0378）只读 `nexloop_recall_entries` 加 `nexloop_recall_scope`。它们自己不需要改。

## 2. hidden 的语义

- **现状**：
  - `eios.ontology.models.PropertyVisibility` 的注释写的是“UI 以多显眼的方式展示这个属性”，可取 prominent、normal、hidden，原本只是展示提示。
  - 但在 NexLoop 里，hidden 唯一的写入路径是 NX-040 F3 的 hide（0254，`schema_publication.py`）。它是**已发布可选属性的回退手段**：Schema 发布不提供删除属性的操作，hide 就是退役。
  - 属性本身不变：名字、类型、READ 资源和已有对象上的值都还在，授权和对象读写都不看 visibility。
- **提议定义**（请 L6 确认）：在语义层，hidden 等于“退役”。语义层包括召回、术语抽取、别名、词表释义和候选合并目标。
  - 一个属性在**最新版本**中是 hidden，它就不再是语义层的活定义元素。它的 name、display_name、description 和枚举值都不再被召回、抽词，也不能作为别名或合并的目标。
  - hidden **不改变**授权、对象读写、Action 绑定和 Context 包里对象属性的投影。这些继续按现有 READ 规则处理。本任务不涉及。
  - 不按版本追溯：判断只看最新版本。如果之后某个版本把属性改回 normal（目前 Schema 流程没有这个入口，但手工发布可能出现），重新建索引后它就恢复。
- **类型级**：`ObjectTypeDefinition` 没有 visibility 字段，类型不在本任务范围内。

## 3. 改法

**新迁移**（临时编号，调度员重编），用 `create or replace` 重定义 `ontology.nexloop_recall_definition_rows`：签名不变，owner、权限和 search_path 不变。`search_path` 顺带收紧为 `pg_catalog,pg_temp`，与 0379 一致。

改动只有一处：属性循环开头加上

```sql
continue when p->>'visibility' = 'hidden';
```

这样，这个属性的 name、display_name、description 和全部枚举值（vocabulary_value）一起跳过。类型行不变。`property_group` 的计算不受影响，因为 hidden 属性不再产生行。

**选这个做法的原因**：改一处，§1 表里的七个调用方语义就一致了。

| 面 | 改后行为 | 需要另写代码吗 |
|---|---|---|
| definition_texts 与 index_definition | 不再写 hidden 行，向量文本随之变化，两边仍然一一对应（attach 的覆盖检查继续成立） | 否 |
| recall_scope alias 分支 | 指向 hidden 属性的已有别名、词表释义（`glossary:`）和发布别名（`release-alias:`）在**查询时**立即失效，不用删行 | 否 |
| index_alias | 指向 hidden 属性的新别名报 `target unavailable` | 否 |
| semantic_release_reindex 与 glossary_reflow | 下一次重排时跳过（不写行） | 否 |
| review_decide merge_into | 候选不能合并到 hidden 属性，报 “merge target is not a live definition…” | 否，但这是**行为变化**，见 §5 |

**备选方案**（不推荐）：另建一个 `nexloop_recall_live_definition_rows` 只给召回用，0082 的合并检查继续用旧函数。缺点有两个：一是要重定义 4 个函数（index_definition、definition_texts、index_alias、recall_scope），再加上 0249 和 0378 两个重排函数，改动面大六倍；二是“活定义”会有两个口径。只有 L6 认为合并到 hidden 属性必须保留时才选它。

**recall_terms 不需要单独改**。它只从 `nexloop_recall_entries ⨝ nexloop_recall_scope` 抽词：

- `type:` 行靠回填清除，见 §4；
- 别名类的行靠 scope 在查询时失效。

不加查询时的 visibility 判断，因为 scope 的非别名分支只比对 `source_revision = 最新版本`，再逐行解析 definition 的开销没有必要。

## 4. 已有索引的回填

**为什么要回填**：hide 会发布 N+1 版本。正常情况下，召回索引服务按 `nexloop_recall_stale_definitions` 发现 `type:<T>` 落后，就按 N+1 重排。但凡是**已经**按 hide 之后的版本重排过的类型，它的 `type:<T>` 行就在最新版本上，并且含 hidden 属性。stale 检查不会再触发重排，这些行会一直留着。

**回填**写在同一个迁移里，在重定义之后执行，并且只删除：

```sql
do $$ declare t text; begin
 for t in select tenant_id from control.nexloop_tenants loop
  perform set_config('eios.tenant_id',t,true);
  delete from ontology.nexloop_recall_entries e
   where e.tenant_id=t and e.index_kind='definition' and e.source_key like 'type:%'
     and e.target_kind in ('property','vocabulary_value')
     and e.source_revision=ontology.nexloop_recall_latest_version(t,e.gate_type)
     and not exists(select 1 from ontology.nexloop_recall_definition_rows(t,e.gate_type,e.source_revision) d
                    where d.ref=e.ref and d.field=e.field and d.body=e.body);
 end loop;
 perform set_config('eios.tenant_id','',true);
end $$;
```

- **向量**：`nexloop_recall_embeddings` 按 `on delete cascade` 一起删掉，剩下的行和向量仍然一一对应。不需要重新计算向量，也不需要调用 provider。
- **别名、释义和发布别名的行不删**：scope 在查询时已经让它们失效。保留它们，回退时就能立即恢复（§7）。到下一次词表重排或语义发布重排时，glossary 和 release-alias 行自然不再写入。
- **旧版本的 `type:` 行**（source_revision 小于最新版本）scope 本来就不读，不处理。
- **规模**：每个租户、每个已建索引的类型调用一次 definition_rows，只遍历已有行。stage 的行数见 §5 的 S2，预计在千行量级，迁移内完成没有问题。
- 迁移要通过 db_boundary、tenant_fk、system_catalog、copy guard、擦除覆盖和 nx041 sim 这几项守卫测试。不新增表，所以擦除覆盖不受影响。

## 5. 对现有真实类型的影响面

**只读统计**：请调度员在 DATA_HOST 用 `ro` 运行。只读表，不调用函数，不需要 `eios.tenant_id`。

**S1：最新版本里 hidden 的属性**（这是全部影响面）

```
ro "with l as (select tenant_id,type_name,max(version) v from ontology.object_type_versions where tenant_id='$TENANT_ID' group by 1,2)
select l.type_name,l.v,p->>'property_name' prop,jsonb_array_length(coalesce(p->'type_descriptor'->'enum','[]')) enum_n
from l join ontology.object_type_versions o on o.tenant_id=l.tenant_id and o.type_name=l.type_name and o.version=l.v
cross join jsonb_array_elements(coalesce(o.definition->'properties','[]')) p
where p->>'visibility'='hidden' order by 1,3"
```

**S2：这些属性在召回索引里的行**，按来源和字段分组。`type` 这一组就是回填要删的行数；`alias`、`glossary`、`release-alias` 这几组是查询时会失效的行数。

```
ro "with l as (select tenant_id,type_name,max(version) v from ontology.object_type_versions where tenant_id='$TENANT_ID' group by 1,2),
h as (select l.type_name,p->>'property_name' prop,l.v from l join ontology.object_type_versions o on o.tenant_id=l.tenant_id and o.type_name=l.type_name and o.version=l.v
 cross join jsonb_array_elements(coalesce(o.definition->'properties','[]')) p where p->>'visibility'='hidden')
select split_part(e.source_key,':',1) src,e.field,(e.source_revision=h.v or e.source_key not like 'type:%') live_now,count(*)
from ontology.nexloop_recall_entries e join h on e.gate_type=h.type_name and e.gate_property=h.prop
where e.tenant_id='$TENANT_ID' and e.index_kind='definition' group by 1,2,3 order by 1,2,3"
```

**S3：近 30 天语义读取返回过这些属性的次数**（改动后，这类读取的结果会变）

```
ro "with l as (select tenant_id,type_name,max(version) v from ontology.object_type_versions where tenant_id='$TENANT_ID' group by 1,2),
h as (select l.type_name,p->>'property_name' prop from l join ontology.object_type_versions o on o.tenant_id=l.tenant_id and o.type_name=l.type_name and o.version=l.v
 cross join jsonb_array_elements(coalesce(o.definition->'properties','[]')) p where p->>'visibility'='hidden')
select h.type_name,h.prop,a.world,count(distinct a.access_id) accesses
from runtime.nexloop_semantic_accesses a cross join jsonb_array_elements(a.result_refs) r join h
 on r.value->>'ref'='eios:property:'||h.type_name||'/'||h.prop or r.value->>'ref' like 'nexloop:vocabulary:'||h.type_name||'/'||h.prop||'/%'
where a.tenant_id='$TENANT_ID' and a.kind='access' and a.recorded_at>=now()-interval '30 days' group by 1,2,3 order by 4 desc"
```

**S4：引用 hidden 属性或其枚举值的候选**，按 status、kind 分组（0082 的行为变化；`like` 宽匹配，可能多算不会漏算）

```
ro "with l as (select tenant_id,type_name,max(version) v from ontology.object_type_versions where tenant_id='$TENANT_ID' group by 1,2),
h as (select l.type_name,p->>'property_name' prop from l join ontology.object_type_versions o on o.tenant_id=l.tenant_id and o.type_name=l.type_name and o.version=l.v
 cross join jsonb_array_elements(coalesce(o.definition->'properties','[]')) p where p->>'visibility'='hidden')
select c.status,c.kind,count(*) from ontology.nexloop_candidate_definitions c join h
 on c.candidate::text like '%eios:property:'||h.type_name||'/'||h.prop||'%' or c.candidate::text like '%nexloop:vocabulary:'||h.type_name||'/'||h.prop||'/%'
where c.tenant_id='$TENANT_ID' group by 1,2 order by 1,2"
```

**怎么读结果**：

- **S1 为空**：stage 现在没有影响，改动只对以后的 hide 生效。我推测是这样，因为 F3 hide 在 stage 上可能还没用过。
- **S1 非空**：S2 的 `type` 组就是回填删除量，S3 是改后会少召回的读取，S4 非 0 时，这些候选的 merge_into 会被拒（可以改成 reject 或另选目标）。
- S4 的列按 0070 和 0082 写：`status`（0082 起取值有 pending_review、rejected 等）、`kind`、`candidate`。`like` 匹配宽一些，可能多算，但不会漏算。

**其他影响**：

- P5 路线 A 的退役步骤（A4）：改动之后，F3 hide 就能让夹具的 F 和 T 不再被抽词，A4 第 1 步撤 definition_read_rules 就不再是唯一手段。A4 文本需要相应更新（L3）。
- EvoOntology 的候选生成：hidden 属性不再出现在召回结果里，同名的新候选不会被识别成“已有定义”，可能被重新提议。schema 发布的 add_optional_property 会因为属性名重复而拒绝，所以不会产生重复属性，但审核人会看到这类候选。是否在候选生成阶段单独提示，留给 L6。

## 6. 测试

迁移落地时，复现测试里标了 CURRENT 的断言翻转为：

- definition_rows 不含 hidden 属性的任何行；
- 按 v2 建索引后，hidden 属性的行数为 0；
- terms 里没有 `关注防水` 和 `两千元以内`；
- 检索结果里没有该属性；
- 已有别名“防泼水”不再召回该属性；
- `index_alias` 报 `target unavailable`。

另外加以下测试：

1. **回填**：先在旧函数下按 v2 建索引（在迁移前的 catalog 上运行，或者直接插入行来模拟），再执行迁移里的回填语句。hidden 行被删掉，向量随之级联删除，其他属性的行和向量数不变；第二次执行删除 0 行（幂等）。
2. **词表释义**：对 hidden 属性写一条 glossary 释义，重排后不写入；hide 之前写入的那条在查询时不再命中。
3. **语义发布别名**：reindex 时跳过 hidden ref。
4. **0082**：merge_into 到 hidden 属性被拒，merge_into 到 normal 属性不受影响。
5. **恢复**：v3 把属性改回 normal，重建索引后 terms 恢复，hide 之前的别名重新有效（因为别名行没被删）。
6. 必须同时运行的已有测试：`test_recall_pg`、`test_semantic_access_record_pg`、`test_world_glossary_pg`、`test_semantic_release_pg`、`test_review_decisions_pg`、`test_workbench_review_decisions_pg`，以及 L3 的 `test_p5_synthetic_fixture_pg` Q6（它断言了现状，需要同步翻转）。批次守卫测试按惯例全部运行。

## 7. 回退

- **代码回退**：再发一个迁移，把 definition_rows 恢复成 0067 的函数体（不能删已发布的迁移）。
  - 别名、释义和发布别名的行没被删，scope 立即恢复它们。
  - 回填删掉的 `type:` 行要靠重排找回：stale 检查认为这些类型是最新的，所以不会自动触发。回退迁移要同时删掉受影响类型在最新版本上的 `type:` 行（S1 列出的类型），让 `stale_definitions` 在下一个 tick 重排它们。
  - 重排需要 embedding provider 可用；不可用时，按现有行为只能用全文检索和 n-gram。
- **运行时止损**（不改代码）：如果改动之后某个真实读取依赖 hidden 属性，正确的做法是发布新版本，把这个属性改回 normal，而不是回退迁移。

## 8. 需要 L6 裁定

1. **hidden 的语义**：§2 的提议是“语义层退役，只看最新版本”。
2. **改法**：改 definition_rows 一处（推荐，0082 的合并目标会一起收紧），还是另建一个只给召回用的函数。
3. **回填**：只删 `type:` 行，别名类的行靠查询时失效（推荐）；还是把别名类的行也一起删。
4. **S4 非 0 时**：先处理待审候选再上线，还是允许上线后由审核人改判。

## 9. 实现（L6 2026-11-11 条件 PASS 之后）

- **迁移**：`0450_recall_hidden_properties.sql`（合批重编号）。内容：
  - 用 `create or replace` 重定义 `definition_rows`。属性循环开头加 `continue when p->>'visibility' is not distinct from 'hidden'`，search_path 加上 pg_temp。
  - 回填：DO 块按租户执行，只删 `type:` 行里位于最新版本、且已不在定义行里的属性行和枚举值行。
  - catalog 和 `versions.lock.json` 的 bootstrap_revision 改为 0449。
- **H-a**：ADR-019 追加 §9“修订 A1”；`PropertyVisibility` 的注释同步更新。
- **H-b**：`tests/test_recall_hidden_property_pg.py` 中 CURRENT 断言已翻转为新行为，并新增：
  - 回填：hidden 行连同向量被删，其他行和向量数不变，第二次执行不变；
  - 恢复：v3 改回 normal 之后，terms 和保留的别名都恢复；
  - S1–S4 SQL 冒烟：在测试库上运行本稿 §5 的四条查询。
- **H-c**：S1–S4 只读脚本已交调度员，存档前不部署本迁移。S1 为空时，存档里写明“只对今后的 hide 生效”。
- **H-d**：部署时序只能在 P5 执行单 §1 之前或 §8 之后，不得落在 §4 预热到 §6 r2 之间。部署后，L3 的 Q6 测试和 A4 文本要按 P5 执行单 §5 同步更新。
- **S4 非 0**：上线前把 S4p 明细列给审核人，改判 reject 或换目标；也可以上线后处理，但须记入运行单。
- **0082 merge_into 收紧**：没有单独写测试。它的判断直接用 `definition_rows`，复现测试已证明 hidden 属性不再出现在定义行里。
