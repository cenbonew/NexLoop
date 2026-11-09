# 运行单：在 stage 上攒够 NX-064 的 p99 样本（备用）

- **状态**：备用。负责人对“NX-064 的耗时样本怎么补”的答复是“先等”。这份单子先写好，**跑不跑由负责人决定**。
- **类别**：B 类（消耗预算）。**执行者是调度员**；开跑前需要负责人在会话里明确确认；判定者是 L6。
- **目的**：11-08 的门禁结果是 `insufficient`，`host_op` 0 个，`guard_op` 2 个。本单的目标是让 `nx064_p99_gate.py` 读到**至少 1000 个 `host_op`/`effect_submit` 样本**，然后按 `run-nx064-effect-request-metrics.md` §6 判定。
- **复用**：负载形状沿用批次 122 HEAVY 回归用的 NX-031 矩阵 `deploy/load/matrix.v2.json`：门禁格的并发等于部署的 `maximum_active_runs`，见 `tests/test_nx064_effect_load_pg.py`。驱动和收集沿用 `scripts/load/driver.py`、`scripts/load/collect.py`。
- 本单不出现主机名和地址。主机用角色名：CI 主机、APP_HOST、DATA_HOST。

## 0. 先说清楚的事实

1. **HEAVY 回归本身不产生 `host_op`。** `test_nx064_effect_load_pg.py` 模仿 Host 的请求方式（`submit_like_the_host`），压的是 guard 和 SQL 这一侧，只会留下 `guard_op`。`host_op` 只在真正的 Agent Host 发出效果请求时才写，见迁移 0405。所以要攒 `host_op`，必须让真实的 Host 进程跑带效果工具的 Run。
2. **Host 的两种模型 profile**：`deterministic-test` 不调用模型、没有费用，`deepseek-flash` 是真模型、按 token 计费。负载用的确定性协议，比如 `deterministic_protocols`、`deterministic_latency`（D5 注入的模型延迟），**只在 `deterministic-test` 下才被接受**，见 `apps/agent-host/src/runtime-host.ts` 的配置检查。
3. **默认方案是 deterministic-test**（§1 方案 A）。模型费用是 0；p99 衡量的是 Host 到 guard 的效果请求，与模型无关，用真模型没有必要。真模型方案（方案 C）只在负责人明确要求时才用，而且必须先定费用上限。

## 1. 方案与选择

| 方案 | 在哪里跑 | 样本来源 | 费用 | 结论 |
| --- | --- | --- | --- | --- |
| **A（推荐）** | CI 主机的负载车道。`scripts/load/matrix.py` 克隆模板，起一套独立的栈，Host 用 `deterministic-test` | 这一格结果目录里的 `process_samples.jsonl`，即 §6 第 5 步 | 0（模型）；只占 CI 主机的时间 | 不碰 stage，也不碰 real |
| B | stage 上起一个**额外的** `deterministic-test` Host，只服务 sim 或 test world 的合成租户 | stage 上 `runtime.nexloop_process_samples` 的 `host_op` 行 | 0（模型）；stage 资源 | 要先确认 §2 的前提；任何一项不满足就退回方案 A |
| C | stage 现有的真模型 Host，合成租户 | 同 B | 真模型 token 费用 | 只有负责人明确要求才用；按 §3 定费用上限 |

方案 A 的样本来自 CI 车道，不来自 stage 部署本身。证据里要写明“来源：CI 负载车道，deterministic-test”。它能证明 Host 侧效果请求耗时的分布，但不能代表 stage 的网络和负载。要不要接受这个来源，由负责人和 L6 决定。不接受的话，用方案 B。

## 2. 隔离（所有方案都必须满足）

- **只用 sim 或 test world 的合成租户和合成顾客**，不碰 world `real` 的任何租户、顾客、Action 或凭据。
- **不触发真实派发**：
  - 方案 A 的栈由 `scripts/load/stack.py` 起，里面固定是 `REAL_DISPATCH_ENABLED=false`。
  - 方案 B 和 C 开跑前，要只读确认这个合成租户的 `real_dispatch` 是关闭的，而且没有打开的真实派发窗口。查询方法见 §5 第 3 项。
- **不能和下面两件事同时开着**：
  - **ADR-045 故障开关**（`nexloop.synthetic_fault.set`，见 `docs/design/synthetic-model-fault.md`）。它会让注入的 Run 失败，样本就不能代表正常耗时。
  - **NX-056 的真实派发窗口**，也就是审批通过、正在生效的真实派发。负载期间所有派发必须是模拟的。
  - 任何一项开着，就不开始；负载期间任何一项被打开，就按 §4 停止。
- 方案 B 的额外 Host 用单独的 `host_id` 和 `world_group`，不注册成 real world 的 Host，也不持有 real 的 Run 凭据。

## 3. 时长与预算

- **样本数**：门禁要求 `samples ≥ 1000`。目标设为 **1500**，留出余量，因为每分钟一份增量，跑到最后可能不满一分钟。
- **时长**：
  - 方案 A：一个门禁格的窗口是预热加稳态，按 `matrix.v2.json` 那一格的设定（`--timing-scale 1.0`，车道上不能缩短）。跑完一格，样本不够 1500 就再跑一格，**最多 3 格**。3 格后还不够，就停下来，报告实际样本数。
  - 方案 B 和 C：按“效果 Run 每分钟数 × 分钟数 ≥ 1500”来估。**上限 2 小时**，到时间不管样本数多少都停。
- **预算**：
  - 方案 A 和 B 的模型费用是 0。开跑前只读确认 Host 配置里 `runtime_profile` 是 `deterministic-test`，而且没有 `model_configuration_file`。
  - 方案 C 需要负责人给出**费用上限**（单位与 `cost_currency` 一致），并用 Host 的 `maximum_request_cost` 和合成租户的 evolution 预算同时把住。调度员事先按“每个 Run 的平均费用 × 1500”估一次，估值超过上限就不跑。

## 4. 停止条件与回退

满足下面任何一条，就立即停止注入负载（方案 A 是停掉 matrix 进程；方案 B 和 C 是停掉驱动），然后按 §5 做跑后核对：

1. 出现任何 world `real` 的写入、派发或效果，包括 outbox 和 real 租户的 intent 计数变化。
2. ADR-045 故障开关或 NX-056 真实派发窗口被打开。
3. stage 告警进入 firing。负载本身引起的预期告警，要事先列出来，才能不算。
4. 方案 C 的累计费用达到上限的 80%。
5. 1 分钟负载超过 12（CI 主机或 APP_HOST）；或者现有服务出现变化，即 `existing_services_unchanged` 失败（见负载车道的检查）。
6. 到了 §3 的时长上限。

**回退**：
- 方案 A 没有需要回退的东西。车道克隆在 `--lane-root` 下面，matrix 跑完或中止后会自己删掉；中止的格子会写 `completed: false`。
- 方案 B：停掉额外的 Host，撤销它的配置，再删掉合成租户的负载会话，或者让它们过期。
- 方案 C：停掉驱动，把合成租户的预算恢复到跑之前的值。
- 迁移和镜像都不动。

## 5. 跑前和跑后的只读核对

**跑前**（全部只读）：

1. 部署时限：`jq '.effect_timeout_ms // 2000' <私有 agent-host 运行配置>`，同 `run-nx064-effect-request-metrics.md` §6 第 1 步。
2. 基线样本数：按 §6 第 2 步导出 7 天的 `host_op`，跑一次门禁，记下 `samples`。11-08 那次是 0。
3. 开关状态（方案 B 和 C，在 DATA_HOST 上用只读会话 `ro`）：
   - ADR-045 故障开关没有打开（`nexloop.synthetic_fault` 的审计或状态表里没有生效的 open）；
   - 合成租户的 `real_dispatch` 是关闭的；
   - NX-056 没有正在生效的真实派发审批。
   具体的表和列，按 `docs/design/synthetic-model-fault.md` 和 `docs/design/NX-056-budget-approval.md` 里写的取，只用 select。
4. Host 的 profile：`runtime_profile` 是 `deterministic-test`（方案 A 和 B），或者负责人确认过的真模型和费用上限（方案 C）。
5. 负载：1 分钟负载低于 10。

**跑后**：

1. 导出样本：
   - 方案 A：`<结果目录>/process_samples.jsonl`。
   - 方案 B 和 C：只导出本次的时间段，用 `window_start between <开始> and <结束>`。
2. 判定：

   ```bash
   python3 scripts/acceptance/nx064_p99_gate.py --samples <文件> --limit-ms <跑前第 1 步的值> --since <开始，带时区> --until <结束，带时区>
   ```

   需要时，再用 `--operation effect_find` 或 `--operation incentive` 各跑一次。
3. 结果：
   - `samples ≥ 1000` 而且是 `pass` 或 `fail`：按 §6 的“证据”一段处理。
   - 仍然是 `insufficient`：记“未测”，并附上实际 `samples`。
4. 隔离复核：负载期间 real 租户的 intent、outbox 和派发计数没有变化；ADR-045 开关和 NX-056 窗口在整个期间都是关闭的。
5. 清理：方案 B 的额外 Host 已经停掉，配置也已撤销。

**证据**只记这几项，不放原始文件：门禁输出的那一行 JSON、样本来源（方案 A、B 还是 C）、profile、时长、费用（方案 C）、跑后核对的结果。
