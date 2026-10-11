# NX-033 本地部分：实现说明

依据：`NX-033-design.md`（D1–D5 已定，§11）。

- 分支 `nx033-local`，从 main `aefe1fb` 切出。
- 没有迁移。

**范围**：只做仓库内的代码、配置模板和测试。以下 B 类步骤都没有做：
- 在主机上建用户、建 bare 仓库、安装 hook；
- 改防火墙；
- 切换 remote。

现有的私有 CI 车道和 `scripts/ci/check` 都没有改动。

**AT-053 和 AT-054 现在只有本地证据。** 首次在真实 APP_HOST 上做隔离探测和断连演练，由负责人执行（§8）。

## 1. 组成

| 文件 | 内容 |
|---|---|
| `scripts/ci/nexloop_ci.py`（只用标准库） | ref 规则（pre-receive）、入队（post-receive 和 `enqueue`）、控制器 `run-once`（Admission、全新检出、阶段执行、受信判定、结果文件）、合并、`ack-policy`、`sync`、`status`、`result`、`check-runner`、`isolation-probe` |
| `.ci-policy/stages.v1.json` | 受信阶段：`dependency-python`、`dependency-node`（只有这两个阶段经允许名单代理联网），`suite`（整体运行 `scripts/ci/check`，环境为 `UV_OFFLINE=1`、`npm_config_offline=true`），`isolation-probe`（使用受信副本） |
| `.ci-policy/required-suites.v1.json` | 必需的 junit：正好是 `scripts/ci/check` 写出的 8 份（有测试比对）。NX-049 时限门槛对应 `python-deadline-gate` |
| `.ci-policy/egress.v1.json` | 依赖源的允许名单（不含 GitHub） |
| `deploy/stage/ci/` | hook、`authorized_keys`、身份、运行器、事实、探测目标、systemd 的示例，全部参数化 |
| `Makefile` | §7 命令契约：`make ci SHA= REF= IDENTITY=`、`make status`、`make test` / `test-integration` / `test-recovery` / `test-e2e` / `lint`。开发机默认 `N=3` |

## 2. 规则（与设计稿的对应）

### ref 命名空间（§2.1，由 pre-receive 强制）
- `incoming/<身份>/<topic>`：只能写自己身份前缀下的 ref，可以非快进，可以删除。
- `integration/*`：只有 integrator 和 maintainer 能写，不能删除。
- `main`：只有 controller 能写，只能快进。
- `tags/v*`：只有 release 身份能建，建后不可移动、不可删除。
- `review/*`：只有 maintainer 能写，**不入队**。
- 其他 ref 一律拒绝，包括 `refs/pull/*`。

### 队列（§2.2）
- post-receive 只为 `incoming/*` 和 `integration/*` 写入队文件，内容包括 ref、**当时解析出的 SHA**、身份、时间和类型；先写临时名，再原子 rename。
- ref 之后再移动，不影响已入队的 SHA。
- 同一 SHA 已有通过的结果时直接复用，不重建。
- 控制器用 flock 保证同一时间只跑 1 个构建。

### Admission 与检出
- Admission 检查两项：身份在允许名单中，SHA 存在。
- 候选与 main 的差异涉及以下路径时，打上 `policy_change` 标记：`scripts/ci/`、`.ci-policy/`、`deploy/`、`.githooks/`。
- 检出方式是 `clone --no-checkout` 再 `checkout --detach <sha>`，在全新目录里进行，用完删除。
- git 的环境变量是 `GIT_ALLOW_PROTOCOL=file`，所以检出和构建只能用本地仓库。

### 受信判定（§4.3）
- 判定依据是受信 policy，候选自己改了 `scripts/ci/**` 或 `.ci-policy/` 也不影响判定。
- 每个必需的 junit 必须存在，失败、错误和跳过都为 0。
- 测试数不得低于上一次 main 结果减去允许下降量；允许下降量默认 0，删除测试要在 policy 中登记。
- 不自动重跑。重跑写成 `attempt-<n>`，结果中保留第一次失败的记录。

### 结果文件（§4.4）
- 位置：`<QUEUE>/results/<sha>/attempt-<n>/result.json`，只追加。
- 内容：
  - SHA、父提交、ref、身份；
  - 控制器版本（自身文件的 sha256）、policy 版本与摘要；
  - 锁文件 hash、迁移 catalog 的 head 与摘要；
  - 各阶段的状态、退出码、耗时，以及 junit 的 sha256 与计数；
  - 总计数 `counts`、`deadline_gate`、`isolation`；
  - 开始和结束时间、主机负载。
- **与 NX-034 的契约**：`sha`、`status`、`policy_version`、`deadline_gate`、`counts` 正好是 `release.py manifest` 的 `ci_result` 输入。有契约测试：同一提交可以生成发布清单，别的提交会被拒绝。

### 合并（§2.3，D4）
满足以下全部条件，才把 main 快进到该 SHA（`update-ref` 带旧值比较），并写合并记录：
- 请求者是 integrator 或 maintainer；
- 该 SHA 有通过的结果；
- 第一父提交等于当前 main；
- 如果有 `policy_change`，已有 maintainer 确认；
- 合并门槛命令（部署时为 prepush-scan）通过。

### 隔离（§3，AT-054）
- **`check-runner`** 在启动时检查运行器。以下任一情况都会被拒绝，退出码 2，只输出代码：
  - 以 root 运行，或属于 docker/root 组；
  - 不是 rootless Podman；特权容器、添加 capability、设备；
  - 测试网络不是 `none`，或依赖阶段不走代理；代理指向数据主机；
  - 挂载了受保护目录（解析符号链接后判断）或容器 socket；可写挂载超出允许范围；
  - 环境变量名像 secret，或变量值指向受保护目录或数据主机；
  - 发布了端口，或通过 add_hosts 加入了数据主机；
  - 缺少 cgroup 限制。
- 控制器带 `--runner` 运行时会先执行这项检查。
- **`podman_argv`**：测试阶段是 `--network=none`；容器根目录只读；`--cap-drop=ALL`；不发布端口；有内存和 pids 上限；只有依赖阶段注入代理。
- **`isolation-probe`**：只要有任何受保护路径可读，或任何目标端口可连，就判失败。结果只报目标代号。

### GitHub 断连（§6，AT-053）
- `sync` 由 `nexloop-git` 运行，只快进推送 main，不 force、不 mirror。
- GitHub 不可达时记为 `sync_pending`，并累计积压（尚未同步的提交数）。这期间入队、构建、判定和合并都照常进行。
- GitHub 恢复后推送一次，积压清零。

## 3. 测试（临时 bare 仓库、临时队列，合成身份）

| 文件 | 内容 |
|---|---|
| `tests/test_ci_queue.py`（8 个） | 真实 hook 下的 ref 规则：别人的 incoming、开发线推 main 或 integration、`refs/pull/*`、未知身份、开发线打 tag，全部被拒；`review/*` 被接受但不入队；integration 不能删除；tag 不可移动；main 只能快进。入队后 ref 移动不影响构建的 SHA；全新检出；同一 SHA 复用。候选改写自己的 CI 和 policy 并删掉测试，仍被受信 policy 判失败，并标记 `policy_change`；有失败或跳过时判失败；重跑会保留第一次失败。合并：开发线不能请求；第一父提交不是 main 时拒绝；`policy_change` 未经 maintainer 确认时拒绝，确认后合并；合并门槛失败时拒绝。依赖缓存未命中判 `dependency_unavailable`，不会被当作跳过而通过。与 release.py 的契约。**AT-053**：同步 remote 不可达时 `sync_pending`，积压从 1 增到 2，这期间 CI 和合并照常；恢复后补推，积压清零。CLI 的 status 和 result |
| `tests/test_ci_isolation.py`（6 个） | 模板通过检查；15 种注入的暴露各自被拒，包括经符号链接进入受保护目录；以 root 运行或属于 docker 组时被拒。CLI 检查和控制器都拒绝启动，输出不含路径。容器命令：测试无网络，依赖只经代理。**AT-054**：探测到可读的 secret 目录或在监听的端口时判失败，只报代号，不可达时判通过。policy 与 `check` 的 junit 一致，测试阶段离线，探测使用受信副本，允许名单中没有 GitHub。模板中没有 IP 地址或家目录路径 |

## 4. 未完成项

### B 类主机步骤（每一步都要负责人确认，§8）
- 建用户 `nexloop-git` 和 `nexloop-ci`、bare 仓库、hook、`authorized_keys`、控制器和同步的 systemd unit。
- 防火墙规则：按 UID 或 cgroup 拒绝 CI 到 DATA_HOST、业务 loopback 端口和 LAN 网段的连接。
- 依赖代理和本地缓存的预热；基础镜像以 OCI archive 预载。
- remote 切换：`origin` 指向 LAN，`github` 作为同步 remote；pre-push hook 的 remote 名检查要相应调整。
- 在真实 APP_HOST 上首次做隔离探测和断连演练（AT-053、AT-054 的真实证据），并与现行测试机的一次完整 CI 结果对照。

### 依赖 NX-032
- 防火墙和 TLS 的主机部分（NX-032 §3）。
- CI 镜像（`<CI_IMAGE>`）的构建与固定。
- 控制器、同步和部署的身份分离在主机上落地。

### 本仓库内还没做
- 把 `scripts/ci/check` 拆成 §4.1 的各个阶段。为了不改动现有车道，本次把它作为单一的 `suite` 阶段运行，时限门槛结论来自它的 `python-deadline-gate` junit。拆分时会出新版本的 `stages.v1.json`。
- 合并门槛的 prepush-scan 改为从私有模式文件读取（§5.3），这部分已属于现行做法，本次没有改动扫描脚本。
- `make ci` 需要已运行的控制器；开发机上没有队列时，可以用 `run-once` 手动驱动。
