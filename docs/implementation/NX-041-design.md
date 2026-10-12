# NX-041 设计稿：模拟世界与 Action 替身

- **状态**：设计稿，只有文档；不写代码、不加迁移；D 项待负责人决定。
- **分支**：`nx041-design`，从 main `146985f` 切出。
- **依据**：
  - planning/tasks.json NX-041：S7，模块 M21/M22，交付物为“数据/凭据/效应强隔离与来源”；
  - 01_PRD：顺序原则第 17 行“真实运营及动作可靠性优先；模拟实验和策略选优后置”；v0.3 第 36 行；M21 第 288–296 行；M19 第 164 行；M13 第 214 行；M20 第 286 行；系统验收第 322 行；
  - 03_DOMAIN_DATA_MODEL 第 23 行（`world_id` 与 `mode`；real 固定 `world_id='real'`）；
  - 16_ADR 第 31 行（“让模拟触达真实客户”不是普通实现细节）；
  - 14_TEST_ACCEPTANCE：AT-042（第 54 行）、AT-046（第 58 行）、第 94 行（模拟或影子回放没有真实外部效应权限）；
  - AT-046 前置的检查点与已知限制：`docs/implementation/AT-046-simulation-credential-guard.md`（随 s5j 合入，迁移 0165）。
- **下游**：
  - NX-042（消费者孪生与校准）在本设计的世界和替身之上运行；
  - NX-043（策略实验与受控线上验证）使用影子世界。

## 1. 定位与原则

- **优先级**：模拟晚于真实运营（PRD 第 17 行）。NX-041 只在 v0.1 的真实链路稳定（NX-037 受控交互完成）之后实施。
- **隔离**：模拟世界与 real 在对象、事件、预算、凭据和 Action 上完全隔离（M21 第 294 行）。
  - 孪生和替身都不是事实库；
  - 模拟结果永远不能升格为真实事实（M13 第 214 行）；
  - 界面不得把模拟显示为真实（M20 第 286 行）。
- **不靠调用方自觉**：隔离不依赖调用方标注 mode。world 和 mode 由服务端从认证上下文推导；即使模型或调用方伪造 mode，也会被拒绝（AT-046）。

## 2. 世界模型

### 2.1 世界与 mode

| world_id | mode | 用途 | 外部效应 |
|---|---|---|---|
| `real` | real | 真实运营 | 受治理 Action 加三层开关（0153） |
| `test` 或其他非保留名 | test | 合成、回归、受控测试 | 只有回环或测试替身 |
| `sim:<id>` | simulation | 孪生模拟、策略筛选（NX-042/043） | 只有 Action 替身 |
| `shadow:<id>` | shadow | 与 real 并行的影子决策（NX-043） | 只有 Action 替身，不发送 |

- **现有的映射**：
  - `runtime.nexloop_cost_data_mode`（0120_nx027_costs.sql:77–79）：simulation 和 shadow 都映射为成本模式 `simulation`，其他非 real 映射为 `test`；
  - `runtime_activation.py:34`：mode 与 world 一致，`(mode=='real') == (world_id=='real')`；
  - Claim 契约：shadow 投影为 simulation（`contracts.py` 中 claim schema 的 mode 说明）。
- **命名（D1）**：推荐用前缀 `sim:`、`shadow:` 区分实例，每个实验一个世界。这样实验之间的数据也互相隔离，可以整体删除。

### 2.2 数据隔离

- **共用租户，分 world**：
  - 一个模拟世界属于一个租户，使用同一个 `tenant_id`；
  - 所有行都带 world，唯一约束、外键和幂等范围都包含 (tenant, world)（03 第 23 行）；
  - RLS 仍按租户隔离，world 是租户内的第二维。
- **初始状态来自快照**：
  - 模拟世界从 real 的脱敏快照建立（M21 第 292 行“输入经过脱敏的世界快照”）；
  - 复制时重新生成对象 ID，并记录来源映射 `sim_object_id → real_object_ref digest`，只存 digest；
  - 原始消息只保留 content_hash，不复制原文；需要文本的孪生输入走 NX-042 的脱敏流程。
- **单向**：
  - 模拟世界的任何行都不写回 real；
  - real 的读取函数一律以 `world='real'` 过滤。现有 0049、0052、0055、0063、0079 的表约束 `check(world='real')` 保证消息、出站、Run 签发和角色绑定只存在于 real（检查点见 AT-046 文档 §1）。
- **删除传播**：
  - real 中删除一个主体后，所有由它派生的模拟世界数据都要被清除。依据来源映射加上删除日志（`control.nexloop_deletion_ledger`；0129、0140）；
  - 模拟世界可以整体丢弃（D2）。

### 2.3 来源标记

- **标记字段**：模拟世界产生的每条记录（事件、意图、替身回执、成本、指标观察）都带以下字段：
  - `world`（`sim:<id>` 或 `shadow:<id>`）；
  - `data_mode='simulation'`；
  - `origin`：`twin`、`replay` 或 `injected`；
  - 生成它的替身版本或孪生版本。
- **防止进入真实指标**：
  - 已有约束：`runtime.nexloop_cost_entries` 的 `check((world='real')=(data_mode='real'))`（0120:45）；商业观察同样有约束（0118_nx027_commercial_intake.sql:99）。这保证模拟成本和指标不会计入 real（AT-042）。
  - 新增的模拟表沿用同一约束形式。

## 3. Action 替身

### 3.1 替身契约

每个外部效应 Action（0153 中 `external_effect` 未声明为 false 的 Action）在模拟世界中都必须有一个替身，否则该 Action 在模拟世界中不可用，按 fail closed 处理。替身契约如下：

- **输入**：与真实 Action 相同的冻结请求，即 `runtime.nexloop_effect_intents` 中的 `frozen_request`、`business_digest`、`provider_payload_digest`（0040_effect_intents.sql:30），外加模拟时钟时间与世界种子。
- **输出**：与真实 provider 相同形状的回执，即 `effect_provider.EffectProviderReceipt`（`effect_provider.py:70`）：状态（fulfilled、failed、unknown）、外部引用、延迟，另加 `simulated: true` 和替身版本。
- **确定性与可重放**：
  - 结果由 (替身版本, 世界种子, intent_id, 模拟时钟) 唯一决定；
  - 同一意图重复执行得到同一回执，同时计为一次重复执行，用于在模拟中检验幂等；
  - 替身记录写在模拟世界自己的回执账本中，按 intent 查询，与 HTTP effect provider 的 `GET /v1/effects/<intent_id>` 语义相同。
- **行为配置**：替身可以配置成功率、失败码分布、结果不明的比例与延迟分布，用于检验对账与恢复路径。配置版本化，写入实验冻结输入（M22）。
- **替身目录**：
  - v0.1 已有的外部效应 Action：`nexloop.service.request`（HTTP 服务端点）、`nexloop.incentive.issue`（优惠发放；v2 中声明的回环账本 `incentives.LocalIncentiveLedger`，`incentives.py:70`）、本地 JSON 投递（NX-016）；
  - 原生 WebChat 回复不是外部效应（0153 `external_effect=false`）。在模拟世界中，它由孪生接收，不经过 outbound 记录服务。0079 的表只允许 real，所以模拟回复写入模拟世界自己的会话表。

### 3.2 派发路径

- **执行器**：模拟世界的意图由替身执行器执行，而不是 effect worker 或 incentive issuer。这两个进程的 `--world` 只接受 `real`（`effect_worker.py:33`、`background_services.py:78`），SQL 侧也拒绝非 real（0042_effect_execution_ledger.sql:165、0051_local_json_delivery.sql:24）。
- **替身执行器的约束**：
  - 只持有模拟世界的服务凭据。AT-046 的迁移 0165规定含 real 的凭据只能含 real，所以它不可能同时持有 real 凭据；
  - 没有任何真实 provider 配置，没有网络出口（D4）；
  - 它调用的是新增的 `authz.nexloop_simulated_effect_command`：只接受 `sim:` 或 `shadow:` world；对 real 一律拒绝；只调用替身。
- **三层开关**：真实派发开关对非 real world 恒为关闭，`closed_layer` 返回 `world`（0153:151）。模拟世界不需要也不能打开它。

### 3.3 替身结果不进入事实层

- **不能变更 real 对象**：替身回执只能推进模拟世界内的意图状态。它不能触发 real 的对象变更、Commitment 结算或商业记录。
- **Claim**：模拟世界中的 Claim 和抽取结果，按 Claim 契约 mode 为 simulation，并带 `world_id≠real` 的约束（`contracts.py` 中 claim schema 末尾的 if/then），不进入 real 的审核队列。
- **对外展示**：评估报告、工作台和指标导出对模拟数据始终标注世界与 mode（M20 第 286 行），并默认不与 real 混合汇总。

## 4. 模拟时钟与事件注入

- **模拟时钟**：
  - 每个模拟世界有自己的逻辑时钟（`sim_clock(world)`），由模拟驱动显式推进，不读墙钟；
  - real 中依赖时间的逻辑，在模拟世界中改读模拟时钟。涉及的逻辑有：回复时限（reply-guarantee）、Commitment 到期、预算周期、保留期、计划重评。
  - **实现前置**：这些逻辑目前直接使用 `clock_timestamp()` 或 `now()`。NX-041 实现时要逐一列出，并改为接收时钟参数，例如 `commitment-keeper`、`reply-guarantor`。在模拟世界中不允许回退到墙钟（D5）。
- **事件注入**：
  - 模拟驱动按时间线注入事件，包括消费者消息（来自孪生）、外部回执延迟、故障（结果不明、超时）；
  - 每个注入事件都带 `origin='injected'`、模拟时间和注入计划的版本；
  - 注入计划和时间线是实验冻结输入的一部分，同一输入可以完整复现（M22 验收）。
- **加速与上限**：模拟可以快于真实时间。但每个模拟世界都有 Run 数上限、模型费用上限与墙钟时长上限，任一超限都会停止。

## 5. 与孪生（NX-042）的接口

- **孪生的角色**：孪生是模拟世界中的“消费者”。它对接一个“消费者端口”：接收 Agent 的回复和替身送达的效应，按行为模型产生下一条消息或行为。
- **端口契约**：
  - `observe(world, consumer_sim_id, event) → None`：送达 Agent 回复、替身效应回执、营销触达；
  - `act(world, consumer_sim_id, clock) → event | None`：产生下一条消息、拒绝联系、付款意向等，带不确定性和版本。
- **约束**：
  - 孪生不能调用 Action，也拿不到任何凭据，只能经端口产生事件（AT-046 “Twin 调用真实消息 Action：服务端拒绝”）；
  - 孪生的输出不是事实（M21 第 294 行）；
  - 校准必须用独立的真实数据，不能把预测包装成确定概率（M21 第 296 行），这部分属于 NX-042。
- **AT-046 的完整验收**：在模拟世界中构造一个孪生，让它（或伪造 mode 的模型）尝试调用真实消息 Action，断言服务端拒绝，并且没有任何外部调用。目前 v0.1 的负例只覆盖服务端各入口（AT-046 文档 §2）。

## 6. Agent Host：模型凭据与预算隔离

- **现状**：
  - Host 的模型凭据和运行配置按进程设置：`runtime_profile` 为 `deterministic-test` 或 `deepseek-flash`，`model_configuration_file`、`maximum_request_cost`（`apps/agent-host/src/runtime-host.ts:40–42`）；凭据不进入 Run（`trusted-model-profile.ts:1`）；
  - 模型凭据不区分 world，这是 AT-046 已知限制之一；
  - runtime worker 和 guard 的 `--world` 已限制为 real 或 test（AT-046 修复）。
- **方案（D3）**：模拟世界使用独立的 Host 与 runtime worker 进程组：
  - 运行在独立的模拟主机或容器网络；
  - 使用独立的模型账号，并在账号侧设置上限；
  - 使用独立的 `model_configuration_file`。real 的 Host 进程拒绝非 real 的 Run 启动，模拟 Host 拒绝 real 的 Run 启动：启动时比较 Run 命令中的 `mode`、`world_id`（`runtime_activation.py:34`）与进程的配置值，fail closed；
  - runtime worker 和 guard 增加一个 `sim` 选项，但只在模拟进程组中可用，并且只能取模拟世界的服务凭据。
- **预算**：
  - 模拟世界的模型费用记入 `runtime.nexloop_cost_entries`，data_mode 为 simulation（0120:79 的映射）；
  - 有独立的模拟预算（`control.nexloop_budget_limits` 中模拟世界自己的 model 行；表主键含 world，0068_nx022_goals_controls.sql:105）；
  - 不占用 real 的 D-g 上限，账号侧另设上限。
- **确定性优先**：策略筛选的大部分模拟用 `deterministic-test` 或固定种子的廉价模型。真实模型只在需要的实验中按预算使用（D6）。

## 7. AT-046 已知限制的处置

| 已知限制（AT-046 文档 §3） | NX-041 的处置 |
|---|---|
| simulation 中可以记录效应意图（0040 无 world 约束） | 保留：模拟世界需要记录意图，由替身执行。新增约束：非 real world 的意图只能由替身执行器领取；0042 与 0051 的 real 检查保持不变 |
| `nexloop.service.query` 不受 world 检查 | 在模拟世界中也由替身回答，不访问真实端点。替身执行器没有真实 provider 配置，所以即使有豁免也够不到外部 |
| 是否算外部效应按 (tenant, world) 声明 | 模拟世界不读外部效应声明：在模拟世界中，所有外部效应 Action 都必须有替身，没有替身即不可用（fail closed），不存在“声明为非外部就直接执行”的路径 |
| Host 模型凭据不分 world | §6：独立进程组、独立账号、启动时比较 mode |
| Twin 尚不存在 | §5：孪生只经端口产生事件；补齐 AT-046 的完整验收 |

## 8. 验收映射

| 验收 | 覆盖方式 |
|---|---|
| AT-046 模拟效应隔离 | 孪生或伪造 mode 的模型尝试调用真实消息 Action，服务端拒绝；替身执行器对 real 拒绝；模拟凭据不能认证 real；没有外部调用 |
| AT-042 指标世界 | 模拟付款或成本只以 data_mode simulation 记录，不计入 real 收入或续费 |
| M21 第 296 行 | 测试环境不能调用真实发送和付款：替身执行器没有真实 provider 配置和网络出口；real 的 Host 拒绝模拟 Run |
| M22 可复现 | 同一冻结输入（快照 digest、种子、替身版本、注入计划、孪生版本）得到同一结果 |
| 系统验收第 322 行 | 模拟污染测试全部拒绝：模拟行不能出现在 real 的读取与指标中 |
| 删除传播 | real 中删除主体后，派生的模拟世界数据被清除，或整个世界被丢弃 |

## 9. 需要负责人决定的事项

| 编号 | 问题 | 推荐 | 代价 |
|---|---|---|---|
| D1 | 世界命名与粒度 | 每个实验一个世界，`sim:<uuid>` 或 `shadow:<uuid>`；不允许 real、test 以外的保留名 | 世界数量多，需要列出和清理 |
| D2 | 模拟世界的生命周期 | 实验结束后保留 30 天（只保留评估所需的 ID、digest 与指标），然后整体删除；real 中的删除立即传播 | 复查旧实验时只能看到摘要 |
| D3 | 模拟进程如何隔离 | 独立的 Host 与 worker 进程组和独立模型账号；在双机部署中，模拟进程组只运行在 APP_HOST 的独立 compose 项目中，不与 real 共用网络命名空间 | 部署更复杂，资源占用更多 |
| D4 | 替身执行器的网络 | 不允许任何外部出口（只能访问本机数据库与 Artifact） | 无法模拟真实端点的网络行为，只能用参数化的延迟与故障 |
| D5 | 模拟时钟的改造范围 | 先只改造回复时限、Commitment 到期和计划重评这三个与孪生交互直接相关的逻辑；其他逻辑在模拟世界中禁用 | 模拟覆盖面受限 |
| D6 | 模拟中使用真实模型 | 默认用 deterministic 或固定种子的廉价模型；真实模型需要实验单独审批预算，模拟账号月度上限 ¥100 | 模拟的行为保真度受限 |
| D7 | 影子世界的输入 | 影子世界只复制 real 的入站事件（脱敏后），Agent 决策只记录不发送；影子与 real 的决策差异作为 NX-043 的评估输入 | 影子需要实时复制入站事件，增加负载 |

## 10. 不在本任务范围

- 孪生行为模型与校准：NX-042。
- 策略实验、holdout 与线上验证：NX-043。
- 本设计中提到的新增函数、约束与进程，都在 NX-041 实现时另行提交，并附迁移与测试。
