# S5 收尾差距清单（NX-032 至 NX-037）

- **日期**：2026-10-18。依据 main `9d1223ee` 上的 `planning/tasks.json`、`planning/acceptance-tests.json`、`planning/ROADMAP.md`，以及 `docs/release/` 下各执行单记录的结果。
- **只读整理**：不改 planning，也不给任何 AT 定结论；结论由调度员按证据回填。只用角色名。
- **执行者**：
  - **调度员（A）**：调度员在主机上执行；
  - **负责人（B）**：B 类步骤，由负责人本人执行或逐步确认；
  - **开发线**：写代码或文档。
- **S5 只认真实证据**：本地测试、CI 主机测试或合成环境的结果，只能作为补充。每一项 S5 验收都要有 stage 上的执行记录。

## 0. 结论先看

| 项 | 现状 |
|---|---|
| 任务 | NX-032 到 NX-037 六个任务全部 `in_progress`，**没有一个能马上标 done** |
| S5 验收 | 共 9 项：AT-050、053、054、055、056、057、058、071、072。只有 **AT-071 已 passed**；其余 8 项 planning 中都是 `not_run`，其中 AT-060（S4，已 passed）不在此列 |
| 已有真实证据、只差整理回填 | 4 项：AT-050（还差 AT-059 删除重放）、AT-055、AT-056、AT-058（还要按当前发布重测） |
| 还要在 stage 上做 | 2 项：AT-057 回滚核对；AT-050 第二次演练（带合成删除） |
| 卡在主机切换 | 2 项：AT-053、AT-054。它们依赖 NX-033 §8 的主机切换（s5-owner-checklist 第 5 阶段），这是工作量最大、风险最高的一段 |
| AT-072 | 10/19 17:00 后演练，结果未知 |
| NX-037 证据包 | 已有 3 项（ci_result、release_manifest、interactions）；**缺 6 类**：清点（before/after 两项）、效应对账、恢复演练报告、负载报告、public-preflight、第三方声明。另外 known_risks 已定稿，只差放进证据包 |
| 关键路径 | NX-033 主机切换（AT-053/054）→ NX-036 public-preflight → NX-037 证据包 → v0.1.0 |

> planning 有明显滞后：ROADMAP §1–§3 仍写着“AT-060 未通过”“两机未部署”。run-33、run-42、run-46 等执行单里已经取得的 stage 结果，也还没有回填到 AT 的证据里。§6 列出了建议的回填项。

## 1. S5 验收总表

| AT | 名称 | planning 状态 | 已有的真实证据（出处） | 缺什么 | 谁做 | 工作量 |
|---|---|---|---|---|---|---|
| **AT-050** | 恢复演练 | not_run | stage 演练 `d20261015a`：passed，RTO 49.4 s，RPO 60.2 s，比对 0 差异，生产库未受影响（[run-33](../release/run-33-20261015.md) §10） | ① 恢复点之后的**合成删除**及其重放（AT-059 的真实备份版本，[NX-035 部署运行手册](NX-035-deploy-runbook.md) §4.2 第 2 步）；② 用当前发布的 catalog 再演练一次（上次 lineage 为 0197，现在是 0216）。stage 演练按设计停在 `verified_readonly`，不开放，所以“开放”由 AT-072 的本机演练覆盖 | 开发线：stage 上负责人执行删除的入口（见 §3.4，约 0.5 天）；负责人（B）+ 调度员：第二次演练 | 开发 0.5 天；演练约 2 小时 |
| **AT-053** | GitHub 断连 | not_run | 只有本地测试 | NX-033 §8 第 5 步的断连演练：GitHub 不可达时本地 CI 与构建照常、同步为 `sync_pending`，恢复后补推。依赖主机切换 | 负责人（B）+ 调度员 | 切换完成后约 1 小时 |
| **AT-054** | CI 隔离 | not_run | run-42 §3a：CI 用户探测（stage 真实，部分） | 切换后的完整隔离探测：运行器检查、`isolation-probe`；公开 PR 不自动执行（`review/*` 不入队，`refs/pull/*` 被拒） | 负责人（B）+ 调度员 | 切换完成后约 1 小时 |
| **AT-055** | 镜像发布一致 | not_run | 多次签名发布已封存（发布 7 起，planning NX-034 证据记到发布 11）；run-46 §2 中容器 digest 与清单一致 | 用**最终验收所用的发布**（当前为发布 19）重做一次 digest 对照：`archive.py check` 加上运行中容器的 digest 与清单逐项比对，结果放入证据包 | 调度员（A，只读） | 30 分钟 |
| **AT-056** | 迁移无备份 | not_run | stage 上 0198 及之后的迁移都经门槛执行，迁移前后都有备份 | 整理一条完整链：门槛拒绝（无备份或备份过旧）→ 备份后放行 → 迁移后补备份。最好在一次实际迁移时取得“先拒后放”的输出；只有放行记录不够证明“拒绝” | 调度员（A）；拒绝的那次在 DATA_HOST 上执行时需要负责人（B） | 下次迁移时顺带，约 30 分钟 |
| **AT-057** | 版本回滚 | not_run | 只有本地测试；stage 上已有 `current` 和 `known_good` | `deploy.py rollback-check` 两次：① 回滚到跨越未声明 expand-only 迁移的旧发布，应被拒绝并给出向前修复提示；② 回滚到 `known_good`，应允许或给出明确理由。只执行检查，不真的回滚 | 调度员（A，只读命令） | 30 分钟 |
| **AT-058** | 端口安全 | not_run | run-42：管理机、DATA_HOST、APP_HOST 等五份 `probe_isolation.py` 报告 `passed`，含 G-APP-SUBNET 修复后的复测和 G-CLAIM-EGRESS §3d | run-42 之后网络又改过（pgBackRest TLS 端口、参考端点、claim 出口网络等），要按当前发布**重跑五份探测**，报告放进 `isolation/` | 调度员（A）；DATA_HOST 那一份需要负责人输入 sudo（B） | 1 小时 |
| **AT-071** | 受控真实模型与真实交付 | **passed** | 试运行 `4f5d795c`：36 条交互，费用 1.59 CNY（上限 100），真实交付 6/6，安全事实全为 0 | 无。证据包里的 `interactions` 已具备 | — | — |
| **AT-072** | 第二维护者演练 | not_run | 无（定于 10/19 17:00 后） | ① 负责人只按公开文档完成发布：CI 主机上构建、签名、验签、部署 dry-run（[入口单](../release/at-072-drill-entry.md)）；② 完成恢复：本机备份、PITR、重放、校验、开放（[本机恢复演练](../release/at-072-local-restore-drill.md)）；③ 偏差记录；④ 文档线按偏差修订并合入 | 负责人（B）演练；开发线（L5）修订文档 | 演练半天；修订 0.5–1 天 |

S1–S4 中与 S5 任务相关的 AT 都已 passed：AT-047、049、060 等。AT-048（S6）不属于 v0.1。

## 2. 按任务

### 2.1 NX-032 双机 TLS 与隔离部署

- **done 还差的验收**：AT-058（重测）；AT-072（演练中的部署 dry-run）。
- **已有的真实证据**：多次部署、部署门禁 19/19 和 20/20、重启演练、metrics DSN 上线，见 planning 证据 2026-10-15/16/17。
- **缺的证据**：按当前发布重跑的 AT-058 探测；一份“最终验收发布”的部署门禁输出与部署后清点（同时是证据包的 `inventory_after`）。
- **顺序**：AT-058 重测可以随时做，与其他项无依赖。
- **工作量**：调度员 1–2 小时，其中有一步需要负责人输入 DATA_HOST 的 sudo。

### 2.2 NX-033 本地 Git 队列与隔离 CI

- **done 还差的验收**：AT-053、AT-054。
- **现状**：发布用的 CI 结果已经由现有车道按 NX-033 队列格式产出，但完整的主机切换（[s5-owner-checklist](../release/s5-owner-checklist.md) 第 5 阶段 5.1–5.6）一直没做（[deploy-days](../release/deploy-days-20261015.md)）。
- **要做的**：

  | 步骤 | 内容 | 谁做 | 工作量 |
  |---|---|---|---|
  | 5.1 | 只读核实 CI 主机能否运行 rootless Podman（ROADMAP R1） | 调度员 | 30 分钟 |
  | 5.2 | 建 `nexloop-git` 用户、bare 仓库、hook、控制器与同步的 systemd unit | 负责人（B） | 2 小时 |
  | 5.3 | 在主机上生成同步用密钥对；负责人在 GitHub 粘贴公钥，作为只限本仓库的 deploy key | 负责人（B） | 15 分钟 |
  | 5.4 | 把 main 推入 bare 仓库，调整 remote 和 pre-push hook，发放各推送身份的公钥 | 负责人（B） | 1 小时 |
  | 5.5 | 隔离探测与断连演练，产出 AT-053、AT-054 的证据 | 负责人（B）+ 调度员 | 1 小时 |
  | 5.6 | 一次完整 CI，与现行车道的结果对照，然后切换 | 负责人确认 | 1 小时，加一轮 CI（约 40 分钟） |

- **依赖与风险**：5.1 不通过时，要先修订 D2，即运行器形态；这可能需要开发线改代码，工作量未知。切换会改变每条开发线推进 main 的方式，建议排在 AT-072 之后、一个没有发布的窗口里。
- **合计**：负责人半天到一天，调度员约半天。

### 2.3 NX-034 发布归档与 GitHub 同步

- **done 还差的验收**：AT-055（复核）、AT-057、AT-053（同步部分，依赖 NX-033）；AT-072 的发布部分。
- **已有的真实证据**：发布 7 起都经 NX-033 队列 CI 后签名并封存（planning 证据记到发布 11）。
- **缺的证据**：AT-055 用最终发布重新对照一次；AT-057 的 `rollback-check` 输出；`make sync-github` 在主机切换后的实际同步记录，带 deploy key。
- **工作量**：调度员 1 小时；同步部分随 NX-033 一起做。

### 2.4 NX-035 备份 / PITR 与恢复演练

- **done 还差的验收**：AT-050（第二次演练，含删除重放）、AT-056（整理）。
- **已有的真实证据**：pgBackRest repo1 的完整备份 verified；迁移门槛在 stage 生效；`d20261015a` 演练 passed。
- **缺的证据**：
  - 恢复点之后的合成删除与它的重放（AT-059 真实备份版本）；
  - 当前 catalog 下的一次演练；
  - AT-056 的“先拒后放”输出；
  - 证据包的 `restore_drill`，即这次演练的报告。
- **开发线的前置**：stage 上，负责人删除一个合成顾客的入口。工作台 HTTP 入口已有（随 r16d 合入） `erase_consumer`（owner 专属），但没有页面按钮，也没有负责人工具。`scripts/dev/drill_workbench.py` 只是开发机脚本。需要的是一个与 `confirm_not_delivered.py` 同样形态的负责人工具：配置文件加一条命令，口令从钥匙串管道输入；或者在工作台加一个删除按钮。约 0.5 天。
- **另一个备选**：用 stage 上 `world=test` 的合成顾客。删除仍须经受治理 Action，不能直接写库。
- **repo2 异地备份**：仍在暂缓（无 S3）。它不属于 AT-050 的验收条件，但是 known_risks 的一项，见 [known-risks-v0.1](../release/known-risks-v0.1.md)。
- **工作量**：开发 0.5 天；演练 2 小时，由负责人（B）和调度员执行。

### 2.5 NX-036 开源文档与供应链检查

- **done 还差的**：NX-036 没有直接对应的 S5 AT。完成条件是公开门槛：`release.py public-preflight` 九步全部通过，加上 rc 发布说明定稿（[s5-owner-checklist](../release/s5-owner-checklist.md) 6.3–6.5）。
- **已有**：全历史审计已确认，私密漏洞报告已开启，安全联系人已填，CHANGELOG 和各手册已合入；gateway 的 Critical 已经清零。
- **缺的**：
  - public-preflight 的一次通过记录，也就是证据包的 `public_preflight`。第 8 步要求漏洞库不超过 7 天（D12），而且不能有未豁免的阻断项；
  - 第三方声明与 SBOM 的最终版，即证据包的 `third_party_notices`。任何许可策略改动之后都要重新生成并通过 `check_licenses --check`；
  - rc 发布说明里的验收摘要还是占位，要等 AT 回填后再填。
- **顺序**：等 AT-072 修订完文档之后再跑 public-preflight，免得文档还在变。
- **工作量**：调度员运行 1 小时，负责人确认 30 分钟；开发线修复预检发现的问题，工作量未知。

### 2.6 NX-037 v0.1 受控真实闭环验收

- **done 的条件**：S1–S5 必需的 AT 全部 passed，证据包完整，已知限制定稿。
- **还差的验收**：上面的 AT-050、053、054、055、056、057、058、072，共 8 项。
- **证据包**：见 §3。

## 3. NX-037 证据包缺的项

`scripts/nx037/evidence_pack.py` 定义了 10 个文件项，另有 interactions、voids、resumes 来自 `--trial`。10/18 在 APP_HOST 上生成的证据包里，present 的是 `ci_result`、`release_manifest`、`interactions`（planning NX-037 2026-10-18 的证据）。

| 证据项 | 内容 | 产生方式 | 谁做 | 前置 | 工作量 |
|---|---|---|---|---|---|
| `inventory_before`、`inventory_after`（清点，算 1 类） | 部署前后两台主机的只读清点 | `scripts/deploy/inventory.py`。before 用部署前基线清点（10/16 负责人已接受新基线）；after 在最终验收发布上取 | 调度员（A） | 选定最终验收发布 | 1 小时 |
| `effect_reconciliation` | 试运行 `4f5d795c` 中每条效应意图的对账结论 | NX-037 效应对账生成器 `scripts/nx037/effect_reconciliation.py`，已合入 | 调度员（A） | 无 | 30 分钟 |
| `restore_drill` | 恢复演练报告，只含数字 | §2.4 的第二次 stage 演练报告；`d20261015a` 的报告可作为备选 | 负责人（B）+ 调度员 | §2.4 | 随演练 |
| `load_report` | AT-060 报告 | 已有：AT-060 于 10/13 passed 的正式整轮报告，只需放进证据包 | 调度员（A） | 无 | 15 分钟 |
| `public_preflight` | `release.py public-preflight` 的输出 | §2.5 | 调度员运行，负责人确认 | AT-072 文档修订之后 | 1 小时 |
| `third_party_notices` | THIRD_PARTY_NOTICES 与 SBOM | 发布目录中的 SBOM，加上仓库的 `THIRD_PARTY_NOTICES`；与 public-preflight 用同一个 SHA | 调度员（A） | §2.5 | 15 分钟 |
| `known_risks`（已就绪） | v0.1 已知风险 | [known-risks-v0.1.md](../release/known-risks-v0.1.md) 已于 10-18 定稿，只需 `--item known_risks=<路径>` | 调度员（A） | 无 | 5 分钟 |

- **说明**：证据包要求每次都**重新生成到一个新目录**，脚本拒绝写入已存在的目录。所以等所有项都齐了，再用同一个最终 SHA 生成一次完整的包，不要逐项追加。
- **AT-072** 不是证据包的文件项。它的证据是演练记录、偏差清单和修订提交，放在私有目录的 `second-maintainer/`（[证据包骨架](../release/at-015-evidence-pack.md) §1）。

## 4. 先后顺序与依赖

```
10/19 17:00  AT-072 演练（负责人）
     │
     ├─► 文档线按偏差修订并合入（0.5–1 天）
     │
可随时、互不依赖（调度员 A 类，合计约 3 小时）：
     AT-055 digest 复核 · AT-057 rollback-check · AT-058 五份探测重跑（DATA_HOST 那一份需负责人 sudo）
     效应对账生成 · load_report 与 known_risks 入包 · 清点 before/after
     │
开发线 0.5 天：stage 上负责人删除合成顾客的入口（工具或按钮）
     └─► AT-050 第二次 stage 演练（含删除重放；负责人 B + 调度员，2 小时）
           └─► restore_drill 入包；AT-056 整理（下次迁移时取得“先拒后放”）
     │
NX-033 主机切换 5.1–5.6（负责人 B，半天到一天；建议在 AT-072 之后、没有发布的窗口里做）
     └─► AT-053 断连演练 · AT-054 隔离探测 · make sync-github 实测
     │
NX-036：public-preflight 与第三方声明（在 AT-072 文档修订之后，用同一个最终 SHA）
     │
调度员回填全部 AT 与任务证据，更新 ROADMAP §1–§3
     │
用最终 SHA 一次性生成完整的 NX-037 证据包 → NX-032 至 NX-037 依次标 done → rc 发布说明定稿 → 负责人签 tag
```

- **最长的一条链**是 NX-033 主机切换。它不依赖其他项，可以和 AT-050 第二次演练并行准备。但它会改变 main 的推进方式，建议和发布窗口错开。
- **“最终验收发布”要先定下来**：AT-055、AT-058、清点、public-preflight 和证据包都要基于同一个发布。建议在 AT-072 修订合入之后冻结一个发布，此后到 v0.1.0 之间只接受修复。

## 5. 工作量合计（估计）

| 执行者 | 内容 | 合计 |
|---|---|---|
| 负责人（B） | AT-072 演练；NX-033 主机切换；AT-050 第二次演练；DATA_HOST 的 sudo；public-preflight 确认；tag | 约 1.5–2 天 |
| 调度员（A） | 只读核对与探测；证据整理；生成证据包；回填 planning | 约 1 天 |
| 开发线 | 负责人删除工具（0.5 天）；AT-072 文档修订（0.5–1 天）；修复 public-preflight 或切换中发现的问题（未知） | 约 1–1.5 天，加未知项 |

## 6. 建议回填的已有证据（调度员判断，本清单不回填）

- **AT-050**：run-33 `d20261015a` 的结果（RTO、RPO、比对、生产库未受影响），以 `test_report` 或 `command` 记录；结论等第二次演练后再定。
- **AT-058**：run-42 五份报告的结论；等重测后再定。
- **AT-055**：发布 7 起的封存记录（含 12–19）与 run-46 的 digest 对照。
- **ROADMAP §1–§3**：AT-060 与 AT-071 已 passed；两机已部署；S5 出口条目的满足度应从“本地”改为“部分”。
