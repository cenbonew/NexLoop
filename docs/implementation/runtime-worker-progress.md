# NX-014 常驻 Worker 与恢复边界

2026-10-08。当前代码已经完成独立 Worker 进程、受控 Host/Pi 恢复和未执行任务恢复；完整本地 CI 第二轮因未登记0040草稿被自动加载而失败；正在推进登记0040后的固定源码完整复验。任务验收与 AT-039 业务对账分开，product_ready=false。

## 实际改动文件

`packages/eios-core/src/nexloop_eios/runtime_worker.py` 提供常驻/once CLI、独立 EIOS guard、私有文件配置、每 tick/guard 重新认证、SIGTERM/SIGINT停止后续claim、当前原lease/fence收尾与固定脱敏输出；`packages/eios-core/pyproject.toml`安装console entrypoint。运行说明见 runtime-worker-operation.md。

`runtime_control.py`在TLS/header读取与线程创建前限制8个连接，finally释放；invalid certificate在bind前失败。实际idle TCP超过8被关闭，释放后真实TLS调用可继续，许可口没有被空连接占用到无限线程。

追加 `0039_runtime_execution_marker.sql` / catalog，原38项SQL/catalog hash均保持，所有39 checksum核对。marker跨activation/fence单调，完整signed model/tool授权、marker与末尾TTL/proof/currentlease复核同PG事务，commit后才allow实际执行。最终lease失效会回滚marker；应用不能直接读/删或调用重命名inner。`versions.lock.json`仅bootstrap_revision从0038推进0039，上游三个commit、Pi对应关系和已有image记录未变。

`runtime_dispatch.py`在reclaim缺文件时不再把attempt次数等同执行历史：snapshot及fresh start授权均要求strictfalse，Host独立再确认。markertrue缺文件技术失败，保留业务对账需求。

`apps/agent-host/src/runtime-host.ts`与`pi-runtime-adapter.ts`要求完整strictboolean授权metadata，model/tool响应必须true；真正创建存储只允许最新false。已有receipt仍幂等。空目录/空SQLite仅无durable conversation/task历史时可初始化同Run；orphanWAL/SHM保留拒绝，liveMap inode丢失仍拒绝；运行目录不可通过model输入指定。

实际测试文件：test_runtime_worker.py、test_runtime_execution_markers.py、test_runtime_dispatch.py、test_runtime_guard_transport.py、test_wheel_install.py、Pi adapter/process helpers，以及head断言bootstrap/core sandbox/doctor/db boundary/compose bootstrap。README和Host使用说明已同步。

## 真实测试与首次失败

- 独立PythonCLI+PG+TLS NodeHost最终19 passed52.49s：once完成、idle/inflight SIGTERM、先kill实际Host再killWorker后同submission/Generation恢复、数据库撤权/文件credential轮换、wrongrole、invalidCA/privatefile/bind无claim、markerfalse/true缺文件、两种安全半初始化后同Run/request单submission与FULL/WAL。新增partial两例首次2 passed12.10s。
- 初始CLI测试4pass/5fail42.38s，fixture误用lease2低于minimum3、初始状态错写queued而非pending；纠正后通过。扩展11pass/1fail23.72s，先killWorker导致仍活跃Host记录真实guard失败；改为目标故障窗口Host先kill，随后15/17/19例通过，没有弱化成功断言。
- RuntimeDispatcher最终17 passed29.58s：真实未获执行许可的reclaim可以同Run启动；actualmodel许可已commit后缺文件拒绝。这不表示provider已调用，marker只证明获权。
- SQL marker+guard/bootstrap28 passed20.09s，包含monotonic/newfence、源撤权、最后检查expiry整笔回滚、直接角色旁路拒绝。
- preTLS并发界、noneditablewheel新CLIhelp、正式Host与nativePG/Pi bridge共10 passed21.37s；Root首次命令猜错不存在test_pi_runtime_pg.py，exit4/0tests，发现实际test_runtime_pg_bridge.py后正确重跑。
- actual frozenPi Harness/SQLite25 passed2.50s，覆盖strictbool、markertrue缺存储拒绝、已有receipt重放、半初始化与orphanWAL保留。半初始化是明确构造的synthetic存储状态，真实kill/reopen另有CLI/Host证据。

实际命令：`source ~/.nvm/nvm.sh && nvm use && uv run pytest tests/test_runtime_worker.py -q`；`uv run pytest -q tests/test_runtime_dispatch.py --tb=short`；`uv run pytest -q tests/test_runtime_execution_markers.py tests/test_runtime_guard_transport.py tests/test_bootstrap.py --tb=short`；`source ~/.nvm/nvm.sh && nvm use && pnpm --filter @nexloop/agent-host build && pnpm exec vitest run apps/agent-host/test/pi-runtime-adapter.test.ts`；prerequisites完整命令与Junit见runtime-worker-evidence.json。

## 边界与下一步

HTTP有绝对deadline，PG沿用实际验证的10s statement/3s lock timeout；循环预算不代表whole函数严格wallclock。此入口不bootstrap/migrate/provision、不管理Host或原系统、不发送业务效果；结果runtime_only/business_action_success=false。

NX-014原交付物是稳定Run/request、FULL、单owner与持久恢复，应依据自己的真实证据验收。AT-039额外含业务对账，继续not_run；NX-015～017负责稳定业务意图、EIOS ActionGateway、query/reconcile和渠道闭环。真实DeepSeek/costpolicy/渠道验证尚未执行，未读取.env或调用provider。stage当前镜像/部署未在0039复验，本地证据不代替双机部署；无stage/commit/push/生产迁移。


完整CI首次1103 passed/1 failed1067.14s：partial emptySQLite测试先观察running，再外部Host inspect时Worker已finish，guard正确拒绝过期lease；不放宽权限，也不简单当作随机失败重跑。新增Worker真实Host终态receipt向PG result.runtime_receipt持久化（request/conversation/submission/FULL），strict验证persistence字段与类型；unknown不伪造receipt。测试改为终态PG记录关联实际SQLite，19CLI与17Dispatcher分别重跑通过。固定源码完整CI重跑中，首轮退出1且后续SQLite/Pi CI段未执行，不能报全通过。

第二轮完整CI终态：925 passed/16 failed/167 errors/1 warning，885.70s。Root要求只不登记catalog但允许0040.sql留在migration目录，实际loader扫描全部SQL，造成bootstrap source checksum mismatch。不是私网/凭据泄露，也不能当作测试通过；草稿已移出包目录。随后将按明确登记的0040稳定源码跑focused PG与完整CI，保留两轮失败记录。

固定head0040完整CI终态exit0：1146 Python1068.32s（1warning）、24实际SQLite、25实际PiRuntime1.92s、零关键跳过，Web/Host/四Pi包构建全部通过。保留此前所有失败记录。Runtime恢复交付完成不等于AT039业务对账或NX015真实效应完成。
