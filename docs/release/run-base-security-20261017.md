# agent-host 与 postgres 基础层安全更新命令单（2026-10-17，草案）

- **背景**：把 grype 升到 0.120.0、改用 v6 漏洞库后（`run-grype-v6-20261017.md`），agent-host 的 Node 基础层与 postgres 的 pgvector 基础层出现了“有修复的 Critical”，public 发布会被阻断。本单把它们消除。
- **执行时机**：仓库改动在分支上，AT-015（10/20）之后随发布合入。CI 主机与 DATA_HOST 上的步骤标为 **[B]**，由负责人执行或逐步授权。开发线未连接任何服务器，也没有构建镜像，因为开发机的 Docker 守护进程没有运行。

## 1. 结论与改动

**上游最新的镜像也没有修复这些包**（2026-10-17 用 syft 1.18.1、grype 0.120.0 和 v6.1.10 库实测）：

- `node:24.22.0-bookworm-slim`、`24.21.0` 仍带 `perl-base 5.36.0-7+deb12u3`，修复版本是 deb12u4；
- `pgvector:0.8.7-pg18-bookworm` 仍带 perl 系列 deb12u3；它的 python3.11 是 deb12u8（修复版本 deb12u9），但只是卸载后残留的配置文件（dpkg 状态 `rc`，没有程序）。

所以只换基础镜像不够。做法是：**在 Dockerfile 里按 `versions.lock.json` 中精确到版本的 `package=version` 清单，只升级这些包**。清单中的版本都已在 Debian `bookworm` 或 `bookworm-security` 的 Packages 索引中确认存在：bookworm-security 的 InRelease 日期为 2026-10-16，main 索引同日读取。

| 镜像 | 基础层 | 安全更新（`security_updates`） | 其他 |
|---|---|---|---|
| agent-host | `node-host-base` **不变**（`node:24.13.0-bookworm-slim`，digest 不变） | perl-base `5.36.0-7+deb12u4`、libgnutls30 `3.7.9-2+deb12u7`、libc6 与 libc-bin `2.36-9+deb12u14`、libcap2 `1:2.66-4+deb12u3+b1`、libpcre2-8-0 `10.42-1+deb12u2`、dpkg `1.21.23` | Node 运行时仍是 24.13.0，与 Host 的构建和测试版本（`.nvmrc`、`prepare_host_build.py`）一致。Node 24.13.0 本身有修复的 High 只列出、不阻断；升级 Node 涉及所有开发线与 CI，另立任务 |
| postgres | `pgvector:0.8.5-pg18-bookworm` → **`0.8.7-pg18-bookworm`**，digest `sha256:2358fcba361ed2233a5ed81b5fe4ca779ccb304120ce531a3bf51c0ed7e2bc11`；PostgreSQL 18.4 → 18.6 | perl-base、perl、perl-modules-5.36、libperl5.36 `5.36.0-7+deb12u4`；libpcre2-8-0 `10.42-1+deb12u2` | 清除 `rc` 状态的残留包，即 python3.11 的配置文件。新基础层已带 openssl `3.0.22-1~deb12u1`，旧基础层是 3.0.20 |

**兼容性**（未跨大版本，不需要停下）：

- PostgreSQL 18.4 → 18.6 是同一大版本内的小版本升级，数据目录格式不变，换镜像后重启即可；
- pgvector 0.8.5 → 0.8.7：上游 CHANGELOG 中 0.8.6 与 0.8.7 都只有缺陷修复，没有磁盘格式变化；
- 已有库中扩展的目录版本保持 0.8.5，直到执行 `ALTER EXTENSION vector UPDATE`。共享库是 0.8.7，与 0.8.5 的 SQL 定义兼容。本单**不执行**该语句；如需执行，作为单独的 B 类步骤，在备份后进行；
- 迁移 `0067_nx021_recall_index.sql` 只做 `create extension vector`，不指定版本。

**仓库改动**：

- `versions.lock.json`：
  - `images.postgres` 改为 0.8.7 的 digest，并写明版本；旧值记在 `previous` 中；
  - `images.postgres` 与 `images.node-host-base` 都新增 `security_updates` 和 `security_reason`。
- `deploy/release/images.v1.json`：两个镜像新增 `security_args`；agent-host 新增 `mirror_args`（Debian 与 Debian security）。
- `scripts/release/release.py`：新增 `pinned_package_list()`，把清单作为一个构建参数 `SECURITY_UPDATES` 传入。清单缺失、格式不对、同一个包重复出现，或含有其他字符，都会拒绝构建。
- `deploy/community/Host.Dockerfile`：
  - 在 Node 阶段的第一个 RUN 中执行 `apt-get update` 和 `apt-get install --only-upgrade ${SECURITY_UPDATES}`；
  - 逐项断言每个包都是 `ii` 状态且版本与清单完全一致；
  - 清理 apt 列表；
  - 可选镜像源的处理与 postgres 相同，apt 仍校验签名。
- `deploy/postgres/Dockerfile`：
  - 在线路径在安装 pgBackRest 之后执行同样的 `--only-upgrade`；
  - 离线路径要求 `debs/` 中也包含这些包（§3.2）；
  - 两条路径之后都逐项断言版本，并 `dpkg --purge` 所有 `rc` 状态的残留包。
- 测试：
  - 新增 `tests/test_base_security_updates.py`，覆盖：
    - 每个镜像取自己基础层的清单；
    - PostgreSQL 仍是 18、pgvector 仍是 0.8.x；
    - 7 种缺失或畸形的清单被拒；
    - 两个 Dockerfile 只做 `--only-upgrade`，没有 `apt-get upgrade`；
    - 用模拟的 `dpkg-query` 实际运行两个 Dockerfile 里的断言循环（正确版本、未升级、版本更高、只剩配置文件、未安装，五种情况）；
    - 所有 RUN 步骤能通过 `sh -n` 检查；
  - `test_postgres_image.py` 相应更新。

**模拟结果**：把清单与残留清理应用到基础层的 SBOM 上（只改包版本、去掉残留包；**不是**真实构建），再用 grype 0.120.0 扫描，按 public 类别判定：

- agent-host：`blocking` 为空。列出的是 node 24.13.0 本身的 6 个 High（不阻断），以及 libcap2 的一项。后者是模拟造成的：SBOM 中的源码包版本没有随之修改，真实构建后应当消失；
- postgres：`blocking` 为空，`listed` 为空，sqlite 的 CVE-2025-7458 豁免仍然生效；
- 模拟时忽略了 npm 的发现（Host 镜像删除了 npm）和 Go stdlib 的发现（postgres 镜像删除了 gosu）。

## 2. 许可证与 SBOM

- `scripts/check_licenses.py` 检查的是源码依赖（uv.lock、pnpm-lock），不涉及镜像里的 Debian 包。本次没有改动许可证策略和源码依赖，所以 THIRD_PARTY_NOTICES 不需要重生；`licenses.sh` 的 rc 为 0。
- 镜像 SBOM 在发布时由 `release.py sbom` 生成，仓库中不保存。下一次发布时，backend 不变；agent-host 与 postgres 的 SBOM 会因包版本变化而变化。

## 3. CI 主机 [B]

### 3.1 预取新的 pgvector 基础镜像（按 digest，从不按标签拉取）

```
docker pull docker.io/pgvector/pgvector@sha256:2358fcba361ed2233a5ed81b5fe4ca779ccb304120ce531a3bf51c0ed7e2bc11
docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' docker.io/pgvector/pgvector@sha256:2358fcba361ed2233a5ed81b5fe4ca779ccb304120ce531a3bf51c0ed7e2bc11
# linux/amd64 平台 manifest 应为 sha256:ad249f9fb9572979643a47694e59b9a8cdcdf6abc6accebf49bb80ad5f20ff9b（开发机读取 registry 所得）
docker run --rm --network none --entrypoint sh docker.io/pgvector/pgvector@sha256:2358fcba361ed2233a5ed81b5fe4ca779ccb304120ce531a3bf51c0ed7e2bc11 -c 'postgres --version; ls /usr/share/postgresql/18/extension/vector--0.8.7.sql'
```

- `nexloop-ci` 的出站受 NX-033 防火墙限制。如果无法拉取，按 `NX-034-first-release-runbook.md` 第 6 步，由负责人在其他机器上 `docker save` 后交给 CI 主机 `docker load`。
- `node-host-base` 与 `python-build-base` 不变。

### 3.2 构建时的包来源

- **在线**（默认）：构建 agent-host 与 postgres 时，apt 需要访问 Debian 与 Debian security，postgres 另外需要 PGDG。这和现在 postgres 的构建方式相同。镜像源可以通过 `NEXLOOP_APT_DEBIAN_MIRROR`、`NEXLOOP_APT_DEBIAN_SECURITY_MIRROR` 指定，签名校验不变。
- **离线**（只有 postgres 支持）：`NEXLOOP_PGBACKREST_DEB_DIR` 指向的目录里，除了 pgBackRest 及其依赖，还要放入以下 5 个包，并写进同一个 SHA256SUMS。下表的 sha256 取自 2026-10-16 的 bookworm-security Packages 索引，下载后要逐个核对：

| 文件（`pool/updates/main/...`） | sha256 |
|---|---|
| `perl-base_5.36.0-7+deb12u4_amd64.deb` | `d7d1943aec9597629bf73075efcc5ef6dc9bda96d78e80d843156ecd448478b8` |
| `perl_5.36.0-7+deb12u4_amd64.deb` | `67e7a883c5d835939638f89a750799282df243ac1446281aa92ef9e19300940b` |
| `perl-modules-5.36_5.36.0-7+deb12u4_all.deb` | `3ed38ffa8320e9bf7597cefa01b6a6ace4a08ccc77cdfd15ef7d8b711dea7166` |
| `libperl5.36_5.36.0-7+deb12u4_amd64.deb` | `ed8480b621ad574c25aea30d36194d0133ea74093756ecbe3dff157d0510ef7f` |
| `libpcre2-8-0_10.42-1+deb12u2_amd64.deb` | `d2f7edfcc7689b9e0761c2742cc824ac86a20768bc5e8057818dc6875291fe76` |

- agent-host 没有离线路径。CI 主机无法访问 Debian 源时，需要负责人指定一个镜像源，属于 **[主机配置变更]**。

### 3.3 构建后核对

```
# agent-host：Node 不变，清单中的包都已升级，npm 仍然不存在
docker run --rm --network none --entrypoint sh <agent-host 镜像@sha256> -c 'node -v; python3 -V; dpkg-query -W perl-base libgnutls30 libc6 libc-bin libcap2 libpcre2-8-0 dpkg; ! command -v npm'
#   预期：v24.13.0、Python 3.12.15，各包版本与 versions.lock.json 一致
# postgres：版本、扩展、没有 rc 状态的残留、没有 gosu
docker run --rm --network none --entrypoint sh <postgres 镜像@sha256> -c 'postgres --version; pgbackrest version; dpkg-query -W perl-base perl libpcre2-8-0; dpkg -l | awk "\$1==\"rc\"" | wc -l; ! command -v gosu'
#   预期：PostgreSQL 18.6、锁定的 pgBackRest 版本、各包版本与清单一致、rc 数量为 0
```

接着运行 `release.py sbom`，然后核对各镜像的 Critical 发现：

```
for f in sbom/*.findings.json; do echo "== $f"; jq -c '.[] | select(.severity=="Critical" and .fix=="fixed")' "$f"; done
```

- 预期没有输出；
- 再运行 `release.py public-preflight`，第 8 步的 `blocking` 应为空；
- 若仍有输出，把编号和包整理出来，补充清单后重建。**不要**为有修复的 Critical 写豁免。

## 4. DATA_HOST 上线 [B]（随 AT-015 之后的发布）

PostgreSQL 小版本升级，数据目录不变：

1. **前置**：
   - 当天有一次已验证的 pgBackRest 备份：`nexloop-doctor --backup` 各项为 true，按 `run-46` 第 7.4 步执行；
   - 最近一次恢复演练有效。
2. **上线**：按现行发布与部署手册，用新的 `nexloop/postgres@sha256:…` 重建 postgres 容器（`run-release7-deploy-20261016.md` 的 compose 流程）。只换镜像，卷与配置不变。
3. **验证**：
   - `select version()` 为 18.6；
   - `select extversion from pg_extension where extname='vector'` 仍为 `0.8.5`（预期不变）；
   - 召回查询冒烟检查；
   - `nexloop-doctor --backup` 与指标门禁按现行手册执行；
   - pgBackRest 的 `check` 通过。
4. **回退**：用上一个发布的 postgres 镜像 digest 重建容器。小版本可以回退，数据目录兼容。
