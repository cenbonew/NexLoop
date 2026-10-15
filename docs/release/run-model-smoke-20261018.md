# 执行单：模型密钥放置（阶段 C）与带模型的完整冒烟（2026-10-18 早上）

**适用状态**：
- APP_HOST `nexloop-app` 已启动；
- 目录已发布（68 个 Action、20 个类型、2 个 Function），configure 已执行；
- 租户已组装（run-assembly-20261015），WebChat 合成账号 `customer-1` 可登录；
- `REAL_DISPATCH_ENABLED=false`（D9）；
- 模型密钥尚未放置。

本单只用角色名与参数，`$X` 由负责人在私有 env 中给出。

**类别标注**：
- **B**：需负责人确认；
- **sudo-A**：需 APP_HOST sudo；
- **sudo-D**：需 DATA_HOST sudo。

**原则**：密钥只经 stdin 写入，不出现在命令行参数、shell 历史和终端输出里；核对时只看属主、权限、长度，不读内容。

## 1. 阶段 C：放置模型密钥（APP_HOST，sudo-A，B）

### 1.1 要放的文件（逐项对照代码）

| 文件（主机） | 容器内 | 使用者 | 属主 / 权限 | 读取方的要求 |
|---|---|---|---|---|
| `$NEXLOOP_ROOT/secrets/agent-host-model/model.key` | `/run/secrets/host_model_key` | agent-host | **agent-host 进程的 UID**（见 1.2）/ 0600 | `private-material.ts:readPrivateMaterial`：权限必须恰为 0600，属主等于进程 UID，只有一个链接，不是符号链接；内容去掉首尾空白后最多 8192 个可打印 ASCII 字符，不能含空格（`trusted-model-profile.ts`）。`model-configuration.json` 里已有 `MODEL_CREDENTIALS_FILE=/run/secrets/host_model_key`，且 `stage: true`：环境变量里的 `MODEL_API_KEY` 会被拒绝 |
| `$NEXLOOP_ROOT/secrets/claim-extraction-worker/model.env` | `/run/secrets/claim_extraction_worker_model` | claim-extraction-worker | 10001 / 0600 | 见下方说明 |
| `$NEXLOOP_ROOT/secrets/claim-matcher/model.env` | `/run/secrets/claim_matcher_model` | claim-matcher | 10001 / 0600 | 同上 |

**两个 `model.env` 的读取规则**：
- **入口与格式**：`background_services.LazyModelProvider` 调用 `model_profile.load_model_profile`，再由 `provider_credentials.configuration_values` 解析。只认 `NAME=value` 形式的行，`#` 开头的行是注释，只保留 `MODEL_` 开头的键。
- **文件检查**（`provider_credentials.private_text`）：必须是普通文件、只有一个链接、属主等于进程 UID、不能有组或其他人的权限位、不超过 16384 字节。
- **固定的三个键**：
  - `MODEL_PROVIDER=deepseek`
  - `MODEL_ID=deepseek-flash`
  - `MODEL_BASE_URL=https://api.deepseek.com`
- **密钥写在哪里**：claim 容器里只挂载了这个 env 文件，没有单独的密钥文件，所以密钥写在同一个文件的 `MODEL_API_KEY=` 行里。这两个 worker 不以 stage 模式读取，所以允许这样写。
- **不放的后果**：没有密钥时，profile 会退回 `test`，每个任务都以 `provider_unavailable` 失败。

### 1.2 先确认 agent-host 的进程 UID（缺口 G-UID）

两份配置写的不一致：
- `stage.env.example` 与 `compose.app.env.example` 写 `AGENT_HOST_UID=10002`，compose 以 `user: ${AGENT_HOST_UID}` 运行 agent-host；
- `stage_secrets.py` 的 OWNERS 和 run-34 写 agent-host 的文件属主是 10020。

`model.key` 的属主必须等于进程 UID，所以先读出实际值（只读）：

```
docker compose -p nexloop-app exec -T agent-host id -u
stat -c '%u %a' $NEXLOOP_ROOT/secrets/agent-host/model-configuration.json   # agent-host 现在能读这个文件，它的属主就是进程 UID
```

两者应一致，记为 `$HOST_UID`。如果不一致，停下报告调度员。

### 1.3 写入（负责人在 APP_HOST 的交互终端执行，B）

`read -rs` 不回显，变量不进入任何命令行参数（`printf` 是 shell 内建命令，不产生新进程）。临时文件写在 0700 的私有目录里，用 `install` 一次设定属主和权限。

```
umask 077; T=$(mktemp -d)
read -rs -p 'model key: ' K; echo
printf '%s\n' "$K" > "$T/model.key"
printf 'MODEL_PROVIDER=deepseek\nMODEL_ID=deepseek-flash\nMODEL_BASE_URL=https://api.deepseek.com\nMODEL_API_KEY=%s\n' "$K" > "$T/model.env"
unset K
sudo install -d -o "$HOST_UID" -g "$HOST_UID" -m 0700 $NEXLOOP_ROOT/secrets/agent-host-model
sudo install -o "$HOST_UID" -g "$HOST_UID" -m 0600 "$T/model.key" $NEXLOOP_ROOT/secrets/agent-host-model/model.key
sudo install -o 10001 -g 10001 -m 0600 "$T/model.env" $NEXLOOP_ROOT/secrets/claim-extraction-worker/model.env
sudo install -o 10001 -g 10001 -m 0600 "$T/model.env" $NEXLOOP_ROOT/secrets/claim-matcher/model.env
shred -u "$T/model.key" "$T/model.env"; rmdir "$T"
```

如果这几个文件此前已作为占位存在，`install` 会覆盖它们。`set -x` 必须是关闭的。

### 1.4 核对（只看属主、权限、长度）

```
sudo stat -c '%n %u %a %s' $NEXLOOP_ROOT/secrets/agent-host-model/model.key $NEXLOOP_ROOT/secrets/claim-extraction-worker/model.env $NEXLOOP_ROOT/secrets/claim-matcher/model.env
```

- **预期**：
  - `model.key` 为 `$HOST_UID 600 <字节数>`，字节数等于密钥长度加 1（换行），小于 8194；
  - 两个 `model.env` 都是 `10001 600 <字节数>`，大小相同。
- **不要**用 `cat`、`head` 或 `diff` 查看内容。

### 1.5 重建服务

```
docker compose -p nexloop-app -f $NEXLOOP_ROOT/compose.app.yaml --env-file $NEXLOOP_ROOT/config/compose.app.env \
  --env-file $NEXLOOP_ROOT/config/compose.images.env up -d --force-recreate agent-host claim-extraction-worker claim-matcher
```

- **预期**：三个服务的状态都是 running 或 healthy。
- **日志核对**：`docker compose -p nexloop-app logs --since 3m agent-host claim-extraction-worker claim-matcher | grep -E 'refused|unavailable'`，结果应为空。出现 `runtime_model_configuration_refused` 时：
  - 属主或权限不对：回到 1.2、1.4 检查；
  - 密钥里有空格或非 ASCII 字符：重新写入。

**出口**：
- agent-host 走 `agent_egress`，两个 claim worker 走 `claim_egress`；
- 防火墙 v3 只放行 `MODEL_PROVIDER_ALLOWLIST` 的 443 端口和 DATA_HOST 的 PG；
- 如果模型地址不在 allowlist 里，报错的形态是 `provider_unavailable` 或 `runtime_model_unavailable`，不是配置错误。

### 1.6 撤销

1. 删除三个文件：`sudo shred -u` 依次处理三个文件。
2. 重建这三个服务。
3. 撤销后两个 claim worker 回到 `test` 模式，每个任务失败为 `provider_unavailable`；agent-host 的 Run 失败为 `runtime_model_configuration_refused`。
4. 如果密钥可能已经泄露，同时在模型服务方吊销这个密钥。

## 2. 带模型的完整冒烟（10/18）

### 2.1 操作

1. `customer-1` 在 WebChat 登录。
2. 调用 `POST /api/v1/conversations/{id}/messages` 发一条消息，正文写合成内容，带幂等键（`web_chat_http.accept_message`）。
3. 记下返回的 `message_id` 和 UTC 时间 `$T0`。
4. 等待 2 分钟后执行 2.2。

### 2.2 只读查询（DATA_HOST 一次性 backend 容器，bootstrap 只读事务）

容器形态同 run-34 §1.5 的 `$COMMON`，挂载 socket 和 `boot.dsn`。

```
docker run $COMMON -v "$NEXLOOP_ROOT/run/postgresql:/var/run/postgresql" -v "$RUN/boot.dsn:/run/bootstrap-dsn:ro" \
  -e TENANT_ID="$TENANT_ID" -e MESSAGE_ID="$MESSAGE_ID" -e T0="$T0" -i "$BACKEND_IMAGE" -
```

stdin 是下面的 Python 片段。`$COMMON` 已经包含 `--entrypoint python`，所以镜像后面的 `-` 表示从 stdin 读程序。

```python
import json, os, psycopg
dsn = open('/run/bootstrap-dsn').read().strip()
t, m, t0 = os.environ['TENANT_ID'], os.environ['MESSAGE_ID'], os.environ['T0']
Q = {
 'route': ("select run_id, queue, task_id from runtime.nexloop_message_routes where tenant_id=%s and world='real' and message_id=%s", (t, m)),
 'jobs': ("select queue, status, attempts, result->>'code', result->>'runtime_outcome' from runtime.jobs where tenant_id=%s and queue in ('operations','claim-extraction') and created_at>=%s order by created_at", (t, t0)),
 'events': ("select j.queue, e.status, e.message from runtime.job_events e join runtime.jobs j on j.job_id=e.job_id and j.tenant_id=e.tenant_id where j.tenant_id=%s and j.created_at>=%s order by e.sequence", (t, t0)),
 'outbound': ("select delivery_state, run_id is not null from runtime.nexloop_outbound_messages where tenant_id=%s and trigger_message_id=%s", (t, m)),
 'model_requests': ("select result_status, cost, currency from runtime.nexloop_model_requests where tenant_id=%s and requested_at>=%s", (t, t0)),
 'costs': ("select cost_kind, sum(amount), currency, count(*) from runtime.nexloop_cost_entries where tenant_id=%s and occurred_at>=%s group by 1,3", (t, t0)),
 'budget_consumption': ("select budget_kind, sum(amount) from control.nexloop_budget_consumption where tenant_id=%s and world='real' group by 1", (t,)),
 'tenant_state': ("select authz.nexloop_nx037_tenant_state(%s,'real')", (t,)),
}
with psycopg.connect(dsn) as c:
    c.execute('set transaction read only')
    c.execute("select set_config('eios.tenant_id',%s,true)", (t,))
    for name, (sql, args) in Q.items():
        try:
            print(name, json.dumps(c.execute(sql, args).fetchall(), default=str))
        except Exception as e:
            print(name, 'ERROR', type(e).__name__); c.rollback(); c.execute('set transaction read only'); c.execute("select set_config('eios.tenant_id',%s,true)", (t,))
```

- 表名与列名取自迁移：
  - `0047`：`message_routes`，没有 status 列；
  - `0001`、`0035`：`jobs`、`job_events`；
  - `0079`：`outbound_messages`；
  - `0089`、`0120`：`model_requests`、`cost_entries`；
  - `0068`：`budget_consumption`；
  - `0161`：`nx037_tenant_state` 是一个函数。
- 列名已逐一对照迁移核实：
  - `runtime.jobs` 的主键是 `job_id`，`created_at` 在 0001，`queue` 与 `attempts` 由 0035 加入；
  - `job_events` 以 `job_id` 与 `tenant_id` 关联 `jobs`；
  - `nexloop_model_requests` 的时间列是 `requested_at`，`currency` 由 0120 加入；
  - `nexloop_budget_consumption` 有 `budget_kind`、`amount`、`world`。
- 执行前仍建议调度员先在 CI 主机的干净库上跑一遍这个片段。

### 2.3 每项的预期值

| 项 | 预期 | 说明 |
|---|---|---|
| route | 1 行，`run_id` 非空，`queue='operations'` | relay 已绑定 Run |
| jobs：operations | 1 条，`status='succeeded'`，`code` 为空或成功码 | runtime worker 经 Agent Host 调用了模型 |
| jobs：claim-extraction | 有 1 条或以上，`succeeded` | 消息触发声明抽取；没有密钥时为 `provider_unavailable` |
| events | operations 依次为 `pending`、`running`，然后结束，没有 `dead_lettered` | — |
| outbound | 1 行，`delivery_state` 为 `delivered`（或先是 `provider_accepted`） | WebChat 回复不受真实派发关闭的影响：在 `action-effect-categories.v3.json` 中它是 `external_effect: false` |
| model_requests | 有 1 行或以上，`result_status='succeeded'`，`cost` > 0，`currency='CNY'` | Agent Host 按 tariff 计费 |
| costs | `cost_kind='model'` 合计大于 0，币种 CNY | 成本记录 |
| budget_consumption 与 tenant_state | `budgets.model` 有 limit；consumption 增加的量等于本次 cost | 预算已扣减 |
| 真实派发 | `tenant_state.tenant_switch_open` 和 `open_action_switches` 按 D9 关闭 | `nexloop.enterprise.deliver` 等外部 Action 不会发出 |

### 2.4 失败判读表

| 现象（`result.code` 等） | 产生位置 | 原因 | 处置 |
|---|---|---|---|
| `budget_exhausted`（NXB01） | `0068`；`runtime_dispatch.py` 中的 ControlDenied | 租户模型预算期间已结束，或额度已用完 | 不会重试。负责人在工作台用 `Budget.set` 续期或调额（`goals.budget.set`，或 `real_tenant_init.py budget`）。核对 `tenant_state.budgets.model` 的 period 与 limit |
| `runtime_budget_exhausted` | Agent Host（`pi-runtime-adapter.ts`） | 单个 Run 的上限用完：成本、轮数、工具调用次数或超时 | 检查 `relay-run-profile.json` 中的 budget（8/8/120/2 CNY）是否过小；不是 owner 预算的问题 |
| `runtime_model_unavailable` | Agent Host（`runtime-provider-boundary.ts`） | 模型服务出错，或出口不通 | 检查 allowlist 与 `agent_egress` 的出口，以及密钥是否有效；能做的话用服务方控制台核对密钥 |
| `runtime_model_configuration_refused` | Agent Host 启动或 Run | `model.key` 的属主或权限不对，或格式不对 | 回到 1.2、1.4 |
| `runtime_unknown`（`runtime_outcome` 为 unknown） | `runtime_dispatch.py` | Host 返回的 Run 结果不确定 | 任务进入 `retry_wait`，下一次检查并恢复**同一个** Run，不会新建。**不要重发消息**；等它对账完成。长时间不结束就报调度员 |
| `runtime_failed`、`runtime_cancelled` | `runtime_dispatch.py` | Run 失败或被取消 | 终态。回复保证会在到期时发兜底回复（reply-guarantor）。查看 Agent Host 日志中的固定错误码 |
| `runtime_authorization_unavailable` | `runtime_dispatch.py` | guard 没有应答（不是拒绝） | 检查 runtime-worker 的 guard 是否存活（4 个 guard 子进程）。兜底回复会接手 |
| `runtime_transport_*`、`runtime_dispatch_timeout`、`runtime_host_status_*` | 属于 `_RETRYABLE` | 与 Host 之间的连接问题 | 自动 `retry_wait`；持续出现就检查 agent-link 网络与 Host |
| `provider_unavailable`（claim 队列） | `claim_extraction_jobs.py`、`claim_match_worker.py` | claim worker 的模型调用失败（没有密钥、出口不通、密钥无效） | 自动退避重试，最终 `dead_lettered`。检查 `model.env` 和 `claim_egress` |
| `real_dispatch_closed`（NXC07） | `0153` | 外部效果被关闭（D9） | 预期行为；记录在 `runtime.nexloop_dispatch_refusals`。WebChat 回复不受影响 |
| 没有 route 行 | — | relay 没有接到消息：relay 没在运行、consumer 没有登记，或 run profile 有误 | 检查 message-relay 容器与 run-assembly §9 |
| route 行存在，但没有 operations job | — | 激活失败 | 查看 relay 日志中的固定码 |

### 2.5 撤销

冒烟只写入合成数据：一条消息、一次 Run、一条回复和成本记录，这些都保留，作为证据。

需要停用模型调用时，按 1.6 删除三个文件并重建三个服务。

## 3. 缺口

| ID | 缺口 | 处置 |
|---|---|---|
| G-UID | `AGENT_HOST_UID`（样例写 10002）与 OWNERS 的 10020 不一致 | 以 1.2 读出的实际进程 UID 为准；之后统一样例 |
| G-CLAIM-KEY | claim worker 的密钥只能写在 `model.env` 的 `MODEL_API_KEY=` 行，没有单独的密钥文件挂载 | 可以接受（这两个 worker 不以 stage 模式读取，允许这样写）；以后可以改为单独挂载密钥文件 |
