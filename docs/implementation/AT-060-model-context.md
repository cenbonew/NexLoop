# AT-060 第 4 项（模型请求上下文）：判定修正与 v6 消息 Run

依据：NX-031 设计稿 §4.4 第 4 项（门槛项，违规须为 0）；调度员 2026-10-13 决定采用 A + B1，验收口径不变。

## 背景

AT-060 正式整轮（200 客户，矩阵 v2）中，门槛格 C4、C4-low 的 model_context 都是 checked 0，观察格有 18–151 个样本，所有格违规都是 0。核查结论：

- 负载格的消息 Run 用 v2 上下文，不写 `runtime.nexloop_model_requests`。只有 v6 Run（回复兜底、计划复评）写入；低速门槛格没有兜底，所以样本为 0。
- 原判定把引用的 ID 与 `_RUNS`（只含 `nexloop_message_routes` 里的消息 Run）连接。兜底 Run 和复评 Run 不在其中，所以观察格的违规在结构上恒为 0：**比对从未真正发生**。
- 部署配置（`deploy/stage/agent-host.runtime-config.example.json`、`context-strategies.v1.json`）用的是 v6，负载用 v2 本来就不代表生产。

## A：判定修正（`scripts/load/collect.py` 第 4 项）

- 每条模型请求经它的 `context_id` 找到 `control.nexloop_effect_contexts` 中该上下文的消费者，与这条请求的上下文来源（`runtime.nexloop_context_sources`）中引用的每个 64 位十六进制 ID 比对。消息 Run、兜底 Run、角色 Run 一视同仁，不再连接消息 Run 表。
- 违规：引用的是其他消费者，或者其他消费者的会话、消息；上下文找不到消费者的请求也计为违规（fail closed）。
- `checked` 为模型请求数，新增 `ids_compared` 为实际比对的 ID 数。`ids_compared` 为 0 表示没有比对任何东西，不能作为门槛证据。
- 阳性对照（`tests/test_load_model_context_pg.py`）：
  - 引用其他消费者、其他消费者的会话或消息，各计 1 条违规；没有消费者的上下文计 1 条；自己的 ID 和非身份 ID 不计。
  - 在实际 Host 跑完的 v6 消息 Run 上：违规 0，`ids_compared` > 0；在同一上下文中加入一条引用其他消费者的来源后，违规变为 1。

## B1：负载格的消息 Run 改用 v6

- **选协议的入口**：消息 relay 的 `--context-strategy`（`nexloop_eios/message_relay_cli.py`；`message_relay.py` 在设置了策略时用 `ContextV6ArtifactProducer`，recipe 模式和 run profile 模式都一样）。不设置就是 v2。
- **改动**：
  - `scripts/load/stack.py` 的 `start_message_chain(..., context_strategy=None)`：设置后给 relay 传 `--context-strategy`；
  - `scripts/load/matrix.py` 的 `MESSAGE_CONTEXT_STRATEGY = 'recent_plus_required'`（部署内置策略，模板在 `load_chain.load_plans` 里发布），各格启动消息链时传入，并写入 `cell.json` 的 `message_context_strategy`。
- **不变的部分**：模板的历史回复步骤（`build_template_hold.reply_history`）仍用 v2。它在模板发布策略之前运行，属于模板准备，不在任何格的窗口内。
- **验证**：`tests/test_load_chain_pg.py::test_the_load_relay_binds_message_runs_with_context_v6_when_given_the_strategy`，用负载链自己的 Source（按登记派生的读取权，0142）以 run profile 模式绑定 v6 pack；Host 侧的写入见上面的端到端测试。

## 后果（需要在 CI 主机上重跑门槛格）

- **v6 让守卫和数据库负担更重**：每个消息 Run 的绑定要在 SQL 里重新推导并逐项核对 v6 pack 的来源（读证明、内容哈希、来源修订）；每次模型调用都要写模型请求、提示与结果记录，并经守卫核对请求快照（编号连续、摘要一致）。上一轮的 guard 调用数、p95 与派发延迟都不能直接沿用。
- **门槛格（C4、C4-low）必须在 CI 主机上用这一版重跑**，才能得到第 4 项的有效证据。观察格的数据也会变化。
- 上一轮（`20261012T234421Z-b8831ff`）各格第 4 项的"违规 0"不能作为证据引用（见背景）。
- 建议的后续（本分支未做）：报告在门槛格 `model_context.ids_compared == 0` 时判失败，避免空样本被当作通过。
