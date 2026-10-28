# 世界模型中文注释（world-model glossary）：后端设计

状态：设计稿，待 L6 审，审过再写代码。分支 `l2/world-glossary`，基于 main 53af5efc。按负责人 10-29 的决定，这是 Schema 改动，走 feature 分支 + PR。前端由 L9 按第 4 节的 API 形状并行做。

## 1. 决定与边界

- 显示形式为 `Consumer（客户）`，用全角括号，由前端拼接；后端只提供译文本身（`客户`）。
- 范围覆盖世界模型里的全部英文标识：栏目名、Object Type、Property、Link 及反向名、Function、Action。
- 词表是**显示用的元数据，不是业务事实**：
  - 不改任何标识、契约名或 Action 名；
  - 不进 ontology，不进 EvoOntology，不参与授权、规划或上下文装配；
  - 发给模型的上下文里也不带它（避免译名反过来影响行为）。
- 首版 v1 用负责人人工翻译的 303 条。以后新增的标识由模型翻译，立即显示并标为“机翻”。负责人可以改正，改正后以人工译文为准。

## 2. 存储（PG，迁移只新增文件，从 main 的下一个号起用临时编号）

| 表 | 用途 |
|---|---|
| `control.nexloop_world_glossary_publications` | 词表清单的发布记录：tenant、world、`manifest_version`、`sha256`、`expected_revision`、发布时间。规则与 remediation rules（0351）相同：同版本同 sha 是重放，什么都不写；同版本不同内容拒绝（40001）；版本只能递增 |
| `control.nexloop_world_glossary` | 当前译文：`(tenant, world, kind, identifier)` 为主键。字段：`gloss`、`source`（`manual`/`machine`）、`revision`、`updated_at`；机翻另记 `model_provider`、`model_id`、`input_sha256`、`translated_at` |
| `control.nexloop_world_glossary_revisions` | 只追加的修订历史：每次清单发布、机翻写入、人工改正各记一行，内容为旧译文、新译文、`source`、`revision`、`intent_id`、`recorded_at`、`actor_principal`（人工改正时才有）。用触发器保证不可修改，写法同 `ontology.nexloop_assessment_revisions`（0058） |
| `control.nexloop_world_glossary_jobs` | 机翻队列：每个缺译标识一行。字段为 attempts、next_attempt_at、status（`pending`/`done`/`failed`），以及只含失败码的 `last_error`（如 `http_429`），不存原文 |

- `kind` 取 `section | object_type | property | relation | relation_inverse | event_type | function | action`。
  - property 的 identifier 只用属性名，不带所属类型：词表 v1 就是这样组织的，同名属性共用一个译名。
  - 反向名作为单独的 kind，因为词表 v1 里反向名和正向名混在 `relations` 一组。发布时按世界模型里实际的 `inverse_name` 分类。
- **擦除**：只有 `actor_principal` 一列涉及员工身份。把它登记进 `STAFF_KEPT`，理由沿用 `_ACT`（记录谁做了治理动作）。其余列都不是个人数据，也不含客户数据，不进客户擦除。会补测试，证明客户擦除和成员擦除都覆盖到。

## 3. 三条写入路径

1. **人工清单发布（v1）**
   - 文件：`deploy/configuration/world-model-glossary.v1.json`，内容取自负责人的草稿，不改，只统一格式（键排序、两空格缩进、末尾换行）。
   - 发布：新增 `scripts/deploy/world_glossary_apply.py`，走 configurator window，`--check` 时只打印 `manifest_sha256`、不连库，SQL 侧同样核对 sha256。发布写 `source=manual`，所以会覆盖同名的机翻。
2. **机翻服务**：新的后台服务 `world-glossary-translator`，按 claim-extraction-worker 的方式接入 compose。
   - **找缺译**：周期性读取世界模型，找出词表中缺的标识，写入任务队列。
   - **调用模型**：复用现有的 `LazyModelProvider`、`model_profile` 和 `OpenAICompatibleExtractionProvider`，模型配置用 stage 已有的 `claim-extraction-worker/model.env`，以 compose secret 只读挂载，不新增凭据。
   - **输入**：只有标识名、kind、所属类型名、已有的 `description`/`display_name`，外加 v1 词表里与它相关的若干条作为术语参考（例如 Consumer=客户）。不含任何客户数据，也不含对象实例。
   - **输出**：严格 JSON `{gloss}`，长度不超过 24 字，必须含中文，不能包含原标识或括号。不合格按 `response_parse` 失败处理。
   - **限额**：每轮最多 20 个标识，每天每租户最多 200 次模型调用，在 PG 计数。达到上限后停到第二天，并打一条告警样本。
   - **重试**：指数退避，基数 60 秒、上限 1 小时；第 3 次失败后标 `failed`，不再自动重试。只要还没成功，前端就只显示英文（`gloss=null`）。
   - **写入**：只能经 SECURITY DEFINER 函数写，而且只能写 `source=machine`。已有 `manual` 的标识，机翻**不能覆盖**（函数里校验）。
3. **人工改正**：新的受治理 Action `nexloop.world_glossary.set`，只给 owner。
   - 入参：`kind`、`identifier`、`gloss`（1–24 字；空字符串的含义见第 6 节问题 3）。
   - 效果：写 `source=manual`，revision 加一，追加一条修订记录。
   - 治理：标识必须在当前世界模型里存在，否则拒绝。不需要审批策略，风险等级 low、无外部效应。
   - 登记：`business-actions.v1.json`、`capabilities.v1.json`、`system-catalog-names.v1.json`，以及 `workbench-roles.v1.json` 里的 owner 授权和 owner-only 条目。`test_stage_register_pg` 中 Action 的数量从 128 改为 129，即 (20, 129, 2)。

## 4. API 形状（给 L9）

`GET /api/v1/workbench/world-model` 新增字段；旧字段一个都不变。

```jsonc
{
  "world": "real", "computed_at": "…",
  "glossary": {                       // 新增
    "manifest_version": 1,            // 已发布的人工清单版本；未发布时为 0
    "sections": {"Object Type": "对象类型", "…": "…"}   // 栏目名，前端按英文栏目名查表
  },
  "object_types": [{
    "name": "Consumer", "gloss": "客户", "gloss_source": "manual",          // 新增
    "properties": [{"name": "external_id", "gloss": "外部编号", "gloss_source": "manual", "…": "…"},
                   {"name": "secret_group_x", "withheld": true, "gloss": null, "gloss_source": null}],
    "…": "…"
  }],
  "relation_types": [{"name": "assignment_goal", "gloss": "分派所属目标", "gloss_source": "manual",
                      "inverse_name": "…", "inverse_gloss": "…", "inverse_gloss_source": "machine", "…": "…"}],
  "event_types": [{"name": "…", "gloss": null, "gloss_source": null, "…": "…"}],
  "actions":   [{"name": "Budget.set", "gloss": "设定预算", "gloss_source": "manual", "…": "…"}],
  "functions": [{"name": "nexloop.conversation.scope_denial", "gloss": "会话超范围拒绝", "gloss_source": "manual", "…": "…"}]
}
```

- `gloss_source` 的取值为 `"manual" | "machine" | null`。null 表示还没有译文，前端只显示英文。`machine` 显示“机翻”标记。
- withheld 的属性同样带 gloss，因为属性名本来就会显示。
- `inverse_name` 为空字符串时，`inverse_gloss` 和 `inverse_gloss_source` 都为 null。
- 改正接口沿用现有工作台 Action 路由：`POST /api/v1/workbench/actions/world_glossary_set`，body 为 `{kind, identifier, gloss}`，返回 `{kind, identifier, gloss, gloss_source:"manual", revision}`。
- 修订历史：`GET /api/v1/workbench/world-model/glossary/history?kind=&identifier=`，只给 owner，按时间倒序，每条含 `gloss`、`source`、`revision`、`recorded_at`、`model_id`（机翻时有）。是否要做见第 6 节问题 4。
- **契约**：`packages/contracts/workbench-world-model.schema.json` 处处是 `additionalProperties:false`，加字段就必须出新版本。新增 `workbench-world-model.v2.schema.json`，v1 原样保留；重新生成 `generated/openapi-components.json`。

## 5. 测试计划

- 授权：
  - 读取：任何能读世界模型的角色都看得到 gloss；跨租户读不到。
  - 改正：非 owner 被拒；owner 成功；不存在的标识、空串、超长、不含中文都被拒。
- 优先级：机翻不能覆盖 manual；清单发布会覆盖 machine；revision 只增；修订表禁止改写。
- 清单发布：重放无副作用；同版本不同内容被拒（40001）；`expected_revision` 不对被拒；sha256 不一致被拒；`--check` 不连库。
- 机翻服务，用桩替代模型提供方：
  - 成功时字段写全，`input_sha256` 可复算；
  - `http_429`、超时、不合格输出时走退避，第 3 次后标 failed，此时 API 中 `gloss=null`；
  - 达到每日限额后停止调用；
  - 发给模型的内容里查不到任何客户数据。用合成客户的标记串做缺席断言，标记串不用类十六进制字符。
- 擦除覆盖：`test_member_erasure_coverage_pg` 和 `test_consumer_erasure_coverage_pg` 都通过；客户全库搜索不受影响。
- register 数量：(20, 129, 2)。
- 契约：v2 schema 能校验真实响应；v1 文件不变。

## 6. 需要负责人确认的问题

1. **v1 词表缺新 Action 本身**：v1 里的 actions 有 128 条，与现在的 Action 数一致；新增的 `nexloop.world_glossary.set` 不在其中。建议在 v1 里补一条“设定世界模型译名”，否则它上线后会先显示机翻。补这一条会改动词表内容，所以要你同意。
2. **事件类型不在词表 v1 中**：世界模型返回 `event_types`，但 v1 没有对应分组。按“全部英文”的范围，现在的做法是让它们走机翻。如果你希望人工翻译，请补一个分组。
3. **人工清空**：是否允许负责人把某个标识设为“不显示中文”？建议允许，`gloss=""` 时记 `source=manual`，前端只显示英文，机翻也不会再覆盖。
4. **修订历史 API**：这一版是否就要提供？可以只留在库里，以后再开。
5. **模型配置**：复用 claim-extraction-worker 的 `model.env`，意味着两个服务共用同一把模型 key 及其额度。如果要分开计费，就得新增凭据，与你“不新增凭据”的要求冲突。现在按共用设计。
