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
