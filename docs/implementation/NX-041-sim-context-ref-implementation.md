# NX-041：sim 上下文按引用登记，加 sim manifest

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
