# v0.1 验收证据包骨架

文件名沿用调度员 2026-10-15 的指定（`at-015-evidence-pack`）。内容覆盖 v0.1 的全部验收项，不只 AT-015。

**依据**：

- 交接规格 [14 测试与验收计划](../handoff/docs/14_TEST_ACCEPTANCE.md)：§1 证据等级，§6 v0.1 完成定义，§7 发布证据包；
- `planning/acceptance-tests.json`：72 项，其中 59 项 passed、13 项 not_run；
- 证据包脚本 `scripts/nx037/evidence_pack.py`。

**状态**：骨架，只定目录结构、每项应附的证据类型，以及哪些已经具备。**不改 planning，不给任何 AT 定结论**；结论由调度员按证据回填。

## 0. 原则（摘自 14 §1、§7）

- 结果只用 `passed`、`failed`、`blocked`、`skipped`、`not_run` 这几种。关键的 PG、授权、Action、恢复测试，不能因为缺少依赖就记为 skip 而算作通过。
- 单元测试中 mock 通过，不能替代真实 PostgreSQL 角色测试；合成业务事件，也不能替代真实的商业验证。
- 每条证据都要写明**来源与等级**：
  - 本地测试；
  - CI 主机测试；
  - stage 合成数据；
  - stage 真实，即受控真实模型与真实交付。
- 不用演示截图代替执行证据。首次失败和重跑的记录要保留。
- 证据包只放 ID、哈希、数字、时间、代码和命令行；不放口令、DSN、主机地址、真实客户内容或真实租户名（真实租户名写作 `<TENANT_DISPLAY_NAME>`）。

## 1. 目录结构

```
v0.1-evidence/                         私有证据目录，0700；公开版只发布 §4 的摘要
├── README.md                          索引：每个 AT → 证据文件、等级、结论
├── release/
│   ├── manifest.json                  发布清单（evidence_pack: release_manifest）
│   ├── archive-check.json             archive.py check 的输出
│   ├── ci-result.json                 发布 SHA 的 CI 结果（evidence_pack: ci_result）
│   ├── sbom/  third-party-notices.md  （evidence_pack: third_party_notices）
│   └── public-preflight.json          （evidence_pack: public_preflight）
├── environment/
│   ├── inventory-before-<host>.json   （evidence_pack: inventory_before）
│   ├── inventory-after-<host>.json    （evidence_pack: inventory_after）
│   ├── deploy-gate-<host>.json        nexloop-doctor --deploy-gate
│   ├── lock-hashes.txt                versions.lock.json 与 uv.lock / pnpm-lock 的 sha256
│   └── catalog.txt                    catalog 尾版本与 lineage
├── tests/
│   ├── <run-id>/junit/ + stages.txt   CI 原始结果（NEXLOOP-STAGE 行）
│   └── reruns.md                      首次失败与重跑的记录
├── backup-restore/
│   ├── drill-<drill-id>.json          nexloop-restore 报告（evidence_pack: restore_drill）
│   └── doctor-backup.json
├── isolation/
│   └── at058-<location>.json          probe_isolation.py 三处报告
├── load/
│   └── report.json                    AT-060 报告（evidence_pack: load_report）
├── trial/                             NX-037 受控交互：run_interactions 的试运行目录
│   ├── interactions.jsonl  state.json  prompts.jsonl  resumes.jsonl
│   └── reconciliation.json            （evidence_pack: effect_reconciliation）
├── second-maintainer/
│   └── log.md  deviations.md          AT-072 演练记录与手册偏差
└── known-risks.md                     （evidence_pack: known_risks）
```

`scripts/nx037/evidence_pack.py --out <新目录> --item <名称>=<路径> … --trial <试运行目录>` 会对上面标出 `evidence_pack: …` 的各项只记 sha256 与字节数，汇总交互结果，并把缺失的项如实标为 `missing`。其余文件由调度员按本目录结构放置。

## 2. 每条验收项应附的证据

证据类型沿用 planning 的 `type`：`command`、`test_report`、`file`、`commit`、`note`。

### 2.1 S1–S4：59 项，已经 passed

- S1 5 项，S2 14 项，S3 28 项，S4 12 项。每项在 `planning/acceptance-tests.json` 中已有 `commit` 与 `test_report` 证据（多为 CI 主机上的全量运行）。
- 证据包中**不重新执行**，只在 `README.md` 索引里列出每项的证据 ref 与等级。
- 有两项需要在索引中单独注明等级：
  - **AT-015**（SSE 恢复）：本地 jsdom、真实浏览器（headless Chromium）核对与 CI 都已有证据。v0.1 的 WebChat 真实接入还要在受控交互中再看一次，记为 stage 真实证据的补充项，不改变它的 passed 结论；
  - **AT-060**（负载与隔离）：正式整轮 run 已通过（v0.1 按 200 客户、0.1 条/s），1000 客户是 v0.1 之后的目标。

### 2.2 S5：9 项，都还是 not_run

| AT | 名称 | 应附证据（类型） | 已具备（截至 10/15 晚） | 还要等什么 |
|---|---|---|---|---|
| AT-050 | 恢复演练 | `test_report`：`nexloop-restore` 报告；`command`：drill-mark 与 record 的输出；`note`：RTO、RPO 与生产库未受影响 | **已具备**：d20261015a passed，RTO 49.4 s，RPO 60.2 s，`result_id` 1（[run-33](run-33-20261015.md) §10） | AT-059 的合成删除重放待补，要等有 Consumer（A1 之后）。下次演练做完后合并为 AT-050 的结论 |
| AT-053 | GitHub 断连 | `test_report`：断开 GitHub 时本地 CI 与构建可用、同步为 pending、恢复后补推 | 本地测试证据已有 | 主机侧 NX-033 §8 第 5 步的断连演练（不在本周排期） |
| AT-054 | CI 隔离 | `test_report`：CI 用户访问不到 DATA_HOST 与内部端口；公开 PR 不会自动执行 | run-42 §3a 的 CI 用户探测（stage 真实） | NX-033 §8 的完整切换与隔离探测 |
| AT-055 | 镜像发布一致 | `command`：`archive.py check`；`file`：清单；`command`：run-46 §2 的容器 digest 对照 | **已具备**：三次签名发布已封存，第三次为 `20261015-4736ea179063-1`；run-46 清点中镜像 digest 与清单一致 | 以最终部署的那次发布重做一次 §2 对照，写入证据包 |
| AT-056 | 迁移无备份 | `test_report`：没有已验证备份时 `nexloop-migrate` 拒绝；`command`：stage 上 0198 迁移前门槛通过、迁移后补备份 | 本地与 CI 测试已有；stage 上 0198 是按门槛执行的 | 把 0198 的门槛输出与前后备份的清单行整理进证据包 |
| AT-057 | 版本回滚 | `command`：`deploy.py rollback-check` 拒绝盲目回滚；有 `known_good` 时的核对 | 本地测试已有；stage 已有多个发布 | 执行单 4.5 回滚核对（`known_good` 已存在） |
| AT-058 | 端口安全 | `test_report`：管理机、DATA_HOST、APP_HOST 三处 `probe_isolation.py` 报告 | **已具备**：run-42 复测通过，包括 G-APP-SUBNET 修复之后与 G-CLAIM-EGRESS §3d | 报告放入 `isolation/`；9643 规则上线后的参考端点探测结果一并放入 |
| AT-071 | 受控真实模型与真实交付 | `file`：试运行目录；`command`：`evidence_pack.py` 的输出；`note`：费用、对账与停止或暂停规则的结论 | 未开始；10/15 冒烟已走到 Run 签发，失败原因为 `budget_exhausted` 与 `provider_unavailable`，预算期间已改为从 10/18 开始 | **10/19–22**：校准与受控交互（≥20 条），逐条记录模型、工具与平台的时间和费用，至少一次真实交付经查询对账 |
| AT-072 | 第二维护者演练 | `note`：演练记录与手册偏差；`commit`：据此修订的文档 | 未开始 | 负责人本人只按公开文档，在一次性环境中完成一次发布与一次恢复。排在受控交互之后 |

### 2.3 S6、S7：AT-023、024、048、046

不属于 v0.1（14 §6：S6/S7 未做不影响 v0.1）。在证据包索引中列为 `not_run (out of v0.1 scope)`。

## 3. 按时间排的产出计划

| 时间 | 产出 | 放入 |
|---|---|---|
| 已具备 | 发布清单与 archive check；CI 结果；run-33 演练报告；run-42 探测报告；run-46 清点；AT-060 报告；许可与 SBOM | `release/`、`backup-restore/`、`isolation/`、`environment/`、`load/` |
| 10/16–18 | 4.5 回滚核对；预算期间生效后的冒烟复测；周六就绪检查（OPERATIONS §9.4a） | `environment/`，以及 AT-057 的证据 |
| 10/19–22 | 校准（3 次）与受控交互；`evidence_pack.py` 汇总；效应对账 | `trial/`；AT-071 |
| 交互之后 | 第二次恢复演练（含 AT-059 合成删除）；AT-072 第二维护者演练；`known-risks.md` 定稿；public-preflight | `backup-restore/`、`second-maintainer/`、`release/` |

## 4. 公开发布的摘要（rc 发布说明使用）

公开版只写：

- 每个 AT 的结论与证据等级；
- 发布 ID、commit 与镜像 digest；
- 数字指标：RTO、RPO、时延分位、费用合计、交互数；
- 已知限制。

不公开试运行目录、清点文件和探测报告的原文。验收报告写明“客户为模拟、系统为真实”（ROADMAP 2026-10-14 参与者变更）。
