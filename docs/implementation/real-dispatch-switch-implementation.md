# 真实派发开关：设计与实现说明（NX-037 前置）

分支 `real-dispatch-switch`，从 `dispatch/integration-s4z` `fa3c153` 切出。迁移 `0220_real_dispatch_switch.sql`（临时号）。依据调度员 2026-10-11 的任务与两次裁定：采用 (B)、分类 fail-closed；`data_mode=test` 视为关闭；粒度按（租户，action_type）。

## 1. 设计

### 1.1 语义

**什么算外部效应**：由 NexLoop 之外的系统执行的效应，例如第三方渠道、企业外部服务端点、真实优惠渠道。只有外部效应受开关约束。原生 WebChat 回复（我们自己的渠道）和回环 / 本地账本不受约束。

**三层开关**：外部效应在派发前要求三层同时打开，任何一层关闭都在派发前拒绝，拒绝码为 `NXC07 real_dispatch_closed`，provider 零请求。

| 层 | 来源 | 打开的条件 |
|---|---|---|
| 部署级总闸 | 派发进程自己的环境变量 `REAL_DISPATCH_ENABLED` | 值恰好为 `true`；未设、为空或其他任何值都是关闭 |
| 租户级 | `control.nexloop_real_dispatch_switches`（scope `tenant`） | 有记录、`enabled` 为真，并且 `data_mode='real'` |
| Action 级 | 同一张表（scope `action_type`，ref 为 resource_id，例如 `eios:action:nexloop.service.request:1`） | 有记录且 `enabled` 为真 |

- `world` 不是 `real` 时一律关闭。
- 没有任何记录就是关闭。
- **租户的 data_mode**：现有代码里没有租户级的 data_mode。租户开关记录自带 `data_mode`（`real`/`test`），由 owner 打开时声明；为 `test` 时一律视为关闭。

### 1.2 外部效应的判定（fail-closed）

- **判定依据**：0112 的效应类别声明（`deploy/configuration/action-effect-categories.v*.json`）新增可选字段 `external_effect`。它与类别由同一个受信配置入口写入（`nexloop_configurator`），按已发布定义的 digest 冻结，存放在旁表 `control.nexloop_action_effect_externality`（只追加）。
- **默认按外部处理**：没有声明、定义已改变（digest 不同），或者字段值不是布尔值（Python 校验和 SQL 都会拒绝），都按外部处理。只有对“当前这份定义”显式声明为 `false` 的 Action 才不受开关约束。
- **当前每个类别的取值**（`action-effect-categories.v2.json`）：

| Action | 类别 | external_effect | 说明 |
|---|---|---|---|
| `nexloop.service.request:1` | customer_contact | **false** | 原生 WebChat 回复 / 外发通道（NX-047），也是 effect 路径唯一的 Action |
| `nexloop.incentive.issue:1` | non_contact_service | **false** | AT-043 的回环账本（D9：唯一的 provider）；联系限制仍由优惠准入自己判断（0145） |
| 其他任何 Action，或以上两者的任何新定义 | — | 未声明，即外部 | 例如将来的第三方渠道、真实优惠渠道 |

- 将来接入真实优惠渠道时，会是一份新的定义；它默认就是外部，必须三层都打开才能派发。
- **测试中的声明**：生产环境由配置人员按租户应用这份声明；测试夹具直接发布这两个 Action。测试集群（`tests/support/loopback_effects.py`，只在测试的 PG 夹具里安装，不在任何迁移里）会对这两个 Action 的每个新定义自动登记 `external_effect=false`，相当于受信配置。它不打开任何开关。开关测试会删除这条声明，使效应重新成为外部效应。

### 1.3 变更：只能由 owner 经受治理入口执行

- **入口**：0124 注册表新增能力 `real_dispatch.set`，操作为 `set_real_dispatch`，subject 只能是 human。
  - Action `nexloop.real_dispatch.set:1`（business-actions v16）只授予 owner（workbench-roles v9，并列入 owner-only 校验集）；
  - 处理函数还会再核对当前的工作台角色必须是 owner；
  - 工作台写入口是 `POST /api/v1/workbench/actions/set_real_dispatch`。
- **版本**：每次变更都是一个控制事件（`event_kind='real_dispatch'`），控制版本号加一。开关行记录这个版本号、原因、变更人和意图 ID。
- **旧快照失效**：派发时，在此之前取得的快照失效，拒绝码为 `NXC02`。
  - 租户级变更是 `tenant` 作用域的事件，0068 原有的检查已经会让该租户的所有快照失效；
  - Action 级变更会让涉及该 Action 的快照失效（0220 的 `control.nexloop_real_dispatch_superseded`）。
- **非 real world**：打开开关会被拒绝。

### 1.4 派发时复核（SQL）

- **effect 路径**：`authz.nexloop_assert_intent_dispatch_controls` 被重命名保留为 `…_before_real_dispatch_v0220`，外面再加一层包装：先执行原有全部检查，再判断外部性、快照是否失效、三层开关。
  - 这一步与准入在同一个事务里；在控制头上加共享锁读取，所以并发的变更要么被看到，要么排在其后。
  - Worker 在同一个事务里把部署级总闸写入事务级设置 `nexloop.real_dispatch_enabled`（`nexloop_eios/real_dispatch.py`）。
  - 被拒的派发经 NX-030 D8 的路径记入 `runtime.nexloop_dispatch_refusals`（代码 `NXC07`）。
- **优惠路径**：`authz.nexloop_incentive_command` 重命名保留并加包装。对 accepted 意图，在 admit 时判断外部性、快照是否失效、三层开关；任一不满足就把意图标为终态 `refused`（`refusal_code='real_dispatch_closed'`）并写审计事件，回环账本零请求。

### 1.5 在途意图

- **effect 路径**：关闭后，已经入队的意图在派发时被拒（快照早于变更时为 NXC02，否则为 NXC07），provider 零请求。
  - 意图本身保持未派发，不是终态：Worker 每次领取时都会再被拒，拒绝表在 10 分钟内只记一条，因此不会被重发。
  - 重新打开后，只有在开关变更之后取得快照的新工作才能派发；旧意图仍会因快照失效而被拒。
- **优惠路径**：直接变为终态 refused。

### 1.6 与服务授权清单的关系

两道防线彼此独立，开关不会授予任何东西。

- **服务授权清单**（`deploy/authorization/service-grants.v1.json`）：决定哪些服务主体可以执行哪些 Action。其排除规则（ADR-020 §3）保持不变：真实外发 Action 不授予任何服务；`REAL_DISPATCH_ENABLED` 仍列在排除项里。
- **开关**：只决定一个已经被授权、并已受理的外部效应能否在派发时离开 NexLoop。
- **两者的关系**：
  - 要派发一个真实外发 Action，必须先有授权（受信配置；对被排除的 Action，需要另行由负责人决定并修改排除清单），再有三层开关；
  - 只打开开关，不会让被排除的 Action 获得授权；
  - 只有授权，开关关闭时也会被拒。

### 1.7 状态报告（只报状态和计数，不带细节）

- **就绪检查**（`/health/ready`，stage 模式）：响应增加 `real_dispatch: {tenants_open, actions_open}`，与已有的 `dispatch_switch`（部署级总闸）并列。这些只是报告，不作为就绪条件。
- **doctor**：`nexloop-doctor --real-dispatch --metrics-database-url-file <私有 DSN>` 报告本进程环境中的总闸状态，以及打开的租户数和 Action 数，通过只读的 metrics 角色读取。总闸值不合法或状态读不到时失败；开关关闭不算失败。
- **工作台**：只读展示。M12 快照（`runtime.nexloop_metrics_snapshot`）增加 `real_dispatch` 块：租户开关（enabled、data_mode、revision）和各 Action 开关（action_type、enabled、revision），不含原因和变更人。工作台的 metrics 视图和只读 metrics 导出都会显示它。
- **前端页面不在本次范围**：数据已经通过工作台 metrics 读接口提供，apps/web 的展示另行排期。

### 1.8 开关只在测试里打开

- 迁移、配置示例和代码都不打开任何开关。`.env.example` 中的 `REAL_DISPATCH_ENABLED=` 保持为空，即关闭；stage Compose 示例保持 `"false"`。
- 只有 `tests/test_real_dispatch_switch_pg.py` 在测试里打开开关。

## 2. 文件

- **迁移**：`0220_real_dispatch_switch.sql`，`catalog.json` 已同步，`versions.lock.json` 的 `bootstrap_revision` 为 `0220`。
- **Python**：
  - `real_dispatch.py`（部署层）；
  - `effect_execution.py` 和 `plan_reevaluation._SignedPort` / `incentives.IncentivePort`：在准入事务里写入部署级总闸；
  - `goal_controls.py`：`set_real_dispatch`、`CAPABILITIES`，以及 `REASONS` 增加 `NXC07`；
  - `workbench_actions_http.py`（操作表）；
  - `business_actions.py` / `workbench_roles.py`（owner 专属）；
  - `effect_categories.py`（可选的 `external_effect`）；
  - `deployment_readiness.py`、`http_api.py`、`doctor.py`（状态报告）。
- **配置**：
  - `business-actions.v1.json` v16；
  - `workbench-roles.v1.json` v9；
  - 新增 `action-effect-categories.v2.json`（v1 保留不变）。
- **测试**：
  - 新增 `tests/test_real_dispatch_switch_pg.py` 和 `tests/support/loopback_effects.py`；
  - `tests/conftest.py`：测试 PG 夹具里安装回环声明；
  - 同步修改 `test_business_actions.py`、`test_governed_entry_pg.py`、`test_restore_fence_pg.py`（检查对象改为被保留的函数）、`test_api_stage_ready_pg.py`（响应体增加计数）、`commercial_fixture.py`（注册表 Action）。

## 3. 测试（`tests/test_real_dispatch_switch_pg.py`，7 个）

- **端到端**：真实受治理的 effect 执行器、独立的 effect Worker、回环 provider。
  - 类别已声明但没有 `external_effect`，按外部处理；
  - owner 经工作台 HTTP 打开租户和 Action 开关；
  - 总闸关闭时被拒（NXC07 有记录，provider 零请求）；三层都打开后派发一次；
  - owner 关闭 Action 后，在途意图被拒（NXC02，快照失效），没有重发；关闭后的新工作由 Action 层拒绝。
- **三层矩阵**（SQL）：
  - 无记录、总闸关闭（包括 `TRUE`、`1`、`yes`、空串）、world 为 test 或 simulation、租户 data_mode 为 test、租户关闭，都按关闭处理；
  - 只打开 A 租户的 `service.request` 时，同租户的优惠 Action 和 B 租户仍被拒。
- **外部性**：默认是外部；只有对当前定义显式声明 false 时不是；定义改变后重新视为外部；表是只追加的。
- **受信配置**：v2 清单写入 `external_effect=false`；非法值被 Python 校验和 SQL 同时拒绝。
- **owner 专属**：
  - operator 403；租户作用域缺少 data_mode 时 409；action_type 不合法时 422；
  - 每次变更版本号加一，并有一个 `real_dispatch` 控制事件；
  - 工作台快照显示开关；
  - 应用角色没有表权限，也不能执行处理函数。
- **优惠路径**：只打开 `service.request` 时，外部优惠被拒为终态 refused，账本零请求；打开优惠 Action 后发放一次；被拒的意图不会重发。
- **状态报告**：就绪检查和 doctor 只报计数（data_mode 为 test 的租户不计入）；总闸值不合法时 doctor 失败；doctor 只能用 metrics 角色。

## 4. 已知事项

- **测试回环声明**：在测试 PG 夹具里通过事件触发器统一完成，而不是在十几个夹具里逐个声明。这只影响测试集群。
- **部署级总闸以事务级设置传入 SQL**：数据库无法自己读取进程的环境变量，所以由派发进程在准入事务里写入。能调用准入函数的服务角色可以自行设置它，这与“总闸是派发进程的部署配置”是同一个信任边界；租户层和 Action 层完全由数据库判断。
- **前端展示另行排期**：工作台的前端展示（apps/web）不在本次范围。
