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
