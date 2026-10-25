# NX-057 R1：补救意图，只生成建议

- **来源**：设计稿 `docs/design/remediation-intents.md` 第 4 版 §3、§4、§5、§6.1、§9.2、§9.4、§9.5、§11、§13 的 R1；L6 复核 R0 与第 4 版通过（调度员 2026-10-23）。L7 执行。
- **分支**：`l7/nx057-r0`。开发时基点为 main `76a9ab00`；2026-10-25 第二十四批（含 R0，即 `0325_effect_failures`）合入 main `9779abb6` 后，R1、R2 在 `9779abb6` 上重建为一个提交，原来逐个复核过的提交保留在本地分支 `l7/nx057-r0-pre-rebase-9779`。
- **迁移**：`remediation_intents`，现编号 0337，接在 main 尾部 0336 之后（之前的临时编号为 0326）。本文只用文件名 stem 指代迁移。
- **范围**：只开放 `escalate`。`reevaluate` 留给 R2，`governed_action` 留给 R3，规则清单写了这两种时 SQL 与 Python 都会拒绝。

## 做了什么

### 规则（受信配置）

- 规则文件 `deploy/configuration/remediation-rules.v1.json`：
  - 限值取设计稿推荐值（Q-5）：每租户每小时 20 条；每个 Consumer 24 小时 3 条。
  - R1 交付时规则为空，等负责人决定（Q-1 至 Q-3）。没有规则时，每条失败记为 `no_rule`，不生成任何补救。
  - **负责人 2026-10-25 决定都按推荐**：现在只有一条规则 `deliver_final_failure`，源为 `nexloop.enterprise.deliver:1`，覆盖两种失败，reevaluate，automatic（需要 R2）。效应类别中只有这一个 Action 声明为外部效应，见设计稿 §14。
- `nexloop_eios.remediation`：
  - `validate_rules`、`load_rules`、`apply_rules`，以及命令行 `python -m nexloop_eios.remediation rules --manifest … (--check|--apply)`；
  - 只有 configurator 能发布。
- SQL `control.nexloop_configure_remediation_rules` 失败关闭，写入只追加的 `control.nexloop_remediation_rules`（FORCE RLS）：
  - 键集合精确匹配；
  - source 必须是本租户在效应类别（0153 `nexloop_action_effect_externality`）中声明为外部效应的 Action，精确到版本；
  - 失败种类只能是 `origin_expired` 与 `confirmed_not_delivered`；
  - `nexloop.service.request` 必须声明 `exclude_bound_replies=true`；
  - 同一个 source 版本、同一种失败、同一种 kind 只能有一条规则；
  - 版本只增；同一份清单重放时返回 `replay`。

### 规划器（服务身份，`eios:action:nexloop.remediation.plan:1`）

- **运行位置**：plan-reevaluator 进程中的第二个 tick（与 reply-guarantor 中的接管到期处理同样的做法）：
  - 每分钟最多一轮，每轮最多 50 条失败记录；
  - 在语句内设置 `statement_timeout` 5 秒、`lock_timeout` 1 秒；
  - 失败记录用 `for update skip locked` 领取。
- **端口**：`authz.nexloop_remediation_plan_command`，复用 `authz.nexloop_plan_port_tenant` 的签名校验，租户来自服务身份。
- **服务授权**：`deploy/authorization/service-grants.v1.json` v19 给 plan_reevaluator 授予这一项。没有授权时规划器回报 `unavailable`，重评估照常进行。
- **每一轮依次做**：
  1. 到期的 proposed 补救记为 `expired`，不论开关状态；
  2. 读取三道开关：部署层 `REMEDIATION_ENABLED`（只有精确的 `true` 才打开）、租户开关、租户控制暂停（NXC01 范围）；
  3. 每条**新**失败记录只考虑一次，结果写入 `runtime.nexloop_remediation_scans`：
     - `generated`：生成了补救；
     - `no_rule`：没有匹配的规则；
     - `not_eligible`：原因是 `refused_one_reply:*`；
     - `not_real`：world 不是 real；
     - `not_failed`：效应意图其实不是 failed；
     - `bound_reply`：是绑定回复，由回复保底负责；
     - `rate_capped`：超过租户每小时限值；
     - `switch_closed`：任一道开关关闭。
  4. 开关关闭期间的失败记为 `switch_closed`，**以后也不会补生成**（L6 M-5：只对之后的失败生效）。
- **速率上限**（S-2）：超出后每租户每小时只累加一行 `runtime.nexloop_remediation_rate_caps`，不逐条生成补救。
- **Consumer 链条上限**（§9.2）：超出后补救只能是 escalate，码为 `chain_capped`，并立即升级。
- `automatic` 的 escalate 立即进入 `escalated`（交给负责人）；`manual` 停在 `proposed`，等负责人处理。
- **补救表不存 consumer id**：Consumer 经源效应意图读出，因此删除传播仍只作用在效应意图上，补救表上没有需要另外删除的身份。

### 负责人操作（人类受治理 Action，只限 owner）

| Action | 能力 | 作用 |
|---|---|---|
| `nexloop.remediation.resolve` | `remediation.resolve` | 把一条 proposed 补救标记为已处理（`escalated`，码 `marked_handled`）或取消（`declined`，码 `cancelled`）。必须填写原因；原因只存在补救事件中，不进入读取接口 |
| `nexloop.remediation.switch` | `remediation.switch` | 暂停（立即生效）或恢复本租户的补救。每次切换是一个新的只追加版本 |

- 两项都登记在 `deploy/configuration/business-actions.v1.json` v41、`capabilities.v1.json`、`system-catalog-names.v1.json`、`deploy/authorization/workbench-roles.v1.json` v37（新的 `remediation` 范围，owner 专用，并列入 owner-only 集合），以及 `business_actions.py` 和 `workbench_roles.py`。
- **与设计稿的差异**：设计稿 §11 列了四项操作（标记已处理、取消、pause、resume），实现合并为两项，各带一个字段（outcome、paused）。这样配置面更小。以后如果负责人决定 resume 需要审批（Q-6），可以给 `remediation.switch` 挂一条 NX-056 条件策略（`paused` 等于 false 时需要审批），pause 仍然不需要审批。

### 读取与展示

- 工作台读取动词 `remediations`（`nexloop.workbench.read:1`，所有成员可读），用改名再包一层的方式加入 `authz.nexloop_workbench_read`，其余动词不变：
  - Consumer id 只在成员角色对该 Consumer 有 READ 时给出（ADR-025）；
  - 正在删除的 Consumer 的补救不列出，只计入 `hidden`；
  - 不返回原因文本。
- HTTP：读取为 `GET /api/v1/workbench/remediations`（可按 state 过滤）；两个负责人操作走现有的 `POST /api/v1/workbench/actions/resolve_remediation` 与 `switch_remediation`。
- 工作台“Action / 异常”页新增“补救”一栏：
  - 状态汇总，以及暂停或恢复补救的开关；
  - 列表显示源 Action、失败原因、规则、方式、状态与期限；
  - proposed 的行可以标记已处理或取消，必须填写原因。
- 标签写入 `labels.ts` 与术语表 §2.6，包括补救状态、补救方式和最终失败原因。

### 指标与告警

- 快照新增 `remediation` 块（只有计数）：`by_state`、`waiting_over_24h`、`generated_last_hour`、`rate_capped_last_hour`、`paused`。
- 告警规则 `alert-rules.v14.json` 等于 v13 加 3 条：`remediation_waiting`、`remediation_rate_capped`、`remediation_paused`。
  - 设计稿中 `remediation_paused` 是 info 级。告警路径只支持 warning 和 critical（Python、SQL、状态表、前端类型四处），所以这里用 warning。
  - `remediation_failed` 与 `remediation_rule_held` 随 R2 加入（熔断属于 R2）。
  - stage 目前仍然指向 v11，v14 是否上 stage 由调度员按部署变更决定。

## 告警规则版本撞号（调度员 2026-10-23，已处理）

- L3 的 NX-056（`l3/unified-approvals-impl`）已经占用 `alert-rules.v14.json`：v13 的 61 条加 6 条审批规则（`approval_waiting` 至 `approval_solo_eligibility_reduced`），共 67 条，随第二十四批合入。
- 本分支原来的 v14 已删除，改为 `alert-rules.v15.json`：前 67 条与 L3 的 v14 逐条相同（随第二十四批进入 main），再追加本线的 4 条（R1 的 3 条加 R2 的 `remediation_rule_held`），共 71 条。
- 测试 `test_alert_rules_v15_are_nx056_v14_plus_the_remediation_rules`：
  - 前 61 条等于 v13；第 62 至 67 条是 NX-056 的 6 条审批规则（按名称核对）；最后 4 条是补救规则；
  - 前 67 条与 main 上的 v14 逐条相同（测试核对）。

## L6 复核 f90b2330 之后的修改（2026-10-23）

| 编号 | 问题 | 修改 |
|---|---|---|
| 重要 1 | `statement_timeout` 在函数体里设置，对正在执行的这次调用不起作用 | 由端口在调用前执行 `set local statement_timeout='5000ms'`（`RemediationPort._prepare`）；函数里只保留每次等锁时都会读取的 `lock_timeout`。新增测试：用一个睡 8 秒的替身命令，确认约 5 秒时被取消（57014），规划器回报 `{'status':'error','code':'57014'}` |
| 重要 2 | 自动升级的补救直接进 `escalated`，与“owner 已处理”混在一起，之后没人能关，也没有提醒 | 新增终态 `handled`。`escalated` 表示“交给人、待处理”，是打开的状态：owner 可以标为已处理（`handled`）或取消（`declined`）；`remediation_waiting` 把超过 24 小时未处理的 `escalated` 也计入 |
| 建议 3 | `failure_id` 单独唯一 | 改为 `unique(failure_id, rule_id)`，直接改在 `remediation_intents` 中，R2 迁移不再另做修改 |
| 建议 4 | 并发两轮时租户上限可能超出 | 每轮在租户上加一把事务级 advisory lock（`pg_try_advisory_xact_lock`）；第二个规划器直接回答 `busy`，不等待。新增测试 |
| 建议 5 | 规划器的异常冒泡，拖垮整个 reevaluator tick | `RemediationPlanner.run_once` 捕获 `psycopg.Error`，写进 `summary['remediation']`，本轮回滚 |
| 建议 6 | 状态迁移不校验 | `runtime.nexloop_remediation_move` 只允许设计 §4.2 的迁移，其余抛出 `illegal_transition`。新增测试：`handled` 不能回到 `proposed` |
| 建议 7 | 没有数据库层的全局开关 | 设计取舍，保持不变：部署层开关只在各进程的环境变量里，由调度员在组批时接入 compose |
| 建议 8 | 负向测试不足 | 补上并发（`busy`）、超时、非法迁移三项；“两条规则匹配同一失败”由 R2 的手动唤醒测试覆盖（reevaluate 与 escalate 两条规则同时匹配） |

复核修正后的测试（2026-10-25，基线换到 main `97072c26` 之后；负载门控通过后用 `pytest -n 2`）：

| 命令 | 结果 |
|---|---|
| `pytest -n 2`：R0、R1、R2 与受影响的现有测试，共 24 个文件（新增 `test_erasure_whole_database_pg.py`） | 250 passed。`test_remediation_pg.py` 共 18 个：新增并发 `busy`、5 秒超时（替身命令约 5 秒被取消，规划器回报 `error 57014`）、`handled` 不能回到 `proposed`、自动升级后 owner 能处理且计入 `waiting_over_24h`；规则文件只有一条 `deliver_final_failure` 并与效应类别一致 |
| `vitest run apps/web/test`；`tsc --noEmit` | 32 个文件、179 passed；无错误 |
| `check_definer_search_path.py`；`check_publication.py` | 0 problems；0 violations |

## 在 main `9779abb6` 上重建后的回归（2026-10-25）

- 共享文件三方合并，第二十四批与本线都往同一批注册表里追加条目的地方，两边都保留：
  - 动作数 109 加 3 为 112；
  - owner 角色 75 加 3 为 78；
  - business-actions v45、workbench-roles v41、service-grants v19；
  - DONE_TEXT 与 OPERATION_ACTION 两边的条目都保留。
- 迁移改为 0337、0338，重算 sha，`bootstrap_revision` 改为 0338。告警规则 v15 等于 main 的 v14 加本线 4 条。
- 负载门控后用 `pytest -n 2` 跑了 30 个文件：R0 到 R2 及受影响的现有测试，另加 NX-056 的审批视图、审批门、首批策略、策略发布，以及 NX-058 的邀请与重置码。
  - **第一次运行**：287 passed，1 failed。`test_stage_register_pg.py` 的第二处动作数断言仍是 109：它不在冲突块里，合并时保留了 main 的值。改为 112 后，该文件 9 passed。
- vitest 33 个文件、184 passed；`tsc` 无错误；definer 检查 0 problems；`check_publication` 0 violations。

## 租户外键（第二十六批回归，2026-10-25）

- **缺陷**：`test_tenant_fk_pg.py::test_every_table_with_tenant_id_has_a_tenant_path_or_a_recorded_exception` 失败。R1、R2 新增的 6 张带 tenant_id 的表，既没有通向 `control.nexloop_tenants` 的外键，也没有登记为例外：
  - `remediation_rules`、`remediation_events`、`remediation_scans`、`remediation_rate_caps`、`remediation_switches`；
  - `remediation_rule_holds`。
- 补救意图表本身经 `(source_intent_id, tenant_id, world)` 指向效应意图的外键已有租户路径。R1 交付时没有跑这个测试。
- **修法**：6 张表的 `tenant_id` 直接 `references control.nexloop_tenants(tenant_id)`，做法与 NX-056 的审批表相同；没有登记例外。0337、0338 未发布，直接改原文件并重算 sha。
- **全库结构性测试**（负载门控后 `pytest -n 2`）：
  - 范围：租户外键、R1 至 R2、复制件守卫、已发布迁移、引导、删除覆盖（4 个文件）、全库删除搜索、应用角色权限边界、函数 search_path、授权漂移、NX-041 的世界、替身与角色检查、NX-042 T7 验收、S6 阶段演进，共 18 个文件。
  - 第一次运行 196 passed，1 failed：`test_nx042_t7_acceptance_pg.py` 中真实 Pi 的 twin Run 启动 Agent Host 时退出。原因是本工作树的 `apps/agent-host/dist` 还是 10-18 的构建，与本次修改无关。重新 `npm run build` 后，该文件 5 passed。
  - definer 检查 0 problems。

## 规划器节拍的测试隔离（第二十六批 CI，2026-10-25）

- **现象**：`test_the_planner_runs_once_a_minute_and_is_off_without_its_grant` 拿到 `not_due`，期望是 `open`；`test_the_port_bounds_the_round_at_5_seconds_and_the_planner_reports_it` 拿到 `{'status': 'not_due'}`，期望是 `error 57014`。
- **原因**：“每分钟一轮”的节拍状态 `_LAST_ROUND` 是进程级字典，键为（租户, world）。worker 每个 tick 都新建规划器，所以生产环境需要它。但同一个 pytest 进程里，各测试用的是同一个租户和各自的假时钟：前一条测试的规划器留下一个更大的时间值，后一条测试的规划器就被判为未到时。结果取决于 xdist 把哪些测试分到同一个进程、以什么顺序执行，与墙钟无关。
- **修法**：`RemediationPlanner` 新增参数 `rounds`，即节拍状态：
  - 不传时仍用进程级状态，生产行为不变，测试断言了这一点；
  - 带自己时钟的调用方传入自己的状态。测试里的三处规划器都改成各自独立的状态；
  - 每分钟一轮的测试还用 `monkeypatch` 在进程级状态里预置一个“更晚的一轮”，证明它不再受进程级状态影响。没有放宽断言，也没有加重试。
- **证据**（负载门控后）：
  - **确定性对照**：按“5 秒测试在前、每分钟一轮测试在后”的顺序在单进程跑。修复前的 `fb1e03ae`：后一条失败，`'not_due' == 'open'`，与 CI 相同；修复后：2 passed。
  - **连跑**：`pytest -n 3 tests/test_remediation_pg.py` 连跑 6 轮，默认分发、`--dist load`、`--dist loadfile`（整个文件在同一进程）各两轮，负载 3.2 至 7.1，每轮 18 passed。
- **连接失败（PostgreSQL connection failed）**：修复前后共 11 轮 `-n 3`，负载不超过 7，都没有重现，见调度员另一条报告。

## 上线说明（L6 对 R2 复核的建议）

- 开关关闭期间发生的最终失败会**永久**记为 `switch_closed`，开关打开后也不补生成。打开开关之前的失败不会有补救，这是有意的（M-5）。
- 规则文件 `deploy/configuration/remediation-rules.v1.json` 不会自动生效，要由 configurator 手动发布。stage 上用配置员窗口（G1，`docs/release/run-nx057-remediation-stage.md` §3）：

  ```
  python3 scripts/deploy/remediation_rules_apply.py --manifest deploy/configuration/remediation-rules.v1.json --tenant <租户> --expected-revision 0 --bootstrap-dsn-file <bootstrap socket DSN 文件>
  ```

  已有 configurator DSN 文件时，也可以用 `python -m nexloop_eios.remediation rules --manifest … --tenant <租户> --apply --expected-revision 0 --database-url-file <configurator DSN 文件>`。两者都按文件原文发布，库里记录文件的 sha256 和 expected_revision（迁移 `remediation_rule_publications`）。

  它要求本租户已经发布效应类别 v3（`nexloop.enterprise.deliver:1` 声明为外部效应），否则会以 `source_not_effect` 拒绝。
- 部署层 `REMEDIATION_ENABLED` 默认关闭（Q-7）。G2 已把它接入 compose：只给 `plan-reevaluator`，私有 compose env 中默认 `false`。

## 没有做的（有意留给后续）

- **部署层开关**：G2（分支 `l7/nx057-g1-g2`）之前没有接入 compose。现在已接入，默认关闭；在 stage 上打开按执行单 `docs/release/run-nx057-remediation-stage.md` §4。关闭期间，规划器把每条失败都记为 `switch_closed`。
- **熔断与 `remediation.rule_release`**：属于 R2。
- **规划器在每次推进状态之前重读开关**：设计稿 §9.4 要求的这一点，R1 只在每轮开始时读取一次。R1 的每次推进都发生在同一个事务里，这个事务就是一轮，所以没有区别；R2 有了跨事务的唤醒之后要补上。

## 测试

环境：真实 PostgreSQL 一次性集群（NX-030 夹具），真实 configurator、真实 domain worker 服务凭据、真实工作台登录；Node 24；`pytest -n 3`。

| 命令 | 结果 |
|---|---|
| `pytest -n 3 tests/test_remediation_pg.py` | 11 passed：<br>- **规则校验**（不连库）：v1 文件为空规则加推荐限值；reevaluate、governed_action 被拒；只接受两种 R0 原因；重复规则、错误的 mode 与期限、`nexloop.service.request` 未排除绑定回复都被拒；<br>- **SQL 发布**：未声明为外部效应的版本被拒（`source_not_effect`）；同一清单重放返回 replay；版本必须增大；非 configurator 被拒；只追加；<br>- **生成**：两种原因各生成一条 proposed；`refused_one_reply`、无规则的版本、意图不是 failed 的记录都不生成；第二轮不重复；事件 `null→proposed`；automatic 立即升级（`escalated_automatic`）；<br>- **开关**：部署层未设置或值不是 `true` 时为 `closed:deployment`，重新打开后关闭期间的失败仍不补生成（`switch_closed`）；租户控制暂停为 `closed:control`；<br>- **上限**：租户每小时上限 2 时，超出的 2 条累加为一行 `over_count=2`；Consumer 上限 1 时，第二条为 escalate 加 `chain_capped` 并立即升级；快照 `rate_capped_last_hour=2`；<br>- **到期**：到期的 proposed 在开关关闭时也记为 expired；<br>- **节奏与授权**：每分钟一轮（第 10 秒返回 `not_due`，第 61 秒再跑）；没有 `remediation.plan` 授权的身份返回 `unavailable`；<br>- **工作台**：owner 读取列表（无原因文本），标记已处理与取消，再次处理返回 409 `not_open`，非法 outcome 返回 422，暂停后规划器为 `closed:tenant`、快照 `paused=1`；operator 能读但切换返回 403；客户身份读取返回 403；<br>- **权限**：6 个应用角色对 6 张表都没有任何权限，FORCE RLS，四张表只追加；<br>- **告警**：v14 等于 v13 加 3 条 |
| `pytest -n 3`：R1 加上受影响的现有测试，共 25 个文件（R0、复制件守卫、已发布迁移、引导、business-actions、目录清单、受治理入口、工作台读取与写入、服务授权、stage 注册、观测、主机健康、计划重评估、接管、后台服务、NX-041 出站表声明、确认未送达、来源过期、系统目录、授权跟随、角色清单） | 262 passed（第一次运行时 3 个注册表断言失败：动作数 102 变为 104、受治理入口的能力列表和 business-actions 的快照列表缺少新的两项、owner 角色的 Action 数 68 变为 70。均为新增 Action 后预期的变化，已同步更新断言） |
| `vitest run apps/web/test`（新增 `remediation.test.ts`） | 32 个文件、179 passed |
| `check_definer_search_path.py`；`check_publication.py` | 0 problems；0 violations |

浏览器预览没有做：补救一栏需要登录后的工作台和带数据的后端，本机没有运行 stage。页面只用了现有的 DataTable、StatusBadge 和 Form 组件；数据形状由 PG 测试中的真实 HTTP 读取和 vitest 中的解析测试覆盖。
