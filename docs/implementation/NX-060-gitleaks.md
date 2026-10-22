# NX-060：泄密扫描加入锁定版本的 gitleaks

- **来源**：负责人 2026-10-22 在 EIOS 能力缺口 Q9 中选择推荐项 A（`docs/design/eios-capability-gap.md` §A.5、E5）。调度员当天派工，L7 执行。
- **分支**：`l7/gitleaks`，基点 main `1892c9ac`。
- **受保护路径**：本任务修改了 `scripts/ci/`（owner ack-policy，由调度员 ack）。
- **范围**：
  - `scripts/ci/prepush-scan.sh` 增加第四道门禁，原有三道不变；
  - 新增 `.gitleaks.toml`、`scripts/ci/install-gitleaks.sh`、`scripts/ci/check_gitleaks_config.py`；
  - `versions.lock.json` 的 `release_tools.gitleaks`；
  - 测试与文档。
- **不在范围内**：没有改写任何历史，仓库也不下载任何东西。L7 没有连接任何主机；CI 主机上的安装由调度员执行（§4）。
- **修订**：
  - 第一稿 `0d2003e1`；
  - 第二稿按 L6 复核修订：确认级 1（`.gitleaksignore`）、确认级 2（配置可信度与运行时结构检查）、重要 3（Linux 摘要策略）、建议 6（处置流程）、建议 7（lock 中注明不分发）、建议 9（`--full` 不能替代前三道门）。另外新发现并关闭了 `gitleaks:allow` 行内注释这一绕过途径。
  - 第三稿：回填 CI 主机的安装摘要；补充受信副本、各组件实际生效范围与需要核对的事项；修复检出中缺少 lock 时静默退出的问题。

## 1. 门禁行为

`scripts/ci/prepush-scan.sh` 的四道门禁，按顺序执行，任何一道失败即退出非 0：

1. 跟踪的私有或运行时路径（不变）；
2. 每个待推送提交中的受限标识（不变）；
3. 新增行中的凭据值正则（不变；`tests/test_history_audit.py` 依赖它的表达式，原样保留）；
4. **新增，gitleaks**：
   - 用 `gitleaks git --log-opts=<base>..<ref>` 只扫描待推送提交的新增行；
   - 也可以用 `prepush-scan.sh --full [ref]`（默认 HEAD），扫描该 ref 可达的全部历史，只跑这一道门。

### 1.1 信任模型（第二稿，按 L6 复核确认级 1、2 修订）

**配置和锁定条目从哪里来**：

- `.gitleaks.toml` 和 `versions.lock.json` 的 `release_tools.gitleaks`，**都取自 base 提交**。base 是已经发布过的版本：
  - `--hook` 下是 origin/main；
  - `sync_github.py` 中是上次同步的 main；
  - `release.py` 中是上一个公开版本。
- 只有 base 里没有这两项时（首次引入），或在 `--full` 模式下，才回退到脚本所在的检出。
- **永远不取被扫描的提交**。待推送提交如果删除、掏空或放宽了 `.gitleaks.toml`，这次扫描仍然用受信版本，并且会输出：

  ```
  NOTE .gitleaks.toml changes in the outgoing commits: owner ack required
  ```

  这一点与受保护路径一致；建议调度员把 `.gitleaks.toml` 加入 ack-policy 的受保护路径清单。

  由此带来的工作流约束：新的 allowlist 必须先单独发布，需要它的值才能在之后的推送中通过。

**结构约束在运行时执行**：`scripts/ci/check_gitleaks_config.py` 会对“即将使用的受信配置”执行检查，测试 `tests/test_gitleaks_config.py` 也调用同一个检查器。规则如下：

- 必须保留默认规则，不允许覆盖或禁用规则，不允许全局 allowlist；
- 最多 3 条 allowlist，每条只针对 `generic-api-key`、条件为 AND，字段固定；
- 路径和值都必须首尾锚定，不能是通配，路径不能匹配任意目录；
- `minVersion` 必须等于锁定版本。

如果 base 本身的配置违反这些规则，同样会失败，不被信任。

**忽略机制一律不生效**：

- gitleaks 以 **git 目录**（`git rev-parse --absolute-git-dir`）为 source，在**私有空目录**中运行；
- `--gitleaks-ignore-path` 指向这个空目录，并加 `--ignore-gitleaks-allow`；
- 清除 `GITLEAKS_CONFIG` 和 `GITLEAKS_CONFIG_TOML` 两个环境变量。

在 8.30.1 上实测：

- 工作树里有一个未跟踪的 `.gitleaksignore` 时，原先的写法会漏报，exit 0；
- **只加 `--gitleaks-ignore-path` 也挡不住**，因为 gitleaks 仍然会读 source 下的 `.gitleaksignore`；
- 改为以 git 目录为 source 之后，两处假凭据都能报出；
- 行内的 `gitleaks:allow` 注释原先会悄悄抑制一处命中，这是本轮新发现的同类绕过，已用 `--ignore-gitleaks-allow` 关闭。

此外，只要被扫描的提交跟踪了任何 `.gitleaksignore`，或者工作树根目录、git 目录里有这个文件，就**直接失败**。

### 1.2 失败关闭的情况

| 情况 | 输出 |
|---|---|
| 平台不是 linux_amd64 或 darwin_arm64 | `FAIL gitleaks gate: platform … is not pinned` |
| 找不到 gitleaks（`NEXLOOP_GITLEAKS` 或 PATH） | `FAIL gitleaks not installed (pinned 8.30.1)` |
| `gitleaks version` 与锁定版本不同 | `FAIL gitleaks version … is not the pinned …` |
| **linux_amd64 上还没有记录 `binary_sha256_linux_amd64`** | `FAIL gitleaks binary digest not recorded for linux_amd64 …`（策略见 §4） |
| 已记录的二进制摘要不符 | `FAIL gitleaks binary digest is not the recorded one` |
| 受信的 `.gitleaks.toml` 缺失 | `FAIL trusted .gitleaks.toml missing` |
| 受信配置违反结构约束 | `FAIL gitleaks configuration (base\|checkout) breaks the structure rules: <规则名>`，不打印规则内容 |
| 被扫描的提交、工作树或 git 目录里有 `.gitleaksignore` | `FAIL a .gitleaksignore is present …` |
| 配置或二进制连合成哨兵 AWS 访问键都检测不到（例如一个回显 8.30.1 却什么都不报的桩程序） | `FAIL gitleaks configuration no longer detects the sentinel key` |
| 有命中 | `FAIL gitleaks: N finding(s) in … (content redacted)`，**只计数，不打印值** |
| 扫描没有完成 | `FAIL gitleaks scan could not complete` |

其他实现细节：

- 退出码用 `--exit-code 17` 区分“有命中”与“出错”；
- 报告写在 `umask 077` 的私有临时目录里，由 trap 删除；
- 兼容 macOS 自带的 bash 3.2：第一稿中“进程替换里嵌 heredoc”在 bash 3.2 下报 ambiguous redirect，已改写；
- `--full` 模式只跑第四道门，**不能替代普通模式下的前三道检查**。

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

**摘要策略（按 L6 重要 3 明确）**：

- **linux_amd64（CI 主机与发版主机）：没有记录摘要就失败**。只要 `binary_sha256_linux_amd64` 还是 null，第四道门就失败；记录之后，只运行摘要与之一致的二进制。这样可以堵住“一个回显 8.30.1 的桩程序也能通过”的漏洞。
- **darwin_arm64（开发机）**：只核对版本。Homebrew 构建的二进制没有官方摘要可以对照。桩程序即使冒充了版本号，也会被哨兵检查挡下，因为它报不出那个合成的 AWS 访问键（测试中有这个用例）。
- **其他平台**：失败。

**首次安装顺序**（B 类，由负责人或调度员执行；我没有连接任何主机，也没有下载任何东西）：

1. **安装之前**，先在另一台机器上独立取得同一个 archive，计算它的 sha256，与官方 checksums 文件比对，然后把结果先记入 `archive_sha256_linux_amd64`。这样 CI 主机安装时，就有一个与 checksums 文件来源无关的独立锚点。L6 的建议是装机前做双机校验，而不是装机后。
2. 在 CI 主机上用上面三条命令安装。安装脚本会同时核对官方 checksums 和已记录的 `archive_sha256_linux_amd64`，任何一项不符都拒绝安装。
3. 把打印出的 `binary_sha256` 记入 `binary_sha256_linux_amd64`，`status` 改为 `installed_ci_host`。
4. 确认运行 `sync_github.py`、`release.py` 的服务账户能找到 gitleaks。`sync_github.py` 只把 PATH、HOME、LANG、LC_ALL、TMPDIR 传给扫描进程，而 `~/.local/bin` 通常不在服务账户的 PATH 里，所以需要调整 PATH，或者设置 `NEXLOOP_GITLEAKS`。

**首次安装记录（2026-10-22，调度员执行，负责人当天同意；L7 未连接任何主机）**：

| 步骤 | 结果 |
|---|---|
| ① 独立锚点：调度员在自己的 Mac 上下载官方 v8.30.1 archive | sha256 `551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb`，与官方 checksums 一致。两个制品名都真实存在：`gitleaks_8.30.1_linux_x64.tar.gz` 8,230,402 字节，`gitleaks_8.30.1_checksums.txt` 999 字节。L6 重要 5 由此关闭 |
| ② CI 主机，非特权账户运行 `install-gitleaks.sh`，lock 副本预先填入 ① 的 archive 摘要作为独立锚点 | 通过。binary sha256 `88f91962aa2f93ac6ab281d553b9e125f5197bbbce38f9f2437f7299c32e5509` |
| ③ 用 sudo 安装为 `/usr/local/bin/gitleaks` | root:root 0755，CI、Git 托管、发版账户都不可写。复核摘要一致 |
| ④ 可见性 | `nexloop-ci`、`nexloop-git`（同步服务，systemd 默认 PATH）、`nexloop-release` 三个账户都能找到 8.30.1，不需要设置 `NEXLOOP_GITLEAKS` |

两个摘要已记入 `versions.lock.json`（`archive_sha256_linux_amd64`、`binary_sha256_linux_amd64`），`status` 为 `installed_ci_host`。从此在 linux_amd64 上，第四道门只运行摘要为 `88f9…5509` 的二进制。

## 5. 上线顺序与风险

**受信副本**：

- 控制器、两个 hook 和同步服务运行的都是受信副本 `<TRUSTED_CI_DIR>`（例如 `/opt/nexloop/ci-trusted/<sha12>`）中的程序，从不运行候选代码。
- 合入本改动后，**要升级受信副本**，流程按 [OPERATIONS §7.2](../OPERATIONS.md)：
  1. 用 `git archive` 展开新 SHA；
  2. 重新派生车道策略并比较；
  3. 备份 hook 和 unit；
  4. 替换路径，执行 `daemon-reload`；
  5. 验证；
  6. 必要时回退。
- 这是 **B 类步骤，由调度员在 CI 主机上执行，需负责人确认**。本改动修改了 `scripts/ci/`，属于必须升级的情形。

**在现行部署中，第四道门实际作用于哪些组件**（以仓库中的代码和模板为准）：

| 组件 | 是否调用 `prepush-scan.sh` | 结论 |
|---|---|---|
| 同步服务（`nexloop_ci.py sync`，`deploy/stage/ci/nexloop-ci-sync.service.example`） | 本稿写成时**否**：只做 fast-forward 推送，不运行任何扫描 | **已由后续任务 `l7/sync-scan-gate` 补上**：推送前对“上次已同步的 main..main”运行四道门禁，见 `NX-060-sync-merge-scan.md` 与 OPERATIONS §7.2a |
| 控制器（`nexloop_ci.py run-once`） | 本稿写成时只有配置了 `--merge-gate` 才会调用，主机上没有配置（调度员 2026-10-22 核实） | **已由 `l7/sync-scan-gate` 补上**：每次合入前对 main..候选运行四道门禁，不再依赖 `--merge-gate` |
| 发版（`release.py` 的 `history_scan`） | 是，运行候选检出中的 `scripts/ci/prepush-scan.sh <sha> <上一个公开版本>` | 生效。配置与锁定条目取自上一个公开版本；首次引入时回退到候选检出 |
| 各开发线的 pre-push hook（`--hook`） | 是 | 生效，开发机已有 gitleaks |
| CI 全量 pytest 中的 `tests/test_github_sync.py`，以及 `make lint` 中的 `test-prepush-scan.py` | 是，在临时仓库中运行 | CI 主机已安装 gitleaks 并记录了摘要，可以通过 |

**需要核对**（L7 无法从仓库得知）：

1. 控制器 unit 上是否配置了 `--merge-gate`，以及具体命令。
2. 如果配置的是受信副本中的 `prepush-scan.sh`：
   - 它在 bare 仓库中运行，因为 `merge()` 以 `cwd=repo` 执行门禁命令；
   - 本次核对发现，在 bare 仓库中脚本开头的 `cd "$(git rev-parse --show-toplevel)"` 会失败，但脚本不会退出，于是**第一道门（`git ls-files`）拿不到任何文件，形同虚设**。这是原有行为，不是本次引入的；**已在 `l7/sync-scan-gate` 中修复**：各道门都改为通过 git 读取被扫描的提交，在 bare 仓库中与在检出中一样生效；
   - 第四道门在 bare 仓库中可以正常工作：它用的是 `git rev-parse --absolute-git-dir`、`git show <base>:…` 与 `git ls-tree`。

   如果第一道门需要在 bare 仓库中生效，建议另开任务：改为对被扫描的 ref 执行 `git ls-tree`，或者在临时 worktree 中运行（与 `sync_github.py` 的做法相同）。

**受信副本不含 `versions.lock.json` 与 `.gitleaks.toml`**（OPERATIONS §7.2 的展开清单只有 `scripts/ci .ci-policy deploy/stage/ci scripts/deploy/probe_isolation.py`）：

- 这不影响普通模式：锁定条目与配置都取自 base 提交（合入后的 main 已经包含两者）。
- 第三稿修复了一个问题：第二稿在检出中缺少 `versions.lock.json` 时，`cp` 会在 `set -e` 下静默退出。现在改为回退到 base；两处都没有时，明确输出 `FAIL gitleaks pin unreadable …`。`test-prepush-scan.py` 中有“只含 `scripts/ci` 的受信副本”这个用例。
- 如果要在受信副本上运行 `--full`，建议把这两个文件加入 §7.2 的展开清单。

**其他**：

- `scripts/ci/` 是受保护路径，需要负责人的 ack-policy（由调度员 ack）。建议把 `.gitleaks.toml` 也加入受保护路径清单。
- 配置变更的节奏：调整 allowlist 的提交要先单独发布，因为扫描用的是 base 的受信配置。

## 6. 发现命中时的处置流程（按 L6 建议 6 补充）

1. **在待推送、尚未发布的提交里命中**：按 AGENTS.md 的规定，用交互式 rebase 或 filter-repo 把它从未推送的历史中移除，然后重扫。如果确认是误报，应当按 §2 的标准新增一条窄 allowlist：先单独提交，经 owner ack 并发布，而不是绕过门禁。
2. **在已发布的历史（main 或 GitHub）里发现真实凭据**：
   - **不改写已发布的历史，不 force push main**（AGENTS.md）；
   - **立即轮换或吊销**该凭据，这是唯一有效的补救，因为公开仓库可能已被缓存或克隆；
   - 立即报告调度员和负责人，只说明位置和类别，不转述凭据值；
   - 用 ADR 或事件记录写明：影响范围、轮换时间、吊销确认，以及为什么此前的门禁没有拦住，并补上对应的规则或测试。

   只有负责人另行决定时，才考虑改写历史。
3. **全量扫描（`--full`）发现历史中的命中**：先分诊（误报还是真实凭据），真实凭据按第 2 条处理，误报按 §2 的标准评估是否新增窄 allowlist。

## 7. 测试与演示

环境：`PYTHONPATH=packages/eios-core/src:tests`，`uv run --frozen`，gitleaks 8.30.1（Homebrew）。没有 PG 测试。

| 命令 | 结果 |
|---|---|
| `uv run --frozen python scripts/ci/test-prepush-scan.py`（第一稿） | 两行 PASS。用例包括：(1) 合成的假 AWS 访问键、假 GitHub token 各提交一次又删除，普通、`--hook`、`--full` 三种模式都被拦，输出中不含假值；(2) 测试路径下的合成幂等键放行，同一个值放在非测试路径被拦；(3) base 之前的旧泄露，普通模式放行，`--full` 被拦；(4) 没有工具、版本不对，失败关闭。假凭据都在运行时拼接 |
| 同上（第二稿，确认级 1、2） | 两行 PASS。新增：(5) 待推送提交删除、掏空、或放成全放行（`regexes = ['''.''']`）的 `.gitleaks.toml`，再带一个假 AWS 键，三种情况都仍然被拦，并输出 `owner ack required`；(6) base 本身发布的是全放行配置时，报 `breaks the structure rules`；(7) base 没有配置时，回退到检出配置（输出 `(checkout configuration)`），假凭据仍被拦；(8) 行内带 `gitleaks:allow` 的假凭据被拦；(9) 用 gitleaks 自己算出的 fingerprint 写成 `.gitleaksignore`，未跟踪或已跟踪两种情况都失败；(10) 一个回显 8.30.1 却永远 exit 0 的桩程序，被哨兵挡下 |
| `uv run --frozen python scripts/ci/test-prepush-scan.py`（第三稿） | 两行 PASS。新增 (11)：从一个只含 `scripts/ci` 的模拟受信副本运行时，配置取自 base（输出 `(base configuration)`），干净提交通过、假凭据被拦；base 也没有 `versions.lock.json` 时，输出 `FAIL gitleaks pin unreadable …` |
| `pytest tests/test_gitleaks_config.py`（第二稿） | 仓库配置通过检查器；15 种放宽方式（关闭默认规则、禁用或覆盖规则、全局 allowlist、改 minVersion、第 4 条 allowlist、换规则、条件改 OR、`.`、`^.*$`、任意目录、未锚定、stopwords、只有值没有路径等）**全部被拒** |
| `pytest tests/test_gitleaks_config.py tests/test_github_sync.py tests/test_history_audit.py tests/test_release_compliance.py`（第二稿） | 62 passed |
| `pytest tests/test_github_sync.py` | 首次 5 failed（临时仓库没有 `.gitleaks.toml`，新门禁按设计失败关闭）；在 lab 的 base 提交里带上真实配置后 10 passed |
| `pytest tests/test_public_repository_checks.py tests/test_license_compliance.py` | 48 passed |
| `bash scripts/ci/prepush-scan.sh HEAD 1892c9ac`（本分支首次提交后） | **首次失败**：`FAIL gitleaks: 1 finding(s) in outgoing added lines`。命中的是 `scripts/ci/test-prepush-scan.py` 中测试用的合成幂等键字面量：它不在 `tests/` 路径下，allowlist 2 不放行。改为运行时拼接后重扫 |
| 同上（修正后，以及第二稿） | 四道门全部 PASS；第二稿还输出 `NOTE .gitleaks.toml changes … owner ack required`，因为 base 中还没有这个文件，所以用的是检出配置 |
| `bash scripts/ci/prepush-scan.sh --full HEAD` | PASS（全部历史 0 命中） |
| `NEXLOOP_GITLEAKS=/nonexistent bash scripts/ci/prepush-scan.sh --full HEAD` | `FAIL gitleaks not installed (pinned 8.30.1)`，exit 1 |
| `install-gitleaks.sh`：用本机二进制自制同名 darwin_arm64 archive 加 checksums，只在 scratch 目录中执行 | 正常路径：安装，并打印两个摘要。checksums 不符：`refused: archive digest differs from the published checksum`，exit 2。名字不对：`refused: archive must be …`，exit 2。被拒时都没有安装任何东西。这只验证脚本逻辑，不代替 CI 主机上用官方 archive 的首次安装 |

文档同步：`AGENTS.md` 与 `GOVERNANCE.md` 中“三道门禁”的描述改为四道。
