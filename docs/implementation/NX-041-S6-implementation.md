# NX-041 S6：模拟进程组与 Host 隔离

依据：`NX-041-design.md` §6、D3、D6、§11 S6。迁移 `0262_nx041_sim_process_group.sql`（编号是临时的，只出现在文件名和 catalog 里）。基础是 L3 的 `side/nx041-s1-host` be8121b7（Host 上的 world 校验），已 cherry-pick 进来。

## 实现

| 内容 | 位置 |
| --- | --- |
| **world 组（D3）**：每个 Host、runtime worker 和 guard 进程只服务一个 world 组。`real` 是默认组，接受 real 和 test；`simulation` 组只接受一个 `sim:`/`shadow:` 世界。real Host 拒绝 sim/shadow 的 Run 命令，模拟 Host 拒绝 real/test 的 Run 命令，在准入、guard 请求和读文件之前就拒绝（`invalid_run_world`）。runtime worker 和 guard 用 `--world-group simulation` 才能接受模拟世界，不带这个参数时仍只接受 real 和 test；多进程 guard 会把组传给子进程 | Host：`worlds.ts`、`runtime-host.ts`；Python：`worlds.py`、`runtime_worker.py`、`runtime_guard_worker.py` |
| **Pi 本地目录按 world 分开**：模拟 Host 把每个世界的 Run 放在 `runtime_root/<sim-uuid 或 shadow-uuid>/<run_id>`。新增 `POST /internal/v1/worlds/purge`（只在模拟 Host 上有）：先关闭这个世界已打开的 Run，再整体删除目录；目录不存在时报告 absent；拒绝符号链接；世界里有运行中的 Run 时返回 409。模拟 Host 的单个 Run purge 需要带上 world | `pi-runtime-adapter.ts`、`run-purge.ts`、`main.ts` |
| **库外数据（S2 审查 1.1）**：世界开始删除时，把它的 Host 目录加入队列 `runtime.nexloop_sim_world_file_purges`。模拟组的 `nexloop_eios.sim_world_files` 进程以 `nexloop_scheduler` 连接，不持有任何 world 凭据（该世界的凭据在删除时已吊销）：调用模拟 Host 删除目录，只把 Host 报告的 purged 或 absent 记为确认；失败的下一轮重试 | 0262 §3、`sim_world_files.py` |
| **模拟预算（D6）**：每个模拟世界创建时自动得到自己生命周期内的 model 预算（100 CNY）。租户的模拟账号每月合计上限 100 CNY（按 UTC 月），跨所有 sim/shadow 世界计算，超过时预留被拒绝（NXB01），单位必须是 CNY。花费记在 `control.nexloop_sim_model_spend`，这张表没有 `world` 列，世界删除不会清掉它，所以删掉世界后这个月的花费仍然计入上限。real 的预算按 world 分开，不受影响；成本的数据模式仍是 simulation（S1） | 0262 §1 |
| **S2 整体删除与预算行**：模拟世界的预算和消耗行带 world 列，会随 S2 的整体删除一起删掉。nx022 append-only 守卫（0129）本来就允许 owner 在 erasure scope 里删除，而 S2 的 purge 会按表打开 erasure scope，所以不需要新增机制 | 0260 / 0129 |
| **S5 遗留**：替身意图的 `submitted_at` 写入世界时间，读取 `runtime.nexloop_world_now`（S5 的接口）。S5 还没集成时回退到墙上时钟 | 0262 §4 |
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
