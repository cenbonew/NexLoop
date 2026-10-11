# 部署形态的 API 与就绪检查：实现说明（跨 NX-032、NX-035）

分支 `api-stage-ready`，从 main `7ee5e31` 切出。背景：规划审计发现 `nexloop-api` 只接受 `--mode test`，`/health/ready` 固定返回 503，stage Compose 示例的 api 命令无法用于真实部署。

## 1. `nexloop-api --mode stage`

- **配置来源**：全部来自文件和命令行参数，私有文件必须是 0600 的普通文件（`read_private_text`）。缺少任何必需项都拒绝启动（argparse 退出码 2，只给出固定的拒绝说明，不复述值）。必需项如下：
  - `--signing-key-id`；
  - `--execution-profile` 只能是 `real-provider` 或 `disabled`；
  - 回环 TLS 证书与私钥；
  - Host 控制三项（`--host-origin`、`--host-control-key-file`、`--host-ca-file`）；
  - 数据库 DSN 文件和签名密钥文件，并且都必须是私有文件。
- **在 stage 模式下一律拒绝的 test 专用开关**：
  - `--execution-profile deterministic-test`；
  - 环境变量 `MODEL_PROVIDER=test`；
  - 任何 `NEXLOOP_TEST_*` 环境变量。
- **环境变量中的秘密同样拒绝**：`NEXLOOP_DATABASE_URL`、`MODEL_API_KEY`、`EMBEDDING_API_KEY`。秘密只能以文件形式交给 API。
- **test 模式不变**：基础控制台与容器测试仍用它，就绪仍为 503 并带诊断信息，`container_entrypoint` 依赖这一点。
- **`deploy/stage/compose.app.example.yaml` 的 api 命令**：
  - 补上 `--mode stage`、`--execution-profile ${API_EXECUTION_PROFILE}`、`--signing-key-id ${SIGNING_KEY_ID}`；原命令缺少必需的 `--signing-key-id` 和 `--mode`，无法启动；
  - `compose.app.env.example` 增加这两个变量，示例值为 `disabled` 和 `example-signing-key`。

## 2. `/health/ready`（stage 模式）

`nexloop_eios/deployment_readiness.py`，由 `Backend.deployment_readiness` 调用：

- **返回**：就绪时 200，否则 503。响应体固定为 `{"ready", "mode": normal|read_only|not_ready, "reasons": [代码], "dispatch_switch": open|closed|invalid}`，不含任何细节。
- **原因代码与判断**：

  | 代码 | 判断 |
  |---|---|
  | `database_unavailable` | 以服务自己的受限角色连不上 PostgreSQL |
  | `database_role_invalid` | 连接角色不是受限应用角色 |
  | `migration_drift` | 已应用迁移与本发布的 catalog 不完全一致（`bootstrap.verify`） |
  | `restore_in_progress` | NX-035 恢复处于 restored 或 replayed |
  | `deletion_replay_open` | NX-029 删除重放正在进行 |
  | `dispatch_switch_invalid` | `REAL_DISPATCH_ENABLED` 的值不是 true 或 false |
  | `dispatch_switch_open_during_restore` | 总闸在 verified_readonly 期间是打开的 |
  | `signer_unavailable` | 签名密钥校验失败 |
  | `artifact_directory_unavailable` | Artifact 目录校验失败 |

- **verified_readonly**：返回 200，`mode` 为 `read_only`（只读和入站）。
- **总闸**：`dispatch_switch` 只报告状态；总闸打开本身不算不就绪。
- **后台服务与 Runtime Worker**：目前没有 HTTP 健康入口，所以没有新增。将来增加时，调用同一个 `deployment_readiness.evaluate`。

## 3. 迁移 0200（临时号）`0200_service_readiness.sql`

- **`authz.nexloop_service_readiness()`**：返回恢复状态（`runtime.nexloop_restore_readiness`）和删除重放是否进行中（只给布尔值，不带 restore_ref）。授予 api、domain_worker、action_worker、scheduler、metrics 这几个角色。
- **`authz.nexloop_backup_status()`**：返回 M12 的 backup 块、恢复状态和删除重放布尔值，只授予 `nexloop_metrics`。
- 两个函数都是 definer 函数，`search_path=pg_catalog,pg_temp`，属主 nexloop_owner，并撤销 public 的执行权。原有的 runtime 函数仍然只有 owner 可执行。
- `versions.lock.json` 的 `bootstrap_revision` 改为 `0200`。

## 4. doctor `--backup`

```
nexloop-doctor --backup --metrics-database-url-file <私有 DSN> --backup-plan deploy/configuration/backup.v1.json
```

- **`deploy/configuration/backup.v1.json`（第一版）**：按 NX-035 设计 §3.2 建立：
  - 每周完整、每天差异、增量可选（6 小时一次）；
  - WAL 持续归档，`archive_timeout` 60 秒；每次备份后校验；文件快照每 15 分钟；
  - PITR 窗口 14 天，月度完整备份最长保留 35 天；恢复日志每分钟导出；
  - 上限：备份 26 小时、完整备份 8 天（7 天加一次宽限）、演练 35 天、WAL 5 分钟、文件 1 小时、日志 5 分钟；
  - 必需仓库 repo1，可选 repo2。
- **加载校验**：键集合、模式名和数值范围都有界，任一不符即拒绝。
- **读取方式**：只通过 `nexloop_metrics` 角色读 `authz.nexloop_backup_status()`。用其他角色读、读不到状态、计划文件缺失或无效、没有备份清单，都判失败。
- **检查项**：
  - 备份：有清单；最近备份；完整备份；repo1 有备份；最近一次已校验；
  - WAL 归档正常；文件快照；日志导出；演练未超期；
  - 恢复状态为 open 或从未恢复；删除重放已关闭。

## 5. 测试 `tests/test_api_stage_ready_pg.py`（19 个，真实 PG，合成文件）

- **stage 启动**：
  - 完整配置可以启动；
  - 缺少签名 id、TLS、Host 控制或执行档案时拒绝；deterministic-test 档案也拒绝；
  - 环境中有 `MODEL_PROVIDER=test`、`NEXLOOP_TEST_*` 或秘密时拒绝；DSN 文件不是私有文件时拒绝；
  - test 模式解析不变。
- **端点**：stage 模式下没有数据库时返回 503，只有 `database_unavailable` 代码。
- **就绪各状态**（干净的 catalog）：
  - 正常；总闸 open、invalid；角色不对；签名不可用；
  - restored 和 replayed 都不就绪；verified_readonly 为只读，且总闸打开时不就绪；
  - 恢复 open 后正常；删除重放进行中不就绪；
  - 迁移校验和被改动时报 migration_drift。
- **授权**：两个函数只授予应当拥有的角色。
- **doctor `--backup`**：
  - 正例：全部在计划之内；
  - 负例：11 个单项各自失败；计划缺失或越界；状态读不到；
  - 真实 PG 上：没有备份清单时失败；不能用 api 角色读取。

## 6. 已知事项

- **临时号 0200 导致 3 个测试失败**，与以往临时迁移相同：`test_bootstrap` 的两个，以及 `test_effect_execution_sql::test_42`。原因是这些测试断言迁移版本号连续，而现在版本号是 0200、数量是 148。调度员重编号为 0148、并同步 `bootstrap_revision` 后即通过。
- **API 环境里禁止出现 `MODEL_API_KEY`**：API 本身不调用模型。如果将来 API 需要模型凭据，应改为文件形式。
