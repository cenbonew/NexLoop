# AT-015 真实浏览器核对：SSE 断线重连

- **分支**：`at015-real-browser`，从 s5k `2a6b50e` 切出。
- **依据**：
  - 14_TEST_ACCEPTANCE AT-015：客户端断开后用 Last-Event-ID 续传，已提交的消息不丢失、不重复显示；
  - NX-037 进入条件：AT-015 要在真实浏览器中核对，因为已有的 DOM 证据来自 jsdom。

## 1. 运行方式

- **前提**：
  - Node 24（`nvm use 24`）；
  - 仓库锁定的 Playwright：`apps/web/package.json` 的 devDependency `playwright` 为 `1.63.0`，`pnpm-lock.yaml` 已锁定；
  - Playwright 1.63.0 对应的 Chromium headless shell（revision 1243，Chrome Headless Shell 153.0.8010.12）。
- **安装浏览器**：在 apps/web 下执行，经 npm 镜像的二进制镜像下载，只需一次：
  ```bash
  PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright pnpm --filter @nexloop/web exec playwright install chromium-headless-shell
  ```
- **运行**：本地 PostgreSQL 测试集群，与其他 PG 测试相同，需要 `LC_ALL=C.UTF-8`；串行运行。
  ```bash
  make test-real-browser
  ```
  - 等价命令：`LC_ALL=C.UTF-8 uv run --frozen pytest -q -p no:cacheprovider -m real_browser tests/test_sse_real_browser.py`。
  - 设置 `NEXLOOP_REAL_BROWSER_EVIDENCE_FILE=<绝对路径>` 时，把观察结果写成证据 JSON。
- **单独标记，不在默认阶段跑**：
  - 测试标记为 `real_browser`；
  - `tests/conftest.py` 的 `pytest_collection_modifyitems` 在没有 `-m real_browser` 时把它从收集结果中排除（deselect），不是 skip，所以默认阶段里不会出现绿色的 skip；
  - 是否把它加入某个 CI 阶段，由调度员决定。

## 2. 测试做了什么

- **驱动**：`tests/test_sse_real_browser.py`。
  1. 用合成租户和夹具凭据（`local_message_assembly_fixture.assembled_message`）启动真实 API 进程：TLS，只在 127.0.0.1，`--web-root` 指向本次构建的前端；
  2. 用 `vite build --outDir` 把前端构建到测试私有的临时目录，不碰共用的 `apps/web/dist`；
  3. 经 API 登录，创建对话，提交消息 1–2；
  4. 收到浏览器脚本的请求时，停止并在同一端口重启 API。
- **浏览器脚本**：`apps/web/test/browser/sse-resume.browser.mjs`，headless Chromium，Playwright。
  1. **登录与初始状态**：通过登录表单登录；等待页面显示 1–2，流已打开。
  2. **断网**：
     - 在打开的流上实时收到 3；
     - 断网：Playwright 的 `context.setOffline(true)`，同时在本地中继上切断页面的全部 TCP 连接，并暂缓新的连接；
     - 服务端在断网期间提交 4–5，由同一账号的另一个客户端直接发给 API；
     - 恢复网络，断言：页面不刷新就显示 1–5，各一次、顺序正确，对话 ID 不变，“实时连接暂不可用”提示消失。
  3. **刷新**：页面刷新后，从 API 恢复同一对话，显示 1–5 各一次。
  4. **服务端重启**：
     - 实时收到 6；
     - 驱动停止并重启 API；
     - 重启后提交 7–8；
     - 断言页面显示 1–8 各一次，对话 ID 不变。
  - **全程记录**：浏览器每次请求事件流时实际发出的 `after` 参数和 `Last-Event-ID` 请求头，取自 `request.allHeaders()`。
- **为什么要切 TCP**：只用 `setOffline` 时，Chromium 不会中断已经建立的 SSE 连接，第一次运行时页面一直没有出现断线提示。真实的网络中断还会断开已有连接，所以页面的来源端口设为脚本自己的 TCP 中继：页面的所有请求都经过它，切断它就等于断网。
- **服务端校验不受影响**：API 校验 `Origin` 与 `Host`，两者都等于页面来源（中继端口），与浏览器直接访问时相同；直接发给 API 的提交请求带上同样的 Host 头。

## 3. 证据（本地两次运行，Chromium 153.0.8010.12）

两次都通过（31.8 s、35.3 s）。下表是带证据文件那次运行的记录。

| 项 | 观察结果 |
|---|---|
| 浏览器 | Chrome Headless Shell 153.0.8010.12（Playwright 1.63.0，revision 1243） |
| 事件流请求（after, Last-Event-ID） | (2, 无) → (2, **3**) → (5, 无) → (5, **6**) |
| 断网期间显示 | 1–3（断网期间什么也收不到） |
| 恢复后显示 | 1–5 各一次，恢复网络后 105 ms 显示完整 |
| 提示消失 | 恢复网络后 2396 ms，这个数字包含脚本主动等待的 1000 ms |
| 刷新后显示 | 1–5，同一对话 |
| 重启后显示 | 1–8 各一次，同一对话 |
| 服务端 outbox | 8 条 |
| 中继连接数 | 14（含页面的 API 请求、事件流与重连） |

各项含义：

- **续传**：断网恢复后，浏览器自带的 EventSource 带着 `Last-Event-ID: 3` 重连（URL 仍是 `after=2`），收到 4–5；服务端重启后带着 `Last-Event-ID: 6` 重连，收到 7–8。这是 HTML 规范定义的重连语义在 Chromium 中实际运行的结果。
- **刷新**：刷新后的新 EventSource 以页面读取到的最大序号作为 `after`（2 或 5），不带 Last-Event-ID，符合预期。
- **不重复、不乱序**：每个阶段都断言显示的列表与期望完全相等，并且没有重复项。

## 4. 与 jsdom 测试的差异

| 方面 | jsdom（`tests/test_sse_dom_tcp_resume.py` + `apps/web/test/e2e/sse-tcp-resume.e2e.ts`） | 本测试（Chromium） |
|---|---|---|
| EventSource | undici 的实现（Node） | Chromium 自带的实现 |
| 页面 | 在 jsdom 中挂载 `WebChat` 组件 | API 提供的生产构建页面，登录表单登录，Cookie 由浏览器管理 |
| fetch 与 Cookie | 注入 undici dispatcher，手工带 Cookie | 浏览器原生 fetch，`credentials` 与同源 Cookie |
| 断线方式 | 本地中继切断 TCP | `setOffline` 加本地中继切断 TCP |
| 场景 | TCP 切断续传，加三个组件级场景（重复或乱序事件、列表重读竞态、unavailable 后 5 s 续订） | TCP 切断续传、页面刷新、服务端重启 |
| 只在 jsdom 测试中覆盖 | 重复或乱序事件的注入、“核对已提交消息”按钮的重读 | — |

两者互补：jsdom 测试覆盖组件对异常事件序列的处理；本测试确认真实浏览器网络栈的重连行为与 jsdom 的假设一致，并覆盖刷新和服务端重启。

## 5. 观察到的现象（不阻塞）

- **提示消失慢于消息到达**：恢复网络后 105 ms 消息就已显示完整，但“实时连接暂不可用”提示约 2.4 s 后才消失（含脚本主动等待的 1 s）。断言因此改为“10 s 内消失”。
- **可能的原因**：在 `setOffline(false)` 之前，中继已经放开，浏览器在离线状态下的重连尝试先失败了一次，把提示重新设为显示，直到下一次连接打开才清除。这只影响提示的显示时间，不影响消息的完整性。是否调整，由 Web 界面的负责人决定。

## 6. 仍需负责人人工核对的部分

- 真实移动网络切换（Wi-Fi 与蜂窝之间切换、IP 变化、长时间休眠后唤醒），以及移动端浏览器（iOS Safari、Android Chrome）后台挂起后的恢复。
- 非 Chromium 内核：Safari 与 Firefox 的 EventSource 重连实现。本次只运行了 Chromium；需要的话，Playwright 的 WebKit 和 Firefox 可以用同一脚本扩展。
- 经过真实反向代理（Caddy，stage 配置）时的长连接超时与缓冲行为：本测试由 API 直接提供 TLS。
- 长时间断网（超过会话或 CSRF 有效期）之后的恢复体验。

## 7. 本次改动

- **依赖**：`apps/web/package.json` 新增 devDependency `playwright@1.63.0`，`pnpm-lock.yaml` 同步更新。版本发布于 2026-09-04，超过两周。
- **测试与脚本**：
  - 新增 `apps/web/test/browser/sse-resume.browser.mjs`；
  - 新增 `tests/test_sse_real_browser.py`；
  - `tests/conftest.py` 增加 `real_browser` 的排除逻辑；
  - `pyproject.toml` 登记 `real_browser` 标记；
  - `Makefile` 新增 `test-real-browser` 目标。
- **没有改动**：前端代码、API 代码、迁移、CI 阶段脚本。
