# 命令单：metrics DSN 上线与部署闸门一次性运行（2026-10-16）

本单写给负责人早上直接执行。只用角色名与变量，不写地址。**B** 表示需负责人确认；**sudo-A** 和 **sudo-D** 分别表示需要 APP_HOST 和 DATA_HOST 的 sudo。

**变量**（每台主机在自己的 shell 里设好）：

| 变量 | 含义 |
|---|---|
| `$RELEASE_CHECKOUT` | main 的检出，至少包含本单的提交（`gate_dir.py`、`metrics_data_host.sh`、G-METRICS-DSN 的 pg_hba 模板），三处都要有 |
| `$NEXLOOP_ROOT` | 主机上的部署根目录 |
| `$STAGE_ENV` | 该主机上私有的 stage.env，也就是当初渲染防火墙和 pg_hba 时用的那一份 |
| `$MGMT_TREE` | 管理机上 `stage_secrets.py generate` 生成的秘密树 |
| `$CA_DIR` | 管理机上的中间 CA 目录 |
| `$PARAMS` | 管理机上的私有 params |
| `$BACKEND_IMAGE` | 发布清单里 backend 的完整 digest 引用，即 `nexloop/backend@sha256:c9b816dd…`，取完整值，不按标签 |

顺序：§1 先完成，因为 §2 闸门的 roles 检查要用 §1 在 APP_HOST 装好的 DSN。§2 还要在 run-42 §8（claim 出口网络上线）之后执行，原因见 §2.3。

## 1. G-METRICS-DSN 上线

### 1.1 管理机：给现有秘密树补 metrics 客户端（不需要 sudo）

```
cp -p "$MGMT_TREE/manifest.json" "$MGMT_TREE/manifest.json.before-metrics"
python3 "$RELEASE_CHECKOUT/scripts/deploy/stage_secrets.py" metrics --out "$MGMT_TREE" --ca-dir "$CA_DIR" --params "$PARAMS"
python3 "$RELEASE_CHECKOUT/scripts/deploy/stage_secrets.py" check --out "$MGMT_TREE"
```

- **预期**：
  - 第二条输出 `{"added": ["management/metrics", "data:backup/metrics.dsn", "management/metrics-role-password.sql"]}`；
  - 第三条输出 `"ok": true`。
- **写出的文件**：
  - `management/metrics/` 下四个文件：`metrics.dsn`（verify-full，用户 `nexloop_metrics`，不带口令）、`metrics-pg-client.crt`、`metrics-pg-client.key`、`pg-ca.crt`；
  - `data/secrets/backup/metrics.dsn`：DATA_HOST 用的 socket DSN，带口令；
  - `management/metrics-role-password.sql`：一条 `ALTER ROLE`，只含 SCRAM 校验值。
  - 所有文件都是 0600，不输出任何值。
- **撤销**：
  ```
  shred -u $MGMT_TREE/management/metrics/* $MGMT_TREE/data/secrets/backup/metrics.dsn $MGMT_TREE/management/metrics-role-password.sql
  rmdir $MGMT_TREE/management/metrics
  ```
  然后把 `manifest.json.before-metrics` 恢复为 `manifest.json`。
- **再次执行**：同一棵树会被拒绝，退出码 2，不会覆盖已有文件。

### 1.2 传到两台主机（不需要 sudo）

- **DATA_HOST**：放到私有目录 `$METRICS_SRC`（0700，属主为负责人账户），两个文件：
  - `data/secrets/backup/metrics.dsn` → `$METRICS_SRC/metrics.dsn`；
  - `management/metrics-role-password.sql` → `$METRICS_SRC/metrics-role-password.sql`。
- **APP_HOST**：`management/metrics/` 下四个文件放到私有目录 `$METRICS_APP_SRC`（0700）。
- 用 `scp -p`，传完核对 sha256 一致。

### 1.3 DATA_HOST：一个脚本完成 sudo-D 的全部步骤（**B**，sudo-D）

```
sudo NEXLOOP_ROOT="$NEXLOOP_ROOT" STAGE_ENV="$STAGE_ENV" RELEASE_CHECKOUT="$RELEASE_CHECKOUT" METRICS_SRC="$METRICS_SRC" \
  bash "$RELEASE_CHECKOUT/scripts/deploy/metrics_data_host.sh"
```

只输入一次 sudo 口令。脚本按顺序执行以下五步，任何一步失败都停在那里，输出 `FAILED step N: <原因>`。

| 步 | 内容 | 预期输出 |
|---|---|---|
| 1 | **预检**：输入文件齐全；`secrets/backup/metrics.dsn` 还不存在；SQL 只有一条针对 `nexloop_metrics` 的 SCRAM `ALTER ROLE`；用 `$STAGE_ENV` 重新渲染 pg_hba，新文件与已安装的相比**只多出两条 `nexloop_metrics` 规则**，一条不少，也没有别的新增 | `step 1: preflight` |
| 2 | 以 `nexloop_bootstrap` 经容器内 socket 执行那条 `ALTER ROLE`，即 bootstrap 窗口。口令只在容器内从 `/run/secrets/pg_bootstrap_password` 读取，不出现在主机的命令行上 | `step 2: …` |
| 3 | 备份 `config/pg_hba.conf` 为 `.before-metrics`，**原地写入**新内容（inode 不变，属主和权限不变）；先在容器内核对 `/etc/postgresql/pg_hba.conf` 与主机文件一致、且含 `nexloop_metrics` 行，再对 postgres 发 HUP，确认日志出现 `received SIGHUP`、没有 pg_hba 报错。容器看不到新内容或重载不干净时，原内容原地写回，然后报错退出 | `step 3: …` |
| 4 | 装入 `$NEXLOOP_ROOT/secrets/backup/metrics.dsn`，属主 `nexloop-backup`，0600；已存在则不覆盖，第 1 步已拦下 | `step 4: …` |
| 5 | 以 `nexloop-backup` 用这个 DSN 执行 `select current_user` | `done: current_user=nexloop_metrics; …` |

- **注意：单文件绑定挂载与 inode**（2026-10-16 stage 实测）：postgres 容器对 `config/pg_hba.conf` 是单文件绑定挂载，绑定的是容器启动时的那个 inode。
  - 用 `install`、`mv` 或编辑器的“另存后替换”换掉这个文件，主机路径会指向新 inode，而容器里看到的仍是旧内容（旧 inode 的链接数为 0）。这时 HUP 重载的还是旧规则，第 5 步报 `no pg_hba.conf entry`。
  - 脚本现在原地写入，并在 HUP 前从容器内核对。
  - 如果第 3 步报“the container does not see the new pg_hba”，说明容器仍持有旧 inode（例如之前有人用 `install` 换过文件）。处理：`docker compose -p nexloop-data restart postgres`，容器重启时会重新挂载当前 inode；然后从第 3 步重跑。这次重启有短暂停机，DATA_HOST 上的 PG 会中断几秒。
- **第 2 步失败**最可能的原因：M9 之后 `nexloop_bootstrap` 已改为 NOLOGIN。先按 M9 的窗口规则临时恢复它，再重跑。这时第 1 步已经通过，pg_hba 还没改。
- **撤销**（sudo-D，**B**）：
  1. 原地写回（保持 inode）：`cat $NEXLOOP_ROOT/config/pg_hba.conf.before-metrics > $NEXLOOP_ROOT/config/pg_hba.conf`，然后 `docker compose -p nexloop-data kill -s HUP postgres`，再用 `docker compose -p nexloop-data exec -T postgres grep -c nexloop_metrics /etc/postgresql/pg_hba.conf` 核对容器内为 0；
  2. `shred -u $NEXLOOP_ROOT/secrets/backup/metrics.dsn`；
  3. 口令的撤销需要 bootstrap 窗口：`ALTER ROLE nexloop_metrics PASSWORD NULL`。

### 1.4 APP_HOST：安装 ops/metrics（**B**，sudo-A）

```
sudo install -d -o root -g root -m 0700 "$NEXLOOP_ROOT/ops/metrics"
sudo install -o root -g root -m 0600 "$METRICS_APP_SRC"/* "$NEXLOOP_ROOT/ops/metrics/"
sudo ls -l "$NEXLOOP_ROOT/ops/metrics"     # 四个文件，root:root，-rw-------
```

**核对**：连接从应用网络发出，因为 pg_hba 只放行来自 APP_HOST 的证书连接。用一次性容器，共享 app-net 的网络命名空间，以 root 读取这个 root 0600 的 DSN：
```
APPNET=$(sudo docker compose -p nexloop-app ps -q app-net)
sudo docker run --rm --user 0 --read-only --network "container:$APPNET" -v "$NEXLOOP_ROOT/ops/metrics:$NEXLOOP_ROOT/ops/metrics:ro" \
  --entrypoint python3 "$BACKEND_IMAGE" -c 'import sys,psycopg
from nexloop_eios.private_configuration import read_private_text
with psycopg.connect(read_private_text(sys.argv[1],maximum=16384),connect_timeout=5) as c:print(c.execute("select current_user").fetchone()[0])' \
  "$NEXLOOP_ROOT/ops/metrics/metrics.dsn"
```

- **预期**：输出 `nexloop_metrics`。这说明证书认证生效，而且没有用口令。
- **不符时**：
  - `certificate authentication failed`：证书的 CN 或 CA 不对；
  - `no pg_hba.conf entry`：1.3 的 pg_hba 没有生效。
- **撤销**：`sudo shred -u $NEXLOOP_ROOT/ops/metrics/*`，然后 `sudo rmdir $NEXLOOP_ROOT/ops/metrics`。

### 1.5 DATA_HOST：备份核对（run-46 §7.4，不需要 sudo 以外的权限）

```
sudo -u nexloop-backup /opt/nexloop/backup-venv/bin/nexloop-doctor --backup --metrics-database-url-file "$NEXLOOP_ROOT/secrets/backup/metrics.dsn" \
  --backup-plan "$RELEASE_CHECKOUT/deploy/configuration/backup.v1.json"
```

- **预期**：以下各项为 true：
  - `backup_manifest_present`、`backup_recent`、`full_backup_recent`、`required_repos_recent`、`latest_backup_verified`；
  - `wal_archiving`、`restore_drill_recent`、`restore_state_open`、`deletion_replay_closed`。
- **预期为 false 的两项**：`file_backup_recent`（M12）；`journal_export` 在恢复日志定时任务启用前为 false。
- **不符时**：只记录检查名，报调度员；`wal_archiving` 为 false 时最优先。

## 2. 部署闸门一次性运行（APP_HOST；**B**，sudo-A）

### 2.1 组装 GATE_DIR

```
GATE_DIR=$NEXLOOP_ROOT/ops/gate-20261016
sudo python3 "$RELEASE_CHECKOUT/scripts/deploy/gate_dir.py" --nexloop-root "$NEXLOOP_ROOT" --release-checkout "$RELEASE_CHECKOUT" \
  --stage-env "$STAGE_ENV" --gate-dir "$GATE_DIR" \
  --planned-ports <GATEWAY_HTTPS_PORT>,<GATEWAY_REDIRECT_PORT>,<HOST_INTERNAL_PORT>,<GUARD_PORT>,<WEBCHAT_DELIVERY_PORT> \
  --before <首次部署时的 before.json>      # 有就给；没有就去掉这一行，工具现场生成一份
```

`gate_dir.py` 只用标准库，以 root 运行，因为 inventory 要读 `ss` 和 `nft`。它自己不读任何秘密，只往一个新目录（0700，文件 0600）写入以下文件：

| 文件 | 来源 |
|---|---|
| `stage.env` | `$STAGE_ENV` 的副本 |
| `before.json` / `after.json` | `inventory.py --phase before/after`；`before` 优先用 `--before` 给的首次部署版本 |
| `running.json` | `running_snapshot.py --project nexloop-app` |
| `compose.json` | `docker compose -p nexloop-app -f $NEXLOOP_ROOT/config/compose.app.yaml --env-file …/config/compose.app.env --env-file …/config/compose.images.env config --format json` |
| `valkey.conf` | 用同一份 stage.env 渲染 `valkey.conf.template`。DATA_HOST 上的文件出自同一模板和 env |
| `tls.json` | **从 `compose.json` 的 secrets 段推导，不靠猜**（详见下面三条） |
| `spec.json` | 由 `deploy-gate.app.example.json` 填好；`roles_database_url_file` 指向 `$NEXLOOP_ROOT/ops/metrics/metrics.dsn` |

`tls.json` 的内容：
- **五张服务证书**：`gateway_cert`/`key`、`api_loopback_cert`/`key`、`host_cert`/`key`、`guard_cert`/`key`、`webchat_delivery_cert`/`key`；
- **证书校验名**取客户端实际校验的名字：
  - Gateway 用 stage.env 的 `APP_HOST_NAME`；
  - 其余四张用 `127.0.0.1`，依据分别是 Caddy 的 `tls_server_name`、Host origin、`guard_url` 和投递 origin；
- **私钥属主**：Gateway 为 0，agent-host 为 10020，其余为 10001；
- **DSN**：所有 `*_dsn` secret 都列入，每条带所属服务的 uid（见 2.3）；
- **CA**：`pg_ca`。

- **预期**：一行 JSON，`"ok": true`、`"certificates": 5`、`"dsn_files"` 等于 compose 中 `*_dsn` secret 的个数、`"before": "given"`（或 `"fresh"`）。
- **不符时**：
  - 报 `Compose has no secret: …`：部署的 compose 不是当前版本；
  - 报 `--gate-dir exists`：换一个目录名，不要覆盖。

### 2.2 运行闸门

需要注意三点：
- 镜像用发布的 backend `$BACKEND_IMAGE`（c9b816dd…）。但闸门要用到的 `deploy_checks` 新代码不在这个镜像里：DSN 按服务 uid 读取（2.3），以及 G-CLAIM-EGRESS 的网络项。所以像 run-41 §3a.1 那样挂载 main 的源码树，并让 PYTHONPATH 优先。
- roles 检查要连数据库，所以共享 app-net 的网络命名空间，不用 `--network none`。
- 路径在容器内外保持一致，spec 无需改写。

```
APPNET=$(sudo docker compose -p nexloop-app ps -q app-net)
sudo docker run --rm --user 0 --read-only --network "container:$APPNET" \
  -v "$GATE_DIR:$GATE_DIR:ro" -v "$NEXLOOP_ROOT/secrets:$NEXLOOP_ROOT/secrets:ro" -v "$NEXLOOP_ROOT/ops/metrics:$NEXLOOP_ROOT/ops/metrics:ro" \
  -v "$RELEASE_CHECKOUT/deploy/stage:$RELEASE_CHECKOUT/deploy/stage:ro" -v "$RELEASE_CHECKOUT/packages/eios-core/src:/opt/nexloop-src:ro" \
  -e PYTHONPATH=/opt/nexloop-src --entrypoint nexloop-doctor "$BACKEND_IMAGE" --deploy-gate "$GATE_DIR/spec.json" | sudo tee "$GATE_DIR/gate-result.json"
```

- **通过标准**：
  - `foundation_checks_passed: true`；
  - `checks` 全部为 true，任何 `input_provided:*` 为 false 都算不通过；
  - `product_ready` 为 false，这是预期的。
- **不符时**：记下为 false 的检查名，以及 `details` 里对应的代码（只有名字和代码），报调度员。不要为了通过而修改输入文件。

### 2.3 已知前提与限制

- **DSN 的属主**：闸门以 root 运行，这样才能看到每个服务的目录。但各服务的 DSN 属主是 10001，而原来的读取函数要求“读取进程本人是属主”，以 root 读会全部判失败。本单的提交为 `deploy_checks.tls` 增加了按条目指定 `uid` 的读法：文件必须是该 uid 的、普通文件、权限 0600、长度有限。`gate_dir` 为每条 DSN 写入服务 uid，所以 2.2 必须挂载 main 源码。
- **G-CLAIM-EGRESS**：`compose_networks_match_firewall` 与 `running_networks_match_firewall` 包含 `claim_egress`。所以闸门要在 run-42 §8 上线之后跑；在那之前，这两项会因为缺 `CLAIM_EGRESS_CIDR` 或缺网络而为 false。
- **before.json**：没有首次部署的 before 时，现场生成的 before 与 after 几乎同时取得，`existing_services_unchanged` 只说明“此刻一致”，不说明部署前后一致。证据里注明用的是哪一种。
- **`agent_host_concurrency`**（2026-10-16 stage 实测为 false）：Host 的 `runtime-config.json` 属主是 10020，闸门以 root 运行，原来的读法因此判为 false。现在按 `agent_host_config_uid`（`gate_dir` 写入 10020）读取；读不了时，`details.agent_host_concurrency` 会给出原因码，例如 `file_owner_differs`、`file_permissions`、`configuration_invalid:<类型>`。
- **防火墙比对，`existing_services_unchanged`**（2026-10-16 stage 实测为 `changed:firewall`）：
  - **原因**：原算法对整个 `nft list ruleset` 求哈希，只排除了 `nexloop_stage` 表。而 Docker 会为每个 compose 网络写规则，所以 NexLoop 自己的网络一变（应用网络换子网、新增 claim_egress），哈希就变。
  - **新算法**（`inventory.py` 算法 2）：剔除只涉及 NexLoop 网络的规则，依据是 `com.docker.compose.project` 以 nexloop 开头的网络、它们的网桥 `br-<id12>`、子网和子网内地址；计数器清零。
  - 规范化后的规则文本另存在 inventory 的 `firewall_normalized_rules`，供私有证据逐行比对。
  - **旧的 before**：首次部署时的 `before.json` 只有旧算法的哈希，无法重算。闸门会报 `firewall_before_old_algorithm`，不会直接判为通过。
  - **接受新基线（B，sudo-A）**：如果负责人核对过当前规则，例如 UFW 逐条一致、没有新表、`nexloop_networks` 列出的网桥都属于 NexLoop，可以用旧 before 生成新基线：
    ```
    sudo python3 "$RELEASE_CHECKOUT/scripts/deploy/inventory.py" --phase before --planned-ports <同 2.1> --data-root "$NEXLOOP_ROOT" \
      --rebaseline <旧 before.json> --out <新 before.json>
    ```
    新文件保留旧 before 的其他各节，防火墙改为当前规则，并写入 `firewall_rebaselined`（时间与旧哈希）。然后用 `--before <新 before.json>` 重做 2.1。闸门 details 会标出 `before_rebaselined`。
- **valkey.conf**：检查的是由同一模板和同一 env 渲染的版本，不是 DATA_HOST 上的实际文件。如需核对实际文件，可从 DATA_HOST 取 `config/valkey.conf`，只读、核对 sha256，替换 `$GATE_DIR/valkey.conf` 后重跑 2.2。

### 2.4 收尾

- 证据只取 `gate-result.json` 里的检查名和代码。`$GATE_DIR` 留在 APP_HOST，0700 root：它含有 stage.env 副本，inventory 里也有地址。
- 不再需要时：`sudo shred -u $GATE_DIR/stage.env`，然后 `sudo rm -r $GATE_DIR`。
- **撤销**：闸门只读，没有需要撤销的东西。
