# NX-033 本地 Git 队列与隔离 CI：设计稿

状态：**设计稿，D1–D5 已定（§11）；未实现，没有执行任何主机操作**。分支 `nx033-034-design`，从 main `7ea25b8` 切出。NX-033 依赖 NX-010 和 NX-032。NX-032 的设计稿已经全部定案，在分支 `nx032-design` `b117d53`，尚未进入 main；本稿引用它的章节号。

**公开仓库约束**：不写真实主机、地址、用户、路径或凭据，全部用参数表示（§9）。现有私有 CI 车道的细节不写进本稿。

## 0. 依据

- **规划**：NX-033「建立本地 Git 队列与隔离 CI」，S5，模块 M22、M23。交付物是“rootless/secret/内网隔离、受信 ref 与不可变 SHA”。
- **验收**：
  - AT-053 GitHub 断连：阻断 GitHub 访问时，本地 CI 和用已有依赖构建仍然可用，同步标为 pending；
  - AT-054 CI 隔离：CI 尝试访问 stage secret 和数据端口时失败；公开 PR 不自动执行。
- **规范** `12_LOCAL_CI_AND_RELEASE.md`：
  - §1：本地 Git 加本地 CI 控制器是主路径，GitHub 不是必需的控制面。初版采用 APP_HOST 上受控的 bare 仓库，加 systemd 调度的 CI 队列；流水线放在 `scripts/ci/`；
  - §2：`nexloop-git`、`nexloop-ci`、`nexloop-deploy` 三个 OS 用户分离，CI 读不到真实 secret 和备份密钥。post-receive 只校验受信 ref 和 SHA 并写入队列，不同步构建；未经评审的外部代码不在本机执行；
  - §3：流水线各阶段（Admission … Sync），依赖获取与测试分开，测试阶段不接触 DATA_HOST；
  - §4：权威构建结果的内容；不靠自动重跑隐藏失败；
  - §9：同一时间默认只跑 1 个 CI build，运行服务的资源优先。
- **其他文档**：
  - `11_DEPLOYMENT.md` §2：CI 和 controller 在 APP_HOST；DATA_HOST 不执行外部 PR 构建；
  - `13_SECURITY_PRIVACY_AND_OPEN_SOURCE.md` §9：CI 不得接触服务 secret 和 DATA_HOST 网络；
  - AGENTS.md 的 Deployment 段：本地 CI 是权威；推送前跑三项扫描门槛；只推 main（加 tag），不 force，不 mirror；跨任务改动走分支加 PR。
- **NX-032 设计**：
  - §3：防火墙中不放行 CI 用户或网络到 DATA_HOST；
  - §5.1：最小发布清单由 CI 结果生成；
  - §8：服务器上的操作属于 B 类，由负责人本人确认；
  - §9：本任务负责 rootless、Git 队列、受信 ref 和外部 PR 隔离。

## 1. 现状核对（只读，main 7ea25b8）

| 事实 | 位置 | 对本设计的影响 |
|---|---|---|
| 唯一的流水线入口是 `scripts/ci/check`，顺序如下：<br>① 依赖阶段（`uv sync --frozen`、预热 wheel 构建、`pnpm install --frozen-lockfile`，**需要网络**）；<br>② 静态门槛（publication、Pi 源码、definer search_path、契约生成、handoff 校验）；<br>③ 前端、Pi、Host 构建与 Node / vitest 测试；<br>④ Phase 1 串行（会改写共享构建产物的测试）；<br>⑤ Phase 1b 串行 NX-049 时限门槛（guard 4 进程）；<br>⑥ Phase 2 并行 pytest（`-n 6`）；<br>⑦ Pi conformance 与 runtime vitest。<br>每个阶段都输出 junit，由 `check_test_report.py` 判定；时限门槛失败在最后统一报告 | `scripts/ci/check` | 作为“阶段内容”保留；本任务在它外面加 Admission、隔离、离线依赖、机器可读结果和队列，不重写测试本身 |
| 没有机器可读的总结果（JSON）、SBOM、镜像构建或发布清单 | — | 结果文件见 §4.4；镜像和清单属于 NX-034 |
| 12 §8 列出的 `make ci`、`make build-release` 等目标命令都不存在 | — | §7 给出命令契约 |
| `scripts/ci/prepush-scan.sh` 有三项门槛：<br>① 跟踪的私有或运行时路径；<br>② 外发历史中每个提交的受限标识；<br>③ 新增行中的凭据值。<br>以 `--hook` 方式安装为 pre-push：只允许名为 `origin` 的 remote，拒绝删除 ref，拒绝 main 非快进，拒绝带 `local-only` 的 ref；有合成数据的自测 `test-prepush-scan.py` | `scripts/ci/` | 保留为同步门槛（NX-034 §3）。受限标识的模式清单改为从私有文件读取（§5.3） |
| 现在只有一个 remote，`origin` 指向公开的 GitHub。12 §7 设想的是 `origin` 为受控 LAN 仓库、`github` 为同步 remote，**尚未建立** | `git remote` | §2 建立 LAN bare 仓库，GitHub 降为同步 remote；remote 改名的切换见 §8 |
| 现行的协作做法：开发线在各自 worktree 的分支上提交，不 push、不合并；调度员组装 `dispatch/integration-*` 集成分支，在测试机上跑全量 CI，然后合入 main，扫描后推 GitHub。集成清单记在 `docs/implementation/*-integration-manifest.json` | LINE-RULES、AGENTS.md | §2 把这一做法固化成本地 Git 的 ref 权限和队列：**角色不变，只把约定变成机器强制** |
| 基础镜像按 digest 固定在 `versions.lock.json`；社区镜像目前用 `scripts/community_test.py` 手工构建 | `versions.lock.json`、`deploy/community/` | 构建阶段改由 CI 产出（NX-034） |

## 2. 本地 Git 与队列

### 2.1 仓库与 ref 命名空间

- **仓库**：APP_HOST 上的 bare 仓库 `<GIT_ROOT>/nexloop.git`，属主是 `nexloop-git`。
- **访问方式**：通过受限 SSH：`git-shell` 加 `authorized_keys` 的 forced command，每把公钥对应一个**推送身份**。
- **ref 命名空间与写入权限**（由 pre-receive hook 强制）：

| ref | 谁能写 | 规则 |
|---|---|---|
| `refs/heads/incoming/<identity>/<topic>` | 对应的推送身份（开发线、维护者） | 只能写自己身份前缀下的 ref；可以非快进；不触发合并 |
| `refs/heads/integration/<name>` | 集成者身份（调度员或维护者） | 用于组装集成分支，相当于现在的 `dispatch/integration-*` |
| `refs/heads/main` | **只有合并控制器**（本机进程，`nexloop-git` 身份） | 只能快进；任何推送身份直接推 main 都会被拒绝 |
| `refs/tags/v*` | 只有发布工具，并且须经负责人确认（NX-034 §4，B 类） | 不可移动，不可删除 |
| `refs/heads/review/*` | 维护者 | 外部贡献的评审副本（§5.2）；**永不进入 CI 队列** |
| 其他 | 无 | 拒绝；删除 ref 一律拒绝，`incoming/` 和 `review/` 下自己的分支除外 |

### 2.2 队列

- **post-receive** 只做三件事，不构建，也不执行候选代码中的任何脚本：
  1. 校验 ref 属于可入队的命名空间（`incoming/*`、`integration/*`）；
  2. 解析出不可变的 SHA；
  3. 写一个入队文件 `<QUEUE_DIR>/pending/<seq>-<sha>.json`。内容为 ref、SHA、推送身份、时间和请求类型（`check` 或 `merge`），先写入临时名再原子 rename。
- **为什么用目录作队列**：CI 队列不是业务状态，并且 CI 连不到 DATA_HOST（AT-054），所以不放在业务 PostgreSQL 中。按 SHA 去重：同一 SHA 已有结果时，不重复构建，直接复用结果。
- **控制器** `nexloop-ci-controller`：
  - 由 systemd 运行，用户是 `nexloop-ci`，程序来自**受信安装路径**，即 main 上已发布的版本，不是候选代码；
  - 用 flock 保证同一时间只跑 1 个构建（12 §9）；
  - 按顺序取出 pending，经过 Admission 后交给沙箱执行，结果写入 `<CI_RESULTS>/<sha>/`。
- **Admission**（12 §3）：
  - SHA 存在且可以从入队 ref 到达；
  - 推送身份在允许名单中；
  - 资源锁可用；
  - 候选与 main 的差异涉及 `scripts/ci/**`、`.ci-policy/**`、`deploy/**` 或 hook 时，打上 `policy_change` 标记（§4.3）。

### 2.3 谁提交、谁合并

- **开发线和维护者**：提交并推送到自己的 `incoming/<identity>/*`，然后入队 `check`。
- **集成者**（调度员）：在 `integration/<name>` 上组装、重排迁移号，入队 `check`；全量通过后入队 `merge`。
- **合并控制器**执行 `merge` 之前要求：
  1. 这个 SHA 有一份 `passed` 的 CI 结果，包括 NX-049 时限门槛；
  2. 这个 SHA 的第一父提交**等于当前的 main**。否则拒绝，集成者需要在新的 main 上重新组装并再跑一次 CI，因为不能拿基于旧 main 的结果去合并；
  3. `policy_change` 标记已有维护者确认（§4.3）；
  4. 符合 publication 和 prepush 扫描（`HEAD` 对 `main`）。
- **合并方式**：满足条件后把 main **快进**到这个 SHA，写合并记录（SHA、CI 结果摘要、集成者），然后触发同步（NX-034 §3）。
- 负责人和维护者具有集成者权限；开发线没有。

## 3. CI 隔离（AT-054）

### 3.1 身份与文件

- **用户隔离**：`nexloop-ci` 是独立的非 root 用户，不属于 `nexloop-deploy` 或任何运行服务的用户组，也不在 docker 组；容器用 rootless 运行时（D2）。
- **路径隔离**：
  - stage secret 目录（`<SECRET_ROOT>`）、发布归档的写入、运行数据（`<RUNTIME_ROOT>`、`<ARTIFACT_ROOT>`）、备份目录和备份密钥，对 `nexloop-ci` 都是 0700 或 0750 且不可读；
  - 沙箱内也**不挂载**这些路径。
- **凭据**：CI 没有任何 GitHub 推送凭据（同步属于 `nexloop-git`，见 NX-034），也没有部署凭据。

### 3.2 网络

- **测试阶段**：沙箱有独立的网络命名空间，只有 loopback。临时 PostgreSQL、Valkey、Host 和 Pi 都在沙箱内运行。
- **依赖阶段**：只能经允许名单代理访问依赖源（PyPI、npm registry、基础镜像所在的 registry）。允许名单是版本化配置 `.ci-policy/egress.v1.json`，并且优先使用本地缓存（§6）。
- **主机防火墙**：由 NX-032 §3 落实，再加一条按 UID 或 cgroup 匹配 `nexloop-ci` 的规则：
  - 拒绝到 DATA_HOST 任何端口；
  - 拒绝到 APP_HOST 上业务服务的 loopback 端口（Gateway、API、Agent Host、guard），通过 `meta skuid` 或 cgroup 匹配实现；
  - 拒绝到 LAN 网段。
- **资源**：沙箱有 cgroup 限制（CPU 权重低于运行服务、内存上限、pids 上限），保证运行服务资源优先（12 §9）。CI 对同机生产服务的实际影响由 NX-031 测量。

### 3.3 负向验证（AT-054 的证据）

在 CI 沙箱内，由流水线中固定的 `isolation-probe` 阶段执行，任何一项能访问就判失败：

- 读取 `<SECRET_ROOT>` 和备份密钥的路径：不存在或权限被拒；
- 连接 DATA_HOST 的 PG 端口和 Valkey 端口：连接被拒或超时，在 TCP 层就被拦下，不需要认证；
- 连接 APP_HOST 上 Gateway、API 和 Agent Host 的 loopback 端口：被拒；
- 连接 LAN 中的其他地址：被拒；
- 测试阶段访问 GitHub 或公网：被拒。

探测只输出“目标代号”（参数名）和结果，不输出地址。第一次在真实 APP_HOST 上的探测属于 B 类，由负责人执行，或在负责人逐步授权下执行（NX-032 §8）。

## 4. 流水线与结果

### 4.1 阶段

沿用 12 §3。`scripts/ci/check` 拆成可单独调用的阶段脚本，内容不变，只是加了阶段边界：

`admission` → `checkout` → `dependency` → `static` → `unit-contract` → `build-artifacts` → `serial-build` → `deadline-gate` → `integration` → `pi-runtime` → `isolation-probe` → `package` → 结果判定

- **checkout**：把指定 SHA 检出到全新目录（`git clone --no-checkout` 后 `git checkout <sha>`），不在可变的工作目录里构建。
- **package** 阶段的镜像构建和清单生成属于 NX-034。

### 4.2 不隐藏失败

- 每个阶段的 junit 原样保存。
- 自动重跑默认关闭。确实需要重跑时（例如 NX-049 B 类基线），结果中记录首次失败和每一次尝试。
- 关键的授权、幂等、迁移和时限门槛测试，不能以“重跑后通过”判为通过（12 §4）。

### 4.3 候选代码改动 CI 本身

候选 SHA 可以修改 `scripts/ci/**` 或测试本身，让它们“通过”。因此：

- **判定由受信的控制器做，不由候选自己的脚本做**。控制器按受信版本的 `.ci-policy/required-suites.v1.json` 检查：
  - 每个必需的 junit 是否存在；
  - 失败数是否为 0；
  - 测试计数是否低于上一次 main 结果减去允许的下降量（默认 0；删除测试必须在 policy 中显式登记）。
- 差异涉及 CI 本身、hook、policy 或部署文件时，打上 `policy_change`，合并前必须有维护者显式确认。确认记录写进合并记录。

### 4.4 结果文件 `<CI_RESULTS>/<sha>/result.json`

- **内容**：
  - SHA、父提交、入队 ref 和推送身份；
  - 受信控制器的版本、policy 版本；
  - `versions.lock.json` 的 digest、`uv.lock` 和 `pnpm-lock.yaml` 的 hash；
  - 迁移 catalog 的最高版本和 digest；
  - 各阶段的状态、耗时、junit digest 与原始计数（通过、失败、跳过、错误）；
  - 时限门槛结论；隔离探测结论；首次失败和重跑记录；
  - 开始和结束时间、主机负载快照。
- 以上内容是 12 §4 要求的“权威构建结果”的 CI 部分；SBOM、镜像 digest 和签名由 NX-034 追加。
- 输出到 stdout 的是同一份 JSON（12 §8 要求机器可读）；退出码为 0 表示通过，1 表示失败，2 表示配置或环境错误。
- 结果目录只追加；同一个 SHA 的重跑写成 `attempt-<n>/`，不覆盖。

## 5. 外部贡献与同步分支

### 5.1 原则

- GitHub 上的 PR、fork 和同步分支，**永远不会自动在 LAN 主机上执行**：GitHub 侧没有 self-hosted runner，没有 webhook 触发本地构建，本地也不轮询 PR。
- `review/*` 命名空间不入队。

### 5.2 流程

1. 维护者手动把 PR 的 head 取到 `review/github-pr-<n>`，只读审查 diff，包括 CI 脚本、构建文件和依赖变更。
2. 审查通过后，维护者把改动以自己的身份重新提交到 `incoming/<maintainer>/*`。这会产生新的 SHA，并保留原作者署名（`Co-authored-by` 或 DCO）。之后走正常的受信 CI。
3. 需要在审查前**运行**未审代码时（D3），只能在一次性 VM `<UNTRUSTED_VM>` 中进行：无 secret、无 LAN 路由、只能访问依赖源，用完即毁。不能用普通容器代替 VM（12 §2）。

### 5.3 扫描门槛的私有模式

`prepush-scan.sh` 的受限标识（私有主机名、地址、密钥指纹）改为从私有文件 `<PRIVATE_SCAN_PATTERNS>`（不入库，0600）读取。公开脚本只保留机制和合成测试样例。私有文件缺失时，同步门槛判失败，不放行。

## 6. GitHub 断连（AT-053）

- **本地 CI 不引用 GitHub**：
  - 入队、检出和构建都只用本地 bare 仓库；
  - 依赖从本地缓存取：uv 缓存加离线 hashed 安装，pnpm store 用 `--offline`，基础镜像按 digest 预载为 OCI archive 或放在本地 registry；
  - 依赖缓存由 `nexloop-ci` 在依赖阶段维护；缓存未命中又无法访问外网时，判 `dependency_unavailable`，失败要明确，不能降级为跳过测试。
- **同步积压**：同步由 NX-034 的同步任务负责。GitHub 不可达时标为 `sync_pending` 并累计积压，不影响 CI 结果和合并（12 §3 的 Sync 行）。
- **测试**：在沙箱和同步任务中阻断 GitHub 的出站，CI 在缓存已热的情况下全量通过；同步任务报告 `sync_pending`，积压计数加 1；恢复后补推，积压清零。

## 7. 命令契约（12 §8 中属于本任务的部分）

| 命令 | 作用 |
|---|---|
| `make ci SHA=<sha>` | 把这个 SHA 入队，等待结果并打印 `result.json` |
| `make test` / `make test-integration` / `make test-recovery` / `make test-e2e` / `make lint` | 单独跑对应阶段（开发机本地可用，不需要队列） |
| `nexloop-ci status` | 队列和最近结果，只有 SHA、状态和计数 |

破坏性命令（清理缓存、清理结果）默认 dry-run，要求给出精确路径。

## 8. 迁移到新形态的步骤（B 类，逐步由负责人确认）

1. 建用户 `nexloop-git` 和 `nexloop-ci`、bare 仓库、hook、控制器的 systemd unit（参数化示例放在 `deploy/stage/ci/`）。
2. 把现有 main 推入本地 bare 仓库。
3. 本地 remote 改名：`origin` 指向 LAN bare 仓库，`github` 指向 GitHub。pre-push hook 的 remote 名检查同步调整：开发机上推 `origin` 指的是 LAN 仓库，GitHub 只由同步任务推送。
4. 开发线和调度员的推送身份发放公钥。
5. 隔离探测（§3.3）。
6. 一次完整 CI 与现行测试机结果对照。
7. 切换：此后 main 只由合并控制器推进。

## 9. 参数

`<APP_HOST>`、`<GIT_ROOT>`、`<QUEUE_DIR>`、`<CI_RESULTS>`、`<CI_CACHE>`、`<SECRET_ROOT>`、`<RUNTIME_ROOT>`、`<ARTIFACT_ROOT>`、`<UNTRUSTED_VM>`、`<PRIVATE_SCAN_PATTERNS>`、`<DEPENDENCY_PROXY>`，以及推送身份的公钥文件。全部来自私有 env 文件。

## 10. 边界与 AT

| 事项 | NX-033 | 其他 |
|---|---|---|
| DATA_HOST 防火墙、TLS | 只要求“不放行 CI” | NX-032 |
| 镜像构建、SBOM、清单、签名、归档、GitHub 同步、tag | 只产出 CI 结果 | NX-034 |
| 部署门槛、回滚 | — | NX-032（部署侧），NX-034（归档清单） |
| 备份 | — | NX-035 |
| CI 对同机的影响 | 资源限制 | NX-031 测量 |

| AT | 覆盖方式 |
|---|---|
| AT-054 | §3.3 的隔离探测（沙箱内，作为流水线阶段）；在真实 APP_HOST 上的首次探测由负责人执行并留证据；外部 PR 不入队：`review/*` 推送后队列为空；非控制器身份推 main 被拒 |
| AT-053 | §6：阻断 GitHub 后 CI 通过，同步为 pending，恢复后补推 |
| 其他 | 不可变 SHA（入队后 ref 移动不影响已入队的 SHA）；第一父提交不等于 main 时拒绝合并；`policy_change` 未确认时拒绝合并；测试计数下降被拒；同一 SHA 去重；控制器来自受信路径（候选改了 `scripts/ci` 也不影响判定） |

## 11. 负责人决定的事项（D 项）

裁定状态：**全部已定。** D3、D4、D5 由负责人 2026-10-11 决定，原话“全部按推荐”；D1、D2 由调度员 2026-10-11 裁定，采用推荐。实现属于 S5，在 NX-032 之后分派；B 类步骤（建用户、建仓库、改防火墙、切换 remote）一律等负责人本人确认。

- **D1 Git 服务形态。**（**调度员裁定 2026-10-11：采用推荐。**）
  - **推荐**：bare 仓库加 `git-shell`、hook 和 systemd 控制器（12 §1 的初版选择），依赖最少，ref 权限由 pre-receive 强制。
  - **替代**：自建 Forgejo 和 Woodpecker（有网页、PR 评审和权限界面，但多一套要维护和加固的服务）。
- **D2 沙箱技术。**（**调度员裁定 2026-10-11：采用推荐。**）
  - **推荐**：受信代码用 rootless Podman，每个 job 一个容器，网络命名空间只有 loopback，加 cgroup 限制；未审代码（如需运行）只用一次性 VM。
  - **替代**：受信代码也用 VM（隔离更强，但启动和缓存成本高，全量 CI 会明显变慢）。
- **D3 未审外部 PR 是否运行。**（**负责人 2026-10-11 决定：“全部按推荐”。**）
  - **推荐**：v0.1 **不在任何 LAN 主机上运行未审代码**，只做 diff 审查；审查通过后以维护者身份重新提交，走受信 CI。
  - **替代**：在 `<UNTRUSTED_VM>` 中运行（要准备并维护一台隔离 VM 和它的网络规则）。
- **D4 谁能推进 main。**（**负责人 2026-10-11 决定：“全部按推荐”。**）
  - **推荐**：只有合并控制器能推进 main。它要求 SHA 有通过的 CI 结果，第一父提交等于当前 main；集成者（调度员、维护者、负责人）发起合并，开发线不能。
  - **替代**：维护者手动快进 main，CI 结果只作参考（与现状接近，但“必须 CI 通过”不再由机器强制）。
- **D5 CI 与生产同机时的资源策略。**（**负责人 2026-10-11 决定：“全部按推荐”。**）
  - **推荐**：CI 在 APP_HOST 上运行，CPU 权重低于运行服务，内存设上限，同一时间 1 个构建；NX-031 测出明显影响时，生产高峰暂停排队。
  - **替代**：CI 放到第三台机器（隔离更好，但不在 11 的两机方案内，需要新增硬件）。

本稿的实现属于 B 类部分（§8，建用户、建仓库、改防火墙、切换 remote），每一步都要负责人本人确认。
