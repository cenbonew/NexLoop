# NX-034 / NX-035：发布镜像 `nexloop/postgres`（PostgreSQL 18 + pgvector + pgBackRest）

依据：调度员 2026-10-14。

DATA_HOST 的 PG 容器里必须有 pgBackRest，因为 `archive_command` 和 stanza 都在容器内调用它（NX-035 运行手册 2.0 第 2 条）。部署用的镜像必须来自签名发布，所以这个镜像要随首个签名发布一起构建。

## 1. 组成

| 文件 | 作用 |
|---|---|
| `deploy/postgres/Dockerfile` | 基础镜像为 `FROM ${POSTGRES_IMAGE}`（`versions.lock.json` 中 `images.postgres`，按 digest 传入）；只安装锁定版本的 pgBackRest；构建结束时核对 `pgbackrest version` 必须等于锁文件中的版本；pgBackRest 的目录属主为 postgres；最后一层 `USER postgres` |
| `scripts/prepare_postgres_build.py` | 准备构建上下文：Dockerfile 和 `debs/`。默认 `debs/` 为空，走在线路径；设置 `NEXLOOP_PGBACKREST_DEB_DIR` 时先校验离线包，再复制进去 |
| `deploy/release/images.v1.json` | 新增 `postgres`：`kind: build`，`base: postgres`，`tool_args` 从锁文件读取 `deployment_tools.pgbackrest.version` 和 `debian_version` |
| `scripts/release/release.py` | `build` 传入 `tool_args`，缺值时拒绝构建；平台固定 `linux/amd64`；镜像按 digest 写入清单。sbom、preflight、verify、load-check 都按清单中的镜像逐个处理，不需要另外改动 |
| `scripts/deploy/deploy.py` | data 角色的 dry-run 改为必须带签名发布：`POSTGRES_IMAGE` 取 `nexloop/postgres@<清单 digest>`，`VALKEY_IMAGE` 照旧取锁文件；新增 `load_images` 步骤；`compose.images.env` 随配置一起下发，并在启动时传给 compose |

## 2. 构建前要在 CI 主机上做的事（A 类，按顺序）

1. **预先拉取基础镜像**。构建使用 `--pull=false`，基础镜像必须已经按 digest 存在于本机。docker 通过加速器访问 Docker Hub：
   ```bash
   docker pull --platform linux/amd64 docker.io/pgvector/pgvector@<images.postgres.digest>
   ```
2. **确认 apt 出站，并读出 Debian 包版本**：
   ```bash
   docker run --rm --platform linux/amd64 --user root docker.io/pgvector/pgvector@<images.postgres.digest> sh -c 'apt-get update && apt-cache madison pgbackrest'
   ```
   - 能输出 `pgbackrest | <版本> | http://apt.postgresql.org/pub/repos/apt bookworm-pgdg/main amd64 Packages` 一类的行，就说明在线路径可用；
   - 从中选出与锁文件 `deployment_tools.pgbackrest.version` 对应的那一行，把它的完整 Debian 版本（例如 `<版本>-1.pgdg120+1`）写进锁文件的 `deployment_tools.pgbackrest.debian_version`，提交 PR；
   - 如果没有对应的版本：PGDG 主仓库通常只保留最新版，旧版本在 apt-archive.postgresql.org。此时有两个办法，由负责人决定：
     - 把锁文件中的 `version` 改成 PGDG 当前提供的版本（同时核对它支持 PG 18，并在锁文件和许可清单中一并修改）；
     - 或改走 §4 的离线路径，从 archive 取包。
3. 锁文件合入 main 后，按正常流程用新的 main 跑发布 CI，再执行 `release.py build`。

**构建需要的出站**：
- **Docker Hub**：经现有加速器 `docker.m.daocloud.io`，只在第 1 步拉取基础镜像时需要；
- **apt.postgresql.org**（HTTP 80 或 HTTPS 443；源地址取基础镜像中 `/etc/apt/sources.list.d/pgdg.list` 的设置）：在线路径需要；
- **deb.debian.org**（bookworm 与 bookworm-updates）和 **security.debian.org**：pgBackRest 的依赖包，例如 libssh2、libyaml，基础镜像中可能没有，在线路径需要。

以上三项都在 `docker build` 的 RUN 步骤中经 docker 的构建网络访问，不经过宿主机的 apt 代理设置。如果 CI 主机上的 docker 也需要通过 apt 镜像源，可以在 `apt-get update` 之前替换源地址，但那样做会改变签名链，需要负责人另行决定。离线路径不需要任何 apt 出站。

## 3. 国内镜像（可选，在线路径）

实测（调度员，2026-10-14，CI 主机）：deb.debian.org 可达但只有约 12 KB/s；apt.postgresql.org、mirrors.aliyun.com、mirrors.tuna.tsinghua.edu.cn 都能连上。因此在线路径可以把 apt 源换成镜像：

| 构建主机环境变量 | 传入的 build-arg | 替换对象（基础镜像中的源） | 例 |
|---|---|---|---|
| `NEXLOOP_APT_DEBIAN_MIRROR` | `APT_DEBIAN_MIRROR` | `http://deb.debian.org/debian` | `https://mirrors.tuna.tsinghua.edu.cn/debian` |
| `NEXLOOP_APT_DEBIAN_SECURITY_MIRROR` | `APT_DEBIAN_SECURITY_MIRROR` | `http://deb.debian.org/debian-security` | `https://mirrors.tuna.tsinghua.edu.cn/debian-security` |
| `NEXLOOP_APT_PGDG_MIRROR` | `APT_PGDG_MIRROR` | `http://apt.postgresql.org/pub/repos/apt/` | `https://mirrors.tuna.tsinghua.edu.cn/postgresql/repos/apt` 或 `https://mirrors.aliyun.com/postgresql/repos/apt` |

用法：`NEXLOOP_APT_PGDG_MIRROR=… NEXLOOP_APT_DEBIAN_MIRROR=… NEXLOOP_APT_DEBIAN_SECURITY_MIRROR=… release.py build …`。三个变量都可选，不设置就使用原始源。

**安全性不变的理由**：
- 只替换源地址的主机和路径。`signed-by` 密钥（PGDG 用基础镜像中的 `postgres.gpg.asc`，Debian 用 `debian-archive-keyring`）和套件名都不变；
- apt 仍然用这些密钥校验 Release 元数据的签名，并通过它校验每个包的 sha256。所以，镜像站提供的内容与原站不一致时，`apt-get update` 或安装会失败，而不会装上不同的包；
- 这些变量是构建主机的环境变量，不写进锁文件。最终镜像由发布清单中的 digest 固定，由 SBOM 记录；
- `release.py` 只接受 http(s) 的主机加路径，含凭据、查询串或空格的值会被拒绝。Dockerfile 内会再检查一次；找不到要替换的源行时构建失败，不会悄悄沿用原始源。

## 4. 在线路径的完整性

- apt 用基础镜像自带的 PGDG 与 Debian 签名密钥，验证 Release 和 Packages 元数据，包的 sha256 由签名元数据保证；
- `pgbackrest=<debian_version>` 精确固定，构建末尾再核对 `dpkg-query` 的版本和 `pgbackrest version` 的输出；
- 依赖包的版本由 bookworm 当时的状态决定，不在锁文件中逐个固定。最终结果由发布镜像的 digest 固定，并由 SBOM 记录。

## 5. 离线路径（apt 不通时）

在一台能访问上述源的机器（管理机）上，**用同一个基础镜像**取包。这样 apt 同样会核对签名，并且只下载基础镜像缺少的依赖：

```bash
mkdir -p debs && docker run --rm --platform linux/amd64 --user root -v "$PWD/debs:/out" docker.io/pgvector/pgvector@<images.postgres.digest> \
  sh -c 'apt-get update && apt-get install --yes --no-install-recommends --download-only "pgbackrest=<debian_version>" && cp /var/cache/apt/archives/*.deb /out/'
cd debs && sha256sum *.deb > SHA256SUMS
```

- PGDG 主仓库没有所需版本时，在容器内把 PGDG 源改为 apt-archive.postgresql.org 的同名套件再取。这一步由负责人确认；
- 把 `debs/`（只含 .deb 和 SHA256SUMS）复制到 CI 主机，核对 SHA256SUMS 与管理机上的一致；
- 在 CI 主机上构建：`NEXLOOP_PGBACKREST_DEB_DIR=<debs 目录> release.py build ...`。
  - 准备脚本会拒绝以下情况：SHA256SUMS 中列出的包缺失或 sha256 不符、目录中有未列出的 .deb；
  - Dockerfile 内会再核对一次 sha256、包的数量、pgbackrest 包的 Version 字段，然后用 dpkg 安装，不访问任何仓库；
  - 依赖不全时，dpkg 会报错，构建失败。

## 6. 对部署的影响（`USER postgres`）

- 官方镜像默认以 root 启动：入口脚本会先 chown 数据目录，再用 gosu 降权。本镜像最后一层是 `USER postgres`（uid 999），入口脚本直接以 postgres 用户运行，不能 chown。
- **因此 DATA_HOST 上，`$NEXLOOP_ROOT/data/postgres` 必须事先属于 uid 999，引导口令的 secret 文件也必须让 uid 999 可读**，否则 PG 无法启动。这一条要写进 NX-032 第 4 步（启动 infra）之前的准备工作。
- 如果负责人不希望改变主机上的属主，可以改回默认的 root 启动方式（去掉最后一行 `USER postgres`）。pgBackRest 照样以 postgres 身份运行（`docker exec -u postgres`，以及 `archive_command` 由 postgres 进程执行）。
- pgBackRest 的配置（`/etc/pgbackrest/pgbackrest.conf`，0600，属主 postgres）在部署时以私有文件挂载，不进镜像。

## 7. 测试

- `tests/test_postgres_image.py`：
  - Dockerfile 的结构：唯一的 FROM、两个版本参数、只安装 pgbackrest、版本核对、最后一层 `USER postgres`、没有 curl 或 wget；
  - release 配置与锁文件一致；
  - 构建参数精确（amd64，`--pull=false`，基础镜像 digest，两个版本）；
  - 清单中有 `nexloop/postgres@<digest>`，其 `base_digest` 是锁文件中的 postgres；
  - `debian_version` 为空时拒绝构建；
  - 离线包的校验；
  - 镜像替换：把 Dockerfile 中的同一段脚本跑在两种格式的临时源文件上，只换主机和路径、密钥不变；不设置时不改动；非 http(s)、含凭据、找不到源行时失败；`release.py` 只从构建主机环境传入，并校验格式。
- `tests/test_deploy_dryrun.py`：data 角色的 `POSTGRES_IMAGE` 映射到发布中的镜像；valkey 仍取锁文件；没有发布、发布中没有 postgres、私有 env 中写了其他 PostgreSQL digest，都会被拒绝；步骤中有 `load_images`。

## 8. 未覆盖

- 真实的 `docker build` 本机没有执行：本机是 arm64，发布镜像只在 CI 主机上构建。
- 锁文件中的 `debian_version` 目前为空，必须在 CI 主机读出后回填。在此之前，`release.py build` 会拒绝构建 postgres 镜像，其他镜像不受影响（可用 `--only`）。
