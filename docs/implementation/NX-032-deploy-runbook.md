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
| 部署 dry-run | `scripts/deploy/deploy.py dry-run --host-role <app\|data> --stage-env "$PRIVATE_STAGE_ENV" --out "$PLAN_DIR" --release-dir "$RELEASE_DIR" --allowed-signers "$ALLOWED_SIGNERS" --repo "$LOCAL_REPO" --trusted-ref main [--current-manifest "$CURRENT_MANIFEST" --database-head <head>]` | `{"ready": true, "executed": false, ...}`，并给出渲染结果和步骤列表 | 发布清单验签通过，SHA 与受信 ref 一致，镜像全部来自清单（DATA_HOST 来自锁文件），回滚检查允许；任一项不符时退出码为 1，不进入后续步骤 |
| Compose 预检 | `docker compose -f deploy/stage/compose.<data\|app>.example.yaml --env-file "$PRIVATE_COMPOSE_<DATA\|APP>_ENV" config -q` | 退出码 0 | 镜像都是 `name@sha256:…`，来自 CI 测过的发布清单 |

**撤销**：只需删除 `$RENDER_DIR` 和 `$CERT_DIR`。根 CA 不删除。

## 每一步通用的前后核对

- 执行前：`ssh "$SSH_ALIAS_<HOST>" sudo python3 - --phase before --planned-ports "$PG_PORT,$VALKEY_PORT" --data-root "$NEXLOOP_ROOT" --out /tmp/nexloop-inventory-before.json < scripts/deploy/inventory.py`。只运行只读命令，写出一个 0600 的文件。以 root 运行：`nft list ruleset` 需要 root；以普通账户运行时它什么也不输出，旧版本会把空规则的哈希（`e3b0c442…`）当作结果，导致前后两份都是“空对空”。现在读不到时写 `firewall_unreadable: true`，闸门报 `firewall_unreadable`、不判通过（2026-10-16）。部署之后要重新取参照时，用 `--phase baseline`（计划端口与 nexloop* 条目记为 NexLoop 所有，闸门只比较其余部分）。
  - APP_HOST 的 `--planned-ports` 改为 `$GATEWAY_HTTPS_PORT,$GATEWAY_REDIRECT_PORT,$API_INTERNAL_PORT,$API_INTERNAL_PORT_2,$HOST_INTERNAL_PORT,$GUARD_PORT`。
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

- **镜像载入（启动前，部署主机上）**：
  ```
  scripts/deploy/deploy.py load-check --release-dir "$RELEASE_DIR"
  ```
  - 它会核对每个镜像归档的哈希，执行 `docker load`，再按 `nexloop/<镜像名>@<清单 digest>` 解析，必须得到清单中的 digest；
  - 任一项不符就拒绝（退出码 1），不进入后续步骤；
  - 要求 Docker 使用 containerd 镜像存储，否则退出码为 2（见 `NX-034-part2-implementation.md` §2）。
  - 也可以在 dry-run 时加 `--load-check` 当场核对（只能在部署主机上加，因为它会调用 docker）。
- **命令**：
  ```
  ssh "$SSH_ALIAS_APP" docker compose -p nexloop-app -f "$NEXLOOP_ROOT/compose.app.yaml" --env-file "$NEXLOOP_ROOT/compose.app.env" up -d
  ```
  - 先停旧的 Agent Host 再启动新的；首次部署时没有旧实例。
  - **中继与 Host 的上下文版本必须一致**：stage 的 `message-relay` 以 `--run-profile-file` 与 `--context-strategy recent_plus_required` 启动，产出 Context v6；Agent Host 运行配置的 `context_input_protocol` 为 `nexloop.context-pack.v6`（[`agent-host.runtime-config.example.json`](../../deploy/stage/agent-host.runtime-config.example.json)）。去掉 `--context-strategy` 会退回 v2，与 Host 不一致；stage compose 的测试核对这一点。
  - **relay 进程数**：2 个（`message-relay`、`message-relay-2`，各自连接池 2；2026-10-13 连接预算方案 B，见 [relay 双进程与连接预算](relay2-budget.md)）。每个进程独占自己的 vault 目录，部署前创建 `data/relay-vault` 与 `data/relay-vault-2`（各自 0700，属主为服务用户），不能共用。腾出的 2 个连接来自 API 浏览器 identity 池（每进程 4 → 3，`--identity-pool-max 3`），合计 59/60；部署门禁按 `nexloop.budget-service` 标签核对容器数与预算。
- **预期**：各服务先显示 Started；API 的 `/health/ready` 只报告已核实的能力。
- **撤销**：`ssh "$SSH_ALIAS_APP" docker compose -p nexloop-app down`，数据目录保留。
- **核对**：`--deploy-gate`（host_role 为 app）的全部检查都为 true，包括：
  - `guard_workers_exact`、`running_processes_registered`、`running_guard_children_exact`；
  - `port_bindings`：内部端口只绑 127.0.0.1；
  - `real_dispatch_closed`；
  - `agent_host_concurrency`；
  - 连接预算：relay 容器数与 `connection-budget.v1.json` 声明的进程数一致；
  - TLS 各项；
  - `existing_services_unchanged`。
- **登记为当前发布**（门槛全部通过之后，只在本地发布根目录内操作，不连接服务器）：
  ```
  scripts/deploy/deploy.py promote --release-root "$RELEASE_ROOT" --release-id <release_id> --allowed-signers "$ALLOWED_SIGNERS"
  ```
  - 先核对签名、完整性和封存状态；
  - 再把原来的 `current` 记为 `known_good`，把本发布设为 `current`；
  - 两个符号链接都是先建临时链接再 rename，原子切换；中途任一步失败，都会恢复成原样；
  - 最后写入部署记录（`deployments.json`，`known_good: true`）。
  - `current` 和 `known_good` 指向的发布，无论保留规则怎么配置，都不会被清理（`make prune-releases`）。

## 7. 开放 PG 运维窗口（按次）

- **命令**：
  1. 在 DATA_HOST 的 `pg_hba.conf` 中取消迁移行的注释，然后执行 `docker exec nexloop-data-postgres-1 pg_ctl reload -D "$PGDATA"`；
  2. 在 nft 表中取消 `MGMT_SOURCE_CIDR` 一行的注释，然后重新执行 `nft -f`。
- **预期**：reload 输出 `server signaled`。
- **撤销**（窗口结束后必须执行）：把两行重新注释掉，然后再执行一次 reload 和 `nft -f`。
- **核对**：窗口关闭后，从管理机连接 `$PG_PORT` 被拒或超时（AT-058 的探测之一）。

## 8. 回滚（设计稿 §7）

- **镜像回滚**（Schema 兼容时）：
  1. 先检查回滚目标：
     ```
     scripts/deploy/deploy.py rollback-check --release-root "$RELEASE_ROOT" --target <release_id> [--database-head <库内 head>]
     ```
     回滚目标必须满足全部条件：
     - 是 `known_good`，或比它更早的已封存发布；
     - 镜像归档仍在（已被清理的发布不能回滚：重新构建出的镜像不是当初测试和部署的那个）；
     - 能在当前 schema 上运行（`release.py rollback-check`，AT-057）。
     否则拒绝并提示向前修复。
  2. 通过后，用该发布重新执行 dry-run、`load-check`，渲染 Compose env 后执行 `up -d`。Agent Host 仍要先停旧的再启新的。
  3. 门槛通过后，对该发布执行 `promote`。
- **防火墙**：执行 `nft delete table inet nexloop_stage`。
- **证书**：回到上一组证书文件，然后 reload。
- **整体撤下**：对两份 Compose 都执行 `down`，不加 `-v`；然后用 inventory 对比核对既有服务没有受影响。数据目录保留，不删除。
- **数据还原**：进入 NX-035 的恢复流程。

## AT-058 外部探测（真实部署，D3）

- **命令**（分别在管理机、DATA_HOST、APP_HOST 上运行，各用一份私有 spec）：
  ```
  python3 -I scripts/deploy/probe_isolation.py --spec "$PRIVATE_PROBE_SPEC" --out "$EVIDENCE_DIR/at058-<位置>.json"
  ```
  - 脚本与模拟（`scripts/deploy/netns_sim/`）中使用的是同一个；spec 的格式与模拟生成的 spec 相同。
  - Agent Host 独立命名空间（NX-037 D4）带来的两条负例：从 API 或 Worker 所在的应用命名空间访问模型白名单主机，应被拒；从 Agent Host 访问 DATA_HOST 的 PG 端口或非白名单目标，应被拒。
  - 预期结果见设计稿 §3.3：管理机连 DATA_HOST 不可达；明文、错 CA、SAN 不符、没有客户端证书都被拒；Valkey 明文和 default 用户被拒；内部端口不可达；受信 LAN 能连到 Gateway。
- **预期输出**：`"passed": true`，退出码 0。报告中只有代号和原因代码。
- **撤销**：无（只读探测）。

## 首次部署内容（D9）

- 只放合成租户，`REAL_DISPATCH_ENABLED=false`。
- 跑完 doctor 全套、smoke 和 AT-058 外部探测（切片 4 的探测脚本，从管理机、DATA_HOST、APP_HOST 三处发起）。
- 真实租户和真实渠道属于 NX-037。
