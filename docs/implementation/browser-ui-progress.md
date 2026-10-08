# NX-011 真实浏览器登录与视觉验收

改动：apps/web/src/styles.css（桌面页脚bottom42→54，修复约12px偏移）、apps/web/index.html（显式空favicon，消除404）、tests/test_web_frontend.py（私有预览元数据加入公共certificate_path）、.gitignore（忽略output/playwright与CLI临时产物）。新增正式截图docs/design/login-desktop.png、login-mobile.png及login-fidelity-ledger.md；更新浏览器证书验证说明、versions.lock.json中web能力状态和planning。依赖锁、上游与32份迁移未变。

Browser/IAB此前无法信任自有localhost证书，按skill转用Chromium。实际工具：Chrome for Testing149.0.7827.55、Playwright CLI0.1.7、其core1.60.0-alpha-1775951570000、Node24.13.0；它们是本机验收工具，不是产品运行依赖。profile由本次创建在忽略目录下，mode0700；不打开用户既有profile或修改系统信任库。

证书管理器发现自定义可信证书功能。UI原生文件选择器不能被CLI upload接管；关闭浏览器后检查本次空profile的ServerCertificate schema/meta version1和零记录，按Chromium官方schema/protobuf配置一份公共测试证书，限制DNS=localhost、CIDR=127.0.0.1/32。重新打开后确认自定义可信证书列表显示localhost，实际HTTPS登录页面成功加载。ignoreHTTPSErrors=false。未知自签名证书实际goto返回ERR_CERT_AUTHORITY_INVALID，该会话随即关闭，没有绕过警告。

来源：[profile绑定](https://chromium.googlesource.com/chromium/src/+/main/chrome/browser/net/server_certificate_database_service_factory.cc)、[数据库格式](https://raw.githubusercontent.com/chromium/chromium/main/components/server_certificate_database/server_certificate_database.cc)、[信任与约束protobuf](https://raw.githubusercontent.com/chromium/chromium/main/components/server_certificate_database/server_certificate_database.proto)。这些是测试配置依据，不是EIOS业务写路径。

实际命令与过程：

- `/Users/chenbowen/.codex/skills/playwright/scripts/playwright_cli.sh --help`；随后用named session/config/独立profile打开chrome://certificate-manager。首次socket路径过长报EINVAL；该CLI已terminal，但留下本次profile浏览器。短TMPDIR重试时仍被profile占用拒绝，确认只属于本次profile的进程后SIGTERM、确认终止、保留profile再打开成功。没有使用kill-all/close-all或停止其它浏览器。
- `NEXLOOP_UI_PREVIEW=1 uv run --frozen pytest -xq tests/test_web_frontend.py --tb=short`：自有临时PG、实际HTTPSAPI与构建产物；人工浏览器阶段结束设置自有stop marker后，测试正常完成，1 passed in561.89s（含浏览器验证时间），并删除私有预览凭据文件、关闭API/PG。
- CLI click/import + upload：原生chooser未形成CLI modal，upload拒绝；未假称UI导入成功，改用上面明确记录的停止状态测试profile配置。
- CLI run-code首次dynamic import因VM无回调失败，require也不可用。后续由自有本机脚本读取合成测试输入、执行真实表单并先脱敏CLI输出；没有读取或使用真实模型凭据。首次结果解析误要求JSON空格导致AssertionError，浏览器本身已返回六项成功；改为JSON解析并新增错误密码与退出后重载检查后通过。
- `uv run --frozen python .ci-results/browser_workflow.py`：在1586×992与390×844分别执行，wrong_password/login/reload/csrf_logout/secure_httponly_strict/no_web_storage/cookie_removed/logout_reload均true。真实401错误alert、真实Session cookie、安全属性、零localStorage/sessionStorage和退出后的匿名重载均验证。临时脚本、trace与profile留在忽略的私有测试区域；原始凭据不入公开日志或正式截图。
- `pnpm --filter @nexloop/web build`：页脚/favicon改动后通过；CLI screenshot scale=css捕获两个视口。最终view_image同回合查看概念与两个最新截图，文案/布局/字体/调色/控件/间距/响应式对照及修复见login-fidelity-ledger.md。DOM实测10项copy完整匹配；移动三控件342×52、无横向溢出。
- 未信任证书负向实验首次PID路径预检未匹配，该次未执行验证；关闭自有会话/服务后再用已知named session/profile执行，实际ERR_CERT_AUTHORITY_INVALID，随后正常关闭自有浏览器与TLS listener。无警告绕过。
- `source /Users/chenbowen/.nvm/nvm.sh && nvm use && scripts/ci/check`：完整最终CI见browser-ui-ci.json。

浏览器与所有自有预览/TLS服务已关闭；保留忽略的测试profile/脱敏日志供复核，不删除用户资料。只有匿名正式截图进入公开目录。此处只有登录界面验收成功；完整工作台、Docker社区全栈、Run-bound身份、Pi Run恢复和S2运营闭环仍未完成。Docker info最终复核依然exit1，manual startup问题尚待负责人回应。NX-011保持in_progress；不晋级AT-013/030/031/033/034/052或整体产品ready。无stage/commit/push、部署、持久环境迁移或原系统修改。

最终CI：874 Python测试通过（185.07s，1条现有Starlette warning）、24 Pi SQLite用例及四个Pi包构建通过，关键用例零跳过；前端/Host构建与非editable wheel/bootstrap/受限PG Artifact smoke通过。planning69检查与公开扫描通过，暂存区为空。
