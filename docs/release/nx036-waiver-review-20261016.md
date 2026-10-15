# NX-036 漏洞豁免审阅材料（供负责人决定，2026-10-16）

- **对象**：`deploy/compliance/vulnerability-waivers.proposed.v1.json` 中的 5 条豁免草稿（D6）。它们都还**未生效**：`release.py` 不读这个文件，现行策略 `vulnerability-policy.v1.json` 的 `waivers` 为空。
- **对照**：发布 `20261015-4736ea179063-1` 的扫描结论（调度员提供）：
  - Critical：backend 2 项、agent-host 1 项、postgres 2 项，`critical_with_fix` 均为 0；
  - stage 策略下 `CVE-2025-13836` 列为 listed，即有修复的 High，只列出不阻断。
- **依据**：`NX-036-design.md` 的 D5（阈值）、D6（豁免，最长 90 天）、D12（public 发布要求漏洞库不超过 7 天），以及 `NX-036-vuln-remediation.md`（首个签名发布的处置记录）。
- 本文只用角色名与编号，不含漏洞描述原文。凡写“需在 CI 主机读取”的，由调度员按 §3 的命令执行，并把结论回填到本文。

## 1. 逐项审阅

5 条草稿实际是两个 CVE，分别作用于 3 个和 2 个镜像。扫描数恰好对应：backend 2 = zlib + sqlite，agent-host 1 = zlib，postgres 2 = zlib + sqlite。这一对应关系需按 §3 第 1 条核对。

### 1.1 CVE-2023-45853 × 3（zlib1g `1:1.2.13.dfsg-1`；backend、agent-host、postgres）

| 项 | 内容 |
|---|---|
| 严重级别 | Critical |
| 修复版本 | 无。Debian bookworm 标为 wont-fix，grype 的 fix state 为 `wont-fix` |
| 漏洞位置 | minizip（`zipOpenNewFileInZip4_64`），即 zlib 源码树 `contrib/minizip` 中的 zip 写入库 |
| 是否可达 | **不可达**（以代码与包清单判断）：<br>1. Debian 的 `zlib1g` 二进制包只含 libz，minizip 在另一个包 `libminizip1` 中；<br>2. 三个镜像的 Dockerfile 都没有安装 `libminizip1`、`unzip` 或 `zip`：`deploy/community/Dockerfile`、`Host.Dockerfile` 和 `deploy/postgres/Dockerfile` 只装了 pgBackRest；<br>3. NexLoop 的代码不生成 zip 文件。Python 的 `zipfile` 与 Node 都不链接 minizip |
| 运行时暴露面 | 无。没有任何服务接收外部 zip 并用 minizip 写出 |
| 待核实 | SBOM 中确实没有 minizip 类包（§3 第 2 条） |
| 建议 | **接受豁免**，到期日 2027-01-12（草稿值，自 10-14 起 90 天，D6 上限）。到期前若 Debian 出修复，就升级基础镜像并删除豁免 |
| 理由 | 漏洞代码不在镜像中；无修复版本；升级同代基础镜像也无法消除（bookworm 全系列都是这个版本）。换到 trixie 基础镜像属于大改动，不在试运行前做 |

### 1.2 CVE-2025-7458 × 2（libsqlite3-0 `3.40.1-2+deb12u2`；backend、postgres）

| 项 | 内容 |
|---|---|
| 严重级别 | Critical |
| 修复版本 | 无。Debian bookworm 为 wont-fix |
| 触发条件 | 需要攻击者能让程序执行其构造的 SQL 语句。草稿理由如此；以官方公告为准，§3 第 1 条一并核对 |
| backend 是否可达 | **不可达，但草稿理由需要更正**：<br>1. 草稿说 backend 不执行 SQLite 语句。实际上 backend 的 Python 包中有两处用到 `sqlite3`：<br>&nbsp;&nbsp;• `nexloop_eios/file_backup.py`：以只读、`immutable=1` 打开 Host 的 `runtime.sqlite`，只执行固定的 `pragma integrity_check`；<br>&nbsp;&nbsp;• `apps/reference-endpoint/reference_endpoint.py`：参考端点自己的库，只执行固定的建表、查询和 pragma，不拼接请求内容。<br>2. 这两处都不在 backend 容器里运行：<br>&nbsp;&nbsp;• `nexloop-file-backup` 由主机上的备份 venv 执行（`deploy/stage/systemd/README.md`），stage compose 中没有该服务；<br>&nbsp;&nbsp;• 参考端点是另一个镜像（`apps/reference-endpoint` 以 `PYTHON_IMAGE` 构建，`deploy/stage/reference-endpoint`），不在发布扫描的镜像清单里，但它的基础层同样带 `libsqlite3-0 deb12u2`。<br>3. 两处的 SQL 都是代码中的常量，没有一处执行来自请求的 SQL |
| postgres 是否可达 | **不可达**：PostgreSQL、pgvector、pgBackRest 都不执行 SQLite 语句；库来自基础镜像的系统包 |
| agent-host 说明 | agent-host 没有这一项发现，但 Pi Durable 用的是 `node:sqlite`，即 Node 二进制内置的 SQLite（`vendor/pi/packages/durable/src/storage/sqlite/node.ts`）。grype 按系统包识别，不会把它记成 `libsqlite3-0`。它的版本需要单独确认（§3 第 3 条）：若低于本 CVE 的修复版本，就要单独评估（该路径执行的是固定 SQL） |
| 建议 | **接受豁免，同时更正理由**。到期日 2027-01-12。backend 一条的 `reason` 建议改为：“backend 镜像中没有服务执行 SQLite；包内用到 sqlite3 的 file_backup 由主机 venv 运行，且只执行固定的 pragma；参考端点是另一个镜像，也只执行固定 SQL。无攻击者控制的 SQL” |
| 理由 | 无修复版本；没有攻击者可控的 SQL 入口；草稿原理由不准确，批准前应改正，避免审计时被认为不实 |

## 2. 不是豁免项，但需要决定：CVE-2025-13836（stage listed）

| 项 | 内容 |
|---|---|
| 性质 | 有修复的 High，在 stage 策略中列出（listed）。public 策略下同样只列出、不阻断，因此不需要豁免 |
| 涉及镜像与组件 | **需在 CI 主机读取**（§3 第 1 条），不要猜测：<br>• 可能是 backend、agent-host 的 Python（3.12.15，复制到 agent-host 的 `/usr/local`）；<br>• 也可能是 postgres 镜像里 Debian 的 `python3.11-minimal` |
| 漏洞位置 | CPython `http.client` 读取响应时，未指定读取量就按 `Content-Length` 读取，恶意服务器可借此消耗内存。以 CPython 安全公告为准，需核对 |
| 在 NexLoop 中是否可达 | backend 中用 `http.client` 读取响应的调用逐处核对如下，除一处外，读取量都有上限：<br>• `effect_provider.py:228`：`read(16385)`；<br>• `embedding_provider.py:67/69`：`read(4 MiB)`、`read(64 KiB)`；<br>• `host_control.py:55/85`：`read(4097)`；<br>• `runtime_dispatch.py:112`：`read(65537)`；<br>• `container_entrypoint.py:78`：`read(65537)`；<br>• **例外：`retention.py:97` 为 `response.read()`**，对端是 Agent Host 的回环端口，属于受信方。<br>外部对端（企业端点、embedding 服务）的读取都有上限，所以外部可达性很低 |
| 建议 | **不豁免，安排升级**：Python 3.12 的下一个含修复的补丁版本发布后，更新 `python-build-base`。另起一个小改动，给 `retention.py:97` 加上读取上限，与其他调用一致 |
| 理由 | 有修复版本，升级是正路；当前外部路径已有上限，试运行期间风险可控 |

## 3. 需在 CI 主机执行的读取（调度员）

在发布目录 `$RELEASE_ROOT/releases/20261015-4736ea179063-1/` 下执行，只读：

```
# 1) 各镜像的 Critical，以及 CVE-2025-13836 所在的镜像与包（只含编号、包、版本、严重级别、修复状态）
for f in sbom/*.findings.json; do echo "== $f"; jq -c '.[] | select(.severity=="Critical" or .id=="CVE-2025-13836")' "$f"; done
# 2) minizip 类包不在任何镜像的 SBOM 中（预期无输出）
for f in sbom/*.spdx.json; do jq -r --arg f "$f" '.packages[] | select(.name|test("minizip|^unzip$|^zip$";"i")) | "\($f) \(.name) \(.versionInfo)"' "$f"; done
# 3) agent-host 内置 SQLite 的版本（镜像按 digest 引用，不联网）
docker run --rm --network none --entrypoint node "<agent-host 镜像@sha256>" -p process.versions.sqlite
```

- 第 1 条若显示 Critical 与草稿不是一一对应，例如包版本不同、多出或少了项，就停下来，先改草稿的 `version_pattern` 或补充分析，再提请批准。

## 4. 决策表（一页）

| # | CVE | 镜像 / 组件 | 级别 | 有修复 | 可达 | 推荐 | 到期 / 动作 |
|---|---|---|---|---|---|---|---|
| 1 | CVE-2023-45853 | backend / zlib1g | Critical | 否 | 否（minizip 不在镜像中） | **接受豁免** | 2027-01-12 |
| 2 | CVE-2023-45853 | agent-host / zlib1g | Critical | 否 | 否 | **接受豁免** | 2027-01-12 |
| 3 | CVE-2023-45853 | postgres / zlib1g | Critical | 否 | 否 | **接受豁免** | 2027-01-12 |
| 4 | CVE-2025-7458 | backend / libsqlite3-0 | Critical | 否 | 否（容器内无 SQLite 执行；包内两处只用固定 SQL，且都不在该容器运行） | **接受豁免，先改 reason** | 2027-01-12 |
| 5 | CVE-2025-7458 | postgres / libsqlite3-0 | Critical | 否 | 否 | **接受豁免** | 2027-01-12 |
| — | CVE-2025-13836 | 待 §3 第 1 条确认 | High | 是 | 外部路径的读取都有上限；`retention.py` 回环例外 | **不豁免：升级 Python，并给 retention 读取加上限** | 下一个含修复的补丁版本 |

**批准后的操作**（调度员）：
1. 把草稿第 4 条的 `reason` 按 §1.2 修改；
2. 把每条的 `approved_by` 换成负责人决定的引用，例如 `owner 2026-10-16 NX-036 D6`；
3. 原样移入 `vulnerability-policy.v1.json` 的 `waivers`；
4. 运行 `release.py public-preflight`，第 8 步应通过。public 发布前还要满足 D12（漏洞库不超过 7 天）。
