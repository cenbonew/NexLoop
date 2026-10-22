# NX-060 后续：合入 main 与推送 GitHub 之前，结构性地经过四道发布门禁

- **来源**：调度员 2026-10-22 在 CI 主机上核实了两件事，然后派工给 L7：
  - `nexloop-ci-controller.service` 的 ExecStart 没有 `--merge-gate`；
  - `nexloop-ci-sync.service` 运行 `nexloop_ci.py sync … --remote github`，只快进推送，不扫描。

  也就是说，**向公开 GitHub 推送之前，系统层面没有任何自动扫描**，只靠调度员每批推 integration 之前手工跑一次 prepush-scan。
- **分支**：`l7/sync-scan-gate`。**叠在 `l7/gitleaks`（`19b3ef19`）之上**，而不是 main `1892c9ac`：
  - “四道门”中的第四道（gitleaks）只在那个分支上；
  - 两个分支都要改 `scripts/ci/prepush-scan.sh`，从 main 分出去必然冲突。

  `19b3ef19` 已定随第二十一批合入。
- **受保护路径**：本任务修改了 `scripts/ci/` 和 `deploy/stage/ci/`，需要负责人的 ack-policy。主机上的 unit 与受信安装的变更由负责人确认、调度员执行（OPERATIONS §7.2a）。L7 没有连接任何主机。

## 1. 改了什么

### 1.1 `scripts/ci/prepush-scan.sh`：在 bare 仓库里也完整生效

- **原来的问题**：
  - 脚本开头是 `cd "$(git rev-parse --show-toplevel)"`。在 bare 仓库里命令替换失败，但 `cd ""` 什么也不做，脚本并不退出，继续往下跑；
  - 第一道门用的是 `git ls-files`，在 bare 里拿到空列表，**私有路径检查形同虚设**；
  - 本次又发现两处：第三道门 `git log -p | grep` 在 pipefail 下，`git log` 出错会被 grep 的 1 掩盖；各项前置检查失败时是被 `set -e` 静默终止，没有 FAIL 行。
- **现在的行为**：
  - 开头判断仓库类型：bare 仓库进入 git 目录，检出进入顶层目录，都不是则输出 `FAIL prepush-scan: not inside a git repository`。
  - **每一道门都通过 git 读取被扫描的提交**，不读工作树：
    - 第一道门检查三处：被扫描 ref 的树（`git ls-tree`）；范围内**每个提交动过的路径**（`git log --name-only`），在范围内加入又删除的私有路径同样会被拦，因为它仍会随历史公开；在检出中还额外检查 index（`git ls-files`），保持原来的保护；
    - 第二道门（受限标识）：`git grep` 按每批 200 个提交扫描所有待推送提交的树，这样全部历史也不会超出参数长度。退出码 0 为命中，1 为未命中，其他为出错，三者分开判断；
    - 第三道门（凭据值）：用 `PIPESTATUS` 分别判断 grep 与 `git log`；
    - 第四道门（gitleaks）：本来就以 git 目录为 source。
  - 前置检查失败时都输出 FAIL 行：ref 或 base 不是提交、路径列表或提交列表取不到、扫描没有完成。
  - **新增 `--root <ref>` 模式**：在没有基线时，对全部历史运行四道门禁，用于首次同步。本仓库全部历史（约 2000 个提交）的第二道门约 80 秒，只在首次同步时发生一次。

### 1.2 `scripts/ci/nexloop_ci.py`：合入和同步都必须先过扫描

- **`prepush_scan(repo, sha, base, restricted_identifiers)`**：
  - 以 bare 仓库为 cwd，运行**本受信安装自带的** `prepush-scan.sh`（从不使用候选代码中的副本）；
  - 扫描范围是 `base..sha`；base 为空时用 `--root sha`；
  - 环境为 `_stage_env` 加上 `NEXLOOP_RESTRICTED_IDENTIFIERS_FILE`，超时 900 秒；
  - 返回 None 表示通过，否则返回第一条 FAIL 行。脚本只输出代号，不输出内容；
  - 清单缺失、超时、任何一道门失败，都算拒绝。
- **合入（`merge`）**：
  - 其它检查都通过之后，对 **main..候选** 运行扫描，失败记为 `merge_scan_failed`，结果中附带 `scan`（失败的门禁），main 不动；
  - `run-once` 的 `--restricted-identifiers` 是可选的：只跑 check 的车道（例如发版车道）可以不带。但不带时，**任何合入都会以 `merge_scan_failed` 被拒**。原有的 `--merge-gate` 继续可用，与扫描叠加；
  - 测试 `test_a_merge_needs_the_publication_scan_of_main_to_candidate` 覆盖这些情况。
- **同步（`sync`）**：
  - `--restricted-identifiers` **必填**，argparse 层面就拒绝；Python 接口里这是没有默认值的关键字参数。因此不存在不扫描的同步代码路径。
  - 推送之前，对“上次已同步的 main（`last_synced`）..main”运行扫描。`last_synced` 为空时（从未同步过）扫描全部历史；main 没有变化时不扫描。
  - 扫描失败记为 `sync_blocked`：
    - 状态中的 `blocked` 为 `{sha, gate}`，同时记录 backlog 和 failures；
    - **这一轮什么都不推**：main、所有 review 分支、tag 都不推；`last_synced` 保持不变；
    - CLI 以 1 退出，unit 因此失败，可以在 journal 和 `systemctl --failed` 中看到；
    - 之后每一轮（timer 每分钟一次）都重新扫描，直到 main 不再失败为止。如果泄露已经在 main 的历史中，按规则不能改写历史，同步会一直保持 blocked，必须先轮换凭据，再由负责人决定如何处理。
  - 每个 `review/*` 分支只扫描它自己的新提交：从该分支上次已同步的 SHA 开始，没有则从 main 开始。失败时只有这个分支记为 `sync_blocked`，其余照常。
  - `status` 的 `sync` 汇总中新增 `blocked`（失败的门禁）和 `review_blocked`（分支名列表）。
- **`scripts/release/sync_github.py`**（NX-034 的手动同步）：调用 `ci.sync` 时传入同一份清单，所以会在它自己的门禁之后再扫描一次；`ci.sync` 返回 `sync_blocked` 时，记为 `scan_failed`，不再误标为 `network_unreachable`。
- **`scripts/release/history_audit.py`**：提取第三道门表达式的正则改为同时接受 `grep -E` 与 `grep -qE`。表达式本身没有变化，`tests/test_history_audit.py` 照样核对两者一致。

### 1.3 模板与文档

- `deploy/stage/ci/nexloop-ci-controller.service.example`、`nexloop-ci-sync.service.example`：ExecStart 末尾加 `--restricted-identifiers <GIT_ROOT>/private/restricted-identifiers.txt`，并附注释说明。
- `deploy/stage/ci/README.md`：增加清单文件一行，并更新两个 unit 的说明。
- `docs/OPERATIONS.md`：
  - §7.2 的展开清单加上 `.gitleaks.toml versions.lock.json`；
  - 新增 §7.2a，写明首次启用的步骤、验证与回退方法，以及出现 `sync_blocked` 时如何处理；
  - §7 中对同步状态的说明补上 `sync_blocked`。
- `docs/implementation/NX-060-gitleaks.md` §5：更新表格中同步服务与控制器两行的结论，并注明 bare 仓库的问题已修复。

## 2. 主机上的变更（B 类，负责人确认后由调度员执行；详见 OPERATIONS §7.2a）

1. 放置私有清单 `<GIT_ROOT>/private/restricted-identifiers.txt`，内容来自负责人的 `local-only/restricted-identifiers.txt`。权限要保证 `nexloop-ci` 与 `nexloop-git` 能读、其他账户不能读。
2. 按 §7.2 升级受信安装，使用包含本改动的 main SHA 和新的展开清单。
3. 两个 unit 的 ExecStart 都加上 `--restricted-identifiers <GIT_ROOT>/private/restricted-identifiers.txt`，路径换到新的受信安装，然后 `daemon-reload`。改动对照：

   ```
   控制器：… run-once --repo <GIT_ROOT>/nexloop.git --queue <GIT_ROOT>/queue --policy <CI_ROOT>/lane-policy/<sha12> --identities <GIT_ROOT>/private/identities.json --stages-version 4 --restricted-identifiers <GIT_ROOT>/private/restricted-identifiers.txt
   同步：  … sync --repo <GIT_ROOT>/nexloop.git --queue <GIT_ROOT>/queue --remote github --timeout 60 --tag-signers <GIT_ROOT>/private/allowed_signers --restricted-identifiers <GIT_ROOT>/private/restricted-identifiers.txt
   ```

4. 验证：同步跑一轮，结果为 `synced` 或 `sync_pending`，不应出现 `blocked`；下一次合入没有 `merge_scan_failed`。
5. 回退：改回旧路径，去掉新参数，执行 `daemon-reload`。

**上线顺序**：

- 必须**先完成第 1 步再改 unit**。同步服务一旦带上参数但清单不存在，就会一直 `sync_blocked`。这是失败关闭，不会推出任何东西。
- 控制器缺清单时会拒绝所有合入。
- 两个 unit 不改时，旧的受信安装仍然按原来的方式运行：不扫描，但也不会出错。所以第 2、3 步应当一起完成。

## 3. 测试

环境：`PYTHONPATH=packages/eios-core/src:tests`，`uv run --frozen`，gitleaks 8.30.1（Homebrew）。没有 PG 测试。开发机负载约 7。

| 命令 | 结果 |
|---|---|
| `uv run --frozen python scripts/ci/test-prepush-scan.py` | 三行 PASS。新增用例：bare clone 中的干净提交，四道门都 PASS；`--root` 通过；在范围内加入又删除的 `local-only/…`、`deploy/secrets/…`、假 AWS 键，在 bare 普通模式和 `--root` 下都被各自的门禁拦下；在非 git 目录中运行、ref 不存在、base 不存在，三种情况都有 FAIL 行 |
| 同上，改动过程中 | **首次失败**：原有用例“`.env` 已加入暂存区但未提交”不再被拦，因为新的第一道门最初只看提交。改为在检出中额外检查 index，成为原行为的严格超集 |
| `pytest tests/test_ci_queue.py`（新增 5 个用例） | (1) `test_the_sync_never_pushes_an_unscanned_leak_and_stays_blocked`：假凭据、私有路径、受限标识三种泄露，各自进入 main 后又被删除，同步都是 `sync_blocked`，GitHub 的 main 不动；CLI 以 1 退出；`status` 中有 `blocked`；输出不含泄露内容；改回干净的 main 后同步恢复，`blocked` 清除。(2) review 分支带泄露时只拦这一个分支。(3) 首次同步扫描全部历史，看到早已删除的旧泄露并拦下；清单缺失时为 `sync_blocked`，GitHub 上仍然没有 main。(4) 三种泄露的候选都能通过 CI check，但合入时都是 `merge_scan_failed`，main 不动；不带清单时合入被拒，带上清单后合入成功。(5) 两个 unit 模板的 ExecStart 都带 `--restricted-identifiers`；`sync` 不带清单时 argparse 拒绝 |
| `pytest tests/test_ci_queue.py tests/test_ci_isolation.py tests/test_github_sync.py tests/test_ci_stages.py tests/test_release_ci_lane.py tests/test_history_audit.py tests/test_release_compliance.py tests/test_gitleaks_config.py -n 3` | **首次失败** 2 个：`test_release_ci_lane` 只跑 check 的 `run-once` 没有传清单，因为当时把它设成了必填。改为可选（没有清单时合入一律被拒）之后，**首次失败** 6 个：`history_audit` 提取不到第三道门的表达式，因为写法由 `grep -E … \|\|` 改成了 `grep -qE`。放宽它的提取正则后，**109 passed** |
| `bash scripts/ci/prepush-scan.sh HEAD 1892c9ac` | 四道门全部 PASS（附带预期的 NOTE：base 中还没有 `.gitleaks.toml`） |

## 4. 风险与限制

- **清单对 `nexloop-ci` 可读**：控制器以 `nexloop-ci` 运行合入扫描，而在方案 L 下，候选代码的测试阶段也以同一个账户运行，所以理论上候选测试能读到这份清单。
  - 现有的缓解：只有评审过的代码才会入队，外部 PR 不入队（D3）；测试阶段只能经允许名单代理联网，nft 规则按 UID 拦截对 LAN 和数据主机的访问。
  - 方案 P（每个 job 一个 rootless Podman 容器）可以彻底隔离。
  - 如果负责人不接受这个暴露，备选做法是：合入时只跑第 1、3、4 道门，第 2 道门只在同步（`nexloop-git`，不运行候选代码）时执行。公开边界仍然有四道门。**需要负责人拍板**。
- **tag 的附注文本不在扫描范围内**：tag 只有在其指向的提交属于 main 时才会推送，而 main 已经扫描过；但附注 tag 的消息文本没有经过第 2、4 道门。负责人签名的版本 tag 由负责人本人撰写，风险低，记为限制。
- **首次同步的耗时**：`last_synced` 为空时需要扫描全部历史，约 1–2 分钟。主机上的 sync 状态已经有 `last_synced`，正常情况下不会走这条路径。
- **blocked 不会自动解除**：泄露进入 main 之后，同步会一直阻塞。这是有意的：公开边界之前宁可停，也不推。处置流程见 OPERATIONS §7.2a 与 NX-060-gitleaks §6。

## 5. 第二稿：按 L6 从严复核修订（`9279069c` 之后）

### 5.1 确认级 1：第一道门可被非 ASCII 路径绕过（已修）

- **原因**：`git ls-tree --name-only`、`git log --name-only`、`git ls-files` 在 `core.quotePath=true`（git 的默认值）时，会给含非 ASCII 字节、引号、反斜杠、控制字符的路径加引号，例如 `"local-only/n\303\251.txt"`。以 `^` 锚定的 `docs/tmp`、`local-only`、`\.env$` 于是全部匹配不到。开发机全局设置了 `core.quotePath=false`，所以本机测试测不出来；CI 主机与 bare 仓库用的都是默认值。
- **修复**：三处列举一律改为 `git -c core.quotePath=false … -z`。路径按 NUL 分隔交给 `path_rule`（Python，`re.S`）逐条匹配，路径中含换行也按它本身匹配。`log --name-only` 同时加了 `-m`，见 5.2。`.gitleaksignore` 的检查也改为 `-z`。
- **一并采纳建议 7**：`.env` 规则从“仅根目录”改为“任意目录下的 `.env`”，即 `(.*/)?\.env`。全部历史中没有命中，不会误报。
- **测试**：`test-prepush-scan.py` 在整个运行过程中用 `GIT_CONFIG_COUNT` 强制 `core.quotePath=true`，不受本机配置影响。以下 9 个路径，在检出和 bare 两种方式下都被第一道门拦下：`local-only/né.txt`、`local-only/a b.txt`、`local-only/line⏎break.txt`、`local-only/q"uote.txt`、`local-only/back\slash.txt`、`docs/tmp/ü.md`、`sub/dir/.env`、`é/secrets/k.txt`、`cache/é.sqlite`。另做了对照：用上一版脚本跑同一测试，在 `local-only/né.txt` 上 PASS，证实缺陷可以复现。

### 5.2 确认级 2：合并提交自己引入的内容不被看到（已修）

- **原因**：默认的 `git log -p` 不输出合并提交的 diff。在冲突解决时加入、随后又被删除的值（evil merge），在范围内只剩下 `-` 行。
- **锁定版本上的实测**（gitleaks 8.30.1，scratchpad 中构造的 evil merge）：

  | 写法 | 第三道门命中 | gitleaks |
  |---|---|---|
  | 默认 | 0 | 不报（exit 0） |
  | `-m` | 2 | 报出（exit 17） |
  | `--diff-merges=first-parent` | 1 | 报出 |
  | `--cc` | 1 | 不报（gitleaks 解析不了合并 diff 的格式） |

  所以**第四道门原来也看不到 evil merge**。
- **修复**：第一、三、四道门都改用 `-m`：第一道门是 `log -m --name-only`，第三道门是 `git log -p -m`，第四道门是 `--log-opts="-m <range>"`。`-m` 在各个 git 版本上都可用，而 `--diff-merges` 需要 git 2.31 以上。本仓库全部历史在 `-m` 下约 219 MB、2 秒，代价可以接受。第二道门本来就用 `git grep` 扫描每个提交（包括合并提交）的完整文件树，不受影响。
- **测试**：构造三种 evil merge，在检出和 bare 两种方式下分别被各自的门禁拦下：
  - 只含 `sk-…`：第三道门；
  - 只含假 AWS 键：第四道门；
  - 合并时加入 `local-only/merged.txt`、再由另一个合并删除：第一道门。

  对照：上一版脚本对同一个 evil merge 四道门全部 PASS。

### 5.3 当前 main（`1892c9ac`，三道门）是否也有这两个缺陷

| 缺陷 | main 上的 prepush-scan | 结论 |
|---|---|---|
| quotePath | 第一道门用 `git ls-files`，即工作树的 index，同样受 quotePath 影响；它也从来不看待推送提交动过的路径 | **在 quotePath 为默认值的环境中是现存缺陷**。调度员与各开发线所在的开发机全局设置了 `core.quotePath=false`，在那里运行的扫描不受影响 |
| evil merge | 第三道门 `git log -p` 不带 `-m` | **现存缺陷，在任何环境下都存在** |
| — | 第二道门对每个提交（包括合并提交）的完整文件树执行 `git grep` | 不受影响 |

**对已推送历史的复扫（只读）**：使用修复后的脚本，并强制 `core.quotePath=true`。

- 全部历史中的路径（所有分支，含合并的 `-m` 视角），共 3183 个：**没有任何非 ASCII 路径**；新规则（含任意目录下的 `.env`）下 **0 命中**。也就是说，quotePath 这条绕过在历史中没有被利用过。
- 调度员另行只读核实：github/main 的全部历史中没有非 ASCII 路径，也没有 `local-only/`、`docs/tmp/` 路径。
- **结论（quotePath）**：当前 main 的第一道门在机制上同样受影响；但已推送的历史中没有任何会被它漏掉的路径，所以**没有实际发生过的绕过**，不需要因此复扫或补救。evil merge 的结论见 5.5。
- 复扫结果：见 5.5。

### 5.4 其余各条

- **重要 3（`sync_github._prepush` 没有超时）**：已修，改用 `ci.run_process_group`，超时为 `PREPUSH_SCAN_TIMEOUT`（900 秒），超时记为 `scan_timeout`。测试：`test_a_scan_that_does_not_finish_blocks_with_scan_timeout`。
- **重要 4（超时只杀 bash）**：已修。`run_process_group` 以 `start_new_session=True` 启动，超时后用 `os.killpg` 杀掉整个进程组；`prepush_scan` 与 `sync_github` 共用它。测试：`test_a_scan_timeout_kills_the_whole_process_group`，后台的孙进程 `sleep 60` 在超时后被杀掉。
- **重要 5（`--root` 依赖新的展开清单）**：OPERATIONS §7.2a 第 2 步已写明必须使用新清单，否则会一直 blocked。
- **重要 6**：只作记录，与本提交无关。
- **建议 8**：第一道门的注释已写明，在检出中检查 index 可能因为“已暂存、但不在被扫描 ref 中”的路径而拒绝，属于失败关闭。
- **建议 9（tag 附注文本）**：未改动，仍记为限制（§4）。
- **建议 10（首次全量扫描的耗时）**：本仓库在开发机上 `--root` 的实测见 5.5。CI 主机上如果超过 900 秒，会一直 `sync_blocked`；处理方法是由维护者确认已公开的 main，写入 `last_synced` 作为基线，与 `sync_github --baseline` 相同。主机上已有 `last_synced`，正常情况下不会走 `--root`。

### 5.5 已推送历史的复扫结果（2026-10-22，只读，修复后的脚本，强制 `core.quotePath=true`）

范围：`refs/remotes/github/*`，即最近一次 fetch 时 GitHub 上的 main 与 17 个其它分支。只输出数量、路径和提交日期，不输出任何受限标识或命中内容。

| 检查 | 结果 | 分类 |
|---|---|---|
| 当前 `github/main` 树：第二道门（受限标识） | **0 命中** | — |
| 本分支 HEAD 的树：第二道门 | 0 命中 | — |
| 17 个其它 GitHub 分支相对 `github/main` 的新提交：四道门 | 全部 PASS | — |
| `github/main` 全历史（`--root`）：第一道门 | PASS | — |
| `github/main` 全历史：第二道门 | FAIL | **历史遗留，已知**，见下文 |
| `github/main` 全历史：第三道门（`-m`，单独运行） | 1 处，带不带 `-m` 都是 1 处，所以**不是合并引入的** | **误报**，见下文 |
| 所有 GitHub 分支全历史：gitleaks（`-m`，带配置） | **0 处** | — |
| 所有分支全部历史中的路径（`-m`），共 3183 个 | 没有非 ASCII 路径；新规则 0 命中 | — |

**第二道门的全历史命中 = 2026-10-11 清理之前的旧提交（已知、负责人知情、未改写历史）**：

- 2026-10-11 当天及之后，`github/main` 共有 1506 个提交，其中 43 个的树中有受限标识，**全部是 2026-10-11 当天、清理提交之前的提交**。
- 最后一个命中的是 `1fbd2fe9`（2026-10-11 09:12:14 +0800）。紧接着的 `eb51df43`（09:17:00，“Publication hygiene: development machine host name in evidence files replaced…”）就是负责人批准的清理提交，从它开始的提交全部干净。
- 命中所在的路径都是当时的交接与证据文档：`docs/handoff/CODEX_START_PROMPT.md`、`docs/implementation/` 下 140 余个 `*-ci.json`、`*-progress.md`、`*-evidence.json` 与设计说明，以及 `planning/tasks.json`。路径本身都不含受限标识。
- 10-11 之前更早的提交也有命中，属于同一批已知的历史遗留。
- **新增命中：0**。按 AGENTS.md，已推送的 main 不得 force push，所以这部分历史不改写。负责人在 2026-10-11 批准清理时已经知情。

**第三道门全历史的 1 处是误报**：

- 位置：提交 `3b2ead23`（2026-10-08），`apps/agent-host/test/runtime-host-model.test.ts`。
- 匹配形式是 `process.env.MODEL_API_KEY=<8 个小写字母>`。核实发现，这 8 个字母是同一文件中由 `process.env.MODEL_API_KEY` 赋值而来的 JS 变量名：测试结束时把保存的旧值赋回环境变量，不是凭据字面量。
- 当前 `github/main` 树中已经没有这一行。

**对首次同步（`--root`）的影响**：同步状态一旦为空，`--root` 会因为上述历史遗留一直 `sync_blocked`。这是失败关闭，不会推出任何东西。CI 主机上的同步状态已经有 `last_synced`，正常情况下不会走 `--root`。如果将来需要从空状态开始，要由维护者确认已公开的 main，并把它写入 `last_synced` 作为基线（与 `sync_github --baseline` 相同），而不是放宽门禁。

**耗时（建议 10）**：开发机负载约 7–12 时：`github/main` 的 `--root` 在第二道门命中后立即停止；17 个分支逐个扫描共约 7 分钟；逐个提交复核 1506 个提交约 6 分钟。
