# NX-049：Agent Host 全局并发上限（ADR-022 §4 的部署前提）

分支 `nx049-host-concurrency`，BASE main `9016021`。没有迁移，也没有改 SQL。

背景：ADR-022 §4（负责人已批准，分支 `nx049-gate`）把 NX-049 的门槛改为在 CI 串行时限阶段测量，测量时用 4 个 guard 进程，并以“生产 Agent Host 全局并发 ≤ 4”为部署前提，由 doctor 核对。此前 Host 代码里没有全局并发上限，这个前提只能靠部署来保证。本次补上这一缺口。

## 1. 语义

- **什么算一个活跃 Run**：从 `runs/start`（或 `resume`）准入开始，直到这个 Run 最新的提交结束（`Submission.wait`，状态为 done 或 unanswered），并且它的 harness 空闲（`waitForIdle`），才释放名额。harness 被关闭，或者 Run 在截止时间被中止，也会释放名额。`inspect`、`cancel` 不占名额。
- **排队在任何 guard 请求之前**：新 Run 先拿名额，再请求 guard 做准入授权、创建存储、提交。等待期间不构建任何证明，不写 runtime 存储，不占用 Run 凭据，所以排队不会延长任何证明或期限。
- **有界等待**：等待上限取 `min(run_admission_wait_ms, command.not_after)`，先进先出。
  - 超时后，Host 返回 **503 `runtime_capacity_exhausted`，`retryable: true`**；
  - 调度器（`runtime_dispatch`）现有逻辑把 Host 的非 200 应答当作 `runtime_transport_unavailable`，任务进入 `retry_wait`，之后重新 claim。重试时 Run、请求和提交的身份不变，与现有的租约和对账语义一致。
  - 被拒绝的这次尝试没有发出任何 guard 请求，也没有创建任何 runtime 存储（测试中已断言）。
- **幂等**：同一个 Run 已经持有名额时，再次 start/resume 不会占用第二个名额。
- **现有的 `pending ≥ 8 → runtime_busy`（并发 HTTP 请求数上限）不变**。排队中的请求也计入这 8 个。

## 2. 配置（Host 的私有运行配置 JSON）

| 键 | 默认值 | 允许范围 | 说明 |
|---|---|---|---|
| `maximum_active_runs` | 4 | 1..64 | 同一 Host 同时活跃的 Run 上限 |
| `run_admission_wait_ms` | 500 | 0..30000 | 新 Run 等待名额的上限，同时不超过 `not_after` |

- 值非法（越界、类型不对）时，`RuntimeHost` 构造失败（`runtime configuration refused`），Host 不启动。
- 默认等待 500 ms：调度器到 Host 的请求时限 `--request-timeout` 默认 2 s，start 本身还要做一次 guard 准入。等待太久会让调度器先超时；超时同样进入 `retry_wait`，不会出错，但语义不如直接返回明确的 503 清楚。
- 部署示例：`deploy/stage/agent-host.runtime-config.example.json`，其中 `maximum_active_runs` 为 4。示例中只出现回环地址和占位的私有路径。

## 3. doctor（启动前检查）

```bash
nexloop-doctor --agent-host-only --agent-host-config <Host 的私有运行配置>
```

- 只读取私有文件（mode 0600，经 `read_private_text`）。要求 `maximum_active_runs`（缺省时按 4 计）**≤ 4**；值本身非法、文件不可读、文件是公开权限时，同样不通过。不通过时退出码为 1。
- 上限 4 写死在 `nexloop_eios/host_concurrency.py` 的 `MAXIMUM_HOST_ACTIVE_RUNS`，与 ADR-022 §4 绑定，部署配置不能放宽。将来要提高并发，必须先在新取值下重测门槛，并修改 ADR。
- 不带 `--agent-host-only` 时，`--agent-host-config` 会作为附加检查并入完整的 foundation doctor。
- Python 侧记录的默认值（4 和 500）与 `run-admission-gate.ts` 中的默认值，由测试校验一致。

## 4. 测试

| 测试 | 断言 |
|---|---|
| vitest `run-admission-gate.test.ts`（8 项） | 上限 4 时，4 个 Run 立即准入，第 5 个等待，有 Run 释放后才准入；有界等待超时后返回 `runtime_capacity_exhausted` 并离开队列；等待不超过 `not_after`，已过期的命令立即拒绝；先进先出；同一 Run 不重复占名额；等待为 0 时满额立即拒绝；配置的默认值、边界和类型；`RuntimeHost` 遇到非法配置时构造失败 |
| `tests/test_agent_host_run_limit.py`（2 项，真实 Pi、TLS Host、guard、PG，两 Pi 计划，`maximum_active_runs=1`） | 等待为 0：第二个 Run 得到 503 `runtime_capacity_exhausted`（retryable），在此之前没有它的任何 guard 请求，也没有它的 runtime 目录；第一个 Run 结束后重试，正常完成。等待 30 s：第二个 Run 的 start 一直阻塞，它的每个 guard 请求都晚于第一个 Run 的最后一次执行类请求（start/model/tool），两个 Run 都成功 |
| `tests/test_host_concurrency.py`（22 项） | 取值 ≤ 4 通过，5 和 64 不通过；9 种非法配置被拒绝；Python 与 TypeScript 的默认值一致；部署示例设为 4；doctor CLI 的正例（4、缺省、1/0）与负例（5、类型不对、公开权限、文件损坏、文件不存在），以及参数缺失时退出码为 2 |

开发中的首次失败：第二项端到端测试最初用“inspect 轮询看到 done 的时刻”作为第一个 Run 的完成时间。inspect 本身也要经过 guard 授权（本机 0.5–1 s），所以这个时刻比真实完成时间晚，断言因此误报。改为“不早于第一个 Run 的最后一次执行类 guard 请求”后，两项都通过。产品代码没有因此改动。

回归结果见第 5 节。

## 5. 回归（本机）

- vitest `apps/agent-host/test`：**13 个文件，183 项通过**。
- Python `-n 4`：**367 passed，446 s**，一次通过。范围：
  - 全部会拉起 Agent Host 的测试，共 26 个文件，包括 host admission、effect e2e/recovery/tools、message relay、context v6、role context、v4、两 Pi、runtime worker（含多进程）、两项 kill 用例等；
  - `test_agent_host`、`test_host_concurrency`、`test_agent_host_run_limit`；
  - `test_vendor_imports`、`test_wheel_install`；
  - 所有引用 doctor 的测试。

现有测试都在默认上限 4 内运行，行为不变。

## 6. 与其他分支的关系

- `nx049-deploy-config`（`5f00e66`）也改了 `doctor.py` 的参数解析（`--connection-budget`、`--budget-only`，让 `--artifact-root` 变为可选）和 `deploy/stage/`。两边合并时，`doctor.py` 的 `main()` 会有相邻行冲突，按“保留两组参数、两类启动前检查各自独立”的原则合并即可。`deploy/stage/` 下的文件名互不重叠；`deploy/stage/README.md` 只在 deploy-config 分支中，合并后建议在其中补一句 Agent Host 的启动前检查命令。
- `runtime_dispatch.py`（L4）没有改动：Host 返回的 503 已经按可重试处理。如果希望在任务结果里区分“容量不足”和“传输失败”，可以在 L4 那边把 `runtime_capacity_exhausted` 单独映射成 `retry_wait`。

## 7. 修复：s4e 全量 CI 中 `test_agent_host_run_limit.py` 两项失败

**现象**（CI run `20261010T041231Z-7cc6b7ab9ba4`，以及调度员在空闲部署主机上的串行复跑）：
- 两项都在 `wait_done` 中拿到 `runtime_authorization_denied`，或者在第 85 行得到 `runtime_outcome='failed'`；
- `NEXLOOP_TEST_GUARD_WORKERS=4` 时仍是 1 败 1 过。

**根因**：
- 测试调用 `guard_server(...)` 时**没有传 `spawn`**，guard 始终运行在 pytest 进程内（单进程），环境变量 `NEXLOOP_TEST_GUARD_WORKERS` 对它不起作用。
- 在单进程里，两个 Run 的 model/tool 授权、测试每 50 ms 一次的 inspect 授权、pytest 自身共用一把 GIL。在部署主机上，guard 请求因此超过 Host 的 2 s 时限，这正是 NX-049 剖析出的结构性原因：部署主机上单进程 guard 的两 Pi 用例 base 为 4/6，4 进程为 6/6。
- 超时的 guard 请求被 Host 当作拒绝：Run 内的超时让 `runtime_outcome='failed'`，inspect 的超时让 `wait_done` 一直拿到 `runtime_authorization_denied`。
- **不是**被限流的第二个 Run 排队超时。那种情况返回的是 `runtime_capacity_exhausted`（503），失败输出中没有出现。

**修复**（只改测试和测试夹具，产品代码不变；2 s 时限不变；断言没有放宽）：
- 两项测试固定使用 **4 个 guard 子进程**（`guard_server(..., spawn=plan['worker_spawn'], workers=4)`）。
  - 理由：ADR-022 §4 和 ADR-024 把“guard 4 进程、Host 全局并发 ≤ 4”定为部署前提，stage 配置就是 `--guard-workers 4`。这两项测试验证的是 Host 的准入上限，不是 guard 的吞吐。若仍让 guard 运行在单进程里，测到的会是部署中不存在的形态。
- 夹具 `guard_server` 新增参数 `workers`：给定时使用固定的进程数，不读环境变量。没有调用方传入时，行为不变。
- **guard 调用的观察方式**：guard 改为子进程后，原来在 pytest 进程中的 monkeypatch 无法生效。改用测试专用的 `tests/support/guard_call_log/sitecustomize.py`：
  - 只在测试把该目录放进 `PYTHONPATH`，并设置 `NEXLOOP_TEST_GUARD_CALL_LOG` 时生效；
  - 各 guard 子进程以 O_APPEND 方式，每次调用追加一行 `{at, run_id, operation}`；
  - 环境变量只在子进程启动期间设置，Host 等其他子进程不会加载这个钩子。
- 断言保持不变：
  - 被拒绝的那次尝试不产生任何 guard 请求，也不建立 runtime 目录；
  - 排队的那个 Run，所有 guard 请求都晚于第一个 Run 的最后一次执行类请求；
  - 两个 Run 都成功。
  - 另外新增一项断言：调用记录非空，用来证明 guard 确实运行在子进程中。

**本机验证**：
- 连续 3 轮，每轮 2 passed（各约 56 s；本机负载 7–9）；
- 回归 `test_runtime_host_admission`、两 Pi、v4[complete]、`test_host_concurrency`、`test_runtime_worker`：`-n 4` 下 54 passed。
