# AT-072 演练入口单：只按公开文档完成一次“发布 + 恢复”

- **验收项**：AT-072（第二维护者演练）。负责人本人执行，只读本仓库的公开文档。
- **时间**：2026-10-19（周一）17:00 之后。
- **本单的作用**：给出从哪份文档开始、按什么顺序读；说明一次性环境的边界；提供偏差记录模板，以及演练期间的沟通规则。本单不替代任何手册，手册里写不清的地方照实记成偏差。
- **桌面推演**：L5 于 2026-10-18 按本单顺序走了一遍。恢复链路的第 1–9 步在开发机上用一次性 PG 18 实测通过；发布链路只做了文档推演，加上 `render.py --check` 一项实测。发现的断点和处理见 §5。

## 1. 演练期间的规则

1. **调度员和各开发线不做口头补充。** 演练开始后，不回答“这一步什么意思”“参数填什么”这类问题，也不主动提示。
2. 执行人遇到手册不清楚的地方，**按自己的理解先做**，并在偏差表（§4）记一行。如果确实无法继续，就记一条“阻断”，然后停在这一步；调度员不现场救火。
3. 只有三种情况可以中途打断演练：会碰到 stage 或生产的资源；需要真实凭据；或者出现安全问题（例如凭据被打印出来）。这时停止演练，记一条“中止”，事后再处理。
4. 演练结束后，偏差表交给文档线，由文档线修订手册。修订合入之后，AT-072 的证据才能回填；回填由调度员执行。

## 2. 一次性环境

| 部分 | 环境 | 允许 | 不允许 |
|---|---|---|---|
| 恢复 | **Mac 本地临时 PG 18**：一个新建的临时目录，里面放源集群、归档、备份和恢复出来的实例 | 合成数据，即测试 profile 的 `world=test` 租户；本机 socket；用完删除整个目录 | 连接 stage 的 DATA_HOST 或 APP_HOST；使用 pgBackRest 仓库或 restic 仓库的真实口令 |
| 发布 | **CI 主机上 `nexloop-ci` 的可用范围**：它自己的构建工作区（`$CI_BUILD_ROOT`）、rootless docker（containerd 存储，linux/amd64）、syft/grype，以及 NX-033 的 CI 队列 | 在构建工作区里新建一个演练目录，放一次性的发布根和临时签名密钥；演练结束后删除 | 使用 `nexloop-release` 用户、`/srv/nexloop/releases` 或正式发布签名私钥；`deploy.py promote`；任何 `docker compose up`；向 stage 拷贝产物 |
| 部署 dry-run | 同上，`nexloop-ci` 账户 | `deploy.py dry-run` 只渲染、只核对，`executed: false` | 读取真实的 stage.env；连接 stage |

- **不碰 stage**：演练中的任何一步都不连接 APP_HOST 或 DATA_HOST，也不读取它们的私有文件。
- **演练产物不得部署**：演练发布用的是临时密钥，正式的 allowed_signers 验证不过，这是有意的。

### 2.1 演练前由调度员准备（属于环境准备，不属于口头补充）

- 确认 `$SHA`：选 main 上一个已经通过 CI 的提交，也就是 `nexloop_ci.py result` 返回 `passed`，并写进本单的执行记录。
- 确认 `nexloop-ci` 能读到受信仓库 `$TRUSTED_REPO`，并且基础镜像已经按 digest 预载（first-release 运行手册 §2 第 6 步）。
  - 这两项如果需要主机配置变更，要在演练开始之前由负责人完成；演练开始后不再处理。
- Mac 上已有 PostgreSQL 18 与仓库的 `.venv`（`uv sync --frozen`）。

## 3. 文档起点与顺序

### 3.1 发布：构建、签名、验签、部署 dry-run（CI 主机，`nexloop-ci`）

| 顺序 | 文档 | 读什么 | 演练中的做法 |
|---|---|---|---|
| 1 | [OPERATIONS.md](../OPERATIONS.md) §6 | 发布工具的全貌与各文档入口 | 只读 |
| 2 | [NX-034 首个签名发布运行手册](../implementation/NX-034-first-release-runbook.md) §0、§1 | 参数表与方案 A | 按 §3.3 的替换表理解参数 |
| 3 | 同上 §2 第 1–9 步 | 核对 CI、干净检出、离线依赖、平台、基础镜像、`build`、`sbom`、`preflight --category stage` | `--release-root` 用演练发布根 |
| 4 | 同上 §2 第 12–16 步 | 独立核对、签名、封存、`archive.py check`（验签） | 由 `nexloop-ci` 执行，密钥与 allowed_signers 用临时的；第 10、11 步的移交与拷贝不做 |
| 5 | 同上 §4 | dry-run 所需的私有文件与命令 | 只做第 1、2、4 步；第 5 步 `load-check` 可选 |
| 6 | 同上 §2 第 17 步 | 清理 | 另外删除演练目录 |

背景资料（遇到问题时查阅）：[NX-034 第二部分实现说明](../implementation/NX-034-part2-implementation.md)、[NX-034 本地演练](../implementation/NX-034-local-dry-run.md)（其中有临时测试密钥的生成方式）。

### 3.2 恢复：备份、PITR 恢复、重放、校验、开放（Mac）

| 顺序 | 文档 | 读什么 | 演练中的做法 |
|---|---|---|---|
| 1 | [OPERATIONS.md](../OPERATIONS.md) §5 | 恢复状态机 `restored → replayed → verified_readonly → open` | 只读 |
| 2 | [NX-035 恢复命令行](../implementation/NX-035-restore-cli.md) §1–§3 | 恢复做什么、开放前核对、参数与凭据 | 只读 |
| 3 | 同上 **§7** | 本机一次性环境的逐条命令（第 1–10 步） | 照做。第 9 步做两次：一次 `--target-name`，一次 `--target-time`（PITR），各用新的 `--pg-path` 与端口 |
| 4 | [NX-035 本地部分实现说明](../implementation/NX-035-local-implementation.md) “开放”一节 | `open_scope` 的条件 | 见 §5 断点 B6 |

背景资料：[NX-035 部署运行手册](../implementation/NX-035-deploy-runbook.md) §4，这是 stage 上的真实演练，本次**不执行**，只用来对照。

### 3.3 发布手册参数在演练中的替换

| 手册参数 | 演练取值 |
|---|---|
| `$RELEASE_ROOT` | `$CI_BUILD_ROOT/at072/releases-root`，由 `nexloop-ci` 新建，0700 |
| `$HANDOFF_ROOT` | 不用。`build` 直接写入演练发布根 |
| `$RELEASE_SIGNING_KEY` | 在演练目录内新生成的 ed25519 临时密钥：`ssh-keygen -t ed25519 -N '' -C at072-drill -f <演练目录>/drill-key` |
| `$ALLOWED_SIGNERS` | 由临时公钥生成的一行：`nexloop-release namespaces="nexloop-release" <公钥类型> <公钥>` |
| `$PRIVATE_STAGE_ENV` | `install -m 600 deploy/stage/stage.env.example <演练目录>/stage.env`，即示例中的文档值（2026-10-18 实测 `render.py --check` 结果为 `valid: true`） |
| `$RELEASE_COMMIT` | 演练发布的 `manifest.json` 中的 `commit` |

## 4. 偏差记录模板

每发现一处就记一行，当场记。“实际怎么做的”要写到能复现的程度，包括命令和输出的关键字段；不要写口令、地址或主机名。

| # | 时间 | 文档与位置（文件 §/步号） | 手册哪里不清楚或不对 | 实际怎么做的 | 结果（成功 / 绕过 / 阻断 / 中止） | 建议怎么改 |
|---|---|---|---|---|---|---|
| 1 | | | | | | |

另外记录：

- 演练开始与结束时间；
- 发布部分用到的 `$SHA` 与 `$RID`；
- 恢复的 RTO，即从第 9 步开始到出现 `verified_readonly`；
- 每份 JSON 输出的关键字段：`ok`、`passed`、`state`、`ready`、`refused`。

## 5. 桌面推演发现的断点（2026-10-18，L5）

| # | 断点 | 处理 |
|---|---|---|
| B1 | OPERATIONS §5 仍写着“命令行恢复工具留到部署切片”，但 `nexloop-restore` 已经实现 | 已修：改为指向恢复命令行文档及其 §7 |
| B2 | OPERATIONS §6 没有链接首个签名发布运行手册，也没有说明开发机构建会被拒绝 | 已修：补上链接与说明 |
| B3 | 公开文档里没有本机一次性恢复的逐条命令；本机路径此前只存在于测试代码 | 已修：恢复命令行文档新增 §7，第 1–10 步已在开发机实测 |
| B4 | macOS 上 PG 起不来的两个原因手册没写：`LC_ALL` 未设置；socket 路径超过 103 字节 | 已写入 §7 的前提 |
| B5 | `nexloop-compose-bootstrap` 只能用于全新空库，失败时只输出笼统的错误 | 已写入 §7 第 4 步；改进报错属于代码改动，列为后续 |
| B6 | **开放**（`open_scope`）在一次性环境里没有公开路径：它只经工作台的受治理 Action 执行，需要运行中的 API 和负责人的工作台身份，而测试 profile 两样都不提供 | **未解决**。演练中恢复部分预计停在 `verified_readonly`，开放一项会记为“阻断”。需要调度员决定：补一份“一次性环境起 API 并以负责人身份开放”的手册，或者接受由 stage 演练的开放证据替代 |
| B7 | 首个签名发布运行手册按 `nexloop-release` 与正式密钥编写，与“只在 nexloop-ci 范围内”的边界不一致 | 已在 §3.3 给出替换表；手册本身不改，那是正式发布的流程 |
| B8 | 本机路径中“重放”只覆盖空日志：§7 的命令无法在恢复点之后造一次合成删除，删除要经工作台；所以 AT-059 意义上的重放不会真正发生 | 未解决，与 B6 同源；演练中照实记录 |
