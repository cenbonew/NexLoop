# NX-032 切片 0–3 与切片 4 预检：实现说明

依据：`NX-032-design.md` §11 的切片 0–4（D1–D9 已定，§12）。

- 分支 `nx032-local`，从 main `3b5a684` 切出。没有迁移。
- 三个切片都没有连接任何服务器，也没有读取 local-only 下的主机信息。
- 证书和私钥只在测试临时目录中生成，测试结束随目录删除；有测试断言仓库中没有私钥或证书文件。
- §8 的 B 类步骤只写成文档：[`NX-032-deploy-runbook.md`](NX-032-deploy-runbook.md)。

## 切片 0：参数化模板与白名单渲染器

### 文件
- `deploy/stage/compose.data.example.yaml`：DATA_HOST 的 PostgreSQL 和 Valkey。
- `deploy/stage/compose.app.example.yaml`：APP_HOST 上的服务，各附一份 `*.env.example`。
- `deploy/stage/config/`：`pg_hba.conf`、`postgresql.conf`、`valkey.conf` 三个模板。
- `deploy/stage/firewall/`：两机各一份 nft 模板。
- `deploy/stage/stage.env.example`：渲染参数示例。

### 渲染器
`nexloop_eios.stage_render`，由 `scripts/deploy/render.py` 包装调用；doctor 读参数时也用它。
- 只接受白名单内的 22 个参数，每个都有格式校验：地址、CIDR、端口、绝对路径、角色列表、大小、UID、地址列表。
- 以下情况一律拒绝：
  - env 文件不是 0600；
  - env 中有未知键或格式不符的值；
  - 模板里有未知占位符，或缺少所需参数；
  - 渲染后仍残留 `__X__`；
  - 输出文件已存在。
- 输出只包含参数名，不包含参数值。

### 模板要点
- **pg_hba**：
  - bootstrap 只能通过容器内的 socket 连接；
  - 应用角色只允许来自 APP_HOST_ADDR，且必须 `hostssl` + `scram-sha-256` + `clientcert=verify-full`；
  - 迁移窗口那一行默认注释；
  - 末尾四行全部 reject，`hostnossl` 在前。
- **postgresql.conf**：TLS 1.3，`ssl_ca_file` 用于校验客户端证书。
- **Valkey**：`port 0`，只开 TLS 端口，`tls-auth-clients yes`，使用 ACL 文件。
- **防火墙**：
  - 规则放在独立表 `inet nexloop_stage` 中，撤销只需 `nft delete table`；
  - forward 钩子按 `ct original` 匹配，因此 Docker 发布的端口同样受控；
  - CI 用户访问 DATA_HOST 和内部端口一律拒绝。
- **APP_HOST 的 Compose**：
  - 由一个锚点服务持有网络命名空间，内部 HTTPS 仍走 127.0.0.1（与现状一致）；
  - 只发布 Gateway；
  - 每个服务逐个挂载自己的秘密文件；
  - Agent Host 只拿到自己的 TLS 证书和私钥、transport key 和 guard CA；
  - `REAL_DISPATCH_ENABLED=false`；Runtime Worker 为 guard 4、dispatch loops 4。

### 验证
- 本机已装 docker：两份 Compose 都通过 `docker compose config`，不需要 daemon。
- 没有 docker 的环境（例如 rootless CI 用户）：测试按 Compose 的变量规则自行做替换检查（必需变量必须有值，镜像必须是 digest 形式），不会跳过。

## 切片 1：证书脚本与 TLS 检查

### `scripts/deploy/issue_certs.py`
- 只用 openssl，按 D1 签发：
  - 根 CA ≤3650 天，只放在管理机；
  - 中间 CA ≤365 天，`pathlen:0`；
  - 服务证书 ≤90 天，`serverAuth`，SAN 用参数；
  - 客户端证书 ≤90 天，`clientAuth`，CN 为角色名或 ACL 用户名。
- 私钥是 EC P-256，0600，不覆盖已有文件，不打印内容，也不出现在命令行参数中。

### `nexloop_eios.deploy_checks.tls` 与 `nexloop-doctor --tls-spec`
- **证书检查**：在本进程内对一个 socketpair 做一次 TLS 握手，一次同时证明证书链、名称和私钥是否匹配。
  - 剩余有效期 < 30 天警告，< 7 天不通过；
  - 私钥文件须为 0600、普通文件、单链接、非符号链接，属主可选核对。
- **DSN 检查**：`sslmode` 必须是 `verify-full`，且必须给出根 CA、客户端证书、私钥、host、user 和 dbname。
- **Valkey 配置检查**：不开明文端口，要求客户端证书，配置了 ACL 文件。
- 输出只包含名称和问题代码。

### 真实负例
在本机一次性 PG 18 上，通过 127.0.0.1 TCP 开启 TLS 1.3、SCRAM 和 `clientcert=verify-full`。正确配置可以连接；以下 7 种都被拒绝：
1. 客户端用错的 CA；
2. 服务端证书 SAN 不匹配；
3. 客户端证书来自另一个 CA；
4. 不带客户端证书；
5. 客户端证书 CN 是另一个角色；
6. 明文连接；
7. 密码错误。

本机没有安装 Valkey，所以 **Valkey 的服务端负例没有运行**，只测了配置检查。

## 切片 2：doctor 其余新增项（`nexloop-doctor --deploy-gate <私有 spec>`）

| 检查 | 内容 |
|---|---|
| `guard_workers_exact` | 连接预算中 Runtime Worker 的 guard 必须**等于** 4 |
| `running_guard_children_exact` | 运行中的 guard 子进程必须正好 4 个 |
| `budget_all_required_declared` | 沿用现有检查，包括 dispatch_loops 和 incentive-issuer |
| `running_processes_registered` | 运行中所有连接 PG 的进程都已登记，且进程数不超过登记值 |
| `port_bindings`、`planned_ports_free_before` | DATA_HOST：PG 和 Valkey 只绑 DATA_HOST_ADDR。APP_HOST：Gateway 只绑 APP_HOST_ADDR，内部端口只绑回环。NexLoop 不得监听通配地址。部署前，计划端口如被其他服务占用即为冲突（D7） |
| `application_roles_unprivileged`、`bootstrap_identities_disabled` | 读取 `pg_roles`：应用角色没有超级权限；管理身份平时禁止登录 |
| `secret_distribution`、`real_dispatch_closed` | 秘密目录 0700、文件 0600、普通文件、单链接、非符号链接，属主可选核对；Agent Host 不持有 DSN、凭据、签名或渠道文件；渲染后的 Compose 中环境变量不含秘密值，Agent Host 不挂载其他服务的秘密或 Artifact，总闸保持关闭 |
| `existing_services_unchanged` | 部署前后两份只读 inventory（`scripts/deploy/inventory.py`）对比：只允许出现 `nexloop` 前缀的新增项；去掉 NexLoop 表之后，防火墙规则集的 hash 不变 |
| `agent_host_concurrency`、TLS 各项 | 沿用现有检查和切片 1 的检查 |

- 输入缺失时，对应检查判为不通过（`input_provided:<名称>` 为 false），不会被跳过。
- 输出不含地址、路径或秘密。

### 测试

| 文件 | 数量 | 内容 |
|---|---|---|
| `tests/test_deploy_render.py` | 4 | 白名单与完整渲染、各种拒绝、Compose 结构、`docker compose config` |
| `tests/test_deploy_tls.py` | 5 | D1 的签发规则、PG 真实负例、doctor 证书检查、DSN 与 Valkey 检查、仓库中没有私钥 |
| `tests/test_deploy_gate.py` | 6 | guard 与登记、端口绑定、角色、秘密分发、inventory 对比，以及 `--deploy-gate` 端到端（缺少输入时不通过） |

`tests/test_connection_budget.py` 中的“stage 示例不含真实地址”这条测试，口径补充为：除回环地址外，也允许未指定地址 `0.0.0.0` 和 RFC 5737 文档保留段。这两类都不是真实地址。

## 切片 3：部署 dry-run（`scripts/deploy/deploy.py dry-run`）

dry-run 只做判断并输出计划：不执行任何步骤，不连接主机，也不调用 docker。

### APP_HOST
- **发布清单校验**（AT-055 部署侧，复用 `release.py verify`）：
  - 已签名；
  - 清单中的提交等于受信 SHA。受信 SHA 可以直接给出，也可以从本地仓库的受信 ref 解析；
  - 清单中的每个镜像都要部署；
  - 每个镜像 archive 都没有被改动。
- **镜像变量**：Compose 的镜像变量由清单生成（`compose.images.env`）。私有 Compose env 中写了其他 digest 的，一律拒绝；只多一个镜像仓库前缀可以接受。
- **回滚**：给出当前部署的清单时，调用 `release.py rollback-check`，拒绝盲目回滚（AT-057），并给出向前修复的提示。

### DATA_HOST
- PostgreSQL 和 Valkey 的镜像必须是 `versions.lock.json` 中固定的 digest。

### 输出
- 本机渲染结果写入新的输出目录：
  - APP_HOST：`Caddyfile`、`app-host.nft`、`compose.images.env`；
  - DATA_HOST：`postgresql.conf`、`pg_hba.conf`、`valkey.conf`、`data-host.nft`、`compose.images.env`。
- 列出将要执行的步骤，每一步带命令和撤销方法，只用参数，与运行手册一一对应。

### 其他改动
- **Gateway 模板** `deploy/stage/config/Caddyfile.template`：
  - 只做服务端 TLS（TLS 1.3，使用内部 CA 签发的 gateway 证书）；
  - HTTP 只做跳转；
  - `/internal/*` 返回 404；
  - 经 127.0.0.1 HTTPS 转发到 API；
  - 不信任客户端传来的 X-Forwarded-For。
- **网络命名空间锚点**：APP_HOST Compose 的锚点改用后端镜像执行 `sleep`，不再引入额外的 pause 镜像（那个镜像没有固定在锁文件中）。
- **地址规则测试**：“stage 示例不含真实地址”测试按调度员要求补了反向断言：RFC 1918 私网段、100.64/10 和非保留的公网地址仍然被拒。

### 测试
`tests/test_deploy_dryrun.py`（5 个）：
- 正常路径：校验通过、生成渲染结果、列出步骤、什么都不执行；
- 受信 SHA 不符、镜像 digest 不在清单中、清单镜像没有映射到变量、archive 被改动，都会被拒绝；
- 盲目回滚被拒绝，同一 head 允许；
- DATA_HOST 使用锁文件中的镜像；
- CLI 退出码；输出目录已存在时拒绝。

## 切片 4 预检：namespace 可行性（`scripts/deploy/netns_feasibility.py`）

### 检查内容
- 只读：读取 `/proc/sys` 下的设置：
  - `max_user_namespaces`、`max_net_namespaces`；
  - Debian 的 `unprivileged_userns_clone`；
  - Ubuntu 23.10+ 的 `apparmor_restrict_unprivileged_userns`。
- 在临时 namespace 内做 4 项探测，进程退出后 namespace 随之消失：
  1. `unshare -rn true`；
  2. 启用 lo；
  3. 创建 veth 对；
  4. 嵌套第二个 net namespace。
- 报告工具是否齐全：`ip`、`slirp4netns` 或 `pasta`、`podman`、`valkey-server`、`docker`；同时给出 Valkey 负例可以用二进制还是只能用容器运行。
- 不改系统设置，不需要 root，不连接任何主机。

### 结论与测试
- 本机（macOS）结论为 `not_linux`，不可行。CI 主机上的结论需要由负责人或调度员以 rootless CI 用户运行这个脚本得到。
- `tests/test_netns_feasibility.py`（4 个）：用注入的设置和探测结果覆盖判定逻辑（每种限制都会给出具体原因），并实际运行一次本机检查。

## 未完成项
- **切片 4 本体**：AT-058 隔离探测脚本、报告格式、双命名空间模拟；Valkey 服务端 TLS 负例也在那个环境中补上（可以用容器运行 Valkey）。要先由调度员根据可行性结论决定在哪里运行。
- **切片 5**：真实双机部署，属于 B 类，按运行手册逐步由负责人确认。
- **开放设计点：Agent Host 出站**。Agent Host 与 API、Worker 共用一个网络命名空间，模型 allowlist 出站对整个命名空间生效。首次部署（D9）不接真实模型，暂不处理；**接入真实模型（NX-037）之前必须解决**。可选方案：给 Agent Host 单独的命名空间，并给 loopback 证书补 SAN。调度员会在 NX-037 之前提交负责人。
- **镜像引用**：dry-run 生成的镜像引用形如 `nexloop/<镜像名>@sha256:…`。`docker load` 之后要能按这个 digest 引用到镜像，这取决于 NX-034 打包时给镜像加的标签和仓库名，需在打包时确认。
