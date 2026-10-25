# NX-057 R2：自动唤醒计划、origin、熔断

- **来源**：设计稿 `docs/design/remediation-intents.md` 第 4 版 §4.3、§6.2、§9.1–§9.3、§13 的 R2；调度员 2026-10-23“继续做 R2”。L7 执行。
- **分支**：`l7/nx057-r0`，接在 R1 之后；现基点 main `9779abb6`（重建说明见 R1 文档）。
- **迁移**：`remediation_wake`，现编号 0338（之前的临时编号为 0327）。

## 做了什么

### 规则

- 开放 `reevaluate`（automatic 与 manual 两种 mode 都可以）。`governed_action` 仍然拒绝，留给 R3。
- 限值新增两项：
  - `plan_wakes_per_week`：同一个 plan 在 7 天内最多被唤醒几次，推荐值 2；
  - `breaker_per_day`：熔断阈值，推荐值 3。
- 规则文件 v1 已补上这两项，仍然没有任何规则。

### 唤醒（`runtime.nexloop_remediation_wake`，definer，唯一的唤醒入口）

在补救的行锁内依次做：

1. **重读开关**（§9.4）：
   - 规划器推进状态时检查三道开关：部署层、租户开关、租户控制暂停；
   - 负责人自己的唤醒不受部署层限制（部署层是规划器进程的开关），租户开关和控制暂停同样生效，关闭时返回 409 `switch_closed`；
   - 开关关闭时，补救保持 proposed。
2. **找等待的计划**：与 0106 观测触发器的规则相同，找当前版本的 step 或 outcome 引用了源意图的 active plan。找不到时记为 `not_applicable`，码 `no_plan`。
3. **plan 上限**（§9.2）：按补救表统计这个 plan 7 天内被唤醒的次数，不看 feed，也不看 feed 中最多 16 条的 trigger 列表。达到上限时不唤醒，记为 `escalated`，码 `chain_capped`。
4. **唤醒**：用 `{kind: final_failure, cause: self, intent_id, failure_kind, failure_id, remediation_id}` 触碰这个 plan 的 feed，记为 `succeeded`，码 `woken`。之后由 NX-024 照常做预检，需要时开一次有预算的重评估 Run。NX-024 的自触发节流只会延后（J-2）。

### origin 与深度上限（§9.1，按补救表派生，L6 K-2）

- NX-024 为某个 plan 开 Run（往 `runtime.nexloop_plan_runs` 写入一行）时，触发器把这个 plan 已唤醒、尚未消费的补救都记上这个 Run（`consumed_by_run`）。
- **派生规则**：意图所属的 Run 取 `origin_run_id`，没有时取最早一次提交的 Run（与 0042 的认领一致）。如果这个 Run 消费过某次唤醒，这个意图就是补救来源的意图。
- **派生方式**：只在服务端按事实派生，没有任何工具参数能设置它。补救行记下 `origin_remediation_id`。
- **结果**：补救来源的意图再失败，只生成 escalate，码 `remediation_origin`，并立即升级给负责人。
- R1 复核修正后，`escalated` 是打开的状态（待人处理）：这些升级与 plan 上限造成的 `chain_capped` 升级都要由 owner 标为已处理（`handled`）或取消；超过 24 小时未处理的计入 `remediation_waiting`。

### 熔断（§9.3）与 `remediation.rule_release`

- 一条规则在 24 小时内出现以下任一情况，达到 `breaker_per_day` 次时被 held（`control.nexloop_remediation_rule_holds`），之后不再生成补救，扫描结果记为 `rule_held`：
  - 补救来源的意图失败（按 origin 补救所属的规则计数）；
  - 补救被记为 `chain_capped`（上一条补救在 24 小时内的事件）。
- **计数窗口**（L6 对 R2 的复核）：从“最近 24 小时”与“上一次解除时间”中较晚的一个开始，所以解除之后不会因为解除前的计数马上又被 held；`chain_capped` 按补救事件计数，不看行上的 `last_code`。
- **解除**：新的负责人 Action `nexloop.remediation.rule_release`（owner 专用，必须填写原因）。解除后这条规则从下一次失败起重新生成补救，被 held 期间的失败不补生成。
- **登记位置**：`business-actions.v1.json` v42、`capabilities.v1.json`、`system-catalog-names.v1.json`、`workbench-roles.v1.json` v38（remediation 范围），以及 `business_actions.py` 和 `workbench_roles.py`。
- **governed_action 失败计入熔断**属于 R3。

### 负责人的手动唤醒

- `remediation.resolve` 新增 outcome `wake`，只用于 manual 的 reevaluate，走同一个唤醒函数，受同样的 plan 上限与开关约束。
- 对 escalate 类补救选择 wake 时返回 409 `not_reevaluate`。

### 读取、指标与告警

- 快照的 `remediation` 块新增：
  - `held_rules`：当前被 held 的规则数；
  - `woken_last_24h`：24 小时内的唤醒次数。
- 工作台 `remediations` 读取新增：
  - 每行的 `plan_id`；
  - `held_rules` 列表，含规则、原因、次数与时间。
- “补救”一栏的变化：
  - 显示被熔断的规则，每条附“解除熔断”按钮；
  - manual 的 reevaluate 多出“唤醒计划”这一选项。
- 告警规则 v15（NX-056 的 v14 加本线 4 条）包含 `remediation_rule_held`（critical），见 R1 文档“告警规则版本撞号”。

### 复制件守卫

- 这次在 `remediation_wake` 中 `create or replace` 了三个 R1 函数：
  - `control.nexloop_configure_remediation_rules`；
  - `authz.nexloop_remediation_plan_command`；
  - `runtime.nexloop_governed_resolve_remediation`。
- 每处改动逐条列在 `tests/support/remediation_r2_edits.py` 中，迁移由同一份清单生成，守卫按这份清单把新函数体还原成旧函数体。
- 快照与工作台列表用改名再包一层的方式扩展。

## R2 中修正的 R1 遗漏

- **约束错误**：`runtime.nexloop_remediation_intents.failure_id` 原来是单独 unique，等于“每个失败最多一条补救”。设计要求的是每个失败、每条规则最多一条（§9）。同一种失败匹配两条规则时，第二条会违反约束，整轮回滚。R2 的新测试（同一种失败同时有 reevaluate 与 escalate 规则）发现了它，L6 复核 R1 时也独立指出（建议 3）。现已直接改在 `remediation_intents` 中，改为 `unique(failure_id, rule_id)`。
- **删除覆盖清单**：R1 新增的 `runtime.nexloop_remediation_events.actor` 与 `control.nexloop_remediation_switches.principal_id` 都是身份列。现在登记进 `tests/test_consumer_erasure_coverage_pg.py` 的 `IDENTITY_KEPT`：它们只记 owner 或规划器的服务身份，永远不会是客户。R1 交付时没有跑这个测试。

## 与设计稿的差异

- **origin 不写进效应意图的新列**：设计稿 §9.1 写的是“意图接收时从 Run 凭据继承、写入不可变列”。实现改为失败时由服务端从两项不可变事实派生：意图的 origin Run，以及哪条补救被这个 Run 消费。这样不用改意图接收函数，效果相同：派生只在服务端，工具参数不能影响它。
- **部署层开关只约束规划器**：负责人手动唤醒时不看部署层（API 进程没有 `REMEDIATION_ENABLED`），只看租户开关和控制暂停。

## 测试

| 命令 | 结果 |
|---|---|
| `pytest -n 3 tests/test_remediation_pg.py` | 16 passed（R1 的 11 个加 R2 的 5 个）：<br>- reevaluate 可以发布，governed_action 在 Python 和 SQL 两边都被拒；<br>- automatic reevaluate 唤醒等待的 plan，feed 中恰好一条 `final_failure`、`cause=self`、带 remediation_id，**效应意图数不变**；没有等待的 plan 时为 `no_plan`；<br>- plan 上限 1 时，第二次失败升级为 `chain_capped`，feed 中仍只有一条 trigger；<br>- 为该 plan 开 Run 时消费唤醒；这个 Run 的意图失败只升级（`remediation_origin`），熔断阈值 1 时规则被 held；之后的失败记为 `rule_held`，不生成补救；快照 `held_rules=1`；工作台列出被熔断的规则；负责人解除后再次解除返回 409 `not_held`；解除后重新生成；<br>- 负责人手动唤醒 manual 的 reevaluate，事件为 human、`woken`；对 escalate 类选择 wake 返回 409 `not_reevaluate` |
| 同上，**第一次运行** | 1 failed：负责人手动唤醒返回 409 `switch_closed`，因为 API 进程没有部署层开关。修正为部署层只约束服务（见上面的差异）。另外 R2 测试发现了 R1 的唯一约束错误（见上） |
| `pytest -n 3`：R0、R1、R2 与受影响的现有测试，共 23 个文件（复制件守卫、已发布迁移、引导、business-actions、目录清单、受治理入口、工作台读写、服务授权、stage 注册、观测、主机健康、**计划重评估**、接管、后台服务、出站表声明、来源过期、确认未送达、角色清单、删除覆盖） | 245 passed，1 failed（删除覆盖清单，见上面的 R1 遗漏）；补登记后 `test_consumer_erasure_coverage_pg.py`、`test_erasure_whole_database_pg.py`、`test_erasure_pg.py` 共 8 passed |
| L6 复核 R2 后（2026-10-25）：`pytest -n 2 tests/test_remediation_pg.py tests/test_function_copy_guard.py tests/test_published_migrations.py` | 68 passed：解除熔断后连续两轮 `held` 都为空，下一次失败照常生成；plan 上限造成的 `chain_capped` 按事件计数，阈值 1 时规则以 `chain_capped` 被 held |
| `vitest run apps/web/test`；`tsc --noEmit` | 32 个文件、179 passed；无错误 |
| `check_definer_search_path.py` | 0 problems |

## 没有覆盖的

- 设计稿 §16 R2 的两项端到端检查没有新写测试：
  - 重评估 Run 联系“别再联系我”的客户时派发被 NXC05 拒绝；
  - real 开关关闭时没有外部效应。

  原因是唤醒不产生任何意图（测试已断言），重评估 Run 提出的意图走的是未改动的派发路径，这些拒绝由现有的派发测试覆盖。如果 L6 认为需要，可以在真实 Pi 链路上补一个端到端测试。
- 规划器只在 domain worker 有 `remediation.plan` 授权、并且部署层打开时运行；stage 接入由调度员在组批时处理。
