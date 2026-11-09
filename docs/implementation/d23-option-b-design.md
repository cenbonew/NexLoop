# D23 方案 B：给不限参数的 Action 补真实的输入模式（设计）

- **状态**：v2 草稿（v1 `a0d67fa2` 由 L6 判条件 PASS，本版写入两处事实更正和 C1–C5），只做设计，不写实现。作者：L4。基线：main `8f5e3511`，加上 L7 的 `9a2526d3`（方案 A 与本项的 planning 草稿）。待 L6 审。
- **依据**：`docs/implementation/d23-open-input-schemas.md`（方案 A 定稿，负责人 2026-11-09）、`docs/planning-drafts/d23-option-b-input-schemas-20261109.md`（L7 草稿）。
- **范围**：给绑定 Consumer、`input_schema = {}` 的 Action 补上与运行时校验一致的输入模式，并以新版本发布。本文只给清单、版本影响、分批与验收、工作量和风险。

## 1. 清单

用 `catalog_publish.compile_catalog('tenant-synthetic', **stage_secrets.CATALOG)` 在本机编译：绑定 Consumer 的 Action 有 **97 个，其中 90 个的 `input_schema` 是 `{}`**。stage 多一个 stage 专用的 `nexloop.synthetic_fault.set`，所以是 98 / 91，和 L7 的数一致。

| 组 | 来源 | 开放 | 已有约束 | 参数的真实校验在哪里（可导出的事实来源） |
|---|---|---|---|---|
| **A. 人工治理操作**（工作台） | `business-actions.v1.json`，capability 是 `nexloop.*`；由 `goal_controls.GoalGovernedActions` 的各个方法经 `_submit` 提交 | **80** | 0 | 三层，按顺序：① `workbench_actions_http.OPERATIONS`，85 个操作，每个都有**精确的字段集合**（`set(body) != set(spec)` 就拒绝）和逐字段检查（正则、整数范围、枚举）；② `GoalGovernedActions.<方法>` 的参数检查，并构造请求体 `{'request_id', 'operation', …}`；③ 注册表的 SQL handler（0124 及之后），按键和取值再查一遍。**①② 可以直接导出成 JSON Schema**：字段集合确定，大多数检查是正则或范围 |
| 其中 `workbench_member.*` | 同上（NX-058） | 17 | 0 | 同上；`issue_owner_reset` 和 `invite_owner` 的 `code_hmac`、`token_hmac`、`issuer_source_hmac` 是执行期字段（`EXECUTION_FIELDS`），**不在**送给模式校验的 `claimed` 请求体里，模式里不能出现它们 |
| **B. 服务对象写入** | `Consumer.edit`（business，service authority） | 1 | 0 | `object_edits.GovernedObjectEditor`：请求体 `{request_id, type_name, properties, object_id, expected_revision}`，`properties` 由 `validate_object_properties` 按对象类型校验。模式只能约束外层结构，`properties` 必须保持开放，否则每次 Schema 演进都要改 Action |
| **C. 系统目录构建** | `system-actions.v1.json`，`input` 为 null | 9 | 7 个已有（`nexloop.context.bind`、`service.request` 等由 builder 生成） | `Budget.set`、`Contact.release`、`Control.set`、`Goal.propose`、`Goal.publish`、`Metric.approve`：`goal_controls` 里同样有方法校验和 `goal_payload`，可以导出。`Consumer.create`：同 B，外层固定、`properties` 开放。`NexLoop.queue.operations`、`nexloop.service.query`：请求体由服务端代码构造，调用方是服务，导出前要逐个核对构造点 |
| **D. stage 专用** | stage 清单 | 1 | 0 | `nexloop.synthetic_fault.set`（ADR-045）：`goal_controls.set_synthetic_fault` 加 `OPERATIONS` 的字段检查 |

**结论**：91 个都能在代码里找到确定的字段集合。A 组 80 个、C 组 6 个和 D 组 1 个都有两层 Python 校验，可以按规则导出；B 和 `Consumer.create` 只约束外层；`NexLoop.queue.operations` 和 `nexloop.service.query` 要人工核对。

**导出规则**（保证模式**等于或宽于**运行时校验，不比它严）：

- 顶层是 `type: object`、`additionalProperties: false`、`required` 为全部字段（与 `OPERATIONS` 的精确字段集合一致），再加 `request_id` 和 `operation`（`operation` 用 `const`）。
- 字段检查只导出**能一一对应**的部分：`_id(re.compile(...))` 导出为 `pattern`（加上 `^…$`），整数范围导出为 `minimum` / `maximum`，枚举导出为 `enum`，长度导出为 `maxLength`。lambda 里其他的语义（例如“版本号必须是下一个”、“SQL 里再查”）**不导出**，那一项只写 `type`。
- 导出由一个生成器完成，产出 JSON 写进清单；另有测试拿生成结果和 `OPERATIONS` 逐项比对，不靠手写。
- 正则方言：Python `re` 和 JSON Schema（ECMA-262）有差别。生成器只接受两边语义相同的子集（字符类、量词、锚点、分组），遇到其他写法就报错，留给人工处理，不猜。
- **不导出 `format`（C1）**：派发时的校验开着 format 校验（见 §2 第一行），`schema_gates` 的回放没有开。导出 `format` 会让两边判定不一致，而且可能比 Python 检查更严。需要格式约束的字段一律用 `pattern` 表达。
- **锚点与换行（C1）**：正则导出为 `^…$`。ECMA 的 `$` 只匹配字符串末尾；运行时用的是 Python 的 `re.fullmatch`，也不接受末尾换行。但 JSON Schema 的 `pattern` 在 Python 的 `jsonschema` 里是用 `re.search` 实现的，Python 的 `$` 会接受末尾多一个 `\n`，所以导出的模式会接受 `"abc\n"`，而运行时的 `fullmatch` 拒绝它。**这是有意宽于运行时**：宁可模式放过、由运行时拒绝，也不让模式拒绝运行时接受的请求。比对测试里专门有一条用例，确认这类值确实是“模式接受、运行时拒绝”。

## 2. 版本

补 `input_schema` 等于改定义。已发布的 Action 不可变，所以每个都要发**新版本**（`version + 1`，`previous_version` 指向旧版本）。

| 受影响的部分 | 影响 | 怎么处理 |
|---|---|---|
| **派发时的输入校验**（`action_governor.py` 48–61 行） | **不只影响闸门**：governor 在派发每个请求时，就用已发布版本的 `input_schema` 校验（`Draft202012Validator`，开着 `FORMAT_CHECKER`），不过就报 `published Action input rejected`。所以新版本一旦发布，过严的模式**当场**就会拒掉真实请求，不是等到下一次 Schema 变更才暴露 | 这就是 §3 的影子校验和 C3 门槛必须在新版本发布**之前**满足的原因；生成规则只导出一一对应的约束，并且不导出 format（C1） |
| **工作台取 Action**（`authz.nexloop_workbench_action_for`，0125） | 它要求同一个 capability **只有一个 active 定义**，有两个就报 `published more than once` | **确实不支持**：`catalog_publish.publish` 只发布缺少的定义，不会把旧版本设为 inactive。所以**第 0 批需要补一个迁移**：可信配置发布 `previous_version` 指向旧版本的新定义时，在同一事务里把旧版本设为 inactive，并且只允许这样一对一地替换。否则新版本一发布，工作台就报 `published more than once` |
| **人工授权**（`workbench-roles.v1.json`，`workbench_roles.py`） | 角色里的资源写的是精确版本（`eios:action:nexloop.real_dispatch.set:1` 等），`action_for` 也按精确版本查 grant | 同一批里把角色清单改为 `:2`，`manifest_version` 加 1，与新版本同一次 register 应用；`owner_only` 集合同步改。漏一个，对应的工作台操作就是 403 |
| **服务授权**（`service-grants`，`follow_latest_version`） | **不会跟随**（第 0 批实现时核实）：`service_grants.resolve_follow` 只跟随人工审核决定发布的、纯 Schema 重绑定的后继版本；改了 `input_schema` 的新版本不是重绑定，会被列为 refused、链在这里停住 | 每个被替换的、有服务授权的 Action（例如 `claim_matcher → Consumer.edit`），都要在 service-grants 里**显式**加上新版本的授权，与替换同一次 register；`tests/test_d23_input_schemas.py` 有用例 |
| **Capability 快照** | 不变：`capability_binding` 的 `schema_hash` 是 capability 的，不含 Action 的 input_schema | 无 |
| **publish / register 计数** | `test_stage_register_pg` 的 `(20, N, 2)` 按 Action 定义数计；新版本会让定义数增加（旧版本仍在表里，只是 inactive） | 每批同步改计数，见 memory 中 “new-action-register-count” 的做法 |
| **已有 effect intent、receipt、governed intent** | 都指向旧版本的 resource id，保留不动，查询和对账照常 | 无 |
| **跨越版本切换的重试** | 同一个 `request_id` 在切换前用 `:1` 提交过，切换后重试会用 `:2`：claim 的 key 是（租户、stable_name、幂等键），binding 里的 action_reference 不同，会被当作冲突拒绝。**fail closed，不会执行两次**；代价是那次重试报错 | 在没有在途人工操作的窗口部署；部署单列出“切换后 24 小时内这类拒绝的计数”作为核对项 |
| **r8 / 方案 A** | Schema 变更时，后继 Action 原样复制 input_schema，父、候选仍然逐字节相同，A 的语义不变。但模式不再开放，就不再走 `open_input` 豁免，而是由 `tool_cases` 按约束生成合法和非法用例来回放，这正是 D23 原本的要求 | **A 和 B 不冲突**：B 做完一个，`tools:open_input:<N>` 的 N 就减一；全部做完后，N 只剩确实不收参数的 Action（如果有，要写明理由） |

## 2a. 停用旧版本后受影响的读取方（C2）

凡是按 stable_name 取 active 版本、或按精确版本引用 Action 的地方，都要在第 0 批核查，并各有一条测试。

| 读取方 | 切换后会怎样 | 处理与测试 |
|---|---|---|
| `authz.nexloop_workbench_action_for`（0125） | 返回新版本（只剩一个 active）；按新版本的精确 resource id 查 grant | 角色清单同步改为新版本。测试：发布 `:2` 后 `action_for` 返回 `:2` 且 `granted`；角色没改时返回 `granted: false`（403），不报 `published more than once` |
| service-grants 的 `follow_latest_version`（register 时解析） | register 解析到新的 active 版本，授予新版本、撤销旧版本 | 测试：发布 `:2` 后 register 的 dry-run 列出“新增 `:2`、撤销 `:1`”，不会两个都留着或都没有 |
| 0183 effect 版本精确匹配（`control.nexloop_effect_actions` 白名单按精确 resource id 比对） | 这 91 个都**不在** effect 白名单里：effect Action（`nexloop.service.request`、`enterprise.deliver` 等）本来就有输入模式，不在本项范围内。所以切换不影响 0183 | 测试：第 0 批断言这 91 个都不在 effect 白名单中；如果以后某个 effect Action 也要换版本，白名单行必须同一次更新（另开一项） |
| Pi 侧工具清单（`apps/agent-host/src/runtime-effect-tools.ts`） | Host 的工具定义是代码里写死的，不由 `input_schema` 生成。**但其中两个在这 91 个之内**（第 0 批实现时发现）：`nexloop.incentive.issue` 和 `nexloop.outreach.send`，它们是 Run 工具，也是模拟世界的替身工具。Run 的凭据和 Role 上限写的是精确版本 `:1`（Host 注释和 SQL 各处共 25 处引用） | 这两个放在第 3 批，同一批把 Run 凭据、Role 上限和 SQL 里的 `:1` 引用一起改到新版本；模型看到的工具参数仍由 Host 定义，不变。测试断言 Host 引用的开放 Action 恰好是这两个 |
| Capability 绑定（`capability_binding`） | 新版本绑定同一个 capability 快照（`schema_hash` 不变） | 测试：新旧版本的 `capability_binding` 逐字节相同 |
| **代码里写死版本号的调用方** | 写死 `:1` 的调用在旧版本停用后会被拒（definition reader 读不到 active 的 `:1`）。在这 91 个里，本机 grep 找到：`business_setup.py:71`（`Consumer.create`，`action_version=1`）、`scripts/nx037/real_tenant_init.py:113`（`nexloop.budget.loosen`，`action_version: 1`）、`scripts/perf/test_micro_authz.py`（`Consumer.create`），以及大量测试 fixture | 写死版本的生产代码改为“取当前 active 版本”，与对应批次一起改；测试 fixture 自己发布定义，不受 stage 切换影响，但第 0 批要 grep 出所有引用并列成清单 |

**可信配置清单只能列最新版本**（第 0 批实现时发现）：`control.nexloop_configure_manifest` 要求清单里的每个 Action 在库里都是 active 的，所以旧版本被替换后，catalog 清单只能列新版本；否则重跑同一份清单就报 `configuration conflict`。第 1 批起，catalog 编译要改为每个 stable_name 只输出最新版本。

**`previous_version` 是一个精确引用**（stable_name、version、contract_digest），不是整数。迁移按它核对上一版的摘要。

## 3. 分批

每批都是“一组 Action 的新版本，加上对应的角色、计数和测试”，单独送 L6。

| 批 | 内容 | 个数 | 理由 |
|---|---|---|---|
| 0a | **前置**：补迁移，让可信配置发布新版本时在同一事务里停用旧版本（见 §2）；写导出生成器和比对测试；核查 §2a 列的全部读取方 | — | 后面所有批次都依赖它 |
| 0b | **影子校验**（见下），以及 **catalog 编译每个 Action 只输出最新版本**（并带上它所替换的上一版本的精确引用）。0b 交付并上 stage 之后，第 0 批才算完成 | — | 第 1 批发布前必须完成 |
| 1 | C 组中有 `goal_controls` 方法的 6 个（Budget、Contact、Control、Goal.propose、Goal.publish、Metric），加上 D 组 1 个 | 7 | 字段少、调用路径单一，用来跑通流程 |
| 2 | A 组里只读或可逆的操作：`alert.silence`、`experiment.report`、`approvals.*`、`object_set` / `object_view` / `semantic_release` 等 | 约 25 | 风险低 |
| 3a | A 组里有外部或业务效果、但**不涉及 Run 工具**的操作：`outreach.approve`、`real_dispatch.set`、`consumer.erase*`、`message.*` 等 | 约 36 | 一次拒错就是业务中断，放在影子校验积累了数据之后 |
| 3b | **`nexloop.incentive.issue` 和 `nexloop.outreach.send`**：它们同时是 Pi 的 Run 工具（§2a）。和这两个 Action 一起改：SQL 与 Host 里写死的 25 处 `:1`、Run 凭据和 Role 上限的版本，以及 Host 的部署 | 2 | **独立的回滚方案**：回滚 Host 镜像、Run 凭据和 Role 上限，并重新启用旧版本；新旧两边要能互相切换。**重新判断是否要进 0183 的 effect 白名单**（它们的 Run 路径在 SQL 里受理，和 effect Action 相似）。影子校验看不到它们的 Run 路径（§5.1），要在 SQL 受理路径上加同样的计数 |
| 4 | `workbench_member.*`（含执行期字段的两个） | 17 | 要与 NX-058 的审批策略一起核对 |
| 5 | B 组与 `Consumer.create`（外层模式，`properties` 开放）、`NexLoop.queue.operations`、`nexloop.service.query` | 4 | 需要人工核对服务端构造点；`Consumer.*` 还会影响 claim matcher 的改绑（同 Consumer v2 那次的窗口问题） |

**每批固定的一步**：在 `service-grants` 里**显式**加上新版本的授权、撤销旧版本的授权（follow_latest 不会跟随，见 §2），与替换在同一次 register 里应用；工作台角色清单也同步改为新版本。

**每批的验收**：

1. **生成器比对**：模式的字段集合和 `OPERATIONS` / 方法构造的请求体逐项相同；每个导出的约束都能在源码里找到对应的检查。
2. **不比运行时更严**：
   - **测试语料回放（在 CI 上执行，C5）**：在 CI 主机上，以影子模式跑一次全量 pytest。governor 对每个请求额外用候选模式校验，只记录、不拒绝；候选模式拒绝了而当前路径接受的请求数必须为 0。本机不跑全套（负载约定），只跑本批 Action 相关的测试文件。这一批里任何一个 Action 在语料里没有出现过，就要先补一个成功路径用例。
   - **stage 影子观察（C3）**：新模式先以影子方式部署 7 天，**按 Action 分别统计**调用次数和“候选会拒绝”的次数（只计数，不记请求体）。每个 Action 都要**至少有 1 次真实调用，并且“候选会拒绝”为 0**，才能发布新版本。7 天里没被调用过的记为 `no_data`，**不能发布**：要么补一次 stage 合成调用（用合成租户或 persona 非 real 的数据，见第 4 节负责人决定第 4 项），要么延长观察期。
   - 说明：库里没有保存请求体（`runtime.nexloop_action_claims` 只存 claim 和摘要），所以“回放已有 intent”只能用测试语料加 stage 影子观察替代，不能从库里取回真实请求。
3. **能拒绝**：每个新模式至少拒绝一个 `schema_gates.OPEN_PROBES` 探测值，`schema_gates.open_input` 对它返回 False。
4. **r8**：在 catalog 编译出的 Consumer Action 上，`tool_cases_built.open_input` 降为上一批的值减去本批个数；回放 0 失败；收紧候选的阳性对照仍判为回归。
5. **部署后**：工作台每个操作抽一次成功调用；跨版本重试的拒绝计数如实记录。
6. **影子报告逐个对照（L6 C2）**：验收时把本批的 Action 清单和 `authz.nexloop_action_input_shadow_report` 逐个对照。报告只列出已经配了候选模式的 Action，清单里有、报告里没有的 Action 一律按 `no_data` 处理，不能发布。

**“带 0430/0431 那次部署”的前提**（这两个迁移不进 R73，R73 的部署单只覆盖到 0429，放到下一次部署）：

- **L6 C1**：部署前在 stage 上只读查询一次，确认 world `real` 里没有哪个 `stable_name` 同时有两个或更多启用的定义（`select tenant_id, definition->>'stable_name', count(*) from control.nexloop_action_definitions where world='real' and active group by 1,2 having count(*)>1` 必须为空）。有的话，替换触发器会把它们按“两个启用”拒绝，要先由人处理。
- **L6 C3**：`shadow_validate` 会让每次治理派发多开一次连接、多一个事务。第 1 批上 stage 之前，量一次治理派发的 p95，和没有影子校验时对比；明显变慢的话，改为“这个租户配了候选模式才去查”。

## 3a. Context 体积（C4）

- **模型看不到这些模式**：Context 组装不带 Action 的 `input_schema`，`nexloop_eios/context_engine` 和各个 context 包函数里都没有引用它；Pi 侧的工具定义是 Host 代码里写死的（§2a）。91 个 Action 里只有 `nexloop.incentive.issue` 和 `nexloop.outreach.send` 是模型工具（第 3b 批），它们的工具参数也是 Host 定义的，不由 Action 的 `input_schema` 生成；其余都是人工治理操作或服务写入。所以补模式**不会让模型看到的工具定义变大**，对真实 Context 的体积影响为 0。
- **r8 的 `context_volume`**：`s6_stage_schema.py` 里写死为父、候选各 1000，表示“Context 体积不变”。补模式之后这个判断仍然成立，**不需要改**。
- **前提**：以后如果 Context 开始携带 Action 的输入模式（例如给模型展示可用的人工操作），这个数就要改为实测，届时另行评估。第 0 批加一条测试，断言 Context 包里不含任何 Action 的 `input_schema`，这样前提一旦变化，测试会提醒。

## 4. 工作量、风险与决定

- **工作量**：
  - 第 0 批约 2–2.5 人日（停用旧版本的迁移 0.5，生成器和比对测试，影子校验与按 Action 计数，§2a 各读取方的测试）；
  - 第 1–5 批每批 0.5–1 人日，主要是比对和测试；
  - 合计约 **6–8 人日**，比 L7 草稿的 3–5 人日多，多出的部分是停用旧版本的迁移、影子校验和 §2a 各读取方的测试；
  - 另外每批要加一次 stage 部署和 7 天影子观察，日历时间约 4–6 周。
- **风险**：
  1. **模式比运行时更严**，拒掉真实请求：靠导出规则（只导出一一对应的约束）、测试语料回放和 stage 影子观察三道把关；
  2. **版本切换的窗口**：旧版本没及时 inactive 会让工作台报 `published more than once`；角色清单漏改会 403；跨版本重试会被拒绝；
  3. **正则方言差异**：生成器只接受两边语义相同的子集；
  4. **计数和清单的连带修改**：每批都要改 register 计数、角色清单的版本和 NX-040 的后继规则测试。
- **负责人 2026-11-09 的决定**（原话所选选项）：
  1. “做，空闲线先做第 0 批（推荐）”；
  2. 第 3 批：“接受 7 天 + 至少 1 次（推荐）”；
  3. 没有真实调用时：“允许合成调用（推荐）”：用合成租户、非 real 的 persona，走受治理的路径，不产生真实效果；
  4. 切换窗口：“跟随普通部署，调度员定（推荐）”。

- **原先列出的待决定项**（已由上面四条决定）：
  1. 是否做方案 B，以及排在哪个阶段：它不阻塞任何已排的工作（方案 A 已解锁 Consumer v2）；
  2. 第 3 批（有业务或外部效果的操作）是否接受“7 天影子观察计数为 0 才发布”的节奏；
  3. 版本切换要求人工操作停一个窗口，请定窗口时间；
  4. **第 3 批里有外部或业务效果的 Action，如果 7 天内一直没有真实调用**：是允许用 stage 合成调用补足覆盖（合成租户、非 real 的 persona，不产生真实效果），还是必须等真实流量。

  其余（生成规则、分批顺序、验收标准）由 L6 判定即可。

## 5. 第 0 批的实现（前置部分）

- **迁移 `action_version_replace`**（临时号 0430）：在 `control.nexloop_action_definitions` 上加一个 before insert 触发器。只在配置员（`session_user = nexloop_configurator`）插入带 `previous_version` 的 Action 时生效：
  - 校验一对一替换：上一版本存在、摘要一致、处于 active、与新版本同一个 capability，并且是这个 stable_name 唯一的 active 定义；新版本号是上一版本号加 1；
  - 通过就把上一版本设为 inactive，不通过就报 `action replacement invalid`，什么都不改；
  - 同一份清单重放时什么都不改；
  - 其他写入路径（NX-040 的后继版本、模拟世界的目录、测试）不受影响；
  - 已发布的定义仍然不可变，只改 `active`。
- **生成器** `nexloop_eios/input_schemas.py`：从 `workbench_actions_http.OPERATIONS` 导出候选模式。**它的结果不能直接发布**（L6 C4）：第 1 批起，每个 Action 的模式要从 `GoalGovernedActions` 构造的请求体推出来，或者逐项比对；测试必须断言“这些方法构造出的请求体能通过候选模式”。运行时正则带有非默认 flags（不是 `re.UNICODE`）时拒绝导出（C3）。
  - 认识的检查导出为对应约束：`_id` 的正则（只接受两边语义相同的写法）、`_bool`、`_text`、`_reason`、`_scope`、`_kind`、UTC 时间（只写 `type: string`，不导出 format）；
  - 不认识的检查导出为 `{}`，只会更宽；
  - 正则按 `^(p)$` 导出；
  - 由请求体决定 spec 的操作（`read_experiment_report`、`invite_workbench_member`）和超出共同写法的正则（`invite_workbench_owner`、`send_outreach`）拒绝导出，留给人工处理。
  - 本机结果：80 个操作导出，4 个留给人工；240 个字段里有 144 个是 `{}`（lambda 检查）。这些要在各批里逐个和 `GoalGovernedActions` 构造的请求体比对后补上。
- **测试**：
  - `tests/test_d23_input_schemas.py`（纯测试）：
    - C1：没有 format、锚点、末尾换行这一点有意宽于运行时、超出共同写法的正则被拒；
    - 每个导出的模式至少拒绝一个探测值；
    - C2：follow_latest 不跟随、Capability 绑定不变、91 个都不是 effect Action、Host 只引用那两个工具；
    - C4：Context 组装从不把 `input_schema` 放进 Context。只有绑定 Action 的授权检查读它，测试按这一点区分。
  - `tests/test_d23_action_replace_pg.py`：
    - 一对一替换后，同一个 capability 只剩 1 个 active 定义（这正是 `action_for` 计的数）；
    - 重放什么都不改；六种不合法的替换都被拒、什么都不改；
    - 非配置员路径不受影响；
    - 变异检查：去掉“停用上一版本”那一行，替换用例按预期失败。
- **没有改任何已发布的 Action**。

### 5.1 影子校验（第 0 批，第 1 批发布前完成）

- **迁移 `action_input_shadow`**（临时号 0431）：
  - `control.nexloop_action_input_candidates`：候选输入模式，按租户、world 和精确 Action 版本存放，只能由可信配置写入（`control.nexloop_configure_input_candidates`，配置员身份）。整组替换并递增 revision；只接受处于 active 的 Action 版本。
  - `runtime.nexloop_action_input_shadow`：按租户、world、Action 版本、候选摘要和 UTC 日期计数，只有两个数：调用次数和“候选会拒绝”的次数。
  - `authz.nexloop_action_input_candidate` 和 `authz.nexloop_action_input_shadow_record`：租户从调用方的凭据推导（服务身份或浏览器会话的 realm），不由调用方提供；推导不出租户时，不返回候选，也不计数。
  - `authz.nexloop_action_input_shadow_report`：只读的 metrics 登录按候选读取 calls、would_reject 和状态，状态取值为：
    - `no_data`：没有调用，按 C3 不能发布；
    - `fail`：有“候选会拒绝”；
    - `pass`；
    - `stale`：这个 Action 版本已经不再 active。
- **派发**（`action_governor.shadow_validate`）：
  - 已发布模式的校验通过之后，如果这个 Action 版本有候选，就用和派发相同的校验器（开着 format 校验）再校验一次，只计数、不拒绝；
  - 不能用的候选按“全部拒绝”计，不会让影子关卡误判通过；
  - 影子本身出错不影响派发，只记一行警告（Action 名和异常类名）。
- **不记什么**：
  - 不记请求体、principal 或 Consumer；
  - 表里只有租户、world、Action 版本、候选摘要、日期和两个计数，测试逐列核对过。
  - 这张表不点名任何 Consumer 或成员，所以不在擦除覆盖范围内（`docs/implementation/erasure-coverage.md` 只针对点名 Consumer 或成员的表）；它随租户一起删除（有租户外键，`test_tenant_fk_pg` 通过）。
- **已知局限**：
  1. 影子只覆盖经过 `govern_published_action` 的调用。`nexloop.incentive.issue` 和 `nexloop.outreach.send` 的 Run 工具路径在 SQL 里受理（0145、外联的受理函数），不经过这里。这两个本来就在第 3 批（§2a），第 3 批要在 SQL 受理路径上加同样的计数，或者明确只看人工路径的调用。
  2. 每次受治理的调用多一次数据库往返（查候选）；没有候选时只读不写。
- **测试**：`tests/test_d23_action_input_shadow_pg.py`，在 `Consumer.edit` 的真实受治理写入路径上验证：
  - 全部拒绝的候选：写入照常 applied，计 1 次调用、1 次会拒绝，状态 `fail`；
  - 全部接受的候选：状态 `pass`；换一个候选后从 `no_data` 重新计数；
  - 只能由可信配置写入候选，只能针对 active 的版本，版本停用后状态为 `stale`；
  - 报告只有 metrics 登录能读；推导不出租户的调用方什么都不计。

### 5.2 L6 对 99c227fb 的条件（2026-11-09，已落实）

- **触发器 C1**：引用里和上一版本里的 `contract_digest` 都必须非空，不再出现 null 对 null 判相等；引用的 `tenant_id` 必须是本租户，`definition_type` 必须是 `action`。
- **触发器 C2**：比较整个 `capability_binding`，包括 capability 的名字、版本和 schema hash。
- **触发器 C5**：配置员插入版本号 ≥ 2、不带 `previous_version` 的定义，而同名已有启用定义时，直接拒绝。
- 触发器只处理 world `real`，模拟世界的定义永远不会被停用。NX-041 的守护测试 `test_a_simulation_definition_has_two_write_paths_only` 改为允许这一条“只改 active、只限 real”的停用路径，其余写入方仍然不能 update。
- **生成器 C3、C4**：见 §5。
- **catalog 只输出最新版本**（0b）：`catalog_publish.latest_only`。同一个声明里保留旧版本、加上新版本时，只输出新版本，并把 `previous_version` 设为旧版本的精确引用；同名同版本出现两次、版本号跳跃、新版本没有声明它替换的旧版本、capability 绑定不同，都拒绝编译。
- **测试**：触发器的拒绝用例从 6 个增加到 12 个（新增缺摘要、两边摘要都为空、其他租户、其他类型、capability 版本不同、版本 ≥ 2 却不替换任何版本）；生成器的 flags 用例；`latest_only` 的正反例。测试 `test_d23_action_replace_pg.py` 也已登记进 `DIRECT_DEFINITION_WRITERS`（它直接写定义行，标为 exception）。
