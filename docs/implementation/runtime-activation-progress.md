# NX-014 正式 Host 与授权引用恢复

2026-10-08。新增受控 activation registry、正式可选 Host admission、独立 loopback HTTPS guard 和完整 Host/Pi 镜像运行依赖。NX-014 继续 in_progress；独立 Worker 的 Run 调度、活跃 Run 自动恢复装配与后续业务 Action 对账尚未完成，不能把 Pi submission done 当业务成功。

## 实际文件与行为

- `packages/eios-core/src/nexloop_eios/runtime_activation.py`、`backend.py` 和追加 `0037_runtime_activation_registry.sql`：API 必须先以真实 Run token 将 task、Run、完整 command、input digest 与 owner epoch 不可变预绑定；Worker 仅凭自身当前 queue 权限和 lease/fence 激活已绑定 Run。后台重开使用 opaque reference 解析原 EIOS Run 身份，重验全部当前 Action 许可链，不返回或持久化 raw Run token。
- `runtime_control.py`：独立 HTTPS loopback 端口，私有控制 key 只识别 Host，不授予业务权限；拒绝浏览器 Origin、错误 Host、重复认证头、不合规 framing 和超大请求。成功响应只包含 authorized/run_id，异常不带 credential 或 backend 对象。TLS 握手有超时且不阻塞主 accept。
- `apps/agent-host/src/runtime-host.ts`、`main.ts` 与 `scripts/agent_host.py`：增加私有 `--runtime-config-file`，按 CA 验证 guard 并执行 start/resume/inspect/cancel；总请求 deadline 两秒。完整 command 与 input 都必须匹配 PG 预绑定。当前仅 deterministic-test profile，业务工具未接入，product_ready 继续为 false。
- `scripts/prepare_host_build.py`、`deploy/community/Host.Dockerfile` 与 Host package files：打包真实编译模块及冻结 Pi production workspace closure，保留四份 Pi 许可；拒绝私有配置、特殊文件、绝对链接及目录逃逸，修正 pnpm deploy 的受控 self-link。镜像仍为 UID10001。
- 测试：`test_runtime_activation.py`、`test_runtime_host_admission.py`、`test_runtime_guard_transport.py`；head assertions 和 versions.lock.json 更新至0037，原36个 SQL 与已测试0036 wheel完全相同。

## 已执行的测试与首次失败

实际命令包括 `uv run pytest tests/test_runtime_activation.py -q`（最终30passed29.72s），`source ~/.nvm/nvm.sh && nvm use && pnpm --filter @nexloop/agent-host build && uv run --frozen pytest tests/test_runtime_host_admission.py tests/test_runtime_guard_transport.py tests/test_runtime_activation.py tests/test_agent_host.py -q --tb=short --junitxml=.ci-results/runtime-activation-focused.xml`（59passed51.03s），以及独立更强的 in-flight Host 恢复检查（4passed7.73s，runtime-host-inflight-recovery.xml）。

首次 activation 24失败/1通过：hint 未设置可信 tenant GUC 导致合法 RLS 查询为空；修复后另修 Backend operation 参数重名，随后25通过并扩展到30通过。首次 Host/bootstrap 合跑3失败/4通过：Adapter 的 start/resume 二次 guard 未携带已登记 input，以及 bootstrap count仍写36；补齐内存 input 绑定和37断言后7通过。首次打包因 pnpm legacy self-link逃出stage被拒绝，核验其唯一合法源后重绑定；不是放宽其他链接保护。无生产凭据或原系统改动。

更强恢复检查：真实 Pi Generation task 已持久化并进入真实 PG guard 后 SIGKILL；关闭原 backend guard，再用重新打开的受限 Worker 服务与新 Host进程恢复相同 receipt、submission、Generation task ID 和 FULL/WAL。Run token 不进入 Host进程/SQLite。其余真实 PG 负例覆盖 live source撤权、Run TTL/task expiry、旧 fence、跨tenant/world、命令/输入冲突、错角色及直接表读取拒绝。

新 head0037 的11服务 Compose foundation 验证通过，HTTPS登录、实际Worker foundation 与各控制端口验证通过；仅按本项目标签对四个常驻容器先 dry-run 后停止，所有11容器exit0、保留volumes。该 Compose 默认未启用 runtime profile，不能替代 native正式Run HTTPS/PG联合证据。对同一个已测试Host镜像，另以network none/read-only/UID10001实际验证RuntimeHost与PiAdapter import和SQLite WAL/FULL；无模型或授权成功声明。具体测试镜像ID和archive SHA见runtime-host-image-evidence.json。

## 尚未完成及输入

独立 Worker Run dispatcher、API入站任务与enrollment的原子编排，以及启动时活跃Run核对仍需代码，继续自主推进。业务工具Gateway、unknown/reconcile、公开HTTP409和渠道闭环按NX-015～017推进；AT-039的业务对账不能由SQLite恢复替代。真实DeepSeek验证仍需可信成本上界和有效凭据，未读取仓库.env，也未调用模型；embedding按既定方向S3前另定。缺这些输入不阻塞独立代码和deterministic测试。

最终固定本地 CI：`source ~/.nvm/nvm.sh && nvm use && scripts/ci/check` exit0；1050 Python（918.94s）、24 SQLite conformance、18 RuntimeAdapter（2.35s），四个Pi包及Web/Host build通过，零关键跳过。更强in-flight恢复4例作为独立Junit证据另列，不重复计数为额外用例。planning验证通过（69 checks、43tasks、60AT、6schemas、11negative），公开扫描911paths、0indexblobs、0violations；没有stage/commit/push/生产修改。
