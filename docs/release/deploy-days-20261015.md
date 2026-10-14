# 部署日执行单：2026-10-15（周四）至 10-16（周五）

把几份运行手册中的部署步骤，按这两天的四个时段排成一张执行单。之后的排期是：10/17（周六）校准，10/18–19 受控交互，10/20 验收（ROADMAP §12）。

- **依据**（命令细节以原手册为准，本单只定顺序、执行者、判据和撤销方式）：
  - [NX-032 部署运行手册](../implementation/NX-032-deploy-runbook.md)：步骤 0–8、AT-058、首次部署内容；
  - [NX-035 备份部署运行手册](../implementation/NX-035-deploy-runbook.md)：LUKS、pgBackRest、restic、恢复演练门槛、定时任务；另见 [pgBackRest 执行者](../implementation/NX-035-pgbackrest.md)、[文件备份执行者](../implementation/NX-035-file-backup.md)、[恢复命令行](../implementation/NX-035-restore-cli.md)；
  - [NX-034 首个签名发布运行手册](../implementation/NX-034-first-release-runbook.md) §4（部署 dry-run 与 Compose 预检），以及 [发布用 CI 结果](../implementation/NX-034-release-ci-queue.md)；
  - [NX-033 设计](../implementation/NX-033-design.md) §8；
  - [参考企业交付端点部署手册](../../deploy/stage/reference-endpoint/README.md)；
  - [S5 负责人操作总清单](s5-owner-checklist.md)。
- **只用角色名和参数**：管理机、APP_HOST、DATA_HOST、CI 主机；`$SSH_ALIAS_APP`、`$NEXLOOP_ROOT`、`<TENANT_ID>` 等。取值都在负责人的私有 env 文件里，本单不写任何地址、主机名或口令。
- **真实租户名**：只记在负责人的私有文件里。公开文档、证据文件和提交说明一律写作 `<TENANT_DISPLAY_NAME>`，不写真实名字。
- **执行者**：
  - “调度员”：调度员提交并执行命令；
  - “负责人”：负责人本人执行；
  - “调度员（逐步授权）”：调度员逐条提交命令，负责人逐条确认后才执行。
- **B 类**：凡改动服务器、防火墙、凭据或数据的步骤都属于 B 类，每一步都要负责人确认。
- **sudo**：凡标“负责人输入 sudo”的步骤，sudo 口令由负责人本人在终端输入，不交给任何会话。DATA_HOST 上的 sudo 一律由负责人亲自输入。LUKS 口令也由负责人在 `ssh -t` 的终端里输入。
- **耗时**都是估计值，不含排障时间。每个时段末尾都留了缓冲。
- **每一步的前后核对**：每一步执行前后都跑只读 inventory 对比（NX-032 手册“每一步通用的前后核对”），`existing_services_unchanged` 必须为 true。下文不再逐步重复。

## 硬约束（任何时段都不得违反）

1. **迁移顺序**：首次 pgBackRest 全量备份写入清单并经校验（`verified`）之前，不执行任何迁移。`nexloop-migrate` 没有豁免开关（NX-035 迁移门槛，AT-056）。
   - 第一天只对**空库**做初次 bootstrap，它是建库，不是迁移；备份执行者写清单要用到的函数，也是 bootstrap 建出来的。
   - 此后对这个库做的任何迁移，都必须先通过 `authz.nexloop_migration_gate()`，即存在 60 分钟以内、catalog 与当前库一致的已验证备份。
   - 这一点的理解请调度员在开工前确认，见文末“待确认”。
2. **relay**：APP_HOST 运行 2 个 relay（`message-relay`、`message-relay-2`），连接预算按方案 B（合计 59/60，两个 API 的 `--identity-pool-max` 都是 3，见 [relay2-budget](../implementation/relay2-budget.md)）。部署门禁会核对 relay 容器数与预算一致。
3. **Context v6**：relay 带 `--context-strategy recent_plus_required`，Agent Host 运行配置的 `context_input_protocol` 是 `nexloop.context-pack.v6`。不得去掉这个参数。
4. **参考企业交付端点**：在 APP_HOST 上作为**独立 Compose 项目**运行，有自己的项目名和卷，不加入 NexLoop 的网络，也不连 NexLoop 的数据库。
5. **首次部署只放合成租户**，`REAL_DISPATCH_ENABLED=false`；三层真实派发开关在这两天都保持关闭（NX-032 D9）。

## 开工前置（10/14 晚之前核对；任一项不满足，就先调整排期）

| # | 项 | 执行者 | 判据 |
|---|---|---|---|
| P1 | 已封存的签名发布 `$RID`：按发布用 CI 结果手册产出该 SHA 的 CI 结果，再按首个签名发布手册 §2 第 1–17 步构建、签名、封存 | 调度员；签名一步由负责人提供密钥 | `archive.py check` 输出 `ok: true`；gateway 已按 digest 固定 |
| P2 | DATA_HOST 的 PG 镜像带有与锁文件一致的 pgBackRest 版本，并写入发布清单（NX-035 手册 2.0 第 2 项） | 调度员 | 发布清单中有该镜像的 digest。**这一项目前未见完成，未满足时 pgBackRest 无法在容器内归档，周四下午的备份步骤会被挡住** |
| P3 | pgBackRest 和 restic 的安装包已在管理机上下载，并与官方校验值核对一致（下载要负责人确认） | 负责人确认，调度员执行 | sha256 与官方值一致，并回填 `versions.lock.json` 的 `deployment_tools` |
| P4 | 私有材料齐全：私有 stage env（0600）与 SSH 配置；服务与客户端证书（已签发）；备份口令（pgBackRest repo1/2、restic repo1/2）；各 LUKS 口令；S3 桶与专用子账号凭据；参考端点的服务凭据与员工凭据 | 负责人 | 都在密码管理器和私有目录中；不进入仓库，也不交给任何会话 |
| P5 | 两台主机可用空间（只读 inventory）：按 NX-035 手册 1.1 估算各 LUKS 卷，合计不超过可用空间的 70% | 调度员 | 估算记录在私有证据目录 |
| P6 | LUKS 开机解锁方式：按 NX-035 手册 1.3 选定，推荐 A“人工解锁” | 负责人 | 决定已记录 |
| P7 | D-b：部署后，CI 车道与压测车道是否继续与运行环境同机 | 负责人 | ROADMAP 已记录决定 |

**NX-033 §8**：完整切换到合并控制器不在这两天做。发布用的 CI 结果已改由现有车道按 NX-033 队列产出。§8 第 5 步的隔离探测和第 7 步的切换，另找时间进行。

## 周四上午：准备、加密卷、目录与秘密

| # | 步骤 | 执行者 | B 类 | sudo | 耗时 | 完成判据 | 失败时撤销 |
|---|---|---|---|---|---|---|---|
| 1.1 | **参数与渲染**（NX-032 步骤 0）：`render.py --check`，再渲染 `pg_hba.conf`、`postgresql.conf`、`valkey.conf`、`data-host.nft`、`app-host.nft`。`postgresql.conf` 在渲染时就写入 NX-035 2.4 的归档三项（`archive_mode`、`archive_command`、`archive_timeout`），避免启动后再重启一次 PG | 调度员（管理机） | 否 | 否 | 30 分钟 | `valid: true`；渲染结果中没有 `__X__` 残留；`pg_hba.conf` 最后四行是 reject | 删除 `$RENDER_DIR` |
| 1.2 | **部署 dry-run 与 Compose 预检**（首个签名发布手册 §4 第 1–4 步）：app 和 data 两个 dry-run；拼出 `$PRIVATE_COMPOSE_APP_ENV` 与 `$PRIVATE_COMPOSE_DATA_ENV` 并交叉核对；两份 `docker compose config -q` | 调度员（管理机）；私有 env 由负责人拼接 | 否 | 否 | 45 分钟 | 两个 dry-run 都是 `ready: true`；`compose_images_from_manifest: true`；`config -q` 退出码 0；app 的 Compose 中有 2 个 relay，且带 `--context-strategy recent_plus_required` | 删除 `$PLAN_DIR_*`；私有 env 保留 |
| 1.3 | **执行前 inventory**（两台主机，只读） | 调度员 | 否（只读） | 否 | 15 分钟 | 两份 before 文件（0600）存入 `$EVIDENCE_DIR` | — |
| 1.4 | **LUKS 加密卷**（NX-035 步骤 1）：DATA_HOST 建 `nexloop-secrets`、`nexloop-file-repo`；APP_HOST 建 `nexloop-secrets`、`nexloop-pg-repo`、`nexloop-drill`。文件型容器，不重新分区 | 负责人本人（DATA_HOST），APP_HOST 调度员逐步授权 | 是 | 是：负责人输入 sudo 与 LUKS 口令（每个卷三处） | 1.5 小时 | 每个卷 `luksDump` 显示 `Version: 2`；`findmnt` 显示挂载自 `/dev/mapper/nexloop-*`；挂载点 700，属主正确 | 卷为空时：`umount` → `cryptsetup close` → 删除镜像文件（先 `ls -A` 确认卷是空的） |
| 1.5 | **目录与服务用户**（NX-032 步骤 1）：`$NEXLOOP_ROOT/secrets` 落在刚建的加密卷上；APP_HOST 建 `config`、`data/artifacts`、`data/runtime`、`data/relay-vault`、`data/relay-vault-2`，以及每个服务一个 `secrets/<service>`；建服务用户 | 负责人本人（DATA_HOST），APP_HOST 调度员逐步授权 | 是 | 是 | 30 分钟 | 秘密目录 700、属主是对应服务；两个 relay vault 各自 0700 | `sudo rm -r` 本步建的空目录（先 `ls -A`）；`sudo userdel` |
| 1.6 | **证书与秘密文件**（NX-032 步骤 2）：PG、Valkey、Gateway 服务证书，各客户端证书，DSN（`verify-full`）；每个文件只放进所属服务的目录。Agent Host 只放模型凭据的位置，此时不放真实模型密钥（见周六就绪检查） | 调度员（逐步授权）；DATA_HOST 上的 `chmod`、`chown` 由负责人输入 sudo | 是 | 是（DATA_HOST） | 45 分钟 | `doctor --tls-spec`：`tls_certificates`、`tls_dsn_verify_full`、`tls_valkey_only` 都为 true；Agent Host 目录里没有 DSN、凭据或签名密钥 | 删除本步放入的文件 |
| — | 缓冲与午间核对 | — | — | — | 30 分钟 | 上午全部判据已记录 | — |

## 周四下午：防火墙、数据层、建库与首次备份

| # | 步骤 | 执行者 | B 类 | sudo | 耗时 | 完成判据 | 失败时撤销 |
|---|---|---|---|---|---|---|---|
| 2.1 | **防火墙**（NX-032 步骤 3）：两台主机加载独立的 `inet nexloop_stage` 表；先 `nft -c` 只做检查，再加载。保持当前 SSH 会话不断开，另开一个会话验证 | 负责人本人（DATA_HOST）；APP_HOST 调度员逐步授权 | 是 | 是 | 30 分钟 | `nft -c` 没有输出；去掉 NexLoop 表之后规则集 hash 不变；新会话仍可 SSH 登录；APP_HOST 的规则中有效应 worker 访问参考端点的那一条出站规则 | `nft delete table inet nexloop_stage`（现有 UFW 和 Docker 规则不动） |
| 2.2 | **启动数据层**（NX-032 步骤 4）：`nexloop-data` 项目，PG 镜像带 pgBackRest（前置 P2），归档配置在 1.1 已写入 | 调度员（逐步授权） | 是 | 否（docker 组；如果需要 sudo，由负责人输入） | 30 分钟 | PG、Valkey 都 healthy；`--deploy-gate`（data）的 `port_bindings`、`planned_ports_free_before`、`existing_services_unchanged` 都为 true；`sslmode=disable` 连接被拒；`show archive_mode` 为 `on` | `docker compose -p nexloop-data down`（不加 `-v`） |
| 2.3 | **空库初次 bootstrap**（NX-032 步骤 5 的首次建库部分）：先对空库 `pg_dump -Fc`，在一次性实例上恢复核对（D5）→ 开运维窗口（步骤 7）→ 用迁移身份执行 bootstrap，到发布清单对应的 catalog head → 关窗口 → `ALTER ROLE nexloop_bootstrap NOLOGIN` | 负责人本人或逐步授权；DATA_HOST 上 `pg_hba` 和 nft 的改动由负责人输入 sudo | 是 | 是 | 45 分钟 | `bootstrap.verify()` 的 lineage 一致；`application_roles_unprivileged` 与 `bootstrap_identities_disabled` 都为 true；窗口已关闭（从管理机连 `$PG_PORT` 被拒或超时）；**没有执行任何 `nexloop-migrate`** | 库是空的，可以整体重建：`down` 后删除数据目录要负责人单独确认；不运行 down 迁移 |
| 2.4 | **pgBackRest 安装与仓库**（NX-035 2.1–2.3）：APP_HOST 安装固定版本，建 `$PGBACKREST_REPO_USER`；DATA_HOST 到 APP_HOST 的受限身份（推荐 TLS 服务器模式，复用 NX-032 的 CA）；两台主机渲染 `pgbackrest.conf`（0600，`grep -c '__'` 为 0） | 调度员（逐步授权）；DATA_HOST 部分由负责人输入 sudo；口令由负责人从密码管理器填写 | 是 | 是 | 1 小时 | `pgbackrest version` 与锁文件一致；受限身份只能执行 pgbackrest；配置中没有占位符 | 删除二进制和用户；删除这把公钥或 TLS 证书；删除配置文件 |
| 2.5 | **stanza 与首次全量备份**（NX-035 2.5）：`stanza-create`、`check`；再用执行者 `nexloop-backup pg … --repo repo1 --kind pg_full`，以及 `--repo repo2`；最后 `info` | 调度员（逐步授权） | 是 | 否 | 1 小时（视数据量） | `check` 输出 `completed successfully`；清单中 repo1、repo2 各有一行 `pg_full`，状态 `verified`；`doctor --backup` 的 `backup_age` 和 `wal_archive_lag` 通过（repo2 还有 `offsite_backup_age`）。**到这一步，迁移门槛才可能允许迁移** | `stanza-delete` 会删除整个 stanza，属于破坏性操作，只在首次失败时使用，并须负责人单独确认 |
| — | 缓冲与当日收尾：重新跑一次 inventory，核对 `existing_services_unchanged`；确认 LUKS 卷挂载、PG 归档在前进 | 调度员 | 否 | 否 | 30 分钟 | 当日判据已记入证据目录 | — |

## 周五上午：restic、迁移门槛、恢复演练、启动应用

| # | 步骤 | 执行者 | B 类 | sudo | 耗时 | 完成判据 | 失败时撤销 |
|---|---|---|---|---|---|---|---|
| 3.1 | **restic**（NX-035 3.1–3.3）：DATA_HOST 用于恢复日志，APP_HOST 用于文件；建 `$FILE_BACKUP_USER`；初始化文件 repo1（DATA_HOST 的加密卷）和 repo2（S3）；首次 `nexloop-backup journal` 与恢复日志的 restic 备份；首次 `nexloop-file-backup --kind artifact` 与 `--kind runtime`，repo1、repo2 各一次 | 调度员（逐步授权）；DATA_HOST 上的安装由负责人输入 sudo；S3 初始化前 `set +x` | 是 | 是 | 1.5 小时 | `restic version` 与锁文件一致；`check` 输出 `no errors were found`；清单中有 `journal` 行和两类 `files_*` 的 `verified` 行（此时 Artifact 与 Run 都还是空的，文件数为 0 也算）；`doctor --backup` 的 `restore_journal_lag` 和 `file_backup_age` 通过 | 仓库中还没有快照时，删除本地仓库目录；S3 前缀由负责人在服务商控制台确认后删除；删除二进制和用户 |
| 3.2 | **迁移门槛核对**：`select authz.nexloop_migration_gate()`（只读，metrics 或 owner 身份）。如果发布的 catalog 比 2.3 建库时新（正常情况下不会），这时才执行 `nexloop-migrate`，随后补一次 `pg_diff`（NX-035 2.6） | 调度员；如需迁移，负责人逐步授权并开关运维窗口 | 迁移是 B 类 | 窗口开关需要（DATA_HOST，负责人输入） | 20 分钟 | 门槛返回 `allowed: true`，或者没有待执行的迁移；迁移后 `bootstrap.verify()` 一致，新的 `pg_diff` 行为 `verified` | 不运行 down 迁移；需要回退时进入 NX-035 恢复流程 |
| 3.3 | **恢复演练门槛**（NX-035 步骤 4，第一次）：打恢复点 `drill-mark` → 提交一次合成删除 → 用 `nexloop-restore` 恢复到 `$DRILL_ROOT`（同一 PG 镜像，`--network none`，不挂任何凭据）→ 停在 `verified_readonly` → 记录 RTO 与 RPO → 销毁 | 调度员（逐步授权）；建立和销毁演练目录由负责人输入 sudo | 是 | 是 | 2 小时 | 报告 `passed: true`；RTO ≤ 2 小时，RPO ≤ 5 分钟；合成删除在恢复后已重放；生产库 `restore_state` 仍是 `open`；`restore_drill_overdue` 通过 | 只销毁演练目录（先核对目录标识）；生产库只多了只追加的恢复点和清单行，不回退 |
| 3.4 | **启动应用**（NX-032 步骤 6）：`deploy.py load-check` 核对镜像 → `docker compose -p nexloop-app … up -d` → 门禁通过后 `deploy.py promote` | 调度员（逐步授权） | 是 | 否 | 1 小时 | `--deploy-gate`（app）全部为 true，包括 `guard_workers_exact`、`port_bindings`（内部端口只绑 127.0.0.1）、`real_dispatch_closed`、`agent_host_concurrency`、TLS 各项、`existing_services_unchanged`；relay 容器数为 2，与预算一致；`/health/ready` 只报告已核实的能力；`promote` 建立 `current` | `docker compose -p nexloop-app down`（数据目录保留）；`promote` 失败时两个链接自动恢复原样 |
| — | 缓冲 | — | — | — | 30 分钟 | — | — |

## 周五下午：核对、外部探测、参考端点、定时任务

| # | 步骤 | 执行者 | B 类 | sudo | 耗时 | 完成判据 | 失败时撤销 |
|---|---|---|---|---|---|---|---|
| 4.1 | **首次部署内容与 smoke**（NX-032 D9）：只放合成租户；跑 doctor 全套（deploy-gate、`--tls-spec`、`--backup`、`--relay-source`）和 smoke | 调度员 | 否（只读加合成数据） | 否 | 45 分钟 | doctor 全部通过；总闸关闭；relay 产出 v6 上下文（stage compose 测试已核对，这里只看 Run 正常完成） | 合成租户的数据留在库中，不回退 |
| 4.2 | **AT-058 外部探测**：管理机、DATA_HOST、APP_HOST 三处，各用一份私有 spec 运行 `probe_isolation.py`；包括 Agent Host 独立命名空间的两条负例 | 调度员（逐步授权）；DATA_HOST 上的运行由负责人确认 | 只读探测 | 否 | 1 小时 | 三份报告都是 `"passed": true` | 无（只读） |
| 4.3 | **参考企业交付端点**（端点部署手册 §1–§4）：在 APP_HOST 上建独立 Compose 项目，有自己的项目名和卷；服务证书由内部 CA 签发（SAN 为 APP_HOST 的名称与地址）；`service_token` 与 `staff_token` 是两个不同的私有文件；启动后运行 `endpoint_conformance.py`，报告 `ok: true` | 调度员（逐步授权）；凭据由负责人生成并放入 | 是 | 是（秘密目录） | 1 小时 | `config -q` 通过；容器非 root，根文件系统只读；一致性检查 `ok: true`；端点不在 NexLoop 的网络中 | `docker compose -p <端点项目> down`（不加 `-v`）；删除私有凭据 |
| 4.4 | **定时任务与告警**（NX-035 步骤 5）：`nexloop-pg-full`、`nexloop-pg-diff`、`nexloop-journal`、`nexloop-journal-restic`、`nexloop-restic-maint`（DATA_HOST），`nexloop-file-backup`（APP_HOST），都加上 `RequiresMountsFor=` | 调度员（逐步授权）；DATA_HOST 上的 unit 安装由负责人输入 sudo | 是 | 是 | 45 分钟 | `systemctl list-timers 'nexloop-*'` 的下次执行时间与计划一致；首次执行后清单中出现对应的行；告警规则包含 `file_backup_age`、`restore_journal_lag` 等 | `systemctl disable --now <timer>`、删除 unit、`daemon-reload`；仓库内容不动 |
| 4.5 | **回滚核对演练**（可选，建议做，AT-057）：`deploy.py rollback-check`，确认会拒绝盲目回滚 | 调度员 | 否 | 否 | 30 分钟 | 拒绝跨越未声明迁移的回滚；首次部署还没有 `known_good`，结果如实记录 | 无 |
| 4.6 | **收尾**：两台主机的 after inventory 与 `existing_services_unchanged`；确认运维窗口已关闭、LUKS 卷挂载、PG 归档前进、首批定时任务已执行；把证据（只含代号、数字、哈希）放入私有证据目录，并交调度员回填 planning | 调度员；负责人确认 | 否 | 否 | 45 分钟 | `existing_services_unchanged` 为 true；`doctor` 全部通过；证据清单齐全 | — |

## 周六校准前的就绪检查（10/16 傍晚或 10/17 开跑前）

对齐 NX-037 的开跑清单。这份清单（OPERATIONS §9.4a）在本单的基线 main bf8597e 上**还没有合入**，下表按 ROADMAP 2026-10-14 的参与者变更与 [运维手册 §9](../OPERATIONS.md#9-nx-037-受控真实闭环操作说明负责人本人执行) 编写。§9.4a 合入后逐项对照，以它为准。

| # | 项 | 执行者 | 判据 |
|---|---|---|---|
| R1 | **负责人授权文件**：D3 改为负责人一次性操作授权（AI 代理在浏览器中扮演消费者，以及可选的员工侧接管、工单标记）。私有文件，0600 | 负责人 | 文件存在于私有目录，格式按 §9.4a |
| R2 | **代理参与者登记**：逐个登记 AI 代理参与者；它们使用的消费者账号标为测试人设，可在分析与客户事实中排除 | 负责人（调度员准备清单） | 每个代理参与者都有登记记录；账号的测试人设标记已核对 |
| R3 | **真实租户初始化**：`real_tenant_init.py plan` → `execute`（受信配置）→ `verify`，除开关外全部通过（OPERATIONS §9.2） | plan 由调度员执行；execute 由负责人执行 | `verify` 除三项开关外都是 ready |
| R4 | **租户装配**（[租户装配手册](../implementation/NX-037-tenant-assembly.md) §1–§6）：消费者 WebChat 账号（给代理用）、工作台成员、business 与 catalog setup、relay 登记，以及企业 slot（`nexloop-enterprise-setup` 加 `nexloop-relay-enroll` 的企业键） | 负责人（逐步授权） | relay profile 带 `enterprise` 块；relay 日志中没有 `enterprise_slot_skipped` |
| R5 | **预算**：负责人在工作台提交 `Budget.set`（累计 ¥100，作为试运行期间的 model 预算）；在 DeepSeek 账号侧设人民币消费上限，作为第二道防线 | 负责人本人 | `real_tenant_init.py verify` 的预算项通过；账号侧上限已设置 |
| R6 | **DeepSeek 凭据文件**：由负责人按下文“模型凭据写入”两条命令，写进 APP_HOST 上 Agent Host 的私有凭据文件 `<MODEL_CREDENTIALS_FILE>`（0600，属主为 Agent Host 服务用户）；Host 运行配置使用 `deepseek-flash`、`cost_currency` 为 `CNY`、人民币价目文件 | 负责人本人写入；调度员只核对配置、权限和字节数，不读也不转发这个值 | 权限 `600`、属主 UID 正确、字节数与本地读出的一致；Host 启动无拒绝；`run_interactions.py` real 前置中的币种与 Host 检查通过 |
| R7 | **根证书**：内部 CA 的根证书装进负责人浏览器，以及代理所用浏览器的信任库，用于 Gateway 的 TLS | 负责人本人 | 浏览器访问 WebChat 与工作台不出现证书警告 |
| R8 | **三层真实派发开关**：此时仍全部关闭；核对交互计划 v2 的 `real_dispatch`（第三层只开 `eios:action:nexloop.enterprise.deliver:1`）。开关在周六校准开始时，由负责人按计划打开 | 负责人 | 实际开关与计划可以一一对应；`real_dispatch_closed` 仍为 true |
| R9 | **参考端点员工凭据**：员工页面能用 `staff_token` 登录（工作时间内处理工单） | 负责人 | 登录成功；一致性检查留下的两张测试工单可以标记“已处理” |
| R10 | **运行入口的私有文件**：metrics DSN、负责人会话文件、交互计划的私有副本、试运行证据目录 | 负责人 | 都是 0600 普通文件；合成模式演练一次（OPERATIONS §9.4）已完成 |

### 模型凭据写入（负责人本人在开发机终端执行）

- **来源**：DeepSeek key 在开发机仓库根目录的 `.env` 中，键名为 `MODEL_API_KEY`。这个文件已被 git 忽略，权限 0600，从未入库。
- **目标**：APP_HOST 上 Agent Host 的私有凭据文件 `<MODEL_CREDENTIALS_FILE>`，0600，属主为 `<AGENT_HOST_UID>`。Host 读取文件内容并去掉首尾空白，文件里只放 key 本身，不放 `KEY=` 前缀。
- **保证**：
  - key 只在管道里流动：不出现在任何命令行参数、环境变量、shell 历史或日志中，命令文本本身不含 key；
  - 中转文件放在 APP_HOST 的 `/dev/shm`（内存文件系统，不落系统盘），权限 0600，安装后立即删除；
  - 核对只看权限、属主和字节数，从不回显内容。调度员不读、不转发这个值。

第 1 条，在开发机的仓库根目录执行。它从 `.env` 取出值，去掉回车、可能的引号和换行（顺序如此，Windows 行尾的文件也能正确去引号），经 ssh 的标准输入写进 APP_HOST 的内存中转文件，然后只打印字节数：

```bash
sed -n 's/^MODEL_API_KEY=//p' .env | head -n 1 | tr -d '\r' | sed -e "s/^[\"']//" -e "s/[\"']\$//" | tr -d '\n' | ssh "$SSH_ALIAS_APP" 'umask 077 && cat > "/dev/shm/nexloop-model-key.$(id -u)" && wc -c < "/dev/shm/nexloop-model-key.$(id -u)"'
```

在本地用同一条管道只数字节数，与上面打印的值比对：

```bash
sed -n 's/^MODEL_API_KEY=//p' .env | head -n 1 | tr -d '\r' | sed -e "s/^[\"']//" -e "s/[\"']\$//" | tr -d '\n' | wc -c
```

第 2 条：sudo 口令由负责人在 `ssh -t` 的终端里输入。它把中转文件安装为 Agent Host 的私有凭据文件，删除中转文件，再打印权限、属主 UID 和字节数：

```bash
ssh -t "$SSH_ALIAS_APP" 'f="/dev/shm/nexloop-model-key.$(id -u)"; sudo install -o <AGENT_HOST_UID> -g <AGENT_HOST_UID> -m 0600 "$f" "<MODEL_CREDENTIALS_FILE>" && rm -f "$f" && sudo stat -c "%a %u %s" "<MODEL_CREDENTIALS_FILE>"'
```

- **判据**：
  - 两次字节数相同，且大于 0；
  - 第 2 条输出 `600 <AGENT_HOST_UID> <字节数>`；
  - `/dev/shm` 下已经没有中转文件。
  - 字节数为 0，说明 `.env` 中没有这个键，或格式不符。这时停下，由负责人自己检查 `.env`，不要把文件内容贴给任何会话。
- **撤销**：`ssh -t "$SSH_ALIAS_APP" 'sudo rm -f "<MODEL_CREDENTIALS_FILE>"; rm -f "/dev/shm/nexloop-model-key.$(id -u)"'`。轮换 key 时，在 DeepSeek 控制台作废旧 key 后重做这两条。
- **不要做的事**：不要用 `scp` 拷贝 `.env`，不要用 `echo "$KEY"`，不要 `export` 这个值，也不要用 `cat .env` 查看。

## 待确认（请调度员在开工前答复）

1. **迁移顺序的理解**：本单把“空库初次 bootstrap”放在首次备份之前（周四下午 2.3），因为备份执行者写清单所用的函数要先由 bootstrap 建出来。“迁移”指的是之后对已有库执行的 `nexloop-migrate`，放在首次备份验证通过之后（周五 3.2）。NX-035 手册“与 NX-032 的先后关系”一节把整个 NX-032 第 5 步都排在备份之后；如果调度员的本意是要求连 bootstrap 也在备份之后，需要另定首次备份写清单的方式。
2. **前置 P2（带 pgBackRest 的 PG 镜像）**：本次核对时，锁文件中的 postgres 镜像仍是上游镜像（`resolved_index_manifest_only_runtime_unverified`）。如果周三晚上之前做不出这个镜像，周四下午的 2.4–2.5 就无法进行，进而挡住迁移门槛和周五的恢复演练。
3. **OPERATIONS §9.4a**：在本单的基线上还没有这一节；周六就绪检查的 R1、R2 格式要等它合入后才能核对。
