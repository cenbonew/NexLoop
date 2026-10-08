# NX-014：Pi RuntimeAdapter 实际源码审计

记录日期：2026-10-08。性质：只读接入审计，不是 NX-014 交付或恢复验收。未调用模型、未启动 Run、未修改生产系统。本次只增加本文档。

依据：`docs/handoff/docs/06_ACTION_RUNTIME_AND_RECOVERY.md` §5–8、`02_ARCHITECTURE.md` 的四个权威边界、`14_TEST_ACCEPTANCE.md`、`planning/tasks.json` 的 NX-014、`packages/contracts/run-command.schema.json`。NX-014 依赖 NX-003、NX-011、NX-013，交付要求为稳定 run/requestId、SQLite FULL、单 owner、恢复测试。

## 冻结来源与当前装配

`versions.lock.json` 锁定 Pi 提交 `adae8246453a2928a268ccecb9fb55125d96d0af`；运行候选是 `vendor/pi` 的 frozen-source workspace。`@earendil-works/pi-durable` 的源码声明版本为 1.0.4，但 registry gitHead/tree 与冻结源码不同；不能用 npm 1.0.4 代替该源码。

`apps/agent-host/package.json` 当前只声明 TypeScript 和 Node 类型构建依赖，没有 Pi Durable 运行依赖。`apps/agent-host/src/main.ts` 仅实现 HTTPS 健康端口、内部鉴权与 owner 文件检查，内部 readiness 明确返回 503 和 `run_admission/runtime_adapter/pg_run_lease` 等缺口。它没有调用 Harness，也没有接收 RunCommand。

`scripts/agent_host.py` 在启动 Node 前获取 `fcntl.flock(LOCK_EX | LOCK_NB)`，经 `os.execve` 传递 owner 文件描述符；Node 检查持有描述符与当前路径 inode 一致。该锁可继续作为本地单存储单进程入口，不等于 PostgreSQL Run lease/fence。

## 可用的真实 Pi API

| RuntimeAdapter 目标 | 冻结代码及实际入口 | 适配要求 |
|---|---|---|
| start | `durable/src/harness/harness.ts`: `Harness.open(storage, options, context)`、`createConversation(options, context)`；Conversation `submit(draft, context)` | 首先验证服务端 RunCommand、当前 PG lease 和预算；建立持久 NexLoop run_id → Pi conversation/submission 映射，然后提交 input。Harness 没有目标 `start(RunCommand)` API。 |
| resume | 同文件 `Harness.openTasks()`、`resume(): void`、`conversation(id, context)`、`submission(id, context)`；`scheduler.ts:239` `open(context)` | open 会把遗留 running task 的已保存 checkpoint 转为 pending；open 本身不 dispatch，resume 才允许调度。必须先重验 owner epoch、PG fence 与当前凭据，再恢复相同文件和映射。 |
| inspect | `Harness.inspect(context)`、`getTask(id, context)`、Conversation `entries/context/viewState/watch` | HarnessInspection 只包含未完成 tasks 与 queued/placed submissions，不能用 absence 判定 NexLoop Run 成功；应结合持久映射、submission settled record 和 PG 状态。 |
| cancel | `abortSubmission(id, context, conversationId?)`、`abortTask(id, context)`、Conversation `abort(context, options?)` | queued submission 可撤回，already_placed/settled 有不同结果；conversation abort 只控制 Pi 任务，不能自动撤销 EIOS 已接受 Action 或外部效应。 |
| orderly close | `Harness.close(context)`、`session/session.ts:351`、SQLite adapter `close()` | 封闭 admission、等待已受理本地 commit 后关闭 storage；正常 checkpoint(TRUNCATE) 不能替代 SIGKILL 恢复测试。 |

以上 Harness、createRegistry、defineTool、defineExtension、LiveDoc 等在 `durable/src/index.ts` 导出。`Harness.open` 要求 registry 含 GenerationTask、ToolTask、CompactionTask；使用 `createRegistry()` 保留内置任务，显式安装业务工具白名单，不导入 `durable/tools` 中 bash/read/write 工具。

`harness/types.ts:453` 的 `HarnessOptions` 要求 `models`、`registry`，可注入 settings、env、conversationCreated、now、onReport。模型从可信 Host 配置提供；不从 RunCommand 或 prompt 接收 API key。测试 provider 与真实 DeepSeek 验证必须分别标记。`conversationCreated` 在创建事务中运行且受 read-before-write 约束，可作为持久绑定设计的候选点；具体绑定 doc/字段仍需实现和测试，不能假定任意外部存储原子性。

## requestId 幂等存在关键差距

`harness/submissions.ts:155–164` 使用 `tx.submissionByRequest(conversationId, requestId)` 查重；存在同类型请求时直接返回已有 submission ID，只拒绝不同 type。**它不比较 input content 或完整 RunCommand payload**。RuntimeAdapter 必须在自己的持久映射中保存 canonical payload digest，并在同 request_id 不同 payload 时明确冲突；不能直接宣称 Pi 已满足 AT-034。

Pi 去重域是 conversation + requestId。不能在重试时新建 conversation，否则同一 NexLoop Run 会多次受理。`LiveDoc.run` 是 Pi 内部 taskId + input submission IDs，不是业务 run_id。外部 business_intent_id、入站 event_id、PG task_id、NexLoop run_id、Pi submission requestId、tool_call_id 保持各自职责。

## SQLite FULL 接入点

`storage/sqlite/node.ts` 的 `openNodeSqliteDatabase(path, options)` 使用 Node `DatabaseSync`，设置 WAL、**synchronous=NORMAL**，支持的 options 只有 walAutoCheckpointPages/busyTimeoutMs；`openNodeSqliteStorage` 直接包装该连接。因此生产适配不能直接调用后者后假定 FULL。

现有 `durable/test/nexloop-sqlite-conformance.test.ts` 的实际方式可复用：先 `openNodeSqliteDatabase(file)`，随后 `db.exec('PRAGMA synchronous = FULL')`，读取 pragma 确认值 2、journal_mode 为 wal，再 `SqliteStorage.open(db)`，最后 `Harness.open(...)`。每次 reopen 都必须重复该连接设置和检查；FULL 不是文件级永久属性。若设置或检查失败应拒绝启动，不回退 MemoryStorage。

现有 24 项 storage conformance 检查的是实际 SQLite FULL 存储契约；本次没有重跑它，也不将历史 storage 通过解释为真实 Pi Run 恢复通过。需要保留本地文件、WAL/SHM 的同一持久目录，不用 NFS/SMB，也不在运行中单独复制 sqlite 文件作为备份。

## 必须补齐的集成与故障证据

1. 创建 RuntimeAdapter interface 与 Pi 专属实现，边界 DTO 不暴露 Pi 对象；对完整 RunCommand 契约进行验证。tenant/role/world/credential 来自可信 backend，而不是 browser header 或模型输出。
2. NX-013 的 PG Run lease/fence 在 Host admission、resume 和最终结果提交前生效。Host owner 锁与 PG lease 两层都要有真实跨进程测试，旧 epoch/fence 不得提交新结果。
3. 在 Pi 本地持久文档中保存稳定 Run 绑定及请求 digest，测试重复 submit 不重复模型/工具执行、payload 冲突拒绝，重开后映射保持。
4. deterministic provider 驱动实际 GenerationTask/ToolTask；在已提交 checkpoint 后 SIGKILL，再重新取得 owner 并 reopen 同文件。核对 submission、transcript、tool attempt、PG Action ledger 与效应数，不能只检查 HTTP 又可用。
5. 工具通过受限 NexLoop Gateway 使用 Run-bound 凭据，当前撤权/过期/暂停在 dispatch fail closed；Runtime 不获得 DB/admin/channel key。未知外部结果由 EIOS 查询/reconcile，不因 Pi task 回到 pending 自动再次发送。
6. 覆盖 AT-030/031/033/034 的实际相应边界；单独记录 SQLite恢复、PG lease、EIOS effect 去重，避免用一层的通过替代另一层。取消只停止新推理，业务撤销与补偿另走 governed Action。

## 本次实际命令与范围

执行了 `rg --files`、`rg -n`、`cat`、`sed -n` 读取上述冻结文档、live contracts、planning、vendored Pi 源码、Host 与启动脚本。没有执行构建、测试、网络调用、迁移或 planning 状态修改；没有读取 `.env` 或生产目录。本文档是接入设计证据，NX-014 仍需代码和实际恢复测试。
