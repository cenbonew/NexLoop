# NX-029 保留、删除与导出闭环：设计稿

状态：**设计稿，未实现**。分支 `nx029-design`，从 main `151d7ef` 切出，合入了 `nx027-impl` `40ab1bd`（以便引用 NX-027 的保留期配置与假名化端口）。NX-029 依赖 NX-021、NX-028（`planning/tasks.json:2208-2224`）；NX-028 切片 2/3（`nx028-s23`，governed entry 注册表 0150）尚在集成，本稿按调度员转发的注册表签名（NX-028 设计稿 §15.2a）设计入口，实现须等 NX-028 合入。

## 0. 依据

- 规划：NX-029「实现保留/删除/导出闭环」，S4，模块 M01、M13、M23，交付物“Artifact/Pi/索引/备份删除水位与权限测试”（`planning/tasks.json:2208-2224`；`15_BACKLOG_AND_MILESTONES.md:56`）。
- 验收：AT-026（更正和删除同步到原文、摘要、向量、Artifact、Run 保留策略，M13 S4）、AT-059（旧备份含已删除顾客，先重放删除列表，不复活可联系状态，M13 S4）、AT-003（无敏感属性权限时，检索/详情/**导出**均不可见，M01 S1）（`planning/acceptance-tests.json:49,442,895`；`14_TEST_ACCEPTANCE.md:15,38,71`）。相邻：AT-050 恢复演练（NX-035）、AT-025（检索隔离，已通过）。
- 产品与数据：
  - 默认保留期（产品默认，不是法定期限）：原文 90 天、上下文快照 30 天、普通日志 14 天、指标汇总 1 年；企业可另行配置（`03_DOMAIN_DATA_MODEL.md:96`、`:37` 的 `tenant_settings.retention_policy`；`13_SECURITY_PRIVACY_AND_OPEN_SOURCE.md:31`）。
  - 删除顾客：枚举证据、Claim、形式对象、关系、摘要、向量、Artifact、可定位的 Pi Run 文件、备份；先阻断检索和新联系，再异步删除或去标识，最后写不含原文的完成凭据；恢复后先重放删除列表（`03_…:98`）。规范流程（`13_…:33`）：①阻断新联系与检索 ②建删除清单 ③停止或对账相关 Run 与 Action ④清除原文与派生物 ⑤处理 Artifact 与 Pi 文件 ⑥刷新索引 ⑦写不含原文的审计 ⑧备份到期后清除；法定或合同例外须由有权限的人登记，**Agent 不能自行创造**。
  - 事件不原地删除，可清除个人内容并留下不含原文的审计 tombstone；“append-only 不意味着永久保留所有原文”（`03_…:29`；`01_PRD.md:314`）。
  - 派生层（chunk、向量、摘要、指标投影）随版本或删除失效（`03_…:19,48`；`01_PRD.md:212,216`）。
  - 运维表：`audit_links`、`retention_jobs`、`deletion_requests`、`release_records`（`03_…:58`）；API：`POST /data-exports`、`POST /deletion-requests`、`GET /deletion-requests/{id}`，单独授权、异步、留审计（`09_API_AND_EVENT_CONTRACTS.md:37`）。
  - 顾客在 WebChat 中提出删除或更正，系统不能当作普通聊天忽略（`10_UX_AND_WORKBENCH.md:42`）；设置与治理页显示保留期并受理导出与删除请求（`10_…:23`）。
  - 备份清单记录删除水位；恢复时先应用删除与撤销列表（`17_OPERATIONS_RUNBOOK.md:27,40`）；破坏性命令默认 dry-run，CI 不调用真实删除 API（`12_LOCAL_CI_AND_RELEASE.md:68`）。
- ADR：ADR-003（Valkey 可丢失）、ADR-005（Pi SQLite 例外，须有属主、备份与删除）、ADR-015（本地 Artifact 须授权访问、备份与删除）、ADR-018（真实恢复先隔离对账）、ADR-020（消息删除后派生 READ 立即失效）、ADR-021（v0.1 不撤回已发出的消息）、ADR-025（工作台成员每次读取留只追加审计，未定保留期与删除处理）。
- 交接给 NX-029 的事项：NX-023-B（模型请求与提示原文进入保留、删除、导出范围，保留期不得长于对应 Message，不得直接 SQL 删除，`NX-023-B.md:41`）；NX-026 D7（来源消息删除后承诺不再给原文，`NX-026-design.md:90-94,359`）；NX-027 D6（假名化端口、`financial_retention_days` 默认 365、到期只留 tombstone，`NX-027-design.md:257-280`；`NX-027-implementation.md:29,59`）；NX-028（设置页只读显示保留期，删除与保留请求属 NX-029，`NX-028-design.md:73`）；NX-007 Artifact 保留（runtime 属主终态保留与删除传播未做，`final-artifact-orphan-progress.md:29`）。

## 1. 目标与不做

**目标**：
1. 每类数据有明确的保留期、到期动作与执行者，配置版本化。
2. 删除一个 Consumer 时，按 §4 的清单把个人内容从原文、Claim、形式对象、索引、上下文与模型请求记录、承诺、商业记录、Artifact、Pi 文件中清除或假名化；顺序为先阻断、后清除，最后留不含原文的凭据。
3. 更正与删除传播到派生物（§5）。
4. 负责人发起、受治理、留审计的导出（§6）。
5. 到期清理有单一执行者、可重入、留 tombstone（§7）。
6. 备份与恢复的删除水位（§9）。

**不做**：法定保留期的判断（由企业登记例外，系统只执行）；v0.1 不撤回已发出的消息（ADR-021）；跨租户批量清退（另一个运维任务）；备份系统本身（NX-035），本稿只定义删除水位与重放接口；指标告警与普通日志脱敏（NX-030）。

## 2. 现状核对（代码事实）

### 2.1 EIOS 已有删除机制

| 机制 | 位置 | 能否直接用 |
|---|---|---|
| 授权操作枚举含 `DELETE`、`EXPORT`；属性操作含 `EXPORT` | `eios/authz/operations.py:10,16`；`eios/authz/property_security.py:392` | **可用**：删除与导出的授权事实沿用这两个操作，不新造 |
| `ontology.objects` 无删除标记；应用与运行时角色对它只有 select/insert/update，无 DELETE；`ontology.relations`、`ontology.events` 外键无 `ON DELETE` | `0001_durable_core.sql:147-158,171-188,241-245` | 硬删对象须先处理事件与关系；只能由 owner 内部函数执行 |
| 内存版 store 的 `delete_objects`（硬删 + 30 天墓碑）、`prune_*` | `eios/ontology/store.py:35,80-98,1289-1417` | 不可用：NexLoop 不走内存 store |
| Postgres 适配器调用 `ontology.app_object_retire`，依赖 `ontology.object_tombstones` | `eios/adapters/postgres/ontology_store.py:1733-1760` | **不可用**：本仓库迁移中没有这两个对象；`nexloop_eios` 不导入该适配器 |
| 上游冻结提交 `1e363db` 中的同名实现 | 只读核对：`0129_tos_incremental_sync.sql:11-35`（墓碑供 TOS 增量同步，30 天）、`0211_teaching_assignment_retire.sql:99-197`（类型白名单 `TennisTeachingAssignment`） | **不移植**：属于 TOS/网球领域代码（工程契约禁止拷贝）。只借鉴两点做法：硬删前先落墓碑；类型白名单 fail-closed |
| 审计与作业表 `runtime.audit_events`、`job_events`、`idempotency_records` 只能插入；无 TTL | `0001:111-120,242,252` | 到期清理须走 owner 内部函数 |
| 本地 Artifact：`retention_until` 必填，pending→available→deleting→deleted，删除前检查期限，`deleted` 行即墓碑；清理候选、孤儿清扫 | `0005:156-157`；`0006:11-64`；`0013:12`；`0014:5-22`；`0017`；`0018`；`postgres_artifacts.py:123-257` | **可用**：Artifact 层的到期删除机制已完整，NX-029 只需补调度（`backend.collect_expired_artifacts` 目前无调用方，`backend.py:318`）和 Consumer 删除时的提前到期 |

### 2.2 NexLoop 侧已有的删除与更正路径

- **消息删除**：`ontology.objects` 删除后，NX-026 触发器为相关承诺写 `source_deleted` 异常（`0111:291-306`），NX-021 的 feed 触发器排队召回索引删除（`0093:38-52`）。**生产代码中没有删除 Message 的路径**，只有测试用管理员 SQL 删除（`tests/test_commitment_guards_pg.py:131-140`）。
- **Claim 更正**：`resolution_state='superseded'`（`0070:157-162`）、`corrects_claim_id`（`0069:6`）、更正生效顺序（`0115`）；承诺收到 `source_superseded`（`0111:276-289`）。Claim 表本身无更新与删除触发器，存有 `quote` 与 `source_message_id`（`0066:21-43`）。
- **召回索引**：`control.nexloop_recall_remove_source` 硬删索引行（`0067:383-392`）；索引工作者在对象消失时返回 `removed`（`recall_index_worker.py:35-42`）。已被删除或撤销的实例不会被召回（AT-025 已通过）。
- **商业记录（NX-027 D6）**：`runtime.nexloop_commercial_pseudonymize_consumer`（`0130:505-525`）随机假名、删除关联、重推导记录；只有 owner 可调，目前无调用方。`financial_retention_days` 校验 1–36500，默认 365，**尚无执行者**。CommercialRecord 对象守卫写明“只能随保留期离开（NX-029）”（`0130:276`）。
- **Valkey**：只存不透明的唤醒键，TTL 1–60 秒，不存事实（`valkey_wakeup.py:1-3,107-109`）。
- **工作台**：只有 GET 读接口（`workbench_http.py:81-176`），无删除与导出入口。
- **导出**：无任何导出代码（上下文包中的 `local.json-export` 是一个服务 Offering，不是数据导出）。
- **读取审计（ADR-025）**：在 NX-028 切片 1b（`nx028-s1b`，迁移 0145）中实现，尚未合入；本稿按 ADR-025 §2.3 的字段设计其保留。

### 2.3 关键障碍：只追加触发器

大量表用触发器禁止 UPDATE 与 DELETE（`control.nexloop_nx022_append_only`，`0068:145`；以及 0089、0092、0106、0109、0111、0114、0130–0133 的同类触发器），其中含个人内容的有：

| 表 | 个人内容 | 位置 |
|---|---|---|
| `runtime.nexloop_model_request_prompts` | 完整提示原文 `request_text` | `0092:55` |
| `runtime.nexloop_context_packs`、`nexloop_context_sources` | 上下文包正文与来源内容 | `0089:56-58` |
| `runtime.nexloop_plans`、`plan_steps`、`plan_outcomes`、`strategies` | 计划文本、结论理由 | `0106:61-67` |
| `control.nexloop_contact_restrictions`、`refusal_hits` | `matched_text` | `0109:96-121` |
| `runtime.nexloop_commitment_events/evidence/exceptions` | 引用与详情 | `0111:107-109` |
| `runtime.nexloop_message_provider_facts`、`reply_links` | 渠道标识 | `0114:64-69` |
| `runtime.nexloop_commercial_*`、`runtime.nexloop_cost_entries`、`control.nexloop_metric_cohorts` | 客户引用、consumer_ref | `0130:145-152`；`0132:53`；`0131:197-217` |

另有无触发器但含原文的表：`runtime.nexloop_conversation_messages.record`、inbox/outbox（`0046`）、`runtime.jobs.normalized_input`（含消息 `input`）、`runtime.nexloop_effect_intents.frozen_request`（含外发消息正文）、`ontology.nexloop_claims.quote`、`ontology.nexloop_recall_entries.body`、`runtime.nexloop_native_web_events`。

**设计结论（§8）**：不放宽只追加约束，也不给任何应用角色 DELETE。新增一条 owner 内部的“受治理清除”路径：只追加触发器只对“当前事务中由清除执行函数登记过的表与列”放行，且只允许把内容列改写为清除值，不允许删除行或改写其它列。

## 3. 保留期总表

默认值写入新的版本化配置 `deploy/configuration/retention.v1.json`（仿 NX-026/027 的设置文件：迁移逐字种入 `control.nexloop_retention_settings`，企业以新版本调整，`load_settings` 有界校验）。“到期动作”中的“清除”指把原文与个人内容列改写为清除值并保留行与摘要外的结构（§8）；“删除”指整行删除并留 tombstone。

| 数据类 | 存储 | 默认保留 | 起算点 | 到期动作 | 依据 / 决策 |
|---|---|---|---|---|---|
| 消息原文 | `runtime.nexloop_conversation_messages.record` 中的 body、Message 对象 `body`、inbox/outbox、`native_web_events`、`message_provider_facts` 原文字段 | 90 天 | 消息受理时间 | 清除正文；行、序号、摘要哈希、时间、方向保留 | `03:96` |
| 原始商业事件 | `runtime.nexloop_commercial_events`、`receipts` | 跟随消息原文（90 天） | 接收时间 | 清除 `customer_ref` 与原始载荷，保留金额、币种、事件类型、处理结果 | NX-027 D6 |
| Claim 原文引用 | `ontology.nexloop_claims.quote`、`subject_text` | 跟随来源消息 | 来源消息 | 清除 `quote`；结构化值、更正链、epistemic 状态保留 | D2 |
| Claim 结构 | `ontology.nexloop_claims` 其余列 | 不随时间到期；superseded 后 1 年 | superseded 时间 | 删除行，留 tombstone | D2 |
| 形式对象 | Consumer 属性、关系、RelationshipAssessment 等 | 不随时间到期（持续事实） | — | 只随 Consumer 删除 | `03:96`（目标、本体、关系、承诺持续存在） |
| 召回索引 | `ontology.nexloop_recall_entries` | 派生，跟随来源 | — | 来源清除或删除时同步移除 | `03:48`；`0067:383` |
| 上下文包与来源 | `runtime.nexloop_context_packs/sources`、上下文 Artifact | 30 天 | Run 创建 | 清除正文与来源内容；保留 ID、版本、哈希、读取决定引用 | `03:96`；AT-027 |
| 模型请求提示原文 | `runtime.nexloop_model_request_prompts.request_text` | 30 天，且不长于对应消息 | 请求时间 | 清除 `request_text` | `NX-023-B.md:41` |
| 模型请求清单 | `runtime.nexloop_model_requests`（摘要、用量、费用、币种） | 1 年 | 请求时间 | 删除行，留 tombstone（费用条目另计） | D3 |
| 计划与复评 | `runtime.nexloop_plans`、`plan_steps`、`plan_outcomes`、`strategies` | 关闭后 1 年；文本字段跟随上下文 30 天 | 计划关闭 | 清除自由文本，保留状态链；到期删除 | D3 |
| 承诺 | Commitment 对象、`nexloop_commitments`、事件、证据、异常 | 关闭（fulfilled/cancelled/breached 结案）后 1 年；引用原文跟随来源消息 | 关闭时间 | 清除引用；到期删除对象与账本，留 tombstone | D4 |
| 商业记录 | CommercialRecord、`nexloop_commercial_records/exceptions/wakes/bindings` | `financial_retention_days`（默认 365） | 记录最后状态时间 | 删除，留不含金额与原始标识的 tombstone | NX-027 D6 |
| 费用条目与结算 | `runtime.nexloop_cost_entries`、`budget_settlements`、`control.nexloop_budget_consumption` | `financial_retention_days` | 发生时间 | 删除，留 tombstone | NX-027 D6 |
| 指标观察与 cohort | `control.nexloop_metric_observations`、`metric_cohorts` | 1 年 | 观察时间 | 删除；KR 计算结果按需重算 | `03:96`；NX-027 D6 |
| 外发与 effect 账本 | `runtime.nexloop_effect_intents.frozen_request` 中的正文、`outbound_messages` | 正文跟随消息原文（90 天）；账本结构 1 年 | 意图创建 | 清除正文；保留状态、回执、摘要 | ADR-021；`06:92` |
| 联系限制 | `control.nexloop_contact_restrictions`、`refusal_hits`、`reply_escalations` | 限制生效期间不清除；解除后 `matched_text` 跟随消息原文 | 解除时间 | 清除 `matched_text`；限制本身见 §4.3 | D5 |
| 运行时作业 | `runtime.jobs.normalized_input`、`job_events`、`idempotency_records`、inbox/outbox | 终态后 30 天 | 终态时间 | 清除 `normalized_input` 中的 `input` 与上下文引用；终态后 1 年删除 | `06:92` |
| Pi Run 文件 | Agent Host 运行时目录 `<runtime-root>/<run_id>/runtime.sqlite` | 终态后 30 天 | Run 终态 | 整个目录删除，留 tombstone | ADR-005；`06:92` |
| 本地 Artifact | `runtime.nexloop_local_artifacts` 与文件 | 各自 `retention_until`（上下文 Artifact 现为上下文有效期） | 创建 | 已有删除流程（§2.1） | ADR-015 |
| 读取审计（ADR-025） | NX-028 1b 的读取审计表 | 1 年 | 读取时间 | 删除，不留单条 tombstone，只留按日计数 | D6 |
| 治理审计 | `control.nexloop_control_events`、`runtime.audit_events`、`job_events`、删除与导出请求、清除 tombstone | 1 年；删除凭据与 tombstone 至少保留到最后一份含该数据的备份过期 | 事件时间 | 删除 | D6；`17:27` |
| 普通进程日志 | 进程 stdout/stderr | 14 天 | — | 由部署日志轮转执行（NX-030 负责内容脱敏） | `03:96` |
| Valkey | 唤醒键 | 1–60 秒 TTL | — | 自行过期；删除流程不需要处理 | ADR-003；`valkey_wakeup.py` |
| 备份 | DATA_HOST 加密备份 | 由备份策略定（NX-035） | — | 轮换；恢复时重放删除列表（§9） | `17:25-40` |

约束：
- 任何派生数据的保留期不得长于其来源（提示原文 ≤ 消息原文，NX-023-B）；`load_settings` 校验这一偏序，违反则拒绝新版本。
- `financial_retention_days` 短于某个已批准指标的成熟窗口时，doctor 给出告警（NX-027 遗留项，在此实现）。
- 法定或合同保留例外由有权限的人类登记为 **保留例外**（§4.4），Agent 与服务不能登记。

## 4. 删除 Consumer 的传播链

### 4.1 入口与状态机

**入口**：
- 负责人在工作台发起：人类 Action `Consumer.erase`（经 NX-028 governed entry 注册表，subject_rule `human`）。
- 顾客在 WebChat 中提出删除（`10:42`）：NX-019 抽取为 `data_rights_request` 类 Claim，**只创建待处理的删除请求**并提示负责人，不自动执行（D1）。

**请求表** `control.nexloop_deletion_requests`：`request_id`（= governed intent）、tenant、world、`consumer_id`、`requested_by`（人类主体）、`source`（`owner` | `consumer_request`）、`state`、`hold_refs`、`watermark`（全局递增序号，§9）、时间戳。状态单调：

```
requested → blocking → settling → purging → completed
                         │            │
                         └→ waiting_unknown_effects（有 unknown 外部结果时，先对账，不盲目重试）
任一阶段 → failed_retryable（自动重试）；purging 中遇保留例外 → completed_with_holds
```

### 4.2 各阶段动作

1. **blocking（同一事务内完成）**：
   - 写一条联系限制（kind `erasure`，范围全部渠道）；NXC 派发检查立即拒绝对该 Consumer 的新联系（复用 0109/0110 的派发时检查，不新增检查点）。
   - 写 Consumer 的 `erasure_state='erasing'` 标记（新增受保护属性，经 Consumer.edit 守卫只允许删除流程写）；召回、上下文组装、工作台读、导出在 SQL 读断言中对标记为 erasing 的 Consumer 一律拒绝（与 ADR-020“删除后派生 READ 立即失效”一致）。
   - 关闭该 Consumer 的所有 active 计划（计划事件 `closed`，原因 `consumer_erasure`）；撤销待处理的 work feed 项（召回、Claim 匹配、复评、回复兜底、承诺监控中属于该 Consumer 的）。
2. **settling**：
   - 未派发的 effect intent：取消（docs/06 §9 与 ADR-021 只允许取消尚未提交的意图；现有代码中没有单独的意图取消函数，由本任务按派发控制的方式补：在派发前的控制检查中对 erasing 的 Consumer 拒绝派发，意图转为 failed 并记原因）。
   - 已派发、结果未知的 intent：进入 `waiting_unknown_effects`，等待回执对账（0065 `nexloop.service.receipt_reconcile`）给出确定结果后继续；不盲目重试。
   - 正在运行的 Run：通过 Host 已有的 `POST /internal/v1/runs/cancel`（`apps/agent-host/src/main.ts:46`，激活操作 `cancel` 见 0037）取消，等待终态。
3. **purging**：按 §4.3 的清单分批执行，每项幂等、单独事务、可重入；每项完成写一条 tombstone（§7.2）。
4. **completed**：写删除凭据（不含原文，只有请求号、Consumer ID 的摘要、各类清除计数、保留例外引用、水位、完成时间）；Consumer 对象删除，留 tombstone。

### 4.3 清单（数据类 → 动作）

| 数据类 | 动作 | 说明 |
|---|---|---|
| 会话、消息原文（stream、Message 对象、inbox/outbox、provider facts、native web events） | 清除正文与渠道标识；删除 Message 对象 | 删除 Message 对象会触发已有的 NX-026 `source_deleted` 与 NX-021 召回删除 |
| Claim | 清除 `quote`、`subject_text`、`value` 中的自由文本；结构化值删除；行留 tombstone | 更正链保留 ID 以便审计 |
| 形式对象（Consumer、关系、RelationshipAssessment、ConsumerRoleLink 等） | 删除对象（先删事件与关系）；留 tombstone | 需要放开 0058 RelationshipAssessment 的删除守卫，只对删除流程放行 |
| 召回索引 | 由已有 feed 触发器与索引工作者移除；删除流程在 purging 末尾校验该 Consumer 相关 `source_key` 计数为 0 | `0067:383`；AT-025 |
| 上下文包、来源、提示原文、模型请求 | 清除正文与提示原文；模型请求清单行保留（无原文）至其自身保留期 | NX-023-B |
| 计划、策略、复评结论 | 清除自由文本；行保留至其保留期 | |
| 承诺 | 清除引用与原文相关字段；对象与账本保留至保留期，`made_to` 改为删除凭据引用 | D4 |
| 商业记录 | 调用 `runtime.nexloop_commercial_pseudonymize_consumer`（NX-027 D6） | 金额、币种、时间、更正链保留 |
| 费用条目、指标 cohort | `consumer_ref`、`member_ref` 替换为同一随机假名 | 与商业假名一致，不保存对应关系 |
| 联系限制 | 见 D5：保留一条不含身份的“已删除”限制，用于防止同一外部标识被重新导入后再次联系 | |
| 外发与 effect 账本 | 清除 `frozen_request` 中的正文；结构保留 | |
| 运行时作业 | 清除 `normalized_input.input` 与上下文引用 | |
| Pi Run 文件 | 删除该 Consumer 相关 Run 的运行时目录（终态后），留 tombstone | Run 与 Consumer 的对应来自 `authz.nexloop_role_run_bindings` 和 run_command 的 `consumer_ref` |
| 本地 Artifact | 把相关 Artifact 的 `retention_until` 提前到当前，走已有删除流程 | `0014`、`postgres_artifacts.py` |
| 读取审计（ADR-025） | 保留；对象引用是 ID，不含原文（D6） | |
| Valkey | 无需处理 | ADR-003 |
| 备份 | 不改写；水位写入删除列表，恢复时重放（§9） | AT-059 |

### 4.4 保留例外

- 表 `control.nexloop_retention_holds`：hold_id、tenant、world、范围（Consumer 或数据类）、法律或合同依据文本（由人填写）、登记人、有效期、撤销。
- 只能由人类 Action `Retention.hold`（注册表 subject_rule `human`）登记；服务、Agent、Run 凭据在 SQL 中先被身份检查拒绝（照 0095/0134 的模式）。
- 删除流程遇到被保留的数据类时跳过并在凭据中记 `completed_with_holds`；保留例外到期后，到期清理执行者补做。

## 5. 更正与删除对派生物的传播

| 事件 | 派生物 | 现有机制 | NX-029 补充 |
|---|---|---|---|
| Claim 被更正（superseded） | 形式事实、关系判断、承诺、召回索引 | 0070/0115 更正链；0111 `source_superseded`；关系判断按证据重算（`01_PRD:146`） | 被取代的 Claim 的 `quote` 在其保留期到期时清除；召回索引只收当前版本（已满足） |
| 消息被删除（单条，人类 Action `Message.erase`） | Claim 引用、召回、承诺、上下文来源 | 0111 `source_deleted`；0093 召回删除 | 新增人类 Action 并补齐：清除该消息作为来源的 Claim `quote`、上下文来源内容、提示原文中的副本无法定位到单条消息时，按 Run 整体清除提示原文（保守） |
| 对象属性更正 | 召回索引 | 0093 feed 触发器重推导 | 无 |
| 来源到期清除 | 上下文来源、提示原文 | 无 | 上下文与提示的保留期不长于来源（§3 约束），所以到期顺序天然先于或同于来源 |
| Consumer 删除 | 全部 | 部分（召回、承诺、商业假名化端口） | §4 |

原则：派生物的完整性校验（如上下文来源的内容哈希再校验）对已清除的行必须识别 `erased` 状态并拒绝复用，而不是报篡改。

## 6. 导出

- **入口**：人类 Action `Consumer.export`（注册表 subject_rule `human`，capability `consumer.export`）。只有负责人和被授予该 Action 的成员可发起；授权事实用 EIOS 已有的 `Operation.EXPORT`。顾客自助导出见 D7。
- **内容**：按发起人当前的读取权限裁剪（AT-003：无敏感属性权限则不导出该字段与证据）。消息原文只在发起人按 ADR-025 有读取权时包含。商业记录、费用、承诺按 NX-027/026 的读投影导出。每个条目带 world 与 data_mode。
- **执行**：导出工作者（与 §7 的执行者同一服务，单独 feed `data-export`）生成 JSON，写入本地 Artifact（`retention_until` 默认 7 天，可配置），文件名与内容不出现在日志。
- **下载**：`GET /api/v1/data-exports/{export_id}`，同源人类会话，只有发起人可下载，每次下载写一条审计；`POST /api/v1/data-exports` 由 NX-028 的 `POST /api/v1/workbench/actions/{operation}` 承担（不另开写入口）。
- **审计**：导出请求、生成、每次下载都写入治理审计；审计不含导出内容。
- **租户级审计导出**（`01_PRD:310` M23）：只导出审计记录（不含原文），归 NX-030，本稿只约定格式与本稿的审计表一致。

## 7. 到期清理的执行者与 tombstone

### 7.1 执行者

- 新服务主体 `retention_keeper`（`nexloop_domain_worker`），后台入口 `nexloop-retention-keeper`（仿 `commercial-recorder`）。
- 它只能调用签名端口 `nexloop.retention.execute`；端口调用 owner 内部函数，逐项执行清除或删除。服务本身没有任何表的 DELETE 权限，也看不到原文。
- 工作来源：
  - feed `retention-sweep`：按数据类与租户分片的到期扫描项，由定时唤醒写入（每个数据类每天一次，可配置）；
  - feed `erasure`：删除请求的清单项（§4.3）；
  - feed `data-export`：导出（§6）。
- 每批有上限（配置），单项单事务，失败按 `retry_delay` 退避，超过次数进入死信并在工作台显示为异常。
- Artifact 到期删除继续用已有的 `collect_expired_artifacts`，由该服务定时调用（补上目前缺失的调度）。
- Pi Run 文件：运行时目录在 Agent Host 主机上，数据库服务无法直接删除。由 Agent Host 提供内部接口 `POST /internal/v1/runs/purge`（只接受已终态且到期或在删除清单中的 Run，删除目录后回报），执行者按清单调用并记录结果；Host 不可达时项目保持待处理。

### 7.2 tombstone 形状

`runtime.nexloop_erasure_tombstones`（只追加，FORCE RLS）：

| 列 | 说明 |
|---|---|
| tenant_id, world | |
| tombstone_id | 确定性：`sha256(request_or_sweep_id || item_class || subject_key)`，重复执行幂等 |
| reason | `consumer_erasure` / `retention_expiry` / `message_erasure` / `correction_expiry` |
| request_ref | 删除请求号或到期扫描批次号 |
| item_class | §3 的数据类代码 |
| subject_kind, subject_key | 表名与主键的结构化引用（对象 ID、Run ID、条目 ID）；**不含**原文、金额、外部客户标识、联系方式 |
| action | `redacted` / `deleted` / `pseudonymized` / `skipped_hold` |
| counts | 行数等计数 |
| watermark | 删除水位（§9） |
| executed_at, executed_by | 执行时间与执行主体 |

NX-027 D6 的商业 tombstone 落在同一张表（item_class `commercial_record`），不含金额与原始标识。

## 8. 只追加表的受治理清除

- 新表 `runtime.nexloop_erasure_scope`（owner 独占，应用角色无任何权限）：`txid`、`table_name`、`columns[]`、`request_ref`。清除执行函数在事务开始时登记本事务要改写的表与列。
- 改造各只追加触发器：仅当 `current_user='nexloop_owner'`、当前事务在 `nexloop_erasure_scope` 有对应表的登记、本次 UPDATE 只改登记的列且新值是规定的清除值（`null`、`''` 或 `{"erased":true}`）时放行；DELETE 只对登记了 `delete` 的表放行。其它情况仍然报错。
- 因为只有 owner 能写 `nexloop_erasure_scope`，应用角色无法自行打开这个口子；`set_config` 类会话变量不能替代这个表（任何角色都能设置会话变量）。
- 已发布迁移不改：触发器函数用“改名保留 + 新包装”替换（与 0094/0131 的做法一致）。
- 每个清除函数都有类型或表白名单，不在白名单中的直接拒绝（借鉴上游 `app_object_retire` 的 fail-closed）。

## 9. 备份与恢复的删除水位

- `control.nexloop_deletion_watermarks`：全局单调序号。每个删除请求进入 `blocking` 时取一个水位；每次到期扫描完成也推进水位。
- 删除列表 `control.nexloop_deletion_ledger`（只追加，不含原文）：水位、租户、world、Consumer ID 或数据类与截止时间。备份清单记录当时的最大水位（`17:27`）。
- 恢复流程（NX-035 实现，本稿定义接口）：恢复后服务对外之前，执行 `runtime.nexloop_replay_deletions(from_watermark)`，把备份水位之后的删除列表逐条重放；未重放完成时，API 与工作者启动检查失败（readiness 不就绪），保证不会复活可联系状态（AT-059）。
- 删除凭据与删除列表的保留期必须长于备份保留期（§3）。

## 10. 授权与入口汇总

| Action / 端口 | 主体 | 注册表 subject_rule | 说明 |
|---|---|---|---|
| `Consumer.erase` | 人类负责人 | human | 创建删除请求并完成 blocking |
| `Message.erase` | 人类负责人 | human | 单条消息删除（§5） |
| `Consumer.export` | 人类负责人或被授予的成员 | human | §6 |
| `Retention.hold` / `Retention.release_hold` | 人类负责人 | human | §4.4 |
| `Retention.configure` | 部署配置（configurator） | — | 新版本保留设置，与 NX-027 设置同法 |
| `nexloop.retention.execute` | `retention_keeper` 服务 | — | 签名端口，只执行已存在的清单项 |
| `GET /api/v1/deletion-requests/{id}`、`GET /api/v1/data-exports/{id}` | 同源人类会话 | — | 只读；照 NX-027 0134 的身份先于授权 |

所有人类 Action 通过 NX-028 的注册表接入：本任务在自己的迁移里建 handler 并插入注册表行，在 `goal_controls.CAPABILITIES` 加映射，不改入口函数。

## 11. 迁移形状（占位编号，合并时由调度员重排）

1. `nx029_retention_settings`：`control.nexloop_retention_settings`（种入 `retention.v1.json`）、保留例外、水位、删除列表、删除请求、tombstone、`nexloop_erasure_scope`。
2. `nx029_erasure_guards`：只追加触发器的改名保留 + 新包装；RelationshipAssessment、Commitment、CommercialRecord 守卫对删除流程放行；各清除函数（按数据类，白名单）。
3. `nx029_erasure_flow`：`Consumer.erase`、`Message.erase`、`Retention.hold` 的 handler 与注册表行；blocking 阶段的限制与标记；读断言对 erasing 的拒绝；feed `erasure`、`retention-sweep`、`data-export`（work feed CHECK 须取 NX-027 `commercial-record` 与 NX-028 `takeover-expiry` 的并集再加本任务的三个）；执行端口。
4. `nx029_export`：导出请求、导出 handler、读投影。
5. `nx029_replay`：删除列表重放函数与 readiness 检查。

## 12. 验收与测试清单

| AT | 本任务覆盖方式 |
|---|---|
| AT-026 | 删除一个有消息、Claim、关系、召回索引、上下文、模型请求、承诺、商业记录、Artifact、Pi 文件的 Consumer：各类数据按 §4.3 清除或假名化，召回与工作台搜索不可见，凭据不含原文；更正后旧版本不进入检索与上下文 |
| AT-059 | 备份（测试库快照）含已删除的 Consumer；恢复后不重放则 readiness 不就绪；重放后不可联系、不可检索 |
| AT-003（导出部分） | 无敏感属性权限的发起人，导出中不含该字段与证据；无消息读权限时不含原文 |
| AT-050（接口部分） | 水位与删除列表写入备份清单（与 NX-035 联合） |
| 其它 | 只追加表在无清除登记时仍拒绝 UPDATE/DELETE；服务与 Agent 不能发起删除、保留例外、导出；保留期偏序校验；到期扫描幂等与可重入；unknown 外部结果先对账；Pi 文件清除；doctor 告警 |

测试全部在一次性测试库与临时目录中进行；破坏性命令默认 dry-run（`12:68`），CI 不调用任何真实删除 API。

## 13. 与 NX-028、NX-030 的文件重叠

| 文件 / 对象 | NX-028 | NX-030（尚无设计稿，推测） | NX-029 预计改动 |
|---|---|---|---|
| `control.nexloop_governed_capabilities`（0150，`nx028-s23`） | 新建，15 行 | 可能无 | 插入 `consumer.erase`、`message.erase`、`consumer.export`、`retention.hold` 等行 |
| `authz.nexloop_goal_governed_action` | 0150 改名并包装 | — | **不改**，只插注册表行 |
| `runtime.nexloop_work_feed` CHECK 与 `work_feed.FEEDS` | 0152 加 `takeover-expiry`（未含 `commercial-record`） | 只读队列深度 | 取并集再加 `erasure`、`retention-sweep`、`data-export` |
| `goal_controls.CAPABILITIES` | s2/s3 加 5 项 | — | 加删除、导出、保留例外映射 |
| `deploy/configuration/business-actions.v1.json` | s2/s3 加行 | 可能加指标读 Action | 加 `Consumer.erase`、`Message.erase`、`Consumer.export`、`Retention.hold/release_hold` |
| `deploy/authorization/service-grants.v1.json` | s3 授权 | 可能加告警服务 | 加 `retention_keeper` 主体与授权 |
| `background_services.py`、`container_entrypoint.py`、`compose.test.yaml`、`pyproject.toml` | s3 加 takeover-expiry | 日志脱敏、指标 | 加 `retention-keeper` 入口与可选 profile |
| `http_api.py`、`backend.py` | s1、s1b、s23 均改 | 可能加指标端点 | 追加 `deletion-requests`、`data-exports` 读路由；不改他人部分 |
| `workbench_reads.py` / 设置页 | s1/s1b 设置页只读显示保留期 | 可能加指标读 | 设置页的保留期数据源改读 `control.nexloop_retention_settings`；删除与导出按钮放在 NX-028 的 Action 槽位 |
| `apps/web/src/workbench/**` | NX-028 独占 | — | 只提供端点与 Action，页面改动由 NX-028 线做 |
| 读取审计表（`nx028-s1b` 0145） | 新建 | 审计导出读取 | 只加保留期执行，不改表结构 |
| `apps/agent-host/src/runtime-host.ts` | — | 可能加指标 | 新增内部 `runs/purge` 接口 |
| NX-027 假名化端口与 `financial_retention_days` | — | 费用可观测 | 调用；补 doctor 告警 |
| `deploy/configuration/retention.v1.json`（新） | 设置页读取 | 读取以展示 | 新建 |
| `catalog.json`、`versions.lock.json` | 均改 | 均改 | 均改（正常文本冲突，由调度员重排） |

## 14. 待负责人决定的事项（D 项）

裁定状态：D8、D9 已由调度员裁定（见各项）；D1–D7 已报负责人，待结论。

能按现有 ADR 或文档定的，不列为 D 项，已在正文写明依据（Valkey 无需处理：ADR-003；不撤回已发出消息：ADR-021；先阻断后清除、凭据无原文、恢复先重放：`13:33`、`03:98`、`17:40`；派生 READ 立即失效：ADR-020；默认保留期：`03:96`）。

- **D1 顾客在 WebChat 提出的删除请求如何处理。** 推荐：只生成待处理的删除请求并通知负责人，由负责人确认后执行（核验身份、处理保留例外）；在确认前对该顾客立即停止营销类主动联系，但不停止服务回复。替代：收到即自动执行（风险：冒充顾客、误判聊天内容）。
- **D2 Claim 的保留。** 推荐：`quote` 等原文字段跟随来源消息（90 天）清除；结构化值作为知识保留，被取代后 1 年删除；Consumer 删除时全部删除。替代：Claim 整体跟随消息 90 天（会丢失已确认的偏好，降低服务质量）。
- **D3 模型请求清单与计划记录的保留。** 推荐：提示原文 30 天；不含原文的清单（摘要、用量、费用）与计划状态链 1 年，以支持 AT-027 审计与成本复盘。替代：与原文同为 30 天。
- **D4 承诺关闭后的保留。** 推荐：关闭后 1 年删除，引用原文跟随来源消息；Consumer 删除时对象保留到期满但 `made_to` 换成删除凭据引用，以免违约与履行统计失真。替代：Consumer 删除时直接删除承诺。
- **D5 删除后的联系限制。** 推荐：保留一条不含身份的限制，以渠道外部标识的加盐哈希为键（盐按租户、保存在 owner 侧），用于防止同一外部标识被重新导入后再次联系；负责人可撤销。替代：随 Consumer 一并删除（风险：重新导入后被再次联系）。
- **D6 读取审计（ADR-025）与治理审计的保留，以及删除 Consumer 后的处理。** 推荐：读取审计 1 年，到期删除只留按日计数；Consumer 删除时审计中的对象 ID 保留（ID 不含原文）。替代：Consumer 删除时把审计中的对象 ID 也替换为假名（审计仍可按删除凭据追溯，但可读性下降）。
- **D7 顾客自助导出（数据可携带）。** 推荐：v0.1 不提供自助导出，顾客请求转为负责人发起的导出，经负责人确认后交付；替代：WebChat 中直接下载本人数据（需要更强的顾客身份核验）。
- **D8 Pi Run 文件的删除方式。**（**调度员裁定 2026-10-10：采用推荐。** Agent Host 新增内部接口 `runs/purge`，只走 loopback 并验证签名，由执行者按清单调用；Host 的定期清理不能替代它。） 推荐：Agent Host 增加内部 `runs/purge` 接口，由执行者按清单调用；替代：Host 自身按目录时间定期清理（无法响应 Consumer 删除）。
- **D9 保留期配置的粒度。**（**调度员裁定 2026-10-10：采用推荐。** v0.1 只做租户级保留配置。） 推荐：v0.1 只做租户级配置（`tenant_settings.retention_policy`），不做按 Consumer 或按目标的覆盖。替代：允许按目标覆盖（复杂度高，易与偏序约束冲突）。

## 15. 实现切片建议（NX-028 合入后）

1. **切片一**：保留设置、只追加表的受治理清除（§8）、tombstone、到期扫描执行者（先覆盖消息原文、上下文与提示、作业输入、Artifact 调度、商业与费用）。
2. **切片二**：`Consumer.erase` 全链路（§4）、`Message.erase`、保留例外、读断言对 erasing 的拒绝、Pi `runs/purge`。
3. **切片三**：导出（§6）、删除请求与导出的读接口、AT-003 导出部分。
4. **切片四**：删除水位与重放（§9）、AT-059、doctor 告警；与 NX-035 对接恢复演练。
