# 主机健康：崩溃循环与定时任务新鲜度（EIOS 补齐 E2 + E3）

依据：`docs/design/eios-capability-gap.md`（L7）的 E2、E3。迁移 `0318_host_health.sql`（临时编号，由调度员重排），告警规则 `deploy/configuration/alert-rules.v12.json`，探针 `scripts/deploy/host_health_probe.py`，unit 示例 `deploy/stage/systemd/nexloop-host-health.{service,timer}.example`，配置示例 `deploy/examples/host-health.example.json`。

## 1. 要解决的问题

- **E2 崩溃循环**：容器由 `unless-stopped` 反复拉起，systemd 服务由 `Restart=` 反复拉起，状态一直是“运行中”，页面全绿。`post_deploy_check.py` 只在部署时检查一次 `RestartCount==0`。
- **E3 定时任务新鲜度**：pg-diff、restic 维护、provider-drift 等 timer 连续失败，没人知道；手工补跑成功会让“最后一次成功”看起来正常，掩盖定时轮次的失败。

## 2. 数据怎样安全取得（权限边界）

- **数据来源与被检测的服务无关**：
  - 由一个独立的探针读取主机自身的状态：`docker inspect` 读容器的 `RestartCount` 与容器 Id；`systemctl show` 读服务的 `NRestarts`，以及 timer 的 `LastTriggerUSec` 和其服务最近一次运行的开始、结束与结果。
  - 被检测的服务挂掉、卡住或反复重启，都不影响探针。
- **探针的身份与权限**：
  - 每台主机（APP_HOST、DATA_HOST）各有一个 root 的 systemd timer，每分钟运行一次。
  - 它只执行上面两条固定的只读命令：参数来自配置，不经 shell，单元名和容器名都有格式校验。
  - unit 加了 `NoNewPrivileges`、`ProtectSystem=strict`、`ProtectHome` 等加固项。
  - 需要 root，是因为读取 docker 与其他 unit 的状态本来就要求这一权限；docker 组等同 root，所以不另建一个加入 docker 组的身份，这与备份身份的已知风险相同，记入证据。
- **写入数据库**：
  - 只经新角色 `nexloop_host_recorder`：它是一个登录角色，**只能执行** `authz.nexloop_record_host_observation`，不能读任何表。
  - 写入的内容只有名称、id、计数、标志和时间，SQL 拒绝其他字段。
  - 三张表都是部署级（没有 tenant 列）、FORCE RLS、只有 owner 能访问。应用角色既不能读写这些表，也不能执行这个函数（`tests/test_host_health_pg.py`）。
- **运行时拿不到任何东西**：Agent、Run、应用服务都没有这个 DSN，也没有 docker 或管理员凭据，探针也不向它们暴露任何接口。
- **数据库连不上时**：探针仍然完成检测，输出一行 JSON 到 journal（退出码 1，只写错误类型，不写 DSN）；告警一侧由 `host_probe_stale`（5 分钟没有新记录）报出。

## 3. 判据

指标在 `runtime.nexloop_metrics_snapshot` 的 `host_health` 块中，键为 `<host_role>:<名称>`。`host` 块已经被 Agent Host 的进程样本占用，所以新块另起名字。

| 指标 | 含义 | 规则（v12） |
| --- | --- | --- |
| `restarts_1h` | 最近 1 小时的重启次数：同一实例重启计数的正增量之和，加上实例变化的次数（容器被重建计 1 次）；计数被重置（负增量）不计。窗口前最后一个样本作为基线 | `crash_loop`：≥ 3，critical |
| `timer_success_age_seconds` | 距最近一次**定时**成功运行的秒数。运行开始时间在 timer 最近一次触发后 `grace_seconds`（默认 120 秒）之内才算定时轮次；手工 `systemctl start` 不算。**fail closed**：从未见过定时成功时，从该 timer 第一次被探针列为“期望”起算 | `timer_stale_<名称>`：每个 timer 一条，阈值等于周期加余量（pg-diff 50 小时，周六到周一相隔 48 小时；pg-full 7 天 2 小时；journal 10 分钟；restic repo1 20 分钟、repo2 45 分钟；restic-maint 26 小时；provider-drift 2 小时） |
| `timer_scheduled_failures_24h` | 最近 24 小时失败的定时轮次数；之后的手工补跑不会抵消 | `timer_scheduled_failure`：> 0，warning |
| `probe_age_seconds` | 距该主机探针最近一次记录的秒数 | `host_probe_stale`：> 300，critical |
| `blind` | 最近一次探针看到的单元不到配置期望的一半（docker 或 systemd 读不到） | `host_probe_blind`：> 0，critical |

- **阈值都在配置里**：`alert-rules.v12.json` 由 configurator 发布。窗口长度体现在指标名中（1 小时、24 小时）。
- **恢复**：不再满足条件时，现有的告警评估自动把状态改为不触发（`tests/test_host_health_pg.py` 覆盖了触发与恢复）。
- **一次性单元**：不判重启，由它的 timer 运行记录判断。
- **运行台账**：`runtime.nexloop_host_timer_runs` 每次运行一行（主键为 host、timer、开始时间），探针每分钟看到同一次运行时不会重复写入。样本与 tick 保留 8 天，但第一个 tick 保留，作为 fail-closed 的起点。

## 4. 部署（B 类，负责人确认）

1. **迁移 0318**：随下一批合入。
2. **发布告警规则 v12**：由 configurator 执行 `apply_rules`。
3. **签发 `nexloop_host_recorder` 的凭据**：DATA_HOST 用本地 socket DSN；APP_HOST 用 hostssl 加客户端证书，与 metrics 客户端同一种方式。

   **尚未接入 `stage_secrets.py` 与 pg_hba**：这是部署配置变更，按 ack-policy 另行处理，见交付报告的后续项。
4. **写配置**：每台主机各写一份 `/etc/nexloop/host-health.json`（0600 root）：
   - DATA_HOST：postgres、valkey 容器，以及 pg-diff、pg-full、journal、journal-restic repo1/repo2、restic-maint 这些 timer；
   - APP_HOST：应用容器、sim 组容器、`nexloop-runtime-worker.service`、provider-drift timer。

   名称必须与规则中的 selector 一致（例如 `data_host:pg-diff`）。
5. **启用探针**：安装 unit，执行 `systemctl enable --now nexloop-host-health.timer`。先用 `--dry-run` 看一次输出。
6. **验收**：
   - 人为制造一次重启循环，例如对一个 sim 容器连续 `docker restart` 3 次，`crash_loop` 应当触发；停止之后，1 小时内恢复。
   - 手工 `systemctl start` 一个 timer 的服务，它不应刷新 `timer_success_age_seconds`。

## 5. 来源与移植（ADR-006）

- 上游：NEX-EIOS 冻结提交 `1e363db982daeca74058453ad12fc4a6cac333da`（`versions.lock.json` 的 `upstream_sources.eios`）：
  - `ops/systemd/detect_crash_loops.py`（sha256 `52f134f057563f2cb2f31c16931d2355f3a306581ee5568fc8f5ec297e7e3d17`）；
  - `ops/systemd/detect_stale_audit_evidence.py`（sha256 `4c8c3da199e975f78d051f06d519b3b4d085cd56fbcd24da39d3501dd560b602`）。
- **移植方式：按判据重写，没有逐行复制代码**，所以不进 `packages/eios-core/PROVENANCE.json`：那份文件只登记随 wheel 发布的复制文件。保留的判据是：
  - 判重启计数的增量，不判 unit 状态；
  - 跳过一次性单元；
  - 看不到就是故障，不是健康（blindness）；
  - 读不到就告警（fail closed）；
  - 检测不依赖应用环境（只用标准库，psycopg 只在写入时延迟导入）。
- **改动**：
  - 窗口和阈值从脚本参数移到 SQL 指标与告警规则；
  - 不用状态文件，历史保存在 PG；
  - blindness 以配置的期望清单为准，不和上一轮比较；
  - 产物新鲜度改为“定时成功运行”的新鲜度，并区分定时与手工；
  - 不带入任何上游单元名，单元名全部来自配置。

## 6. 限制

- **探针每分钟看一次 systemd 的“最近一次运行”**：一分钟内连续发生的两次运行（例如定时失败后马上手工补跑）只能看到后一次。对于每分钟运行的 journal，中间的运行可能看不到；新鲜度不受影响，失败计数可能偏少。完整的定时器运行台账属于 B9，不在本次范围内。
- **容器被重建计为 1 次重启**：一次正常部署（重建所有容器）最多给每个容器计 1 次，低于阈值 3。
- **Prometheus 文本导出暂未包含 `host_health` 块**：告警与工作台指标都已经包含。
