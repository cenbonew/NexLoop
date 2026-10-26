# NX-054 §8 第 3、5 步：负责人操作卡（release fb0e3d4f 及以后）

用于 `docs/implementation/NX-054-stage-publish.md` §8 的第 3 步（空闲基线，发布之前）与第 5 步（派生基线，发布之后）；§2、§4 是这两步的说明，本卡只写**谁在哪里做什么、看哪个数、记到哪里**。

- 负责人在 **CI 主机**上的终端执行（负责人工作台的会话：命令自己登录工作台、结束时登出）；调度员在 **DATA_HOST** 上同时计时和读数。
- 所有口令一律从负责人的 Keychain 经管道送入（`--password-stdin` 或脚本自己的 stdin 通道），**不进命令行参数，也不在浏览器 DevTools 里执行任何代码**。
- 下面的命令都已在 fb0e3d4f 上核对存在，参数名照抄自各脚本的 argparse：`scripts/deploy/sim_world.py`、`scripts/deploy/erase_synthetic_consumer.py`、`scripts/acceptance/synthetic_customer.py`、`scripts/acceptance/relation_derive_timing.py`。
- 记录表：本卡末尾的“记录”一节，填完后由调度员记入 NX-054 的证据（执行人与判定人分开写）。

## 0. 前置（调度员，一次）

| 项 | 怎么做 |
| --- | --- |
| 两个合成顾客 `perf-1`、`perf-2` | 用 stage 上已有的、persona 为 `simulated_agent` 的两个合成 WebChat 账号（它们的口令在 Keychain `nexloop/<stage>/<别名>` 中），由负责人指定哪两个当作 perf-1、perf-2。**它们会被擦除，之后不再可用**；不要用 S6 还要用的账号。 |
| 它们的 consumer_id | 调度员只读查出（metrics 登录不能读这张表；用 S6 的只读 DSN）：`select o.consumer_id from control.nexloop_consumer_owners o where o.tenant_id = '<TENANT_ID>' and o.world = 'real' and o.principal_id = '<账号的 principal_id>'`。**必须正好返回 1 行，否则停止**（0 行：账号不对或还没建会话主体；多于 1 行不会发生，出现就说明查询条件错了）。结果写进下面两份配置。 |
| 配置文件（CI 主机，负责人，0600） | `sim-create.json`（`deploy/examples/sim-world-create.example.json` 的格式：`kind` 为 `simulation`，`purpose` 写“NX-054 §8 基线 空闲/派生”，`expires_in_days` 1）、`sim-generate.json`（`deploy/examples/sim-world-generate.example.json`：`kind` 为 `synthetic`，`consumers` **200**，`events_per_consumer` 与示例相同，`world` 在创建后填入）、`sim-delete.json`、`erase-perf-1.json`（`deploy/examples/erase-synthetic-consumer.example.json`：`consumers` 只有 perf-1 的 consumer_id，`confirmation_ref` 例如 `nx054-s8-perf-1`）、`erase-perf-2.json`、`customers.json` 与 `plan-perf-1.json` / `plan-perf-2.json`（`docs/implementation/S6-stage-acceptance.md` §合成交互的格式：每份计划 25 轮，`account` 都是该账号，`wait_reply_seconds` 为 0，即不等回复） |

## 1. sim 生成 N=200（第 3 步一次，第 5 步再一次，每次新建 world）

| 顺序 | 谁 | 在哪 | 做什么 | 看什么、记到哪 |
| --- | --- | --- | --- | --- |
| 1 | 负责人 | CI 主机 | `security find-generic-password -s <工作台 Keychain 条目> -a <owner 用户名> -w \| python3 scripts/deploy/sim_world.py create --config sim-create.json --origin https://<APP_HOST_NAME>:<PORT> --ca-file <CA> --username-file <用户名文件> --password-stdin` | 输出中的 `world`（`sim:<uuid>`），填入 `sim-generate.json` 与 `sim-delete.json` |
| 2 | 负责人 | CI 主机 | 先 `python3 scripts/deploy/sim_world.py generate --config sim-generate.json --dry-run`，再 `time ( security find-generic-password -s <工作台 Keychain 条目> -a <owner 用户名> -w \| python3 scripts/deploy/sim_world.py generate --config sim-generate.json --origin https://<APP_HOST_NAME>:<PORT> --ca-file <CA> --username-file <用户名文件> --password-stdin )` | `time` 的 real 秒数，记为“sim 生成墙钟（空闲 / 派生）”；输出中的计数（生成的对象数）一并记下 |
| 3 | 调度员 | DATA_HOST | 负责人开始第 2 步时记下时刻；结束时再记 | 两个时刻（核对墙钟） |
| 4 | 负责人 | CI 主机 | 用完删除 world：`security find-generic-password -s <工作台 Keychain 条目> -a <owner 用户名> -w \| python3 scripts/deploy/sim_world.py delete --config sim-delete.json --origin https://<APP_HOST_NAME>:<PORT> --ca-file <CA> --username-file <用户名文件> --password-stdin` | 输出 `status` |

注意：sim 生成只写 `runtime.nexloop_sim_objects`，不写 `ontology.objects`，派生触发器不参与。它是对照组，空闲与派生两轮的墙钟应当只差噪声（§2 第 1 条）。

## 2. 擦除批量假名化 M≈50（第 3 步用 perf-1，第 5 步用 perf-2）

| 顺序 | 谁 | 在哪 | 做什么 | 看什么、记到哪 |
| --- | --- | --- | --- | --- |
| 1 | 负责人 | CI 主机 | 合成顾客发言 25 轮：`( printf 'perf-1\t%s\n' "$(security find-generic-password -s nexloop/<stage>/perf-1 -w)" ) \| uv run --frozen python scripts/acceptance/synthetic_customer.py --config customers.json --plan plan-perf-1.json`（脚本自己从 stdin 读 `别名<TAB>口令`；必须运行脚本文件，不要用 `python -`） | 汇总行：25 轮都是 sent 或 replayed |
| 2 | 调度员 | DATA_HOST | 擦除前对象数：`docker run $COMMON -v "$RUN/metrics.dsn:/run/metrics-dsn:ro" "$BACKEND_IMAGE" /opt/release/scripts/acceptance/relation_derive_timing.py --dsn-file /run/metrics-dsn --tenant "$TENANT_ID" erasure --consumer <perf-1 的 consumer_id>` | `objects_total`（应当在 50 左右）记为“擦除前对象数” |
| 3 | 负责人 | CI 主机 | 先 `security find-generic-password -s <工作台 Keychain 条目> -a <owner 用户名> -w \| python3 scripts/deploy/erase_synthetic_consumer.py erase --origin https://<APP_HOST_NAME>:<PORT> --ca-file <CA> --username-file <用户名文件> --password-stdin --config erase-perf-1.json --dry-run`（读预检：persona 为 simulated_agent、可擦除），再 `security find-generic-password -s <工作台 Keychain 条目> -a <owner 用户名> -w \| python3 scripts/deploy/erase_synthetic_consumer.py erase --origin https://<APP_HOST_NAME>:<PORT> --ca-file <CA> --username-file <用户名文件> --password-stdin --config erase-perf-1.json` 正式执行 | 输出中 perf-1 已进入擦除；**记下发起时刻** |
| 4 | 调度员 | DATA_HOST | 等 keeper 把它推进到 `completed`（几分钟内；工作台擦除页或再跑一次第 2 步的命令看 state） | `completed` 的时刻 |
| 5 | 调度员 | DATA_HOST | 再跑一次第 2 步的命令 | **`sync_purge_seconds`**（判定用）、`request_to_completion_seconds`（仅参考）、`by_item_class` 中的 `run_file`（不计入）|

第 5 步用 perf-2 和 `erase-perf-2.json`、`plan-perf-2.json` 重复本节（命令中的 perf-1 换成 perf-2，其余参数不变）。

## 3. real p95（调度员，24 h 窗口，不需要负责人）

- **relay 各步骤的 p95**（§4 门槛用的就是这个）：`docker run $COMMON -v "$RUN/metrics.dsn:/run/metrics-dsn:ro" "$BACKEND_IMAGE" /opt/release/scripts/acceptance/relation_derive_timing.py --dsn-file /run/metrics-dsn relay --since <ISO> --until <ISO>`（DATA_HOST，`$COMMON`、`$RUN`、`$BACKEND_IMAGE` 见 NX-054-stage-publish.md §0）。它调用 `authz.nexloop_relation_relay_p95`（0336）：取窗口内 `runtime.nexloop_process_samples` 中 kind 为 `relay_step` 的每分钟样本，按 relay 步骤输出这些分钟 p95 的中位数 `p95_ms_median`（以及最大值 `p95_ms_max`、样本数与调用数）。`goal_create`、`step_create`、`assignment_create` 三步写的对象正是派生要跟随的。
  - 发布前基线：`--since` 为发布前 24 小时、`--until` 为发布时刻（调度员今晚先取一份）。
  - 发布后：`--since` 为发布时刻、`--until` 为发布后 24 小时。
- **API 读写两类的 p95**（0323，补充，不作为门槛）：0323 在数据库中只按类（read / write）合并，per-route 的明细只在 API 进程内（`eios.api.metrics`，可选的本机导出），数据库里没有 per-route 指标。按类的值在指标快照的 `api.classes.read.p95_ms` / `api.classes.write.p95_ms`（5 分钟窗口），也是告警规则 v19 `api_read_p95_high` 读的那个值；要看 24 h 的走势，就在工作台的指标页按时间读，或者在 24 h 内定时记录这两个值。

## 4. 记录

| 场景 | 空闲（第 3 步） | 派生（第 5 步） | 门槛（§4） | 判定 |
| --- | --- | --- | --- | --- |
| sim 生成墙钟（s） | | | 增加不超过 25%（对照组，应当几乎不变） | |
| 擦除前对象数 | perf-1： | perf-2： | — | |
| 擦除 `sync_purge_seconds` | | | 增加不超过 25% | |
| relay `goal_create` / `step_create` / `assignment_create` 的 `p95_ms_median`（ms） | | | 增加不超过 10 ms | |
| API read / write p95（参考） | | | — | |

执行人：负责人（第 1、2 节中标为负责人的步骤）、调度员（其余）。判定人：L6（数字与门槛），负责人确认。
