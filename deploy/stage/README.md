# APP_HOST stage 部署配置（Runtime Worker 与连接预算）

依据：ADR-024（Runtime guard 多进程；负责人 2026-10-10 选择选项 1：代码默认值保持 1，部署配置设为 4），以及 `docs/handoff/docs/11_DEPLOYMENT.md` §6（连接预算）和 §7（进程初值）。

这里的文件都是**示例**：主机名、地址、端口、路径、镜像和密钥都来自私有的 env 文件或私有目录。公开仓库不写任何真实值。

| 文件 | 用途 |
|---|---|
| `connection-budget.v1.json` | 声明 APP_HOST 上每个会打开 PostgreSQL 连接池的服务进程，包括 pool max 和进程数。是 doctor 预算校验的输入 |
| `compose.runtime-worker.example.yaml` | Compose 示例：一次性的预算检查服务 `runtime-worker-budget`，不通过时不启动 `runtime-worker` |
| `systemd/nexloop-runtime-worker.service.example` | systemd 示例：预算检查放在 `ExecStartPre`，不通过时 unit 不启动 |
| `compose.data.example.yaml`、`compose.data.env.example` | NX-032：DATA_HOST 的 PostgreSQL 18 与 Valkey，只在 `DATA_HOST_ADDR` 上发布两个端口 |
| `compose.app.example.yaml`、`compose.app.env.example` | NX-032：APP_HOST 的 Gateway、API、各 Worker 和 Agent Host。内部链路在同一网络命名空间的 127.0.0.1 上，只发布 Gateway；每个服务只挂载自己的秘密文件 |
| `config/*.template`、`firewall/*.template` | NX-032：pg_hba、postgresql、Valkey、两机 nftables。由 `scripts/deploy/render.py` 按白名单参数渲染，不允许残留未渲染的占位符 |
| `stage.env.example` | NX-032：渲染器接受的参数，示例值都是文档保留地址 |

## 1. Runtime Worker 的进程形态

- `--guard-workers 4`：父进程负责调度，4 个 guard 子进程共用父进程绑定的 loopback 端口（子进程继承文件描述符，不用 `SO_REUSEPORT`）。
- **连接池**：
  - `--dispatcher-pool-max 2`：只做调度的父进程，连接池上限为 2；
  - `NEX_EIOS_DB_POOL_MAX=4`：每个 guard 子进程的连接池上限。每个进程同时最多处理 `4 // 2 = 2` 个 guard 请求。
  - N=1 时只有一个 `Backend`，同时负责调度和 guard，连接池上限取 `NEX_EIOS_DB_POOL_MAX`；`--dispatcher-pool-max` 不起作用。
- 这台 Worker 合计最多 `2 + 4 × 4 = 18` 个 PG 连接（N=1 时是 4 个）。
- `--dispatch-loops 4`（NX-031，调度员 2026-10-11）：父进程里并行跑 4 个派发循环，共用同一个 queue。
  - 一个循环一次只驱动一个 Run（轮询 Host 直到这个 Run 结束），所以只有 1 个循环时，Host 同时活跃的 Run 最多 1 个，ADR-022 §4 的“全局并发 ≤ 4”达不到。
  - 多个循环之间靠 claim/lease fencing 分配任务：一个任务只会被一个循环领取；循环所在进程被 kill 后，lease 到期，任务由 reclaim 恢复，Run 不变、fence 增加。
  - 循环共用调度连接池（`--dispatcher-pool-max`），不增加连接数。
  - 代码默认值是 4，等于 Agent Host 的默认 `maximum_active_runs`。部署时两者应当一致；循环数超过 Host 上限，多出的 start 请求只会在 Host 的准入队列里等待，超过 2 s 请求时限后转为重试。
  - SIGTERM 后每个循环都不再领取新任务，各自把手上的 Run 跑完。
- **停机**：
  - SIGTERM 只发给父进程（systemd `KillMode=mixed`；Compose 默认就只发给 PID 1）；
  - 父进程停止 claim，转发 SIGTERM 给子进程；
  - 子进程等已接受的请求全部应答完（最多 15 s），父进程最多等 20 s；
  - 所以 `TimeoutStopSec` 或 `stop_grace_period` 设为 40 s。
- **崩溃**：子进程意外退出时立即替换；60 s 内重启超过 4 次时，父进程停止 claim 并以退出码 1 退出，由 systemd 或 Compose 的重启策略接手。

## 2. 前提：生产 Agent Host 全局并发 ≤ 4

N=4 的依据是：同一 Worker 同时在处理的受时限 guard 请求，实测峰值为 4（ADR-024 §4），而 11_DEPLOYMENT §7 中 Agent Host 的全局并发也是 4。

- **这是部署前提，Host 代码目前不强制执行。** Agent Host 没有全局 Run 并发上限的配置，同时运行的 Run 数由派发它们的 Worker 和队列决定。
- 部署时必须让同一 APP_HOST 上同时活跃的 Run ≤ 4。超过时，guard 请求会重新在子进程内重叠，2 s 时限的余量随之下降。
- 在 Host 里加上可强制执行的全局并发上限，是一个单独的跟进项。
- 需要更高并发时，先修改 ADR-024 的取值依据并重新实测，再同时调整 `--guard-workers` 和 `connection-budget.v1.json`。

## 3. 连接预算（启动前检查）

```bash
nexloop-doctor --budget-only --connection-budget deploy/stage/connection-budget.v1.json --database-url-file <私有 DSN 文件>
```

- doctor 按“每个服务的 pool max × 进程数”逐项求和。Runtime Worker 按第 1 节的公式计算：N=1 时是 `NEX_EIOS_DB_POOL_MAX`，N>1 时是 `dispatcher_pool_max + N × guard_pool_max`。
- 两个上限写死在 `nexloop_eios/connection_budget.py`，不能由部署文件放宽：
  - 合计必须 **≤ 60**；
  - 服务器的 `max_connections` 必须 **≥ 80**。超出应用 60 的那 20 个，按 ADR-037 分配为：模拟进程组 10、运维 7、superuser 3（doctor 读取服务端的 `superuser_reserved_connections`）。
- 任一项不满足，或预算文件格式错误、连不上数据库，退出码都是 1，服务不启动。
- 当前声明的合计是 52（NX-031 D7，实测的最小可用池，调度员 2026-10-11 批准）：
  - `api 2×4 + api-browser-identity 2×2 + domain-worker 2 + action-worker 2 + runtime-worker 18 + message-relay 2×(2+语义读取 1) + outbound-recorder 2`（2026-10-22 NX-038 E3：relay 每进程加 1 个语义读取连接，identity 每进程 3→2，待登录高峰复测；2026-10-13 方案 B：两个 relay，identity 池每进程 3）；
  - 10 个后台服务各 1；
  - 两个 launcher 的 API 池各 2。
- 模拟进程组单独设一份预算（ADR-037，负责人 2026-10-22）：预算文件的 `simulation` 段，上限 10，不计入应用的 60。现在登记的是 sim-runtime-worker 2 + 2×2、sim-stand-in-executor 1、sim-world-files 1，合计 8。给出服务器的 `max_connections` 时，doctor 还会核对：应用 + 模拟 + 运维余量 7 ≤ `max_connections` − superuser 预留 3。模拟组默认关闭，只在验收时启动（`docs/implementation/sim-stage-enable.md`）。
- doctor 另外核对 Runtime Worker 的 `processes × dispatch_loops ≤ 4`（ADR-022 §4 的 Host 上限，检查项 `runtime_dispatch_loops`）；派发循环不计入连接数。
- doctor 另外核对每个常驻服务都已登记（`connection_budget.required_services()`），漏登记一项就判不通过。
- 预算中不再有 `scheduler`：它对应交接包 Compose 模板里的 `nexloop-scheduler`，现有代码没有这个进程。原先保留的 4 个连接已删除，改为登记 API 浏览器身份池（调度员 2026-10-11 裁定，取代 NX-031 D7 中保留它作为预留的说法）。
- 实际的服务进程数以部署时的只读 inventory 为准。每新增一个会连接 PG 的服务进程，都要先在这个文件里登记。

## 4. 回退

1. 把 `--guard-workers 4` 改回 `1`：
   - systemd 改完要 `daemon-reload`，然后重启；
   - Compose 改完后 `up -d` 重新创建该服务。
2. `--dispatcher-pool-max` 可以保留，N=1 时它不起作用。`connection-budget.v1.json` 可以暂时保留 4，预算只会偏保守；也可以同时改成 1。
3. 没有 schema、数据或契约变化，不需要迁移。

## 5. 持有机密的进程

N=4 时，DSN、服务凭据、签名密钥、guard 密钥和 guard TLS 私钥由 5 个进程持有（父进程 1 个，子进程 4 个）；Host 控制密钥和 Host CA 只有父进程持有。

所有进程都使用同一个服务用户和同一组 mode 0600 私有文件，机密不经过环境变量或命令行；子进程只输出一行固定的 ready 信息，不输出任何其他内容（ADR-024 §7）。

## 6. Message relay（多客户，迁移 0160–0162）

设计与实现：`docs/implementation/relay-multi-consumer-design.md`。

- **一个 relay 进程服务所有已登记的客户**：
  - 启动参数用 `--run-profile-file`，指向部署级的 Run 设置，共 6 个键：`role_ref`、`context_manifest_ref`、`runtime_profile`、`budget`、`runtime_owner_epoch`、`queue`。它与单客户的 `--recipe-file` 互斥，两者同时给出时进程不启动。reply-guarantor 的参数相同。
  - 容器入口在私有目录中看到 `relay-run-profile.json` 时传入这个参数。
- **登记客户**：business setup（Consumer、EffectControl、归属）和 catalog setup（offering、binding）完成后，用 `nexloop-relay-enroll --enrollment-file <私有文件>` 登记，调用者必须是该 EffectControl 的 owner 凭据。
  - 登记、撤销和读取都是受治理的签名端口，relay 自己不能登记；
  - 撤销登记、客户进入删除流程（NX-029）或被接管时，relay 在下一次认领或签发时就会停止服务该客户，兜底回复的签发同样如此。
- **Source 主体固定**：relay 的 Source 凭据（`--source-credential-file`）**必须使用主体 ID `nexloop-service-relay-source-principal`**。
  - service-grants v16 的 `relay_source_read_rules` 只为这个主体发布读派生规则（0161）：它对已登记客户的 Consumer、offering 和 binding 的读取，在每次读取时由 SQL 派生，不配置任何逐对象授权；
  - 用其他主体 ID 的 Source，读不到任何客户；
  - catalog setup 创建 binding 时，`source_principal` 也必须是这个主体。
  - 这个 Source 的其他授权（`nexloop.service.request` 等）仍按原方式由 owner 配置，不在 service-grants 中。
- **部署检查**：
  ```bash
  nexloop-doctor --relay-source --metrics-database-url-file <只读 nexloop_metrics 的私有 DSN 文件>
  ```
  - 对每个有已登记客户的 real 租户核对三件事：存在针对固定主体、处于 active 的规则；该主体有 active 的服务凭据；没有其他 Source 主体的 active 规则。
  - 任一项不通过时退出码为 1。
- **告警**：使用 `deploy/configuration/alert-rules.v2.json`，比 v1 多两条：
  - `relay_unenrolled_pending`（critical）：有待处理消息、却没有任何 relay 登记的客户；
  - `relay_backlog_age`（warning）。

## 两个 relay（2026-10-13，连接预算方案 B）

`compose.app.example.yaml` 默认运行 `message-relay` 与 `message-relay-2`。两者配置与凭据相同，各用自己的 vault 目录（`data/relay-vault`、`data/relay-vault-2`，各自 0700，属主为服务用户），不能共用：一个 vault 只能有一个属主进程。连接预算为 relay 2×2，API 浏览器 identity 池每进程 3（`--identity-pool-max 3`），合计 59/60。详见 `docs/implementation/relay2-budget.md`。

## 7. 依赖租户状态的 worker 配置文件（NX-032 A1 设计 §4，T6）

三个文件都放在 APP_HOST 的 `$NEXLOOP_ROOT/config/`，属主 10001，权限 0600，Compose 以只读方式挂到 `/etc/nexloop/`。

| 主机文件 | 读取方 | 来源 |
|---|---|---|
| `claim-match.json` | claim-matcher（`background_services.load_match_configuration`） | 复制 `deploy/stage/claim-match.example.json`，内容是 `{}`：两个键 `edit_actions`、`create_actions` 都可以省略，即不配置匹配 Action。要启用时写成 `{"edit_actions": {"<类型>": ["<Action>", 1]}, "create_actions": {...}}`，所列 Action 须已发布 |
| `recall-types.json` | recall-indexer（`background_services.load_types`） | publish 之后执行 `scripts/deploy/tenant_config.py recall-types --tenant <UUID> --bootstrap-dsn-file <F> --out recall-types.json [--types Consumer,...]`。只接受已为该租户发布的类型，默认 `{"Consumer": null}` |
| `relay-run-profile.json` | 两个 message-relay、reply-guarantor（`message_relay.validate_run_profile`） | 租户组装之后执行 `tenant_config.py relay-run-profile --tenant <UUID> --bootstrap-dsn-file <F> --out relay-run-profile.json --role-id <RoleDefinition 对象 ID> --maximum-cost <每 Run 上限> --currency <币种>` |

- `tenant_config.py` 只在只读事务中读库，从不写库；输出文件必须是新文件，不会覆盖。
- `relay-run-profile` 的检查：
  - RoleDefinition 必须 active 且当前有效；
  - `NexLoop.queue.<queue>:1` 必须已发布；
  - 结果必须能通过 relay 自身的校验。
- `context_manifest_ref` 固定写 `artifact:context-bind-pending`。relay 与兜底回复在绑定前都会把它换成实际绑定的 Context artifact。
- **setup 主体（A3）**：`business_setup` 与 `catalog_setup` 以各自的服务主体运行，即 `stage-principals.v1.json` 第 2 版中的 `business_owner` 与 `catalog_maintainer`。`business_owner` 还负责 `nexloop-relay-enroll`。
  - 在 `register` 之前先执行 `stage_secrets.py setup-credentials --out <树>`，在 `setup/` 下生成这两个主体的凭据文件，并加入 `management/service-secrets.json`。
  - executor 凭据复用 `effect_executor` 的 token，Source 凭据复用 `relay_source` 的 token；
  - 重复执行不会再新增。
