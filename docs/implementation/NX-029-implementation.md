# NX-029 保留、删除与导出闭环：实现说明（切片一、二）

设计稿 `NX-029-design.md`（D1–D7 负责人“全部按推荐”，D8/D9 调度员裁定）。分支 `nx029-s1`，切自 `nx027-registry`（main `a5d7c48` + NX-027 注册表提交），带上设计稿提交。临时迁移号 0141（本任务可用 0141–0149），合并时由调度员重排。

## 1. 切片一范围

保留设置、只追加表的受治理清除（§8）、tombstone、到期扫描执行者。人类入口（`Consumer.erase`、`Message.erase`、导出、保留例外）、erasing 读拒绝（含 §4.2a 的 v0072 两层要求）、Pi `runs/purge`、删除水位与重放属于后续切片。

## 2. 迁移 0141 `nx029_retention`

| 部分 | 内容 |
|---|---|
| 保留设置 | `control.nexloop_retention_settings`（逐字种入 `deploy/configuration/retention.v1.json`，摘要 `d3fe6bec…0cb3`；只追加；插入时校验每类 1–36500 天、派生类不长于来源、扫描参数有界）；`runtime.nexloop_retention_settings_current()`、`runtime.nexloop_retention_days(class)`（`financial` 取 NX-027 商业设置的 `financial_retention_days`，不重复配置） |
| 受治理清除 | `control.nexloop_erasure_scope`（owner 独占，应用角色无任何权限；只存在于清除事务内）；`runtime.nexloop_erasure_open/close`；`runtime.nexloop_erasure_permits(user, table, op, old, new)`：只对 owner、只在本事务登记过该表时放行，删除须登记为可删，更新只能把登记列改为清除值（null、`''` 或标记 erased 的对象） |
| 守卫函数体替换 | 原函数原地替换（触发器不变）：`control.nexloop_nx022_append_only`、`control.nexloop_context_append_only`、`runtime.nexloop_commercial_event_guard`（删除）、`ontology.nexloop_commercial_record_guard`（删除分支）。未登记时报错与原来一致 |
| tombstone | `runtime.nexloop_erasure_tombstones`（只追加，FORCE RLS）：原因、请求或扫描批次、数据类、对象种类与键、动作、计数、执行者；不含原文、金额、外部标识 |
| 扫描 | `runtime.nexloop_retention_sweep(tenant, world, class, batch, ref, by)`：一批、`skip locked`、幂等。类：`message_text`（流记录、relay outbox、Message 对象正文置空并标 erased）、`claim_quote`（来源消息过期或已清除时 quote 置空，D2）、`prompt_text`（30 天，D3）、`job_input`（终态作业的 input）、`commercial_receipt`、`metric_observation`（删除）、`financial`（最后一个已核验事件早于 `financial_retention_days` 的记录连同事件、对象事件、例外、wake、承诺绑定、登记删除，每条记录一个 tombstone；费用条目与结算删除，NX-027 D6） |
| 执行端口 | `authz.nexloop_retention_command`（`nexloop-retention-v1`，`eios:action:nexloop.retention.execute:1`，domain worker）：`due`（各类按 `interval_seconds` 到期）、`sweep`、`status`（tombstone 计数、各类最近一次结果、doctor 告警）；扫描记账表 `runtime.nexloop_retention_sweeps` |
| doctor | `runtime.nexloop_retention_warnings`：已批准指标的成熟窗口长于 `financial_retention_days` 时告警（NX-027 遗留项） |

已发布迁移未改。新函数 `search_path=pg_catalog,pg_temp`，SECURITY DEFINER 函数归 `nexloop_owner` 并撤销 PUBLIC；`runtime.nexloop_erasure_permits` 例外地授予 PUBLIC 执行（触发器以调用者身份询问它；它只对已登记的 owner 事务回答 true）。

## 3. 部署材料与代码

- `deploy/configuration/retention.v1.json`（新）：v1 只含本切片执行的类；其余类随后续切片以新版本加入。
- `deploy/authorization/service-grants.v1.json` v13：`retention_keeper` 主体（domain worker）与 `nexloop.retention.execute` 授权。
- `packages/eios-core/src/nexloop_eios/retention.py`：`load_settings`（有界、派生类不长于来源）、`canonical_settings`、`RetentionPort`、`RetentionKeeper.run_once()`。
- 后台入口 `nexloop-retention-keeper`（容器作业 `retention-keeper`，compose 可选 profile `background`）。

## 4. 与设计稿的差异

1. **不使用 work feed**：到期扫描是按类、按时间间隔的周期工作，用扫描记账表与 `due` 判断代替 `retention-sweep` feed；避免再次重写 work feed 的 CHECK（NX-027 与 NX-028 已各改一次）。多个执行者并发时由 `skip locked` 保证每行只处理一次。
2. **批量 tombstone**：原地清除（redacted）的行本身带 erased 标记，每批只写一条带计数的 tombstone；整行删除的商业记录每条一条 tombstone。
3. **Artifact 调度不在执行者中**：Artifact 清理候选按所属主体划分（`authz.nexloop_artifact_cleanup_candidates` 只返回调用主体自己的 Artifact），中央执行者无法代删他人 Artifact。改为后续切片中由各 Artifact 所属服务调度，或新增 owner 内部的跨主体清理端口（待定）。
4. **上下文包与来源**：经核对，`runtime.nexloop_context_packs/sources` 只存引用、版本与哈希，不存正文；正文在上下文 Artifact 中（随 Artifact 保留期删除）。因此不另设 context_text 类。
5. **原始商业事件的 `customer_ref`**：未在本切片清除。记录状态由事件重新推导，改写 `customer_ref` 会改变已关联记录的 `consumer_ref` 并使守卫拒绝后续编辑；需要先让记录在原始事件过期前冻结关联结果，留到切片二与 Consumer 删除一起设计。本切片只删除过期回执。
6. **已清除消息仍出现在会话中**：正文为空并带 `erased:true`。上下文组装、WebChat 历史与工作台读取尚未把 erased 消息排除或显示为“已过保留期”，留作后续切片。

## 5. 测试（真实 PG，合成数据）

- `tests/test_retention_pg.py`（4）：设置逐字种入、无效版本被拒、只追加；未登记时 owner 也不能改写或删除，登记其它表或只登记列都不能删除，只能把登记列改为清除值，关闭后恢复；应用角色无法登记；执行者端到端（消息正文、Message 对象、Claim quote、终态作业 input、过期商业记录连同事件与对象事件、费用条目、回执，tombstone 不含原文、金额、外部标识；同一间隔内不再到期；重复扫描为零）；指标观察过期删除、doctor 告警、无效扫描与无授权服务被拒。
- `tests/test_retention_prompts_pg.py`（1）：真实 v6 Run 经守卫记录的提示原文，过期后清除，请求清单摘要不变；扫描外仍只追加。
- 时间推进：测试以管理员在一次性测试库中暂停触发器回拨时间（`session_replication_role=replica`），各测试注明。


## 6. 切片二：Consumer 删除、Message 删除、保留例外、erasing 读拒绝、Run 文件与 Artifact 清理（迁移 0142）

### 6.1 迁移 0142 `nx029_erasure`

| 部分 | 内容 |
|---|---|
| 请求与状态 | `control.nexloop_consumer_erasures`（每个 Consumer 一条；`pending_confirmation`→`blocking`→`settling`→`waiting_unknown_effects`→`purging`→`completed`/`completed_with_holds`；删除水位取自序列 `control.nexloop_deletion_watermark`）。D1：顾客自己的请求 `runtime.nexloop_consumer_erasure_requested` 只建 `pending_confirmation`，不算 erasing，负责人 `Consumer.erase` 确认后才执行 |
| erasing 读拒绝（§4.2a） | `runtime.nexloop_read_consumer` 从读声明解析所涉 Consumer（Consumer 本身或其属性、Conversation、Message）。**两层**：公开入口 `authz.nexloop_assert_read_authority` 与内层 `authz.nexloop_assert_read_authority_before_message_read_v0072` 都改名保留并加同名包装。另外两处补充：0084 的调用点送进 v0072 的是类型级 READ（`object_type:Consumer`），看不到具体 Consumer，所以在 `authz.nexloop_assert_derived_property_access` 自身也加一层（读、写都覆盖）；公开编辑入口 `authz.nexloop_assert_edit_authority` 同样拒绝 |
| blocking（同一事务） | 联系限制（`rule_id='consumer_erasure'`，经 NX-022 控制修订）；该 Consumer 的 active 计划关闭（`closed`/`consumer_erasure`），复评 feed 项移除；erasing 期间 `Contact.release` 被拒（0124 适配函数体替换） |
| settling | 有 `dispatching`/`unknown` 的 effect intent 时进入 `waiting_unknown_effects`，等回执对账给出结果；不重发 |
| purging 阶段 | `messages`（逐条 `runtime.nexloop_erase_message`）→ `claims`（quote/subject/条件/时间表达清空，value 标 erased）→ `runs_text`（该 Consumer 的 Run 的提示原文与作业输入）→ `commercial`（NX-027 假名化端口；费用条目 `consumer_ref` 置空；cohort 成员换成随机假名）→ `commitments`（D4：`made_to` 改为删除请求引用、条件原文清空；0111 守卫加受登记的清除分支）→ `runs_files`（入 Run 清理队列）→ `artifacts`（该 Consumer 的上下文与提示 Artifact 入队）→ `consumer`（等文件清完；D5 无身份联系键；删除 Consumer 对象及其事件、关系；凭据 tombstone） |
| Message.erase | 流记录与 relay 副本清空并标 erased、Claim quote 清空、引用过它的 Run 提示原文清空、Message 对象（连同对象事件、关系）删除（NX-026 记 `source_deleted`，NX-021 移出召回）、tombstone |
| 保留例外 | `control.nexloop_retention_holds`（Consumer 或数据类；只有人能登记/释放）；Consumer 例外使删除 `completed_with_holds` 且不清除，Message.erase 被拒；数据类例外使到期扫描跳过该类（0141 扫描函数改名保留 + 包装）并记 `skipped_hold` tombstone |
| Run 文件（D8） | `runtime.nexloop_run_purge_items`；执行者只取任务已终态的 Run，经 Host loopback `runs/purge` 删除，`purged`/`absent` 才写 tombstone 与 `runtime.audit_events`，失败只记次数，留在队列 |
| Artifact（调度员裁定） | owner 内部跨主体端口：`artifact_candidates` → `artifact_claim`（SQL 重新核对：已到保留期或属于 erasing Consumer；只被终态作业引用；从不被 invocation 引用）→ 执行者删文件 → `artifact_deleted`（核对租约与 fence 后置 deleted，同一事务写 tombstone 与审计）。删文件失败不写任何东西，租约到期后重试 |
| 人类 Action | 注册表行：`consumer.erase`/`erase_consumer`、`message.erase`/`erase_message`、`retention.hold`/`hold_retention`、`retention.release_hold`/`release_retention_hold`，均 human |
| 执行端口 | `authz.nexloop_erasure_command`（`nexloop.erasure.execute:1`，只授予 `retention_keeper`）：`requests`、`step`、`run_purges`、`run_purged`、`artifact_candidates`、`artifact_claim`、`artifact_deleted`、`status`；只返回计数与 ID |

### 6.2 代码与配置

- `deploy/configuration/business-actions.v1.json` v10：`nexloop.consumer.erase`、`nexloop.message.erase`、`nexloop.retention.hold`、`nexloop.retention.release_hold`（human_owner，定型在 Consumer v1）。
- `deploy/authorization/workbench-roles.v1.json` v4：四项只给 owner（不可逆删除与法定例外留给负责人）。
- `deploy/authorization/service-grants.v1.json` v14：`retention_keeper` 增加 `nexloop.erasure.execute`。
- `goal_controls.CAPABILITIES` 与方法 `erase_consumer`、`erase_message`、`hold_retention`、`release_retention_hold`。
- `retention.py`：`ErasurePort`、`HostPurger`（TLS 到 127.0.0.1，Host 内部密钥）、执行者扩展（删除步骤、Run 文件、Artifact）。
- 后台入口 `retention-keeper` 新增 `--host-port/--host-key-file/--host-ca-file`（三者同时给或都不给；不给时 Run 文件留在队列）；执行者使用服务自身的 Artifact 根目录。
- Agent Host：新增 `src/run-purge.ts`；`pi-runtime-adapter.ts` 追加 `purge`（只关闭已 settle 的 Run，活跃 Run 返回 409）；`runtime-host.ts` 追加 `purge`；`main.ts` 追加 `POST /internal/v1/runs/purge` 路由（同一 loopback 与内部密钥）。

### 6.3 测试（切片二）

- 每个调用点的负例（Consumer 处于 erasing 时拒绝，撤出后同一读取恢复）：
  - `test_message_read_derivation.py`：公开入口、0086 Message 包装、0127 函数体（0080 名称）、0077 函数体、内层 v0072；
  - `test_evidence_read_derivation.py`：0086 `assert_derived_consumer_read`，以及会话窗口读取；
  - `test_property_grant_derivation_pg.py`：0084（读与写）与公开编辑入口；
  - `test_read_assert_memo.py`：公开入口与内层 v0072（配置型 Consumer READ）；
  - `test_outbound_messages_pg.py`（真实 Pi）：0127 函数体的外发分支；
  - `test_takeover_relay_pg.py`：0127 函数体的员工回复分支。
- `test_erasure_pg.py`（3）：Consumer 删除全链路（服务被拒、unknown 结果先等待、blocking、限制不可解除、各阶段清除与假名化、承诺保留至期满、凭据不含原文）；Message 删除、保留例外、D1 待确认；Run 文件与跨主体 Artifact 只在删除确认后离开（失败不写 tombstone）。
- `test_host_run_purge.py`（真实 Host）与 `apps/agent-host/test/run-purge.test.ts`（vitest）。

### 6.4 与设计稿的差异与缺口

1. 0080 的函数体已被 0127 的 `create or replace` 取代（`_actor_body_v0080`），因此活着的直接调用点是 4 个函数、5 处调用（0127 函数体内入站分支委托 0077，外发与员工回复分支各自直接调用）；每处都有负例。
2. Claim 不整行删除而是抹除内容（quote、主体文本、条件、时间表达、value 标 erased）：Claim 被抽取运行、承诺、复核等多处按 ID 引用，整行删除会破坏这些引用；内容层面与删除等价。
3. cohort 成员换成新的随机假名，未与商业记录的假名统一（NX-027 假名化端口不返回假名，设计上不保存对应关系）。
4. 计划与策略的自由文本、关系判断（RelationshipAssessment）与 ConsumerRoleLink 等引用该 Consumer 的其他对象，本切片未清除，留待后续切片。
5. D5 的无身份联系键已写入，但“重新导入时拒绝联系”的执行点（NX-018 身份关联）尚未接入；负责人撤销联系键的入口也未做。
6. erased 消息在 WebChat 历史与上下文组装中尚未排除或标注。
