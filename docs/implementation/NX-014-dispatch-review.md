# NX-014 原子入站与独立 Worker dispatch 审查

2026-10-08，审查文档；不修改源码/planning，不访问生产系统/凭据，不把尚未运行的集成当完成。

## 原子入站的审查要求

`accept_runtime_event` 应在同一个受限 EIOS 事务中完成已授权 queue accept 和 Run enrollment：任何 register/proof/绑定错误都回滚 invocations/jobs/events/inbox/outbox/enrollment，commit 前不 ACK。同 event 相同全 payload/input/command 重投返回同 task/绑定；不同 payload 必须冲突。签名 proof 和当前 EIOS 全许可链在取得目标锁及最终返回前再检查，不能以事务开始时允许替代最终提交授权。只存 Run digest 与不可变引用，不持久化 raw token；RunBearer 只由可信 API 在内存认证。

## 冻结 Pi 的实际 outcome 语义

读取 `vendor/pi/packages/durable/src/types.ts`、`harness/harness.ts`、`harness/generation.ts`、`harness/live.ts`。

- Submission `done` 表示用户输入已获答复；不是业务 Action 成功，也不能单独证明所有 Tool 成功。generation 在工具失败后仍可能把错误交给模型并完成答复。
- `TaskState.completing` 已决定 outcome，但其普通子工作尚未全部 terminal；不能按完成处理。仅 `terminal` 是持久结果回执。
- `TaskOutcome.status` 为 completed/failed/aborted/orphaned/faulted；completed 与 failed 均是任务终态，只有前者是相应任务成功。
- Harness.inspect 只列未完成 tasks 和 queued/placed submissions；tasks 为空不证明成功，也不返回所有历史失败。

### 最小正确的公开 API 推导

每 Run 独立 Harness/固定 conversation。使用公开 `Harness.commit(async tx => ...)` 的 `tx.scanTasks({conversationId}, limit, cursor)` 只读分页历史 TaskRecord；必要时同读事务读取 submission 和 answer entry，避免拆读产生不同快照。该 Tx 接口在 frozen `types.ts` 公开，不直接读 SQLite 私表、不修改 Pi 源码、不使用 Memory。

1. input submission 仍 queued/placed：runtime running，不提交成功。
2. 任一关联 task pending/running/waiting/completing：runtime running；父任务有 outcome 仍不足。
3. submission unanswered：根据 reason 明确 runtime failed/cancelled；不得 blind retry 业务效应。
4. done 且 answer entry存在、`byTaskId` 指向真实 generation、该 task terminal completed，同时有至少一个 generation，全部关联 task 都 terminal completed，才报告 runtime succeeded。
5. 任意关联 task failed/faulted/orphaned/aborted：报告 runtime failure/cancelled 或对应明确状态，不覆盖成 succeeded。对于未来 background/子conversation 工作，需明确归属与检查范围；当前不能无说明忽略。

Worker ledger result 必须明确 `runtime` outcome，而不是 `business_action_succeeded`。Action ledger/真实效应证明属于独立受治理执行链；unknown external result 仍 query/reconcile，不从 Pi done推导重试或效应成功。

## 独立 Worker 必须保持的边界

claim/create/Host inspect/start/resume/renew/fenced finish 全程使用自身受限 Worker credential，Host无DSN/RawRun；activation_ref只对应预绑定Run和当前fence。重开本地SQLite不能更换command/input或派生grant。Host失联/返回不明时先inspect原Run，不直接新建；最终账本写由live lease/fence保护，旧worker不得覆盖。runtime失败与外部未知状态分别表达。

实际命令：只读 `rg`、`sed` 查上述冻结公开API。已向Root发送可复核的最小outcome推导。本次尚未审阅正在编写的原子accept和Worker源码，等待文件出现。

## 原子 accept 代码复核

最新 `RuntimeActivationPort.accept` 与 Backend `accept_runtime_event` 已读取：先真实认证RunBearer，生成Run/queue当前完整proof；同一个受限pool connection的 `db.transaction()` 内先调用旧queue definer，再用definer实际返回task_id签名register；离开transaction完成commit后才return，不在中间ACK。最终register再次检查全部proof、Run/source identity及command.not_after。Run token只在认证入口内存使用，持久enrollment只含digest。

首次审查指出payload conflict被broadexcept抹成AuthorizationUnavailable。实现Agent已补已知SQL `queue_payload_conflict` / enrollment conflict 映射为固定 QueueConflict，其他错误仍脱敏。这保留未来API409语义，不把确定冲突当可重试依赖故障。

旧standalone register只比run_command，没有检查已有payload.input与enrollment input_digest。新增独立0038 wrapper保留0037字节，原inner先完整核角色/签名/当前授权/Run，再锁实际task检查如果payload.input存在则必须string且SHA256与input_digest一致；最后重复inner回放做最终授权时效复核。任何失败均在调用者原事务回滚，不留下半enrollment。

0038初版发现PLpgsql变量 `result` 与runtime.jobs.result同名，WHERE job_id=result->>task_id可能column ambiguous；已通知实现Agent修为独特变量名。未运行本次PG用例，实际结果由执行Agent留存。

fresh复核已确认0038改用v_result，search_path仅pg_catalog并row_security=on。实现Agent报告固定40项联合真实PG回归48.60秒通过，旧36字节/38 hash也复核通过；这是其执行报告，本审查没有自行执行或将其扩展为完整Worker闭环验收。

## 最新 Pi inspect 代码复核

已看到adapter采用public tx.scanTasks分页历史，超过1024保守unknown；非terminal保持running；failed/faulted/orphaned为failed、aborted为cancelled；input done还要求answer entry对应真实terminal completed generation才runtime succeeded。它不宣称businessAction成功。本次未运行Vitest或Worker集成测试；Root新增runtime_dispatch文件仍待出现后逐行复核。

## 独立 Worker 首次源码复核

已读取 `runtime_dispatch.py`：通过受限Worker claim、create activation、每次请求前assert current lease并周期renew，最后assert+fenced finish；直接HTTPSConnection，不读环境proxy、不跟redirect，私有key/CA读与loopback固定origin；socket计时器限制请求总deadline、响应64KiB上限；claim attempts>1遇missing state不start。结果明确scope runtime_only/business_action_success false。

必要问题已通知Root与实现Agent：

1. client强制响应Content-Length且拒绝chunked，但首次读取Root Host respond未写Content-Length，Node HTTP/1.1通常分块；必须统一framing，否则actualHost全被判transport unavailable。
2. 初版inspect unknown直接finish failed，总deadline耗尽也可能finish failed，但Host尚可能在执行；unknown不是terminal failure，需sameRun retry_wait/reconcile或确认cancel终态后提交，不得将不明观察当失败。

transport失联已用retry_wait而非新Run，已发生效应仍不能blindretry。原任务业务意图/渠道效应不由此runtime dispatcher重发或推导成功。本次未执行Worker用例，待最终源码与实际PG/Host测试复核。

fresh再次复核，client已支持受限chunked与Content-Length，两者同时出现/重复framing/Content-Encoding拒绝；body解码后最大64KiB，socket总deadline保持。unknown与dispatcher总timeout改为retry_wait，保留same Run及unknown结果；明确failed/cancelled才terminal failed。先前两项必要问题已见修复。

已读到 `tests/test_runtime_dispatch.py` 的实际PG→HTTPS Host→Pi用例：正常runtime-only完成、reclaim缺文件拒绝start、已存在submission复用、真实PG renew、Host失联unknown retry、错误私有key脱敏、SIGKILL in-flight generation后新Backend/新fence保留submission和原task。它们没有用Host响应stub替代集成；延迟guard只增加合成延迟，当前授权仍调用真实worker。本审查没有执行这些用例，终态实际运行报告由实现/测试Agent提供。

仍不能由这些runtime-only结果推出EIOS实际Action或渠道效应成功；后续真实效应unknown reconcile/独立Action lease与受治理业务账本验收保持独立。

## 固定源码最终只读复核

Root补充ToolResultEntry检查已读取：公开 tx.scanEntries 遍历该Run所有task归属conversation（含主conversation）；即使Pi ToolTask outcome是completed，持久toolResult.isError或ToolResultEntry.data.diagnostics severity=error也将runtime_outcome判failed。冻结tool.ts明确isError仍complete、appendToolResult持久diagnostics数组，因此仅扫TaskOutcome不足，新增检查覆盖这一真实语义。不会把transcript内容返回Dispatcher；超1024条保守unknown。Root报告对应20项实际Pi用例通过，本审查未执行。

最终Worker测试源码已读取12项：7项真实PG/Host/SQLite/owner恢复，再加4项明确transport-only的redirect/oversize/trickle-header/silent-TLS deadline用例，以及受限Worker连接SHOW statement_timeout=10s、lock_timeout=3s、session_user=nexloop_domain_worker。total_timeout是调度循环预算，每个HTTP另有绝对deadline，PG操作按既有10s statement/3s lock超时，不宣称整个run_once硬30秒。测试执行结果仍由执行Agent/完整CI证明，本审查未运行这些命令。

## 下一最薄 Gateway：NX-015 的实际接入点

已读取live任务NX-015（not_started，deliverable为payload冲突/unknown/provider查询/撤权重验）及 `NX-015-effect-source-audit.md`，并复核当前代码接口：

- Backend目前有create_object等具体受治理方法，没有通用execute_action或效应provider。Gateway需新增实际submit/find intent端口，不调用预期但不存在的函数。
- 已有PostgresActionDefinitionReader.get/get_with_schemas读取发布契约；govern_published_action组合冻结ActionGovernor和真实PostgresActionClaimPort。当前no-approval、空Action-specific policy_refs才支持，审批/特定policy evidence缺失必须failclosed。
- PostgresActionClaimPort提供reserve(ActionClaimRequest)、mark_retryable(ActionClaimRetryableCommand)、finalize(ActionClaimFinalizeCommand)。ClaimBinding固定invocation_id=可信stable intent_id、canonical_request_digest、发布Action reference/capability、adapter_id、target_system；新tool_call_id不能改变这些字段。
- Action reservation有tenant/world/action/intent唯一键、revision/fence、terminal replay和principal绑定。跨角色共享intent的归属/读取规则必须明确且每角色当前授权；不能删除principal检查或用fake human来共享claim。
- TerminalOutcomeStatus仅succeeded/permanent_failure/compensated。unknown/dispatching应是独立持久effect attempt状态，不能伪造terminal失败后mark_retryable盲发。
- vendored eios.actions.write_attempts提供ExternalWriteAttemptStore Protocol、STARTED/UNKNOWN/SAFE_TO_RETRY等状态与revision CAS契约，没有当前extracted PG store/迁移。可借用状态机语义，但不能把Protocol当数据库实现或复制上游领域RPC。

建议只接一个明确发布的nexloop Action与一个受控独立HTTP durable provider，先实现真实PG稳定intent/409冲突/受理outbox，再通过受限Actionworker在dispatch前完整EIOS许可及controls重验、短事务落dispatching与provider stable key；事务退出才HTTP。provider受理后kill恢复时query同key，confirmed才写原receipt，未知保留unknown；证明未受理且当前许可有效才允许协议化重试。Pi execute_action只有该submit/find协议完成实际幂等和恢复测试后才replay safe。

controlled-provider fault证据、实际服务交付和真实渠道验证分别标注；渠道凭据缺失不阻止Gateway/PG/query恢复代码与测试。消费Run lease只保护调度，不能替代Action独立许可/lease，Runtime不接DB/channel secret。这里没有生成空模块或修改固定CI正在检验的任何源码/tests。

## 常驻 Runtime Worker 与 NX-014 完成边界

本轮只读读取 `runtime_worker.py`、`test_runtime_worker.py`、private_configuration、assembly 与 runtime_control。Worker启动使用显式私有文件：有所有者/mode/no-follow/长度校验，没有环境DSN或credential fallback。SQL连接须domain_worker或scheduler且current_user=session_user、无elevated membership；每tick及每个guard请求重新authenticate实际EIOS credential。启动配置/认证/guard bind失败均在首次claim前。SIGTERM/SIGINT置stop，阻止后续claim，不取消当前Run；当前Dispatcher仍以原lease/fence受限收尾，不重建submission。stdout/stderr仅固定ready/不可用及allowlist状态，不打印Run/task/输入/token/原异常。

首次测试源码已有真实PG/NodeHost的once消费与idle SIGTERM、bind失败/公开credential/已撤权credential不claim、四类无效时间配置。尚待执行Agent的真实结果；本审查没有运行这些测试。已建议补充in-flight SIGTERM保留原fence/submission且后续任务不claim、启动后credential撤权/轮转、错误SQL角色拒绝，用于证明新常驻入口而非重复既有Dispatcher证据。

具体资源缺口已通知Root：`runtime_control.py`的ThreadingHTTPServer在TLS握手/headers前创建线程，但8个slots仅do_POST取得，3秒socket timeout不能限制同时存在的线程数量。需要连接层非阻塞容量限制、超额关闭socket与线程finally释放；当前不可把POST容量称为整个TLS listener的并发上限。此项属于新常驻入口可靠性边界，未发现它直接绕过EIOS许可链。

live NX-014原deliverable是“稳定run/requestId、SQLite FULL、单owner、恢复测试”。已读取runtime-dispatch-evidence.json及planning中固定head0038的实际PG/Host、SQLite FULL、OS owner锁、in-flight SIGKILL/backend reopen同receipt/submission/task和完整CI1076 Python/24 SQLite/20 Pi记录：这些足以支持NX-014原任务完成，不应以尚属NX-015的真实效应Gateway或额外业务对账永久维持in_progress。证据是其他Agent/Root执行报告，本审查未重跑或自称执行。若Root本轮将常驻Worker与stage assembly作为额外交付，则先完成其新增验证再更新done。

AT-039期望还包括业务对账；当前runtime_only结果与product_ready=false必须保留，AT-039额外业务reconcile仍not_run/相关验证blocked，不能随NX-014 done自动标passed。真实模型、渠道与生产部署证据也独立记载，不能从Runtime恢复测试推导。

## 执行前崩溃恢复与拟新增0039 marker

docs06§8明确“Run已分配未提交Pi”应相同run_id/requestId重试。attempts>1只代表领取次数，不代表模型/工具已经执行；因此当前一概拒绝reclaimed missing会把claim后尚未Host启动的崩溃误判为丢失执行状态。拟用PG execution marker区分，方向符合原阶段目标，不改变既有0038。

审查要求已同步Root/实现Agent：marker必须单调，绑定稳定Run/enrollment并跨activation/fence保留。所有实际model/tool入口（含deferred poll和safe tool recovery）在完整signed EIOS许可链成功后，同事务写marker，并在commit后才返回authorized；最终proof/TTL/当前lease/fence检查失败必须整体回滚。普通inspect/start/auth读取不能提前写marker，也不能开放可由调用者删除或伪造marker的直接端口。

create返回ever_execution_authorized=false只是当时快照；missing→start前须在当前activation服务端再次确认无marker，不能只相信缓存布尔值。缺字段、非严格bool、查询失败均failclosed。已有marker的缺失状态继续runtime_state_missing并留存原业务意图/证据，不自动派生grant或盲目重建。无marker仅证明没有模型/工具取得执行许可，不证明SQLite从未存在；允许的是原Run/request尚未执行工作重新落地，而不是声称丢失推理无损恢复。

需要真实PG故障证据：claim/create后未启动Host kill→reclaim同Run/request成功；已执行授权后删/失去文件→禁止start；旧fence或失效proof不能写marker；markercommit后返回丢失/进程崩溃→仍保留禁止重建；并发marker与缺失start检查不接受过期false快照。目前为设计审查，尚未宣称代码或测试完成。

### 0039 与连接限制 fresh 源码复核

已读取实际0039：首次旧链校验后取得Run advisory transaction lock，锁后再完整校验；固定enrollment tenant/world/command digest，model/tool授权才insert marker，最终第三次旧链检查后返回严格bool。marker表与旧inner均revoke全部application直接权限，wrapper只授api/scheduler/domain_worker执行；marker无删除/重置端口。标记与最终许可失败同事务回滚，跨新activation/fence沿用原Run。未发现新增可复核授权绕过或锁顺序环；此结论限于当前不可变Run/task绑定与独立受限调用链。

Dispatcher已在missing分支先检查create snapshot false，再真实authorize(start) false；Host guard每次请求核严格bool，model/tool必须true，Adapter实际start再authorize而非只信Worker旧响应。Root报告22 Pi、7 PG marker、17 Guard与bootstrap联合28通过，CLI17/Dispatcher13通过；本审查仅读源码与这些执行报告，没有自行执行或扩展成真实业务效应成功。

Guard已在process_request生成线程之前非阻塞取得8个connection slots，超额socket关闭；线程finally及线程启动失败均release，覆盖TLS和header阶段。原先POST-only容量缺口已见修复。

已通知Root一个具体剩余恢复缺口：本次读取Adapter open时，目录已存在但runtime.sqlite不存在仍无条件state_missing；SQLite已存在但无binding同样拒绝，即使fresh服务端marker为false。mkdir之后/文件创建之后、binding之前崩溃属于尚未执行模型/工具的部分初始化窗口，不等同已有marker的推理丢失。需要补该窗口实际测试并明确支持范围或修复，不能仅凭claim-before-Host测试宣称所有执行前崩溃均恢复。已有marker的缺失仍须无条件拒绝。

### 部分初始化缺口修复复核

fresh源码已修上述两种窗口：现有私有Run目录缺主文件，只在create与当前服务端marker=false时用600/O_EXCL/O_NOFOLLOW创建并fsync文件/目录；任何orphan WAL/SHM原样保留并拒绝重建。现有SQLite无binding，只在相同条件且公开Harness Tx scanConversations/scanTasks均无记录时允许初始化；非空orphan、marker=true、resume均拒绝。已经在liveMap打开的Harness若文件/inode缺失仍拒绝，不用该恢复分支覆盖当前实例。

已读Node两种partial初始化同Run/request恢复与orphan WAL保留用例；Root报告固定25项真实Pi测试2.50秒通过，本审查未执行。测试Agent正在追加实际PG/CLI partial窗口用例，执行结果须独立留存。当前已见原具体源码缺口修复，没有新增可复核授权/锁序/秘密输出缺陷；OS服务owner保护与PG marker/currentfence限制仍为明确前提。

### 终态回执补丁与竞态修复复核

已只读最新runtime_dispatch.py及两个测试文件：inspect响应必须持久化声明为精确字段集合journal_mode=wal、synchronous严格int且值2；缺失、额外字段、NORMAL或字符串2均拒绝。已知succeeded/failed/cancelled才提取allowlist request_id/conversation_id/submission_id/FULL状态到受限PG finish的runtime_receipt，unknown/transport失败不生成回执。run/request与当前command匹配、ID格式检查、最后assert lease与fenced finish保持，不放宽身份或旧fence权限。

partial测试改读已授权inspect_task的持久终态回执，并在实际SQLite核对conversation/submission唯一记录；不再于终态finish后借已失效activation另读Host。这修复测试外inspect与Worker finish竞态，没有把失败状态改成成功或取消持久化验证。两个partial是明确合成中断初始化，不冒充真实进程kill证据。Root报告最新17 Dispatcher 29.58秒、19 CLI 52.49秒通过；完整CI首次1103 passed/1 failed为该外inspect竞态，固定源码全量重跑仍待Root报告。本审查不跑额外重叠PG suite，也不自称执行这些命令。
