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
- **本切片未装载**：商业事件、承诺、活跃计划，`template.json` 中如实列出。
  - 商业事件要经签名 webhook 和 test 连接器（`data_mode=test`、world `test`，D1）；
  - 承诺要经提取 worker 和确定性模型；
  - 计划要经计划建立入口；
  - 这三项放进切片 1，与负载驱动一起做。

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
