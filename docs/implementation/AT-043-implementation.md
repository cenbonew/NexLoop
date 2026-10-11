# AT-043 发放优惠：实现说明

分支 `at043-impl`，从 main `c835158` 切出。设计稿 `AT-043-incentive-design.md`（已定案，随本分支提交）：调度员裁定 D1、D3、D6、D8，负责人 2026-10-11 对 D2、D4、D5、D7、D9 回复“全部按推荐”。

## 1. 迁移 0145 `at043_incentives`（合并时由临时号 0160 重新编号）

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
- Consumer 删除（NX-029）：`runtime.nexloop_erasure_step`（0144 函数体复制，加 `incentives` 阶段，位于 commitments 之后）。有 provider 答复未决（dispatching/unknown）的发放时先等待（执行者查询对账，不重发）；accepted 的改为 refused（`consumer_erasing`）；随后经清除登记把 `consumer_id` 换成随机假名、清空 `notification_text`、`reply_to_message_id` 与控制快照（快照里有 Consumer ID）。金额、状态、ID、预算消耗与费用条目不变。受理审计事件不再记录 Consumer ID。表守卫只对登记过的清除放行。
- 成本计数类指标：冲销条目（`budget_release`）不计单位（0120 `runtime.nexloop_cost_project` 函数体加一个条件），失败一笔只算一次。

## 2. 代码与配置

- `nexloop_eios/incentives.py`：目录类型、`IncentivePort`、`RunIncentivePort`、回环 provider `LocalIncentiveLedger`（fsync 的 JSON 记录，可按 intent id 查询，可注入 fail / 写前超时 / 写后超时）、执行者 `IncentiveIssuer`。
- `goal_controls.GoalControls.issue_incentive`；`backend` / `runtime_worker.issue_run_incentive`；守卫路由 `POST /internal/v1/runtime/incentives/issue`（400 / 403 `outside_catalog_terms` / 409 `incentive_payload_conflict` / 403 `incentive_unavailable`）。
- `context_engine/sections.open_work_items` 增加 incentive 条目。
- Agent Host：`RuntimeEffectClient.issueIncentive` 与工具 `nexloop.incentive.issue`，只在 Host 配置 `incentive_tool: true`（且 `effect_tools: true`）时提供。
- `business-actions.v1.json` v13/v15：`nexloop.incentive.issue`（human_owner，EIOS 风险 low，D2 见 §4）；`business_actions.PROFILES` 增加 `incentive.issue`。
- `service-grants.v1.json` v17：新主体 `incentive_issuer`（domain worker）持有 `nexloop.incentive.execute:1`；`runtime_worker` 持有 `nexloop.incentive.run_submit:1`（两项原在 v16 的 `deferred`，接线时转为正式授权，各写明理由）。
- 测试夹具：`runtime_effect_fixture.SOURCE_EXTRA_TARGETS`（默认空，测试模块按需 monkeypatch，让 Source 可以签发列出优惠 Action 的 Run）；`commercial_fixture.REGISTRY_ACTIONS` 增加该 Action。

## 3. 测试（真实 PG，回环 provider，合成数据）

- `tests/test_incentives_pg.py`（12）：失败一笔在计数指标中只算一次；Consumer 删除覆盖意图表（等待未决答复、拒绝 accepted、去掉识别信息、金额汇总不变）；额度内发放只预留一次、只发一次并记成本；预算耗尽派发前拒绝且已发放的保留；未配置预算；未知结果先查询再对账（写后超时 → fulfilled，写前超时 → 查询未找到 → failed 并释放），过期 fence 被拒；明确失败释放、成本指标净额为 0；目录外不受理；每 Consumer 周期上限；暂停、联系限制带通知、删除中的 Consumer；入口与端口的主体限制；D5 目录外许诺为异常、目录内许诺与普通服务许诺照常登记。
- `tests/test_incentive_runs_pg.py`（4）：复评 Run 在 Run 内发放（来源 plan、重放），客户受限后派发被拒（D7）；回复来信的 Run 对受限客户可发放，带通知整条拒绝；Run 未列出该 Action、超目录上限、篡改命令都不受理；Role Run 的 ceiling 未列出该 Action 时被拒，列出时受理。
- `tests/test_incentive_guard_route.py`（10）：守卫路由（真实 TLS、假 worker）传递确切参数、只返回固定回执，NXI01 / NXB02 / 其他拒绝 / 参数错误映射为固定码且不带细节，畸形请求不到达 worker，他人 Run 的回执不返回。
- `tests/test_incentive_context_v6_pg.py`（2）：v6 上下文列出该客户的优惠条目与可发区间、不含预算、不列他人的目录项；放大可发区间的上下文包在 bind 时被拒。
- `apps/agent-host/test/runtime-effect-tools.test.ts`：工具默认不提供、开启后发送确切参数、只接受固定形状回执。

## 4. 待定与已知限制

- D2（负责人 2026-10-11）：EIOS 风险 low、无审批；NexLoop 分类 medium，补偿控制为三重约束（目录单次上限、每 Consumer 周期上限、租户预算）。在 EIOS 里按 low 发布，不接审批流，不改 vendored ActionGovernor；`business-actions.v1.json` v15 的 decision 与 purpose 已同步（v14 留给先合入的 NX-035）（purpose 不进入编译出的 Action 定义，已发布定义不变）。
- 回复来源与 Role Run 只在 SQL 端口层测过：测试中按写入者形状补写的消息 Run 签发记录与 Role 绑定不是完整的消息路由 / Role 上下文，守卫调用前的 Python 激活预检（`RuntimeActivationPort.authorize`，要求完整的消息路由或 Role 上下文）会拒绝；构造完整消息路由需要真实的入站投递与消息 Run 签发链路，成本过高。激活、任务租约、命令一致的权威复核都在 SQL 端口中，守卫预检路径由复评 Run 测试经 `issue_run_incentive` 覆盖，守卫路由本身由 `test_incentive_guard_route.py` 覆盖。
- D5 的金额换算假定两位小数币种（主单位 × 100 = 最小单位）；零位或三位小数的币种需要另行处理，调度员已确认保留此假定。

## 5. 部署接线（分支 at043-wiring，从 main 560f49e 切出）

- 后台入口 `nexloop-incentive-issuer`（`background_services` 的 `incentive-issuer`，nexloop_domain_worker，单进程、连接池 1）：每个 tick 重新读取并认证服务凭据，领取 accepted 意图、在派发时 admit、经回环账本发放一次，unknown 只查询；`--ledger-directory` 必填（D9：回环账本是唯一 provider，私有可写目录）。
- 容器：`container_entrypoint` 增加 `incentive-issuer` 作业（账本目录 `/var/lib/nexloop/incentive-ledger`）；`compose.test.yaml` 增加 opt-in（profile `background`）服务与独立的 `incentive_ledger` 卷，其他服务不挂载它。
- 连接预算：`connection-budget.v1.json` 增加 `incentive-issuer`（1），默认四个 guard worker 时合计由 52 变为 53（上限 60）；guard worker 增到 5 仍在上限内，6 个将超过 60（原先 6 个恰好 60）。
- 授权：`service-grants.v1.json` v17（见 §2）。
- Agent Host：`incentive_tool` 开关默认关（配置中不出现即关闭；只接受 `true`，且需 `effect_tools: true`）；部署示例 `agent-host.runtime-config.example.json` 保持不开启。
- 测试：`test_background_services_pg.py` 中该入口以 manifest 凭据跑一次空闲 tick，并领取一条 accepted 意图完成回环发放（预算预留、账本一条记录，第二次 tick 不重发）；`apps/agent-host/test/incentive-tool-profile.test.ts` 验证未开启时 Host 不暴露该工具、开启时暴露、`incentive_tool` 取非 `true` 值或未开 `effect_tools` 时拒绝启动。

