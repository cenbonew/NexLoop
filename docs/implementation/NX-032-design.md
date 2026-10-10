# NX-032 双机 TLS 与隔离部署：设计稿（待审核；未实现，未执行任何部署）

分支 `nx032-design`，从 main `700f4b2` 切出。本稿只有文档：没有代码、配置、迁移，也没有连接或修改任何主机。

- 所有主机、地址、端口、用户、路径都写成可配置参数，见 §1.2。真实映射只存在于私有附件中，本稿不引用其中任何值。
- **服务器上的业务部署和持久库迁移属于 B 类操作，必须由负责人本人确认后才能执行**；本稿只给方案和检查清单。
- NX-032 依赖 NX-031、NX-004、NX-049。

## 0. 依据与结论

**依据**（行号以 main `700f4b2` 为准）：
- 任务 NX-032（S5）：交付 DATA_HOST 承载数据、APP_HOST 承载应用，实际验证端口与角色，不修改任何既有服务。
- `11_DEPLOYMENT.md`：
  - §2 双机放置；
  - §3 端口规划，以及“Docker 发布端口可能绕过 UFW，必须结合 DOCKER-USER/nftables 规则和外部连通性测试验证”；
  - §4 TLS，PG 使用 hostssl、SCRAM、`sslmode=verify-full`，Valkey 使用 TLS 加独立 ACL 用户，不得因证书配置失败而静默关闭校验；
  - §5 目录；
  - §6 分权角色与连接预算 ≤60；
  - §8 机密以 `_FILE` 方式挂载，镜像按 digest 引用；
  - §9 部署顺序 D0–D5；
  - §11 本方案不是高可用。
- `12_LOCAL_CI_AND_RELEASE.md`：
  - §3–5：Package 阶段产出 image archive、digest 和 manifest；发布前 drain 并做备份；Agent Host 先停旧的再启新的；运行中的 SHA 必须与通过测试的 SHA 一致；
  - §6 回滚：不盲跑 down 迁移，存在 Schema 变化时向前修复。
- `13_SECURITY…` §2：外部渠道凭据只留在 Action Worker；模型 Provider 凭据只留在 Agent Host 或受控配置；数据库管理员凭据只用于 bootstrap 和迁移。§9：CI 不得接触 service secret 和 DATA_HOST 网络。
- `docs/handoff/deploy/README.md` 与 `examples/`：infra 和 app 两份 Compose 模板、pg_hba/valkey/Caddy 模板、白名单渲染、未渲染的占位符阻止启动。
- 已实现：
  - ADR-024：guard 4 进程，部署配置为 4；
  - NX-049：Host 全局并发 ≤4，doctor `--agent-host-config`；连接预算 doctor `--budget-only`，合计 ≤60、`max_connections` ≥80（`deploy/stage/README.md`）；
  - Host 与 Worker、API 与 Host 之间是 loopback HTTPS：私有 CA，证书含 127.0.0.1 SAN，Host 和 guard 的 transport key 分开（`runtime-worker-operation.md`、`host-control-progress.md`）。
- 尚未合入、但部署前检查要用到的：
  - NX-030 的 `doctor --observability`（其实现说明列为下一项）；
  - NX-031 切片 0：后台服务登记进连接预算（NX-031 设计稿 §4.3、D7）。

**结论**：
1. **两台主机各跑一份 Compose，不共享 Docker network。** 跨机流量只有两条：APP_HOST 到 DATA_HOST 的 PostgreSQL 和 Valkey，都是 TLS 加双向认证（D2）。其余内部端口只绑 loopback，或只在 APP_HOST 的 Compose 内部网络里。
2. **证书由离线内部 CA 签发**（D1），按固定周期轮换；doctor 在证书到期前 30 天报警。`sslmode=verify-full` 不可关闭。
3. **每个服务使用独立的 PG 角色和独立的凭据文件**；Agent Host 只有自己的 TLS、transport key 和模型 key（启用真实模型之后），没有任何 DB、渠道或 Artifact 秘密。
4. **部署由发布清单驱动**：清单里的镜像 digest、迁移 catalog digest 和测试报告 digest 必须与 CI 测过的一致，否则拒绝部署。NX-032 先实现最小清单校验（D4），归档、签名和同步留给 NX-034。
5. **部署前 doctor 一次性跑完全部前置检查**，任一项不通过就不启动：
   - ADR-022 §4：Host 并发 ≤4，guard 4 进程；
   - 连接预算 ≤60；
   - TLS；
   - 端口暴露；
   - 发布清单；
   - NX-030 的 `--observability`。
6. **隔离要有外部证据**：从非 APP_HOST 的位置做负向探测（AT-058）；部署前后的只读 inventory 对比，证明没有修改任何既有服务。
7. **回滚只回滚镜像，而且只在 Schema 兼容时回滚**；Schema 不兼容时向前修复，需要还原数据库时进入灾难恢复（NX-035）。

## 1. 角色划分与参数

### 1.1 两机放置（沿用 11 §2，补充进程形态）

| 主机 | 常驻服务 | 持久数据 | 不放什么 |
|---|---|---|---|
| **DATA_HOST** | PostgreSQL 18（pgvector、pg_trgm）；Valkey；备份任务（NX-035） | 主库、WAL、来自 APP_HOST 的加密 Artifact 备份 | 不跑 Agent、API、CI；不保存任何应用服务的私钥（PG 和 Valkey 自己的服务端证书除外） |
| **APP_HOST** | Gateway（HTTPS）、Web、API（2 进程）、Domain Worker、Action Worker、Scheduler、Runtime Worker（guard 4 进程）、Agent Host（1 进程，全局并发 ≤4）、后台服务（NX-019–030 各 1 进程）、本地 Git 与 CI 车道（NX-033） | Runtime SQLite、Artifact、发布归档、PG 备份副本 | 不放业务 PG 主库；CI 的一次性库属于构建服务，不是业务库（12 §3） |

### 1.2 参数（全部来自私有 env 文件，仓库只放示例）

| 参数 | 含义 | 示例值的写法 |
|---|---|---|
| `APP_HOST_ADDR`、`DATA_HOST_ADDR` | 两台主机的 LAN 地址（DHCP 保留或静态） | `__APP_HOST_ADDR__` |
| `APP_HOST_NAME`、`DATA_HOST_NAME` | 证书 SAN 用的名称（可以是 IP SAN） | `__DATA_HOST_NAME__` |
| `TRUSTED_LAN_CIDR` | 允许访问 Gateway 的客户端网段 | `__TRUSTED_LAN_CIDR__` |
| `MGMT_SOURCE_CIDR` | 运维与 SSH 来源 | `__MGMT_SOURCE_CIDR__` |
| `PG_PORT`、`VALKEY_PORT`、`GATEWAY_HTTPS_PORT`、`GATEWAY_REDIRECT_PORT` | 默认 55432、56379、8443、8080（11 §3 的计划值，部署时由 doctor 检查是否冲突） | 可覆盖 |
| `API_INTERNAL_PORT`、`HOST_INTERNAL_PORT`、`GUARD_PORT` | APP_HOST 内部端口 | 只绑 loopback |
| `SSH_ALIAS_APP`、`SSH_ALIAS_DATA` | 运维用的 SSH 别名，从不写用户名或地址 | 只出现在私有 ssh config |
| `NEXLOOP_ROOT` | 默认 `/srv/nexloop`（11 §5） | 可覆盖 |

## 2. TLS

### 2.1 证书与签发（D1）
- **内部 CA 分两级**：
  - 根 CA 私钥离线保存在负责人的管理机上，不放在任何一台主机上；
  - 由根 CA 签一张中间 CA（有效期 1 年），用中间 CA 签发服务证书。
- **服务证书**，SAN 一律用参数：

  | 证书 | SAN | 用途 |
  |---|---|---|
  | `pg-server` | `DATA_HOST_NAME` / `DATA_HOST_ADDR` | PG 服务端 |
  | `valkey-server` | 同上 | Valkey 服务端 |
  | `gateway` | `APP_HOST_NAME` / `APP_HOST_ADDR` | LAN 用户访问 Gateway |
  | `pg-client-<role>` | CN = 该 PG 角色名 | PG 客户端证书，每个应用角色一张（D2） |
  | `valkey-client-<service>` | CN = ACL 用户名 | Valkey 客户端证书（D2） |
  | loopback 证书 | `127.0.0.1` | Host、guard、API 之间（沿用现状，由部署脚本统一签发） |

- **工具**：用 openssl 脚本签发（`scripts/deploy/issue-certs`，不新增依赖）。脚本只在管理机上运行，输出私钥文件（0600）和证书链；私钥从不经过仓库、日志或命令行参数。
- **有效期与轮换**：
  - 服务证书 90 天，剩余 30 天时轮换；
  - 签发后经 SSH 别名把文件分发到目标目录的 `secrets/tls/`，属主为对应服务的 UID；
  - PG 用 `pg_ctl reload` 重载证书（`ssl_cert_file` 等支持 SIGHUP），不必重启；
  - Valkey 用 `CONFIG SET tls-cert-file`；Caddy 重载即可；
  - loopback 证书随所在服务重启生效。
- **doctor**：检查每张证书的链、SAN 是否与参数一致、剩余有效期（<30 天警告，<7 天不通过）、私钥文件的权限和属主。

### 2.2 PostgreSQL
- `postgresql.conf`：
  - `ssl=on`，`ssl_min_protocol_version=TLSv1.3`，`password_encryption=scram-sha-256`；
  - `listen_addresses` 只包含 `DATA_HOST_ADDR` 和 localhost。
- `pg_hba.conf`（模板已有，补充如下）：
  - 每个应用角色一行 `hostssl nexloop_stage <role> APP_HOST_ADDR/32 scram-sha-256 clientcert=verify-full`（D2）；
  - 迁移和 bootstrap 身份只允许本机或 `MGMT_SOURCE_CIDR`，按次启用；
  - 最后两行兜底：`hostnossl all all 0.0.0.0/0 reject` 和 `host all all 0.0.0.0/0 reject`。
- **客户端**：每个服务的 DSN 文件都是完整连接串，包含 `sslmode=verify-full sslrootcert=<CA> sslcert=<client> sslkey=<client-key>`。
  - doctor 逐个 DSN 文件检查 `sslmode` 必须是 `verify-full`，缺少或取其他值都判不通过；
  - 代码里没有任何“验证失败就降级”的路径（11 §4）。

### 2.3 Valkey
- 只开 TLS 端口（`port 0`、`tls-port VALKEY_PORT`），`tls-auth-clients yes`（D2）。
- 禁用默认用户；每个服务一个 ACL 用户，只给需要的命令和 key 前缀，密码存在文件里。
- Valkey 只是可丢弃的缓存（AT-032）：它不可用时系统降级运行，但不丢任务。

### 2.4 mTLS 的范围（D2）

| 链路 | 服务端认证 | 客户端认证 |
|---|---|---|
| APP_HOST → PG | TLS verify-full | **客户端证书 + SCRAM** |
| APP_HOST → Valkey | TLS（校验主机名） | **客户端证书 + ACL 密码** |
| LAN 用户 → Gateway | TLS（内部 CA，用户安装根证书） | 应用登录（浏览器会话），不用客户端证书 |
| Gateway → API、API → Host、Worker ↔ Host / guard | loopback 或内部网络上的 TLS（现状） | 现有 transport key 和控制密钥；不加 mTLS（理由：不跨机，key 已分开） |

## 3. 网络隔离

### 3.1 端口

| 主机:端口 | 服务 | 绑定 | 允许来源 |
|---|---|---|---|
| DATA_HOST:`PG_PORT` | PostgreSQL | `DATA_HOST_ADDR` | 只允许 `APP_HOST_ADDR`；迁移和运维时允许 `MGMT_SOURCE_CIDR`（按次开放，见 §8） |
| DATA_HOST:`VALKEY_PORT` | Valkey TLS | `DATA_HOST_ADDR` | 只允许 `APP_HOST_ADDR` |
| APP_HOST:`GATEWAY_HTTPS_PORT` | Gateway | `APP_HOST_ADDR` | `TRUSTED_LAN_CIDR` |
| APP_HOST:`GATEWAY_REDIRECT_PORT` | HTTP 跳转到 HTTPS | `APP_HOST_ADDR` | 同上；不接受明文登录 |
| APP_HOST:`API_INTERNAL_PORT` | API | Compose 内部网络，不发布 | Gateway |
| APP_HOST:`HOST_INTERNAL_PORT`、`GUARD_PORT` | Agent Host、guard | 127.0.0.1 | 本机 Worker 和 API |
| APP_HOST loopback | 可选 Prometheus/Grafana、NX-030 指标导出 | 127.0.0.1 | 经运维 SSH 隧道；默认关闭 |
| 两机 SSH | 管理 | 现有配置 | `MGMT_SOURCE_CIDR`（沿用现有规则，不改动） |

Agent Host 的出站只允许访问模型 Provider 的 allowlist 主机（启用真实模型时）以及本机 guard；其他目标一律拒绝（13 §2、11 §8）。

### 3.2 防火墙（示例；按次由负责人执行）

**说明：Docker 发布的端口不经过 UFW 的 INPUT 链**，规则必须写在 `DOCKER-USER` 链（iptables/nftables），或者干脆不发布端口、只绑定到具体地址（11 §3）。示例文件为 `deploy/stage/firewall/*.example`，占位符由白名单渲染器替换：

```text
# DATA_HOST — nftables, inet filter, chain docker-user (示例；占位符为参数)
tcp dport __PG_PORT__     ip saddr __APP_HOST_ADDR__ accept
tcp dport __PG_PORT__     ip saddr __MGMT_SOURCE_CIDR__ accept   # 仅迁移/运维窗口启用，默认注释
tcp dport __VALKEY_PORT__ ip saddr __APP_HOST_ADDR__ accept
tcp dport { __PG_PORT__, __VALKEY_PORT__ } drop

# APP_HOST — Gateway 只对受信 LAN；内部端口不发布
tcp dport { __GATEWAY_HTTPS_PORT__, __GATEWAY_REDIRECT_PORT__ } ip saddr __TRUSTED_LAN_CIDR__ accept
tcp dport { __GATEWAY_HTTPS_PORT__, __GATEWAY_REDIRECT_PORT__ } drop

# APP_HOST — Agent Host 容器出站（示例：按容器网络或 UID 匹配）
oifname != "lo" meta skuid __AGENT_HOST_UID__ ip daddr != { __MODEL_PROVIDER_ALLOWLIST__ } drop
```

- UFW 现有的 SSH 规则不改动。新增规则单独成表或单独成链，便于整体撤销（§7）。
- 防火墙改动是对系统安全设置的修改，**由负责人本人执行**；本稿和部署脚本只提供待执行的规则文本和撤销命令。

### 3.3 隔离验证（AT-058，需要外部证据）
- 负向探测由 `scripts/deploy/probe-isolation` 执行，只读，只发起连接、不发送任何业务数据。探测位置有三个（D3）：
  1. 管理机：不是 APP_HOST；
  2. DATA_HOST 本机：对 APP_HOST 的内部端口；
  3. APP_HOST 本机：对 DATA_HOST，作为正向对照。
- 预期结果：

  | 探测 | 预期 |
  |---|---|
  | 管理机 → DATA_HOST:`PG_PORT`（默认窗口关闭） | 连接被拒或超时 |
  | 任意来源 → PG 明文连接（`sslmode=disable`） | 被拒（`hostnossl … reject`） |
  | APP_HOST → PG，用错误的 CA，或证书 SAN 不匹配 | 客户端拒绝（verify-full） |
  | APP_HOST → PG，不带客户端证书 | 服务端拒绝（clientcert） |
  | APP_HOST → Valkey，明文或默认用户 | 被拒 |
  | 管理机 → APP_HOST 的 API、Host、guard 内部端口 | 不可达 |
  | 受信 LAN → Gateway HTTPS | 可达；HTTP 端口只返回跳转 |
  | Agent Host 容器 → DATA_HOST:`PG_PORT` | 被拒（出站规则） |

- 探测结果写进不含地址的报告：只记参数名、端口参数名、预期结果、实际结果和时间，作为 AT-058 的证据。

### 3.4 不修改既有服务
- 部署前后各做一次只读 inventory：监听端口、容器、Compose project、systemd unit、防火墙规则集的 hash、数据目录列表。
- doctor 对比两次结果，**只允许出现 NexLoop 新增的项**；任何既有项发生变化都判不通过。报告只写 hash 和数量。
- 两份 Compose 的 project 名固定带 `nexloop-` 前缀；不复用任何既有 volume、network 或 PGDATA（11 §1、§5）。

## 4. 角色与凭据分发

### 4.1 PG 角色（11 §6；沿用现有角色名）

| 角色 | 谁使用 | 说明 |
|---|---|---|
| `nexloop_bootstrap` | 只在 bootstrap 时 | 创建角色和扩展；用完后禁用登录 |
| 迁移身份（catalog 的 owner 身份，按现有 bootstrap 约定） | 只在迁移窗口 | 执行已审核的 catalog；平时禁用登录 |
| `nexloop_configurator` | 可信配置时 | service-grants、business-actions、workbench 成员等；按次启用登录 |
| `nexloop_api` | API、claim-extraction-scheduler | |
| `nexloop_domain_worker` | Domain Worker 与各后台服务 | 每个服务一张独立的客户端证书（CN 相同、证书不同，便于单独吊销） |
| `nexloop_action_worker` | Action Worker | 唯一持有外部渠道凭据的进程 |
| `nexloop_scheduler` | Scheduler | |
| Runtime Worker 的角色 | Runtime Worker（父进程和 guard 子进程） | |
| `nexloop_metrics` | NX-030 导出 | 只能执行导出函数 |
| `nexloop_identity` | 身份存储 | |

- 应用角色一律不具备 superuser、BYPASSRLS、CREATEROLE、CREATEDB。doctor 用 `pg_roles` 核对。

### 4.2 凭据文件与分发
- **目录**：`NEXLOOP_ROOT/secrets/<service>/`，每个服务一个目录，属主为该服务的 UID，目录 0700、文件 0600；容器以只读方式逐个挂载单个文件，不挂载整个目录。
- **每个服务只拿到自己的那一份**：

  | 服务 | 持有 |
  |---|---|
  | API | DSN（`nexloop_api`）+ 客户端证书和私钥；服务凭据；签名密钥；Host 控制密钥；Host CA；Gateway 不持有这些 |
  | Domain / 后台服务 | 各自的 DSN 和客户端证书；各自的服务凭据（service-grants 中的主体）；签名密钥 |
  | Action Worker | DSN；服务凭据；**外部渠道凭据**（启用真实渠道后，由负责人配置） |
  | Runtime Worker | DSN；服务凭据；签名密钥；guard transport key；guard TLS 证书和私钥；Host 控制密钥；Host CA（ADR-024 §7：5 个进程共用一组私有文件） |
  | **Agent Host** | 只有 Host TLS 证书和私钥、Host transport key 的副本、guard CA，以及启用真实模型后的模型 Provider key；**没有 DB DSN、服务凭据、签名密钥、渠道凭据，也不能读 Artifact 宿主目录**（11 deploy README、13 §2） |
  | PG / Valkey | 各自的服务端证书和私钥、CA；Valkey 的 ACL 文件 |

- **服务凭据和授权只经可信配置写入**：`service-grants.v1.json` 和 business-actions 由 configurator 应用；workbench 成员来自私有成员文件（ADR-020 §3、NX-028）。应用角色不能给自己授权。
- **轮换**：
  - PG 密码：`ALTER ROLE … PASSWORD`，然后替换文件、重启该服务；同一时刻只轮换一个服务。
  - 客户端证书：签发新证书，替换文件，重启该服务，再吊销旧证书。v0.1 不做 CRL；旧证书的失效靠 `pg_hba` 中按 CN 映射到角色，以及到期。
  - 服务凭据：按现有的凭据轮换流程。
- **doctor**：
  - 各秘密文件的属主、权限、单链接，以及不是符号链接；
  - Agent Host 容器的挂载和环境变量中不含 DSN、凭据或签名密钥的文件名模式；
  - 任何进程的环境变量和命令行中都不出现秘密，只允许 `_FILE` 形式。

## 5. 镜像与发布清单

### 5.1 最小发布清单（NX-032 实现；NX-034 扩展，D4）
- CI（APP_HOST 的 CI 车道，NX-033 之前用现有 `scripts/ci/check`）通过后，生成 `releases/<release_id>/manifest.json`，内容如下：
  - `release_id`；
  - 提交 SHA；
  - 父提交；
  - `versions.lock.json` 的 digest；
  - 每个镜像的 `name@sha256` 及其 image archive 的 SHA256；
  - 迁移 catalog 的 digest 与最高版本；
  - 测试报告（junit）各自的 digest 与汇总计数（通过、失败、跳过）；
  - ADR-022 串行时限阶段的结论；
  - 生成时间。
- **部署只认清单**：
  - 部署脚本从清单中读取镜像，`docker load` 之后核对每个镜像的 digest；
  - Compose 的 `*_IMAGE` 由清单渲染，必须是 `name@sha256` 形式；
  - 清单中的提交 SHA 必须与本地 Git 中受信 ref 指向的 SHA 一致；
  - 测试报告必须没有失败，或者只有合并基线允许的 B/C 类观察项；
  - 任一项不符就拒绝部署（AT-055）。
- **运行中核对**：部署完成后，doctor `--release` 读取各运行容器的镜像 digest，与 `current/manifest.json` 比对；不一致就判不通过，并阻止开放 dispatch。
- NX-034 再加：归档保留策略、清单签名、GitHub 同步，以及 SBOM 和安全扫描入清单。

### 5.2 发布步骤（12 §5，对应 11 §9 的 D2–D3）
1. **drain**：停止领取新的自主工作，确认没有新的外部 dispatch；在途的等待完成或登记；unknown 先分类。
2. **备份门槛**：迁移之前必须有一份可验证的备份（AT-056）。
   - 在 NX-035 之前，最小门槛是 `pg_dump` 加上恢复到一次性隔离实例，并核对 catalog 和行数（D5）；
   - 没有通过验证的备份就不执行迁移，也不能以开发方便为由跳过。
3. **迁移**：用迁移身份执行清单中 catalog 的已审核迁移，只做向前迁移（expand）。
4. **启动顺序**：
   1. API 以 `REAL_DISPATCH_ENABLED=false` 启动；
   2. readiness 检查 catalog、身份和 Artifact；
   3. 启动各 Worker、Scheduler 和后台服务；
   4. **先停旧的 Agent Host，再启动新的**，同一时刻只允许一个 owner 打开 SQLite；
   5. 启动 Gateway。
5. **升级后检查**：readiness、迁移 catalog、凭据 scope、Artifact 读写、PG 队列、runtime 单 owner、合成 smoke、doctor 全套；全部通过后才按授权恢复 dispatch。

## 6. 部署前检查（doctor）

新增一个汇总入口 `nexloop-doctor --deploy-gate --release <manifest>`，依次执行以下检查，任一项不通过时退出码为 1，服务不启动：

| 检查 | 内容 | 现状 |
|---|---|---|
| Host 并发 | `maximum_active_runs` ≤4（ADR-022 §4） | 已有（`--agent-host-config`） |
| guard 进程 | Runtime Worker 的配置和连接预算中的 `guard_workers` **必须等于 4**（ADR-024 的部署取值）；运行中再核对实际子进程数 | **新增**：现在只核对预算合计，没有核对“=4” |
| 连接预算 | 合计 ≤60，`max_connections` ≥80；所有会连 PG 的进程都已登记，并与运行中的进程清单对比，有未登记的进程就不通过 | 合计已有；**“全部登记”是新增**，依赖 NX-031 切片 0 |
| 可观测 | NX-030 的 `--observability`：评估器在 5 分钟内运行过、规则版本一致、样本在写入 | 依赖 NX-030 合入 |
| TLS | §2.1–2.3：证书链、SAN、有效期、私钥权限；每个 DSN 都是 `verify-full`；Valkey 只开 TLS 端口 | **新增** |
| 端口与绑定 | 各监听端口的绑定地址符合 §3.1 表（内部端口只绑 loopback 或内部网络）；计划端口没有被既有服务占用 | **新增** |
| 角色 | 应用角色无超级权限；bootstrap 和迁移身份平时禁用登录 | **新增** |
| 秘密分发 | §4.2 的 doctor 项 | **新增** |
| 发布清单 | §5.1：镜像 digest、SHA、catalog、测试报告 | **新增** |
| 既有服务未变 | §3.4 的 inventory 对比 | **新增** |
| 总闸 | 首次启动时 `REAL_DISPATCH_ENABLED=false` | **新增**（只是检查，不修改） |

## 7. 回滚

| 情况 | 方式 |
|---|---|
| 没有 Schema 变化，数据兼容 | 按上一份已知可用的清单重新渲染并启动：镜像回到旧 digest，Agent Host 仍先停旧再启新 |
| 有 Schema 变化，旧应用兼容新 Schema（expand 阶段） | 同上，回到旧镜像；新列和新表保留 |
| 有 Schema 变化，旧应用不兼容 | **不盲跑 down 迁移、不回滚镜像**：部署脚本发现清单的 catalog 最高版本低于库内版本时拒绝回滚（AT-057）；改为向前修复 |
| 数据损坏，需要还原 | 进入灾难恢复（NX-035）：阻断 real dispatch，恢复到隔离实例，核对已经发生的外部动作，再决定切换 |
| Runtime 存储格式不兼容 | 保留旧 runtime 文件，任务标记 blocked，不删除文件（12 §6） |
| 防火墙或证书改动出错 | 新增规则在独立表或独立链中，整体撤销；证书回到上一组文件后重载 |
| 整个部署撤下 | 停止两份 `nexloop-` Compose project，按 §3.4 的 inventory 核对既有服务未受影响；数据目录保留，不删除 |

回滚本身也是 B 类操作，由负责人确认后执行。

## 8. 执行与授权（B 类）
- 以下步骤都会修改服务器状态，**每一步执行前都要负责人本人确认**；设计稿和脚本只做准备工作（渲染、校验、dry-run 输出）：
  1. 新建目录和服务用户；
  2. 安装证书；
  3. 改防火墙；
  4. 启动 infra；
  5. bootstrap 与迁移；
  6. 启动应用；
  7. 开放 PG 运维窗口；
  8. 回滚。
- 每一步都给出：要执行的命令（只用参数，不含真实值）、预期输出、撤销命令和核对项。
- 任何会话（包括开发线）都不得自行执行这些步骤，也不得连接服务器；隔离探测（§3.3）同样由负责人执行，或在负责人逐步授权下执行。
- 首次部署只用合成租户（11 §9 D4）和 `REAL_DISPATCH_ENABLED=false`；真实租户的初始化和真实渠道属于 NX-037。

## 9. 与 NX-033 / NX-034 / NX-035 的边界

| 事项 | NX-032（本任务） | NX-033 | NX-034 | NX-035 |
|---|---|---|---|---|
| CI 在哪里跑、如何隔离 | 只规定 CI 不能访问 DATA_HOST 网络和服务秘密（防火墙规则里不放行 CI 用户或网络） | rootless、Git 队列、受信 ref、外部 PR 隔离（AT-054） | — | — |
| 发布清单 | 最小清单与部署时校验（AT-055 的部署侧） | 产出受信构建 | 归档、签名、保留、GitHub 同步、SBOM（AT-053、AT-055 的完整实现） | — |
| 备份 | 迁移前的最小可验证备份门槛（AT-056 的最小形态） | — | — | 定时备份、WAL/PITR、加密异机副本、恢复演练、RPO/RTO（AT-050、AT-056 完整实现） |
| 回滚 | 镜像回滚与拒绝盲回滚（AT-057） | — | 回滚使用归档中的旧清单 | 数据库还原的灾难恢复流程 |
| 证书与防火墙 | 全部 | CI 网络的出站规则 | — | 备份传输通道使用同一个 CA |

## 10. 对应 AT

| AT | 关系 |
|---|---|
| **AT-058 端口安全** | **本任务主验收**：§3.3 的外部负向探测，以及 TLS 和 ACL 拒绝的证据 |
| AT-055 镜像发布一致 | 部署侧：清单与运行 digest 不一致时拒绝（§5.1）；完整实现见 NX-034 |
| AT-056 迁移无备份 | 最小门槛：没有可验证备份就拒绝迁移（§5.2）；完整实现见 NX-035 |
| AT-057 版本回滚 | 拒绝不兼容的盲回滚（§7） |
| AT-054 CI 隔离 | 只提供网络层前提（§9），验收在 NX-033 |
| AT-049 日志秘密 | 部署后 smoke 时复查（NX-030 的哨兵扫描） |
| ADR-022 §4 部署前提 | doctor 的 Host 并发和 guard=4（§6）；NX-031 C4 的结论是规模依据 |

## 11. 实现切片（建议）

| 切片 | 内容 | 在哪里验证 |
|---|---|---|
| 0 | 参数化模板：两份 stage Compose 示例、pg_hba、Valkey 配置、防火墙示例、白名单渲染器（拒绝未渲染的占位符） | 本机与 CI：渲染器测试、`docker compose config` |
| 1 | 证书脚本、doctor TLS 项、DSN 的 `verify-full` 检查 | 本机或 CI 的一次性 PG 和 Valkey（客户端证书、错误 CA、SAN 不匹配的负例） |
| 2 | doctor 其余新增项：guard=4、预算全部登记、端口绑定、角色、秘密分发、inventory 对比 | 一次性集群 |
| 3 | 最小发布清单的生成与校验、部署脚本 dry-run、回滚拒绝 | CI |
| 4 | 隔离探测脚本与报告格式 | 一次性的双网络命名空间模拟（不接触真实主机） |
| 5 | **真实双机部署与外部验证**：负责人逐步确认后执行 | 两台主机（B 类） |

切片 0–4 都不接触服务器；切片 5 只在负责人确认后进行。

## 12. 需要负责人拍板的点（D 项）

- **D1 CA 与证书工具**：
  - 推荐：根 CA 私钥离线保存在负责人的管理机，下设中间 CA（1 年）；服务证书 90 天，剩余 30 天时轮换；用 openssl 脚本签发，不新增依赖。
  - 备选：Caddy 或 step-ca 的内部 CA。CA 私钥会常驻主机，所以不推荐。
- **D2 mTLS 范围**：
  - 推荐：跨机的 PG 使用客户端证书加 SCRAM，Valkey 使用客户端证书加 ACL 密码；loopback 和内部网络沿用现有 TLS 加 transport key，不加 mTLS；Gateway 只做服务端 TLS。
  - 备选：跨机也只用 SCRAM 和 ACL 密码，不加客户端证书。配置更简单，但密码泄露后可以直接连库。
- **D3 谁执行，以及探测从哪里发起**：
  - 推荐：所有服务器改动和外部探测都由负责人本人执行，或在负责人逐步授权下执行；探测位置为管理机、DATA_HOST、APP_HOST 三处。
  - 开发线和调度会话都不连接服务器（LINE-RULES）。
- **D4 NX-032 先做最小发布清单**：
  - 推荐：在 NX-032 实现 §5.1 的最小清单和部署校验；归档、签名和同步留给 NX-034。
  - 原因：任务依赖顺序是 NX-032 → 033 → 034，部署不能等 NX-034 完成后才有一致性校验。
- **D5 迁移前的最小备份门槛**：
  - 推荐：在 NX-035 之前，每次迁移前做一次 `pg_dump`，恢复到一次性隔离实例并核对 catalog 和行数；首次部署是空库，也照样执行，以证明这条流程本身可用。
  - PITR、WAL 和异机加密副本留给 NX-035。
- **D6 Valkey 放置**：
  - 推荐沿用 11 §2，放在 DATA_HOST（TLS + ACL + 客户端证书）。
  - 备选：放在 APP_HOST 只绑 loopback。少一个跨机端口，但会占用 APP_HOST 的内存，而它同时要跑 CI 和压测。
- **D7 端口取值**：沿用 11 §3 的计划值（55432、56379、8443、8080）作为默认，由 doctor 检查冲突；如果冲突，在私有 env 中改端口，不改既有服务。
- **D8 Gateway 暴露范围**：v0.1 只对 `TRUSTED_LAN_CIDR` 开放，HTTP 端口只做跳转；公网暴露另立方案和 ADR。
- **D9 首次部署内容**：推荐只用合成租户、`REAL_DISPATCH_ENABLED=false`，跑完 doctor 全套、smoke 和 AT-058 探测；真实租户和真实渠道在 NX-037。
