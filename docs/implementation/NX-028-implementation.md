# NX-028 负责人工作台与人工接管：实现说明（L4：切片 2、3）

设计稿 `NX-028-design.md`（D1–D10 全部定案）。分支 `nx028-s23`，从 main `036c640` 切出，带上设计稿提交。临时迁移号 0150–0159（调度员指定；设计占位 0116 → 0150，0118 → 切片 3），合并时由调度员重排。切片 1（身份与读端口）由 L2 实现。

## 切片 2：写入口

### 迁移
| 临时号 | 内容 | 依赖 |
|---|---|---|
| 0150 `nx028_governed_entry` | D9 统一入口注册表 `control.nexloop_governed_capabilities`（owner-only、append-only，handler 签名由插入触发器校验）；`authz.nexloop_goal_governed_action` 改名保留为 `..._before_registry_v0115`（无授权）+ 新包装（校验同 0111 函数体，capability → operation / 主体规则 / handler 来自注册表）；种入 13 行；派发预判 `control.nexloop_intent_dispatch_prediction` 与其无身份的快照检查 `control.nexloop_dispatch_controls_check`；人类请求审计表 `runtime.nexloop_human_requests`；手动复评与查询执行结果的 handler | 0068 控制面、0097 快照、0106 计划 feed、0109/0110/0112 联系检查、0111 承诺人类函数、0042/0065 effect outbox |
| 0151 `nx028_workbench_actions` | `authz.nexloop_workbench_action_for`：人类会话下按 capability 查本租户唯一的已发布 Action，并报告调用者是否持有该 Action 的授权（缺授权报 403 而不是 503）；不授予任何权限 | 0150 |

签名详见设计稿 §15.2a（已通知调度员转 L3）。

### HTTP 与 Python
- `nexloop_eios/workbench_actions_http.py`：`POST /api/v1/workbench/actions/{operation}`，同源、登录 Cookie、CSRF、`Idempotency-Key`（作为受治理请求 id，重放返回终态结果）。操作：`set_control`、`release_contact_restriction`、承诺五项、`request_plan_reevaluation`、`request_effect_query`；字段逐项校验，多余字段或非 UTC 时间为 422。错误码：401 / 403（含无授权）/ 404（租户未发布该 Action）/ 409 `not_allowed_in_state`（handler 拒绝该状态）/ 409 `conflict` / 422 / 503（授权或依赖暂时无法核验）。
- `WorkbenchActions`：每次请求重新做浏览器人类认证，经 `GoalGovernedActions` 提交；`http_api.py` 只追加挂载（`backend.py` 未改）。
- `GoalGovernedActions` 增加 `request_plan_reevaluation`、`request_effect_query`；`business_actions.PROFILES` 与 `business-actions.v1.json`（v6）增加两项 human_owner Action：`nexloop.plan.request_reevaluation:1`、`nexloop.service.query_request:1`（类型挂 Consumer v1）。

### 新增 Action（需切片 1 的角色映射）
- `nexloop.plan.request_reevaluation:1`、`nexloop.service.query_request:1`：建议 owner 与 operator 都持有（设计稿 §2 表）。

### 测试（Mac，`-n 3`，开跑负载约 5–7）
- `tests/test_governed_entry_pg.py` 4 例：注册表内容、append-only、错误 handler 被拒、旧入口无授权；预判与真实派发一致（暂停 → 拒绝且 provider 零请求；恢复后旧意图 `control_revision_stale` 且仍被拒；复评后的新意图可派发并派发一次；附带通知与联系限制；预判不留锁）；手动复评（服务主体持同授权被拒、计划进入 feed、重放不重复、非 active 计划被拒）；查询执行结果（排队中的意图被拒；unknown 时只把 outbox 的到期时间提前，状态、fence、意图数不变）。
- `tests/test_workbench_actions_http.py` 4 例：未登录 / CSRF 不符 / 未知操作 / 字段错误 / 多余字段 / 非 UTC / 缺 Idempotency-Key 全部拒绝且不写；工作台暂停与恢复与派发一致（AT-006 界面部分的后端）且同键重放不重复写；解除联系限制、标记沟通类、延期、已取消承诺再取消返回 409、手动复评；登录但无授权的人类 403 且不写。
- 回归 99 + 21 例通过（review_http、goal_controls、contact_refusal、承诺三组、nx022 派发、清单、db_boundary、计划复评、effect 派发、闭环知识、web chat、http_api 等）；`check_definer_search_path.py` 0 问题。
- `test_bootstrap.py` 在临时号下有 2 例预期失败（0116–0149 断档）；把 0150 临时改为 0116 后 4/4 通过（未提交）。
