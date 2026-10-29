# 世界模型中文注释（world-model glossary）：后端设计

状态：设计稿 r2，L6 复审 PASS（9a4dcc4f），补 I5、S4–S6 后开始实现。第 6 节按调度员 10-29 的决定定稿，第 2–5 节按 L6 对 bbfd18e6 的审查意见（C1、I1–I4、S1–S3）修订。待 L6 复审，复审通过前不写代码。

分支 `l2/world-glossary`，基于 main 53af5efc。按负责人 10-29 的决定，这是 Schema 改动，走 feature 分支 + PR。前端由 L9 按第 4 节的 API 形状并行做。

## 1. 决定与边界

- **显示形式**：`Consumer（客户）`，全角括号，由前端拼接。后端只提供译文本身（`客户`）。
- **范围**：世界模型里的全部英文标识，包括栏目名、Object Type、Property、Link 及反向名、Function、Action。
- **词表只是显示用的元数据，不是业务事实**：
  - 不改任何标识、契约名或 Action 名；
  - 不进 ontology，不进 EvoOntology，不参与授权、规划或上下文装配；
  - 发给运行时模型的上下文里也不带它，免得译名反过来影响行为。
- **首版 v1**：负责人人工翻译，共 304 条：

  | 分组 | 条数 |
  |---|---|
  | sections | 14 |
  | object_types | 20 |
  | properties | 95 |
  | relations | 44 |
  | functions | 2 |
  | actions | 129（含 `nexloop.world_glossary.set`） |

  `event_types` 分组预留，v1 为空。
- **之后新增的标识**：由模型翻译，立即显示，标为“机翻”。负责人可以改正，改正后以人工译文为准。
- **real 和 sim 共用一份词表**：词表按租户存，不按世界分。理由：
  - 两个世界的标识是同一套定义；
  - 词表只是显示元数据，没有任何效应。

  改正 Action 只在 real 世界受理，sim 世界一律拒绝，免得在模拟里改到真实显示。

## 2. 存储

PG 迁移只新增文件，从 main 的下一个号起用临时编号。修订历史表和触发器这一版就建（S3），查询 API 延后。

| 表 | 用途 |
|---|---|
| `control.nexloop_world_glossary_publications` | 词表清单的发布记录（见下方“发布规则”）。字段：`tenant_id`、`manifest_version`、`sha256`、`expected_revision`、`published_at` |
| `control.nexloop_world_glossary` | 当前译文，主键 `(tenant_id, kind, identifier)`。字段：`gloss`（可为空串）、`source`（`manual`/`machine`）、`revision`、`updated_at`。机翻行另记 `model_provider`、`model_id`、`input_sha256`、`translated_at` |
| `control.nexloop_world_glossary_revisions` | 只追加的修订历史。清单发布、机翻写入、人工改正各记一行，含 `revision`、旧译文、新译文、`source`、`intent_id`、`recorded_at`、`actor_principal`（只有人工改正时有值）。触发器禁止 update 和 delete，写法同 `ontology.nexloop_assessment_revisions`（0058） |
| `control.nexloop_world_glossary_jobs` | 机翻队列，每个缺译标识一行。字段：`attempts`、`next_attempt_at`、`status`（`pending`/`done`/`failed`），以及只含失败码的 `last_error`（如 `http_429`），不存原文 |
| `control.nexloop_world_glossary_usage` | 机翻用量（I4），每次模型调用一行：`tenant_id`、`day`（UTC 日）、`model_id`、`prompt_tokens`、`completion_tokens`、`cost`、`currency`、`recorded_at` |

**发布规则**（同 remediation rules，0351）：
- 同版本同 sha256 是重放，什么都不写；
- 同版本不同内容拒绝（40001）；
- `expected_revision` 必须等于当前已发布版本，否则拒绝（40001）；
- 版本只能递增。

**`kind` 的取值**：`section | object_type | property | relation | relation_inverse | event_type | function | action`。
- property 的 identifier 只用属性名，不带所属类型。词表 v1 就是这样组织的，同名属性共用一个译名。
- 词表 v1 的 `relations` 一组里混有正向名和反向名。发布时按世界模型里实际的 `inverse_name` 分成 `relation` 和 `relation_inverse`。

**擦除**：
- 只有 `actor_principal` 一列涉及员工身份，登记进 `STAFF_KEPT`，理由沿用 `_ACT`（记录谁做了治理动作）。
- 其余列都不是个人数据，也不含客户数据，不进客户擦除。
- 会补测试，证明客户擦除覆盖和成员擦除覆盖都通过。

## 3. 写入路径与函数规范

### 3.1 人工清单发布

- **清单文件**：`deploy/configuration/world-model-glossary.v1.json`。内容取自负责人草稿，不改，只统一格式：键排序、两空格缩进、末尾换行。
- **发布脚本**：新增 `scripts/deploy/world_glossary_apply.py`，走 configurator window。
  - `--check` 只打印 `manifest_sha256`，不连库；
  - SQL 侧同样核对 sha256。
- **发布语义**（I1）：
  - 清单里的每条都写成 `source=manual`，覆盖同名的机翻；
  - 新版清单里**省略**的条目保留原译文和来源，不删除；
  - 只有清单里**显式写 `null`** 的条目才清除。这时删掉当前行，修订表记一行，新译文为 null。
  - 所以 v2 删掉 v1 里的某条，那条人工译文仍然保留。
  - **`null` 与 `""` 不同**（S4）：清单里写 `null` 表示“恢复为自动翻译”，当前行删除后该标识会重新进入机翻队列；写 `""` 表示“人工清空”，记 `source=manual`，只显示英文，机翻不覆盖。

### 3.2 机翻服务 `world-glossary-translator`

**身份与部署**（C1）。模型 key 和 claim-extraction 共用，PG 身份单独一个：

- **PG 主体**：新主体 `world_glossary_translator`，有自己的 DSN、pg-client 证书和服务凭据。不借用 claim-extraction-worker 的任何 DB 凭据。
- **登记**：
  - `deploy/authorization/stage-principals.v1.json` 加上 `world-glossary-translator/service-credential` 等条目；
  - `deploy/authorization/service-grants.v1.json` 加上它的 grant；
  - `deploy/stage/connection-budget.v1.json` 和 `deploy-gate.app.example.json` 各加一条；
  - `test_stage_register_pg` 中按主体或 grant 计数的断言相应调整（具体数在实现时按 register 输出核对）。
- **权限只有四项**：
  1. 执行写机翻的 definer 函数（只能写 `source=machine`）；
  2. 读世界模型的标识，经一个只返回标识名、kind、所属类型、`description`、`display_name` 的 definer 读函数，不给表权限；
  3. jobs 队列：认领、完成、失败，都经 definer 函数（S5），不给表权限；
  4. 限额预占与 usage 回填，同样经 definer 函数（I5）。

  没有其他表权限，没有任何 Action 执行权。成本记账见下方“限额与成本”。
- **compose**：`deploy/stage/compose.app.example.yaml` 新增服务，写法照 `claim-extraction-worker`：只读根文件系统，`cap_drop ALL`，非 root 用户。
  - 只挂三样：共用的 `claim-extraction-worker/model.env`（只读 secret）、它自己的 DSN 和 pg-client 证书、它自己的服务凭据；
  - 不挂渠道、admin、bootstrap 或其他服务的凭据；
  - 出网只到模型 provider：加入现有的 `claim_egress` 网络，防火墙模板 `deploy/stage/firewall/app-host.nft.template` 中按 claim-extraction-worker 的出站规则给它同样的条目。
- **host-health**：`deploy/examples/host-health-install.app-host.example.json` 的期望容器加上它。
- **部署单步骤**：
  1. `stage_secrets.py add-service --service world-glossary-translator` 生成 DSN、证书和服务凭据；
  2. 执行 register（`stage_secrets.py publish` 和 register 步骤），并 dry-run 确认新 Action 和 grant 都已发布；
  3. 迁移；
  4. `world_glossary_apply.py` 发布 v1；
  5. 启动服务，确认 host-health 通过。

**流程**：
- **找缺译**：每轮经读函数取世界模型的标识，找出词表里没有的，写入 jobs。
- **模型调用**：复用 `LazyModelProvider`、`model_profile` 和 `OpenAICompatibleExtractionProvider`。
- **发给模型的输入**（S1）：
  - 标识名、kind、所属类型名；
  - 已有的 `description` 和 `display_name`，各截断到 200 字；
  - v1 词表里与它相关的若干条，作为术语参考，例如 Consumer=客户。

  不含任何客户数据或对象实例。withheld 属性的**名字**也会发给模型：它本来就显示在世界模型里，只有它的说明不发。
- **输出校验**：严格 JSON `{gloss}`，1–24 字，必须含中文，不能含原标识或括号。不合格按 `response_parse` 失败处理。

**写入函数规范**（I1）：

```sql
insert into control.nexloop_world_glossary(tenant_id,kind,identifier,gloss,source,revision,…)
values (…,'machine',1,…)
on conflict (tenant_id,kind,identifier) do update
  set gloss=excluded.gloss, source='machine', revision=nexloop_world_glossary.revision+1, …
  where nexloop_world_glossary.source<>'manual'
returning revision;
```

- `returning` 没有行，说明那条已是 manual，机翻放弃写入，job 标 `done`。
- 机翻和人工改正在同一行上的竞争，由这条语句的行锁裁决，manual 不会被抢写。
- 修订表在同一事务里追加。

**限额与成本**（I4）：
- **条数上限，调用前原子预占**（I5）：每轮最多 10 条，每租户每天最多 50 条。translator 在调用模型**之前**先调 definer `reserve`：同一事务里按 (租户, UTC 日) 对计数行 `for update`、判断、`+1`，返回 `allowed` 或 `full`。只有 `allowed` 才调模型；调用结束（成功或失败）再用 `usage` 回填 token 和费用到这次预占的那一行。预占不退还（失败的调用也算一条），所以上限是调用次数的硬上界。
- **用尽**：当天停止调用，并打一条告警样本。
- **用量记录**：每次调用（成功或失败）都在 usage 表记 token 数。
- **单价来源**（S6）：只用仓库里已有的模型单价配置（实现时核实 `deploy/configuration` 下现有的模型价格或预算清单，例如嵌入预算用的 tariff 文件那一类）。找不到该模型的单价时，`cost` 和 `currency` 留空，只记 token，不估价。
- **成本归集**：费用接入现有的 `model` 预算类型（control 里的预算表 `budget_kind='model'`），这样预算里能看到，与 claim-extraction 共用 key 时也能按服务分开汇总。
- **`nexloop.cost.record` 的用法**（调度员第 5 条）：机翻服务本身没有 Action 执行权，所以由写入函数在同一事务里按 `nexloop.cost.record` 的记账口径记一笔。实现时先核实它的入参和记账表；如果不能在 definer 内复用，回报后再定，不另开记账路径。

**重试**：
- 指数退避，基数 60 秒，上限 1 小时；
- 第 3 次失败后标 `failed`，不再自动重试；
- 只要还没成功，API 就返回 `gloss=null`，前端只显示英文。

### 3.3 人工改正：`nexloop.world_glossary.set`

- **授权**：只给 owner。
- **入参**：body 为 `{kind, identifier, gloss}`，用 exactKeys 校验，多一个键少一个键都拒绝（I3）。
  - `tenant_id` 取自服务端已认证的上下文，不从 body 传；
  - 只在 real 世界受理，sim 世界拒绝（见第 1 节）。
- **gloss 规则**（I2）：
  - 空串合法，表示人工清空：记 `source=manual`，前端只显示英文，机翻不得覆盖；
  - 非空时必须含中文，长度不超过 24 字；
  - 其他一律拒绝。
- **校验时点**：标识必须在当前世界模型里存在。这个检查在派发时于 Action 内做，不在规划时做。
- **效果**：在同一事务里对当前行 `for update`，写 `source=manual`，revision 加一，追加修订记录（`actor_principal` 为当前成员）。
- **治理**：不需要审批策略，风险等级 low，无外部效应。
- **登记**：
  - `business-actions.v1.json`、`capabilities.v1.json`、`system-catalog-names.v1.json`；
  - `workbench-roles.v1.json` 里的 owner 授权和 owner-only 条目；
  - `test_stage_register_pg` 中 Action 数从 128 改为 129，即 `(20, 129, 2)`。

## 4. API 形状（给 L9）

`GET /api/v1/workbench/world-model` 新增字段，旧字段一个都不变。返回体按 **v2 契约**（S2）。

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

**字段语义**：

| `gloss` | `gloss_source` | 含义 | 前端显示 |
|---|---|---|---|
| `null` | `null` | 还没有译文 | 只显示英文 |
| `""` | `"manual"` | 人工清空 | 只显示英文 |
| 非空 | `"machine"` | 机翻 | 英文（译文），加“机翻”标记 |
| 非空 | `"manual"` | 人工译文 | 英文（译文） |

- withheld 属性同样带 gloss，因为属性名本来就会显示。
- `inverse_name` 为空串时，`inverse_gloss` 和 `inverse_gloss_source` 都为 null。

**改正接口**：沿用现有工作台 Action 路由 `POST /api/v1/workbench/actions/world_glossary_set`。
- body：`{kind, identifier, gloss}`，只接受这三个键；
- 返回：`{kind, identifier, gloss, gloss_source:"manual", revision}`。

**修订历史 API**：延后，这一版只存库。

**契约**（S2）：
- 新增 `packages/contracts/workbench-world-model.v2.schema.json`，v1 原样保留：
  - `glossary.sections` 写成枚举栏目名的对象，`additionalProperties:false`；
  - `gloss_source` 和 `inverse_gloss_source` 取 `enum ["manual","machine",null]`；
  - `gloss` 的类型为 `string | null`，`maxLength` 24。
- 重新生成 `generated/openapi-components.json`，把这个接口的返回体声明为 v2。

## 5. 测试计划

- **限额预占**（I5）：当天计数已满时，桩模型的调用次数为 0；并发两个 translator 同时预占最后一条，只有一个得到 `allowed`。
- **授权**：
  - 读：能读世界模型的角色都看得到 gloss；跨租户读不到。
  - 改正：非 owner 被拒，owner 成功。
  - 上下文：body 多键、少键、带 `tenant_id` 都被 exactKeys 拒绝；sim 世界被拒。
  - 派发时校验：在规划和派发之间把标识从世界模型里撤下，派发会拒绝。
- **gloss 取值**（I2）：
  - 空串成功，记为 manual，API 返回 `""` 和 `manual`；之后机翻不覆盖它；
  - 25 字、不含中文的非空串都被拒。
- **优先级与并发**（I1）：
  - 机翻不覆盖 manual，包括并发场景：两个连接同时写机翻和人工改正，结果必为 manual；
  - 清单发布会覆盖 machine；
  - 新版清单省略某条时保留原译文，显式写 `null` 才清除；
  - revision 只增；修订表的 update 和 delete 都被触发器拒绝。
- **清单发布**：
  - 重放无副作用；
  - 同版本不同内容被拒（40001）；
  - `expected_revision` 不对被拒；
  - sha256 不一致被拒；
  - `--check` 不连库；
  - v1 文件为 304 条，其中 actions 129 条。
- **机翻服务**（用桩替代模型提供方）：
  - 成功时字段写全，`input_sha256` 可复算，usage 表记录 token 和费用，model 预算里看得到；
  - `http_429`、超时、不合格输出都走退避，第 3 次后标 failed，这时 API 返回 `gloss=null`；
  - 达到每日条数上限后停止调用；
  - 发给模型的内容里查不到任何客户数据。用合成客户的标记串做缺席断言，标记串不用类十六进制字符；`description` 和 `display_name` 被截断到 200 字（S1）。
- **身份**（C1）：
  - `world_glossary_translator` 只能调这四类函数；
  - 直接读写词表或其他业务表、执行任何 Action，都被拒绝；
  - compose 测试确认它只挂 model.env 和它自己的凭据；
  - `test_stage_compose` 和 host-health 期望容器测试都通过。
- **擦除覆盖**：`test_member_erasure_coverage_pg` 和 `test_consumer_erasure_coverage_pg` 都通过；客户全库搜索不受影响。
- **register 数量**：`(20, 129, 2)`，以及主体和 grant 计数的相应调整。
- **契约**：v2 schema 能校验真实响应；v1 文件不变。

## 6. 已定事项（调度员 10-29，按推荐方案，报负责人）

1. **v1 补新 Action 的译名**：负责人草稿已加 `"nexloop.world_glossary.set": "设定世界模型译名"`。actions 现为 129 条，总数 304。实现时从草稿重新读取。
2. **事件类型**：stage 现在 `event_types` 为 0。v1 不加这个分组，以后出现时走机翻；词表格式里预留 `event_types` 分组，可选，缺省为空。
3. **允许人工清空**：规则见 §3.3 和 §4。
4. **修订历史 API 延后**：表和触发器这一版就建。
5. **模型配置共用**：用 claim-extraction 的模型 key（model.env），但 PG 身份独立（§3.2）。
   - 每日条数上限设得低：每租户每天 50 条，每轮 10 条；
   - 成本按 §3.2 的“限额与成本”记账。

## 7. 实现与设计的差异（实现时发现，r2 之后）

1. **服务的 gate Action**：签名服务端口沿用 claim feed（0069）的模式，必须经一个 Action 资源授权。所以机翻主体有且只有一条授权 `nexloop.world_glossary.translate:1` execute，写在 service-grants 里；它不是发布的 Action 定义，与 `nexloop.claim.extract` 相同，不计入 register 的 Action 数。register 仍是 `(20, 129, 2)`，主体 26，凭据文件 35。
2. **关系的 kind**：正向名和反向名都用 `relation` 一个 kind。两者是不同的字符串，词表 v1 也把它们放在同一组；`relation_inverse` 不再单列。API 形状不变：关系仍返回 `inverse_gloss` 和 `inverse_gloss_source`。
3. **全角括号**：词表 v1 里有 4 条带全角括号（如“金额（最小单位）”）。人工译文允许全角括号，ASCII 括号不允许；机翻译文两种括号都不允许。
4. **常驻改为定时运行**：应用的 PG 连接预算已用满（60/60），机翻服务不常驻，详见第 8 节（调度员 10-29 选 A）。
5. **单价**：挂载 Agent Host 已有的 `host_model_tariff`（非秘密值），每次调用按 `configured_rate` 记入 `runtime.nexloop_cost_entries`。没有单价文件时记 `unpriced`，只记 token 和条数。
6. **每日上限**：每租户每天 50 次，固定写在 SQL 端口里，客户端无法放宽；每轮 10 条，为客户端参数，SQL 端口最多给 10 条。
7. **任务编号**：治理注册表要求 `source_task` 形如 `NX-0xx`，暂用 `NX-064`，请调度员给正式编号后统一替换。

## 8. 一次性运行（--once）与运维保留（调度员 10-29 选 A）

### 8.1 运行方式

- **定时**：systemd timer `nexloop-world-glossary.timer` 每 10 分钟触发一次，落在每小时的 03、13、23……分，避开每分钟整点跑的 journal。触发后，oneshot 服务 `nexloop-world-glossary.service` 执行 `docker compose --profile world-glossary run --rm -T world-glossary-translator`，跑一轮就退出。样例在 `deploy/stage/systemd/nexloop-world-glossary.{service,timer}.example`。
- **身份和容器**：与常驻方案相同。用它自己的服务主体、DSN、pg-client 证书和服务凭据，另外只挂 model.env（共用 claim-extraction 的）和单价文件 `host_model_tariff`（不是秘密）。容器只读、`cap_drop ALL`、`no-new-privileges`，网络只接 `claim_egress`：出网只能到 DATA_HOST 的 PostgreSQL 和模型 provider 的 443 端口。
- **同一时间只有一个实例**，有两层保证：
  1. systemd 不会在 oneshot 单元还在运行时再次启动它；
  2. 命令外层套 `flock -n /run/lock/nexloop-world-glossary.lock`。手工补跑或 timer 撞上上一轮时，后来者拿不到锁，直接退出（退出码 1），不会启动第二个容器。
- **一次运行有多长**：
  - 每轮最多处理 10 条（SQL 端口 `due` 最多返回 10 条）；
  - 运行满 240 秒后不再发起新的模型调用（`RUN_SECONDS`），剩下的留给下一轮；
  - 每次模型调用最多 60 秒（provider 的 deadline）；
  - 所以一次运行最长约 240 + 60 秒，再加启动和收尾，不到 10 分钟一个周期的一半；
  - `TimeoutStartSec=9min` 兜底，卡住的运行会被 systemd 结束。
- **每天的花费上限**：每租户每天 50 次模型调用，在 SQL 里预占计数，与运行频率无关。
- **`--once` 模式下仍然先预占、后调用**：`--once` 和常驻模式走同一个 `run_once`，每条都先调端口的 `reserve`（锁住计数行并加 1），拿到 `allowed` 才调模型（I5）。测试：当天已满时调用 0 次；两路并发抢最后一个名额只有一路成功；运行时间到了不再调用。
- **连接**：运行期间只用 1 个连接（服务 pool 上限 1），进程退出时连接即释放。

### 8.2 运维保留 7 的峰值

`OPERATIONS_RESERVE = 7`（`connection_budget.py`，ADR-037），给“管理、CI、备份、一次性的 sim-setup”用。

**一次快照**（调度员 10-29 08:2x +0800 在 stage 上只读查询 `pg_stat_activity`，按角色计）：domain_worker 17、api 11、action_worker 3、identity 2、bootstrap 2（含这次只读查询本身），后台进程 8；`max_connections` 80，`superuser_reserved_connections` 3。当时运维侧实际只占 bootstrap 2。这只是非备份、非迁移时段的一次快照；**峰值出现在 02:00 的备份窗口或迁移窗口，没有实测过**。

仓库里没有逐项清单，下面按 `deploy/stage/systemd` 下各单元实际打开的 DSN 估一个保守的上界。

| 占用方 | 时间 | 同时连接数 |
|---|---|---|
| journal（`nexloop-backup journal`，recorder DSN） | 每分钟 00 秒 | 1 |
| file-backup（`file_backup_run.py`，recorder DSN） | 每 15 分钟 | 1 |
| host-health 探针（hostssl DSN 直连 PG，`nexloop_host_recorder_app_host`，调度员 10-29 确认） | 每分钟 15 秒 | 1 |
| pg-diff / pg-full（recorder 1 + pgBackRest 备份连接 1） | 每天 02:00 | 2 |
| relation-read-gaps（DATA_HOST，metrics DSN） | 每天 03:40 | 1 |
| runtime-worker 的 `ExecStartPre` doctor | 只在服务启动时 | 1，短暂 |
| 人工管理会话或迁移、sim-setup | 人工，不定时 | 见下 |
| **world-glossary（本节）** | 每 10 分钟的 03、13…分 | **1** |

- **定时任务的最坏重叠**：02:03 前后，pg-diff 2 + journal 1 + file-backup 1 + host-health 1 + world-glossary 1 = **6**。
- **人工操作**：迁移和 sim-setup 都按运行单在维护窗口里做，同时最多 1 个会话，峰值 7，不超过运维保留。
- **约束写进运行单**（调度员 10-29 接受）：人工迁移、sim-setup 和 02:00 的 PG 备份不能同时进行（它们原本就分开做）；迁移前先 `systemctl stop nexloop-world-glossary.timer` 并等正在跑的那一次结束，迁移后再 start。这样即使人工会话开 2 个连接，峰值也是 6 − 1 + 2 = 7。
- **数据库也有硬上限**：除运维保留外，`max_connections` 80 − superuser 保留 3 − 应用 60 − 模拟 10 = 7，超出时新连接被 PG 拒绝，不会挤占应用的 60 个。

### 8.3 监测

- **不再列为期望容器**：它不常驻，不在 host-health 的 `containers` 里，也不在连接预算和 deploy-gate 的常驻服务里。
- **按 timer 的新鲜度监测**：APP_HOST 的 host-health 配置在 `timers` 里加上 `world-glossary`（`nexloop-world-glossary.timer` / `.service`）。用的是 host-health 现有的口径（`docs/implementation/host-health.md` §4.5）：
  - 只认**定时**轮次的成功，手工补跑不刷新新鲜度；
  - **fail closed**：从未见过定时成功时，从这个 timer 第一次被列为期望起计时，到阈值就报警，不会静默通过；
  - 告警规则 v22 新增 `timer_stale_world_glossary`，超过 1800 秒（3 个周期）报 warning；
  - 定时轮次失败（例如端口拒绝、数据库不可用）由现有的 `timer_scheduled_failure` 报出。
- **哪些不算失败**：模型调用失败（429、超时等）、当天名额用满，都只记在任务和用量里，进程仍正常退出。它们不是服务故障，不会让 timer 显得失败。
