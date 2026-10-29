# 准备清单：S6 stage 执行（post-R53 计划阶段 4–7，NX-038/039/040）

- **执行人**：调度员（标为“负责人在终端执行”的步骤除外）。**判定人**：L6 复核本清单；各 AT 的判定按 run-nx038/039/040。
- **依据**：main 01d875dd。`plan-post-r53-stage.md` §1–§2、`run-nx038-stage.md`、`run-nx039-stage.md`、`run-nx040-stage.md`、`docs/implementation/S6-stage-acceptance.md`、`evolution-stage-enrollment.md`、`NX-038-E3-implementation.md`（G-S6-1 接线与失败口径）、`docs/design/NX-056-budget-approval.md`（Q4 续期）。
- **本清单只做准备**：写清配置从哪里来、合成数据怎么造、哪些情况判 blocked，以及预算到期前后的时间表。本清单不连任何主机。下文的“现状”都来自调度员 10-29 的说明，没有经过只读核对；第 0 节把它们列为第一批核对项。

## 0. 现状与第一批只读核对

| # | 现状（调度员说明） | 核对 | 影响 |
|---|---|---|---|
| C1 | BUD 已完成：evolution 预算 50 CNY，期间是 10 月（`2026-10-01T00:00:00Z` 到 `2026-11-01T00:00:00Z`），L6 PASS | run-nx038 P3 的查询加上 `revision` 列，应有一行 `CNY|50.00|2026-10-01 00:00:00+00|2026-11-01 00:00:00+00|<revision>`，记下 revision | 第 4 节的续期要用到 revision |
| C2 | 10-22 的 `s6_stage_inputs.py` 判 blocked：window、erasure_consumer_id、protected_refs 都不满足 | 第 2 节造完数据后再跑一次 | P5、P7、P9 |
| C3 | L5 的合成交互执行单（分支 `l5/s6-stage-data`，ec9bf9a0）**不在 main 上** | `git merge-base --is-ancestor ec9bf9a0 main`，01d875dd 上返回 1 | 那张单只改了 `S6-stage-acceptance.md`，内容写于 G-S6-1 接线之前，第 2 步用的是人工发言。本清单第 2 节取代它，用的是 main 上已有的 `synthetic_customer.py` 一节和 E3 接线。不需要先合并那个分支 |
| C4 | stage 的 sim 世界 10-28 全部过期并已删除 | 不用核对 | **S6 不需要 sim 世界**：合成交互走 real world 的 WebChat，顾客是 persona 为 simulated_agent 的合成 Consumer；L1 用的是 test world 的评估凭据和 D16 替身，不是 sim 世界。所以不用重新 prepare |
| C5 | DATA_HOST 的 `~/nx-publish/evolution` 下已有 `evolution-writer`、`evolution-evaluator`、`evolution-reader`、`evolution-evaluation` 四个登记目录 | DATA_HOST 上用 `python3 scripts/deploy/evolution_enroll.py --config <私有 enroll 配置> --check`，退出码 0，自检四项都为 true；`ls -l` 三个 writer/evaluator/reader 目录，**确认各有 `database-url`（0600）**。enroll 工具只生成 `service-credential`/`test-credential`，`database-url` 是操作单第 1 步由调度员手写的 | 没有 `database-url` 就按 1.2 补写 |
| C6 | 管理机上还没有任何 s6-stage 配置 | — | 按第 1 节新建，放在 DATA_HOST，不放在管理机（见 1.1） |
| C7 | 语义读取已接入 Run 路径（G-S6-1，迁移 0313），但只有 message-relay 用的上下文策略 `semantic.enabled=true` 并且带 `--semantic-read-pool-max` 时才写读取记录 | **调度员 10-29 已只读核对**：14 天内读取 0 条；租户 real 世界只发布了 `recent_plus_required@1`（semantic false），近 14 天 125 个 context pack 全部用它；两个 message-relay 容器都带 `--semantic-read-pool-max 1` | 负责人 10-29 决定启用 `ontology_hybrid`（A1），步骤见第 7 节 |

`ro` 的写法见 run-nx057 §0.3（DATA_HOST，bootstrap 只读 DSN，经 socket）。

## 1. s6-stage 配置的每个字段从哪里来

### 1.1 在哪台主机上跑（以 10-22 的做法为准）

- **DATA_HOST 的一次性容器，经 postgres socket 连接**。依据是 `evolution-stage-enrollment.md` 的 pg_hba 一节：`nexloop_evo_reader/writer/evaluator` 三个角色**只有一条 `local` 规则**，没有 hostssl，所以管理机连不上。10-22 的 `s6_stage_inputs.py` 也是这样跑的：inspector 用的 bootstrap DSN 是给一次性容器准备的 `host=/var/run/postgresql`。
- run-nx038 §2 写的是“管理机的发布检出根目录”，这一说法与 pg_hba 冲突，以本节为准。命令形态不变，只是改在 DATA_HOST 一次性容器里，在发布检出（与 stage 部署相同的提交）的根目录执行。容器的挂载形态沿用 `run-34-20261015.md` §1（socket 目录、只读的发布检出、私有目录）。
- **例外：需要工作台 HTTPS 的步骤**。`s6_stage_schema.py` 的 at024/r10/r8/r6、`erase_synthetic_consumer.py`、BUD 的 `approval_drill.py` 要以 approver3 登录工作台，口令来自钥匙串，所以这些步骤在**管理机**上执行。它们的配置只需要 `owner.*` 和 `inputs.candidate_template`，以及各自的 DSN 字段（见 1.2 的“主机”列）。如果 `s6_stage_schema.py` 的某项还要求演进角色 DSN，那么这一项就要在 DATA_HOST 执行，同时从 DATA_HOST 访问工作台的 HTTPS。这一点 01d875dd 上没有现成的跨主机先例，**第一次 dry-run 时核对**：管理机上执行 `s6_stage_schema.py at024 --dry-run` 不报连接错误即可。报错就停下，报调度员决定，不要临时给演进角色加 hostssl。
- 结果：同一份配置有**两份私有副本**。DATA_HOST 那份包含全部 roles；管理机那份只含 owner 与 schema 步骤用到的字段。两份的 `inputs` 必须相同，每次改动后各记一次 sha256（X5、run-nx039 P8）。

### 1.2 字段表

`<EVO>` 指 DATA_HOST 上的 `~/nx-publish/evolution`，在容器里挂载为只读的 `/run/evo`。

| 字段 | 值 / 来源 | 主机 | 怎么生成 |
|---|---|---|---|
| `schema` | `nexloop-s6-stage/1` | 两边 | 照抄 example |
| `executor` | `dispatcher` | 两边 | — |
| `release` | stage 当前的发布号 | 两边 | 部署单 |
| `tenant_id` | stage 租户 | 两边 | 私有 stage.env |
| `results_dir` | 私有目录下新建的 0700 目录，例如 `<私有根>/s6-<日期>/results` | 两边各一个 | `install -d -m 700` |
| `roles.writer` | `{dsn_file: <EVO>/evolution-writer/database-url, token_file: <EVO>/evolution-writer/service-credential}` | DATA_HOST | token 由 enroll 生成（C5）。`database-url` 是一行 `host=/var/run/postgresql dbname=<stage 库> user=nexloop_evo_writer`，**不含口令**；口令放在同目录 0600 的 passfile（`PGPASSFILE`），从钥匙串写入 |
| `roles.evaluator` | 同上，`evolution-evaluator/…` | DATA_HOST | 同上 |
| `roles.reader` | 同上，`evolution-reader/…` | DATA_HOST | 同上 |
| `roles.evaluation` | `{token_file: <EVO>/evolution-evaluation/test-credential}` | DATA_HOST | enroll 生成，只含 test |
| `roles.inspector` | `{dsn_file: <bootstrap 只读 DSN 文件>}` | DATA_HOST | 10-22 用过的那份 bootstrap DSN（socket 形式）；脚本连上后自己设为只读并核对 |
| `roles.verify` | `{dsn_file: <只读应用 DSN 文件>}` | DATA_HOST | NX-040 的 r8/r6 用 `bootstrap.verify`；与 run-nx040 §2 相同 |
| `roles.registrar` | 可选，不填 | — | R5 的登记负例可以不做（S6-stage-acceptance） |
| `owner.origin` | `https://<APP_HOST_NAME>:<PORT>`（stage 工作台） | 管理机 | 与 run-nx056 相同 |
| `owner.ca_file` | 网关 CA | 管理机 | 与 run-nx056 相同 |
| `owner.username_file` | 0600 文件，内容是 approver3 的用户名 | 管理机 | 取法同 run-nx056；**不能是负责人** |
| `inputs.candidate_template` | `results_dir` 旁的 0600 路径，先不存在 | 两边（DATA_HOST 写，管理机读） | 总序第 7 步 `candidate-template` 写入；写入后拷到管理机，记 sha256 |
| `inputs.evolution.window` | `s6_stage_inputs.py` 输出 | — | 第 2 节造完数据之后才有。**结束时间必须早于 OUT §3.1 开始**（X5） |
| `inputs.evolution.personas` | `["simulated_agent"]` | — | 不加 real |
| `inputs.evolution.label` | `s6-stage-<造数据那天的日期>-1` | — | 换 window 就换 label |
| `inputs.evolution.parent_revision` | `s6_stage_inputs.py` 输出；没有发布过语义版本时用 10-22 定的基线标签 `sem-0` | — | 注意 run-nx040 Q4：没有已发布的版本时，r6 的回退一步会 blocked |
| `inputs.evolution.evaluator_version` / `model_profile` / `l0_inputs` | `l0-2` / `none` / 照抄 example | — | D16 的合成测量 |
| `inputs.evolution.created_at` | 总序第 1 步开始前的 `date -u +%FT%TZ`，之后不再改 | — | 不要估时间 |
| `inputs.evolution.protected_refs` | `s6_stage_inputs.py` 输出；blocked 时按第 3 节处理 | — | — |
| `inputs.evolution.erasure_consumer_id` | `s6_stage_inputs.py` 输出 | — | 必须不是 OUT 要用的 customer-1..4（X1） |
| `inputs.schema_type` 等 NX-040 字段 | `s6_stage_inputs.py` 输出 | — | — |
| `inputs.semantic` | at023 之后用 `--only semantic` 取 | — | 总序第 5 步 |

口令的写法：每个演进角色的口令已在钥匙串里（`nexloop/stage/evo-writer` 等，以 `~/.nexloop/vault-index.md` 的条目为准）。passfile 由调度员在管理机上生成，写入 DATA_HOST，命令形态是 `security … -w | ssh <DATA_HOST> 'umask 077; cat > <EVO>/<role>/pgpass'`。口令不进 argv，也不放进 heredoc，并且要先把 `pgpass` 的格式行拼好（`*:*:<stage 库>:<role>:` 加上口令）。如果 10-22 已经有这三份 passfile，就直接复用。

## 2. 造出满足 P5/P7/P9 的合成交互

### 2.1 前提

| # | 前提 | 不满足时 |
|---|---|---|
| A1 | C7：语义读取会被写入 | **负责人 10-29：启用 `ontology_hybrid`（top_k 5）**，按第 7 节执行并核对后才发言。不要用播种工具直接插读取记录 |
| A2 | R3 需要一次占满浏览上限（**10 条**）的读取（`s6_stage_inputs.py` 的 `WIDE = 10`），而 Run 路径的读取上限是策略的 `top_k`，`ontology_hybrid` 是 **5**，到不了 10 条 | **负责人 10-29：不发布 top_k ≥ 10 的版本，r3 判 blocked**（原因：Run 路径读取上限 5 < 浏览上限 10，P7 不可满足），其余项照做（plan §5：单项只判这一项） |
| A3 | 失败口径（负责人 10-22 选 A）：`attribution._failed` 把 sample 的 `outcome` 属于 `FAILED_OUTCOMES = {error, reply_fallback}` 的 Run 算作失败。`error`：Run 有 failures、outcome 为 error 或模型调用失败；`reply_fallback`：入站消息后来发起了兜底回复 Run（stage 在 300 秒后发起，`reply-guarantee.v3`）。真实模型一般几秒内就成功回复，两种都不出现时 Run 不算失败，**也就不会产生 retrieval_miss 草稿** | **负责人 10-29：照常发言并观测，不改失败口径。** 再跑 `s6_stage_inputs.py` 后 window 仍 blocked 且 `runs_with_miss=0` 时，把依赖 window / 草稿的项判 blocked（原因写“合成 Run 无 error / reply_fallback 结局，未产生检索未命中”），其余照做；不加轮次碰运气，不改模型档案或超时 |
| A4 | 至少 4 个 simulated_agent 合成 Consumer 能发言（P9 要 4 个簇单元）。customer-4、refusal-1 已擦除；**10-30 起按 §2.1a 只用 customer-1/2/3**，customer-5 是条件分支 | `synthetic_customer.py` 预检会报 `conversation_unavailable`；customer-5 不存在时，按 S6-stage-acceptance 用 `webchat_consumers.py` 加一个，并用 `erase_synthetic_consumer.py mark` 标为 simulated_agent（approver3） |
| A5 | 没有正在运行的 online 实验；real 外发的三层开关与 run 入口计划一致；routes 里企业端点指向参考替身 | 照 S6-stage-acceptance“副作用”一节。确认不了时请负责人临时关闭 `nexloop.enterprise.deliver:1` |
| A6 | OUT 还没开始（X5） | OUT 已开始就等 OUT §4.4 结束，并且整个窗口要避开 OUT 的 Run |

**WebChat 合成顾客要登录**，所以要口令。发言这一步标为“**负责人在终端执行**”。

### 2.1a 3 账号路径（负责人 2026-10-30，L6 同意）

refusal-1 的 Consumer 已擦除，customer-4 也已擦除。今天只用 customer-1/2/3 发言，共 9 轮。

- **blocked**（plan §5，单项只判这一项）：依赖 4 个簇单元的 at048、r5、r7、template，照实判 blocked，原因写“只有 3 个合成 Consumer 能发言”。
- **r7 一律不做**：它的擦除不可逆，没有第 4 个簇单元时不执行。
- **window**：用 `s6_stage_inputs.py` 输出的 `candidates[0]`，证据里注明来源是 blocked 时的候选值。
- 其余项照做。customer-5 是条件分支（§2.2a），今天不用。

### 2.2 发言与观察：逐轮执行

**为什么逐轮**：观察方 `run_interactions.py --driver real` 用 v3 计划，每次交互（SEQ）先给出提示，只接受这一次所列账号的消息，并等待 `$TRIAL_DIR/operator-done/<SEQ>`（`interaction_drivers` 的 `prompt`/`_views`）。一次连发多轮时，第二个账号的消息落在同一次交互里，判为 `offplan_sender`。所以每轮单独发，发完才写完成标记。

**私有文件**（0600，放 `local-only/s6-<日期>/`；模板在调度员的交接材料里）：

| 文件 | 内容 |
|---|---|
| `customers.json` | `nexloop-synthetic-customer/1`：origin、CA、customer-1/2/3 的用户名；`persona_check` 走 workbench（owner 用户名文件，3 个 consumer_id） |
| `plan-calibration.json` | `plan_id` `s6-<日期>b-calibration`，3 轮：customer-1/2/3 各 1 轮 (a) |
| `plan-main.json` | `plan_id` `s6-<日期>b-main`，6 轮：每个账号再 1 轮 (a)、1 轮 (b)；(c) 的近重复对在 customer-1 与 customer-2 |
| `trial.json`（APP_HOST，10001） | `nx037-trial.v1` 的副本：`total` 9，`mix` 只有 `ordinary_reply` 9，`calibration_interactions` 3，窗口 10:00–18:00（Asia/Shanghai），`operator_authorization`，`participants`（customer-1/2/3 三个 agent 加 owner 的 staff） |
| `plan.json`（APP_HOST，10001） | `nx037-interaction-plan.v3`：9 个 `ordinary_reply`，顺序与发言一致，`consumer` 为 `consumer:<id>`；`real_dispatch` 填实际开关状态（A5） |

所有 `wait_reply_seconds` 都是 120。

**1. 预检**（调度员，APP_HOST；`$RUNI`、metrics 副本与 host-config 副本按 run-calibration-20261017 §2.1）：

```
$RUNI scripts/nx037/run_interactions.py --driver real --trial-id "$TRIAL_ID" --out "$TRIAL_DIR" --plan-file "$TRIAL_IN/plan.json" \
  --trial-config "$TRIAL_IN/trial.json" --tenant "$TENANT_ID" --owner-principal "$OWNER_PRINCIPAL_REF" --value-catalog "$TRIAL_IN/value-catalog.json" \
  --channel-ref "$CHANNEL_REF" --period-start 2026-10-01T00:00:00Z --period-end 2026-11-01T00:00:00Z --metrics-dsn-file "$M/metrics.dsn" \
  --owner-session-file "$TRIAL_IN/owner-session" --host-config "$TRIAL_IN/host-config.json" --operator-authorization-file "$TRIAL_IN/authorization.txt" \
  --webchat-provider-file /run/secrets/action_worker_webchat_provider --preflight
```

预期 `{"ok": true, "preflight": true, "refused": [], …}`，退出码 0。计划不开真实企业交付，所以不带 `--enterprise-provider-file`；`real_dispatch` 列了 `nexloop.enterprise.deliver:1` 时，按 10-17 加上它和 `--endpoint-parameters-file`。

**2. 正式运行**：同一命令去掉 `--preflight`。`TRIAL_ID` 用新的 uuid，`TRIAL_DIR` 事先不存在。

**3. 每一次交互**：entry 在 stderr 和 `$TRIAL_DIR/prompts.jsonl` 给出 SEQ 的提示后，负责人才发这一轮：

| SEQ | 命令（负责人终端，管理机） |
|---|---|
| 1–3 | `bash local-only/s6-<日期>/speak-turn.sh plan-calibration.json <SEQ> <SEQ>` |
| 4–9 | `bash local-only/s6-<日期>/speak-turn.sh plan-main.json <SEQ−3> <SEQ>`（正式份第 1–6 轮） |

- 只有 `synthetic_customer.py` 退出码 0（已发或重放；收到回复或等满 120 秒）时，脚本才写完成标记。
- 在 SEQ 的提示出现之前不要发言：提示前接受的消息属于上一次交互。
- 退出码 1（这一轮没发出）：同一 SEQ 重跑同一命令，单轮 `plan_id` 不变，按重放处理。
- 每次交互共 900 秒（含等待），超时是暂停。

**4. 校准暂停与继续**：SEQ 3 之后 entry 以 `detail` 为 `paused_needs_resume`（规则 `calibration`）结束，退出码 3。负责人看 `$TRIAL_DIR/interactions.jsonl`，`cost.max_run` 小于 2 CNY 时确认，调度员用原参数加 `--resume s6-<日期>-calibration-ok` 继续，entry 给出 SEQ 4 的提示。

**退出码**（`run_interactions.main`）：

- 0：完成；
- 3：读输出 JSON 的 `detail`：
  - `paused_needs_resume`：唯一可以继续的情况，用负责人新的确认引用 `--resume`；
  - `trial_stopped`：一律不继续，换新的 `TRIAL_ID`；
  - 其他：输入或前提被拒，修正后重跑；
- 1 / 2：运行以 `status` 为 `paused` / `stopped` 的状态结束，分别按上面两种处理。

**`speak-turn.sh`**（0700）：

```bash
#!/bin/bash
# S6 speaking, one turn per call (owner terminal, management machine):  bash speak-turn.sh <PLAN.json> <TURN> <SEQ>
#   TURN: 1-based turn in PLAN; SEQ: the run entry's interaction number it answers (calibration 1-3 = turns 1-3, main 4-9 = turns 1-6).
# Why per turn: with plan v3 the run entry prompts interaction SEQ, accepts only that interaction's account and waits for
# $TRIAL_DIR/operator-done/SEQ; a second account speaking inside the same interaction is offplan_sender.
# The turn is sent as a one-turn plan <plan_id>-t<TURN> (Idempotency-Key <plan_id>-t<TURN>-1-<alias>): rerunning a turn replays it.
# Passwords: that turn's account plus `owner` (persona_check via workbench), from Keychain over stdin only.
set -euo pipefail
D=$(cd "$(dirname "$0")" && pwd); PLAN=$1; TURN=$2; SEQ=$3
: "${REPO:?release checkout}" "${APP_SSH:?ssh target of APP_HOST}" "${TRIAL_DIR:?run entry --out on APP_HOST}"
export SSL_CERT_FILE=$HOME/.nexloop/stage/endpoint-20261015/ca-bundle.crt
ONE=$(mktemp "$D/.turn.XXXXXX"); trap 'rm -f "$ONE"' EXIT
ACCOUNT=$(python3 - "$PLAN" "$TURN" "$ONE" <<'PY'
import json, sys
plan, n, out = json.load(open(sys.argv[1])), int(sys.argv[2]), sys.argv[3]
turn = plan['turns'][n - 1]
json.dump({'schema': plan['schema'], 'plan_id': f"{plan['plan_id']}-t{n}", 'turns': [turn]}, open(out, 'w'), ensure_ascii=False)
print(turn['account'])
PY
)
( printf '%s\t%s\n' "$ACCOUNT" "$(security find-generic-password -s "nexloop/stage/$ACCOUNT" -w)"
  printf 'owner\t%s\n' "$(security find-generic-password -s "${OWNER_KEYCHAIN:-nexloop/stage/approver3}" -w)"
) | (cd "$REPO" && uv run --frozen python scripts/acceptance/synthetic_customer.py --config "$D/customers.json" --plan "$ONE")
# Exit 0 only: mark interaction SEQ done for the run entry (root touch; the entry only checks existence and mtime).
ssh "$APP_SSH" "sudo -n touch '$TRIAL_DIR/operator-done/$SEQ'" && echo "operator-done/$SEQ"
```

- 口令只走 stdin，脚本必须是文件，不能用 `python -`。钥匙串条目名以 vault-index 为准。
- 完成标记由 root `touch`；entry 只看文件是否存在及其 mtime。

### 2.2a customer-5（A4，条件分支）

只在 A3 观测到检索未命中、且负责人需要完整结果时才用；用之前先做 run-assembly §6.1–6.3。今天的 3 账号路径不用它。

1. `--apply password`：口令直接生成进 Keychain；
2. `--apply identity`：`initial_identity --create`，口令经 ssh stdin 写进容器的 `/work`（tmpfs）；
3. 调度员在 DATA_HOST 把 customer-5 加进私有配方，执行 `webchat_consumers.py --apply`；
4. `--apply open`：customer-5 开一个会话，不发消息，于是有了 Consumer；
5. 调度员只读查出 consumer_id，填 `persona-customer-5.json`；
6. approver3 先 `--apply mark-dry` 再 `--apply mark`（`erase_synthetic_consumer.py mark`）。

不带参数时是 dry-run。

```bash
#!/bin/bash
# A4: customer-5 (owner terminal, management machine). Default is a dry run; `--apply STEP` performs one step.
#   1 password   generate customer-5's password straight into Keychain nexloop/stage/customer-5 (never printed, no file)
#   2 identity   initial_identity --create on APP_HOST; the password goes over ssh stdin into the container's /work tmpfs only
#   (3 dispatcher: webchat_consumers.py --apply with customer-5 added to the private recipe, DATA_HOST, configurator window)
#   4 open       customer-5 opens one conversation (no message): its Consumer now exists
#   (5 dispatcher: read the consumer_id, fill persona-customer-5.json)
#   6 mark       erase_synthetic_consumer.py mark as approver3: --dry-run first (`--apply mark-dry`), then `--apply mark`
set -euo pipefail
D=$(cd "$(dirname "$0")" && pwd)
: "${REPO:?release checkout}" "${ORIGIN:?https://<APP_HOST_NAME>:<PORT>}" "${APP_SSH:?ssh target of APP_HOST}"
: "${APPTOOL:?docker run options of run-assembly-20261015 §1 (identity DSN secret, /in mount, --user 10001)}" "${BACKEND_IMAGE:?}"
export SSL_CERT_FILE=$HOME/.nexloop/stage/endpoint-20261015/ca-bundle.crt
CA=$SSL_CERT_FILE; K=nexloop/stage/customer-5
need() { for f; do [ -f "$f" ] && [ "$(stat -f %Lp "$f")" = 600 ] || { echo "missing or not 0600: $f" >&2; exit 2; }; done; }
case "${1:-} ${2:-}" in
 " ")
  echo "dry run:"; security find-generic-password -s "$K" >/dev/null 2>&1 && echo "  1: Keychain $K exists (step 1 would refuse)" || echo "  1: would create Keychain $K"
  need "$D/customer-5.username" "$D/approver3.username" "$D/webchat-consumers.json"; [ -f "$D/persona-customer-5.json" ] || echo "  6: persona-customer-5.json pending (step 5)"
  ssh "$APP_SSH" "sudo -n docker run --rm $APPTOOL $BACKEND_IMAGE -m nexloop_eios.initial_identity --manifest /in/identity-customer-5.json --check"
  echo "  2: manifest checks; 4/6: inputs present" ;;
 "--apply password")
  security find-generic-password -s "$K" >/dev/null 2>&1 && { echo "exists: $K" >&2; exit 2; }
  P=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)
  printf 'add-generic-password -s %s -a customer-5 -w %s\n' "$K" "$P" | security -i >/dev/null 2>&1 || { unset P; echo "keychain: add failed" >&2; exit 1; }; unset P
  security find-generic-password -s "$K" >/dev/null && echo "keychain: $K stored" ;;
 "--apply identity")
  # APPTOOL (as ~/nx-a1/a1-publish.sh on APP_HOST) already has --tmpfs /tmp and the /work tmpfs owned by 10001: no extra mount;
  # its entry is switched from python to sh exactly as a1-publish.sh does, and must be the only one left.
  security find-generic-password -s "$K" -w | ssh "$APP_SSH" 'set -eu; APPTOOL='"$(printf %q "$APPTOOL")"'
    OPTS="${APPTOOL/--entrypoint python/--entrypoint sh}"
    case "$OPTS" in *"--entrypoint python"*|*"--entrypoint sh"*"--entrypoint"*) echo "entrypoint not unique" >&2; exit 2;; *"--entrypoint sh"*) ;; *) echo "no sh entrypoint" >&2; exit 2;; esac
    sudo -n docker run -i --rm $OPTS '"$BACKEND_IMAGE"' -c "umask 077; cat > /work/pw; exec python -m nexloop_eios.initial_identity --manifest /in/identity-customer-5.json --create --identity-database-url-file /run/secrets/api_identity_dsn --password-file /work/pw --idempotency-key-file /in/identity-seal"' ;;
 "--apply open")
  need "$D/customer-5.username"
  security find-generic-password -s "$K" -w | python3 "$D/open_conversation.py" "$ORIGIN" "$CA" "$D/customer-5.username" "$REPO" ;;
 "--apply mark-dry"|"--apply mark")
  need "$D/approver3.username" "$D/persona-customer-5.json" "$D/webchat-consumers.json"
  DRY=--dry-run; [ "$2" = mark ] && DRY=
  security find-generic-password -s nexloop/stage/approver3 -w | (cd "$REPO" && python3 scripts/deploy/erase_synthetic_consumer.py mark \
    --origin "$ORIGIN" --ca-file "$CA" --username-file "$D/approver3.username" --password-stdin \
    --config "$D/persona-customer-5.json" --recipe "$D/webchat-consumers.json" $DRY) ;;
 *) echo "usage: $0 [--apply password|identity|open|mark-dry|mark]" >&2; exit 2 ;;
esac
```

`open_conversation.py`：

```python
#!/usr/bin/env python3
"""Open (or find) one WebChat conversation for one account, sending no message: creates the account's Consumer so its
consumer_id can be read and marked before it speaks. Password on stdin (one line); never printed.
    security find-generic-password -s nexloop/stage/customer-5 -w | python3 open_conversation.py ORIGIN CA_FILE USERNAME_FILE
Output: {"conversation": "existing"|"created"} only."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[4] if len(sys.argv) > 4 else '.') / 'packages/eios-core/src'))
from nexloop_eios.consumer_channel import ConsumerClient  # noqa: E402

origin, ca, username_file = sys.argv[1:4]
password = sys.stdin.readline().rstrip('\n')
client = ConsumerClient(origin, ca_file=ca)
client.login(Path(username_file).read_text().strip(), password)
try:
    items = client.list_conversations(limit=1).get('items') or []
    if not items:
        client.create_conversation('s6-20261030-open-customer-5')
    print(json.dumps({'conversation': 'existing' if items else 'created'}))
finally:
    client.logout()
```

### 2.3 时间

| 步 | 耗时 |
|---|---|
| A1–A6 核对 | 约 15 分钟（A1/A2 要请示时另计） |
| 预检，正式运行启动 | 约 5 分钟 |
| 校准 3 轮（每轮：等提示、登录、发送、最多 120 秒等回复、写标记、entry 判定，约 2–3 分钟） | 约 6–9 分钟 |
| 成本确认与 resume | 约 5 分钟 |
| 其余 6 轮（同上，逐轮） | 约 12–18 分钟 |
| 兜底时间（300 秒）过后，等最后一轮可能出现的兜底 Run 落定 | 约 10 分钟 |
| 再跑 `s6_stage_inputs.py`，填配置，记 sha256 | 约 10 分钟 |

合计约 **1–1.5 小时**，不含请示等待。发言必须落在 trial 窗口（北京时间 10:00–18:00）里。

### 2.4 window 与 X5

- window 由 `s6_stage_inputs.py` 给出，两侧各留 1 小时余量。所以**发言结束后至少 1 小时，OUT §3.1 才能开始**；否则 window 的结束时间会晚于 OUT 的开始。
- window 的起点不能早于 7.4 通过的时间 T（7.4 第 6 条）。
- 固定配置后记下 window 的 `until` 和配置的 sha256。OUT §3.1 开始前核对 `date -u` 晚于 `until`，并写进 S6 证据。
- window 一旦确定，就不要在里面再发言。重造数据要换 label（run-nx039 F5）。

## 3. protected_refs 没有联系限制或拒绝类定义时

`s6_stage_inputs.py` 的 blocked 有两种，处理不同：

| 情况 | 输出 | 处理 |
|---|---|---|
| P-1：读取结果里有这类定义，但没有出现在占满上限（10 条）的读取中 | `candidates` 里有 `wide_reads: 0` 的 ref | 这正是 A2 造成的。策略没有改为 top_k ≥ 10 时，**r3 判 blocked**，不手工填一个 ref 去凑：r3 的断言要求窗口内真实存在一次占满上限的读取，手工填 ref 只会让 r3 在内存规划时自己报 blocked。其余项不依赖 protected_refs 的“宽读”，可以照常进行，所以把 `candidates` 里第一个 ref 填进 `protected_refs`，满足配置校验，并在证据里注明“仅为满足配置，R3 blocked” |
| P-2：ontology 里根本没有这类定义（`candidates` 里 `reads: 0` 的项也没有） | `candidates` 为空 | 不要为了验收去发布一个联系限制定义：那是正式 schema 的变更，不是测试数据。r3 判 blocked，原因写“租户本体没有联系限制或拒绝定义”；`protected_refs` 填 ontology 里任意一个被读到过的 ref，满足配置校验，并在证据里注明。是否要补这类定义，报负责人决定 |

无论哪种情况，at023 的受保护表 digest（F7）不依赖 `protected_refs`，照常判定。

## 4. 时间表：赶在 10-31 结束前跑完 run-nx038 §2.2

### 4.1 预算的边界

- 预算期间的终点是 `2026-11-01T00:00:00Z`，也就是北京时间 **11-01 08:00**。所以“10-31 结束前”在 UTC 上还有余量，真正的截止时间是 11-01 08:00（北京时间）。
- 终点之后，开诊断、开评和 L1 预留都找不到覆盖当前时间的 evolution 上限，会报 `budget evolution not configured`，脚本判 blocked，**不会失败**。

### 4.2 推荐时间表（北京时间）

| 时间 | 内容 | 依赖 |
|---|---|---|
| 10-29 | 第 0 节 C1–C7 只读核对；A1/A2/A3 需要请示的当天报出 | — |
| 10-30 上午 | 负责人确认 A1/A2 以后，第 1 节建配置（DATA_HOST、管理机各一份），`all --dry-run` 和 `at024 --dry-run` 先跑一次。这时 window 还是 blocked，只核对连接与角色 | C5、C7 |
| 10-30 下午 | 第 2 节发言（负责人在场约 1 小时）→ 等 1 小时 → `s6_stage_inputs.py` → 固定配置并记 sha256 | A1–A6 |
| 10-30 晚 – 10-31 | plan 阶段 4 的总序 1–9（约 45–90 分钟）；OUT 在 X5 满足后开始，与总序交替；然后阶段 6（r7，在 OUT §4.4 之后）、阶段 7（r6、可选的 r8 正式发布） | 阶段 4 的 dry-run 无 blocked |
| 11-01 08:00 之前 | 必须完成所有开诊断、开评的步骤。r7-verify 和 r6 不开诊断，但 r7-prepare 会开评和 L1，所以要在这个时间之前 | — |

### 4.3 赶不上时：11-01 08:00 之后同额续期

条件（NX-056 设计稿 Q4，全部满足才免审批）：服务端时间不早于旧期间终点；新期间的起点等于旧期间的终点；长度相同（整月按月数比较，11-01 到 12-01 虽然少一天，也算同长）；上限和单位都不变。

1. **只读**：用 C1 的查询读出当前行和 revision，确认终点是 `2026-11-01 00:00:00+00`，并且 `date -u` 已经过了它。
2. **dry-run**（approver3，管理机，驱动形态同 run-nx038 §2.0，只用 `P3`/`D3`）：

   ```bash
   RENEW='{"budget_kind":"evolution","unit":"CNY","limit_amount":"50.00","period_start":"2026-11-01T00:00:00Z","period_end":"2026-12-01T00:00:00Z"}'
   "${D3[@]}" --dry-run request --operation set_budget --body-json "$RENEW"
   ```

   预期打印 `POST /api/v1/workbench/actions/set_budget` 和五个字段名，不连接。
3. **执行**：`"${D3[@]}" request --operation set_budget --body-json "$RENEW"`，预期 HTTP 200。C1 的查询应为一行 `CNY|50.00|2026-11-01 …|2026-12-01 …`，revision 加 1。
4. 判定：
   - 409 `budget_loosen_requires_approval`：时间还没到旧期间终点，或者字段与上面的条件不符。不要改走 `loosen_budget`；等到时间之后，或者核对字段再来。
   - 409 `budget_changed`：读到的行与预期不符，停下来报调度员。
   - 401/403：approver3 不是 active owner。判 blocked，不改用负责人。
5. 续期**不换 label，也不换 window**。总序从中断的那一步继续，已经完成的步骤重跑按重放处理。续期之前和之后的消耗分属两个期间，在证据里分开记录。

预算续期属于收紧以外唯一免审批的变化，所以不占用 approver2 的投票冷却，也不受 X7“BUD 只做一次”的限制（那一条说的是放宽）。

## 5. 证据

- 第 0 节各项核对的结果（只记计数和是否满足）；
- 两份配置的 sha256、window 的 `until`、OUT §3.1 开始时间；
- 发言的汇总行（不含正文）；
- `s6_stage_inputs.py` 前后两次的 `blocked` 列表；
- A1/A2/A3 的请示与决定（谁决定，什么时候）；
- 续期时：续期前后的预算行和 revision；
- A1：7.3 预检 echo 出的命令行（确认没有 python 入口）、发布输出的 `strategy_ref`/`replayed`、两个 relay 各自的 `docker inspect` 结果、7.4 的 T 与第 1、2 条结果，以及 S6 结束前每次重建或部署后的重跑结果（7.4 第 5 条）。

不写令牌、DSN、主机地址、用户名或正文。

## 6. 局限

- 第 0 节的现状来自调度员的说明，没有经过只读核对。
- 1.1 中 `s6_stage_schema.py` 在哪台主机上执行，只按源码和 pg_hba 推断，第一次 dry-run 才能确认。
- A3（失败口径下合成 Run 很少失败）没有在 stage 数据上验证过，只是按代码推断。
- 钥匙串条目名（演进角色口令、customer-N）以 vault-index 为准，本清单没有读过它。

## 7. 启用 A1：message-relay 改用 `ontology_hybrid`

### 7.1 策略是怎么选出来的（源码核对，01d875dd）

- **按进程参数选，不按租户绑定**。`message_relay.py` 把启动参数 `--context-strategy <id>` 传给 `ContextV6ArtifactProducer(source, strategy_id, …)`。组装时 `StrategyRegistry.get(strategy_id)` 不带版本，经 `authz.nexloop_context_read`（0090，verb `strategy`）取该租户、该 world 下这个 `strategy_id` 的**最新版本**。stage 的两个 relay 在 compose 里写的是 `--context-strategy, recent_plus_required`（`deploy/stage/compose.app.example.yaml` 两处）。 stage 的 compose 从第 58 批之后改为 `--context-strategy, "${RELAY_CONTEXT_STRATEGY:-recent_plus_required}"`（两个 relay 各一处），值来自私有的 `compose.app.env`，默认 `recent_plus_required`（`tests/test_stage_compose.py` 锁住默认值，覆盖时只到两个 relay）。
- **只发布 `ontology_hybrid` 不会生效**：relay 仍然按 `recent_plus_required` 取策略。
- **`nexloop.context.bind` 不是选择开关**：它是每个 Run 自己的 Source 凭据绑定这一次 Context（0052/0092），由 Run 签发时下发，不能用来给租户指定策略。没有租户级的“策略绑定”入口。
- **context 策略没有工作台 HTTP 入口**。api-routes 里的 `publish_strategy` 是 NX-043 的实验策略（`nexloop-strategy-version/1`），不是 context 策略。context 策略只能用 `nexloop_eios.context_strategy_setup` 发布：以 owner 身份登录（用户名和口令从文件读，0088 只允许人发布），在 APP_HOST 的一次性容器里跑，形态同 `run-assembly-20261015.md` §4。approver3 是 owner，workbench roles 的 owner 角色含 `nexloop.context.strategy.publish:1`。
- 兜底回复 Run 用的是 `reply-guarantee.v3` 的 `recent_plus_required`，不受影响；它也不做语义读取（E3 文档）。

### 7.2 最小步骤

| 步 | 谁 | 做什么 |
|---|---|---|
| 1 | 调度员 | 只读核对：`ro "select entity_key[2] from authz.nexloop_authority_facts where tenant_id='$TENANT_ID' and fact_kind='grants' and entity_key[1]='stage-principal-approver3' and entity_key[2]='eios:action:nexloop.context.strategy.publish:1'"` 一行；`ro "select strategy_id, version from control.nexloop_context_strategies where tenant_id='$TENANT_ID' and world='real'"` 只有 `recent_plus_required|1` |
| 2 | 调度员 | 在 APP_HOST 放好非秘密输入：配方 `<APP_RUN>/in/ctx-ontology-hybrid.json` = `{"schema_version":"nexloop-context-strategy-setup/1","tenant_id":"<TENANT_ID>","workbench_application_id":"<WORKBENCH_APPLICATION_ID>","strategy_id":"ontology_hybrid","version":1,"request_id":"context-strategy-ontology-hybrid-1"}`（属主 10001，0600）；以及 7.3 的远端脚本 `<APP_RUN>/a1-publish.sh` |
| 3 | **负责人在终端执行** | 7.3 的 `local-only/run-a1-context-strategy.sh`：先 dry-run，再发布 `ontology_hybrid@1` |
| 4 | 调度员 | **R55 部署时 env 先保持 `RELAY_CONTEXT_STRATEGY=recent_plus_required`**（或不写，即默认）；等第 3 步负责人发布 `ontology_hybrid@1` 成功之后，才在 stage 的 `compose.app.env` 里**改 `RELAY_CONTEXT_STRATEGY` 这一行为 `ontology_hybrid`，没有这一行就加上**；改完 `grep -c '^RELAY_CONTEXT_STRATEGY=' compose.app.env` 必须为 `1`。然后只重建两个 relay：`docker compose … up -d --no-deps message-relay message-relay-2`，**紧接着跑 7.4 第 1 条**（S20：变量值写错或策略未发布时部署门禁看不出来，只有新 Run 的 `strategy_ref` 或 relay 组装失败能暴露；第 1 条不符就按 7.5 撤回）。策略未发布就改 env，relay 的 Context 组装会失败 |
| 5 | 调度员 | 7.4 核对，包括对两个 relay 各自 `docker inspect` |

这不是本地偏离：变量在仓库的 compose 里，stage 只在私有 env 里改一个值，以后按仓库重新部署会保留这个值。S6 收尾以后是否撤回（7.5）由调度员决定，写进证据。

### 7.3 负责人脚本（调度员复制到 `local-only/`）

管理机：`local-only/run-a1-context-strategy.sh`。

```bash
#!/bin/bash
# A1: publish context strategy ontology_hybrid@1 as approver3 (owner). Run by the owner in a terminal.
set -euo pipefail
APP_SSH="${APP_SSH:?ssh target of APP_HOST deploy account}"
APP_RUN="${APP_RUN:?APP_HOST run directory holding a1-publish.sh and in/}"
U3="${U3:?approver3 workbench username (not the owner)}"
export SSL_CERT_FILE="$HOME/.nexloop/stage/endpoint-20261015/ca-bundle.crt"   # Python ignores the macOS keychain

echo "== dry-run (local, no login, no database)"
uv run --frozen python local-only/a1_strategy_check.py deploy/configuration/context-strategies.v1.json ontology_hybrid 1
echo "== APP_HOST preflight (sh entrypoint, substituted command line; no secret sent)"
ssh "$APP_SSH" "bash $APP_RUN/a1-publish.sh --preflight" || { echo "preflight failed: nothing sent, nothing published" >&2; exit 1; }

if [ "${1:-}" != "--publish" ]; then echo "dry-run only; rerun with --publish"; exit 0; fi

echo "== publish ontology_hybrid@1 as approver3"
if ! { printf '%s\n' "$U3"; security find-generic-password -s nexloop/stage/approver3 -w; } | ssh "$APP_SSH" "bash $APP_RUN/a1-publish.sh"; then
  echo "publish failed: see the JSON reason above (exit 2 = refused; nothing published)" >&2; exit 1
fi
```

dry-run 用的检查脚本由调度员放进 `local-only/a1_strategy_check.py`（仓库里没有），内容如下。它不读任何秘密，只核对策略文件里有这个版本、`semantic.enabled` 为真、并打印将发布的 `strategy_ref`：

```python
import json, sys
sys.path.insert(0, 'packages/eios-core/src')
from nexloop_eios.context_strategy_setup import strategy_definition, validate_recipe
f, sid, ver = sys.argv[1], sys.argv[2], int(sys.argv[3])
r = validate_recipe({'schema_version': 'nexloop-context-strategy-setup/1', 'tenant_id': '00000000-0000-4000-8000-000000000000',
                     'workbench_application_id': 'dry-run', 'strategy_id': sid, 'version': ver, 'request_id': 'dry-run'})
d = strategy_definition(f, r)
assert d['semantic']['enabled'] is True, 'semantic.enabled is false'
print(json.dumps({'strategy_ref': f'context-strategy:{sid}@{ver}', 'semantic': d['semantic']}))
```

APP_HOST：`<APP_RUN>/a1-publish.sh`（调度员放置，0700；变量沿用 run-assembly §0.2/§0.3 的 `$APPTOOL`、`$BACKEND`、`$BACKEND_IMAGE`、`$BROWSER_ORIGIN`，由该脚本自己 source 私有环境文件）。用户名和口令经 stdin 进容器的 tmpfs，**不落主机磁盘**，容器退出即消失：

```bash
#!/bin/bash
set -euo pipefail
source "$HOME/<私有环境文件>"   # APPTOOL BACKEND BACKEND_IMAGE BROWSER_ORIGIN APP_RUN
OPTS="${APPTOOL/--entrypoint python/--entrypoint sh}"
case "$OPTS" in *"--entrypoint python"*|*"--entrypoint sh"*"--entrypoint"*) echo "entrypoint substitution failed" >&2; exit 1;; esac
case "$OPTS" in *"--entrypoint sh"*) ;; *) echo "no sh entrypoint in the options" >&2; exit 1;; esac
INNER='umask 077; IFS= read -r u; IFS= read -r p; printf %s "$u" > /tmp/u; printf %s "$p" > /tmp/p; unset u p
  exec python -m nexloop_eios.context_strategy_setup --recipe /in/ctx-ontology-hybrid.json \
    --strategies-file /opt/release/deploy/configuration/context-strategies.v1.json '"$BACKEND"' \
    --identity-database-url-file /run/secrets/api_identity_dsn --username-file /tmp/u --password-file /tmp/p \
    --browser-rate-key-file /run/secrets/api_browser_rate_key --browser-origin '"$BROWSER_ORIGIN"
if [ "${1:-}" = "--preflight" ]; then
  echo "docker run -i $OPTS $BACKEND_IMAGE -c '$INNER'"
  docker run $OPTS "$BACKEND_IMAGE" -c true && echo "sh entrypoint: ok"
  exit 0
fi
docker run -i $OPTS "$BACKEND_IMAGE" -c "$INNER"
```

- 预期：stdout 一行 JSON，`strategy_ref = context-strategy:ontology_hybrid@1`，`replayed: false`；重跑 `replayed: true`，不会发布第二次（`request_id` 是稳定意图 ID）。
- 失败：stderr 一行 `{"ok": false, "reason": …}`，退出码 2，什么都没发布。`CredentialInvalid` 是用户名或口令不对；`strategy not declared …` 是发布检出不含该策略；其他异常类名报调度员。每次执行消耗一次 `local_login` 速率额度。
- **S18 预检**：管理机脚本每次先执行 `a1-publish.sh --preflight`：核对替换后的选项里只有 `--entrypoint sh`、已经没有 python 入口，echo 出完整的 `docker run` 命令行（只含挂载路径和参数，不含口令；口令此时还没有发送），再跑一次 `-c true`，退出码 0 才继续。镜像没有 `sh` 时停下报 L3，不要改成把口令写进主机文件。

### 7.4 启用后核对（调度员，只读）

第 4 步完成后，调度员对**两个 relay 容器各自**执行 `docker inspect --format '{{.Name}} {{json .Config.Cmd}}' <容器>`，确认各自都有 `--context-strategy`、`ontology_hybrid` 和 `--semantic-read-pool-max`、`1`，两份结果都写进证据（I13 a）。然后记下时间 `T=$(date -u +%FT%TZ)`，用一个合成顾客发一句话（可以就是 2.2 计划的第 1 轮）：

1. `ro "select strategy_ref, count(*) from runtime.nexloop_context_packs where tenant_id='$TENANT_ID' and world='real' and created_at > '$T' group by 1"`：只有 `context-strategy:ontology_hybrid@1`。出现 `recent_plus_required@1` 说明还有 relay 没切换。
2. `ro "select count(*), max(result_count), count(*) filter (where unresolved_count>0) from runtime.nexloop_semantic_accesses where tenant_id='$TENANT_ID' and world='real' and kind='access' and recorded_at > '$T'"`：第一列 ≥ 1。
3. relay 日志里语义读取的 `timeout`、`failed`、`skipped` 计数与 warning：偶发可以接受；连续出现时停下，按 7.5 撤回，报 L3。
4. 第 1 条通过、第 2 条为 0：读取被跳过或超时（第 3 条可见原因），停下，不进入第 2 节发言。
5. **I13 (b)**：从第 4 步起到 S6 结束（plan 阶段 7 完成）之前，APP_HOST 上**每次重建或部署之后**都要重跑第 1 条和两个 relay 的 inspect，结果写进那一次部署单的检查项。出现 `recent_plus_required@1` 或 inspect 不含 `ontology_hybrid` 时，停 S6 的发言和总序，先恢复 env 再继续。
6. **I13 (c)**：S6 配置的 `inputs.evolution.window.since` **不能早于 7.4 通过的时间 T**（`s6_stage_inputs.py` 给出的起点更早时，改为 T，并在证据里注明）。T 之前的 Run 没有语义读取，混进窗口只会稀释簇单元。

### 7.5 撤回

- **撤回：删掉 stage `compose.app.env` 里的 `RELAY_CONTEXT_STRATEGY` 这一行，只重建两个 relay**（回到默认 `recent_plus_required`）。立即生效，新 Run 回到 `recent_plus_required@1`，语义读取停止。之后按 7.4 第 1 条（期望 `recent_plus_required@1`）和两个 relay 的 inspect 再核对一次。
- 已发布的 `ontology_hybrid@1` **删不掉**（策略只能发新版本），留在库里无害：没有 relay 引用它就不会被使用。
- “发布回 `recent_plus_required`”不需要也不起作用：relay 一直按 id 取最新版本，`recent_plus_required@1` 没有动过；在 relay 仍指向 `ontology_hybrid` 时发布 `recent_plus_required` 的新版本，也不会改变 relay 的选择。
- 不存在“重新绑定”：`nexloop.context.bind` 是每个 Run 的绑定，不是租户的策略选择（7.1）。
