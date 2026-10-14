# 部署日执行单核对（2026-10-15/16）

对照对象：
- 执行单 [deploy-days-20261015.md](deploy-days-20261015.md)；
- main `e8ccc7f` 上的脚本与示例；
- 首个签名发布 `20261014-241993b9cc12-1`（commit `241993b…`，4 个镜像：agent-host、backend、gateway、postgres）。

核对方式：只读代码。没有连接任何服务器。

本分支 `l2/deploy-days-fix` 已修正示例模板与执行单中可以直接改的部分（见各条“处理”）。其余需要负责人或调度员先做决定。

## 一、必须改（不改明天会卡住）

| # | 位置 | 问题 | 处理 |
|---|---|---|---|
| M1 | 1.2、首个发布手册 §4 第 1–2 步 | dry-run 用 `--repo --trusted-ref refs/heads/main` 解析受信 SHA。main 已经前进到 `e8ccc7f`，而发布的 commit 是 `241993b…`，`release.verify` 会报 `deploy_sha_differs_from_tested_commit`，`ready: false` | 改为 `--trusted-sha <发布 commit 的完整 40 位>`，取自 `manifest.json` 的 `commit`，并核对它是 main 上的提交。执行单与首个发布手册都已修改 |
| M2 | P2、1.2、2.2 | `<PG_PGBACKREST_IMAGE>` 已经确定为 `nexloop/postgres`。data 的 dry-run 从签名发布取它：必须带 `--release-dir`，并按发布 digest 写出 `POSTGRES_IMAGE=nexloop/postgres@<digest>`；valkey 仍取锁文件 | 执行单已改名，1.2 的 data dry-run 命令已补全 |
| M3 | 1.5、2.2 | `nexloop/postgres` 以 `USER postgres`（uid 999）启动，入口脚本不能 chown。需要预先满足：`data/postgres`、`run/postgresql`、`secrets/pgbackrest` 属于 999；`secrets/postgres/server.key` 属于 999 且为 0600；引导口令的 secret 文件对 999 可读。否则 PG 起不来 | 执行单 1.5 已加入这些要求，并在 2.2 判据中加了核对 |
| M4 | 1.1 | 执行单说“渲染时写入归档三项”，但 `postgresql.conf` 模板原本没有这三项，渲染器只做白名单替换，加不进去 | 模板已加入 `archive_mode`、`archive_command`（pgBackRest，stanza `nexloop`）、`archive_timeout` 和 socket 目录。stanza 建立之前 archive-push 会失败、WAL 会累积，所以 2.2 启动后要尽快做 2.3 的 `stanza-create` |
| M5 | 2.2–2.4 | compose.data 原本没有挂载 pgBackRest 配置；容器里的 pgBackRest 连库没有口令来源 | compose.data 示例已加入：挂载 `secrets/pgbackrest/pgbackrest.conf` 和 `secrets/pgbackrest/pgpass`，并设置 `PGPASSFILE`（`archive_command` 与 `docker exec` 都继承它） |
| M6 | 2.4、2.6 及之后 | pg_hba 只允许 `nexloop_bootstrap` 走本机 socket，`nexloop_backup` 和 `nexloop_backup_recorder` 没有规则，pgBackRest 的 `backup`、`check` 和执行者写清单都会被拒。这两个角色由 bootstrap 建立，而且**没有口令** | 模板已加入两条 `local … scram-sha-256`。执行单新增 2.5b：在 bootstrap 的窗口内由负责人用 `\password` 给这两个角色设口令，再写 `pgpass` 和 recorder DSN 文件。2.4 时 `pgpass` 中只放 `nexloop_bootstrap` 一行 |
| M7 | 2.6 → 执行者连库 | 执行者 `nexloop-backup` 在 DATA_HOST 主机上运行，而 PG 的端口只对 APP_HOST 开放（防火墙加 pg_hba），主机进程连不上 | compose.data 示例已把 socket 目录挂到 `$NEXLOOP_ROOT/run/postgresql`；recorder DSN 用 `host=$NEXLOOP_ROOT/run/postgresql`（设计稿 §4.1 的本机 socket） |
| M8 | 2.3 | 执行单推荐 TLS 服务器模式，但 APP_HOST 上 `pgbackrest server` 的 unit、端口、证书，以及 DATA_HOST 到 APP_HOST 该端口的防火墙规则，都不在任何模板或手册里。SSH 模式又要求容器内有 ssh 客户端，而镜像是 `--no-install-recommends` 安装的，未核实有没有 | **需要决定**。建议今晚先跑预演 P-6 核实容器内有没有 `ssh`。用 TLS 模式时，要补：APP_HOST 的 `pgbackrest server` systemd unit（以 `$PGBACKREST_REPO_USER` 运行，`tls-server-address` 为 APP_HOST 地址，`tls-server-auth` 只放行 DATA_HOST 的证书 CN），以及 `app-host.nft` 中 `ip saddr <DATA_HOST_ADDR> tcp dport <PGBACKREST_TLS_PORT> accept` 一条。这些都是 B 类 |
| M9 | 2.5 → 3.2 | 2.5 执行 `ALTER ROLE nexloop_bootstrap NOLOGIN`。但 `nexloop_bootstrap` 是唯一的超级用户，也是迁移身份（`MIGRATION_PG_ROLE`），NX-032 §7 的运维窗口又不会恢复 LOGIN。设为 NOLOGIN 之后，周五 3.2 需要迁移时没有任何身份能登录，只能停库进单用户模式 | **需要决定**。建议这两天先不执行 NOLOGIN：bootstrap 身份本来就只能经容器内 socket 和窗口规则登录。等 3.2 完成后再设 NOLOGIN，并在 NX-032 §7 写明以后如何恢复（单用户模式，属于停机操作）。执行单已在 2.5 和 3.2 标注 |
| M10 | 2.6 判据 | `select authz.nexloop_migration_gate()` 没有授权给任何应用角色。2.5 之后能执行它的只有 `nexloop_bootstrap`（窗口内）。按执行单原文这一步做不了 | 判据改为 `doctor --backup` 的各项（见 M11）。门槛本身由 3.2 的 `nexloop-migrate` 在窗口内判断，输出 `migrated` 或拒绝原因 |
| M11 | 2.6、3.1、3.3、4.1 | `doctor --backup` 的检查名写错了。代码里实际是 `backup_manifest_present`、`backup_recent`、`full_backup_recent`、`required_repos_recent`、`latest_backup_verified`、`wal_archiving`、`file_backup_recent`、`journal_export`、`restore_drill_recent`、`restore_state_open`、`deletion_replay_closed`。执行单写的 `backup_age`、`wal_archive_lag`、`offsite_backup_age`、`restore_journal_lag`、`file_backup_age`、`restore_drill_overdue` 都不存在 | 执行单已改为真实名称，并写明周四只看前 6 项，文件、日志和演练三项要到周五对应步骤之后才能通过 |
| M12 | 3.1 | 文件备份执行者在 APP_HOST 运行，但 recorder 只能经 DATA_HOST 本机 socket 连接。文件备份说明 §5 第 1 项（recorder 从哪里连接）还写着“待定”，按现有模板这一步会被拒 | **需要决定**。两种做法二选一：(a) pg_hba 加一条 `hostssl … nexloop_backup_recorder <APP_HOST_ADDR>/32 scram-sha-256 clientcert=verify-full`，并给 recorder 签发客户端证书；(b) 这两天只做恢复日志的备份，Artifact 和 Run 目录推迟到下一次（这两天里它们都是空的）。建议 (b) |
| M13 | 3.3 | `nexloop-restore` 要求 nexloop 的 Python 包、PG 18 二进制（`--pg-bin`）和 pgBackRest 在同一环境中。`nexloop/postgres` 镜像里没有 nexloop 的 Python 包，backend 镜像里又没有 PG 二进制和 pgBackRest。执行单写的“同一 PG 镜像、`--network none`”跑不起来 | **需要决定**演练在哪里运行。建议放在 APP_HOST 的演练卷上，用主机 Python 虚拟环境（从发布 commit 的源码安装 nexloop-eios-core）加上主机安装的 PG 18 与锁定版本的 pgBackRest。或者把演练推迟一天 |
| M14 | 2.6、3.1、4.4 | 执行单没有写主机上的执行者（`nexloop-backup`、`nexloop-file-backup`）怎么安装。它们是 Python 命令，不在任何镜像的可执行入口里 | 与 M13 一起决定。建议在 DATA_HOST 和 APP_HOST 各建一个只给备份身份用的虚拟环境，从发布 commit 的源码安装 `packages/eios-core`，版本记入证据 |

## 二、建议改

| # | 位置 | 建议 |
|---|---|---|
| S1 | 4.4 | 执行单与手册 5.1 只有单元名和计划，没有 unit 示例。建议周五上午之前在 `deploy/stage/systemd/` 补齐 6 组 service 加 timer 的示例，写好 `User=`、`RequiresMountsFor=`、`EnvironmentFile=` |
| S2 | P3 | pgBackRest 已经随 `nexloop/postgres` 镜像提供，DATA_HOST 不需要单独安装包；APP_HOST 的 repo1 仍然要装同一版本 2.59.3。P3 要改写成：APP_HOST 的 pgBackRest 包与 restic 包，sha256 核对后回填锁文件 |
| S3 | `compose.data.env.example` | `VALKEY_IMAGE` 的示例写成 `registry.invalid/nexloop/valkey@…`，容易让人以为它来自发布。实际取值是 dry-run 写出的 `docker.io/valkey/valkey@<锁文件 digest>` |
| S4 | 2.4 | 第一次备份用 `nexloop_bootstrap` 作为 `pg1-user`，口令放在 `pgpass` 中。2.5b 写入 `nexloop_backup` 的行时，同时删掉 `nexloop_bootstrap` 那一行 |
| S5 | 1.2 | 预检时加 `--load-check`（只在有 containerd docker 的部署主机上），确认 4 个镜像按 digest 解析一致 |
| S6 | 4.1 | 建议把“relay 产出 v6 上下文”改成可以核对的判据：`runtime.nexloop_context_artifact_bindings` 中最近的 `pack_text` 的 `schema_version` 为 `nexloop.context-pack.v6`，用 metrics 只读角色或 doctor 查 |

## 三、只读预演命令（开发机或 CI 主机；尖括号由调度员替换）

- `<RID>` = `20261014-241993b9cc12-1`；
- `<RELEASE_COMMIT>` 取 `jq -r .commit <RELEASE_DIR>/manifest.json`；
- `<RELEASE_DIR>` = `<RELEASE_ROOT>/releases/<RID>`。

**P-1 发布 commit 在 main 上**（M1）

```bash
git -C <TRUSTED_REPO> merge-base --is-ancestor "$(jq -r .commit <RELEASE_DIR>/manifest.json)" origin/main && echo on-main
```

**P-2 app 的 dry-run**（预期：`ready: true`；4 项 checks 都为 true；`compose.images.env` 中有 3 个镜像变量）

```bash
python3 scripts/deploy/deploy.py dry-run --host-role app --stage-env <PRIVATE_STAGE_ENV> --out <新的空目录>/plan-app --release-dir <RELEASE_DIR> --allowed-signers <ALLOWED_SIGNERS> --trusted-sha <RELEASE_COMMIT>
```

**P-3 data 的 dry-run**（预期：`ready: true`；`compose.images.env` 只有两行，其中 `POSTGRES_IMAGE=nexloop/postgres@<发布 digest>`，`VALKEY_IMAGE=docker.io/valkey/valkey@<锁文件 digest>`）

```bash
python3 scripts/deploy/deploy.py dry-run --host-role data --stage-env <PRIVATE_STAGE_ENV> --out <新的空目录>/plan-data --release-dir <RELEASE_DIR> --allowed-signers <ALLOWED_SIGNERS> --trusted-sha <RELEASE_COMMIT>
```

**P-4 渲染检查**（预期：`valid: true`；输出中没有 `__X__` 残留）

```bash
python3 scripts/deploy/render.py --env <PRIVATE_STAGE_ENV> --check
for t in pg_hba.conf postgresql.conf valkey.conf; do python3 scripts/deploy/render.py --env <PRIVATE_STAGE_ENV> --template deploy/stage/config/$t.template --out <新的空目录>/render/$t; done
grep -c '__' <新的空目录>/render/* | grep -v ':0$' || echo no-placeholders
```

**P-5 Compose 预检**（预期：退出码都为 0）。私有 compose env 由负责人拼好，镜像变量取 P-2、P-3 的输出：

```bash
docker compose -f deploy/stage/compose.app.example.yaml --env-file <PRIVATE_COMPOSE_APP_ENV> --env-file <新的空目录>/plan-app/compose.images.env config -q
docker compose -f deploy/stage/compose.data.example.yaml --env-file <PRIVATE_COMPOSE_DATA_ENV> --env-file <新的空目录>/plan-data/compose.images.env config -q
docker compose -f deploy/stage/compose.app.example.yaml --env-file <PRIVATE_COMPOSE_APP_ENV> --env-file <新的空目录>/plan-app/compose.images.env config | grep -c 'context-strategy'
```

最后一条预期输出 `2`，即两个 relay 都带有 `--context-strategy`。

**P-6 镜像内容核对**（CI 主机，镜像已经按 digest 载入）。预期第一行为 `pgBackRest 2.59.3`，第二行为 `999`。第三行用来判断 M8：有输出，说明容器内有 ssh 客户端，SSH 模式可以考虑；没有输出，就只能用 TLS 模式：

```bash
docker run --rm --network none --entrypoint pgbackrest nexloop/postgres@<发布中 postgres 的 digest> version
docker run --rm --network none --entrypoint id nexloop/postgres@<同上> -u
docker run --rm --network none --entrypoint sh nexloop/postgres@<同上> -c 'command -v ssh || true'
```

**P-7 封存的发布**（预期 `ok: true`）

```bash
python3 scripts/release/archive.py check --root <RELEASE_ROOT> --release-id <RID> --allowed-signers <ALLOWED_SIGNERS>
```

## 四、依赖今天新变化、执行单原来没写的地方

| 变化 | 执行单原状 | 处理 |
|---|---|---|
| `nexloop/postgres` 与 `POSTGRES_IMAGE` | 写的是占位名 `<PG_PGBACKREST_IMAGE>`，data dry-run 没带发布参数 | M2：已修正 |
| `USER postgres`（uid 999） | 没有提 | M3：已修正 |
| relay 2 副本 | 1.5 已有 `relay-vault-2`；1.2 已核对 Compose | 无需改动。P-5 第三条可以直接核对 |
| Context v6 | 硬约束第 3 条已有 | S6：建议补一条可核对的判据 |
| 参考端点项目 | 4.3 已有 | 无需改动 |
| 两次备份夹着 bootstrap | 2.4–2.6 已排好 | 这个顺序要成立，还依赖 M5、M6、M7（连库方式）、M9（NOLOGIN）、M10（门槛判据），都已列出 |
