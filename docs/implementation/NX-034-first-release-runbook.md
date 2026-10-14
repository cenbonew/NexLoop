# NX-034 首个签名发布与 NX-032 部署 dry-run：CI 主机运行手册

- **性质**：只是文档。命令全部参数化，不含地址、主机名或真实路径以外的私有值。每一步由调度员或负责人在 CI 主机上执行；标为 **[主机配置变更]** 的步骤需负责人确认后才能做。
- **依据**：
  - `NX-033-design.md`：§2 用户分离，§3.2 用户隔离，D2 rootless；
  - `NX-034-design.md`：§2.1 构建，§2.3 签名，§2.4 归档；
  - `NX-034-part2-implementation.md`；
  - `NX-034-local-dry-run.md`（含 §6 已知问题处置）；
  - `scripts/release/release.py`、`scripts/release/archive.py`；
  - `Makefile`（`build-release`）；
  - `scripts/deploy/deploy.py`；
  - `NX-032-deploy-runbook.md` §0。

## 0. 参数

| 参数 | 含义 |
|---|---|
| `$SHA` | 要发布的完整提交（40 位），必须在 main 上并且有 CI 通过记录 |
| `$CI_QUEUE` | NX-033 队列目录（含 `results/<sha>/attempt-<n>/result.json`） |
| `$TRUSTED_REPO` | `nexloop-git` 的 bare 仓库（受信 ref 的来源） |
| `$CI_BUILD_ROOT` | `nexloop-ci` 自己的构建工作区，0700 |
| `$HANDOFF_ROOT` | 构建产物移交目录，见 §1.3 |
| `$RELEASE_ROOT` | 发布归档根，即已建好的 `/srv/nexloop/releases`，属主 `nexloop-release` |
| `$RELEASE_SIGNING_KEY` | 发布签名私钥，在 `/srv/nexloop/release-signing/` 下，0600，属主 `nexloop-release` |
| `$ALLOWED_SIGNERS` | 由发布公钥生成的 allowed_signers 文件（只含公钥），见 §4 |
| `$RID` | 构建产生的 release_id（`<UTC 日期>-<短 SHA>-<序号>`） |

## 1. 构建方式

### 1.1 设计的本意与现状的差距

- **设计本意**（NX-034 §2.1）：执行者是 `nexloop-release`，它与 `nexloop-ci` 分开。它能写发布归档，但读不到 stage secret。构建在 NX-033 的沙箱中进行，离线，只用依赖缓存和按 digest 预载的基础镜像。签名私钥只由 `nexloop-release` 持有（§2.3）。
- **CI 主机现状**：
  - `nexloop-release` 是 nologin 用户，不在 docker 组，**没有自己的容器运行时**；
  - 只有 `nexloop-ci` 有 rootless docker；
  - `syft` 和 `grype` 也只装在 `nexloop-ci` 自己的目录下（`versions.lock.json` 的 `release_tools`）。
- **代码对执行环境的要求**：
  - `release.py build` 要求执行它的检出处于 `$SHA` 且干净（`require_checkout_at`）；
  - docker 使用 containerd 镜像存储，daemon 为 linux/amd64（0175 之后，`require_release_platform`）；
  - `sbom` 和 `preflight` 会改写 `manifest.json`，所以必须在签名之前完成；
  - `sign` 之后只能做 `seal` 和只读核对。

### 1.2 两种方案

| | 方案 A：`nexloop-ci` 构建，`nexloop-release` 签名封存（推荐用于首个发布） | 方案 B：`nexloop-release` 独立构建（设计的最终形态） |
|---|---|---|
| 构建（build、sbom、preflight） | `nexloop-ci`，用它的 rootless docker 和它的 syft/grype | `nexloop-release`，需要它自己的 rootless 运行时和工具 |
| 签名、封存 | `nexloop-release`（只有它读得到私钥） | `nexloop-release` |
| 主机配置变更 | 移交目录与权限（§1.3）；CI 结果目录对 `nexloop-release` 可读 | 再加上：为 `nexloop-release` 配置 subuid/subgid、`loginctl enable-linger`、rootless docker（containerd 存储）、离线依赖缓存和 syft/grype 的安装，并把这个运行时纳入 NX-033 沙箱（网络只有 loopback、cgroup 限制） |
| 安全性质 | 签名私钥与构建运行时分离：CI 能构建，但不能签名。`nexloop-release` 在签名前独立核对产物哈希和 CI 记录（§2.4）。弱点：产物由 CI 用户生成，依赖 CI 用户本身可信，这与“CI 结果是权威构建结果”的设定一致 | 构建与签名在同一个受限用户下，CI 用户无法触碰发布产物 |
| 首次落地难度 | 低：不新增容器运行时 | 高：需要新的 rootless 运行时、依赖缓存和工具安装 |

- **推荐**：首个发布用方案 A。方案 B 作为后续主机配置任务，由负责人决定何时做（D-R1）。
- **以下 §2 按方案 A 编写**。`make build-release` 把全部步骤放在一个用户下执行，不适用于方案 A，所以下面拆成逐条命令。

### 1.3 产物移交：只读拷贝

- **移交目录 `$HANDOFF_ROOT`** **[主机配置变更]**：
  - 属主 `nexloop-ci`，组为一个只含 `nexloop-release` 的专用组（如 `nexloop-release-read`），权限 2750（setgid，组只读）；
  - `nexloop-ci` 在其下写 `releases/<RID>/`，文件 0640、目录 2750；
  - `nexloop-release` 只读，不能改动。
  - 不需要 ACL；如果主机策略要求，也可以改用 ACL：`setfacl -m g:nexloop-release-read:rx`。
- **CI 结果可读** **[主机配置变更]**：`$CI_QUEUE/results/` 对 `nexloop-release` 可读，加入同一个只读组，目录 2750、文件 0640。这样 `nexloop-release` 能独立核对 CI 记录（§2.4）。
- **拷贝**：`nexloop-release` 把 `$HANDOFF_ROOT/releases/<RID>/` 完整拷贝到 `$RELEASE_ROOT/releases/<RID>/`，用 `cp -R`，不保留属主。之后在自己的副本上核对、签名、封存。移交目录中的原件不再使用，发布完成后由 `nexloop-ci` 删除。

### 1.4 “发布的 SHA 必须有 CI 通过记录”

核对分两层，签名前由 `nexloop-release` 再独立做一次：

1. **CI 结果**：`nexloop_ci.py result --queue $CI_QUEUE --sha $SHA` 返回 `"status": "passed"`。取最新一次 attempt 的 `result.json`，其中 `sha` 等于 `$SHA`、`status` 等于 `passed`。`release.py build` 也会拒绝不符的结果。
2. **在 main 上**：`git -C $TRUSTED_REPO merge-base --is-ancestor $SHA refs/heads/main` 退出码为 0。
3. **清单与结果一致**：清单中的 `ci_result_digest` 等于该 `result.json` 的 sha256，`commit` 等于 `$SHA`。

## 2. 首个签名发布：逐条命令（方案 A）

前提：gateway 已按 §3 固定 digest，并且固定的提交已合入 main、CI 通过。下面的 `$SHA` 就是这个提交或其后的提交。

| # | 执行用户 | 命令 | 预期输出 | 失败时 |
|---|---|---|---|---|
| 1 | nexloop-ci | `nexloop_ci.py result --queue "$CI_QUEUE" --sha "$SHA"` | JSON，`status` 为 `passed` | 不是 passed 就停止；先让该 SHA 通过 CI |
| 2 | nexloop-ci | `git -C "$TRUSTED_REPO" merge-base --is-ancestor "$SHA" refs/heads/main && echo on-main` | `on-main` | 不在 main 上就停止 |
| 3 | nexloop-ci | `git -C "$TRUSTED_REPO" worktree add --detach "$CI_BUILD_ROOT/checkout-$SHA" "$SHA"`，或 `git clone --no-hardlinks` 后 `checkout $SHA` | 干净检出 | 已存在就换一个新目录；不复用旧检出 |
| 4 | nexloop-ci | 在检出中：`uv sync --frozen` 和 `pnpm install --frozen-lockfile`（离线，只用依赖缓存） | 退出码 0 | `dependency_unavailable`：缓存未命中，按 NX-033 处理缓存，不降级 |
| 5 | nexloop-ci | `docker info --format '{{.OSType}}/{{.Architecture}} {{json .DriverStatus}}'` | `linux/x86_64` 加 `io.containerd.snapshotter.v1` | 不是 containerd 存储：**[主机配置变更]** 在 `nexloop-ci` 的 rootless daemon 配置中启用 containerd snapshotter，需负责人确认 |
| 6 | nexloop-ci | 按 digest 核对基础镜像都在本地：`docker image inspect --format '{{.Id}}' <repo>@<digest>`，逐项对照 `versions.lock.json` 的 `python-build-base`、`node-host-base` 和 `gateway` | 每项输出该 digest | 缺失就按 digest 预取（`docker pull <repo>@<digest>`）。`nexloop-ci` 的出站受 NX-033 防火墙限制，预取可能需要**[主机配置变更]**：临时放行镜像仓库，或由负责人用其他账户 `docker save` 后交给 `nexloop-ci` 执行 `docker load`。从不按标签拉取 |
| 7 | nexloop-ci | `ci_result=$(ls -d "$CI_QUEUE/results/$SHA"/attempt-* \| sort -t- -k2 -n \| tail -1)/result.json`，然后在检出中执行 `uv run --frozen python scripts/release/release.py build --sha "$SHA" --ci-result "$ci_result" --release-root "$HANDOFF_ROOT"` | `{"ok": true, "release_id": "<RID>", "images": {...三个镜像...}}` | 退出码 1：读 `refused`。平台不是 amd64、带 attestation、digest 不一致、基础镜像未固定，都要先修好再从第 3 步重来。退出码 2：输入或环境问题。发布目录已存在：用 `--sequence 2` |
| 8 | nexloop-ci | `uv run --frozen python scripts/release/release.py sbom --release-dir "$HANDOFF_ROOT/releases/$RID"` | `{"ok": true, "scanned": true, ...}` | 退出码 3：syft 或 grype 未安装或版本不符，按 lock 重装（只在 CI 主机）。退出码 1：漏洞策略问题（stage 只列出，不阻断；无效或过期的豁免会阻断）|
| 9 | nexloop-ci | `uv run --frozen python scripts/release/release.py preflight --release-dir "$HANDOFF_ROOT/releases/$RID" --category stage` | `{"ok": true, "category": "stage", "steps": {... "images_pinned": true}}` | `licenses` 失败：读 `preflight/preflight.json`。`images_pinned` 失败：回到 §3。预检只能做一次，失败后从第 7 步用新的 sequence 重建 |
| 10 | nexloop-ci | `chmod -R g+rX,o-rwx "$HANDOFF_ROOT/releases/$RID"` | 无输出 | — |
| 11 | nexloop-release | `cp -R "$HANDOFF_ROOT/releases/$RID" "$RELEASE_ROOT/releases/$RID"` | 无输出 | 目标已存在就停止；发布不可覆盖 |
| 12 | nexloop-release | 独立核对 CI 记录：`sha256sum "$ci_result"` 等于清单的 `ci_result_digest`；清单 `commit` 等于 `$SHA`；重复第 2 步 | 三项一致 | 任一不符就删除第 11 步的副本并停止 |
| 13 | nexloop-release | 签名前核对产物：对清单中每个 `images[].archive` 计算 sha256，与 `archive_sha256` 比对；对每份 SBOM 与 `sbom_sha256` 比对 | 全部一致 | 不一致就删除副本并停止，不签名 |
| 14 | nexloop-release | `python3 scripts/release/release.py sign --release-dir "$RELEASE_ROOT/releases/$RID" --key "$RELEASE_SIGNING_KEY"`。脚本来自第 3 步的同一 SHA 检出，用只读拷贝或该提交的 `git archive`，不用候选代码 | `{"ok": true, "signed": true, "signature_sha256": "..."}` | `already signed`：这个副本已签过，不要重签 |
| 15 | nexloop-release | `python3 scripts/release/archive.py seal --root "$RELEASE_ROOT" --release-id "$RID" --allowed-signers "$ALLOWED_SIGNERS"` | `{"ok": true, ...}`；文件 0444、目录 0555 | `preflight_not_passed` 等：不封存，查第 9 步的结果 |
| 16 | nexloop-release | `python3 scripts/release/archive.py check --root "$RELEASE_ROOT" --release-id "$RID" --allowed-signers "$ALLOWED_SIGNERS"` | `{"ok": true}` | 失败就保留现场，交给负责人 |
| 17 | nexloop-ci | `rm -rf "$HANDOFF_ROOT/releases/$RID"`、`git -C "$TRUSTED_REPO" worktree remove "$CI_BUILD_ROOT/checkout-$SHA"`；按标签删除本次构建的 `nexloop/*:<RID>` 镜像标签 | — | 只删本次创建的，按标签前缀判断 |

**说明**：
- 第 14–16 步的 `python3` 只用标准库和 `ssh-keygen`，不需要依赖环境。`nexloop-release` 不需要 uv 或 node。
- **记录证据**：第 7、8、9、14、15、16 步的 JSON 输出与 `$RID`、`$SHA` 一起存入私有证据目录，然后回填 planning 证据（由调度员执行）。

## 3. gateway 固定 digest（`release.py pin-upstream`）

在首个发布之前做一次，产生一个改动 `versions.lock.json` 的提交，经评审合入 main。

| # | 执行用户 | 命令 | 预期输出 | 失败时 |
|---|---|---|---|---|
| 1 | 负责人选定 | 选定 Caddy 版本标签 `docker.io/library/caddy:<版本>`（只用作 reference 记录） | — | — |
| 2 | nexloop-ci（需出站，见 §2 第 6 步的配置说明） | `docker buildx imagetools inspect docker.io/library/caddy:<版本> --format '{{json .Manifest}}'` | JSON；在 `manifests` 中取 `platform` 为 `{"architecture":"amd64","os":"linux"}` 那一项的 `digest` | 没有 amd64 项就换版本 |
| 3 | nexloop-ci | `docker pull docker.io/library/caddy@<digest>` | 拉取成功 | 不按标签拉取；出站被拒属于主机配置问题 |
| 4 | nexloop-ci（在一个 main 的工作检出中） | `uv run --frozen python scripts/release/release.py pin-upstream --lock-name gateway --reference docker.io/library/caddy:<版本> --digest <digest>` | `{"ok": true, "lock": "gateway", "reference": "docker.io/library/caddy@<digest>", "platform": "linux/amd64"}` | 拒绝时 lock 不变。逐项核对：daemon 为 amd64；本地按 digest 存在；`{{.Id}}` 等于 digest；平台为 linux/amd64 |
| 5 | nexloop-ci | `git diff versions.lock.json` 只改动 `images.gateway`（reference、digest、platform、status: pinned、pinned_at）；`uv run --frozen python -c "import sys; sys.path.insert(0,'scripts/release'); import release; print(release.pinned_image_problems())"` 输出 `[]` | 两项都符合 | 其他字段被改动就放弃这个改动 |
| 6 | 调度员 | 按正常流程提交（`NX-034: pin gateway image digest`）并评审合入；CI 通过后，这个提交或之后的提交才能作为 §2 的 `$SHA` | — | — |

## 4. 部署 dry-run 与 Compose 预检所需的私有文件

全部是私有文件（0600），不进仓库，放在负责人的私有目录或对应主机上。

| 文件 | 用途 | 生成方式 |
|---|---|---|
| `$PRIVATE_STAGE_ENV` | `deploy.py dry-run --stage-env`，以及 `render.py` | 以 `deploy/stage/stage.env.example` 为模板，填入真实值：地址、名称、CIDR、端口、`NEXLOOP_ROOT`、`NEXLOOP_DB`、PG 角色、`AGENT_HOST_UID`、`CI_UID`、`MODEL_PROVIDER_ALLOWLIST` 等。取值来自负责人的只读 inventory（`scripts/deploy/inventory.py`）。用 `scripts/deploy/render.py --env "$PRIVATE_STAGE_ENV" --check` 核对，只列出参数名 |
| `$ALLOWED_SIGNERS` | 验签（dry-run、seal、check、promote） | 由发布公钥生成一行：`nexloop-release namespaces="nexloop-release" <公钥类型> <公钥>`。公钥取自 `/srv/nexloop/release-signing/` 中对应的 `.pub`，只含公钥，部署侧可读 |
| `$PLAN_DIR_APP` / `$PLAN_DIR_DATA` | dry-run 输出（渲染的配置和 `compose.images.env`） | 由 `deploy.py dry-run --out` 生成，目录不能已存在 |
| `$PRIVATE_COMPOSE_APP_ENV` | `docker compose ... compose.app.example.yaml --env-file` 预检 | 两部分拼接。① 非镜像变量：以 `deploy/stage/compose.app.env.example` 为模板，去掉三个 `*_IMAGE` 行，填入端口、地址、`AGENT_LINK_*`、`AGENT_EGRESS_CIDR`、`AGENT_HOST_UID`、`NEXLOOP_ROOT`、`SIGNING_KEY_ID`、`API_EXECUTION_PROFILE`、`BROWSER_*`、`WORKBENCH_APPLICATION_ID`。不放任何 secret（文件开头的注释写明了）。② 镜像变量：直接取 app 主机 dry-run 输出的 `$PLAN_DIR_APP/compose.images.env`，包含三项 `nexloop/<name>@sha256:…`，来自已签名清单 |
| `$PRIVATE_COMPOSE_DATA_ENV` | data 主机 Compose 预检 | 以 `deploy/stage/compose.data.env.example` 为模板；镜像变量取 data 主机 dry-run 输出的 `$PLAN_DIR_DATA/compose.images.env`（发布中 `nexloop/postgres` 的 digest；valkey 取 `versions.lock.json` 的 digest） |
| `$RELEASE_COMMIT` | dry-run 的受信 SHA `--trusted-sha` | `jq -r .commit "$RELEASE_ROOT/releases/$RID/manifest.json"`，并用 `git merge-base --is-ancestor "$RELEASE_COMMIT" origin/main` 确认它在 main 上。不能用 `--trusted-ref refs/heads/main`：发布之后 main 会继续前进，解析出的 SHA 与发布的 commit 不同，`release.verify` 会拒绝 |
| `$CURRENT_MANIFEST`（首次没有） | dry-run 的回滚核对 `--current-manifest` | 首次部署不传。之后取当前部署发布的 `manifest.json`，即 `$RELEASE_ROOT/current` 指向的那份 |

### 命令

| # | 执行用户 | 命令 | 预期输出 | 失败时 |
|---|---|---|---|---|
| 1 | 部署账户（能读 `$RELEASE_ROOT`；dry-run 不需要 docker） | `scripts/deploy/deploy.py dry-run --host-role app --stage-env "$PRIVATE_STAGE_ENV" --out "$PLAN_DIR_APP" --release-dir "$RELEASE_ROOT/releases/$RID" --allowed-signers "$ALLOWED_SIGNERS" --trusted-sha "$RELEASE_COMMIT"` | `ready: true`；checks 中 `release_verified`、`image_variables_mapped`、`compose_image_variables_complete`、`compose_images_from_manifest` 全为 true；`executed: false` | `release_refused`：签名、preflight、digest 或部署 SHA 不符。`missing_image_variables`：映射漏了镜像变量。都不能继续 |
| 2 | 同上 | `scripts/deploy/deploy.py dry-run --host-role data --stage-env "$PRIVATE_STAGE_ENV" --out "$PLAN_DIR_DATA" --release-dir "$RELEASE_ROOT/releases/$RID" --allowed-signers "$ALLOWED_SIGNERS" --trusted-sha "$RELEASE_COMMIT"` | `ready: true`；`compose.images.env` 中 `POSTGRES_IMAGE=nexloop/postgres@<发布 digest>` | 同上；发布中没有 `postgres` 镜像时 `unmapped_images: ["postgres"]`，需要重新构建发布（[NX-034-postgres-image.md](NX-034-postgres-image.md)） |
| 3 | 负责人或部署账户 | 按上表拼出 `$PRIVATE_COMPOSE_APP_ENV` 和 `$PRIVATE_COMPOSE_DATA_ENV`，再执行 dry-run 交叉核对：`deploy.py dry-run --host-role app ... --compose-env "$PRIVATE_COMPOSE_APP_ENV" --out "$PLAN_DIR_APP-2"` | `compose_images_from_manifest: true` | 私有 env 中的镜像与清单不一致，就修正拼接 |
| 4 | 负责人或部署账户（只需 compose CLI，不连 daemon） | `docker compose -f deploy/stage/compose.app.example.yaml --env-file "$PRIVATE_COMPOSE_APP_ENV" config -q`，data 主机同样执行 | 退出码 0 | 缺变量或格式错误，按报错补齐私有 env |
| 5 | 有 containerd docker 的部署主机账户（可选，load 在部署时也会做） | `scripts/deploy/deploy.py load-check --release-dir "$RELEASE_ROOT/releases/$RID"` | `loaded` 列出全部镜像（含 `postgres`），按 digest 解析一致 | `loaded_digest_differs`、`archive_changed`：停止部署 |

- 之后的 promote 和 rollback-check 属于 NX-032 部署的 B 类步骤，见 `NX-032-deploy-runbook.md` 和 NX-034 part2 §8。首次部署成功后，`deploy.py promote` 建立 `current`；第二个发布部署之后，才会有 `known_good`。

## 5. 需要负责人确认的事项

| 编号 | 事项 | 推荐 |
|---|---|---|
| D-R1 | 采用方案 A（CI 构建，`nexloop-release` 签名封存）还是方案 B（`nexloop-release` 独立构建） | 首个发布用 A；B 另立主机配置任务 |
| D-R2 | 移交目录与只读组（§1.3）、CI 结果对 `nexloop-release` 可读 | 新建只含 `nexloop-release` 的只读组，目录 2750 |
| D-R3 | `nexloop-ci` 的 rootless docker 是否已启用 containerd snapshotter（§2 第 5 步） | 未启用就启用；只改 `nexloop-ci` 自己的 daemon 配置 |
| D-R4 | 基础镜像和 gateway 按 digest 预取时的出站方式（§2 第 6 步、§3） | 临时、限时、只放行镜像仓库，执行后恢复；或由负责人在其他账户预取后交接 |
| D-R5 | gateway 使用的 Caddy 版本 | 负责人选定，只固定 amd64 镜像 manifest 的 digest |
