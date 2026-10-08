# NX-015 / AT-033～039：实际完成条件与剩余切片

2026-10-08，只读 `planning/tasks.json`、`planning/acceptance-tests.json`、冻结 `docs/handoff/docs/14_TEST_ACCEPTANCE.md` 和当前 0041/0042、Backend、effect/runtime源码及测试。本报告不更新状态，不执行provider/生产，不修改源码、迁移、锁或planning。测试代码存在不等于已执行通过；下面按已收到终态区分证据。

## 任务原文与当前证据范围

NX-015 的 deliverable 原文是“幂等payload冲突、unknown、provider查询、撤权重验”，依赖 NX-012/NX-013，当前 `in_progress`。任务不要求仅构造 Intent，更不允许把 Runtime done、HTTP accepted 或合成provider的 fulfilled 当成真实客户服务已兑现。

0041 已实现实际 Backend owner/planner registrar、正式受治理 Consumer/Goal/PlanStep/EffectControl、真实 Run/独立executor绑定、distinct A/B 的同Step权限集合、跨Step共享预算。`uv run pytest -q tests/test_effect_contexts.py tests/test_effect_intents.py --tb=short` 最终26passed32.90s：包括真实Goal.edit revision失效、control行锁等待后真实proof过期整事务回滚。公开 `effect_plan_schemas()` 提供 schema 模型，但返回模型不是正式生产治理发布流程。

0042 当前实际 `EffectExecutionPort` / `EffectDispatcher` 与 signed definer 已装配：固定executor/current source双proof、实际 Actionclaim、独立effect lease/fence、先PG提交attempt再POST、reclaim query-first、READ observation与EXECUTE finalize分开。原source过期/撤权时，独立QUERY可记录 `observed_fulfilled`，不能将原EXECUTE claim标成功。`read_effect_receipt` 仅当前固定executor/query权限读取已governed-finalized历史receipt，不复活lease，不作provider IO。

0042已确认的focused终态为 SQL5passed4.27s、head/bootstrap/wheel/roles prerequisites22passed20.30s（`.ci-results/effect-execution-head-prerequisites.xml`）。dispatch初次8 errors9.64s为fixture context_ref映射错误；修capability绑定后单case1passed1.58s；8suite7passed1failed34.57s为result遗漏provider_state，已补。含terminal ACKloss3新case的11case suite最终 `uv run pytest tests/test_effect_dispatch.py -q` 为11passed48.95s（session27898 exit0）；**这是实际PG+独立spawn合成HTTPprovider范围，不宣称head0042完整CI通过**。所有provider是独立持久合成HTTP故障服务，不是生产渠道交付。

head0040 full CI1146Python1068.32s/24SQLite/25PiRuntime已exit0，是当时源码的完整验证，不覆盖后续0041/0042。

## 逐项核对

| ID | 冻结验收条件 | 现有真实证据与实现 | 准确缺口 / 可登记边界 |
|---|---|---|---|
| AT-033 | 外部已受理后本地记账前kill；查询原意图，不重复发送 | `test_provider_commit_then_worker_sigkill_recovers_query_first` 使用独立Worker SIGKILL、实际PG dispatching attempt/fence与持久provider accepted barrier，恢复只GET原intent；0042状态机禁止orphan重新POST | 修后11suite真实终态已通过。此例形成“实际PG+合成持久provider故障恢复”证据；不是实际渠道履行证明。 |
| AT-034 | 同key不同payload；409、不覆盖原请求 | 0041升级后的真实Backend Intent测试已通过：distinct当前获准Run使用同稳定Step/slot，`EffectIntentConflict.http_status=409`，原frozen request/outbox不覆盖；SQLcanonical业务digest包含版本/语义，provider wire digest分开 | 核心Python/PG契约已有真实通过证据；若对外HTTP入口列入交付，还需实际HTTP409映射测试，不能只把异常属性称HTTP响应。当前planning仍not_run，状态由主Agent依据证据决定。 |
| AT-035 | 重试LLM产生新tool_call_id；相同业务意图仍命中原receipt | Intent业务key确实不含Run/toolcall/contractversion；0041跨真实A/B Run同Step受理共享receipt已通过 | 当前agent-host源码没有真实service.request submit/find工具，未见真实Pi两次Generation生成不同tool_call_id并经实际Backend返回同receipt的端到端用例。跨Run Python调用不能替代这个确切触发。 |
| AT-036 | 规划后执行前撤销grant；dispatch拒绝，不凭旧快照 | `test_dispatch_rechecks_current_source_and_control_zero_send` 真实独立Worker pause-before-admit，撤销实际source Run/source grant或通过正式control变更；provider POST/effects均0。0042实际admit/finalize当前proof和PG时钟复核 | 最新11case dispatch suite终态通过。建议再覆盖executor EXECUTE grant撤销与source expiry；QUERY仍可读的证据必须与禁止EXECUTE分开。 |
| AT-037 | 两个角色同时发送同意图；关键提交受控、重复副作用0 | 0041两distinct主体并发/同真实Step共享Intent/outbox/submissions与一次预算已实际通过；0042固定executor实际claimprincipal保持，effect outbox领取有fence | 当前 `accepted(fixture)` 是A后B顺序submit，Worker故障用例也不是两个角色/两个effectWorker真正同时竞态外发。仍需实际A/B并发工具/受理+两个独立真实executorWorker竞争，assert总POST1/effects1/receipt同一个且旧fence拒绝。受理去重不能单独证明重复副作用0。 |
| AT-038 | 第二进程打开同Run存储；进程锁/owner拒绝，不并发写文件 | 当前planning已passed；`pi-runtime-evidence.json` 的actual Node kernel flock/PG桥3passed5.27s，第二进程被拒绝、SIGKILL释放owner、相同SQLite inode/receipt/FULL；后续Worker CLI真实Host测试补强 | 核心owner条件已有真实证据，不因0042加入而自行撤销或泛化成business exactly-once；仍须最终release运行时包保持同约束。 |
| AT-039 | 真实存储kill/reopen；conformance与业务对账通过，非纯Memory | 实际Pi WAL/FULL kill/reopen、Run/conversation/submission稳定与真实PG lease/activation已过；`runtime-worker-evidence.json` 明确dispatcher结果scope=runtime_only/business_action_success=false。0042另有provider/Worker SIGKILL query与PGreceipt/claim对账测试 | 两条链尚分离：Pi工具→真实业务Intent→外发后kill Pi/Worker→reopen实际SQLite→QUERY原intent→PGoriginal receipt/claim一致的完整conformance未见。不能将runtime_only恢复与单独effectWorker恢复相加宣称该验收全部通过。 |

当前 acceptance planning 精确状态：AT033/034/035/036/037/039均 `not_run`；AT038 `passed`。本报告只分析，不将代码存在、局部通过或局部suite直接写成完整passed。

## 下一最薄可运行切片

优先接一个真实Pi `nexloop.service.request` submit/find工具到受限Backend，而不是再加空模块。工具仅传 effect parameters/receipt_ref；tenant/world/Run/consumer/Goal/Step/slot/executor均由实际opaque activation与可信已登记上下文解析。Backend在实际tool dispatch重查activation的当前lease/fence/Run与EIOS许可，再调用真实Run-bound Intent port；Node仍无DSN、raw Run token、signer或provider credential。不能在Runtime tool的test callback里直接“授予授权”。

使用真实Harness/SQLite FULL和deterministic模型provider驱动两个不同tool_call_id，同一稳定Step/context必须回同receipt；下一用例让A/B两个不同真实主体各自Run并发，经此工具受理同Intent。两个独立实际effectWorkers竞争，持久provider统计POST1/effects1，PG共享quota1/claim executor不变。这样最少新代码同时闭合AT035/037的触发条件。

最后在provider已提交、本地未记账窗口SIGKILL Pi/Host与effectWorker，保持真实PG及provider，重新认证并以同Run/request/conversation/submission/SQLite恢复：只能GET原provider key，原receipt/claim最终一致；缺当前EXECUTE许可只允许合法QUERY observation、业务成功false。补齐这一跨runtime/业务的实际对账，才能讨论AT039的完整登记。

独立effectWorker常驻CLI/Compose组装、正式schema/Action治理发布工具、credential rotation及真实服务provider配置是产品交付剩余项。可以继续做代码与合成故障验证；真实凭据缺失仅阻塞相应真实模型/渠道验证，不得编造实际履行。`accepted`、`fulfilled`、`confirmed` 保持独立：provider fulfilled且当前双EXECUTE证明成功finalize才是这项Action成功，仍不自动证明客户Goal或承诺已确认兑现。

## 本次实际只读核对

执行rg/sed/cat读取NX015 task、AT033～039原文与live acceptance状态、0041/0042 SQL、effect_execution/effect_dispatch、tests/test_effect_dispatch.py及SQLtest、pi/runtime-worker evidence。`rg` 搜索agent-host实际effect工具入口未命中。收到主Agent明确测试终态/进行中状态，按其scope记载；本次未自行运行上述测试，未改planning/锁/源码。

## 后续实际验证（本报告初次核对之后）

`uv run pytest tests/test_effect_dispatch.py -q` 最终 **12 passed，50.00s**（session57888 exit0）。新增 AT-037 用例让两个不同真实授权主体分别持独立 Backend 并发提交，同一 Step 得同一 receipt；两个独立 spawn Worker 同时竞争，实际 PG reservation/共享预算均为1，持久合成 provider POST1/effects1。首次竞争暴露实际 SQL40001；第一版重试未穿透冻结 Governor 的 unavailable 包装（11passed/1failed49.56s）。私有 reserve 只保留实际 SerializationFailure，再由外层重做完整 fresh claim transaction；focused1passed2.88s，完整12passed50s。未放宽 lease/fence 或原 Governor。

此范围的 AT-033/036/037 已依据实际测试登记 passed，仍不宣称真实渠道履行。AT-035/039 和完整当前源码 CI 继续待验；本报告上方对并发缺口与当时 planning 的叙述保留为历史快照。实际权威证据为 `effect-execution-evidence.json` 与 live planning。
