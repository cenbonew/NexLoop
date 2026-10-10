# NX-029 保留、删除与导出闭环：实现说明（切片一）

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
