# NX-036 开源文档与供应链检查：设计稿（待审，未实现）

分支 `nx036-design`，从 main `aefe1fb` 切出。只写设计。

- 任务：NX-036「完成开源文档和供应链检查」，S5，模块 M23，依赖 NX-034、NX-035；交付物“README 双语、许可/NOTICE/SBOM/贡献/安全/操作手册”（`planning/tasks.json`）。
- 分工：README（中英）与操作手册由 L5 写作；本稿只覆盖**能检查、能测试**的部分：许可合规、SBOM 与漏洞、贡献与安全政策、公开仓库卫生，以及把它们接进发布门槛。
- 依据：`docs/handoff/docs/13_SECURITY_PRIVACY_AND_OPEN_SOURCE.md` §6–§10；`14_TEST_ACCEPTANCE.md`（AT-054、AT-055）；`17_OPERATIONS_RUNBOOK.md`；`adr/ADR-026-license-mit.md`（项目保持 MIT）；`NX-034-design.md` D2/D7 与 `NX-034-local-implementation.md`（`release.py sbom`）；`NOTICE`、`licenses/`、`versions.lock.json`；`AGENTS.md`（本地 CI 权威、公开远程、推送前扫描）。
- 本稿中的主机、地址、邮箱一律用参数表示（`<SECURITY_CONTACT>`、`<RELEASE_ROOT>` 等），不写真实值。

## 1. 现状核对

| 项 | 现状（main `aefe1fb`） | 缺口 |
|---|---|---|
| 项目许可 | 根 `LICENSE` 为 MIT（“NexLoop contributors”）；ADR-026 确认保持 MIT | — |
| NOTICE | 根 `NOTICE` 列出 NEX-EIOS（负责人授权、MIT 分发、无上游 LICENSE）、Pi（MIT）、EvoOntology（MIT）；`packages/eios-core/NOTICE` 为同文副本 | 没有检查两份是否一致、是否覆盖 `versions.lock.json` 的全部上游 |
| `licenses/` | `Pi-MIT.txt`、`EvoOntology-MIT.txt` | 依赖的许可证文本没有汇总（13 §7 要求 `THIRD_PARTY_NOTICES.md`） |
| vendored 代码 | `packages/eios-core`：`PROVENANCE.json`（上游提交、逐文件 sha256、适配记录，license 字段为 MIT 授权说明）；`vendor/pi`：`PROVENANCE.json`（源码未改，保留 MIT）、4 个包均声明 MIT；EvoOntology 目前只有设计参照（`context_engine/semantic.py` 注释说明输出形状参照其 SemanticLayer），没有复制代码 | 没有自动核对“PROVENANCE 记录 ↔ 文件哈希 ↔ NOTICE ↔ licenses/”；Pi 源码由 `scripts/check_pi_source.py` 核对，EIOS 内核没有同类检查 |
| Python 依赖 | `uv.lock` 45 个包（实测已安装元数据）：MIT 24、BSD 8、Apache-2.0 及组合 3、PSF-2.0 1、MIT-0 1，**LGPL-3.0-only 3**（`psycopg`、`psycopg-binary`、`psycopg-pool`，运行时依赖），**MPL-2.0 2**（`certifi`、`hypothesis`，都只在 dev 依赖闭包里，不进入运行时），另有以分类器表示、未写 SPDX 的若干项（如“BSD License”） | 没有清单、没有策略、没有检查 |
| Node 依赖 | `pnpm-lock.yaml`，已安装 455 个包：MIT 227、Apache-2.0 164、0BSD 27、BSD-3-Clause 26、**MPL-2.0 5**（`lightningcss` 及平台包，web 构建期）、ISC 4、Unlicense 2（`fast-sha256`） | 同上 |
| SBOM 与漏洞 | `release.py sbom --release-dir`：syft 生成镜像 SPDX、grype 统计各严重级别数量和“Critical 且有修复”数量，写入清单；工具版本锁在 `versions.lock.json` 的 `release_tools`（syft 1.18.1、grype 0.86.1），缺工具报 `not_installed`、退出码 3；按 NX-034 D2 扫描**不自动阻断**，tag 确认时列出 | 没有源码级 SBOM；没有阈值与豁免流程；SBOM 与许可清单没有交叉核对 |
| 公开文件 | 只有 `README.md`、`LICENSE`、`NOTICE`、`CHANGELOG.md`、ADR | 缺 `README.zh-CN.md`、`THIRD_PARTY_NOTICES.md`、`CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`、`SECURITY.md`、`GOVERNANCE.md`、贡献签署说明（13 §7） |
| 仓库卫生 | `scripts/check_publication.py`（扫描暂存区：私有路径、内网地址、私钥、模型密钥赋值）在 `scripts/ci/check` 里运行；`scripts/ci/prepush-scan.sh`（受跟踪的私有/运行时路径、每个待推送提交里的受限主机标识、新增行里的凭据值）作为 pre-push 钩子 | 两者都只看“当前暂存”或“待推送区间”，没有“发布对象（tag 的源码归档）”和“全部历史”的模式 |
| CI 与外部 PR | 本地 CI 权威，仓库没有 `.github/workflows`；GitHub 只是同步远程 | 没有书面政策，也没有检查防止将来加入会在有秘密的环境里自动运行外部 PR 的工作流（AT-054） |

## 2. 许可合规

### 2.1 清单范围

检查脚本对四类来源逐个列出（包名、版本、来源、作用域、SPDX 表达式、许可证文本位置）：

1. **Python**：`uv.lock` 中的每个包。许可信息取自 `uv sync --frozen` 之后的已安装元数据（`License-Expression`，没有时取 `License ::` 分类器）。作用域按 `pyproject.toml` 区分 `runtime`（`nexloop-eios-core` 的依赖闭包）与 `dev`（dev 组）。锁里有、环境里找不到的包算“清单不完整”。
2. **Node**：`pnpm-lock.yaml` 中的每个包，取 `node_modules/.pnpm/<id>/node_modules/<name>/package.json` 的 `license`；作用域按 importer 的 `dependencies` / `devDependencies` 区分 `runtime` / `build` / `dev`（web 前端的构建工具属于 `build`：只用于构建，不进入运行产物）。
3. **vendored**：`packages/eios-core`（`PROVENANCE.json` 的 license 与逐文件哈希）、`vendor/pi`（`PROVENANCE.json`、各包 `package.json`、`licenses/Pi-MIT.txt`）。核对哈希与仓库文件一致、`NOTICE` 写有该上游及其提交、`licenses/` 中有对应文本。`versions.lock.json` 的每个 `upstream_sources` 条目都必须在 `NOTICE` 中出现（EvoOntology 当前为 `selective_reuse`，仍要求 NOTICE 与许可证文本存在）。
4. **仓库自有资产**：`docs/design` 等目录下的图片、图标、字体。默认视为项目自有（MIT）；若引入第三方资产，必须在策略文件里逐项登记来源与许可证。

镜像里的操作系统包不在本检查范围：按 NX-034 D7，镜像不公开发布，社区从源码构建；镜像内容由 SBOM 记录（§3）。

### 2.2 规范化

- 把非 SPDX 写法映射为 SPDX 标识（“MIT License”→`MIT`、“Apache Software License”→`Apache-2.0`、“Mozilla Public License 2.0 (MPL 2.0)”→`MPL-2.0`、“Python Software Foundation License”→`PSF-2.0`）。映射表放在策略文件里，版本化。
- 含义不确定的分类器（如只写“BSD License”）**不猜**：必须在策略文件中为该包写一条澄清记录（包、版本范围、确认后的 SPDX 标识、所依据的许可证文件路径及其 sha256、确认日期），否则按未知处理。
- 表达式：`A OR B` 只要有一项允许即可；`A AND B` 要求全部允许；`WITH` 例外按整体标识登记。

### 2.3 策略文件（版本化配置）

`deploy/compliance/license-policy.v1.json`，字段：`schema`、`version`、`decision`（引用 ADR-026 与本稿的 D 项裁定）、以及：

- `allowed`：宽松许可，任何作用域都可：`MIT`、`MIT-0`、`BSD-2-Clause`、`BSD-3-Clause`、`Apache-2.0`、`ISC`、`0BSD`、`PSF-2.0`、`Python-2.0`、`Unlicense`、`Zlib`、`CC0-1.0`、`BlueOak-1.0.0`。
- `conditional`：弱 copyleft（`LGPL-2.1-only`、`LGPL-2.1-or-later`、`LGPL-3.0-only`、`LGPL-3.0-or-later`、`MPL-2.0`）。只对**逐包登记**的条目放行：包名、允许的版本范围、作用域、使用方式（必须是“未修改、作为独立库使用”，不得 vendored 或修改）、理由、复核日期。当前应登记：`psycopg`、`psycopg-binary`、`psycopg-pool`（LGPL-3.0-only，运行时）、`certifi`、`hypothesis`（MPL-2.0，dev）、`lightningcss` 及其平台包（MPL-2.0，build）。
- `denied`：强 copyleft 与非开源许可（`GPL-*`、`AGPL-*`、`SSPL-1.0`、`BUSL-1.1`、带 Commons Clause 的组合、`CC-BY-NC-*`、专有或 `UNLICENSED`），以及 `NOASSERTION`、`UNKNOWN`。
- `clarifications`：§2.2 的澄清记录。
- `assets`：仓库内第三方资产的登记（当前为空）。

与 MIT 的兼容判断（写入策略文件的 `decision`，由负责人确认，见 D7）：

- 宽松许可与 MIT 兼容，分发时保留原版权与许可声明。
- LGPL：NexLoop 以 Python 导入的方式使用 psycopg，不修改、不静态合并，项目代码可以继续采用 MIT。义务落在**分发这些库的一方**：源码发布只引用依赖、不附带其二进制，社区用户从 PyPI 安装；若将来分发含这些库的镜像，须随镜像提供 LGPL 文本并保持库可替换（镜像内的 wheel 自带许可证文件；SBOM 记录版本）。`psycopg-binary` 的 wheel 内含 libpq 与 OpenSSL 等组件，其许可证随 wheel 提供，由 SBOM 记录。
- MPL-2.0：文件级 copyleft；不修改这些文件时与 MIT 项目并存没有问题。
- Unlicense 视为宽松许可。

### 2.4 检查脚本

`scripts/check_licenses.py`，本地与本地 CI 都可以运行，不联网（读取依赖阶段已安装的元数据）：

- `--check`：任何 `denied`、未知、未登记的 `conditional`、作用域或版本超出登记范围、澄清记录对应的文件哈希不符、复核日期已过期，都使退出码为 1；清单不完整（锁里的包找不到元数据）退出码为 3，**从不视为通过**。
- vendored 检查同时失败于：PROVENANCE 中的文件哈希与仓库不一致、`NOTICE` 缺上游条目或提交号不符、`licenses/` 缺文本、`packages/eios-core/NOTICE` 与根 `NOTICE` 不一致（或不再是声明过的副本）、`LICENSE` 不再是 MIT 文本。
- `--write-notices`：从清单生成 `THIRD_PARTY_NOTICES.md`（按作用域分组：包、版本、SPDX、许可证文本来源；条件放行的条目附上义务说明）。`--check` 会比较生成结果与仓库中的文件，不一致即失败（防止新增依赖后忘记更新）。
- `--report <path>`：输出 JSON 清单，供发布目录与 SBOM 交叉核对（§3）。
- 输出只含包名、版本、许可证标识和计数，不含任何路径之外的本地环境信息。

接入：`scripts/ci/check` 在依赖阶段之后运行 `--check`；发布门槛（§6）再运行一次，结果写入发布清单。

### 2.5 测试

全部使用合成的锁文件、元数据与策略文件（`tests/test_check_licenses.py`）：

1. 全部宽松许可 → 通过，并生成稳定的 `THIRD_PARTY_NOTICES.md`；
2. 出现 GPL / AGPL / `UNKNOWN` → 失败；
3. LGPL 包未登记 → 失败；登记后通过；版本超出范围或作用域不符 → 失败；
4. `A OR B` 其一允许 → 通过；`A AND B` 其一被拒 → 失败；
5. 只有“BSD License”分类器且无澄清 → 失败；有澄清但许可证文件哈希不符 → 失败；
6. 锁中的包在环境里找不到 → 退出码 3；
7. vendored：PROVENANCE 哈希与文件不符、NOTICE 缺上游、`licenses/` 缺文本、两份 NOTICE 不一致 → 各自失败；
8. 依赖变化后 `THIRD_PARTY_NOTICES.md` 未重新生成 → 失败；
9. 对真实仓库运行 `--check`（作为 CI 的一部分），当前登记下应通过。

## 3. SBOM 与漏洞扫描

与 NX-034 `release.py sbom` 衔接，不新增第二套工具：

- **镜像 SBOM**：沿用 NX-034（syft，SPDX JSON，每个镜像一份；版本锁在 `release_tools`；缺工具或版本不符为失败，退出码 3）。
- **源码 SBOM（新增）**：同一 syft 对发布 tag 的源码归档（`git archive`，只含受跟踪文件）生成 SPDX JSON，随 GitHub Release 附上（NX-034 D7：Release 只附源码归档、公开版清单与 SBOM）。
- **交叉核对（新增）**：源码 SBOM 中由锁文件得出的包，必须与 `check_licenses.py --report` 的清单在包名与版本上一致；多出或缺少都使发布失败。这样“SBOM 里的东西”和“许可检查过的东西”是同一份。
- **漏洞阈值与豁免**：放在版本化配置 `deploy/compliance/vulnerability-policy.v1.json`：
  - `thresholds`：按发布类别（`stage` / `public`）分别给出阻断条件。推荐（D5）：stage 维持 NX-034 D2（不阻断，只列出）；public 发布在存在“Critical 且已有修复版本”的发现时阻断，“High 且已有修复版本”必须列出。
  - `waivers`：每条包括漏洞标识（CVE 或 GHSA 编号）、包与版本范围、作用域（镜像或源码）、理由、批准人（引用负责人决定，不写个人联系方式）、创建日期与**到期日**（最长期限见 D6）。到期的豁免使检查失败；指向已不存在的发现的豁免列为“可清理”，不失败。
  - 漏洞库新鲜度：记录 grype 数据库的构建日期；public 发布要求不超过规定天数（D12）。
  - 输出仍然只有计数、编号与豁免引用，不写漏洞描述原文（NX-034 的规则）。
- 测试：用锁定版本号的替身工具（同 NX-034 的做法）验证阈值判定、豁免到期、数据库过期、SBOM 与许可清单不一致、缺工具这几种失败。

## 4. 贡献与安全政策

### 4.1 CONTRIBUTING.md（L5 写作，本稿给出必须包含、且可检查的条款）

- 开发环境与本地 CI：`uv`、`pnpm`、Node 24；`scripts/ci/check` 是权威 CI，GitHub 不是必需的 CI。
- 提交要求：测试与证据、不提交秘密、提交说明格式；贡献签署方式见 D1（推荐 DCO：每个提交带 `Signed-off-by:`）。
- **外部 PR 的运行规则（AT-054）**：
  - 未经维护者审阅的外部代码，不在任何能访问内网、DATA_HOST 端口、服务凭据、真实模型或渠道密钥的环境里运行；
  - 仓库不设置会在 `pull_request_target` 或带秘密的上下文里运行外部 PR 的工作流；
  - 维护者先审阅差异，确需运行时，在一次性的隔离环境中运行（无内网、无秘密、无 docker 宿主 socket），合入 main 之后才进入 CI 主机的本地 CI。
- 检查（新增 `scripts/check_ci_isolation.py`，接入 `scripts/ci/check`）：仓库中不存在工作流文件；若将来存在，任何工作流都不得使用 `pull_request_target`、不得引用 `secrets.*`、权限必须为只读。AT-054 的另一半（CI 用户访问 stage 秘密或 DATA_HOST 端口被拒）由 NX-032 的隔离探测提供证据，本任务只引用。
- 若采用 DCO：`scripts/check_signoff.py <base> <head>` 检查区间内每个非合并提交的 `Signed-off-by` 与作者一致；维护者合入外部 PR 前运行。

### 4.2 SECURITY.md

- 报告渠道：`<SECURITY_CONTACT>`（D2；推荐 GitHub 私密漏洞报告，可再加一个由负责人提供的邮箱）。按 13 §10，**联系方式未填写时，不得宣称已建立正式响应渠道**：模板中保留占位符时，public 发布门槛失败。
- 响应时限（D3）：确认收到、初步评估、修复目标三个时限，写成目标值而非保证（项目仍在探索）。
- 支持版本（D4）：推荐只支持最新的一个发布。
- 处置流程：引用 13 §10（暂停受影响的 scope 与派发 → 保留最小证据 → 撤销相关凭据 → 核对已发生的动作 → 修复验证 → 受控恢复），以及公开披露的时机。

### 4.3 其他公开文件（13 §7）

- `CODE_OF_CONDUCT.md`（D10，推荐 Contributor Covenant 2.1，联系方式同为参数）、`GOVERNANCE.md`（维护者即负责人；技术决定以 ADR 记录）、`THIRD_PARTY_NOTICES.md`（由 §2.4 生成）、`README.zh-CN.md`（L5）。
- 检查：`scripts/check_public_files.py` 确认这些文件存在、`README.md` 与 `README.zh-CN.md` 互相链接、`SECURITY.md` 与行为准则中没有未填写的占位符（仅对 public 发布门槛生效）、`LICENSE` 为 MIT。

### 4.4 公开发布检查清单

写入 `docs/release/public-release-checklist.md`（L5 写作），每一项注明是否自动化：

| 项 | 自动 | 由谁执行 |
|---|---|---|
| 许可检查、`THIRD_PARTY_NOTICES.md` 最新 | 是（§2.4） | 发布门槛 |
| 源码 SBOM、与许可清单一致 | 是（§3） | 发布门槛 |
| 漏洞阈值与豁免 | 是（§3） | 发布门槛 |
| 发布对象与历史的卫生扫描 | 是（§5） | 发布门槛 |
| 公开文件齐全、无占位符 | 是（§4.3） | 发布门槛 |
| CI 隔离静态检查 | 是（§4.1） | 本地 CI |
| AT-054 隔离探测证据 | 引用 NX-032 | 维护者核对 |
| 可移植运行验收（13 §8：clean checkout、按 README 从源码启动） | 半自动 | 维护者 |
| tag、GitHub Release（B 类操作） | 否 | 负责人本人确认（NX-034） |

## 5. 公开仓库卫生

- **纳入发布门槛**：
  - `check_publication.py` 增加 `--tree <ref>` 模式：扫描某个提交的**全部受跟踪文件**（现在只扫暂存区），对 tag 运行；
  - 同样的规则扫描 `git archive` 生成的源码归档内容（发布对象本身）；
  - `prepush-scan.sh` 的三项检查在 public 发布时对 tag 所在的整段待发布历史运行（base 为上一次公开发布的 tag；首次为空树，见下）。受限标识名单由调度员维护，本任务不改动。
- **首个公开 release 之前的历史审计**：
  - 用上面的规则与 `prepush-scan.sh` 对**全部历史**扫描一遍，输出只含命中类别、提交号与路径的报告（不复述命中内容），存入私有证据；
  - 已知事实：2026-10-11 的隐私清理只处理了当前文件，历史中仍有当时的主机别名、本机路径、硬件型号和开发机主机名；
  - AGENTS.md 禁止对 main 强推，历史不能改写。因此处置原则（D9）：非秘密的标识（主机别名、本机路径）接受其留在历史中；凡是凭据、私钥或受限的网络标识出现在历史中，一律视为已泄露，立即**轮换或作废**该凭据，并在审计报告中记录，不依赖改写历史；
  - 审计结论与负责人确认是首个 public release 的前置条件。
- **镜像与构建产物**：镜像不公开（NX-034 D7），image archive 只在内部发布目录；源码归档只含受跟踪文件，已由上面的扫描覆盖。
- **额外的秘密扫描工具**：见 D11（推荐暂不引入新工具）。

## 6. 发布门槛汇总

`release.py` 增加 `public-preflight --tag <tag>`（或独立脚本，由 NX-034 的发布流程调用），按顺序执行，任一失败即拒绝；结果（只有通过与否、计数与报告的 sha256）写入发布清单：

1. clean checkout（tag 对应提交的独立工作树）与依赖阶段（锁定安装）；
2. `check_licenses.py --check`；
3. `check_public_files.py --public`；
4. `check_ci_isolation.py`；
5. `check_publication.py --tree <tag>` 与源码归档扫描；
6. `prepush-scan.sh <tag> <上一次公开 tag 或空树>`；
7. 源码 SBOM、与许可清单交叉核对；
8. 漏洞阈值与豁免（public 类别）；
9. 输出待负责人确认的摘要：Critical/High 且有修复的数量、生效的豁免、条件放行的许可证清单。

stage 发布继续按 NX-034 执行，只增加第 2 步（许可检查）与第 7 步中的 SBOM 交叉核对（不阻断，D5）。

## 7. 实现与文件清单

- 新增脚本：`scripts/check_licenses.py`、`scripts/check_ci_isolation.py`、`scripts/check_public_files.py`、`scripts/check_signoff.py`（若采用 DCO）；`check_publication.py` 增加 `--tree`；`release.py` 增加源码 SBOM、交叉核对、漏洞策略与 `public-preflight`。
- 新增配置：`deploy/compliance/license-policy.v1.json`、`deploy/compliance/vulnerability-policy.v1.json`。
- 生成文件：`THIRD_PARTY_NOTICES.md`。
- 文档（L5 写作，本稿给出检查要求）：`README.zh-CN.md`、`CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`、`SECURITY.md`、`GOVERNANCE.md`、`docs/release/public-release-checklist.md`、操作手册。
- `scripts/ci/check`：在依赖阶段后加入许可检查与 CI 隔离检查。
- 不涉及数据库迁移与 `packages/contracts/` 改动。
- 验收：AT-054 的静态部分由 `check_ci_isolation.py` 的测试覆盖，网络隔离部分引用 NX-032 的证据；AT-055（构建 SHA 与部署 SHA 一致）仍归 NX-034，本任务只在发布门槛中复用清单。

## 8. 需要负责人决定的点

- **D1 贡献签署：DCO 还是 CLA。** 推荐 DCO（提交带 `Signed-off-by:`，由 `check_signoff.py` 检查）：不需要法律文本与签署系统，与 MIT 搭配常见。替代：CLA（可以将来改授权，但需要签署流程与法律文本）。
- **D2 安全报告渠道。** 推荐 GitHub 私密漏洞报告（仓库设置项，由负责人开启），可另加一个由负责人提供的邮箱 `<SECURITY_CONTACT>`。未开启或未填写之前，SECURITY.md 写明“尚无正式响应渠道”，public 发布门槛失败。
- **D3 响应时限（目标值）。** 推荐：3 个工作日内确认收到，10 个工作日内给出初步评估；修复目标 Critical 30 天、High 60 天，其余随常规版本。
- **D4 支持版本。** 推荐 1.0 之前只支持最新的一个发布；安全修复只进入下一个发布。
- **D5 漏洞阈值。** 推荐 public 发布在存在“Critical 且已有修复版本”的发现（未豁免）时阻断，“High 且已有修复版本”在确认摘要中列出；stage 发布维持 NX-034 D2（不阻断，只列出）。
- **D6 豁免期限与批准。** 推荐每条豁免必须有理由与到期日，最长 90 天，由负责人批准；到期即失败，需要续期时重新批准。
- **D7 弱 copyleft 依赖。** 推荐允许 LGPL 与 MPL-2.0 作为**未修改的独立库**使用，逐包登记（当前：psycopg 家族为运行时依赖；certifi、hypothesis 为开发依赖；lightningcss 为前端构建依赖）；GPL、AGPL 等强 copyleft 与未知许可一律拒绝。替代：只允许宽松许可（需要替换 psycopg，代价很大）。
- **D8 THIRD_PARTY_NOTICES 的产生方式。** 推荐由检查脚本从清单生成并在 CI 中核对是否最新；替代：手写维护（容易与依赖脱节）。
- **D9 历史中的非秘密标识。** 推荐接受（不改写历史，main 禁止强推）；凡历史中出现凭据、私钥或受限网络标识，一律轮换或作废并记录，审计结论由负责人确认后才能做首个 public release。
- **D10 行为准则。** 推荐 Contributor Covenant 2.1，执行联系人与 D2 相同的参数。
- **D11 额外的秘密扫描工具。** 推荐暂不引入（沿用现有规则，增加整棵树与全部历史的扫描模式）；如需更广的规则覆盖，再评估固定版本的专用工具（新的发布工具须写入 `versions.lock.json` 的 `release_tools`）。
- **D12 漏洞库新鲜度。** 推荐 public 发布要求 grype 数据库不超过 7 天；stage 只记录日期。
