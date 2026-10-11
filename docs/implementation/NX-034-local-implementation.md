# NX-034 本地部分：实现说明

依据：`NX-034-design.md`（D1–D7 已定），以及 NX-032 D4“先做最小发布清单”。分支 `nx034-local`，从 main `c835158` 切出，没有迁移。

**范围限定**：本部分只在执行它的机器上运行。它不会 push、打 tag、推送镜像，也不会连接任何服务器。这些属于 B 类操作，或者依赖 NX-032、NX-033。

## 1. 工具 `scripts/release/release.py`（只用标准库）

| 子命令 | 作用 | 对应 |
|---|---|---|
| `manifest --sha --images --ci-result --out [--sequence]` | 从构建产物生成 `manifest.json`，内容见下文 | §2.2；NX-032 §5.1 |
| `sign --release-dir --key` | `ssh-keygen -Y sign -n nexloop-release`，生成 `manifest.json.sig` | §2.3；D1 |
| `verify --release-dir --allowed-signers --deploy-sha [--deploy-image name=sha256:…]…` | 验签，并核对“部署的 SHA 和镜像 digest 等于测过的清单” | AT-055 |
| `sbom --release-dir` | 用 syft 生成 SPDX，用 grype 统计漏洞数，写回清单的 `scan` | D2 |
| `rollback-check --current --target [--database-head] [--runtime-format]` | 拒绝盲目回滚，并给出向前修复的提示 | §5；AT-057 |

- 每个子命令只输出一个 JSON 对象。
- 退出码：
  - 0：通过；
  - 1：某项检查不通过，拒绝；
  - 2：输入或环境错误；
  - 3：缺少必需的外部工具。**缺少工具从不当作通过**。

### 清单内容（只有哈希、digest、版本、计数和时间）

- **构建身份**：`release_id`（`<UTC 日期>-<短 SHA>-<序号>`）、`commit`、`parents`、`tree`。
- **测试来源**：
  - `ci_result_digest`：CI 结果文件的 sha256。该文件必须写明**这个提交**，并且 `status=passed`，否则拒绝生成清单；
  - `ci_policy_version`、`deadline_gate`、`tests`（计数）。
- **镜像**：`images[]` 每项包含 `name`、`digest`、`archive`（发布目录内的副本）、`archive_sha256`、`base_digest`（来自 `versions.lock.json`）和 `sbom_sha256`。
  - 如果 OCI 归档的 `index.json` 声明了 digest，必须包含输入给出的 digest，否则拒绝。
- **迁移 catalog**：`head`、`count`、`digest`（`(version, checksum)` 列表的 sha256），以及 `expand_only_since`（见 §2）。
- **运行时与源码**：
  - `runtime_store_format`：取 Pi Durable 的 `SQLITE_MIGRATIONS` 最高版本（当前为 1）；
  - `contracts_digest`、`locks`（`versions.lock.json`、`uv.lock`、`pnpm-lock.yaml` 各自的 sha256）、`source_archive_sha256`（`git archive` 的 sha256）。
- **其他**：`scan`、`built_at`、`builder_version`。
- **发布目录不可覆盖**：目录已存在就拒绝；需要重做时，换一个序号生成新的发布。

### 签名与验签（D1）

- 测试只用临时生成的 ed25519 密钥，不读取任何真实密钥。
- 验签使用 allowed_signers 文件，身份和命名空间都是 `nexloop-release`。
- 签名之后再修改清单、换其他签名者、或者没有签名，都会拒绝。
- 发布签名私钥的保管和公钥是否入库，属于部署事项（NX-032），本部分不涉及。

### SBOM 与漏洞扫描（D2）

- `versions.lock.json` 新增 `release_tools` 段：syft 1.18.1、grype 0.86.1、OpenSSH ≥ 8.2。
  - **二进制 digest 待在 CI 主机上首次安装官方发布包时记录**（状态为 `pinned_version_binary_digest_pending_ci_install`）；
  - 届时核对这两个版本号的官方发布确实存在。
- 工具缺失时报 `not_installed`，退出码 3；版本与锁定值不一致时拒绝。
- 清单中只写各严重级别的计数，以及“Critical 且已有修复版本”的数量，不写漏洞描述原文。
- 按 D2，扫描结果**不自动阻断**发布：负责人确认 tag 时会列出“Critical 且已有修复版本”的数量（NX-034 §4）。

## 2. 回滚保护（AT-057）

`rollback-check` 依次判断。任一项不满足就拒绝，并给出提示：

1. **目标的迁移 head（h）大于当前库的 head（H）**：这不是回滚，而是升级，提示走正常发布流程。
2. **h 小于 H**：(h, H] 之间的每个迁移，都必须在文件头声明 `-- nexloop:expand-only`。
   - 有任何一个没有声明，就拒绝，提示“向前修复：在当前 schema 上发布修正版本，不跑 down 迁移”；
   - 当前检出不认识库的 head 时，同样拒绝。
3. **目标的 `runtime_store_format` 低于当前值**：拒绝，提示保留新格式的运行时文件、把受影响的 Run 标为 blocked，再向前修复。

- **现状**：已发布的迁移都没有声明 expand-only，所以只要回滚目标的迁移 head 低于当前库，就会被拒绝。这是有意的保守口径：迁移是否只增不减，要在 PR 审查时人工核对后声明，CI 只检查声明的格式。
- **当前库 head 的来源**：默认取当前发布清单中的 `catalog.head`。部署时由部署工具从数据库读取后，通过 `--database-head` 传入（NX-032）。

## 3. 测试

`tests/test_release_local.py`，共 5 个，全部在临时目录中运行：

- **清单**：字段齐全，其中 catalog、运行时格式、锁文件、镜像 archive 哈希、测试计数都与输入一致；清单中不出现本机路径；发布目录不可覆盖。
- **拒绝生成清单**：CI 结果不是通过、或不是这个提交；OCI 归档中声明的 digest 与输入不一致。
- **签名与 AT-055**：
  - 未签名、提交 SHA 不同、镜像 digest 不同、缺少镜像、出现清单外镜像、archive 被替换、换其他签名者、清单被修改，都会拒绝；
  - 正确的部署通过。
- **SBOM 与扫描**：
  - PATH 中没有工具时退出码为 3（`not_installed`）；
  - 用锁定版本号的替身工具时，计数正确写入清单，且不含漏洞描述原文；
  - 工具版本不符时拒绝。
- **回滚**：同一 schema 允许；中间有未声明 expand-only 的迁移时拒绝并给出提示；只跨越 expand-only 迁移时允许；运行时格式降级、目标更新、库 head 超出当前检出时，都拒绝；CLI 退出码为 1。

## 4. 未做（需要前提）

- 镜像的实际构建、`docker load` 后 digest 的核对、运行中容器的核对（doctor `--release`）：需要 NX-032。
- CI 结果文件由 NX-033 的流水线产出；本部分先接受“写明这个提交且已通过”的 JSON 文件。
- 归档目录的只读权限与保留清理、GitHub 同步、公开 tag：都属于 B 类或依赖 NX-033。
