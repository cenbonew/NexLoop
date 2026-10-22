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
  - 每台主机一个登录角色：`nexloop_host_recorder_app_host`、`nexloop_host_recorder_data_host`。它们**只能执行** `authz.nexloop_record_host_observation`，不能读任何表。
  - **主机身份由数据库决定**（L6 复核 C2）：函数按 `session_user` 推出 host_role，记录中带 `host_role` 字段会被拒绝。一台主机的凭据即使泄露，也写不了另一台主机的 tick、样本或定时运行，压不住另一台主机的备份新鲜度告警（`test_a_host_writes_only_its_own_ticks`）。
  - 写入的内容只有名称、id、计数、标志和时间，SQL 拒绝其他字段。
  - 三张表都是部署级（没有 tenant 列）、FORCE RLS、只有 owner 能访问。应用角色既不能读写这些表，也不能执行这个函数（`tests/test_host_health_pg.py`）。
- **运行时拿不到任何东西**：Agent、Run、应用服务都没有这个 DSN，也没有 docker 或管理员凭据，探针也不向它们暴露任何接口。
- **数据库连不上时**（L6 复核 I1）：
  - 探针仍然完成检测，输出一行 JSON 到 journal（退出码 1，只写错误类型，不写 DSN）；
  - 告警一侧由 `host_probe_stale`（5 分钟没有新记录）报出，前提是 PG 和评估器还在运行；
  - **不依赖 PG 的本地通道**：探针自己也判断崩溃循环（移植上游的锚定窗口增量，状态保存在 `state_file`，默认窗口 900 秒、阈值 3 次），并把本地发现（崩溃循环、看不见、写入失败）写成 journal 中优先级为 err 的一行（sd-daemon 的 `<3>` 前缀）。外部值守用 `journalctl -p err -u nexloop-host-health` 读取。PG 自身崩溃循环时，这是唯一能报出来的路径（限制见 §6）。

## 3. 判据

指标在 `runtime.nexloop_metrics_snapshot` 的 `host_health` 块中，键为 `<host_role>:<名称>`。`host` 块已经被 Agent Host 的进程样本占用，所以新块另起名字。

| 指标 | 含义 | 规则（v12） |
| --- | --- | --- |
| `restarts_1h` | 最近 1 小时的重启次数：同一实例重启计数的正增量之和，加上实例变化的次数（容器被重建计 1 次）；计数被重置（负增量）不计。窗口前最后一个样本作为基线 | `crash_loop`：≥ 3，critical |
| `timer_success_age_seconds` | 距最近一次**定时**成功运行的秒数。定时轮次要同时满足（L6 复核 C1）：探针看到运行开始在 timer 最近一次触发后 `grace_seconds`（默认 5 秒；timer 拉起服务通常在 1 秒内）之内；SQL 核对开始时间在触发后 1 分钟内；并且它是该次触发的**第一次**运行。同一次触发之后再跑的（例如定时轮次很快失败后，在宽限内手工补跑）一律记为手工，不刷新新鲜度，失败计数保留（`test_a_manual_rerun_inside_the_grace_is_not_the_scheduled_round`）。**fail closed**：从未见过定时成功时，从该 timer 第一次被探针列为“期望”起算 | `timer_stale_<名称>`：每个 timer 一条，阈值等于周期加余量（pg-diff 50 小时，周六到周一相隔 48 小时；pg-full 7 天 2 小时；journal 10 分钟；restic repo1 20 分钟、repo2 45 分钟；restic-maint 26 小时；provider-drift 2 小时） |
| `timer_scheduled_failures_24h` | 最近 24 小时失败的定时轮次数；之后的手工补跑不会抵消 | `timer_scheduled_failure`：> 0，warning |
| `probe_age_seconds` | 距该主机探针最近一次记录的秒数 | `host_probe_stale`：> 300，critical |
| `blind` | 最近一次探针看到的单元不到配置期望的一半（docker 或 systemd 读不到） | `host_probe_blind`：> 0，critical |
| `missing_units` | 最近一次 tick 期望、但没有看到的容器或服务，每个单元一个键（L6 复核 I2） | `host_unit_missing`：> 0 持续 120 秒，warning |

- **阈值都在配置里**：`alert-rules.v12.json` 由 configurator 发布。窗口长度体现在指标名中（1 小时、24 小时）。
- **恢复**：不再满足条件时，现有的告警评估自动把状态改为不触发（`tests/test_host_health_pg.py` 覆盖了触发与恢复）。
- **一次性单元**：不判重启，由它的 timer 运行记录判断。
- **运行台账**：`runtime.nexloop_host_timer_runs` 每次运行一行（主键为 host、timer、开始时间），探针每分钟看到同一次运行时不会重复写入。样本与 tick 保留 8 天，但第一个 tick 保留，作为 fail-closed 的起点。

- **多租户**（L6 复核 I4）：`host_health` 没有租户维度，但会拼进每个租户的快照。告警评估由持有 `nexloop.alert.evaluate` 凭据的评估器按它自己的租户执行；stage 只有一个评估器、一个租户，所以只产生一份告警。以后有多个租户各自运行评估器时，同一次崩溃循环会在每个租户各报一次，届时要把 `host_health` 只挂到部署级的运维租户上。
- **写入量**（L6 复核 S1）：每台主机每分钟最多 10 个 tick（超出时返回 `rate_limited`，不报错），每个 tick 最多 100 个单元和 100 个 timer；样本与 tick 保留 8 天；运行台账保留 90 天，但每个 timer 最后一次定时成功的记录不删。

## 4. 部署（负责人 2026-10-23 决定启用）

分两条命令：负责人在 DATA_HOST 跑一条（sudo），调度员在 APP_HOST 跑一条；然后调度员发布告警规则 v12，最后验收。工具：`scripts/deploy/host_health_install.sh`（root 包装，由 `host_health_install.py` 执行全部步骤）、`host_health_hba_data_host.sh`、`host_recorder_password.py`、`alert_rules_apply.py`。

### 4.1 取舍

- **探针的运行环境：在每台主机上建一个独立的 venv，不复用 backend 镜像。**
  - 探针要读宿主机的 docker 与 systemd，必须作为宿主机进程运行；容器里看不到宿主机的 systemd unit。
  - venv 只装 psycopg：`deploy/host-health/requirements.txt` 取自 `uv.lock` 的版本和哈希，用 `pip --require-hashes --no-deps` 从内网 PyPI 镜像安装。解释器用两台主机都已部署的 `/opt/nexloop/python-3.12.15`（与 backup-venv、restore-venv 的升级手册相同）。
  - 与其他 venv 互不影响：升级 backup-venv 不会波及探针，反之亦然。
  - 位置是 `/opt/nexloop/host-health/<发布 sha>/`，root:root，目录 0755，文件 0644 或 0755，不允许组和其他用户写入；`current` 指向当前 sha，旧 sha 的目录保留，便于回退。
- **APP_HOST 的连接方式：hostssl + scram-sha-256，客户端校验服务端证书（`sslmode=verify-full`），不使用客户端证书。** 这一点需要 L6 复核。
  - 设计原本写的是客户端证书（CN）。但在 stage 上签发新的客户端证书要动 CA，比本次上线本身的影响更大。
  - 取舍：
    - 少了一层“持有证书”的要求，连接只靠口令。口令只在 APP_HOST 的 `/etc/nexloop/host-recorder.dsn`（0600 root）中，不进任何服务的 secret 目录。
    - pg_hba 只放行 APP_HOST 地址的 /32、只放行 stage 库；角色只能执行一个函数，不能读任何表，并且只能写 app_host 自己的数据（C2）。
    - 泄露的最坏结果，是伪造 APP_HOST 的健康数据。它压不住 DATA_HOST 的备份新鲜度告警，也拿不到任何业务数据。
    - 这与 sim 角色现有的接入方式相同（pg_hba 模板中的 sim 行）。
  - 以后 CA 签发流程顺手时，可以改成 `scram-sha-256 clientcert=verify-full`（与应用角色相同），只需改模板的一行，并给 APP_HOST 发一张证书。
- **pg_hba 两行**（`deploy/stage/config/pg_hba.conf.template`，放在 sim 行之后、末尾的 reject 行之前）：

```
local   __NEXLOOP_DB__ nexloop_host_recorder_data_host                     scram-sha-256
hostssl __NEXLOOP_DB__ nexloop_host_recorder_app_host __APP_HOST_ADDR__/32  scram-sha-256
```

  DATA_HOST 走本地 socket，DSN 为 `host=/srv/nexloop/run/postgresql`。

### 4.2 前置条件：只执行 root 自己的代码（L6 复核 I1、I2）

安装以 root 运行：它读取发布 checkout，执行其中的脚本，并用其中的解释器建 venv；timer 之后也以 root 执行 release 目录中的探针。所以 `host_health_install.py` 在动手之前会逐一核对以下路径，**连同它们上面的每一级目录**，必须属于 uid 0、组和其他用户不可写、途中没有符号链接（lstat，不跟随）。任何一项不满足，就拒绝执行，什么都不做：

- 发布 checkout；
- 配置中的 python 解释器；
- 安装器所在目录及它调用的脚本（`host_health_hba_data_host.sh`、`host_recorder_password.py`、`configurator_window.py`）；
- DATA_HOST 上还有 `render.py` 与 `private_configuration.py`；
- `/opt/nexloop/host-health`。

复用已有的 release 时，同样逐个文件核对。安装过程中的 umask 为 022。

因此，**发布 checkout 必须是 root:root、只读的一份拷贝**，部署账号自己的工作树会被拒绝。做法（R1）：

```
sudo git clone <仓库> /opt/nexloop/release-<sha>
sudo git -C /opt/nexloop/release-<sha> checkout --detach <sha>
sudo chown -R root:root /opt/nexloop/release-<sha>
sudo chmod -R go-w /opt/nexloop/release-<sha>
```

- 核对的范围（R2）：两台主机上都对 `scripts/deploy` 与 `packages/eios-core/src` **全树**逐个文件核对（root 以 `-I` 运行其中的脚本，并 import `nexloop_eios`）。
- 解释器（S1）：配置中的 `python` 必须是真实文件的路径，不能是符号链接，例如 `/opt/nexloop/python-3.12.15/bin/python3.12` 或 `/usr/bin/python3.12`；`/usr/bin/python3` 是符号链接，会被拒绝。运行安装器本身的解释器用 `HOST_HEALTH_PYTHON` 指定（默认 `python3`）；所有 python 调用都加 `-I`，不读取环境变量和当前目录。
- **真正的防线是这一条前置条件**（S4）：安装器的核对是在执行前确认它成立，而不是替代它。检查与执行之间仍有极短的时间窗；只要 checkout 和 `/opt/nexloop` 从一开始就只有 root 可写，这个窗口就无从利用。

### 4.2b 准备（调度员，Mac）

1. 两个口令各生成一个，长度至少 32、随机，放进钥匙串：`nexloop/stage/host-recorder-data-host` 和 `nexloop/stage/host-recorder-app-host`，并更新口令索引。
2. 两份私有配置，以 `deploy/examples/host-health-install.{data-host,app-host}.example.json` 为模板，填入：
   - 发布 checkout 路径与 40 位发布 sha；
   - PyPI 镜像；
   - APP_HOST 一份：DATA_HOST 的 PG 地址与 CA 文件；
   - DATA_HOST 一份：bootstrap socket DSN 文件、`NEXLOOP_ROOT`、部署时的 stage.env。

   容器与 timer 的名字已经与告警规则 v12 的 selector 一致（`test_the_examples_match_the_alert_rules_v12_selectors` 会核对）。两份配置拷到各自主机，`install -m 0600 -o root`。

### 4.3 DATA_HOST（负责人，一条命令）

```
printf 'data_host\t%s\napp_host\t%s\n' "<data_host 口令>" "<app_host 口令>" | sudo bash <发布 checkout>/scripts/deploy/host_health_install.sh /etc/nexloop/host-health-install.json
```

口令由调度员经钥匙串管道送入，不进入 argv。这条命令依次：

1. 建 release 目录与 venv；
2. 写入 pg_hba 两行并 reload（与 `sim_hba_data_host.sh` 相同的保护：只允许新增这两行，删掉任何行都会被拒；容器看不到新文件或 reload 不干净时回滚；备份为 `config/pg_hba.conf.before-host-health`）；
3. 以 bootstrap 身份设置**两个**角色的口令，只发送 SCRAM 校验值；
4. 写 `/etc/nexloop/host-health.json` 和 `/etc/nexloop/host-recorder.dsn`；
5. 安装 unit 与 timer；
6. dry-run 一次并打印摘要；
7. 启用 timer。

dry-run 的结果是 blind 时就停在这里，不启用 timer。

### 4.4 APP_HOST（调度员，一条命令；在 4.3 之后）

```
printf 'app_host\t%s\n' "<app_host 口令>" | sudo bash <发布 checkout>/scripts/deploy/host_health_install.sh /etc/nexloop/host-health-install.json
```

与 4.3 相同，但不动 pg_hba、不设口令；DSN 使用 hostssl + verify-full。

### 4.5 告警规则 v12（调度员，DATA_HOST 一次性 backend 容器，与 run-41 §4.2 相同的形态）

```
... "$BACKEND_IMAGE" /opt/release/scripts/deploy/alert_rules_apply.py --manifest /opt/release/deploy/configuration/alert-rules.v12.json \
    --tenant "$TENANT_ID" --bootstrap-dsn-file /run/bootstrap-dsn
```

先在任意地方执行 `--check`（不连库）。预期输出为 `{"applied": true, "version": 12, "rules": 60, ...}`。

### 4.6 验收

1. 两台主机都执行 `systemctl list-timers nexloop-host-health.timer`，下一次运行时间不超过 1 分钟；`journalctl -u nexloop-host-health -n 5` 中每分钟一行 `"recorded": true`，没有 `<3>` 行。
2. 以 metrics 只读角色读快照的 `host_health` 块：`probe_age_seconds` 中 `app_host`、`data_host` 都在 120 秒以内；`blind` 都为 0；`missing_units` 都为 0。任何主机缺键，说明它从未写入（S2），要先排查再继续。
3. **崩溃循环演练**：在 APP_HOST 对一个 sim 容器连续执行 3 次 `docker restart <sim 容器>`（sim 组，不影响 real）。
   - 下一次评估后 `crash_loop`（`app_host:<名称>`）触发；
   - 探针的 journal 中出现 `<3>nexloop-host-health crash_loop` 一行，这是不依赖 PG 的本地通道；
   - 停止重启后 1 小时内恢复。
4. **手工补跑不算定时**：在 DATA_HOST 执行一次 `systemctl start nexloop-restic-maint.service`。`timer_success_age_seconds` 中 `data_host:restic-maint` 不应因此变小；台账中这一行 `scheduled=false`。
5. **fail closed**：首次启用后，没有定时成功记录的 timer，其 age 从首次期望时起算。例如每周一次的 pg-full 在下一个周日之前会一直增长，但不会超过它的阈值 7 天 2 小时；超过就说明该 timer 确实没有成功跑过。

**回退**：在每台主机上执行 `sudo bash host_health_install.sh <配置> --uninstall`，它会删除 timer、unit、配置、DSN、release 目录和状态目录。数据库角色和 pg_hba 行保留：没有 DSN，就没有人能用它们连接。告警规则回退到 v11，用同一个 `alert_rules_apply.py`。

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
  - 告警依据的历史保存在 PG；本地状态文件只用于不依赖 PG 的本地崩溃循环判断（与上游相同的锚定窗口）；
  - blindness 以配置的期望清单为准，不和上一轮比较；
  - 产物新鲜度改为“定时成功运行”的新鲜度，并区分定时与手工；
  - 不带入任何上游单元名，单元名全部来自配置。

## 6. 限制

- **PG 自身崩溃循环**（L6 复核 I1）：探针写不进去，评估器也跑不起来，数据库内的告警全部失效。这时只有探针的本地 err 行，要有一个外部值守读取主机 journal 才会被人看到；NexLoop 目前没有这样的外部值守，部署时要决定由谁看（例如负责人的巡检脚本）。
- **从未写入过的主机不告警**（L6 复核 S2）：部署时人工确认首次写入（§4 第 5 步）。

- **探针每分钟看一次 systemd 的“最近一次运行”**：一分钟内连续发生的两次运行（例如定时失败后马上手工补跑）只能看到后一次。对于每分钟运行的 journal，中间的运行可能看不到；新鲜度不受影响，失败计数可能偏少。完整的定时器运行台账属于 B9，不在本次范围内。
- **容器被重建计为 1 次重启**：一次正常部署（重建所有容器）最多给每个容器计 1 次，低于阈值 3。
- **Prometheus 文本导出暂未包含 `host_health` 块**：告警与工作台指标都已经包含。
