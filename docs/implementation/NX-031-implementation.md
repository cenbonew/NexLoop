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
- seed 31 下 1000 个消费者的结果：digest `a5ae0862…`，1675 个会话、19395 条消息，受限 74、承诺 229、活跃计划 313、商业事件 1045 条。

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

### 本机实测（Mac，负载 5–7）
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

### 测试（Mac，`-n ≤3`；Pi 相关单独串行；负载 4–7）
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
- 按裁定，部署配置文件的数值暂不改动，仍是 `adf5200` 的版本：后台服务 2、launcher 2、domain-worker 2，合计 60，relay 和 outbound-recorder 尚未登记。等表确认后再按建议值一次修改，并把 relay、outbound-recorder 纳入预算文件和 doctor 的核对。
