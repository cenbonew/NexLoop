# AT-072 演练入口单：只按公开文档完成一次“发布 + 恢复”

- **验收项**：AT-072（第二维护者演练）。负责人本人执行，只读本仓库的公开文档。
- **时间**：
  - 历史预约：2026-10-19（周一）17:00 之后（负责人 2026-10-18 的安排）；
  - 实际执行窗口：由负责人另行指定，以演练记录中的开始与结束时间为准。
- **本单的作用**：给出从哪份文档开始、按什么顺序读；说明一次性环境的边界；提供偏差记录模板，以及演练期间的沟通规则。本单不替代任何手册，手册里写不清的地方照实记成偏差。
- **桌面推演**：L5 于 2026-10-18 按本单顺序走了一遍。恢复链路（备份 → 恢复 → 重放 → 校验 → 开放）在开发机上按 [本机恢复演练手册](at-072-local-restore-drill.md) 实测通过；发布链路只做了文档推演，加上 `render.py --check` 一项实测。发现的断点和处理见 §5。

## 1. 演练期间的规则

1. **调度员和各开发线不做口头补充。** 演练开始后，不回答“这一步什么意思”“参数填什么”这类问题，也不主动提示。
2. 执行人遇到手册不清楚的地方，**按自己的理解先做**，并在偏差表（§4）记一行。如果确实无法继续，就记一条“阻断”，然后停在这一步；调度员不现场救火。
3. 只有三种情况可以中途打断演练：会碰到 stage 或生产的资源；需要真实凭据；或者出现安全问题（例如凭据被打印出来）。这时停止演练，记一条“中止”，事后再处理。
4. 演练结束后，偏差表交给文档线，由文档线修订手册。修订合入之后，AT-072 的证据才能回填；回填由调度员执行。

## 2. 一次性环境

| 部分 | 环境 | 允许 | 不允许 |
|---|---|---|---|
| 恢复 | **Mac 本地临时 PG 18**：`scripts/dev/restore_drill.sh` 建的归档集群与回环 API，加上 `.dev-drill/` 里的备份、日志和恢复出来的实例 | 合成租户、合成账号；本机 socket 与 127.0.0.1；在恢复出来的一次性实例上轮换 API 角色的口令并开放；用完删除 | 连接 stage 的 DATA_HOST 或 APP_HOST；使用 pgBackRest 仓库或 restic 仓库的真实口令 |
| 发布 | **CI 主机上 `nexloop-ci` 的可用范围**：它自己的构建工作区（`$CI_BUILD_ROOT`）、rootless docker（containerd 存储，linux/amd64）、syft/grype，以及 NX-033 的 CI 队列 | 在构建工作区里新建一个演练目录，放一次性的发布根和临时签名密钥；演练结束后删除 | 使用 `nexloop-release` 用户、正式发布归档根或正式发布签名私钥；`deploy.py promote`；任何 `docker compose up`；向 stage 拷贝产物 |
| 部署 dry-run | 同上，`nexloop-ci` 账户 | `deploy.py dry-run` 只渲染、只核对，`executed: false` | 读取真实的 stage.env；连接 stage |

- **不碰 stage**：演练中的任何一步都不连接 APP_HOST 或 DATA_HOST，也不读取它们的私有文件。
- **演练产物不得部署**：演练发布用的是临时密钥，正式的 allowed_signers 验证不过，这是有意的。

### 2.1 演练前由调度员准备（属于环境准备，不属于口头补充）

- 在 `nexloop-ci` 的登录环境（`bash -lc` 或登录 shell）里导出四个非秘密变量：`CI_BUILD_ROOT`、`CI_QUEUE`、`TRUSTED_REPO`、`CI_TRUSTED`。CI 工具的入口是 `python3 "$CI_TRUSTED/scripts/ci/nexloop_ci.py"`。演练前用 [运行手册 §0.1](../implementation/NX-034-first-release-runbook.md#01-参数从哪里来) 的只读核对命令验证。**这属于环境准备**：执行人只读取这些变量，不需要读任何私有材料。
- **工具链与镜像的验收**（属于环境准备，**必须在演练开始前完成**；演练中发现缺失时记“阻断”，不现场补）：
  1. 以 `nexloop-ci` 的登录 shell（`bash -lc`），在一个临时检出（`git -C "$TRUSTED_REPO" worktree add --detach <临时目录> "$SHA"`）里执行 `uv sync --frozen --offline` 和 `pnpm install --frozen-lockfile --offline`，两条的退出码都是 0。退出码不为 0，通常是登录环境没有加载 CI 自己的缓存设置，uv 或 pnpm 用了空的默认缓存。
  2. 按 [运行手册 §2 第 6 步](../implementation/NX-034-first-release-runbook.md#2-首个签名发布逐条命令方案-a) 核对五个基础镜像都已按 digest 在本地：`python-build-base`、`node-host-base`、`go-build-base`、`gateway`、`postgres`。缺失的按 digest 预取，从不按标签拉取。
  3. 核对完删除临时检出（`git -C "$TRUSTED_REPO" worktree remove <临时目录>`）。
- 确认 `$SHA`：选 main 上一个已经通过 CI 的提交，也就是 `python3 "$CI_TRUSTED/scripts/ci/nexloop_ci.py" result --queue "$CI_QUEUE" --sha <SHA>` 返回 `passed`，并写进本单的执行记录。
- 确认 `nexloop-ci` 能读到受信仓库 `$TRUSTED_REPO`，并且基础镜像已经按 digest 预载（first-release 运行手册 §2 第 6 步）。
  - 这两项如果需要主机配置变更，要在演练开始之前由负责人完成；演练开始后不再处理。
- Mac 上已有 PostgreSQL 18 与仓库的 `.venv`（`uv sync --frozen`）。

## 3. 文档起点与顺序

**两部分按顺序做，不要并行**：先做恢复（§3.2），恢复部分全部完成，并按 [本机恢复演练](at-072-local-restore-drill.md) §9 清理、核对没有残留之后，再开始发布（§3.1）。第三次演练中两部分并行进行，恢复的清理和发布构建交错在一起，日志难以对应。

### 3.1 发布：构建、签名、验签、部署 dry-run（CI 主机，`nexloop-ci`）

| 顺序 | 文档 | 读什么 | 演练中的做法 |
|---|---|---|---|
| 1 | [OPERATIONS.md](../OPERATIONS.md) §6 | 发布工具的全貌与各文档入口 | 只读 |
| 2 | [NX-034 首个签名发布运行手册](../implementation/NX-034-first-release-runbook.md) §0、§0.1、§1 | 参数表、参数来源与开工前核对、方案 A | 先执行 §0.1 的只读核对，任一项不符就记“阻断”并停止；再按 §3.3 的替换表设好 `DRILL_ROOT` 等参数 |
| 3 | 同上 §2 第 1–9 步 | 核对 CI、干净检出、离线依赖、平台、基础镜像、`build`、`sbom`、`preflight --category stage` | 第 3 步的检出放在 `$DRILL_ROOT/checkout`；`--release-root` 用 `$DRILL_ROOT/releases-root` |
| 4 | 同上 §2 第 12–16 步 | 独立核对、签名、封存、`archive.py check`（验签） | 由 `nexloop-ci` 执行，密钥与 allowed_signers 用临时的；第 10、11 步的移交与拷贝不做 |
| 5 | 同上 §4 与 §4.1 | dry-run 所需的文件与命令 | 第 1、2 步：`--out` 用 `$DRILL_ROOT/plans/app`、`$DRILL_ROOT/plans/data`；第 3、4 步按 §4.1 从公开示例构造非秘密 env 后执行，账户上没有 `docker compose` 时第 4 步记 `not_run`；第 5 步 `load-check` 可选。**演练自己从公开示例生成的文件可以读写，禁止读取的只是事先存在的私有 env** |
| 6 | 同上 §2 第 17 步 | 清理 | 用运行手册 §2“说明”中的**演练目录清理命令**删除整个 `$DRILL_ROOT`：它先断言路径在 `$CI_BUILD_ROOT` 下、目录名是演练目录名，再恢复写权限后删除。不允许 `rm -rf` 时用同处的 Python 替代。这条命令绝不能用于正式归档，正式归档用 `make prune-releases` |

背景资料（遇到问题时查阅）：[NX-034 第二部分实现说明](../implementation/NX-034-part2-implementation.md)、[NX-034 本地演练](../implementation/NX-034-local-dry-run.md)（其中有临时测试密钥的生成方式）。

### 3.2 恢复：备份、PITR 恢复、重放、校验、开放（Mac）

| 顺序 | 文档 | 读什么 | 演练中的做法 |
|---|---|---|---|
| 1 | [OPERATIONS.md](../OPERATIONS.md) §5 | 恢复状态机 `restored → replayed → verified_readonly → open` | 只读 |
| 2 | [NX-035 恢复命令行](../implementation/NX-035-restore-cli.md) §1–§3 | 恢复做什么、开放前核对、参数与凭据 | 只读 |
| 3 | [AT-072 本机恢复演练](at-072-local-restore-drill.md) §0–§9 | 源端（归档 PG + 工作台 API）→ 备份 → 恢复点 → 经工作台合成删除 → 命名点恢复 → 在恢复实例上起 API → 校验 → 负责人开放 → PITR → 清理 | **照做**，这是恢复部分的主手册。§10 是出错时的对照表 |
| 4 | [NX-035 本地部分实现说明](../implementation/NX-035-local-implementation.md) “开放”一节 | `open_scope` 的条件 | 只读，用来理解第 3 行 §6 的结果 |

[NX-035 恢复命令行](../implementation/NX-035-restore-cli.md) §7 是不带工作台的最小命令序列，只覆盖到 `verified_readonly`，本次不用。

背景资料：[NX-035 部署运行手册](../implementation/NX-035-deploy-runbook.md) §4，这是 stage 上的真实演练，本次**不执行**，只用来对照。

### 3.3 发布手册参数在演练中的替换

先定一个演练目录，之后所有演练文件都放在它下面：

```bash
DRILL_ROOT="$CI_BUILD_ROOT/<演练目录名>"   # 目录名由执行人自定，例如带日期；不能已存在
```

```bash
install -d -m 700 "$DRILL_ROOT" "$DRILL_ROOT/plans" "$DRILL_ROOT/logs"
```

| 手册参数 | 演练取值（都是 `$DRILL_ROOT` 下的相对路径） |
|---|---|
| 检出（§2 第 3 步） | `$DRILL_ROOT/checkout` |
| `$RELEASE_ROOT` | `$DRILL_ROOT/releases-root`，由 `nexloop-ci` 新建，0700 |
| `$HANDOFF_ROOT` | 不用。`build` 直接写入演练发布根 |
| `$RELEASE_SIGNING_KEY` | `$DRILL_ROOT/drill-key`，新生成的 ed25519 临时密钥：`ssh-keygen -t ed25519 -N '' -C drill-release -f "$DRILL_ROOT/drill-key"` |
| `$ALLOWED_SIGNERS` | `$DRILL_ROOT/allowed_signers`，由临时公钥生成一行：`nexloop-release namespaces="nexloop-release" <公钥类型> <公钥>` |
| `$PRIVATE_STAGE_ENV` | `$DRILL_ROOT/stage.env`：`install -m 600 deploy/stage/stage.env.example "$DRILL_ROOT/stage.env"`，即示例中的文档值（2026-10-18 实测 `render.py --check` 结果为 `valid: true`） |
| `$PLAN_DIR_APP` / `$PLAN_DIR_DATA` | `$DRILL_ROOT/plans/app`、`$DRILL_ROOT/plans/data`（不能已存在，由 dry-run 新建） |
| `$PRIVATE_COMPOSE_APP_ENV` / `$PRIVATE_COMPOSE_DATA_ENV` | `$DRILL_ROOT/compose.app.env`、`$DRILL_ROOT/compose.data.env`，按运行手册 §4.1 从公开示例构造 |
| `$RELEASE_COMMIT` | 演练发布的 `manifest.json` 中的 `commit` |

## 4. 偏差记录模板

**计时与 tee 包装（开工前先准备好）**：每条命令都经同一个包装执行，同时记录起止时间（UTC，微秒）、退出码和原始输出。在每个 shell 里定义一次：

```bash
run() { local name=$1; shift; local log="$LOGDIR/$name.out"; local now='import datetime; print(datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds"))'; { echo "# start $(python3 -c "$now")"; echo "# cmd $*"; } >> "$log"; "$@" 2>&1 | tee -a "$log"; local rc=${PIPESTATUS[0]}; { echo "# end $(python3 -c "$now") rc=$rc"; } >> "$log"; return "$rc"; }
```

用法：先设 `LOGDIR`（发布用 `$DRILL_ROOT/logs`，恢复用 `.dev-drill/logs`，见下文），然后执行 `run 07-build uv run --frozen python scripts/release/release.py build …`。退出码取命令本身的退出码，不是 tee 的。这个函数用了 bash 的 `PIPESTATUS`，要在 bash 里定义和使用（zsh 的写法是小写的 `pipestatus`）；2026-10-19 在 bash 下核对过：命令以 3 退出时 `run` 也返回 3，日志首尾有 start/end 两行。预期返回非 0 的命令（例如恢复手册 §6.3）按该节的写法处理。

**原始输出**：每一步的原始 stdout 都用 `tee` 存成演练目录里的文件，例如 `<命令> 2>&1 | tee "$DRILL_ROOT/logs/<步号>-<短名>.out"`；恢复部分存在 `.dev-drill/logs/` 下。源端脚本要求 `.dev-drill` 事先不存在，所以源端的启动日志先 tee 到演练目录之外，等出现 ready 之后再建 `.dev-drill/logs` 并把日志搬进去（[本机恢复演练](at-072-local-restore-drill.md) §1）。报告与偏差表只引用这些文件名，不靠事后回忆或终端回滚。2026-10-19 的演练中，有一段输出因为执行工具故障丢失了。

每发现一处就记一行，当场记。“实际怎么做的”要写到能复现的程度，包括命令和输出的关键字段；不要写口令、地址或主机名。

| # | 时间 | 文档与位置（文件 §/步号） | 手册哪里不清楚或不对 | 实际怎么做的 | 结果（成功 / 绕过 / 阻断 / 中止） | 建议怎么改 |
|---|---|---|---|---|---|---|
| 1 | | | | | | |

另外记录：

- 演练开始与结束时间；
- 发布部分用到的 `$SHA` 与 `$RID`；
- 恢复的 RTO：从 [本机恢复演练手册](at-072-local-restore-drill.md) §5 的恢复命令开始执行，到它输出 `verified_readonly` 为止，用外层墙钟计时（例如在命令前后各记一次 `date -u +%FT%TZ`，或用 `time`）。
  - 工具报告里的 `rto_seconds` 是恢复工具自己记录的，从它开始恢复算到 `verified_readonly`，不含命令启动前的准备和工具外的等待，所以通常比外层墙钟时间短。两者都记，标明各自的来源；
- 每份 JSON 输出的关键字段：`ok`、`passed`、`state`、`ready`、`refused`。

## 5. 桌面推演发现的断点（2026-10-18，L5）

| # | 断点 | 处理 |
|---|---|---|
| B1 | OPERATIONS §5 仍写着“命令行恢复工具留到部署切片”，但 `nexloop-restore` 已经实现 | 已修：改为指向恢复命令行文档及其 §7 |
| B2 | OPERATIONS §6 没有链接首个签名发布运行手册，也没有说明开发机构建会被拒绝 | 已修：补上链接与说明 |
| B3 | 公开文档里没有本机一次性恢复的逐条命令；本机路径此前只存在于测试代码 | 已修：恢复命令行文档新增 §7，第 1–10 步已在开发机实测 |
| B4 | macOS 上 PG 起不来的两个原因手册没写：`LC_ALL` 未设置；socket 路径超过 103 字节 | 已写入 §7 的前提 |
| B5 | `nexloop-compose-bootstrap` 只能用于全新空库，失败时只输出笼统的错误 | 已写入恢复命令行 §7 第 4 步；代码修正见 B9 |
| B6 | **开放**（`open_scope`）在一次性环境里没有公开路径：它只经工作台的受治理 Action 执行，需要运行中的 API 和负责人的工作台身份 | 已修（负责人 2026-10-18 选 A）：新增 [AT-072 本机恢复演练](at-072-local-restore-drill.md)，以及三个开发机脚本 `scripts/dev/restore_drill.sh`、`restore_drill_serve.py`、`drill_workbench.py`。负责人经工作台开放，operator 被拒；2026-10-18 已按手册实测到 `open` |
| B7 | 首个签名发布运行手册按 `nexloop-release` 与正式密钥编写，与“只在 nexloop-ci 范围内”的边界不一致 | 已在 §3.3 给出替换表；手册本身不改，那是正式发布的流程 |
| B8 | 本机路径中“重放”只覆盖空日志：恢复点之后的合成删除要经工作台，而工作台的 HTTP 入口没有 `erase_consumer` | 已修：工作台 HTTP 入口补上 `erase_consumer` 的参数校验（注册表 0132 已登记为人类操作，owner 专属，授权路径不变；测试 `tests/test_workbench_erase_consumer_pg.py`）。命名点恢复与 PITR 都实测重放了 1 条删除和 1 条联系限制 |
| B9 | `nexloop-compose-bootstrap` 失败时只输出笼统错误（B5） | 另开分支修代码，给出具体的拒绝原因 |
