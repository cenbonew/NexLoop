# AT-043 发放优惠：实现说明

分支 `at043-impl`，从 main `c835158` 切出。设计稿 `AT-043-incentive-design.md`（已定案，随本分支提交）：调度员裁定 D1、D3、D6、D8，负责人 2026-10-11 对 D2、D4、D5、D7、D9 回复“全部按推荐”。

## 1. 迁移 0160 `at043_incentives`（临时号）

- 目录：`IncentiveOffering`（`incentive_code`、`kind=fixed_amount`、`max_amount_minor`、`per_consumer_period_limit_minor`、`currency`、`eligibility`、`title`、`active`、`valid_until`）与 `ConsumerIncentiveOffering`。`runtime.nexloop_incentive_catalog` 断言形状、当前修订、有效期、币种与单次上限，目录外报 NXI01。D4：负责人同样只能从目录发放。
- 意图：`runtime.nexloop_incentive_issues`，含来源 `origin`（human / reply / plan / agent）、Run 与所回复的来信；请求内容不可改，终态不再移动；FORCE RLS，只有 owner。受理（`runtime.nexloop_incentive_accept`）先核对目录，再记录意图与控制快照，不预留。
- 两个入口：
  - 人类：0124 registry `incentive.issue` / `issue_incentive`（human only）。
  - Agent：`authz.nexloop_run_incentive_command`（`nexloop.incentive.run_submit:1`，runtime worker）。与 NX-024 run-outcome 相同的复核：Run 的活动激活、调用者自己的任务租约、完全一致的命令；Run 凭据须列出 `nexloop.incentive.issue:1`；Role Run 还须在 Role ceiling 的 `action_resources` 内（D3，形状不变）；Consumer 取自 Run 命令；来源：为某条来信签发的消息 Run 为 reply，复评计划 Run 为 plan，其余为 agent；每个 Run 每个目录项一笔，业务键由系统派生。
- 执行端口 `authz.nexloop_incentive_command`（`nexloop.incentive.execute:1`）：admit 在派发时、行锁下依次检查——Consumer 未在删除中、所有者控制、联系限制下附带通知整条拒绝（D6/ADR-023 §3）、D7（受限客户只接受负责人发起或回复来信的发放，plan/agent 来源拒绝 `contact_restricted`）、目录复核、每 Consumer 周期上限、租户 incentive 预算（NXB01/NXB03）。observe 按 fence 记录：fulfilled 结算 fully_used；failed 释放并追加负额 discount 条目（D6）；unknown 保持预留、只查询不重发。
- 费用条目：`basis` 增加 `budget_release`，只有它可以为负。
- D5：NX-026 登记（0127 的 `commitment_prepare_claim` 函数体加一段判定）：规则 `incentive-promise-v1` 识别企业已送达话语中的优惠许诺（折扣、优惠券、代金券、返现、立减、减免、红包、赠送、免单等）；该客户没有覆盖它的有效绑定目录项（金额以两位小数币种的主单位换算，币种一致）时，记异常 `unauthorized_incentive_promise`，不登记 Commitment，也不发放。
- D8：v6 上下文 `open_work` 增加 `incentive` 条目（ref `eios:object:IncentiveOffering/<id>`，formal_object）：目录项与本周期仍可发给该客户的区间 `issuable_minor`（单次上限与每 Consumer 周期剩余额度取小），不含预算余额；SQL 在 bind 时重算（0113 的 open-work 读取与来源核对函数体各加一个分支）。不钉住：预算紧张时可被裁掉。契约形状未变（open_work 条目本身是通用形状）。
- D9：只有回环 provider。

## 2. 代码与配置

- `nexloop_eios/incentives.py`：目录类型、`IncentivePort`、`RunIncentivePort`、回环 provider `LocalIncentiveLedger`（fsync 的 JSON 记录，可按 intent id 查询，可注入 fail / 写前超时 / 写后超时）、执行者 `IncentiveIssuer`。
- `goal_controls.GoalControls.issue_incentive`；`backend` / `runtime_worker.issue_run_incentive`；守卫路由 `POST /internal/v1/runtime/incentives/issue`（400 / 403 `outside_catalog_terms` / 409 `incentive_payload_conflict` / 403 `incentive_unavailable`）。
- `context_engine/sections.open_work_items` 增加 incentive 条目。
- Agent Host：`RuntimeEffectClient.issueIncentive` 与工具 `nexloop.incentive.issue`，只在 Host 配置 `incentive_tool: true`（且 `effect_tools: true`）时提供。
- `business-actions.v1.json` v13：`nexloop.incentive.issue`（human_owner，`risk_level` 见 §4）；`business_actions.PROFILES` 增加 `incentive.issue`。
- `service-grants.v1.json`：`nexloop.incentive.execute:1`（执行者）与 `nexloop.incentive.run_submit:1`（runtime worker）记入 `deferred`：执行者服务主体、后台入口与 Host 开关属于部署配置改动，另行提交。
- 测试夹具：`runtime_effect_fixture.SOURCE_EXTRA_TARGETS`（默认空，测试模块按需 monkeypatch，让 Source 可以签发列出优惠 Action 的 Run）；`commercial_fixture.REGISTRY_ACTIONS` 增加该 Action。

## 3. 测试（真实 PG，回环 provider，合成数据）

- `tests/test_incentives_pg.py`（10）：额度内发放只预留一次、只发一次并记成本；预算耗尽派发前拒绝且已发放的保留；未配置预算；未知结果先查询再对账（写后超时 → fulfilled，写前超时 → 查询未找到 → failed 并释放），过期 fence 被拒；明确失败释放、成本指标净额为 0；目录外不受理；每 Consumer 周期上限；暂停、联系限制带通知、删除中的 Consumer；入口与端口的主体限制；D5 目录外许诺为异常、目录内许诺与普通服务许诺照常登记。
- `tests/test_incentive_runs_pg.py`（3）：复评 Run 在 Run 内发放（来源 plan、重放），客户受限后派发被拒（D7）；回复来信的 Run 对受限客户可发放，带通知整条拒绝；Run 未列出该 Action、超目录上限、篡改命令都不受理。
- `tests/test_incentive_context_v6_pg.py`（2）：v6 上下文列出该客户的优惠条目与可发区间、不含预算、不列他人的目录项；放大可发区间的上下文包在 bind 时被拒。
- `apps/agent-host/test/runtime-effect-tools.test.ts`：工具默认不提供、开启后发送确切参数、只接受固定形状回执。

## 4. 待定与已知限制

- D2 与 EIOS 治理冲突：负责人选定 `risk_level=medium`、`approval_mode=none`，但 vendored EIOS 的 ActionGovernor 对非 low 风险一律要求审批证据，部署清单也只发布 low 风险 Action；按 medium 发布会让负责人发放必须走审批流。暂按 low 发布（三重约束不变），请调度员裁定：保持 low 并在文档中记录 NexLoop 自己的 medium 分类，或接入审批流。
- 回复来源的 Agent 测试直接调用 SQL 端口：测试中补写的消息 Run 签发记录不是完整的消息路由，守卫的激活预检会拒绝；激活、租约、命令的权威复核在 SQL 中，守卫预检路径由复评 Run 测试覆盖。Role ceiling 分支没有专门的 Role Run 测试。
- 部署：执行者服务主体、后台入口、Host 的 `incentive_tool` 开关与两项授权（deferred）待部署配置提交。
- Consumer 删除（NX-029）尚未覆盖 `runtime.nexloop_incentive_issues`（`consumer_id`、`notification_text`）；表守卫已为登记过的清除留口。
- 成本“计数”类指标会把冲销条目也计为一个单位；金额类指标正确。
- D5 的金额换算假定两位小数币种。
