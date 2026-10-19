# AT-072 本机恢复演练：备份 → 恢复（命名点与 PITR）→ 重放 → 校验 → 开放

- **用途**：AT-072 的恢复部分。负责人本人在 Mac 上，用一次性环境和合成数据，独立完成“备份、PITR 恢复、重放、校验、开放”。
- **边界**：
  - 只用本机：一个 WAL 归档的临时 PostgreSQL 18、一个回环 HTTPS 的 API，以及测试 profile 的合成租户；
  - 不连接任何主机，不使用任何真实凭据；
  - `REAL_DISPATCH_ENABLED=false`，模型和渠道都不会被调用。
- **与 stage 演练的区别**：stage 上的恢复演练（[NX-035 部署运行手册](../implementation/NX-035-deploy-runbook.md) §4）永远停在 `verified_readonly`，不开放。本手册的实例是一次性的，所以在恢复后**开放**：负责人经工作台的受治理 Action 执行 `open_scope`。
- **实测**：L5 于 2026-10-18 在开发机上按本手册逐条执行，结果见 §5。

## 0. 前提

| 项 | 要求 |
|---|---|
| 仓库 | 一份干净检出；在仓库根目录执行全部命令；`uv sync --frozen` 已完成 |
| PostgreSQL 18 | 含 `initdb`、`pg_basebackup`、`pg_verifybackup`。默认路径是 `/opt/homebrew/opt/postgresql@18/bin`，其他路径用 `NEXLOOP_TEST_PG_BIN` 指定 |
| 终端 | 开两个终端窗口：窗口 A 运行源端（第 1 步，一直开着）；窗口 B 执行其余步骤 |
| locale | 两个窗口都先执行 `export LC_ALL=en_US.UTF-8`。不设置时 macOS 上 PG 起不来（日志为 `postmaster became multithreaded during startup`） |
| 演练目录 | `.dev-drill/` 不存在。它被 git 忽略；脚本发现它已存在时会拒绝启动 |

窗口 B 在开始前执行一次，后面的命令都会用到这些变量：

```bash
export LC_ALL=en_US.UTF-8 PYTHONPATH=packages/eios-core/src REAL_DISPATCH_ENABLED=false
D=.dev-drill
V=.venv/bin
```

**进程基线**（启动前记一次，供 §9 核对）：

```bash
pgrep -fl 'postgres -D' > /tmp/nx-drill-baseline.txt; cat /tmp/nx-drill-baseline.txt
```

**跨 shell 共享变量（可选）**：后面的 `K2`、`K3`、`T` 分别在第 3、5、7 步产生，而 §6.1 要在另一个窗口使用 `K2`。手动操作时在每个窗口重新设置即可。自动化执行人每条命令都在新 shell 里运行时，可以改用一个状态文件：只存路径和时间，**不存任何凭据**。

```bash
S=/tmp/nx-drill-state.env   # 演练目录之外，因为第 1 步要求 .dev-drill 不存在
```

产生值的那一步之后，追加一行，例如 `echo "K2=$K2" >> "$S"`。每个新 shell 开头执行：

```bash
[ -f "$S" ] && . "$S"
```

**高精度时间**：外层计时和 `T` 都需要秒以下的精度。GNU date 可以用 `date -u +%FT%T.%NZ`；macOS 自带的 date 不支持 `%N`，改用：

```bash
python3 -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat(timespec="microseconds"))'
```

## 1. 源端：归档的 PG 与工作台 API（窗口 A）

```bash
scripts/dev/restore_drill.sh 2>&1 | tee /tmp/nx-drill-source.log
```

- **日志先放在演练目录之外**：脚本要求 `.dev-drill/` 事先不存在，所以源端的启动日志先 tee 到 `/tmp/nx-drill-source.log`。出现 `source ready` 之后，在窗口 B 执行 `mkdir -m 700 .dev-drill/logs && cp /tmp/nx-drill-source.log .dev-drill/logs/01-source.out`；之后各步的输出直接 tee 到 `.dev-drill/logs/`。
- **预期**：输出 `restore drill: source ready`，以及 `API: https://127.0.0.1:8753`。
- **它做了什么**：
  - 用测试夹具建一个 WAL 归档的临时 PG 18，执行 bootstrap，建一个 `world=test` 的合成租户；
  - 写入一个顾客会话；
  - 经受信配置（configurator）写入 owner 与 operator 两个工作台成员；
  - 发布本演练用到的两个租户 Action：`nexloop.consumer.erase` 与 `nexloop.restore.open_scope`；
  - 用生产的 `create_app` 在回环 HTTPS 上提供工作台 API。
- **生成的文件**（都在 `.dev-drill/`，0600）：
  - 角色连接串 `admin.dsn`、`recorder.dsn`、`replication.dsn`：无口令，只经该集群自己的 socket；
  - `credentials.json`：合成账号的口令，只在这个文件里；
  - `authority-key`、`tls-cert.pem`、`tls-key.pem`；
  - `stack.json`：PG 路径、端口、归档目录、租户与应用 ID。
- **一直开着，直到第 9 步**：WAL 归档在集群自己的临时目录里，窗口 A 停止就会删除它。

窗口 B 读出 `stack.json` 里的值：

```bash
j() { python3 -c "import json,sys; print(json.load(open('$D/stack.json'))[sys.argv[1]])" "$1"; }
PGB=$(j pg_bin); ARC=$(j wal_archive); SP=$(j pg_port)
```

## 2. 备份（窗口 B）

```bash
$V/nexloop-backup pg --tool pg_basebackup --recorder-dsn-file $D/recorder.dsn --replication-dsn-file $D/replication.dsn --pg-bin $PGB --target $D/base
```

预期 `"ok": true`、`"verify_status": "verified"`。备份由非超级用户 `nexloop_backup` 执行，并用 `pg_verifybackup` 校验。

```bash
mkdir -m 700 $D/journal
```

```bash
$V/nexloop-backup journal --recorder-dsn-file $D/recorder.dsn --journal-dir $D/journal
```

预期 `"ok": true`。这一步导出的是恢复日志（删除、联系限制、吊销）。

## 3. 打恢复点，记录 PITR 的目标时间

```bash
$V/nexloop-backup drill-mark --recorder-dsn-file $D/recorder.dsn --drill-id a1 --out $D/mark.json
```

预期 `"restore_point": "drill-a1"`。它在同一次调用里记录各只追加表的最大序号、计数和抽样行哈希，写入 `mark.json`。

```bash
sleep 2; T=$(date -u +%Y-%m-%dT%H:%M:%S+00:00); echo "$T"; sleep 2
```

`$T` 是第 7 步 PITR 的目标时间，比恢复点晚，比第 4 步的删除早。把它记进演练记录。

## 4. 恢复点之后：经工作台造一次合成删除

```bash
$V/python scripts/dev/drill_workbench.py --drill-dir $D --origin https://127.0.0.1:8753 erase-consumer
```

- **预期**：`"status": 200`，`"operation": "erase_consumer"`。
- **它做了什么**：负责人账号按工作台页面同样的方式登录（口令取自 `credentials.json`，不作参数，也不打印），经工作台的受治理 Action `nexloop.consumer.erase`（owner 专属）删除合成顾客，然后登出。删除会同时为该顾客设立联系限制。

```bash
$V/nexloop-backup journal --recorder-dsn-file $D/recorder.dsn --journal-dir $D/journal
```

预期 `"segments"` 中有一个新分段，`"exported_seq"` 大于 0。

```bash
"$PGB/psql" "$(cat $D/admin.dsn)" -c 'select pg_switch_wal()'
```

让删除所在的 WAL 段归档，然后等 2 秒。

> 恢复出来的库停在恢复点（或 `$T`），也就是删除之前。删除只存在于恢复日志里，所以第 5、7 步的重放必须把它重新执行一遍，AT-059 就是验证这一点。

## 5. 恢复到命名点（窗口 B）

socket 目录的完整路径要短：`<目录>/.s.PGSQL.<端口>` 不能超过 103 字节，所以用 `/tmp` 下的短目录。

```bash
K2=$(mktemp -d /tmp/nxr.XXXX)
```

```bash
$V/nexloop-restore --source basebackup --base-backup $D/base --wal-archive $ARC --target-name drill-a1 --drill-mark-file $D/mark.json --pg-bin $PGB --pg-path $PWD/$D/restored --port 55461 --socket-dir $K2 --owner-user nexloop_bootstrap --hba-method trust --restore-ref drill-a1 --journal-dir $D/journal --running-port $SP --old-primary-fenced --report-file $D/restore-a1.json
```

预期（输出的最后一行 JSON；完整报告在 `restore-a1.json`）：

| 字段 | 预期 |
|---|---|
| `passed` / `state` | `true` / `verified_readonly` |
| `journal_entries` | ≥ 2 |
| `replay.deletions` / `replay.contact_restrictions` | 都是 1：第 4 步的删除被重放 |
| `checks.controls` | 五项全为 true |
| `checks.drill_compare` | `count_mismatches` 与 `sample_mismatches` 为 0，`catalog_equal` 为 true |

不加 `--stop`：实例保持运行，供第 6 步使用。

## 6. 校验并开放（窗口 B 和窗口 C）

**6.1 在恢复出来的实例上启动 API**（开第三个终端窗口 C，先执行 §0 的变量设置，并设好 `K2`）：

```bash
$V/python scripts/dev/restore_drill_serve.py --drill-dir $D --pg-data $D/restored --pg-socket $K2 --pg-port 55461
```

- **预期**：`{"served": true, "origin": "https://127.0.0.1:8754", "state": "verified_readonly"}`，之后一直运行。
- **它做了什么**：恢复工具已经把所有应用角色设为 NOLOGIN，pg_hba 也只放行恢复者。这个命令只在演练目录里的实例上执行以下操作，然后用源端的签名密钥和账号提供工作台 API：
  - 为 API 使用的两个角色 `nexloop_api` 与 `nexloop_identity` **轮换新口令**并允许登录。旧口令随物理备份一起回来，所以要换（设计 §6.5）；
  - 在本实例的 socket 上为这两个角色加一行 scram-sha-256。

  按 Ctrl-C 停止时，这两个角色会重新设为 NOLOGIN。

**6.2 开放前的只读核对**（窗口 B）：

```bash
"$PGB/psql" "host=$K2 port=55461 dbname=postgres user=nexloop_bootstrap" -qAt -c "set role nexloop_owner" -c "select runtime.nexloop_restore_readiness()"
```

预期 `"state": "verified_readonly"`、`"dispatch": false`：恢复之后、开放之前，派发一律被拒（NXR01）。

**6.3 operator 不能开放**：

```bash
$V/python scripts/dev/drill_workbench.py --drill-dir $D --origin https://127.0.0.1:8754 --as operator open-restore-scope --restore-ref drill-a1
```

预期 `"status": 403`，`"code": "forbidden"`，**命令退出码为 1**：这是预期中的拒绝，不是失败。

在 `set -e` 的批处理里，这条命令会让整批退出。保存这个拒绝、并核对它确实是 403 的写法：

```bash
rc=0; $V/python scripts/dev/drill_workbench.py --drill-dir $D --origin https://127.0.0.1:8754 --as operator open-restore-scope --restore-ref drill-a1 > $D/logs/06.3-operator.out || rc=$?
```

```bash
[ "$rc" = 1 ] && grep -q '"status": 403' $D/logs/06.3-operator.out && echo operator-refused-as-expected
```

第二条输出 `operator-refused-as-expected` 才算通过；退出码不是 1、或状态不是 403，都是真正的失败。

**6.4 负责人开放**：

```bash
$V/python scripts/dev/drill_workbench.py --drill-dir $D --origin https://127.0.0.1:8754 open-restore-scope --restore-ref drill-a1
```

预期 `"status": 200`、`"opened": true`、`"restore_open": true`。

**6.5 开放后的核对**：

```bash
C=$(python3 -c "import json; print(json.load(open('$D/credentials.json'))['consumer_id'])")
```

```bash
"$PGB/psql" "host=$K2 port=55461 dbname=postgres user=nexloop_bootstrap" -At -c "select state, rpo_seconds, rto_seconds, opened_at is not null from control.nexloop_restore_state" -c "select state from control.nexloop_deletion_replays where restore_ref='drill-a1'" -c "select runtime.nexloop_consumer_erasing(tenant_id,'real','$C') from control.nexloop_restore_scopes limit 1" -c "select count(*) from control.nexloop_contact_restrictions where consumer_id='$C' and active" -c "select runtime.nexloop_drill_compare(pg_read_file('$PWD/$D/mark.json')::jsonb)"
```

逐行预期：

1. `open|<RPO>|<RTO>|t`
2. `replayed`
3. `t`：被删除的顾客在恢复后仍处于删除中，不可检索
4. `1`：联系限制已恢复
5. `count_mismatches` 和 `sample_mismatches` 为 0，`catalog_equal` 为 true

把 RPO 和 RTO 记进演练记录。

窗口 C 按 Ctrl-C 停止 API，再停止这个实例：

```bash
"$PGB/pg_ctl" -D $D/restored -m fast -w stop
```

## 7. PITR：按时间点恢复（窗口 B）

```bash
K3=$(mktemp -d /tmp/nxt.XXXX)
```

```bash
$V/nexloop-restore --source basebackup --base-backup $D/base --wal-archive $ARC --target-time "$T" --pg-bin $PGB --pg-path $PWD/$D/pitr --port 55462 --socket-dir $K3 --owner-user nexloop_bootstrap --hba-method trust --restore-ref pitr-a1 --journal-dir $D/journal --running-port $SP --old-primary-fenced --report-file $D/restore-pitr.json --stop
```

- **预期**：`passed: true`、`state: verified_readonly`、`instance: stopped`，并且 `replay.deletions` 为 1。`$T` 早于删除，所以删除只能靠重放得到。
- `checks.drill_compare` 为 null：时间点恢复不和恢复点标记比对。
- 第 4 步的删除和 `pg_switch_wal` 保证了目标时间之后已经有一次已归档的提交；没有这次提交，恢复会提前结束。

## 8. 记录

| 记录项 | 来源 |
|---|---|
| 备份 ID、恢复点、`$T` | 第 2、3 步的输出 |
| 两次恢复的 `passed`、`journal_entries`、`replay`、`controls` | `restore-a1.json`、`restore-pitr.json` |
| operator 被拒、owner 开放成功 | 第 6.3、6.4 步的输出 |
| 开放后的状态、RPO、RTO、删除重放、比对 | 第 6.5 步的输出 |

报告里只有 ID、数字和代码。把它们拷到私有证据目录，不要拷 `credentials.json`、`*.dsn` 或 `authority-key`。

## 9. 清理

顺序不能颠倒：**先停源端 → 核对 PG 已退出 → 复制日志与证据 → 最后删除 `.dev-drill`**。`stack.json` 和 `pg-root` 都在 `.dev-drill` 里，删早了就无法核对源端的 PG 是否已经停止。

1. **停源端**：在窗口 A 按 Ctrl-C。脚本会停止 API 和源端 PG，并删除集群的临时目录。
2. **核对 PG 已退出**（窗口 B）：

   ```bash
   PGD="$(cat $D/pg-root)/data"; if [ -f "$PGD/postmaster.pid" ] && kill -0 "$(head -1 "$PGD/postmaster.pid")" 2>/dev/null; then echo source-pg-running; else echo source-pg-stopped; fi
   ```

   预期 `source-pg-stopped`。如果输出 `source-pg-running`（例如源端是被强制结束的，没有执行清理），用下面的**显式停止命令**。它只停 `pg-root` 记录的那一个实例，只删那一个以 `nexloop-pg-` 开头的临时目录：

   ```bash
   scripts/dev/restore_drill.sh --stop
   ```

   预期输出 `restore drill: stopped`，然后再执行一遍上面的核对。
3. **复制日志与证据**：把 `$D/logs/`、`$D/restore-a1.json`、`$D/restore-pitr.json` 复制到私有证据目录。不要复制 `credentials.json`、`*.dsn`、`authority-key`。
4. **删除**：

   ```bash
   rm -rf $D "$K2" "$K3" /tmp/nx-drill-state.env
   ```

5. **核对没有残留进程**：

   ```bash
   pgrep -fl 'postgres -D' | diff /tmp/nx-drill-baseline.txt - && echo same-as-baseline
   ```

   预期 `same-as-baseline`。多出来的行如果是本演练的实例（数据目录在 `nexloop-pg-*`、`.dev-drill/restored` 或 `.dev-drill/pitr` 下），用 `pg_ctl -D <该目录> -m fast -w stop` 停掉。

## 10. 出错时

| 现象 | 原因与做法 |
|---|---|
| 第 1 步 `setup failed` | 读 `.dev-drill/server.log` 的最后几行；常见原因是 PG 18 的路径不对（设置 `NEXLOOP_TEST_PG_BIN`），或者端口 8753 已被占用（设置 `NEXLOOP_DRILL_API_PORT`） |
| 第 5、7 步 `restore target is not empty` | `--pg-path` 已存在；换一个新目录 |
| 第 5、7 步提示端口被占用 | 换一个空闲端口；源端端口要用 `--running-port` 列出 |
| 第 5、7 步只输出 `configuration unavailable` | 参数解析失败。在 zsh 中不要用 `set -- $变量` 来拆分参数，按 §1 的方式分别取值 |
| 第 6.1 步 `not verified_readonly` | 第 5 步没有通过，或者实例已被停止 |
| 窗口 A 按 Ctrl-C 后源端 PG 没有退出（2026-10-19 第二、三次演练） | 修复前的脚本在两种情况下会留下 PG：一是经 `\| tee` 运行时收到 SIGHUP，holder 被直接结束，fixture 的 teardown 没有执行；二是脚本或整个进程组被 SIGKILL，而 PG 在自己的进程组里，不会跟着结束。现在脚本会忽略 SIGPIPE，holder 会忽略 SIGHUP，并且有一个独立会话里的看门狗兜底。仍有残留时，执行 `scripts/dev/restore_drill.sh --stop`（§9 第 2 步） |
| 第 1 步显示 `source ready`，但后面的 API 请求连不上 | 端口 8753 被别的进程占用：holder 在 API 开始监听之前就写了 ready，`.dev-drill/server.log` 中会有 `address already in use`。换一个端口（`NEXLOOP_DRILL_API_PORT`），从第 1 步重来；之后各步 `--origin` 里的端口也要改成同一个 |
| 第 6.4 步返回 403 | 登录的不是 owner，或者 `--restore-ref` 与第 5 步不一致 |
