# 公开发布检查清单

适用于 NexLoop 的**公开发布**（公开 tag 或 GitHub Release）。依据：[NX-036 设计稿](../implementation/NX-036-design.md) §4.4、§6（D1–D12 负责人 2026-10-11 全部按推荐定案），交接规格 [13_SECURITY_PRIVACY_AND_OPEN_SOURCE](../handoff/docs/13_SECURITY_PRIVACY_AND_OPEN_SOURCE.md) §7–§10，以及 NX-034 的本地发布工具。

- 自动化部分由 `scripts/release/release.py public-preflight --ref <tag 或 sha> --out <空目录> [--previous-public <上一次公开发布>] [--release-dir <发布目录>]` 执行（NX-036 实现）。九步全部执行并记录，任何一步失败即整体失败；命令缺失按失败处理。报告写入 `<out>/preflight.json` 并输出其 sha256。
- 人工部分必须由负责人本人确认，门槛不能代替。
- 创建 tag 和 GitHub Release 是 B 类操作，由负责人本人执行；`public-preflight` 不创建 tag、不改写历史。

## A. `public-preflight` 的九步（已自动化）

| # | 步骤 | 检查内容 | 自动化 |
|---|---|---|---|
| 1 | clean checkout | 对被发布的 ref 运行 `git archive`（只含受跟踪文件）并解压到新目录，再做锁定安装（`uv sync --frozen`、`pnpm install --frozen-lockfile`）。后续各步使用被发布 ref 自己的脚本与策略 | 已自动化 |
| 2 | 许可合规 | `scripts/check_licenses.py --check --report`：依赖许可按 `deploy/compliance/license-policy.v1.json` 判定；强 copyleft 与未知许可拒绝；弱 copyleft 只能逐包列入；`THIRD_PARTY_NOTICES.md` 与清单一致 | 已自动化 |
| 3 | 公开文件 | `scripts/check_public_files.py --public`：`deploy/compliance/public-release.v1.json` 列出的公开文件齐全（含本清单、`GOVERNANCE.md`、`README.zh-CN.md`）；README 中英文双向链接；文档中没有 `<SECURITY_CONTACT>` 占位符；`security_contact` 已填写；文档写明的响应时限、支持版本、行为准则与配置一致；LICENSE 为 MIT | 已自动化 |
| 4 | CI 隔离（静态） | `scripts/check_ci_isolation.py`：`.github/` 下的工作流不得使用 `pull_request_target`、不得引用 `secrets`，权限只读 | 已自动化（网络隔离部分另见 C 节） |
| 5 | 发布对象扫描 | `scripts/check_publication.py --tree <sha>` 扫描整棵树，`--archive <源码归档>` 扫描归档每个成员：私有路径、私网地址、私钥、模型凭据、符号链接与子模块 | 已自动化 |
| 6 | 历史扫描 | `scripts/ci/prepush-scan.sh <sha> <上一次公开发布>`：受限标识与凭据值。**没有上一次公开发布时本步失败**，首次公开发布必须先完成 B 节的全历史审计 | 已自动化（首次发布需人工审计） |
| 7 | 源码 SBOM | 用锁定版本的 syft 对 clean checkout 生成 SBOM，与许可清单逐项交叉核对（排除自有包），两边任何一边多出都失败 | 已自动化（需安装锁定版本的 syft） |
| 8 | 漏洞阈值 | 用锁定版本的 grype 扫描，按 `deploy/compliance/vulnerability-policy.v1.json` 的 public 类别判定：未豁免的“Critical 且已有修复版本”阻断；豁免最长 90 天、须负责人批准、到期即失败；漏洞库不超过 7 天。给出 `--release-dir` 时合并镜像扫描结果 | 已自动化（需安装锁定版本的 grype） |
| 9 | 确认摘要 | 输出供负责人确认的摘要：阻断、列出、已豁免与待清理的发现编号，以及条件放行（弱 copyleft）的许可证清单 | 已自动化（摘要由负责人在 B 节确认） |

## B. 负责人确认（人工）

- [ ] `public-preflight` 报告 `ok: true`，记录报告的 sha256 与被发布的提交。
- [ ] 已阅读第 9 步摘要：没有未豁免的阻断发现；列出的 High 发现与条件放行的许可证可以接受。
- [ ] 安全报告渠道已开通（GitHub 私密漏洞报告；可另加一个由负责人提供的联系方式），并已填入 `deploy/compliance/public-release.v1.json` 的 `security_contact`；`SECURITY.md` 与 `CODE_OF_CONDUCT.md` 中的 `<SECURITY_CONTACT>` 已替换（D2）。
- [ ] **首次公开发布**：已完成全部提交历史的审计。历史不改写；历史中出现过的凭据、私钥或受限网络标识一律轮换或作废并记录；审计结论经负责人确认（D9）。
- [ ] 发布对应的本地 CI 结果为通过，并且发布清单经过签名与验签（NX-034 `release.py manifest` / `sign` / `verify`）。
- [ ] CHANGELOG 已把“未发布”整理到该版本号下，版本号遵循语义化版本。
- [ ] README（英文）与 README.zh-CN.md（中文）如实说明：项目仍在探索、不保证收入；自主范围由 Action 政策控制；哪些已实现、哪些仅规划；运行需要自带模型 key，另有显式测试模式；GitHub CI 不是必需的；示例数据均为合成（13 §7）。
- [ ] 由负责人本人创建 tag 与 GitHub Release（B 类操作）。

## C. 尚未自动化或尚未具备条件的项

| 项 | 现状 |
|---|---|
| CI 网络隔离的实测（AT-054 网络部分） | 由 NX-033 的运行器检查与隔离探测提供；在真实 APP_HOST 上的首次探测要等主机侧 B 类步骤执行后（NX-033、NX-032） |
| stage 发布的源码 SBOM 交叉核对 | 尚未接入 NX-034 发布流程；stage 的许可检查已由本地 CI 的 `check_licenses.py --check` 覆盖 |
| `preflight.json` 写入已签名的发布清单 | 目前单独存放并输出哈希，不写回已签名清单；是否在签名前合并，待 NX-034 发布流程确定 |
| 公开镜像 | 镜像目前不公开（NX-034 D7）；若将来公开，需要重新评估镜像中弱 copyleft 依赖的分发义务 |
| 13 §7 列出的其余公开文档（配置清单、连接器开发、评估运行、升级/迁移、故障排查） | 部分已有入口，见 [运维手册](../OPERATIONS.md) 与 [架构说明](../ARCHITECTURE.md)；连接器开发与评估运行指南尚未编写【待确认：是否作为首个公开发布的门槛】 |
