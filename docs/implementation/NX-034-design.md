# NX-034 发布归档与 GitHub 同步：设计稿

状态：**设计稿，未实现，没有执行任何发布或推送**。分支 `nx033-034-design`，从 main `7ea25b8` 切出。NX-034 依赖 NX-033，本稿与 NX-033 的设计稿放在同一分支。

**公开仓库约束**：不写真实主机、地址、用户、路径、仓库地址或凭据，全部用参数表示（§8）。

本稿中的 **“B 类操作”** 沿用 NX-032 §8 的含义：会改变服务器或公开状态的操作，每次执行前必须由负责人本人确认，任何会话都不能自行执行。它与测试基线里的 B 类观察项是两回事。

## 0. 依据

- **规划**：NX-034「实现发布归档与 GitHub 同步」，S5，模块 M23。交付物是“镜像校验、release manifest、GitHub 断连不阻断本地”。
- **验收**：
  - AT-055 镜像发布一致：构建 SHA 与部署 SHA 不同时，拒绝发布，并校验 release manifest；
  - AT-057 版本回滚：遇到不兼容的 schema 或 runtime 镜像回滚时，阻止盲回滚，改走向前修复或恢复流程；
  - AT-053 GitHub 断连：同步部分由本任务负责。
- **规范** `12_LOCAL_CI_AND_RELEASE.md`：
  - §3：Build（锁定的基础镜像、SBOM）、Package（image archive、checksum、manifest、测试结果；不生成虚假的成功 attestation）、Sync（失败只标 `sync_pending`）；
  - §4：发布输出使用 `release_id` 加 SHA，保存 OCI image archive 与 SHA256，部署账户校验后再 load；按 digest，不用 latest；
  - §5：发布记录出具后，运行的 SHA 必须与通过测试的 SHA 一致；
  - §6：回滚；
  - §7：只推受信分支和发布标签，不用未经检查的 mirror；GitHub 只是代码副本。
- **AGENTS.md 的 Deployment 段**：
  - 每次推送前跑 `scripts/ci/prepush-scan.sh` 的三项门槛，任一命中都阻断；
  - 不 mirror，不推 local-only 分支，不 force push main；
  - 推送后核对 `git rev-list --count origin/main..main` 为 0；
  - 部署的镜像 SHA 或 digest 必须与通过测试的 release manifest 一致。
- **NX-032 设计**（`nx032-design` `b117d53`）：
  - §5.1：最小发布清单，NX-032 实现并在部署侧校验；NX-034 扩展；
  - §5.2：发布步骤；
  - §6：doctor `--deploy-gate --release`；
  - §7：回滚，以及拒绝不兼容的盲回滚；
  - §8：B 类操作；
  - D4：NX-032 先做最小清单，归档、签名、同步留给 NX-034。
- **NX-033 设计**：CI 结果 `result.json`（§4.4）、合并控制器（§2.3）、私有扫描模式（§5.3）。
- **NX-035 设计**：清单中引用 `release_id`；不备份 Git 和发布归档（本稿 §2.4 决定发布归档如何保存）。

## 1. 现状核对（只读，main 7ea25b8）

- 没有发布构建、发布清单、签名、SBOM 或归档的实现；12 §8 中的 `make build-release`、`make verify-release`、`make sync-github` 都不存在。
- 镜像目前由 `scripts/community_test.py` 在一次性测试中构建；基础镜像按 digest 固定在 `versions.lock.json`。
- 推送 GitHub 目前由人工在 main 上执行 `git push origin main`，靠 pre-push hook（`prepush-scan.sh --hook`）把关：只允许名为 `origin` 的 remote，main 只能快进，不能删除 ref，拒绝 local-only，外发提交要做三项扫描。没有 tag 推送的做法，也没有同步积压的记录。

## 2. 发布构建与清单

### 2.1 构建（`make build-release SHA=<sha>`）

- **输入**：已经由 NX-033 合并到 main、`result.json` 为 `passed` 的 SHA。CI 结果没有通过时直接拒绝。
- **执行者**：`nexloop-release` 用户。它与 `nexloop-ci` 分开：可以写发布归档，但读不到 stage secret。构建在 NX-033 的沙箱中进行，仍然离线，只用依赖缓存和按 digest 预载的基础镜像。
- **产物**：
  - API / 后台镜像、Host 镜像、Web 静态资源（打进 API 镜像）。每个镜像以 OCI archive 输出，并记录 archive 的 SHA256 和镜像 digest；
  - SBOM（SPDX JSON，每个镜像一份），以及依赖漏洞扫描的结果摘要（D2）；
  - 源码归档：`git archive` 的 SHA256，用于对照公开 tag。
- **可复现性**：不承诺逐位可复现。约束力来自“部署只认清单中记录的 digest”，而不是重新构建得到相同的 digest。

### 2.2 清单 `<RELEASE_ROOT>/<release_id>/manifest.json`

在 NX-032 §5.1 最小清单的字段之上，追加以下内容：

| 字段 | 说明 |
|---|---|
| `release_id` | `<UTC 日期>-<短 SHA>-<序号>`，只在内部使用 |
| `commit`, `parents`, `tree` | 提交、父提交和树的 SHA |
| `ci_result_digest`, `ci_policy_version` | NX-033 的 `result.json` 的 sha256，以及 policy 版本 |
| `images[]` | `name`、`digest`、`archive_sha256`、`base_digest`（来自 `versions.lock.json`）、`sbom_sha256` |
| `scan` | 每个镜像按严重级别统计的发现数，以及扫描数据库的版本或日期（D2）；只有数字，没有漏洞明细原文 |
| `catalog` | 迁移的最高版本、catalog digest，以及 `expand_only_since`（§5） |
| `runtime_store_format` | Pi 运行时存储格式的版本（Agent Host 声明），用于回滚判断 |
| `contracts_digest` | `packages/contracts` 的 digest |
| `source_archive_sha256` | `git archive` 的 SHA256 |
| `deadline_gate` | ADR-022 串行时限阶段的结论（沿用 NX-032） |
| `built_at`, `builder_version` | 构建时间和构建工具版本 |
| `signature` | 清单签名，见 §2.3 |

清单中只有哈希、digest、版本、计数和时间，不含主机信息、路径或测试日志原文。

### 2.3 签名

- **发布签名**：由 `nexloop-release` 持有的发布签名密钥 `<RELEASE_SIGNING_KEY>`（0600）对 `manifest.json` 签名，产生 `manifest.json.sig`。
  - 推荐使用 OpenSSH 的 `ssh-keygen -Y sign`，签名命名空间为 `nexloop-release`（D1）。它离线可用，不依赖外部透明日志。
  - 公钥 `release-signing.pub` 入库，供 doctor 和部署脚本校验。
- **部署时的校验**（NX-032 的 `--deploy-gate --release` 扩展），任一项不符都拒绝部署（AT-055）：
  1. 签名有效；
  2. `commit` 等于本地 bare 仓库 main 上的某个提交，并且有通过的 CI 结果（`ci_result_digest` 与 `<CI_RESULTS>/<sha>/result.json` 一致）；
  3. 每个 archive 的 SHA256 相符，`docker load` 后的 digest 等于清单中的值；
  4. Compose 渲染出来的每个 `*_IMAGE` 都是 `name@sha256:<清单 digest>`；
  5. 部署后运行中的容器 digest 等于 `current/manifest.json` 中的值（doctor `--release`）。
- **公开 tag**：签名用的是负责人自己的密钥（§4）。发布签名密钥只用于内部清单，不用于公开 tag。

### 2.4 归档与保留

- **目录**：`<RELEASE_ROOT>/<release_id>/`，内容为清单、签名、image archive、SBOM 和扫描摘要。写完后改为只读（文件 0444，目录 0555，属主 `nexloop-release`）；部署账户只能读。`current` 是指向当前部署的符号链接，由部署工具切换。
- **保留**（D3）：
  - 清单、签名、SBOM 和扫描摘要**永久保留**，体积很小；
  - image archive 保留最近 5 个发布，加上最近 90 天内部署过的所有发布，加上当前和上一个已知可用的发布。
  - 清理默认 dry-run，绝不删除 `current` 及其前一个已知可用的发布。
- **副本**：清单、签名和 SBOM 由 NX-035 的文件备份带到另一台主机。image archive 不备份；丢失后从同一 SHA 重新构建，得到的是**新的发布**（新的 release_id 和 digest），不冒充旧发布。

## 3. GitHub 同步（`make sync-github`）

### 3.1 推什么、不推什么

- **只推两类**：
  1. `main`，只快进；
  2. 负责人确认过的 tag `v*`（§4）。
- **永远不推**：其他任何分支（`incoming/*`、`integration/*`、`review/*`、`dispatch/*`、带 `local-only` 的 ref）。
- **禁止**：`--force`、`--mirror`、`--all`、`--tags`（批量推 tag）、删除 remote ref，以及改写 GitHub 上已有的 tag。

### 3.2 执行者与门槛

- **执行者**：同步任务 `nexloop-sync-github` 由 `nexloop-git` 运行，用一把只限这个仓库的 deploy key 推送（`<GITHUB_DEPLOY_KEY>`，0600，CI 和开发线都读不到）。
- **触发**：main 由合并控制器推进后触发（D4）。
- **推送前的门槛**，与开发机的 pre-push hook 是同一份受信脚本：
  1. 三项扫描。外发范围是 `github/main..main`；推 tag 时还要扫描 tag 对象和它指向的提交；受限标识的模式从私有文件读取（NX-033 §5.3），私有文件缺失时判失败；
  2. `github/main` 必须是 `main` 的祖先，即快进；
  3. 要推的 tag 指向的提交必须在 main 上，并且有负责人的签名（§4）；
  4. 本地 main 上的每个提交都有通过的 CI 结果。合并控制器已经保证了这一点，这里再核一遍。
- **推送后**：核对 `git rev-list --count github/main..main` 为 0，并把推送后的 `github/main` 哈希写进同步记录（AGENTS.md 要求的 task 证据从这里取）。

### 3.3 状态与积压（AT-053 的同步部分）

- **状态**：同步记录 `<SYNC_DIR>/state.json` 的状态值为 `synced`、`sync_pending`（网络失败）或 `sync_blocked`（门槛失败）。
  - 记录内容：积压的提交数、最早积压时间、最近一次失败代码（`network_unreachable`、`scan_failed`、`non_fast_forward`、`tag_unsigned` 等），只有代码，不含扫描命中的内容。
- **处理方式**：
  - **`sync_pending`**：按指数退避重试，最长 1 小时一次。期间不影响本地 CI、合并和部署。
  - **`sync_blocked`**：不重试，等维护者处理。扫描命中时按 AGENTS.md 的流程，从未推送的历史中清除后重新扫描。
- **告警**：积压超过 24 小时告警，作为 NX-030 告警规则的新增一条，读取同步记录的数字。
- **测试**：用一个本地 bare 仓库模拟 GitHub remote：
  - 阻断网络时为 pending，恢复后补推、积压清零；
  - 扫描命中合成的受限标识时为 blocked，并且 remote 不变；
  - 非快进、未签名 tag、批量推 tag 和 force 都被拒绝。

## 4. 公开 tag 与 GitHub Release（B 类操作）

- **候选**：由维护者提出：一个已在 stage 部署并通过 doctor 全套检查的 `release_id`，加上版本号 `vX.Y.Z`（D5）。
- **负责人本人确认**，确认内容要完整列出：版本号、提交 SHA、`release_id`、清单摘要、变更说明（CHANGELOG 中对应的一节），以及扫描结论（只有计数）。
  - 任何会话都不能代为确认，也不能自行创建或推送 tag。
  - 推荐由负责人在自己的机器上用自己的签名密钥执行 `git tag -s vX.Y.Z <sha>`（D6）；工具只负责生成命令、核对结果。
- **推送**：负责人确认并签名后，tag 进入本地 bare 仓库的 `refs/tags/v*`（只允许新建，不允许移动），由同步任务按 §3 推到 GitHub。
- **GitHub Release 页面**也属于 B 类，由负责人发布。附件只有：
  - 源码归档及其 SHA256；
  - 公开版清单：去掉内部 `release_id` 和路径，保留 digest 和 SBOM 摘要；
  - SBOM。

  **不附 image archive**（v0.1 不发布公开镜像，D7），也不附测试日志。

## 5. 回滚保护（AT-057）

部署工具执行回滚（目标是某个旧发布 T）之前，按以下顺序判断，任一项不满足都拒绝，并提示改走向前修复或 NX-035 的恢复流程：

1. **数据库 schema**：设库内的迁移最高版本为 H，T 的清单中 `catalog.head` 为 h。
   - `h == H`：允许回滚。
   - `h < H`：只有当 (h, H] 之间的每个迁移都在文件头声明了 `-- nexloop:expand-only`，并且 T 的清单中 `expand_only_since ≤ h` 时才允许（旧应用能在只增不减的新 schema 上运行）。
     - 迁移是不是 expand-only，在 PR 审查时人工核对，CI 只检查头部声明存在且格式合法；
     - 没有声明的迁移一律视为不兼容。
   - `h > H`：不是回滚，而是带迁移的升级，走正常的发布流程。
2. **Runtime 存储格式**：T 的 `runtime_store_format` 低于当前 Host 已写入的格式版本时拒绝。旧运行时文件保留，并标记为 blocked，不删除（12 §6）。
3. **契约**：T 的 `contracts_digest` 与当前不同、且契约版本主号更低时，提示人工确认是否有外部调用方依赖新契约。v0.1 没有外部调用方，所以只提示，不阻断。
4. **恢复围栏**：处于 NX-035 的恢复围栏中（state 不为 `open`）时，拒绝任何发布切换。
5. **清单校验**：T 的清单签名、归档完整性和 CI 结果，按 §2.3 全部重新校验。T 的 image archive 已被清理时拒绝；不能用重新构建的镜像冒充 T。

回滚本身是 B 类操作（NX-032 §7）。工具输出判断过程和将要执行的命令，由负责人确认后执行。

## 6. 命令契约（12 §8 中属于本任务的部分）

| 命令 | 作用 | 默认行为 |
|---|---|---|
| `make build-release SHA=<sha>` | §2.1 和 §2.2，生成并签名清单 | 只在 CI 通过时执行 |
| `make verify-release RELEASE=<id>` | §2.3 中的全部校验，不部署 | 只读 |
| `make deploy-stage RELEASE=<id>` | 调用 NX-032 的部署流程，含 §5 的回滚判断 | **dry-run**；实际执行属于 B 类，需要显式参数 `--confirmed-by-owner`，并只能在负责人当面确认时使用 |
| `make sync-github` | §3 | 只推 main；tag 只有在 §4 的签名存在时才推 |

所有命令都输出 JSON 结果，退出码 0 表示成功，1 表示门槛失败，2 表示配置或环境错误。失败时也保留证据，不中断。

## 7. 边界与 AT

| 事项 | NX-034 | 其他 |
|---|---|---|
| CI 结果、队列、隔离、合并控制器 | 读取并校验 | NX-033 |
| 最小清单、部署侧 digest 校验、部署步骤 | 扩展字段，补签名校验 | NX-032 |
| 备份与恢复、迁移前备份门槛 | 回滚时检查恢复围栏 | NX-035 |
| 开源文档、许可证、NOTICE、SBOM 的公开说明 | 产出 SBOM | NX-036 |

| AT | 覆盖方式 |
|---|---|
| AT-055 | 清单的 commit 不等于受信 main 上的提交、CI 结果不是通过、archive 哈希不符、load 后 digest 不符、签名无效、运行中容器 digest 不符，各自拒绝或判 doctor 失败（合成镜像在一次性环境中测试） |
| AT-057 | 在合成库上 `h < H` 且有未声明 expand-only 的迁移时拒绝；全部 expand-only 时允许；runtime 格式降级时拒绝；处于恢复围栏中时拒绝；目标 archive 已清理时拒绝 |
| AT-053（同步部分） | §3.3 |
| 其他 | 只推 main 和已签名的 tag；force、mirror、删除 ref 和批量推 tag 被拒；扫描命中时 blocked 且 remote 不变；推送后积压为 0 并记录 `github/main` 哈希；清理不碰 `current` 及其前一个发布 |

## 8. 参数

`<RELEASE_ROOT>`、`<RELEASE_SIGNING_KEY>`、`<CI_RESULTS>`、`<SYNC_DIR>`、`<GITHUB_REMOTE_URL>`、`<GITHUB_DEPLOY_KEY>`、`<PRIVATE_SCAN_PATTERNS>`，以及负责人签名公钥的指纹（存放在私有配置中，公开仓库只放公钥文件）。

## 9. 待负责人决定的事项（D 项）

- **D1 发布清单的签名工具。**
  - **推荐**：OpenSSH 的 `ssh-keygen -Y sign/verify`：系统自带，离线可用，不需要额外服务。
  - **替代**：cosign（生态好，但无密钥模式依赖在线透明日志，与离线优先冲突；有密钥模式则与 SSH 签名能力相近）、minisign（多一个依赖）。
- **D2 SBOM 与漏洞扫描。**
  - **推荐**：syft 生成 SPDX JSON，grype 扫描；两者都固定版本并写入 `versions.lock`，漏洞库在依赖阶段更新并记录日期。
    - 扫描结果**不自动阻断**发布，但“严重级别、且已有修复版本”的发现必须在负责人确认 tag 时列出（§4）；
    - stage 发布不受扫描结果阻断。
  - **替代**：trivy（一个工具同时做 SBOM 和扫描；在离线场景下同样需要维护漏洞库）。
- **D3 归档保留。**
  - **推荐**：清单、签名和 SBOM 永久保留；image archive 保留最近 5 个发布，加上 90 天内部署过的发布，加上 `current` 和前一个已知可用的发布。
  - **替代**：保留全部 image archive（磁盘占用随发布次数线性增长）。
- **D4 main 推送到 GitHub 的触发方式。**
  - **推荐**：合并控制器推进 main 之后自动同步，经过 §3.2 的全部门槛。这与 AGENTS.md 现行的“门槛通过后可以推 main”一致，只是由人工改为机器执行。
  - **替代**：维护者手动执行 `make sync-github`（多一步人工，但每次推送都有人看过）。
- **D5 版本号。**
  - **推荐**：SemVer，0.x 阶段从 `v0.1.0` 开始；内部 stage 发布只用 `release_id`，不打 tag。
  - **替代**：日历版本，例如 `v2026.10.0`。
- **D6 公开 tag 的签名人与执行地点。**
  - **推荐**：由负责人在自己的机器上用自己的密钥签名 tag，工具只生成命令、做核对；tag 推入本地 bare 仓库后由同步任务推送。
  - **替代**：由发布工具在 APP_HOST 上用专用 tag 签名密钥签名（更自动，但公开 tag 的签名人不再是负责人本人）。
- **D7 v0.1 是否发布公开镜像。**
  - **推荐**：不发布（不推 GHCR），社区用户从源码构建；GitHub Release 只附源码归档、公开版清单和 SBOM。
  - **替代**：同时把镜像推到 GHCR（需要 registry 凭据，以及 digest 公开后的维护承诺）。
