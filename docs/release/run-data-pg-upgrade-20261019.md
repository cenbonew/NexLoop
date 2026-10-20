# 执行单：DATA_HOST 的 PostgreSQL 升级到发布 22 的 postgres 镜像（2026-10-19）

- 背景：AT-055 只读核对发现，DATA_HOST 的 postgres 容器一直运行首次部署时的镜像，即发布 `20261014-76df1926d6c0-1`（pgvector 0.8.5-pg18-bookworm，PostgreSQL 18.4）。发布 22（`20261019-47aa87b898f2-1`）的 postgres 镜像是 pgvector 0.8.7-pg18-bookworm、PostgreSQL 18.6，带 5 个 Debian 安全补丁（NX-036，见 `deploy/postgres/Dockerfile`、`versions.lock.json` 的 `images.postgres`）。
- 性质：同一大版本（18）内的小版本升级，同一个数据目录直接使用，不需要 dump/restore，也不需要 pg_upgrade。这一点已经演练验证（§7）。
- 范围：只重建 DATA_HOST 的 postgres 服务，不动 valkey。不涉及 schema 迁移；APP_HOST 的镜像不变。
- 分类：**B** 表示负责人执行（sudo / docker）；**A** 表示调度员可执行的只读核对。本文只用角色名和变量；实际取值见私有部署参数。

| 变量 | 含义 |
|---|---|
| `NEXLOOP_ROOT` | DATA_HOST 的部署根目录 |
| `COMPOSE_DIR` | `$NEXLOOP_ROOT/compose`，其中有 `compose.data.yaml` 和 `compose.data.env` |
| `RELEASE_DIR` | 发布 22 的签名发布目录（含 `manifest.json`、`manifest.json.sig` 和 `images/`） |
| `PG_DB` | 业务数据库名 |
| `PG_ARCHIVE` | 清单中 postgres 镜像的归档文件名（`images[].archive`） |
| `NEW_DIGEST` / `OLD_DIGEST` | 新旧 postgres 镜像的 digest（新的取自发布 22 清单，旧的取自当前 `compose.data.env`） |
| `RUN`、`BACKEND_IMAGE` | 发布 22 在 DATA_HOST 的运行目录（含 `src/` 与 `boot.dsn`）和 backend 镜像，同 run-assembly §0.3 |

**数据库查询一律走 bootstrap 一次性容器窗口**：在 DATA_HOST 上用 `postgres` 角色直连 psql 会被 pg_hba 拒绝，这是预期行为。`$COMMON` 和 `$WINDOW` 的定义见 run-assembly §0.3，`--user` 与 `boot.dsn` 的属主一致。本文的 `Q` 是只读查询（每条 SQL 输出一行），写法同 run-release7 §1.2：

```
Q() { docker run $COMMON $WINDOW -i "$BACKEND_IMAGE" - "$@" <<'PY'
import sys, psycopg
with psycopg.connect(open('/run/bootstrap-dsn').read().strip()) as c:
    c.execute('set transaction read only')
    for q in sys.argv[1:]:
        print(q, '=>', c.execute(q).fetchall())
PY
}
```

compose 命令统一写作：

```
DC="docker compose -p nexloop-data -f $COMPOSE_DIR/compose.data.yaml --env-file $COMPOSE_DIR/compose.data.env"
```

## 0. 时机与影响

- **停机时间**：postgres 容器停止后再启动，数据库不可用约 **10–30 秒**。演练中合成库从停机到可连接只用了 1.2 秒；生产库的关闭检查点和启动恢复会更久一些。建议预留 **5 分钟**维护窗口，包括核对时间。
- **对 APP_HOST 的影响**：
  - API 和各 worker 的连接会断开，然后自动重连；
  - 窗口内的读写请求返回 503（工作台、WebChat）或在下一个 tick 重试（worker）；
  - 已受理的消息、effect 意图和队列任务都已经持久化在 PG 中，重连后继续处理，不会重复执行（幂等键和 fence 不变）。
- **建议**：选一个没有冒烟、试验和真实派发的时段。如果真实派发开关是开着的，先在设置页关闭，结束后再按原状态打开。

## 1. 前置：已验证的备份（B）

1. 用命令 A 触发一次差异备份（`nexloop-pg-diff.service`），确认状态为 `verified`：
   ```
   sudo systemctl start nexloop-pg-diff.service
   systemctl show -p Result,ExecMainStatus nexloop-pg-diff.service
   sudo journalctl -u nexloop-pg-diff.service -n 5 -o cat
   ```
   预期：`Result=success`、`ExecMainStatus=0`，最后一行 JSON 为 `ok: true` 且 `verify_status: verified`（repo1 与 repo2 各一次）。
2. 记录升级前的状态，保存到证据目录：
   ```
   docker exec -u postgres nexloop-data-postgres-1 pgbackrest --stanza=nexloop info
   Q "show server_version" "select system_identifier from pg_control_system()" "show data_directory" \
     "select extname, extversion, extowner::regrole::text from pg_extension order by 1"
   ```
   预期：
   - `18.4`；
   - 扩展为 `vector 0.8.5`、`pg_trgm 1.6`、`pgcrypto 1.4`（调度员 2026-10-19 已用只读窗口核对过一次）。
   - 记下 `system_identifier`、`data_directory` 和 vector 扩展的属主，第 4、5 步要逐项比对。

## 2. 新镜像：核对已载入（A；stage 上已完成载入）

调度员 2026-10-19 已完成以下事项，**未切换**：
- 把发布 22 的 postgres 归档传到 DATA_HOST，sha256 与清单一致（`c948f3cd…`）；
- `docker load` 后按 digest 解析为 `nexloop/postgres@sha256:647fb7d1…`；
- 核对镜像内容：PostgreSQL 18.6（Debian 18.6-1.pgdg12+2）；扩展目录有 `vector--0.8.5--0.8.6.sql`、`vector--0.8.6--0.8.7.sql`，`vector.control` 的 `default_version = '0.8.7'`；pgBackRest 2.59.3，与 stage 备份记录的 `tool_version` 一致。

执行前只需再核对一次：

```
docker image inspect --format '{{json .RepoDigests}}' "nexloop/postgres@$NEW_DIGEST"
docker run --rm --network none --entrypoint postgres "nexloop/postgres@$NEW_DIGEST" --version
docker run --rm --network none --entrypoint pgbackrest "nexloop/postgres@$NEW_DIGEST" version
```

预期：`RepoDigests` 中包含 `nexloop/postgres@$NEW_DIGEST`；`postgres (PostgreSQL) 18.6`；`pgBackRest 2.59.3`。

在其他环境首次载入时（stage 上不需要），按以下步骤：

1. 从签名发布取 postgres 归档，先验签（与发布 22 部署单相同，`deploy.py verify` 或 `release.py` 的验签步骤），再核对归档哈希：
   ```
   python3 -c 'import json,sys;m=json.load(open(sys.argv[1]));i=[x for x in m["images"] if x["name"]=="postgres"][0];print(i["archive"],i["archive_sha256"],i["digest"])' "$RELEASE_DIR/manifest.json"
   sha256sum "$RELEASE_DIR/images/$PG_ARCHIVE"
   ```
   预期：`sha256sum` 的值等于清单中的 `archive_sha256`。不一致就停下，不载入。
2. 把归档用 `scp -p` 传到 DATA_HOST 的私有目录，传完重新核对一次 `sha256sum`，然后载入：
   ```
   docker load -i "<私有目录>/$PG_ARCHIVE"
   docker image inspect --format '{{json .RepoDigests}}' "nexloop/postgres@$NEW_DIGEST"
   ```
   预期：能按 digest 解析，`RepoDigests` 中包含 `nexloop/postgres@$NEW_DIGEST`。
3. 不要在 DATA_HOST 跑 `deploy.py load-check`：它要求清单中的每个镜像都已载入，会以退出码 3 结束（同 run-release7 §2）。

## 3. 切换镜像（B）

```
cp -p "$COMPOSE_DIR/compose.data.env" "$COMPOSE_DIR/compose.data.env.before-pg-20261019"
sed -i "s|^POSTGRES_IMAGE=.*|POSTGRES_IMAGE=nexloop/postgres@$NEW_DIGEST|" "$COMPOSE_DIR/compose.data.env"
grep -E '^(POSTGRES|VALKEY)_IMAGE=' "$COMPOSE_DIR/compose.data.env"
$DC config -q && echo config-ok
$DC up -d --no-deps postgres
```

- 只改 `POSTGRES_IMAGE`，`VALKEY_IMAGE` 保持不变（第三行输出应与改动前一致）。
- `up -d --no-deps postgres` 只重建 postgres，不动 valkey。compose 会按 `stop_grace_period` 先发出关闭请求，让 PostgreSQL 正常关闭（检查点完成后再退出）。

## 4. 核对

### 4.1 数据库（B 执行，A 判读）

```
$DC ps postgres
Q "show server_version" "select system_identifier from pg_control_system()" "show data_directory" \
  "select extname, extversion from pg_extension order by 1" \
  "select last_archived_wal, last_archived_time, failed_count, last_failed_time from pg_stat_archiver"
docker exec -u postgres nexloop-data-postgres-1 pgbackrest --stanza=nexloop check
docker exec -u postgres nexloop-data-postgres-1 pgbackrest --stanza=nexloop info
```

预期：
- 容器状态为 `healthy`；`server_version` 以 `18.6` 开头；
- `system_identifier` 和 `data_directory` 与第 1 步相同；
- `vector 0.8.5`：二进制已是 0.8.7，目录中记录的版本要到第 5 步才更新；
- `pgbackrest check` 通过：WAL 能归档到 repo1 和 repo2。新旧镜像的 pgBackRest 都是 2.59.3。
- `pg_stat_archiver` 的 `last_archived_time` 在切换之后，且 `failed_count` 没有增加。

### 4.2 APP_HOST（A，重启一步为 B）

- **逐个重启 API 实例（B；部署了 v0.1 之后的批次 1 之后为可选）**。PG 重启后，API 连接池里会留下已断开的连接：池子要等到下一次取用失败才丢弃它们，所以每个这样的连接会让一个请求返回 503（`dependency_unavailable`，日志为 `psycopg.pool … discarding closed connection`）。2026-10-19 在 stage 执行时，负责人的第一次登录和第一次 erasure-precheck 都是这样返回 503 的；依次重启 api-1、api-2 后恢复。所以 §4.1 核对通过后，立即逐个重启（同时只重启一个，另一个继续服务）：
  ```
  docker compose -p nexloop-app <APP_HOST 的 compose 参数> restart api-1   # 等它 healthy
  docker compose -p nexloop-app <APP_HOST 的 compose 参数> restart api-2
  ```
  不重启也会自愈：每个旧连接只造成一次 503，但会零星出现在负责人的操作上。worker 的每个 tick 会重新取连接，失败的那个 tick 记一次 `background tick failed` 后自动恢复，所以 worker 不需要重启。批次 1 部署之后，连接池在取出连接时会先检查：PG 已经恢复时，断开的连接被换成新连接，重启后的第一次请求不再返回 503，worker 的 tick 也不再失败，所以这一步可以跳过，保留它只作为额外的保险。PG 仍在中断时，检查只重试一次新连接，连不上就让请求立即失败，不会挂起。见 known-risks-v0.1 §4。
- `docker ps` 仍是 25 个运行、0 个异常（25/0）；除了上面的 API 重启，没有其他容器重启。
- 后台服务没有持续的 tick 失败：
  - 各服务日志中 `background tick failed` 只在窗口内出现，之后有 `background tick recovered`；
  - 窗口结束后不再出现 `background tick failed`。
- 工作台能登录，“运行状态”页快照时间是新的；告警评估时间在更新，`alert_evaluator_recent` 为 true。
- 召回质量：
  ```
  nexloop-doctor --recall-quality --tenant <TENANT> --metrics-database-url-file <metrics DSN>
  ```
  预期：`recall_quality_status` 与升级前相同。向量和 HNSW 索引在小版本升级中原样保留，演练中 KNN 结果逐行一致（§7）。

## 5. pgvector 扩展从 0.8.5 更新到 0.8.7：作为运维步骤一次性执行（B）

**结论：不新增迁移，作为运维步骤由扩展属主一次性执行 `ALTER EXTENSION vector UPDATE`。** 理由：

1. **扩展版本跟随镜像，不属于 schema 演进。** 0067 只执行 `create extension vector`，不指定版本，所以全新 bootstrap 会装上当时镜像的默认版本：发布 22 镜像装 0.8.7，CI 主机用哪个 pgvector 就装哪个版本。如果写成迁移，迁移的结果就取决于运行时二进制的版本：在 0.8.5 环境上是空操作，在 0.8.7 环境上才真正更新。同一份迁移在不同环境效果不同，违反迁移可重放、校验和稳定的原则。
2. **升级内容只是元数据。** 演练核对了新镜像自带的升级脚本：`vector--0.8.5--0.8.6.sql` 除了提示行之外没有任何语句；`vector--0.8.6--0.8.7.sql` 只有 `COMMENT ON FUNCTION/TYPE`，不改函数签名、类型、操作符和索引访问方法。数据和索引不受影响，演练中更新前后 KNN 结果一致。
3. **不会改成直接写业务表。** 这是数据库扩展维护，与 pgBackRest 的 stanza 操作同属 DATA_HOST 运维，不经过业务路径，也不写业务表。
4. **迁移号冲突。** main 末尾是 0221，并行分支上还有一串临时号，在 v0.1.0 之后由调度员统一重编号。为一个只改注释的更新插入迁移，只会增加集成风险。

执行：经 bootstrap 窗口执行，它是建立扩展的那条迁移所用的身份；在 DATA_HOST 上用 `postgres` 角色直连会被 pg_hba 拒绝。第 1 步记下的属主应当就是 bootstrap DSN 的角色；不是的话，停下来报调度员，不要换用其他身份。这是本单唯一一次写入，只有这一条语句，在它自己的事务里执行：

```
docker run $COMMON $WINDOW -i "$BACKEND_IMAGE" - <<'PY'
import psycopg
with psycopg.connect(open('/run/bootstrap-dsn').read().strip()) as c:
    owner = c.execute("select extowner::regrole::text = current_user from pg_extension where extname='vector'").fetchone()[0]
    before = c.execute("select extversion from pg_extension where extname='vector'").fetchone()[0]
    if not owner or before != '0.8.5':
        raise SystemExit(f'stop: owner_is_bootstrap={owner} extversion={before}')
    c.execute('alter extension vector update')
    print(before, '->', c.execute("select extversion from pg_extension where extname='vector'").fetchone()[0],
          c.execute("select default_version, installed_version from pg_available_extensions where name='vector'").fetchone())
PY
```

预期：`0.8.5 -> 0.8.7 ('0.8.7', '0.8.7')`。输出 `stop:` 时什么都不会改，按提示处理。

核对：
- 再做一次 §4.2 的召回质量检查，结果应不变；
- 用 `Q` 执行 `select count(*) from pg_proc p join pg_depend d on d.objid=p.oid and d.deptype='e' join pg_extension e on e.oid=d.refobjid where e.extname='vector'` 的结果与更新前相同（函数数目不变）。

以后全新部署的 bootstrap 装的就是 0.8.7，不需要这一步。

## 6. 回滚（B）

| 时机 | 做法 |
|---|---|
| 第 3 步之后、第 5 步之前 | `cp -p compose.data.env.before-pg-20261019 compose.data.env`，`$DC config -q`，`$DC up -d --no-deps postgres`。同一个数据目录用旧镜像（18.4）启动，PG 小版本可以回退。演练已验证 `system_identifier` 和数据都不变 |
| 第 5 步之后 | 同上，换回旧镜像即可。旧镜像的 0.8.5 二进制可以加载目录中记录为 0.8.7 的扩展；演练中回退后 KNN 结果一致，HNSW 索引仍然可用。**扩展本身没有降级路径**：`ALTER EXTENSION vector UPDATE TO '0.8.5'` 会报 `no update path`，所以目录中的版本会保持为 0.8.7。0.8.6 和 0.8.7 的升级脚本只改了注释，这对旧二进制没有影响 |
| 数据异常（没有预料到的情况） | 按 run-33 的恢复演练路径，从第 1 步的已验证备份恢复（pgBackRest，repo1 优先） |

## 7. 演练记录（2026-10-19，CI 主机，`nexloop-ci`，rootless docker）

- 环境：
  - 旧镜像 `nexloop/postgres:20261014-76df1926d6c0-1`（digest `1c326867…`）；新镜像 `nexloop/postgres:20261019-47aa87b898f2-1`（digest `647fb7d1…`）；两者都已在 `nexloop-ci` 的镜像列表中。
  - 容器使用 `--network none`，本地 trust 认证，合成数据。
  - 演练目录在 `$CI_BUILD_ROOT/pg-upgrade-drill-<时间>`，用完已删除。没有动 stage 和 `/srv/nexloop` 下 stage 的目录。
- 步骤与关键输出：

| 步骤 | server_version | system_identifier | vector 版本（目录 / 可用默认） | 5000 行、KNN（20 条）结果 md5 | 计划 |
|---|---|---|---|---|---|
| 旧镜像初始化、建扩展、写入数据、建 HNSW | 18.4 | 7698300533402730528 | 0.8.5 / 0.8.5 | `904eb4e4…` | Index Scan using items_hnsw |
| 停旧镜像，在同一目录用新镜像启动（停机到可连接 1.2 秒） | 18.6 | 同上 | 0.8.5 / 0.8.7 | 同上 | 同上 |
| `ALTER EXTENSION vector UPDATE` | 18.6 | 同上 | 0.8.7 / 0.8.7 | 同上 | 同上 |
| 回退：同一目录用旧镜像启动 | 18.4 | 同上 | 0.8.7 / 0.8.5 | 同上 | 同上 |
| 回退后尝试 `UPDATE TO '0.8.5'` | — | — | `ERROR: extension "vector" has no update path from version "0.8.7" to version "0.8.5"` | — | — |

- 新镜像的升级脚本：`vector--0.8.5--0.8.6.sql`（无语句）、`vector--0.8.6--0.8.7.sql`（只有 `COMMENT ON …`）。
- 新旧镜像的 pgBackRest 都是 2.59.3。

## 8. 证据

放入 `local-only/evidence/v0.1-evidence/data-pg-upgrade/`：
- 第 1 步的备份结果和升级前状态；
- 第 2 步的哈希比对与 `RepoDigests`；
- 第 3 步改动前后的 `compose.data.env`（只需镜像那两行）；
- 第 4、5 步的输出；
- APP_HOST 窗口内的日志摘要（只要计数与时间）。
