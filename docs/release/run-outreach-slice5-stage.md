# 执行单：外联第 5 片在 stage 上的验收（计划复评 Run 经 `nexloop.outreach.run_submit:1` 申请主动消息）

外联第 5 片（迁移 0239，`authz.nexloop_run_outreach_command`；`service-grants` v18 授予 runtime_worker `nexloop.outreach.run_submit:1`）已在 main 上，stage 授权已于 2026-10-28 随 NX-054 §8 第 6 步的 register 生效（authority_revision 3197）。负责人 2026-10-28 接受“Agent 主动外发在 stage 上可用”，要求提前验收。

本单在 stage 上让计划复评 Run 发起主动消息，只发给合成顾客 `s6c-1`..`s6c-3`，并验证：

> **10-30 改用 s6c 账号（L6 I18）**：customer-1/2/3 的 WebChat 授权已在 S6 重建（rebuild-s6c R2）中撤销，它们的装配也已过期（valid_until 10-22），customer-4 已擦除。OUT 改用 s6c-1/2/3（persona `simulated_agent`，装配 valid_until 2026-12-01；完整 consumer_id 在私有 `local-only/s6c-20261030/index.json`）。旧 customer-1..4 只作“没有误发”的对照。

| # | 验收项 | 本单的判定依据 |
|---|---|---|
| V1 | 申请被接受后，仍要经过执行器准入和第 4 片的各道闸 | `outreach_sends` 行从 `accepted` 变为终态，`admitted_at` 非空，`refusal_gates` 记录了逐道闸的结论 |
| V2 | 回复兜底 Run 不能发起（ADR-023 §2.7） | stage 上只读核对 + 本地测试证据（§6 说明为什么 stage 上不能主动构造） |
| V3 | Role 上限没有 `outreach.send` 时被拒 | 阴性对照顾客的 Run 没有任何 agent 行；阳性对照顾客有 |
| V4 | 联系限制生效时被拒 | 阳性对照（受限顾客）终态为 `refused / contact_restricted`；阴性对照（未受限顾客）终态不是 `contact_restricted` |
| V5 | `initiator='agent'` 的行带 `run_id` | 只读查询；表上的 CHECK 也保证这一点 |

- **执行者**：调度员（命令与只读查询）。**人工步骤**：只用合成 owner `approver3`（工作台）与合成顾客账号（WebChat）；负责人账号不参与。**判定者**：L6。证据里三者分开写。
- **按 main 17aa74c1 核对过**（只读核对源码，未连主机）：
  - SQL 入口 `authz.nexloop_run_outreach_command`（0239）：依次复核活动激活、任务租约与命令原文 → 兜底 Run 一律拒绝（`authz.nexloop_is_fallback_run`）→ 必须是计划复评 Run（`runtime.nexloop_plan_runs`）→ Run 凭据含 `nexloop.outreach.send:1` → 有 Role 绑定时 Role 执行上限也含它、且消费者一致 → `runtime.nexloop_outreach_accept(…,'agent','run:<run_id>',…,run_id)`；
  - 受理不发送；执行端口 `nexloop.outreach.execute:1`（`authz.nexloop_outreach_command`，只给 `nexloop_domain_worker`）在派发时依次检查联系限制、请求方当前权限（Agent 发起时要求 Role 上限仍含发送）、负责人控制快照，再过目的、营销资格、测试人设、勿扰、频率、消费者偏好、三层真实派发开关、模型预算；
  - Host 工具 `nexloop.outreach.send` 只在运行配置同时有 `outreach_tool: true`、`plan_outcome_tool: true`、`effect_tools: true` 时提供，并且只给非消息 Run（`apps/agent-host/src/runtime-host.ts`）；
  - 表 `runtime.nexloop_outreach_sends` 的 CHECK：`(initiator='agent') = (run_id is not null)`，且 agent 行的 `principal_id = 'run:'||run_id`（0239）。
  - 本地测试：`tests/test_outreach_runs_pg.py`（4 个用例）、`apps/agent-host/test/outreach-tool-profile.test.ts`。

## 0. 缺口：入口不全，本单在补齐前只能执行 §2 的只读部分

在 17aa74c1 上，下面几项不具备，第 3 节以后的步骤都依赖它们：

| # | 缺口 | 影响 | 补件 | 估计 |
|---|---|---|---|---|
| **G-1** | **没有外联执行器进程**。`nexloop_eios.outreach.OutreachSender` 只在测试里使用，没有 CLI、没有后台服务；`service-grants.v1.json` 中没有任何主体持有 `nexloop.outreach.execute:1`，`stage-principals.v1.json` 也没有对应主体 | 受理后的行永远停在 `accepted`，V1、V4 无法在 stage 上观察 | 代码（L 线）：`python -m nexloop_eios.outreach_sender --once / --tick-seconds N`，provider 只用 `LocalOutreachOutbox`（私有目录，0700）；service-grants 新版本给新主体 `outreach_sender`（`nexloop_domain_worker`）授予 `nexloop.outreach.execute:1`；stage-principals 增加该主体；compose 增加服务与 secret 分发；测试：CLI 的 `--once` 与 `run_once` 结果一致、凭据只读私有文件、未知结果只查询、`test_stage_register_pg` 与 compose 相关测试更新 | **1.5–2 天**（含 L6 复核）；部署件改动走 ack-policy |
| **G-2** | **stage 的 Host 运行配置没有 `outreach_tool` 和 `plan_outcome_tool`**（`deploy/stage/agent-host.runtime-config.example.json` 只有 `effect_tools: true`） | 计划复评 Run 拿不到 `nexloop.outreach.send` 工具，不可能发起 | 部署配置：两项都设为 `true`，重启 Host。注意：`plan_outcome_tool` 同时让计划复评 Run 获得计划结论工具，属于行为变化，需调度员确认 | 0.25 天；部署配置改动走 ack-policy |
| **G-3** | **Role 上限与 Role Source 凭据未核实**。stage 上现有 Role 的 `RoleExecutionCeiling.action_resources` 是否含 `nexloop.outreach.send:1`、计划复评 Run 的凭据 `allowed_resources` 是否含它，源码里看不出，要在 stage 只读核对（§2.2、§2.3） | 两者缺一，SQL 入口都会拒绝，阳性对照无法成立 | 不缺代码：对阳性对照顾客用 `nexloop_eios.role_assembly` 以新 `request_id` 组装一个 `action_resources` 含 `eios:action:nexloop.outreach.send:1` 的 Role，再按 run-g-plan-20261017 §3–§6 重新登记 relay、写运行配置。Role Source 凭据的目标集合若不含发送，需重签（具体工具以 §2.3 的结果为准，若现有签发工具不支持增加目标，则另列代码缺口） | 0.5 天操作；如需重签工具另估 0.5 天 |
| **G-4** | **触发不确定**：stage 的 Host 是 `deepseek-flash`，计划复评 Run 是否调用发送工具由模型决定 | V1、V3、V4 的“阳性”可能需要多次触发 | 不改代码。用 §3.2 的计划步骤与 `commitment_followup` 场景引导；每个顾客最多触发 3 次（计划自身的 `self_trigger_cap` 为 2，间隔受 `min_reassess_seconds` 约束）；3 次都没调用时记“未触发”，不判 failed，交回调度员 | — |

G-1 是唯一必须写代码的缺口。G-1、G-2 合入并部署后，本单才执行 §3 以后的步骤。

## 1. 前提（不满足就不开始）

| # | 前提 | 怎么核对 |
|---|---|---|
| P1 | 迁移 0238、0239 在库里；stage 已到 service-grants v18 或更高（authority_revision ≥ 3197） | `ro "select version from control.schema_migrations where version in ('0238','0239') order by 1"` 为 2 行（`ro` 见 run-nx057-remediation-stage §0.3：只读事务、`set local role nexloop_owner`、设置 `eios.tenant_id`） |
| P2 | G-1 的执行器已部署并运行 | `docker ps` 中有外联执行器容器；它的日志只有计数行 |
| P3 | G-2 的 Host 配置已生效 | Host 启动日志不报 `runtime configuration refused`；运行配置文件中两项为 `true`（只看键，不贴文件） |
| P4 | `approver3` 可登录工作台，角色为 owner | run-nx056-approvals-enable 的人员表 |
| P5 | `s6c-1`..`s6c-3` 可登录 WebChat，口令在钥匙串 `nexloop/stage/s6c-N`；每个账号已有会话（S6 发言时开出；没有时先用 `open_conversation.py` 开一个，否则 `synthetic_customer.py` 预检会拒绝） | rebuild-s6c R1、R10 |
| P5a | **S6 的 9 轮发言已全部结束，S6 window 已固定**，`date -u` 晚于 window 的 `until`（X4、S6 prep §2.4） | S6 证据中的 `until` |
| P6 | 目的 `commitment_followup` 在租户已启用 | `approver3` 在工作台“外联目的”页查看；未启用时由 `approver3` 用 `set_outreach_purpose` 启用（受治理，留审计）。不启用营销类目的 |
| P7 | 执行时间在租户勿扰时段之外 | 工作台“外联控制”页；否则所有行会以勿扰闸拒绝，V4 的阴性对照无法区分 |
| P8 | **三层真实派发开关的实际状态已读出并写进证据**（调度员 2026-10-28 核实：stage 上三层都是开的，所以本单不能假设开关关闭） | 部署层：`docker exec <action-worker 容器> printenv REAL_DISPATCH_ENABLED`，**只记值是 `true` 还是 `false`**，不贴其他环境变量；租户层与 Action 层：`ro "select world, scope_kind, scope_ref, enabled, data_mode, revision from control.nexloop_real_dispatch_switches order by 1,2,3"`（租户层 `scope_kind='tenant'`、`scope_ref='*'`；Action 层 `scope_kind='action_type'`，看 `eios:action:nexloop.outreach.send:1` 一行，没有该行即该 Action 层为关）。执行后再读一次，前后一致 |
| P9 | **硬前提：外联执行器的 provider 只有 `LocalOutreachOutbox`（回环，写私有目录），执行器没有任何渠道凭据**。开关打开时，这是唯一挡住对外发送的一道；不满足就不开始 | ① 执行器的启动命令与配置：只有本地 outbox 目录参数，没有 provider 选择项或渠道地址（核对部署的源码树中执行器入口只构造 `LocalOutreachOutbox`：`grep -n "LocalOutreachOutbox\|Provider" <源码树>/…/outreach_sender.py`，以 G-1 合入后的文件为准）；② `docker inspect <执行器容器> --format '{{json .Mounts}}'` 只列出它自己的 DSN、服务凭据、PG 客户端证书和 outbox 目录，没有任何渠道、provider 或 webhook 的 secret；③ `docker inspect … --format '{{json .Config.Env}}'` 只看变量**名**，没有渠道类变量；④ 执行器的网络：只有到 PG 的连接（与其他 worker 相同的 compose 网络），不需要出站。任何一项不满足，判 blocked，交回调度员 |

## 2. 只读核对（现在就能做，不改任何东西）

### 2.1 外联记录现状

```bash
ro "select initiator, state, refusal_code, count(*) from runtime.nexloop_outreach_sends group by 1,2,3 order by 1,2,3"
```

预期：没有 `initiator='agent'` 的行（调度员 2026-10-28 授权生效后的只读核对：0 行）。

### 2.2 各顾客 Role 上限是否含发送

```bash
ro "select l.properties->>'consumer_id', d.object_id, o.properties->'action_resources' ? 'eios:action:nexloop.outreach.send:1'
    from ontology.objects d join ontology.objects o on o.tenant_id=d.tenant_id and o.world=d.world
     and o.type_name='RoleExecutionCeiling' and o.object_id=d.properties->>'ceiling_ref'
    join ontology.objects l on l.tenant_id=d.tenant_id and l.world=d.world and l.type_name='ConsumerRoleLink'
     and l.properties->>'role_id'=d.object_id
    where d.type_name='RoleDefinition' order by 1"
```

记下每个顾客的 Role 与布尔值。`ConsumerRoleLink` 的属性 `consumer_id`、`role_id` 与 0057 的唯一索引一致。

**一次性库核对（L6 S2）**：§2.1、§2.2、§2.3、P1、P8 的开关查询、§3.1、§4.1、§4.2、§4.3 的 SQL，已在一次性 PostgreSQL 上按 stage 方式组装的租户（`tests/test_plan_setup_stage_pg.py` 的夹具：register → business/catalog/enterprise → relay 登记 → role_assembly → plan_setup → plan-reevaluator 首轮发起 Role Run）中，用与 `ro` 相同的会话（只读事务、`set local role nexloop_owner`、设置 `eios.tenant_id`）逐条执行，全部无错（L4，2026-10-28，main 17aa74c1）。结果：§2.2 一行，Role 上限不含发送（false）；§2.3 一行，Run 凭据不含发送（false）；§2.1、§4.1、§4.2 无行；§4.3 为 0；开关表无行（夹具未设开关）；P1 两行。这只证明 SQL 在该 schema 上可执行、列名正确，不代表 stage 的数据。

### 2.3 最近的计划复评 Run 凭据

```bash
ro "select p.created_at, 'eios:action:nexloop.outreach.send:1' = any(r.allowed_resources)
    from runtime.nexloop_plan_runs p join authz.nexloop_run_credentials r on r.run_id=p.run_id
    order by p.created_at desc limit 5"
```

第二列全为 false 时，G-3 的 Role Source 凭据部分成立。

## 3. 对照设计

| 顾客 | 角色 | Role 上限含发送 | 联系限制 | 期望 |
|---|---|---|---|---|
| `s6c-1` | V4 阴性对照 | 是（G-3 组装） | 无 | 有 agent 行，终态**不是** `contact_restricted`，具体以 `refusal_gates` 为准：三层开关都开（P8）且其余各闸通过时，预期 `fulfilled`，请求写进执行器的本地 outbox（P9）；也可能被测试人设、勿扰、频率等后面的闸拒绝 |
| `s6c-2` | V4 阳性对照 | 是（G-3 组装） | **有**（§3.1） | 有 agent 行，终态 `refused / contact_restricted`；联系限制是第一道闸 |
| `s6c-3` | V3 阴性对照 | **否**（保持 rebuild-s6c R7 的 Role：上限只有 `nexloop.service.request:1`） | 无 | 计划复评 Run 照常运行，**没有** agent 行（SQL 入口以 `outreach outside the Role ceiling` 或 `outreach outside the Run` 拒绝，工具调用失败） |
| 旧 `customer-1`..`customer-4` | 不参与 | — | — | 不触发（授权已撤销或已擦除，装配已过期），作为“没有误发”的对照：全程 0 行 |

G-3 给 `s6c-1`、`s6c-2` 重新组装含发送的 Role 并重新登记 relay，同样要等 S6 的 9 轮发言结束、window 固定之后（P5a）：登记修订号变化会落进 S6 的输入。

本单不改变三层真实派发开关，按 P8 读出的实际状态执行并记录（stage 上 10-28 为三层都开）。开关打开时，挡住对外发送的只有 P9：执行器的 provider 只有回环 `LocalOutreachOutbox`，没有任何渠道凭据；真实 WebChat 投递不在第 5 片范围内（outreach-impl-plan §4 “留给后续”）。`fulfilled` 的行只表示请求写进了本地 outbox，不表示顾客收到。

### 3.1 给 `s6c-2` 加联系限制（负责人终端，一条命令）

**必须在 S6 的 9 轮发言全部结束、window 固定之后**（P5a）：这条消息会写入联系限制，落在 S6 window 里就会改变 S6 的输入。

`s6c-2` 在自己的会话里发一条命中拒绝规则 v2 `stop-contact` 的话（“以后不要再联系我了。”）。计划 `out-restrict.json`（`plan_id` `out-s6c-20261030-restrict`，一轮，120 秒等回复），先 dry-run 再执行：

```bash
REPO=<release 检出> RULES=deploy/configuration/contact-refusal-rules.v2.json bash local-only/out-s6c/out-restrict.sh
REPO=<release 检出> RULES=deploy/configuration/contact-refusal-rules.v2.json bash local-only/out-s6c/out-restrict.sh --apply
```

- dry-run 用 `check_refusal.py` 核对这句话**命中** `refuse / stop-contact`（与 S6 发言前的核对方向相反：S6 的正文必须全部不命中）；没命中就不发。
- 口令只走钥匙串管道（`s6c-2` 和 owner 一行一个），不打印、不落文件。

```bash
ro "select active, certainty, rule_id from control.nexloop_contact_restrictions where consumer_id='<s6c-2 的 consumer_id>'"
```

预期：`t | refuse | stop-contact`。没有生效就停，不往下做。

### 3.2 触发计划复评 Run（调度员；**B**）

按 run-g-plan-20261017 §5–§6 的方式，为 `s6c-1`、`s6c-2`、`s6c-3` 各建立（或复用）一个计划步骤，`reassess_at` 设在约 5 分钟后，计划步骤说明写成承诺跟进场景（例如“顾客预约的服务明天开始，提醒顾客如需调整请回复”），让模型有理由使用 `commitment_followup`。常驻的 plan-reevaluator 到期后自动认领。

## 4. 执行后的只读核对

### 4.1 agent 行（V1、V5）

```bash
ro "select s.consumer_id, s.initiator, s.run_id is not null, s.principal_id = 'run:'||s.run_id::text, s.origin_ref like 'plan:%',
           s.purpose_id, s.state, s.refusal_code, s.admitted_at is not null
    from runtime.nexloop_outreach_sends s where s.initiator='agent' order by s.created_at"
```

| 列 | 预期 |
|---|---|
| `run_id is not null` | 全部 `t`（V5） |
| `principal_id = 'run:'||run_id` | 全部 `t` |
| `origin_ref like 'plan:%'` | 全部 `t` |
| `state` | 终态（`refused`、`fulfilled` 或 `failed`），没有长期停在 `accepted` 的行；`admitted_at` 非空（V1） |

### 4.2 逐道闸（V1、V4）

```bash
ro "select consumer_id, refusal_code, refusal_gates from runtime.nexloop_outreach_sends where initiator='agent' order by created_at"
```

- `s6c-2`：`refusal_code='contact_restricted'`（阳性对照）；
- `s6c-1`：`refusal_code` 不是 `contact_restricted`（阴性对照），`refusal_gates` 显示联系限制闸通过；
- `s6c-3`：没有行（V3）；旧 `customer-1`..`customer-4`：没有行（对照）。

### 4.3 兜底 Run 没有发起（V2）

```bash
ro "select count(*) from runtime.nexloop_outreach_sends s where s.initiator='agent' and authz.nexloop_is_fallback_run(s.run_id)"
```

预期 0。

### 4.4 联系限制解除（负责人终端，一条命令：`approver3`）

**只要做过 §3.1，无论结果如何都要做**（X4）。`approver3` 经工作台 Action `release_contact_restriction` 解除 `s6c-2` 的限制，理由“外联第 5 片验收结束（OUT §4.4）”。先不带 `--apply`（只登录、登出），再执行：

```bash
security find-generic-password -s nexloop/stage/approver3 -w | python3 local-only/out-s6c/release_restriction.py <release 检出> https://<APP_HOST_NAME>:<PORT> "$HOME/.nexloop/stage/endpoint-20261015/ca-bundle.crt" local-only/out-s6c/approver3.username <s6c-2 的 consumer_id> --apply
```

预期 `{"ok": true, "applied": true, "status": 200, ...}`。再查 §3.1 的语句，预期 `active = f`。§4.4 完成之前，不用 `s6c-2` 跑新的合成交互。

## 5. 证据（不含口令、令牌、DSN、主机地址和消息正文）

- 执行者（调度员）：执行时间（UTC）、main 与 release、G-1/G-2 的部署记录；§2、§4 各查询的输出（`consumer_id` 只写前 12 位）；P8 三层开关执行前后的状态（部署层只写 true/false）；P9 四项核对的结论（只写是否满足与挂载、变量的名字，不写值）；
- 人工步骤（`approver3`、`s6c-2`）：时间与操作名，不写正文；
- 判定者（L6）：按 §3 的期望逐项判 V1–V5 为 passed / failed / 未触发（G-4）。

## 6. 说明与限制

- **V2 在 stage 上只能做否定性核对**：Host 只把发送工具提供给非消息 Run，兜底回复 Run 是消息 Run，所以它在 stage 上根本拿不到这个工具；SQL 侧的拒绝（`authz.nexloop_is_fallback_run`）只能在本地构造。判定依据是 §4.3 的 0 行，加本地测试 `test_outreach_runs_pg.py::test_only_a_plan_reevaluation_run_naming_the_send_may_ask` 与 `outreach-tool-profile.test.ts`（测试证据，与 stage 证据分开写）。
- **V3 的拒绝在工具调用处**：被拒的申请不写 `outreach_sends`，所以 stage 上看到的是“没有行”；如果需要正面证据，可在 Run 的工具调用记录中查该次调用的错误码（只记错误码，不记正文）。
- **模型不确定**（G-4）：阳性对照没有出现，可能是模型没有调用工具，不是系统拒绝；判“未触发”，不判 failed。
- 本单不改变真实派发开关（按 P8 记录实际状态），不启用营销类目的，不给 `s6c-1`..`s6c-3` 以外的任何顾客触发。

## 附：负责人脚本（调度员复制到 `local-only/out-s6c/`，0700；私有文件 0600）

`customers.json` 与 S6 的同一份（s6c-1/2/3，`persona_check` 走 workbench）；`check_refusal.py` 见 S6 prep §2.2。

`out-restrict.sh`：

```bash
#!/bin/bash
# OUT §3.1 (owner terminal, management machine): s6c-2 says one sentence that hits refusal rule v2 stop-contact.
#   bash out-restrict.sh            dry run: the sentence must hit refuse/stop-contact (check_refusal.py exit 1 with that id)
#   bash out-restrict.sh --apply    sends it (one turn, 120 s reply wait) through synthetic_customer.py
# Only after S6's nine turns are over and its window is fixed (X4). s6c-2 must already have a conversation (S6 gave it one;
# otherwise run open_conversation.py for s6c-2 first: the preflight refuses an account without one).
set -euo pipefail
D=$(cd "$(dirname "$0")" && pwd)
: "${REPO:?release checkout}" "${RULES:?the current contact-refusal-rules file, e.g. deploy/configuration/contact-refusal-rules.v2.json}"
export SSL_CERT_FILE=$HOME/.nexloop/stage/endpoint-20261015/ca-bundle.crt
HITS=$(python3 "$D/check_refusal.py" "$REPO/$RULES" "$D/out-restrict.json" || true)
case "$HITS" in *"refuse stop-contact"*) echo "rule check: hits stop-contact";; *) echo "rule check: no stop-contact hit; not sent" >&2; exit 2;; esac
[ "${1:-}" = --apply ] || exit 0
( printf 's6c-2\t%s\n' "$(security find-generic-password -s nexloop/stage/s6c-2 -w)"
  printf 'owner\t%s\n' "$(security find-generic-password -s "${OWNER_KEYCHAIN:-nexloop/stage/approver3}" -w)"
) | (cd "$REPO" && uv run --frozen python scripts/acceptance/synthetic_customer.py --config "$D/customers.json" --plan "$D/out-restrict.json")
```

`release_restriction.py`：

```python
#!/usr/bin/env python3
"""OUT §4.4: approver3 releases one Consumer's contact restriction (workbench Action release_contact_restriction).
    security find-generic-password -s nexloop/stage/approver3 -w | \\
      python3 release_restriction.py REPO ORIGIN CA_FILE USERNAME_FILE CONSUMER_ID [--apply]
Without --apply: log in and out only (checks the account and the origin). Password on stdin (one line), never printed.
Output: one JSON line {ok, applied, status} - no id beyond the first 12 hex, no body, no token."""
import json, sys
from pathlib import Path
repo, origin, ca, username_file, consumer = sys.argv[1:6]
apply = sys.argv[6:] == ['--apply']
sys.path.insert(0, str(Path(repo) / 'scripts/deploy'))
from real_dispatch_switch import HttpsTransport, Workbench, read_private  # noqa: E402
password = sys.stdin.readline().rstrip('\n')
w = Workbench(HttpsTransport(origin, ca), origin)
w.login(read_private(username_file, 1024).strip(), password)
try:
    status = None
    if apply:
        status, _ = w._call('POST', '/api/v1/workbench/actions/release_contact_restriction',
                            {'consumer_id': consumer, 'reason': '外联第 5 片验收结束（OUT §4.4）'},
                            {'X-CSRF-Token': w.csrf, 'Idempotency-Key': 'out-s6c-release-' + consumer[:16]})
finally:
    w.logout()
print(json.dumps({'ok': status in (None, 200), 'applied': apply, 'status': status, 'consumer': consumer[:12]}))
sys.exit(0 if status in (None, 200) else 1)
```
