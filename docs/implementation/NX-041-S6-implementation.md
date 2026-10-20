# NX-041 S6：模拟进程组与 Host 隔离

依据：`NX-041-design.md` §6、D3、D6、§11 S6。迁移 `0264_nx041_sim_process_group.sql`（编号是临时的，只出现在文件名和 catalog 里）。基础是 L3 的 `side/nx041-s1-host` be8121b7（Host 上的 world 校验），已 cherry-pick 进来。

## 实现

| 内容 | 位置 |
| --- | --- |
| **world 组（D3）**：每个 Host、runtime worker 和 guard 进程只服务一个 world 组。`real` 是默认组，接受 real 和 test；`simulation` 组只接受一个 `sim:`/`shadow:` 世界。real Host 拒绝 sim/shadow 的 Run 命令，模拟 Host 拒绝 real/test 的 Run 命令，在准入、guard 请求和读文件之前就拒绝（`invalid_run_world`）。runtime worker 和 guard 用 `--world-group simulation` 才能接受模拟世界，不带这个参数时仍只接受 real 和 test；多进程 guard 会把组传给子进程 | Host：`worlds.ts`、`runtime-host.ts`；Python：`worlds.py`、`runtime_worker.py`、`runtime_guard_worker.py` |
| **Pi 本地目录按 world 分开**：模拟 Host 把每个世界的 Run 放在 `runtime_root/<sim-uuid 或 shadow-uuid>/<run_id>`。新增 `POST /internal/v1/worlds/purge`（只在模拟 Host 上有）：先关闭这个世界已打开的 Run，再整体删除目录；目录不存在时报告 absent；拒绝符号链接；世界里有运行中的 Run 时返回 409。模拟 Host 的单个 Run purge 需要带上 world | `pi-runtime-adapter.ts`、`run-purge.ts`、`main.ts` |
| **库外数据（S2 审查 1.1）**：世界开始删除时，把它的 Host 目录加入队列 `runtime.nexloop_sim_world_file_purges`。模拟组的 `nexloop_eios.sim_world_files` 进程以 `nexloop_scheduler` 连接，不持有任何 world 凭据（该世界的凭据在删除时已吊销）：调用模拟 Host 删除目录，只把 Host 报告的 purged 或 absent 记为确认；失败的下一轮重试 | 0264 §3、`sim_world_files.py` |
| **模拟预算（D6）**：每个模拟世界创建时自动得到自己生命周期内的 model 预算（100 CNY）。租户的模拟账号每月合计上限 100 CNY（按 UTC 月），跨所有 sim/shadow 世界计算，超过时预留被拒绝（NXB01），单位必须是 CNY。花费记在 `control.nexloop_sim_model_spend`，这张表没有 `world` 列，世界删除不会清掉它，所以删掉世界后这个月的花费仍然计入上限。real 的预算按 world 分开，不受影响；成本的数据模式仍是 simulation（S1） | 0264 §1 |
| **S2 整体删除与预算行**：模拟世界的预算和消耗行带 world 列，会随 S2 的整体删除一起删掉。nx022 append-only 守卫（0129）本来就允许 owner 在 erasure scope 里删除，而 S2 的 purge 会按表打开 erasure scope，所以不需要新增机制 | 0260 / 0129 |
| **S5 遗留**：替身意图的 `submitted_at` 写入世界时间，读取 `runtime.nexloop_world_now`（S5 的接口）。S5 还没集成时回退到墙上时钟 | 0264 §4 |
| **部署**：新增独立的 compose 项目 `deploy/stage/compose.simulation.example.yaml`（`nexloop-simulation`）。所有服务都带 profile `simulation`，默认不启用，现有 compose 文件没有改动。模拟 Host 用自己的 bridge 网络；runtime worker、替身执行器和文件清理进程共用模拟 Host 的网络命名空间，不使用 host 网络，也不与 real 项目共用命名空间。secret 全部是 `sim_` 前缀，包括模拟世界自己的凭据、单独的签名密钥 id、单独的模型配置和 Host 传输材料；没有 real 凭据、通道 secret 或 provider/路由配置 | compose |

## 测试

- Host（Node 24，重建 dist 后串行）`test/nx041-sim-host.test.ts`：
  - 组规则；
  - 两种 Host 都拒绝跨组的 start/inspect，并且拒绝前不读任何文件；
  - 未知组在构造时拒绝；
  - 世界目录的整体删除、absent、非法 world 和符号链接；
  - 真实 Pi adapter 把 Run 放在世界目录下，删除世界后 Run 状态不存在。
- PG `tests/test_nx041_sim_process_group_pg.py`：
  - runtime worker 和 guard 的组参数（含负例）；
  - compose 项目独立、默认不启用、没有 real secret，现有项目不变；
  - 每月上限跨世界合计，删除世界后花费仍计入，real 不受影响，nx022 守卫在删除之外照常拒绝；
  - 目录删除的队列、Host 失败时不确认、只有 scheduler 能读写队列；
  - 替身意图的世界时间。
- S2 测试的计数加 1：模拟世界自带的 model 预算行也会被整体删除。

## 未完成与限制

- **完整 Run 的端到端**：没有做“模拟进程组跑完一个 Pi Run”的端到端。现有 Run 链路需要同一世界里的三个服务凭据：队列 API、worker 和 agent（用来签发 Run 凭据）。S2 按 D10 实现的规则是每个模拟世界只能有一个 active 凭据，所以这条链路在 sim 世界里建不起来。需要先决定：是把 D10 放宽成“同一世界的一小组凭据，全部只绑定该世界”，还是让一个凭据承担这三种身份。
  - 这次的 AT-046 覆盖到：
    - 进程组的配置和参数层；
    - Host 对跨组命令的拒绝；
    - 替身执行链路（S4 端到端）；
    - 预算；
    - 删除和目录清理。
- Host 的效应工具在 sim 世界里不可用：real 效应入口和表拒绝 sim（S4），而 Host 还没有调用替身提交的工具。这一步等接入孪生时（NX-042）一起做。
- 模拟组的签名密钥用单独的 key id（`NEXLOOP_SIM_SIGNING_KEY_ID`），需要在部署时登记为单独的 authority signing key；登记脚本这次没有做。
- 模拟 Host 网络的出口限制（只允许访问数据库）依靠主机防火墙，compose 文件里只写了说明，没有改防火墙规则。

## L6 审查修复（`nx041-s6-review.md`；迁移 `0265_nx041_sim_files_review.sql`）

| 条目 | 处理 |
| --- | --- |
| 1.2 / 3.1 数据库角色 | 删目录的进程改用新建的最小角色 `nexloop_sim_files`：它能执行的 security definer 函数只有 pending 和 confirm 两个入口（另有对 public 开放、只对 owner 生效的 `erasure_permits`），`nexloop_scheduler` 不再能调用。替身执行器的 DSN 改为 `nexloop_stand_in_worker` 专用（角色和授权在 L3 的 8ca4c820）。**未完成**：sim 的 runtime worker、guard 和 api 仍然使用应用 worker 角色。原因是 `nexloop_domain_worker` 能执行的 security definer 函数有 115 个，其中 39 个在函数体里按角色名检查 `session_user`；要建专用角色，就得重定义这批已发布的函数并逐一补授权，范围太大，建议单独立任务 |
| 1.3 出口 | 注释改成准确的描述。sim 网络使用固定子网 `NEXLOOP_SIM_SUBNET`。compose 层做不到按域名放行，所以边界放在主机防火墙，模板见 `NX-041-S6-simulation.nft.example`（table `inet nexloop_simulation`）：只允许访问 DATA_HOST 的 PostgreSQL 和模型 provider 白名单的 443 端口，其他新建连接一律丢弃，包括访问 APP_HOST 自身的端口（real Host、guard、gateway）。规则由负责人手工应用，部署脚本不会自动改主机。部署单的检查：从 sim 容器连接 real Host 端口，应当被拒绝 |
| 2.3 purge 鉴权 | 只有数据库对删除中或已删除的 world 签发 permit。permit 是用该 Host 的 purge key 算的 HMAC，限定 Host 和 world，有效 10 分钟，Host 最多接受 15 分钟。没有有效 permit，Host 什么都不删。持有 internal key 的 runtime worker 拿不到 permit，所以删不了 active world 的目录 |
| 3.2 确认可伪造 | Host 返回签名的删除证明，内容包括：outcome、是否持有过该 world、删除前目录清单的摘要和条目数、删除后状态（absent）、签发时间。数据库用 `authz.nexloop_sim_hosts` 中该 Host 的 key 验证签名，签发时间必须在 1 小时以内；验证不过就拒绝。删目录的进程不持有任何 Host key，既造不出 permit，也造不出证明 |
| 3.3 多 Host | 确认按 Host 分开记录（`runtime.nexloop_sim_world_file_confirmations`）。所有 active Host 都确认之后，这个 world 的文件才算删完；某个 Host 回 absent 只完成它自己那一份。Host 用 `.worlds-seen` 记录自己持有过的 world，并把是否持有过写进证明。一个 Host 都没有登记时，入队时直接确认 |
| 3.4 可见 | S2 摘要的 `completed_at` 要等文件确认之后才写入，在此之前摘要也不会被保留策略删除。`runtime.nexloop_sim_world_files_overdue(tenant, hours)` 列出已经 deleted 但文件还没确认的世界 |
| 4.2 结算与月份 | 补了测试：预留 10、释放 7.5，计入 2.5。月份按 UTC 计算：负责人定的 ¥100 按 UTC 自然月统计，北京时间每月 1 日 0–8 点仍算上个月 |
| 4.3 指标 | 核对结果：alert-rules v9 里成本和预算相关的规则没有 world 选择器，但 0131 的告警评估只对 real 世界执行（`alerts_real_world_only`），sim 的成本进不了 real 告警。Prometheus 导出的每条时序都带 world 标签。仓库里没有面板定义 |
| 5.1 / 5.2 时间 | 删掉了把 `submitted_at` 改成世界时间的触发器，`submitted_at` 恢复为墙钟审计时间。`runtime.nexloop_sim_world_time` 去掉了异常捕获：S5 存在时调用 `world_now`，内部出错直接抛出；S5 不存在时才用墙钟。集成时 L3 的 8ca4c820 由服务端取 sim_clock，以它为准 |

### 部署步骤（只写说明，不改 deploy/ 下的 secret）

1. **数据库角色口令**：`nexloop_sim_files` 和 `nexloop_stand_in_worker` 由迁移创建，可以登录，但没有口令。由负责人用 `alter role ... password` 设置口令，口令存入 Keychain，再写进 `NEXLOOP_SIM_SECRET_DIR` 下的 `sim_files_dsn` 和 `sim_stand_in_worker_dsn`（0600）。
2. **pg_hba**：在 DATA_HOST 的 pg_hba 中，这两个角色只允许从 `NEXLOOP_SIM_SUBNET` 经主机出口地址连接，使用 `hostssl ... scram-sha-256`；real 角色不允许从 sim 子网连接。这一条手工加，也可以等 stage 模板加入 sim 子网参数后再纳入渲染。
3. **Host 登记**：为每个 sim Host 生成 32 字节的 purge key，写出两份：十六进制的 `sim_host_purge_key` 给 Host，原始字节用于登记。登记方式是 owner 执行 `insert into authz.nexloop_sim_hosts(host_id,key_material) values(<id>,<key>)`。key 不能修改；停用后不能恢复，只能换新 id 重新登记。sim Host 的运行配置写入 `host_id` 和 `world_purge_key_file`；删目录的进程的 `--host-id` 与之相同。
