# S5 负责人操作总清单

面向负责人本人。把 S5（部署、本地 CI、发布、备份恢复、公开发布）与 NX-037（v0.1 受控真实闭环）中需要负责人本人执行或确认的步骤，按先后顺序合成一张清单。

- **依据**：[NX-032 部署运行手册](../implementation/NX-032-deploy-runbook.md)（步骤 0–8、AT-058）、[NX-033 设计](../implementation/NX-033-design.md) §8、[NX-034 设计](../implementation/NX-034-design.md) §4 与 [第二部分实现说明](../implementation/NX-034-part2-implementation.md)、[NX-035 设计](../implementation/NX-035-design.md) §7、[NX-036 实现说明](../implementation/NX-036-implementation.md) 与 [公开发布检查清单](public-release-checklist.md)、[NX-037 设计](../implementation/NX-037-design.md) 与 [运维手册 §9](../OPERATIONS.md#9-nx-037-受控真实闭环操作说明负责人本人执行)、[ROADMAP](../../planning/ROADMAP.md) §6 的 O1–O6 与 §7 的定案（到 2026-10-14）。
- **剩余工作核对**（逐任务分为已完成、缺代码、B 类步骤、负责人材料四类）：[S5 剩余工作核对（2026-10-14）](s5-status-20261014.md)。
- **现状**（main 2d10f20）：S4 已收口（AT-060 通过，NX-031 done）；v0.1 只剩 S5 的 NX-032～037 与 NX-052。
- **命令与核对细节**以上述文档为准，本清单只给顺序、材料、执行者、判据与对应编号。
- 只用角色名（管理机、APP_HOST、DATA_HOST、CI 主机）与参数（`$NEXLOOP_ROOT`、`<TENANT_ID>` 等）；不写主机名、地址、口令或任何真实值。
- **执行者**：“负责人”= 负责人本人；“调度员（逐步授权）”= 调度员提交命令，负责人逐步确认后执行。服务器、防火墙、凭据、真实外发、tag 与 Release 都属于 B 类。
- **预计耗时**为估计值，不含等待与排障。

## 0. 提前准备的材料

| 材料 | 用于 | 保管方式 | 状态 |
|---|---|---|---|
| 两台主机的 sudo 口令（或等效的提权方式） | 第 2–4 阶段的目录、用户、防火墙、加密卷 | 负责人本人输入，不交给任何会话 | 待准备 |
| 私有 stage env（白名单参数，0600）与 SSH 配置 | 渲染配置、dry-run、部署 | 管理机上 git 忽略的私有目录（O2） | 待准备 |
| 离线根 CA、中间 CA、服务与客户端证书 | TLS | 根 CA 私钥在平时不挂载的加密磁盘映像中（O2） | 已完成 |
| 发布签名私钥与 allowed_signers；负责人 tag 签名密钥 | 发布清单签名、tag 签名 | 密码管理器 + 加密离线备份（O1） | 已完成 |
| 备份加密口令（32 位以上随机） | restic 文件备份、pgBackRest | 密码管理器 + 纸质副本两处（O6） | 待准备 |
| 异地备份仓库账号（国内 S3 兼容对象存储，与两台主机不同的物理地点） | 异地备份 | 私有凭据文件 | 待准备 |
| GitHub deploy key（只限本仓库） | NX-033 同步 | 主机生成密钥对，负责人只在 GitHub 粘贴公钥（O5） | 待准备 |
| 模型 API 密钥（DeepSeek `deepseek-flash`） | NX-037 受控交互 | 私有凭据文件（`MODEL_CREDENTIALS_FILE`），只给 Agent Host | 待准备 |
| 企业交付端点：默认用项目提供的参考端点（APP_HOST 独立 Compose 项目，服务工单），需要员工凭据与服务凭据；接真实企业系统时另需其地址与凭据 | NX-037 真实交付动作（D1，2026-10-13 定案改用参考端点） | 私有文件；服务凭据只给 Effect Worker | 待准备 |
| 价值目录文件、渠道引用 | NX-037 真实租户初始化 | 私有目录 | 待准备 |
| 知情的内部人员（扮演客户） | NX-037 受控交互（D3） | — | 待安排 |
| 连续两个工作日（10:00–18:00） | NX-037 受控交互（D-i） | — | 待安排 |
| 模型账号侧的消费上限 | NX-037 费用第二道防线（D-g） | Provider 控制台 | 待设置 |

## 1. 决定与前置

| # | 做什么 | 前置条件 | 所需材料 | 执行者 | 预计耗时 | 完成判据 | 对应 |
|---|---|---|---|---|---|---|---|
| 1.1 | 决定部署后 CI 车道与压测车道是否继续与运行环境同机（D-b），须在第 3 阶段步骤 1 之前 | 调度员提交方案 | — | 负责人 | 30 分钟 | ROADMAP 记录定案 | ROADMAP D-b、R6 |

## 2. 构建并签名首个 stage 发布（NX-034）

逐条命令见 [首个签名发布运行手册](../implementation/NX-034-first-release-runbook.md)。

**前置条件现状（2026-10-14，上午按调度员通报更正）**：

| 前置条件 | 状态 |
|---|---|
| D-R1–R5 定案（方案 A 由 CI 构建、`nexloop-release` 签名封存；只读移交组；containerd snapshotter；临时限时出站；Caddy 2.x 最新稳定版） | 已满足（2026-10-13） |
| 发布签名密钥与 allowed_signers（O1）；CI 主机发布用户与签名材料 | 已满足 |
| syft、grype 固定版本（O3） | 已满足 |
| 基础镜像 `python-build-base`、`node-host-base` 按 digest 固定 | 已满足（`versions.lock.json`） |
| main 上有 CI 通过的提交 | 已满足：CI 合并基线已清零（s5t RESULT=PASS），此后各轮集成按合并规则通过；发布时取 gateway 固定提交之后、CI 通过的 SHA |
| gateway 按 digest 固定（运行手册 §3） | **已完成，待合入**：Caddy 2.11.7，只固定 amd64 manifest digest，`pinned_image_problems()` 为 `[]`；在分支 `nx034-pin-gateway`，随集成轮 int/s5d 跑 CI。D-R4 不再需要：CI 账户的 docker 经镜像加速即可按 digest 拉取，不必临时开放出站 |
| 移交目录与只读组（D-R2） | **已满足**：只读组 `nexloop-release-read` 已建，`$HANDOFF_ROOT` 与 `$RELEASE_ROOT` 的发布目录已建好，权限 2750（负责人已确认） |
| `nexloop-ci` 使用 containerd snapshotter（D-R3） | **已满足**：其 rootless docker 本来就是 containerd snapshotter，无需改配置 |
| 运行手册使用的 `$CI_QUEUE`（CI 结果）与 `$TRUSTED_REPO`（受信 bare 仓库） | **有方案，待合入**：L2 分支 `l2/release-ci-queue` 在现有车道上用 NX-033 队列产出发布用的 CI 结果，手册为 `docs/implementation/NX-034-release-ci-queue.md`（随该分支合入）；不需要先完成第 5 阶段的完整切换 |
| 发布用的应用镜像（backend、agent-host、web） | 由 2.2 的构建产生，不是前置条件 |


| # | 做什么 | 前置条件 | 所需材料 | 执行者 | 预计耗时 | 完成判据 | 对应 |
|---|---|---|---|---|---|---|---|
| 2.0 | gateway 固定 digest 的提交合入 main（已固定，随 int/s5d 跑 CI） | 该轮 CI 通过 | — | 调度员 | — | main 上 gateway 为 pinned，`pinned_image_problems()` 为空 | 运行手册 §3 |
| 2.1 | 在 CI 主机上对发布提交跑本地 CI，按发布用 CI 队列的手册产出结果文件 | 2.0 与 `l2/release-ci-queue` 已合入 | — | 调度员 | 约 40–50 分钟 | 结果文件写明该 SHA、`status=passed` | NX-033、NX-034 |
| 2.2 | 首次真实构建（运行手册 §2 第 1–17 步：build → sbom → stage 预检 → 移交 → 独立核对 → sign → seal → check） | 2.1（D-R2、D-R3 已满足） | 发布签名私钥 | 调度员（逐步授权），签名一步由负责人提供密钥 | 约 1 小时 | `archive.py check` 通过；归档已封存 | NX-034、AT-055 |

## 3. 双机部署（NX-032，按运行手册逐步执行）

每一步执行前后都跑只读 inventory 对比（运行手册“每一步通用的前后核对”），`existing_services_unchanged` 必须为 true。

| # | 做什么 | 前置条件 | 所需材料 | 执行者 | 预计耗时 | 完成判据 | 对应 |
|---|---|---|---|---|---|---|---|
| 3.0 | 步骤 0 剩余部分：校验参数、渲染配置、部署 dry-run、Compose 预检（CA 与证书已完成） | 2.2；私有 stage env | 私有 stage env | 调度员（逐步授权） | 1 小时 | dry-run 通过、渲染无残留占位符 | NX-032 |
| 3.1 | 步骤 1：两台主机建目录与服务用户 | 3.0 | sudo 口令 | 负责人本人或逐步授权 | 30 分钟 | 秘密目录 700、属主正确 | NX-032 |
| 3.2 | 为备份仓库、演练目录、秘密目录建 LUKS 加密卷（不重装系统） | 3.1 | sudo 口令、加密卷口令 | 负责人本人 | 1 小时 | 加密卷可挂载、开机行为符合预期 | NX-035 D3、O6 |
| 3.3 | 步骤 2：安装证书与秘密文件 | 3.1 | 证书与 DSN 文件 | 负责人本人或逐步授权 | 30 分钟 | `doctor --tls-spec` 与 `secret_distribution` 为 true | NX-032 |
| 3.4 | 步骤 3：加载防火墙规则（独立 nft 表，保持现有 SSH 会话不断开） | 3.3 | sudo 口令 | 负责人本人 | 30 分钟 | 去掉 NexLoop 表后规则集 hash 不变；SSH 仍可登录 | NX-032 |
| 3.5 | 步骤 4：启动 DATA_HOST 上的 PostgreSQL 与 Valkey | 3.4 | — | 逐步授权 | 20 分钟 | `--deploy-gate`（data）全部 true；明文连接被拒 | NX-032 |
| 3.6 | 步骤 5 前置：可验证的备份（空库也照做：`pg_dump` 并在隔离实例上恢复核对；NX-035 部署后改由迁移门槛强制） | 3.5 | — | 逐步授权 | 30 分钟 | 恢复核对通过 | NX-032 D5、AT-056 |
| 3.7 | 步骤 5（**须先完成 4.1**：迁移门槛要求清单里有 pgBackRest 的已验证备份）：开放 PG 运维窗口 → bootstrap 或 `nexloop-migrate` → 关闭窗口 → bootstrap 身份 NOLOGIN | 3.6 | 迁移身份 DSN | 负责人本人或逐步授权 | 30 分钟 | lineage 一致；`application_roles_unprivileged`、`bootstrap_identities_disabled` 为 true；窗口已关 | NX-032、AT-056 |
| 3.8 | 步骤 6：`load-check` 载入镜像 → 启动 APP_HOST（含 2 个 relay，Context v6）→ `promote` 登记为当前发布 | 3.7 | 发布目录、allowed_signers | 逐步授权 | 1 小时 | `--deploy-gate`（app）全部 true，`real_dispatch_closed` 为 true，relay 容器数与连接预算一致 | NX-032、AT-055 |
| 3.9 | AT-058 外部探测：从管理机、DATA_HOST、APP_HOST 三处运行 `probe_isolation.py` | 3.8 | 三份私有 probe spec | 逐步授权 | 1 小时 | 三份报告 `"passed": true` | AT-058 |
| 3.10 | 首次部署内容：只放合成租户，跑完 doctor 全套与 smoke | 3.9 | — | 调度员（逐步授权） | 1 小时 | doctor 全部通过，总闸关闭 | NX-032 D9 |
| 3.11 | 回滚演练（可选但建议）：`rollback-check` 与拒绝盲目回滚 | 3.8 | — | 逐步授权 | 30 分钟 | 拒绝跨越未声明迁移的回滚 | AT-057 |

## 4. 备份与恢复的部署部分（NX-035）

> 命令以 [NX-035 备份部署运行手册](../implementation/NX-035-deploy-runbook.md) 为准；与 NX-032 的先后关系见该手册：LUKS 卷最先建，迁移（3.7）之前必须有已验证的 pgBackRest 备份。
>
> **代码前置（尚未合入，见 [剩余工作核对](s5-status-20261014.md) §2）**：`nexloop-backup pg` 改用 pgBackRest 并写入清单（4.1 与 3.7 依赖）、`nexloop-restore`（4.3 依赖）、`nexloop-file-backup`（4.2 中 Artifact 与 Run 目录部分依赖）、两者的版本锁与许可登记。恢复日志的 restic 备份现在就能做。

| # | 做什么 | 前置条件 | 所需材料 | 执行者 | 预计耗时 | 完成判据 | 对应 |
|---|---|---|---|---|---|---|---|
| 4.1 | 运行手册第 2 步：安装并配置 pgBackRest（实测非超级用户下能否完成备份，做不了先报调度员）与 WAL 归档 | 3.5、3.2 | 备份加密口令 | 逐步授权 | 2 小时 | 完整备份 `verified`；WAL 归档健康 | NX-035、ROADMAP R3 |
| 4.2 | 配置 restic 文件备份（恢复日志分段、Artifact、已终态 Run 目录）与异地仓库 | 4.1 | 备份加密口令、异地仓库账号 | 逐步授权 | 2 小时 | 本地与异地快照可列出；恢复日志已静态加密（受控试运行前门槛） | NX-035 D5、O6 |
| 4.3 | 首次真实恢复演练：在一次性隔离环境恢复到命名恢复点，停在 `verified_readonly`，核对一致性与删除重放，记录 RTO/RPO，销毁环境 | 4.1、4.2 | — | 逐步授权 | 半天 | 演练记录写入，报告只有数字 | AT-050、AT-059 |
| 4.4 | 迁移门槛在部署环境生效：之后每次迁移前先做差异备份并校验 | 4.1 | — | 逐步授权 | — | `nexloop-migrate` 在无备份时拒绝 | AT-056 |

## 5. 本地 Git 队列与隔离 CI 的主机侧（NX-033 §8）

| # | 做什么 | 前置条件 | 所需材料 | 执行者 | 预计耗时 | 完成判据 | 对应 |
|---|---|---|---|---|---|---|---|
| 5.1 | 先只读核实 CI 主机能否运行 rootless Podman（做不了就提交 D2 修订，不改系统安全设置） | — | — | 调度员 | 30 分钟 | 结论记录 | ROADMAP R1 |
| 5.2 | 建 `nexloop-git` 与 `nexloop-ci` 用户、bare 仓库、hook、控制器与同步的 systemd unit | 5.1 | sudo 口令 | 负责人本人或逐步授权 | 2 小时 | 服务可启动；ref 规则生效 | NX-033 |
| 5.3 | 主机生成同步用密钥对，负责人在 GitHub 粘贴公钥作为只限本仓库的 deploy key | 5.2 | GitHub 账号 | 负责人本人 | 15 分钟 | `make sync-github` 能读远端 | O5 |
| 5.4 | 把现有 main 推入 bare 仓库；remote 改名（`origin` 指 LAN 仓库，`github` 指 GitHub），pre-push hook 同步调整；发放各推送身份的公钥 | 5.2 | — | 负责人本人 | 1 小时 | 各身份推送只能写自己的 ref | NX-033 |
| 5.5 | 隔离探测（运行器检查与 `isolation-probe`）与断连演练 | 5.4 | — | 逐步授权 | 1 小时 | 探测通过；GitHub 断开时 `sync_pending`、恢复后补推 | AT-053、AT-054 |
| 5.6 | 一次完整 CI 与现行车道结果对照，然后切换：此后 main 只由合并控制器推进 | 5.5 | — | 负责人确认切换 | 1 小时 | 结果一致；切换记录 | NX-033 |

## 6. 首个公开发布：`v0.1.0-rc`（NX-036、NX-034 §4，D-d）

| # | 做什么 | 前置条件 | 所需材料 | 执行者 | 预计耗时 | 完成判据 | 对应 |
|---|---|---|---|---|---|---|---|
| 6.3 | 运行 `release.py public-preflight`（九步），阅读第 9 步摘要并确认；首次公开前补上 stage 源码 SBOM 交叉核对（代码项） | 2.2（全历史审计与安全联系人已完成，见 §9） | — | 调度员运行；负责人确认摘要 | 1 小时 | 报告 `ok: true` | NX-036 |
| 6.4 | 整理 CHANGELOG 版本节，定稿 [rc 发布说明](v0.1.0-rc-notes.md)（填入验收摘要与来信速率） | 6.3 | — | 调度员起草；负责人确认 | 1 小时 | 发布说明无占位符 | NX-036 |
| 6.5 | 负责人本人在自己的机器上用 tag 签名密钥执行 `git tag -s vX.Y.Z <sha>`（是否先打 `v0.1.0-rc.<n>` 见 [发布 tag 执行单](run-release-tag.md) 的 D-tag-1，推荐直接 `v0.1.0`），以 maintainer 身份 `git push origin refs/tags/<tag>` 推到 LAN 仓库（pre-receive 按负责人 allowed signers 验证签名，tag 不可移动、不可删除），由部署中的同步（`--tag-signers`）推到 GitHub；发布 GitHub Release（附源码归档与 SHA256、公开版清单、SBOM；不附镜像与测试日志） | 6.4、5.3 | 负责人 tag 签名密钥 | 负责人本人 | 1 小时 | GitHub 上的 tag 通过 `git verify-tag` | NX-034 §4、AT-055 |

## 7. v0.1 受控真实闭环（NX-037）

前置：ROADMAP §8 的进入条件，以及第 3、4 阶段完成。企业交付默认用参考端点：先按其 [部署手册](../../deploy/stage/reference-endpoint/README.md) 在 APP_HOST 起独立 Compose 项目并通过一致性检查，再按 [租户装配手册](../implementation/NX-037-tenant-assembly.md) §1–§6 装配（企业 slot 只能经 enrollment 模式开通）。D-g 费用上限：累计 ¥100、单个 Run ¥2、租户月度 ¥300；D-i 计划：30 次交互（15/4/4/4/3），连续两个工作日 10:00–18:00。

| # | 做什么 | 前置条件 | 所需材料 | 执行者 | 预计耗时 | 完成判据 | 对应 |
|---|---|---|---|---|---|---|---|
| 7.1 | `real_tenant_init.py plan`（dry-run）核对步骤与 `Budget.set` 请求 | 第 3 阶段 | 价值目录、渠道引用 | 调度员 | 15 分钟 | 输出步骤与稳定 request_id | NX-037 |
| 7.2 | `real_tenant_init.py execute`（受信配置：服务授权、效应类别、告警规则） | 7.1 | configurator DSN、签名密钥、服务凭据文件 | 负责人本人 | 30 分钟 | 执行成功，无开关打开 | NX-037 |
| 7.3 | 负责人在工作台提交 `Budget.set`（累计上限为试运行期间的 model 预算）；在模型账号侧设消费上限 | 7.2 | 工作台登录 | 负责人本人 | 15 分钟 | `real_tenant_init.py verify` 除开关外全部通过 | NX-037 D-g |
| 7.4 | 配置真实交付端点与凭据（只给 Effect Worker）、模型凭据（只给 Agent Host）；按计划在工作台打开三层真实派发开关（只对该租户与选定 Action） | 7.3、6.5（D-d 顺序） | 交付端点凭据、模型 API 密钥 | 负责人本人 | 1 小时 | 实际开关与交互计划的 `real_dispatch` 完全一致 | NX-037 D1–D2、真实派发开关 |
| 7.5 | 编辑交互计划（顺序 15/4/4/4/3，`real_dispatch` 按实际计划），先用合成模式演练一次 | 7.4 | — | 调度员 | 1 小时 | 合成演练完成 | NX-037、OPERATIONS §9.4 |
| 7.6 | 运行 real 模式的前 3 次（校准），核对单次费用后用 `--resume <确认引用>` 继续 | 7.5 | 内部人员、负责人会话文件、metrics DSN | 负责人本人 | 半天 | 校准费用经负责人确认 | AT-071、D-g |
| 7.7 | 完成其余 27 次交互；遇停止按 SECURITY.md 处置，遇暂停复核后恢复 | 7.6 | 连续两个工作日、内部人员 | 负责人本人 | 两个工作日 | 30 次完成，无停止规则命中，不明效应全部了结 | AT-071 |
| 7.8 | 关闭三层开关；生成证据包 | 7.7 | — | 调度员 | 1 小时 | 证据包只含允许的值，缺失项如实标注 | NX-037、14 §7 |
| 7.9 | 第二维护者演练：负责人本人只按公开文档在一次性环境完成一次“发布 + 恢复”，记录手册偏差 | 第 4 阶段 | 一次性环境 | 负责人本人 | 半天 | 演练记录与偏差清单 | AT-072、ROADMAP D-h |
| 7.10 | 在 NX-037 全部完成后打 `v0.1.0`（同 6.3–6.5 的门槛与流程；签名、推送与 GitHub 验证按 [发布 tag 执行单](run-release-tag.md)） | 7.8、7.9 | 负责人 tag 签名密钥 | 负责人本人 | 2 小时 | 正式版发布 | D-d |

## 8. 部署之后的周期性操作

- 服务证书 90 天轮换；中间 CA 与备份口令每年轮换；
- 恢复演练每月一次，重大 Schema 或 Pi 存储升级后各一次；
- 每次迁移开放一次 PG 运维窗口，迁移前先做差异备份并校验。

## 9. 已完成

| 项 | 内容 | 依据 |
|---|---|---|
| O1 | 发布签名密钥离线生成，公钥与 allowed_signers 入库；负责人 tag 签名密钥单独一把 | ROADMAP §6 |
| O4 | 开启 GitHub 私密漏洞报告（不公开个人邮箱） | ROADMAP §6 |
| O3 | CI 主机安装固定版本的 syft、grype 并登记校验值 | ROADMAP §6、更新记录 |
| 网络 | 开发机到 APP_HOST 的有线直连（主用） | ROADMAP §5 |
| CI 主机发布用户与签名密钥 | 发布构建所需的用户与签名材料已就位 | 调度员记录 |
| 运行手册步骤 0 的 CA 与证书 | 离线根 CA、中间 CA、服务与客户端证书已签发 | NX-032 运行手册步骤 0 |
| D-a、D-g、D-h、D-i | 容量（200 客户、持续来信 ≤ 0.1 条/秒）、费用上限、第二维护者（负责人本人）、交互计划已定案 | ROADMAP §6–§7 |
| AT-015 | 真实浏览器断线重连核对已通过 | planning |
| 原 1.2、S4 出口 | AT-060 正式整轮通过（200 客户、矩阵 v2、relay 2 个进程），NX-031 done，S4 收口（2026-10-14） | ROADMAP §7 |
| 原 6.1 | 全历史审计结论经负责人确认（D9，2026-10-12） | ROADMAP §7 |
| 原 6.2 | 安全联系人填入 public-release.v1.json、SECURITY.md、CODE_OF_CONDUCT.md（O4） | ROADMAP §7 |
| D-R1～D-R5 | 首个签名发布的构建方式（方案 A）、移交只读组、containerd snapshotter、临时限时出站、Caddy 2.x 最新稳定版（2026-10-13） | ROADMAP §7、NX-034 首个签名发布运行手册 §5 |
| 空闲派发门槛 | v0.1 的 `idle_dispatch_p95_ms`（受理→claim，C4-low）门槛为 ≤5 s；2 s 为 v0.2 目标（2026-10-13） | ROADMAP §7 |
| NX-052 判据修订 | C4-low 格按 0.1/s 只比较 K 对 N、P 对 N 三项 p95 增幅（各 ≤5%），不再要求该格门槛判定为 passed；其余门槛项仍须通过（2026-10-14） | ROADMAP §7、NX-052 实现说明 §11 |
| ADR-029a、ADR-031、ADR-032 | relay 上下文准备优化：同一条来信内复用 Source 签名信封；一次对象读取内只派生一次 Source 权限（0191）；一次读取授权检查内只解析一次调用者身份（0192）。已批准并合入 main（int/s5c） | adr/ |
| 参考企业交付端点 | 负责人暂无企业端点，由项目提供参考端点（APP_HOST 独立 Compose 项目、服务工单语义），已合入（2026-10-13） | ROADMAP §7 |
| relay 双进程 | stage 默认 2 个 relay，连接预算方案 B 合计 59/60；登录高峰复测已完成（identity 池 3 时 ACK p95 885 ms，没有变差），方案 B 可行 | relay2-budget |
| D-R2、D-R3 | 只读组 `nexloop-release-read` 与移交、发布目录（2750）已建并经负责人确认；`nexloop-ci` 本来就是 containerd snapshotter（2026-10-14） | 调度员通报 |
| NX-052 测量方式（1A/2A） | N、K 各跑 3 轮，取 p95 中位数；P 的 ACK 写进限制说明（负责人 2026-10-14）。N 状态已跑通（删除 134 个外键） | 调度员通报 |
| C4-low 优化后 | 空闲派发 p95 3194 ms（门槛 5 s），context_prepare p50 1213 ms | 调度员通报 |
