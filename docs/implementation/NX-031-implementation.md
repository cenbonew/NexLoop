# NX-031 运行 1000 消费者与并发矩阵：实现说明（L4）

设计稿 `NX-031-design.md`（D1–D9 全部定案）。分支 `nx031-s0`，从 `dev-smoke` `31cdf00`（基于 main `7ea25b8`）切出。切片 0 不需要迁移，没有占用临时号 0150–0159。压测本身由调度员在 CI 主机的夜间窗口执行（D9）；本机只做小规模验证。

## 切片 0

### 合成数据生成器（固定 seed）
- `scripts/load/synthetic.py`：
  - `generate(seed, consumers)` 是纯函数，输出 JSON Lines，`digest()` 是这些行的 SHA-256。同一 seed、同一消费者数、同一份代码得到逐字节相同的输出；
  - 比例按设计 §2.2：受限 8%、承诺 20%、活跃计划 30%、商业事件 40%，平均每人 20 条历史消息，每人 1–3 个会话；
  - 文本只来自代码中固定的中文模板加编号；
  - 受限客户的最后一条来信是拒绝联系语句，能命中 0109 规则；
  - 最后一行是汇总（trailer）。
- seed 31 下 1000 个消费者的结果（切片 1 修正后：承诺客户的第一个会话里一定有一条承诺请求）：digest `b880a7f0…`，1733 个会话、20055 条消息，受限 75、承诺 204、活跃计划 322、商业事件 1002 条。

### 经受治理入口装载（`tests/load_template.py`）
- 写入路径：
  - **身份**：与身份夹具、`browser_test_profile` 相同的可信 bootstrap 写入（系统没有自助注册入口）；
  - **Consumer 与归属**：registrar 服务经受治理的 `Consumer.create` 与 `ConsumerOwnership.create`；
  - **顾客授权**：与会话夹具相同的可信授权事实（`Conversation.create`、`Message.create`、会话读取），主体换成该顾客；
  - **会话与消息**：每个顾客用自己的浏览器会话（真实口令认证）经 `ConversationMessagePort` 写入。走的是 0046 写入路径，所以 0109 拒绝检查、NX-051 投影和提取 feed 都照常运行；
  - **工作台**：1 名 owner、2 名 operator 经 configurator 写入；请求类、接管类、目标类 Action 发布到租户。
- **顺序**：身份 → Consumer 与归属 → 全部授权事实 → 逐个顾客写消息 → 工作台配置。这样每个已认证的会话快照在使用时都仍然有效（授权事实一变，旧会话快照就会失效）。
- **商业事件（已装载）**：用 test 连接器（`data_mode=test`、world `test`，D1），经 configurator 配置；每条事件用连接器密钥签名后交给实际的 intake 函数（与 webhook 同一入口），recorder 把 feed 排空后再做模板。test world 的连接器只能关联 test world 的 Consumer，所以 customer_ref 不做关联。小规模测试核对：全部 202 接受、只写入 world `test`、feed 排空。
- **本切片未装载**：承诺（要经提取 worker 和确定性模型）、活跃计划（要经计划建立入口），`template.json` 中如实列出，放进切片 1。

### 模板与克隆
- 装载完成后，用 `pg_basebackup -X stream -c fast` 对运行中的集群做在线一致副本，作为模板（`<out>/pgdata`）。
- `Clone(template, dest)` 把 PGDATA 复制到格子自己的目录，socket 目录取短路径（Unix socket 路径约有 103 字节的上限），换一个新端口启动，`stop()` 停止并删除。
- 选这种方式而不是 `createdb -T` 的原因：库名保持 `postgres`，各服务的 DSN 不用改；模板对应一个停止状态的完整集群，克隆之间互不影响。
- reader 只用 PostgreSQL，签名密钥在库里有一份，另在 `private/signing.json` 存一份供格子侧的服务使用。所以模板只有 PGDATA，没有 Artifact 目录。
- 构建入口 `scripts/load/build_template.sh [--seed] [--consumers] [--out]`：
  - 默认输出到被忽略的 `.ci-results/load/template`；
  - 输出包括：`manifest.jsonl`；`template.json`（seed、digest、清单汇总、实际装载计数、各阶段耗时、代码提交、catalog 版本，不含秘密）；`private/credentials.json` 与 `private/signing.json`（0600）；`pgdata/`；
  - 构建用的一次性集群在构建结束后停止并删除。

### 本机实测（开发机，负载 5–7）
- 100 个消费者（2338 条消息）的模板构建总计 3 分 27 秒：
  - 身份 2.6 秒，Consumer 与归属 6.9 秒，授权 0.9 秒，消息 191.6 秒（单条约 82 ms），`pg_basebackup` 0.8 秒；
  - 模板 82 MB；克隆复制 0.3 秒、启动 0.1 秒，克隆中的消息数与模板一致。
- 推算 1000 个消费者（约 1.94 万条消息）约 30 分钟，在设计 §2.3 估计的 20–40 分钟以内。实际值以 CI 主机上的首次构建为准。

### D7 连接预算（实测与改动）
- **代码**：
  - `background_services` 新增 `--pool-max`（默认 `POOL_MAX=2`）；plan-reevaluator 和 reply-guarantor 新增 `--api-pool-max`（默认 `API_POOL_MAX`）；
  - `domain_worker` 新增 `--pool-max`（默认 2）。
- **实测：launcher 的 API 池为 1 时不够用**。
  - 计划复评 launcher 和兜底回复 launcher，在单独打开、1 个连接的 nexloop_api 池上都没有启动 Run（launched=0、fallback_started=0）；
  - 同一路径改用 2 个或 4 个连接都能启动；
  - 说明一次启动需要同时持有两个连接。因此 `API_POOL_MAX=2`，设计 D7 里的 1 不成立。
- **预算**：
  - 若 launcher 用 2、其余不变，合计 38 + 10×2 + 2×2 = 62，超过上限 60；
  - 按方案 A（**待调度员确认**）把 `domain-worker` 从 4 降到 2：它每次运行只处理一页 Artifact 维护，同一时刻只用一个连接，`test_domain_worker` 在池 2 下通过；
  - 合计 60，doctor 通过；
  - 备选方案 B 是把 API 从 2 个进程降到 1 个，影响 ACK 吞吐，需要压测数据，所以未采用。
- **静态核对**（`test_connection_budget.py`）：
  - 每个后台服务都以代码默认的池大小登记，两个 launcher 也单独登记；
  - domain-worker 的池与代码一致；
  - 合计 60，再多 4 个连接就判不通过。
- **运行实测**（`test_connection_budget_live_pg.py`，一次性集群，凭据来自部署清单）：
  - 方法：EIOS 的连接名都是 `nex-eios`，所以逐个服务单独运行，取高于空闲基线的峰值，最后十个一起运行再测一次；
  - 空闲时：每个服务 1 个连接，两个 launcher 服务各 2 个；十个同时运行共 12 个，都在声明范围内（声明合计 24）；
  - 负载下的峰值由夜间压测的 NX-030 连接池样本（M10）给出。
- **launcher 池的测试**：`test_plan_role_launcher_pg`、`test_reply_fallback_pg` 各新增一例，在部署取值的 launcher 池上实际启动 Role Run 或兜底 Run。
- **仍未登记的常驻进程（需要决定）**：message relay（`message_relay_cli`）和 outbound-recorder（`outbound_messages`）都会开池，按代码默认各 4 个连接。登记后合计会超过 60，需要调度员或负责人决定。

### 测试（开发机，`-n ≤3`；Pi 相关单独串行；负载 4–7）
- `test_load_synthetic` 2 例；`test_load_template_pg` 1 例（6 个消费者装载、治理记录、归属、模板与两个克隆）。
- `test_connection_budget` 与 `test_background_services_pg` 等共 61 例（含 conversations、community container）；连接预算相关 43 例（含 domain_worker 与 live 实测）。
- 串行：`test_plan_role_launcher_pg` 5 例、`test_reply_fallback_pg` 7 例。首次运行时 launcher 池为 1，两例新用例失败，这就是上面的实测结论；改为 2 后通过。

## D7 最小可用池实测（调度员裁定 3）

方法（`scripts/load/measure_pools.py` + `tests/pool_probe/sitecustomize.py`，只用于测量，不改产品代码）：
- 每个服务在池大小 1 和 2 下各跑一遍它的功能测试。探针把目标池强制设为该大小，选择方式有两种：
  - 按服务自己的入口模块（包括 `python -m` 启动的子进程）；
  - 按服务的 DB 角色（同一测试里该角色的所有池，比只压服务自己的池更严）。
- 判定“可用”：功能测试全部通过，且探测到的池没有超时（`requests_errors=0`）。排队次数和等待时长另行记录。
- 本机 `-n 3`，含子进程或 Pi 的测试串行运行，开跑负载 3.5–6.5。
- 第一轮里 relay、outbound-recorder、guard 子进程以 `python -m` 启动，探针没有命中（池计数为 0），结果作废。修正模块匹配后重测，下表用的是第二轮的结果。

| 服务 | 进程数 | 池 1 | 池 2 | 实测最小池 | 建议池 | 建议合计 |
|---|---|---|---|---|---|---|
| api | 2 | 2 例失败 | 15 例通过，排队 2 次 / 2 ms | 2 | **4**（见注 1） | 8 |
| domain-worker | 1 | 等待第二个连接直到超时 | 通过（含 resume） | 2 | 2 | 2 |
| action-worker（effect_worker） | 1 | 3 例失败，1 次池超时（30 s） | 10 例通过，排队 3 次 / 5 ms | 2 | 2 | 2 |
| scheduler | 0 | — | — | 无对应进程（见注 2） | 0 | 0 |
| runtime-worker 调度父进程 | 1 | 未单测 | 沿用 | 2（代码约定） | 2 | 2 |
| runtime-worker guard 子进程 | 4 | 1 例失败（代码也要求 ≥2） | 3 例通过，排队 2 次 / 3 ms | 2 | **4**（见注 3） | 16 |
| 10 个后台服务（每个） | 各 1 | 全部通过，无超时（claim-extraction-worker 排队 6 次 / 42 ms） | 全部通过 | 1 | 1 | 10 |
| 两个 launcher 的 API 池 | 各 1 | 不启动 Run | 启动 Run | 2 | 2 | 4 |
| message relay | 1 | 8 例失败 | 22 例通过，排队 6 次 / 9 ms | 2 | 2 | 2 |
| outbound-recorder | 1 | 1 例失败 | 6 例通过，排队 2 次 / 2 ms | 2 | 2 | 2 |
| **合计** | | | | 全部取最小值：**36** | | **48 ≤ 60** |

- 注 1：API 每进程的 LifecycleLock 容量等于池大小的一半。池为 2 时每个进程同一时刻只处理 1 个请求，会直接影响 ACK 吞吐（方案 B 同类问题），所以建议维持 4，等夜间压测的 ACK 数据再议。
- 注 2：预算中的 `scheduler`（4）对应交接包 Compose 模板里的 `nexloop-scheduler`，代码里没有这个入口。建议去掉这一项，或者明确作为预留，需要裁定。
- 注 3：guard 池从 4 降到 2，每个子进程同一时刻只能处理 1 个 guard 请求，与 2 s 工具时限直接相关。功能测试在 2 下通过，但要改就必须先在 2 下重跑 ADR-022 的串行时限门槛，所以建议暂时维持 4。
- 关于 claim-extraction-scheduler：按入口模块选择时，池为 1 也通过；按 `nexloop_api` 角色选择、池为 1 时有 7 次超时。后者把同一测试里夹具自己的 `nexloop_api` 池也压成了 1，不能算作它自己的下限，结论取前者。
- **结论**：按建议池合计 48，在 60 以内；全部取实测最小值时合计 36。relay 和 outbound-recorder 都已按建议池计入；如果 `scheduler` 作为预留保留 4，合计 52，也在 60 以内。
- **调度员批准后落地（2026-10-11）**：
  - 代码默认值：后台服务 `POOL_MAX=1`，launcher `API_POOL_MAX=2`；domain-worker、action-worker（effect_worker）、message relay、outbound-recorder 新增 `--pool-max`，默认 2；
  - 预算文件：合计 52，relay 和 outbound-recorder 已登记，`scheduler` 注明为预留；
  - API 每进程 4、guard 每子进程 4 维持不变，要降须先有夜间压测的 ACK 数据和 ADR-022 门槛复测；
  - doctor 的预算核对新增 `connection_budget_services`：每个常驻服务（`connection_budget.required_services()`：基础 7 项、10 个后台服务、2 个 launcher）都必须登记，漏一项即退出码 1；
  - 负例：逐一去掉每个必需条目，`evaluate` 都判不通过；doctor 去掉 outbound-recorder 时退出码 1，且总和那一项仍为通过。
- 新默认值下的功能回归全部通过：预算 25 例；后台服务、live 实测、domain_worker、effect_worker CLI 等 37 例；串行的 message relay 20 例、outbound 6 例、计划 launcher 5 例、兜底回复 7 例、接管 relay 2 例。

## 切片 1（分支 `nx031-s1`：`nx031-s0` 合并 main `7f5823f`，带上 NX-030 D6 的 `tool_timing`）

不需要迁移，没有占用临时号 0150–0159。输出对齐 L2 的结果目录契约（`nx031-report` `a35602c`，`NX-031-results-contract.md`）。

### 矩阵文件 `deploy/load/matrix.v1.json`
- 设计 §3.2 的 8 格，门槛格只有 C4 和 C4-low；
- Host 并发 1/2/4/8，C4 系列的速率 0.2/1/3（D2）；后台关闭只在 C4-quiet，同机 CI 只在 C4-ci；
- 时序：预热 120 s、稳态 600 s、排空最长 600 s、快照间隔 30 s；
- 消息组合；模型时延（D5）；预期拒绝码 NXC05、NXC06；越权探测 50 对。
- 执行器会校验矩阵文件：8 个格子缺一不可，门槛格只能是 C4 和 C4-low。

### 矩阵执行器 `scripts/load/matrix.py`（含 `stack.py`、`driver.py`、`collect.py`）
- **参数**：`--template --matrix --out --lane-root`；另有 `--cells` 只跑部分格子，`--timing-scale` 只用于本机小规模验证，车道上固定为 1.0。
- **安全前提**：`REAL_DISPATCH_ENABLED` 不为 false 时直接拒绝运行。
- **每个格子的流程**：
  1. 在 `lane-root/<cell>` 下克隆模板；
  2. 起服务：API 2 个进程（loopback HTTPS，浏览器域和工作台域）；后台开启时再起 10 个后台服务，各用自己的凭据；
  3. 消息 Run 链的挂点（见下文）；
  4. 顾客登录、预热；
  5. 稳态窗口：同时运行泊松负载、工作台活动（owner 每分钟读一次总览；operator 走一轮接管 → 员工回复 → 交还）、每 30 秒一次 NX-030 快照；
  6. 停止入站，排空；
  7. 越权探测；
  8. 采集写入，按契约 §2；
  9. 停掉所有进程，删除克隆和该格的私有文件。
- **中止**：失败的格子写 `completed: false`，`aborted` 取 `template_clone_failed`、`service_start_failed`、`service_exited`、`ci_command_missing`、`runner_error` 之一；只要有格子中止，进程退出码就非零。
- **C4-ci**：
  - 用 `LOAD_CI_COMMAND` 在稳态开始时启动全量 CI，自己不取锁；
  - 由一个等待线程记录 CI 的真实墙钟时间；
  - 只统计本次 CI 写出的 junit 文件，旧的 `.ci-results` 文件不计入。
- **`cell.json`**：`host_metrics` 暂为 `approx`（NX-030 M09 未交付）；另附 `message_runs`、`login_failures`、`sent`、`workbench_events`、`timing_scale`。这几个是契约之外的说明字段，报告可以忽略。
- **`checks.json`**，按 §4.4 的 7 项：
  - 实际核对的：外发归属、Claim 归属、越权探测（由驱动执行）、重复副作用、未结局工作；
  - Run 绑定、模型上下文这两项要等消息 Run 链，`checked` 为 0，不把请求数当作已核对。

### 装载补充（模板）
- **服务凭据**：按 service-grants 清单经 configurator 发放各服务主体，令牌只存在 `private/service-tokens.json`（0600）。
- **承诺**，全部经受治理入口：
  - 用进程内的生产 app 和真实工作台登录 Cookie，由 operator 对每个承诺客户接管其第一个会话，以员工身份回复一句合成的承诺语句，绑定到客户的请求，再交还；
  - 部署形态的提取 scheduler 和 worker 用各自的凭据，配合 `LoadRulesProvider`（只认合成模板「我会在…」的规则式确定性 provider，不是模型）做提取；
  - 部署形态的 keeper 登记承诺。
  - 发布 Commitment 类型和 Action，以及“负责人对 Commitment 属性组的决定”与 keeper 生命周期规则，都按 `commitment_fixture` 的可信配置方式写入。
  - 小规模验证：8 个消费者时 3 条承诺全部登记，Claim 都是 speaker=agent 的 commitment；30 个消费者的模板中 7 条承诺全部登记，55 个会话全部提取。
- **活跃计划：未装载**。计划建立需要每个客户的 role、offering、binding、control（plan recipe），与多客户 relay 要登记的每客户 recipe 是同一份，所以等 L2 交付后再做。

### 本机验证（`--timing-scale 0.05`，稳态约 30 秒）
- 20 个消费者，C4：完成；ACK 22 条，全部 202，p95 88 ms；接管、员工回复、交还都是 200；越权探测 19 对，全部被拒；连接峰值 29；没有残留进程。
- 30 个消费者，C4-low 完成；C4-ci 不设 `LOAD_CI_COMMAND` 时正确中止，设为替代命令时完成（CI 墙钟 3 秒，没有 junit 文件时 `failed` 为空）。

### 已知限制（都与消息 Run 链未启动有关，没有伪造任何数据）
1. `runs.jsonl` 和 `dispatch.jsonl` 为空，按契约报告会判定逐 Run 分解缺失。
2. 模板里的历史来信都没有回复，reply-due 积压，格子排空不了，“未结局”核对全部是未结局。应当在构建模板时由消息链给历史来信回复。
3. ~~运行期提取一直重试~~：已按调度员裁定解决，见下节。

### 需要 L2 知道的接口约束（多客户 relay，方案 A）
- **进程形态**：执行器为每个格子起 **1 个** relay 进程，池为 2，凭据来自 service-grants 清单的 relay 主体（模板的 `private/service-tokens.json`），不再为每个客户准备 recipe 文件。
- **每客户 recipe 的登记**：要有一个能在构建模板时于进程内调用的受治理登记入口，函数或 CLI 都可以；要幂等，1000 个客户在分钟级内完成。登记的内容即 recipe 的各项：control、offering、binding、role、link、step 的 id 和 revision。
- **计划建立要读取每客户 recipe**：执行器装载活跃计划时，要能按 `consumer_id` 读到该客户已登记的 recipe 中的 role_id、link_id、step_id、offering_id、binding_id、control_id，只读即可。
- **Host 并发**：每个格子的 Agent Host 用压测专用配置，`maximum_active_runs` 等于该格的 Host 并发，C8 为 8，不经 doctor；runtime worker 固定 `--guard-workers 4`。
- **D6 字段**：消息 Run 结束时 `runtime.jobs.result.tool_timing` 中要有 `total_ms` 和 `calls`；执行器据此写 `runs.jsonl`，并按 trigger 消息关联 e2e 和 outcome。

### 压测专用的提取 provider：load-rules（调度员 2026-10-11 裁定）
- **定位**：`nexloop_eios/load_rules.py` 的 `LoadRulesExtractionProvider`，是 test 模型档案的一个变体：`MODEL_PROVIDER=test` 加上 `MODEL_TEST_VARIANT=load-rules`。
  - 只认合成数据里的承诺句「我会在…」：企业一侧的这类句子生成承诺 Claim，其他文本只给话题、不生成 Claim；
  - 不调用任何模型和网络，没有凭据；
  - **它只用来让压测时的提取链路有真实的负载形状，提取质量不作为任何 AT 的依据**。
- **准入**：
  - `model_profile.load_model_profile` 只在调用方显式允许（`allow_load_rules`）、不是 stage、没有 key、变体名正确时才接受，否则报配置错误；
  - 提取 worker 新增 `--mode`，默认 `stage`；只有 `--mode test` 时允许这个变体，否则在连库之前就拒绝启动；matcher 一律拒绝；
  - doctor 在非 test 模式下核对模型配置时，这个变体判为不通过。
  - 负例测试（`test_load_rules.py`）：默认、stage、带 key、错误变体名、错误 provider 都被拒；提取 worker 不带 `--mode` 或 `--mode stage` 时拒绝启动，matcher 拒绝启动，`--mode test` 时正常启动；stage doctor 判不通过；provider 不发起任何网络请求。
- **执行器**：每格的提取 worker 用这个变体并加 `--mode test`。模板构建时进程内的提取也改用同一个产品类，删掉了测试里那份重复实现。

### 顺带修复的产品缺陷：提取 worker 处理任何非空窗口都会出错
- **现象**：部署形态的提取 worker（`background_services.LazyModelProvider`）在处理非空窗口时总是抛 AttributeError，任务记为 `unexpected` 并进入 `retry_wait`。原因是 extractor 计算输入 digest 时要读 `provider` 和 `model_id`，而 `LazyModelProvider` 没有这两个属性。原有测试只覆盖空闲的 tick，所以一直没有暴露。
- **修复**：provider 在首次使用时解析，并对外提供这两个属性；解析失败时按原语义抛 provider 不可用的错误。测试 `test_lazy_provider_names_its_provider_and_model_for_the_input_digest`。
- **验证**：本机格子里，负载期间的 59 个提取任务全部 `succeeded/recorded`；修复前有 24 个卡在 `retry_wait/unexpected`。
- **诊断**：`cell.json` 新增 `jobs` 字段，按队列、状态、结果代码计数（只有代码），供夜间排查。

## 切片 2（分支 `nx031-s2`，基于 main `c835158`）：消息 Run 链、历史回复、计划数据、逐 Run 结果和 7 项核对

### 消息 Run 链（每格）
- 进程：
  - 1 个多客户 relay（run profile 模式，池 2）；
  - 1 个 Agent Host（deterministic-test，`maximum_active_runs` 等于该格的 Host 并发）；
  - 1 个 runtime-worker（4 个 guard 子进程，`--dispatch-loops` 等于该格的 Host 并发）；
  - 本地 JSON 投递 provider、effect worker、outbound recorder 各 1 个。
- 登记：模板构建时在进程内调用 `relay_enrollment.enroll`，relay 和兜底 launcher 按 `consumer_id` 读取登记的 id；一条 `relay_source_read_rule` 让一个 Source 读取全部已登记客户（`tests/load_chain.py`）。
- 凭据和 run profile 在模板的 `private/chain.json` 里，不写入任何日志。

### 产品改动 1：runtime-worker `--dispatch-loops N`（调度员 2026-10-11）
- **问题**：一个 RuntimeDispatcher 循环一次只驱动一个 Run，会一直轮询 Host 直到这个 Run 结束，所以一个 runtime-worker 同一时刻最多 1 个活跃 Run。ADR-022 §4 的“全局并发 4”在生产上达不到。
- **改动**：
  - 父进程并行跑 N 个派发循环（1..16），共用同一个 queue；
  - 任务分配靠已有的 claim、lease 和 fencing；
  - 默认值 4，等于 Agent Host 默认的 `maximum_active_runs`；stage 部署示例也设为 4；
  - `--once` 仍然只跑一个有界 tick。
- **连接预算**：
  - 各循环共用调度连接池（`dispatcher_pool_max` 2），不增加连接数；
  - `connection-budget.v1.json` 的 runtime-worker 条目新增 `dispatch_loops`；合计仍是 52；
  - doctor 新增检查项 `runtime_dispatch_loops`，要求 `processes × dispatch_loops ≤ 4`（ADR-022 §4 的 Host 上限）；
  - 本机格子实测的总连接数见下文。
- **测试**：
  - `test_runtime_worker.py::test_cli_dispatch_loops_share_the_queue_each_task_claimed_once`：两个循环同时跑两个排队的 Run，每个任务只被领取一次（一个 running 事件、fence 1、Host 一个 submission），SIGTERM 后两个 Run 都正常结束；
  - `test_cli_killed_worker_and_host_automatically_reclaim_same_run[1|2]`：1 个和 2 个循环时，被 kill 的任务都由 reclaim 恢复，还是同一个 Run，fence 增加；
  - `test_cli_dispatch_loops_invalid_count_fails_closed[0|17|x]`：非法循环数在领取任务之前就拒绝启动；
  - `test_cli_sigterm_drains_current_run_without_claiming_next`：显式指定 1 个循环，原语义不变；
  - `test_connection_budget.py`：预算登记和 doctor 负例。
- **复测**：ADR-022 §4 的串行门槛由调度员在测试机上复测。

### 产品改动 2：Host deterministic-test profile
- **faux 响应自动补充**（`apps/agent-host/src/deterministic-queue.ts`）：
  - 原来每个 Host 进程只预置 16 或 64 个按会话生成的响应，消息 Run 每个用 4 个，第 17 个 Run 起全部以 `runtime_model_unavailable` 失败；
  - 现在每个响应被消费时把自己放回队列，仍然只读本会话的内容，没有全局下标。
- **D5 时延注入** `deterministic_latency {median_ms, p95_ms, seed}`（`deterministic-latency.ts`）：
  - 对数正态分布，用固定 seed 的生成器抽样（mulberry32 加 Box–Muller），同一个 seed 得到同一序列；
  - 只在每个脚本化响应之前 sleep，不改变输出；sleep 发生在模型调用内部，计入模型耗时；
  - 上限：median 1..10000 ms，median ≤ p95 ≤ 30000 ms，单次抽样最多 30000 ms；
  - 只允许用于 deterministic-test，并且必须带一个按会话生成的协议（消息、计划结局或兜底回复），否则配置加载直接拒绝；
  - doctor 遇到部署的 Host 配置带这个键时判不通过，检查项 `agent_host_test_only_keys_absent`；
  - 矩阵的 `model_latency`（median 1500 ms，p95 4000 ms）加上 run seed，传给每格的 Host。
- **deepseek-flash 路径没有任何变化**：
  - 两段代码共用的只有允许键列表；这个键出现在 deepseek-flash 下会被拒绝，和以前作为未知键被拒绝的结果相同；
  - deepseek 的模型配置文件本来就拒绝未知键；
  - Host 全部 vitest 220/220 通过。

### 历史来信（方案 A）
- manifest 升级为 `nexloop.load-manifest.v2`：每个客户 1 个会话，最近 1–2 条来信。
  - 承诺客户的第一条是承诺请求；受限客户的第二条是拒收；其余客户有一半带一条跟进。
  - seed 31、1000 客户时共 1547 条，digest 变为 `618fe92f…`（v1 是每客户平均 20 条）。
- 模板构建在承诺装载之前启动消息链（`scripts/load/stack.py`，4 个派发循环，不注入时延），直到同时满足：没有未路由的来信、没有未结束的 Run 任务、没有待回复项。
  - 如果所有 Run 都已结束而仍有待回复，60 秒没有变化就停止，未回复数如实写进 `history_unreplied`（兜底 Run 不能在消息 Host 上运行，见下文）。
- 本机 25 客户：34 条历史全部回复，用时约 59 秒，Run 时长 p50 约 6 s。按这个速度估算，1000 客户约 40 分钟。
- 受治理的“历史会话导入”记为以后的产品议题。

### 活跃计划（方案 B：只装载数据）
- 流程：
  - workbench owner 在进程内用自己的 workbench 会话批准指标（`Metric.approve`）、发布目标（`Goal.publish`）、发布内置上下文策略 `recent_plus_required`（`StrategyPublisher`）；
  - 发布策略需要的 Action 来自业务清单，owner 的授权按 workbench 角色编译方式加入，发布后 workbench 应用目录恢复为原角色；
  - 部署的 plan-reevaluator 身份在 `nexloop_domain_worker` 角色上用 `PlanPort.establish` 建立每个计划。
- recipe 里的 control、offering、binding 来自该客户的登记；role、link、step 是合成 id，因为没有装载 role 链。
- 每个步骤的 `reassess_at` 都为空。格子不启动 plan-reevaluator，排空判断也不计 plan-reevaluate feed。所以**不会产生计划 Run**。
- **本轮没有覆盖计划重估 Run 和兜底 Run 的负载，相关指标不能据此声称通过。**
- 跟进项（调度员裁定）：让 deterministic-test 的 Host 根据 run profile 或请求里声明的协议，选择确定性输出（消息 v2、计划 v6、兜底 reply_once）。这样一个 Host 能服务全部 Run，拓扑和生产一致。只改测试 profile。

### 登录限流（安全控制，不放宽）
- 构建和压测脚本都不写 `nexloop_browser_rate_policies`。模板保留信任配置的正式值（装配配置为 10 次/60 秒，按 operator 加客户端摘要计）。
- 构建时把正式值记进 `template.json` 的 `login_rate_policy`。每格开始前从克隆库读出实际生效的策略写进 `cell.json`，不一致就以 `login_policy_mismatch` 中止。
- 驱动的预热登录不计入测量：
  - 每个客户绑定独立的 loopback 源地址 127.x.y.z，模拟来自不同地址的客户，每个来源一个限流桶，这和生产上一个来源一个桶的语义一致；
  - 主机绑不了这些地址时（macOS 只有 127.0.0.1），退回为共用一个地址、遇到 429 按 `Retry-After` 等待。
- 证据：
  - `tests/test_load_matrix.py::test_the_load_never_relaxes_the_login_rate_limit`（静态）；
  - `tests/test_login_rate_limit_process.py`（运行时：真实 http_api 进程，同一来源在 60 秒内第 11 次登录返回 429 并带 `Retry-After`，错误密码同样被限流拒绝）。

### 结果文件（结果契约 §2）
- `runs.jsonl`：按消息 Run 写 e2e、工具耗时与次数、排队等待和 outcome。
  - `model_ms` 为 `null`：v2 上下文的消息 Run 不写 `runtime.nexloop_model_requests`（只有 v6 写）。
  - `admission_wait_ms` 为 `null`：Host 没有逐 Run 的准入等待记录。
  - 报告会把模型分解标为缺失。
- `dispatch.jsonl`：按窗口内的投递写出。
- `checks.json` 7 项全部实测：
  - 第 4 项 model_context 因为同样的原因 checked=0；
  - 第 7 项未结局核对此前在管理连接上读不到租户行（security definer 加行级安全），已改为先设置租户上下文。
- `cell.json` 新增 `jobs`、`run_durations` 和 `login_rate_policy`。
- 排空判断：任务、已到期的 feed 条目，以及未路由的来信。

### 顺带修复（压测脚本）
- 格子的排空判断只计已到期的 feed 条目，以前排在未来时间的承诺监控会让格子永远排不空。

### 本机实测（开发机，25 个客户，seed 31；完整时长：预热 120 s，稳态 600 s，排空最多 600 s）
模板：25 个客户全部登记；历史来信 34 条；活跃计划 9 个；承诺 9 条；登录限流为正式值 10 次/60 s；构建约 2–3 分钟。本机绑不了独立的 loopback 地址，预热登录退回共用地址加 `Retry-After` 等待，预热期间没有登录失败。

**第一轮（作废，结果原样保留）**
- **C4 完成**：
  - 发送 770 条，ACK 655 条，全部 202，p95 491 ms；
  - 窗口内 645 个消息 Run：562 个升级，83 个回复；e2e p50 300.8 s，排队等待 p50 294.5 s；
  - operations 任务：165 个成功，563 个 `runtime_dispatch_unavailable`，75 个 `control_revision_stale`，1 个 `runtime_failed`；
  - 7 项核对违规都是 0，未结局为 0（共 645 条）。
- **C4-low 中止**，`service_start_failed`：
  - outbound recorder 的凭据是 fixture 按 30 分钟签发的，C4-low 开始时（模板构建后约 45 分钟）已经过期；
  - 其他 fixture 凭据的有效期只有 1 小时，同样撑不过一整轮。
- `report.py` 因 C4-low 的窗口为空，判为违反契约（exit 2）。
- **作废原因**是执行器的缺陷：模板凭据的有效期短于一轮压测。
  - 修复见提交 `7f39c30`：fixture 默认仍是 1 小时，模板构建时设为 30 天，recorder 的凭据跟随这个值；
  - 两轮之间没有其他代码变化。

**第二轮（重建模板后，同一模板依次跑两格）**
- 模板：历史来信 30 条回复，4 条未回复。其中 1 个 Run 是 `runtime_failed`；历史步骤 129 s 后因 60 s 没有变化而停止，未回复数如实记录。
- **C4（1 条/秒，Host 并发 4）判不通过**：
  - 发送 776 条，ACK 660 条，全部 202，p50 81 ms，p95 126 ms；
  - 窗口内 545 个 Run：460 个升级，85 个回复；e2e p50 300.7 s，排队等待 p50 294.7 s；工具耗时 p50 1.07 s（42 个有记录）；
  - operations 任务：185 个成功，451 个 `runtime_dispatch_unavailable`，67 个 `control_revision_stale`，1 个 `runtime_failed`；
  - 7 项核对违规都是 0；未结局为 0（共 651 条，大多以升级了结）；
  - 报告判不通过的项：排空 601 s、死信增量 9、意外拒绝 NXC02 66 次。其余项通过：ACK p95、连接峰值 52、guard p95 416 ms、超时率 0、身份违规 0。
  - **原因是过载**：在 D5 时延下，一个消息 Run 平均 4 次模型调用加工具，约 11 s；4 个并发约 0.36 Run/s，而到达是 1 条/秒。排队超过 Run 凭据的 300 s 有效期后派发失败，回复保障升级。确定性协议每个 Run 固定 4 次模型调用（提交、重建、查询、收尾），这会高估真实模型的轮数。
- **C4-low（0.2 条/秒）判不通过**：
  - 发送 153 条，ACK 133 条，全部 202，p95 87 ms；
  - 窗口内 126 个 Run 全部回复；e2e p50 6.5 s，p95 15.0 s；排队等待 p50 138 ms，p95 8.3 s；工具耗时 p50 1.04 s；
  - operations 任务：177 个成功，5 个 `runtime_failed`（原因未查清）；
  - 7 项核对违规都是 0，未结局为 0；连接峰值 53；
  - 判不通过的项：排空 601 s、死信增量 9、空闲派发 p95 10.6 s（限值 2 s）。这里的派发时延包含 D5 注入的模型时延，口径是否合适需要裁定。
- **两格共同的阻碍**：
  - 死信 9 条，全部来自 claim-match feed，数量和承诺 Claim 相同；
  - 排空不了，是因为 commitment-monitor 里有 9 条已到期的条目一直挂起；
  - 这两项都没有查清，留给夜间整轮之前处理。
- **两轮的差异**：
  - C4 的形态一致：都过载，任务因有效期失败，升级比例相近（562 和 460）；
  - 第二轮 C4-low 才真正跑完，第一轮没有 C4-low 的数据。
