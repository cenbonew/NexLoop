# 部署日执行单：2026-10-15（周四）至 10-16（周五）

把几份运行手册中的部署步骤，按这两天的四个时段排成一张执行单。**2026-10-14 晚的核对结果见 [deploy-days-20261015-review.md](deploy-days-20261015-review.md)**：其中 M8、M9、M12、M13、M14 五项需要在开工前先做决定，下文相应步骤处已标出。之后的排期是：10/17（周六）校准，10/18–19 受控交互，10/20 验收（ROADMAP §12）。


## 实际进展（截至 2026-10-15，按各逐条命令单整理）

本节只记录实际完成了什么、出现了哪些偏差；下文原有步骤的正文不改。命令与证据以各逐条命令单为准。

| 时间 | 执行单步骤 | 结果 | 偏差摘要 | 逐条命令单 |
|---|---|---|---|---|
| 10/14 晚 | 1.1–1.5 | 完成：加密卷、目录、服务用户 | 提前一天完成；评审结论见 [review](deploy-days-20261015-review.md)（M8 TLS 服务器模式，M9 推迟 NOLOGIN，M12 文件备份这两天只做恢复日志，M13/M14 用主机 venv） | — |
| 10/14 晚 | 开工前置 P1 | 完成：签名发布 `20261014-76df1926d6c0-1`（commit 76df192）已封存 | — | [run-34](run-34-20261015.md) 适用状态 |
| 10/15 | 2.2–2.6 | 完成：DATA_HOST 库到 0197；bootstrap 完成；首次备份（D1）成立 | 迁移门槛按两次备份夹着 bootstrap 执行（硬约束第 1 条） | [run-34](run-34-20261015.md) 适用状态 |
| 10/15 上午 | 3.3 恢复演练 | **passed**：d20261015a，RTO 49.4 s，RPO 60.2 s，`drill_compare` 0 差异，`result_id` 1；生产库未受影响 | 6 处偏差：APP_HOST 只有 Python 3.14、演练卷挂载点 0711、`spool-path`、`en_US.UTF-8` locale、`--dbname`、RPO 以 `marked_at` 为基准。另有 glibc 排序规则差异的限制；AT-059 合成删除待补 | [run-33](run-33-20261015.md) §10 |
| 10/15 | 3.4 启动应用 | `nexloop-app` 已启动，14 个服务在运行；合成租户已执行 register、browser、configure | api 与 gateway 的启动问题由 c56a18f 修复；provider 配置原先要手写，已由生成器解决（G-ROUTES）；其余缺口 G3–G7 见该单 §5 | [run-34](run-34-20261015.md) |
| 10/15 10:30 起 | 4.1 首次部署内容 | 进行中：7 个 worker 反复退出（它们的前置条件见该单 §2） | 业务对象类型与 Action 原先没有生产发布入口（A1），已由 `stage_secrets.py publish` 解决；A2–A7 待办 | [run-41](run-41-20261015.md) |
| 10/15 | 组装演练（本地，不是 stage） | 在本地干净 PG 上从 bootstrap 跑到每个 worker 的首轮调用 | 缺口：D-QUEUE（已修）、G-WEBCHAT-AUTH、G-CONTEXT-STRATEGY、G-WORKBENCH-AUTH、G-ROLE、G-ROUTES（已补）；G-PLAN、G-ROLE-READ 未定 | [assembly-dryrun-gaps](assembly-dryrun-gaps.md) |

尚未开始：3.1 restic 的文件部分（按 M12 推迟）、4.2 AT-058 外部探测、4.3 参考端点、4.4 定时任务的全部启用、4.5 回滚核对、4.6 收尾，以及周六就绪检查。各项进展以调度员的记录为准。

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

1. **迁移顺序**（调度员 2026-10-14 答复）：没有验证过的备份，就不做迁移。周四下午按以下顺序执行：
   1. 启动空集群；
   2. 第一次 pgBackRest 全量备份，并做 verify。此时库里还没有清单表，结果只写进私有证据文件，不写库；
   3. 空库初次 bootstrap；
   4. 第二次 pgBackRest 全量备份，由备份执行者写进清单，状态必须是 `verified`。
   
   此后的 `nexloop-migrate` 一律先通过迁移门槛 `authz.nexloop_migration_gate()`，即存在 60 分钟以内、catalog 与当前库一致的已验证备份。门槛没有豁免开关（NX-035，AT-056）。
2. **relay**：APP_HOST 运行 2 个 relay（`message-relay`、`message-relay-2`），连接预算按方案 B（合计 59/60，两个 API 的 `--identity-pool-max` 都是 3，见 [relay2-budget](../implementation/relay2-budget.md)）。部署门禁会核对 relay 容器数与预算一致。
3. **Context v6**：relay 带 `--context-strategy recent_plus_required`，Agent Host 运行配置的 `context_input_protocol` 是 `nexloop.context-pack.v6`。不得去掉这个参数。
4. **参考企业交付端点**：在 APP_HOST 上作为**独立 Compose 项目**运行，有自己的项目名和卷，不加入 NexLoop 的网络，也不连 NexLoop 的数据库。
5. **首次部署只放合成租户**，`REAL_DISPATCH_ENABLED=false`；三层真实派发开关在这两天都保持关闭（NX-032 D9）。

## 开工前置（10/14 晚之前核对；任一项不满足，就先调整排期）

| # | 项 | 执行者 | 判据 |
|---|---|---|---|
| P1 | 已封存的签名发布 `$RID`：按发布用 CI 结果手册产出该 SHA 的 CI 结果，再按首个签名发布手册 §2 第 1–17 步构建、签名、封存 | 调度员；签名一步由负责人提供密钥 | `archive.py check` 输出 `ok: true`；gateway 已按 digest 固定 |
| P2 | DATA_HOST 的 PG 镜像是发布中的 **`nexloop/postgres`**（PostgreSQL 18 + pgvector + pgBackRest 2.59.3，以 uid 999 的 postgres 用户运行），已随首个签名发布 `20261014-241993b9cc12-1` 提供；data dry-run 写出 `POSTGRES_IMAGE=nexloop/postgres@<发布 digest>` | 调度员 | 发布清单中有 `postgres`；data 主机的 dry-run 与 Compose 预检使用它（核对文档预演 P-3、P-6） |
| P3 | APP_HOST 用的 pgBackRest 包（PGDG 2.59.3 的 deb，供 repo1 的 TLS 服务器）和 restic 包，已在管理机上下载，并与官方校验值核对一致（下载要负责人确认）。DATA_HOST **不单独安装** pgBackRest：它随 `nexloop/postgres` 镜像提供，版本相同 | 负责人确认，调度员执行 | 两个包的 sha256 与官方值一致，并回填 `versions.lock.json` 的 `deployment_tools`；镜像内 `pgbackrest version` 为 2.59.3 |
| P4 | 私有材料齐全：私有 stage env（0600）与 SSH 配置；服务与客户端证书（已签发）；备份口令（pgBackRest repo1/2、restic repo1/2）；各 LUKS 口令；S3 桶与专用子账号凭据；参考端点的服务凭据与员工凭据 | 负责人 | 都在密码管理器和私有目录中；不进入仓库，也不交给任何会话 |
| P5 | 两台主机可用空间（只读 inventory）：按 NX-035 手册 1.1 估算各 LUKS 卷，合计不超过可用空间的 70% | 调度员 | 估算记录在私有证据目录 |
| P6 | LUKS 开机解锁方式：按 NX-035 手册 1.3 选定，推荐 A“人工解锁” | 负责人 | 决定已记录 |
| P7 | D-b：部署后，CI 车道与压测车道是否继续与运行环境同机 | 负责人 | ROADMAP 已记录决定 |

**NX-033 §8**：完整切换到合并控制器不在这两天做。发布用的 CI 结果已改由现有车道按 NX-033 队列产出。§8 第 5 步的隔离探测和第 7 步的切换，另找时间进行。

## 周四上午：准备、加密卷、目录与秘密

| # | 步骤 | 执行者 | B 类 | sudo | 耗时 | 完成判据 | 失败时撤销 |
|---|---|---|---|---|---|---|---|
| 1.1 | **参数与渲染**（NX-032 步骤 0）：`render.py --env <私有 stage env> --check`，再用 `--template … --out …` 渲染 `pg_hba.conf`、`postgresql.conf`、`valkey.conf`、`data-host.nft`、`app-host.nft`。`postgresql.conf` 模板（本次修订后）已经包含 NX-035 2.4 的归档三项和 socket 目录；`pg_hba.conf` 模板已经包含 `nexloop_backup`、`nexloop_backup_recorder` 的本机 socket 规则 | 调度员（管理机） | 否 | 否 | 30 分钟 | `valid: true`；渲染结果中没有 `__X__` 残留；`pg_hba.conf` 最后四行是 reject | 删除 `$RENDER_DIR` |
| 1.2 | **部署 dry-run 与 Compose 预检**（首个签名发布手册 §4 第 1–4 步）：app 和 data 两个 dry-run，**都用 `--release-dir <发布目录> --allowed-signers <F> --trusted-sha <发布 commit 的完整 40 位>`**（不能用 `--trusted-ref refs/heads/main`：main 已经越过发布的 commit）；拼出 `$PRIVATE_COMPOSE_APP_ENV` 与 `$PRIVATE_COMPOSE_DATA_ENV` 并交叉核对；两份 `docker compose config -q` | 调度员（管理机）；私有 env 由负责人拼接 | 否 | 否 | 45 分钟 | 两个 dry-run 都是 `ready: true`；`compose_images_from_manifest: true`；`config -q` 退出码 0；app 的 Compose 中有 2 个 relay，且带 `--context-strategy recent_plus_required` | 删除 `$PLAN_DIR_*`；私有 env 保留 |
| 1.3 | **执行前 inventory**（两台主机，只读） | 调度员 | 否（只读） | 否 | 15 分钟 | 两份 before 文件（0600）存入 `$EVIDENCE_DIR` | — |
| 1.4 | **LUKS 加密卷**（NX-035 步骤 1）：DATA_HOST 建 `nexloop-secrets`、`nexloop-file-repo`；APP_HOST 建 `nexloop-secrets`、`nexloop-pg-repo`、`nexloop-drill`。文件型容器，不重新分区 | 负责人本人（DATA_HOST），APP_HOST 调度员逐步授权 | 是 | 是：负责人输入 sudo 与 LUKS 口令（每个卷三处） | 1.5 小时 | 每个卷 `luksDump` 显示 `Version: 2`；`findmnt` 显示挂载自 `/dev/mapper/nexloop-*`；挂载点 700，属主正确 | 卷为空时：`umount` → `cryptsetup close` → 删除镜像文件（先 `ls -A` 确认卷是空的） |
| 1.5 | **目录与服务用户**（NX-032 步骤 1）：`$NEXLOOP_ROOT/secrets` 落在刚建的加密卷上；APP_HOST 建 `config`、`data/artifacts`、`data/runtime`、`data/relay-vault`、`data/relay-vault-2`，以及每个服务一个 `secrets/<service>`；建服务用户。**DATA_HOST（`nexloop/postgres` 以 uid 999 运行）**：`data/postgres`、`run/postgresql`（0700）、`secrets/pgbackrest`（0700）属于 999；`secrets/postgres/server.key` 属于 999、0600；引导口令的 secret 文件对 999 可读 | 负责人本人（DATA_HOST），APP_HOST 调度员逐步授权 | 是 | 是 | 30 分钟 | 秘密目录 700、属主是对应服务；两个 relay vault 各自 0700 | `sudo rm -r` 本步建的空目录（先 `ls -A`）；`sudo userdel` |
| 1.6 | **证书与秘密文件**（NX-032 步骤 2）：PG、Valkey、Gateway 服务证书，各客户端证书，DSN（`verify-full`）；每个文件只放进所属服务的目录。Agent Host 只放模型凭据的位置，此时不放真实模型密钥（见周六就绪检查） | 调度员（逐步授权）；DATA_HOST 上的 `chmod`、`chown` 由负责人输入 sudo | 是 | 是（DATA_HOST） | 45 分钟 | `doctor --tls-spec`：`tls_certificates`、`tls_dsn_verify_full`、`tls_valkey_only` 都为 true；Agent Host 目录里没有 DSN、凭据或签名密钥 | 删除本步放入的文件 |
| — | 缓冲与午间核对 | — | — | — | 30 分钟 | 上午全部判据已记录 | — |

## 周四下午：防火墙、数据层、建库与首次备份

| # | 步骤 | 执行者 | B 类 | sudo | 耗时 | 完成判据 | 失败时撤销 |
|---|---|---|---|---|---|---|---|
| 2.1 | **防火墙**（NX-032 步骤 3）：两台主机加载独立的 `inet nexloop_stage` 表；先 `nft -c` 只做检查，再加载。保持当前 SSH 会话不断开，另开一个会话验证 | 负责人本人（DATA_HOST）；APP_HOST 调度员逐步授权 | 是 | 是 | 30 分钟 | `nft -c` 没有输出；去掉 NexLoop 表之后规则集 hash 不变；新会话仍可 SSH 登录；APP_HOST 的规则中有效应 worker 访问参考端点的那一条出站规则 | `nft delete table inet nexloop_stage`（现有 UFW 和 Docker 规则不动） |
| 2.2 | **启动空集群**（NX-032 步骤 4）：`nexloop-data` 项目，PG 使用 `nexloop/postgres`（前置 P2），`compose.images.env` 由 data dry-run 写出；归档配置已在 1.1 写入（stanza 建立之前 archive-push 会失败，所以启动后尽快做 2.3 的 `stanza-create`）；`secrets/pgbackrest/pgbackrest.conf` 与 `pgpass` 已放好（2.4 时 `pgpass` 只有 `nexloop_bootstrap` 一行）；库里还没有任何 NexLoop 对象 | 调度员（逐步授权） | 是 | 否（docker 组；如果需要 sudo，由负责人输入） | 30 分钟 | PG、Valkey 都 healthy；`--deploy-gate`（data）的 `port_bindings`、`planned_ports_free_before`、`existing_services_unchanged` 都为 true；`sslmode=disable` 连接被拒；`show archive_mode` 为 `on`；`docker exec nexloop-data-postgres-1 id -u` 为 `999`；容器内 `pgbackrest version` 为 `pgBackRest 2.59.3` | `docker compose -p nexloop-data down`（不加 `-v`） |
| 2.3 | **【待决定 M8：repo1 的连接方式】pgBackRest 安装与仓库**（NX-035 2.1–2.3）：APP_HOST 安装固定版本，建 `$PGBACKREST_REPO_USER`；DATA_HOST 到 APP_HOST 的受限身份（推荐 TLS 服务器模式，复用 NX-032 的 CA）；两台主机渲染 `pgbackrest.conf`（0600，`grep -c '__'` 为 0）；`stanza-create` 与 `check` | 调度员（逐步授权）；DATA_HOST 部分由负责人输入 sudo；仓库口令由负责人从密码管理器填写 | 是 | 是 | 1 小时 | `pgbackrest version` 与锁文件一致；受限身份只能执行 pgbackrest；配置中没有占位符；`check` 输出 `completed successfully`，WAL 已能归档 | 删除二进制和用户；删除这把公钥或 TLS 证书；删除配置文件；stanza 的处理见 2.4 |
| 2.4 | **第一次全量备份（空集群，只写私有证据）**：直接调用 pgBackRest（`--repo=1 --type=full backup`，再对 repo 2 执行一次），然后 `verify`，再用 `info --output=json` 取得两份备份的标签。此时还没有清单表，也没有 `nexloop_backup` 角色（两者都由 bootstrap 建立），所以这一次用镜像自带的本机管理身份经容器内 socket 连接（`--pg1-user` 指向它），**不写库**。`info` 与 `verify` 的输出、备份标签、时间存为私有证据文件（0600，`$EVIDENCE_DIR/pgbackrest-first-<日期>.json`） | 调度员（逐步授权） | 是 | 否 | 45 分钟 | `verify` 无错误；`info` 中 repo1、repo2 各有一份 full，状态为 ok；证据文件已写入，内容只有标签、时间、LSN 和状态 | 这份备份只是空集群的基线，失败时重做即可；需要清掉 stanza 时用 `stanza-delete`，属于破坏性操作，须负责人单独确认 |
| 2.5 | **空库初次 bootstrap**（NX-032 步骤 5 的首次建库部分）：开运维窗口（步骤 7）→ 用迁移身份执行 bootstrap，到发布清单对应的 catalog head → **2.5b：在同一窗口内，负责人以 `nexloop_bootstrap` 用 psql 的 `\password` 给 `nexloop_backup` 与 `nexloop_backup_recorder` 设 SCRAM 口令（不进命令行、不进日志），写入 `secrets/pgbackrest/pgpass`（换成 `nexloop_backup` 那一行）与 recorder 的私有 DSN 文件（`host=$NEXLOOP_ROOT/run/postgresql`）**。**方案 A 过渡（D1，负责人 2026-10-14）**：本次发布还不含 0197（`pg_read_all_settings`），`nexloop_backup` 跑不了 pgBackRest 的 `stanza-create` 和 `check`；因此 `pgpass` 暂时保留 `nexloop_bootstrap` 那一行，备份执行者暂用 `nexloop_bootstrap`。`nexloop_backup` 的口令照样设置，等 0197 上线后再把 `pgpass` 换成 `nexloop_backup` 那一行 → 关窗口。**【待决定 M9】`ALTER ROLE nexloop_bootstrap NOLOGIN` 暂不执行**：它是唯一的超级用户和迁移身份，设为 NOLOGIN 后，周五 3.2 无法迁移。D5 要求的可验证备份就是 2.4 那一份 | 负责人本人或逐步授权；DATA_HOST 上 `pg_hba` 和 nft 的改动由负责人输入 sudo | 是 | 是 | 45 分钟 | 2.4 的证据文件存在且 `verify` 无错误（**不满足就不开始**）；`bootstrap.verify()` 的 lineage 一致；`application_roles_unprivileged` 为 true（`bootstrap_identities_disabled` 在 M9 决定之前为 false，属于预期）；窗口已关闭（从管理机连 `$PG_PORT` 被拒或超时）；**没有执行任何 `nexloop-migrate`** | 不运行 down 迁移；需要回到建库前，用 2.4 的备份恢复空集群（NX-035 恢复流程），由负责人单独确认 |
| 2.6 | **第二次全量备份（由备份执行者写进清单）**（NX-035 2.5）：在 DATA_HOST 上执行 `nexloop-backup pg --recorder-dsn-file <recorder DSN> --pgbackrest-command-file <JSON：["docker","exec","-u","postgres","nexloop-data-postgres-1","pgbackrest"]> --stanza nexloop --expected-version 2.59.3 --repo repo1 --kind pg_full`，再对 `--repo repo2` 执行一次，然后 `info`。**【待决定 M14】执行者的安装方式**。**身份（方案 A 过渡）**：pgBackRest 经 `pgpass` 以 `nexloop_bootstrap` 连接，原因见 2.5b；清单仍然由 `nexloop_backup_recorder` 写入。下一个签名发布经 3.2 迁移门槛上线 0197 后，把 `pgpass` 换成 `nexloop_backup`，重做 `check` 和一次 `pg_full`，确认 `verify_status: verified` 后才算切回 | 调度员（逐步授权） | 是 | 否 | 1 小时（视数据量） | 两条命令都输出 `ok: true`、`verify_status: verified`；`doctor --backup` 中 `backup_manifest_present`、`backup_recent`、`full_backup_recent`、`required_repos_recent`、`latest_backup_verified`、`wal_archiving` 都为 true（`file_backup_recent`、`journal_export`、`restore_drill_recent` 要到周五对应步骤之后才会通过）。迁移门槛函数不授权给应用角色，由 3.2 的 `nexloop-migrate` 在窗口内判断。**到这一步，迁移门槛才可能允许迁移** | 失败时重做；清单行只追加，不回退；`stanza-delete` 须负责人单独确认 |
| — | 缓冲与当日收尾：重新跑一次 inventory，核对 `existing_services_unchanged`；确认 LUKS 卷挂载、PG 归档在前进 | 调度员 | 否 | 否 | 30 分钟 | 当日判据已记入证据目录 | — |

## 周五上午：restic、迁移门槛、恢复演练、启动应用

| # | 步骤 | 执行者 | B 类 | sudo | 耗时 | 完成判据 | 失败时撤销 |
|---|---|---|---|---|---|---|---|
| 3.1 | **【待决定 M12：APP_HOST 上文件备份执行者的 recorder 连接】restic**（NX-035 3.1–3.3）：DATA_HOST 用于恢复日志，APP_HOST 用于文件；建 `$FILE_BACKUP_USER`；初始化文件 repo1（DATA_HOST 的加密卷）和 repo2（S3）；首次 `nexloop-backup journal` 与恢复日志的 restic 备份；首次 `nexloop-file-backup --kind artifact` 与 `--kind runtime`，repo1、repo2 各一次 | 调度员（逐步授权）；DATA_HOST 上的安装由负责人输入 sudo；S3 初始化前 `set +x` | 是 | 是 | 1.5 小时 | `restic version` 与锁文件一致；`check` 输出 `no errors were found`；清单中有 `journal` 行和两类 `files_*` 的 `verified` 行（此时 Artifact 与 Run 都还是空的，文件数为 0 也算）；`doctor --backup` 的 `journal_export` 和 `file_backup_recent` 为 true | 仓库中还没有快照时，删除本地仓库目录；S3 前缀由负责人在服务商控制台确认后删除；删除二进制和用户 |
| 3.2 | **迁移门槛核对**：先确认有没有待执行的迁移（发布的 catalog head 与 2.5 建库时一致，正常情况下没有）。如果有：先用备份执行者补一次 `pg_diff`（NX-035 2.6；门槛要求 60 分钟以内的已验证备份，2.6 那一份到周五已经过期），再看 `select authz.nexloop_migration_gate()` 为 `allowed: true`，然后开窗口执行 `nexloop-migrate --database-url-file <迁移 DSN>`（门槛在其中判断）、关窗口，迁移后再补一次 `pg_diff`。之后按 M9 的决定设置 `nexloop_bootstrap` 为 NOLOGIN | 调度员；如需迁移，负责人逐步授权并开关运维窗口 | 迁移是 B 类 | 窗口开关需要（DATA_HOST，负责人输入） | 20 分钟（需要迁移时 1 小时） | 没有待执行的迁移；或者迁移前的 `pg_diff` 为 `verified`、门槛为 `allowed: true`、`nexloop-migrate` 输出 `migrated: true`、迁移后 `bootstrap.verify()` 一致、迁移后的 `pg_diff` 为 `verified` | 门槛拒绝时不迁移（没有豁免）；不运行 down 迁移；需要回退时进入 NX-035 恢复流程 |
| 3.3 | **【待决定 M13：演练的运行环境】恢复演练门槛**（NX-035 步骤 4，第一次）：打恢复点 `drill-mark` → 提交一次合成删除 → 用 `nexloop-restore` 恢复到 `$DRILL_ROOT`（同一 PG 镜像，`--network none`，不挂任何凭据）→ 停在 `verified_readonly` → 记录 RTO 与 RPO → 销毁 | 调度员（逐步授权）；建立和销毁演练目录由负责人输入 sudo | 是 | 是 | 2 小时 | 报告 `passed: true`；RTO ≤ 2 小时，RPO ≤ 5 分钟；合成删除在恢复后已重放；生产库 `restore_state` 仍是 `open`；`doctor --backup` 的 `restore_drill_recent` 与 `restore_state_open` 为 true | 只销毁演练目录（先核对目录标识）；生产库只多了只追加的恢复点和清单行，不回退 |
| 3.4 | **启动应用**（NX-032 步骤 6）：`deploy.py load-check` 核对镜像 → `docker compose -p nexloop-app … up -d` → 门禁通过后 `deploy.py promote` | 调度员（逐步授权） | 是 | 否 | 1 小时 | `--deploy-gate`（app）全部为 true，包括 `guard_workers_exact`、`port_bindings`（内部端口只绑 127.0.0.1）、`real_dispatch_closed`、`agent_host_concurrency`、TLS 各项、`existing_services_unchanged`；relay 容器数为 2，与预算一致；`/health/ready` 只报告已核实的能力；`promote` 建立 `current` | `docker compose -p nexloop-app down`（数据目录保留）；`promote` 失败时两个链接自动恢复原样 |
| — | 缓冲 | — | — | — | 30 分钟 | — | — |

## 周五下午：核对、外部探测、参考端点、定时任务

| # | 步骤 | 执行者 | B 类 | sudo | 耗时 | 完成判据 | 失败时撤销 |
|---|---|---|---|---|---|---|---|
| 4.1 | **首次部署内容与 smoke**（NX-032 D9）：只放合成租户；跑 doctor 全套（deploy-gate、`--tls-spec`、`--backup`、`--relay-source`）和 smoke | 调度员 | 否（只读加合成数据） | 否 | 45 分钟 | doctor 全部通过；总闸关闭；relay 产出 v6 上下文，核对方法：用 metrics 只读角色或 doctor 查询 `runtime.nexloop_context_artifact_bindings` 中最近一条 `pack_text` 的 `schema_version`，应为 `nexloop.context-pack.v6`（只读这一个字段，不读上下文内容） | 合成租户的数据留在库中，不回退 |
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

## 已答复的事项（调度员 2026-10-14）

1. **迁移顺序**：按“硬约束”第 1 条执行，周四下午的 2.2–2.6 已按此重排。
2. **带 pgBackRest 的 PG 镜像**：由 L2 做成派生镜像，纳入首个签名发布（前置 P2）。
3. **OPERATIONS §9.4a**：在 L3 的 `l3/nx037-trial-guards` 中，已包含在正在跑 CI 的集成轮 int/s5g 里。合入后再核对周六就绪检查 R1、R2 的格式。
