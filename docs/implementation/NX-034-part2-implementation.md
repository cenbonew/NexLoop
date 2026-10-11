# NX-034 第二部分：构建打包、归档保留、GitHub 同步接线

依据 `NX-034-design.md`（D1–D7 已定）和 `NX-034-local-implementation.md` §4 的剩余事项。分支 `nx034-s2`，从 `nx036-impl` `2a2f975` 切出（release.py 与 NX-036 共用），没有迁移。

**范围限定**：本机没有启动 Docker，也没有构建真实镜像。测试中的 docker 是替身：记录每次调用，写出 OCI layout 形式的 tar。真实构建由调度员在 CI 主机上执行。没有 push，没有打 tag，没有发 GitHub Release（B 类操作）。

## 1. 构建与打包（§2.1）：`release.py build`

```
release.py build --sha <sha> --ci-result <result.json> --release-root <RELEASE_ROOT> [--sequence N] [--only <name>]...
make build-release SHA=<sha>    # build → sbom → preflight stage → sign → archive seal
```

- **前提**：
  - CI 结果写明这个提交并且 `passed`；
  - 本检出的 HEAD 就是该提交，没有修改，也没有未跟踪文件（镜像只从被发布的提交构建）；
  - Docker 使用 containerd 镜像存储（见 §2）。
  - 任一不满足都拒绝。
- **镜像清单** `deploy/release/images.v1.json`：
  - `backend`：API、后台、Web 静态资源，构建上下文由 `prepare_community_build.py` 准备；
  - `agent-host`：构建上下文由 `prepare_host_build.py` 准备；
  - `gateway`：`upstream` 类，按 digest 固定的第三方镜像，本地已有时重新打标签，从不按标签拉取。
  - 构建参数取自 `versions.lock.json`，按 digest 传入。`base` 指最终阶段的基础镜像，记为 `base_digest`。
- **步骤**（每个镜像）：
  1. `docker build --pull=false --build-arg …@sha256:… --label org.opencontainers.image.revision=<sha> --tag nexloop/<name>:<release_id>`（upstream 类改为 `docker tag <pinned>@sha256:… nexloop/<name>:<release_id>`）；
  2. `docker save -o <name>.tar nexloop/<name>:<release_id>`；
  3. 从归档的 OCI `index.json` 读出该标签对应的清单 digest（`io.containerd.image.name` 或 `org.opencontainers.image.ref.name` 注解；必须恰好一个）；
  4. 再与 `docker image inspect --format {{.Id}}` 比对，不一致即拒绝。
- **产物**：
  - 发布目录 `<RELEASE_ROOT>/releases/<release_id>/`，清单 `manifest_version` 为 2；
  - `images[]` 新增 `reference = nexloop/<name>@<digest>`，与 NX-032 dry-run 渲染的引用一致；
  - 构建暂存目录在 `.ci-results/release-build/` 下（已被 git 忽略），结束后删除。
- **当前状态**：`versions.lock.json` 中 `gateway` 尚无 digest，构建 gateway 会拒绝（`gateway: not pinned`）。在固定 digest 之前，可用 `--only backend --only agent-host` 构建另外两个镜像；但 NX-032 dry-run 默认需要 gateway 镜像。

## 2. `docker load` 之后的引用与标签规则

- **镜像引用**：部署与 Compose 只用 `nexloop/<name>@sha256:<清单 digest>`，从不用标签，也从不用 `latest`。
- **标签**：`nexloop/<name>:<release_id>` 只是给人看的标签。`docker load` 之后它随镜像一起出现，可以重复 load，不会移动其他发布的标签。
- **为什么要求 containerd 镜像存储**：
  - 在 containerd 镜像存储中，`docker save` 记录的是镜像清单 digest，`docker load` 之后 `docker image inspect nexloop/<name>@<digest>` 能解析到同一个 digest；
  - 经典存储（overlay2 graphdriver）load 后没有 RepoDigests，按 digest 引用会解析失败，Compose 会尝试拉取。
  - 因此 `build` 与 `load-check` 都先检查 `docker info` 的 `driver-type io.containerd.snapshotter.v1`，不符即退出码 2。
- **部署侧** `release.py load-check --release-dir <dir>`（NX-032 的 `load_images` 步骤可调用）：逐个镜像核对归档哈希，执行 `docker load -q -i`，再按 `nexloop/<name>@<digest>` 解析，必须得到清单中的 digest。任何一项不符都拒绝。
- **待在 CI 主机上首次真实构建时确认**：
  - 替身模拟的是 Docker 25+ 在 containerd 存储下 `docker save` 的注解形式；
  - 首次真实构建需要核对三点：`index.json` 的注解；`{{.Id}}` 等于清单 digest；load 之后按 digest 能够解析。
  - 若真实行为与此不同，修改 `saved_image_digest` 和本节说明，部署引用的写法不变。

## 3. 清单中的 `preflight` 块（NX-036 遗留的两项）

- **stage 预检**：`release.py preflight --release-dir <dir> --category stage`，对清单中的提交执行，必须在签名之前，且只能执行一次。
  - clean checkout（`git archive`，锁定安装）；
  - `check_licenses.py --check`（阻断）；
  - 源码 SBOM（syft dir 扫描后用 grype 扫描）与许可清单交叉核对：差异只列出，不阻断（D5）；**缺少工具判失败**；
  - stage 类别的漏洞策略：只有无效或过期的豁免会失败。
- **public 预检**：`--category public` 执行 NX-036 的 9 步门槛，合并本发布的镜像发现。
- **写入方式**：报告存到 `<release>/preflight/`（`preflight.json`、`licenses.json`、`source.spdx.json`），清单增加：

  ```
  "preflight": {"category", "commit", "ok", "report": "preflight/preflight.json", "report_sha256", "steps": {名称: 通过与否}}
  ```

  签名覆盖这个块。预检失败也照样写入（保留证据），命令退出码为 1。
- **`verify`**（AT-055）新增的拒绝条件：
  - `manifest_version` 不是 2；
  - 缺少 preflight 块（`preflight_missing`）；
  - 报告文件哈希不符（`preflight_report_changed`）；
  - 报告或块的提交与清单不同（`preflight_commit_differs`）；
  - 报告与块的摘要不一致（`preflight_summary_differs`）；
  - 预检没有通过（`preflight_not_passed`）。
- **测试辅助的变化**：`tests/test_release_local.py` 的 `make()` 现在默认写入一份合成的、已通过的 stage 预检。NX-032 的 dry-run 测试（`nx032-local` 分支）复用 `make()`，因此合并后无需改动。

## 4. 归档与保留（§2.4，D3）：`scripts/release/archive.py`

- **布局**（`<RELEASE_ROOT>` 是参数）：
  - `releases/<release_id>/`：`manifest.json`、`manifest.json.sig`、`images/*.tar`、`sbom/*.spdx.json|*.findings.json`、`preflight/*`；
  - `index.json`：按 release_id 的索引，从目录重建，可以删除；
  - `deployments.json`：部署记录，只追加，由部署工具写入；
  - `current`：符号链接，由部署工具切换，本工具从不改动。
- **`seal`**：
  - 先验签，再核对完整性：清单列出的每个归档与 SBOM 哈希相符、preflight 块有效；出现清单之外的文件或符号链接即拒绝；
  - 然后把文件改为 0444、目录改为 0555，并重建索引；
  - 已封存的发布不能再次封存。
- **`check`**（`make verify-release`）：只读核对签名、完整性和是否已封存。
- **`record-deployment`**：只接受已封存的发布，记录 `deployed_at` 和 `known_good`。
- **`prune`**（`make prune-releases`，默认 dry-run，`APPLY=1` 才执行）：
  - 保留以下发布的 image archive：最近 5 个、90 天内部署过的、`current`，以及 `current` 之外最近一个 known_good（`deploy/release/retention.v1.json`）；
  - 清理只删除 `images/*.tar`，清单、签名、SBOM 和预检报告永久保留；
  - 无论策略怎么配置，`current` 都不会被清理；
  - 已清理的发布无法再部署或回滚到（verify 报 `archive_changed`）；同一提交重新构建会得到新的发布。
- **只在本机**：不推送，不复制。清单、签名和 SBOM 由 NX-035 的文件备份带到另一台主机。

## 5. GitHub 同步接线（§3）：`scripts/release/sync_github.py`，`make sync-github`

- **推送范围**：
  - 只推 `main`（快进）和 `--tag` 显式点名的 `v*` tag，一律使用显式 refspec；
  - 从不使用 `--force`、`--mirror`、`--all`、`--tags`，也从不删除 ref；
  - remote 若配置为 mirror 或带有自己的 push refspec，直接拒绝。
- **推送前的门槛**：外发范围是 `<上次已同步>..main`；首次同步须由维护者给出 `--baseline`（GitHub 上现有的 main），否则报 `no_sync_baseline`。
  1. 快进；
  2. 范围内每个 first-parent 提交都有通过的 CI 结果（`ci_result_missing`）；
  3. `check_publication.py --tree main`（`publication_scan_failed`）；
  4. 在 main 的临时工作树中运行 `prepush-scan.sh main <base>`，受限标识名单为私有文件参数，缺失即失败（`scan_failed`、`restricted_identifiers_missing`）；
  5. 每个 tag 的检查依次是：名称为 vX.Y.Z、是附注 tag、指向 main 上的提交、用负责人的 allowed signers 文件通过 `git verify-tag`（SSH 签名）。对应的失败码为 `tag_name_invalid`、`tag_not_annotated`、`tag_not_on_main`、`tag_unsigned`。
  - 所用脚本取自运行同步的这份受信检出，不用外发提交里的脚本。
- **推送**：门槛全部通过后，调用 NX-033 的 `nexloop_ci.sync`（只推 main，快进）。成功后用 `ls-remote` 读回远端 main，写入 `github_main`（task 证据从这里取）；读回不符记 `post_push_mismatch`。之后再逐个推 tag，不加 force。
- **状态**（`<queue>/sync/<remote>.json`）：
  - `sync_blocked` 只记录失败码，不含扫描内容，也不推送任何东西；
  - `sync_pending` 沿用 NX-033（AT-053），并增加 `pending_since`、`retry_after`：退避 1、2、4…分钟，最长 1 小时一次；退避期内调用会直接跳过；
  - 恢复后补推，积压清零。
- **尚未做的**：积压超过 24 小时的告警（NX-030 的规则）；由合并控制器在推进 main 后自动触发（D4，属于部署接线）。
- **测试不碰真实 GitHub**：远端由本地 bare 仓库代替。

## 6. 测试

- `tests/test_release_build_archive.py`（14）：
  - 构建：引用与 NX-032 一致、按 digest 传基础镜像、写入 revision 标签、清理暂存目录、清单不含本机路径；
  - 拒绝构建：CI 未通过、经典存储、digest 不一致、构建失败、gateway 未固定、未知镜像、发布目录已存在；
  - upstream 镜像只重新打标签，不拉取；归档中的 digest 必须恰好一个；检出不在该提交或有未跟踪文件时拒绝；
  - load-check：成功；load 后 digest 不符、经典存储、归档被改都拒绝；
  - stage 预检：许可检查阻断、交叉核对只列出、缺工具失败、豁免过期失败；
  - preflight 块：签名覆盖、verify 通过，报告被改、预检未通过、缺少块、提交不符、旧版本清单都被拒；
  - seal、check 与索引；保留策略（最近、近期部署、current、前一个 known_good；dry-run 与 apply；current 永不清理）；部署记录只接受已封存的发布；CLI 退出码。
- `tests/test_github_sync.py`（10）：
  - 门槛通过后推 main 并记录 `github_main`，远端只有 main；
  - 受限标识、私有路径、凭据、缺少 CI 结果都会 blocked，远端不变，状态中不含命中内容；
  - 受限名单缺失时 fail closed；非快进、mirror、push refspec 都被拒；
  - 不可达时 pending、退避、跳过，恢复后补推；
  - tag 的五种拒绝情况（含其他签名者），以及已签名 tag 的推送；CLI 退出码。
- `tests/test_release_local.py`：`make()` 写入合成预检，原有 5 个测试不变。

## 7. 未做与待确认

- 真实镜像构建，以及 containerd 存储下 save/load 的实际行为，由调度员在 CI 主机上首次构建时核对（见 §2）。
- `versions.lock.json` 中 gateway 的 digest（以及 backend、agent-host、web 的“unresolved”状态说明），在首次真实构建后更新。
- 部署工具（NX-032）切换 `current` 和调用 `record-deployment`、`load-check` 的接线，在 NX-032 合入后进行。
- 同步积压告警（NX-030）、合并后自动触发同步（D4）、负责人签名公钥文件入库：都属于部署或负责人事项。

## 8. 部署侧接线（分支 deploy-release-wiring，从 main 7f2711d 切出）

- **`deploy.py load-check --release-dir R`**：部署主机上执行，调用 `release.load_check`：先核对归档哈希，再 `docker load`，然后 `nexloop/<镜像名>@<digest>` 必须解析回清单中的 digest，否则拒绝。
  - dry-run 的 `load_images` 步骤改为执行这条命令；
  - dry-run 加 `--load-check` 时当场核对，结果记为 `images_loaded_by_digest`。不加时 dry-run 仍然不调用 docker。
- **`deploy.py promote --release-root ROOT --release-id ID --allowed-signers F`**：部署门槛通过之后执行。
  - 先用 `archive.check` 核对签名、完整性和封存状态；
  - 再执行 `archive.promote`：旧的 current 变为 `known_good`，ID 变为 `current`，两个链接都通过“临时链接 + rename”原子切换；
  - 任一步失败，两个链接都恢复原样；成功后写入部署记录（`known_good: true`）。
  - 已经是 current 的发布不能再次 promote。
- **`deploy.py rollback-check --release-root ROOT --target ID [--database-head]`**：
  - 回滚目标必须是 known_good，或构建时间更早的已封存发布，且镜像归档仍在；
  - 并且要通过 `release.rollback_check`（当前 current 的清单对比目标清单，检查 schema 和运行时格式）；
  - 否则拒绝，并提示向前修复。
- **保留规则**：`current` 和 `known_good` 链接指向的发布永不清理。保留计划中分别记为 `current_link` / `known_good_link`；即使策略关闭了“保留当前和前一个可用发布”，`prune` 也会跳过这两个发布。
- **测试**：`tests/test_deploy_release_wiring.py`（5 个），用合成的已封存发布和 docker 替身，覆盖：
  - load 后 digest 不一致被拒；
  - promote 的原子切换：在两个注入点失败，都恢复原样、不留临时链接，部署记录不变；
  - 回滚目标的各种拒绝情况；
  - 保留规则不清理 current 和 known_good；
  - CLI 退出码。
- **其他测试的同步**：`test_deploy_dryrun`（步骤列表增加 `promote`）、`test_release_build_archive`（保留理由增加 `current_link`）。
- **运行手册**：`NX-032-deploy-runbook.md` §6、§8 已同步。
