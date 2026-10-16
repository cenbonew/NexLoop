# 命令单：NX-037 校准（stage，2026-10-17）

**范围**：交互计划 `nexloop.nx037-interaction-plan.v3` 的前 3 条，也就是校准交互（试运行配置 `calibration_interactions: 3`）：

| 序号 | kind | variant | 账号 |
|---|---|---|---|
| 1 | `ordinary_reply` | `single_question` | `consumer:<CUSTOMER_1>` |
| 2 | `real_delivery` | `direct_order` | `consumer:<CUSTOMER_1>` |
| 3 | `unknown_reconciliation` | `post_delayed` | `consumer:<CUSTOMER_2>` |

第 3 条之后，运行入口自动暂停（规则 `calibration`，退出码 1）。这次试运行的 trial id 和目录在 10/18–19 继续使用：校准后用 `--resume` 恢复，已记录的 3 条不会重复执行（OPERATIONS §9.4“恢复暂停”）。

**前提**：发布 8 已部署（catalog 末尾为 0202 `context_latest_consumer_turn`），按 run-release7-deploy 的流程完成（迁移、register、全量 up -d、门禁）。

**依据**（参数一律取自这些文件，出处写在每步后面）：
- `scripts/nx037/run_interactions.py`、`trial_guards.py`、`real_tenant_init.py`、`evidence_pack.py`、`unknown_drill_relay.py`；
- `apps/reference-endpoint/reference_endpoint.py`、`deploy/stage/reference-endpoint/README.md` §7；
- OPERATIONS §9.2–§9.6；NX-037-agent-players、NX-037-trial-guards、NX-037-plan-v3-draft §6；
- at-015-evidence-pack；run-model-smoke §2.2（只读查询容器）。

**客户为模拟、系统为真实**：消费者由负责人操作的 AI 代理扮演；部署、网关、DeepSeek 真实模型、企业端点和 WebChat 都是真实的。

类别标注：**B** 表示需负责人逐步确认；**sudo-A** 表示需要 APP_HOST 的 sudo；**sudo-D** 表示需要 DATA_HOST 的 sudo。只用角色名和变量。

## 0. 参数（负责人私有 env；命令单只用变量）

| 变量 | 含义 | 出处 |
|---|---|---|
| `TRIAL_ID` | 新的 uuid，10/17–19 共用 | run_interactions `--trial-id` |
| `TRIAL_DIR` | 试运行目录，APP_HOST 上的私有目录，属主 10001，0700；**事先不存在** | `--out` |
| `PLAN_FILE` | 管理机私有目录中 v3 计划的副本，占位符已替换成真实账号，复制到 APP_HOST 私有输入目录 | `--plan-file`；agent-players §6 第 3 项 |
| `TRIAL_CONFIG` | `deploy/configuration/nx037-trial.v3.json` 的私有副本，已填 `participants` 与 `operator_authorization.authorization_sha256` | `--trial-config`（**必须给**：默认值是 v2 配置，与 v3 计划不配） |
| `TENANT_ID`、`OWNER_PRINCIPAL_REF`、`VALUE_CATALOG_FILE`、`CHANNEL_REF` | 租户装配时的值 | real_tenant_init `parameters` |
| `PERIOD_START`、`PERIOD_END` | `2026-10-16T00:00:00+08:00`、`2026-10-23T00:00:00+08:00` | ROADMAP 2026-10-16 |
| `OWNER_AUTHORIZATION_FILE` | 负责人的授权书，0600 | trial_guards `consent` |
| `OWNER_SESSION_FILE` | 0600 普通文件；运行入口只检查权限，不用它登录 | OPERATIONS §9.4a |
| `METRICS_DIR` | metrics 只读角色的 DSN、客户端证书、私钥和 CA（run-metrics-gate §1 的 `management/metrics/` 四个文件） | `--metrics-dsn-file` |
| `ENDPOINT_HOST` | 参考端点所在的主机 | reference-endpoint README §7 |

**所有私有文件的属主**：运行入口以 10001 运行（见 §2.1），`trial_guards` 和 `RealDriverConfig` 要求授权书、会话文件、provider 文件属于当前 euid、权限 0600。所以上表中的私有文件都要放在 APP_HOST 上一个 10001 所有的 0700 输入目录 `$TRIAL_IN` 中，权限 0600。provider 文件不复制，直接用 action-worker 容器里的那几份（§2.1）。

## 1. 前提核对（只读）

### 1.1 发布、镜像与迁移

- 镜像：按 run-46 §2.2，`docker compose -p nexloop-app ps --format '{{.Service}} {{.Image}} {{.State}}'` 中，每个服务的 image 都等于发布 8 的 `compose.images.env`；25 个服务都是 running，restart 次数为 0（run-release7-deploy §4.5 的核对）。
- 迁移：按 run-release7-deploy §1.2 在 DATA_HOST 只读查询 `max(version)`，预期为 `0202`。

### 1.2 租户、预算和告警（metrics 角色）

```
python scripts/nx037/real_tenant_init.py verify --tenant "$TENANT_ID" --owner-principal "$OWNER_PRINCIPAL_REF" --value-catalog "$VALUE_CATALOG_FILE" \
  --channel-ref "$CHANNEL_REF" --period-start "$PERIOD_START" --period-end "$PERIOD_END" --metrics-dsn-file "$METRICS_DSN_FILE"
```

- **出处**：OPERATIONS §9.2 第 4 步；参数取自 `real_tenant_init.main`（`verify` 需要 `--metrics-dsn-file`）。
- 在 §2.1 的同一个一次性容器里执行（入口改为这个脚本），这样 metrics 文件的属主和路径都与运行入口一致。
- **预期**：
  - `tenant_active`、`model_budget_is_cumulative_cap`（100 CNY）、`model_budget_covers_trial`（10/16–10/23）、`alert_rules_in_effect` 为 true；
  - 开关打开之前，三项 `*_switch*_closed` 为 true；打开之后（§1.4）变为 false，这是预期的。运行入口只要求它们与计划一致。
- **预算余额**（DATA_HOST 只读容器，§3.2 的查询中的 `budget` 一项）：用量远低于 100 CNY。10/16 冒烟之后的实际用量记入证据。

### 1.3 模型额度（负责人本人）

- 在 DeepSeek 控制台查看账户余额，记下数额（只记数字，不截图凭据）。校准 3 次按 agent-players 和 plan-v3 草案 §3 估算，每个 Run 约 0.2–0.5 CNY；余额不足 10 CNY 时不开始。
- Host 的 `maximum_request_cost` 为 `0.45`（`agent-host.runtime-config.example.json`），单 Run 上限 2 CNY 由执行器判定。

### 1.4 三层真实派发开关（**B**，负责人本人）

计划 v3 的 `real_dispatch` 是 `{"deployment": true, "tenant": true, "data_mode": "real", "actions": ["eios:action:nexloop.enterprise.deliver:1"]}`。运行入口要求实际开关与它**完全一致**，多开、少开都拒绝（`switch_<层>_differs`）。

1. **租户层与 Action 层**：负责人用下面“租户层与 Action 层：配置驱动的开关工具”一节的工具，按私有配置一次打开该租户（data_mode `real`）和 `eios:action:nexloop.enterprise.deliver:1`（OPERATIONS §9.3）。它走的仍是工作台的受治理 owner Action `set_real_dispatch`，不再需要浏览器控制台。
2. **部署层 `REAL_DISPATCH_ENABLED`**：见下面的“打开”和“收工关闭”两段。计划 v3 中 `deployment: true` 是负责人已经定下的开关状态，但打开这个动作本身仍是 **B** 类，当天由负责人确认。
3. **核对**：§2.1 容器内 `REAL_DISPATCH_ENABLED` 的值，加上 `authz.nexloop_nx037_open_switches` 的结果，与计划一致。运行入口（包括 §2.2a 的 `--preflight`）会自动核对，不一致会以退出码 3 列出 `switch_*_differs`。

#### 租户层与 Action 层：配置驱动的开关工具（负责人本人，管理机；**B**）

工具：`scripts/deploy/real_dispatch_switch.py`，只用标准库，负责人在自己的管理机上运行。

**做什么**：
1. 以负责人的工作台账号登录（工作台自己的登录域），取得 CSRF；
2. 读取当前开关：`GET /api/v1/workbench/metrics` 中的 `real_dispatch`，只有状态和 revision，来自 0153 的 `runtime.nexloop_real_dispatch_switches_read`；
3. 与配置比对，**只对状态不同的项**调用受治理的 owner Action：`POST /api/v1/workbench/actions/set_real_dispatch`，即 `nexloop.real_dispatch.set:1`；
4. 再读一次，输出前后状态和每次变更的 `control_revision`，然后退出登录。

它不连数据库，不写开关表，也不改部署层；部署层只打印一条提示。

**私有输入**（管理机，0600，属于运行它的用户）：
- 配置文件：以 `deploy/examples/real-dispatch-switch.example.json` 为模板，内容为 `tenant {enabled, data_mode}`、`actions [{action_type, enabled}]` 和 `reason`。配置中没有列出的 Action 保持不变。
- 用户名文件：负责人的工作台用户名。
- 口令：从钥匙串经管道传给 `--password-stdin`，不出现在命令行参数、shell 历史或输出中；也可以用私有文件 `--password-file`。

```
O=https://<APP_HOST_NAME>:<GATEWAY_HTTPS_PORT>
# 1) 预演：只读当前状态并列出将要做的变更，不改任何东西
security find-generic-password -s <KEYCHAIN_SERVICE> -a <KEYCHAIN_ACCOUNT> -w | python3 scripts/deploy/real_dispatch_switch.py \
  --origin "$O" --ca-file <内部 CA> --username-file <用户名文件> --password-stdin --config <私有配置> --dry-run
# 2) 执行（B：负责人当日确认）
security find-generic-password -s <KEYCHAIN_SERVICE> -a <KEYCHAIN_ACCOUNT> -w | python3 scripts/deploy/real_dispatch_switch.py \
  --origin "$O" --ca-file <内部 CA> --username-file <用户名文件> --password-stdin --config <私有配置>
```

- `--ca-file` 用签发 Gateway 证书的内部 CA，与浏览器信任的是同一个。`--origin` 必须与工作台的 `BROWSER_ORIGIN` 完全一致，因为服务端核对 Origin 和 Host。
- 管理机要在 Gateway 的受信 LAN 中，也就是防火墙 `TRUSTED_LAN_CIDR` 范围内。
- **预期**：
  - `ok: true`；
  - `changes` 列出改了哪些项；
  - `results` 中每一项 `status: 200`，并带 `control_revision`，每次变更加 1；
  - `after` 与配置一致，`converged: true`；
  - `deployment_layer` 是一条提示，说明部署层不由这个工具管。
- **再次执行同一配置**：`changes: []`，什么都不发送。
- **请求键**：每次变更的 Idempotency-Key 由目标状态、该开关的当前 revision 和 reason 派生。中途失败后重跑，会重放同一个请求，不会重复执行；之后再次改同一个开关，会从新的 revision 开始，是一个新请求。
- **失败**（退出码 1）：
  - 登录被拒：`login refused (401)`；
  - 不是 owner：`results` 中为 `status: 403, code: forbidden`；
  - 读不到状态：`switch state unreadable (…)`；
  - 收敛失败：`converged: false`。
  - 工具在第一个被拒的请求处停止，再读一次并输出实际状态。不要改用浏览器控制台绕过，先查原因。
- **输入错误**（退出码 2）：文件不私有、配置格式错误、`--close-all` 没给 `--reason` 等。这时不会发出任何请求。
- **收工一键全关**：
  ```
  security find-generic-password -s <KEYCHAIN_SERVICE> -a <KEYCHAIN_ACCOUNT> -w | python3 scripts/deploy/real_dispatch_switch.py \
    --origin "$O" --ca-file <内部 CA> --username-file <用户名文件> --password-stdin --close-all --reason "<日期> 收工关闭"
  ```
  关闭租户层和**所有**已打开的 Action 层，`after` 中全部为 `enabled: false`。
- **测试**：`tests/test_real_dispatch_switch_tool.py`：
  - 配置校验、变更计划、请求键、私有文件、各种输入在发出请求之前就被拒；
  - 用生产的 `create_app` 端到端运行：预演不改状态；打开后 revision 连续；重跑不发送；operator 被拒 403；全关；同一配置再次打开是新请求，而不是重放。

#### 部署层：打开（APP_HOST；**B**，sudo-A；窗口开始前）

从发布 8 之后的 compose 起，这个开关是 `compose.app.env` 中的变量 `REAL_DISPATCH_ENABLED`：缺省为 false，只接受 `true` 或 `false`。依据是 `compose.app.example.yaml` 的 `x-service` 锚点和 agent-host 两处 `${REAL_DISPATCH_ENABLED:-false}`；app-net 的 `extends` 指向 `real-dispatch.<值>.yaml`，其他任何值都找不到文件，`compose config` 因此失败。

**前提**：部署目录 `$NEXLOOP_ROOT/config/` 中已经有这个版本的 `compose.app.yaml`，以及与它同目录的 `real-dispatch.true.yaml`、`real-dispatch.false.yaml`（从发布提交的 `deploy/stage/` 复制，root 0640）。核对：
```
grep -c 'REAL_DISPATCH_ENABLED:-false' $NEXLOOP_ROOT/config/compose.app.yaml; ls $NEXLOOP_ROOT/config/real-dispatch.*.yaml
```
预期：`3`，两个文件都在。否则先按 run-release7-deploy §4.3 更新 compose。

```
B=$NEXLOOP_ROOT/ops/dispatch-switch-backup; sudo install -d -o root -g root -m 0700 "$B"
sudo cp -p $NEXLOOP_ROOT/config/compose.app.env "$B/compose.app.env.$(date -u +%Y%m%dT%H%M%SZ)"
sudo sed -i 's/^REAL_DISPATCH_ENABLED=.*/REAL_DISPATCH_ENABLED=true/' $NEXLOOP_ROOT/config/compose.app.env
grep -q '^REAL_DISPATCH_ENABLED=true$' $NEXLOOP_ROOT/config/compose.app.env || echo 'REAL_DISPATCH_ENABLED=true' | sudo tee -a $NEXLOOP_ROOT/config/compose.app.env >/dev/null
$C config --quiet && echo config-ok
$C up -d
```

- `$C` 的含义见 run-release7-deploy §0：`docker compose -p nexloop-app -f …/compose.app.yaml --env-file …/compose.app.env --env-file …/compose.images.env`。
- **`up -d` 只重建引用这个变量的服务**：变量变了，只有环境里有它的服务配置会变，compose 只重建这些服务。按 compose 渲染，是 app-net 以外的 24 个：`x-service` 锚点下的全部服务，加上 agent-host。app-net 的环境里没有这个变量（它只有 `extends` 那一个空片段），所以**不会重建**，依附它的 18 个服务的网络命名空间仍然有效。
- **network_mode 依附**：webchat-delivery 依附 action-worker，agent-link-relay 依附 agent-host。两组的两方都在这 24 个里，会一起重建。重建后必须核对依附方指向的是**当前**的目标容器，用 run-release7-deploy §4.5 的 netns 核对。如果显示 STALE，执行 `$C up -d --force-recreate --no-deps webchat-delivery`（或 `agent-link-relay`）。不要只重建 action-worker：它重建后，webchat-delivery 也必须跟着重建。
- **核对两处容器内的值**：
  ```
  for s in action-worker agent-host; do printf '%s ' $s; sudo docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' $($C ps -q $s) | grep '^REAL_DISPATCH_ENABLED='; done
  ```
  预期两行都是 `REAL_DISPATCH_ENABLED=true`。action-worker 代表锚点下的服务，agent-host 是单独写的那一处。
- 再核对 25 个服务都是 running，restart 次数为 0（run-release7-deploy §4.5）。
- **门禁**：开关打开期间，`real_dispatch_closed` 为 false，`details.secrets.dispatch_open` 列出这 24 个服务，这是预期的。只在试运行窗口内接受这个结果。收工关闭后重跑门禁，它应回到 true。

#### 部署层：收工关闭（**B**，sudo-A；窗口结束或校准暂停后）

1. 负责人用开关工具 `--close-all` 关闭租户层和 Action 层（见上面的“配置驱动的开关工具”）。
2. 用上面同样的命令，把值写回 `false`（先备份），执行 `$C config --quiet`、`$C up -d`，再做 netns 核对。核对两处容器内的值，预期都是 `REAL_DISPATCH_ENABLED=false`。
3. 重跑部署门禁（run-release7-deploy §5，用新的 `GATE_DIR`），`real_dispatch_closed` 应为 true。

- 也可以由负责人决定保持打开到 10/19 交互结束；但窗口外（10:00–18:00 以外）没有人值守，所以建议每天收工都关闭。
- **撤销**（打开过程中出错）：把备份的 `compose.app.env` 复制回去，执行 `$C up -d`。

### 1.5 参考端点演练开关是关闭的

```
docker compose -f deploy/stage/reference-endpoint/compose.example.yaml --env-file <PRIVATE_COMPOSE_ENV> exec endpoint \
  python /opt/reference-endpoint/reference_endpoint.py drill --data-dir /var/lib/reference-endpoint --status
```

- **出处**：reference-endpoint README §7；`drill` 子命令的参数是 `--data-dir`，以及 `--delay-next-posts N [--delay-seconds S]`、`--get-unavailable-seconds S`、`--clear`、`--status` 四者之一。
- 在 `$ENDPOINT_HOST` 上执行。
- **预期**：没有待生效的延迟，也没有 GET 不可用的窗口。不是这样就先 `--clear`。

## 2. 运行

### 2.1 执行形态（APP_HOST；**B**，sudo-A）

运行入口必须同时满足以下条件：
- 与 action-worker 在同一个网络命名空间，因为 WebChat 投递的健康检查走 `https://127.0.0.1:<WEBCHAT_DELIVERY_PORT>`；
- 能读 action-worker 的 provider 文件（容器内路径 `/run/secrets/…`，属主 10001，用 `read_private_text` 读取）；
- 环境变量 `REAL_DISPATCH_ENABLED` 与部署一致；
- 运行代码是发布提交的源码。

所以用一次性容器借用 action-worker 的挂载和网络（compose 的 file secret 是绑定挂载，`--volumes-from` 会一并带上）：

```
AW=$(sudo docker compose -p nexloop-app ps -q action-worker)
RUNI="sudo docker run --rm -i --user 10001:10001 --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
  --network container:$AW --volumes-from $AW:ro -e REAL_DISPATCH_ENABLED=$(sudo docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' $AW | sed -n 's/^REAL_DISPATCH_ENABLED=//p') \
  -v $RELEASE_CHECKOUT:/opt/release:ro -v $TRIAL_IN:$TRIAL_IN:ro -v $METRICS_DIR:$METRICS_DIR:ro -v $TRIAL_DIR:$TRIAL_DIR \
  -v $TRIAL_IN/host-model-tariff:/run/secrets/host_model_tariff:ro \
  -w /opt/release --entrypoint python $BACKEND_IMAGE"
```

- `$RELEASE_CHECKOUT` 是发布 8 提交的检出：运行入口从 `scripts/nx037/` 和 `deploy/configuration/` 读文件。
- `$METRICS_DIR`：metrics 角色的 10001 副本，生成方法见下面的“metrics 文件的 10001 副本”。DSN 中的证书路径是绝对路径，所以副本要挂到 DSN 中写的同一路径上。
- **Host 配置**（`--host-config`）：
  - Agent Host 的 `runtime-config.json` 属主是 10020，10001 读不了；它引用的价目文件 `model_tariff_file` 是容器内路径 `/run/secrets/host_model_tariff`；
  - 运行入口只读这两个文件来核对 `runtime_profile`、`cost_currency` 和价目字段（`currency_preconditions`），不读模型凭据；
  - 做法：把这两个文件各复制一份，属主 10001、0600，内容不改（核对 sha256），配置副本放在 `$TRIAL_IN/host-config.json`；价目副本用 `-v $TRIAL_IN/host-model-tariff:/run/secrets/host_model_tariff:ro` 挂到原路径。
- **metrics 文件的 10001 副本**（sudo-A）：
  ```
  M=$NEXLOOP_ROOT/ops/metrics; METRICS_DIR=$M-10001
  sudo install -d -o 10001 -g 10001 -m 0700 "$METRICS_DIR"
  for f in metrics.dsn metrics-pg-client.crt metrics-pg-client.key pg-ca.crt; do sudo install -o 10001 -g 10001 -m 0600 "$M/$f" "$METRICS_DIR/$f"; sudo cmp "$M/$f" "$METRICS_DIR/$f"; done
  ```
  - 文件名取自 run-metrics-gate §1 的 `management/metrics/` 四个文件。以 APP_HOST 上 `$M` 中的实际文件名为准；`cmp` 不报差异，就说明内容没有改。
  - **DSN 中证书路径的处理**：DSN 里的 `sslcert`、`sslkey`、`sslrootcert` 指向 `$M` 下的绝对路径。在容器里把副本挂到原路径即可，**不改 DSN 内容**：§2.1 的 `RUNI` 中把 `-v $METRICS_DIR:$METRICS_DIR:ro` 换成 `-v $METRICS_DIR:$M:ro`，`--metrics-dsn-file` 用 `$M/metrics.dsn`。
  - **为什么不直接改原文件的属主**：
    - `$M` 中的文件属于 root，部署门禁（run-metrics-gate §2.2）以 root 读取它们；
    - libpq 要求私钥属于当前用户（或者属于 root、且权限不超过 0640）。改成 10001 后，以 root 运行的门禁仍然能读，但这会让一个服务 uid 永久持有只读角色的凭据。而 action-worker 和其他服务都是 10001，这等于把这份凭据交给了所有以 10001 运行的服务；
    - 副本只在试运行期间存在，用完删除，原文件保持不变。
  - **用后删除**（校准暂停后或当天收工时）：`sudo shred -u "$METRICS_DIR"/* && sudo rmdir "$METRICS_DIR"`。下一个试运行日重新生成。
- **这个执行形态还没有在 stage 演练过**：先跑 §2.2a 的 `--preflight`。
  - 2.2 的第一次运行如果在交互之前就被拒（退出码 3），按 `refused` 中的代码处理。被拒时不会发出任何提示，也不会产生交互；
  - 被拒时，`consent.json` 还没有写（守卫全部通过后才写），只有企业端点检查在 `$TRIAL_DIR/preflight/` 留下报告。可以用同一个 `TRIAL_DIR` 重跑，也可以换一个新的。

### 2.2a 预检：`--preflight`（不外发、不写入）

用 2.2 的完整命令加 `--preflight`，`--out` 可以指向一个还不存在的目录：

```
$RUNI scripts/nx037/run_interactions.py <与 2.2 相同的参数> --preflight
```

- **作用**（`run_interactions.py` 的 `--preflight`）：解析参数，读取全部私有文件，执行真实开跑时的每一项开跑前检查，包括租户 verify、三层开关、币种与 Host、授权与参与者、窗口、WebChat 投递 `GET /v1/health`、录入器积压，然后退出。
- 它不发出提示，不写 `consent.json`，不建试运行状态，也不创建目录。
- 企业端点一致性检查**不运行**，因为它会在端点上创建三条带标记的效应。预检只读企业 provider 文件，读不了时给出 `enterprise_provider_unreadable`；输出的 `not_run` 中会列出 `enterprise_endpoint_conformance`。
- **预期**：`{"ok": true, "preflight": true, "refused": [], "not_run": ["enterprise_endpoint_conformance"], …}`，退出码 0。
- **不是 0 时**：按 `refused` 中的代码修正执行形态或输入，再预检，直到通过。这一步可以在开关打开之前做；那时 `switch_deployment_differs` 等开关代码是预期会出现的，其余代码都应该为空。
- 依据：`tests/test_nx037_synthetic_five_pg.py` 在完整的本地栈上运行 `--preflight`：通过时什么都不写，也没有提示；投递不可达时报 `webchat_delivery_unavailable`，provider 文件不私有时报 `enterprise_provider_unreadable`。
- `tests/test_stage_compose.py` 核对了 §2.1 这一形态所依赖的 compose 事实：
  - action-worker 以 file secret 挂载了两个 provider 文件及其凭据和 CA 文件，`--volumes-from` 会带上这些绑定挂载；
  - action-worker 的文件属于 10001；
  - webchat-delivery 与 action-worker 共享网络命名空间。
- **局限**：本地没有在真实的 docker 容器中运行过这一形态，因为开发机没有可用的 Docker daemon。预检就是 stage 上的第一次实证。

### 2.2 启动（**B**：这一步开始会有真实的企业交付和 WebChat 外发）

```
$RUNI scripts/nx037/run_interactions.py --driver real --trial-id "$TRIAL_ID" --out "$TRIAL_DIR" --plan-file "$TRIAL_IN/plan.json" \
  --trial-config "$TRIAL_IN/trial.json" --tenant "$TENANT_ID" --owner-principal "$OWNER_PRINCIPAL_REF" --value-catalog "$TRIAL_IN/value-catalog.json" \
  --channel-ref "$CHANNEL_REF" --period-start "$PERIOD_START" --period-end "$PERIOD_END" --metrics-dsn-file "$METRICS_DIR/metrics.dsn" \
  --owner-session-file "$TRIAL_IN/owner-session" --host-config "$TRIAL_IN/host-config.json" --operator-authorization-file "$TRIAL_IN/authorization.txt" \
  --webchat-provider-file /run/secrets/action_worker_webchat_provider --enterprise-provider-file /run/secrets/action_worker_enterprise_provider \
  --endpoint-parameters-file "$TRIAL_IN/noop-parameters.json"
```

- **出处**：
  - 参数取自 `run_interactions.main` 的 argparse，real 模式必需的有 `tenant, owner_principal, value_catalog, channel_ref, period_start, period_end, metrics_dsn_file, owner_session_file, host_config`；
  - `--operator-authorization-file`、`--webchat-provider-file`、`--enterprise-provider-file` 由守卫要求，计划打开了 deliver；
  - 两个 provider 的容器路径取自 compose secrets：`action_worker_webchat_provider`、`action_worker_enterprise_provider`；
  - `--interaction-timeout` 用默认值 900，`--poll-seconds` 用默认值 2。
- **`--endpoint-parameters-file`**：端点一致性检查会在端点上创建三条带标记的效应（OPERATIONS §9.4 第 4 项）。参数文件用企业确认为空操作的内容，例如参考端点的 `summary` 写“端点一致性检查”。
- **窗口**：10:00–18:00（Asia/Shanghai），窗口外以 `outside_trial_window` 拒绝。
- **开跑前自动核对**（任何交互之前，不满足就以退出码 3 列出代码）：
  - 租户 verify 中开关以外的各项；
  - 三层开关与计划一致；
  - 币种与 Host：`host_not_real_model`、`host_currency_differs`、`model_tariff_*`、`reply_fallback_currency_differs`；
  - 守卫：`authorization_missing`、`consent_missing`、`outside_trial_window`、`enterprise_endpoint_nonconformant`、`webchat_delivery_unavailable`、`outbound_recorder_stalled`。
- 通过后，终端（stderr）和 `$TRIAL_DIR/prompts.jsonl` 会给出第 1 条交互的提示。

### 2.3 分工：谁做什么（agent-players §1–§4）

| 序号 | 客户代理（浏览器，一个会话只登录一个账号） | 负责人 | B 类 |
|---|---|---|---|
| 1 `ordinary_reply/single_question` | 以 `<CUSTOMER_1>` 登录 WebChat，问一个与服务相关的普通问题；回复出现后，创建提示中的 `done_file` | 盯终端与告警页 | WebChat 外发（回复）：开跑本身即是确认 |
| 2 `real_delivery/direct_order` | 仍用 `<CUSTOMER_1>`，明确提出购买或预约目录中的服务；回复确认已受理后，创建 `done_file` | 在参考端点员工页面确认出现一张新工单（只看，不标记） | **真实企业交付**：负责人在客户代理发出这条消息之前确认 |
| 3 `unknown_reconciliation/post_delayed` | 以 `<CUSTOMER_2>` 登录（另开一个会话），明确提出交付请求；回复出现后，创建 `done_file` | **在客户代理发消息之前**，在 `$ENDPOINT_HOST` 执行 `drill --data-dir /var/lib/reference-endpoint --delay-next-posts 1 --delay-seconds 5`（README §7 的完整命令），再用 `--status` 确认已经生效 | **演练加真实交付**：负责人执行演练命令，代理不执行 |

- 代理的循环、完成标记和禁止事项，按 agent-players §2、§7。特别注意：**不要提前创建完成标记**，否则标记之后发出的消息会算到下一次交互里。
- 第 3 条使用参考端点自带的演练开关，不需要 `unknown_drill_relay.py` 中继。中继只在第三方端点无法这样做时使用（README §7）；它的 `arm` 子命令只接受 `--control` 和 `--count`。
- 第 3 条之后，运行入口以 `paused_needs_resume`（规则 `calibration`）结束，终端退出码为 1。代理停下，不再发消息。

## 3. 每条交互之后的只读核对

### 3.1 运行入口自己的判定（`$TRIAL_DIR/interactions.jsonl`，每条一行）

查看对应行（只含 ID、计数、毫秒、金额、代码），确认以下几点：
- 规则判定中没有命中停止或暂停（第 3 条之后的 `calibration` 除外）；
- `run_ids` 和 `messages` 与这次交互的来信数一致（单问为 1）；
- `cost.total`、`cost.period_increase`、`cost.counted` 都有值，`cost.max_run` 小于 2 CNY；
- `persona` 为 `simulated_agent`。

### 3.2 数据库只读查询（DATA_HOST 一次性容器，bootstrap 只读事务）

容器形态同 run-model-smoke §2.2（`$COMMON` 加 socket 和 `boot.dsn`，`--user` 按 run-release7-deploy §0 取 `$RUN` 的属主）。`T0` 为这次交互的提示时间（`prompts.jsonl` 中的时间，或者负责人记下的发送前时间）。`MESSAGE_ID` 是这次交互最后一条来信的 id。

```
docker run $COMMON $WINDOW -e TENANT_ID="$TENANT_ID" -e MESSAGE_ID="$MESSAGE_ID" -e T0="$T0" -i "$BACKEND_IMAGE" - <<'PY'
import json, os, psycopg
t, m, t0 = os.environ['TENANT_ID'], os.environ['MESSAGE_ID'], os.environ['T0']
Q = {
 'pack': ("""select b.created_at, b.pack_text::jsonb -> 'insufficient',
            (b.pack_text::jsonb -> 'evidence' -> 0 ->> 'subsection'),
            (select count(*) from jsonb_array_elements(b.pack_text::jsonb -> 'evidence') i where i ->> 'subsection' = 'conversation'),
            (select count(*) from jsonb_array_elements(b.pack_text::jsonb -> 'evidence') i where i -> 'tags' ? 'latest_consumer_turn')
          from runtime.nexloop_context_artifact_bindings b where b.created_at >= %s order by b.created_at""", (t0,)),
 'jobs': ("select queue, status, attempts, result->>'code', result->>'runtime_outcome', result->>'failure_code', result->>'failure_detail' from runtime.jobs where tenant_id=%s and created_at>=%s order by created_at", (t, t0)),
 'outbound': ("select delivery_state, run_id is not null from runtime.nexloop_outbound_messages where tenant_id=%s and trigger_message_id=%s", (t, m)),
 'effects': ("""select i.intent_id, i.action_name, i.state, o.state, o.available_at,
            (select json_object_agg(k, n) from (select event_kind k, count(*) n from runtime.nexloop_effect_events e where e.intent_id=i.intent_id group by 1) s)
          from runtime.nexloop_effect_intents i join runtime.nexloop_effect_outbox o using(intent_id)
          where i.tenant_id=%s and exists (select 1 from runtime.nexloop_effect_events e where e.intent_id=i.intent_id and e.created_at>=%s)""", (t, t0)),
 'budget': ("select budget_kind, sum(amount), count(*) from control.nexloop_budget_consumption where tenant_id=%s and world='real' group by 1", (t,)),
 'claims': ("select count(*) from ontology.nexloop_claims where tenant_id=%s and world='real'", (t,)),
}
with psycopg.connect(open('/run/bootstrap-dsn').read().strip()) as c:
    c.execute('set transaction read only')
    for k, (q, p) in Q.items():
        print(k, json.dumps(c.execute(q, p).fetchall(), default=str))
PY
```

- **出处**：
  - pack 字段取自 ContextPackV6 契约（`packages/contracts`）：`evidence` 项的 `subsection` 和 `tags`；
  - `latest_consumer_turn` 标签来自 0202；
  - jobs 与 outbound 查询来自 run-model-smoke §2.2；
  - `failure_code` 和 `failure_detail` 来自 run-context-sources §5；
  - effects 查询来自 run-unknown-reconcile §2；
  - 只输出状态、计数、时间和代码，不输出消息正文、pack 内容或 Claim 的值。
- 如果 pack 的 JSON 路径与上面不同，以 `jsonb_pretty` 的实际结构为准，判定标准不变（run-context-sources §4）。

**判定**：

| 项 | 第 1 条 | 第 2 条 | 第 3 条 |
|---|---|---|---|
| `pack`：insufficient | `[]` | `[]` | `[]` |
| `pack`：evidence 首项为 `conversation`、有 `latest_consumer_turn` 的项恰好 1 个 | 第 1 条是该会话的首条消息时，可能没有上一轮：首项可以不是 conversation，标签数可以为 0，记下即可 | 是，1 | 是，1（`<CUSTOMER_2>` 的首条来信同第 1 条的说明） |
| `jobs`：operations 的 Run | `succeeded`；`result` 中没有 `intent_payload_conflict`，没有 `failure_code` | 同左 | 同左 |
| `outbound` | `delivered` | `delivered` | `delivered` |
| `effects` | 一条 `nexloop.service.request`（回复），state 为 fulfilled | 回复一条，加上 `nexloop.enterprise.deliver` 一条：state 为 fulfilled，事件为 dispatching → fulfilled；参考端点员工页面有对应工单 | deliver 一条：事件中先有 `unknown`（端点延迟 5 秒，超过 2 秒截止），然后**经查询对账**变为 fulfilled（`observed` 或 `fulfilled` 事件），**dispatching 只有一次，没有重新提交**。按 0199，unknown 之后的下一次查询要等退避（第 n 次后至少 min(60, 2^(n-1)) 秒），所以通常在 1–2 分钟内了结。参考端点上只有**一张**工单 |
| `budget` | 只增加 model 一项，金额与 `cost.counted` 量级一致 | 同左 | 同左 |
| `claims` | 计数不减；claim-extraction 队列的 job 为 succeeded（`jobs` 一项中），没有失败 | 同左 | 同左 |

- 第 3 条中，运行入口要等对账了结才判定这次交互完成（未了结的结果不明会计入 `intents_open`）。所以如果超过 900 秒仍未了结，就以 `interaction_timeout` 暂停，不会猜测结果。
- 交付回执（`receipt_id`）和效应证据由运行入口的 `PgCollector` 采集，写进 `interactions.jsonl` 的这一行；上面的查询只做交叉核对。

## 4. 失败时的处置与停止条件

| 情况 | 运行入口的表现 | 处置 |
|---|---|---|
| 开跑前被拒 | 退出码 3，`refused` 中给出代码，不会发出任何提示 | 按代码修正后再跑；不要为了通过而修改输入（例如三层开关多开、少开） |
| 停止规则命中（`unauthorized_send`、`wrong_recipient`、`contact_after_refusal`、`duplicate_external_effect`、`cross_tenant_read`、`per_run_cost_exceeded`、`critical_alert`、`owner_stop`） | 退出码 2，`state.json` 的 status 为 `stopped`；**不能恢复** | 按 OPERATIONS §9.5 和 SECURITY 的处置流程：关闭三层开关（§1.4 收工时关闭的步骤）→ 保留证据 → 核对已经发生的动作（结果不明只查询对账，**不重发**）→ 修复 → 用新的 trial id 重新开始 |
| 暂停（错误率超过 10%（窗口 10 次）、费用达到累计上限的 80%、未了结的结果不明、`interaction_timeout`） | 退出码 1 | 负责人核对原因后，用 `--resume <确认引用>` 恢复（原命令加这一项）。窗口外不能恢复 |
| 校准暂停（第 3 条之后，预期） | 退出码 1，规则 `calibration` | 见 §5 |
| 预算 | model 预算耗尽时，Run 签发被拒（`budget_exhausted`），交互会以错误或超时结束 | 不在校准中追加预算；记下数字，报调度员 |
| 第 3 条迟迟没有对账 | 900 秒后暂停（`interaction_timeout`，或未了结的结果不明） | **不要**手工改表或重投。先在 `$ENDPOINT_HOST` 用 `drill --status` 确认演练已用完，再用 §3.2 的 effects 查询看 `available_at` 和事件；仍为 unknown 时，按 run-unknown-reconcile §3–§4 处理 |
| 告警 | critical 告警使运行入口停止，但**不会通知任何人** | 整个过程中有人盯着工作台告警页（OPERATIONS §9.4a“告警值守”） |

**任何时候都不要做的事**：手工写授权表或业务表、删除事件、重发 unknown 的效应、在窗口外恢复、让代理执行服务器命令。

## 5. 校准暂停之后（负责人）

1. 查看 3 行的 `cost.total`、`cost.period_increase` 和 `cost.counted`。按 OPERATIONS §9.4a 的“单 Run 成本人工查看”：如果期间增量明显高于触发 Run 的成本，先查明来源（兜底、复评、角色 Run 或抽取）。
2. 以 3 次的实测单次费用估算 36 次的总额，确认在累计 100 CNY 以内，并且低于 80% 暂停线。
3. 决定是否恢复，并给出确认引用（例如复核记录编号）。恢复在 10/18 的窗口内用 `--resume` 进行，命令同 2.2 加 `--resume <确认引用>`。
4. 按 §1.4 决定当天是否关闭开关。

## 6. 证据

**位置**：`local-only/evidence/v0.1-evidence/trial/`（at-015-evidence-pack §1 的目录结构；`local-only/` 不进 git）。

| 证据 | 来源 | at-015 条目 |
|---|---|---|
| `interactions.jsonl`、`state.json`、`prompts.jsonl`、`resumes.jsonl`、`consent.json`、`preflight/endpoint-conformance.json` | `$TRIAL_DIR`（试运行结束后整体复制，0700） | `trial/`，AT-071 |
| §3.2 每条交互的查询输出 | 本单 | `trial/` 旁边的 `calibration-checks-20261017.json`，AT-071 的 `note` |
| §1.2 verify 输出，§1.3 模型余额（只记数字） | 本单 | `environment/` |
| 校准结论：单次费用、是否恢复、确认引用 | 负责人 | AT-071 的 `note` |

- 汇总在全部交互结束后统一做：`scripts/nx037/evidence_pack.py --out <新目录> --item … --trial <TRIAL_DIR>`（OPERATIONS §9.6；`--out` 必须是不存在的目录）。校准当天不做。
- 证据只放 ID、哈希、数字、时间和代码；不放口令、DSN、主机地址、消息正文或真实租户名（真实租户名写作 `<TENANT_DISPLAY_NAME>`）。

## 7. 已处理的缺口（调度员 2026-10-16 裁定，分支 l4/calibration-gaps）

1. **部署层开关**：改为 compose env 变量，只能是 true 或 false（§1.4 的“打开”和“收工关闭”两段）。
2. **运行入口的执行形态**：新增 `--preflight`，§2.2a 先做预检；compose 测试核对了这一形态依赖的挂载、uid 和网络命名空间。真实容器中的首次实证就是 stage 上的预检。
3. **metrics 副本**：以 10001 安装、内容不变（`cmp` 核对）、挂回原路径、用后删除（§2.1）。
