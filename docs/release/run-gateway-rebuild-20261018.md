# gateway 的 3 个有修复 Critical：重编 Caddy（2026-10-18，分支 l7/gateway-critical）

- **背景**：发布 16（`dd42090f`）在 CI 主机上用 grype 0.120.0 和 v6 库（built 2026-10-17）实扫，gateway 的 `critical_with_fix=3`。public 预检会被这 3 项阻断。
- **执行时机**：仓库改动在分支上。CI 主机上的步骤标为 **[B]**，由调度员执行。开发线未连接任何服务器。

## 1. 是哪几个、在哪个包里

在开发机复现：用 syft 1.18.1 加 grype 0.120.0，库为同一份 v6（built 2026-10-17T06:27:09Z），扫描锁定的 `caddy:2.11.7`（linux/amd64 `sha256:173b2630…`）。结果与 CI 主机一致：

| 级别 | 编号 | 包 | 版本 | 修复版本 |
|---|---|---|---|---|
| Critical | GO-2026-6604 | Go stdlib | go1.26.8 | 1.26.9 |
| Critical | GO-2026-6612 | Go stdlib | go1.26.8 | 1.26.9 |
| Critical | GO-2026-6612 | golang.org/x/net | v0.59.0 | v0.60.0 |

- 3 项都在 **caddy 二进制**里：Caddy 2.11.7 的官方发布二进制是用 go1.26.8 与 x/net v0.59.0 构建的；
- 不在 Alpine 3.23.6 基础层；
- 有修复的 High 还有 14 项：stdlib 9、x/net 4、Alpine zlib `1.3.2-r0` 1。CI 主机报 22 项，差异来自扫描对象不同：CI 扫的是发布归档。这些 High 只列出、不阻断；
- 上游没有更新的 Caddy 版本：2.11.7 发布于 10-03，Docker Hub 上 `caddy:2.11.7` 的 index 仍指向锁定的 amd64 manifest；
- Go 1.26.9 已发布。

因此不是更新基础层就能解决的问题，而是要**换掉 Caddy 二进制的 Go 工具链和 x/net 版本**。

## 2. 修复

gateway 从 `upstream`（只是给锁定的 caddy 镜像重新打标签）改为 `build`：

- `deploy/gateway/caddy/`：`main.go` 就是标准发行版，即官方镜像包含的 `modules/standard`，没有其他模块。`go.mod` 锁定 `caddy/v2 v2.11.7`、`golang.org/x/net v0.60.0`，`go 1.26.9`。
  - `go.sum` 是用 go1.26.9 生成的，经 sumdb 校验，`go mod verify` 通过。
- `deploy/gateway/Dockerfile`：
  - 构建阶段用锁定的 `golang:1.26.9-alpine3.23`，即 `images.go-build-base`，digest `sha256:67503085…a0216c`；
  - 构建时设置 `GOFLAGS=-mod=readonly`、`GOTOOLCHAIN=local`，执行 `go mod verify`，并断言：Go 版本等于锁定值、Caddy 模块版本等于锁定值、产物由 go1.26.9 构建；
  - 编译参数为 `CGO_ENABLED=0 -trimpath -ldflags '-s -w'`；
  - 最终阶段仍是锁定的 `caddy:2.11.7` 镜像，只替换 `/usr/bin/caddy`，并像官方镜像一样执行 `setcap cap_net_bind_service=+ep`。compose 中依赖这个文件能力的说明不变。
- `versions.lock.json`：
  - 新增 `images.go-build-base`；
  - `images.gateway` 增加 `caddy_version` 和 `rebuild` 说明，digest 不变。
- `deploy/release/images.v1.json`：gateway 改为 build，`build_args` 取两个锁定镜像，`tool_args` 取 `GO_VERSION` 和 `CADDY_VERSION`，`mirror_args` 增加可选的 `GOPROXY_MIRROR`。
- `scripts/prepare_gateway_build.py`：构建上下文只包含 Dockerfile 与 3 个模块文件。
- `release.py`：增加 `gateway` 上下文。
  - `pin-upstream` 仍只服务 upstream 类镜像；
  - 以后 gateway 的 digest 与 postgres 一样，改 lock 并经审阅，不再用 `pin-upstream`。
- 测试：
  - 新增 `tests/test_gateway_image.py`，覆盖：
    - lock 中 Go ≥ 1.26.9、x/net ≥ 0.60.0，且与 go.mod/go.sum 一致；
    - 发布配置；
    - Dockerfile 的校验与断言、最终阶段只复制二进制；
    - main 只编进标准模块；
    - 构建上下文的文件清单；
    - release 构建时按 digest 传入两个镜像与两个版本。
  - `test_release_build_archive.py` 中两个以 gateway 为 upstream 示例的测试，改为合成的 upstream 条目。

**开发机实测**（只读，非发布构建）：

- 用 go1.26.9（官方包，sha256 已校验）交叉编译 linux/amd64：`go version -m` 显示 go1.26.9、caddy v2.11.7；syft 识别到 stdlib go1.26.9、x/net v0.60.0，共 163 个包；
- grype 扫描该二进制：**Critical 与 High 均为 0**；
- 对 `deploy/gateway/caddy` 做源码扫描，同样为 0。

## 3. CI 主机 [B]

```bash
# 1) 按 digest 预取构建阶段镜像（caddy 镜像不变，已在本地）
docker pull docker.io/library/golang@sha256:6750308561953d451210ed1a8086d36c8c8bdcbf672d38d9d0d424b8a7a0216c
docker run --rm --network none docker.io/library/golang@sha256:6750308561953d451210ed1a8086d36c8c8bdcbf672d38d9d0d424b8a7a0216c go version   # go1.26.9 linux/amd64
```

**构建时需要的出站**：

- `go mod download` 要访问 Go 模块代理，默认是 `proxy.golang.org`；
- 也可以设 `NEXLOOP_GOPROXY_MIRROR` 指向内部代理。无论哪个代理，模块内容都按仓库中的 go.sum 校验；
- 若 NX-033 防火墙不放行，属于 **[主机配置变更]**。

**构建后核对**：

```bash
docker run --rm --network none --entrypoint sh <gateway 镜像@sha256> -c 'caddy version; getcap /usr/bin/caddy; caddy list-modules | wc -l'
#   预期：v2.11.7 …；/usr/bin/caddy cap_net_bind_service=ep；模块数与 caddy:2.11.7 官方镜像相同（发布 15 的 gateway 上可以用同一命令对照）
for f in <RELEASE_DIR>/sbom/*.findings.json; do echo "== $f"; jq -c '.[] | select(.severity=="Critical" and .fix=="fixed")' "$f"; done   # 预期没有输出
```

**上线前冒烟**：用 stage 现行的 Caddyfile 启动，或者直接按发布流程部署到 stage 后检查 HTTPS 入口。Caddyfile 和配置都不变。

## 4. 附：postgres 基础层 digest 本地缺失但 build rc=0

**结论**：`release.py build` 用的是 `docker build --pull=false --build-arg POSTGRES_IMAGE=docker.io/pgvector/pgvector@sha256:2358fcba…`。

- `FROM` 是按 **digest** 引用的，不可能退回旧层：同一个 digest 只对应同一份内容，要么拿到这份内容，要么构建失败；
- `--pull=false` 的含义只是“不强制刷新本地已有的镜像”。BuildKit 遇到本地没有的基础镜像，**仍会按该 digest 从 registry 拉取**；
- 所以 rc=0 表示构建时按 digest 拉取了 pgvector 0.8.7 镜像。这与运行手册“预取”的约定不同：没有违反完整性，但出站发生在构建时；
- 另外，清单中的 `base_digest` 是从 lock 抄写的，不是观测值。

**核对方法**（CI 主机，只读）：

```bash
# a) 该 digest 现在已在本地（构建时拉取的）
docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}} {{.Created}}' docker.io/pgvector/pgvector@sha256:2358fcba361ed2233a5ed81b5fe4ca779ccb304120ce531a3bf51c0ed7e2bc11
docker image ls --digests pgvector/pgvector
# b) 发布镜像的层以新基础层的层开头（逐层比较 diff_id）
B=$(docker image inspect --format '{{json .RootFS.Layers}}' docker.io/pgvector/pgvector@sha256:2358fcba361ed2233a5ed81b5fe4ca779ccb304120ce531a3bf51c0ed7e2bc11)
R=$(docker image inspect --format '{{json .RootFS.Layers}}' <发布 16 的 nexloop/postgres@sha256>)
python3 -c "import json,sys;b,r=json.loads(sys.argv[1]),json.loads(sys.argv[2]);print('base layers prefix:', r[:len(b)]==b, len(b), len(r))" "$B" "$R"
# c) 镜像内容：PostgreSQL 18.6、pgvector 0.8.7 的扩展文件、清单中的包版本
docker run --rm --network none --entrypoint sh <发布 16 的 nexloop/postgres@sha256> -c 'postgres --version; ls /usr/share/postgresql/18/extension/vector--0.8.7.sql; dpkg-query -W perl-base libpcre2-8-0'
# d) 构建日志（若保留）中 BuildKit 会打印 "resolve docker.io/pgvector/pgvector@sha256:2358…" 与下载记录
```

- b 输出 `True`，且 c 显示 18.6、0.8.7，就说明用的是新基础层；
- b 输出 `False` 就停下，交给 L7。

**建议**（未改代码，需调度员决定）：在 `release.build` 中，对每个 `build_args` 基础镜像先执行 `docker image inspect <repo>@<digest>`，本地没有就拒绝，与 upstream 镜像“预取后才构建”的规则一致。这样构建时就不会联网拉取。代价是每次换基础层都必须先预取。
