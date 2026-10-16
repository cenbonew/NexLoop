# 主机侧 Python 3.12.10 → 3.12.15 升级命令单，附 grype 漏洞库处置建议（2026-10-17，草案）

- **背景**：CVE-2025-13836（CPython `http.client` 读取缓冲，High）的受影响范围是 `3.12.0 ≤ v < 3.12.13`。发布镜像已经是 3.12.15，并带构建期下限断言，见 `docs/implementation/NX-036-vuln-remediation.md` 末节。镜像之外，开发机、CI 主机、APP_HOST、DATA_HOST 上还有 uv 管理的 CPython 3.12.10 或它的拷贝。本单把它们升到 3.12.15。
- **执行时机**：AT-015（10/20）之后。stage 试运行期间不动主机。
- **分类**：
  - 标 **[B]** 的步骤改动主机，由负责人本人执行或逐步授权（`docs/OPERATIONS.md` §B 类）；
  - 标 **[只读]** 的步骤是盘点，不改动任何东西；
  - 标 **[A]** 的是仓库内改动或开发机操作。
- **编写方式**：本单由开发线 L7 编写，未连接任何服务器。主机上的现状来自仓库文档，凡是没有记录的都写成“待盘点”。
- **路径约定**：路径与 `deploy/stage/systemd/*.example`、`docs/release/run-33-20261015.md` 一致。实际值以负责人私有 env 为准。

## 0. 结论先行

1. **uv 版本是根因**：
   - 开发机的 uv 是 0.7.8，它的托管 Python 下载表里 3.12 最高只有 3.12.10。`.python-version` 写的是 `3.12`，uv 优先用托管解释器，所以一直落在 3.12.10。即使 Homebrew 已经装了 3.12.13，uv 也不会用它；
   - 能下载 3.12.15 的第一个 uv 版本是 **0.12.22**（2026-10-02 发布，python-build-standalone `20261001`）。0.12.21 最高只到 3.12.14。
2. **主机侧升级都要“并排新建、核对、再切换”**：
   - venv 里写死了解释器的绝对路径，只替换解释器目录不够，venv 必须重建；
   - 旧目录改名保留，用于回退。
3. **grype 0.86.1 的漏洞库已经停更**（见 §6）：
   - 它只能用 schema v5 的库，而 v5 的库最后一次构建是 **2026-03-09**；
   - 后果一：D12（public 发布要求库龄不超过 7 天）在这个版本下**永远无法满足**；
   - 后果二：发现的数据停在 3 月，既有误报，也会漏掉 3 月以后公布的漏洞；
   - 建议另立任务，把 grype 升到 v0.120.0。只给建议，本单不执行。

## 1. 已知使用点（来自仓库文档）

| 位置 | 解释器 | 谁在用 | 现状来源 | 是否需要升级 |
|---|---|---|---|---|
| 开发机 `.venv`（每个 worktree 各一份） | uv 托管 `cpython-3.12.10` | 测试、`scripts/ci/stages/*`、release 预备 | 本机 `uv python list`；`docs/s0/dev-machine-inventory.md`（uv 0.7.8） | 是 [A] |
| CI 主机 `nexloop-ci` 的检出 `.venv` | uv 托管 3.12.10 | `scripts/ci/check` 各阶段；`release.py build/sbom/preflight`（`uv run --frozen`） | `run-33-20261015.md`（“CI 主机上 uv 管理的 CPython 3.12.10”）；uv 版本**待盘点** | 是 [B] |
| CI 主机 `/usr/bin/python3`（系统） | 发行版 Python | CI 控制器与同步单元、git hook（`deploy/stage/ci/*.example`）；`nexloop-release` 的 `sign`、`seal`、`check`，只用标准库 | 版本**待盘点** | 按发行版包版本判断（§2 第 4 条），不在本单范围 |
| APP_HOST `/opt/nexloop/python-3.12` | 从 CI 主机拷来的 uv CPython 3.12.10 | `/opt/nexloop/restore-venv`（`nexloop-restore`：恢复演练、AT-072） | `run-33-20261015.md` 第 1 步 | 是 [B] |
| APP_HOST `/opt/nexloop/venv` | **待盘点** | `nexloop-runtime-worker.service` 与 ExecStartPre 里的 `nexloop-doctor --budget-only`（单元示例） | 仅有示例单元，是否部署**待盘点** | 存在且低于 3.12.13 就升 [B] |
| DATA_HOST `/opt/nexloop/backup-venv` | **待盘点**（APP_HOST 只有系统 Python 3.14；DATA_HOST 的来源未记录） | 定时器：`nexloop-journal`（每分钟）、`nexloop-file-backup`（每 15 分钟）、`nexloop-pg-diff`（周一到周六 02:00）、`nexloop-pg-full`（周日 02:00）；`nexloop-doctor --backup`；`scripts/deploy/metrics_data_host.sh` 中的 `BACKUP_PY` | `deploy/stage/systemd/*.example`；`run-46`、`run-metrics-gate`、`run-release7-deploy` | 低于 3.12.13 就升 [B] |
| 发布镜像 backend、agent-host | 3.12.15 | — | `versions.lock.json` 的 `python-build-base` | 已完成 |

**主机侧的可达性**（对紧迫程度的判断）：

- 备份和恢复工具连接的是 PostgreSQL、pgBackRest 和 restic，不用 `http.client` 读取不受信 HTTP 服务器的响应；
- pip 安装时会访问包索引，但 requirements 都按哈希安装，索引是受信镜像。

所以主机侧的风险低。按负责人 10/15 “不豁免”的决定，仍然升级，不需要抢在验收前做。

## 2. 只读盘点 [只读]

在每台主机上，以该目录属主或只读身份执行。输出里只记版本、路径和单元名，**不要**贴 DSN 或秘密文件的内容。

```
# 1) 各个 NexLoop 解释器与 venv：实际版本、是否含修复（_MIN_READ_BUF_SIZE 是 3.12.13 的修复引入的）
for p in /opt/nexloop/*/bin/python3 /opt/nexloop/python-3.12/bin/python3.12; do [ -x "$p" ] && echo "$p -> $(readlink -f "$p")" && "$p" -c 'import sys,http.client as c;print(sys.version.split()[0],hasattr(c,"_MIN_READ_BUF_SIZE"))'; done
for v in /opt/nexloop/*/pyvenv.cfg; do echo "== $v"; cat "$v"; done
# 2) 依赖这些 venv 的单元与定时器
systemctl list-timers 'nexloop-*' --all --no-pager
systemctl cat 'nexloop-*' --no-pager 2>/dev/null | grep -nE '^# /|ExecStart(Pre)?=' 
# 3) 基线：每个 venv 的 pip freeze，留作切换后比对
/opt/nexloop/<VENV>/bin/pip freeze > "$HOME/pip-freeze-<VENV>-before.txt"
# 4) 若某个 venv 基于发行版 Python，按发行版包版本判断是否已修复，不按 3.12.x 的上游版本号
dpkg-query -W 'python3.1*' 2>/dev/null
# 5) 仅 CI 主机：uv 版本与已装的托管 Python
uv --version; uv python list --only-installed
```

**判读规则**：

- 第 1 条输出 `3.12.10 False` 或任何低于 3.12.13 的版本，就要升级；
- 发行版 Python 例外，以 Ubuntu 或 Debian 安全公告为准。Ubuntu noble 的 `python3.12` 在 `3.12.3-1ubuntu0.10` 已修复；Debian bookworm 的 `python3.11` 在 `3.11.2-6+deb12u7` 已修复；
- APP_HOST 系统自带的 Python 3.14 不受影响：3.14 的受影响范围只到 3.14.0，Ubuntu resolute 也标为 not-affected。

## 3. 开发机 [A]

```
# 1) uv 0.12.22：官方 release 档案，按 sha256 校验（aarch64-apple-darwin）
curl -LO https://github.com/astral-sh/uv/releases/download/0.12.22/uv-aarch64-apple-darwin.tar.gz
echo "5d714de09501a59393ceca78f4bc232a50478729640d251907160299b2a93ddd  uv-aarch64-apple-darwin.tar.gz" | shasum -a 256 -c
#    解包后替换原有 uv（安装位置以本机为准）；uv --version 应为 0.12.22
# 2) 托管 Python 3.12.15（uv 按自带下载表中的 sha256 校验）
uv python install 3.12.15
# 3) 锁文件不被改写（必须无输出、rc=0）；若要求重写锁，停下来，另立改动
uv lock --check
# 4) 每个 worktree 重建 .venv：已有的 3.12.10 venv 仍满足 "3.12" 的请求，uv 不会自动换
rm -rf .venv && uv sync --frozen && .venv/bin/python -c 'import sys,http.client as c;print(sys.version.split()[0],hasattr(c,"_MIN_READ_BUF_SIZE"))'   # 3.12.15 True
```

验证：

- `LC_ALL=C.UTF-8 bash scripts/ci/stages/static.sh`、`bash scripts/ci/stages/licenses.sh`、`bash scripts/ci/stages/dependency-python.sh` 都是 rc=0；
- `pytest -n 3 tests/test_wheel_install.py tests/test_community_container_contract.py tests/test_release_build_archive.py` 全部通过。这几项会用到 `uv build`、`uv export`、`uv venv`，用来确认新版 uv 导出的 requirements 与哈希仍被接受。

回退：

- 旧版 uv 的档案保留一份；
- `uv python uninstall 3.12.15`，换回 uv 0.7.8，再 `rm -rf .venv && uv sync --frozen`；
- 在全部主机完成并验证之前，**不要**卸载 3.12.10。

并发线注意：worktree 各有各的 `.venv`。调度员统一通知后再逐个重建，避免某条线跑测试时 `.venv` 正在被重建。

## 4. CI 主机 [B]

uv 档案与 Python 发行包都在开发机下载并校验，再拷到 CI 主机，之后在 CI 主机上再校验一次。这和 O3 安装 syft、grype 的方式相同，CI 主机不需要额外放行出站。

```
# 开发机：下载两个档案并校验
curl -LO https://github.com/astral-sh/uv/releases/download/0.12.22/uv-x86_64-unknown-linux-gnu.tar.gz
echo "b9980552309f09c15172b8be828555e375097f16deb459795ce7bfd200380f0b  uv-x86_64-unknown-linux-gnu.tar.gz" | shasum -a 256 -c
mkdir -p pbs-mirror/20261001 && curl -L -o pbs-mirror/20261001/cpython-3.12.15+20261001-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz \
  https://github.com/astral-sh/python-build-standalone/releases/download/20261001/cpython-3.12.15%2B20261001-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz
echo "7bb1659e3235077b7f63d5b6eb6ce653c6fcd6c5041e9d5f73b42ce10421464d  pbs-mirror/20261001/cpython-3.12.15+20261001-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz" | shasum -a 256 -c
```

[B] CI 主机，以 `nexloop-ci` 身份执行，安装位置与 O3 相同，都在该用户自己的目录里，不需要 root：

```
sha256sum -c <<< "b9980552309f09c15172b8be828555e375097f16deb459795ce7bfd200380f0b  uv-x86_64-unknown-linux-gnu.tar.gz"
# 旧 uv 改名保留（回退用），再放入新 uv；uv --version 应为 0.12.22
# uv 从本地镜像目录安装 3.12.15，仍按自带下载表中的 sha256 校验；不需要联网
UV_PYTHON_INSTALL_MIRROR=file://<PBS_MIRROR_DIR> uv python install 3.12.15
uv python list --only-installed       # 3.12.15 与 3.12.10 并存
```

- 先确认 uv 的选择不会受 CPU 指令集影响：看安装输出或 `uv python list --only-installed` 中的名称，所选文件名必须是 `x86_64-unknown-linux-gnu`，而不是 `x86_64_v2`、`x86_64_v3` 等变体。若 uv 选的是变体，就把对应的变体档案（同一 release 页，各有 sha256）也放进镜像目录，或设 `UV_PYTHON_DOWNLOADS=never`，并用 `--python <解释器路径>` 指定已解包的基线版。
- 若 uv 不接受 `file://` 镜像，就直接解包同一档案（方法同 §5.1），再用 `uv sync --frozen --python <解包目录>/bin/python3.12`；之后检出的 `.venv` 需要同样指定。
- CI 检出的 `.venv` 要重建。CI 每次运行若都是新检出，下一次运行会自动用上 3.12.15；若有复用的工作目录，就删掉其中的 `.venv`。
- **验证**：
  - 推送一个只含文档改动的提交，或对当前 main 重跑一次；`scripts/ci/check` 写出的 8 份 junit 全部通过，`python-deadline-gate` 不退步；
  - 在结果目录里确认解释器是 3.12.15：`uv run --frozen python -V`；
  - 下一次发布按 `NX-036-vuln-remediation.md` 末节的三条命令核对。
- **回退**：恢复旧 uv 档案，删掉 `.venv`；3.12.10 仍在，uv 会回到它。

## 5. APP_HOST 与 DATA_HOST [B]

两台主机都**并排新建**：新解释器目录 `/opt/nexloop/python-3.12.15`，新 venv 用 `<VENV>.new`。核对通过后再改名切换，旧目录改名为 `<VENV>.old-3.12.10` 保留。

### 5.1 解释器（两台主机相同）

用 §4 中校验过的 `install_only_stripped` 档案。它是自包含的 CPython 发行，正是 uv 自己安装的那一份，不再从 CI 主机的 uv 目录打包：

```
sha256sum -c <<< "7bb1659e3235077b7f63d5b6eb6ce653c6fcd6c5041e9d5f73b42ce10421464d  cpython-3.12.15+20261001-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
sudo mkdir /opt/nexloop/python-3.12.15
sudo tar -C /opt/nexloop/python-3.12.15 --strip-components=1 -xf cpython-3.12.15+20261001-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz   # 档案顶层目录为 python/
sudo chown -R root:root /opt/nexloop/python-3.12.15 && sudo chmod -R go-w /opt/nexloop/python-3.12.15
/opt/nexloop/python-3.12.15/bin/python3.12 -c 'import sys,http.client as c;print(sys.version.split()[0],hasattr(c,"_MIN_READ_BUF_SIZE"))'   # 3.12.15 True
```

### 5.2 venv 重建（每个需要升级的 venv：APP_HOST 的 `restore-venv`，以及 `venv`（如已部署）；DATA_HOST 的 `backup-venv`）

依赖与 wheel 和现用 venv **完全相同**：同一份 `uv export --frozen --no-dev --no-emit-workspace` 的 requirements（带哈希），同一个 `nexloop-eios-core` wheel（sha256 与现用的一致）。这次只换解释器，不升级依赖。

```
sudo /opt/nexloop/python-3.12.15/bin/python3.12 -m venv /opt/nexloop/<VENV>.new
sudo /opt/nexloop/<VENV>.new/bin/pip install --require-hashes -r <REQUIREMENTS_FROM_UV_LOCK> --index-url <PYPI_MIRROR>
sudo /opt/nexloop/<VENV>.new/bin/pip install --no-deps <NEXLOOP_EIOS_CORE_WHEEL>
/opt/nexloop/<VENV>.new/bin/pip freeze | diff "$HOME/pip-freeze-<VENV>-before.txt" -   # 只允许 wheel 路径那一行不同
```

- 属主与权限照旧 venv 设置，用 `stat -c '%U:%G %a' /opt/nexloop/<VENV>` 对照。
- 冒烟检查：用对应的服务身份运行入口的 `--help`：
  - `sudo -u nexloop-pgbackrest /opt/nexloop/restore-venv.new/bin/nexloop-restore --help`；
  - `sudo -u nexloop-backup /opt/nexloop/backup-venv.new/bin/nexloop-backup --help`。

### 5.3 切换

**APP_HOST 的 `restore-venv`**：没有定时器，只在演练时使用。直接改名即可：

```
sudo mv /opt/nexloop/restore-venv /opt/nexloop/restore-venv.old-3.12.10 && sudo mv /opt/nexloop/restore-venv.new /opt/nexloop/restore-venv
```

**APP_HOST 的 `venv`**（若 runtime worker 已部署）：切换前停服务，切换后启动：

```
sudo systemctl stop nexloop-runtime-worker
# 改名同上
sudo systemctl start nexloop-runtime-worker
```

ExecStartPre 中的 `nexloop-doctor --budget-only` 会随启动一起执行，它通过就是第一层验证。

**DATA_HOST 的 `backup-venv`**：`nexloop-journal` 每分钟运行一次，所以切换窗口要短，并且必须避开 02:00 的 PG 备份与 03:30 的 restic 维护：

```
sudo systemctl stop nexloop-journal.timer nexloop-file-backup.timer nexloop-pg-diff.timer nexloop-pg-full.timer
systemctl is-active nexloop-journal.service nexloop-file-backup.service nexloop-pg-diff.service nexloop-pg-full.service   # 全部 inactive 才继续；有在跑的就等它结束
sudo mv /opt/nexloop/backup-venv /opt/nexloop/backup-venv.old-3.12.10 && sudo mv /opt/nexloop/backup-venv.new /opt/nexloop/backup-venv
sudo systemctl start nexloop-journal.timer nexloop-file-backup.timer nexloop-pg-diff.timer nexloop-pg-full.timer
```

- 停的只是定时器，进行中的服务不会被打断；
- 窗口控制在 5 分钟内。journal 导出停一两分钟，只会让 RPO 计量短暂变大，不丢数据；
- `nexloop-journal-restic@*`、`nexloop-restic-maint` 不用这个 venv，不需要停。

### 5.4 对备份定时器、doctor 与门禁的影响和验证

| 项 | 影响 | 验证 |
|---|---|---|
| 备份定时器 | 只在切换窗口内暂停。各定时器设了 `Persistent=true`，但它只补跑定时器停用期间错过的触发中的最后一次，所以启动定时器时可能立即运行一次，这是预期行为 | 启动定时器后 5 分钟内：`journalctl -u nexloop-journal.service --since -5min` 显示成功；下一个 15 分钟点 `nexloop-file-backup.service` 成功；次日 02:00 后 `nexloop-pg-diff.service` 成功 |
| `nexloop-doctor --backup` | 不检查 Python 版本，按 venv 路径调用，路径没变 | 按 `run-46` 第 7.4 步、`run-release7-deploy` 的命令重跑，各项都应为 true |
| 指标门禁（`scripts/deploy/metrics_data_host.sh`，`BACKUP_PY` 默认指向 backup-venv） | 路径不变 | 按 `run-metrics-gate-20261016.md` 重跑一次采集，门禁结论不变；journal 延迟指标在窗口后回落 |
| 恢复演练 / AT-072 | `restore-venv` 换了解释器 | 下一次演练前用 `nexloop-restore --help` 冒烟；完整演练按 `run-33` 执行，不另做 |
| runtime worker（若已部署） | 重启一次 | 服务处于 active，ExecStartPre 的 doctor 通过；Host 与 worker 的就绪检查按现行手册 |

**回退**：每个 venv 都可以单独回退，`/opt/nexloop/python-3.12` 在验证期内保留。

```
# DATA_HOST 先按 5.3 停定时器，APP_HOST 的 worker 先停服务，然后：
sudo mv /opt/nexloop/<VENV> /opt/nexloop/<VENV>.failed-3.12.15 && sudo mv /opt/nexloop/<VENV>.old-3.12.10 /opt/nexloop/<VENV>
```

**收尾**（全部验证通过并稳定至少 7 天后，再做一次 [B]）：

- 删除 `<VENV>.old-3.12.10` 与 `/opt/nexloop/python-3.12`；
- CI 主机与开发机 `uv python uninstall 3.12.10`。

**可选的仓库改动 [A]**：所有主机都切换后，把 `.python-version` 从 `3.12` 改成 `3.12.15`，以后 uv 不会再静默落回旧的补丁版本。必须在 CI 主机装好 3.12.15 **之后**合入，否则 CI 会失败；由调度员排期。

## 6. grype 0.86.1 漏洞库与官方记录不一致：处理建议

> **更新（同日）**：调度员已要求按选项 B 实施。仓库改动、CI 主机的安装与离线更新库步骤，见 `run-grype-v6-20261017.md`。下文保留当时的分析。

**事实**（2026-10-17，匿名读取 Anchore 公开的库列表）：

- `toolbox-data.anchore.io/grype/databases/listing.json` 中，schema v5 的最新构建是 **2026-03-09**；schema v1 到 v4 更早；
- v6 的 `grype.anchore.io/databases/v6/latest.json` 是 `v6.1.10`，构建于 2026-10-16；
- `versions.lock.json` 固定的 grype 0.86.1 只读 v5 的库。所以 CI 主机每天的“更新”拿到的始终是 3 月的库。

**影响**：

1. **D12 无法满足**：public 类别要求库龄不超过 7 天（`evaluate_vulnerabilities`），而 3 月的库永远超龄。所以 `public-preflight` 第 8 步对任何发布都会失败。stage 只记录日期，不受影响。
2. **数据陈旧**：
   - 误报：例如 Debian `python3.11` 在 deb12u7 已修复，旧库仍标为 wont-fix；若 3.12.15 被列出 CVE-2025-13836，也属于这一类；
   - 漏报：3 月以后公布的漏洞都不会出现。发布清单上“有修复的 Critical 为 0”，在这个前提下不可信。
3. 豁免与列表都建立在这份陈旧数据上。2027-01-12 豁免到期复审时，也需要新库。

**选项**：

| 选项 | 内容 | 优点 | 代价与风险 |
|---|---|---|---|
| A 维持 0.86.1 | 不动 | 无改动 | public 发布被 D12 永久阻断；扫描结论不可信。**不建议** |
| B 只升 grype 到 v0.120.0（2026-10-02 发布，已满两周） | `release_tools.grype` 改版本、档案 sha256（`grype_0.120.0_linux_amd64.tar.gz`：`a5a1218dce63acdac152a6b3b5bb366e7267e36f4069848cf455543b3fa5700e`）、安装后的二进制 sha256 和 git_commit；CI 主机按 O3 方式安装 | 库恢复每日更新，D12 可以满足；改动面最小；它仍读取 syft 1.18.1 生成的 SPDX SBOM | 发现集会**明显变化**：3 月以后的新漏洞会出现，可能出现新的“有修复的 Critical”而阻断 public；现有豁免可能进入 cleanup 或需要调整 `version_pattern`。需要重新审阅一轮 |
| C 同时升 syft 到 1.54.0（2026-10-01，与 grype 0.120.0 内部用的 syft 一致；`syft_1.54.0_linux_amd64.tar.gz`：`54a87372498168b2d033e876fd41fa4e8035b872699e525a57046e1f2f09c860`） | 在 B 的基础上一并升级 | 编目器与匹配器同代，包识别更准（例如二进制 Python 的识别） | SBOM 内容会变（`sbom_sha256` 与以往发布不可比）；改动面更大 |

**建议**：

- **选 B**，另立任务，排在下一次 public 发布之前，不影响 stage 发布；
- C 可以在 B 稳定后单独做。

B 的落地要点：

- **仓库 [A]**：
  - 修改 `versions.lock.json` 的 `release_tools.grype`；
  - `release.py` 读取库时间时已经同时尝试 `descriptor.db.built` 和 `descriptor.db.status.built` 两种结构，但还没有用 v6 的真实输出验证过。修复状态无论取哪些值，策略都只认 `fixed`；
  - 需要在 CI 主机取一份 v0.120.0 的真实 JSON 输出，只保留编号、包、版本、严重级别和修复状态，作为测试夹具，确认 `findings()` 与库时间解析正确；
  - 跑 `tests/test_release*.py` 中与 sbom、vulnerability 相关的测试。
- **CI 主机 [B]**：
  - 按 O3 方式在开发机与 CI 主机两次校验档案后安装，旧二进制保留以便回退；
  - 每日库更新任务改为 v6：`grype db update` 访问 `grype.anchore.io`。若 NX-033 的出站白名单只放行了旧域名，需要 **[主机配置变更]** 加入新域名；也可以改成在开发机下载库档案，按 `latest.json` 中的 checksum 校验后 `grype db import`；
  - 先对最近一次 stage 发布（`20261015-…` 或其后一次）重扫，**只读比较**新旧发现，整理成一页给负责人。确认没有新的阻断项、豁免仍然匹配之后，再用于 public 发布。
- **回退**：恢复 lock 中的 0.86.1 与旧二进制。代价是 D12 再次无法满足。

**CVE-2025-13836 本身**：

- 升级到 B 之后，backend 与 agent-host 的 `python 3.12.15` 预计不再列出；
- 在那之前若仍被列出，它是 High，stage 与 public 都只列出、不阻断；
- 负责人 10/15 已决定不豁免，所以**不为它写豁免**。
