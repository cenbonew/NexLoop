# 消费者通道第 1 期：客户端库、CLI 与 MCP

**依据**：负责人 2026-10-17：“目前消费者只能通过 webchat 的方式，需要开放 API，并且封装成 CLI 和 MCP，这样我就可以让 Codex 去模拟消费者对系统进行测试了。”

**范围**：第 1 期**不改服务端**，只在现有的 WebChat HTTP API 外面包一层客户端。消费者走的仍是人在浏览器里走的那条路：

- `browser_http` 的登录、cookie 会话与 CSRF；
- `web_chat_http` 的会话、消息、回执、Run 状态与 SSE。

所以每条消息都经受治理的 Conversation Action 进入，以消费者本人的授权执行，联系限制、回复保证都不变；不直连数据库，也不绕过 EIOS。正式的非浏览器 API 是第 2 期，设计见 [消费者通道 API 设计](../design/consumer-channel-api.md)。

**状态**：在分支 `l5/consumer-channel-api` 上，不部署。stage 正在跑试运行。

## 1. 组成

| 模块 | 入口 | 作用 |
|---|---|---|
| `nexloop_eios/consumer_channel.py` | `ConsumerClient` | 客户端库：login、logout、whoami、create_conversation、list_conversations、send、history、receipt、wait_reply、watch |
| `nexloop_eios/consumer_cli.py` | `nexloop-consumer` | 命令行，每条命令输出一个 JSON 对象 |
| `nexloop_eios/consumer_mcp.py` | `nexloop-consumer-mcp` | MCP server（stdio），工具：login、new_conversation、send_message、wait_for_reply、get_history、get_receipt |

**与 API 的对应关系**：

| 客户端方法 | HTTP |
|---|---|
| login | `POST /api/v1/auth/login`，返回 cookie `__Host-nexloop_session` 与 `csrf_token` |
| logout | `POST /api/v1/auth/logout` |
| whoami | `GET /api/v1/auth/session` |
| create_conversation | `POST /api/v1/conversations` |
| list_conversations | `GET /api/v1/conversations` |
| send | `POST /api/v1/conversations/{id}/messages`，带 `Idempotency-Key`，返回 202 |
| history | `GET …/messages?after=&limit=` |
| receipt | `GET /api/v1/messages/{id}/receipt` 与 `…/run` |
| watch | `GET …/events`，SSE，`Last-Event-ID` |
| wait_reply | 轮询 history，直到出现“不是本消费者发的”消息 |

- **请求头**：每个请求都带 `Origin`（等于配置的 origin）与 cookie；写操作另带 `X-CSRF-Token` 与 `Idempotency-Key`。服务端的同源检查不变。
- **幂等**：`send` 自动生成 Idempotency-Key；重放同一个键，结果为 `created: false`，不会多写一条消息。

## 2. 凭据规则

- **口令**：只从私有文件（`--password-file`，0600、属主是当前用户）或 stdin 的第一行（`--password-stdin`）读取。**从不出现在 argv 或环境变量中。** 钥匙串用管道传入：
  ```bash
  security find-generic-password -w -s nexloop/stage-consumer -a customer-1 | nexloop-consumer --origin https://<WEBCHAT_ORIGIN> --ca-file <CA_FILE> login --username customer-1 --password-stdin
  ```
- **会话**：cookie 与 CSRF token 只保存在内存中，或保存在私有会话文件 `--session-file` 中（默认 `~/.nexloop/consumer/session.json`，0600）。它们从不出现在任何输出里；会话文件权限过宽时，命令拒绝运行。
- **MCP**：`login` 工具**只接受 username**。口令由 server 自己按启动参数读取：
  - `--password-dir <目录>/<username>`，私有文件；
  - 或 `--keychain-service <服务名>`，执行 `security find-generic-password -w -s <服务> -a <账户>`，不经过 shell。服务名中的 `{username}` 会替换成登录用户名；`--keychain-account` 指定固定账户，不给时账户就是用户名。stage 的条目是服务 `nexloop/stage/{username}`、账户 `nexloop`。
  
  所以口令不会进入代理的对话记录、工具参数或结果。
- **TLS**：`--ca-file` 指向 stage 的内部 CA，不提供关闭证书校验的选项。

## 3. CLI

```
nexloop-consumer [--origin URL] [--ca-file F] [--session-file F] <命令>
  login --username U (--password-stdin | --password-file F)
  whoami | logout | new-conversation | conversations [--limit N]
  send [--conversation-id ID] (--text T | --text-stdin)
  wait-reply [--conversation-id ID] [--after-sequence N] [--timeout S] [--poll S]
  history [--conversation-id ID] [--after N] [--limit N]
  receipts --message-id ID
  watch [--conversation-id ID] [--after N]
```

- **输出**：每条命令一行 JSON，成功为 `{"ok": true, …}`，失败为 `{"ok": false, "code": "…", "status": …}`。退出码：0 成功；1 被拒绝或失败；2 参数错误。
- **会话**：没给 `--conversation-id` 时，用会话文件中最近一次的会话；还没有会话就新建一个。首次登录之后，`--origin` 可以省略，从会话文件里读。
- **错误代码**：

| code | 含义 |
|---|---|
| `unauthenticated` | 用户名或口令错误（401） |
| `rate_limited` | 登录限流（429），带 `retry_after` |
| `session_expired` | 会话已失效（401），需要重新 login |
| `forbidden` | 跨源，或会话受限 |
| `conversation_payload_conflict` | 同一 Idempotency-Key 但内容不同（409） |
| `invalid_request` | 请求不合法（422） |
| `dependency_unavailable` | 服务端暂不可用（503） |
| `login_required`、`origin_required`、`session_file_not_private` 等 | 本地检查未通过 |

## 4. MCP（供 Codex 模拟消费者）

server 用 stdio 传输 JSON-RPC 2.0，每行一条消息。支持 `initialize`、`tools/list`、`tools/call` 与 `ping`。工具结果放在 `content[0].text` 中，是一段 JSON 字符串；失败时 `isError` 为 true。

**Codex 配置示例**（`~/.codex/config.toml`；路径和地址按负责人私有环境填写）：

```toml
[mcp_servers.nexloop_consumer]
command = "/opt/nexloop/consumer-venv/bin/nexloop-consumer-mcp"
args = ["--origin", "https://<WEBCHAT_ORIGIN>", "--ca-file", "<CA_FILE>", "--keychain-service", "nexloop/stage/{username}", "--keychain-account", "nexloop"]
```

Claude Code 用的是同一组参数（`claude mcp add nexloop-consumer -- <command> <args…>`）。

**给代理的用法提示**：

1. `login(username)`；
2. `send_message(text)`，返回 `message.sequence`；
3. `wait_for_reply(after_sequence=<上一步的 sequence>, timeout_seconds=120)`；
4. 需要时调用 `get_history` 或 `get_receipt(message_id)`。

每个代理进程只持有一个消费者会话；模拟多个消费者时，启动多个 server 实例（使用不同的 username）。

**测试人设**：代理使用的消费者账号必须是登记过的测试人设（OPERATIONS §9.4a）。口令由调度员生成，存进钥匙串，只告知条目名（OPERATIONS §8a.9）。

## 5. 测试

- `tests/test_consumer_channel_pg.py`，6 项，在真实栈上运行：`create_app` 加真实的浏览器登录、PostgreSQL 与受治理的 Conversation Action。客户端经进程内 transport 访问，每次请求前清空测试客户端的 cookie，只用客户端自己的 cookie 与请求头。覆盖：
  - 登录；
  - 发送：202、重放、外发队列已持久化；
  - history、receipt 与 run、SSE watch；
  - wait-reply 超时（这套栈上没有运行时回复）；
  - 错误口令转为 401，再转为限流 429；
  - 登出后的旧会话：读和写都是 401 `session_expired`；
  - 非法 ID 在任何写入之前就被拒绝；
  - 别人的会话被拒绝；
  - CLI 的会话文件（0600、不含口令）；
  - MCP 全流程，并确认 login 的参数中没有口令。
  - 所有输出中都没有口令、cookie 或 CSRF token。
- `tests/test_consumer_channel.py`，9 项，离线：
  - wait-reply 的回复识别与游标、截止时间；
  - 请求头（写操作才带 CSRF）；
  - origin 校验；
  - 口令来源：私有文件、stdin、钥匙串（不经过 shell，拒绝不合法的用户名）；
  - 会话文件的权限；
  - MCP 的协议外壳。

## 6. 限制

- **等待回复**：`wait_reply` 是轮询，默认每 2 秒一次。凡是“不是本消费者发的”消息都算回复，包括员工接管后的回复。SSE watch 只读一次流：服务端会在 `stream_seconds` 后结束这次连接，需要时再调用一次。
- **会话受限**：消费者会话受限（`restricted`）时，写操作会被服务端以 403 拒绝。客户端不绕开这一点。
- **定位**：这是测试驱动用的客户端，不是对外开放的 API。浏览器会话的限流与 cookie 策略原样适用。

## 7. 负责人上手（管理机，按步骤复制执行）

- **前提**：
  - 本地 main 检出须包含本节所在的提交，因为 MCP 的 `--keychain-account` 选项是随本节一起加入的；
  - 管理机上有 `uv`，以及 Python 3.12（也可以让 uv 自动获取）；
  - 钥匙串中已有调度员写入的测试人设条目：服务名为 `nexloop/stage/<用户名>`，账户为 `nexloop`，用户名为 `customer-1`…`customer-4`、`refusal-1`、`refusal-2`（见 `~/.nexloop/vault-index.md`）。
- **约定**：本节不写真实地址或主机名。WebChat 的 origin 与 CA 文件名放在你自己的私有 env 文件中，下面的命令都从变量读取。

### 7.1 写私有 env（一次）

```bash
mkdir -p ~/.nexloop && cat > ~/.nexloop/consumer.env <<'EOF2'
export NEXLOOP_CHECKOUT=<本地 main 检出的绝对路径>
export NEXLOOP_WEBCHAT_ORIGIN=<WebChat origin，形如 https://<主机>:<端口>，取自私有 stage env>
export NEXLOOP_STAGE_CA="$HOME/.nexloop/stage/<CA 文件名>"
EOF2
chmod 600 ~/.nexloop/consumer.env
```

以后每开一个终端，先执行：

```bash
source ~/.nexloop/consumer.env
```

### 7.2 安装两个命令

推荐用 uv tool。这种方式以可编辑模式指向本地检出，`git pull` 之后不必重装：

```bash
uv tool install --python 3.12 --editable "$NEXLOOP_CHECKOUT/packages/eios-core"
command -v nexloop-consumer nexloop-consumer-mcp
nexloop-consumer --help >/dev/null && echo installed
```

如果不用 uv，可以改用独立 venv：

```bash
python3.12 -m venv ~/.nexloop/consumer-venv
~/.nexloop/consumer-venv/bin/pip install -e "$NEXLOOP_CHECKOUT/packages/eios-core"
export PATH="$HOME/.nexloop/consumer-venv/bin:$PATH"
```

- **判据**：两个命令都能找到；`--help` 退出码为 0。
- **撤销**：`uv tool uninstall nexloop-eios-core`，或者删除 `~/.nexloop/consumer-venv`。

### 7.3 核对钥匙串条目（不显示口令）

```bash
for u in customer-1 customer-2 customer-3 customer-4 refusal-1 refusal-2; do
  security find-generic-password -s "nexloop/stage/$u" -a nexloop >/dev/null 2>&1 && echo "$u ok" || echo "$u missing"
done
```

第一次读取某个条目时，macOS 会弹窗询问是否允许 `security` 访问，选“允许”或“始终允许”。

### 7.4 冒烟：login → send → wait-reply（CLI）

> **注意**：这是在 stage 上发出一条真实消息。它会触发真实的 Run，产生真实的模型费用，并且会出现在会话与试运行的观测中。请在受控交互的窗口之外执行，并且与调度员约定好使用哪个测试人设，避免混进正在进行的试运行记录。

```bash
source ~/.nexloop/consumer.env
U=customer-1
S="$HOME/.nexloop/consumer/$U.session.json"
C=(--ca-file "$NEXLOOP_STAGE_CA" --session-file "$S")

security find-generic-password -w -s "nexloop/stage/$U" -a nexloop \
  | nexloop-consumer --origin "$NEXLOOP_WEBCHAT_ORIGIN" "${C[@]}" login --username "$U" --password-stdin

nexloop-consumer "${C[@]}" send --text '你好，我想咨询一下上门维修的时间。' | tee /tmp/consumer-send.json
SEQ=$(python3 -c 'import json; print(json.load(open("/tmp/consumer-send.json"))["message"]["sequence"])')

nexloop-consumer "${C[@]}" wait-reply --after-sequence "$SEQ" --timeout 180
```

- **预期**：
  - login 输出 `{"ok": true, "principal_id": …, "authenticated": true, …}`；
  - send 输出 `"created": true`；
  - wait-reply 在回复写入后输出 `"replied": true` 和回复内容。180 秒内没有回复时，输出 `"timed_out": true`，这时用 `nexloop-consumer "${C[@]}" receipts --message-id <id>` 看 Run 的状态。
- **失败时**：
  - `unauthenticated`：口令与账号对不上，找调度员重置（OPERATIONS §8a.9）；
  - `rate_limited`：等 `retry_after` 秒后再试；
  - `transport_unavailable`：检查 origin 与 CA 文件。
- **收尾**：`nexloop-consumer "${C[@]}" logout`。会话文件可以保留，下次 login 会覆盖它。

### 7.5 配置 Codex（MCP）

Codex 的配置文件不会展开环境变量，所以先用下面的命令，把本机的实际值写成一段配置，追加到 `~/.codex/config.toml`。这个文件只在你本机，不进仓库：

```bash
source ~/.nexloop/consumer.env
mkdir -p ~/.codex
cat >> ~/.codex/config.toml <<EOF2

[mcp_servers.nexloop_consumer]
command = "$(command -v nexloop-consumer-mcp)"
args = ["--origin", "$NEXLOOP_WEBCHAT_ORIGIN", "--ca-file", "$NEXLOOP_STAGE_CA", "--keychain-service", "nexloop/stage/{username}", "--keychain-account", "nexloop"]
EOF2
tail -4 ~/.codex/config.toml
```

- **说明**：
  - `{username}` 是字面量，由 server 在 login 时替换成用户名，所以一个 server 可以登录任一测试人设；
  - 口令由 server 从钥匙串读取，不会进入 Codex 的对话；
  - 想让几个代理并行扮演不同顾客，就复制这一段，换一个名字（例如 `nexloop_consumer_2`），各自 login 不同的用户名。
- **重启 Codex** 后，可以这样交代代理（示例提示）：
  > 用 nexloop_consumer 工具扮演测试顾客 customer-2：先 `login`，再用 `send_message` 咨询上门维修，然后用 `wait_for_reply`（`after_sequence` 填上一步返回的 `message.sequence`，`timeout_seconds` 填 180）等回复，最后用 `get_history` 汇报整段对话。不要尝试其他账号，不要索要或输出任何口令。
- **撤销**：从 `~/.codex/config.toml` 删除 `[mcp_servers.nexloop_consumer]` 这一段。
