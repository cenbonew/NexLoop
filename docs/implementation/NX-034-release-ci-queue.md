# NX-034：在现有 CI 车道上按 NX-033 队列产出发布用 CI 结果

依据：调度员 2026-10-14（负责人已批准首个签名发布，NX-034 方案 A）。

`scripts/release/release.py build` 的 `--ci-result` 必须是 NX-033 队列格式的 `result.json`：
- `release.py` 会拒绝的情况：`sha` 不是这个提交，或 `status` 不是 `passed`（release.py:164–167）；
- 写进清单的字段：`policy_version`、`deadline_gate`、`counts`。

现有车道脚本产出的结果只有 sha、label、exit_code，格式对不上，也不能手工改造。本手册给出最小路径：以 CI 身份，在现有车道上用 `scripts/ci/nexloop_ci.py` 的队列（enqueue、run-once、result）产出这份结果。不切换 remote，不改 systemd，不建新系统用户。

本文只用参数和占位值，真实值都在 CI 主机的私有文件里。

## 1. 参数

| 参数 | 含义 |
|---|---|
| `<CI_USER>` | 现有 CI 身份（运行车道脚本的那个用户） |
| `<GIT_REPO>` | 现有 bare 仓库（调度员推入 `refs/ci/<SHA>` 的那个） |
| `<CI_ROOT>` | CI 身份可写的工作根目录 |
| `<QUEUE_DIR>` | 队列目录，建议 `<CI_ROOT>/queue`。每次都用参数传入，不写死在程序里 |
| `<TRUSTED_CI_DIR>` | 受信安装：main 上已评审提交的 `scripts/ci/` 与 `.ci-policy/`，从不使用候选代码中的副本（NX-033 §4.3） |
| `<TRUSTED_SHA>` | 受信安装取自的 main 提交，必须已包含 `scripts/ci/lane_policy.py` |
| `<LANE_POLICY_DIR>` | 车道 policy 实例（由 `lane_policy.py` 生成，写一次） |
| `<PRIVATE_DIR>` | 私有文件目录，权限 0700，文件权限 0600 |
| `<RELEASE_CI_IDENTITY>` | 入队身份，见 `deploy/stage/ci/release-lane-identities.example.json` |
| `<SHA>` | 待发布的提交（40 位） |
| `<TAG>` | 本次发布的短标签，用在 integration ref 名里，例如版本号 |
| `<RELEASE_ROOT>` | `release.py build` 的输出根目录 |

需要 Python 3.11 及以上（控制器用到 `datetime.UTC`）和 git。只用标准库，不需要额外安装。

## 2. 各项的确定方式

- **队列**：`<QUEUE_DIR>`。控制器在第一次使用时自动建 `pending/ running/ done/ results/ merges/ acks/ sync/`。结果只追加，同一个 SHA 重跑写成 `attempt-<n>/`。
- **仓库**：沿用现有 bare 仓库 `<GIT_REPO>`。
  - `enqueue` 只接受 `refs/heads/incoming/*` 和 `refs/heads/integration/*`；`refs/ci/<SHA>` 会被拒绝（`ref_not_queueable`）。
  - 因此调度员推入 `refs/ci/<SHA>` 后，由 CI 身份在仓库内执行 `git update-ref`，把 `refs/heads/integration/release-<TAG>` 指向同一个 SHA。这是本地 ref 写入，不推送，也不触发 hook。
  - 队列请求记录的是当时解析出的 SHA，之后 ref 再移动也不影响已入队的请求。
- **identities**：一个私有文件，只含 `<RELEASE_CI_IDENTITY>`，角色为 `release`。
  - 检查请求只要求身份在文件里，角色只对合并请求有意义；
  - 本路径不提交合并请求。
- **policy**：受信的 `.ci-policy/stages.v4.json` 不能在车道上原样使用：
  - 它的 `isolation-probe` 阶段运行 `/trusted/...`，这个路径只存在于 NX-033 的 Podman 运行器里。在车道上这个阶段必然失败，结果就是 `failed`（测试 `test_the_trusted_policy_as_is_fails_off_the_runner_and_the_release_refuses_it`）；
  - 控制器只把 PATH、HOME、LANG、LC_ALL、TMPDIR 传给阶段。车道脚本导出的其他非秘密设置，需要显式交给每个阶段。

  所以由 `scripts/ci/lane_policy.py` 从受信 policy 派生一个车道实例：
  - 去掉 `isolation-probe`；
  - 把私有 env 文件中的非秘密变量加到每个阶段。看起来像秘密的变量名，以及控制器自己设置的变量名，都会被拒绝；
  - `required-suites.v2.json` 原样复制，判定规则（必需 suite、计数、时限门槛、`policy_version`）不变；
  - 派生记录写进 stages 文件的 `decision` 字段和 `lane.json`，结果中的 `policy_digest` 因此与原 policy 不同。
- **stages 版本**：`--stages-version 4`。车道实例的阶段及顺序与 `scripts/ci/check`（即现有车道脚本运行的内容）完全一致：

  `dependency-python`、`static`、`dependency-node`、`licenses`、`build-artifacts`、`unit-contract`、`serial-build`、`deadline-gate`、`integration-parallel`、`integration-heavy`、`pi-runtime`

  测试 `test_the_repository_trusted_v4_policy_derives_to_the_stages_of_scripts_ci_check` 钉住这一点。

## 3. 逐条命令

以下命令都以 `<CI_USER>` 身份执行，第 4 步除外。每条命令输出一个 JSON 对象，退出码 0 表示成功或通过，1 表示被拒或失败，2 表示配置或环境错误。

**第 0 步 前置检查**
```bash
python3 -c 'import sys; assert sys.version_info >= (3, 11)' && git --version
```
预期：无报错，打印 git 版本。

**第 1 步 受信安装**（每个 `<TRUSTED_SHA>` 做一次）
```bash
mkdir -p <TRUSTED_CI_DIR> && git -C <GIT_REPO> archive <TRUSTED_SHA> scripts/ci .ci-policy | tar -x -C <TRUSTED_CI_DIR>
```
预期：`<TRUSTED_CI_DIR>/scripts/ci/nexloop_ci.py`、`<TRUSTED_CI_DIR>/scripts/ci/lane_policy.py`、`<TRUSTED_CI_DIR>/.ci-policy/stages.v4.json` 都存在。`<TRUSTED_SHA>` 必须是 main 上已评审的提交，不能是候选提交。

**第 2 步 私有文件**（做一次）
- 按 `deploy/stage/ci/release-lane-identities.example.json` 写 `<PRIVATE_DIR>/release-lane-identities.json`；
- 按 `deploy/stage/ci/release-lane.env.example` 写 `<PRIVATE_DIR>/release-lane.env`。内容是现有车道脚本导出的非秘密设置，只抄名字和非秘密值；
- 两个文件权限都设为 0600。

**第 3 步 车道 policy 实例**（每个 `<TRUSTED_SHA>` 做一次，输出目录不能已存在）
```bash
python3 <TRUSTED_CI_DIR>/scripts/ci/lane_policy.py --source <TRUSTED_CI_DIR>/.ci-policy --stages-version 4 --without isolation-probe --stage-env <PRIVATE_DIR>/release-lane.env --out <LANE_POLICY_DIR>
```
预期：`"written": true`，`stages` 为上面的 11 个阶段，`env_names` 为 env 文件中的变量名。

**第 4 步 推入候选**（调度员在开发机上执行，不需要 CI 身份）
```bash
git push <CI_REMOTE> <SHA>:refs/ci/<SHA>
```

**第 5 步 integration ref**
```bash
git -C <GIT_REPO> update-ref refs/heads/integration/release-<TAG> <SHA>
```

**第 6 步 入队**
```bash
python3 <TRUSTED_CI_DIR>/scripts/ci/nexloop_ci.py enqueue --repo <GIT_REPO> --queue <QUEUE_DIR> --ref refs/heads/integration/release-<TAG> --sha <SHA> --identity <RELEASE_CI_IDENTITY> --identities <PRIVATE_DIR>/release-lane-identities.json
```
预期：`"enqueued": true`，`sha` 等于 `<SHA>`。

**第 7 步 运行**（一次一个构建，全套耗时与现有车道相当；请在前台或用现有车道的调度方式执行，资源限制与现有车道相同）
```bash
python3 <TRUSTED_CI_DIR>/scripts/ci/nexloop_ci.py run-once --repo <GIT_REPO> --queue <QUEUE_DIR> --policy <LANE_POLICY_DIR> --identities <PRIVATE_DIR>/release-lane-identities.json --stages-version 4
```
预期：`"ran": true`，`outcome.status` 为 `passed`，退出码 0。工作目录 `<QUEUE_DIR>/work/<SHA>-<attempt>` 在运行结束后自动删除。

**第 8 步 取结果**
```bash
python3 <TRUSTED_CI_DIR>/scripts/ci/nexloop_ci.py result --queue <QUEUE_DIR> --sha <SHA>
```
预期：
- `"status": "passed"`；
- `"deadline_gate": "passed"`；
- `"policy_version": 2`；
- `counts` 中 failed、errors、skipped 都为 0；
- `isolation` 为 `null`（见 §5）。

结果文件是 `<QUEUE_DIR>/results/<SHA>/attempt-<n>/result.json`，其中 n 为最大的那次尝试。它与 stdout 内容相同。

**失败时查看失败名**：工作目录在运行结束后删除，但 `attempt-<n>/` 保留以下内容，保留期与结果目录相同：
- `failures.json`（schema `nexloop-ci-failures/1`）：按 suite 列出失败和错误的用例，包括 `classname`、`name`、`kind`（failure 或 error）、`message`。
  - `message` 是用例的 message 属性，没有时取元素文本的第一行；
  - 处理方式：去掉控制字符，遮蔽 URI 中的凭据、`password=`/`token:` 一类片段和 40 个字符以上的不透明串，截断到 300 字符；
  - 不含原文日志；
  - 每个 suite 最多保存 200 条，超出时 `truncated` 为 true。
- `junit/<阶段>/<文件名>`：各阶段 JUnit 文件的原样副本，权限 0640；sha256 与 result.json 中 `stages[].junit` 记录的一致。
- `result.json` 中新增的 `failures` 字段：`{file, count, truncated}`，只是摘要。这是对 `nexloop-ci-result/1` 的增量改动，schema 版本不变。`release.py` 只读取 `sha`、`status`、`policy_version`、`deadline_gate`、`counts`，不受影响。

```bash
python3 -c 'import json,sys; f=json.load(open(sys.argv[1])); [print(s, c["kind"], c["classname"]+"::"+c["name"], "|", c["message"]) for s,v in f["suites"].items() for c in v["cases"]]' <QUEUE_DIR>/results/<SHA>/attempt-<n>/failures.json
```

按合并规则，失败需要在同一检出上串行复跑取证。复跑的用例就取上面输出的 `classname::name`（pytest 用例 id 是 `<文件路径>::<name>`，classname 中的点号对应文件路径），例如：

```bash
uv run --frozen pytest -q -p no:cacheprovider '<tests/路径.py>::<name>'
```

复跑结论另行记录。结果目录只追加，失败的 `result.json` 不修改。

**第 9 步 交给发布**
```bash
python3 scripts/release/release.py build --sha <SHA> --ci-result <QUEUE_DIR>/results/<SHA>/attempt-<n>/result.json --release-root <RELEASE_ROOT>
```
在 `<SHA>` 的干净检出中执行。清单记录 `ci_result_digest`（结果文件的 sha256）、`ci_policy_version`、`deadline_gate` 和 `tests`。

## 4. 失败处理

| 现象 | 含义 | 处理 |
|---|---|---|
| 第 3 步 `written: false`，`looks like a secret` 或 `set by the controller` | env 文件里有不允许的变量名 | 删除该行。秘密不能进入 CI 阶段 |
| 第 3 步 `output directory exists` | 实例只写一次 | 换一个新的 `<LANE_POLICY_DIR>`。旧实例保留，作为已有结果的依据 |
| 第 6 步 `ref_not_queueable` | ref 不在 `incoming/*` 或 `integration/*` 之下 | 用第 5 步的 integration ref |
| 第 6 步 `sha_unknown` | 仓库里没有这个提交 | 先完成第 4 步，再用 `git -C <GIT_REPO> cat-file -t <SHA>` 确认，应输出 `commit` |
| 第 7 步 `"reason": "busy"` | 另一个构建正在运行（同一时间只跑一个） | 等它结束后重跑第 7 步 |
| 第 7 步 `"reason": "empty"` | 队列里没有请求 | 检查第 6 步 |
| 第 7 步 `status: failed`，`reasons` 含 `stage_failed:<阶段>`、`suite_missing:<suite>`、`suite_not_clean:<suite>`、`suite_too_small:<suite>` | 判定未通过 | 结果文件中有各阶段的 junit 计数与 sha256。修复后重新推送新的 SHA 并从第 4 步开始。不能把失败的结果改为通过 |
| 第 7 步阶段状态为 `dependency_unavailable` | 依赖阶段失败（例如网络或缓存问题） | 排除环境问题后，对同一个 SHA 重跑第 6、7 步，结果写成 `attempt-2`，`first_failure` 记录首次失败 |
| 第 7 步退出码 2，`work directory exists` | 上次运行中断，留下了工作目录 | 确认没有构建在运行后，删除 `<QUEUE_DIR>/work/<SHA>-<attempt>` 再重跑 |
| 第 7 步卡住 | 单个阶段有超时（stages.v4.json 的 `timeout_seconds`），超时记为失败 | 等待超时；不要中途删除队列文件 |
| 第 8 步 `not_available` | 还没有结果 | 先完成第 7 步；或者加 `--wait <秒>` |
| 第 9 步 `CI result is not a pass of this commit` | 结果不是该 SHA 的 passed 结果 | 核对 `<SHA>` 和 attempt 目录 |

**重跑规则**：同一个 SHA 已有 passed 结果时，`run-once` 直接复用它（`reused: true`），不再构建。没有 passed 结果时，每次重跑都追加一个新的 attempt。

**计数基线**：判定会和 `<GIT_REPO>` 中 main 的最新结果比较测试数。在这个队列里，main 没有结果，所以本路径没有基线比较；必需 suite、无失败、无跳过、`min_tests` 这几项照常检查。

## 5. 与 NX-033 §8 完整切换的区别

本路径只是在现有 CI 车道上**使用队列和受信判定**来产出发布用的 CI 结果。

| 项 | 本路径 | NX-033 §8 完整切换 |
|---|---|---|
| main 由谁推进 | 不变（仍按现行方式） | 只由合并控制器推进（kind=merge、快进、policy_change 确认） |
| remote | 不变 | `origin` 改指 LAN bare 仓库，`github` 只由同步任务推送 |
| bare 仓库 hook（§2.1 ref 规则，post-receive 自动入队） | 不安装；由 CI 身份手动 `update-ref` 和 `enqueue` | 安装 |
| 用户与 systemd（`nexloop-git`、`nexloop-ci` 单元与定时器） | 不建、不改；用现有 CI 身份在前台运行 | 建立 |
| 运行器 | 无（阶段直接在 CI 身份下运行，与现有车道相同） | rootless Podman，`check-runner` 检查，测试阶段无网络 |
| 隔离探测（AT-054 证据） | 不运行，结果中 `isolation` 为 `null` | `isolation-probe` 阶段运行，结果记录结论 |
| policy | 受信 v4 的车道实例（去掉隔离探测，加非秘密 env），`policy_digest` 与原 policy 不同 | 受信 `.ci-policy` 原样 |
| 判定（必需 suite、计数、时限门槛、`policy_version`） | 相同 | 相同 |

所以，本路径的结果可以证明：这个 SHA 在受信判定下通过了与现有车道相同的全套阶段，并通过了时限门槛。它**不能**作为 AT-054（CI 隔离）的证据，也不代表合并控制器已经启用。

## 6. 本地模拟（`tests/test_release_ci_lane.py`）

在临时目录中按本手册的顺序执行：
1. 建一个没有 hook 的 bare 仓库；
2. 推入 `refs/ci/<SHA>`（候选为本仓库的 HEAD）；
3. `update-ref` 建 integration ref；
4. 用 `lane_policy.py` 派生车道实例；
5. 依次执行 enqueue、run-once、result，得到 passed 的结果，其中 `deadline_gate` 为 passed，`policy_version` 为 2；
6. `release.build_manifest` 接受这份结果，清单记录它的 digest、policy 版本和时限门槛。

受信 policy 用的是合成内容：阶段写出必需的 junit，`isolation-probe` 阶段用真实的 argv。

反例：
- 原样使用受信 policy（保留 `isolation-probe`）时，结果为 failed，原因含 `stage_failed:isolation-probe`，发布拒绝；
- 秘密变量名或控制器自设的变量名会被拒绝；
- 未知阶段名、已存在的输出目录会被拒绝；
- 真实 `.ci-policy` v4 的车道实例与 `scripts/ci/check` 的阶段一致。
