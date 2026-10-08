# NX-011 API 到 Agent Host 的 HTTPS 内部探针

新增 packages/eios-core/src/nexloop_eios/host_control.py 与 tests/test_host_control.py；修改 http_api.py、apps/agent-host/src/main.ts、scripts/agent_host.py、Host 使用说明及实际进程测试。versions.lock.json 仅更新 Host 的已验证能力描述；包版本、上游提交、镜像与依赖锁没有变化。

Node Host 现在要求私有 TLS certificate/key 文件，以 HTTPS 监听 loopback。私有材料有类型、所有者、0600、单链接和长度检查，读取有硬性字节上限。原 OS owner 锁继续由实际 Node 进程持有。

API 新增完整可选配置组 --host-origin、--host-control-key-file、--host-ca-file。配置仅接受精确 HTTPS loopback origin，不接受用户信息、路径、query、别名或低端口。调用固定只读内部健康路由，显式校验受信任 CA 与证书主机名；不用环境代理，不跟随重定向；请求带私有控制密钥，响应限4096字节，设置1秒 socket 超时。对外只返回 reachable/authenticated/owner_lock/product_ready 四个布尔值，不暴露内部返回内容或配置值。

整体 readiness 仍为503。内部控制鉴权不是 Run-bound 身份，不授予 EIOS 业务 Action 权限。未接收 Run，未声称 Pi恢复、真实运营或部署成功。

实际执行命令：

- `source /Users/chenbowen/.nvm/nvm.sh && nvm use && uv run --frozen pytest -xq tests/test_host_control.py tests/test_agent_host.py tests/test_http_api.py --tb=short`：首轮23 passed in5.19s；新增重定向/代理用例后24 passed in6.22s；新增真实双进程入口后25 passed in6.74s。它们是实施期间的 loopback HTTP 基线，不是最终 HTTPS 验收。
- `source /Users/chenbowen/.nvm/nvm.sh && nvm use && pnpm --filter @nexloop/agent-host build && uv run --frozen pytest -xq tests/test_host_control.py tests/test_agent_host.py tests/test_http_api.py --tb=short`：切换 HTTPS 后25 passed in7.16s；新增错误 CA/主机名验证后27 passed in7.44s；最后加强私有材料读取上限后最终27 passed in7.23s。没有首次失败；重跑由新增用例或代码变化触发。
- `source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check`：完整 CI 结果见 host-control-ci.json。
- `docker info --format '{{.ServerVersion}}'`：exit1，daemon 仍不可连接；没有启动容器或构建镜像。

真实证据包括：Python API/Node Host 两个独立进程间 HTTPS 探测；正确控制密钥；密钥轮换期间旧调用方失败、更新私有调用方文件后恢复；权限漂移拒绝；Host 退出后 API 保持存活、报告不可达；错误 CA 与证书 IP SAN 不匹配拒绝；重定向/环境代理目标未收到请求；既有第二进程拒绝与 SIGKILL/重新打开测试在 HTTPS 下通过。证书只在自有测试目录生成，客户端显式信任测试证书，无系统信任库修改或 TLS 验证关闭。

NX-009 容器证据、NX-011 浏览器实际 DOM/截图验收和完整生产传输/社区栈仍未完成；Run-bound 身份、PG租约、RuntimeAdapter 与 S2 完整闭环还需继续实施。NX-011 保持 in_progress；无 git stage/commit/push、部署、原系统写入或持久环境迁移。

完整 CI：873 Python 测试通过（186.49s，1条现有 Starlette warning）、24 Pi SQLite 测试及四个 Pi 包构建通过，关键用例零跳过。前端与 Host 构建通过。实际非 editable core wheel 安装、打包 bootstrap、受限 PG/Artifact smoke 与五个 console help 通过。32份 SQL、uv.lock、pnpm-lock.yaml 均保持原哈希。planning69校验与公开扫描通过，暂存区为空。
