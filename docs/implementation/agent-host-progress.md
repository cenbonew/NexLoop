# NX-011 Agent Host 进程与内部鉴权基础

新增 apps/agent-host/package.json、tsconfig.json、src/main.ts 与 README.md，以及 scripts/agent_host.py 和 tests/test_agent_host.py。CI 在 Python 测试前构建实际 Host，.gitignore 排除生成的 dist。versions.lock.json 增加 Host 包记录；pnpm-lock.yaml 增加工作区入口，没有引入新运行时依赖。

锁由 Python 启动器取得后，通过 execve 继承至实际 Node24 Host。同一目录第二个进程无法取得 OS flock；SIGTERM/SIGKILL 自动释放内核锁，不删除锁文件或原 runtime 文件。内部健康探针验证随机控制密钥、精确 loopback Host、无浏览器 Origin、私有文件权限，并支持运营方原子替换密钥。此控制密钥不代表 EIOS 服务/agent 业务身份。

实际命令：`source /Users/chenbowen/.nvm/nvm.sh && nvm use && pnpm install --lockfile-only && pnpm install --frozen-lockfile && pnpm --filter @nexloop/agent-host build`，通过。`source /Users/chenbowen/.nvm/nvm.sh && nvm use && uv run --frozen pytest -xq tests/test_agent_host.py --tb=short`，首次8 passed in0.61s，无失败。随后强化旧/新轮换密钥均不进入进程日志的断言，最终断言经独立8项测试重跑通过（0.64s）；Host 实现代码与完整 CI 测试版本相同。

只使用自有临时目录、合成内部凭据和 loopback 端口，无原生产系统/数据库改动。接口未接收 Run，没有 runtime恢复成功或业务写成功的虚构响应。readiness 保持503。该验证不是 AT-030/031 或 Pi Run kill/reopen 验收；Run-bound 身份、PG租约、RuntimeAdapter 和本地挂载类型检查仍需继续实施。

本轮重新运行 `open -a Docker` 后，`docker info --format '{{.ServerVersion}}'` 仍返回 daemon 不可连接；未执行镜像构建/容器启动。NX-009 的真实容器验收继续缺证。NX-011 浏览器证书信任/实际DOM与截图验收、生产内部传输仍未完成。

完整 CI 的实际命令为 `source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check`，结果以 agent-host-ci.json 为准。保持 NX-011 in_progress，不晋级 S2 验收；无 git stage/commit/push、部署或持久环境迁移。

完整 CI：860 Python 测试通过（183.37s，1 条现有 Starlette warning）、24 Pi SQLite 测试及四个 Pi 包构建通过，关键用例零跳过。前端及 Host 构建通过；非 editable core wheel 安装、受限 PG/Artifact smoke、五个 console help 与打包 bootstrap 通过。uv.lock、32份已发布 SQL 与上游/镜像锁定值未改动。证据和最终覆盖范围见 agent-host-ci.json。
