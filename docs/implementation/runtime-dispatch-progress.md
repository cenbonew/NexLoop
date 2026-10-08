# NX-014 原子入站与独立 Runtime Worker

2026-10-08，NX-014 继续 in_progress，product_ready=false。实现当前队列与 RuntimeAdapter 的最薄调度闭环，不把运行时完成当业务 Action 效应成功。

## 实际改动

`runtime_activation.py` / `backend.py` 增加 `accept_runtime_event`，现有 signed queue accept 与实际 Run credential enrollment 共用一个 PostgreSQL 事务；提交后 ACK，raw Run token 不入 payload。COMMIT 发出后客户端死亡属于结果未知，重放可恢复完整原记录，不能假定回滚。

追加 `0038_runtime_input_binding.sql`，独立 register 同样匹配队列已保存 input digest；非字符串/不同 prompt 拒绝。旧函数改为不可由应用执行的内部实现。原37个 SQL 与已测试 wheel 字节及 catalog 条目一致，所有38个 checksum 已独立核验。`versions.lock.json` 只将 bootstrap_revision 从0037推进0038；upstream commit、Pi package 和既有 image 记录未变。

`runtime_dispatch.py` 增加受限 Worker：claim、activation、实际 TLS Host inspect/start/resume、续租及 fenced finish。无 PG 或 Run bearer 到 Host。恢复仍使用原 Run/request/submission；reclaim 丢失 SQLite 禁止新建。unknown/网络超时保持同 Run retry_wait。成功结果只声明 scope=runtime_only、business_action_success=false。

`pi-runtime-adapter.ts` 的 inspect 通过实际公共 Harness Tx 扫描 TaskOutcome、answer generation 和 ToolResultEntry，识别返回 isError 或 severity=error 的工具结果。Pi 会把这类 ToolTask 标为 completed，因此不能仅靠 submission done/Task completed 报成功。历史扫描超过有界上限返回 unknown；不向 Worker 返回 transcript。

## 测试与首次失败

固定0038联合原子/activation回归40 passed48.60s；新专门原子负向14 passed22.60s；Root实际PG focused50 passed54.74s。首次0038 wrapper变量歧义且测试期间更改 SQL 造成MigrationDrift，固定SQL后重跑通过。

实际Pi Harness/SQLite回归20 passed2.39s；独立真实probe先复现返回 isError=true 被误报 succeeded，补公共持久化 entry 检查后两个新返回错误用例通过。

Host/guard/bootstrap/doctor/wheel37 passed33.96s。首次Root命令未先 nvm use 导致3 Host启动失败/34通过；切换固定Node24.13.0后重跑通过。私有Junit见 .ci-results/runtime-dispatch-prerequisites.xml，公开证据仅记录无凭据摘要。

Worker最终12项真实PG/Host/TLS测试通过23.55s，覆盖执行中SIGKILL、新backend/Host、Worker reclaim原submission/Generation ID、续租、拒绝重定向/超大/慢响应，以及实际受限角色PG statement_timeout=10s、lock_timeout=3s。首次client误假设Host必有Content-Length和字符串receipt，修复实际chunked/数字接口后通过；另测试关闭Backend错误使用public shutdown，改正确关闭路径后通过。HTTP绝对deadline不代表整个包含多次PG操作的函数有严格总时限；当前总时限为调度循环预算。固定源码完整本地CI exit0：1076 Python通过842.13s、24 SQLite conformance、20 RuntimeAdapter通过2.41s；四个冻结Pi包与Web/Host构建通过，零关键跳过。唯一Python warning是Starlette TestClient/httpx弃用提示，未导致失败。完整命令 `source ~/.nvm/nvm.sh && nvm use && scripts/ci/check`；证据与私有Junit SHA写入 runtime-dispatch-evidence.json。上一轮1050Python/24SQLite/18Pi的head0037结果只作历史，不能替代当前源码验证。

## 未完成边界

目前按模块调用的独立Worker调度已接入；常驻Worker进程入口、stage启动装配、真实工具Gateway和业务 unknown/reconcile 后续推进。AT-039包含业务对账，继续not_run，不能用运行时恢复替代。真实DeepSeek调用需要有效凭据与可信成本上界；未读取.env、未调用渠道/模型。embedding留待S3前决定。未stage、commit、push或修改生产系统。
