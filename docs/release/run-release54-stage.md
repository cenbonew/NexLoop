# 执行单：release 54 的 stage 部署（第 56、57 批，世界模型中文注释）

> **自 R55 起**：本单以后用于 release 55 及以后的部署。门禁预期是 **21/22**（多了 `glossary_tariff_copy_matches`，I9）；按 release 54 部署时是 20/21，回退到 release 53 以后的门禁也是 20/21，因为 release 53 的镜像里没有这一项。

release 54 = **28e7689a**（第 57 批“世界模型词表”的合并提交，第一父 c77b5129 = 第 56 批，再下是 main 01d875dd = 第 53–55 批）。stage 当前是 release 53（**53af5efc**，迁移头 0373，告警规则 v21，register 已到 authority revision 3539）。本单从 53af5efc 直接到 28e7689a，每一步写明预期结果和怎么撤回。

- 同名的旧文件（第 50 批的 release 54 单，f56cd5af）已并入 `run-release53-54-stage.md`，内容保留在 git 历史里（78c38b28）；本文件替换它。
- **执行者**：调度员。**判定者**：L6。证据里两者分开写，并分开记录测试、演练和真实结果。负责人账号不参与。
- **依据**：`docs/design/world-model-glossary.md`、`docs/release/run-world-glossary-stage.md`。顺序以本单为准：那份单把 Action 发布放在迁移前面，但 `nexloop.world_glossary.set` 的受治理操作 `set_world_glossary` 由 0374 建立，所以先迁移、再发布。
- **按 28e7689a 核对过**（只读核对源码和文档，没有连接主机）。53af5efc → 28e7689a 的部署件变化如下表，表外没有变化：迁移只有 0374；compose 只多一个不常驻的服务；连接预算不变。

| 项 | 28e7689a 上的值（53af5efc 上的值） | 来源 |
|---|---|---|
| 新迁移 | 0374 `world_model_glossary`（`sha256` c6f9363f…41bd）；迁移头 0373 → **0374** | `packages/eios-core/src/eios/migrations/catalog.json` |
| 业务 Action | `business-actions` v53（v52）；编译计数 object_types 20、actions **129**、functions 2（多一个 `nexloop.world_glossary.set:1`，human_owner、low、无审批） | `stage_secrets.py publish --out <空目录> --tenant t --dry-run` 在 28e7689a 上实测：`{"actions": 129, "functions": 2, "object_types": 20}` |
| 能力快照 | `capabilities` v6（v5），新增 `world_glossary.set` 1.0.0 | `deploy/configuration/capabilities.v1.json` |
| 服务 Action | `nexloop.world_glossary.translate:1` **不是发布的 Action 定义**：它只是签名服务端口 `authz.nexloop_world_glossary_port` 的门禁资源（同 claim feed 0069 的 `nexloop.claim.extract`），只作为授权写进 service-grants，publish 里**不会**出现，也不计入 129 | 设计 §8 第 1 条；0374 第 273–283 行 |
| 服务授权 | `service-grants` v23（v22），16 个主体（15）；新增 `world_glossary_translator`（`principal_id` `nexloop-service-world-glossary-principal`，数据库角色 `nexloop_domain_worker`），唯一一条授权 `eios:action:nexloop.world_glossary.translate:1` execute | `deploy/authorization/service-grants.v1.json` |
| stage 主体 | `stage-principals` v8（v7），10 个主体不变；凭据映射新增 `world-glossary-translator/service-credential` → `world_glossary_translator` | `deploy/authorization/stage-principals.v1.json` |
| register 计数 | principals **26**、credential_files **35**、编译 (20, 129, 2)；清单授权（principal + resource 去重）**79** 行（53af5efc 上 78），其中队列授权 5 行、object_type 4 行、artifact 3 行 | 下文 S6 的 `jq` 在两个提交上实测 |
| 工作台角色 | `workbench-roles` v49（v48）；owner **95** 个 Action（94），新增 `nexloop.world_glossary.set:1`；operator 12、reviewer 1 不变 | `deploy/authorization/workbench-roles.v1.json` |
| API 路由 | `api-routes` v5（v4），新增 `set_world_glossary` → `world_glossary.set`；由 CI 核对，随镜像生效，stage 上没有要应用的步骤 | `deploy/authorization/api-routes.v1.json` |
| 词表 | `world-model-glossary` v1：**304** 条，没有 `null` 或空串；`manifest_sha256` = `0e25be1f4e42e1567e302a92eb01f4c1ae5251961a4a030557d1505b49f64ffe` | `world_glossary_apply.py --check` 实测 |
| 告警规则 | `alert-rules` v22（v21）：81 条（78），新增 3 条，其余不变：`timer_stale_world_glossary`（`host_health.timer_success_age_seconds`，selector `app_host:world-glossary`，> 1800，warning）、`world_glossary_failed`（`world_glossary.failed_24h` > 0，warning）、`world_glossary_no_success`（`world_glossary.pending_without_success_24h` > 0，warning）；文件 sha256 `03a0384f9ecb0561823e15e07b3b5b9b965d49f915adb394557a2b16926b5da0` | `deploy/configuration/alert-rules.v22.json` 与 v21 逐条比较 |
| compose | 新服务 `world-glossary-translator`：`profiles: [world-glossary]`、`restart: 'no'`、`--once`、预算标签 `none`，**不常驻**，`up -d` 不会启动它；网络只接 `claim_egress`；只读、`cap_drop ALL`；挂它自己的 4 个文件，加 `signing_key`、`pg_ca`、共用的 `claim_extraction_worker_model`（`secrets/claim-extraction-worker/model.env`）和它**自己的单价副本** `world_glossary_translator_tariff`（`secrets/world-glossary-translator/model-tariff.cny.json`）。28e7689a 上原本挂的是 Agent Host 的 `host_model_tariff`，读不到（见 P6），本分支已改 compose 和 `tests/test_stage_compose.py`，属于部署配置变更，合并走 ack-policy | `deploy/stage/compose.app.example.yaml` |
| 连接预算 | 不变：应用 60 / 60；机翻运行时占 1 个连接，算在运维保留的 7 个里（同 sim-setup） | `deploy/stage/connection-budget.v1.json`（未改） |
| systemd | 新示例 `nexloop-world-glossary.service.example`（oneshot，`flock -n -E 75`，锁被占时写 `skipped: locked` 并以 0 退出，`TimeoutStartSec=9min`）与 `.timer.example`（`OnCalendar=*-*-* *:03/10:00`，每 10 分钟，落在 03、13……分） | `deploy/stage/systemd/` |
| host-health | APP_HOST 安装配置的 `timers` 增加 `{"name": "world-glossary", "timer": "nexloop-world-glossary.timer", "service": "nexloop-world-glossary.service"}`；DATA_HOST 不变 | `deploy/examples/host-health-install.app-host.example.json` |
| 部署门禁 | `secret_uids` 增加 `"world-glossary-translator": 10001` | `deploy/stage/deploy-gate.app.example.json` |
| 防火墙模板 | 只改注释（`claim_egress` 的说明加上机翻服务），规则不变，不需要重新下发 | `deploy/stage/firewall/app-host.nft.template` |
| 前端 | 世界模型各页的中文注释与“机翻”标记、owner 的改正入口；关系图选中类型后“适应画布”；第 53–56 批的登录卡片、账号页、SSO 等页面 | `apps/web` |

## 0. 记号

- `$NEXLOOP_ROOT`：APP_HOST 上的部署根目录；`$RUN`：DATA_HOST 上的运行目录；`<树>`：管理机上的秘密树；`<检出>`：管理机上 28e7689a 的检出。
- `ro "<SQL>"`：DATA_HOST 上的只读会话，同 `run-nx057-remediation-stage.md` §0.3。
- `$COMMON`、`$WINDOW`：同 `run-nx057-remediation-stage.md` §0.2。`$COMMON` 写死了 `--user 999:999`，而 stage 上 `$RUN/boot.dsn` 的属主是 1000，所以本单的一次性容器一律用 `run-nx055-stage.md` §1.3 的写法：`RUN_AS=$(stat -c %u:%g "$RUN/boot.dsn")`、`CHECK_COMMON="${COMMON/--user 999:999/--user $RUN_AS}"`，并用其中的 `case` 一行确认替换生效。
- `stage_release.py`：例行发布的分步执行器，用到 `backup`、`precheck`、`migrate`、`publish`、`roles`、`rebuild`、`gate`、`promote`、`post`。register、秘密树、词表、告警规则、systemd 和 host-health 不在它里面，单独执行。
- 每一步先 `--dry-run` 看命令再正式执行（这只打印命令；Action 的“预演”是 S4 里 `stage_secrets.py publish --dry-run`）。任何一步与预期不符就停下，交调度员，不往下做。

## 1. 前提（不满足就不开始）

| # | 前提 | 怎么核对 |
|---|---|---|
| P1 | 28e7689a 的 CI（211/212）通过并已合入 main，release 已构建、签名、封存 | `stage_release.py --step build`、`--step sign` 的 JSON 都是 ok；签名前它会核对提交在 main 上 |
| P2 | stage 当前是 release 53，迁移头 0373 | `ro "select max(version) from control.schema_migrations"` 为 `0373` |
| P3 | stage 已应用的版本：service-grants v22、stage-principals v7、workbench-roles v48、告警规则 v21、register 在 authority revision 3539 | 上次部署记录；S6 第 2 步的差异核对再核一次授权；任一项不符就停下，按实际版本重写受影响的步骤 |
| P4 | 最近一次 pgBackRest 备份成功 | OPERATIONS §8a.2 |
| P5 | `stage_secrets.py check --out <树>` 为 ok，`<树>` 是 stage 正在用的那份 | 执行一次 |
| P6 | APP_HOST 上机翻要读的两个现有文件存在：`model.env` uid 10001 能读；Agent Host 的单价文件存在（S6 第 1 步从它复制） | 调度员 10-29 只读核实：`secrets/claim-extraction-worker/` 属 nexloop-svc（10001）、0700，文件 0600，机翻（10001）可以直接挂 `model.env`；`secrets/agent-host/` 属 nexloop-agent（10020）、0700，`model-tariff.cny.json` 属 nexloop-agent、0600，10001 读不到。所以单价**不**直接挂 Host 的文件，而是在 S6 复制一份到机翻自己的目录。**不改 `secrets/agent-host` 的属主或权限**。执行时再看一次：`sudo stat -c '%n %U:%G %a' "$NEXLOOP_ROOT/secrets/claim-extraction-worker/model.env" "$NEXLOOP_ROOT/secrets/agent-host/model-tariff.cny.json"` |
| P7 | 部署门禁参照 `$NEXLOOP_ROOT/ops/baseline-r36/baseline.json` 存在 | `test -f` |
| P8 | 真实派发三层开关的现状记在上次部署的证据里（门禁预期 21/22，自 R55 起；唯一不通过的是 `real_dispatch_closed`） | 上次部署的门禁记录；以执行时的读数为准 |

## 2. 步骤

### S1 迁移前差异备份

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step backup
```

- 预期：`nexloop-pg-diff.service` 退出 0，日志里有本次差异备份完成的记录。这是 S3 唯一的恢复点，记下它的标签。
- **撤回**：不需要。

### S2 只读预检

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step precheck
```

预检清单（私有 JSON）至少包含：

| name | sql | expect |
|---|---|---|
| migration_head | `select max(version) from control.schema_migrations` | `0373` |
| glossary_absent | `select to_regclass('control.nexloop_world_glossary') is null` | `t` |

- **撤回**：不需要（只读）。

### S3 迁移 0374

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step migrate
```

`migrate` 一步依次做完下面三项，任一不符就停：

1. 迁移门槛回答 `allowed: true`；
2. 执行迁移，输出 ok；`ro "select max(version) from control.schema_migrations"` 为 `0374`；
3. definer 检查 `0 problems`。0374 新增的 `security definer` 函数（发布、人工写入、机翻端口、指标）都在检查范围内。

然后**再做一次差异备份**（重跑 S1），这是迁移后的恢复点。

- 0374 只新增对象：词表、修订历史（只追加，触发器禁止 update/delete）、机翻任务和用量表，发布函数、`set_world_glossary` 受治理操作、机翻端口、指标函数，以及工作台世界模型读取的注释字段。已有的表和函数行为不变；没有发布词表之前，各页只显示英文标识。
- 机翻的每日上限 50 次写在 SQL 里（`c_daily`）。
- **遇到 `DeadlockDetected`**：先确认迁移头仍是 0373（整体回滚），重试一次；再遇到，就停掉 APP_HOST 的 worker 服务再迁移（OPERATIONS 的迁移死锁说明）。
- 本次 timer 还没有装，所以不需要先停机翻 timer。以后每次迁移都要先停（见 S10 第 4 条）。
- **撤回**：迁移只能前进，见 §4 的 R1。不要手工删除迁移建的对象。

### S4 Action 发布：先预演，再发布

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step publish
```

`publish` 一步先带连接文件做 `stage_secrets.py publish --dry-run`（与库里已发布的状态比较，什么也不写），再正式发布。

- **预演预期**：有变化，变化**只有一个**新 Action：`nexloop.world_glossary.set:1`。编译计数 object_types 20、actions 129、functions 2。
  - `nexloop.world_glossary.translate:1` 不会出现，这是正常的（见表中“服务 Action”一行）。
  - 出现别的变化就停下。
- **发布预期**：ok；发布前后各记一次计数。
- **撤回**：目录发布只追加。要让新 Action 不可用，撤回 S5（没有人持有它的授权）。

### S5 工作台角色 v49

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step roles
```

- 预期：ok；JSON 输出里 owner 的授权数为 **95**（v48 为 94），多出的一项是 `eios:action:nexloop.world_glossary.set:1`；operator 12、reviewer 1。
- **撤回**：用 53af5efc 的 `workbench-roles.v1.json`（v48）按 OPERATIONS §8a.11 第 3 步重新应用，会撤销成员的 `world_glossary.set` 授权。

### S6 机翻服务主体：生成凭据、安装、register

**1. 秘密树（管理机）**。先 `cp -a <树> <树>.before-r54`（0700，不进仓库），作为本步的撤回点。然后：

```bash
python3 <检出>/scripts/deploy/stage_secrets.py add-service --out <树> --ca-dir <中间 CA 目录> --params <私有 params> --service world-glossary-translator
```

- 预期输出：`{"added": ["world-glossary-translator/dsn", "world-glossary-translator/pg-client.crt", "world-glossary-translator/pg-client.key", "world-glossary-translator/service-credential"], "credentials_added": ["app:world-glossary-translator/service-credential"], "service": "world-glossary-translator"}`。
  - DSN 用 `nexloop_domain_worker` 已有的口令，从树里同一角色的另一个 DSN 读出，所以不改任何角色口令。
  - 客户端证书的 CN 是 `nexloop_domain_worker`，由同一个中间 CA 签发。
  - 服务凭据是新令牌，同时写进 `service-secrets.json`。
  - 不复制任何别的服务的 DSN 或证书。model.env 和单价文件是 APP_HOST 上的现有文件，不进秘密树。
- `python3 <检出>/scripts/deploy/stage_secrets.py check --out <树>`：预期 ok。
- **装到 APP_HOST**：`$NEXLOOP_ROOT/secrets/world-glossary-translator/` 目录 0700、属 10001；四个文件用 `install -o 10001 -g 10001 -m 0600` 逐个装入，必须在 S9 之前（源文件缺失时 Docker 会自建同名空目录，OPERATIONS §8a.5）。
- **单价副本**（APP_HOST，root；P6）：把 Agent Host 的单价文件原样复制到机翻自己的目录，属 10001、0600，再核对两份的 sha256 一致：

  ```bash
  sudo install -o 10001 -g 10001 -m 0600 "$NEXLOOP_ROOT/secrets/agent-host/model-tariff.cny.json" "$NEXLOOP_ROOT/secrets/world-glossary-translator/model-tariff.cny.json"
  sudo sha256sum "$NEXLOOP_ROOT/secrets/agent-host/model-tariff.cny.json" "$NEXLOOP_ROOT/secrets/world-glossary-translator/model-tariff.cny.json"
  sudo stat -c '%n %u:%g %a' "$NEXLOOP_ROOT/secrets/world-glossary-translator" "$NEXLOOP_ROOT/secrets/world-glossary-translator/model-tariff.cny.json"
  ```

  - 预期：两行 sha256 相同；目录 `10001:10001 700`，文件 `10001:10001 600`。sha256 不同就删掉副本重做，不往下做。
  - 单价文件不是秘密，但仍按 secret 目录的规矩放（0600），门禁的 `secret_distribution` 对它与其他文件一样检查。
  - 只读 Agent Host 的文件，不改它和它所在目录的属主或权限。
  - 以后 Agent Host 的单价文件更新时（换价目版本），这份副本要同时更新并重新核对 sha256；否则机翻按旧价记账。写进交接记录。
  - 这份副本不在 `<树>` 里（`stage_secrets.py` 只管理 Host 的那份），所以 `stage_secrets.py check` 看不到它，以上面的 sha256 核对为准。
- **同步到 DATA_HOST 的 `$RUN`**（register 读的是 `$RUN` 下的树），做法同 run-release53-54 S6 第 6 步。

**2. 差异核对（OPERATIONS §8a.11a，只读；L6 对 R53 的 S1：队列授权要算进去）**

清单一侧，从本次 `$RUN/src` 的两份清单取**全部**授权：

```bash
jq -r -s '(map(.principals[])|map({(.role):.principal_id})|add) as $id | map(.grants[])[] | "\($id[.principal]) \(.resource_id)"' \
  deploy/authorization/service-grants.v1.json deploy/authorization/stage-principals.v1.json | sort -u > manifest-grants.txt
wc -l < manifest-grants.txt                                   # 79
grep -c 'NexLoop.queue\.' manifest-grants.txt                 # 5
```

- 不按 `resource_type` 过滤，也不按资源名前缀过滤。队列授权的资源是大写开头的 `eios:action:NexLoop.queue.*`，按 `eios:action:nexloop.%` 过滤会把它们漏掉。
- 5 行队列授权：`claim_extraction_scheduler` 和 `claim_extraction_worker` 的 `NexLoop.queue.claim-extraction:1`，`runtime_worker`、`message_relay`、`role_queue` 的 `NexLoop.queue.operations:1`。
- 另有 object_type 4 行（claim_matcher 2、commitment_keeper、commercial_recorder）和 artifact 3 行。

库一侧，从授权事实的 payload 展开，口径同 register 自己的 `_service_grant_triples`（`nexloop_eios/service_grants.py`）：fact kind `grants`，subject_kind 为 `service` 或 `agent`，只取 `grants` 非空的事实。已撤销的授权事实还在，只是 `grants` 为空，所以不能直接取 `entity_key`。

```bash
ro "select distinct f.payload->>'principal_id'||' '||(g->'resource'->>'resource_id') from authz.nexloop_authority_facts f
    cross join lateral jsonb_array_elements(f.payload->'grants') g
    where f.tenant_id='<TENANT_ID>' and f.fact_kind='grants' and f.payload->>'subject_kind' in ('service','agent')
      and f.payload->>'principal_id' like 'nexloop-service-%' order by 1" | sort -u > db-grants.txt
grep -c 'NexLoop.queue\.' db-grants.txt                       # 5
comm -23 manifest-grants.txt db-grants.txt                     # register 会新增的
comm -13 manifest-grants.txt db-grants.txt                     # register 会撤销的
```

预期：

- `db-grants.txt` 有 **78** 行，其中队列授权 5 行。没有 5 行，说明库一侧的提取漏了队列授权，先修提取，不往下做。
- `comm -23` **恰好一行**：`nexloop-service-world-glossary-principal eios:action:nexloop.world_glossary.translate:1`。
- `comm -13` **为空**。`nexloop-sim-*` 不以 `nexloop-service-` 开头，不在这里；它们在 register 输出的 `extra_grants_outside_manifest` 里。
- 出现别的行，按 §8a.11a 逐条查来源（`git log -S`）。`follow_latest_version` 的那一条授权（claim_matcher 的 `Consumer.edit`）可能因为库里是后继版本而出现，按 §8a.11a 判断。交调度员确认后才 register。
- operations 不比较（§8a.11a）。本次清单里没有改 operations 的授权（v22 → v23 只新增一条）。

**3. register 前的 dry-run（不连库）**

```bash
python3 <检出>/scripts/deploy/stage_secrets.py register --out <树> --tenant <TENANT_ID> --dry-run
```

预期 `principals` **26**、`credential_files` **35**，`grants` 里有一条 `world_glossary_translator action eios:action:nexloop.world_glossary.translate:1 execute`。

**4. register**（DATA_HOST，用含 v23 / v8 的 `$RUN/src`，命令同 run-nx054-s8-backfill 第 6 步第 3 条）。预期：

- `grants_added` **1**（等于第 2 步 `comm -23` 的行数），`grants_revoked` 0；
- `credentials_created` 只有 `world_glossary_translator` 一条；
- `extra_grants_outside_manifest` 只有 `nexloop-sim-*`；
- authority revision 从 3539 前进（记下新值）。

**5. 核对**：重跑第 2 步，`comm -23` 和 `comm -13` 都为空，`db-grants.txt` 为 79 行。

- **撤回**：
  - 撤销授权：用 `<树>.before-r54`（没有 `app:world-glossary-translator/service-credential` 条目）配 53af5efc 的 v22 / v7 清单再 register 一次，`grants_revoked` 应为 1。不能用当前的 `<树>` 配旧清单：v7 没有对应的主体，register 会以 `a generated credential file has no principal` 拒绝，什么也不写。
  - 删除 APP_HOST 上的 `$NEXLOOP_ROOT/secrets/world-glossary-translator/`（S10 的 timer 停用之后）。

### S7 词表 v1 发布（DATA_HOST，一次性容器，配置员窗口）

1. **校验**（不连库）：

   ```bash
   docker run $CHECK_COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/world_glossary_apply.py \
     --manifest /opt/release/deploy/configuration/world-model-glossary.v1.json --check
   ```

   预期 `{"valid": true, "manifest_version": 1, "entries": 304, "manifest_sha256": "0e25be1f4e42e1567e302a92eb01f4c1ae5251961a4a030557d1505b49f64ffe"}`。sha256 与 `sha256sum $RUN/src/deploy/configuration/world-model-glossary.v1.json` 相同。

2. **发布**：

   ```bash
   docker run $CHECK_COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/world_glossary_apply.py \
     --manifest /opt/release/deploy/configuration/world-model-glossary.v1.json \
     --tenant "$TENANT_ID" --expected-revision 0 --bootstrap-dsn-file /run/bootstrap-dsn
   ```

   预期：`"applied": true`、`"entries": 304`、`expected_revision_passed` 0、`expected_revision_recorded` 0；`result` 中 `manual_entries` **304**、`restored` 0、`replay` false。

3. **重放**：同一条命令再执行一次。预期 `"applied": true`，`result.replay` 为 **true**，`result.entries` 304，`expected_revision_recorded` 0。重放不写任何东西：修订历史条数不变。
   - 同一版本号配不同的文本会以 40001 拒绝（`version 1 published with another text`）；`--expected-revision` 不等于当前版本也以 40001 拒绝。两种都不写。

4. **核对**（只读）：

   ```bash
   ro "select manifest_version, manifest_sha256, expected_revision, entries from control.nexloop_world_glossary_publications where tenant_id='$TENANT_ID'"
   ro "select source, count(*) from control.nexloop_world_glossary where tenant_id='$TENANT_ID' group by 1"
   ro "select count(*) from control.nexloop_world_glossary_revisions where tenant_id='$TENANT_ID' and manifest_version=1"
   ```

   预期：一行 `1 | 0e25be1f…ffe | 0 | 304`；`manual 304`，没有 `machine`；修订 304 行；第 3 步重放之后仍是这三个值。

- **撤回**：词表只是显示数据。要撤回某条，发布 v2 把它写成 `null`（恢复机翻）或 `""`（只显示英文）；不直接改表。整体撤回见 §4 R1。

### S8 告警规则 v22（DATA_HOST，配置员窗口）

```bash
docker run $CHECK_COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/alert_rules_apply.py --manifest /opt/release/deploy/configuration/alert-rules.v22.json --check
docker run $CHECK_COMMON $WINDOW "$BACKEND_IMAGE" /opt/release/scripts/deploy/alert_rules_apply.py --manifest /opt/release/deploy/configuration/alert-rules.v22.json \
  --tenant "$TENANT_ID" --bootstrap-dsn-file /run/bootstrap-dsn
```

- 预期：`--check` 为 `{"valid": true, "version": 22, "rules": 81}`；应用后 `applied: true`。
- 下一轮告警评估之后（约 1 分钟），`nexloop-doctor --observability` 的 `alert_rules_current` 为 true。
- **时序**：`timer_stale_world_glossary` 由 host-health 的 timer 读数驱动，S10 第 3 步把 timer 加进 host-health 之前没有这项读数，所以发布本步不会立刻触发它。`world_glossary_failed` 和 `world_glossary_no_success` 在 S10 之前都是 0。
- **撤回**：告警规则只能前进（不能再发布低于 v22 的版本）。要关掉某条规则，发布新版本。

### S9 镜像与重建

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step rebuild
```

- 镜像只按签名清单里的 digest 载入（`rebuild` 会比对）。
- 用 28e7689a 的 compose 文件。`world-glossary-translator` 属于 profile `world-glossary`，`up -d` **不会**启动它，常驻服务的个数与 release 53 相同。
- 预期：`docker compose -p nexloop-app ps` 里没有 `world-glossary-translator`；其余服务都在运行，日志里没有 `background tick failed`。
- `docker compose -p nexloop-app -f "$NEXLOOP_ROOT/compose.app.yaml" --env-file "$NEXLOOP_ROOT/config/compose.app.env" --env-file "$NEXLOOP_ROOT/config/compose.images.env" config --format json | jq -r '.services["world-glossary-translator"].secrets[].source'` 列出的正好是 compose 里那 8 项：自己的 4 个文件，加 `signing_key`、`pg_ca`、`claim_extraction_worker_model`、`world_glossary_translator_tariff`；**没有** `host_model_tariff`。
- **撤回**：不能只退镜像，见 §4 R1。

### S10 机翻：手工跑一轮，再装 timer 和 host-health（APP_HOST）

**1. 手工跑一轮**（timer 还没装，确认能跑通）：

```bash
docker compose -p nexloop-app -f "$NEXLOOP_ROOT/compose.app.yaml" --env-file "$NEXLOOP_ROOT/config/compose.app.env" \
  --env-file "$NEXLOOP_ROOT/config/compose.images.env" --profile world-glossary run --rm -T world-glossary-translator; echo "exit=$?"
```

- 预期 `exit=0`，输出一行计数摘要（enqueued、translated、failed、manual_kept、quota），不含正文、DSN 或 key。
- 词表 v1 已覆盖 304 个标识。没有译名的标识才进入机翻，每天最多 50 次模型调用。
- 失败时，按输出里的错误类名对照 P6，不重试第二次就交调度员。`PermissionError` 或单价文件相关的错误，先核对 S6 的单价副本（属主、权限、sha256）。

**2. 安装 systemd 单元**（root）。示例写的是 `/srv/nexloop` 和不带 `-p` 的 compose 命令，**必须按 stage 实际填写**。

- 用 `$NEXLOOP_ROOT` 的实际值替换路径。
- compose 命令改成与第 1 步完全相同的形式：`-p nexloop-app -f $NEXLOOP_ROOT/compose.app.yaml`，加两个 `--env-file`。
  - 不带 `-p` 时，项目名会取自目录名，容器接到另一个项目的 `claim_egress` 网络上，与 nft 的 G-CLAIM-EGRESS 规则对不上。

步骤：

- 将 `<检出>/deploy/stage/systemd/nexloop-world-glossary.service.example` 填写后装为 `/etc/systemd/system/nexloop-world-glossary.service`。
- 将 `.timer.example` 原样装为 `/etc/systemd/system/nexloop-world-glossary.timer`。
- 两个文件都是 0644、属 root。
- `systemctl daemon-reload`，再 `systemctl enable --now nexloop-world-glossary.timer`。

预期：

- `systemctl list-timers nexloop-world-glossary.timer` 的下次触发时间落在某个 `x3` 分（03、13……）；
- 第一次触发之后，`journalctl -u nexloop-world-glossary.service -n 20` 有计数摘要，且 `systemctl show -p Result nexloop-world-glossary.service` 为 `Result=success`。

**3. host-health**：把私有 APP_HOST 安装配置的 `probe.timers` 按新示例加上 `world-glossary` 一项，然后重跑安装（幂等）。

```bash
printf 'app_host\t%s\n' "$APP_PW" | sudo bash <检出>/scripts/deploy/host_health_install.sh <私有 app-host 配置>
```

- 口令从 Keychain 经管道传入，不写进命令行，也不进 python heredoc。
- **必须在第 2 步 timer 已经成功跑过一次之后**再加这一项：没有成功记录时探针按 fail closed 处理，30 分钟后会报 `timer_stale_world_glossary`。
- 预期：安装 ok；下一轮探针之后，`world-glossary` 的 `timer_success_age_seconds` 小于 1800，`timer_stale_world_glossary` 不 firing。
- DATA_HOST 的 host-health 不变，不执行，记录“无变化”。

**4. 运维约束**（写进交接记录）：

- 以后每次迁移或 sim-setup 前，先 `sudo systemctl stop nexloop-world-glossary.timer`，再 `while systemctl is-active --quiet nexloop-world-glossary.service; do sleep 5; done`，做完后 `sudo systemctl start nexloop-world-glossary.timer`；
- 不在 02:00 的 PG 备份窗口里做人工操作。

- **撤回**：
  - `systemctl disable --now nexloop-world-glossary.timer`，删除两个单元文件，`daemon-reload`；
  - host-health 私有配置去掉这一项，重跑安装；
  - 已写入的机翻译名保留，它们只是显示数据。

### S11 I6 实测：重叠触发不会算作失败（L6 S11）

目的：确认一次运行正在进行时再触发一次，第二个实例只记 `skipped`；service 不会变成 failed；也不会产生 `timer_scheduled_failure`。

1. **锁被占时触发**（确定能复现的做法）：

   ```bash
   sudo flock /run/lock/nexloop-world-glossary.lock sleep 90 &
   sleep 2
   sudo systemctl start nexloop-world-glossary.service; echo "exit=$?"
   journalctl -u nexloop-world-glossary.service -n 5 --no-pager
   systemctl show -p Result -p ActiveState nexloop-world-glossary.service
   systemctl is-failed nexloop-world-glossary.service; echo "is-failed=$?"
   ```

   - 用 `sleep 90` 占住与单元相同的锁文件，模拟一次正在运行的实例；这不启动任何容器，也不连库。
   - 预期：`exit=0`；journal 最后一行是 `skipped: locked`，没有启动容器（`docker ps -a` 里没有新的 `world-glossary-translator` 容器）；`Result=success`、`ActiveState=inactive`；`is-failed` 输出 `inactive`，`is-failed=1`（不是 failed）。

2. **timer 正在跑时再手工触发**：在 timer 的一次运行进行中（`systemctl is-active nexloop-world-glossary.service` 为 `active`）执行 `sudo systemctl start nexloop-world-glossary.service`。
   - 预期：systemd 不会对正在运行的 oneshot 单元再起第二个实例，这条命令等到当前那次结束才返回，退出码为 0。
   - journal 里只有那一次运行的计数摘要，service 不是 failed。
   - 当前那次运行很快结束、来不及做时，记录“未赶上”，以第 1 项为准。

3. **告警**：等下一轮探针（几分钟），以及告警评估之后：
   - `host_health.timer_scheduled_failures_24h` 对 `world-glossary` 为 0；
   - `timer_scheduled_failure` 不 firing；
   - `timer_stale_world_glossary` 不 firing。

- 任一项不符（第二个实例启动了容器、service 变成 failed、出现 `timer_scheduled_failure`）就停下交 L6；不改单元文件去“修”结果。
- **撤回**：不需要（第 1 项结束后 `sleep` 自行退出、释放锁；确认 `jobs` 里没有残留）。

### S12 部署门禁

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step gate
```

- 私有配置：`expected_gate_failures: ["real_dispatch_closed"]`；`--before` 用 `$NEXLOOP_ROOT/ops/baseline-r36/baseline.json`。
- 私有门禁配置的 `secret_uids` 按新示例加上 `"world-glossary-translator": 10001`，否则 `secret_distribution` 会把新目录当成未声明的目录。
- 预期 **21/22**（自 R55 起，含 `glossary_tariff_copy_matches`），唯一不通过的是 `real_dispatch_closed`。`secret_distribution` 必须通过：`secrets/world-glossary-translator/` 目录 0700、文件 0600、属 10001。连接预算比较两个方向都通过：机翻服务的预算标签是 `none`，不在应用的 60 里。
- `details.glossary_tariff_copy` 必须等于 `sha256_equal`（机翻挂的是自己的单价副本，与 Agent Host 的原件一致）。出现 `translator_reads_original` 或 `translator_not_configured` 都要停。
- gate 步骤渲染的 `compose.json` 带 `--profile world-glossary`（`scripts/deploy/gate_dir.py` 的 `RENDERED_PROFILES`）。手工准备 gate 目录时，也要用 `docker compose ... --profile world-glossary config --format json` 生成，否则渲染结果里没有机翻服务。
- 软链（L6 S21）：门禁按完整路径比较渲染后 Compose 里的单价路径和 gate spec 的 `secret_root` 拼出的路径，不解析软链。如果 `secret_root` 和 Compose 渲染时用的 `NEXLOOP_ROOT` 一个经过软链、一个不经过，即使是同一个文件，也会误判为 `translator_tariff_elsewhere`。两边要用同一种写法，与 `compose.app.env` 里的 `NEXLOOP_ROOT` 一致。
- 出现别的失败就停，不 promote。
- **撤回**：不需要（只检查）。

### S13 promote 与部署后核对

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step promote
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step post
```

- `promote` 只在本 release 最近一次门禁只因预期项失败时执行；`post` 的 `post_deploy_check` 为 `pass: true`，日志错误统计不超过上限。
- 只读核对：
  - `ro "select max(version) from control.schema_migrations"` 为 `0374`；
  - `ro "select count(*) filter (where source='manual'), count(*) filter (where source='machine') from control.nexloop_world_glossary where tenant_id='$TENANT_ID'"`：manual 仍为 304，machine 为机翻写入的条数；
  - `ro "select count(*) from control.nexloop_world_glossary_usage where tenant_id='$TENANT_ID' and reserved_at >= now() - interval '24 hours'"` 不超过 50；
  - `ro "select basis, count(*) from runtime.nexloop_cost_entries where entry_id like 'model:world-glossary:%' group by 1"`：只有 `configured_rate`。出现 `unpriced` 说明单价副本没有挂上，回到 S6 的单价副本一步。
  - 所有服务运行至少 5 分钟后，`ro "select count(*) from pg_stat_activity where datname=current_database()"` 记入证据；应用服务的总数不超过 60（机翻运行时多 1 个，算运维保留）；
  - `nexloop-doctor` 全部通过；工作台“告警”页没有新的 firing。
- **撤回**：见 §4 R1。

### S14 浏览器走查（调度员，合成账号 approver3，S13 之后）

stage 工作台，用 approver3（合成 owner）登录。口令从 Keychain 取，只在浏览器的登录框里输入，不进命令行，也不进截图。

**走查只看不改**：不点任何“确认”或“保存”提交，打开的编辑器和确认面板都用“取消”或“关闭”退出。

每页记录：页面能打开、没有整页空白、没有错误边界的告警框、浏览器控制台没有报错；截图不含口令和地址。

| # | 页面（侧栏） | 怎么看 | 预期 |
|---|---|---|---|
| W2 | 模型与资产 › 企业世界模型 › **Object Type**，切到关系图 | 看默认视图；点一个类型（如 Consumer）打开详情，再点“适应画布”；再用 Tab/Enter、方向键、+/−、0 操作 | 节点标签带中文注释，例如 `Consumer（客户）`，长注释换行，不溢出节点。每个节点都在画布内，标签不小于 12 px。**打开详情后点“适应画布”，画面仍框住全部节点，选中的类型仍在视野里并保持选中**（加粗边框，屏幕阅读器读出“已选中”）（L6 S3）。同一对类型间的多条关系分开画 |
| W4 | **实验空间**（复走） | 打开列表和一条实验的详情（没有实验就看空状态） | 同 release 53+54 单的 W4：侧栏不标“未启用”；列表只有计数，不出现客户；预算演练的实验带“预算演练”标记和说明；登记、开始、停止只打开确认面板，然后“取消”；Escape 也能关闭 |
| W5 | 企业世界模型各页：Object Type、Event Type、Link、Action、Function、实例、对象集 | 逐页看表格和详情；把鼠标停在一个“机翻”标记上；各页底部的“改正…的中文译名”（Object Type、Property、Link、反向名、Action、Function、Event Type、栏目名各一处）任选两处，打开后“取消” | **排版**：标识写成“英文（中文）”，没有译名时只显示英文，**没有空括号**；栏目名也带注释；窄屏下注释换行，不挤压表格列。**机翻标记**：只有机翻条目在括号后有小号“机翻”，悬停提示“由模型翻译，负责人可改正”；词表 v1 发布的 304 条是人工译名，不带标记。**改正入口**：owner 在真实世界看到“改正…的中文译名”按钮；打开后有“中文译名”输入框，提示“1–24 个字，需含中文；保存后作为人工译名，机翻不会再覆盖。”，然后“取消”，不保存。模拟世界中按钮禁用，说明“只能在真实世界改正” |

- 任一项不符：截图（不含敏感信息）并记录控制台报错，交调度员；不在 stage 上改任何东西。W2、W5 任一失败就不判本 release 通过。
- **撤回**：不需要（只看）。

## 3. 证据与判定

| 步骤 | 执行者记录 | 判定者（L6）核对 |
|---|---|---|
| S1–S5 | 各步 `stage_release.py` 日志行（已脱敏）、两次备份的标签、预演与发布前后的计数、roles 的 owner 授权数 | 迁移头 0374、definer 0 problems；预演只有 `world_glossary.set:1`；owner 95 |
| S6 | `add-service` 与 `check` 的输出；单价副本的两行 sha256 与 `stat`；`manifest-grants.txt` 与 `db-grants.txt` 的行数和队列授权行数；两个方向的 `comm` 输出；register 的 `grants_added`、`grants_revoked`、`credentials_created`、新的 authority revision | 79 / 78 行，队列授权两侧都是 5；`comm -23` 恰好是机翻那一行，`comm -13` 为空；`grants_added` 1 |
| S7 | `--check`、发布、重放三次输出；三条核对查询 | sha256 与 28e7689a 的文件一致；manual 304；重放 `replay: true`，不写 |
| S8 | `--check` 与应用的输出、doctor 的 `alert_rules_current` | v22，81 条 |
| S9 | `docker compose ps`、机翻服务 secret 列表 | 机翻服务没有常驻；secret 正好 8 项 |
| S10 | 手工一轮的计数摘要与退出码、`list-timers`、第一次 timer 运行的 journal 与 `Result`、host-health 安装输出与探针读数 | 退出 0；`Result=success`；timer 成功读数小于 1800 |
| S11 | 第 1–3 项的命令输出、journal 行、`docker ps -a` | `skipped: locked`、`Result=success`、不是 failed、没有 `timer_scheduled_failure` |
| S12–S13 | 门禁失败项名单、promote 与 post 的输出、只读核对、连接数 | 只有 `real_dispatch_closed`；成本只有 `configured_rate` |
| S14 | W2、W4、W5 每项的结论、截图、控制台记录 | W2、W5 符合预期 |

证据里不贴 DSN、口令、令牌、地址或主机名，只写角色名（APP_HOST、DATA_HOST、CI 主机）。

## 4. 回退

**R1 整体回退到 release 53：不能只退镜像。** release 53 的镜像按 `assembly.open_core` → `bootstrap.verify` 要求迁移谱系与镜像目录完全相等（同 run-release53-54 S9a），在 0374 的库上启动即以 `MigrationDrift` 退出。所以整体回退只有一条路，需要调度员决定：

1. 停掉机翻 timer（S10 撤回）；
2. 停掉 APP_HOST 全部应用服务（`docker compose -p nexloop-app stop`）；
3. 用 S1 的迁移前备份（迁移头 0373）做 pgBackRest 恢复；恢复点之后的数据丢失，包括词表、机翻结果和这段时间的业务数据；
4. 恢复后 `ro "select max(version) from control.schema_migrations"` 必须为 `0373`；
5. 授权和目录随库恢复回到 S1 时的状态（revision 3539、v48 角色、v21 告警规则）；秘密树用 `<树>.before-r54`，APP_HOST 删除 `secrets/world-glossary-translator/`（含单价副本），host-health 私有配置去掉 `world-glossary` 后重跑安装，门禁私有配置去掉对应的 uid；
6. 用 known_good（release 53）的镜像和 compose 重建，再跑一次门禁（预期 20/21）。

不想丢数据时，不回退，往前修：在 28e7689a 之上修复后发新版本，库不动。只关掉机翻，则停用 timer 即可（下表 S10）。

| 步骤 | 怎么撤回 | 代价 |
|---|---|---|
| S3 迁移 | 只能按 R1 用迁移前备份恢复 | 恢复点之后的数据丢失；需要调度员决定 |
| S4 Action 发布 | 不能删除；撤回 S5 让无人持有授权 | 无 |
| S5 角色 | 重新应用 v48 | owner 失去改正入口（按钮提交被拒） |
| S6 主体与 register | `<树>.before-r54` 配 v22 / v7 清单再 register（`grants_revoked` 1）；删除 APP_HOST 上的新目录 | 机翻无法认证 |
| S7 词表 | 发布 v2，用 `null` 或 `""` 撤回单条；整体按 R1 | 无删除路径 |
| S8 告警规则 | 只能发布更高的版本 | 无 |
| S9 重建 | 按 R1 | 同 R1 |
| S10 timer 与 host-health | `disable --now` timer、删单元文件、`daemon-reload`；host-health 去掉该项重跑安装 | 不再新增机翻；已有译名保留 |
| S11 I6、S12 门禁、S14 走查 | 不需要（只检查、只看） | 无 |
| S13 promote | 按 R1，或往前修 | 同 R1 |
