# NX-041 Host 的 sim 上下文输入协议

依据：S7b 契约 `nexloop.context-pack.sim.v1`，L6 `nx041-s7b-review` 第 3.4 节。起点 bbea551a。迁移 `0269_nx041_host_sim_context.sql`（编号是临时的，只出现在文件名和 catalog 里）。

## 实现

| 内容 | 位置 |
| --- | --- |
| 解析函数 **`parseSimContextPack(input, command)`**，定义在 `apps/agent-host/src/sim-context.ts`，`context-input.ts` 原名重新导出（L3 的 `pi_run_blockers` 探测用这个名字）。<br>· 按契约逐项校验：每个键、常量、格式和上限；每个条目都必须是 `source: simulated`；`budget_report` 的计数必须自洽。<br>· 再与 Run 命令对照：world 和 tenant 必须相同，且 world 的种类与 mode 一致。<br>· 不满足任何一项就抛 `runtime_context_invalid`，v1–v6 的包也会被拒。<br>同一模块还导出 `CONTEXT_PROTOCOL_SIM`、`renderSimContextPack`、`declaresSimContextPack` | Host |
| **1. sim 组只收 sim 包**：simulation 组的 Host 必须配置 `context_input_protocol: nexloop.context-pack.sim.v1`，并且不能启用多协议模式和其他确定性输入选项。start 和 resume 时先解析 sim 包，在准入、guard 请求和读文件之前就校验，不通过返回 `invalid_sim_context`（HTTP 400，不重试） | `runtime-host.ts` |
| **2. real 组拒收 sim 包**：不管配置了哪种 context 模式（包括没配置），real 组只要看到输入声明了 sim 包，就返回 `invalid_sim_context`；real 组也不能配置成 sim 协议。v1–v6 的校验函数遇到 sim 包同样直接拒绝 | `runtime-host.ts`、`context-input.ts` |
| **3. 渲染**：模型读到的不是原始包，而是 `renderSimContextPack` 的输出：<br>· 第一行是固定前言“[模拟环境 SIMULATION] 以下全部是模拟输入（source: simulated）……不是任何真实客户的对话、事实或承诺”；<br>· 之后每一行都以 `[模拟][source: simulated]` 开头。<br>· 依次列出：world 与世界时间、当前事件、消费者状态（profile、persona）、模拟对话（序号和标签）、goal、supply、relationship、insufficient、budget_report。<br>· 渲染结果是确定的，因此 resume 时的幂等摘要不变 | `sim-context.ts` |
| **缺 goal、supply、relationship 段：选择“渲染为空段”**。sim 契约本来就没有这三段。渲染时各写一行“空：模拟包不含…段（不从真实…补全）”，明确告诉模型这些内容不存在，也不会被补上。另一个选项是报 insufficient，但这三段是契约层面就没有的，并不是“数据不足”，所以没有选。包自带的 insufficient 代码（`no_simulated_events`、`older_events_omitted`）照常列出 | `sim-context.ts` |
| **4. 不回退 v6**：<br>· sim 组配置成 v6、v2 或不配置协议，都会在启动时被拒；<br>· sim 包不合法时直接拒绝，没有任何代码路径会改用 v6；<br>· v6 的校验函数也拒收 sim 包 | 同上 |
| **装配调用**：`nexloop_eios.sim_context.assemble_sim_context` 和 `sim_run_input` 用该 world 自己的凭据调用 S7b 的 `authz.nexloop_sim_context_command`，结果作为 sim Run 登记时的输入。传入 real 或 test 世界时，在调用数据库之前就拒绝。real 组的装配路径一字未改 | Python |
| **sim 角色**：这个分支的基线 bbea551a 还没有 S6c，所以 0269 只有一个条件块。存在 `authz.nexloop_app_role` 时（即集成后，S6c 先执行），它把 S7b 入口的角色判断改为 `app_role(p_world)`，并授权 `nexloop_sim_api`。在这个分支上该块不会执行，入口保持 S7b 原样 | 0269 |

## 完整性

sim Run 不经过 real 的 context artifact 绑定，那套绑定只支持 real 世界。sim Run 的输入在登记时写入 activation，start 时 guard 会核对输入摘要，这就是完整性依据。Host 对每次 start 和 resume 都重新做严格校验。

## 测试

- Host（Node 24，重建 dist 后串行）`test/nx041-host-sim-context.test.ts`：
  - 协议模块按名字导出 `parseSimContextPack`；
  - 解析函数的 10 个负例；
  - 四个验收项各配一个负例，第 1 项另有一个正例，第 3 项另有一条实际调用测试，确认交给 adapter 的是渲染后的文本，不是原始包。
- PG `tests/test_nx041_host_sim_context_pg.py`：
  - 用数据库装配出的真实 sim 包，交给本仓库构建出的 Host 模块（Node 24）解析和渲染：每一行都标了模拟，三个空段齐全；
  - 同一个解析器拒绝其他 world、real 命令和 v6 包；
  - 装配函数遇到 real 或 test 时，不碰数据库就拒绝；
  - 条件块在没有 S6c 时不生效；
  - 用 S6c 接口的替身实际执行一遍条件块：`nexloop_sim_api` 能在 sim 世界调用入口，在 real 世界被拒，real 角色不受影响。

## 遗留

- S6c 集成后：S6c 的白名单测试要给 `nexloop_sim_api` 加上 `authz.nexloop_sim_context_command(text,text,text,integer)`。
- sim 的事件入队服务（使用 `nexloop_sim_api`）由 S3 和孪生侧接入，届时调用 `sim_run_input` 生成 Run 输入。
