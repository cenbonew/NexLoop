# deploy/stage/systemd：主机侧 unit 示例

这里都是示例。把文件复制到 `/etc/systemd/system/` 并去掉 `.example` 后缀，然后执行 `systemctl daemon-reload` 和 `systemctl enable --now <timer>`。这些都属于 B 类，由负责人逐条确认。

unit 文件里不写任何秘密：私有值都是 `EnvironmentFile=` 中的路径。环境文件为 0600，属主 root，属组为执行身份。`RequiresMountsFor=` 写的是 NX-035 第 1 步建立的 LUKS 挂载点，卷未挂载时 unit 不会启动。

示例里的路径（`/srv/nexloop/...`、`/srv/nexloop-backup/...`、`/opt/nexloop/backup-venv`）只是文档用值，必须与负责人私有 env 中的挂载点和安装位置一致。

## 备份（NX-035 部署运行手册第 5.1 步，计划与 `deploy/configuration/backup.v1.json` 一致）

| unit | 主机 | 身份 | 计划 | 内容 |
|---|---|---|---|---|
| `nexloop-pg-full` | DATA_HOST | `nexloop-backup` | 每周日 02:00 | `nexloop-backup pg --kind pg_full`，先 repo1 后 repo2 |
| `nexloop-pg-diff` | DATA_HOST | `nexloop-backup` | 周一至周六 02:00 | `nexloop-backup pg --kind pg_diff`，先 repo1 后 repo2 |
| `nexloop-journal` | DATA_HOST | `nexloop-backup` | 每分钟 | `nexloop-backup journal` |
| `nexloop-journal-restic@repo1` / `@repo2` | DATA_HOST | `nexloop-backup` | repo1 每 5 分钟，repo2 每 15 分钟 | 恢复日志目录 → restic；`/etc/nexloop/restic-<实例>.env` 中放 `RESTIC_REPOSITORY_FILE` 与 `RESTIC_PASSWORD_FILE`（都是路径，restic 自己读取），repo2 另放 AWS 键 |
| `nexloop-restic-maint` | DATA_HOST | `nexloop-backup` | 每天 03:30 | `forget --keep-within 14d --keep-monthly 1 --prune` 与 `check`；周日加做 `check --read-data-subset 1/10` |
| `nexloop-file-backup` | APP_HOST | `nexloop-file-backup` | 每 15 分钟 | `nexloop-file-backup --kind artifact` 与 `--kind runtime`，repo1 与 repo2 各一次。**10/15–16 不启用（M12）**：这两天只备份恢复日志；启用前须解决 recorder 的连接位置与读权限（[文件备份说明](../../../docs/implementation/NX-035-file-backup.md) §5） |

- **执行者**：`nexloop-backup` 与 `nexloop-file-backup` 来自只给备份身份用的 Python 虚拟环境，从发布 commit 的源码安装 `packages/eios-core`，安装的版本记入证据（M13/M14）。
- **pgBackRest**：在 `nexloop/postgres` 镜像内运行，命令前缀写在 `pgbackrest-command.json` 中（`docker exec … pgbackrest`）。因此 `nexloop-backup` 身份需要能访问 docker；docker 组等同 root 权限，这是已知风险，要在证据中记录。

## pgBackRest TLS 服务器（M8，负责人定案：TLS 服务器模式）

| 文件 | 内容 |
|---|---|
| `nexloop-pgbackrest-server.service.example` | APP_HOST 上以 `nexloop-pgbackrest`（repo1 属主）运行 `pgbackrest server`；pgBackRest 为 PGDG 2.59.3 的 deb（已验签，见 `versions.lock.json`） |
| `pgbackrest-server.conf.example` | `tls-server-address` 为 APP_HOST 地址，端口为 `PGBACKREST_TLS_PORT`；证书由 NexLoop CA 签发；`tls-server-auth` 只放行 DATA_HOST 客户端证书的 CN（`pgbackrest-data-client`），只允许访问 stanza `nexloop` |

防火墙：`deploy/stage/firewall/app-host.nft.template` 的 input 链只接受来自 DATA_HOST、目标为 `__PGBACKREST_TLS_PORT__` 的连接，其他来源访问这个端口一律丢弃。新参数 `PGBACKREST_TLS_PORT` 已加入 `stage.env.example` 和渲染白名单。

DATA_HOST 容器内的 `pgbackrest.conf` 相应写：

- `repo1-host-type=tls`、`repo1-host`、`repo1-host-port`；
- `repo1-host-cert-file`、`repo1-host-key-file`：客户端证书的 CN 为 `pgbackrest-data-client`；
- `repo1-host-ca-file`。

`repo1-cipher-*` 仍由客户端设置，服务器端不持有仓库口令。

**撤销**：`systemctl disable --now <unit>`，删除 unit 文件，再执行 `systemctl daemon-reload`。仓库内容不动。

## 防火墙表持久化（G-NFT-PERSIST）

| 文件 | 内容 |
|---|---|
| `nexloop-nft.service.example` | 两台主机各一份。oneshot，在 `docker.service` 之前从根文件系统加载 `inet nexloop_stage`，并且是 `docker.service` 的 RequiredBy：加载失败时 Docker 不启动。`reload` 原子替换这张表，`stop` 只删除这张表 |

- **加载文件**：`render.py --nft-transaction <渲染后的规则> --out <host>.nft` 生成。它是一个事务：`table`、`delete table`、`include`。
- **文件位置**：两个文件都在 `$NEXLOOP_ROOT/config/firewall/`，root 0600，不放在 LUKS 卷上。
- **不得同时启用** `nftables.service`：它默认的配置文件以 `flush ruleset` 开头。
- 安装与核对见 [run-42 §6a](../../../docs/release/run-42-20261015.md)。
