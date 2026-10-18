# 运维手册（简明版）

面向部署与运维 NexLoop 的人。本文只做导航：说明每类运维工作当前做到哪一步、入口命令是什么、详细步骤在哪份文档里，不重复实现细节。

> **现状（以 [`planning/tasks.json`](../planning/tasks.json)、[ROADMAP](../planning/ROADMAP.md) 与 [CHANGELOG](../CHANGELOG.md) 为准）**：NexLoop **尚未部署**。双机部署（NX-032）有参数化模板、全部 18 个进程的 Compose 编排（Agent Host 独立网络命名空间）、证书脚本、部署门槛检查、dry-run 与隔离探测，但没有连接任何服务器；本地 CI 队列（NX-033）、发布工具（NX-034）和备份恢复（NX-035）只完成了本地部分。真实外发渠道未开通，三层真实派发开关默认全部关闭；`nexloop-api --mode stage` 的 `/health/ready` 做真实判断，但尚未在部署环境运行。
>
> 主机一律用角色名：APP_HOST（应用主机）、DATA_HOST（数据主机）、CI 主机。真实主机名、地址、用户、端口和路径只存在于负责人管理机上的私有 env 文件或私有目录中，不进入仓库。

## 1. 原则

- 会改动服务器、防火墙、数据库、凭据或 remote 的操作都是 **B 类操作**，每一步都由负责人本人执行或逐步授权（[GOVERNANCE.md](../GOVERNANCE.md) §4）。
- 只读盘点和 doctor 检查先于任何改动；每一步执行前后的 inventory 与 doctor 输出保存在管理机的私有证据目录，不进入仓库。
- 结果不明的外部效应先查询对账，不重发；没有已校验的备份不迁移（没有豁免）。
- 运维手册的目标设计见交接规格 [17_OPERATIONS_RUNBOOK](handoff/docs/17_OPERATIONS_RUNBOOK.md) 与 [11_DEPLOYMENT](handoff/docs/11_DEPLOYMENT.md)；本文只列出已经实现的入口。

## 2. 部署（NX-032）

| 内容 | 入口 | 详细说明 |
|---|---|---|
| 双机部署步骤（B 类，逐步确认） | 运行手册：准备 → 目录与服务用户 → 证书与秘密 → 防火墙 → 启动 DATA_HOST infra → bootstrap 与迁移 → 启动 APP_HOST 应用 → 按次开放 PG 运维窗口 → 回滚 | [NX-032 部署运行手册](implementation/NX-032-deploy-runbook.md) |
| 参数校验与配置渲染 | `scripts/deploy/render.py --env <私有 env> --check`；`--template deploy/stage/config/<f>.template --out <目录>/<f>`（白名单参数，不允许残留占位符） | [NX-032 实现说明](implementation/NX-032-local-implementation.md) |
| 证书 | `scripts/deploy/issue_certs.py root|intermediate|server|client`（离线根 CA、年度中间 CA、≤90 天服务证书、按角色的客户端证书） | 同上 |
| 只读盘点 | `scripts/deploy/inventory.py`（部署前后对比，只允许出现 `nexloop` 前缀的新增项） | 同上 |
| 部署模板 | `deploy/stage/`：两机 Compose 示例、配置与防火墙模板、连接预算、Runtime Worker 与 Agent Host 示例 | [deploy/stage/README.md](../deploy/stage/README.md) |
| 部署预演 | `scripts/deploy/deploy.py dry-run`：只判断不执行，核对已签名发布清单与受信提交、由清单生成镜像变量、拒绝盲目回滚，输出渲染结果与逐步计划 | 同上（切片 3） |
| 隔离探测（AT-058） | `scripts/deploy/probe_isolation.py`；CI 主机上的 rootless Docker 双机模拟 `scripts/deploy/netns_sim/` | 同上（切片 4） |
| 首次部署范围 | 只放合成租户，真实外发保持关闭；真实租户与渠道属于 NX-037 | 运行手册“首次部署内容” |

## 3. 启动前与日常检查：`nexloop-doctor`

doctor 只读、失败即非零退出、输出不含地址、路径或秘密。常用开关：

| 开关 | 检查内容 | 来源 |
|---|---|---|
| `--deploy-gate <私有 spec>` | 某一台主机的全部启动前检查：guard 进程数、已登记进程、端口绑定、应用角色无特权、管理身份禁用、秘密文件分发、总闸关闭、既有服务未受影响、TLS 各项 | NX-032 |
| `--tls-spec <私有 spec>` | CA、证书、DSN 与 Valkey 的 TLS 配置 | NX-032 |
| `--connection-budget <文件>`、`--budget-only` | 连接池总和 ≤ 60、`max_connections` ≥ 80、每个常驻服务已声明、`processes × dispatch_loops ≤ 4` | ADR-024、NX-031 |
| `--agent-host-config <文件>`、`--agent-host-only` | Agent Host 全局并发 `maximum_active_runs ≤ 4` | ADR-022 §4 |
| `--observability` | 告警评估器在运行、规则版本、进程样本（只读 `nexloop_metrics` 角色） | NX-030 |
| `--backup` | 备份清单与计划（`deploy/configuration/backup.v1.json`） | NX-035 |
| `--relay-source` | relay 的固定 Source 主体与凭据 | relay 多客户 |

保留与删除相关的告警（卡住的删除、Run 文件或 Artifact 删除失败、逾期未清理的数据类）经保留端口 `status` 报告，见 [NX-029 实现说明](implementation/NX-029-implementation.md)。

## 4. 监控与告警（NX-030）

- 指标快照 `runtime.nexloop_metrics_snapshot` 只含代码、计数、年龄和比率，不含原文；评估器 `nexloop-alert-evaluator` 只评估 `real` world。
- 告警规则是版本化文件，新版本是新文件：[`alert-rules.v1.json`](../deploy/configuration/alert-rules.v1.json)（21 条）、[`v2`](../deploy/configuration/alert-rules.v2.json)（加 relay 两条）、[`v3`](../deploy/configuration/alert-rules.v3.json)（加备份与恢复 7 条，共 30 条）。规则只能经 configurator 写入：`python -m nexloop_eios.observability rules --check|--apply`。
- 负责人可在工作台静默告警，最长 7 天；工作台有告警、运行状态和审计页（审计只给负责人）。
- 本机指标导出接口默认关闭，只绑回环；导出只用 `nexloop_metrics` 角色。
- 详见 [NX-030 实现说明](implementation/NX-030-implementation.md) 与 [架构说明 §3.6](ARCHITECTURE.md#36-可观测性nx-030)。

## 5. 备份、恢复与迁移（NX-035，本地部分）

| 内容 | 入口 | 现状 |
|---|---|---|
| PG 备份与恢复日志导出 | `nexloop-backup pg|journal`（非超级用户 `nexloop_backup`，凭据只经 0600 文件传入） | 本地用 `pg_basebackup` + `pg_verifybackup`；部署时换成 pgBackRest 与 restic（依赖 NX-032） |
| 迁移 | `nexloop-migrate --database-url-file <私有文件>`：先过迁移门槛（无已校验备份、备份过旧或来自别的集群、WAL 归档异常、恢复日志导出滞后、处于恢复围栏中都拒绝），**没有任何豁免** | 已实现 |
| 恢复 | `restored → replayed → verified_readonly → open`：恢复后派发全部关闭（NXR01），先重放删除、联系限制与吊销，再对账外部效应窗口并暂停无法核实的渠道，最后由负责人逐个 scope 执行 `open_scope` | 命令行 `nexloop-restore`（basebackup 与 pgBackRest 两种来源）已实现，见 [恢复命令行](implementation/NX-035-restore-cli.md)；本机一次性环境的逐条命令见其 §7。开放（`open_scope`）只经工作台的受治理 Action，由负责人执行 |

详细流程与限制见 [NX-035 本地部分实现说明](implementation/NX-035-local-implementation.md)、[NX-035 设计](implementation/NX-035-design.md) 与 [架构说明 §3.11](ARCHITECTURE.md#311-备份与恢复nx-035-本地部分)。

## 6. 发布与回滚（NX-034、NX-036）

- 本地发布工具 `scripts/release/release.py`：`manifest`（发布清单）→ `sbom`（锁定版本的 syft/grype）→ `sign` → `verify`（部署的 SHA 与镜像 digest 必须等于测过的清单）；`rollback-check` 拒绝跨越未声明 expand-only 的迁移或降级运行时格式的盲目回滚，提示向前修复。只在本机运行，不推送、不打 tag、不连接服务器。见 [NX-034 本地部分实现说明](implementation/NX-034-local-implementation.md)。
- 构建、归档与同步（NX-034 第二部分）：`make build-release SHA=`（build → sbom → stage 预检 → sign → 封存）、`release.py load-check`、`scripts/release/archive.py seal|check|prune|record-deployment`、`make sync-github`（只快进 main 与负责人签名的 tag）。见 [NX-034 第二部分实现说明](implementation/NX-034-part2-implementation.md)。
- 首个签名发布与部署 dry-run 的逐条命令（CI 主机，方案 A）：[NX-034 首个签名发布运行手册](implementation/NX-034-first-release-runbook.md)。开发机（arm64）上 `release.py build` 会被平台检查拒绝，构建只能在 CI 主机进行。
- 公开发布：`release.py public-preflight` 九步门槛加负责人确认，见 [公开发布检查清单](release/public-release-checklist.md)。
- 部署侧回滚（重新渲染上一份已知可用的发布清单、防火墙与证书回退、整体撤下但保留数据）见 [NX-032 部署运行手册](implementation/NX-032-deploy-runbook.md) §8。

## 7. 本地 CI（NX-033，本地部分）

- 权威入口 `bash scripts/ci/check`（已拆成 `scripts/ci/stages/` 下可单独调用的阶段；前置阶段失败即停，测试阶段一律跑完，每阶段输出一行 `NEXLOOP-STAGE` 结果）；控制器 `scripts/ci/nexloop_ci.py`（ref 规则、按 SHA 入队、全新检出、受信判定、只快进合并、GitHub 同步）与 `Makefile` 命令契约。
- 主机侧（服务用户、bare 仓库与 hook、防火墙、remote 切换）属于 B 类，尚未执行。见 [deploy/stage/ci/README.md](../deploy/stage/ci/README.md) 与 [NX-033 本地部分实现说明](implementation/NX-033-local-implementation.md)。

## 8. 常见运维操作的入口

| 操作 | 入口 |
|---|---|
| 真实派发开关 | 只有负责人，工作台写入口 `set_real_dispatch`；部署总闸 `REAL_DISPATCH_ENABLED`、租户、（租户，Action）三层都打开才派发外部效应（[实现说明](implementation/real-dispatch-switch-implementation.md)）。发布 16 起，工作台“设置与治理”页有开关按钮：负责人一键开启或关闭租户层与交付 Action 层，必须填写原因，走同一个受治理 Action；部署层只读显示。stage 上部署层平时保持打开（`REAL_DISPATCH_ENABLED=true`），日常收放只在工作台做；要整体停止外部效应时才关部署层，并重建读它的服务 |
| 就绪检查 | `nexloop-api --mode stage` 的 `/health/ready`：只返回状态与原因代码（[实现说明](implementation/api-stage-ready-implementation.md)） |
| 暂停 / 恢复（租户、角色、客户、策略、Action 类型） | 工作台写入口（负责人），受治理 Action `Control.set`；恢复不回放旧任务，只登记复评 |
| 结果不明的效应 | 工作台“查询执行结果”（只把对账提前，不重发） |
| 确认结果不明的效应未发生 | 只有负责人，按顺序：① 先确认 provider 的记录保留期覆盖这次投递的时间——覆盖不到时 not_found 只说明记录不在了，不能判定，改为人工联系顾客；② 用重查订单让 provider 回答一次（[重查手册](release/run-stale-unknown-requery.md)；0214 起 provider 回答 not_found 会记为观测）；③ 首次 unknown 满 24 小时、not_found 观测满 10 分钟后，用配置文件加一条命令 `scripts/deploy/confirm_not_delivered.py`，先 `--dry-run` 预查，全部 `eligible` 后再判定（工作台写入口 `confirm_effect_not_delivered`，0210；配置中 `provider_retention_checked` 必须为 true；[运行手册](release/run-confirm-not-delivered.md)）。判定不发送任何东西，也不可撤回 |
| 解除联系限制 | 只有负责人（ADR-023） |
| 撤销删除后联系键的拦截 | 只有负责人，写明理由，工作台写入口 `revoke_contact_key`（NX-053） |
| 人工接管 / 交还 | 工作台（负责人、运营），默认 2 小时、上限 24 小时 |
| 删除客户或消息、保留例外、数据导出 | 只有负责人，经工作台受治理 Action（NX-029） |
| 知识工作台 | 审核员审核候选与证据；发布 17 起负责人可只读查看（`nexloop.knowledge.read`，0213，角色清单 v13），审核决定仍只归审核员。无权限时页面说明需要的角色 |
| 服务授权清单 | `python -m nexloop_eios.service_grants --check|--doctor|--apply`（configurator 连接；`--doctor` 只读报告漂移）。服务凭据的 worlds 只要含 `real` 就只能是 `real`，不能同时含 real 与其他 world（AT-046，写入时拒绝） |
| 服务进程的 `--world` | runtime worker 与 guard 只接受 `real` 或 `test`；effect worker、出站记录、优惠执行者只接受 `real`；其他值在读取凭据之前退出（码 2） |
| Runtime Worker 的进程形态与回退 | [deploy/stage/README.md](../deploy/stage/README.md)（`--guard-workers 4`、`--dispatch-loops 4`，回退改回 1 并重启） |
| 连接器开发 | [CONNECTORS.md](CONNECTORS.md) |
| 评估运行 | [EVALUATION.md](EVALUATION.md) |
| 故障处理的目标流程 | [17_OPERATIONS_RUNBOOK](handoff/docs/17_OPERATIONS_RUNBOOK.md) §7 |
| 安全事件 | [SECURITY.md](../SECURITY.md) 的处置流程：暂停 scope 与派发 → 保留证据 → 撤销凭据 → 核对已发生动作 → 修复验证 → 受控恢复 |

## 8a. stage 运维要点（两台主机，NX-032/035，10/15 部署后）

只用角色名与参数：`$NEXLOOP_ROOT`、`<卷名>`、`<挂载点>` 的取值都在负责人的私有 env 中。凡改动主机的操作都属于 B 类：DATA_HOST 上的 sudo 由负责人本人输入，APP_HOST 上的操作逐步授权。

### 8a.1 主机重启后：LUKS 人工解锁与挂载顺序

LUKS 卷采用人工解锁（NX-035 手册 1.3 方式 A），主机重启后卷**不会自动挂载**。按下面的顺序恢复：

0. **先确认防火墙表已加载**（G-NFT-PERSIST，run-42 §6a）：
   - `nexloop-nft.service` 在 `docker.service` 之前从根文件系统加载 `inet nexloop_stage`，不依赖 LUKS 卷；
   - 它是 `docker.service` 的 RequiredBy，加载失败时 Docker 与全部容器都不会启动（失败即停）。
   ```
   systemctl is-active nexloop-nft docker
   sudo nft list table inet nexloop_stage | grep -c -E 'accept|drop'
   ```
   - 第一行应为两个 `active`，第二行的条数与 run-42 §6a 第 4 步记录的相同。
   - 如果 `nexloop-nft` 失败：
     1. 看 `journalctl -u nexloop-nft`，常见原因是文件缺失或渲染出错；
     2. 修好 `$NEXLOOP_ROOT/config/firewall/<host>.nft` 后，先 `sudo nft -c -f` 检查，再 `sudo systemctl start nexloop-nft docker`；
     3. **不要**为了让 Docker 起来而禁用这个依赖。
1. **先停住依赖这些卷的容器**。Compose 项目带 `restart: unless-stopped`，开机后 Docker 会在卷挂载之前就把它们拉起来，这时它们读到的是挂载点下面空的底层目录（见 8a.5）。所以先执行：
   ```
   docker compose -p <项目> stop
   ```
   DATA_HOST 是 `nexloop-data`；APP_HOST 是 `nexloop-app`，以及参考端点的独立项目。
2. **逐卷解锁并挂载**（负责人输入 LUKS 口令）：
   ```
   sudo cryptsetup open <镜像文件> <卷名>
   sudo mount /dev/mapper/<卷名> <挂载点>
   findmnt <挂载点>
   ```
   - DATA_HOST：`nexloop-secrets`（`$NEXLOOP_ROOT/secrets`），以及文件仓库与恢复日志卷；
   - APP_HOST：`nexloop-secrets`、pgBackRest repo1 卷、演练卷。
   - 秘密卷最先挂。
3. **确认 systemd unit 会自己恢复**：备份定时任务和 pgBackRest TLS 服务都带 `RequiresMountsFor=`，卷挂载之前它们不会启动，卷挂好后也不会自动补跑，所以要手动启动：
   - DATA_HOST：`nexloop-*.timer`；
   - APP_HOST：`nexloop-pgbackrest-server`。
   ```
   sudo systemctl start <unit>
   systemctl list-timers 'nexloop-*'
   ```
4. **按顺序重新启动容器**：
   1. DATA_HOST 的 `nexloop-data`，等 PG healthy；
   2. APP_HOST 的 pgBackRest TLS 服务；
   3. APP_HOST 的 `nexloop-app`；
   4. 参考端点。
   ```
   docker compose -p <项目> up -d
   ```
   用 `up -d` 而不是 `start`，让容器按当前的挂载重新建立绑定。
5. **核对**：
   - `nexloop-doctor --deploy-gate` 中两台主机的检查都为 true；
   - `nexloop-doctor --backup` 见 8a.2；
   - PG 的 WAL 归档恢复前进：`wal_archiving` 为 true。

### 8a.2 备份定时任务与 `doctor --backup` 的日常核对

- **定时任务**：unit 示例与计划见 [`deploy/stage/systemd/README.md`](../deploy/stage/systemd/README.md)：
  - DATA_HOST：`nexloop-pg-full`、`nexloop-pg-diff`、`nexloop-journal`、`nexloop-journal-restic@repo1` 与 `@repo2`、`nexloop-restic-maint`；
  - APP_HOST：`nexloop-file-backup`，10/15–16 未启用（M12）。
- **每天**看一次：
  ```
  systemctl list-timers 'nexloop-*'
  systemctl --failed 'nexloop-*'
  nexloop-doctor --backup --metrics-database-url-file <METRICS_DSN_FILE> --backup-plan deploy/configuration/backup.v1.json
  ```
  检查名以代码为准：
  - `backup_manifest_present`、`backup_recent`、`full_backup_recent`、`required_repos_recent`、`latest_backup_verified`、`wal_archiving`；
  - `journal_export`、`file_backup_recent`、`restore_drill_recent`；
  - `restore_state_open`、`deletion_replay_closed`。
  
  `file_backup_recent` 在文件备份启用之前不会通过，是已知项。
- **失败时**：先看 `journalctl -u <unit>`。执行者只打印计数、标签和错误类型，不打印凭据。不要手工改清单表（它只追加，也没有手工入口）。

### 8a.3 pgBackRest TLS 服务（repo1，APP_HOST）

- **服务与配置**：
  - unit：`nexloop-pgbackrest-server`，以 `nexloop-pgbackrest` 身份运行，它也是 repo1 的属主；
  - 配置：`/etc/pgbackrest/pgbackrest-server.conf`，0600；
  - `tls-server-auth` 只放行 DATA_HOST 客户端证书的 CN；
  - 防火墙：`app-host.nft` 的 input 链只接受 DATA_HOST 访问 `PGBACKREST_TLS_PORT`。
- **核对**：
  - `systemctl status nexloop-pgbackrest-server`；
  - DATA_HOST 上 `docker exec -u postgres <PG 容器> pgbackrest --stanza=nexloop check` 成功。
- **证书轮换**（服务证书 90 天）：替换 `secrets/pgbackrest/` 下的证书和私钥，然后 `systemctl restart nexloop-pgbackrest-server`。客户端证书的 CN 不能变，变了 `tls-server-auth` 就不再放行。
- **注意**：服务端不持有仓库口令，加密由 DATA_HOST 的客户端完成。在 APP_HOST 上直接读 repo1 时（例如恢复演练），口令只放进演练用的私有配置，用完 `shred`（见 [run-33](release/run-33-20261015.md)）。

### 8a.4 配置者登录窗口与“配置者平时 NOLOGIN”

stage 上 `nexloop_configurator` 平时是 **NOLOGIN、没有口令**。需要配置者身份的工具，经 `scripts/deploy/configurator_window.py` 的登录窗口运行：

1. 用一次性口令（只以 SCRAM verifier 形式写入 PG）让配置者暂时可以登录；
2. DSN 只存在于一个私有临时文件中；
3. 无论成败，窗口关闭时都恢复为 NOLOGIN、无口令。

| 工具 | 用途 |
|---|---|
| `stage_secrets.py register` | 建租户、服务主体、凭据与授权（0050） |
| `stage_secrets.py browser` | 浏览器应用登记 |
| `stage_secrets.py signing-key` | 再登记一把 Authority 签名密钥 |
| `stage_secrets.py publish` | 发布业务对象类型与 Action 目录（A1，见 [run-41](release/run-41-20261015.md) §3a） |
| `workbench_roles.py … --apply` | 工作台成员与角色 |
| `webchat_consumers.py … --apply` | WebChat 消费者账号 |
| `scripts/nx037/real_tenant_init.py configure` | 效应类别与告警规则 |

这些工具都通过 `--bootstrap-dsn-file`，经 DATA_HOST 本机 socket 开窗（pg_hba 中 `local all nexloop_configurator scram-sha-256` 那一行）。

**核对**（每次用完、每天一次）：以 bootstrap 身份执行下面的查询，应返回 `f` 和 `t`：

```
select rolcanlogin, rolpassword is null from pg_authid where rolname='nexloop_configurator'
```

返回其他值，说明有窗口没关好：立即执行 `alter role nexloop_configurator nologin password null`，并查这次窗口是谁开的。

### 8a.5 Docker 绑定挂载：源文件缺失时会自建同名空目录（10/15 踩坑）

Compose 的绑定挂载（`volumes:` 中 `<主机路径>:<容器路径>`）在主机路径不存在时，Docker 会以 root 身份**创建一个同名的空目录**再挂进去，不报错。本该是文件的挂载就变成了目录，容器读到的是空目录，表现为配置读取失败或权限错误。

- **常见原因**：
  - 源文件还没生成，或者文件名拼错；
  - LUKS 卷未挂载，路径落在了底层空目录上（见 8a.1）。
- **处理**：
  1. `docker compose -p <项目> stop <服务>`；
  2. 用 `ls -la <主机路径>` 确认它是 Docker 自建的空目录：属主 root，目录为空；
  3. `sudo rmdir <主机路径>`，只删空目录；
  4. 放入正确的文件，属主和权限按服务要求；
  5. `docker compose -p <项目> up -d <服务>`。
- **预防**：启动前先 `docker compose … config -q`，并逐一核对每个绑定源存在且类型正确（文件或目录）。`secrets:` 中 `file:` 指向的源文件缺失时，Compose 会直接拒绝启动，比绑定挂载更安全。

### 8a.6 `sed -i` 会换 inode：容器看不到新内容

`sed -i` 并不改原文件，而是写一个新文件再改名替换，文件的 inode 因此变了。以**单个文件**绑定挂载进容器的配置（例如 `postgresql.conf`、`pg_hba.conf`），容器里挂的仍是旧 inode，看不到修改；这时 HUP 或 reload 读到的也还是旧内容。

- **两种做法**：
  - **原地写入**，inode 不变：`sudo sh -c 'cat new > <文件>'`，或 `sudo tee <文件> < new > /dev/null`；
  - **或者**改完后重建容器：`docker compose -p <项目> up -d --force-recreate <服务>`。对 PG 这是一次停机重启，要放在维护窗口内做。
- **核对**：比较主机与容器里文件的摘要：`sha256sum <文件>` 对比 `docker exec <容器> sha256sum <容器内路径>`，两者一致后，再执行 HUP 或 reload。

### 8a.7 `nexloop-nft`：防火墙表的 reload 与重启顺序（G-NFT-PERSIST）

两台主机都由 `nexloop-nft.service` 在**开机时、Docker 之前**加载 `inet nexloop_stage`。

- 加载文件放在根文件系统的 `$NEXLOOP_ROOT/config/firewall/`，不在 LUKS 卷上；
- 它是一个事务：`table`、`delete table`、`include`；
- unit 以 `RequiredBy=docker.service` 挂在 Docker 上：加载失败时 Docker 不启动，容器也就不会在没有隔离规则的情况下运行。

安装步骤见 [run-42](release/run-42-20261015.md) §6a。

- **改规则**：
  1. 用私有 `stage.env` 与发布检出中的模板重新渲染，生成新的事务文件（旧文件先 `shred -u`）；
  2. 执行 `sudo systemctl reload nexloop-nft`，原子地只替换这一张表；
  3. 核对：`sudo nft list table inet nexloop_stage` 中出现了新的规则。
- **不要 `restart`**：`restart` 会先删表再加载，中间有一段没有隔离规则的窗口。也**不要**启用 `nftables.service`：它的默认配置以 `flush ruleset` 开头，会清掉 UFW 与 Docker 的规则。
- **主机重启时的顺序**（与 8a.1 合在一起看）：
  1. `nexloop-nft` 自动加载，早于 Docker；
  2. Docker 启动，`unless-stopped` 的容器被拉起，但这时 LUKS 卷还没解锁，容器读到的是空的底层目录；
  3. 按 8a.1 先 `stop` 各项目，解锁并挂载卷，启动依赖卷的 unit，再按顺序 `up -d`。
- **核对**：`systemctl is-active nexloop-nft` 为 `active`；部署闸门的 `compose_networks_match_firewall` 与 `running_networks_match_firewall` 为 true。
- **停用**（只在撤回时使用）：执行 `sudo systemctl disable nexloop-nft`，**不带** `--now`，这样内存里的表会保留；然后删除 unit 文件，再 `daemon-reload`。

### 8a.8 Claim worker 的出口网络与故障判读（G-CLAIM-EGRESS）

claim-extraction-worker 与 claim-matcher 在自己的网络 `claim_egress` 上（子网 `CLAIM_EGRESS_CIDR`，compose 固定）。nft 对这个网段只放行三类目标：

1. 模型 allowlist 的 443；
2. DATA_HOST 的 PG；
3. 其余一律 `ct state new drop`。

它们不在应用命名空间里：应用网络到模型地址的规则是 drop（NX-037 D4）。

| 现象 | 先查什么 | 判读 |
|---|---|---|
| Claim 提取或匹配一直不前进，日志里是连接超时或 provider 不可用 | `sudo docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}' nexloop-app_claim_egress` 是否等于 `CLAIM_EGRESS_CIDR`；`sudo nft list table inet nexloop_stage \| grep -c "ip saddr <CLAIM_EGRESS_CIDR>"` 是否为 3 | 子网不符：compose 与 env 不一致，按 run-42 §8 重建这两个服务。规则不足 3 条：重新渲染后 `reload nexloop-nft` |
| 子网和规则都对，仍然连不上模型 | 模型域名当前解析到的地址，是否都在私有 env 的 `MODEL_PROVIDER_ALLOWLIST` 里 | 供应商地址变了：由负责人重新解析、更新 allowlist、重新渲染并 reload（B 类）。不要临时放开整段网段 |
| 能连模型，但模型返回鉴权错误 | 两个 worker 各自的 `model.env` 与模型凭据文件（只看是否存在、权限和属主，不看内容） | 凭据问题，按 deploy-days 执行单的“模型凭据写入”重新写入 |
| 连不上 PG | 部署闸门；DATA_HOST 防火墙对 APP_HOST 的放行 | 与其他 worker 的 PG 故障相同 |
| 想确认隔离是否完好 | 按 run-42 §3d 用一次性容器探测：模型与 PG 应通，Valkey、SSH、未列出的外网、API 都应不通 | 不通的变通了：最高优先级报负责人 |

- 预算耗尽时（`budget_exhausted`），worker 的日志是拒绝而不是连接错误，不要误判为网络问题。
- 这两个 worker 首次允许在凭据到位之前失败（run-34 G5）；凭据到位之后仍然失败，才按上表排查。

### 8a.9 操作员重置口令：`identity_password_reset` 的使用规则（G-PW-RESET）

适用于口令无人知道、邮件重置又无法送达的本地浏览器账户，例如 stage 上地址为 `.invalid` 的模拟客户与测试账户。命令与前提见 [run-assembly](release/run-assembly-20261015.md) §1a。

- **身份**：以 identity 操作员（`nexloop_identity`，API 的 identity DSN）调用受治理函数 `control.nexloop_operator_reset_browser_password`（迁移 0198），不直接写任何表。
- **效果**：
  - 账户修订号加 1；
  - `session_epoch` 加 1，所以旧会话全部失效；
  - 失败计数与锁定清零；
  - 写一条证据行，内容为操作员标签和 request id，不含口令。
- **规则**：
  1. **测试账户的口令由调度员用工具生成**：使用 `--generate-password-file <新文件>`，由工具在私有目录中生成 20 位随机口令，文件为 0600，已存在就拒绝。**只把文件路径告诉使用者**，口令本身不进聊天、仓库、证据、工单或日志。
  2. 使用者从文件中读出口令后存入自己的密码管理器，然后 `shred -u` 这个文件。
  3. 每次重置用一个新的、稳定的 `--request-id`，例如 `pw-reset-<日期>-<账户>`。相同的 request id 重跑只会回放结果（`replayed: true`），不会改两次。
  4. 必须带 `--confirm-reset`，并给出 `--operator-label`，记录是谁执行的。
  5. 只用于测试账户和确有需要的账户。真实客户的账户首选自助重置；不得不用本工具时，须经负责人批准，并告知客户。
  6. 新口令至少 12 位，不能与当前口令或历史口令相同。
- **核对**：
  - 输出只有 ID、修订号与 epoch，形如 `{"reset": true, "replayed": false, …}`，不含口令或 hash；
  - 旧口令登录失败，新口令登录成功。
- **撤销**：无法恢复原口令。需要时用新的 request id 再重置一次。


### 8a.10 共用网络命名空间的服务：一起重建（10/16 踩坑）

compose 里用 `network_mode: service:<X>` 的服务，共用 X 容器的网络命名空间。X 被重建后会换成新容器，引用它的服务仍挂在已删除容器的命名空间上，网络就断了。10/16 agent-host 重建后，`agent-link-relay` 因此断开了到 guard 的链路。

**规则**：重建（`up -d --force-recreate`，或让配置变化生效的 `up -d`）或重启（`restart`；X 重启后网络命名空间也会重新建立）任何被引用的服务 X 时，必须在同一条命令里同时处理所有引用它的服务。

目前的依赖关系（`compose.app.example.yaml`，由 `tests/test_compose_network_mode_rebuild.py` 守护）：
- `app-net` 被 18 个服务引用：
  - app-link-relay、gateway、api-1、api-2；
  - domain-worker、runtime-worker、message-relay、message-relay-2、outbound-recorder；
  - claim-extraction-scheduler、recall-indexer、plan-reevaluator、reply-guarantor；
  - commitment-keeper、commercial-recorder、retention-keeper、alert-evaluator、incentive-issuer。

  重建 app-net 实际上等于重启整个项目，在维护窗口内进行。
- `agent-host` 被 `agent-link-relay` 引用。
- `action-worker` 被 `webchat-delivery` 引用。

**查找依赖**（只读，APP_HOST 上执行）：

```
docker compose -p nexloop-app -f $NEXLOOP_ROOT/compose.app.yaml --env-file $NEXLOOP_ROOT/config/compose.app.env \
  --env-file $NEXLOOP_ROOT/config/compose.images.env config --format json \
  | python3 -c 'import json,sys; s=json.load(sys.stdin)["services"]; [print(v["network_mode"][8:], "<-", k) for k,v in sorted(s.items()) if str(v.get("network_mode","")).startswith("service:")]'
```

**核对**：重建后，对每个引用方执行 `docker inspect -f '{{.HostConfig.NetworkMode}}' <容器>`。它应指向 X 的**当前**容器 ID（与 `docker compose ps -q X` 一致）。

### 8a.11 例行发布部署的固定步骤（发布 16–18 起）

每次在 stage 部署一个新发布，部署单都包含以下步骤；某一步本次没有变化时，照样执行并记录“无变化”。

1. **备份核验与迁移门槛**：先核验最近一次 pgBackRest 备份，迁移前做一次差异备份，迁移只经 `nexloop-migrate` 的门槛（[NX-035 部署运行手册](implementation/NX-035-deploy-runbook.md) §2.6）。迁移号以 catalog 为准，不看文档里的旧编号。
2. **Action 发布预演**（发布 17 起固定加入）：

   ```bash
   python scripts/deploy/stage_secrets.py publish --out <管理机秘密树> --tenant <TENANT_ID> --bootstrap-dsn-file <BOOTSTRAP_DSN_FILE> --signing-key-id <SIGNING_KEY_ID> --dry-run
   ```

   带连接文件的 `--dry-run` 只把编译出的业务对象类型与 Action 目录和库中已发布的状态比较，什么都不写；不带连接文件时只输出编译计数（`compared: false`），不能代替预演。比较结果里有变化时，再去掉 `--dry-run` 正式发布；发布前后各记录一次计数。
3. **角色清单升版本时应用工作台角色**：`deploy/authorization/workbench-roles.v1.json` 的 `manifest_version` 比 stage 上已应用的版本高时（例如发布 17 的 v13），用 `scripts/deploy/workbench_roles.py --roles deploy/authorization/workbench-roles.v1.json --members <私有成员文件> --apply --bootstrap-dsn-file <BOOTSTRAP_DSN_FILE> --signing-key-id <SIGNING_KEY_ID> --signing-key-file <SIGNING_KEY_FILE>` 应用。它在 configurator 登录窗口内先写成员，再写成员授权，窗口随后关闭。只发布 Action、不应用角色时，新 Action 没有任何人持有授权。
4. **镜像与重建**：镜像只按已签名清单中的 digest 载入；重建时遵守 [§8a.10](#8a10-共用网络命名空间的服务一起重建1016-踩坑)。
5. **租户配置随版本下发**（在新镜像运行之后）：告警规则或试运行配置升版本时（发布 18：告警规则 v5、试运行配置 v4），用 `real_tenant_init.py configure` 下发（[§9.2](#92-真实租户初始化scriptsnx037real_tenant_initpy)）。发布 18 起，`configure` 还包含候选合并配置这一步：先只读核对，缺失时才应用（幂等），并报告 `in_sync`、`applied` 或 `blocked`。
   - 合并配置要求租户当前生效的召回 profile 就是校准过的 embedding profile，所以要等新的召回索引服务运行过一次（§8a.12）。在此之前执行，会得到 `blocked`（`embedding_profile_mismatch`），什么都不写；等服务运行后重跑即可。
   - 也可以单独核对：`python -m nexloop_eios.merge_configuration --manifest deploy/ontology/merge-config.v1.json --tenant <TENANT_ID> --doctor --database-url-file <CONFIGURATOR_DSN_FILE>`。
6. **部署后核对**：`nexloop-doctor` 全部通过（含 §8a.12 的检查项）；工作台“告警”页没有新的 firing。

### 8a.12 后台服务的故障判读（发布 17–18）

- **循环失败有日志**（发布 17）：后台服务（告警评估、回复保证、claim-matcher 等）的某次循环失败时，第一次失败和此后每连续 30 次失败，各写一条结构化警告 `background tick failed`，只含异常类名、SQLSTATE 和连续失败次数，不含消息或数据；恢复后写一条 `background tick recovered`，带失败次数。以前失败被静默吞掉，容器照常运行，日志为空。
- **告警评估不再随数据增长超时**（0215）：观测样本改为增量汇总。判断告警评估是否停摆，看 `control.nexloop_alert_state` 的 `last_evaluated_at`：正常时每分钟前进。
- **claim-matcher 缺合并配置时空闲**（0216，告警规则 v5）：租户没有生效的候选合并配置时，claim-matcher 进入明确的空闲状态，不再每次循环都失败。有候选在等待合并超过 10 分钟时，告警 `candidates_without_merge_configuration` 触发（warning），`nexloop-doctor` 的 `candidates_glueable` 不通过，并列出缺配置的租户。处理方法：按 §8a.11 第 5 步下发合并配置。
- **召回 profile 激活**（发布 18）：召回索引服务在每个进程首次运行时，经受治理入口激活租户配置的召回 embedding profile。这一步是幂等的；租户已有另一个生效的 profile 时，服务报错，不会静默切换。

## 9. NX-037 受控真实闭环操作说明（负责人本人执行）

> 依据：[NX-037 设计稿](implementation/NX-037-design.md)、[NX-037 A 类前置实现说明](implementation/NX-037-prep-implementation.md)、[ROADMAP](../planning/ROADMAP.md) §6–§7（D-g、D-i 定案）。参数在 [`deploy/configuration/nx037-trial.v2.json`](../deploy/configuration/nx037-trial.v2.json)（运行入口默认读 v2；v1 保留不变）。
>
> **现状**：初始化脚本、交互执行器、证据包与只读核对函数（迁移 0161）已就绪，只在合成或测试环境跑过；real driver 已实现（见 9.4，只观测、不发送），但尚未在真实租户上运行；人民币计价（D-g 方案 2）见 9.1 末尾。进入条件见 [ROADMAP §8](../planning/ROADMAP.md)。下文只用占位符（`<TENANT_ID>`、`<DSN_FILE>` 等），不写任何主机、地址或真实值。
>
> 标记：**【B】** = 负责人本人执行或逐步授权；**【A】** = 只读或仓库内的操作，维护者可准备。

### 9.1 定案参数

| 项 | 值 |
|---|---|
| 费用上限（D-g） | 累计 ¥100；单个 Run ¥2；租户月度 ¥300。先跑 3 次交互校准实际单次费用，由负责人确认或调整；建议在模型账号侧另设消费上限作为第二道防线 |
| 交互计划（D-i） | 共 30 次，配比：普通回复 15、真实交付 4、拒收后受限回复 4、人工接管 4、结果不明对账 3；两个连续工作日 10:00–18:00 |
| 立即停止（出现 1 次即停） | 越权发送、发错对象、拒收后仍联系、同一意图执行超过 1 次、跨租户读取、单个 Run 超过 ¥2、critical 告警、负责人叫停 |
| 暂停 | 最近 10 次中错误超过 10%（即至少 2 次）；累计费用达到累计上限的 80%；本月费用达到月度上限的 80%；对账窗口结束仍有结果不明的效应 |

数据库中只设置累计上限（试运行期间的 model 预算）；单个 Run 与月度上限由执行器的停止与暂停规则执行（预算表每租户每 world 只有一个 model 预算）。

**人民币计价（D-g 方案 2，负责人 2026-10-13）**：预算与费用按币种分别记录，任何地方都不做汇率换算，币种不一致一律拒绝。

- Agent Host 运行配置的 `cost_currency`（默认 `USD`，只接受 `USD` 或 `CNY`）决定 Host 成本策略的币种；Run 预算的币种必须与它相同，否则拒绝（`runtime_cost_policy_missing`）。
- `deepseek-flash` 使用非 USD 币种时必须配置 `model_tariff_file`，指向版本化价目 [`deploy/configuration/model-tariffs/deepseek-flash.cny.v1.json`](../deploy/configuration/model-tariffs/deepseek-flash.cny.v1.json)（DeepSeek 官方人民币价，记录来源与核对日期，每百万 token 的输入、缓存命中、输出价），否则 Host 拒绝启动。Host 用价目替换模型的费率，Pi 报出的用量费用与预留下限因此直接是人民币。价目一律按**高峰时段**价格计费（试运行时段 10:00–18:00 基本在高峰内）：**低谷时段会高估**（约为实际的 2 倍），只会多挡、不会超支。
- 每次模型请求的预留下限按请求大小上限（196608 字节规范化请求 JSON，超过即在 Host 侧拒绝 `runtime_model_request_too_large`）限定输入 token，而不是按 1M 上下文：(196608 × 2 + 4096 × 8) / 1e6 = ¥0.425984。Host 示例配置的 `maximum_request_cost` 为 ¥0.45。
- 人民币版策略：回复保障 [`reply-guarantee.v3.json`](../deploy/configuration/reply-guarantee.v3.json)（兜底 Run ¥1.00，2 轮）与计划复评 [`plan-reevaluation.v2.json`](../deploy/configuration/plan-reevaluation.v2.json)（¥2.00，4 轮）。使用人民币的部署须让 Reply/Plan Worker 的 `--policy-file` / `--settings-file` 指向这两个文件；已发布的 USD 版本不变。依据见 [人民币计价实现说明](implementation/NX-037-cny-pricing.md)。

### 9.2 真实租户初始化：`scripts/nx037/real_tenant_init.py`

1. **【A】plan（默认，dry-run）**：只输出将要执行的受治理步骤，什么都不执行。

   ```bash
   python scripts/nx037/real_tenant_init.py plan --tenant <TENANT_ID> --owner-principal <OWNER_PRINCIPAL_REF> --value-catalog <VALUE_CATALOG_FILE> --channel-ref <CHANNEL_REF> --period-start <PERIOD_START_ISO> --period-end <PERIOD_END_ISO>
   ```

   租户 ID 必须是 uuid，且不能是合成 demo 租户；三个费用上限默认取定案值，覆盖时必须满足“单次 ≤ 累计 ≤ 月度”。输出中包含 `Budget.set` 的完整请求体与稳定的 request_id。

2. **【B】execute（受信配置）**：只执行受信配置入口（服务授权、效应类别、告警规则），以 configurator 角色运行，可重复执行；需要负责人提供私有文件：

   ```bash
   python scripts/nx037/real_tenant_init.py execute --tenant <TENANT_ID> --owner-principal <OWNER_PRINCIPAL_REF> --value-catalog <VALUE_CATALOG_FILE> --channel-ref <CHANNEL_REF> --period-start <PERIOD_START_ISO> --period-end <PERIOD_END_ISO> --configurator-dsn-file <CONFIGURATOR_DSN_FILE> --signing-key-file <SIGNING_KEY_FILE> --signing-key-id <SIGNING_KEY_ID> --service-secrets-file <SERVICE_SECRETS_FILE> --credential-expires-at <EXPIRES_AT_ISO>
   ```

   不写任何表、不打开任何真实派发开关、不读取渠道密钥。未声明效应类别的 Action 按“触达客户、外部效应”处理（fail closed）。

3. **【B】Budget.set**：负责人本人登录工作台，用 plan 输出的请求体提交 `Budget.set`（受治理人类 Action，脚本不代为提交），把累计上限设为试运行期间的 model 预算。

4. **【A】verify（只读，metrics 角色）**：

   ```bash
   python scripts/nx037/real_tenant_init.py verify --tenant <TENANT_ID> --owner-principal <OWNER_PRINCIPAL_REF> --value-catalog <VALUE_CATALOG_FILE> --channel-ref <CHANNEL_REF> --period-start <PERIOD_START_ISO> --period-end <PERIOD_END_ISO> --metrics-dsn-file <METRICS_DSN_FILE>
   ```

   全部满足才算 ready：租户 active；三层真实派发开关都关闭；model 预算等于累计上限、币种一致并覆盖试运行期间；告警规则生效版本等于配置版本。效应类别声明数只报告。

> 真实租户的消费者 WebChat 账号、工作台用户、业务装配（business setup、catalog setup、relay 登记、`relay-run-profile.json`）、企业端点一致性检查与“结果暂时未知”演练中继，见 [NX-037 真实租户业务装配手册](implementation/NX-037-tenant-assembly.md)。

### 9.3 开放真实派发（【B】）

按 [NX-037 设计稿](implementation/NX-037-design.md) §3 第 2–3 步：渠道配置与凭据只交给 Effect Worker；只对该租户、只对选定的 Action 打开三层真实派发开关（部署总闸、租户、租户与 Action，见 [真实派发开关实现说明](implementation/real-dispatch-switch-implementation.md)），由负责人在工作台执行 `set_real_dispatch`。第三层只开 `eios:action:nexloop.enterprise.deliver:1`（与交互计划 v2 一致）；企业交付只能经 relay 的 enrollment 模式开通，见 [租户装配手册 §6](implementation/NX-037-tenant-assembly.md)。

### 9.4 受控交互：运行入口 `scripts/nx037/run_interactions.py`

依据：[NX-037 A 类前置实现说明](implementation/NX-037-prep-implementation.md) §7。执行器（`nexloop_eios.controlled_interactions`）按计划顺序逐次发起交互，每次交互的证据在下一次开始前写入 `interactions.jsonl` 并 fsync；证据只有 ID、计数、毫秒数、金额、币种与状态码（端到端、模型、工具、平台四段耗时，模型调用与失败次数，按类别费用，意图状态，拒绝码，交付回执，对账结果，规则判定）。采集器以 metrics 角色读取单个 Run 的证据（`authz.nexloop_nx037_run_evidence`）。

> **real driver（[实现说明](implementation/NX-037-prep-implementation.md) §8）**：driver 不扮演消费者、也不发送，不持有任何发送代码路径、渠道凭据或写权限。真实交互由负责人安排的知情内部人员在 WebChat 完成。每次交互 driver 做五件事：读服务端当前时间；在终端（stderr）提示操作者执行第 N 次交互的类型与要点并给出时限（提示以 ID 和代码追加到试运行目录的 `prompts.jsonl`）；以 metrics 只读角色轮询 `authz.nexloop_nx037_interaction_observation`，等到提示之后的第一条消费者消息、它的 Run 已结束、Run 的效应没有未结束的（`observed_fulfilled` 也算未结束）；从记录推出停止规则所需的事实（越权发送、发错对象、拒收后仍联系、重复投递或重复完成、跨客户读取、critical 告警、未了结的不明效应）；证据仍由 `PgCollector` 采集。在 `--interaction-timeout`（默认 900 s）内没完成的交互记为 `interaction_timeout` 并**暂停**，不猜测结果。
>
> **观测口径**（迁移 0189、0190，详见 [五类场景合成测试](implementation/NX-037-synthetic-five.md) §4.3–§4.4）：
> - 结果不明（`unknown`）的意图计入 `intents_open`：有结果不明的效应时，交互不算完成，要等对账了结或交互超时。
> - `contact_after_refusal`（拒收后仍联系）只计真正的违规（ADR-023 第 6 条）：受限期间发给该客户、没有绑定到其会话内某条来信的外发（主动联系），或同一条来信有一条以上的可见回复。对客户来信的一条绑定回复（包括拒收消息本身的回复）不计入。

#### 试运行目录中的证据文件

| 文件 | 内容 |
|---|---|
| `interactions.jsonl` | 每次交互一行证据，在下一次交互开始前写入并 fsync |
| `state.json` | 试运行状态（完成、暂停或停止、计划顺序 digest），原子写入 |
| `prompts.jsonl` | real 模式给操作者的每次提示（只有 ID 与代码） |
| `resumes.jsonl` | 每次恢复的负责人确认引用，先 fsync 再继续 |

这些文件只含 ID、计数、时间、金额与代码，保存在负责人的私有证据目录；证据包见 9.6。

#### 交互计划文件（负责人可编辑）

[`deploy/configuration/nx037-interaction-plan.v2.json`](../deploy/configuration/nx037-interaction-plan.v2.json)（默认；v2 的 `real_dispatch.actions` 是 `eios:action:nexloop.enterprise.deliver:1`，v1 保留不变）；建议复制到私有目录后修改，用 `--plan-file` 指定。

- `order`：30 次交互的类型与顺序，类型计数必须正好是 普通回复 15、真实交付 4、拒收后受限回复 4、人工接管 4、结果不明对账 3，否则拒绝。**试运行开始后顺序不能再改**（state 中记录了顺序 digest）。
- `real_dispatch`：负责人**计划打开**的三层真实派发开关状态——`deployment`（部署总闸）、`tenant` 与 `data_mode`（租户开关）、`actions`（打开的 Action 资源 ID 列表）。随附文件里的值只是样例，必须按实际计划修改。real 模式的前置检查要求实际开关与计划**完全一致**：多开或少开任何一层、任何一个 Action 都拒绝。

#### 合成模式（演练，【A】）

```bash
python scripts/nx037/run_interactions.py --driver synthetic --trial-id <TRIAL_UUID> --out <NEW_TRIAL_DIR> --plan-file <PLAN_FILE>
```

- 使用确定性 provider 与回环渠道，只在合成租户上运行；可选 `--inject <SEQ>=<FAULT>` 注入违规以演练停止规则，`--calibration-pause` 演练校准暂停。
- 测试栈（`test_stack`）只有测试代码里的关键字入口，没有命令行选项；它的每条记录都标记 `test_stack: true`，**不能作为真实证据**。

#### real 模式（【B】负责人本人）

```bash
python scripts/nx037/run_interactions.py --driver real --trial-id <TRIAL_UUID> --out <PRIVATE_EVIDENCE_DIR> --plan-file <PLAN_FILE> --tenant <TENANT_ID> --owner-principal <OWNER_PRINCIPAL_REF> --value-catalog <VALUE_CATALOG_FILE> --channel-ref <CHANNEL_REF> --period-start <PERIOD_START_ISO> --period-end <PERIOD_END_ISO> --metrics-dsn-file <METRICS_DSN_FILE> --owner-session-file <OWNER_SESSION_FILE> --host-config <AGENT_HOST_RUNTIME_CONFIG_FILE> --operator-authorization-file <OWNER_AUTHORIZATION_FILE> --webchat-provider-file <WEBCHAT_PROVIDER_FILE> --enterprise-provider-file <ENTERPRISE_PROVIDER_FILE> [--endpoint-parameters-file <NOOP_PARAMETERS_FILE>]
```

- 私有文件（DSN、负责人会话）必须是 0600 的普通文件；缺参数、带 `--inject` 或文件不私有时，在连接数据库之前就拒绝。
- **前置检查**（任何交互之前，不满足即退出码 3 并列出原因代码）：
  1. 9.2 节 verify 中除三项开关以外的检查全部通过：租户 active，model 预算等于 D-g 累计上限并覆盖试运行期间，告警规则生效；
  2. 三层开关与计划的 `real_dispatch` 完全一致：本进程的 `REAL_DISPATCH_ENABLED`；租户开关及其 data_mode、打开的 Action 开关列表（经 metrics 角色的 `authz.nexloop_nx037_open_switches` 读取）。
  3. 币种一致（D-g 方案 2，不换算）：`--host-config` 指向的 Agent Host 运行配置使用 `deepseek-flash`（否则 `host_not_real_model`），其 `cost_currency` 等于试运行币种（否则 `host_currency_differs`）；非 USD 时价目文件存在（`model_tariff_missing`）且模式、提供方、模型与币种正确（`model_tariff_invalid`）；回复保障兜底 Run 预算的币种等于试运行币种（否则 `reply_fallback_currency_differs`；检查的是 `reply-guarantee.v3.json`）。
  4. **开跑与恢复守卫**（`scripts/nx037/trial_guards.py`，开始和每次 `--resume` 都检查）：
     - **D3（负责人 2026-10-14 变更）**：试运行的客户由负责人操作的 AI 代理扮演（Claude Code / Codex 通过浏览器操作）；部署、网关、DeepSeek 真实模型、企业端点和 WebChat 都是真实的。
       - 负责人一次性操作授权（`authorization_missing`）：授权书是一份私有文件（0600），不进仓库；试运行配置的 `operator_authorization.authorization_sha256` 登记它的 sha256，运行时用 `--operator-authorization-file` 给出。
       - 参与者登记（`consent_missing`）：负责人是唯一的员工。计划 `consumers` 里的每个消费者账号都必须登记为 `kind: agent`，写明代理工具名（例如 "Claude Code via browser"），不写凭据。`kind: human` 只出现一次，就是负责人本人担任员工角色（`{"kind": "human", "role": "staff"}`，不带消费者账号）。人不会扮演客户。
       - 试运行目录的 `consent.json` 和证据包只写 consumer id、kind、persona，不写工具说明，也不写姓名。
       - 示例：`deploy/examples/nx037/trial-participants.example.json`、`interaction-plan-consumers.example.json`。
       - 全部参与者都是代理时，每条交互记录、state 和证据包都标 `persona: simulated_agent`，后续分析可以据此排除。
     - **试运行窗口**（`outside_trial_window`）：试运行配置的 `plan.window`（本地时间，时区默认 Asia/Shanghai），窗口外不开始，也不恢复。
     - **企业端点**（`enterprise_endpoint_nonconformant`）：计划打开 `nexloop.enterprise.deliver:1` 时，用 effect worker 的企业 provider 文件运行 `endpoint_conformance.py`，报告留在 `<试运行目录>/preflight/`。这项检查会在端点上创建三条带标记的效应，因此应给 `--endpoint-parameters-file`，用企业确认为空操作的参数。
     - **WebChat 投递**（`webchat_delivery_unavailable`）：经 effect worker 的回复 provider 文件请求投递服务的 `GET /v1/health`，必须返回 200 `{state: ok}`，表示凭据通过、执行者权限有效、存储可用。
     - **出站录入器**（`outbound_recorder_stalled`）：已投递但超过 60 s 仍未写入会话的回复为 0（迁移 0195）。录入器停了但当时没有积压，这项会通过；之后第一次交互收不到回复，会以 `interaction_timeout` 暂停。
- **期间实际费用**：每次交互前后各读一次租户本月 model 费用（0185）。记录里保留三项：`cost.total`（触发 Run 自身，用于单 Run 上限）、`cost.period_increase`（期间实际增量，含兜底、复评、角色 Run 和抽取等）、`cost.counted`（两者取大）。累计上限和月度上限按 `counted` 计算，所以不会因为触发 Run 之外的花费而被低估。按交互精确归属尚未实现：同期其他活动的花费也会计入这一次交互，计数只会偏高、不会偏低。
- **月度费用起点**：执行器的月度上限从租户本月（UTC）已发生的 model 费用开始计算，经 metrics 角色的 `authz.nexloop_nx037_month_model_cost`（迁移 0185，只统计试运行币种）读取，并扣除本试运行已记录的部分，避免恢复时重复计算。
- **按币种汇总**：每次交互只把试运行币种的费用计入合计；其他币种的非零费用单独记在 `other_currencies`，并立即停止（`currency_mismatch`）。
- 可选参数：`--interaction-timeout <SECONDS>`（每次交互的时限，默认 900）、`--poll-seconds <SECONDS>`（观测间隔，默认 2）。
- **校准暂停**：第 3 次交互（校准）之后自动暂停（规则 `calibration`），由负责人核对单次费用后再继续。

#### 恢复暂停

在原命令后追加 `--resume <OWNER_CONFIRMATION_REF>`（负责人确认的不透明引用，例如复核记录编号）。

- 引用先追加到 `<试运行目录>/resumes.jsonl` 并 fsync，然后继续；已记录的交互不会再执行，错误窗口重新计数。
- 暂停后不带 `--resume` 再次运行会被拒绝（`paused_needs_resume`）；没有暂停时带 `--resume` 也会被拒绝。
- 已停止的试运行不能恢复，需要用新的 trial ID 重新开始。

#### 退出码

| 码 | 含义 |
|---|---|
| 0 | 完成 |
| 1 | 暂停（错误率、费用、未了结的不明效应、交互超时 `interaction_timeout` 或校准），等负责人确认后 `--resume` |
| 2 | 停止（立即停止规则命中），按 9.5 处置 |
| 3 | 拒绝：输入错误、real 模式前置条件或开跑守卫不满足（此时不会提示操作者）、需要恢复确认、试运行已停止 |

#### 计划 v3（负责人 2026-10-14）

- 文件：`deploy/configuration/nx037-interaction-plan.v3.json`，配套 `nx037-trial.v3.json`。共 36 次交互，配比 16/6/5/5/4；上限与停止、暂停规则和 v2 相同。
- 每项写成 `{kind, variant, consumer}`。把 `consumer:<…>` 替换成代理使用的消费者账号，同一个占位符替换成同一个账号。
- `REFUSAL_1`、`REFUSAL_2` 两个账号只用于拒收类，绝不用于交付类和结果未知类，否则运行入口拒绝（`account groups`）。
- **多轮交互**：一次交互内的全部来信及其 Run 都纳入这次交互的判定（迁移 0196），包括每一轮 Run 的错误、拒收后联系、费用；单 Run 上限按其中最大的一个 Run 判定。
- **完成标记**：运行入口提示后，扮演客户的代理把这次交互的全部消息发完，再创建提示里给出的 `<试运行目录>/operator-done/<序号>` 文件，表示完成。运行入口随后等每一条来信都了结再判定；到时限仍未标记，按 `interaction_timeout` 暂停。

### 9.4a 开跑清单（负责人本人，每个试运行日）

> **客户为模拟、系统为真实**：消费者由负责人操作的 AI 代理（Claude Code / Codex 通过浏览器）扮演。负责人是唯一的员工：员工侧的工作台接管回复和参考端点工单标记，由负责人本人做，或由负责人操作代理使用负责人唯一的工作台账号来做。部署、网关、DeepSeek 真实模型、企业端点与 WebChat 都是真实的。排期：10/17 校准，10/18–19 跑 30 次以上交互，10/20 验收（ROADMAP §7，负责人 2026-10-14）。

代理扮演客户的操作说明（指令模板、账号分组、完成标记、暂停处理、禁止事项）：[NX-037-agent-players.md](implementation/NX-037-agent-players.md)。

开跑前：

1. 三层开关按计划打开（9.3 节），`real_tenant_init.py verify` 中除开关以外的检查全部通过。
2. 授权与参与者：负责人本人的一次性操作授权书放进私有目录（0600），在 `operator_authorization` 里登记 sha256；每个代理使用的消费者账号都在 `participants` 里登记（`kind: agent` 和工具名），并在计划的 `consumers` 里列出；负责人本人登记一次 `{"kind": "human", "role": "staff"}`。代理只用这些消费者账号登录 WebChat；员工侧使用负责人唯一的工作台账号。
5. **负责人在场的时点**：只有两个，一是校准后的暂停核对（第 3 次交互之后，核对单次费用，然后用 `--resume` 恢复），二是异常决策（任何停止，或校准以外的暂停：错误率、费用、未了结的不明效应、交互超时，以及 critical 告警）。其余由代理按计划执行：扮演客户发消息、接管回复、标记工单、按运行入口的提示推进。
3. 准备 provider 文件：企业 provider 文件与 effect worker 用的是同一份；回复 provider 文件指向 `webchat-delivery`；端点检查用的空操作参数文件已经和企业确认。
4. 当前时间在 `plan.window` 内。

运行入口会自动检查以下各项，不通过就拒绝开跑，按原因代码处理：同意、窗口、企业端点、WebChat 投递、录入器积压、开关、币种与 Host。

**仍需人工补偿的项**（运行入口不检查）：

- **单 Run 成本人工查看**：没有针对单个 Run 或单个请求真实成本的告警规则。
  - 每次校准暂停和每个试运行日结束时，查看 `interactions.jsonl` 每条记录的 `cost.total` 与 `cost.period_increase`；
  - 发现单次交互的期间增量明显高于触发 Run 成本时，查明来源（兜底、复评、角色 Run 或抽取）后再恢复。
  - 执行器的 `per_run_cost_exceeded` 只针对触发 Run。
- **告警值守**：告警只写入 `control.nexloop_alert_events`，没有外部通知渠道。试运行期间必须有人盯着工作台告警页或告警表：
  - critical 告警会让执行器停止（`critical_alert`），但不会通知任何人；
  - warning（例如 `budget_near_limit`、`unknown_oldest`、`effect_route_unconfigured`）只能靠值守人员发现。
- **录入器存活**：开跑时如果没有积压，守卫无法判断录入器是否在运行。第一次交互后核对回复确实出现在会话里，否则会以 `interaction_timeout` 暂停。
- **负责人会话文件**：运行入口只检查这个文件的权限，不用它登录。负责人核对自己的工作台会话有效，主体与 `--owner-principal` 一致。

### 9.5 停止或暂停之后

- **停止**：已记录的交互证据都已落盘。按 [SECURITY.md](../SECURITY.md) 的处置流程：暂停相关 scope 与派发（必要时关闭三层开关）→ 保留证据 → 撤销相关凭据 → 核对已发生的动作（结果不明只查询对账，不重发）→ 修复验证 → 受控恢复。**已停止的试运行不能恢复**，需要用新的 trial ID 重新开始。
- **暂停**：负责人复核原因（错误、费用或未了结的不明效应）后可以恢复；恢复后已记录的交互不会再执行，错误窗口重新计数。

### 9.6 证据包：`scripts/nx037/evidence_pack.py`（【A】）

```bash
python scripts/nx037/evidence_pack.py --out <NEW_EVIDENCE_DIR> --item release_manifest=<MANIFEST_FILE> --item ci_result=<CI_RESULT_FILE> --trial <TRIAL_DIR>
```

- 证据项：`inventory_before`、`inventory_after`、`release_manifest`、`ci_result`、`restore_drill`、`effect_reconciliation`、`load_report`、`public_preflight`、`third_party_notices`、`known_risks`、`interactions`（14 §7）。
- 未提供的项记为 `missing`，从不编造；一般文件只记 sha256 与字节数，不复制内容；发布清单只抽取固定字段；交互汇总为计数、耗时 p50/p95、费用、停止或暂停结论与对账状态。
- 只允许 uuid、哈希、时间、数字、布尔与固定词表中的代码；输出目录不能已存在。证据包保存在负责人的私有证据目录。

## 10. 尚未具备的运维能力

- 真实双机部署、AT-058 外部隔离探测、CI 主机上的隔离探测与断连演练（依赖 NX-032、NX-033 的 B 类步骤）。
- pgBackRest、restic、异地备份、恢复命令行与部署环境演练（NX-035 部署部分）；Artifact 与 Run 目录的文件备份。
- 只放行 Agent Host 的模型出站：独立网络命名空间已编排（Compose 示例），白名单防火墙规则随真实部署执行（B 类）。Gateway 的 Caddy 模板已提供（`deploy/stage/config/Caddyfile.template`）。
