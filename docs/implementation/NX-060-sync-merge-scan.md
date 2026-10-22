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
