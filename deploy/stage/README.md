# APP_HOST stage 部署配置（Runtime Worker 与连接预算）

依据：ADR-024（Runtime guard 多进程；负责人 2026-10-10 选择选项 1：代码默认值保持 1，部署配置设为 4），以及 `docs/handoff/docs/11_DEPLOYMENT.md` §6（连接预算）和 §7（进程初值）。

这里的文件都是**示例**：主机名、地址、端口、路径、镜像和密钥都来自私有的 env 文件或私有目录。公开仓库不写任何真实值。

| 文件 | 用途 |
|---|---|
| `connection-budget.v1.json` | 声明 APP_HOST 上每个会打开 PostgreSQL 连接池的服务进程，包括 pool max 和进程数。是 doctor 预算校验的输入 |
| `compose.runtime-worker.example.yaml` | Compose 示例：一次性的预算检查服务 `runtime-worker-budget`，不通过时不启动 `runtime-worker` |
| `systemd/nexloop-runtime-worker.service.example` | systemd 示例：预算检查放在 `ExecStartPre`，不通过时 unit 不启动 |

## 1. Runtime Worker 的进程形态

- `--guard-workers 4`：父进程负责调度，4 个 guard 子进程共用父进程绑定的 loopback 端口（子进程继承文件描述符，不用 `SO_REUSEPORT`）。
- **连接池**：
  - `--dispatcher-pool-max 2`：只做调度的父进程，连接池上限为 2；
  - `NEX_EIOS_DB_POOL_MAX=4`：每个 guard 子进程的连接池上限。每个进程同时最多处理 `4 // 2 = 2` 个 guard 请求。
  - N=1 时只有一个 `Backend`，同时负责调度和 guard，连接池上限取 `NEX_EIOS_DB_POOL_MAX`；`--dispatcher-pool-max` 不起作用。
- 这台 Worker 合计最多 `2 + 4 × 4 = 18` 个 PG 连接（N=1 时是 4 个）。
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
  - 服务器的 `max_connections` 必须 **≥ 80**，差额留给运维和故障核对。
- 任一项不满足，或预算文件格式错误、连不上数据库，退出码都是 1，服务不启动。
- 当前声明的合计是 `api 2×4 + domain-worker 4 + action-worker 4 + scheduler 4 + runtime-worker 18 = 38`。
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
