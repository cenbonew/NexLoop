# 运维手册（简明版）

面向部署与运维 NexLoop 的人。本文只做导航：说明每类运维工作当前做到哪一步、入口命令是什么、详细步骤在哪份文档里，不重复实现细节。

> **现状（以 [`planning/tasks.json`](../planning/tasks.json)、[ROADMAP](../planning/ROADMAP.md) 与 [CHANGELOG](../CHANGELOG.md) 为准）**：NexLoop **尚未部署**。双机部署（NX-032）只有参数化模板、证书脚本、部署门槛检查、dry-run 与隔离探测，没有连接任何服务器；本地 CI 队列（NX-033）、发布工具（NX-034）和备份恢复（NX-035）只完成了本地部分。真实外发渠道未开通，`REAL_DISPATCH_ENABLED` 保持关闭；`/health/ready` 固定返回 503。
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
| 恢复 | `restored → replayed → verified_readonly → open`：恢复后派发全部关闭（NXR01），先重放删除、联系限制与吊销，再对账外部效应窗口并暂停无法核实的渠道，最后由负责人逐个 scope 执行 `open_scope` | 恢复工具目前是库函数，只在本机演练过；命令行恢复工具留到部署切片 |

详细流程与限制见 [NX-035 本地部分实现说明](implementation/NX-035-local-implementation.md)、[NX-035 设计](implementation/NX-035-design.md) 与 [架构说明 §3.11](ARCHITECTURE.md#311-备份与恢复nx-035-本地部分)。

## 6. 发布与回滚（NX-034、NX-036）

- 本地发布工具 `scripts/release/release.py`：`manifest`（发布清单）→ `sbom`（锁定版本的 syft/grype）→ `sign` → `verify`（部署的 SHA 与镜像 digest 必须等于测过的清单）；`rollback-check` 拒绝跨越未声明 expand-only 的迁移或降级运行时格式的盲目回滚，提示向前修复。只在本机运行，不推送、不打 tag、不连接服务器。见 [NX-034 本地部分实现说明](implementation/NX-034-local-implementation.md)。
- 构建、归档与同步（NX-034 第二部分）：`make build-release SHA=`（build → sbom → stage 预检 → sign → 封存）、`release.py load-check`、`scripts/release/archive.py seal|check|prune|record-deployment`、`make sync-github`（只快进 main 与负责人签名的 tag）。见 [NX-034 第二部分实现说明](implementation/NX-034-part2-implementation.md)。
- 公开发布：`release.py public-preflight` 九步门槛加负责人确认，见 [公开发布检查清单](release/public-release-checklist.md)。
- 部署侧回滚（重新渲染上一份已知可用的发布清单、防火墙与证书回退、整体撤下但保留数据）见 [NX-032 部署运行手册](implementation/NX-032-deploy-runbook.md) §8。

## 7. 本地 CI（NX-033，本地部分）

- 权威入口 `bash scripts/ci/check`；控制器 `scripts/ci/nexloop_ci.py`（ref 规则、按 SHA 入队、全新检出、受信判定、只快进合并、GitHub 同步）与 `Makefile` 命令契约。
- 主机侧（服务用户、bare 仓库与 hook、防火墙、remote 切换）属于 B 类，尚未执行。见 [deploy/stage/ci/README.md](../deploy/stage/ci/README.md) 与 [NX-033 本地部分实现说明](implementation/NX-033-local-implementation.md)。

## 8. 常见运维操作的入口

| 操作 | 入口 |
|---|---|
| 暂停 / 恢复（租户、角色、客户、策略、Action 类型） | 工作台写入口（负责人），受治理 Action `Control.set`；恢复不回放旧任务，只登记复评 |
| 结果不明的效应 | 工作台“查询执行结果”（只把对账提前，不重发） |
| 解除联系限制 | 只有负责人（ADR-023） |
| 人工接管 / 交还 | 工作台（负责人、运营），默认 2 小时、上限 24 小时 |
| 删除客户或消息、保留例外、数据导出 | 只有负责人，经工作台受治理 Action（NX-029） |
| 服务授权清单 | `python -m nexloop_eios.service_grants --check|--doctor|--apply`（configurator 连接；`--doctor` 只读报告漂移） |
| Runtime Worker 的进程形态与回退 | [deploy/stage/README.md](../deploy/stage/README.md)（`--guard-workers 4`、`--dispatch-loops 4`，回退改回 1 并重启） |
| 连接器开发 | [CONNECTORS.md](CONNECTORS.md) |
| 评估运行 | [EVALUATION.md](EVALUATION.md) |
| 故障处理的目标流程 | [17_OPERATIONS_RUNBOOK](handoff/docs/17_OPERATIONS_RUNBOOK.md) §7 |
| 安全事件 | [SECURITY.md](../SECURITY.md) 的处置流程：暂停 scope 与派发 → 保留证据 → 撤销凭据 → 核对已发生动作 → 修复验证 → 受控恢复 |

## 9. 尚未具备的运维能力

- 真实双机部署、AT-058 外部隔离探测、CI 主机上的隔离探测与断连演练（依赖 NX-032、NX-033 的 B 类步骤）。
- pgBackRest、restic、异地备份、恢复命令行与部署环境演练（NX-035 部署部分）；Artifact 与 Run 目录的文件备份。
- `doctor --backup` 与可配置的备份阈值；恢复就绪状态尚未接入 `/health/ready`。
- 只放行 Agent Host 的模型出站：独立网络命名空间已定案（NX-037 D4），实现待合入。Gateway 的 Caddy 模板已提供（`deploy/stage/config/Caddyfile.template`）。
