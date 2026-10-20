# NX-041 S1：world 统一校验（实现说明，2026-10-20）

依据：NX-041 设计稿（`l5/nx041-design` f21c0220）D1、D9、§2.1、§11 S1。分支 `l2/nx041-s1`，起点 `l2/nx040-f4`（f05ad22c）。迁移编号 0259：F4 已经用掉 0257 和 0258。

## 规则

- world 只能是以下四种之一：`real`、`test`、`sim:<uuid>`、`shadow:<uuid>`。uuid 为小写的规范格式；除 real、test 外，不允许任何保留名（D1）。
- `simulation` 不再是 world，只保留为成本和 Claim 的**数据模式**（D9）。
- 统一函数：
  - SQL：`runtime.nexloop_world_valid`、`runtime.nexloop_world_kind`（real / test / simulation / shadow）、`runtime.nexloop_world_data_mode`（real、sim:* 和 shadow:* 映射为 simulation、test）、`runtime.nexloop_assert_world`、`runtime.nexloop_worlds_valid`。都是纯函数，授予 public，因为 CHECK 约束和以调用者权限执行的投影都会用到它们。
  - Python：`nexloop_eios/worlds.py`（`valid_world`、`world_kind`、`data_mode`），规则与 SQL 一致。

## 现有 world 检查清单（改造前 → 改造后）

| 位置 | 原写法 | 处理 |
|---|---|---|
| 0234 `runtime.nexloop_semantic_accesses` | `check(world in ('real','test','simulation'))` | 改为 `check(runtime.nexloop_world_valid(world))` |
| 0067 `ontology.nexloop_recall_tenant` | `p_world ~ '^[A-Za-z][A-Za-z0-9_.-]{0,63}$'`（任意名字都放行） | 改为调用统一校验；search_path 补上 pg_temp |
| 0165 `authz.nexloop_service_credentials` | 只检查“real 必须单独出现”，worlds 的取值本身不检查 | 新增约束：每个 world 都必须合法（原有的 real 单独约束保留） |
| 0120 `runtime.nexloop_cost_data_mode` | `p_world in ('simulation','shadow')` 映射为 simulation，把字面值当 world | 改为调用 `runtime.nexloop_world_data_mode` |
| 0155 `authz.nexloop_claim_contract` 的 mode | 同上 | 同上，只改 mode 表达式 |
| 0131 `authz.nexloop_metrics_export` | 遍历的 world 列表写死为 real、test、simulation | 改为 real、test；登记过的 sim 世界由 S2 加入 |
| `runtime_activation._command`（Python） | mode 属于 {real, simulation, shadow, test}，并且 `(mode=='real')==(world=='real')` | 改为 world 必须合法，且其种类等于 mode |
| `runtime_worker`、`runtime_guard_worker` 的 `--world` | 只允许 real、test | 不变（有意比统一规则更窄，AT-046） |
| 0237、0248 评估凭据 | `worlds=array['test']` | 不变（这是专门的约束，不是 world 格式检查） |
| 各表上的 `check(world='real')`（21 个迁移） | 只允许 real | 不变（这是业务约束） |
| Agent Host `runtime-adapter.ts` | 只检查 mode 的取值，不检查 world_id | 不变，S1 没有改 Host；Run 命令的 world 已由 Python 侧检查 |

real 和 test 的行为完全不变：两者的数据模式、召回入口、指标导出、Run 命令校验结果都与改造前相同。只有过去被当作 world 使用的 `simulation`、`shadow` 字面值和其他任意名字，现在会被拒绝。

## 测试

- 新增 `tests/test_nx041_world_check_pg.py`（5 项），全部是负例：
  - 16 个非法值：包括 `simulation`、`shadow`、`sim:` 后面不是 uuid、大写的 uuid、`Real`、`staging`、带空格的值等；
  - Python 与 SQL 两边规则一致；real 和 test 的数据模式不变；
  - 服务凭据、语义访问记录、召回入口三处都拒绝 `simulation` 和格式错误的 world；
  - 指标导出只遍历 real 和 test；
  - Run 命令的 world 必须合法，并与 mode 一致。
- 改动的已有测试：凡是用字面值 `'simulation'` 当 world 的，统一改为 `tests/support/tenants.SIM_WORLD`（`sim:<uuid>`）。涉及 `authority_fact_cache`、`authority_request_memo(_jobs)`、`batch_authorities`、`identity_fact_memo`、`candidate_merge_pg`、`claim_store_pg`、`recall_pg`、`observability_pg`。测试“模式”的用例（如 Claim 契约中的 mode）没有动。
- AT-064 白名单加入 `_nx041_world_check`：它只是照抄 0155 的 Claim 契约投影，改了 mode 表达式，不读取也不投影任何 Claim 值。

## 已知的无关失败

`tests/test_outreach_runs_pg.py::test_a_plan_run_asks_inside_its_run_and_the_closed_switch_refuses_at_dispatch` 依赖当前时间：在夜间免打扰时段（测试时是 23:21），它得到的是 `quiet_hours`，而不是预期的 `real_dispatch_closed`。在基点 f05ad22c 上跑，结果相同，与 S1 无关。

## 未接入（审查后记录，另行安排）

- 契约 `packages/contracts/claim.schema.json` 的 `world_id` 只要求非空字符串，`simulation` 这类非法 world 仍能通过契约校验。收紧 pattern 会变更契约，这次不做（测试数据已改用 `SIM_WORLD`）。
- Agent Host `runtime-adapter.ts` 的 world_id 校验：已派给 L3 单独处理。
