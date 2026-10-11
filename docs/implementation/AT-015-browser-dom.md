# AT-015 浏览器端：实际挂载的 WebChat 不重复、不丢失

AT-015 原有证据（`sse-tcp-reconnect-evidence.json`）已经证明：服务端在 TCP 断开后，能按 Last-Event-ID 补齐已提交消息。本次补的是浏览器 DOM 这一侧。

## 发现的两个缺陷与根因

1. **列表重新读取与实时事件竞态时，事件会从 DOM 消失。**
   - 场景：以下几种操作都会触发 `GET /messages`：
     - 点击“核对已提交消息”；
     - 发送成功后的 invalidate；
     - React Query 默认的窗口聚焦重新读取。
   - 如果这个 GET 在消息 N 提交之前读到快照，却在 SSE 推送 N 之后才返回，React Query 会用旧快照整体替换缓存，N 就从屏幕上消失了。
   - SSE 不会再推送 N，因为它的游标已经越过 N。所以在用户再次手动核对之前，这条消息一直处于丢失状态。
   - **修复**：`chat-api.ts` 新增 `mergeCommitted`，挂到 messages 查询的 `structuralSharing` 上，在写入缓存时原子合并：
     - 新快照覆盖它涵盖的范围，即序号 ≤ 快照最高序号的部分。因此范围内被删除的行会消失，这与 NX-029 删除传播一致；
     - 序号更高的旧行是实时到达的，予以保留；
     - 按 id 去重，按 sequence 排序。
2. **服务端以 `unavailable` 结束流之后，客户端不再续订。**
   - 原代码收到 `unavailable` 后会 `close()` 并显示“正在等待重新连接”，但实际上不会再重连，直到切换对话为止。浏览器把 EventSource 判为 CLOSED（例如非 200 响应）时，情况也一样。
   - **修复**：`subscribe` 新增 `onClosed` 回调，WebChat 在 5 秒后以屏幕上已有的最高序号重新订阅。
   - 数据格式无效的事件仍然只关闭、不自动重连，避免对同一个坏事件无限循环。
   - 网络错误照常交给浏览器自带的重连（带 Last-Event-ID）处理。

按 id 去重（`items.filter(id!==…)`）原本就有，所以重发和重复投递本来就不会重复显示。新测试把这一点固定了下来。

## 测试

- `apps/web/test/sse-resume-dom.test.ts`（jsdom，挂载真实的 `WebChat` 和 React Query；EventSource 按 HTML 规范模拟重连与 Last-Event-ID）。共 4 个场景：
  1. 断线后重连，服务端重发 2、3（已经显示过）以及断线期间提交的 4、5；
  2. 同一事件因竞态被收到两次，且乱序到达；
  3. 列表重新读取与实时事件竞态；
  4. 服务端发出 `unavailable` 后，5 秒内续订，且不丢消息。

  修复前，场景 3 和 4 失败；修复后 4 个全部通过。
- `tests/test_sse_dom_tcp_resume.py` 配合 `apps/web/test/e2e/sse-tcp-resume.e2e.ts`：
  - 环境：实际 API 进程，TLS，真实登录，真实 PG；
  - WebChat 挂载在 jsdom 中，EventSource 用的是 undici 的实现（遵循 HTML 规范的重连语义），流的 TCP 连接经过本地中继；
  - 过程：
    1. 消息 1、2 读入后，实时收到 3；
    2. 切断 TCP，并阻止新连接；
    3. 断线期间通过同一 API 提交 4、5；
    4. 放行。
  - 断言：
    - 第二条连接只收到 `['4','5']`。流 URL 中仍然是 `after=2`，所以这说明续传用的是 Last-Event-ID 3；
    - DOM 恰好显示 1–5 各一次，顺序正确；之后再“核对”一次，结果不变；
    - API 列表与 outbox 都是 5 条。
  - 这个 e2e 不在 `vitest run apps/web/test` 的收集范围内（文件后缀是 `.e2e.ts`），由上面的 pytest 驱动，需要先 `nvm use 24`。

## 依赖

`apps/web` 新增 devDependencies：`jsdom` 30.1.0 和 `undici` 8.11.2。其中 undici 原本已经作为 jsdom 的依赖进入锁文件。`pnpm-lock.yaml` 中对应变化如下：
- vitest 和 openai 的 peer 解析各多了一个可选 peer（jsdom 和 undici）；
- 没有改动其他包的版本。
