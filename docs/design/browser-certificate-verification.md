# 浏览器测试证书验证：后续验证方向

当前 Browser/IAB 对自有 localhost 测试证书报 ERR_CERT_AUTHORITY_INVALID；尚无浏览器登录/重载/退出、桌面/移动截图或 fidelity 验收成功证据。不关闭 TLS 验证、不绕过浏览器警告、不修改系统信任库或用户现有浏览器 profile。

2026-10-08 查询官方资料：Chrome Certificate Manager 在 Chrome134及以后提供 chrome://certificate-manager 入口，见 [Chrome Root Store FAQ](https://chromium.googlesource.com/chromium/src/+/main/net/data/ssl/chrome_root_store/faq.md)。Chromium 的 ServerCertificateDatabaseServiceFactory 以当前 profile 的 GetPath 创建数据库服务，见 [官方源码](https://chromium.googlesource.com/chromium/src/+/main/chrome/browser/net/server_certificate_database_service_factory.cc)。因此临时独立 Chromium profile 中的证书管理值得实际验证；这只是待验证方向，不是证书隔离或浏览器验收成功声明。

后续须使用自有临时 profile，检查其 Certificate Manager 是否支持测试证书的 profile 内导入与信任限制；导入前确认操作不写 macOS Keychain。仅在确证 profile 隔离、保留证书和主机名校验后运行真实 PG 登录页面的浏览器操作与截图。清理只能针对自有测试目录，不能删除或改动用户既有浏览器资料。

本次未安装 NSS/Firefox、未导入任何浏览器证书、未选择或查看用户浏览器 profile，也未关闭 TLS 验证。未运行 ego-browser 或 Playwright 浏览器；仅阅读 skill 以核对工作流与边界。

后续实际验证（2026-10-08）：Chromium149.0.7827.55 在本次创建的独立 profile 显示自定义可信证书；原生证书文件选择器无法被 CLI upload 接管，因此关闭该浏览器后，按官方 ServerCertificate 数据库 schema/protobuf 在这个初始空 profile 中配置1份公共测试证书，约束 DNS=localhost、CIDR=loopback/32。浏览器重新打开后在自定义可信证书列表确认 localhost；保留 ignoreHTTPSErrors=false，实际 HTTPS 页面成功加载，桌面/移动真实 PG 登录闭环通过。未使用系统证书管理入口或 OS 信任写入 API。详细文件、失败与重跑见 docs/implementation/browser-ui-progress.md。早先待验证描述是当时状态，当前结果以这段及证据为准。
