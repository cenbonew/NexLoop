# ADR-040｜动作最终失败后的补救意图

状态：**采用**。负责人 2026-10-26 按推荐采纳；设计稿 §14 的 Q-1 至 Q-7 已于 2026-10-25 按推荐决定。实施任务 NX-057：切片 R0、R1、R2、R4 与 G1、G2 已实现并在 release 46 中。**R3（`governed_action`，预填目标能力）继续暂缓**：负责人 2026-10-26 按推荐，首批不设目标能力（Q-2），等出现具体的人类能力需求时再立项；它依赖的 NX-056 A4 已合入。

关联：

- 设计稿 `docs/design/remediation-intents.md` 第 4 版（§15 为本 ADR 的草案，§14 为负责人决定），以及 L6 对第 1 至第 4 版和各切片的复核；
- 实现说明：
  - `docs/implementation/NX-057-R0-effect-failures.md`
  - `docs/implementation/NX-057-R1-remediation-suggestions.md`
  - `docs/implementation/NX-057-R2-plan-wake.md`
  - `docs/implementation/NX-057-R4-claims-and-evidence.md`
  - `docs/implementation/NX-057-G1-G2-rules-publication-and-switch.md`
- 上线执行单：`docs/release/run-nx057-remediation-stage.md`；
- 迁移（main 上的编号）：0325 `effect_failures`（R0）、0337 `remediation_intents`（R1）、0338 `remediation_wake`（R2）、0345 `remediation_claims`（R4）、0351 `remediation_rule_publications`（G1）；
- 受信配置：
  - `deploy/configuration/remediation-rules.v1.json`，发布命令 `scripts/deploy/remediation_rules_apply.py`；
  - 服务授权 `service-grants.v1.json` v19 中规划器的 `nexloop.remediation.plan:1`；
  - 告警规则 v15 起的四条补救告警；
- 安全不变式：“只有受治理的 EIOS Action 可改正式业务对象”“unknown 先对账，不盲目重试”（AGENTS.md）；ADR-039（统一审批）；NX-024（计划重评估）。

## 1. 背景

负责人在 EIOS 能力差距 Q2 中决定引入补救。NexLoop 的效应只有三条路径会终结为 failed；预留类副作用在失败时已经原子退回。上游“同一适配器做逆操作”的做法，不适合 NexLoop 的 provider 协议。

## 2. 决定

1. **只为真失败生成补救意图**。条件全部满足才生成：
   - R0 失败记录中的原因码是 `origin_expired`（来源 Run 过期，而且从未派发）或 `confirmed_not_delivered`（负责人带证据确认未送达）；
   - world 是 real；
   - 有精确匹配的规则。

   unknown 先对账；一次性回复被拒、按规则的拒绝、配置缺失，以及动作认领的 permanent_failure，都不生成补救。
2. **补救的种类**：
   - `reevaluate`：规划器经 definer 包装函数唤醒原计划，触发原因为 `final_failure`，`cause='self'`。首批规则选这一种，自动执行（Q-1、Q-3）；
   - `escalate`：交给负责人；
   - `governed_action`（R3，暂缓）：工作台用冻结的参数预填**目标能力自己的请求**，由负责人本人直接提交，目标能力自己的权限检查和 NX-056 审批策略照常生效。

   不存在逆操作，不重放原意图，也没有嵌套调用处理函数的入口。在 R3 实现之前，SQL 和规则校验都拒绝 `governed_action`（`kind_unavailable`）。
3. **规划器的权限**：只持有 `nexloop.remediation.plan:1`，只调用 definer 包装函数，租户从服务身份得出；不新增直接 SQL，也不替换身份。
4. **上限与刹车**：
   - 每次失败、每条规则最多一条补救；
   - 补救 Run 的 origin 由启动器在服务端派生，带 origin 的意图再失败时只升级；
   - 按 plan 和 consumer 设链条上限；
   - 熔断按补救来源的失败计数，由负责人解除；
   - 超出速率时，每小时聚合成一次升级；
   - 两级紧急开关：部署层 `REMEDIATION_ENABLED` 默认关闭，只给 `plan-reevaluator`；租户开关由负责人暂停或恢复，都不需要审批（Q-6）。开关关闭期间的失败，以后也不补生成。
   - 默认数值写在受信配置中（Q-5）。
5. **原失败保持原样**：原意图的 failed 与失败率不因补救而改变（Q-4：认领不标 compensated）。失败率的分子和分母都按事件时间计入；补救结果单独统计（`outcomes.remediation`）。
6. **补救规则是受信配置**，精确到 Action 版本，没有规则的版本不生成补救。规则按文件原文发布，库中记录文件的 sha256 和 expected_revision，配置员不能绕过这份记录（G1）。

## 3. 后果

- 计划不再一直等待已经失败的意图；负责人能看到每一次真失败，以及它是怎样处理的。
- 面向客户的后续动作，仍然受联系约束、real 开关、预算和目标能力的审批约束。
- 代价：
  - 新增失败记录、补救意图、补救事件、熔断、发布记录等表，一份受信配置，一个规划器包装函数和一个每分钟的节拍；
  - R3 上线时，受治理入口要为 `rem.*` 请求多写一行调用回执，并依赖 NX-056。

## 4. 被否决的方案

- 上游的同适配器自动逆操作；
- 把 `compensation_mode` 写在 Action 定义上；
- 补救成功后，把原失败从失败率中扣除；
- 允许补救的补救；
- 补救能力在自己的处理函数中嵌套调用目标能力的处理函数（设计稿第 1 版的 `remediation.execute`）：会绕过请求人对目标能力的权限检查，以及目标能力自己的审批策略；
- 以 `outcome_classes` 的 defect 类别作为生成条件：会漏掉曾被拒绝、后来才失败的真失败；
- 用认领摘要或 NX-056 审批摘要关联预填：前者含 request_id，后者含策略版本，都对不上；
- 依赖 NX-024 的自触发节流作为上限：它只在全部 trigger 都是 self 时生效，而且只是延后；
- 拒绝之后由 HTTP 层在第二个事务里写回执：回执依据的是调用方的转述，进程崩溃时会丢失；
- 不记录被拒的提交：补救一栏看不到原因，熔断也无法统计。
