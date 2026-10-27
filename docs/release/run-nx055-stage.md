# 执行单：NX-055 在 stage 上的剩余验收（E1 授权漂移检查一次，崩溃循环演练一次）

NX-055 的 C3、D2、E1 代码与 E2+E3 主机健康都已上 stage（任务证据：E2+E3 两台主机的探针在记录，告警规则 v13 及之后已发布）。判 done 还缺两样，本单只做这两样：

1. **E1**：在 stage 库上跑一次只读的授权漂移检查，记录输出并判定；
2. **E2**：在 APP_HOST 用一次性容器做一次崩溃循环演练，记录告警和本地通道的输出。

- **执行者**：调度员（两部分都是）。**判定者**：L6。证据里两者分开写；负责人不需要操作。
- **按 main 17aa74c1 核对过**：
  - E1 工具 `scripts/deploy/grant_drift.py`（子命令 `expected --out`、`check --database-url-file --expected [--json] [--allow-unknown-revision]`），模块 `packages/eios-core/src/nexloop_eios/grant_drift.py`（`IGNORED` 目前为空），一次性集群来自 `scripts/check_definer_search_path.py` 的 `disposable()`（`NEXLOOP_TEST_PG_BIN`，默认 Homebrew `postgresql@18`）；
  - E2 演练的出处：`docs/implementation/host-health.md` §4.6 第 3 条（L5 6620b05d），安装入口 `scripts/deploy/host_health_install.sh`，安装配置示例 `deploy/examples/host-health-install.app-host.example.json`；
  - 告警规则（stage v19）：`crash_loop`（`host_health.restarts_1h`，selector `*`，≥ 3，`for_seconds` 0，critical），`host_unit_missing`（`host_health.missing_units` > 0，`for_seconds` 120，warning）。
  - main 前进后，执行前重新核对这几项。
- 本单不改任何现有服务，不停、不重启、不 kill 任何现有容器。上一次在 sim 容器上手工 `docker kill`，sim-world-files 停了约 40 秒（host-health.md §4.6）；本单不用这种做法。

## 0. 前提（不满足就不开始）

| # | 前提 | 怎么核对 |
|---|---|---|
| P1 | 知道 stage 当前部署的 main 提交 `$RELEASE_COMMIT`（部署单记录），管理机上有它的检出 | `git -C <检出> rev-parse HEAD` 等于 `$RELEASE_COMMIT` |
| P2 | stage 库的迁移尾与 `$RELEASE_COMMIT` 的 catalog 尾相同 | `ro "select max(version) from control.schema_migrations"`（`ro` 见 run-nx057 §0.3），等于检出中 `packages/eios-core/src/eios/migrations/catalog.json` 的最后一个 `version` |
| P3 | 管理机的 PostgreSQL 与 stage 同一大版本（18） | `"$NEXLOOP_TEST_PG_BIN/postgres" --version`（未设置时用 `/opt/homebrew/opt/postgresql@18/bin/postgres`）为 18.x；`ro "show server_version"` 为 18.x。大版本不同，`acldefault` 和预定义角色会不同，E1 的比较没有意义 |
| P4 | DATA_HOST 的 `$RUN/src` 是 `$RELEASE_COMMIT` 的打包（含 `scripts/deploy` 和 `packages/eios-core/src`），属主 999；`$RUN/boot.dsn` 属主 999、0600 | 做法同 run-assembly §0.4；`stat -c '%u %a' $RUN/boot.dsn` 为 `999 600` |
| P5 | 两台主机的主机健康探针正常 | host-health.md §4.6 第 1、2 条：`probe_age_seconds` 两台都在 120 秒以内，`blind`、`missing_units` 都为 0 |
| P6 | APP_HOST 有 release 的 backend 镜像（带 digest），不需要拉取 | `docker image inspect --format '{{index .RepoDigests 0}}' "$BACKEND_IMAGE"` 有输出 |
| P7 | 钥匙串里有 APP_HOST 记录器口令条目 `nexloop/stage/host-recorder-app-host`（只查是否存在） | `security find-generic-password -s nexloop/stage/host-recorder-app-host >/dev/null && echo present` |
| P8 | 演练开始前告诉负责人：会有一次 `crash_loop` critical 告警，最长约 1 小时后自行恢复 | 调度员记录通知时间 |

变量与 run-nx057 §0.2、§0.3 相同（`TENANT_ID`、`RUN`、`NEXLOOP_ROOT`、`BACKEND_IMAGE`、`PG_CONTAINER`、`DB`、`$COMMON`、`$WINDOW`、`ro`）。另外：`RELEASE_COMMIT`，以及管理机上它的检出 `$RELEASE_CHECKOUT`。

## 1. E1：授权漂移检查（只读）

### 1.1 期望侧（管理机，A）

期望侧只能来自工具自己建、自己删的一次性集群，不能取自任何现有库（工具没有这个入口）。

```
cd "$RELEASE_CHECKOUT"
mkdir -p local-only/nx055 && chmod 700 local-only/nx055
LC_ALL=C.UTF-8 PYTHONPATH=packages/eios-core/src <venv>/bin/python scripts/deploy/grant_drift.py expected --out local-only/nx055/grant-snapshot.json
```

- `LC_ALL=C.UTF-8` 必须设置，否则 `pg_ctl` 启动失败。`<venv>` 是装有 psycopg 的开发 venv。
- **预期**：一行 `grant snapshot written: <N> entries`，退出码 0。临时目录 `nexloop-pgpath-*` 由工具删除。
- 快照里只有对象名、角色名和权限，不含口令或 DSN。仍按私有材料处理：放在 `local-only/` 下，不进仓库。
- 记下 `<N>`，以及 `python3 -c "import json;print(json.load(open('local-only/nx055/grant-snapshot.json'))['catalog_revision'])"` 的值，它应等于 P2 的迁移尾。

### 1.2 送到 DATA_HOST（A）

```
scp local-only/nx055/grant-snapshot.json <data_host>:$RUN/grant-snapshot.json
ssh <data_host> "sudo chown 999:999 $RUN/grant-snapshot.json && sudo chmod 0640 $RUN/grant-snapshot.json && sha256sum $RUN/grant-snapshot.json"
shasum -a 256 local-only/nx055/grant-snapshot.json
```

- 两边的 sha256 必须相同。

### 1.3 检查（DATA_HOST，一次性容器，A）

```
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/grant_drift.py check \
  --database-url-file /run/bootstrap-dsn --expected /stage/grant-snapshot.json --json > "$RUN/grant-drift.json"; echo "exit=$?"
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/grant_drift.py check \
  --database-url-file /run/bootstrap-dsn --expected /stage/grant-snapshot.json | tail -1
```

- **连接身份**：`nexloop_bootstrap`，经 PG 容器的本地 socket，与 run-nx057 的只读会话和其他数据主机工具相同。工具在只读事务里只读系统目录，并把 `transaction_read_only` 读回确认；不写库。
- 容器 `--network none`、只读根、UID 999；只挂 `$RUN/src`、`$RUN`（只读）、PG socket 目录和 `boot.dsn`。
- 不用 metrics 角色：它读不到 `control.schema_migrations`，只能加 `--allow-unknown-revision`，就失去了版本核对。
- 输出不含 DSN 或口令。把 `$RUN/grant-drift.json` 取回管理机 `local-only/nx055/`，然后删除 DATA_HOST 上的 `grant-drift.json` 和 `grant-snapshot.json`。

### 1.4 预期与判定

- **退出码**：0 为一致，1 为漂移或版本不符，2 为用法或连接错误。
- **第一次运行预期是 1**（工具文档已写明）：stage 的部署和运行时会在迁移之外建角色（stage 的登录角色、每个 world/租户的运行角色、主机健康记录器角色 `nexloop_host_recorder_*` 等），它们的 `role`、`member` 和授权会以 `EXTRA` 出现。`IGNORED` 目前为空，所以这些都会列出来。
- 判定者逐条分类 JSON 中的 `missing`、`extra`：

| 类别 | 例子 | 处理 |
|---|---|---|
| **R：部署或运行时在迁移之外有意建的**（来源能指到部署单或代码） | stage 登录角色、world 运行角色、主机健康记录器角色，以及它们的成员关系和授权 | 由 L 线提交 `IGNORED` 条目：每条写明 kind 和主体（不能用通配），理由至少一句并指出来源。经 L6 审、进批、上 stage 后重跑 1.3 |
| **D：真正的漂移** | 任何 `missing`；迁移角色（`nexloop_owner`、`nexloop_configurator`、应用角色等）上多出的授权、属主或角色属性 | 停下，报给调度员和 L6。不能在 stage 上手工 `GRANT`/`REVOKE`，修正走新迁移 |
| **V：版本不符** | 输出中有 `REVISION live … != snapshot …` | 快照不是按当前部署的提交生成的：回到 P1/P2，按正确的提交重做 1.1 |

- **E1 通过条件**（三条都要满足）：
  1. 在包含已审 `IGNORED` 的 release 上，1.3 退出码为 0，最后一行为 `grant drift: match (0 missing, 0 extra, <k> ignored; …)`；
  2. `revision_match` 为 true；
  3. 没有 `STALE IGNORE`（工具把它算作不一致，退出码不会是 0）。
- 第一次运行如果直接是 0：同样记录，E1 通过。
- 第一次运行有 D 类：E1 不通过，NX-055 留在 `in_progress`，另开修复任务。

### 1.5 失败处理

| 现象 | 原因 | 处理 |
|---|---|---|
| 1.1 中 `initdb` 或 `pg_ctl` 失败 | 没设 `LC_ALL=C.UTF-8`，或 PG 路径不对 | 设置后重跑；临时目录会由工具清理，若残留 `nexloop-pgpath-*`，只删这个前缀的目录 |
| 1.3 退出码 2，stderr `grant drift: OperationalError` | socket 或 `boot.dsn` 挂载不对 | 核对 P4 和 `$WINDOW`，不要改用网络 DSN 或其他角色 |
| 1.3 退出码 2，`ValueError` | 快照文件不完整 | 重做 1.2，核对 sha256 |
| 输出含 `STALE IGNORE` | `IGNORED` 中有条目已不匹配任何差异 | 由 L 线删除这一条，经审后重跑；不要保留 |

## 2. E2：崩溃循环演练（APP_HOST，一次性容器）

### 2.0 为什么这样做

- `docker restart` 是手动重启，RestartCount 不增加，探针看不到；`docker kill` 被当成手动停止，on-failure 的容器会停在 exited，不再拉起。所以不能拿现有容器演练。
- 只有容器自己以非零码退出、再由 docker 按重启策略拉起，RestartCount 才增加。演练容器每次运行 50 秒后以退出码 1 结束，最多重启 5 次，跨越约 5 个探针周期。
- **与 host-health.md §4.6 的顺序差别**：那里先起容器、再在第一次退出前改探针配置，安装步骤如果超过 50 秒，前几次重启会进入基线而不被计数。本单反过来：**先**把演练容器加进探针配置（2.2），**再立刻**起容器（2.3）。两步之间容器还不存在，`missing_units` 会短暂为 1；`host_unit_missing` 要持续 120 秒才触发，所以 2.3 必须在 2.2 完成后 60 秒内执行。

### 2.1 准备演练配置（APP_HOST，A）

当前安装配置是 `/etc/nexloop/host-health-install.json`（host-health.md §4.4）。复制一份，只加一个容器，其他字段（包括 `release_sha`）一律不改：

```
sudo python3 -I -c '
import json,sys
src,dst=sys.argv[1:]
c=json.load(open(src))
assert all(x["name"]!="hh-drill" for x in c["probe"]["containers"])
c["probe"]["containers"].append({"name":"hh-drill","container":"nexloop-hh-drill"})
open(dst,"w").write(json.dumps(c,indent=1)+"\n")
' /etc/nexloop/host-health-install.json /etc/nexloop/host-health-install.drill.json
sudo chmod 0600 /etc/nexloop/host-health-install.drill.json
sudo diff /etc/nexloop/host-health-install.json /etc/nexloop/host-health-install.drill.json
```

- **预期**：diff 只有新增的一行 `hh-drill`。配置里没有口令。

### 2.2 让探针期望演练容器（管理机 → APP_HOST，B）

口令从钥匙串经管道进 ssh 的 stdin，不出现在命令行参数、环境变量或文件里：

```
{ printf 'app_host\t'; security find-generic-password -s nexloop/stage/host-recorder-app-host -w; } | ssh <app_host> \
  "sudo HOST_HEALTH_PYTHON=/opt/nexloop/python-3.12/bin/python3.12 bash <发布 checkout>/scripts/deploy/host_health_install.sh /etc/nexloop/host-health-install.drill.json"
```

- **预期**：退出码 0；dry-run 摘要中的 `units` 比平时多 1。记下完成时间 `T_INSTALL`。
- 安装是幂等的，只会重写 `/etc/nexloop/host-health.json`，不重启任何业务服务。

### 2.3 起演练容器（APP_HOST，B，`T_INSTALL` 后 60 秒内）

```
docker run -d --name nexloop-hh-drill --label nexloop.drill=host-health --restart=on-failure:5 \
  --network none --read-only --cap-drop ALL --security-opt no-new-privileges --user 65534:65534 \
  --entrypoint sh "$BACKEND_IMAGE" -c 'sleep 50; exit 1'
```

- 本机已有的 backend 镜像，不拉取；不联网、只读、去掉全部 capability、无挂载、nobody 用户。记下起始时间 `T_DRILL`。

### 2.4 预期（约 3–6 分钟内，A）

```
docker inspect -f '{{.RestartCount}} {{.State.Status}}' nexloop-hh-drill                 # APP_HOST，逐次增长，最后为 5 exited
ro "select runtime.nexloop_metrics_snapshot('$TENANT_ID','real')->'host_health'->'restarts_1h'->'app_host:hh-drill',
           runtime.nexloop_metrics_snapshot('$TENANT_ID','real')->'host_health'->'missing_units'->'app_host:hh-drill'"
ro "select recorded_at, rule_id, kind, selector, value, rules_version from control.nexloop_alert_events where tenant_id='$TENANT_ID'
    and world='real' and rule_id in ('crash_loop','host_unit_missing') and recorded_at >= '<T_INSTALL>' order by recorded_at"
sudo journalctl -p err -u nexloop-host-health --since '<T_DRILL>' --no-pager                 # APP_HOST
```

- `restarts_1h` 中 `app_host:hh-drill` 增长到 3 以上；`missing_units` 中 `app_host:hh-drill` 为 0。
- 下一次评估后，告警事件中有 `crash_loop` 触发（critical，selector `app_host:hh-drill`）；**没有** `host_unit_missing`（2.3 及时执行时）。
- 本地通道（不依赖 PG）：journal 中有一行 `<3>nexloop-host-health crash_loop host=app_host unit=hh-drill restarts=3`（窗口 900 秒，阈值 3）。
- 5 次重启后容器停在 `5 exited`，不再增长。
- 现有服务不受影响：演练前后各执行一次 `docker ps --format '{{.Names}} {{.Status}}' | sort`，除演练容器外，各容器的 Up 时间连续，没有重启。

### 2.5 收尾（顺序不能反，B）

1. 用**原来的**配置重跑 2.2 的命令（把配置路径换回 `/etc/nexloop/host-health-install.json`），把 `hh-drill` 从探针配置撤掉。必须先做这一步，否则探针仍期望这个容器，删掉它会触发 `host_unit_missing`。
2. `docker rm -f nexloop-hh-drill`。
3. `sudo rm /etc/nexloop/host-health-install.drill.json`。
4. 核对：`docker ps -a --filter label=nexloop.drill=host-health` 为空；下一次 tick 中 `missing_units` 不含 `hh-drill`。
5. `crash_loop` 的恢复：一小时内的样本过期后，`restarts_1h` 中的 `app_host:hh-drill` 消失，告警随之恢复，最长约 1 小时。用 2.4 的告警查询看到 `crash_loop` 的恢复事件后，演练结束。

### 2.6 E2 演练通过条件

- 2.4 的四项预期都满足：`restarts_1h` ≥ 3；`crash_loop` 触发；journal 本地通道一行；容器停在 `5 exited`；
- 现有服务没有任何重启（2.4 最后一条）；
- 2.5 收尾完成，`crash_loop` 在约 1 小时内恢复，期间没有 `host_unit_missing`。

### 2.7 失败处理

| 现象 | 原因 | 处理 |
|---|---|---|
| 2.2 退出码非 0 | 安装步骤失败（输出会写明哪一步） | 不起容器。用原配置重跑 2.2 恢复，报给 L5/L6 |
| `restarts_1h` 一直为 0 | 重启发生在探针第一次看到容器之前，被计入基线 | 按 2.5 收尾，等 1 小时让样本过期，再从 2.2 重做；2.3 要紧跟 2.2 |
| `missing_units` 中 `hh-drill` 为 1 并触发 `host_unit_missing` | 2.3 晚于 120 秒，或容器名写错 | 核对容器名；告警会在容器出现后恢复，记入证据 |
| `crash_loop` 未触发，但 `restarts_1h` ≥ 3 | 告警评估没跑或规则版本不对 | 查 `select version from control.nexloop_alert_rules where tenant_id='$TENANT_ID' order by version desc limit 1`（应为 19 或更高），报给 L6，不要改规则 |
| 现有容器有重启 | 与演练无关的问题，或演练影响了它 | 立刻按 2.5 收尾，报告；不要手工重启任何服务 |

## 3. 证据（记入 NX-055 任务证据；不含口令、DSN）

- 执行者（调度员）和判定者（L6）分开写。
- E1：`$RELEASE_COMMIT`、快照的条目数和 `catalog_revision`、两边的 sha256；第一次 1.3 的退出码和最后一行；`missing`、`extra` 的分类摘要（R/D/V 各多少条）；如有 `IGNORED` 提交，写它的哈希和 L6 的审查结论；最终通过那一次的最后一行。完整 JSON 留在 `local-only/nx055/`，不公开。
- E2：`T_INSTALL`、`T_DRILL`；`docker inspect` 的最终输出；`restarts_1h` 的最大值；`crash_loop` 触发和恢复事件的时间；journal 那一行；演练前后的容器状态比对结论；收尾核对结果。
