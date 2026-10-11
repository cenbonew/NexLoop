# AT-043 发放优惠：实现说明（第一部分）

分支 `at043-impl`，从 main `c835158` 切出。依据设计稿 `AT-043-incentive-design.md`（`at043-design` `fca52a9`，集成时由调度员带入）。调度员已裁定 D1（独立 Action）、D3（不改 Role ceiling 形状）、D6（失败时追加负额 discount 条目冲销）、D8（上下文列出可用目录，契约改动已批准）；D2、D4、D5、D7、D9 待负责人，本部分不依赖它们。

## 1. 迁移 0160 `at043_incentives`（临时号）

- 目录：`IncentiveOffering`（9 个字段：`incentive_code`、`kind=fixed_amount`、`max_amount_minor`、`per_consumer_period_limit_minor`、`currency`、`eligibility`、`title`、`active`、`valid_until`）与 `ConsumerIncentiveOffering`（与 ConsumerServiceOffering 同形）。`runtime.nexloop_incentive_catalog` 断言形状、当前修订、有效期、币种与单次上限。对象由现有受治理 create / edit Action 写入（`incentives.offering_schemas()`，`only_edit_via_actions`）。
- `runtime.nexloop_incentive_issues`：每个受治理意图一行；请求内容不可改，终态不再移动；FORCE RLS，只有 owner。
- 人类入口：0124 registry 的 `incentive.issue` / `issue_incentive`（human only）。受理时立即核对目录（目录外不生成意图，AT-010），并从存储行生成控制快照（Consumer 与 Action 范围、两个目录对象的修订、incentive 预算）；此时不预留。
- 执行端口 `authz.nexloop_incentive_command`（`nexloop-incentive-v1`，EXECUTE `nexloop.incentive.execute:1`，只给 domain worker 服务）：
  - `admit` 在派发时、意图行锁下依次检查：Consumer 未在删除中 → 所有者控制（`authz.nexloop_assert_dispatch_controls`，暂停、之后的控制事件、目录对象修订变化）→ 联系限制下附带通知整条拒绝（ADR-023 §3 / NX-026 D6）→ 目录复核 → 每个 Consumer 的周期上限 → 租户 incentive 预算（`authz.nexloop_reserve_budget`，`incentive:<intent>`；NXB01 → `budget_exhausted`，NXB03 → `budget_unconfigured`）。拒绝写入原因码与审计事件，provider 收到零请求。
  - `observe`：按当前 fence 记录 provider 答复。fulfilled → 结算 fully_used；failed → 结算 released：追加 `incentive:<intent>:settle` 释放行和负额 discount 条目 `discount:incentive:<intent>:settle`（`basis=budget_release`，D6），成本指标净额归零；unknown → 保持预留，执行者按 intent id 查询，绝不重发。
  - `due`、`status`。
- 费用条目：`basis` 增加 `budget_release`，只有它可以是负额。

## 2. 代码与配置

- `nexloop_eios/incentives.py`：目录类型、`IncentivePort`、回环 provider `LocalIncentiveLedger`（每个意图一条 fsync 的 JSON 记录，可按 intent id 查询，测试可注入 fail / 写前超时 / 写后超时）、执行者 `IncentiveIssuer`（受理的意图只 admit 一次、只发一次；unknown 与租约过期的 dispatching 只查询；同步回环账本查询为 not_found 视为明确未发放）。
- `goal_controls.GoalControls.issue_incentive`（人类入口）。
- `deploy/configuration/business-actions.v1.json` v13：`nexloop.incentive.issue`（human_owner）；`risk_level` 暂按 `low`，待 D2。`business_actions.PROFILES` 增加 `incentive.issue`。
- `deploy/authorization/service-grants.v1.json`：`nexloop.incentive.execute:1` 记入 `deferred`。执行者的服务主体、后台入口与部署接线属于部署配置改动，另行提交；测试中经 `seed_multi_authority` 授权。

## 3. 测试（真实 PG，回环 provider，合成数据）

`tests/test_incentives_pg.py`（9）：额度内发放只预留一次、只发一次并记 discount 成本，同请求重放；预算耗尽在派发前拒绝且已发放的保留（含额度调低）；未配置预算；未知结果先查询再对账（写后超时 → fulfilled；写前超时 → 查询未找到 → failed 并释放），过期 fence 的观察被拒；明确失败释放预算、负额条目使成本指标净额为 0、释放的额度可再用；目录外（超上限、币种、修订、他人绑定、停用）不受理；每 Consumer 周期上限；暂停、联系限制附带通知、删除中的 Consumer 在派发时拒绝；服务主体不能经人类入口发起，无执行授权的服务不能使用执行端口，应用角色不能直接读表。

## 4. 未做 / 待定

- Agent 发起（Role 内工具 `nexloop.incentive.issue`）与上下文目录（D8，`context-pack-v6` 契约改动已批准）：下一部分。
- D2（风险级别与审批）、D4（目录外单次发放）、D5（口头许诺目录外优惠）、D7（受限客户与 KR 驱动的发放）、D9（真实渠道）：等负责人。
- Consumer 删除（NX-029）尚未覆盖 `runtime.nexloop_incentive_issues`（`consumer_id` 与 `notification_text`）；表的守卫已为登记过的清除留口。
- 执行者服务主体与后台入口（部署配置）。
