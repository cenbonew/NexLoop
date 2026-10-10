# NX-031 运行 1000 消费者与并发矩阵：设计稿（D1–D9 全部定案；未实现）

分支 `nx031-design`，从 main `a5d7c48` 切出。本稿只有文档：没有代码，也没有迁移；文中文件名、函数名、配置键、数值都是占位或建议值，由 D 项拍板。NX-031 依赖 NX-029（保留/删除）与 NX-030（指标与告警），两者在实现中，实现排在它们合入之后。

## 0. 依据与结论

**依据**（行号以 main `a5d7c48` 为准）：
- 任务（`planning/tasks.json` NX-031，S4，模块 M14/M16/M22）：交付“固定 seed 数据、1/2/4/8 并发、CI 同机影响报告”。
- 验收 **AT-060“负载与隔离”**（M14，S4）：1000 消费者、并发 1/2/4/8；测平台 / 模型 / 工具分解延迟，无身份串用。
- PRD 性能目标：ACK p95 ≤500 ms，空闲队列派发 p95 ≤2 s（NX-030 设计稿 §0 引 `01_PRD.md:332`）。
- ADR-022 §4：
  - 2 s 工具时限不变；
  - “生产 Agent Host 全局并发 ≤4、Runtime Worker guard 4 个进程”是部署前提，由 doctor 核对；
  - 生产并发要提高时，先在对应并发下重测门槛。
- NX-049：
  - Host 并发上限 `maximum_active_runs` 默认 4，代码允许 1..64；
  - doctor 写死 ≤4（`host_concurrency.MAXIMUM_HOST_ACTIVE_RUNS`）；
  - 排队等待 500 ms 后返回 503 `runtime_capacity_exhausted`（`NX-049-host-concurrency.md` §1–3）。
- 部署（`11_DEPLOYMENT.md`）：
  - 应用连接池合计 ≤60，`max_connections` ≥80（§6，`connection_budget.py` 写死）；
  - 资源初值：API 2、Domain Worker 1、Action Worker 1、Scheduler 1、Agent Host 1 进程且全局并发 4，同一时间只跑 1 个 CI 构建；
  - **压测报告要保存背景 CI 是否在运行**（§7）。
- 本地 CI（`12_LOCAL_CI_AND_RELEASE.md` §9）：
  - 默认同一时间 1 个 CI build；
  - 运行服务的资源优先，后台演进和全量测试可以暂停。
  - `scripts/ci/check` 现有阶段：Phase 1 串行构建，Phase 1b 是 ADR-022 的串行时限阶段（guard 4 进程），Phase 2 按测试并行。
- 主机（`docs/s0/server-inventory.md`）：APP_HOST 16 逻辑 CPU、13 GiB 内存；DATA_HOST 8 CPU、15 GiB。
- NX-030（分支 `nx030-impl` `cf9adf3`，未合入）：
  - 指标快照 `runtime.nexloop_metrics_snapshot(tenant, world)`，包含 refusals、effects、replies、commitments、queues、feeds、extraction、guard、host、pool、connections、cost、evaluator；
  - 派发拒绝表 `runtime.nexloop_dispatch_refusals`；
  - 进程样本表 `runtime.nexloop_process_samples`（guard 时延与 2 s 超时、连接池），每分钟一行；
  - 告警评估器只评估 world `real`；
  - **M09 Agent Host 并发样本没有实现**（实现说明 §4 第 1 项），`host` 区块为 unavailable。

**结论**：
1. **在一次性集群里跑，只用合成数据，不碰持久库、真实渠道和真实模型。**
   - `REAL_DISPATCH_ENABLED=false`；
   - effect provider 用回环；
   - 模型用确定性 provider，并注入时延分布（D5）。
2. **固定 seed 生成 1000 个消费者及其历史。**
   - 历史装载一次后做成模板库，矩阵的每个格子都从模板克隆一份全新的库，保证各格起点完全相同。
3. **并发矩阵 8 个格子**，D2 是建议的最小覆盖集：
   - 核心轴是 Agent Host 全局并发 1/2/4/8；
   - 并发 4 上再扫入站速率、后台负载开关；
   - 另加一个“同机跑全量 CI”的干扰格。
4. **指标全部复用 NX-030 的快照和样本**；压测驱动端只补客户端视角的 ACK 时延和端到端时延，以及“无身份串用”的核对。
5. **门槛只在部署前提格上判定**（并发 ≤4、名义速率、后台开启、无 CI）。
   - 并发 8 格和 CI 干扰格只观察、只记录；
   - 不达标时只记录、定位，不放宽 2 s 时限，也不改部署前提（§7）。
6. **先修一个前置问题：连接预算没有登记后台服务**（§4.3）。不修就无法在“合计 ≤60”的前提下跑满后台 worker。

## 1. 范围

**做**：
- 合成数据生成器与装载；
- 入站负载驱动；
- 矩阵运行器；
- 每格指标采集与汇总；
- 身份串用核对；
- 报告（含 CI 同机影响）；
- CI 车道入口；
- 普通 CI 里的小规模冒烟（保证脚本本身不腐烂）。

**不做**：
- 真实渠道或真实模型的压测；
- 生产库或 stage 库上的任何操作；
- 提高部署并发前提（那需要 ADR，见 ADR-022 §4.4）；
- 性能优化本身（不达标时另开任务）；
- 双机部署（NX-032）。

## 2. 合成数据：生成与装载

### 2.1 原则
- **固定 seed**：
  - 生成器 `scripts/load/synthetic.py --seed <int> --consumers 1000`，同一 seed 和同一提交得到逐字节相同的数据清单（JSON Lines，含 digest）；
  - 报告里记录 seed、清单 digest 和代码提交。
- **只用合成数据**：
  - 租户 id 前缀 `synthetic-load-`；
  - 文本由固定中文模板加编号拼成，不含任何真实客户信息；
  - 模板词表随代码提交。
- **world 与 data_mode**（D1）：
  - 对话、Run、effect、承诺、工作台读取这些链路只在 world `real` 下才完整运行：消息读派生 0080 要求 `real`，工作台读端口 0117 要求 `real`，告警评估只评估 `real`。
  - 因此建议在**一次性集群的合成租户**里使用 world `real`；
  - 商业事件按 NX-027 D1 用 test 连接器写入：`data_mode=test`，world `test`。
  - 安全靠下面四点保证，而不是靠 world 标签：
    - `REAL_DISPATCH_ENABLED=false`；
    - effect provider 用回环；
    - 模型用确定性 provider；
    - 集群是一次性的。

### 2.2 数据构成（建议值，D2 一并确认）

| 项 | 数量 / 比例 | 用途 |
|---|---|---|
| 消费者（Consumer 对象 + 浏览器身份 + 会话） | 1000 | AT-060 的规模 |
| 每个消费者的会话 | 1–3（seed 决定） | 会话路由、接管范围 |
| 历史消息 | 平均 20 条/人，约 2 万条 | 上下文与提取的真实负担 |
| 明确拒绝联系（命中 0109 规则） | 8% | NXC05 派发拒绝、来信必回 |
| 未结束的承诺 | 20% | keeper 负载、承诺异常 |
| 活跃计划（NX-024） | 30% | 复评负载 |
| 商业事件（test 连接器） | 40% 的消费者，每人 1–4 条 | commercial-recorder 负载 |
| 工作台成员 | owner 1、operator 2 | 负载期间的读取与接管 |

### 2.3 装载
- **只走受治理入口**，不直接写业务表：
  - 消费者和会话经现有的受治理写入；
  - 消息经原生 WebChat 入站（`/api/v1/conversations/{id}/native-messages`，每个消费者用自己的浏览器会话）；
  - 商业事件经签名 webhook；
  - 成员和角色经 configurator。
  - 唯一的例外是一次性集群的 bootstrap 和可信配置（与 CI 相同）。
- **装载一次，做成模板**：
  - 装载完成后，等后台 worker 排空：提取、匹配、索引、keeper、商业记录都处理完，snapshot 显示各 feed pending=0；
  - 再停服，把该库作为模板（`createdb -T`）；
  - 每个格子从模板克隆，Artifact 根目录同样复制。
  - 模板的 key 包括 seed、清单 digest、迁移 catalog digest 和代码提交，任何一项变化都要重新装载。
- **登录预热**：1000 个浏览器会话的密码校验（argon2）在每格开始前的预热阶段完成，不计入测量。
- 装载耗时估算约 20–40 分钟（2 万条消息，主要耗在提取和匹配排空），实测后写进报告。

## 3. 负载驱动与并发矩阵

### 3.1 入站负载
- 驱动 `scripts/load/driver.py`，单进程 asyncio，只连 API（HTTPS，回环），不连数据库。
- 到达过程：泊松到达，速率为 λ（D2）；消费者按 Zipf 分布选择（s=1.1，少数客户特别活跃），全部由 seed 决定。
- 消息内容按模板比例生成：
  - 普通咨询 60%；
  - 会产生承诺的请求 15%；
  - 明确拒绝联系 3%；
  - 对受限客户的来信 5%（应走来信必回或兜底，不能主动联系）；
  - 同一请求重放 2%（同一 `provider_event_id`，检验去重）；
  - 同一客户 1 秒内连发 3 条，占 5%（检验会话内顺序和 Run 合并）。
- 负载期间的工作台操作（合成成员）：每分钟 1 次总览读取，每 10 分钟 1 次接管加员工回复再交还，覆盖 NX-028 与 ADR-025 读取在负载下的表现。

### 3.2 矩阵

固定不变的维度：
- guard 4 个进程（`NEXLOOP_TEST_GUARD_WORKERS=4`，部署形态）；
- API 2 进程；
- 各后台服务 1 进程；
- 连接预算合计 ≤60（§4.3）。

| 格 | Host 全局并发 | 入站速率 λ | 后台 worker | 同机 CI | 角色 |
|---|---|---|---|---|---|
| C1 | 1 | 名义 | 开 | 无 | 观察（并发曲线） |
| C2 | 2 | 名义 | 开 | 无 | 观察 |
| **C4** | **4** | **名义** | **开** | **无** | **门槛格（部署前提）** |
| C8 | 8 | 名义 | 开 | 无 | 观察：超出部署前提（D3） |
| C4-low | 4 | 低 | 开 | 无 | **门槛格**：空闲队列派发 p95 |
| C4-peak | 4 | 峰值 | 开 | 无 | 观察：积压与排空 |
| C4-quiet | 4 | 名义 | 关（只开 runtime 链路） | 无 | 观察：后台负载对 Run 时延的影响 |
| C4-ci | 4 | 名义 | 开 | 全量 CI 同时运行 | 观察：CI 同机影响（§5.3） |

- 建议速率（D2）：低 0.2 条/秒、名义 1 条/秒、峰值 3 条/秒。名义速率约等于 1000 个客户每人每小时 3.6 条。这是假设，没有业务实测，请负责人按预期规模确认。
- 后台 worker 指以下服务（`background_services.py`），每个 1 个进程：
  - claim-extraction-scheduler/worker；
  - claim-matcher；
  - recall-indexer；
  - plan-reevaluator；
  - reply-guarantor；
  - commitment-keeper；
  - commercial-recorder；
  - NX-030 的 alert-evaluator；
  - NX-029 的 retention-keeper。
- “开”时它们按部署形态运行；`C4-quiet` 只保留消息 Run 链路所需的服务（relay、runtime worker、reply-guarantor）。
- C8 用压测专用的 Host 配置，`maximum_active_runs=8`。这份配置不经 doctor（doctor 会拒绝），只存在于压测车道，报告里明确标注“超出部署前提”。

### 3.3 每格的时间结构
- 克隆模板、启动服务、登录预热：约 3 分钟，不测量。
- 预热负载 2 分钟，不测量。
- **稳态 10 分钟**：测量窗口。NX-030 的样本按分钟写入，10 分钟才有足够样本算 p95。
- 停止入站后的**排空期**：观察积压降到 0 的时间，最长 10 分钟，超时记为“未排空”。
- 收集结果、销毁集群：约 2 分钟。
- 合计每格约 25 分钟；8 格加一次模板装载，约 4 小时（§5.2）。

## 4. 测量

### 4.1 指标（每格；稳态窗口内的值，以及排空期的值）

| 指标 | 来源 | 说明 |
|---|---|---|
| 2 s 工具时限超时率；guard p50/p95/max | NX-030 M08：`process_samples` 中 kind=guard | 按调用类型分列；超时判定与运行时的 2 s 截止相同 |
| 派发拒绝，按原因码 | NX-030 M01：`nexloop_dispatch_refusals`（快照中的 `refusals`） | 预期内的拒绝（NXC05 等，保护在生效）与非预期的拒绝分开列 |
| 积压：pending 数、最老年龄、retry_wait | 快照中的 `queues` / `feeds` / `extraction` | 每 30 秒取一次快照，画曲线；排空时间单列 |
| 死信 | 快照中的 `queues.dead_lettered` 与 `feeds` | 门槛格要求为 0（D4） |
| Host 并发与排队：activeRuns、waiting、`runtime_capacity_exhausted` 次数 | NX-030 M09（未实现，§8 依赖 1） | 实现之前，驱动端改用 runtime worker 的 `runtime_transport_unavailable` / `retry_wait` 次数近似，并在报告中标注 |
| 连接：总数、各池 requests_waiting、`BackendBusy` | NX-030 M10 和快照中的 `connections` | 要求总数 ≤60（预算内）；另记峰值 |
| ACK 时延 p50/p95/p99 | 驱动端：从 POST 到 202 | PRD：p95 ≤500 ms |
| 空闲队列派发时延 p95 | 消息 accepted_at 到对应 Run 任务被 claim（`runtime.jobs`） | PRD：≤2 s；在 C4-low 判定 |
| 端到端：来信到回复（或兜底 / 升级）落库 | 会话流中 accepted_at 的差值 | 分解见 §4.2 |
| 告警 | NX-030 告警事件 | 门槛格不应出现 critical（D4），出现的全部列出 |
| 资源 | 主机 CPU、内存、负载、IO 等待（每 10 秒取一次 `/proc`） | 只记录，不判定 |

所有指标只含代码、计数、时延和 ID，不含原文。快照函数本身保证这一点，驱动端也只记录 ID。

### 4.2 平台 / 模型 / 工具 分解（AT-060）
- **模型**：`runtime.nexloop_model_requests` 每次调用的 `completed_at − requested_at`，按 Run 求和。
- **工具**：guard 时延样本（M08，按调用类型的聚合分布）。现在没有逐 Run 的工具耗时：要么只报聚合，要么由 NX-030 在 Run 结束摘要里加工具耗时之和（D6，建议后者，作为 NX-030 的小补充）。
- **平台**：端到端减去模型和工具，包含排队、准入、SQL 和中继。另外单列队列等待（任务 created 到 claimed）和准入等待（Host 排队）。
- 报告按格给出三段的 p50/p95，看并发从 1 到 8 时哪一段增长。

### 4.3 前置问题：连接预算没有登记后台服务
- `deploy/stage/connection-budget.v1.json` 现在只登记了 api、domain-worker、action-worker、scheduler、runtime-worker，按配置合计 38。
- `background_services.py` 的每个后台服务都是独立进程，各自 `open_backend(pool_max_size=4)`；plan-reevaluator 和 reply-guarantor 还各开一个 API 池。按代码默认值估算：
  - 现有 8 个服务加 NX-029、NX-030 新增的 2 个，共 10 × 4 = 40；
  - 加上那两个 API 池 2 × 4 = 8；
  - 再加原有的 38，合计约 86 > 60。
- **这是按代码默认值做的估算**，实现时要在一次性集群上按 `application_name` 统计 `pg_stat_activity` 核实。NX-027 设计稿 §295 已写明要登记，但 main 的预算文件还没有这几项。
- 处理建议（D7）：
  - 后台服务的池上限降为 2（NX-018 实测单请求同时最多持有 2 个连接）；
  - 两个 launcher 的 API 池降为 1；
  - 全部登记进预算文件，doctor 照常核对。
  - 估算合计 38 + 10×2 + 2×1 = 60，正好卡在上限。
  - 如果核实后超过 60，备选是：domain-worker 那一项若与后台服务重复则合并，或者把 API 降为 1 个进程并另行实测。
- 这一步在 NX-031 实现的切片 0 完成，并与 NX-029、NX-030 协调（它们各新增一个后台服务）。

### 4.4 无身份串用（AT-060，门槛项）
每格结束后，用只读查询加采样探针核对：
1. **Run 绑定**：每个消息 Run 的 context pack 中，consumer 与触发消息所属会话的 consumer 一致。只比较 ID。
2. **外发归属**：每条外发消息（Agent、兜底、员工回复）都与其 trigger 消息在同一会话、同一 consumer。
3. **Claim 归属**：每个 Claim 的 consumer 与其来源消息所在会话一致。
4. **模型请求上下文**：每次模型请求的 context 清单里出现的对象 ID，只属于该 Run 的 consumer 或租户级对象。逐条比对 context manifest，只比 ID 不看原文。
5. **越权探针**：负载期间随机抽 50 对 (A, B)，用 A 的会话读 B 的会话和消息，必须是 403 或 404；工作台 operator 读审计必须是 403。
6. **副作用不重复**：同一业务意图的 effect 只有一次被渠道接受（AT-037 的口径），重放的入站请求不产生第二个 Run 或第二条回复。
7. **已接受的工作不丢**：稳态和排空期结束时，每条已接受的入站消息都有确定的结局：
   - 已回复；
   - 已结清（受限、接管或兜底）；
   - 已升级；
   - 或明确列为“仍在处理”并说明原因。

第 1–6 项任一违例数大于 0，AT-060 判为不通过。第 7 项计入门槛（D4）。

## 5. 在哪里跑

### 5.1 一次性集群（CI 车道）
- 在 CI 主机（APP_HOST）的 CI 车道上运行，入口是 `scripts/ci/load-lane`，独立于 `scripts/ci/check`。每个格子新建以下资源，跑完销毁：
  - PostgreSQL 实例：一次性 PGDATA，端口随机，`max_connections=100`，满足预算的 ≥80 要求；
  - Valkey；
  - Artifact 根目录；
  - Agent Host；
  - 各服务进程。
- **不碰持久库**。车道脚本启动前检查以下各项，任一不满足就拒绝运行：
  - DSN 指向的实例必须是本车道刚刚创建的（PGDATA 在车道临时目录下，`system_identifier` 与创建时记录的一致）；
  - 不读取任何部署用的 env 文件；
  - `REAL_DISPATCH_ENABLED` 必须是 false；
  - provider 配置必须指向回环；
  - 模型 provider 必须是确定性 provider。
- 报告只写入车道的结果目录（`.ci-results/load/`，已在忽略列表），提交进仓库的只有不含原文的汇总报告（D8）。

### 5.2 用时与错峰
- 一次完整运行约 4 小时：模板装载约 40 分钟，8 格各约 25 分钟。
- **与常规 CI 错峰**：
  - 车道启动时取得 CI 资源锁（`12_LOCAL_CI` §9，同一时间 1 个 build），整个运行期间常规 CI 排队等待；
  - 只有 C4-ci 这一格会在锁内主动启动一轮全量 CI 作为干扰；
  - 建议安排在夜间窗口（D9）。
- 运行频率（D9）：不进每次 CI。在以下时机各跑一次：
  - AT-060 首次验收；
  - NX-032 部署前；
  - 运行时、guard、派发、连接池相关改动合入后；
  - 部署并发前提要变化时（ADR-022 §4.4）。
- **普通 CI 冒烟**：`tests/test_load_smoke.py` 用 20 个消费者、1 个格子、1 分钟，只检查生成器能复现、驱动和采集能跑通、身份核对的 7 项都能执行，不判定性能。

### 5.3 CI 同机影响报告
- C4-ci 与 C4 对比：干扰下各指标的变化，例如 guard 超时率、p95 和积压。
- 反方向：同一提交上，全量 CI 单独跑与在 C4 负载下跑相比，CI 墙钟时间和 B 类（时限敏感）失败数的差异。单独那一轮直接引用当天最近一次常规全量 CI 的结果，在报告中注明 run id。
- 报告固定记录每格是否有背景 CI 在运行（`11_DEPLOYMENT` §7）。

## 6. 通过门槛与对应 AT

### 6.1 门槛（只在门槛格 C4、C4-low 上判定，D4）

| 项 | 门槛（建议） | 依据 |
|---|---|---|
| 无身份串用（§4.4 第 1–6 项） | 0 | AT-060、AT-037 |
| 已接受的工作不丢（§4.4 第 7 项） | 0 条没有结局 | AT-030 的口径扩展到负载下 |
| 2 s 工具时限超时率 | 任一 5 分钟窗口 ≤1% | 与 NX-030 告警阈值一致；2 s 本身不变 |
| guard p95 | ≤1500 ms | NX-030 告警阈值，即逼近 2 s 的提前量 |
| 死信 | 0 | NX-030 告警：任一新增即告警 |
| ACK p95 | ≤500 ms | PRD |
| 空闲队列派发 p95（C4-low） | ≤2 s | PRD |
| 排空时间 | 停止入站后 ≤10 分钟积压归零 | NX-030 最老 pending >10 分钟告警 |
| 连接总数 | 峰值 ≤60 | `11_DEPLOYMENT` §6 |
| critical 告警 | 0 | NX-030 |
| 非预期派发拒绝 | 0；预期内的（NXC05、NXC06 等）单独列出 | NX-030 M01 |

观察格（C1、C2、C8、C4-peak、C4-quiet、C4-ci）记录同样的指标，不判定。

### 6.2 对应 AT
- **AT-060**（本任务的验收）：所有格子都跑完、记录了分解时延，且门槛格全部达标，才是 passed。只要某一格没跑完或缺少测量，就是 failed 或 blocked，不能写成 passed。
- 只提供补充证据、不改变其状态的 AT：
  - AT-037：并发下重复副作用为 0；
  - AT-031：负载下 lease 和 fence 正确；
  - AT-030：已接受的工作不丢；
  - AT-049：负载期间所有进程日志中不出现合成哨兵原文，前提是 NX-030 的统一日志器已合入。

### 6.3 与 ADR-022 §4 部署前提的关系
- C4 用的是部署前提的形态（Host 全局并发 4、guard 4 进程），**它的门槛就是部署前提在 1000 消费者规模下是否成立的证据**。
- C8 只观察。C8 劣化是预期的，不改变前提，也不构成提高前提的依据。要提高到 8，必须另走 ADR-022 §4.4：先在 8 下重测 NX-049 的门槛并修改 ADR。
- 如果 C4 不达标，说明部署前提在这个规模下不成立。要么降低前提（例如降到 2，需改部署说明、doctor 上限和 ADR），要么另开优化任务。**由负责人决定，压测本身不做任何调整**（§7）。

## 7. 不达标时的处理

1. **只记录、定位**：
   - 报告写明哪一格、哪一项、实测值多少；
   - 用分解时延（§4.2）、guard 样本、连接池等待和锁等待（每 30 秒采样一次 `pg_locks` 的计数，只记计数和锁类型）定位到“平台 / 模型 / 工具”中的哪一段，以及具体是哪个服务或池。
2. **不放宽**：
   - 不放宽 2 s 工具时限；
   - 不降低门槛；
   - 不把门槛格改成观察格；
   - 不删测试，不跳过格子；
   - 不改部署并发前提。
3. **登记**：
   - AT-060 记为 failed，附报告作证据；
   - 每个已定位的原因另开任务，交调度员排期；
   - 同一提交上至少复跑一次失败的格子，排除偶发。复跑结果与首次结果都保留。
4. **涉及部署前提时**：按 §6.3 交负责人决定。

## 8. 依赖与前置

1. **NX-030 M09（Agent Host 并发样本）**：
   - 需要 Host 提供 `/internal/v1/metrics`，并由 runtime worker 代写样本；
   - 改动的是 L3 的 Host 文件，NX-030 实现说明列为未完成；
   - NX-031 在它完成后才能给出 Host 排队的直接测量，在此之前用近似值并在报告中标注（§4.1）。
2. **NX-030 逐 Run 工具耗时（D6）**：如果采纳，由 NX-030 补充。
3. **连接预算登记（§4.3，D7）**：NX-031 切片 0。
4. **NX-029 合入**：
   - 删除中拒绝读取会在公开入口和 v0072 层增加检查，读路径时延会变化，必须在它合入之后测量才有意义；
   - retention-keeper 作为后台 worker 一同参与负载。
5. **NX-028 合入**（s4l）：负载期间的工作台读取、接管和员工回复。

## 9. 实现切片（建议）

| 切片 | 内容 | 产出 |
|---|---|---|
| 0 | 连接预算：后台服务池上限、登记、doctor 核对；在一次性集群上实测 `pg_stat_activity` | 预算文件新版本与实测记录 |
| 1 | 合成数据生成器（固定 seed、清单 digest）、经受治理入口装载、模板化、冒烟测试 | `scripts/load/synthetic.py`、`tests/test_load_smoke.py` |
| 2 | 负载驱动、指标采集（快照、样本、驱动端）、身份核对 7 项 | `scripts/load/driver.py`、`scripts/load/checks.py` |
| 3 | 矩阵运行器、车道入口与安全检查、报告生成（含 CI 同机影响） | `scripts/ci/load-lane`、`deploy/load/matrix.v1.json` |
| 4 | 在 CI 车道完整跑一次，提交汇总报告，登记 AT-060 | `docs/implementation/NX-031-report.md` |

## 10. 与 NX-029 / NX-030 的文件重叠

| 文件 | NX-029 | NX-030 | NX-031 计划 | 协调方式 |
|---|---|---|---|---|
| `deploy/stage/connection-budget.v1.json` | 新增 retention-keeper（应登记，未登记） | 新增 alert-evaluator（应登记，未登记） | 登记全部后台服务，降低池上限（§4.3） | NX-031 切片 0 统一处理；如 NX-029 或 NX-030 先合入，由 NX-031 追加 |
| `nexloop_eios/background_services.py` | 新增 retention-keeper | 新增 alert-evaluator | 池上限改为可配置（只追加参数，默认值不变或按 D7） | 只追加；合并时以后合入者为准 |
| `deploy/community/compose.test.yaml` | background profile 追加 | background profile 追加 | 不改；压测车道用自己的进程编排，不经 compose profile | 无冲突 |
| `nexloop_eios/observability.py`、`0151_nx030_observability.sql`（临时号） | — | 快照、样本、告警 | 只读调用快照和样本；D6 需要的逐 Run 工具耗时由 NX-030 补 | NX-031 不改 NX-030 文件 |
| Agent Host `run-admission-gate.ts`、`main.ts`（L3） | — | M09 待做 | 依赖 M09；C8 只用压测配置，不改代码 | 由 NX-030 与 L3 约定后实现 |
| `nexloop_eios/retention.py`、retention 迁移 | 保留与清除 | — | 只读：合成租户使用默认保留设置 | 无冲突 |
| `deploy/configuration/alert-rules.v1.json` | — | 21 条规则 | 只读：门槛引用其中的阈值 | 阈值变化时同步更新本稿 §6.1 |
| `scripts/ci/check` | — | — | 不改；新入口 `scripts/ci/load-lane` | 无冲突 |
| `doctor.py` | 告警项 | `--observability` | 不改（C8 不经 doctor） | 无冲突 |
| 新文件 `scripts/load/*`、`deploy/load/*`、`tests/test_load_smoke.py`、`docs/implementation/NX-031-*` | — | — | 新增 | 无冲突 |

## 11. 需要负责人拍板的点（D 项）

- **D1 world 与 data_mode**：
  - 推荐：一次性集群里的合成租户使用 world `real`，跑对话、Run、effect、承诺和工作台链路，因为这些链路只在 `real` 下完整运行；
  - 商业事件按 NX-027 D1 走 test 连接器（`data_mode=test`，world `test`）；
  - 安全靠 `REAL_DISPATCH_ENABLED=false`、回环 provider、确定性模型和一次性集群来保证。
  - 备选：全部用 world `test`。但这样消息读派生、工作台和告警都不会运行，测出来的不是部署形态。
- **D2 矩阵与速率**：
  - 推荐 8 个格子（§3.2）：低、名义、峰值速率分别为 0.2、1、3 条/秒；数据构成见 §2.2。
  - 速率是假设，请按预期业务规模确认。
- **D3 并发 8**：
  - 推荐保留，交付清单写明了 1/2/4/8；
  - 用压测专用的 Host 配置，不经 doctor，只观察，报告标注“超出部署前提”，不作为提高前提的依据。
- **D4 门槛**：推荐 §6.1 表中的取值，只在 C4 和 C4-low 上判定。
- **D5 模型时延**：
  - 推荐用确定性模型 provider 注入对数正态时延，中位 1.5 s、p95 4 s，作为假设值；如果有真实模型的时延记录，改用记录值；
  - 不调用真实模型，原因是成本、隐私和可复现性。
  - 以后如需校准，另开一个小样本真实模型格，不在 v0.1。
- **D6 逐 Run 工具耗时**：推荐由 NX-030 在 Run 结束摘要中记录工具耗时之和（只记数字），让 AT-060 的分解可以逐 Run 给出。备选是只报 guard 聚合分布。
- **D7 连接预算**：推荐后台服务池上限 2、launcher 的 API 池 1，全部登记，在 NX-031 切片 0 实测核实；超过 60 时再在 §4.3 的两个备选中选。
- **D8 结果存放**：推荐仓库只提交汇总报告（不含原文：指标、门槛结论、seed、digest、提交）；原始样本放 `.ci-results/load/`（已忽略），保留 30 天。
- **D9 频率与时段**：推荐不进每次 CI，在 §5.2 列出的时机跑；时段为夜间窗口，整个运行期间持有 CI 资源锁。

### 裁定记录
- **调度员裁定（2026-10-10，均采用推荐）**：
  - D3：保留 C8。用压测专用的 Host 配置，不经 doctor；报告标注“超出部署前提”，不作为提高前提的依据。
  - D5：确定性 provider 注入对数正态时延（中位 1.5 s、p95 4 s），不调用真实模型。
  - D6：由 NX-030 在 Run 结束摘要里记录工具耗时之和（只记数字），已转 L2，作为一个小提交。
  - D7：后台服务池上限 2，launcher 的 API 池 1，在切片 0 实测核实。
  - D8：仓库只提交汇总报告；原始样本放 `.ci-results/load/`，保留 30 天。
  - D9：不进每次 CI；夜间窗口运行，全程持有 CI 资源锁。
- **负责人决定（2026-10-10，原话“全部按推荐”）**：
  - D1：一次性集群里的合成租户使用 world `real`，跑对话、Run、effect、承诺和工作台链路；商业事件走 test 连接器（`data_mode=test`，world `test`）。安全由 `REAL_DISPATCH_ENABLED=false`、回环 provider、确定性模型和一次性集群这四项保证。
  - D2：速率按低 0.2、名义 1、峰值 3 条/秒，作为假设记录；以后有了业务实测再校准。
  - D4：按 §6.1 门槛表执行，只在 C4 和 C4-low 上判定。
- D1–D9 全部定案。
- 实现在 NX-029、NX-030 合入后再分派。
