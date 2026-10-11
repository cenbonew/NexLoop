# NX-032 双机部署运行手册（B 类步骤，逐步由负责人确认）

依据：`NX-032-design.md` §8（D1–D9 已定）。本手册只是文档，任何会话都不得自行执行其中的步骤。每一步都由负责人本人执行，或在负责人逐步授权下执行。

- **参数**：命令里出现的都是参数（`$SSH_ALIAS_APP`、`$SSH_ALIAS_DATA`、`$NEXLOOP_ROOT`、`$PG_PORT` 等，见设计稿 §1.2 和 `deploy/stage/stage.env.example`），取值来自负责人管理机上的私有 env 文件。本手册和仓库都不写真实值。
- **执行位置**：
  - “管理机”：负责人的管理机，离线根 CA 也在这里；
  - DATA_HOST、APP_HOST：通过管理机上私有 ssh config 中的别名登录。
- **记录**：每一步执行前后的 inventory 和 doctor 输出保存在管理机的私有目录（`$EVIDENCE_DIR`）中，不进仓库。

## 0. 准备（只在管理机上，不改动服务器）

| 项 | 命令 | 预期输出 | 核对 |
|---|---|---|---|
| 校验参数 | `scripts/deploy/render.py --env "$PRIVATE_STAGE_ENV" --check` | `{"valid": true, "parameters": [...]}` | 只列出参数名；任何未知键或格式不符都会被拒绝 |
| 渲染配置 | `scripts/deploy/render.py --env "$PRIVATE_STAGE_ENV" --template deploy/stage/config/<f>.template --out "$RENDER_DIR/<f>"`，对 `pg_hba.conf`、`postgresql.conf`、`valkey.conf`、`data-host.nft`、`app-host.nft` 各执行一次 | 每个文件输出 `{"rendered": true, ...}` | 输出中不能残留 `__X__`；`pg_hba.conf` 最后四行都是 reject |
| 根 CA（仅首次） | `scripts/deploy/issue_certs.py root --dir "$OFFLINE_ROOT_CA_DIR"` | `{"issued": "root", ...}` | `ca.key` 为 0600，只存在管理机上 |
| 中间 CA（每年） | `scripts/deploy/issue_certs.py intermediate --root-dir "$OFFLINE_ROOT_CA_DIR" --dir "$CA_DIR"` | `{"issued": "intermediate", ...}` | 有效期 ≤365 天 |
| 服务证书 | `issue_certs.py server --ca-dir "$CA_DIR" --out "$CERT_DIR" --name pg-server --san "DNS:$DATA_HOST_NAME" --san "IP:$DATA_HOST_ADDR"`；`valkey-server` 同样；`gateway` 用 APP_HOST 的名称和地址 | `{"issued": "server", ...}` | 有效期 ≤90 天（D1） |
| 客户端证书 | 每个 PG 应用角色一张：`issue_certs.py client --ca-dir "$CA_DIR" --out "$CERT_DIR" --name pg-client-<role> --cn <role>`；Valkey 每个 ACL 用户一张 | 同上 | CN 等于角色名或 ACL 用户名（D2） |
| Compose 预检 | `docker compose -f deploy/stage/compose.<data\|app>.example.yaml --env-file "$PRIVATE_COMPOSE_<DATA\|APP>_ENV" config -q` | 退出码 0 | 镜像都是 `name@sha256:…`，来自 CI 测过的发布清单 |

**撤销**：只需删除 `$RENDER_DIR` 和 `$CERT_DIR`。根 CA 不删除。

## 每一步通用的前后核对

- 执行前：`ssh "$SSH_ALIAS_<HOST>" python3 - --phase before --planned-ports "$PG_PORT,$VALKEY_PORT" --data-root "$NEXLOOP_ROOT" --out /tmp/nexloop-inventory-before.json < scripts/deploy/inventory.py`。只运行只读命令，写出一个 0600 的文件。
  - APP_HOST 的 `--planned-ports` 改为 `$GATEWAY_HTTPS_PORT,$GATEWAY_REDIRECT_PORT,$API_INTERNAL_PORT,$HOST_INTERNAL_PORT,$GUARD_PORT`。
- 执行后：同一命令，改用 `--phase after` 和另一个输出文件。
- 核对：`nexloop-doctor --deploy-gate <spec>` 中的 `existing_services_unchanged` 必须为 true，即只出现 `nexloop` 前缀的新增项，既有项一项都没变。

## 1. 新建目录和服务用户（两台主机）

- **命令**：
  ```
  ssh "$SSH_ALIAS_DATA" sudo install -d -m 0750 "$NEXLOOP_ROOT" "$NEXLOOP_ROOT/config" "$NEXLOOP_ROOT/data"
  ssh "$SSH_ALIAS_DATA" sudo install -d -m 0700 "$NEXLOOP_ROOT/secrets" "$NEXLOOP_ROOT/secrets/postgres" "$NEXLOOP_ROOT/secrets/valkey"
  ```
  - APP_HOST：同样建立 `config`、`data/artifacts`、`data/runtime` 和 `secrets/<service>`。每个服务一个目录，属主为该服务的 UID（`sudo chown <uid>:<uid>`）。
  - 服务用户（如 `$AGENT_HOST_UID`、`$CI_UID`）使用 `sudo useradd --system --uid <uid> --no-create-home --shell /usr/sbin/nologin <name>`。
- **预期**：命令无输出，退出码 0。
- **撤销**：`sudo rm -r "$NEXLOOP_ROOT"`。此时目录是空的，删除前先 `ls -A` 确认里面只有本步建立的目录。删除用户用 `sudo userdel <name>`。
- **核对**：inventory 对比只多出 `nexloop:` 数据目录；`stat -c '%a %U' "$NEXLOOP_ROOT/secrets"/*` 都是 700，属主是对应服务。

## 2. 安装证书和秘密文件

- **命令**：
  ```
  scp "$CERT_DIR/pg-server.chain.crt" "$SSH_ALIAS_DATA:$NEXLOOP_ROOT/secrets/postgres/server.crt"
  scp "$CERT_DIR/pg-server.key" "$SSH_ALIAS_DATA:$NEXLOOP_ROOT/secrets/postgres/server.key"
  ssh "$SSH_ALIAS_DATA" sudo chmod 0600 "$NEXLOOP_ROOT/secrets/postgres/server.key"
  ssh "$SSH_ALIAS_DATA" sudo chown <pg-container-uid> "$NEXLOOP_ROOT/secrets/postgres/server.key"
  ```
  - Valkey、Gateway 和各客户端证书同样处理，每个文件只放进它所属服务的目录。
  - DSN 文件的内容是完整的 `verify-full` 连接串，权限 0600。
- **预期**：scp 只显示文件名。
- **撤销**：删除本步放入的文件，同时回到上一组证书文件，然后执行 `pg_ctl reload` 或重启对应服务（设计稿 §7）。
- **核对**：
  - `nexloop-doctor --tls-spec <私有 spec>`：`tls_certificates`、`tls_dsn_verify_full`、`tls_valkey_only` 都为 true；
  - `--deploy-gate` 中的 `secret_distribution` 为 true；
  - Agent Host 目录里没有 DSN、凭据或签名密钥。

## 3. 改防火墙（DATA_HOST、APP_HOST）

- **命令**：
  ```
  scp "$RENDER_DIR/data-host.nft" "$SSH_ALIAS_DATA:/tmp/nexloop-stage.nft"
  ssh "$SSH_ALIAS_DATA" sudo nft -c -f /tmp/nexloop-stage.nft
  ssh "$SSH_ALIAS_DATA" sudo nft -f /tmp/nexloop-stage.nft
  ```
  - 第二条命令只做语法检查，第三条才加载。APP_HOST 用 `app-host.nft`，步骤相同。
- **预期**：`nft -c` 没有输出；加载后 `sudo nft list table inet nexloop_stage` 显示规则。
- **撤销**：`ssh "$SSH_ALIAS_<HOST>" sudo nft delete table inet nexloop_stage`。新规则在独立的表里，现有 UFW 和 Docker 规则都不动。
- **核对**：inventory 对比中，去掉 NexLoop 表之后规则集的 hash 不变（`changed:firewall` 不应出现）。SSH 仍可登录：保持当前会话不断开，另开一个会话验证。

## 4. 启动 infra（DATA_HOST）

- **命令**：
  ```
  ssh "$SSH_ALIAS_DATA" docker compose -p nexloop-data -f "$NEXLOOP_ROOT/compose.data.yaml" --env-file "$NEXLOOP_ROOT/compose.data.env" up -d
  ```
  - Compose 文件和 env 文件已在第 1、2 步放好。
- **预期**：`nexloop-data-postgres-1` 和 `nexloop-data-valkey-1` 先显示 Started，随后变为 healthy。
- **撤销**：`ssh "$SSH_ALIAS_DATA" docker compose -p nexloop-data down`。不加 `-v`：数据目录保留，不删除。
- **核对**：
  - `--deploy-gate`（host_role 为 data）：`port_bindings` 为 true，即 PG 和 Valkey 只绑在 `$DATA_HOST_ADDR` 上；`planned_ports_free_before` 为 true；`existing_services_unchanged` 为 true；
  - 从 APP_HOST 用 `sslmode=disable` 连接 PG 被拒（`hostnossl … reject`）。

## 5. bootstrap 与迁移

- **前置**：最小可验证备份（D5）。对空库也照做：
  ```
  pg_dump -Fc
  ```
  - 导出后恢复到一次性隔离实例，并核对 catalog 和行数。NX-035 的 `nexloop-migrate` 门槛（AT-056）落地后，改由它强制。
- **命令**：
  1. 按第 7 步开放运维窗口；
  2. 用迁移身份执行受审核的 catalog（bootstrap 或 `nexloop-migrate --database-url-file "$PRIVATE_MIGRATION_DSN"`）；
  3. 关闭窗口；
  4. `ALTER ROLE nexloop_bootstrap NOLOGIN`。
- **预期**：`nexloop-migrate` 输出 `{"migrated": true, "applied": [...]}`。门槛拒绝时，输出拒绝原因，退出码为 1，不会执行任何迁移。
- **撤销**：不运行 down 迁移。需要回到迁移之前的状态时，进入灾难恢复（NX-035，见第 8 步）。
- **核对**：`--deploy-gate` 中的 `application_roles_unprivileged` 和 `bootstrap_identities_disabled` 都为 true；`bootstrap.verify()` 显示 lineage 完全一致。

## 6. 启动应用（APP_HOST）

- **命令**：
  ```
  ssh "$SSH_ALIAS_APP" docker compose -p nexloop-app -f "$NEXLOOP_ROOT/compose.app.yaml" --env-file "$NEXLOOP_ROOT/compose.app.env" up -d
  ```
  - 先停旧的 Agent Host 再启动新的；首次部署时没有旧实例。
- **预期**：各服务先显示 Started；API 的 `/health/ready` 只报告已核实的能力。
- **撤销**：`ssh "$SSH_ALIAS_APP" docker compose -p nexloop-app down`，数据目录保留。
- **核对**：`--deploy-gate`（host_role 为 app）的全部检查都为 true，包括：
  - `guard_workers_exact`、`running_processes_registered`、`running_guard_children_exact`；
  - `port_bindings`：内部端口只绑 127.0.0.1；
  - `real_dispatch_closed`；
  - `agent_host_concurrency`；
  - TLS 各项；
  - `existing_services_unchanged`。

## 7. 开放 PG 运维窗口（按次）

- **命令**：
  1. 在 DATA_HOST 的 `pg_hba.conf` 中取消迁移行的注释，然后执行 `docker exec nexloop-data-postgres-1 pg_ctl reload -D "$PGDATA"`；
  2. 在 nft 表中取消 `MGMT_SOURCE_CIDR` 一行的注释，然后重新执行 `nft -f`。
- **预期**：reload 输出 `server signaled`。
- **撤销**（窗口结束后必须执行）：把两行重新注释掉，然后再执行一次 reload 和 `nft -f`。
- **核对**：窗口关闭后，从管理机连接 `$PG_PORT` 被拒或超时（AT-058 的探测之一）。

## 8. 回滚（设计稿 §7）

- **镜像回滚**（Schema 兼容时）：按上一份已知可用的发布清单重新渲染 Compose env，然后 `up -d`。Agent Host 仍要先停旧的再启新的。
  - 发布清单中的 catalog 最高版本低于库内版本时，`release.py rollback-check` 会拒绝回滚（AT-057），改为向前修复。
- **防火墙**：执行 `nft delete table inet nexloop_stage`。
- **证书**：回到上一组证书文件，然后 reload。
- **整体撤下**：对两份 Compose 都执行 `down`，不加 `-v`；然后用 inventory 对比核对既有服务没有受影响。数据目录保留，不删除。
- **数据还原**：进入 NX-035 的恢复流程。

## 首次部署内容（D9）

- 只放合成租户，`REAL_DISPATCH_ENABLED=false`。
- 跑完 doctor 全套、smoke 和 AT-058 外部探测（切片 4 的探测脚本，从管理机、DATA_HOST、APP_HOST 三处发起）。
- 真实租户和真实渠道属于 NX-037。
