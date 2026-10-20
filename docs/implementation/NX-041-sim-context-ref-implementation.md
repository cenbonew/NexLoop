# NX-041：sim 上下文按引用登记，加 sim manifest

> **D10 状态（2026-10-21 更新）**：D10 已定案（负责人 2026-10-21 放宽为三凭据：api、worker、agent，每个只绑定这一个模拟世界，删除世界时一并吊销），实现中。下文中“D10 待定”“等 D10 定下来”“D10 定案时”等说法，均指这一定案。

依据：L6 Host 协议审查 `nx041-host-sim-context-review.md` 4.3。原来的完整性三点保证，对「不被篡改」够用，对「来源可信」和「可审计」不够。

迁移：`0270_nx041_sim_context_ref.sql`，临时编号。

## 做法

| 项 | 实现 |
| --- | --- |
| **按引用登记** | 新增入口 `authz.nexloop_sim_context_register(digest, world, run_id, consumer, limit)`。只有 `nexloop_sim_api` 能调用，也就是 `app_role` 为 api 的 sim 组；认证用该 world 唯一的凭据。调用方只给 Run、Consumer 和 limit。Run 必须是这个 world 的 Run 凭据，并且由调用方自己的凭据签发、处于 active 状态。入口在同一个事务里调用 S7b 的 `sim_context_command`（唯一的组装方）组装包，以包的 jsonb 文本作为 Run 的 input 文本，连同 sha256 一起固定到 Run 上（`runtime.nexloop_sim_context_bindings`），然后返回这段文本。同一个 Run 重复登记，返回已固定的文本，不会重新组装；换 Consumer 或 limit 报 `run_bound`。 |
| **自拼的包被拒** | `authz.nexloop_runtime_run_bindings`（0037，Run 登记）上加了触发器 `nx041_sim_run_input_fixed`，设为 `ENABLE ALWAYS`，replica 模式也会触发。sim 或 shadow world 的 Run，其 `input_digest` 必须等于已固定包的摘要，否则报 `input_not_registered`。real 和 test 的行直接放行，real 路径的行为不变。0037 的入口函数体没有改动。 |
| **manifest** | `runtime.nexloop_sim_context_sources` 按包的顺序，记录每个模拟来源：section（consumer_state、evidence、current_event）、ref、revision、content_hash。binding 行上还记了包的摘要、世界时间、包含的事件数和 limit。这样事后能回答「模型当时看到了什么、来自哪些事件」。两张表都是 append-only（沿用 S4 的 `sim_ledger_guard`），都有 world 列：S2 的整体删除会按依赖拓扑序删除它们，sources 先于 bindings，bindings 先于 run_credentials；递归依赖守护测试也不需要新增声明。 |
| **Python / Host 衔接** | `nexloop_eios.sim_context`：新增 `register_sim_context`，`sim_run_input(session, run_id, consumer, limit)` 改为按引用登记，返回固定下来的文本作为 Run 的 input。`assemble_sim_context` 保留为只读预览，不能作为 Run 的 input。**Host 的 TS 没有改**：input 文本仍然是包本身，只是改由 SQL 给出；`parseSimContextPack` 和 guard 的摘要核对都照旧。 |

## 测试（`tests/test_nx041_sim_context_ref_pg.py`）

- **按引用登记**：
  - 包和摘要固定，世界时间、事件数、证据引用都正确；
  - 重复登记返回同一段文本，之后新增的端口事件不会改变它；
  - 换 Consumer 或 limit 被拒；
  - Host（Node 24）能解析并渲染这段固定的文本。
- **自拼的包被拒**：改动过的包，或者一个还没固定包的 Run 去登记，都报 `input_not_registered`。阳性对照：固定的包能正常登记；real 的登记不受影响。
- **只有 sim 组 api 能登记，而且只能登记自己的 Run**：
  - real 组的 api、domain_worker、twin_port、sim_worker 都被拒，real world 也被拒；
  - 用 A 的凭据登记 B world 的 Run，报 `run_unavailable`；
  - A 的凭据进入 B world，在服务身份那一步就被拒。
- **manifest 完整**：按顺序逐行与包对应；每个 evidence 来源都能对上本 world 的端口事件及其序号；manifest 只能追加。
- **整体删除**：删除后 bindings 和 manifest 在该 world 都是零行，没有 blocked；另一个 sim world 的数据保留。

## 限制

- sim world 里的 Run 端到端（D10 等前提）仍未打通。本项只保证「一旦登记，输入来自唯一的组装方，并且可以审计」。
- 来源可信依赖 `sim_context_command` 本身：它只读模拟来源，这一点由 S7b 的守护测试固定。

## 收尾（L6 审查 `nx041-sim-context-ref-review.md`，L2、L4 意见）

| 项 | 处理 |
| --- | --- |
| 双重保险（L2） | `sim_context_register` 除了 `app_role` 判断，还要求 `session_user='nexloop_sim_api'`。测试：临时把执行权误授给 `nexloop_api`，调用仍然报 `sim context: unavailable`。 |
| 0037 触发器 | update 时监视的列加上 `tenant_id`：把 sim Run 的登记行改到另一个租户，按新登记处理，因为那个租户下没有固定的包，所以被拒。测试同时确认触发器是 `ENABLE ALWAYS`。 |
| 渲染版本 | 采用「Host 导出常量 + 登记时写入」：Host 导出 `SIM_RENDER_VERSION='nexloop-sim-render/1'`（`apps/agent-host/src/sim-context.ts`，**只加了这一个常量**）；SQL 用 `runtime.nexloop_sim_render_version()` 给出同一个值，每次登记写入 binding 的 `render_version` 列。为了把版本号和实际渲染输出绑在一起，加了一份金样 `tests/support/golden/nx041-sim-render.json`，记录版本号和契约示例渲染结果的 sha256。只要渲染输出变了而版本号没升，测试就会失败。没有选「Host 回报渲染摘要」：那需要新增一条 Host 到数据库的回报路径，改动更大。 |
| 删除回归 / 权限 | 原有的整体删除测试已经覆盖两张表（含外键），删除后零残留；新增断言确认 sources 到 bindings 的外键存在。两张表对所有 `nexloop%` 角色（owner 除外）和 PUBLIC 都没有权限。 |
| 最小权限 | 0270 收回了 0269 授予 `nexloop_sim_api` 的 `sim_context_command` 直接执行权，只保留 register。register 以 owner 身份调用组装函数，组装函数里的角色判断仍然读 `app_role`。S6c 的白名单和 L2 的 Host 测试里相应的断言都已改成「sim_api 不能直接调用组装函数」。 |
| 契约示例（L4） | 示例改为自洽的数据：上限 1，共 2 条事件，包含 1 条，省略 1 条，并报告 `older_events_omitted`；补了 persona 条目；各条目的 `content_hash` 按 PG jsonb 文本重新计算。测试会核对计数是否一致，并在 PG 里重算每个哈希。 |

## D10 之后的接线顺序

1. sim 组的 api（`nexloop_sim_api`）用该 world 的凭据签发这次 Run 的 Run 凭据（`issue_run_credential`）。
2. 调用 `authz.nexloop_sim_context_register(digest, world, run_id, consumer, limit)`（Python 为 `nexloop_eios.sim_context.sim_run_input(session, run_id, consumer, limit)`），拿到返回的 `pack_text`。
3. 用这段 `pack_text` **原样**作为 Run 的 input 登记 Run（`accept_runtime_event` / register）。input 摘要必须等于固定包的摘要，否则登记会被拒（`input_not_registered`）。
4. Host 用 `parseSimContextPack` 解析、用 `renderSimContextPack` 渲染；guard 核对 input 摘要，这一步不变。
