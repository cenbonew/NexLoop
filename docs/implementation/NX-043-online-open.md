# NX-043 放开 online

**负责人 2026-10-21：测试阶段放开 online，D9 校准暂不作为前置。** 原话：“目前这个还在测试阶段，所以可以放开”。同时，真实外发的三层开关（租户层、enterprise.deliver、outreach.send）已经由负责人打开；stage 上没有真实用户，外部效应只落到参考企业端点这个替身。

迁移 `0309_nx043_online_open.sql`（临时编号）。

## 改动

| 位置 | 改动 |
|---|---|
| `control.nexloop_governed_start_experiment`（复制自 0290） | 只去掉 `online_not_available`。online 选用的孪生未校准时不再拒绝，结果和 `nexloop.experiment.started` 审计中带 `notice: calibration_waived` |
| `runtime.nexloop_experiment_twin_ok(tenant, registration)` | 新增：未选孪生、stage 为 online、或孪生可选用时为真 |
| `assign`（0300）、`strategy_for`（0295）、`guardrails_hit`（0295）、`guard`（0295）、`levers`（0304） | 只把孪生可选用的判断换成 `twin_ok`：online 实验不因孪生而拒绝分配、不回退对照、不触发 `twin_withdrawn` |
| `runtime.nexloop_experiment_allocate_on_inbound`（0304） | 修正：record 中 `direction` 为 `inbound` 的消息也算入站（之前只认没有 `direction` 的） |

六处重定义都登记在 `tests/test_function_copy_guard.py`（`nx043_online_open`）。

## 保留的护栏

- start 时重查目的：营销类或需同意的目的一律拒绝（renewal_reminder 现在是营销类）；
- X5：guard、预算、对账异常、拒绝和退订率；
- X4：分配排除（erasing、in_same_comparison 等）；
- 杠杆只能关、不能开；外发受理时冻结策略版本并写审计；
- screening 和 shadow 仍按 D9 要求孪生已校准；
- real 派发开关只读不写：开关关着时外发仍被 `real_dispatch` 门挡住。

## 测试

`tests/test_nx043_online_open_pg.py`：online 可以 start（含 `calibration_waived`），screening 仍要求校准；候选组走候选策略、外发审计带策略版本；guard 触发后停止、所有人回到对照组；营销类目的仍被拒；real 派发开关关着时候选组外发仍被拒。`tests/test_nx043_stage_pg.py`：X7 工具的 online 段。
