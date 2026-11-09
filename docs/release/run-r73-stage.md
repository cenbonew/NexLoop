# 执行单：R73 的 stage 部署（第 142–144 批：guard 透传故障判定、0428、0429）

R73 = main **2026c088**（第 141 批）+ 第 142 批（ADR-045 guard 透传，3e74b6b4）+ 第 143 批（NX-055 done，只有文档和 planning）+ 第 144 批（0429，L2 c0cf241b + 29d7a09e）。合并提交的 hash 由调度员组批后填入私有配置。stage 当前是 R72（**cff5985f**，迁移头 0427）。

步骤沿用 R72：迁移 → definer → publish → register → roles → 镜像 → gate → promote → post。工具和记号同 `run-release54-stage.md` §0（`stage_release.py --config <私有配置> --step …`，`ro "…"` 为 DATA_HOST 上的只读会话，`$T` 为租户）。本单不写主机名和地址；两台主机按角色写（APP_HOST、DATA_HOST）。

## 0. 这次改了什么，哪些不动

| 项 | cff5985f → R73 | 本单的处理 |
|---|---|---|
| 迁移 | 0428（data_health 召回不再把词表别名算作缺向量，顶层 attention 重算）、0429（合成删除前核对 WebChat 授权，临时编号，以组批后的 catalog 为准） | S3 |
| Python（backend 镜像） | runtime guard 透传 `synthetic_model_fault`（`runtime_control.py`）；`schema_gates` D23 豁免；`erase_synthetic_consumer.py`；`s6_stage_schema.py` | S6 重建 |
| Action、business-actions、capabilities、service-grants、workbench-roles、stage-principals、告警规则 | **无变化**（`git diff --stat cff5985f <R73> -- deploy` 为空，组批后再核一次） | publish、register、roles 只核对“无变化” |
| Agent Host 镜像 | **不换**，见 §0.1 | 不重启 agent-host |

### 0.1 Agent Host 镜像为什么不用换

- `deploy/community/Host.Dockerfile` 的输入只有：Python 与 Node 基础镜像（`versions.lock.json` 的 images 段）、`scripts/agent_host.py`、编译后的 `apps/agent-host`（runtime.tar、main.js、package.json）。镜像里**没有** `packages/eios-core`。
- cff5985f → 29d7a09e 之间 `apps/`、`scripts/agent_host.py`、`deploy/community/` 都没有变化；`versions.lock.json` 只改了 `bootstrap_revision`（0427→0428→0429），不在 Host 镜像的输入里。
- Host 读 `synthetic_model_fault` 的代码（`runtime-host.ts:247`、`:448`；`pi-runtime-adapter.ts:285`）R72 已经有，缺的只是 guard 的应答字段。
- 组批后的核对：签名清单里 agent-host 的 digest 应与 R72 相同。不同就停下，查 Host 镜像的构建输入有没有变化。

### 0.2 会随 backend 镜像一起重建的容器

`compose.app.example.yaml` 里用 `BACKEND_IMAGE` 的服务都会重建，包括 **agent-link-relay**（`<<: *service`，镜像是 backend，`network_mode: service:agent-host`）。relay 重建时，Host 和 guard 之间的连接会断几秒：正在运行的 Run 的授权请求可能失败一次，按现有规则失败关闭。agent-host 容器本身不重启。建议在没有顾客发言的时段执行 S6。

relay 只做 TCP 转发（`nexloop_eios.loopback_relay._pipe` 读多少写多少，不解析 HTTP），TLS 在 Host 和 guard 两端建立，所以 relay 不会丢应答字段。

## 1. 前提（不满足就不开始）

| # | 条件 | 怎么核 |
|---|---|---|
| P1 | R73 的 CI 通过，已合入 main，release 已构建、签名、封存 | `--step build`、`--step sign` 的 JSON 为 ok |
| P2 | stage 迁移头是 0427 | `ro "select max(version) from control.schema_migrations"` 为 `0427` |
| P3 | ADR-045 开关**已关闭**（不要在开关打开时迁移和重建） | `ro "select runtime.nexloop_metrics_snapshot('$T','real')->'synthetic_fault'"` 中 `active` 为 0 |
| P4 | 没有正在进行的合成删除（0429 原地重定义了 0220 的 handler） | 与负责人确认；`ro "select state, count(*) from control.nexloop_consumer_erasures where tenant_id='$T' and world='real' and state not in ('completed','completed_with_holds') group by 1"` 无行 |
| P5 | APP_HOST 上没有 CI 在跑（CI 会增加回环监听，导致 `existing_services_unchanged` 失败） | CI 主机的队列；APP_HOST 上 `ss -ltn` 与 R72 基线比较 |

## 2. 步骤

### S1 迁移前差异备份

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step backup
```

预期：备份标签写进日志；失败就停。

### S2 只读预检，并记下部署前读数

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step precheck
```

部署前读数（S7 用来比较）：

```bash
ro "select runtime.nexloop_data_health('$T','real')->'recall'"
ro "select runtime.nexloop_data_health('$T','real')->'attention'"
```

R72 上 recall 块预期为 `missing_embeddings` 161、`status` attention、`reasons` 含 `missing_embeddings`，并且没有 `gloss_aliases` 字段。顶层 attention 的值记为 A0。

### S3 迁移 0428、0429，definer 检查

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step migrate
```

预期：

1. 迁移头为 R73 catalog 的最后一个版本（组批后的 0429 号）。
2. definer 检查结果为 `0 problems`（LC_ALL=C.UTF-8）。新增或改名的 security definer 函数如下：
   - 0428：`runtime.nexloop_data_health`；
   - 0429：`control.nexloop_consumer_webchat_grants`、`authz.nexloop_workbench_observe_read`。
3. 出现 DeadlockDetected：先确认事务已回滚，重试一次；仍失败就让 worker 停止取任务后再试（同 R36 的处理）。

**0429 对在用会话和 worker 的影响（核对结论）：**

- **`authz.nexloop_workbench_observe_read` 改名后包装**：
  - 原函数改名为 `…_before_grants_held`，并撤销 `nexloop_api` 的执行权；新函数沿用原名，只授权给 `nexloop_api`。
  - 改名、建新函数和撤权都在迁移的同一个事务里，其他会话看不到中间状态。
  - API 进程按名字调用。psycopg 用的预备语句会因 pg_proc 变化而失效，下次调用重新解析名字，取到新函数。
  - 最坏情况：迁移提交那一刻正在执行的一次工作台读，可能因权限被撤销而失败一次（工作台显示不可用，刷新即可）。
  - 只有 `erasure_precheck` 这个 verb 的返回多了 `held.grants_held` 和 `held.grants_trace`，其他 verb 原样返回。
  - worker 不调用这个函数。
- **`control.nexloop_governed_erase_synthetic_consumer` 原地 `create or replace`**：
  - registry 以 regprocedure 引用它，OID 不变，所以不用重新注册 Action，函数复制守护已登记。
  - 只在负责人执行合成删除时调用（P4 已确认没有进行中的删除）。
- **`control.nexloop_consumer_erasures` 加了可空列 `grants_cleared`**：`alter table … add column` 不带默认值，只改元数据，锁持有时间很短；已有的行该列为 null。
- 0429 不碰 runtime 队列、Run 和消息表，正在运行的 Run 和 worker 不受影响。

### S4 Action 发布：只核对无变化

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step publish
```

预期：预演里没有新增或变更的 Action，发布也没有新增。出现任何新增就停下核对（R73 没有新增 Action）。

### S5 register 与 roles：只核对无变化

- register：先 `stage_secrets.py register --out <树> --tenant <TENANT_ID> --dry-run`，用 `run-release54-stage.md` S6 第 2 步的 `comm` 两个方向比较。预期两个方向都为空，就不执行 register。
- roles：

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step roles
```

预期 workbench-roles 版本与 R72 相同，没有新增授权。

### S6 镜像与重建

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step rebuild
```

预期：

- `digests_match: true`；
- backend 的 digest 是新的，agent-host 和 gateway 的 digest 与 R72 相同（§0.1）；
- backend 镜像的容器都重建了，包括 api-1/2、worker、agent-link-relay；agent-host 没有重启：

```bash
docker inspect -f '{{.State.StartedAt}}' nexloop-app-agent-host-1   # 早于本次重建
```

### S7 部署后只读核对（gate 之前）

**data_health：**

```bash
ro "select runtime.nexloop_data_health('$T','real')->'recall'"
ro "select runtime.nexloop_data_health('$T','real')->'attention'"
```

- `missing_embeddings` 为 0，`gloss_aliases` 为 161；`status` 为 ok，`reasons` 为 `[]`；`coverage_ratio` 为 1（entries 不为 0 时）。
- 顶层 attention 小于 A0。R72 上它由 0245 时的块状态计算，0428 改为按六个块当前的 status 重新计数，所以 recall 块转为 ok 后至少少 1。
- `missing_embeddings` 不为 0：用 `ro "select gloss_source is null, count(*) from ontology.nexloop_recall_entries e where tenant_id='$T' and not exists(select 1 from ontology.nexloop_recall_embeddings m join ontology.nexloop_recall_settings s on s.tenant_id=e.tenant_id and m.profile_id=s.active_profile_id where m.entry_id=e.entry_id) group by 1"` 区分。剩下的应当是真正缺向量的定义条目，交 L6，不回退。

**0429 对象与权限：**

```bash
ro "select p.oid::regprocedure, pg_get_userbyid(p.proowner), p.prosecdef, p.proacl from pg_proc p where p.oid in ('authz.nexloop_workbench_observe_read(text,text,text,text,text)'::regprocedure, 'authz.nexloop_workbench_observe_read_before_grants_held(text,text,text,text,text)'::regprocedure, 'control.nexloop_consumer_webchat_grants(text,text,text,text)'::regprocedure, 'control.nexloop_governed_erase_synthetic_consumer(text,text,text,text,text,jsonb)'::regprocedure, 'runtime.nexloop_data_health(text,text)'::regprocedure, 'runtime.nexloop_data_health_before_gloss_not_missing(text,text)'::regprocedure)"
ro "select column_name, data_type, is_nullable from information_schema.columns where table_schema='control' and table_name='nexloop_consumer_erasures' and column_name='grants_cleared'"
ro "select has_function_privilege('nexloop_api','authz.nexloop_workbench_observe_read(text,text,text,text,text)','execute'), has_function_privilege('nexloop_api','authz.nexloop_workbench_observe_read_before_grants_held(text,text,text,text,text)','execute'), has_function_privilege('nexloop_api','control.nexloop_consumer_webchat_grants(text,text,text,text)','execute'), has_function_privilege('nexloop_domain_worker','control.nexloop_consumer_webchat_grants(text,text,text,text)','execute')"
```

预期：

- 六个函数都存在，owner 为 `nexloop_owner`；
- `observe_read` 和 `webchat_grants` 的 `prosecdef` 为 true；`governed_erase_synthetic_consumer` 为 false，与 0220 相同；
- `grants_cleared` 为可空的 boolean；
- 第三条依次为 `t, f, f, f`：API 只能执行新包装的 observe_read，旧函数和 `webchat_grants` 都不对 API 或 worker 开放。

### S8 部署门禁

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step gate
```

- 私有配置的 `expected_gate_failures` 与 R72 相同（R73 不新增也不移除预期失败项）。
- `existing_services_unchanged` 应为 pass。若失败：
  - 先看 APP_HOST 上有没有 CI 或别的一次性容器增加了回环监听（P5），等它们结束后再跑一次 gate；
  - 不要为了通过把它加进 expected。
- 镜像 digest 不在 gate 里核对，由 S6 的 `digests_match` 保证；gate 之前 S6 必须是 `digests_match: true`。
- 出现其他非预期失败就停，不 promote。

**gate 和 promote 分开执行**：gate 跑完，看完失败名单并记下证据后，再单独执行 S9。不要写成 `gate && promote`。

### S9 promote 与 post

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step promote
```

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step post
```

预期：promote 只在最近一次 gate 只有预期失败时执行；`post_deploy_check` 为 `pass: true`，日志错误统计不超过上限。

### S10 存量顾客的残留授权撤销（customer-4、refusal-1、refusal-2）

依据 `run-erase-synthetic-consumer.md` 的“存量顾客”一节（第 144 批）。这三个顾客在 0429 之前已经删除，删除时没有核对授权，它们的登录主体仍持有 WebChat 授权。由调度员执行，在 DATA_HOST 的配置员窗口里操作。

1. **撤销前的阳性对照**（只读）：

   ```bash
   ro "select f.entity_key[1] principal, count(*) filter (where jsonb_array_length(coalesce(f.payload->'grants','[]'::jsonb))>0) n_grants, count(*) n_facts from authz.nexloop_authority_facts f where f.tenant_id='$T' and f.fact_kind='grants' and f.entity_key[1] in ('<PRINCIPAL-customer-4>','<PRINCIPAL-refusal-1>','<PRINCIPAL-refusal-2>') group by 1 order by 1"
   ```

   预期三行，每行 `n_grants` 大于 0（stage 11-09 读数为 5）、`n_facts` 为 5。少于三行就停下：登录主体写错，或者事实不在。

2. **改私有配方**（webchat-consumers 配方，不提交到仓库）：
   - 只在这三个顾客的条目上加 `"revoke": true`；`principal_id`、`subject_id`、`persona` 保持不变。
   - 其他顾客的条目（s6c-1/2/3、customer-5 等）原样保留，不要删除条目。删除条目不会撤销授权，只是不再发布。
   - 这三个条目如果已经不在配方里，就按私有记录补回，带上 `revoke: true`。
   - 改之前把配方复制一份 `.before-r73`，权限 0600。

3. **先 `--check`，再 `--apply`**：

   ```bash
   python3 <检出>/scripts/deploy/webchat_consumers.py --role deploy/authorization/webchat-consumer-role.v1.json --recipe <私有配方> --check
   ```

   ```bash
   python3 <检出>/scripts/deploy/webchat_consumers.py --role deploy/authorization/webchat-consumer-role.v1.json --recipe <私有配方> --apply --bootstrap-dsn-file <F> --signing-key-file <management/signing.key.hex> --signing-key-id <ID>
   ```

   预期：summary 中 `revoked` 为 3，`granted` 等于其余顾客数，与 R72 时相同；authority revision 前进 1。

4. **撤销后**：再跑第 1 步的 SQL。三行的 `n_grants` 都为 0，`n_facts` 仍为 5（空授权集，说明确实撤销了，而不是查不到）。`n_facts` 为 0 不算通过。

5. **其他顾客不受影响**：用同一条 SQL 查 s6c-1/2/3 的登录主体，`n_grants` 应与撤销前相同（大于 0）。ADR-045 演练要用这三个账号。

回退：用 `.before-r73` 配方再 `--apply` 一次，三个顾客恢复授权（只有在误撤了其他顾客时才需要）。

## 3. 部署之后由负责人执行的两件事（调度员通知，不在本单的步骤里）

### 3.1 Consumer v2 的 r8 重跑

- 脚本：`local-only/nx040/consumer-v2-r8.sh`，按 R72 的做法生成模板，并加上 r8。
- 预期：
  - r8 结果 `tool_cases_built` 一步有 `open_input` 计数（stage 形状下约 90，以实际为准）和 `no_invalid_case` 名单，`actions` 为 Consumer 绑定的 Action 总数；
  - 评估报告 14 个闸门全部 pass，其中 `tool_context_compatible` 的 evidence 前两条是 `tools:regression_set` 和 `tools:open_input:<N>`；
  - 结论为 `accepted_for_release`。
- 仍被 `tool_context_compatible` 拒绝：看 `incomplete` 与 `failures` 名单，交 L7。不要改模板来凑通过。

### 3.2 ADR-045 故障演练（R72 的 3 次留作证据）

1. 负责人先 `fault-switch.sh close`，再 `fault-switch.sh open`：3 名成员（s6c-1/2/3），`remaining` 9。快照应为 `active 1, remaining 9, members 3`。
2. 按调度员的轮次发言，3 个顾客共 9 次。
3. 每个 Run 的预期：
   - `runtime.nexloop_synthetic_fault_decisions` 中 `injected=true`；
   - Run 证据 `failure` 为 `synthetic_model_fault`，model calls 0，cost 0；
   - 顾客约 5 分钟后收到兜底回复，不重发；
   - `remaining` 每次减 1。
4. 若再次出现“判定为注入但模型调用成功”：先查 guard 应答。在 APP_HOST 的 agent-host 网络命名空间里看不到明文，只能从 Host 的行为判断：activation 带上 `fault:true` 后，Run 会以 `synthetic_model_fault` 结束。再核对 S6 中 backend 的 digest 是否为 R73 的。
5. 9 次用完时开关自动关闭（`exhausted`）；否则演练结束后负责人执行 `close`。

## 4. 证据

| 步骤 | 记什么 | 通过标准 |
|---|---|---|
| S1–S3 | 备份标签、precheck 输出、部署前 recall 与 attention（A0）、迁移头、definer 输出 | 迁移头为 0429（组批后的号），definer `0 problems` |
| S4–S5 | 预演计数、`comm` 两个方向的输出、roles 版本 | 全部无变化 |
| S6 | 签名清单与加载后的 digest、agent-host 的 `StartedAt` | backend 是新 digest，agent-host 不变、未重启 |
| S7 | recall 块、attention、0429 的三条 SQL 输出 | `missing_embeddings` 0，`gloss_aliases` 161，attention 小于 A0；权限为 `t, f, f, f` |
| S8–S9 | gate 失败名单、promote 与 post 的输出 | 只有预期失败项，post 为 `pass: true` |
| S10 | 撤销前后的 `n_grants` 与 `n_facts`、apply 的 summary、s6c 三人的读数 | 3 行 5→0，`n_facts` 仍为 5；s6c 三人不变 |

## 5. 回退

| 步骤 | 回退 | 影响 |
|---|---|---|
| S3 迁移 | 已发布的迁移不回退。0428 有问题时，另写一个迁移重新包装 `runtime.nexloop_data_health`；0429 有问题时，新迁移恢复原函数名和 0220 的 handler 体 | 只影响 data_health 读数，以及负责人的删除预查和合成删除 |
| S6 镜像 | 用 R72 的签名清单再 `--step rebuild` | guard 不再透传，ADR-045 演练无效，其他功能与 R72 相同 |
| S10 撤销 | `.before-r73` 配方再 `--apply` | 恢复这三个已删除顾客的授权 |
