# 登录高峰复测运行单（relay2-budget §4）

执行人：调度员。在 CI 主机的负载车道上执行，时间在夜间窗口内（22:00 之后）。依据是 `docs/implementation/relay2-budget.md` 的 §4。本文只写角色名和参数，实际取值放在 CI 主机的私有 env 里。

## 0. 只跑两格：不用子矩阵文件

`scripts/load/matrix.py` 的 `validate_matrix` 要求矩阵文件包含全部 8 格，只含 C4-peak 和 C4 的子矩阵文件会被拒绝。因此不新建矩阵文件，改用已有的格子筛选执行器：

- `LOAD_RUNNER_CMD=scripts/load/matrix_cells.py`
- `NEXLOOP_LOAD_CELLS=C4-peak,C4`

这个执行器就是给 `matrix.py` 加上 `--cells`。NX-052 只跑 C4-low 时用的也是它。报告只有这两格时也能正常生成：CI 同机影响一节缺少 C4-ci，会直接跳过。

矩阵和客户数在三组之间必须相同（§4 要求同一模板和 seed）：

| 选项 | 设置 | C4 速率 | 说明 |
|---|---|---|---|
| A（建议） | 默认：`deploy/load/matrix.v1.json`，`LOAD_CONSUMERS` 不设（即 1000） | 1/s | 登录高峰为 1000 个客户；耗时见 §3 |
| B | `LOAD_MATRIX=deploy/load/matrix.v2.json LOAD_CONSUMERS=200` | 0.1/s | v0.1 的客户规模；v2 仍是草稿，等负责人补 D-a |

两种矩阵里 C4-peak 都是 3/s。

## 1. 前置条件

| 项目 | 说明 |
|---|---|
| `NEXLOOP_CI_LOCK`（必需） | 和常规 CI 共用的锁文件路径。取值与车道上常规 CI 所用的相同，在 CI 主机车道账户的私有 env 里，不进仓库 |
| `NEXLOOP_TEST_PG_BIN`（必需） | PostgreSQL 18 的 bin 目录。取值同常规 CI，也在同一份私有 env 里 |
| CI 锁 | `run_on_lane.sh` 用 `flock` 持有这把锁，**整轮都不释放**，包括模板、两格和报告。在此期间，常规 CI 会在同一把锁上排队，**会被挡住**，等这一轮结束才能跑。开跑时如果锁正被常规 CI 占用，最多等 `LOAD_LOCK_WAIT` 秒（默认 1800），等不到就以退出码 75 退出 |
| 检出 | 必须是干净的 main 检出，提交就是要测的那个；`.venv` 和 `apps/agent-host/dist` 必须由这个提交构建（`uv sync --frozen`，`npm ci && npm run build`） |
| 环境 | `REAL_DISPATCH_ENABLED=false`；不能有 `MODEL_*`、`EMBEDDING_*`、DSN 或凭据变量。车道脚本会检查，不满足就以退出码 2 拒绝 |
| 窗口 | 默认 `LOAD_WINDOW=22:00-06:00`；在窗口外运行需加 `--outside-window`，由调度员决定 |
| 预检（只读） | `uv run --frozen python scripts/load/lane_preflight.py`。全部通过时退出码为 0。锁只做一次非阻塞探测，探测完立即释放 |

建议开跑前先确认当晚没有常规 CI 在排队，或者已经暂停。三组合计会占用锁约 5 小时（见 §3）。

## 2. 三组运行

公共变量（每组都要设）：

```bash
export LOAD_RUNNER_CMD=scripts/load/matrix_cells.py NEXLOOP_LOAD_CELLS=C4-peak,C4 LOAD_RELAY_PROCESSES=2
```

依次执行，每组各调用一次 `run_on_lane.sh`：

```bash
LOAD_IDENTITY_POOL_MAX=4 scripts/load/run_on_lane.sh
```
```bash
LOAD_IDENTITY_POOL_MAX=3 scripts/load/run_on_lane.sh
```
```bash
LOAD_IDENTITY_POOL_MAX=2 scripts/load/run_on_lane.sh
```

- 第一组（identity 池 4）是基线。
- 每组结束都会打印 `<run_id>`，结果在 `.ci-results/load/<run_id>/`。核对 `cells/<格>/cell.json` 里的 `identity_pool_max`，应等于该组设置的值。
- 中途失败时，可以用 `--resume <run_id>` 续跑，已完成的格会保留。

**三组共用一个模板**（200 客户时推荐；2026-10-23 复测实际用的是三次 `run_on_lane.sh`）：

- identity 池只是启动 API 的参数，与模板无关；模板中 fixture 凭据的有效期是 30 天。
- 第一组照常用 `run_on_lane.sh`，记下它的运行目录 `R1`。后两组自己拿 CI 锁，直接运行 `matrix_cells.py` 和 `report.py`，复用 `R1/template`。
- **路径必须是绝对路径**：各服务在自己的工作目录里启动，相对路径会让 API 读不到 TLS 文件，报 `private service-owned TLS files required`（`service_start_failed`）。包含修复的提交中，`matrix.py` 会自己把路径转成绝对路径；在较早的检出上，必须按下面的写法使用 `$PWD`。
- 车道脚本会设置的环境变量，这里要显式设置（`REAL_DISPATCH_ENABLED=false`、`NEXLOOP_TEST_GUARD_WORKERS=4`）。这种方式不经过车道预检和失败收尾，失败时要手工检查 `$R/lane` 下是否有残留的进程和集群。

```bash
R1=$PWD/.ci-results/load/<池4的run_id>; for P in 3 2; do R=$PWD/.ci-results/load/$(date -u +%Y%m%dT%H%M%SZ)-pool$P; mkdir -m 700 -p $R/lane && flock -w 1800 "$NEXLOOP_CI_LOCK" env REAL_DISPATCH_ENABLED=false NEXLOOP_TEST_GUARD_WORKERS=4 LOAD_CI_COMMAND=scripts/ci/check NEXLOOP_LOAD_CELLS=C4-peak,C4 uv run --frozen python scripts/load/matrix_cells.py --template $R1/template --matrix $PWD/deploy/load/matrix.v2.json --out $R --lane-root $R/lane --relay-processes 2 --identity-pool-max $P && uv run --frozen python scripts/load/report.py $R --out $R; echo "pool $P -> $R exit $?"; done
```

**“identity=2 加 relay 语义池 1”这一组目前模拟不了：**
- 负载栈 `scripts/load/stack.py` 不会把 `--semantic-read-pool-max` 传给 relay。
- relay 的这个参数（89fcc48a）还在第十五批里，不在 main c58b6a33 上。
- 要模拟这一组，需要另外改 `stack.py` 和 `matrix.py`（例如新增 `LOAD_RELAY_SEMANTIC_POOL_MAX`），而且要基于第十五批合入之后的 main。
- 另外，这个池只在 E3 语义读取真正发生时才会占用连接。负载模板里的 Run 会不会走到语义读取，需要 L3 确认；如果不会，这一组即使加上，也测不出差别。

## 3. 预计耗时

依据 NX-031 车道手册的估算，以车道实测为准。每次调用 `run_on_lane.sh` 都会重建一次模板。

| 步骤 | 选项 A（1000 客户） | 选项 B（200 客户） |
|---|---|---|
| 模板 | 约 50–60 分钟 | 约 10–15 分钟（按客户数折算） |
| C4-peak | 约 20–25 分钟 | 约 20–25 分钟 |
| C4 | 约 20–25 分钟 | 约 20–25 分钟 |
| 报告 | 不到 1 分钟 | 不到 1 分钟 |
| **每组** | **约 1.5–1.8 小时** | **约 0.9–1.1 小时** |
| **三组合计** | **约 4.5–5.5 小时** | **约 2.7–3.3 小时** |

- 从 22:00 开始，选项 A 预计 02:30–03:30 结束，选项 B 预计 01:00 前后结束，都在 06:00 之前。
- 如果以后能加上第四组，选项 A 合计约 6–7.5 小时，仍在 8 小时窗口内，但余量很小。
- 预热阶段会让全部客户各登录一次（每次 8 个并发），这就是要测的登录高峰。

## 4. 判定表与读取方法

以第一组（identity 池 4）为基线，后面每组逐格（C4-peak、C4）与它对比：

| 指标 | 读取位置 | 判定 |
|---|---|---|
| 登录 p95 / max | `cells/<格>/cell.json` → `.logins.p95_ms`、`.logins.max_ms` | p95 比基线增加不超过 20% |
| 登录 429、503、无应答 | `cell.json` → `.logins.statuses["429"]`、`["503"]`、`["0"]` | 都为 0（429 不为 0 说明没有做到每个客户一个桶，这一组作废） |
| 稳态 ACK p95 / p99 | `report.json` → `.cells[].metrics.ack_p95_ms`、`.ack_p99_ms` | 只对门槛格 C4：p95 不超过 500 ms（C4-peak 是观察格，只记录） |
| ACK 503（BackendBusy） | `report.json` → `.cells[].ack_statuses["503"]` | 不比基线多 |
| 工作台事件 | `cell.json` → `.workbench_events`（标签为 `<类型>:<状态>`） | 全部是 200 |
| 连接峰值 | `report.json` → `.cells[].metrics.connections_peak` | 不超过 60 |

判定标准来自 §4 的建议，由调度员或负责人确认。

**对比脚本**：三组的运行目录依次传入，第一个是基线。脚本输出一张 Markdown 对比表和一段 JSON 判定，全部通过时退出码为 0，有任何一项不通过时为 1，读不到输入时为 2。

```bash
uv run --frozen python scripts/load/login_retest_compare.py .ci-results/load/<run_id_池4> .ci-results/load/<run_id_池3> .ci-results/load/<run_id_池2>
```

**只看原始字段时**，可以用 jq（对每个运行目录执行一次）：

```bash
R=.ci-results/load/<run_id>; for c in C4-peak C4; do jq -c --arg c $c '{cell:$c, pool:.identity_pool_max, logins:.logins, wb:.workbench_events}' $R/cells/$c/cell.json; done; jq -c '.cells[] | {cell, ack_p95:.metrics.ack_p95_ms, ack_p99:.metrics.ack_p99_ms, ack:.ack_statuses, conn:.metrics.connections_peak}' $R/report.json
```

## 5. 结论处理（同 §4）

- 全部满足：维持方案 B。
- 登录 p95 明显变差或出现 503：
  - 改用方案 A（relay 池 1）复测；
  - 或者保留 identity 池 4，relay 回到 1 个进程，但这样 C4-low 会失去余量。
- 池 2 的结果是 1ed61fe3（每个进程 3→2）的依据：池 2 通过，1ed61fe3 才能进批次。
- 证据：三个 `report.json`、对比脚本的输出、运行 ID 和提交号，作为 evidence 写入，类型为 `test_report`。
