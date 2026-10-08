# NX-011 内部 TLS 环境隔离修复

修改 packages/eios-core/src/nexloop_eios/host_control.py、tests/test_host_control.py 与 tests/test_agent_host.py。探针从 ssl.create_default_context 改为显式 PROTOCOL_TLS_CLIENT context，加载私有配置指定 CA，保留 CERT_REQUIRED 与主机名验证，并不继承 SSLKEYLOGFILE 的环境日志设置。Host 测试观察客户端也使用显式 TLS context；退出后检查端口时明确传入正确受信任证书，防止证书不受信任掩盖未关闭的 listener。

首次真实回归命令：`source /Users/chenbowen/.nvm/nvm.sh && nvm use && uv run --frozen pytest -xq tests/test_host_control.py::test_host_probe_does_not_honor_ambient_tls_key_logging --tb=short`。结果1 failed in0.52s：仅在合成测试中设置 SSLKEYLOGFILE 后，旧探针创建了 TLS 会话密钥日志文件；不记录文件内容或任何实际凭据。没有读取、使用或测试真实模型密钥。

修复后：`source /Users/chenbowen/.nvm/nvm.sh && nvm use && uv run --frozen pytest -xq tests/test_host_control.py tests/test_agent_host.py tests/test_http_api.py --tb=short`，28 passed in7.39s。加强 Host 退出端口检查后最终重跑28 passed in7.46s，然后执行完整 `scripts/ci/check`，最终结果见 host-tls-isolation-ci.json。

真实 Node/HTTPS 连接仍能通过正确 CA 与控制密钥验证，错误 CA/主机名继续拒绝；环境 TLS 日志文件不会创建。所有实验仅使用自有临时目录、合成凭据和临时证书。版本锁、依赖锁、上游源码与32份迁移保持原值。

Docker 只读复核显示后端进程存在，但 docker.sock 缺失、docker info 失败；Computer Use 打开 Docker 的 AppleEvent 超时。没有强制停止或重置 Desktop。已向负责人询问是否需手动完成启动/授权；容器验证继续待启动完成。浏览器 profile 内信任方向另见 docs/design/browser-certificate-verification.md，尚未实际导入或完成浏览器验收。

NX-011 保持 in_progress；NX-009 与 S2 完整闭环未验收。未 stage/commit/push、部署、持久环境迁移或修改原生产系统。

完整 CI：874 Python 测试通过（184.47s，1条现有 Starlette warning）、24 Pi SQLite 测试和四个 Pi 包构建通过，关键用例零跳过。前端与 Host 构建、非 editable wheel 安装、受限 PG/Artifact smoke 与五个 console help 通过。versions.lock.json、uv.lock、pnpm-lock.yaml 与32份已发布SQL哈希未变化。最终planning69检查和公开扫描通过，暂存区为空。
