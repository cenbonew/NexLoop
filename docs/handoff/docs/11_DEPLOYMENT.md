# 部署规划

## 1. 环境与边界

目标是用户提供的两台本地服务器。准确地址见 私有 LAN 附件（私有附件，公开副本不包含）。公开文档使用 DATA_HOST 和 APP_HOST；IP 不写入应用代码、镜像和通用快速启动配置。

只读盘点结果在私有附件中（公开文档不含）；两台主机的地址必须在路由器做 DHCP 保留或改静态，部署脚本通过 SSH 别名/主机名而非硬编码 IP 访问。所有资源值仍须由 doctor/inventory 在部署时复核。不要自动重装操作系统、停止现有服务或复用现有生产数据卷。

## 2. 双机服务放置

| 主机 | 常驻服务 | 持久数据 | 不部署什么 |
|---|---|---|---|
| DATA_HOST | PostgreSQL 18＋pgvector＋pg_trgm；Valkey 8.1；备份/健康任务 | 主库、WAL、缓存可选数据、来自 APP_HOST 的加密工件备份 | 不运行模型 Agent，不执行 GitHub 外部 PR 构建 |
| APP_HOST | Gateway、Web、API、Domain Worker、Action Worker、Scheduler、Agent Host、本地 Git、CI/controller | Runtime SQLite、Artifact 文件、构建缓存、发布归档、Git、来自 DATA_HOST 的 PG 备份 | 不把业务 PostgreSQL 主库搬到应用机；CI 临时 DB 是隔离测试例外 |

原 EIOS 的已有生产实例与这两个 NexLoop 环境无默认网络或数据库依赖。

## 3. 网络与端口

| 主机/端口 | 服务 | 允许来源 |
|---|---|---|
| DATA_HOST:55432 | NexLoop 专属 PostgreSQL TLS | APP_HOST 运行服务与明确运维入口 |
| DATA_HOST:56379 | NexLoop 专属 Valkey TLS＋ACL | APP_HOST 运行服务，不面向整个 LAN |
| APP_HOST:8443 | Gateway HTTPS | 受信任 LAN 客户端；后续公网另行方案 |
| APP_HOST:8080 | 仅 HTTP→HTTPS 跳转 | 与8443同范围；不接受明文登录 |
| APP_HOST 内部8000 | API | Gateway/授权后台；不发布给 LAN |
| APP_HOST 内部8100 | Agent Host | API/Worker；不发布给 LAN |
| APP_HOST loopback19090/13000 | 可选 Prometheus/Grafana | 运维 SSH 隧道；默认 profile 关闭 |
| 现有 SSH 端口 | 管理、Git、受限备份 | 已确认管理来源；不假定默认22可用 |

55432/56379 是避免与已有服务冲突的计划端口，不是已经占用/空闲的事实；doctor 必须检查冲突。两份 Compose 不共享跨机 network，APP_HOST 通过 LAN IP/TLS 连接 DATA_HOST。

Docker 发布端口可能绕过常规 UFW 路径，应结合实际 Docker firewall backend、DOCKER-USER/nftables 规则与外部连通性测试验证，不能只做 `ufw deny` 就宣布隔离。[S13]

## 4. TLS、DNS 与登录

本地测试可使用带主机 IP SAN 的内部 CA 证书，或 Caddy internal CA；安装受信任根证书后访问 HTTPS。内部 CA 只用于授权 LAN，不视为公网可信证书。证书私钥与 CA 密钥不进入 Git。[S18]

PostgreSQL 使用 hostssl、SCRAM、最小角色和 `sslmode=verify-full`，证书 SAN 必须匹配配置的 DATA_HOST 名称/IP；客户端必须挂载受信任 CA。Valkey 使用 TLS 和独立 ACL 用户，禁止无密码默认用户。不得因证书配置失败在 stage 静默禁用校验。

本地单机开发可以明确启用 localhost-only 的开发登录配置；该配置不能用于 LAN stage。

## 5. 文件与目录

DATA_HOST：`/srv/nexloop/data/postgres/`、`/srv/nexloop/config/`、`/srv/nexloop/secrets/`、`/srv/nexloop/backups/app/`。

APP_HOST：`/srv/nexloop/git/`、`/srv/nexloop/worktrees/`、`/srv/nexloop/releases/<release_id>/`、`/srv/nexloop/current/`、`/srv/nexloop/data/runtime/`、`/srv/nexloop/data/artifacts/`、`/srv/nexloop/ci/`、`/srv/nexloop/backups/postgres/`。

配置、运行数据、构建缓存、备份使用不同目录与 OS 权限；CI 用户不可读运行 secrets、artifact 原文和备份解密密钥。宿主磁盘启用适当加密与空间告警，容器 root filesystem 尽量只读，tmpfs 有大小上限。

PostgreSQL 18 容器的数据目录按所选基础镜像核验，本包模板显式使用 `/var/lib/postgresql/18/docker` 并挂载 `/var/lib/postgresql`；禁止直接套用旧版本目录假设覆盖现有 PGDATA。

## 6. 数据库与连接预算

独立库 `nexloop_stage`。扩展安装由 bootstrap 管理权限完成，常驻 API/Worker 不具备 CREATE EXTENSION、CREATE ROLE、superuser/BYPASSRLS。

目标角色为迁移、API、业务 Worker、EIOS Action Worker、Scheduler、只读指标分别分权，实际映射须兼容 EIOS 原生角色/RPC。绝不以一个 owner DSN 注入所有服务。

初始连接预算以总和不超过60为约束，预留运维和故障核对；配置必须计算每进程 pool max ×进程数，不能每个服务各配50连接。连接池满时返回明确容量/依赖错误，不无界等待。

## 7. 资源与服务初值（待实测）

建议预留独立 SSD/NVMe 持久卷和稳定千兆级 LAN，但不把这些视作已确认硬件。小规模验证可先低并发；资源不足时降低后台/构建并发，不降低持久化和权限校验。

初始 API 2个进程；Domain Worker 1；Action Worker 1个进程、受限任务并发；Scheduler 1个主动 leader；Agent Host 1个进程、全局并发4；CI同一时间仅1个构建。监控 profile 可按资源启用。

构建设置 CPU/内存限制和 nice/ionice；不得让 OOM killer 优先杀死运行服务。总容器内存 limits 不应承诺全部同时吃满，需结合实机容量；压测报告保存背景 CI 是否运行。

## 8. 配置策略

通用环境变量定义见 `deploy/examples/env.example`（仓库内即 `.env.example`）；本地开发把真实值放在 git 忽略的 `.env`，stage 的真实值位于宿主私有路径。密码与长时凭据以 secret 文件挂载，应用支持 `_FILE` 读取，且 `_FILE` 非空时优先于同名环境变量。镜像引用必须通过仓库根 `versions.lock.json`（段：`upstream_sources`、`packages`、`images`）与 release manifest 解析到确切 digest，不使用 latest。镜像名允许带可配置的 registry 镜像前缀（如国内镜像站），digest 不随前缀变化，预检仍按 digest 校验。

预检确认模型 Provider、model_id、embedding profile/dimension、费用上限和外部数据发送策略。没有模型 key 时只能启用显式 test provider，UI 标识“测试模型”；不得作为真实模型验收。

工作台默认禁止任意自定义 LLM base URL 访问私网服务；Provider host 由维护者 allowlist，避免 SSRF。

## 9. 部署顺序

**D0 只读盘点。** OS、架构、资源、时间同步、磁盘、现有端口、Docker/Compose、SSH身份、host key、备份目的地和网络路径。输出无密钥 inventory。

**D1 基础服务。** 为 NexLoop 新建数据目录、角色与证书；启动独立 PostgreSQL/Valkey；运行 TLS/ACL/连接测试。不得复用或改写现有 PGDATA。

**D2 构建和迁移。** 本地 CI 生成不可变 release artifact；先验证空库 bootstrap，再备份 stage；用迁移专用身份运行已审核 migrations。没有可验证备份不执行迁移。

**D3 应用启动。** API只读/dispatch disabled→检查 Catalog/身份/Artifact→Worker/Scheduler→唯一 Agent Host→Gateway。readiness 失败不得开放真实 dispatch。

**D4 种子和验收。** 显式创建合成 demo tenant 与1,000消费者；真实 tenant 独立初始化。跑回归与故障注入，验证商业事件 test 标识和 real 隔离。

**D5 受控真实运行。** 负责人确认渠道和模型权限后开启指定 tenant/Action Pack 的 dispatch；监控 unknown、预算、承诺和联系拒绝。发布授权不等于逐动作人工审批。

## 10. 单机社区配置

社区 `compose.local.yaml` 包含相同 API/Worker/Host 与本地 PostgreSQL/Valkey/Artifact，默认 loopback 入口；不需要私有 IP、原 EIOS 云服务器或 GitHub CI。功能和数据边界与双机一致，只有 deployment adapter 不同。

本交接提供配置模板和目标命令，不包含应用 Dockerfile/entrypoint 实现。Codex 的 S1 必须交付能实际启动的单机配置与由它派生的双机配置，不能把未实现镜像名当可运行软件。

## 11. 可用性与恢复限制

本方案是开发/测试及受控试运行，不是 HA。任一主机或 LAN 故障都会影响部分或全部服务。两机互存备份不能抵御两台同时损坏、勒索或机房事故；真实商业化前必须增加独立离线/异地备份。不要用“有两台机器”宣称高可用。
