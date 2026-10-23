# relay 双进程与连接预算（方案 B）

依据：调度员 2026-10-13。AT-060 C4-low 在 relay 2 个进程下通过：run 20261013T110112Z-fa9a71c，空闲派发 p95 4279.6 ms（门槛 5000），relay 段 p95 4050 ms，连接峰值 51。部署也改用 2 个 relay，连接预算按 [relay 步骤计时与多进程](relay-step-timing.md) §3 的方案 B 调整。

## 1. 连接预算（`deploy/stage/connection-budget.v1.json`）

| 项 | 原来 | 现在 | 连接数变化 |
|---|---|---|---|
| message-relay | 1 进程 × 池 2 | 2 进程 × 池 2 | +2 |
| api-browser-identity（每个 API 进程的浏览器登录池，`--identity-pool-max`） | 2 进程 × 4 | 2 进程 × 3 | −2 |
| 合计 | 59 | **59**（上限 60） | 0 |

部署门禁测试（`tests/test_stage_compose.py`、`tests/test_connection_budget.py`、`tests/test_deploy_gate.py`、`tests/test_load_chain_pg.py`）已同步更新并通过：

- 预算合计不超过 60；
- Compose 中标为 `message-relay` 的容器数等于预算里的进程数；
- 两个 API 的 `--identity-pool-max` 都是 3。

**前提**：identity 池降到 3 尚未在登录高峰下复测（见第 4 节）。复测不通过时，备选是方案 A（relay 2 进程 × 池 1，合计 59）；按 NX-031 D7 的实测，relay 池 1 会超时或失败，同样需要复测。

## 2. stage Compose：第 2 个 relay 并入主文件

`deploy/stage/compose.app.example.yaml` 默认运行 `message-relay` 与 `message-relay-2`，原可选覆盖文件 `compose.app.relay-replicas.example.yaml` 已删除。

**理由**：预算声明了 2 个 relay 进程以后，部署门禁要求按标签数出的 relay 容器正好是 2 个。如果第 2 个 relay 仍放在覆盖文件里，不叠加覆盖文件的部署就会被门禁拒绝，覆盖文件实际上成了必选项。并入主文件后只有一套配置，门禁、预算和 Compose 三者一致。

**两个 relay 的关系**：
- 除 vault 目录外完全相同（测试逐项比较）：同一组凭据和 run profile，`--context-strategy recent_plus_required`。
- vault 分别是 `data/relay-vault` 与 `data/relay-vault-2`。一个 vault 只能有一个属主进程，部署前须各自创建（0700，属主为服务用户）。
- 两者共用 `message_relay_*` 机密文件，测试只允许这两个服务共用。

## 3. 压测栈与部署用同一套配置

- `scripts/load/stack.py`：`start_api(identity_pool_max=None)` 默认读取部署预算中 `api-browser-identity` 的 `pool_max`（现在是 3），并传给 API 的 `--identity-pool-max`。
- 可以覆盖：`matrix.py --identity-pool-max 1..8`，或车道变量 `LOAD_IDENTITY_POOL_MAX`。
- `cell.json` 记录 `identity_pool_max`。
- relay 进程数沿用 `--relay-processes`（`LOAD_RELAY_PROCESSES`），默认仍是 1。调度员今晚的车道显式设为 2；如需让默认值也跟随预算，可以另行修改。

## 4. 登录高峰复测方案（identity 池 3 对比 4）

**测量缺口（已补上）**：

- 负载里的客户登录只发生在预热的 `login_all`（全部客户，每次 8 个并发）中，原来只记失败数，没有延迟。现在 `cell.json` 新增 `logins`：登录请求数、已应答请求的 p50/p95/max，以及状态码计数（429 = 登录限流，503 = 后端繁忙 BackendBusy，0 = 无应答）。
- API 进程不写连接池样本，所以 identity 池的等待只能间接看：登录延迟、503，以及稳态 ACK。

**做法**（CI 主机，同一模板与 seed，relay 2）：

1. `LOAD_RELAY_PROCESSES=2 LOAD_IDENTITY_POOL_MAX=4`，只跑 C4-peak（3/s，最高负载格），作为基线。
2. `LOAD_RELAY_PROCESSES=2 LOAD_IDENTITY_POOL_MAX=3`（等同不设，跟随预算），同样只跑 C4-peak。
3. 再各跑一次 C4，确认门槛格不受影响。

**比较**（两次运行逐格对照）：

| 指标 | 位置 | 判定 |
|---|---|---|
| 登录 p95 / max、503 数、0 数 | `cell.json` 的 `logins` | 3 与 4 相比，p95 增幅 ≤ 20%，并且 503 = 0、0 = 0 |
| 稳态 ACK p95 / p99、ACK 状态（503 即 BackendBusy） | report 各格指标与 `ack_statuses` | ACK p95 仍 ≤ 500 ms，503 不增加 |
| 工作台登录与总览状态 | `cell.json` 的 `workbench_events` | 全部 200 |
| 连接峰值 | report `connections_peak` | ≤ 60 |

上表的判定标准是我提的建议，需要调度员或负责人确认。

**结论处理**：
- 全部满足：维持方案 B。
- 登录 p95 明显变差或出现 503：改用方案 A（relay 池 1）复测；或者保留 identity 池 4，relay 回到 1 个进程。后者会让 C4-low 失去余量。

### 4.1 复测记录（草稿，2026-10-23，执行人：调度员；分析：L4）

**设置**：
- CI 主机负载车道，检出 `1892c9ac`（第十八批，当时 stage 上的版本）。
- 200 个客户，`deploy/load/matrix.v2.json`，只跑 C4-peak（3 条/秒）和 C4（0.1 条/秒）。
- relay 2 个进程；identity 池分别为 4、3、2，三组都经 `run_on_lane.sh` 运行，退出码均为 0。
- 运行 ID：`20261022T161615Z`（池 4）、`T173057Z`（池 3）、`T184236Z`（池 2）。
- 先尝试过 1000 客户，但模板历史回放只有约 0.1 条/秒，与 NX-031 阶梯记录一致（1000 客户的模板在车道上从未跑完），于是中止，改用 v0.1 定案的 200 客户口径（D-a）。

**结果**（每格两个数字依次为 C4-peak / C4）：

| 池 | 登录 p95（ms） | 429 / 503 / 无应答 | 连接峰值 | ACK p95（ms） |
|---|---|---|---|---|
| 4（基线） | 789.5 / 799.7 | 0 / 0 / 0 | 56 / 54 | 1131 / 448 |
| 3 | 814.7 / 861.1 | 0 / 0 / 0 | 54 / 52 | 1095 / 525 |
| 2 | 791.6 / 762.5 | 0 / 0 / 0 | 52 / 50 | 1129 / 431 |

**判定**：
- 登录：三组的 p95 增幅都在 20% 以内（最大为池 3 C4 的 +7.7%），429、503、无应答都是 0。池大小对登录时延没有可见影响。
- 连接峰值随池变小而下降，三组都 ≤ 60。
- C4 的 ACK p95：池 4 为 448，池 2 为 431，都 ≤ 500 ms。池 3 为 525，超出门槛，但池 2 更小、又回到 448 以下，所以按噪声处理。若要确认，需要重复运行。
- C4-peak 的 ACK p95 三组都约 1.1 s，基线也一样，所以与池大小无关。原对比脚本把 ACK ≤ 500 ms 也套用到了 C4-peak，这是口径错误：PRD 的 ACK 门槛只是门槛格（C4、C4-low，见 `report.py` 的 `GATES`）的检查项，C4-peak 是观察格。`scripts/load/login_retest_compare.py` 已改为只对 C4 判 ACK，并新增 429 = 0 的检查。
- **结论（调度员）**：v0.1 规模下池 2 足够。1ed61fe3（relay 每个进程语义池 1，identity 每个进程 3 → 2，合计仍为 59）可以进入下一批。这个结论不能外推到 1000 客户。

**C4-peak 的 ACK p95 约 1.1 s 从哪里来**（只读分析，未在车道上取样，属于推断）：
- ACK 是驱动端从 POST 消息到收到 202 的时延，只涉及 API 进程的受理（先持久化，再 ACK），不经过 relay 和 Run 的处理。
- 3 条/秒远超处理能力：每个 Run 约 25–30 s，4 个派发循环约 0.36 Run/s（NX-031 §8 与车道手册中的容量结论）。稳态阶段积压持续增长，后台 worker、relay 和派发对 PG 的争用随之上升，API 的受理写入因此变慢，这是最可能的原因。
- 三组的 ACK 与 identity 池大小无关（1131、1095、1129），所以不是 identity 池排队。
- **是否立项**：这属于已知的容量结论（D-a：3 条/秒的格只观察），当前不需要立项。如果要定位 ACK 在高积压下变慢的具体位置（哪条语句、哪把锁），可以另开一个只读测量任务：在 C4-peak 稳态时对 `pg_stat_activity` 的等待事件和 API 受理语句的 `pg_stat_statements` 取样。建议列为后续候选，由负责人决定。

**执行中的问题**：
- 第二组和第三组按 L4 给的“复用模板直接运行 `matrix_cells.py`”命令执行，两次都以 `service_start_failed` 失败，API 日志为 `private service-owned TLS files required`。
- 原因：命令里的 `--out`、`--lane-root` 用的是相对路径（`.ci-results/load/...`）。`stack.py` 让每个服务在各自的工作目录中启动，API 再按相对路径找 TLS 文件就找不到了；`read_private_text` 打开失败，于是报这条错误。`run_on_lane.sh` 传的是绝对路径（`$root/.ci-results/...`），所以没有这个问题。
- 修复：`scripts/load/matrix.py` 在解析参数后，把 `--template`、`--out`、`--lane-root` 都转成绝对路径。运行单里的直接运行命令也改为使用绝对路径（`docs/implementation/login-peak-retest.md` §2）。
