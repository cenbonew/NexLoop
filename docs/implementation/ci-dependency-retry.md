# CI 依赖阶段的有限重试（调度员 2026-10-28）

第 49 批 CI seq 176 的 `dependency-python` 阶段在 93 秒时以 `dependency_unavailable` 失败，没有跑任何测试。重排后同样的依赖又通过了。判断是包镜像或网络的偶发问题，但白白多跑了一轮约 1.5 小时的 CI。本分支 `l7/ci-dep-retry` 基于 c7f00079。

## 现状

- 两个依赖阶段是 `.ci-policy/stages.v4.json` 里 `network: "dependency"` 的 `dependency-python`（`scripts/ci/stages/dependency-python.sh`：`uv sync --frozen`，再用 `uv build`、`uv export`、`uv pip install --require-hashes` 预热离线测试要用的缓存）和 `dependency-node`（`scripts/ci/stages/dependency-node.sh`：`pnpm install --frozen-lockfile`）。只有这两个阶段允许访问外网（经代理）。
- 阶段由受信控制器 `scripts/ci/nexloop_ci.py` 的 `run_check` 逐个执行，只执行一次：退出码非 0 就记为 `dependency_unavailable`，准备阶段在第一个失败处停下。
- 阶段脚本来自候选提交，重试和分类必须放在受信控制器里，不能放在阶段脚本里。

## 改动（只改 `scripts/ci/nexloop_ci.py`）

- 新函数 `run_stage`：`network` 为 `dependency` 的阶段最多运行 `DEPENDENCY_ATTEMPTS = 3` 次，第 1、2 次失败后分别等待 30 秒、90 秒（`DEPENDENCY_BACKOFF_SECONDS`）。其他阶段仍只运行一次。
- `dependency_failure_class` 按阶段输出分类，只在内存中读输出：
  - 先检查**不重试**的类别，命中就立即结束：
    - `integrity`：`Hash mismatch`、`ERR_PNPM_TARBALL_INTEGRITY`、`EINTEGRITY`、校验和不符；
    - `lock`：uv 的 lockfile needs to be updated、`ERR_PNPM_OUTDATED_LOCKFILE`、`ERR_PNPM_LOCKFILE`；
    - `resolution`：`No solution found`、`ERR_PNPM_NO_MATCHING_VERSION`、`ERR_PNPM_PEER_DEP`；
    - `parse`：`Failed to parse`、TOML/YAML 解析错误、`ERR_PNPM_BROKEN_LOCKFILE`。
  - 以上都没命中时，只有输出含网络或镜像不可用的错误才算 `unavailable`、可以重试。这类错误包括连接重置、超时、DNS、`ECONNRESET`/`ETIMEDOUT`/`EAI_AGAIN` 等、`ERR_PNPM_FETCH_5xx`、`ERR_PNPM_META_FETCH_FAIL`、uv 的 `Failed to fetch`/`Failed to download`/`error sending request`，以及 502/503/504。
  - 阶段超时（`timeout`，1800 秒）不重试：三次就是 1.5 小时。认不出的失败（`unknown`）也不重试。
  - 同一份输出里既有网络错误、又有校验和不符时，按校验和不符处理，不重试。
- 结果文件新增三个字段（对 `nexloop-ci-result/1` 只增不改），只在依赖阶段的条目里有：
  - `attempts`：运行次数；
  - `retries`：每次被重试的运行一条 `{exit, class}`；
  - `failure_class`：最终失败的类别，通过时为 `null`。
- **状态语义（L6 I1）**：失败的依赖阶段只在 `failure_class` 为 `unavailable` 或 `timeout` 时记 `dependency_unavailable`（可能是偶发，可以重排）；`integrity`、`lock`、`resolution`、`parse` 和认不出的 `unknown` 一律记 **`dependency_failed`**，表示真实的依赖问题，重排不会好，要先修。以前所有依赖失败都叫 `dependency_unavailable`，第 49 批就是凭这个名字判成偶发的。`unknown` 也归为真实失败：没有证据表明是网络问题时，不给出“可能偶发”的暗示；如果日后发现某种网络错误被认成 `unknown`，就把它加进可重试的模式。
- `judge` 只看阶段是否 `passed`，所以 `dependency_failed` 同样让结果失败，release 和合并门不受影响。结果里只有类别代码，不保存输出，也不保存其中的地址。
- **重试前清理（L6 S1）**：每次重试之前，先删掉本阶段上一次运行留下的半截输出，避免在不完整的目录上重跑：`dependency-python` 删检出里的 `.venv`，`dependency-node` 删每个 `node_modules`（不包括其他 `node_modules` 里面的）。只在检出目录里找，不进 `.git`；符号链接只删链接本身，从不跟随；不碰其他阶段的输出（重跑 `dependency-node` 时不删 `.venv`，`static` 阶段已经用过它）。
- 重试在同一个检出目录里重跑同一个阶段脚本（先按上一条清理）；预热用的临时目录每次重新 `mktemp`。

## 测试（`tests/test_ci_queue.py`）

- `test_a_dependency_stage_unavailable_twice_passes_on_the_third_run`：前两次输出连接重置、退出 2，第三次成功。结果通过，`attempts` 3，两条 `unavailable` 的重试，两次等待依次是 30 秒、90 秒；结果 JSON 里没有输出原文。
- `test_a_dependency_stage_unavailable_every_time_stops_after_three_runs`：每次都超时。正好运行 3 次后以 `dependency_unavailable` 失败，后面的阶段不再运行。
- `test_a_dependency_failure_a_retry_cannot_fix_fails_at_once`（7 组参数）：校验和不符（uv 与 pnpm 各一组，uv 那组同时含连接重置）、lock 不一致（uv 与 pnpm）、解析失败、无解、未知错误。每组都只运行 1 次，不等待，`failure_class` 为对应类别。
- `test_a_dependency_timeout_and_other_stages_are_never_retried`：超时归为 `timeout`；7 个类别对应的状态（两个 `dependency_unavailable`，五个 `dependency_failed`）；非依赖阶段即使输出网络错误也只运行一次。
- 上面不可重试的 7 组参数都断言状态为 `dependency_failed`；`test_a_dependency_cache_miss_fails_explicitly`（没有任何输出的失败）改为断言 `dependency_failed` 和 `unknown`。
- `test_a_retry_starts_without_the_failed_run_s_outputs`（python、node 两组）：第一次运行留下半截目录后以网络错误失败；如果目录还在，第二次运行会以退出 9 失败。结果通过、运行 2 次，说明重试前目录已清掉。node 一组含根目录、`apps/web` 与 `packages/x` 三处 `node_modules`。
- `test_clearing_outputs_removes_a_link_and_never_follows_it`：指向检出外的 `node_modules` 链接只删链接，外部文件仍在；嵌套的 `node_modules` 随外层一起删；`.venv` 不受 node 阶段的清理影响。

## 部署到 CI 主机（合入 main 之后，调度员按 ack-policy 另做一步）

本改动在 `scripts/ci/` 下，候选会被判为策略变更，合入要先取得 owner 的 `ack-policy`。合入不会改变 CI 主机上正在运行的控制器：控制器、hook 和同步服务运行的都是受信副本 `<TRUSTED_CI_DIR>`（`/opt/nexloop/ci-trusted/<sha12>`），要另外安装。

1. 取合入后 main 的提交 `<MAIN_SHA>`。按 `docs/release/run-nx033-switch-20261020.md` §3.4，把 `scripts/ci`、`scripts/deploy/probe_isolation.py`、`.ci-policy`、`deploy/stage/ci` 展开到新的 `/opt/nexloop/ci-trusted/<MAIN_SHA 前 12 位>`。文件属 root、不可被 `nexloop-ci` 写，旧目录保留。
2. 等控制器空闲：队列里没有正在运行的检查。也就是没有 `running` 状态，或者 timer 的这一次已经结束。
3. 把两个 hook（`<GIT_ROOT>/nexloop.git/hooks/pre-receive`、`post-receive`）、控制器 unit 与同步 unit 里的路径改到新目录，然后 `systemctl daemon-reload`。
4. 核对：
   - `sha256sum <新目录>/scripts/ci/nexloop_ci.py` 与 `<MAIN_SHA>` 中的文件一致；
   - 下一次检查的 `result.json` 里，`controller_version` 是新文件的 sha256，依赖阶段的条目带 `attempts` 字段。
   - ack 记录里写明新的状态语义：`dependency_unavailable` 只表示可能偶发，`dependency_failed` 表示真实依赖问题。按 `dependency_unavailable` 重排的操作说明（`NX-034-release-ci-queue.md` 第 157 行那一行）仍然适用；遇到 `dependency_failed` 不重排，先修 lock 或依赖。
5. **回退**：把 hook 和 unit 改回旧目录，再 `daemon-reload`。结果文件新增的字段是只增的，旧控制器不读它们。
