# Action、Pi Durable 与故障恢复

## 1. 目标和非承诺

要求的是：已接受工作可追溯、可恢复，重复投递不重复造成同一业务效应，结果不明确时能够停下核对。**不宣称任何外部系统都能做到 exactly-once。** 外部服务不支持幂等或结果查询时，自动恢复能力必须显式受限。

Pi Durable 用于内部模型/工具任务的持久化，不替代 EIOS 的业务执行账本。[S04]

## 2. ID 与责任

| ID | 生成/权威 | 用途 |
|---|---|---|
| event_id | 入站服务 | 同一个外部事件去重 |
| task_id | 业务调度 | 一次业务待办，含租约与 due_at |
| run_id | NexLoop | 一次目标上下文下的执行 |
| submission requestId | RuntimeAdapter | 确保同一个 Run 不被重复提交到 Pi |
| business_intent_id | 受信任的计划/意图服务 | 表示同一个业务意图，跨重试/重启稳定 |
| action_receipt_id / attempt_id | EIOS | 已受理动作与每次外部尝试 |
| provider_reference | 外部渠道 | 查询是否受理、生效、送达 |

`tool_call_id` 只用于模型轮次，不作为业务去重唯一依据。相同逻辑意图重新生成工具调用时仍找到同一 business_intent_id。改变目标版本也不自动生成新的发送幂等键；需要新的真实意图时显式 supersedes，并先处理旧意图状态。

相同 idempotency key＋不同规范化 payload 必须返回409，不覆盖旧请求。不同有效业务意图可以合法发送相同内容，不能用全局文本 hash 一刀切去重。

## 3. 意图状态与执行状态分开

意图：`proposed → validated → accepted`，或 `blocked/cancelled/superseded`。

执行：`accepted → dispatching → succeeded|failed|unknown`。必要时 `unknown → reconciling → succeeded|failed|still_unknown`。补偿是关联的另一项受治理 Action，可标 `compensated`，但不删除已发生事实。

`accepted` 只代表 EIOS 已接收并持久化；`succeeded` 还需定义业务语义。消息分别区分 provider accepted、delivered、read；服务分别区分 requested、fulfilled、confirmed。

## 4. 提交流程

1. Agent 通过受限工具网关请求一个注册 Action，不提交可自定义权限的主体。
2. 服务端解析 Run-bound 身份，查找/建立稳定业务意图；检查参数版本和 payload hash。
3. EIOS 在短事务中校验当前权限、目标/计划有效性、消费者最新约束、预算、资源版本及冲突；持久化 ActionIntent 与 outbox。
4. Action Worker 领取工作，获取 fence，执行前再次校验可变控制条件；不在 HTTP 调用期间持有长数据库事务。
5. 外部系统支持幂等时传 provider idempotency key；记录 provider reference。无法确认时标 unknown，交给对账。
6. Receipt/Event 更新后，NexLoop 的运行/工作台读取权威结果；Pi 工具返回 receipt 而不是自己宣布业务完成。

同库的 EIOS 与 NexLoop 意图注册尽量使用共享受治理 UnitOfWork；存在异步边界时使用 outbox/inbox，不把两次独立提交描述为原子。

## 5. 并发与 fencing

同一消费者的触达、同一权益/订单和同一预算以资源键串行化关键提交；取得多个锁时固定顺序。Run lease 和 Action lease 分开，前者不能直接授予外部执行权。

Worker 更新结果必须包含匹配的 fence；租约过期重新领取后旧 Worker 的结果提交被拒绝。对于已经离开系统的外部请求，DB fence 不会自动撤回它；仍依靠 provider 幂等和查询，无法确认时不能新发。

PostgreSQL `FOR UPDATE SKIP LOCKED` 可用于短事务领取队列；实际 SQL 和角色授权需由集成测试验证。[S12]

## 6. Pi 集成要求

用 `RuntimeAdapter` 封装 start/resume/inspect/cancel，外部模块不依赖 Pi 具体对象。模型、工具、提示片段与 hooks 按所冻结版本的实际 API 接入。业务工具白名单不安装 CodingTools，不暴露 bash/read/write 等生产主机能力。

Pi 当前同 requestId 可以返回已有 submission；中断工具只有声明 `replay: safe` 才会自动重跑。[S04] `execute_action` 只有在“提交/查找同一意图”协议幂等经过验证后才可作为安全重放工具；不能把“重新发送消息”直接标 safe。查询工具也必须保持租户和权限。

Pi 会话/任务不是消费者长期记忆。一个 Run 关闭后，其重要结果进入 PG/EIOS；下次 Run 由 Context Engine 重建上下文，不复制全部历史会话。

## 7. SQLite 例外和 owner

首版每 Run 独立本地存储文件，由唯一 Agent Host 打开；每个文件单进程持有，并用操作系统锁阻止第二个 host 容器同时访问 runtime 目录。启动先获得全局 owner 锁，再恢复登记的活跃 Run。

默认 SQLite NORMAL 不能当作主机掉电不丢提交保证。需要在实际 Pi SQLite 连接设置 FULL、运行官方 conformance suite 并做 kill/reopen 验证；上游缺少配置时记录小补丁及升级测试。[S04] 不把 SQLite 文件放 NFS/SMB；不在运行中只复制 `.sqlite` 丢弃 WAL 当备份。

Run 文件丢失时：PG 中标 `runtime_state_missing`，保留业务意图与证据，先对所有未确认 Action 对账，再创建新的 reevaluation Run；不能假装原推理可无损继续。

## 8. 故障矩阵

| 故障位置 | 应有状态 | 恢复原则 |
|---|---|---|
| 入库前 API 崩溃 | 未接受 | 客户端按 provider_event_id 重投 |
| 已入库未通知 | event accepted/outbox pending | relay 或轮询补偿 |
| Run 已分配未提交 Pi | run queued/starting | 相同 run_id/requestId 重试 |
| 模型返回但状态未提交 | Run 未完成 | 重试可能产生不同文本，不能据此制造第二业务意图 |
| Action 已入账未外发 | accepted | 领取后重新核验控制条件 |
| 外发成功但本地未记录 | unknown | 查询 provider reference/幂等结果，不盲目新发 |
| 权限/目标在等待期改变 | stale/blocked | 重新评估，不恢复旧行动意愿 |
| PostgreSQL 不可达 | admission/execution blocked | 不接受新业务成功、不执行新外部写 |
| Valkey 不可达 | degraded | PG 轮询继续，缓存可重建 |
| Agent Host 重启 | owned runs pending | 同一 owner 重新打开、先核对业务状态 |
| CI/发布中断 | release incomplete | 保留旧服务，核对迁移及镜像，不跨版本并行打开同一 Run 存储 |

## 9. 取消和人工干预

取消分为停止新推理、取消待提交意图、请求取消已受理任务和外部补偿。不能把一个 cancel 按钮当作全部动作逆转。立即控制路径不经过 LLM；负责人暂停动作改变控制 revision，dispatch 校验必须读取它。

首版由注册策略明确哪些可自动执行、哪些需额外批准；“Agent 可以做所有事”是业务自主性，不是覆盖所有未注册能力和高权限操作。

## 10. 隐私与审计

运行文件可能含对话原文。按保留策略关闭并删除终态文件、清理备份到期内容；活跃任务删除请求先阻断并处理未完成动作，再执行数据删除。删除后的审计仅保留必要 ID/hash/状态，不借“可追溯”永久保留敏感原文。
