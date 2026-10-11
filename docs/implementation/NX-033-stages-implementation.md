# NX-033 本地剩余项：CI 阶段脚本与 stages.v2

分支 `nx033-stages`，从 `dispatch/integration-s4y` `863a463` 切出，没有迁移。依据 `NX-033-design.md` §4.1。另外按调度员 2026-10-11 的裁定，把三份文档加入公开发布门槛（§5）。

## 1. 拆分

`scripts/ci/check` 改为依次调用 `scripts/ci/stages/` 下的阶段脚本。各阶段共用 `lib.sh`：严格模式、切换到仓库根目录，以及 `WORKSPACE_BUILD_TESTS` 列表。

| 阶段脚本 | 内容（与原脚本逐行相同） | JUnit |
|---|---|---|
| `dependency-python.sh` | `uv sync --frozen`、预热构建后端与索引 | — |
| `static.sh` | publication、pi_source、definer、contracts、handoff、ci_isolation、public_files | — |
| `dependency-node.sh` | Node 24 检查、`pnpm install --frozen-lockfile` | — |
| `licenses.sh` | `check_licenses.py --check` | — |
| `build-artifacts.sh` | web、pi、agent-host 构建 | — |
| `unit-contract.sh` | deepseek SDK、web、scope-ui 测试 | deepseek-sdk、web、scope-ui |
| `serial-build.sh` | 串行的工作区构建测试 | python-build |
| `deadline-gate.sh` | NX-049 串行时限门槛（guard 4 进程），退出状态即门槛结果 | python-deadline-gate（文件名与位置不变） |
| `integration.sh` | 其余 pytest，并行度由 `NEXLOOP_CI_PYTEST_WORKERS` 控制，默认 6 | python |
| `pi-runtime.sh` | Pi SQLite 一致性测试、agent-host 运行时测试 | pi、runtime |

- **`scripts/ci/check` 仍是整体入口**：
  - 任一阶段失败，就以该阶段的退出码结束，这个退出码就是阶段内第一个失败命令的退出码，和原脚本一致；
  - deadline-gate 失败时只记下结果，继续执行其余阶段，最后输出 `NX-049 deadline gate failed (rc=N)` 并退出 1，和原脚本一致；
  - CI 主机现行车道调用方式不变。
- **阶段顺序**：和原脚本一致，与设计稿 §4.1 的列表有两处不同，都是为了不改变行为：static 在 Node 依赖阶段之前；build-artifacts 在 unit-contract 之前。admission、checkout、package 由控制器或 NX-034 负责，不是脚本。
- **为单独调用新增的唯一一行**：后四个阶段各加了一行 `mkdir -p .ci-results`。它是幂等的，在整体运行时不改变结果。
- **`.ci-policy/stages.v2.json`**：
  - 每个阶段一项，各自声明网络（只有两个 dependency 阶段可以联网）、离线环境、JUnit 和超时；
  - deadline-gate 设为 `stop_on_failure: false`，其余阶段失败即停止，最后仍是 isolation-probe；
  - 必需套件不变（`required-suites.v1.json`）；
  - `stages.v1.json` 未改动。
- **控制器**：`nexloop_ci.py run-once --stages-version 1|2`，默认 1，现行行为不变。`load_policy` 会核对文件里的版本号，结果中记录 `stages_version`，策略 digest 改为对实际所用的 stages 文件计算。在 CI 主机上切换到 v2，是受信策略的变更，需要负责人确认。

## 2. 拆分前后的等价性如何确认

- **一次性对照**（交付前执行）：
  - 方法：把 `863a463` 的原 `scripts/ci/check` 和新的入口放在同一检出中运行。`uv`、`pnpm`、`node` 换成替身：替身记录完整的 argv，并可以在第 N 个命令处以指定退出码失败。随机的 `mktemp` 目录名做归一化处理。
  - 覆盖：
    - 34 个外部命令逐个以退出码 1、2、3 失败，再加全部通过的情况，共 105 种；
    - 每种都比较三样东西：完整的命令序列、退出码、标准错误的最后一行。
  - 结果：全部一致（`bad=0`）。
  - 已知例外：第 13 个命令是 `node -p` 版本探测，位于 `[[ ]]` 之内，它失败在新旧两版中都不会中止执行，两边一致。
- **常驻测试** `tests/test_ci_stages.py`（4 个），同样使用替身：
  - v2 的阶段顺序等于 check 调用的顺序；
  - 每个阶段声明的 JUnit 等于该脚本实际写出的文件，合起来正好是必需套件，门槛文件名不变；
  - check 的命令序列等于逐个运行各阶段脚本的拼接；
  - 各位置失败时，check 以该命令的退出码停止；门槛的两个命令失败时，check 继续执行到底并以 1 结束，报出正确的 rc；单独运行 deadline-gate 时返回门槛自己的状态；
  - v1 不变且仍是默认值；版本 3 被拒绝。
- **`tests/test_ci_isolation.py` 的调整**：它读取 `scripts/ci/check` 中写出的 JUnit 名称来比对必需套件，现在改为从阶段脚本中读取。
- **没有做的**：本机没有运行全量 check（不跑真实构建和测试）。真实车道上的等价性，以下一轮集成全量的套件计数与上一轮一致为准。

## 3. ADR-020 §4 基线的退出条件（提交负责人的材料，不改 ADR）

**依据**：调度员维护的现行合并基线（2026-10-11，s4u 之后）。A 类为 0；B 类（时限敏感）20 项；C 类（并行负载抖动）约 18 项，以清单为准。以下“建议”只是本线的意见，由负责人定夺。

**三种处置**：
- **移入串行时限阶段**：放进 deadline-gate，或新增的串行阶段，用部署形态（guard 4 进程）运行。通过即算通过，从全量并行阶段的基线中移除。
- **降低并行度**：放进新增的 `integration-heavy` 阶段，用较低的并行度（例如 `-n 2`）运行。仍在必需套件中，失败即阻断。
- **保留观察**：留在基线中，满足退出条件后移除。

**通用退出条件**：
- 在其指派的阶段中，连续两轮全量通过，即从基线移除。
- C 类连续两轮全量失败，就升级为 B 或 A。
- 带“违规”断言的用例（例如 read_assert_memo）任何违规都按 A 类处理。

### B 类（20）

| 用例 | 现状证据（摘要） | 建议 |
|---|---|---|
| relationship_context_v4 `v4[complete]` | 已在串行时限阶段，多轮通过 | **移入串行时限阶段**（已完成）。并行阶段应排除它，不再作为基线项 |
| role_pi_effect_checkpoint 两个 Pi 的用例 | 同上 | 同上 |
| agent_host_run_limit 两个用例 | 部署并发下串行通过，超部署并发的并行负载下超时 | **移入串行时限阶段**：它测的正是部署并发上限 |
| closure_plan_run_pg 两个用例、closure_refusal_versions_pg | 计划 Run inspect 503，形态同负载超时 | **降低并行度** |
| outbound_messages_pg agent_reply | CI 主机上串行也失败（retry_wait），疑似连接池饥饿，修复中 | **保留观察**：修复合入后，在 CI 主机串行 3 次全过，再降为 C 或移除 |
| context_artifacts 三进程、context_host_export、context_host_recovery 两例、runtime_effect_recovery、runtime_effect_tools | 真实 Pi，2 秒工具时限；CI 主机串行失败，开发机串行通过 | **保留观察**，归 NX-049 性能跟踪。CI 主机串行 3 次全过后移入串行时限阶段 |
| local_message_delivery_assembly、message_driven_delivery、message_relay、native_web_delivery、scope_denials | 同上（真实 Pi 外发链） | 同上 |
| role_context_v6_pg、context_v6_relationships_pg | CI 主机串行 2/2 失败，开发机通过 | 同上 |

### C 类

| 用例 | 现状证据（摘要） | 建议 |
|---|---|---|
| runtime_dispatch 的 SIGKILL reclaim、role_tail 两例、role_post_accept、model_response_commit_window | 真实 Pi 或进程 kill；同一检出串行通过 | **降低并行度** |
| outbound_messages_pg 的 scope_denied 与 unknown_reconciled、context_engine open_work、message_runtime_effect_e2e、reply_fallback、commercial_j01 | 共用外发链或计划 Run fixture；并行中 503 或观测时序问题；串行通过 | **降低并行度** |
| relationship_context_v4 `tail_statement_wait`、context_v6_pg、message_driven_delivery、native_web_delivery | 真实 Pi，2 秒时限；并行中传输不可用 | **降低并行度** |
| offering_runtime_pg 的 HTTPS 拒绝 | 并行抖动 | **降低并行度** |
| role_policy_dispatch 的并发提交 | 根因已修复（55P03 重试） | **保留观察**：连续两轮通过即移除 |
| read_assert_memo 并发撤销 | 只有正向对照条件不满足 | **保留观察**；任何违规都按 A 类处理，不得放宽 |
| backend_lifecycle_capacity 8 并发 | 根因未观测，可观测性在补 | **保留观察**；下一轮再失败就升级 |

### 落地方式（建议，需另行分派）

- **新增 `integration-heavy` 阶段**：
  - 在 `stages.v3.json` 中新增，用较低并行度运行上表中“降低并行度”的用例；
  - `integration` 阶段用 `--deselect` 排除这些用例，并排除已在串行时限阶段的两个用例；
  - 必需套件增加 `python-heavy`，需要新的 `required-suites` 版本，并登记计数的转移，以免触发 `test_count_dropped`。
- **“移入串行时限阶段”的 agent_host_run_limit**：门槛总时长约 140 秒，加入后会变长。需要负责人确认门槛时长的上限。
- **哪些项何时移除**：以每轮全量的结果为准，由调度员按上面的退出条件更新基线清单。

## 4. 测试与检查

- `tests/test_ci_stages.py` 新增 4 个测试；`tests/test_ci_isolation.py` 和 `tests/test_public_repository_checks.py` 做了同步修改。
- 回归（`-n 3`）：`test_ci_stages`、`test_ci_isolation`、`test_ci_queue`、`test_public_repository_checks`、`test_load_report_lane`，共 44 个通过。

## 5. 公开发布门槛新增三份文档

- **改动**：`deploy/compliance/public-release.v1.json` 的 `required_files.public` 加入 `docs/CONNECTORS.md`、`docs/EVALUATION.md`、`docs/OPERATIONS.md`（调度员 2026-10-11 裁定），说明文字一并更新。
- **测试**：在临时目录中构造这三份文件，不依赖它们是否已在仓库里（L5 的 docs-20261011-6 尚未合入）。
  - 文件齐全时，public 模式通过；
  - 逐个删掉其中一份时，public 模式报 `missing`；
  - 本地 CI 模式不要求这三份文件。

## 6. stages.v3（负责人 D-j 2026-10-11 定案：“全部按推荐”，门槛上限 5 分钟）

分支 `ci-stages-v3`，从 `dispatch/integration-s5b` `9058f02` 切出。实现 §3 的退出方案。

- **串行时限门槛**（`deadline-gate.sh`，junit `python-deadline-gate.xml` 的文件名和位置不变）：
  - 包含 v4[complete]、两个 Pi 的用例，以及 agent_host_run_limit 的两个用例，共 4 个；
  - guard 4 进程，2 秒工具时限不放宽；
  - 这 4 个用例不再在并行阶段运行；
  - v3 策略中阶段超时为 300 秒，超时即判失败。
- **并行阶段**（`integration-parallel.sh`）：原 integration 阶段，用 `--deselect` 排除门槛用例和低并行用例。
- **低并行阶段**（`integration-heavy.sh`，`-n 2`，junit `python-heavy.xml`，必需套件 `python-heavy`）：
  - 放 closure_plan 两例、closure_refusal，以及大部分 C 类用例，共 19 个 ID，参数化展开后 22 个用例；
  - 清单在 `scripts/ci/stages/lib.sh`；
  - 失败即停止，与其他阶段相同。
- **保留观察**：role_policy_dispatch、read_assert_memo、backend_lifecycle_capacity 不移动；CI 主机上串行也失败的 B 类真实 Pi 用例留在并行阶段观察。
- **`scripts/ci/check`**：按 v3 的阶段顺序运行，退出码语义不变：门槛失败只记录，继续执行其余阶段，最后退出 1；其他阶段失败即停止。
- **v1、v2 不变**：`integration.sh` 保留给 v2，它不排除任何用例，所以按 v2 运行不会漏掉用例。控制器只有在受信安装选择 `--stages-version 3` 时才使用 v3 和 `required-suites.v2.json`；默认仍是 v1。
- **计数转移**（`required-suites.v2.json` 的 `count_groups`）：
  - python、python-heavy、python-deadline-gate 三个套件按合计核对，合计不得低于上一轮 main 结果中同样三个套件的合计；
  - 组内套件不再单独核对下降，但 `min_tests` 仍逐个检查；
  - 组的 `allowed_drop` 为 2：v1 下原有的两个门槛用例在门槛和并行阶段各计一次，所以切换后的第一轮会比原来少 2。之后可以在新版本里改回 0。
- **基线规则写入受信策略的说明**：
  - 在指定阶段连续两轮全量通过，即从基线移除；
  - C 类连续两轮全量失败，升级为 B 或 A；
  - 带“违规”断言的用例出现任何违规，都按 A 类处理；
  - 基线清单本身由调度员维护，不入库。

### 测试（`tests/test_ci_stages.py`，用替身记录命令）

- v3 的阶段顺序等于 check 调用的顺序；
- 各阶段声明的 junit 等于脚本实际写出的文件，合起来正好是必需套件，包含 `python-heavy`；
- 门槛命令带 `NEXLOOP_TEST_GUARD_WORKERS=4`，并包含这 4 个用例；
- 并行阶段排除了全部门槛用例和低并行用例；低并行阶段用 `-n 2` 运行清单中的用例，不含门槛用例；
- 清单中的全部 ID 实际能收集到 26 个用例，ID 过期会使测试失败；
- 转移不算计数下降，丢失用例算下降；不在组内的套件仍按原规则逐个核对；
- v2 不变，并且仍引用它自己的脚本。

### 预计门槛时长

| 用例 | 开发机实测（串行，guard 4） | CI 主机 |
|---|---|---|
| v4[complete] | 约 30 s（call 27.6 + setup 2.4） | 实测 72.6–73.4 s（s4u） |
| 两个 Pi 的用例 | 约 29.6 s（23.7 + 6.0） | 实测 63.7–65.2 s（s4u） |
| agent_host_run_limit 拒绝 | 约 26.4 s（19.9 + 6.5） | 估算约 57–62 s |
| agent_host_run_limit 排队 | 约 25.8 s（19.7 + 6.1） | 估算约 57–62 s |

- **开发机整体**：门槛脚本整体运行 111 s，4 个用例全部通过。
- **CI 主机估算**：按前两个用例的 CI 主机与开发机耗时比例（约 2.2–2.4 倍）推算，合计约 255–270 s，低于 300 s，但余量只有约 10%。
- **超过 300 s 时的备选**：把 run_limit 两例改为门槛之后的独立串行阶段（同样是 guard 4、不放宽时限，单独计时）。这需要负责人另行确认，因为它改变的是“移入门槛”这一决定。
