# NX-011 登录前端候选

新增 apps/web React 19/Vite/TypeScript/TanStack Query 应用，包含真实登录、读取当前会话、加载和错误状态、CSRF 更新及退出。密码只在表单内存中存在；没有浏览器持久化、自动重试写请求或模拟业务数据。服务状态来自实际 health/ready 响应。尚未具备运营工作台和 Agent Host。

Python API 通过可选 --web-root 同 origin 提供构建产物，静态路由排在 API 路由之后。CI 先执行 Node 24 检查、冻结 pnpm 安装和前端构建，再运行 Python 测试，避免依赖未生成产物。

真实命令：`source /Users/chenbowen/.nvm/nvm.sh && nvm use && pnpm install --lockfile-only && pnpm install --frozen-lockfile && pnpm --filter @nexloop/web build`，构建通过。增补真实 readiness 后第二次构建也通过。

`NEXLOOP_UI_PREVIEW=1 uv run --frozen pytest -xq tests/test_web_frontend.py --tb=short`：1 passed in 247.80s，包含人工观察等待。独立临时 PG、实际 HTTPS API 子进程、显式校验测试证书，验证页面和脚本、真实登录、会话、CSRF 更新及退出，以及进程/连接关闭和日志凭据排除。所有临时凭据文件由测试清理。此测试不等于浏览器 DOM 操作验收。

内置浏览器访问本地 HTTPS 时首次失败为 net::ERR_CERT_AUTHORITY_INVALID；未绕过警告、未禁用证书验证、未写入全局信任。浏览器登录/重载/退出、桌面和移动截图，以及概念图与实际渲染对照仍待完成。docs/design/login-design-spec.md 明确记录自选设计和待验收项，不声称用户已批准设计。

versions.lock.json 增加 @nexloop/web 精确依赖记录；pnpm-lock.yaml 新增前端依赖。上游冻结提交、bootstrap0032、32份迁移、uv.lock 与镜像记录不改动。NX-011 保持 in_progress；S2 验收不晋级。未 stage、commit、push、部署或修改原系统。

完整 CI：852 Python 测试通过（190.42s，1 条现有 Starlette warning）、24 Pi SQLite 测试及四个 Pi 包构建通过，关键用例零跳过。实际非 editable wheel 安装、打包 bootstrap/受限 Artifact smoke 也通过。未出现 pytest 首次失败；浏览器证书拒绝独立记录，不算浏览器验收成功。结果与输入哈希见 web-login-ci.json。planning69检查和公开内容扫描通过。
