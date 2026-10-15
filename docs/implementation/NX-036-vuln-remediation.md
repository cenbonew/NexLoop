# NX-036 首个签名发布的漏洞处置

## 范围与依据

- 对象：发布 `20261014-241993b9cc12-1` 的 grype 发现。原件在私有附件中，只读，未提交。本文只记录编号、包和版本。
- 依据：`NX-036-design.md` 中的 D5（阈值）、D6（豁免）、D12（漏洞库时效），以及 `deploy/compliance/vulnerability-policy.v1.json`。
- 分支：`l4/nx036-vuln-fix`，从 main `cbe1b17` 切出。

## 新 digest 的来源

新 digest 都是在开发机上以匿名只读方式读 Docker Hub registry 得到的，没有连接项目的任何服务器。

| 镜像 | 核对内容 | 结果 |
|---|---|---|
| `docker.io/library/python:3.12.15-slim-bookworm` | index digest | `sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258` |
| | config | 创建于 2026-10-06，`PYTHON_VERSION=3.12.15` |
| | linux/amd64 前三层的 dpkg status（逐层下载后核对 sha256） | libssl3/openssl `3.0.22-1~deb12u1`、libsqlite3-0 `3.40.1-2+deb12u2`、libc6/libc-bin `2.36-9+deb12u14`、libpam* `1.5.2-6+deb12u2`、libgnutls30 `3.7.9-2+deb12u7`、perl-base `5.36.0-7+deb12u3`、gpgv `2.2.40-1.1+deb12u2`、zlib1g `1:1.2.13.dfsg-1`（未变） |
| `docker.io/pgvector/pgvector:0.8.5-pg18-bookworm`（现锁 digest，未改） | config | `GOSU_VERSION=1.19` |
| | 含 `/usr/local/bin/gosu` 的层（`sha256:0c3aadba…`） | 二进制内嵌 `go1.24.6` |
| 上游较新的 `pgvector:0.8.7-pg18-bookworm`、`postgres:18.6-bookworm` | config | 仍是 `GOSU_VERSION=1.19` |
| `docker.io/library/node:24.21.0-bookworm-slim`（仅作对照，未采用） | npm 层 | npm 11.19.0 自带 tar 7.5.19、minimatch 10.2.5、glob 13.0.6 |

结论：

- grype 发现文件里没有记录位置，而 postgres 镜像中带 Go 构建信息的二进制就是 gosu。为此只下载了 gosu 所在的层；postgres、pgvector 自身的各层没有逐层扫描。
- 升级上游镜像不能消除 gosu 的发现，因为上游新版本仍是 gosu 1.19。

## 改动

1. `versions.lock.json`：`images.python-build-base` 改为 3.12.15-slim-bookworm，使用上面的 digest。旧值记在 `replaces` 中；`status` 写明“registry 读取、未经构建验证”。backend 用它作最终镜像；agent-host 从它复制 `/usr/local`。
2. `deploy/community/Host.Dockerfile`：删除 Node 基础镜像自带的 npm、npx 和 corepack，并在构建时断言它们已不存在。Host 运行时只用 `node` 和 `python3`，仓库代码不调用它们。`node-host-base` 保持 24.13.0 不变，构建 Host 用的 Node 版本也不变。
3. `deploy/postgres/Dockerfile`：删除 `/usr/local/bin/gosu`，并断言它已不存在。上游 entrypoint 只在以 root 启动时才用 gosu 降权；本镜像固定以 `USER postgres` 运行，stage compose 也没有设置 user。改动后，以 root 启动会直接失败，而不是降权后继续运行。
4. `deploy/compliance/vulnerability-waivers.proposed.v1.json`：wont-fix 的 Critical 发现的豁免草稿，字段与策略文件完全一致，期限 2026-10-14 至 2027-01-12（90 天）。
   - `approved_by` 写的是“待负责人决定”。`release.py` 不读这个文件；负责人批准后，把 `approved_by` 换成决定引用，再原样移入 `vulnerability-policy.v1.json`。
   - 测试 `test_the_waiver_proposals_have_the_policy_shape_and_are_not_in_effect` 核对两点：每条草稿格式合法，并且恰好匹配它所指的发现；草稿没有进入现行策略。

## 处置表（Critical）

| CVE | 镜像 / 包 | 修复状态 | 处置 | 依据 |
|---|---|---|---|---|
| CVE-2025-15467 | backend / libssl3, openssl 3.0.16-1~deb12u1 | fixed | 升级基础镜像，新版本为 3.0.22-1~deb12u1 | 新层 dpkg status |
| CVE-2025-4517 | backend, agent-host / python 3.12.10 | fixed | 升级基础镜像到 Python 3.12.15 | config 中的 `PYTHON_VERSION` |
| CVE-2025-6965 | backend / libsqlite3-0 3.40.1-2+deb12u1 | fixed | 升级基础镜像，新版本为 deb12u2 | 新层 dpkg status；postgres 镜像已是 deb12u2，扫描中也没有这一项 |
| CVE-2025-68121 | postgres / Go stdlib go1.24.6，即 gosu 1.19 | fixed（上游 gosu 尚未重编） | 从镜像删除 gosu | gosu 所在层的 Go 版本；compose 与 Dockerfile 中的运行用户 |
| CVE-2025-7458 | backend、postgres / libsqlite3-0 deb12u2 | wont-fix | 豁免草稿（待批准） | Debian bookworm 无修复；需要攻击者控制的 SQL；这两个镜像都不执行 SQLite 语句 |
| CVE-2023-45853 | backend、agent-host、postgres / zlib1g 1:1.2.13.dfsg-1 | wont-fix | 豁免草稿（待批准） | 漏洞在 minizip，zlib1g 不包含它，镜像中也没有使用它的程序 |

backend 镜像里的 CVE-2025-7458 原本是 deb12u1。升级后预计变为 deb12u2，所以草稿的版本模式按 deb12u2 写。如果重扫显示版本不同，需要相应修改草稿。

## 处置表（High）

**有修复的 High（public 发布只列出，不阻断）：**

| 镜像 | 包 / 编号 | 处置 |
|---|---|---|
| backend | python 3.12.10：CVE-2025-13836、4138、4330、4435、8194 | Python 3.12.15 |
| backend | libssl3/openssl：CVE-2025-69419、69420、69421、9230 | 3.0.22-1~deb12u1 |
| backend | libc6/libc-bin：CVE-2025-4802 | deb12u14 |
| backend | libpam*：CVE-2025-6020 | deb12u2 |
| backend | libgnutls30：CVE-2025-32988、32990 | deb12u7 |
| backend | perl-base：CVE-2023-31484 | deb12u3 |
| backend | gpgv：CVE-2025-68973 | deb12u2 |
| agent-host | python 3.12.10：同上 5 个 | 随 python-build-base 升级 |
| agent-host | npm 自带的 minimatch、glob、tar、@isaacs/brace-expansion（11 个 GHSA） | 删除 npm 和 corepack |
| postgres | Go stdlib：CVE-2025-58187、58188、61723、61725、61726、61729、61731、61732 | 删除 gosu |

**wont-fix 的 High（共 36 个，策略不阻断也不列出）：**

| 镜像 | 包（发现数） |
|---|---|
| backend | libc6/libc-bin（3+3）、libtasn1-6（1）、dpkg（1） |
| agent-host | libc6/libc-bin（3+3）、libtasn1-6（1）、dpkg（1） |
| postgres | libc6/libc-bin/libc-l10n/locales（各 3）、python3.11-minimal/libpython3.11-minimal（各 2）、libldap-2.5-0（1）、libtasn1-6（1）、dpkg/dpkg-dev（各 1） |

这些都是 Debian bookworm 中没有修复的基础系统包。D5 只列出有修复的 High，所以它们本来就不会出现在确认摘要里。这次把它们记录在这里，避免被静默放过。没有为它们写豁免草稿，因为逐条写理由需要逐个分析可达性；是否需要豁免，由负责人决定。

gateway 只有 3 个 Medium（修复状态 unknown），不处理。

## 下一次发布构建要验证的事项（调度员在 CI 主机上执行）

1. 按 digest 预取 `docker.io/library/python@sha256:34386ef0…`（从不按标签拉取），并核对 linux/amd64 platform manifest 是 `sha256:` 开头且与 index 一致。
2. 重建发布，确认以下几点：
   - backend、agent-host 中 `python3 --version` 为 3.12.15；
   - agent-host 中没有 `npm`、`npx`、`corepack`，而 `node` 仍是 24.13.0；
   - postgres 镜像中没有 `gosu`，并能以 postgres 用户正常启动。
3. 重新扫描。预期：
   - 有修复的 Critical 为 0。剩下的 5 个 Critical 都是 wont-fix（zlib1g ×3、libsqlite3-0 ×2），由负责人决定是否批准豁免草稿；
   - 有修复的 High 下降。如果仍有剩余（例如 Node 基础镜像中的 Debian 包），逐条列出，再决定是否升级 `node-host-base`。
4. 运行 Host 与 Pi 的真实用例，以及 community Host 镜像的运行时检查，确认去掉 npm 后 Host 正常。
5. public 发布前：grype 数据库不超过 7 天（D12）；批准的豁免移入策略后，`release.py public-preflight` 第 8 步通过。

## 2026-10-15：豁免批准与试运行后的待办

**豁免已生效**：
- 负责人 2026-10-15 批准五条豁免草稿：CVE-2023-45853 zlib1g（backend、agent-host、postgres）与 CVE-2025-7458 libsqlite3-0（backend、postgres）。审阅材料见 `docs/release/nx036-waiver-review-20261016.md`；
- 五条豁免已移入 `deploy/compliance/vulnerability-policy.v1.json`；`approved_by` 写决定引用；backend 那条 libsqlite3-0 的 reason 按审阅材料 §1.2 更正；
- 草稿文件已清空；
- 期限都是 2026-10-14 至 2027-01-12（90 天，D6 上限）。

**第 8 步（`vulnerabilities`）在发布 `20261015-4736ea179063-1` 上的预期**，D12 的漏洞库年龄另计：
- 五个 wont-fix 的 Critical 都被豁免：`waived` 为 CVE-2023-45853 与 CVE-2025-7458，`cleanup` 为空，即每条豁免都用上了；
- 有修复的 Critical 为 0，`blocking` 为空；
- 有修复的 High 只列出：`listed` 至少含 CVE-2025-13836（backend、agent-host 的 python 3.12.15）；
- 不阻断。

**试运行后待办**（这里没有修改 planning，请由调度员同步到 ROADMAP 的待办）：

1. **CVE-2025-13836**（High，有修复，不豁免）：
   - Python 3.12 出现含修复的补丁版本后，更新 `versions.lock.json` 的 `python-build-base`，按本文"下一次发布构建要验证的事项"重建与重扫；
   - postgres 镜像中的 `python3.11-minimal` 为 wont-fix（Debian），不可达，D5 下不列出。
2. **`retention.py:97`**：把 `response.read()` 改为有上限的读取，与其他 `http.client` 调用一致。对端是 Agent Host 回环，属受信方，这样改只是对齐做法。
3. **豁免到期 2027-01-12**：
   - 到期后，`_waiver_problem` 判为 expired，stage 与 public 的检查都会失败；
   - 到期前要复核：Debian 是否已出修复。有修复就升级并删除豁免；仍无修复且仍不可达，就由负责人续批新的 90 天。
