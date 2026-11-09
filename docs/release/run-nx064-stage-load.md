# 运行单：在 stage 上攒够 NX-064 的 p99 样本（备用）

- **状态**：备用。负责人对“NX-064 的耗时样本怎么补”的答复是“先等”。这份单子先写好，**跑不跑由负责人决定**。
- **类别**：B 类（消耗预算）。**执行者是调度员**；开跑前需要负责人在会话里明确确认；判定者是 L6。
- **目的**：11-08 的门禁结果是 `insufficient`，`host_op` 0 个，`guard_op` 2 个。本单的目标是让 `nx064_p99_gate.py` 读到**至少 1000 个 `host_op`/`effect_submit` 样本**，然后按 `run-nx064-effect-request-metrics.md` §6 判定。
- **复用**：负载形状沿用批次 122 HEAVY 回归用的 NX-031 矩阵 `deploy/load/matrix.v2.json`：门禁格的并发等于部署的 `maximum_active_runs`，见 `tests/test_nx064_effect_load_pg.py`。驱动和收集沿用 `scripts/load/driver.py`、`scripts/load/collect.py`。
- 本单不出现主机名和地址。主机用角色名：CI 主机、APP_HOST、DATA_HOST。

## 0. 先说清楚的事实

1. **HEAVY 回归本身不产生 `host_op`。** `test_nx064_effect_load_pg.py` 模仿 Host 的请求方式（`submit_like_the_host`），压的是 guard 和 SQL 这一侧，只会留下 `guard_op`。`host_op` 只在真正的 Agent Host 发出效果请求时才写，见迁移 0405。所以要攒 `host_op`，必须让真实的 Host 进程跑带效果工具的 Run。
2. **Host 的两种模型 profile**：`deterministic-test` 不调用模型、没有费用，`deepseek-flash` 是真模型、按 token 计费。负载用的确定性协议，比如 `deterministic_protocols`、`deterministic_latency`（D5 注入的模型延迟），**只在 `deterministic-test` 下才被接受**，见 `apps/agent-host/src/runtime-host.ts` 的配置检查。
3. **默认方案是 deterministic-test**（§1 方案 A）。模型费用是 0；p99 衡量的是 Host 到 guard 的效果请求，与模型无关，用真模型没有必要。
4. **stage 的真模型 Host 服务的是 world real**。上面的“合成租户”也在 real 里，所以用它跑负载就违背“只用 sim/test、不碰 real”。这条路不在本单里（见 §1 方案 C）。

## 1. 方案与选择

| 方案 | 在哪里跑 | 样本来源 | 费用 | 结论 |
| --- | --- | --- | --- | --- |
| **A（推荐）** | CI 主机的负载车道。`scripts/load/matrix.py` 克隆模板，起一套独立的栈，Host 用 `deterministic-test` | 这一格结果目录里的 `process_samples.jsonl`，即 §6 第 5 步 | 0（模型）；只占 CI 主机的时间 | 不碰 stage，也不碰 real |
| B | stage 上起一个**额外的** `deterministic-test` Host，只服务 sim 或 test world 的合成租户 | stage 上 `runtime.nexloop_process_samples` 的 `host_op` 行 | 0（模型）；stage 资源 | 要先确认 §2 的前提；任何一项不满足就退回方案 A |
| C | **负责人另定**，不在本单里 | — | — | 用 stage 现有的真模型 Host，会在 **world real** 里产生 Run 和效果请求（派发全部是模拟的）。这和 §2 的隔离要求冲突，所以要单独写一份运行单、单独送审，本单不执行 |

**方案 A 样本来源的局限（L6 C5）**：样本来自“CI 负载车道，deterministic-test”，**不代表 stage 的网络**，也不代表 stage 的负载。证据里照这句话写。**负责人没有明确确认接受这个来源之前，不能用方案 A 的结果去定 NX-064 的 `effect_timeout_ms`**；在那之前，方案 A 的门禁输出只作为参考记录。负责人不接受的话，用方案 B。

**方案 A 和部署互斥（L6 C4）**：CI 主机和 APP_HOST 是同一台机器。方案 A 不能和 stage 的 gate 或 promote 在同一时段运行，**部署窗口里不跑负载**。开跑前，先向调度员的部署队列确认没有正在进行或排定在负载时段内的 gate 或 promote；负载期间如果需要部署，先停掉负载（§4）。

## 2. 隔离（所有方案都必须满足）

- **只用 sim 或 test world 的合成租户和合成顾客**，不碰 world `real` 的任何租户、顾客、Action 或凭据。
- **不触发真实派发**：
  - 方案 A 的栈由 `scripts/load/stack.py` 起，里面固定是 `REAL_DISPATCH_ENABLED=false`。
  - 方案 B 开跑前，要只读确认没有打开的真实派发窗口，并且额外 Host 是 simulation world group。查询方法见 §5 第 3 项。
- **不能和下面两件事同时开着**：
  - **ADR-045 故障开关**（`nexloop.synthetic_fault.set`，见 `docs/design/synthetic-model-fault.md`）。它会让注入的 Run 失败，样本就不能代表正常耗时。
  - **NX-056 的真实派发窗口**，也就是审批通过、正在生效的真实派发。负载期间所有派发必须是模拟的。
  - 任何一项开着，就不开始；负载期间任何一项被打开，就按 §4 停止。
- **方案 B 的额外 Host 只能是 simulation world group 的 Host**，按 `deploy/stage/compose.simulation.example.yaml` 部署。这种 Host 拒绝所有 real 和 test 的 Run 命令（`runtime-host.ts`：`world_group` 只能是 `real` 或 `simulation`），所以方案 B 只用 simulation world 的合成租户。开方案 B 之前，三项确认都要做到，**做不到任何一项就不开方案 B**，退回方案 A：
  1. 它的私有运行配置里 `world_group` 是 `"simulation"`，`runtime_profile` 是 `"deterministic-test"`，没有 `model_configuration_file`，也没有 `synthetic_model_fault`（只读：`jq '{world_group,runtime_profile,model_configuration_file,synthetic_model_fault,host_id}' <它的私有运行配置>`）。
  2. 它的 `host_id` 已经登记在 `authz.nexloop_sim_hosts` 里，而且是 active：`ro "select host_id, active from authz.nexloop_sim_hosts where host_id='<HOST_ID>'"`，必须返回一行，active 为 true。real 的 Host 不登记在这张表里。
  3. 它挂载的 secrets 只有 compose 文件里列出的 `sim_*` 那几项（只读：`docker inspect` 它的容器，看 Mounts 的名字；不读内容），没有 real Host 的传输密钥、guard 密钥或 Run 凭据。

## 3. 时长与预算

- **样本数**：门禁要求 `samples ≥ 1000`。目标设为 **1500**，留出余量，因为每分钟一份增量，跑到最后可能不满一分钟。
- **时长**：
  - 方案 A：一个门禁格的窗口是预热加稳态，按 `matrix.v2.json` 那一格的设定（`--timing-scale 1.0`，车道上不能缩短）。跑完一格，样本不够 1500 就再跑一格，**最多 3 格**。3 格后还不够，就停下来，报告实际样本数。
  - 方案 B：按“效果 Run 每分钟数 × 分钟数 ≥ 1500”来估。**上限 2 小时**，到时间不管样本数多少都停。
  - **方案 A 多格合并（L6 C3）**：把各格的 `process_samples.jsonl` 拼成一个文件，**只跑一次门禁**，不要每格各跑一次再挑结果。各格的时间窗口不能重叠，用每格的开始和结束时间核对。中止的格，也就是结果里 `completed: false` 的格，不计入。证据里写明计入的格数和每格的样本数。
- **预算**：
  - 方案 A 和 B 的模型费用是 0。开跑前只读确认 Host 配置里 `runtime_profile` 是 `deterministic-test`，而且没有 `model_configuration_file`。

## 4. 停止条件与回退

满足下面任何一条，就立即停止注入负载（方案 A 是停掉 matrix 进程；方案 B 是停掉驱动），然后按 §5 做跑后核对：

1. 出现任何 world `real` 的写入、派发或效果，包括 outbox 和 real 租户的 intent 计数变化。
2. ADR-045 故障开关或 NX-056 真实派发窗口被打开。
3. stage 告警进入 firing。负载本身引起的预期告警，要事先列出来，才能不算。
4. 方案 A 期间，部署队列里出现了要在这个时段执行的 gate 或 promote（C4）。
5. 1 分钟负载超过 12（CI 主机或 APP_HOST）；或者现有服务出现变化，即 `existing_services_unchanged` 失败（见负载车道的检查）。
6. 到了 §3 的时长上限。

**回退**：
- 方案 A 没有需要回退的东西。车道克隆在 `--lane-root` 下面，matrix 跑完或中止后会自己删掉；中止的格子会写 `completed: false`。
- 方案 B：停掉额外的 Host，撤销它的配置，再删掉合成租户的负载会话，或者让它们过期。
- 迁移和镜像都不动。

## 5. 跑前和跑后的只读核对

**跑前**（全部只读）：

1. 部署时限：`jq '.effect_timeout_ms // 2000' <私有 agent-host 运行配置>`，同 `run-nx064-effect-request-metrics.md` §6 第 1 步。
2. 基线样本数：按 §6 第 2 步导出 7 天的 `host_op`，跑一次门禁，记下 `samples`。11-08 那次是 0。
3. **开关状态**（L6 C2）。在 DATA_HOST 上用只读会话 `ro` 执行；`ro` 的定义同 `run-nx057` §0.3，输出第一行是租户 ID。`$TENANT_ID` 是 stage 的业务租户。方案 A 也要查，因为它要求 ADR-045 开关和 NX-056 窗口都没有开着：
   - **ADR-045 故障开关**，查法同 `run-synthetic-model-fault.md`：

     ```bash
     ro "select runtime.nexloop_metrics_snapshot('$TENANT_ID','real')->'synthetic_fault'"
     ```

     `active` 必须是 0（或者不存在这个块）。不是 0 就不开始。
   - **真实派发开关**，查法同 `run-outreach-slice5-stage.md` P8：

     ```bash
     ro "select world, scope_kind, scope_ref, enabled, data_mode, revision from control.nexloop_real_dispatch_switches order by 1,2,3"
     ```

     原样记进证据。方案 A 不读这张表，车道里固定是 `REAL_DISPATCH_ENABLED=false`。方案 B 只跑 simulation world，real 的开关不受影响；这里记录读数，是为了跑后核对“负载前后没有变化”。
   - **NX-056 的真实派发审批**：不能有 `real_dispatch.set` 正在等待审批，也不能有已经批准但还没消费的请求：

     ```bash
     ro "select r.world, r.status, r.action_resource, r.due_by, c.result from control.nexloop_approval_requests r
          left join control.nexloop_approval_consumptions c using (tenant_id, world, approval_id)
          where r.tenant_id='$TENANT_ID' and r.action_resource like 'eios:action:nexloop.real_dispatch.set:%'
            and (r.status='waiting_approval' or (r.status='approved' and c.approval_id is null)) order by r.requested_at"
     ```

     必须是 0 行。有的话，说明随时可能打开真实派发窗口，就不开始。
4. Host 的 profile：`runtime_profile` 是 `deterministic-test`（方案 A 和 B）。方案 B 还要满足 §2 的三项确认。
5. 负载：1 分钟负载低于 10。

**跑后**：

1. 导出样本：
   - 方案 A：`<结果目录>/process_samples.jsonl`。
   - 方案 A：各格的文件按 §3 拼接成一个文件（C3）。
   - 方案 B：只导出本次的时间段，用 `window_start between <开始> and <结束>`。
2. 判定：

   ```bash
   python3 scripts/acceptance/nx064_p99_gate.py --samples <文件> --limit-ms <跑前第 1 步的值> --since <开始，带时区> --until <结束，带时区>
   ```

   需要时，再用 `--operation effect_find` 或 `--operation incentive` 各跑一次。
3. 结果：
   - `samples ≥ 1000` 而且是 `pass` 或 `fail`：按 §6 的“证据”一段处理。
   - 仍然是 `insufficient`：记“未测”，并附上实际 `samples`。
4. 隔离复核：把跑前第 3 项的三条查询再执行一遍，结果和跑前一致；负载期间 real 租户的 intent、outbox 和派发计数没有变化。
5. 清理：方案 B 的额外 Host 已经停掉，配置也已撤销。

**证据**只记这几项，不放原始文件：门禁输出的那一行 JSON；样本来源，方案 A 照写“CI 负载车道，deterministic-test，不代表 stage 的网络”；方案 A 计入的格数和每格样本数；profile；时长；跑前和跑后核对的结果。方案 A 的结果写明负责人有没有确认接受这个来源（C5）。
