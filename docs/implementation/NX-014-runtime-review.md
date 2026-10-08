# NX-014 RuntimeAdapter 边界审查

2026-10-08，只读审查记录；本文件不是 NX-014 完成或验收证明。未读取凭据、调用模型、访问生产环境或修改 planning。实现文件尚在另一 Agent 开发，以下先记录实际冻结源码要求，随后对实现复核。

## 已核实的源码与目标边界

依据 `packages/contracts/run-command.schema.json`、`docs/handoff/docs/06_ACTION_RUNTIME_AND_RECOVERY.md`、`docs/handoff/docs/14_TEST_ACCEPTANCE.md`、`vendor/pi/packages/durable/src/harness/{harness,submissions}.ts`、`vendor/pi/packages/durable/src/storage/sqlite/node.ts`、`scripts/agent_host.py` 和 `apps/agent-host/src/main.ts`。

- Pi `openNodeSqliteDatabase` 每次打开都写 `synchronous=NORMAL`；NexLoop 必须在每次 reopen 后重新设 FULL 并确认 pragma 为 2，且 WAL 生效，之后才能打开 Harness。不得依赖 SQLite 文件曾经设置过 FULL。
- Pi SQLite helper 会 mkdir 并打开数据库；resume 必须先拒绝缺失文件、缺失持久绑定或不匹配的 inode/owner，不能把丢失文件当新 Run 创建。数据库、WAL 和 SHM 均在同一私有本地目录。
- `Submissions.submit()` 和 `Submission.wait()` 内部调用 scheduler resume；`Conversation.waitForIdle/abort/compact` 也会 resume。调度域是整个 Harness，所以只在显式 `Harness.resume()` 检查一个 Run 的 PG lease 不足以保护其他旧任务。采用每 Run 独立 Harness/SQLite，或核实全域当前 lease 并在每次 Generation/Tool dispatch 再检查，不能用取消或读取便利 API 意外启动失权任务。
- Pi requestId 去重域为 conversation，且只拒绝 submission type 不同，不比较内容。RuntimeAdapter 必须持久化稳定 runId/requestId/conversation 绑定和完整 command + input 摘要；同 key 不同 payload 冲突，重开不创建新 conversation。同命令并发 admission 也不能留下多个 conversation。
- Host 的内核 flock/inherited descriptor 保证本地单 owner；它不替代 PG Run lease/fence 或 EIOS 当前 Action 权限。owner epoch 从可信控制上下文取得，不能接受模型提供的身份。
- credential_ref 可作为引用保存；Run token、模型 key、DB/admin/channel key 不得进入 Pi SQLite、prompt、事件或错误日志。恢复时重新从可信端口取得/验证临时凭据，不依赖本地持久 secret。
- RunCommand 必须按 live schema 全量验证，包括 unknown fields、UUID、mode/world、request_id、引用、预算、not_after、owner epoch。预算包括 turn/tool 次数、active timeout、cost/currency；无法证明成本上限的 provider 应拒绝，不能把空 cost 执行当验证通过。
- 工具显式白名单，不安装 CodingTools 或主机 shell/read/write。`execute_action` 只有同业务意图可查可重放协议已验证时才能 `replay:safe`；Runtime task fence 不授予业务 Action 权限。

## 当前实现与证据限制

本次初读时 `apps/agent-host/src/main.ts` 仍为 foundation-only，未受理 RunCommand，readiness 为 503；没有把此前 SQLite storage conformance 解释为 Pi Run kill/reopen 恢复。真实 Pi Generation/Tool checkpoint、SIGKILL 同文件恢复、当前 PG fence 拒绝、EIOS intent/effect 去重需分别执行并留证。

AT-031 要证明旧 worker/fence 不覆盖新结果，AT-033 要查询原外部意图而不重复发送，AT-034 要冲突不覆盖原请求；本地 SQLite 恢复不能独自证明这些 PG/效应层验收。

实际执行：`rg --files`、`rg -n`、`cat`、`sed -n` 读取以上源码和契约；未执行构建、测试或网络调用。本次只新增此审查文件。

## 首版实现源码复核（待修复及复核）

开发中的 `apps/agent-host/src/pi-runtime-adapter.ts` 和 `runtime-adapter.ts` 已出现。以下行号针对本次读取版本，后续改动可能移动。

1. `pi-runtime-adapter.ts` 第80–92行检查 turn/tool 次数，但完全未执行 maximum_cost/currency 上限。只能明确受信的 zero-cost deterministic profile 免费用预留；真实 provider 需可信成本上界/预留与核算，否则启动拒绝。
2. 第91行 actual tool.execute 仅执行 authorize，而持久 active timeout/tool 数量在 beforeTool 内检查。冻结 Pi `harness/tool.ts` 第95–105行的 execute recovery 直接 run，不再调用 beforeTool；safe 重放因而绕过其时间/预算检查。需在实际执行入口覆盖恢复，并明确 unique tool calls 与执行 attempts 的预算语义。
3. 第47–53行异步 authorize 返回后仅重查 owner，不重查 not_after。如果授权端口等待跨过命令期限，start 仍可提交 input/返回 receipt；需等待后再核时效及持久 active deadline。
4. 第70–73行在 lstat 后按 pathname 打开 SQLite，没有比较打开前后 inode；未显式拒绝 SQLite WAL/SHM 的符号链接/不合规所有权。私有目录降低跨用户风险，但需要明确本地同 UID 替换是否在威胁边界内并补文件安全测试，不能把 owner 锁等同每个 SQLite 文件锁定。

正向检查：每 Run 独立 Harness；fresh create 的 BindingDoc 与 conversation 同事务，submission requestId 重放填补 input commit 后/绑定 submission_id 前的崩溃窗口；command/input 摘要持久化并复核；每次 SQLite open 设 FULL/WAL 读回；缺文件与缺持久绑定拒绝；没有引入 raw credential 字段或 CodingTools。这些是源码事实，尚未经真实 build/kill/reopen 集成验证。

当前 `assertOwner/authorize` 是可信注入接口，实际 Host owner 与 PG lease/fence 接入尚未证明；接口约定不能替代 AT-031/EIOS 真实权限证据。已向实现 Agent 和主 Agent发送上述问题；本审查未修改实现代码或测试。

## 后续源码复核

已读到三项修复：引入显式 costPolicy、以 BigInt 固定 1e8 单位持久预留每模型请求的上界；实际 tool.execute 消耗持久次数并检查 active timeout，覆盖 safe recovery；外部 authorize 返回后再次检查 not_after。真实 provider 的 maximum_request_cost 仍需可信来源并与实际 token/输出限制匹配，声明的数字本身不证明真实成本上界。

新增必要问题：冻结 Pi 默认 `compaction.enabled=true`（`harness/agent.ts`），`CompactionTask` 在 `harness/compaction.ts` 直接调用 Models.completeSimple，绕过当前仅安装在 GenerationTask.beforeRequest 的授权、次数、成本和时间 guard。应首片明确禁用自动 compaction，或把所有 Models 请求统一守卫；已及时通知实现 Agent。`runtime-adapter.ts` 的 mode 校验也需先确认 primitive string，不能通过 String(value) 接受 String object。

再次复核源码已看到：compaction 明确 disabled；deferred fetch/poll 经过 model consume、cancelDeferred 重新授权；业务 Tool API models 访问被拒绝；mode 改为 primitive string 校验；现存 DB 开前/开后及缓存再次访问检查 device/inode，sidecar 检查私有权限/所有者/链接类型。剩余同 UID 同目录竞争替换的强威胁边界应在部署说明清楚；上述 pathname 开前/开后检查不等于原子 O_NOFOLLOW SQLite 打开。

新增脱敏问题已通知：authorize callback 和实际 tool.execute 的异常原样传播，Pi `harness/tool.ts` 用 errorText 将 beforeTool 错误写入 transcript。可信 Gateway/Provider 若抛出包含 token 的 HTTP 异常，可能进入 SQLite 和下一轮 prompt。必须将这些异常转换成固定公开 code，且 Provider stream 内部错误也需脱敏；使用合成 secret sentinel 验证失败路径不持久化、不出日志。仅没有 raw credential 字段不足以证明异常路径安全。

最终源码复核已看到上述异常包装：authorize 固定 `runtime_authorization_denied`、Gateway 固定 `runtime_tool_unavailable`；新增 `runtime-provider-boundary.ts` 对异步 stream/result 和 deferred reply 删除 diagnostics/rawStopReason，规范 errorMessage，清空 error/aborted content，并固定异常 code。已向测试 Agent 提出 sentinel 的 SQLite/WAL/SHM、transcript 和 stdout/stderr 实测要求；本审查没有自行运行这些测试，仍以其终态实际报告为依据。

## 真实预算测试暴露的 Hook fail-open 与最终修复

冻结 `vendor/pi/packages/durable/src/harness/scheduler.ts` 的 `#runtime().hooks.each` 在 handler 抛错时，如果 task 未 abort，只调用 report 后继续执行。之前把 authorize/预算 gate 放在 GenerationTask.beforeRequest/afterResponse hook，会被吞异常而继续请求 provider。测试 Agent 的真实 model-turn/cost 用例发现了这个旁路；此前只检查源码存在 hook 或工具流程正常不能证明安全闸门有效。

最终读取的 adapter 已移除安全 Hook 依赖：

- 模型 `streamSimple` 返回 lazyStream，真正调用 provider 前先 await consume；consume 先重验可信授权，再用 SQLite 事务持久预留次数/费用，之后再次重验授权、not_after 和持久 active deadline。失败不会调用原 provider。
- actual tool.execute 直接执行 consume，safe recovery 也走同一入口；不能依赖 beforeTool 对恢复路径计数。业务 Tool API 禁止 models 访问，所有 raw Gateway 错误改为固定公开 code。
- 自动 compaction disabled。deferred fetch/poll 在实际入口计数/预留，cancelDeferred 在实际入口核授权。
- `safeProviderStream` 对 terminal done/error 和 result() 均执行成本检查，校验 cost 为有限非负值、zero profile 返回零、bounded profile 不超过预留上界。对错误及超额响应先抛安全 code，避免 terminal reply 被当作成功输入下一轮 Tool；正常 stream partial 仍是业务输出证据，不把它当完整费用结算。
- Provider errors/debug 字段进入 Pi 前净化；固定 code 保留实际 budget denial，不能重新把所有预算错误归并成成功或忽略。

主 Agent/测试 Agent报告最终 18 项 RuntimeAdapter 用例通过。本审查只读取其测试与实现源码，没有执行 Vitest；正式终态命令、耗时、首次预算失败和重跑报告由运行测试的 Agent 留存。测试中的 `assertOwner:()=>{}`、`authorize:async()=>{}` 是隔离 Harness 测试替身；SIGKILL 同文件恢复证明实际 Pi SQLite 恢复，不能证明实际 Host OS flock 或 PG lease。

## 仍未证明的产品集成范围

1. `apps/agent-host/src/main.ts` 尚未证明把真实 owner descriptor、内部 Run admission 和 adapter 连接。`liveStorageOwners` 仅是进程内防重复打开集合，跨进程独占必须由现有内核 flock 启动链保证并实测。
2. authorize 的可信回调仍未证明连接 PostgreSQL Run lease/fence、Run-bound credential 及当前 tenant/world/Agent release；no-op 单测不会证明 AT-031，也不会证明 EIOS business intent/effect 路径。
3. Runtime 本地 receipt 的持久化、Pi request 幂等与 PG Run 结果提交是不同事务边界；尚需 PG admission/lease 与结果 fenced commit 的端到端证据，不能把只在本地消耗预算当完整可运行运营闭环。
4. bounded_request 的上界来自可信 Provider profile，真实模型成本/限额尚未验证；响应后发现超上界能够停止后续 Tool，但不能撤销已经发生的模型费用。真实成本硬上限仍需输入/output token 限制与 Provider 配置共同证明。
5. 文件私有权限、sidecar/inode检查和进程内 owner 防护已实现；pathname 开前/开后检查不提供对同 UID 恶意竞争替换的原子安全保证。需要清楚定义部署同 UID 的信任边界及真实 OS owner 测试范围。

这些是后续集成工作，不宣称本地 18 项测试已满足全部 S2 或实际模型/外部效应验收。本文件仅作为审查证据；本次没有改源码、planning 或发布内容。
