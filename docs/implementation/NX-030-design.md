# NX-030 审计指标与告警：设计稿（待调度员审核，未实现）

分支 `nx030-design`，从 main `151d7ef` 切出。本稿只有文档，没有代码和迁移；文中迁移号、函数名、配置文件名都是占位。NX-030 依赖 NX-028，NX-028 的切片 2/3 与 ADR-025 的读取审计尚未全部合入 main，实现排在它们之后。

## 0. 依据与结论

**依据**（行号以 main `151d7ef` 为准）：
- PRD M23（`01_PRD.md:86, :310–316`）：
  - 串联 Event、Run、Goal、Context、Model Call、Mutation、Action、Outcome 和版本；
  - 提供指标、结构化日志和审计导出；
  - 日志默认脱敏，原文须授权访问；
  - 能按 `run_id` 还原发生了什么、哪些结果仍未知；
  - 告警不能泄露跨租户原文。
- PRD 的性能目标（`:332`）：ACK p95 ≤500 ms，空闲队列派发 p95 ≤2 s。
- 运行手册（`17_OPERATIONS_RUNBOOK.md`）：
  - 监控清单（`:11`）：DB 连接/事务/磁盘/WAL 归档延迟、任务积压与最老年龄、活跃 Run、模型/工具延迟、Action unknown 数量与年龄、权限拒绝、超期承诺、提取/索引水位、备份年龄与恢复检查、CI 资源；
  - 归档积压必须告警（`:23`）；
  - 普通日志只记 ID、状态、耗时和脱敏错误，原文存受权 Artifact（`:63`）；
  - 每日检查备份年龄（`:67`）。
- 部署（`11_DEPLOYMENT.md`）：
  - Prometheus/Grafana 是可选 profile，默认关闭，只走运维 SSH 隧道（`:28`）；
  - 只读指标数据库角色（`:57`）；
  - 连接总数 ≤60，池满时返回明确错误、不无界等待（`:59`）；
  - Agent Host 单进程，全局并发 4（`:65`）。
- 界面（`10_UX_AND_WORKBENCH.md`）：总览显示异常/unknown、队列积压、费用与数据窗口（`:13`）。
- 验收：AT-049“日志秘密”（M23，S4）——请求或错误含 key 和原文时，普通日志脱敏，Artifact 受限访问。

**结论**：
1. **PostgreSQL 是指标、告警和审计的唯一权威**，不引入新的外部依赖：不加 Prometheus/OTel/statsd 库，不新增必需服务（ADR 约束见 `16_ADR.md:31`）。
2. **库里已有的状态（队列、unknown、拒绝、承诺、待回复、成本）用 SQL 快照函数算出**。进程内状态（Agent Host 并发与排队、guard 时延与 2 s 超时、连接池）由进程定期写入 PG 的聚合样本；Agent Host 不直连数据库，经 runtime worker 代写。
3. **告警由一个后台评估器按版本化规则计算**，写入 PG 的告警事件表，在工作台展示。v0.1 的接收人是负责人，只在工作台内通知（D2）。
4. **审计视图是只读联合视图**：把已有的人类 Action 记录（控制事件、承诺事件、Action claim、接管、员工回复、人工请求、审核决定）和 ADR-025 的读取审计统一呈现。只含 ID、类别、主体、时间、结果，不含原文。
5. **AT-049 按“结构化日志白名单 + 哨兵原文全进程扫描”验收**。

## 1. 现状（已核对代码）

| 能力 | 现状 | 缺口 |
|---|---|---|
| 健康检查 | API `/health/live`、`/health/ready`（`http_api.py:106–130`，含 foundation、浏览器会话、Host 探测、cache degraded）；Agent Host `/health/live`、`/internal/v1/health/ready`（`main.ts:45–60`） | 只回答“活着吗、缺什么”，没有数量和时延 |
| doctor | `nexloop-doctor`：catalog、postgres、扩展、Artifact、模型配置、连接预算、Host 并发上限（`doctor.py:17–121`） | 部署前检查，不是运行时指标 |
| 队列 | `runtime.jobs` 的 status：pending / running / retry_wait / succeeded / failed / dead_lettered；`runtime.nexloop_work_feed` 的 pending / dead_lettered；`backlog` 动词返回 pending 数、最老秒数和死信（`0111:470`，`work_feed.py:67`） | 只有服务主体能按单个 feed 查，没有汇总 |
| 派发拒绝 | NXC01–05、NXB01–03、NXM01（`goal_controls.py:43`）；s23 分支新增 NXC06/07。拒绝码只落在 `runtime.jobs.result.code`（`runtime_dispatch.py:164,193`），没有专门的表 | 没有按原因计数 |
| unknown 与对账 | `nexloop_effect_intents.state`、outbox、attempts、observations、events；对账在 `0065` 的 query_admissions / recovery_audits；工作台 actions 动词已能算 unknown 数和最老年龄（0117） | 没有“对账中 / 仍未知”的分布和趋势 |
| 待回复与兜底 | `reply-due` feed；`control.nexloop_reply_escalations`（4 种原因） | 没有超时率 |
| 承诺 | `runtime.nexloop_commitment_exceptions`（9 种原因，含 breached） | 工作台只显示当前列表 |
| 成本 | `control.nexloop_budget_limits/consumption`；`runtime.nexloop_model_requests` 的 usage/cost；NX-027 的 `nexloop_cost_entries` 在 s4j，未进 main | 没有按类别的成本时间序列 |
| guard 时延 | 2 s 超时散落在 `runtime-host.ts:74,87`、`runtime-effect-tools.ts`、`effect_provider.py:40`。只有 `effect_intents.py:67` 在失败时记 `elapsed_ms` | 没有成功时的时延分布，没有超时率 |
| Agent Host 并发 | `run-admission-gate.ts:8–29`：上限 4、等待 500 ms、FIFO；`activeRuns` / `waiting` 有 getter 但没有导出；`runtime_busy`（`runtime-host.ts:226`） | 没有导出 |
| 连接池 | `open_backend(pool_max_size=4)`、`LifecycleLock`→`BackendBusy`、`pool.wait(timeout=10)`；没有调用 `get_stats` | 不可见 |
| 日志 | 没有集中配置；worker 关掉 `psycopg.pool` logger；每轮在 stdout 打一行 JSON 摘要；多项测试断言 token/key/DSN 不出现在输出里 | 没有测试断言**消息原文**不进日志；没有统一字段白名单 |
| 审计 | `runtime.audit_events`（`0001:78`）存在但从未写入；人类 Action 记录分散在 6–8 张表；ADR-025 读取审计在 s1b | 没有统一视图 |
| 备份 | NexLoop 没有备份实现（属于 NX-035，S5） | 只能预留指标位 |

## 2. 指标清单（v1）

约定：
- 每个指标都带 `tenant_id`、`world`，`real` / `test` / `simulation` 分开统计，告警只看 `real`（D6）；
- 计数是当前值或窗口内增量，年龄单位为秒，时延单位为毫秒；
- 不带任何原文、正文、客户名或 Consumer 属性值，标签只用枚举、ID 前缀之外的固定代码。

| 编号 | 指标 | 来源 | 粒度 / 标签 | 工作台 | 默认告警（D3 待定） |
|---|---|---|---|---|---|
| M01 | 派发拒绝数，按原因 | `runtime.jobs` 已结束任务的 `result.code`；s23 起含 NXC06/07。实现时由记录拒绝结果的那个事务追加一行到 `runtime.nexloop_dispatch_refusals`（code、intent/task id、时间），不再依赖 `jobs.result` 的保留期。SQL 拒绝本身会回滚派发事务，所以不在派发函数里写（D8） | 原因码 NXC01–07 / NXB01–03 / NXM01，5 分钟窗口 | Action 页、总览 | NXB01 预算耗尽：出现即告警；NXC02/03 过期：1 小时 >20 条告警（复评没跟上）；NXC05：只计数，不告警（这是保护在生效） |
| M02 | unknown 数与最老年龄 | `effect_intents.state='unknown'`（已在 0117 实现） | 按 action_name | 总览、Action 页 | 最老 >15 分钟告警；>2 小时升级 |
| M03 | 对账进度 | query_admissions / recovery_audits / observations：对账中、已转成功、已转失败、仍未知 | 24 小时窗口 | Action 页 | 仍未知 >0 且年龄 >24 小时告警 |
| M04 | 待回复超时与兜底升级 | `reply-due` feed pending 的最老年龄；`reply_escalations` 按 reason 计数 | 按 reason | 总览、联系限制页 | 任一升级：告警（ADR-023“来信必回”被打破） |
| M05 | 承诺违约与异常 | `commitment_exceptions` 按 reason 计数，包括新增的 breached | 按 reason | 总览、承诺页 | breached 新增：告警；`made_under_contact_restriction`：告警 |
| M06 | 后台积压与死信 | `runtime.jobs`（queue × status）与 `nexloop_work_feed`（feed × status）：pending 数、最老 pending 秒数、retry_wait 数、dead_lettered 数；提取窗口 feed（0069）单列 | 按 queue / feed | 总览“队列积压”块（替换切片 1 的 unavailable） | 任一死信新增：告警；最老 pending >10 分钟告警（`14_TEST_ACCEPTANCE.md:86` 要求 lease 回收 ≤60 s） |
| M07 | 提取 / 匹配 / 索引水位 | 提取 feed 最老未处理消息时间、claim-match 与 recall-instance 的 pending 年龄 | 按 feed | 总览 | 水位落后 >30 分钟告警 |
| M08 | guard 时延与 2 s 超时率 | 新增：runtime guard worker 每分钟写一行聚合到 `runtime.nexloop_process_samples`（count、p50/p95/max、超时数，按调用类型），超时判定与现有 2 s 截止相同 | 1 分钟样本 | 设置与治理 → 运行状态 | 5 分钟超时率 >1% 告警；p95 >1500 ms 告警（逼近 2 s） |
| M09 | Agent Host 并发与排队 | Agent Host 新增鉴权的 `/internal/v1/metrics`（activeRuns、waiting、admission 超时数、`runtime_busy` 数、累计 Run 数），runtime worker 定期拉取并写样本（Host 不碰数据库，不打破“Runtime 无 DB 秘密”） | 1 分钟样本 | 运行状态 | 排队等待超时（`runtime_capacity_exhausted`）>0 告警；activeRuns 持续 =4 达 10 分钟告警 |
| M10 | 连接池 | 每个持池进程每分钟写 `psycopg_pool` 的 `get_stats()`（pool_size、available、requests_waiting、requests_errors、等待时长），以及 `BackendBusy` 次数；PG 侧 `pg_stat_activity` 按角色计数（只读指标角色读取，D4） | 按进程 × 池 | 运行状态 | requests_waiting >0 持续 5 分钟告警；总连接 >50（上限 60）告警 |
| M11 | 成本 | `budget_consumption`（模型 / 激励）、`model_requests.cost`；NX-027 合入后加上 `nexloop_cost_entries`。real / test 分开，unknown 不计成功 | 按类别、币种，日窗口 | 总览“费用”块（依赖 NX-027） | 消耗 >上限 80% 告警（上限本身由 NX-022 阻断，告警只是提前提醒） |
| M12 | 备份年龄与归档延迟 | 预留：读 NX-035 的备份清单表（LSN、版本、完成时间）；不存在时为 unavailable，不显示为 0 | — | 运行状态 | 备份年龄 >26 小时告警；WAL 归档延迟 >5 分钟告警（RPO ≤5 分钟） |
| M13 | 评估器自身 | 最近一次评估的时间、规则版本、失败数 | — | 运行状态 | 评估停止 >5 分钟：在工作台显示“告警系统不可用”（自身不能靠告警通知） |

M08–M10 的样本表是只追加的运行数据，定期汇总后删除，保留期见 D5，与 NX-029 协调。

## 3. 告警

- **规则配置**：`deploy/configuration/alert-rules.v1.json`（版本化，新版本是新文件）。
  - 每条规则：`rule_id`、`metric`、`selector`（例如原因码、feed）、`condition`（gt / ge / increase / age_gt）、`threshold`、`window_seconds`、`for_seconds`（持续多久才触发）、`severity`（warning / critical）、`purpose`。
  - 只能经可信配置写入（configurator 函数，与 effect categories、workbench 成员相同的方式），应用角色不能改阈值。
- **评估器**：`nexloop-alert-evaluator`，compose 的 `background` profile，单实例，每 60 秒一轮。
  - 读取 SQL 快照函数和样本，写 `control.nexloop_alert_events`（只追加：firing / resolved / silenced）。当前状态由视图推导。
  - 用同一个 DB 角色，不新建高权限角色。
- **去重**：去重键为 `rule_id + 选择器值 + tenant + world`。同一个键持续触发只算一条告警（记录首次时间、最近时间、次数），恢复后再触发才算新告警。
- **静默**：
  - owner 专属的受治理人类 Action `nexloop.alert.silence:1`，参数为规则、选择器、截止时间（≤7 天）、原因，必填。
  - 静默期间仍然评估、仍然记录，只是不提示；到期自动结束。
  - 不能静默 M13（告警系统不可用）。
- **接收人**：负责人（owner）。
  - v0.1 在工作台显示：总览的告警块、告警页（列表、详情、静默），critical 置顶；
  - operator 可读、不能静默（D2）。
  - 外部通知渠道（邮件、短信、IM）需要渠道凭据和负责人选择，不在 v0.1 范围（D2）。
- **不泄露**：
  - 告警内容只引用指标、代码、ID 和计数，不带原文；
  - 跨租户永不聚合，评估和展示都按 tenant 过滤（PRD :316）；
  - 告警详情链接到对应工作台页面，原文仍然受 ADR-025 的读取控制和审计。

## 4. 审计视图

- **人类 Action 审计**：只读视图 `authz.nexloop_human_action_audit`，以函数形式按 owner 授权。它联合已有的记录，统一字段为 `occurred_at, principal_id, role（当时的工作台角色）, action, target_kind, target_ref, outcome, intent_id`。来源：
  - `control.nexloop_control_events`：暂停/恢复、预算、目标、指标、解除联系限制；
  - `runtime.nexloop_commitment_events`：取消、延期、履约确认、条件满足、标记沟通类承诺；
  - `runtime.nexloop_action_claims` 中的 human 主体；
  - s23 分支的 `runtime.nexloop_human_requests`（手动复评、查询执行结果）、`control.nexloop_takeovers`（接管/交还）、`runtime.nexloop_staff_replies`（员工回复，只给 message_id，不给正文）；
  - NX-044 审核决定（`ontology.nexloop_review_decisions`）；
  - NX-030 自己的告警静默。
- **读取审计**：ADR-025 的 `runtime.nexloop_workbench_read_audit`（s1b）原样纳入同一页面，区分“操作”和“读取”两类。
- **权限**：只有 owner 能看（与 ADR-025 一致，审计角色以后由负责人另定）。导出只导出视图里的字段，经同一 tenant/world 过滤（`13_SECURITY…:11`）。
- **保留**：审计记录只含 ID、类别和时间，不含个人信息原文；保留期与删除传播属于 NX-029（见 §8）。
- **按 run_id 还原（PRD :316）**：工作台 Action 页加“追溯”视图，按 `run_id` 串起 `event_id → goal/context → submission → receipt → provider evidence`（运行手册 `:15`），只显示 ID、状态和时间。各段数据都已在库中，NX-030 只做只读拼接。

## 5. 指标从哪里取、在哪里显示

| 形式 | 用途 | 说明 |
|---|---|---|
| SQL 快照函数 `authz.nexloop_metrics_snapshot(scope)` | 唯一计算口径 | owner-only definer 函数，返回 §2 的 JSON。评估器、工作台、可选导出都调用它，同一口径 |
| 工作台 | 负责人和运营日常查看 | 新增读动词 `metrics`、`alerts`、`audit`，经 0117 读端口按角色授权（读 `nexloop.workbench.read`，审计只给 owner）；总览“队列积压”“费用”块不再显示 unavailable；新页面“运行状态”和“告警” |
| `GET /internal/metrics`（Prometheus 文本格式） | 可选监控 profile | 只绑 loopback，用只读指标角色（`11_DEPLOYMENT.md:57`）连库，默认关闭；输出只是快照函数的文本形式，不引入客户端库（D1） |
| doctor | 部署检查 | 新增 `--observability`：评估器最近一轮是否在 5 分钟内、规则文件版本与库内一致、样本是否在写入、备份清单是否存在（NX-035 前为 unavailable）。它是检查，不是指标源 |
| 结构化日志 | 排障 | 见 §6；日志不是指标源 |

## 6. 无原文普通日志（AT-049）

- **统一日志器**：新增 `nexloop_eios/structured_log.py`。所有服务的 stdout/stderr 只输出一行 JSON，字段是白名单：`ts, service, event, tenant, world, ids{run_id,intent_id,task_id,message_id,…}, status, code, elapsed_ms, attempt`；`error` 只能是固定代码加脱敏摘要。
  - 不在白名单的字段直接丢弃；
  - 字符串值长度设上限，值形如密钥、DSN 或 JWT 时替换为 `[redacted]`。
- **现有输出迁移**：各 worker 的摘要行改走统一日志器；`effect_intents.py:67` 的 diagnosis 已经只含代码和耗时，保持不变；uvicorn 访问日志继续关闭。
- **原文的去处**：模型完整请求、消息原文只存受权 Artifact 或业务表（运行手册 `:63`），日志里只出现它们的 ID。
- **验收测试（AT-049）**：
  - 用一段哨兵原文和一个哨兵 key 跑完整的入站→提取→Run→外发链路，加一个故意失败的分支（provider 报错、guard 超时、SQL 拒绝）；
  - 收集所有进程的 stdout/stderr、PG 日志（`log_min_error_statement` 下的报错语句）和告警事件；
  - 断言哨兵原文和 key 都不出现；
  - 同时断言 Artifact 只能由受权主体读取（复用已有 Artifact 授权测试）。
- **数据中的原文片段不在 AT-049 范围**：`contact_refusal_hits.matched_text` 是业务数据，受 ADR-025 控制，不算日志，单列说明。

## 7. 对应 AT

| AT | 关系 | NX-030 提供的证据 |
|---|---|---|
| AT-049 日志秘密（M23，S4） | 主验收 | §6 的哨兵扫描测试 |
| AT-050 恢复演练（M23，S5） | 前置 | M12 备份年龄与归档延迟的观测位；演练本身属于 NX-035 |
| AT-043 成本上限（M19，S4） | 可见性 | M11 成本与预算告警（阻断已由 NX-022 实现） |
| AT-006 暂停一致（界面） | 可见性 | M01 按原因的拒绝计数，与 NX-028 的派发预判互相印证 |
| AT-044 人工接管 | 可见性 | 审计视图中的接管/交还/员工回复记录 |
| AT-045 页面状态 | 继承 | 新页面沿用切片 1 的状态模型；M12/M11 不可用时显示 unavailable，不显示 0 |
| PRD M23 验收（:316） | 主验收 | 按 run_id 追溯视图；告警跨租户隔离测试 |

建议在 planning 中把 AT-049 的证据映射到 NX-030（由调度员更新）。

## 8. 迁移、配置与测试形状（占位）

迁移（临时号由调度员分配）：
1. `nx030_observability`：
   - 新表：`runtime.nexloop_dispatch_refusals`、`runtime.nexloop_process_samples`、`control.nexloop_alert_rules`（configurator 写入）、`control.nexloop_alert_events`；
   - 函数：`authz.nexloop_metrics_snapshot`、`authz.nexloop_human_action_audit`、`authz.nexloop_record_process_sample`（只接受服务主体）；
   - 只读指标角色 `nexloop_metrics`：只能执行快照函数，不能直接读表。
2. 派发拒绝写入：不包装任何派发函数，也**不改** `control.nexloop_contact_assert_intent`（NX-028 切片 3 已在其上加了 NXC06）。拒绝行由已经持久化拒绝结果的事务一并写入：
   - Run 派发：`runtime_dispatch.py` 的 `finish_task` 事务（今天已经把 `result.code` 写进 `runtime.jobs`）；
   - effect 派发：effect worker 记录拒绝或终态的同一事务。
   实现时改的是这两个“结束”函数（以合入时的最新函数体为对象），见 D8。
3. 工作台读端口新增 `metrics`、`alerts`、`audit` 动词：以 0117 最新函数体做 create or replace，或新函数加 Python 分派（D7）。

配置：`deploy/configuration/alert-rules.v1.json`；`deploy/authorization/workbench-roles.v<N>.json` 加 `nexloop.alert.silence:1`（owner）。

测试（真实 PG，合成数据，本机 `-n ≤3`，真实 Pi 单独串行）：
- 每个指标给定一个构造状态，断言快照值，包括 unavailable；
- 规则触发、持续、恢复、再触发的去重；
- 静默到期；M13 不可静默；
- operator 不能静默，非 owner 读不到审计；
- 跨租户告警与快照隔离；
- AT-049 哨兵扫描；
- Agent Host metrics 端点鉴权、无原文，Host 停止时为 unavailable；
- guard 超时率在人为 2 s 超时下计数正确；
- 连接池等待在池满时可见，且依旧不无界等待；
- doctor `--observability`；
- vitest：运行状态页、告警页、审计页的 AT-045 状态。

## 9. 与现有分支的文件重叠

| 文件 / 对象 | 重叠方 | 处理 |
|---|---|---|
| `workbench_reads.py`（overview 的 backlog / commercial 块）、`workbench_http.py`、`backend.py` 的 WorkbenchServices | NX-028 切片 1（main）、s1b（ADR-025）、s23（WorkbenchActions） | 只追加动词和路由；overview 两个块由 unavailable 改为真实数据，按合入顺序基于最新版本修改 |
| 0117 `authz.nexloop_workbench_read` | NX-028 切片 1；s23 若也扩展动词 | 二选一（D7）：新增独立函数，或以最新函数体 create or replace；与 L4 约定只由一方改 |
| `apps/web/src/workbench/pages/Overview.tsx`、`api.ts`、`route.ts` | NX-028（L2 切片 1、L4 切片 2/3 的 actions 插槽） | 新页面放 `pages/Operations.tsx`、`pages/Alerts.tsx`、`pages/Audit.tsx`；Overview 只改两个块 |
| `deploy/authorization/workbench-roles.v*.json` | NX-028（L2） | 加 `nexloop.alert.silence:1`，manifest_version 递增 |
| `business-actions.v*.json` | NX-028 切片 2/3、NX-027 | 新增一项 Action 声明，版本递增，按合入顺序 |
| `runtime_dispatch.py`、派发 SQL 入口 | NX-028 切片 3（NXC06/07）、NX-026（0112 包装） | 只包装、不改语义；以合入时最新函数为改名对象 |
| ADR-025 读取审计表 | s1b（L2） | 只读引用 |
| `runtime.nexloop_human_requests`、`control.nexloop_takeovers`、`runtime.nexloop_staff_replies` | s23（L4） | 只读引用 |
| `nexloop_cost_entries`、NX-027 指标 | NX-027（s4j） | 只读引用；未合入时 M11 只用 NX-022 的预算表 |
| 审计记录、告警事件、过程样本的保留与删除 | **NX-029**（L3 正在设计） | NX-030 只定义字段（只含 ID 和代码）；保留期、删除传播（例如删除 Consumer 后审计里的 target_ref 怎么处理）和审计导出格式交给 NX-029 统一，这里列为接口约定。NX-029 设计稿目前还不存在，需要两线对齐 |
| 备份清单表 | NX-035（S5） | M12 只读引用，表名和字段由 NX-035 定，NX-030 先保留 unavailable |
| `compose.test.yaml` 的 background profile | NX-027、NX-028 新增的后台服务 | 新增 `alert-evaluator` 服务，追加 |
| `doctor.py` | L3（Host 并发检查） | 新增 `--observability` 分支，追加 |
| `apps/agent-host/src/run-admission-gate.ts`、`main.ts` | L3（Host 并发上限） | 只新增 `/internal/v1/metrics` 和计数器读取，不改准入逻辑 |

## 10. 需要负责人或调度员决定（D 项）

- **D1 可选 Prometheus 导出**：推荐提供 loopback 的 `/internal/metrics` 文本端点，默认关闭，用只读指标角色，不引入客户端库；Prometheus/Grafana 本身仍按 `11_DEPLOYMENT.md:28` 作为可选 profile，由负责人决定是否部署。备选：完全不提供，只看工作台。
- **D2 告警接收与通知渠道**：推荐 v0.1 只在工作台内通知负责人，operator 只读；外部渠道（邮件、短信、企业 IM）以后作为一项受治理的外发 Action 接入，需要渠道凭据和负责人选择。备选：现在就接一个渠道，需负责人提供渠道和凭据。
- **D3 默认阈值**：推荐采用 §2 表中的默认值，写入 `alert-rules.v1.json`，负责人可按新版本调整。关键几项：
  - unknown 最老 >15 分钟；
  - 死信新增即告警；
  - 兜底升级即告警；
  - 承诺违约即告警；
  - guard 5 分钟超时率 >1%；
  - 连接 >50；
  - 备份 >26 小时。
- **D4 只读指标角色**：推荐新建 `nexloop_metrics`（login，只能执行快照函数），用于 D1 的导出和运维查询，计入连接预算 1 个；它不读任何业务表、不读原文。备选：不建角色，导出走 API 角色。
- **D5 样本与告警事件的保留期**：推荐 1 分钟样本保留 7 天、小时汇总保留 90 天；告警事件保留 1 年；人类 Action 审计按 NX-029 的统一策略（推荐不短于 1 年）。由 NX-029 落实删除传播。
- **D6 test / simulation 数据**：推荐指标分 world 和 data_mode 统计，工作台带标签，告警只对 `real`，避免测试数据触发负责人告警（M20 验收：模拟不显示为真实）。
- **D7 工作台读端口扩展方式**：推荐新增独立函数（`authz.nexloop_workbench_metrics_read` 等）并在 Python 侧分派，不改 0117 的函数体，避免与 NX-028 后续切片争同一个函数。备选：create or replace 0117。
- **D8 派发拒绝持久化**：SQL 拒绝会回滚派发事务，所以拒绝行不能在派发函数里写。推荐新增只追加的 `runtime.nexloop_dispatch_refusals`，由已经持久化拒绝结果的那个事务一并写入：Run 派发是 `finish_task`，effect 派发是 effect worker 的拒绝/终态记录。两条路径都不改派发判定本身，也不碰 NX-028 切片 3 的派发包装。需要调度员确认这两个“结束”函数的归属（L3 的 runtime worker、L4 的 effect 路径）。备选：只从 `runtime.jobs.result.code` 统计，受任务保留期限制，且看不到 effect 派发的拒绝。
