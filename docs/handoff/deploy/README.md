# 部署模板说明

本目录是Codex需要落实的配置基线，不是可直接一键部署的已完成应用。应用镜像、Dockerfile、CLI、health endpoint、实际DSN、证书和digest尚未生成。YAML能解析不代表Docker或应用已验证。

`compose.infra.yaml`在DATA_HOST运行；`compose.app.yaml`在APP_HOST运行。不会创建跨主机Docker network。先读[部署方案](../docs/11_DEPLOYMENT.md)和[本地CI](../docs/12_LOCAL_CI_AND_RELEASE.md)。社区单机Compose由Codex在S1提供。

## 必须完成的渲染步骤

1. 私有读取inventory；确认新目录和端口，不碰已有服务。根据`env.example`创建宿主私有环境文件，权限0600。
2. 根据实际源代码构建镜像并冻结digest。所有`*_IMAGE`必须`name@sha256:...`；Compose中的required变量只检查非空，Codex预检另检查格式和release manifest。
3. 生成内部CA、PG/Valkey server cert和客户端信任CA；证书SAN匹配DATA_HOST。按容器实际UID设置key权限，不用chmod777解决。
4. `pg_hba.conf.template`、`valkey.conf.template`中的`__...__`是必须渲染的占位符，不会被Compose自动替换。配置渲染器只允许白名单变量，拒绝换行/任意路径注入；任何未渲染占位符阻止启动。
5. 在bootstrap中创建最小权限角色、安装扩展、执行新catalog。PG bootstrap管理员密码只注入infra，不放API。应用DSN为完整verify-full连接串，引用挂载CA，分别对应API/domain worker/Action worker/scheduler身份；不复用owner。
6. TLS/ACL/readiness检查后，先`real_dispatch_enabled=false`启动。首次真实dispatch还需具体tenant/渠道授权，并执行验收。

## 文件清单

- `examples/compose.infra.yaml`：PostgreSQL与Valkey模板。
- `examples/compose.app.yaml`：Gateway/Web/API/三个职责worker/Agent Host模板；UI/API镜像共享版本但进程分工。
- `examples/env.example`：无真实IP/secret的变量说明。
- `examples/postgresql.conf.template`、`pg_hba.conf.template`、`valkey.conf.template`：基础服务保守配置；资源值必须按实机校正。
- `examples/Caddyfile.template`、`web-nginx.conf.template`：同源入口和SPA fallback。
- `examples/PUBLICATION_EXCLUSIONS.example`：公开仓库必须排除的私有资料。

## 镜像必须实现的目标入口

以下是NexLoop拟实现命令，不是上游已有CLI：`nexloop-api`、`nexloop-domain-worker`、`nexloop-action-worker`、`nexloop-scheduler`。Action worker内部接入EIOS原生执行机制，不增加第二套副作用账本。

API需要`/health/live`和`/health/ready`；live只说明进程存活，ready检查当前catalog/身份/存储等必要依赖。PG `pg_isready`也不是权限/迁移/扩展的充分readiness。

本包故意不提供`latest`镜像、默认弱口令、伪造SSH用户或下载即执行安装脚本。CI隔离构建、备份、迁移与发布命令在相应任务完成后才成为可执行软件。所有模板需通过`docker compose config`和真实受控启动测试后进入release。

## 网络与安全

Compose network本身不能实现租户授权或完整出站策略。API/worker连接DATA_HOST，Agent Host只访问受限工具网关和许可的模型provider。Codex需补齐宿主防火墙/出站ACL和负向连通性测试。Agent Host不可读DB/渠道密码或Artifact宿主目录。

`REAL_DISPATCH_ENABLED=false`是初始总闸；运行中tenant/policy的暂停由PG/EIOS权威决定，环境变量true不能覆盖当前业务撤权。后台任务需要以实际role进行RLS与许可校验。
