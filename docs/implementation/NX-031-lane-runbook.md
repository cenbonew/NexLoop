# NX-031 车道运行说明（1000 个客户的整轮矩阵）

执行人：调度员，在 CI 主机的夜间窗口执行。本文只写角色名和参数，实际取值放在 CI 主机的私有 env 里。整轮由 `scripts/load/run_on_lane.sh` 驱动：持有 CI 锁 → 建模板 → 跑 8 格矩阵 → 生成报告。结果目录的格式见 `NX-031-results-contract.md`。

## 0. 参数（环境变量，取值不进仓库）

| 变量 | 必需 | 说明 |
|---|---|---|
| `NEXLOOP_CI_LOCK` | 是 | 和常规 CI 共用的锁文件 |
| `NEXLOOP_TEST_PG_BIN` | 是 | PostgreSQL 18 的 bin 目录 |
| `LOAD_SEED` / `LOAD_CONSUMERS` | 否 | 默认 31 / 1000 |
| `LOAD_WINDOW` | 否 | 夜间窗口，默认 `22:00-06:00`（本地时间） |
| `LOAD_LOCK_WAIT` | 否 | 等锁的秒数，默认 1800 |
| `LOAD_MIN_FREE_GIB` / `LOAD_MIN_FREE_MEM_GIB` | 否 | 磁盘、内存余量下限，默认 20 / 8 |
| `LOAD_CI_BASELINE` | 否 | 当天最后一次常规全量 CI 的 JSON（`run_id`、`wall_seconds`、`deadline_gate_failures`、`failed`），供报告对比 C4-ci |

其他前提：
- `REAL_DISPATCH_ENABLED=false`；环境里不能有任何 `MODEL_*`、`EMBEDDING_*`、DSN 或凭据变量（车道脚本会再检查一遍）。
- 检出目录必须干净，并且就是要测的那个提交；`.venv` 和 `apps/agent-host/dist` 必须是这个提交构建出来的（`uv sync --frozen`，`npm ci && npm run build`）。

## 1. 前置检查（只读）

```bash
uv run --frozen python scripts/load/lane_preflight.py
```

- 不写任何东西，不启动任何进程，不连数据库。CI 锁只做一次非阻塞探测，探测完立即释放。
- 输出 JSON。全部通过时退出码为 0，任何一项不通过为 2。

| 检查项 | 内容 |
|---|---|
| `ci_lock_idle` | 现在没有人持有 CI 锁 |
| `disk_free` / `memory_available` | `.ci-results/load` 所在磁盘的剩余空间，以及可用内存 |
| `clean_checkout` | 检出目录没有本地改动；结果必须属于一个确定的提交 |
| `deployment_premises` | 连接预算里 runtime-worker 为 `guard_workers 4`、`dispatch_loops 4`，合计 ≤ 60；门槛格（C4、C4-low）的 Host 并发 ≤ 4；每格的链路按这些值启动（guard 4、`--dispatch-loops` 等于 Host 并发、Host 的 `maximum_active_runs` 等于该格并发、一个 Host 服务全部 Run）；矩阵配置了 D5 对数正态时延 |
| `login_limit_untouched` | 任何压测脚本或辅助代码都不写登录限流表；每格启动时还会把实际生效的策略和模板记录的正式值核对一遍，不一致就以 `login_policy_mismatch` 中止 |
| `distinct_loopback_sources` | 能绑定独立的 loopback 源地址（127.x.y.z）。这样每个客户各占一个限流桶，和生产上“一个来源一个桶”的语义一致。绑不了时整轮仍能跑，但每格预热要按 `Retry-After` 等待，1000 个客户每格约多 50 分钟，整轮会超出夜间窗口，这种情况请不要开跑 |
| `no_leftover_processes` | 没有上一轮遗留的进程 |
| `safety_environment` | 关闭真实派发；环境里没有模型凭据 |
| `template_matches_seed` | 只在传 `--template <run_dir>/template` 时检查（续跑或复用模板）：种子、客户数、manifest digest、提交号、正式登录限流的记录，以及 `pgdata` 是否存在 |

## 2. 运行

```bash
scripts/load/run_on_lane.sh
```

- 退出码：0 表示报告已生成（AT-060 的结论要看报告）；1 表示某一步失败；2 表示前置检查拒绝；75 表示 CI 锁一直被占用。
- 在夜间窗口之外运行，必须由调度员决定，加 `--outside-window`。
- 加 `--prune-old` 会删除 30 天以前的运行目录，并逐个列出。

### 预计时长（依据本机 25 个客户的实测外推，以车道实测为准）

| 步骤 | 时长 | 依据 |
|---|---|---|
| 模板 | 约 50–60 分钟 | 1000 个客户共 1547 条历史来信，按约 0.6 Run/s 回复完，约 40–45 分钟；登记、来信、承诺、计划合计约 10 分钟 |
| 每个普通格 | 约 20–25 分钟 | 预热 120 s + 稳态 600 s + 排空最多 600 s + 启动和收尾 |
| C4-ci | 约为常规全量 CI 的墙钟时长 | 全量 CI 和格子同时开始，格子要等 CI 跑完才收尾 |
| 报告 | 不到 1 分钟 | |
| **合计** | **约 4.5–5.5 小时** | 夜间窗口 8 小时 |

### C4-ci：在锁内再跑一轮全量 CI

- 车道脚本把 `LOAD_CI_COMMAND=scripts/ci/check` 交给矩阵执行器。C4-ci 格在稳态开始时启动这条命令，就在车道已经持有的 CI 锁之内，它自己不再取锁。
- 格子会等 CI 结束，把 CI 的墙钟时长、门槛阶段的失败数和失败总数写进 `cell.json` 的 `ci` 字段。CI 日志在 `lane/C4-ci/ci.log`，只计这一轮 CI 写出的 junit 文件。
- 报告的 CI 同机影响（§5.3）比较 C4 和 C4-ci 两格的负载侧指标；提供了 `LOAD_CI_BASELINE` 时，还会比较 CI 自身相对基线的变化。

## 3. 结束后要收集的内容

运行目录是 `.ci-results/load/<run_id>/`，`<run_id>` 的格式为“UTC 时间-提交号前 7 位”：

| 内容 | 位置 |
|---|---|
| 结论 | `report.md`、`report.json`：AT-060 的结果和原因、两个门槛格的逐项判定、平台/模型/工具分解、CI 同机影响 |
| 原始结果 | `run.json`、`cells/<cell>/`（结果契约 §2；保留 30 天） |
| 车道摘要 | `lane-summary.json`：每步耗时、负载、磁盘、遗留进程数 |
| 模板说明 | `template/template.json`：种子、digest、各项装载数量、历史回复情况、正式登录限流的记录。整个 `template/` 目录（含 `pgdata`）很大，只在续跑时需要 |

- 不要收集 `template/private/`、`lane/*/private/`：里面是合成凭据。格子结束时会自动删除各格的 private 目录。
- 证据登记：`report.md` 的结论、`run_id` 和提交号。

### 怎么判 AT-060

`report.json` 的 `at060.result`：
- `passed`：8 格全部完成；每格都有逐 Run 的分解；身份核对违规为 0；C4 和 C4-low 的门槛项全部通过。
- `failed`：`reasons` 逐条列出原因，例如 `C4:gate_drain_seconds,...`、`<cell>:identity_violations`。
- `blocked`：有格子没有跑（`cells_not_run:...`）。

已知的容量结论（调度员 2026-10-11 裁定，不调整门槛）：在 D5 时延下，每个 Run 约 11 s，4 个派发循环约 0.36 Run/s。C4（1 条/秒）必然过载；C4-low 的派发 p95（入队到 Host 接受）在本机实测约 8 s，超过 2 s。这两项会判不通过，作为容量结论交给负责人决定（ROADMAP D-a）。

## 4. 中途失败：保留首轮结果并续跑

- 失败时，车道脚本会停掉本轮启动的所有进程和集群，写出 `lane-summary.json` 和 `failure-summary.md`（失败的步骤、退出码、已有的格子、失败步骤日志的最后 40 行），运行目录完整保留。
- **原样保留首轮结果**，不要删除，也不要手工改动。
- 续跑：

  ```bash
  uv run --frozen python scripts/load/lane_preflight.py --template .ci-results/load/<run_id>/template
  scripts/load/run_on_lane.sh --resume <run_id>
  ```

- 续跑要求检出必须是同一个提交（运行 ID 的后缀），否则拒绝。
- 续跑时的处理：
  - 原来的 `lane-summary.json`、`failure-summary.md`、`logs/` 改名为 `*.before-resume-<时刻>` 保留；
  - 模板完整就复用；残缺的模板改名为 `template.incomplete-<时刻>` 后重建；
  - 已经完成的格子原样保留，不再重跑；
  - 未完成的格子：结果目录挪到 `attempts/<cell>.<n>`，lane 目录挪到 `lane/attempts/<cell>.<n>`，然后重跑这一格；
  - `run.json` 记录 `resumed_at`，以及每次尝试的格子、序号、中止原因和保留位置。
- 报告只读 `cells/` 下的最终结果。首轮的尝试保留在 `attempts/` 里，最终报告需要写明重跑了哪些格子、为什么、两轮的差异。
- 如果是因为代码缺陷而要修改代码，那就不是续跑：提交号变了，要用新提交重新跑一整轮；旧的运行目录作为首轮记录保留。

## 5. 安全边界（§5.1）

- 只用一次性的集群（模板加上每格的克隆），从不连接任何持久化数据库，也不连接服务器。
- 只用合成数据，不使用真实模型或真实渠道：确定性 Host 加上 D5 注入时延，投递是本地 JSON provider。
- 报告里只有指标、门槛结论、种子、digest 和提交号，不含原文，也不含 ID（D8）。
