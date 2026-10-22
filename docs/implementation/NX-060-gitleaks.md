# NX-060：泄密扫描加入锁定版本的 gitleaks

- **来源**：负责人 2026-10-22 在 EIOS 能力缺口 Q9 中选择推荐项 A（`docs/design/eios-capability-gap.md` §A.5、E5）。调度员当天派工，L7 执行。
- **分支**：`l7/gitleaks`，基点 main `1892c9ac`。
- **受保护路径**：本任务修改了 `scripts/ci/`（owner ack-policy，由调度员 ack）。
- **范围**：
  - `scripts/ci/prepush-scan.sh` 增加第四道门禁，原有三道不变；
  - 新增 `.gitleaks.toml`、`scripts/ci/install-gitleaks.sh`；
  - `versions.lock.json` 的 `release_tools.gitleaks`；
  - 测试与文档。
- **不在范围内**：没有改写任何历史，没有在任何主机上安装，仓库也不下载任何东西。

## 1. 门禁行为

`scripts/ci/prepush-scan.sh` 的四道门禁，按顺序执行，任何一道失败即退出非 0：

1. 跟踪的私有或运行时路径（不变）；
2. 每个待推送提交中的受限标识（不变）；
3. 新增行中的凭据值正则（不变；`tests/test_history_audit.py` 依赖它的表达式，原样保留）；
4. **新增，gitleaks**：
   - 用 `gitleaks git --log-opts=<base>..<ref>` 只扫描待推送提交的新增行；
   - 也可以用 `prepush-scan.sh --full [ref]`（默认 HEAD），扫描该 ref 可达的全部历史，只跑这一道门。

第四道门在以下任何一种情况下都**失败关闭**：

| 情况 | 输出 |
|---|---|
| 找不到 gitleaks（`NEXLOOP_GITLEAKS` 或 PATH） | `FAIL gitleaks not installed (pinned 8.30.1)` |
| `gitleaks version` 与锁定版本不同 | `FAIL gitleaks version … is not the pinned …` |
| 本平台已记录 `binary_sha256_<platform>`，但二进制摘要不符 | `FAIL gitleaks binary digest is not the recorded one` |
| 被扫描的提交里没有 `.gitleaks.toml` | `FAIL .gitleaks.toml missing in <ref>` |
| 配置连一个合成哨兵 AWS 访问键都检测不到（被禁用或被掏空） | `FAIL gitleaks configuration no longer detects the sentinel key` |
| 有命中 | `FAIL gitleaks: N finding(s) in … (content redacted)`，**只计数，不打印值** |
| 扫描没有完成 | `FAIL gitleaks scan could not complete` |

设计要点：

- **锁定版本**从脚本所在的检出读取，即 `versions.lock.json` 的 `release_tools.gitleaks.version`，而不是从被扫描的树读取；
- **配置**取被扫描提交自身的 `.gitleaks.toml`（`git show <ref>:.gitleaks.toml`），这样放宽配置的改动会与需要它的提交一起被复核。为防止放宽到“什么都不报”，加了两层：
  - 运行时的哨兵检查（借鉴上游 EIOS gate 的做法）；
  - 结构测试 `tests/test_gitleaks_config.py`：必须保留默认规则，不允许覆盖或禁用规则，最多 3 条 allowlist，每条只针对 `generic-api-key`、条件为 AND，路径和值都必须首尾锚定且不能是通配；
- 退出码用 `--exit-code 17` 区分“有命中”与“出错”；
- 报告写在 `umask 077` 的私有临时目录里，由 trap 删除；
- 用 `--redact` 打码；
- 兼容 macOS 自带的 bash 3.2（第一次实现时“进程替换里嵌 heredoc”在 bash 3.2 下报 ambiguous redirect，已改写）。

调用方不用改：`--hook` 模式、`sync_github.py`、`release.py` 的 `history_scan` 都会自动经过第四道门。

## 2. `.gitleaks.toml`

- 扩展 gitleaks 默认规则（`[extend] useDefault = true`）；`minVersion = "8.30.1"`。
- **没有带入上游 EIOS 的任何规则或白名单**。上游的 `.gitleaks.toml` 有 4 条厂商规则和 2 条针对其自身夹具的 allowlist，都与 NexLoop 无关，本次一条都没有复制。
- allowlist 只有 3 条，每条都同时限定规则、路径和值的形状，条件为 AND。逐条说明如下：

| # | 允许什么 | 在哪些路径 | 值的形状 | 来自全量扫描的命中 | 为什么不是凭据 |
|---|---|---|---|---|---|
| 1 | 以文件名为键的 SHA-256 摘要，以及 claim 合约示例中的 `correlation_key`/`topic_key` 摘要 | `docs/implementation/*.json\|md`、两份 `claim.valid.json`、`vendor/pi/.../providers/data/.manifest.json` | 整段匹配必须是 `"<文件名>.(py\|sql\|ts\|tsx\|json\|md)": "<64 位小写十六进制>"`，或 `(correlation_key\|topic_key)": "<64 位十六进制>"` | 119 处 | 这些是 CI 证据、源码一致性清单、供应商清单里记录的文件摘要，以及合约示例里的合成摘要。`generic-api-key` 是被文件名里的 api、credential、key、token 字样触发的 |
| 2 | 测试中的合成幂等键 | `tests/*.py`（仅顶层） | `^(synthetic\|wb\|nx0NN)-[a-z0-9-]+$` | 39 处 | 例如 `synthetic-wb-owner-reject-0001`、`nx047-outbound-conversation-key`，是可读的业务幂等键 |
| 3 | 召回分词器版本号 | `nexloop_eios/recall.py`、`eios/migrations/NNNN_*.sql` | `^nexloop-cjk-bigram-v[0-9]+$` | 2 处 | NX-021 写进每行索引的公开版本名 |

第 2 条特意只放行测试路径：同一个值写在 `service/demo.py` 里仍会被拦，`test-prepush-scan.py` 中有这个用例。

## 3. 对现有历史的全量扫描（2026-10-22）

- 范围：`gitleaks git --log-opts=--all`，覆盖本仓库全部 2233 个提交、所有分支。
- 第一轮用默认规则、不带配置：**160 处命中，全部是 `generic-api-key`**。按值去重后 69 个，分类如下：

| 类别 | 处数 | 判定 |
|---|---|---|
| 64 位十六进制摘要（CI 证据、source-parity、供应商清单的文件名到 sha256，以及 claim 示例的 correlation/topic key） | 119 | 误报，由 allowlist 1 覆盖 |
| 测试幂等键（`idempotency_key=`、`Idempotency-Key`、`key=`） | 39 | 误报，由 allowlist 2 覆盖 |
| 分词器版本号 `nexloop-cjk-bigram-v1`（`recall.py`、`0066_nx021_recall_index.sql`） | 2 | 误报，由 allowlist 3 覆盖 |

- **真泄露：0 处。** 除 `generic-api-key` 外，没有任何其它规则命中（私钥、云厂商密钥、GitHub token 等都没有）。
- 带上 `.gitleaks.toml` 再跑一遍全量：**0 处命中**（exit 0）。`prepush-scan.sh --full HEAD` 输出 PASS，约 3.5 秒。
- 附带观察：`gitleaks dir .` 会命中 `.ci-results/` 下的私钥和 DSN，共 486 处。这些是被 git 忽略的本地测试产物（`git check-ignore` 确认，`git ls-files .ci-results` 为空），是每次测试临时生成的测试密钥，从未进入仓库，也不在门禁的扫描范围内。

## 4. 二进制的获取与校验

| 机器 | 来源 | 校验 |
|---|---|---|
| 开发机（darwin arm64） | 已有的 Homebrew 瓶装 `gitleaks 8.30.1`（formula 锁定源码并由 brew 校验） | 门禁检查版本。Homebrew 构建的二进制与官方 release 的摘要不同，所以不记录 darwin 的二进制摘要 |
| CI 主机，以及运行 `sync_github.py`、`release.py` 的账户（linux amd64） | 官方 release：`release_tools.gitleaks.source`，即 `gitleaks/gitleaks` 的 v8.30.1 release，制品名 `gitleaks_8.30.1_linux_x64.tar.gz`，校验文件 `gitleaks_8.30.1_checksums.txt` | `scripts/ci/install-gitleaks.sh`，见下 |

`install-gitleaks.sh <archive> <checksums> [dest]` 的行为：

- 本身**不联网**；
- 制品名必须与 lock 中记录的一致；
- 制品必须与官方 checksums 文件中唯一的那一行一致；如果 lock 已经记录了 `archive_sha256_<platform>`，也必须一致；
- 只解出 `gitleaks` 这一个普通文件，并确认其版本；
- 安装到账户自己的目录（默认 `~/.local/bin`，不需要 root），并打印 archive 与 binary 的摘要。

**首次安装步骤（B 类，由负责人或调度员在 CI 主机上执行；我没有连接任何主机）**：

```bash
curl -fsSLO https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz
```

```bash
curl -fsSLO https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_checksums.txt
```

```bash
scripts/ci/install-gitleaks.sh gitleaks_8.30.1_linux_x64.tar.gz gitleaks_8.30.1_checksums.txt
```

之后：

1. 在开发机上取同一个 archive，独立计算一次摘要并比对，与 syft 的先例相同：双机校验；
2. 把打印出的 `archive_sha256` 和 `binary_sha256` 写进 `versions.lock.json` 的 `archive_sha256_linux_amd64` 和 `binary_sha256_linux_amd64`，`status` 改为 `installed_ci_host`。此后门禁在 CI 主机上也会核对二进制摘要；
3. 确认运行 `sync_github.py`、`release.py` 的服务账户，其 PATH 里有安装目录。`sync_github.py` 只把 PATH、HOME、LANG、LC_ALL、TMPDIR 传给扫描进程；也可以设置 `NEXLOOP_GITLEAKS` 指向二进制。

说明：制品名按 gitleaks release 的命名规则填写，我**没有下载核对**。名字不对时，安装脚本会拒绝，不会悄悄通过。

## 5. 上线顺序与风险

- **合并之前，必须先在运行 `sync_github.py` 和 `release.py` 的主机上装好 gitleaks**。否则第四道门失败关闭，GitHub 同步和发版的 `history_scan` 会被阻塞。这是设计上的有意行为：缺工具从不算通过。
- `scripts/ci/check` 的各 stage 不调用 `prepush-scan.sh`。`test-prepush-scan.py` 只在 `make lint` 中运行。CI 的判定不会因此变化，但在没有 gitleaks 的机器上跑 `make lint` 会失败。
- `.git/hooks/pre-push` 已经调用 `prepush-scan.sh --hook`，各开发线推送前也会经过第四道门，开发机已有 gitleaks。

## 6. 测试与演示

环境：`PYTHONPATH=packages/eios-core/src:tests`，`uv run --frozen`，gitleaks 8.30.1（Homebrew）。没有 PG 测试。

| 命令 | 结果 |
|---|---|
| `uv run --frozen python scripts/ci/test-prepush-scan.py` | 两行 PASS。新增的用例包括：(1) 合成的假 AWS 访问键、假 GitHub token 各提交一次又删除，普通模式、`--hook` 模式、`--full` 模式都被拦（FAIL gitleaks），输出中不含假值；(2) 测试路径下的合成幂等键放行，同一个值放在非测试路径被拦；(3) base 之前的旧泄露，普通模式放行（不在待推送范围内），`--full` 被拦；(4) 没有工具、版本不对（用桩程序）、提交里没有配置、配置被掏空（哨兵检测不到），全部失败关闭。假凭据在运行时拼接，源码里从不出现完整的值 |
| `pytest tests/test_gitleaks_config.py tests/test_history_audit.py tests/test_release_compliance.py` | 39 passed |
| `pytest tests/test_github_sync.py` | 首次 5 failed（临时仓库没有 `.gitleaks.toml`，新门禁按设计失败关闭）；在 lab 的 base 提交里带上真实配置后 10 passed |
| `pytest tests/test_public_repository_checks.py tests/test_license_compliance.py` | 48 passed |
| `bash scripts/ci/prepush-scan.sh HEAD 1892c9ac`（本分支首次提交后） | **首次失败**：`FAIL gitleaks: 1 finding(s) in outgoing added lines`。命中的是 `scripts/ci/test-prepush-scan.py` 中测试用的合成幂等键字面量：它不在 `tests/` 路径下，allowlist 2 不放行。改为运行时拼接后重扫 |
| 同上（修正后） | 四道门全部 PASS |
| `bash scripts/ci/prepush-scan.sh --full HEAD` | PASS（全部历史 0 命中） |
| `NEXLOOP_GITLEAKS=/nonexistent bash scripts/ci/prepush-scan.sh --full HEAD` | `FAIL gitleaks not installed (pinned 8.30.1)`，exit 1 |
| `install-gitleaks.sh`：用本机二进制自制同名 darwin_arm64 archive 加 checksums，只在 scratch 目录中执行 | 正常路径：安装，并打印两个摘要。checksums 不符：`refused: archive digest differs from the published checksum`，exit 2。名字不对：`refused: archive must be …`，exit 2。被拒时都没有安装任何东西。这只验证脚本逻辑，不代替 CI 主机上用官方 archive 的首次安装 |

文档同步：`AGENTS.md` 与 `GOVERNANCE.md` 中“三道门禁”的描述改为四道。
