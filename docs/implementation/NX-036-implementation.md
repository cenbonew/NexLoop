# NX-036 开源文档与供应链检查：实现说明

分支 `nx036-impl`，从 main `3b5a684` 切出。设计稿 `NX-036-design.md`（已合入）。负责人 2026-10-11 对 D1–D12 回复“全部按推荐”（调度员转达），即设计稿 §8 中每项的推荐值；实现开工时按推荐值暂行，定案后配置值无需改动。所有受 D 项影响的值都放在版本化配置文件里，代码中不写死；将来如要调整，只改配置（以及配置所对应的文档措辞，`check_public_files.py` 会提示两者不一致）。

## 1. D 项（负责人 2026-10-11 定案（全部按推荐））

| D 项 | 定案值 | 所在配置 |
|---|---|---|
| D1 贡献签署 | DCO，`scripts/check_signoff.py` | `deploy/compliance/public-release.v1.json` `contribution_signoff` |
| D2 安全报告渠道 | GitHub 私密漏洞报告，另可加一个联系方式；`security_contact` 为 null，public 门槛失败 | 同上 `security_contact`、`placeholder` |
| D3 响应时限 | 确认 3 个工作日、初评 10 个工作日、修复 Critical 30 天、High 60 天（目标非保证） | 同上 `response_targets_days` |
| D4 支持版本 | 1.0 之前只支持最新的一个发布 | 同上 `supported_releases` |
| D5 漏洞阈值 | stage 不阻断，列出 Critical/High 且有修复；public 阻断未豁免的 Critical 且有修复，列出 High 且有修复 | `deploy/compliance/vulnerability-policy.v1.json` `thresholds` |
| D6 豁免 | 最长 90 天，负责人批准，到期或格式不对即失败（stage 与 public 都失败） | 同上 `waiver_max_days`、`waivers` |
| D7 弱 copyleft | 只能按包逐个列入（名称、版本模式、范围、用途、复审日期） | `deploy/compliance/license-policy.v1.json` `conditional` |
| D8 第三方声明 | `THIRD_PARTY_NOTICES.md` 生成并检查漂移 | 同上（生成规则在脚本中） |
| D10 行为准则 | Contributor Covenant 2.1，以引用方式采用 | `public-release.v1.json` `code_of_conduct` |
| D11 秘密扫描工具 | 不新增 | `public-release.v1.json` `secret_scanning_tool: null` |
| D12 漏洞库时效 | public 发布时漏洞库不超过 7 天 | `vulnerability-policy.v1.json` `database_max_age_days` |

D9（不改写历史；历史中出现的凭据、私钥或受限网络标识一律作废并记录；审计结论经负责人确认后才做首个 public release）不是配置项：本任务没有改写历史，`public-preflight` 在没有上一次公开发布时失败，首个公开发布需要全历史审计与负责人确认（见 §5）。

## 2. 许可合规（D7、D8）

- `scripts/check_licenses.py`：
  - **清单**：
    - Python 来自 `uv.lock`：运行时范围是自有包的依赖闭包（含 extra），其余为 dev。
    - Node 来自 `pnpm-lock.yaml`：运行时范围从各 importer 的 dependencies 沿 snapshots 闭包，其余为 dev。
    - 另外检查 vendored 源码（`PROVENANCE.json` 中的提交与逐文件哈希、`NOTICE` 写明仓库与锁定提交、`licenses/` 文本哈希、`packages/eios-core/NOTICE` 与根 `NOTICE` 一致）、项目 `LICENSE`（MIT，ADR-026）和受跟踪的自有资产（图片、字体、音视频必须登记归属）。
  - **不猜测许可证**：
    - 元数据缺失或含糊（如只有 “BSD License” 分类）时，必须有澄清记录，并附所依据许可证文件的 sha256。
    - 许可证不在名单内即失败。
    - SPDX 表达式中，OR 取最好的一项，AND 取最差的一项。
  - **本平台未安装的锁定包**：例如其他操作系统或 CPU 的二进制包。每个都要有离线记录，记录绑定锁文件里的确切制品哈希（wheel `sha256:` / npm `integrity`），并给出恰好一种许可证文本证据：
    1. 制品自带许可证文件的哈希；
    2. 同项目、已安装主包的许可证文件（检查时核对哈希）；
    3. 经人工复核的“制品不含许可证文本”说明，该说明会在声明文件中单列。
  - **记录的生成**：`--record-off-platform`（联网，只在依赖阶段使用）下载确切制品并核对哈希，打印记录草稿，不改策略文件；记录由人工复核后写入。
  - **退出码**：0 通过；1 违反策略；2 输入错误；3 清单不完整（例如锁定包既未安装也无记录），退出码 3 永远不算通过。
- 澄清记录（httpx 0.28.1 只有 “BSD License” 分类）：依据 `httpx-0.28.1.dist-info/licenses/LICENSE.md`，sha256 `4ec59d544f12b5f539a3a716fd321ac58ccd8030b465221f2c880200cdf28d8d`，三条款 BSD，记为 BSD-3-Clause。
- 平台外记录共 60 条：
  - Python 2 条：colorama 只有 BSD 分类，许可证文件为三条款文本，记为 BSD-3-Clause；tzdata 的核心元数据为 `License: Apache-2.0`。两条都经人工复核并写明依据。
  - Node 58 条：其中 41 个 esbuild / rolldown 平台包本身不带许可证文本，引用同版本已安装主包的许可证文件；`@napi-rs/wasm-runtime` 不带许可证文本，单列说明；11 个 lightningcss 平台包是 MPL-2.0，逐个列入 `conditional`（dev）。
- 当前结果：274 个第三方包，257 个 allowed，17 个 conditional。17 个 conditional 是 psycopg 3 个（LGPL-3.0-only，运行时）、certifi、hypothesis、lightningcss 及其 11 个平台包（MPL-2.0，dev）。弱 copyleft 条目的复审日期都是 2027-04-11。
- `THIRD_PARTY_NOTICES.md` 由 `--write-notices` 生成，`--check` 比对漂移；`scripts/ci/check` 在 `pnpm install` 之后运行 `--check`。

## 3. 漏洞策略与 SBOM（D5、D6、D12）

- `release.py sbom`（NX-034）：
  - 仍要求锁定版本的 syft/grype，工具缺失或版本不对时不会通过（退出码 3，或拒绝）。
  - 现在还会把每个镜像的发现写成 `sbom/<image>.findings.json`，只含标识：编号、包、版本、严重度、修复状态、范围，不含漏洞描述。
  - 按 stage 类别应用策略，结果写入 `manifest.scan.policy`。
  - 豁免过期或无效时拒绝。
- `evaluate_vulnerabilities`：按类别产出 blocking / listed / waived；未被任何发现用到的豁免列入 cleanup；public 类别缺少漏洞库时间或超过 7 天即失败。
- 源码 SBOM：对 clean checkout 运行 `syft scan dir:`，再用 grype 扫描该 SBOM；SBOM 中的 pypi/npm purl 与许可清单逐项交叉核对（排除自有包），两边任何一边多出都失败。

## 4. 公开文件与 CI 隔离（D1、D2、D3、D4、D10、D11）

- `CONTRIBUTING.md`：本地 CI 为权威；只用合成数据；DCO 与 `check_signoff.py`；外部 PR 的运行规则（AT-054）。
- `SECURITY.md`：渠道参数 `<SECURITY_CONTACT>`，未填写时写明尚无正式响应渠道；D3 目标、D4 支持版本、13 §10 处置流程；历史中出现过的凭据一律轮换（D9）。
- `CODE_OF_CONDUCT.md`：以引用方式采用 Contributor Covenant 2.1（不复制全文），报告联系方式同为参数。
- `scripts/check_public_files.py`：
  - 默认（本地 CI）模式：检查已有的公开文件，以及文档写明的值与配置一致（D3/D4/D10）、DCO 说明、LICENSE 为 MIT。
  - `--public`：另外要求 `README.zh-CN.md`、`GOVERNANCE.md`、`docs/release/public-release-checklist.md` 存在，README 双向链接，文档中没有占位符，且 `security_contact` 已填写。
- `scripts/check_ci_isolation.py`（AT-054 静态部分）：`.github/` 下的工作流不得使用 `pull_request_target`，不得引用 `secrets`（含 `secrets: inherit`），顶层权限必须声明且只读（`read-all`、`{}` 或只有 read/none），job 级权限同样只读。目前仓库没有工作流，检查通过。AT-054 的网络隔离部分仍引用 NX-032/NX-033 的探测证据。
- `scripts/check_signoff.py <base> <head>`：区间内每个非合并提交须有与作者姓名、邮箱一致的 `Signed-off-by`。现有历史没有签署，DCO 自采用之日起适用；本分支的提交带签署。

## 5. 发布对象扫描与 public-preflight

- `check_publication.py --tree <ref>`：扫描提交整棵树的每个 blob；子模块和符号链接直接报告，不跟随。`--archive <file.tar>`：扫描源码归档的每个成员，非普通文件即报告。两者规则与暂存区模式相同，输出不复述命中内容。
- `release.py public-preflight --ref <ref> --out <dir> [--previous-public <ref>] [--release-dir <dir>]` 的 9 步。每一步都会执行并记录，任一步失败即整体失败：
  1. clean checkout：对 ref 运行 `git archive` 并解压（只含受跟踪文件），再做锁定安装（`uv sync --frozen`、`pnpm install --frozen-lockfile`）；
  2. `check_licenses.py --check --report`；
  3. `check_public_files.py --public`；
  4. `check_ci_isolation.py`；
  5. `check_publication.py --tree` 与 `--archive`；
  6. `prepush-scan.sh <ref> <上一次公开发布>`；没有上一次公开发布时本步失败（首次公开发布需要设计稿 §5 的全历史审计与负责人确认，门槛不能代替）；
  7. 源码 SBOM 与许可清单交叉核对；
  8. public 类别的漏洞阈值与豁免；给出 `--release-dir` 时合并镜像发现，但该目录下没有发现文件时失败；
  9. 给负责人确认的摘要：阻断、列出、已豁免与待清理的发现编号，以及条件放行的许可证清单。
- 各步使用被发布 ref 自己的脚本与策略。报告写入 `<out>/preflight.json`，并输出它的 sha256。命令缺失按失败处理（不算通过）。
- 本工具不创建 tag 或 GitHub Release（B 类操作，由负责人本人执行），也不改写历史。

### 5.1 首个公开 release 前的全历史审计（`scripts/release/history_audit.py`）

- **用法**：`python scripts/release/history_audit.py [<ref>] [--all-refs] --json <私有证据目录>/history-audit.json --markdown <私有证据目录>/history-audit.md`
  - 默认 ref 为 HEAD；`--all-refs` 扫描全部本地分支与 tag。
  - 报告文件以 0600 新建，已存在时拒绝（退出码 3）。报告存入负责人的私有证据目录，不入库。
- **范围**：ref 可达的全部提交。合并提交按每个父提交分别取差异（`git diff-tree -m`），根提交与空树比较。
  - 每个新增或修改的 blob（按 blob 与路径去重）：直接调用 `check_publication.violation`（同一套规则：私有路径、私网地址、私钥、模型凭据；含 NUL 的 blob 视为二进制，只检查路径；没有大小上限，与它一致）；
  - 同一 blob 的文本与每个提交消息：再应用 `prepush-scan.sh` 的两条规则：
    - 受限标识：名单位置与 prepush-scan 相同（主克隆的 `local-only/restricted-identifiers.txt` 或 `NEXLOOP_RESTRICTED_IDENTIFIERS_FILE`），缺失或为空即失败；
    - 凭据值：表达式在运行时从 `prepush-scan.sh` 中读出，不另存一份。
- **输出**：只有类别、规则编号（受限标识按名单行号，如 `identifier:3`）、路径或“commit message”、引入它的提交、首次与末次提交、ref 当前树中该路径是否仍命中。不复述命中内容。
- **分类与处置（D9）**：
  - 须轮换或作废：`credential`、`private_key`、`restricted_network_identifier`；
  - 接受留在历史：`host_alias`、`local_path`、`hardware`、`other`（如私有路径规则）。
  - 受限标识的类别：名单中可用 `# [host_alias]`、`# [local_path]`、`# [hardware]`、`# [restricted_network_identifier]` 分节标注；未标注时按形状判断（地址或主机指纹为 `restricted_network_identifier`，含 `/` 为 `local_path`，其余为 `host_alias`）。
- **退出码**：0 无命中；1 仅有可接受类；2 存在须轮换类；3 输入错误（ref 无效、名单缺失或为空、报告文件已存在）。
- **注意**：凭据值规则与 prepush-scan 共用，也会命中“`MODEL_API_KEY=` 后接变量名”这类环境变量引用。报告只给出位置，需要人工判断是否为真实凭据，不能据此自动轮换。

## 6. 测试（合成数据，本地，不联网）

- `tests/test_license_compliance.py`（28）：合成锁文件与策略。覆盖：
  - 依赖闭包的范围判定、声明文件漂移；
  - 元数据缺失或含糊不猜测，澄清记录的哈希核对；
  - 平台外记录绑定制品，以及三种证据；
  - 弱 copyleft 的逐包条目（版本、范围、复审日期）；
  - 表达式分类与畸形表达式；
  - 资产登记、项目 LICENSE 改动；
  - 仓库 vendored 源码一致，篡改可被发现；
  - 策略形状严格，策略中的平台外记录与锁文件一致。
- `tests/test_release_compliance.py`（25）：
  - 策略定案值；stage 列出不阻断，public 阻断；
  - 豁免只覆盖对应的发现；过期、超期限、未来日期、格式错误都失败；
  - 漏洞库时效；发现只含标识；
  - `sbom` 写出发现，豁免过期时拒绝，缺工具仍判失败；
  - SBOM 与许可清单的交叉核对；
  - public-preflight 9 步全部通过时的摘要；任一步失败、首次发布无基线、缺工具、漏洞库过期、清单不一致、有阻断发现、发布目录无镜像发现都失败；缺工具时 CLI 拒绝。
- `tests/test_public_repository_checks.py`（18）：
  - CI 隔离的各种违规形式；
  - DCO（未签、他人签署、合并提交豁免、参数注入）；
  - 公开文件的两种模式与配置值漂移；
  - `--tree` / `--archive` 能发现 diff 之外的私有路径、私网地址与符号链接。

## 7. 已知限制与待办

- `README.zh-CN.md`、`GOVERNANCE.md`、`docs/release/public-release-checklist.md` 由 L5 写作；在此之前 `check_public_files.py --public` 失败（预期如此）。13 §7 要求 `README.md` 为英文、`README.zh-CN.md` 为中文，而现有 `README.md` 是中文，需由 L5 调整。
- 设计稿 §6 中 stage 发布的许可检查与 SBOM 交叉核对（不阻断）：已在 NX-034 第二部分接入（`release.py preflight --category stage`，见 `NX-034-part2-implementation.md`）。
- 弱 copyleft 条目的用途说明写的是“源码发布不再分发”。镜像（内部，不公开，NX-034 D7）中含有 psycopg-binary 等 wheel；镜像公开前须重新评估分发义务（调度员 2026-10-11：本次不做）。
- public-preflight 与 stage 预检的结果：NX-034 第二部分改为在签名前写入清单的 `preflight` 块，签名覆盖它，`verify` 核对报告文件与摘要（见 `NX-034-part2-implementation.md`）。
- `--record-off-platform` 依赖 PyPI 与 npm registry 的公开下载地址；记录写入后检查完全离线。锁文件更新时需重新生成并复核记录（锁中出现无记录的未安装包会以退出码 3 失败）。
