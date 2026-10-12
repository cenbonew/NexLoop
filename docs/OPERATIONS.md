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
| 恢复 | `restored → replayed → verified_readonly → open`：恢复后派发全部关闭（NXR01），先重放删除、联系限制与吊销，再对账外部效应窗口并暂停无法核实的渠道，最后由负责人逐个 scope 执行 `open_scope` | 恢复工具目前是库函数，只在本机演练过；命令行恢复工具留到部署切片 |

详细流程与限制见 [NX-035 本地部分实现说明](implementation/NX-035-local-implementation.md)、[NX-035 设计](implementation/NX-035-design.md) 与 [架构说明 §3.11](ARCHITECTURE.md#311-备份与恢复nx-035-本地部分)。

## 6. 发布与回滚（NX-034、NX-036）

- 本地发布工具 `scripts/release/release.py`：`manifest`（发布清单）→ `sbom`（锁定版本的 syft/grype）→ `sign` → `verify`（部署的 SHA 与镜像 digest 必须等于测过的清单）；`rollback-check` 拒绝跨越未声明 expand-only 的迁移或降级运行时格式的盲目回滚，提示向前修复。只在本机运行，不推送、不打 tag、不连接服务器。见 [NX-034 本地部分实现说明](implementation/NX-034-local-implementation.md)。
- 构建、归档与同步（NX-034 第二部分）：`make build-release SHA=`（build → sbom → stage 预检 → sign → 封存）、`release.py load-check`、`scripts/release/archive.py seal|check|prune|record-deployment`、`make sync-github`（只快进 main 与负责人签名的 tag）。见 [NX-034 第二部分实现说明](implementation/NX-034-part2-implementation.md)。
- 公开发布：`release.py public-preflight` 九步门槛加负责人确认，见 [公开发布检查清单](release/public-release-checklist.md)。
- 部署侧回滚（重新渲染上一份已知可用的发布清单、防火墙与证书回退、整体撤下但保留数据）见 [NX-032 部署运行手册](implementation/NX-032-deploy-runbook.md) §8。

## 7. 本地 CI（NX-033，本地部分）

- 权威入口 `bash scripts/ci/check`（已拆成 `scripts/ci/stages/` 下可单独调用的阶段；前置阶段失败即停，测试阶段一律跑完，每阶段输出一行 `NEXLOOP-STAGE` 结果）；控制器 `scripts/ci/nexloop_ci.py`（ref 规则、按 SHA 入队、全新检出、受信判定、只快进合并、GitHub 同步）与 `Makefile` 命令契约。
- 主机侧（服务用户、bare 仓库与 hook、防火墙、remote 切换）属于 B 类，尚未执行。见 [deploy/stage/ci/README.md](../deploy/stage/ci/README.md) 与 [NX-033 本地部分实现说明](implementation/NX-033-local-implementation.md)。

## 8. 常见运维操作的入口

| 操作 | 入口 |
|---|---|
| 真实派发开关 | 只有负责人，工作台写入口 `set_real_dispatch`；部署总闸 `REAL_DISPATCH_ENABLED`、租户、（租户，Action）三层都打开才派发外部效应（[实现说明](implementation/real-dispatch-switch-implementation.md)） |
| 就绪检查 | `nexloop-api --mode stage` 的 `/health/ready`：只返回状态与原因代码（[实现说明](implementation/api-stage-ready-implementation.md)） |
| 暂停 / 恢复（租户、角色、客户、策略、Action 类型） | 工作台写入口（负责人），受治理 Action `Control.set`；恢复不回放旧任务，只登记复评 |
| 结果不明的效应 | 工作台“查询执行结果”（只把对账提前，不重发） |
| 解除联系限制 | 只有负责人（ADR-023） |
| 撤销删除后联系键的拦截 | 只有负责人，写明理由，工作台写入口 `revoke_contact_key`（NX-053） |
| 人工接管 / 交还 | 工作台（负责人、运营），默认 2 小时、上限 24 小时 |
| 删除客户或消息、保留例外、数据导出 | 只有负责人，经工作台受治理 Action（NX-029） |
| 服务授权清单 | `python -m nexloop_eios.service_grants --check|--doctor|--apply`（configurator 连接；`--doctor` 只读报告漂移）。服务凭据的 worlds 只要含 `real` 就只能是 `real`，不能同时含 real 与其他 world（AT-046，写入时拒绝） |
| 服务进程的 `--world` | runtime worker 与 guard 只接受 `real` 或 `test`；effect worker、出站记录、优惠执行者只接受 `real`；其他值在读取凭据之前退出（码 2） |
| Runtime Worker 的进程形态与回退 | [deploy/stage/README.md](../deploy/stage/README.md)（`--guard-workers 4`、`--dispatch-loops 4`，回退改回 1 并重启） |
| 连接器开发 | [CONNECTORS.md](CONNECTORS.md) |
| 评估运行 | [EVALUATION.md](EVALUATION.md) |
| 故障处理的目标流程 | [17_OPERATIONS_RUNBOOK](handoff/docs/17_OPERATIONS_RUNBOOK.md) §7 |
| 安全事件 | [SECURITY.md](../SECURITY.md) 的处置流程：暂停 scope 与派发 → 保留证据 → 撤销凭据 → 核对已发生动作 → 修复验证 → 受控恢复 |

## 9. NX-037 受控真实闭环操作说明（负责人本人执行）

> 依据：[NX-037 设计稿](implementation/NX-037-design.md)、[NX-037 A 类前置实现说明](implementation/NX-037-prep-implementation.md)、[ROADMAP](../planning/ROADMAP.md) §6–§7（D-g、D-i 定案）。参数在 [`deploy/configuration/nx037-trial.v1.json`](../deploy/configuration/nx037-trial.v1.json)。
>
> **现状**：初始化脚本、交互执行器、证据包与只读核对函数（迁移 0161）已就绪，只在合成或测试环境跑过；**真实 driver（经 WebChat 与企业服务端点发起交互）尚未实现**，属于 NX-037 B 类工作。进入条件见 [ROADMAP §8](../planning/ROADMAP.md)。下文只用占位符（`<TENANT_ID>`、`<DSN_FILE>` 等），不写任何主机、地址或真实值。
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

### 9.3 开放真实派发（【B】）

按 [NX-037 设计稿](implementation/NX-037-design.md) §3 第 2–3 步：渠道配置与凭据只交给 Effect Worker；只对该租户、只对选定的 Action 打开三层真实派发开关（部署总闸、租户、租户与 Action，见 [真实派发开关实现说明](implementation/real-dispatch-switch-implementation.md)），由负责人在工作台执行 `set_real_dispatch`。

### 9.4 受控交互：运行入口 `scripts/nx037/run_interactions.py`

依据：[NX-037 A 类前置实现说明](implementation/NX-037-prep-implementation.md) §7。执行器（`nexloop_eios.controlled_interactions`）按计划顺序逐次发起交互，每次交互的证据在下一次开始前写入 `interactions.jsonl` 并 fsync；证据只有 ID、计数、毫秒数、金额、币种与状态码（端到端、模型、工具、平台四段耗时，模型调用与失败次数，按类别费用，意图状态，拒绝码，交付回执，对账结果，规则判定）。采集器以 metrics 角色读取单个 Run 的证据（`authz.nexloop_nx037_run_evidence`）。

> **真实发送路径尚未实现**：real driver 目前只有接口。real 模式的前置条件全部满足时，入口在第一次交互之前以 `real_driver_unavailable` 退出（退出码 4），不写任何交互记录。真实发送路径属于 NX-037 B 类后续工作。

#### 交互计划文件（负责人可编辑）

[`deploy/configuration/nx037-interaction-plan.v1.json`](../deploy/configuration/nx037-interaction-plan.v1.json)；建议复制到私有目录后修改，用 `--plan-file` 指定。

- `order`：30 次交互的类型与顺序，类型计数必须正好是 普通回复 15、真实交付 4、拒收后受限回复 4、人工接管 4、结果不明对账 3，否则拒绝。**试运行开始后顺序不能再改**（state 中记录了顺序 digest）。
- `real_dispatch`：负责人**计划打开**的三层真实派发开关状态——`deployment`（部署总闸）、`tenant` 与 `data_mode`（租户开关）、`actions`（打开的 Action 资源 ID 列表）。随附文件里的值只是样例，必须按实际计划修改。real 模式的前置检查要求实际开关与计划**完全一致**：多开或少开任何一层、任何一个 Action 都拒绝。

#### 合成模式（演练，【A】）

```bash
python scripts/nx037/run_interactions.py --driver synthetic --trial-id <TRIAL_UUID> --out <NEW_TRIAL_DIR> --plan-file <PLAN_FILE>
```

- 使用确定性 provider 与回环渠道，只在合成租户上运行；可选 `--inject <SEQ>=<FAULT>` 注入违规以演练停止规则，`--calibration-pause` 演练校准暂停。

#### real 模式（【B】负责人本人）

```bash
python scripts/nx037/run_interactions.py --driver real --trial-id <TRIAL_UUID> --out <PRIVATE_EVIDENCE_DIR> --plan-file <PLAN_FILE> --tenant <TENANT_ID> --owner-principal <OWNER_PRINCIPAL_REF> --value-catalog <VALUE_CATALOG_FILE> --channel-ref <CHANNEL_REF> --period-start <PERIOD_START_ISO> --period-end <PERIOD_END_ISO> --metrics-dsn-file <METRICS_DSN_FILE> --owner-session-file <OWNER_SESSION_FILE>
```

- 私有文件（DSN、负责人会话）必须是 0600 的普通文件；缺参数、带 `--inject` 或文件不私有时，在连接数据库之前就拒绝。
- **前置检查**（任何交互之前，不满足即退出码 3 并列出原因代码）：
  1. 9.2 节 verify 中除三项开关以外的检查全部通过：租户 active，model 预算等于 D-g 累计上限并覆盖试运行期间，告警规则生效；
  2. 三层开关与计划的 `real_dispatch` 完全一致：本进程的 `REAL_DISPATCH_ENABLED`；租户开关及其 data_mode、打开的 Action 开关列表（经 metrics 角色的 `authz.nexloop_nx037_open_switches` 读取）。
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
| 1 | 暂停（错误率、费用、未了结的不明效应或校准），等负责人确认后 `--resume` |
| 2 | 停止（立即停止规则命中），按 9.5 处置 |
| 3 | 拒绝：输入错误、real 模式前置条件不满足、需要恢复确认、试运行已停止 |
| 4 | real driver 不可用（`real_driver_unavailable`，真实发送路径尚未实现） |

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
