# NX-054：在 stage 发布关系类型（执行单）

依据：`NX-054-P1.md` §3、§4，`NX-054-P2.md`。release 42（main 76a9ab00，库 head 0324）中 P1（0319）与 P2（0321）都已在线；关系类型还没有按租户发布，所以派生触发器处于空闲状态。本执行单由调度员执行，L5 准备。

工具：

| 用途 | 脚本 | 连接 |
| --- | --- | --- |
| 只读预检 | `scripts/acceptance/relation_types_precheck.py` → `authz.nexloop_relation_precheck`（0336） | 只读的 metrics 登录 `nexloop_metrics`：服务端强制只读，只能执行函数、对任何表都没有权限；工具核对会话身份，并设 statement_timeout 60s、lock_timeout 3s |
| 基线测量 | `scripts/acceptance/relation_derive_timing.py` → `authz.nexloop_relation_erasure_timing` / `authz.nexloop_relation_relay_p95`（0336）；`scripts/deploy/sim_world.py generate`；`scripts/acceptance/synthetic_customer.py`；`scripts/deploy/erase_synthetic_consumer.py` | 计时用 `nexloop_metrics`（同上）；后三个是负责人工作台与合成顾客的既有入口 |
| 发布与回退 | `scripts/deploy/relation_types_publish.py` | configurator 登录窗口（由 bootstrap socket DSN 打开，只在这一次调用期间可登录） |

## 0. 准备（DATA_HOST，私有临时目录 `$RUN`，与 run-34 §1.4 相同的形态）

| 文件 | 来源 | 权限 |
| --- | --- | --- |
| `$RUN/src/` | 发布提交 76a9ab00 的 `git archive` | 0755，只读挂载 |
| `$RUN/boot.dsn` | `host=/var/run/postgresql dbname=$NEXLOOP_DB user=nexloop_bootstrap password=<bootstrap 口令>` | 0600，属主 999 |
| `$RUN/metrics.dsn` | DATA_HOST 已有的 `secrets/backup/metrics.dsn`（G-METRICS-DSN：`nexloop_metrics` 经本机 socket，SCRAM）的副本 | 0600，属主 999 |

```
COMMON="--rm --network none --read-only --user 999:999 --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  -v $RUN/src:/opt/release:ro -v $NEXLOOP_ROOT/run/postgresql:/var/run/postgresql --entrypoint python"
```

- **configurator DSN 怎么取得**：不需要单独准备。`relation_types_publish.py` 用 `configurator_window`（与 stage_secrets register、alert_rules_apply 相同）：
  1. 经 bootstrap socket DSN 给 `nexloop_configurator` 设一个一次性口令和很短的有效期，并临时允许登录；
  2. 把 configurator DSN 写进一个 0600 的临时文件，本次调用用它连接；
  3. 结束时恢复 NOLOGIN、清除口令、删掉临时文件。

  如果 M9 已经把 bootstrap 设为 NOLOGIN，按 M9 的窗口规则临时恢复，执行完再恢复 NOLOGIN。

前提：迁移 0336（这几个只读函数）已经应用。预检和计时不使用 bootstrap 口令（L6 复核 R-2）。

## 1. 只读预检（P1 §3）

```
docker run $COMMON -v "$RUN/metrics.dsn:/run/metrics-dsn:ro" "$BACKEND_IMAGE" \
  /opt/release/scripts/acceptance/relation_types_precheck.py --dsn-file /run/metrics-dsn --tenant "$TENANT_ID" > precheck-before.json
```

- 输出只有计数，不含 id、属性值或业务内容：
  - `fields`：每个首批关系类型、每个 world 一行，包含 objects、with_value、hex64、pseudonyms、target_same_world、target_other_world、dangling、distinct_targets、sharing_a_target；
  - `supersedes`；
  - `assessments`（与首批重名的 relation_name、version 0 的数量）；
  - `catalog`（已发布的版本、每个类型的最新版本、派生映射行数及其中仍在派生的数量 `deriving`、关系数、其中派生关系数 `derived_relations`）。
- 退出码 0 才继续。退出码 1 时，`blocking` 会写明原因：跨 world 的目标、既不是 64 位 hex 也不是擦除假名的值、RelationshipAssessment 的重名。先处理，再重跑预检。
- 发布前预期：`catalog.versions` 为空，`derivation_map_rows` 为 0，`relations` 为空。

## 2. 基线：空闲状态（发布之前）

范围：sim 组，加上 real 的既有指标。不向 real 注入流量。

1. **sim 生成 N=200（对照组）**：在负责人的 Mac 上执行：

   ```
   time sim_world.py generate --config <N=200 的私有配置> --origin … --username-file … --password-stdin
   ```

   在一个新建的 simulation world 中进行，记录墙钟时间。

   注意：sim 生成只写 `runtime.nexloop_sim_objects`，**不写 `ontology.objects`**（0261），派生触发器不会被触发。所以它是对照组：发布前后的差异应当只是噪声。
2. **擦除批量假名化 M≈50**：
   1. 新建一个 simulated_agent 合成顾客（`webchat_consumers.py` 加 `erase_synthetic_consumer.py mark`），记为 `perf-1`。
   2. 用 `synthetic_customer.py` 让它发言 25 轮（不等回复），大约产生 50 个 Message 对象，以及 Conversation 和 ConsumerOwnership。
   3. 记录擦除前的对象数：

      ```
      docker run $COMMON -v "$RUN/metrics.dsn:/run/metrics-dsn:ro" "$BACKEND_IMAGE" \
        /opt/release/scripts/acceptance/relation_derive_timing.py --dsn-file /run/metrics-dsn --tenant "$TENANT_ID" erasure --consumer <perf-1 的 consumer_id>
      ```

      看 `objects_total`，应当在 50 左右。
   4. 负责人执行 `erase_synthetic_consumer.py erase`，等 keeper 把它推进到 `completed`。
   5. 再执行一次同一条命令，记录 `sync_purge_seconds`：同步 purge 的时间跨度，即该请求（精确的 `request_ref = 'erasure:<request_id>'`）除 `run_file` 以外所有 tombstone 的首尾间隔。`run_file` 要等 keeper 删除文件后在另一个事务里写入，取决于调度周期，单独列在 `by_item_class` 中，不计入（L6 复核 R-3）。`request_to_completion_seconds` 仅供参考。
3. **real p95**：以发布前 24 小时为窗口：

   ```
   ... relation_derive_timing.py --dsn-file /run/metrics-dsn relay --since <发布前 24h，ISO> --until <发布时刻，ISO>
   ```

   记录每个 relay 步骤的 `p95_ms_median`。`goal_create`、`step_create`、`assignment_create` 这几步写的对象正是派生要跟随的。

## 3. 发布

```
docker run $COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/relation_types_publish.py --dry-run
# 预期 {"catalog_version": 1, "relation_types": 26, "deriving": 26, "longest_ownership_chain": 3, "manifest_digest": "…"}
docker run $COMMON -v "$RUN/boot.dsn:/run/bootstrap-dsn:ro" "$BACKEND_IMAGE" \
  /opt/release/scripts/deploy/relation_types_publish.py --tenant "$TENANT_ID" --bootstrap-dsn-file /run/bootstrap-dsn
# 预期 result: {"published": true, "replayed": false, "new_relation_types": 26, "longest_ownership_chain": 3}
```

- 重跑同一条命令是重放：`replayed: true`，`new_relation_types: 0`。
- 发布后再跑一次第 1 步的预检：`catalog.versions` 只有版本 1，`derivation_map_rows` 为 26。从这一刻起，新写入或属性有变化的对象会派生关系；存量对象等到 P3 回填。

## 4. 基线：派生状态（发布之后）

重复第 2 步，参数相同：

- sim 生成 N=200，用新的 world；
- 擦除换一个新的合成顾客 `perf-2`，同样 25 轮；
- real p95 取发布后 24 小时的窗口。

**门槛**（P1 §4）：

| 场景 | 判定 |
| --- | --- |
| 擦除 `sync_purge_seconds` | 派生状态比空闲状态增加不超过 25% |
| sim 生成墙钟 | 派生状态比空闲状态增加不超过 25%（对照组，应当几乎不变） |
| real 各 relay 步骤的 `p95_ms_median` | 增加不超过 10 ms |

- 超过门槛时，按 P2 §3 的顺序优化（P-3 缓存派生映射，P-4 集合化），**或者执行第 5 步回退**。
- 两次擦除的样本很少，`request_to_completion_seconds` 又由 keeper 的调度间隔主导，所以以 `sync_purge_seconds` 为准。数字记入证据。

## 5. 回退：停止派生

```
docker run $COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/relation_types_publish.py --dry-run --stop-derivation --from-version 1
# 预期 {"catalog_version": 2, "deriving": 0, …}
docker run $COMMON -v "$RUN/boot.dsn:/run/bootstrap-dsn:ro" "$BACKEND_IMAGE" \
  /opt/release/scripts/deploy/relation_types_publish.py --tenant "$TENANT_ID" --bootstrap-dsn-file /run/bootstrap-dsn --stop-derivation --from-version 1
```

- 效果：
  - 发布目录版本 2，每个首批类型都有一个不带 `source_property` 的新版本；
  - 派生映射跟随最新版本，所以**从这次提交起不再派生新的关系**；
  - 已经派生的关系，在其源对象下一次属性变化时删除，其余的暂时保留。P4 之前没有任何读方使用关系，所以保留它们不影响业务。
- 这条路径能在 stage 执行：它用的是同一个发布函数和同一个 configurator 窗口，只是 manifest 不同。本地 PG 已经验证了“发布 → 停止 → 新写入不派生、已有派生关系在下一次变化时删除 → 恢复”的全过程（`tests/test_relation_publish_runbook_pg.py`）。
- 已发布的版本不可变，所以**恢复派生要发布更新的版本**：`--resume-derivation --from-version 2`（版本 3）。
- **`--from-version` 必须是当前最新的已发布版本**（L6 复核 R-1）：脚本在窗口内先读 `control.nexloop_relation_catalog_head`，比它旧的版本一律拒绝。否则旧版本会被重新构造成一个已经存在的版本，返回 replayed，看起来像回退成功，实际上什么都没做。同一条命令重跑（head 已经是 from+1）按重放接受；如果内容不同，数据库会返回 40001 拒绝。
- **回退后的预期值**（再跑一次第 1 步的预检）：
  - `catalog.versions` 为 [1, 2]；`latest_type_versions` 全部为 2；
  - `derivation_map_rows` 为 26，`deriving` 为 0；
  - `derived_relations` 不会立刻归零，见下一条。
- **回退后仍然存在的东西**：
  - **两个派生触发器仍然在线**。每次对象写入仍有一次按类型的索引探测，属性有变化的更新仍会在提交时排一个事件，然后发现没有可派生的属性。这是空闲状态的开销（P2 §3 实测 0–10%），不是零。要完全去掉，需要一个新迁移删除触发器，这属于另一个决定。
  - **已经派生出的关系**：源对象的属性下一次变化时删除；源对象如果从此不再变化，它的派生关系会一直保留。P4 之前没有读方使用关系，所以不影响业务。
  - **擦除会清理它们**：擦除假名化属性时，对象的属性发生变化，按最新版本（没有 `source_property`）删除派生关系；擦除删除对象时，BEFORE DELETE 触发器删除它的所有关系。所以擦除之后，不会留下指向已擦除 Consumer 的关系。

## 6. 证据

把 `precheck-before.json`、发布输出、发布后的预检、两轮基线的数字，以及判定结论记入 NX-054 的证据。**执行人与判定人分开写**。

## 7. P3 回填的执行身份与凭据

回填（`docs/design/relation-backfill-p3.md`）以专门的服务主体 `relation_backfiller` 执行。它在 `service-grants.v1.json` 第 20 版中登记，只授权 `eios:action:nexloop.relation.backfill:1`，数据库角色为 `nexloop_domain_worker`。它是一次性的主体：stage 上的回填跑完、verify 核对平衡之后，用下一版把它撤销。

1. **生成凭据**（管理机，私有的 stage secrets 树）：
   1. `stage_secrets.py setup-credentials --out <树>`：按 `stage-principals.v1.json` 第 5 版，在 `setup/relation-backfiller-credential` 生成这个主体的凭据文件（0600），并加入 `management/service-secrets.json`；重复执行不会再新增。
   2. `stage_secrets.py register --out <树> --tenant "$TENANT_ID" --bootstrap-dsn-file <bootstrap socket DSN 文件>`：按 v20 注册这个主体，写入它的唯一一条授权（与其他主体的 register 相同，经 configurator 窗口）。
2. **放到私有配置里**（APP_HOST，私有临时目录 `$BF`，在 tmpfs 上新建：`BF=$(mktemp -d /dev/shm/nexloop-backfill.XXXXXX)`，mktemp 建出的目录就是 0700；凭据和签名密钥的副本不落盘）：

   | 文件 | 来源 | 权限 |
   | --- | --- | --- |
   | `$BF/service-credential` | 树中 `setup/relation-backfiller-credential` 的副本 | 0600 |
   | `$BF/dsn` 与 `$BF/pg-client.crt`、`$BF/pg-client.key` | 一个现有 `nexloop_domain_worker` 服务的 DSN 文件和客户端证书、私钥（例如 retention keeper 的那一组）的副本：回填与它们使用同一个数据库角色，身份由服务凭据区分。**DSN 中的 `sslcert`、`sslkey`（以及 `sslrootcert`，如有）改为指向 `$BF` 中的副本**，不引用原服务的 secrets 目录 | 0600 |
   | `$BF/signing.key` | 服务进程使用的签名密钥文件的副本 | 0600 |

3. **执行**：`python scripts/deploy/relation_backfill.py --database-url-file $BF/dsn --signing-key-file $BF/signing.key --service-credential-file $BF/service-credential --artifact-root <临时空目录> <命令> [--run-id <run>]`。命令依次为 `preflight` → `start` → `run --max-batches 1`（校准）→ `run` → `verify` → `revert`（演练）→ 新的 `start` 与 `run` → `verify`（设计稿 §8）。
   - 工具只开一条连接，计入 ADR-037 中运维预留的 7 个连接；不在 APP_HOST 应用的连接预算中登记。
4. **收尾**：回填完成并核对之后，`rm -rf -- "$BF"`。`$BF` 在 tmpfs 上，内容只在内存中，删除即释放，不需要 shred；`service-grants` 的下一版（v21）去掉 `relation_backfiller`，register 时撤销它的授权和凭据。

## 8. 执行单：发布与回填（release 45/46，迁移 0344、0349、0350 已在线）

按顺序执行；每一步的输出都存进 `$RUN`（DATA_HOST）或 `$BF`（APP_HOST）旁边的证据目录。**执行人与判定人分开写**。

| 步 | 在哪里 | 谁执行 | 做什么 | 通过条件 | 不通过或要撤回时 |
| --- | --- | --- | --- | --- | --- |
| 0 | DATA_HOST | 调度员 | 只读确认迁移头：`select max(version) from <迁移记录表>` 不低于 0350；`ontology.nexloop_relation_derive_one` 存在；两个派生触发器都指向 `nexloop_relation_derive_each` 且启用；记下发布前关系目录的头版本（预检输出的 `catalog.versions` 最大值，首次为空；只作记录，回退时另读） | 三项都成立 | 停止，报 L5（不需要回退，什么都没改） |
| 1 | DATA_HOST | 调度员 | §0 准备 `$RUN`（`boot.dsn` 需要 bootstrap 口令：从负责人的 Keychain 经管道写入，口令不进命令行和日志） | 文件 0600，属主 999 | 删除 `$RUN` |
| 2 | DATA_HOST | 调度员 | §1 只读预检 → `precheck-before.json` | 退出码 0，`blocking` 为空 | 按 `blocking` 处理后重跑（只读，无需回退） |
| 3 | CI 主机（负责人工作台会话）+ DATA_HOST | **负责人**（sim 生成、合成顾客擦除要用负责人工作台的凭据）；调度员计时 | §2 空闲基线：sim N=200、擦除 perf-1、real p95 24 h | 数字记入证据 | 只是测量；sim world 用完由 `sim_world.py` 删除 |
| 4a | DATA_HOST | 调度员 | **备份**：`sudo -n /usr/bin/systemctl start nexloop-pg-diff.service`，再 `systemctl show -p Result,ExecMainStatus nexloop-pg-diff.service` | `Result=success`、`ExecMainStatus=0` | 不发布；查备份失败的原因 |
| 4 | DATA_HOST | 调度员 | §3 发布：先 `--dry-run`，再正式发布（configurator 窗口经 bootstrap DSN 自动开关）；再跑一次预检 | `relation_types: 26`、`deriving: 26`；预检 `derivation_map_rows: 26` | §5 回退：`--stop-derivation --from-version <回退时的头版本>`。**回退时**再用 `control.nexloop_relation_catalog_head` 读一次当前头版本（发布之后的值，首次发布后是 1），不要用第 0 步记下的发布前的值；脚本会拒绝比头版本旧的值 |
| 5 | CI 主机（负责人工作台会话）+ DATA_HOST | **负责人** + 调度员 | §4 派生基线（新 world、perf-2、发布后 24 h），对照门槛 | 三项都在门槛内 | 超标：执行 §5 回退，P3 不开始 |
| 6 | 管理机 | 调度员 | §7 第 1 步：`setup-credentials`，再 `register`（v20 只给 `relation_backfiller` 一条授权；register 是幂等的，其他主体 `changed: false`） | register 输出中 `grants_added` 为 1，`credentials_created` 只有这个主体 | 撤回授权：service-grants v21 去掉这个主体后再 register（L5 准备） |
| 7 | APP_HOST | 调度员 | §7 第 2 步：`BF=$(mktemp -d /dev/shm/nexloop-backfill.XXXXXX)`，放入凭据、DSN、证书、私钥、签名密钥的副本，DSN 中的证书路径指向 `$BF` | 文件全部 0600 | `rm -rf -- "$BF"` |
| 8 | APP_HOST | 调度员 | `relation_backfill.py ... preflight` | `blocking` 为空；`digest` 记下 | 只读；按 `blocking` 处理 |
| 9a | DATA_HOST | 调度员 | **备份**：同 4a | `Result=success`、`ExecMainStatus=0` | 不开始回填 |
| 9 | APP_HOST | 调度员 | `... start --run-id p3-1`，然后 `... run --run-id p3-1 --max-batches 1` | 一行 `event=batch`：`elapsed_ms` < 300、`waiters` 为 0；不满足就停下，把这一行交给 L5 校准 | `... abort --run-id p3-1`，再 `... revert --run-id p3-1` |
| 10 | APP_HOST | 调度员 | `... run --run-id p3-1`（前台；中途可以 Ctrl-C，退出码 3，再执行同一命令继续） | 最后一行 `event=complete`、`completed: true`；运行期间对照 0323 的写请求 p95（自动暂停暂不看 p95） | 见下面的中止条件；`abort` 后 `revert` |
| 11 | APP_HOST | 调度员 | `... verify` | 每一行 `a == b`、`extra == 0`，`skipped_remaining == 0`；悬空和擦除中的数量照实记录 | 不平：不进入第 13 步，`revert`，把 verify 输出交给 L5 |
| 12 | APP_HOST | 调度员 | revert 演练：`... revert --run-id p3-1`，再 `verify` | 状态 `reverted`；`b` 回到回填前的值（只剩派生出的关系）。如果回填之后有 sim world 被清除，revert 输出的 `deleted` 会小于 created 的行数，差额就是随 world 一起删除的关系（设计稿 §3.1 的 B-1），不是错误 | 重跑 `revert`（幂等）；仍不到 `reverted` 就停下报 L5 |
| 13 | APP_HOST | 调度员 | 正式回填：`... start --run-id p3-2`、`... run --run-id p3-2`、`... verify` | 同第 11 步 | 同第 10、11 步 |
| 14 | APP_HOST、管理机 | 调度员 | 收尾：`rm -rf -- "$BF"`；L5 准备 service-grants v21（去掉 `relation_backfiller`），合入后 register 撤销 | `$BF` 不存在；register 输出 `grants_revoked` 为 1 | — |

- **必须由负责人执行的**：第 3、5 步中用到负责人工作台凭据的操作（在 CI 主机上的负责人工作台会话中：sim 生成、合成顾客的标记与擦除）。其余步骤按 2026-10-21 的口径由调度员执行（dev/test 环境、没有真实用户）。
- **前置条件**：release 45/46 已上线（含 0344、0349、0350 与 `relation_backfill.py`）；第 5 步通过；第 6 步的 register 之后，`relation_backfiller` 的凭据有效。
- **中止**：任何一步不满足通过条件就停下。回填运行中想停止：`... abort --run-id <run>`（终态，之后 revert 或开新运行）。回填运行中出现下列任一情况也要中止（Ctrl-C，然后 abort）：
  - 连续 5 批的 `waiters` 大于 3，或者 runner 输出 `event=paused` 持续超过 10 分钟；
  - 同一个 cursor 上 `event=retry` 的 55P03 / 40001 / 40P01 累计超过 20 次（说明与 real 写入或擦除持续冲突）；
  - 出现 `code=connection` 的重试 5 次之后 runner 退出（结果不明的批已经按意图 id 对账，重新 `run` 前先看 `status`）；
  - 0323 的写请求 p95 比回填前高出 25% 以上，或者超过 500 ms。
- **运行容器**：第 8–13 步在 APP_HOST 上用发布镜像执行（与服务相同的镜像、只读根文件系统、`--user` 非 root、`--cap-drop ALL`），网络与 domain worker 服务相同（它要经 TLS 连 DATA_HOST 的 PostgreSQL）；`$BF` 只读挂载。


## 9. P4（P4a + P4c）上线前

P4a（读侧并集，0360）与 P4c（purge 取并集、开始时固定目标，0361）同一批上线（设计稿 `relation-read-p4.md` §3 R4）。stage 就是 real，没有另一套环境。

| 步 | 在哪里 | 谁执行 | 做什么 | 通过条件 | 不通过时 |
| --- | --- | --- | --- | --- | --- |
| 0 | DATA_HOST | 调度员 | 只读：`select count(*) from control.nexloop_consumer_erasures where state not in ('pending_confirmation','completed','completed_with_holds')` | 结果为 **0**：没有进行中的擦除（0361 在下一步开始时才固定关系目标，已经过了 messages 阶段的擦除补不回被切断的链） | 不部署；等这些擦除跑到 completed（keeper 正常推进），再重新执行第 0 步 |
| 1 | DATA_HOST | 调度员 | 备份：`sudo -n /usr/bin/systemctl start nexloop-pg-diff.service`，`systemctl show -p Result,ExecMainStatus nexloop-pg-diff.service` | `Result=success`、`ExecMainStatus=0` | 不部署 |
| 2 | — | 调度员 | 按 ack-policy 部署这一批（迁移 0360、0361，告警规则 v19） | 迁移头含 0361；`alert_rules` 已应用 v19 | 按发布手册回退该批 |
| 3 | DATA_HOST | 调度员 | 安装 `nexloop-relation-read-gaps` 的 timer 与 service（示例在 `deploy/stage/systemd/`），把 `relation-read-gaps` 加进 DATA_HOST 的 host-health 配置，再执行设计稿 §2.5 的演练 | 演练两轮都符合预期 | 停用 timer，报 L5 |
