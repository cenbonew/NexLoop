# 命令单：发布 7 在 stage 的一次性部署（2026-10-16）

**发布内容**（main b39b61b）：
- 迁移 0199 `effect_query_window`：unknown 意图的退避生效，加 6 小时自动查询窗口（run-unknown-reconcile）。
- 迁移 0200 `message_run_reply_settle`：没提交回复的 message Run 判为失败，回复保证立即到期（reply-failfast）。
- 迁移 0201 `context_conversation_turns`：pack 带上一轮对话和 Consumer（run-context-sources）。
- 门禁新项 `webchat_delivery_profile_matches_route`（run-delivery-ca）。
- context-sources：stage-principals 中 relay_source 的 message_read_rule 字段增加 `accepted_at`、`conversation_id`、`sequence`。
- runtime-worker 改用 `nexloop_domain_worker`（run-runtime-role）。

本单把以下命令单合成一份，按顺序执行：run-33、NX-035 部署手册 §2.6、run-34 §4、run-assembly §0.3/§0.5、run-metrics-gate §2、run-delivery-ca、run-runtime-role、run-context-sources、run-unknown-reconcile、run-model-smoke §2.2。命令和参数一律取自这些单子或脚本的 argparse，出处写在每步后面。

只用角色名和变量。类别标注：
- **B**：需负责人确认；
- **sudo-A**：需要 APP_HOST 的 sudo；
- **sudo-D**：需要 DATA_HOST 的 sudo。

**不可逆点**：第 2 步迁移。迁移之前的任何一步失败，都可以原样撤回；迁移之后只能前向修复（见 §7）。

## 0. 参数与前提

| 变量 | 含义 | 出处 |
|---|---|---|
| `RELEASE_DIR` | 发布 7 的签名发布目录（含 `images/*.tar` 与 manifest），由 b39b61b 构建 | run-34 §4.1 |
| `RELEASE_CHECKOUT` | b39b61b 的检出，管理机与 APP_HOST 各一份 | run-metrics-gate §0 |
| `RUN` | DATA_HOST 的 stage 管理树（含 `management/`、`boot.dsn`）；`$RUN/src` 要换成 b39b61b 的源码树 | run-assembly §0.2 |
| `COMMON`、`WINDOW` | DATA_HOST 一次性容器参数，原样取自 run-assembly §0.3 | run-assembly §0.3 |
| `BACKEND_IMAGE` | **新**的 backend 引用（第 4.1 步 dry-run 产物 `compose.images.env` 中的值）；第 4 步之前也可用，只要 `docker load` 过 | run-34 §4.4 |
| `TENANT_ID`、`KEY_ID`、`CREDENTIAL_EXPIRES_AT` | 与首次 register 相同的值。`CREDENTIAL_EXPIRES_AT` 不同会重写已有主体的凭据事实 | run-assembly §0.5 |
| `PGBACKREST_COMMAND_JSON`、`PGBACKREST_VERSION` | 与 DATA_HOST 定时任务 `nexloop-pg-diff` 使用的相同（版本 2.59.3） | NX-035 部署手册 §2.6，deploy-days 2.6 |
| `C` | `sudo docker compose -p nexloop-app -f $NEXLOOP_ROOT/config/compose.app.yaml --env-file $NEXLOOP_ROOT/config/compose.app.env --env-file $NEXLOOP_ROOT/config/compose.images.env` | run-delivery-ca §1 |

**时机**：选一个没有进行中冒烟和交互的时段。第 4 步会重建全部 25 个 APP_HOST 服务，期间 WebChat 不可用几十秒到几分钟。已受理的消息和 effect 都在 PG 里，重建后继续处理。

**已单独执行过的命令单**：run-runtime-role 和 run-delivery-ca 如果已经在 stage 上执行过，第 4.3 步对应的子项会显示“已存在”，跳过即可，不要重复替换文件。

## 1. 部署前（只读为主）

### 1.1 pgBackRest 备份核验（DATA_HOST，sudo-D）

```
sudo docker exec -u postgres nexloop-data-postgres-1 pgbackrest --stanza=nexloop info
sudo -u nexloop-backup /opt/nexloop/backup-venv/bin/nexloop-doctor --backup --metrics-database-url-file "$NEXLOOP_ROOT/secrets/backup/metrics.dsn" \
  --backup-plan "$RELEASE_CHECKOUT/deploy/configuration/backup.v1.json"
```

- **出处**：run-33 0.1 与 0.4；`metrics.dsn` 位置取自 metrics_data_host.sh 第 4 步。
- **预期**：
  - `info` 中 repo1 至少有一份 full，状态 ok，archive 的 min/max 有值；
  - doctor 中 `latest_backup_verified`、`wal_archiving`、`restore_state_open` 为 true。
- **不符**：停下，不进入第 2 步。

### 1.2 记录当前 catalog 版本（DATA_HOST，只读）

```
docker run $COMMON $WINDOW -i "$BACKEND_IMAGE" - <<'PY'
import psycopg
with psycopg.connect(open('/run/bootstrap-dsn').read().strip()) as c:
    c.execute('set transaction read only')
    print(c.execute('select max(version) from control.schema_migrations').fetchone()[0])
PY
```

- **出处**：容器形态同 run-model-smoke §2.2；查询同 run-assembly §1a 的“核对”。
- **预期**：`0198`。是别的值就停下，报调度员。
- 把这个值和时间写进私有证据。

## 2. 迁移门槛 + 应用 0199–0201（DATA_HOST；**B**，sudo-D）

### 2.1 迁移前差异备份

```
sudo -u nexloop-backup /opt/nexloop/backup-venv/bin/nexloop-backup pg --recorder-dsn-file "$NEXLOOP_ROOT/secrets/backup/recorder.dsn" \
  --pgbackrest-command-file "$PGBACKREST_COMMAND_JSON" --stanza nexloop --expected-version "$PGBACKREST_VERSION" --repo repo1 --kind pg_diff
```

- **出处**：NX-035 部署手册 §2.6。参数取自 `backup.backup_main` 的 argparse（`--kind` 可选 `pg_full|pg_diff|pg_incr`）。
- **预期**：`ok: true`，`verify_status: verified`，退出码 0。
- 门槛要求 60 分钟以内的已验证备份，所以 2.1 到 2.3 之间不要超过 60 分钟。

### 2.2 门槛预检（只读）

把 1.2 的查询换成 `select authz.nexloop_migration_gate()`，其他不变。

- **预期**：`allowed: true`。
- **拒绝时**：按输出里的拒绝代码处理（代码表见 `NX-035-local-implementation.md` §3），不绕过。迁移门槛没有豁免开关。

### 2.3 应用迁移

`$RUN/src` 必须是 b39b61b 的源码树，因为迁移文件和 catalog 从这里读取：

```
docker run $COMMON $WINDOW "$BACKEND_IMAGE" -c 'import sys; from nexloop_eios.backup import migrate_main; sys.exit(migrate_main(["--database-url-file", "/run/bootstrap-dsn"]))'
```

- **出处**：
  - `nexloop-migrate` 就是 `nexloop_eios.backup:migrate_main`（pyproject `[project.scripts]`），唯一的参数是 `--database-url-file`；
  - `$COMMON` 已含 `--entrypoint python`，所以这里用 `-c` 调用同一个函数；
  - 运行身份为 bootstrap，经 socket 连接，与 run-assembly §0.5 的 register 相同。
- **预期**：`{"migrated": true, "applied": ["0199", "0200", "0201"]}`，退出码 0。
- **拒绝时**：输出 `{"migrated": false, "reasons": [...]}`，退出码 1，没有执行任何迁移。按代码处理后从 2.1 重来。
- **不手写 SQL**。

### 2.4 迁移后

1. 重跑 1.2 的查询，预期为 `0201`。
2. 立即再执行一次 2.1 的差异备份，预期 `verified`。

## 3. 重跑 register，更新 relay_source 的 message_read_rule（DATA_HOST；**B**，sudo-D）

**出处**：run-context-sources §3 第 2 步；命令形态同 run-assembly §0.5；参数取自 `stage_secrets.py` 的 `register` 子命令（`--out --tenant --configurator-dsn-file|--bootstrap-dsn-file --signing-key-id --credential-expires-at --dry-run`）。

### 3.1 先比对，再 dry-run

```
git -C "$RELEASE_CHECKOUT" diff <上次 register 用的提交> b39b61b -- deploy/authorization/
docker run $COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/stage_secrets.py register --out /stage --tenant "$TENANT_ID" --dry-run
```

- **diff 的预期**：只有 `stage-principals.v1.json` 中 relay_source 的 message_read_rule，`fields` 增加 `accepted_at`、`conversation_id`、`sequence`，`purpose` 文本变了。
  - 这一点已在 CI 主机核对：`git diff 0e5b681 b39b61b -- deploy/authorization` 只有这一处。
  - 上次 register（run-assembly §0.5）之后，authorization 目录还有 G-WEBCHAT-AUTH、G-WORKBENCH-AUTH 等提交。它们改的是 roles 和 webchat_consumers 的输入，不是 register 读的两个文件。如果 diff 显示 `stage-grants` 或其他主体也有变动，停下报调度员。
- **dry-run 的作用和局限**：
  - dry-run 不连库，只列出清单中的主体、授权和 message_read_rule 的主体名（`register(dry_run=True)`）；
  - 所以它**不能**显示与库中现状的差异，只能确认主体数（24）和凭据文件数（33）与上次一致。
  - 与库的差异只能看正式执行的输出（3.2）：前后两段是分开计算的，`service_grants.apply` 按事实逐项比较，`_apply_extra_facts` 也只写与库中不同的 message_read_rule。

### 3.2 正式执行

```
docker run $COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/stage_secrets.py register --out /stage --tenant "$TENANT_ID" \
  --bootstrap-dsn-file /run/bootstrap-dsn --signing-key-id $KEY_ID --credential-expires-at "$CREDENTIAL_EXPIRES_AT"
```

- **预期**：
  - `message_read_rules_written: 1`，只有 relay_source；
  - `credentials_created: []`，`grants_added: 0`，`grants_revoked: 0`，`extra_grants_outside_manifest: 0`；
  - `changed: true`，这次的改动只来自 message_read_rule。
- **其他已登记项会不会变**：
  - 正常情况下不会。主体和授权都按事实做幂等比较，凭据按 credential_id 判断是否已存在；
  - 如果 `grants_added` 或 `grants_revoked` 不是 0，或者 `credentials_created` 不为空，说明库里的授权与清单已经有偏差（不是这次发布造成的）。记下数字，报调度员。
- **再执行一次**：`changed: false`，`message_read_rules_written: 0`。
- **只读核对**：重新登记后，下一次 message Run 的 pack 中，evidence 能读到 Conversation（第 6 步验证）。

## 4. 载入镜像，全量重建（APP_HOST；**B**，sudo-A）

### 4.1 dry-run 生成 compose.images.env（管理机）

```
scripts/deploy/deploy.py dry-run --host-role app --stage-env "$STAGE_ENV" --release-dir "$RELEASE_DIR" \
  --allowed-signers "$ALLOWED_SIGNERS" --trusted-sha <b39b61b 完整 40 位> --compose-env "$COMPOSE_APP_ENV" --out "$R7_OUT"
```

- **出处**：run-34 §4.1；参数取自 `deploy.py` 的 `dry-run` 子命令。`--out` 必须是不存在的目录。
- **预期**：`ready: true`，`executed: false`。`release_verified`、`image_variables_mapped`、`compose_image_variables_complete`、`compose_images_from_manifest` 为 true。镜像未载入时 `images_loaded_by_digest` 为 false，属于预期。
- **核对**：`$R7_OUT/compose.images.env` 中 backend 和 agent-host 的 digest 是发布 7 清单的值；gateway 应与当前部署的相同（本发布没有改 gateway）。

### 4.2 载入镜像

```
docker load -i "$RELEASE_DIR"/images/<backend>.tar
docker load -i "$RELEASE_DIR"/images/<agent-host>.tar
scripts/deploy/deploy.py load-check --release-dir "$RELEASE_DIR"
```

- **出处**：run-34 §4.4；`load-check` 的唯一参数是 `--release-dir`。
- **预期**：退出码 0。退出码 3 表示有镜像没有载入。

### 4.3 替换配置（sudo-A）

```
B=$NEXLOOP_ROOT/ops/release7-backup; sudo install -d -o root -g root -m 0700 "$B"
sudo cp -p "$NEXLOOP_ROOT/config/compose.images.env" "$NEXLOOP_ROOT/config/compose.app.yaml" "$B/"
```

1. **compose.images.env**：用 `$R7_OUT/compose.images.env` 替换，属主 root，0640（run-34 §4.3）。
2. **compose.app.yaml**：先比较差异，再替换：
   - 比较：`sudo diff "$NEXLOOP_ROOT/config/compose.app.yaml" <(git -C "$RELEASE_CHECKOUT" show b39b61b:deploy/stage/compose.app.example.yaml)`；
   - 差异只应是已合入 main 的 compose 改动：
     - delivery-ca：`webchat_delivery_provider_ca` secret，以及 webchat-delivery 的 `--provider-ca-file`；
     - G-CLAIM-EGRESS：在 run-42 §8 上线时已部署；
     - 如果 run-delivery-ca 已手工执行，这部分没有差异；
   - 出现其他差异时停下；
   - 替换：用 b39b61b 的版本替换，属主 root，0640。
3. **webchat-delivery 的 CA 副本**：如果不存在，按 run-delivery-ca §1 安装：
   ```
   sudo install -o 10001 -g 10001 -m 0600 "$NEXLOOP_ROOT/secrets/action-worker/webchat-provider-ca.crt" "$NEXLOOP_ROOT/secrets/webchat-delivery/provider-ca.crt"
   ```
   如果已存在，用 `sudo cmp` 核对与 action-worker 的 CA 相同即可。
4. **runtime-worker 的 DSN**：如果 run-runtime-role 还没执行，先按该单 §1–§3 换好三个文件（换文件即可；第 4.4 步的全量重建会加载它们）。
5. **检查 compose**：`$C config --quiet && echo config-ok`。

### 4.4 全量启动

```
$C up -d
```

**为什么全量重建，不按服务挑选**：
- 两个镜像都换了，backend 镜像几乎被所有服务使用，逐个挑选和全量重建的结果差不多；
- 有两组服务通过 `network_mode: service:<X>` 共享网络命名空间：
  - `agent-link-relay` 依附 `agent-host`；
  - `webchat-delivery` 依附 `action-worker`；
  - 另外 18 个服务依附 `app-net`；
- 只重建被依附的一方（agent-host、action-worker），依附方会留在已经消失的旧命名空间里，表现为连不上、但进程不退出。全量 `up -d` 会按依赖顺序一起重建两方。

### 4.5 核对

```
$C ps --format '{{.Service}} {{.State}}' | sort
for s in $($C ps --services); do id=$($C ps -q $s); printf '%s restarts=%s mode=%s\n' $s "$(sudo docker inspect -f '{{.RestartCount}}' $id)" "$(sudo docker inspect -f '{{.HostConfig.NetworkMode}}' $id)"; done
for pair in agent-link-relay:agent-host webchat-delivery:action-worker app-link-relay:app-net; do
  dep=${pair%%:*}; tgt=${pair##*:}
  [ "$(sudo docker inspect -f '{{.HostConfig.NetworkMode}}' $($C ps -q $dep))" = "container:$($C ps -q --no-trunc $tgt)" ] && echo "$dep netns current" || echo "$dep netns STALE"
done
```

- **出处**：服务数和 `network_mode` 取自 b39b61b 的 `compose.app.example.yaml`：25 个服务，没有 profiles，20 个服务带 `network_mode`。
- **预期**：
  - 25 行，全部 running 或 healthy；
  - 每个服务 `restarts=0`。10 分钟后重跑一次，仍为 0；
  - 三行都是 `netns current`；
  - 每个带 `network_mode` 的服务指向的都应是当前目标容器的完整 id。上面只列了三组代表，其余 app-net 依附者可以用同一判断检查。
- **STALE 时**：`$C up -d --force-recreate --no-deps <依附方>`，然后重做核对。
- 再按 run-46 §2.2 核对每个服务的 image 等于新 `compose.images.env` 中的 `<仓库>@sha256:…`。

## 5. 部署门禁（APP_HOST；**B**，sudo-A）

完全按 run-metrics-gate §2.1–§2.2 执行，只把目录名换成新的：

```
GATE_DIR=$NEXLOOP_ROOT/ops/gate-release7
sudo python3 "$RELEASE_CHECKOUT/scripts/deploy/gate_dir.py" --nexloop-root "$NEXLOOP_ROOT" --release-checkout "$RELEASE_CHECKOUT" \
  --stage-env "$STAGE_ENV" --gate-dir "$GATE_DIR" \
  --planned-ports <GATEWAY_HTTPS_PORT>,<GATEWAY_REDIRECT_PORT>,<HOST_INTERNAL_PORT>,<GUARD_PORT>,<WEBCHAT_DELIVERY_PORT> \
  --before <部署后基线 baseline.json>
```

然后运行 run-metrics-gate §2.2 的 `docker run … nexloop-doctor --deploy-gate "$GATE_DIR/spec.json"`。

- **出处**：参数取自 `gate_dir.py` 的 argparse，`--before` 可选；`--phase baseline` 的用法见 run-metrics-gate §2.3。
- 本发布的镜像已经包含 `deploy_checks` 的新代码，但继续按 §2.2 挂载 `$RELEASE_CHECKOUT` 的源码，保证与 b39b61b 一致。
- **预期**：
  - `foundation_checks_passed: true`；
  - `checks` 全部为 true，包括新项 **`webchat_delivery_profile_matches_route`**；
  - `product_ready` 为 false，这是预期的。
- **新项为 false 时**：看 `details.webchat_delivery_profile` 的代码：
  - `trust_digest_differs`：webchat-delivery 的 `--provider-ca-file` 不是 action-worker 的 CA 副本，回到 4.3 第 2、3 项；
  - `input_owner_differs` 等 `input_*` 代码：文件属主或权限不对，按 run-delivery-ca §1 修正；
  - `origin_differs`：webchat-delivery 的 `--origin` 与路由配置中的 origin 不一致，报调度员。
- 其余检查为 false 时，按 run-metrics-gate §2.3 的已知前提处理，不修改输入文件。

## 6. 冒烟（只读确认）

### 6.1 两条消息

1. 用 WebChat 合成账号 `customer-1`，在**同一会话**中先后发两条消息，间隔约 1 分钟。
2. 记下开始时间 `T0`，以及第二条消息的 `MESSAGE_ID`（run-model-smoke §2.1 的记法）。
3. 等 2 分钟后执行 6.2。

### 6.2 只读查询（DATA_HOST 一次性容器，bootstrap 只读事务）

```
docker run $COMMON $WINDOW -e TENANT_ID="$TENANT_ID" -e MESSAGE_ID="$MESSAGE_ID" -e T0="$T0" -i "$BACKEND_IMAGE" - <<'PY'
import json, os, psycopg
t, m, t0 = os.environ['TENANT_ID'], os.environ['MESSAGE_ID'], os.environ['T0']
Q = {
 'pack': ("""select b.created_at, b.pack_text::jsonb -> 'insufficient',
            (select count(*) from jsonb_array_elements(b.pack_text::jsonb -> 'items') i where i ->> 'subsection' = 'conversation'),
            (select count(*) from jsonb_array_elements(b.pack_text::jsonb -> 'items') i where i ->> 'section' = 'consumer_state')
          from runtime.nexloop_context_artifact_bindings b where b.created_at >= %s order by b.created_at desc limit 5""", (t0,)),
 'outbound': ("select delivery_state, run_id is not null from runtime.nexloop_outbound_messages where tenant_id=%s and trigger_message_id=%s", (t, m)),
 'legacy_intents': ("""select i.intent_id, i.state, o.state, o.fence, o.available_at, o.lease_until,
            (select count(*) from runtime.nexloop_effect_events e where e.intent_id=i.intent_id and e.event_kind='unknown')
          from runtime.nexloop_effect_intents i join runtime.nexloop_effect_outbox o using(intent_id)
          where i.intent_id::text like '1f6ac3bd%%' or i.intent_id::text like 'd1dc3853%%'""", ()),
 'legacy_outbound': ("""select intent_id, delivery_state from runtime.nexloop_outbound_messages
          where intent_id::text like '1f6ac3bd%%' or intent_id::text like 'd1dc3853%%'""", ()),
}
with psycopg.connect(open('/run/bootstrap-dsn').read().strip()) as c:
    c.execute('set transaction read only')
    for k, (q, p) in Q.items():
        print(k, json.dumps(c.execute(q, p).fetchall(), default=str))
PY
```

- **出处**：
  - pack 查询来自 run-context-sources §4；
  - outbound 查询来自 run-model-smoke §2.2；
  - 两条存量意图的查询来自 run-unknown-reconcile §2（来源 Run 过期时间那一列需要时再加）；
  - 只输出状态、计数和时间，不输出消息正文或 pack 内容。
- **预期**：
  - `pack`：第二条消息对应的那一行，`insufficient` 为 `[]`，conversation 条目 ≥ 1（上一轮对话），consumer_state 条目 ≥ 1。如果 JSON 路径不同，以 `jsonb_pretty` 的实际结构为准，判定标准不变（run-context-sources §4）；
  - `outbound`：`delivered`，`run_id` 不为空；
  - `legacy_intents`：两条意图都**不再被频繁领取**：
    - 0199 生效后，`available_at` 要么在未来（退避中），要么为 `infinity`（超过 6 小时窗口，或来源 Run 已过期，经 `settle_expired_origin`）；
    - 隔 5 分钟再查一次，unknown 事件数基本不增长（退避期间每分钟最多一条，超出窗口后不再增加）；
  - `legacy_outbound`：仍为 `unknown`，或已由查询对账变为 `delivered`/`provider_accepted`。
    - **不要**手工改表或重投（run-unknown-reconcile §4 第 4 步）；
    - 停在 not_found 的，等回复保证上报 `reply_unknown`，由负责人处理。
- **失败的 Run**：新失败的 Run，任务结果中应带 `failure_code` 和 `failure_detail`（run-context-sources §5）。

## 7. 回滚

| 步骤 | 撤销方式 |
|---|---|
| 1 | 只读，没有需要撤销的东西 |
| 2 迁移 | **不可逆**：没有 down 迁移，也不运行 down（deploy-days 3.2）。出问题时前向修复：在 main 修好，以新的临时迁移号发布，走同一套门槛和备份。真要回到迁移前的库，只能走 NX-035 恢复流程（run-33），由负责人单独决定。0199–0201 都只重定义函数或新增函数，不删改业务数据，回退旧镜像时的兼容性见第 4 行 |
| 3 register | message_read_rule 是可信配置事实，没有“删除”这一说。如需回到旧字段，用上次 register 的源码树（`$RUN/src` 换回旧提交）以同样的参数重跑一次 register，`_apply_extra_facts` 会把字段写回旧值（**B**）。正常情况下不需要回退，因为新字段只扩大派生授权，旧镜像也能读 |
| 4 镜像与 compose | `sudo cp -p "$B/compose.images.env" "$B/compose.app.yaml" "$NEXLOOP_ROOT/config/"`，然后执行 `$C up -d`，再做 4.5 的核对。执行前，先用 `scripts/deploy/deploy.py rollback-check --release-root "$RELEASE_ROOT" --target <发布 6 的 id> --database-head 0201` 确认旧发布能接受当前的库版本（参数取自 `deploy.py` 的 `rollback-check` 子命令）；如果拒绝，就不回退镜像，改走前向修复。`webchat-delivery/provider-ca.crt` 可以保留，旧 compose 不引用它 |
| 5 门禁 | 只读，没有需要撤销的东西。`$GATE_DIR` 的收尾按 run-metrics-gate §2.4 |
| 6 冒烟 | 合成消息和回复留在库中，不回退 |

## 8. 证据（私有）

记录以下内容：
- 1.2 和 2.4 的 catalog 版本；
- 2.1 和 2.4 两次 `pg_diff` 的清单行；
- 2.3 的输出；
- 3.2 的计数；
- 4.5 的服务数和 restart 计数；
- `gate-result.json` 中的检查名；
- 6.2 中的状态和计数。

不记录 DSN、口令、地址，也不记录 pack 或消息的内容。
