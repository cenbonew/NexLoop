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
| **服务授权**（`service-grants`，`follow_latest_version`） | 带 follow_latest 的（例如 `claim_matcher → Consumer.edit`）在下一次 register 时解析成新版本 | 部署单写明“发布后必须 register”，dry-run 预期新增的授权逐条列出（同 Consumer v2 那次） |
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
| Pi 侧工具清单（`apps/agent-host/src/runtime-effect-tools.ts`） | Host 的工具定义是代码里写死的，不由 `input_schema` 生成，只对应 effect、激励、外联、计划结果这几类工具，都不在这 91 个里。**不受影响** | 测试：第 0 批 grep 断言 Host 源码里没有引用这 91 个 Action 的 stable_name |
| Capability 绑定（`capability_binding`） | 新版本绑定同一个 capability 快照（`schema_hash` 不变） | 测试：新旧版本的 `capability_binding` 逐字节相同 |
| **代码里写死版本号的调用方** | 写死 `:1` 的调用在旧版本停用后会被拒（definition reader 读不到 active 的 `:1`）。在这 91 个里，本机 grep 找到：`business_setup.py:71`（`Consumer.create`，`action_version=1`）、`scripts/nx037/real_tenant_init.py:113`（`nexloop.budget.loosen`，`action_version: 1`）、`scripts/perf/test_micro_authz.py`（`Consumer.create`），以及大量测试 fixture | 写死版本的生产代码改为“取当前 active 版本”，与对应批次一起改；测试 fixture 自己发布定义，不受 stage 切换影响，但第 0 批要 grep 出所有引用并列成清单 |

## 3. 分批

每批都是“一组 Action 的新版本，加上对应的角色、计数和测试”，单独送 L6。

| 批 | 内容 | 个数 | 理由 |
|---|---|---|---|
| 0 | **前置**：补迁移，让可信配置发布新版本时在同一事务里停用旧版本（见 §2）；写导出生成器和比对测试；写**影子校验**（见下）；核查 §2a 列的全部读取方 | — | 后面所有批次都依赖它 |
| 1 | C 组中有 `goal_controls` 方法的 6 个（Budget、Contact、Control、Goal.propose、Goal.publish、Metric），加上 D 组 1 个 | 7 | 字段少、调用路径单一，用来跑通流程 |
| 2 | A 组里只读或可逆的操作：`alert.silence`、`experiment.report`、`approvals.*`、`object_set` / `object_view` / `semantic_release` 等 | 约 25 | 风险低 |
| 3 | A 组里有外部或业务效果的操作：`incentive.issue`、`outreach.*`、`real_dispatch.set`、`consumer.erase*`、`message.*` 等 | 约 38 | 一次拒错就是业务中断，放在影子校验积累了数据之后 |
| 4 | `workbench_member.*`（含执行期字段的两个） | 17 | 要与 NX-058 的审批策略一起核对 |
| 5 | B 组与 `Consumer.create`（外层模式，`properties` 开放）、`NexLoop.queue.operations`、`nexloop.service.query` | 4 | 需要人工核对服务端构造点；`Consumer.*` 还会影响 claim matcher 的改绑（同 Consumer v2 那次的窗口问题） |

**每批的验收**：

1. **生成器比对**：模式的字段集合和 `OPERATIONS` / 方法构造的请求体逐项相同；每个导出的约束都能在源码里找到对应的检查。
2. **不比运行时更严**：
   - **测试语料回放（在 CI 上执行，C5）**：在 CI 主机上，以影子模式跑一次全量 pytest。governor 对每个请求额外用候选模式校验，只记录、不拒绝；候选模式拒绝了而当前路径接受的请求数必须为 0。本机不跑全套（负载约定），只跑本批 Action 相关的测试文件。这一批里任何一个 Action 在语料里没有出现过，就要先补一个成功路径用例。
   - **stage 影子观察（C3）**：新模式先以影子方式部署 7 天，**按 Action 分别统计**调用次数和“候选会拒绝”的次数（只计数，不记请求体）。每个 Action 都要**至少有 1 次真实调用，并且“候选会拒绝”为 0**，才能发布新版本。7 天里没被调用过的记为 `no_data`，**不能发布**：要么补一次 stage 合成调用（用合成租户或 persona 非 real 的数据，见第 4 节负责人决定第 4 项），要么延长观察期。
   - 说明：库里没有保存请求体（`runtime.nexloop_action_claims` 只存 claim 和摘要），所以“回放已有 intent”只能用测试语料加 stage 影子观察替代，不能从库里取回真实请求。
3. **能拒绝**：每个新模式至少拒绝一个 `schema_gates.OPEN_PROBES` 探测值，`schema_gates.open_input` 对它返回 False。
4. **r8**：在 catalog 编译出的 Consumer Action 上，`tool_cases_built.open_input` 降为上一批的值减去本批个数；回放 0 失败；收紧候选的阳性对照仍判为回归。
5. **部署后**：工作台每个操作抽一次成功调用；跨版本重试的拒绝计数如实记录。

## 3a. Context 体积（C4）

- **模型看不到这些模式**：Context 组装不带 Action 的 `input_schema`，`nexloop_eios/context_engine` 和各个 context 包函数里都没有引用它；Pi 侧的工具定义是 Host 代码里写死的（§2a）。这 91 个 Action 都是人工治理操作或服务写入，不是模型工具。所以补模式**不会让模型看到的工具定义变大**，对真实 Context 的体积影响为 0。
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
- **需要负责人决定（B 类）**：
  1. 是否做方案 B，以及排在哪个阶段：它不阻塞任何已排的工作（方案 A 已解锁 Consumer v2）；
  2. 第 3 批（有业务或外部效果的操作）是否接受“7 天影子观察计数为 0 才发布”的节奏；
  3. 版本切换要求人工操作停一个窗口，请定窗口时间；
  4. **第 3 批里有外部或业务效果的 Action，如果 7 天内一直没有真实调用**：是允许用 stage 合成调用补足覆盖（合成租户、非 real 的 persona，不产生真实效果），还是必须等真实流量。

  其余（生成规则、分批顺序、验收标准）由 L6 判定即可。
