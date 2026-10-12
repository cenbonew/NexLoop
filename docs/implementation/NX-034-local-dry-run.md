# NX-034 发布流水线本地演练（开发机，首次 CI 主机真实构建之前）

- **分支与环境**：分支 `release-dry-run`，从 main `25312f7` 切出。演练在开发机上进行：Docker 28.1.1，containerd 镜像存储（`io.containerd.snapshotter.v1`）；Node 24。
- **路径占位**：`<SCRATCH>` 是临时目录；`<RELEASE_ROOT>` 为 `<SCRATCH>/root`。
- **签名**：使用演练时在 `<SCRATCH>` 内临时生成的 ed25519 测试密钥，以及对应的临时 `allowed_signers`（身份与命名空间 `nexloop-release`）。没有使用、也没有读取 `deploy/release` 正式公钥对应的私钥。
- **CI 结果文件**：`make build-release` 要求本地 CI 队列中有该提交的通过结果，开发机上没有。演练直接调用 `release.py build`，传入 `<SCRATCH>/ci-result.json`。该文件标注 `policy_version: rehearsal-not-a-ci-run`，不是 CI 结果；本次产物只是演练产物，不得部署。

## 1. 步骤、命令与结果

| # | 步骤 | 实际命令（路径为占位） | 耗时 | 结果 |
|---|---|---|---|---|
| 0 | 启动 Docker Desktop | `open -a Docker` | 数秒 | 守护进程可用，没有出现对话框；资源与网络设置未改 |
| 1 | 预取基础镜像（只按 digest） | `docker pull docker.io/library/python@sha256:fd95fa22…` | 2 s | 成功；`node-host-base`（`node@sha256:4660b1ca…`）本机已有 |
| 2 | 构建 | `release.py build --sha 25312f7cbe6e… --ci-result <SCRATCH>/ci-result.json --release-root <RELEASE_ROOT> --only backend --only agent-host` | 165 s（负载峰值约 10） | 成功；release_id `20261012-25312f7cbe6e-1` |
| 3 | SBOM | `release.py sbom --release-dir <RELEASE_ROOT>/releases/<id>` | <1 s | **退出码 3**：`syft not installed (pinned 1.18.1)`（见 §3.1） |
| 4 | 预检（stage） | `release.py preflight --release-dir … --category stage` | 5 s | **失败**：`licenses` 步骤失败（发现的缺陷，见 §2），`source_sbom` 与 `vulnerabilities` 因缺少工具失败；结果按设计记入清单 |
| 5 | 签名 | `release.py sign --release-dir … --key <SCRATCH>/keys/release-test` | <1 s | 成功；签名 sha256 前缀 `8e3e6407bca50d5f` |
| 6 | 封存与核对 | `archive.py seal / check --root <RELEASE_ROOT> --release-id <id> --allowed-signers <SCRATCH>/keys/allowed_signers` | <1 s | 按设计拒绝：`preflight_not_passed`；check 另报 `not_sealed` |
| 7 | 载入核对 | 先删除本次构建的两个镜像标签（删除后按 digest 已查不到），再执行 `deploy.py load-check --release-dir …` | 4 s | **成功**：两个归档都 `docker load` 成功，`nexloop/<name>@<digest>` 的 `{{.Id}}` 等于清单 digest |
| 8 | 部署 dry-run（app 主机） | `deploy.py dry-run --host-role app --stage-env <SCRATCH>/stage.env --out <SCRATCH>/plan-app --release-dir … --allowed-signers … --trusted-sha 25312f7cbe6e… --image-var BACKEND_IMAGE=backend --image-var AGENT_HOST_IMAGE=agent-host --load-check` | 数秒 | 渲染出 `Caddyfile`、`app-host.nft`、`compose.images.env` 和 9 个步骤。`images_loaded_by_digest`、`image_variables_mapped`、`compose_images_from_manifest` 均为 true；`release_verified` 为 false，原因只有 `preflight_not_passed`（签名用临时密钥验证通过）。另见 §3.3 |
| 9 | promote | `deploy.py promote --release-root <RELEASE_ROOT> --release-id <id> --allowed-signers …` | <1 s | 按设计拒绝：`preflight_not_passed;not_sealed`，没有建立 `current` 链接 |
| 10 | rollback-check | `deploy.py rollback-check --release-root <RELEASE_ROOT> --target <id> --database-head 0172` | <1 s | 按设计拒绝：`no_current_or_known_good`、`target_unknown_or_not_sealed`，提示向前修复 |

### 产物

| 产物 | 大小 | digest / sha256（前缀） |
|---|---|---|
| `images/backend.tar` | 67,252,224 B | 镜像 `sha256:c8f790748f3c…`；基础镜像 `sha256:fd95fa221297…` |
| `images/agent-host.tar` | 134,608,384 B | 镜像 `sha256:5c9f13c5a297…`；基础镜像 `sha256:4660b1ca8b28…` |
| `manifest.json`（manifest_version 2） | 2,216 B | `0e09daff4e119b98…`；catalog head `0172`，count 172；runtime_store_format 1 |
| `manifest.json.sig` | — | `8e3e6407bca50d5f…` |

## 2. 发现并修复的断点：预检的许可证步骤在干净检出中必然失败

- **现象**：预检在 `git archive` 解压出的干净检出（没有 `.git`；`release.py` `_clean_checkout`）中运行 `check_licenses.py --check`。其中的资产检查调用 `git ls-files`，以退出码 128 崩溃，`licenses` 步骤因此失败。
- **影响**：在 CI 主机上同样会失败。在当前代码下，没有任何 stage 发布能通过预检，也就无法封存和 promote。
- **修复（最小）**：`scripts/check_licenses.py` 新增 `tracked_files(root)`：
  - 有 `.git` 时照旧使用 `git ls-files`；
  - 没有 `.git` 时（即 `git archive` 解压），树中的文件就是受跟踪的文件，排除依赖安装阶段生成的 `.venv`、`node_modules`、`__pycache__`。
  - 修复后，在同一干净检出中执行 `check_licenses.py --check` 通过（276 个包，问题为空）。
- **测试**：`tests/test_license_compliance.py::test_assets_are_checked_in_a_git_archive_extraction_without_git`。
  - 对 HEAD 执行 `git archive` 并解压，确认没有 `.git`；
  - 加入 `.venv` 与 `node_modules` 下的资产文件后，资产检查仍为空；
  - 再加入一个未登记的资产，能被准确报出。

## 3. 已知问题（处置见 §6，分支 `release-known-issues`）

### 3.1 SBOM 与漏洞工具只在 CI 主机上

- `syft 1.18.1` 与 `grype 0.86.1` 按 `versions.lock.json` 的 `release_tools` 只安装在 CI 主机上（linux/amd64 二进制，2026-10-11 安装）。
- 开发机上没有这两个工具，本次没有安装。因此：
  - SBOM、来源 SBOM 交叉核对、漏洞策略三步在本机必然失败（报告为“未安装”，不算通过）；
  - 封存、promote、rollback-check 的成功路径无法在本机走通。这些路径由 `tests/test_deploy_release_wiring.py` 等单元测试覆盖。
- 首次 CI 主机构建时需要核对这三步的成功路径。

### 3.2 gateway 镜像仍未固定 digest（NX-034 证据“首次真实构建待核对”第 3 项）

- `versions.lock.json` 中 `images.gateway` 为 `{"digest": "", "status": "unresolved_not_deployable"}`，`release.py build` 构建 gateway 时会拒绝（`gateway: not pinned`）。本次只构建了 backend 和 agent-host。
- 固定哪个上游 Caddy 镜像的 digest 由负责人或部署线决定。固定之后，在 CI 主机上按 digest 预取，再构建三镜像发布。

### 3.3 显式 `--image-var` 可以漏掉 compose 必需的镜像变量

- `deploy/stage/compose.app.example.yaml` 要求 `GATEWAY_IMAGE`（`${GATEWAY_IMAGE:?gateway image by digest}`）。
- 本次只传了 backend 和 agent-host 两个 `--image-var`。dry-run 报告 `image_variables_mapped: true`，渲染出的 `compose.images.env` 中没有 `GATEWAY_IMAGE`；真正执行 `docker compose up` 时才会失败。
- 默认映射（`DEFAULT_APP_IMAGES`）包含 gateway，不会出现这个问题。现有的 `tests/test_deploy_dryrun.py` 有意只传两个变量。
- **建议**：dry-run 增加检查 `compose_image_variables_complete`，从所用 compose 文件中读出 `*_IMAGE` 必填变量，与渲染结果比对。这会改变现有测试的预期，等决定后再改。

### 3.4 开发机构建的是 linux/arm64，且发布 digest 是带 attestation 的 OCI index

- 在 Apple Silicon 开发机上构建的镜像平台为 `linux/arm64`。`docker save` 记录的 digest 是一个 OCI image index，里面包含：
  - 一个 arm64 镜像 manifest；
  - 一个 attestation manifest（`vnd.docker.reference.type: attestation-manifest`，buildx 默认生成的来源证明）。
- 结论：
  - 开发机的产物不能部署到 x86_64 主机。发布必须在 CI 主机上构建，这与 NX-034 的设计一致；
  - 清单中的镜像 digest 是 index digest，不是单平台 manifest digest。本次核对确认，`{{.Id}}`、`docker load` 之后的 `nexloop/<name>@<digest>` 引用、清单三者一致；
  - CI 主机上需要再确认一次：平台为 amd64，并且 attestation 是否保留（是否保留会改变 digest，但三处仍会一致）。

## 4. “首次真实构建待核对”三项的结论

| 项 | 结论 |
|---|---|
| `docker save` 注解 | 归档 `index.json` 每个镜像有一项，注解为 `io.containerd.image.name = docker.io/nexloop/<name>:<release_id>`，`org.opencontainers.image.ref.name = <release_id>`。`saved_image_digest` 依靠带 `docker.io/` 前缀的名称匹配成功，两个镜像各得到唯一的 digest |
| `{{.Id}}` 与载入后的 digest 引用 | 构建时：`{{.Id}}` 等于归档 digest（两个镜像的 build 内校验都通过）。载入时：先删除本地镜像，`docker load` 后 `nexloop/<name>@<digest>` 能解析，`{{.Id}}` 等于清单 digest。containerd 存储下三者一致（见 §3.4 关于 index digest 的说明） |
| gateway 固定 digest | 未完成，lock 中仍为 unresolved，构建会拒绝（§3.2） |

## 5. 清理

- 删除了本次构建和载入产生的 `nexloop/backend:20261012-25312f7cbe6e-1` 与 `nexloop/agent-host:20261012-25312f7cbe6e-1`。
- 按 digest 预取的 `python@sha256:fd95fa22…`（锁定的构建基础镜像）保留，供之后的构建使用。
- 其他镜像和容器没有动。Docker Desktop 保持运行。
- `<SCRATCH>` 中的临时密钥、发布目录和计划目录在演练结束后删除。

## 6. 已知问题的处置（分支 `release-known-issues`，从 `dff0d91` 切出）

### 6.1 显式镜像变量漏掉 compose 必需的镜像（§3.3）：已修复

- **做法**：`scripts/deploy/deploy.py` 从各主机角色的 Compose 文件读出全部必填镜像变量：
  - app 主机：`BACKEND_IMAGE`、`AGENT_HOST_IMAGE`、`GATEWAY_IMAGE`；
  - data 主机：`POSTGRES_IMAGE`、`VALKEY_IMAGE`。
  - 读取规则：所有 `${*_IMAGE:?…}`，函数为 `required_image_variables`。
- **新检查**：dry-run 新增 `compose_image_variables_complete`。渲染结果缺少任何一个必填变量时 dry-run 不就绪，`missing_image_variables` 列出缺少的变量。即使显式传了 `--image-var` 也不例外。
- **测试**（`tests/test_deploy_dryrun.py`）：
  - 合成发布加入 gateway 镜像；
  - 三个必填变量逐一省略，都被拒绝；
  - data 主机两项齐全。

### 6.2 gateway 未固定 digest（§3.2）：预检拒绝，回填命令已提供

- **预检拒绝**：stage 预检新增步骤 `images_pinned`（`pinned_image_problems`）。在被发布提交的 lock 中，`images.v1.json` 的每个构建基础镜像和上游镜像（gateway）都必须有 reference 与 digest，否则预检失败，不会拖到构建或部署时才失败。当前 lock 下这一步报告 `gateway: not pinned by digest (unresolved_not_deployable)`。
- **固定的对象**：固定 linux/amd64 镜像 manifest 的 digest，不固定多平台 index 的 digest。这样上游镜像与自建镜像一样，是单平台、不带 attestation 的镜像（见 6.3）。
- **回填（在 CI 主机上，由调度员执行；本机没有拉取外部镜像）**：
  1. 选定 Caddy 版本标签，例如 `docker.io/library/caddy:<版本>`，读出其中 linux/amd64 manifest 的 digest：
     ```bash
     docker buildx imagetools inspect docker.io/library/caddy:<版本> --format '{{json .Manifest}}'
     ```
     在输出的 `manifests` 中取 `platform` 为 `{"architecture":"amd64","os":"linux"}` 那一项的 `digest`。
  2. 按 digest 拉取（不按标签）：
     ```bash
     docker pull docker.io/library/caddy@<digest>
     ```
  3. 写入 lock：
     ```bash
     python scripts/release/release.py pin-upstream --lock-name gateway --reference docker.io/library/caddy:<版本> --digest <digest>
     ```
     - 写入前会核对四件事：daemon 为 linux/amd64；该镜像已按 digest 存在于本地；`{{.Id}}` 等于该 digest；平台为 linux/amd64。
     - 任何一项不符都拒绝，lock 不变。
     - 写入的条目为 `{reference, digest, platform: linux/amd64, status: pinned, pinned_at}`。
  4. 校验：
     - `git diff versions.lock.json` 只改动了 `images.gateway`；
     - `python -c` 调用 `release.pinned_image_problems()` 返回空；
     - lock 的改动经评审提交。
     - 之后构建三镜像发布，执行 `release.py build`（不加 `--only`），在 `load-check` 中核对 `nexloop/gateway@<digest>`。
- **测试**（`tests/test_release_build_archive.py`）：
  - 预检在真实 lock 下报告 gateway 未固定；
  - 全部固定的 lock 通过，基础镜像缺 digest 时被报出；
  - `pin-upstream` 在以下情况都拒绝且不写 lock：非上游镜像、非法 digest、arm64 daemon、本地不存在、平台不符；
  - 成功时只改动 gateway 一项。

### 6.3 开发机 arm64 构建与 attestation index（§3.4）：选择拒绝，不是告警

- **理由**：stage 主机是 linux/amd64。如果只告警，arm64 的发布仍会被签名、封存，成为一个不可部署却看似完整的发布。拒绝发生在构建开始之前，代价只是开发机无法再做本地构建演练，而发布构建本来就只在 CI 主机进行。
- **规则**：
  - `release.py build` 在 daemon 平台不是 linux/amd64 时拒绝：`release images are built only on the CI host (linux/amd64)`，函数为 `require_release_platform`；
  - 构建一律带 `--platform linux/amd64 --provenance=false --sbom=false`，发布 digest 指向的就是那个单平台镜像本身，不含构建主机的 provenance 信息；SBOM 另由锁定版本的 syft 生成；
  - 每个归档保存后核对：digest 指向的内容必须恰好是一个 linux/amd64 镜像，且没有 attestation manifest（`require_release_image`），否则拒绝。
- **测试**：
  - arm64 daemon 在任何构建之前被拒绝；
  - 带 attestation 的归档被拒绝；
  - arm64 镜像被拒绝；
  - 正常构建的参数中包含上述三个选项。
- **本地复核受影响**：本机（arm64）不能再运行 `release.py build`，本文 §1 的构建一步今后只能在 CI 主机上复现。

### 6.4 SBOM 与漏洞工具只在 CI 主机（§3.1）：保持现状

- `syft` 与 `grype` 只按 lock 安装在 CI 主机上，开发机不安装。
- 开发机上的 SBOM、来源 SBOM 与漏洞三步报告“未安装”，不算通过。所以开发机上的发布不能通过预检，也不能封存或 promote，这符合预期。
- 发布与部署的成功路径以 CI 主机为准。
