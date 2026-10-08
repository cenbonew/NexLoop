# NX-017 / AT-032 缓存故障验证

主线定向验证 **67 passed，109.01s，0 failures/errors/skips**（session 56409）。这证明缓存故障切片，不表示 NX-017 或完整本地 CI 已完成。公开证据见 `NX-017-cache-fault-evidence.json`；原始 JUnit 为忽略产物 `.ci-results/nx017-main-cache-rerun.xml`，SHA256 `d069c14d897d08e9dec80947d6efc9ef14f7bd36e167137cff9cf5ea3e82095c`。

变更为 `valkey_wakeup.py`、`runtime_worker.py`、`runtime_dispatch.py`、`http_api.py` 及 `test_valkey_wakeup_product.py`。缓存工作没有新增 SQL/bootstrap、上游/包/镜像锁修改。每份实际源码 SHA 记录在 JSON。

## 实际执行

```sh
source ~/.nvm/nvm.sh && nvm use 24 >/dev/null && PYTHONPATH=packages/eios-core/src:tests uv run --frozen pytest -q tests/test_valkey_wakeup_product.py tests/test_runtime_worker.py tests/test_durable_queue.py tests/test_browser_http.py tests/test_http_api_execution_profile.py --tb=short --junitxml=.ci-results/nx017-main-cache-rerun.xml
```

此为实际测试命令，公开版仅省略临时 stdout/stderr 文件重定向。16 cache +19 RuntimeWorker +20 queue +7 browser HTTP +5 execution-profile 用例通过；既有 Starlette/httpx deprecation warning 保留。

首次主线命令误填不存在的 `tests/test_runtime_worker_cli.py`，session33607 exit4，0 tests collected；改为真实文件后上述67项通过。候选阶段也保留 Docker stopped 状态端口读取、running/leased 枚举误判、TLS CN 回退、漏 fixture 导入、候选 workspace link、macOS backlog RST 等首次失败；修复与重跑列在 JSON。旧50项候选绿对应旧 SHA；literal-IP 修复后候选16项绿对应新 SHA，不混用验证范围。

## 缓存与队列事实

真实自有 TLS/ACL Valkey 清空、停止、同一 CID 固定端点重启后，原适配器配置不变自动 reconnect。cache 只存短 TTL namespace/task SHA256 提示，不存业务正文、token、授权或租约。业务 ACK 在受治理 PG commit 后成立，缓存不可用不会重试业务或改变 receipt；实际两个 queue task 完成，cache 路径正式对象写0。

实际 RuntimeWorker 通过 `--cache-configuration-file` 读取私有文件，缓存禁用/可用/停止三种模式均完成真实 PG 与 Node24 Pi SQLite FULL 的同一任务；`business_action_success=false` 仍保留，runtime 成功不是履行证明。RuntimeDispatcher 始终走原 PG claim/guard/finish，不用缓存决定权限，也不消费或 ACK outbox。独立 Scheduler 的技术 notification ACK 只表示提示投递。

API readiness 的 cache 字段独立报告 degraded 与恢复，`authoritative=false`；overall `product_ready=false` 不因缓存健康而改成 true。该 HTTP 证据为 FastAPI TestClient 与实际 TLS cache，不冒称独立 Human 浏览器验收。

客户端只接受明确 literal IPv4/IPv6；`server_name` 可为证书 DNS SAN，但不解析 DNS。单个 numeric sockaddr connect 使用连接前计算的剩余 deadline，避免 hostname/getaddrinfo、多地址逐次 timeout；TLS/RESP 也受同一 deadline。真实 owned TCP 已接受但 TLS 握手停滞，以及慢字节回复均被截止，PG ACK 与 pending task 保留。macOS backlog 实际返回 RST，只记录 unavailable，不编造慢 SYN 成功。

cache timeout 超过 `min(lease_seconds,total_timeout)/6` 会在 RuntimeDispatcher preflight 拒绝；真实短租约负例保持 pending/fence0、无 SQLite。SAN、密钥文件权限、错误认证、CONFIG/FLUSHDB/foreign key 限制均有实际负例。

## 边界

未部署持久环境，未接通 Compose cache secret mount，也未读取真实模型/渠道凭据。重建覆盖实际 claim 与 bounded recent task 的当前 inspect，不承诺完整 pending 快照或历史通知重放。独立 Scheduler 最多64项维护没有整个 tick 的实时保证；实际 RuntimeDispatcher 不运行该维护循环。

另两组实际终态已复核 JUnit：session11027 **27 passed，99.32s**，覆盖 queue commit/lease、effect dispatch、local effect worker kill、runtime effect bridge；session4432 **4 passed，24.15s、2个既有 warning**，覆盖 PG admission outage、Runtime allocated-before-Pi kill、model response commit window。三条当前主线定向命令合计 **98 passed，0 failures/errors/skips**，不是单次完整仓库 CI。

```sh
source ~/.nvm/nvm.sh && nvm use 24 >/dev/null && PYTHONPATH=packages/eios-core/src:tests uv run --frozen pytest -q tests/test_queue_commit_lease.py tests/test_effect_dispatch.py tests/test_local_effect_worker_kill.py tests/test_runtime_effect_bridge.py --tb=short --junitxml=.ci-results/nx017-fault-regression.xml
source ~/.nvm/nvm.sh && nvm use 24 >/dev/null && PYTHONPATH=packages/eios-core/src:tests uv run --frozen pytest -q tests/test_precise_pg_admission_outage.py tests/test_runtime_allocated_before_pi_kill.py tests/test_model_response_commit_window.py --tb=short --junitxml=.ci-results/nx017-admission-recovery.xml
```

两个 JUnit 分别为 `.ci-results/nx017-fault-regression.xml` 与 `.ci-results/nx017-admission-recovery.xml`，SHA、源码 SHA、确切命令在 JSON。Root 的独立只读复核未发现新的授权、缓存完成语义、泄露或单次 deadline 问题；64-hint 整轮延迟累加和 Compose 尚未部署的限制仍保留。

完整当前源码 CI 与 NX-017 状态由相应终态证据决定。此次仅新增两份报告，不修改 planning、业务代码或提交。
