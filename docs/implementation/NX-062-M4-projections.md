# NX-062 M4 准备：首批机器读取动词的字段投影与测试数据（草案，不含迁移）

- **状态**：草案。调度员 2026-11-09 要求，在授权方式的结论（机器读取授权落在哪里，由 L6、负责人或 ADR 决定）出来之前，只准备与授权方式无关的部分：投影清单和测试数据。**本文不含迁移，也不改变任何行为。**
- **依据**：设计稿 §6 M4、ADR-041 §2.5 与 Q-9（默认不返回会话正文、消息正文和联系方式；需要时由负责人按能力逐条加入投影并留审计）、威胁评审 T-12（投影在 SQL 读取函数里执行；擦除和保留与工作台读取一致，复用 NX-054 判定）。
- **字段来源**：`packages/contracts` 中 `workbench-object-page`、`workbench-object-values`、`workbench-object-neighbors`、`commitment-view` 的现有结构，以及 `deploy/ontology/business-object-types.v1.json` 的类型定义（以 main 6fd1bda9 为准）。

## 1. 通用规则

1. **投影是白名单**。`control.nexloop_machine_read_verbs.projection` 写出每个动词返回的字段路径，没写的一律不返回。新增字段不登记就不出现，测试会断言返回的字段集合与登记的投影完全相等。
2. **对象属性再加一层类型白名单**：只有登记了（type_name, property_name）的属性才会出现在 `properties` 中。首批只开放两类结构化业务类型：Commitment 和 CommercialRecord。**Consumer、Conversation、Message 以及所有运行时类型首批都不开放**：对象读取对它们返回 404，neighbors 里只计入 `invisible`。
3. **永不进入默认投影**：正文、消息内容、自由文本备注、联系方式（手机号、邮箱、外部账号）、外部订单号、指向消息原文的引用、内部执行主体，以及 json 证据载荷。负责人以后要放开其中某一项，只能为某个能力单独追加一条投影修订，并且留审计（Q-9）。
4. 字段被省略时，响应里不出现这个字段名，也不给 withheld 占位，以免泄露字段存在。工作台的 `status/reason` 结构不用于机器读取。
5. **擦除与保留**：完全复用工作台读取的 NX-054 判定。已擦除或正在擦除的 Consumer，其相关对象返回 404；legal hold 和保留期的结果与工作台一致。不另写判定。
6. **不存在、不可见、他人租户一律返回 404**，响应正文相同（T-11）。
7. **上限**：list 每页最多 50 条，neighbors 深度为 1，每组最多 20 条。

## 2. 动词与投影

### object.get：读取一个对象

| 字段 | 返回 | 说明 |
|---|---|---|
| type_name、object_id、revision、updated_at | 是 | 元数据 |
| schema_version | 是 | |
| properties.<登记的属性>.value、value_type | 是 | 只限下表登记的属性 |
| properties.*.status / reason / truncated | 否 | 工作台专用 |

### object.list：按类型分页列出

| 字段 | 返回 | 说明 |
|---|---|---|
| type_name、items[].object_id、items[].revision、items[].updated_at | 是 | |
| items[].properties | 是 | 同 object.get 的属性白名单 |
| next_after | 是 | 不透明游标 |
| total、invisible | **否** | L6 N2：两者都会泄露看不到的对象数量；一页可能少于扫描数 |

### object.neighbors：一跳关系

| 字段 | 返回 | 说明 |
|---|---|---|
| type_name、object_id、depth（固定为 1）、group_limit | 是 | |
| groups[].direction、via、type_name | 是 | 关系类型名属于元数据 |
| groups[].items[].type_name、object_id | 是 | 仅限已登记类型 |
| groups[].invisible、collapsed | 是 | 未登记类型的邻居只计入 invisible |

### commitment.list：承诺列表

| 字段 | 返回 | 说明 |
|---|---|---|
| commitment_id、revision、properties.status、origin、registered_at | 是 | |
| source_available | 是 | 布尔值 |
| consumer_id | 是 | 内部 ID（hex），不是联系方式；对已擦除的 Consumer 行按 NX-054 处理 |
| quote、quote_status | **否** | 引用了消费者或企业的原话 |
| consumer_display（含 name） | **否** | 姓名 |
| evidence.*.ref | **否** | 指向消息或外部事件 |
| evidence.*.kind、occurred_at | 是 | 只给类别和时间 |
| events[].kind、recorded_at | 是 | |
| events[].detail、principal_id | **否** | 自由文本与内部主体 |
| exceptions[].reason、raised_at | 是 | reason 是固定码 |
| exceptions[].detail | **否** | 自由文本 |

## 3. 属性白名单（object.get 和 object.list）

| 类型 | 返回的属性 | 不返回的属性（原因） |
|---|---|---|
| Commitment | status, due_at, due_precision, promised_at, late, made_to, related_goal_ref, supersedes, fulfillment_basis | condition（自由文本）、content_ref（指向消息原文）、made_by（内部执行主体、Run 与意图）、fulfillment_evidence（json 证据载荷） |
| CommercialRecord | kind, currency, occurred_at, status, status_at, amount_minor, refunded_minor, verification_state, link_state, data_mode, connector_id, event_count, late_event_count | external_id（外部订单号，可识别个人）、consumer_ref（指向消费者）、related_record_id、last_event_ref、provider_sequence（内部事件引用） |
| 其他类型 | — | 首批不开放 |

> 需要负责人确认的两项：`Commitment.made_to` 和 `commitment.list.consumer_id` 是消费者的内部 ID，不是联系方式，但能把多条记录关联到同一个人。推荐**返回**：没有它，企业 Agent 无法按客户归并承诺；它不能用来联系任何人，擦除后也不再出现。如果负责人选择不返回，这两项就从白名单里删掉。

## 4. 测试数据准备

合成数据，全部在 test world 或测试 PG 中生成；断言统一用不像十六进制的标记值。

| # | 数据 | 用途 |
|---|---|---|
| D-1 | 租户 A：2 个 Commitment，`condition` 里写入标记“机器读取不应出现-条件文本”，`content_ref` 指向一条正文含标记“机器读取不应出现-消息正文”的 Message | 投影排除，逐字段断言标记不出现 |
| D-2 | 租户 A：1 个 CommercialRecord，`external_id` 写入标记“机器读取不应出现-外部单号”，`consumer_ref` 指向 D-4 的 Consumer | 排除外部单号和消费者引用 |
| D-3 | 租户 A：1 个 Consumer，带联系方式属性（标记“机器读取不应出现-联系方式”），与 D-1 的 Commitment 有关系 | Consumer 不开放：get 返回 404；在 neighbors 里只计入 invisible |
| D-4 | 租户 A：1 个已完成擦除的 Consumer 及其 Commitment | 结果与工作台读取一致（404 或擦除后的表现），不出现擦除前的值 |
| D-5 | 租户 A：1 个处于 legal hold 的对象 | 与工作台读取一致 |
| D-6 | 租户 B：1 个 Commitment | 租户 A 的密钥读它返回 404，且与读不存在的 ID 时响应正文相同 |
| D-7 | 一份投影快照：每个动词的字段集合（JSON） | “返回字段集合等于登记投影”的期望值；新增字段而不登记，测试失败 |
| D-8 | 51 个 Commitment | 分页上限与游标 |

变异检查（实现时）：
- 把投影改为 `select *`，D-7 断言变红；
- 删掉擦除判定，D-4 变红；
- 去掉类型白名单，D-3 变红；
- 去掉租户过滤，D-6 变红。

## 5. 与授权方式无关、可以先做的部分

- 上述投影清单（本文）；
- 测试数据构造函数（放进 `tests/support/`，实现 M4 时一起提交）；
- 投影快照 D-7。

授权的判定（M4 方案用 NexLoop 授权表，还是等 M5 的机器事实快照）只影响读取函数里“这个主体能否用这个动词”这一项，不影响上面任何一项。
