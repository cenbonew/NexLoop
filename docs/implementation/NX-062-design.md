# NX-062 设计稿：企业自有 Agent / 程序的授权接入（API 密钥、MCP、SDK）

- **状态**：设计稿第 2 版。L6 审 c52993cb 判条件 PASS（C1–C9，见 §10）；负责人 2026-11-09 已决定 Q-A 至 Q-G（§7）。按 M1→M6 分片实现，**§10 优先于正文中与之冲突的表述**。
- **来源**：负责人 2026-11-09 选项 7B：“NX-062 现在开工，可以新开线程：先出设计稿，交 L6 审，再分片实现”。执行：L10。
- **基线**：main 038dba21（catalog 尾部 0429；0430–0432 被在途批次占用，本设计的迁移临时号从 0433 起）。
- **上位决定（不重议）**：`adr/ADR-041-machine-access-channel.md`（负责人 2026-10-28，Q-1 至 Q-9 全部 A）与威胁评审 `docs/design/NX-062-threat-review.md`（T-1 至 T-12）。本稿把它们落到现有代码上，回答“具体怎么做、分几片、每片怎么验收”。文中 `T-n` 指威胁评审条目，`ADR §n` 指 ADR-041 的小节。
- **已完成的前置**：C8-1 路由清单与发布门（`deploy/authorization/api-routes.v1.json`，当前 manifest_version 12，106 条，realm 7 种；G-1 至 G-6，`docs/implementation/NX-062-C8-1-route-inventory.md`）；I31 / S79 服务凭据续期与范围轮换（0384、0391）；NX-056 统一审批（0327、0347、0369 等）。

## 0. 一页结论

1. **主体**：企业程序是 EIOS 的 `SubjectKind.SERVICE` 主体，另在 NexLoop 侧登记为 `machine_client`（新表），**不新增 EIOS 主体种类，不做委托**。它与 Pi Run、渠道、`service-grants` 清单里的平台服务主体用不同的登记来源、不同的凭据前缀、不同的 realm，互不通用。
2. **身份推导**：请求只带 `Authorization: Bearer nxm_…`。API 计算 HMAC 摘要 → SQL 按摘要找到唯一一行密钥 → 推出 tenant、world、machine principal、scope。请求里出现身份字段一律 422（ADR §5）。
3. **授权是三者合取，在 PG 受治理入口最终判定**：密钥 scope ∩ 机器主体的当前服务授权 ∩ 能力当前策略（含 NX-056 审批）。新增 `control.nexloop_machine_capabilities` 允许表（append-only + “永不授予”触发器），复用 0124 的统一受治理入口，不另开写路径。
4. **三种接入形态只有一个服务端**：`/machine/v1/*` HTTP API（新 realm `machine_key`）。MCP 是本地 stdio 客户端，SDK 是 Python 薄封装，两者都不持有服务端权限（ADR §9，Q-5）。
5. **第一期只开放只读动词 + 无外部效应的受治理 Action**（Q-1），外部效应放第二期并强制审批，机器只能是请求人（Q-6）。
6. **分 6 片**（M1–M6，§6），每片独立可合、可回滚；M1–M3 是服务端主干，M4 读取，M5 写入，M6 客户端。需要负责人决定的 6 项见 §7。

## 1. 主体模型

### 1.1 选型

| 选项 | 说明 | 结论 |
|---|---|---|
| A. EIOS `SERVICE` 主体 + NexLoop `machine_client` 登记 | 复用 EIOS 授权事实（subject、membership、actor、authentication、grants、scope、policies）与现有 `PostgresAuthorityUnitOfWork`；NexLoop 侧一张登记表区分“企业程序”与“平台服务” | **采用** |
| B. 新增 EIOS 主体种类 `enterprise_agent` | 要改 vendored `eios.identity.models.SubjectKind` 与 `_fact_resolver`，所有按 kind 分支的 SQL 都要复查；与上游分歧变大，收益只是一个标签 | 不选 |
| C. 委托（代表负责人或员工） | 违反“不伪造人类身份”，与 ADR-033、Q5 冲突 | 不选（ADR §3 B、C） |

### 1.2 与现有服务主体的区别

现有 `nexloop_eios/service_grants.py` 编译的平台服务主体（extractor、relay 等）由部署时的可信配置（`deploy/authorization/service-grants.v<N>.json` → 0050 configurator）写入，凭据是 `sha256(token)` 摘要，由 `stage_secrets.py register` 签发。机器主体**不走这条路**：

| 维度 | 平台服务主体（service-grants） | 机器主体（本设计） |
|---|---|---|
| 登记者 | 部署（可信配置文件 + configurator 角色） | 负责人，在工作台经受治理 Action（Q-4） |
| 凭据摘要 | `sha256(token)` | `HMAC-SHA256(pepper_v, token)`，记 pepper 版本（ADR §2.2） |
| 凭据前缀 | 无固定前缀 | `nxm_<prefix_id>_<secret>`，`prefix_id` 非秘密 |
| 有效期 | 部署参数 `credential_expires_at` | 必填，≤ 90 天；每主体最多 2 把有效密钥，重叠 ≤ 7 天（Q-2） |
| 可得授权 | `service-grants` 清单里的 Action / object_type | 只能来自 `control.nexloop_machine_capabilities` 允许表 |
| realm | `service_bearer` | `machine_key`（互斥） |
| doctor | `service_grants doctor` 管辖 | `service_grants doctor` 把机器主体视为“外部管理”，不当漂移；另有 `machine_access doctor`（M3） |

`service_grants.FORBIDDEN_MARKERS` 与机器的“永不授予”集合（§3.4）是两份独立清单，M2 的测试断言后者 ⊇ 前者。

### 1.3 tenant、actor、world 的推导

- `machine_client` 行带 `tenant_id`；`machine_key` 行带 `tenant_id`、`world`（Q-7：一把密钥一个 world）。
- API 只做：取 `Authorization` 头 → 校验前缀形状 → 用当前 pepper 版本集合计算候选摘要 → 调 `authz.nexloop_machine_identity_snapshot(digest)`（SECURITY DEFINER，M2）。函数按**当前事实**检查密钥 `active`、未过期、未吊销、所属 machine_client `active`、来源网段（可选，Q-8），返回与 `authz.nexloop_service_identity_snapshot` 同形的 binding，外加 `machine:{client_id, key_prefix}`。**不缓存“有效”**；Valkey 只允许缓存“已吊销”负结果（T-2）。
- actor = machine_client 的 EIOS principal；world = 密钥的 world。后续所有 SQL 调用都只传摘要，SQL 自己从摘要再推一次（与 0124 入口 `p_digest` 的做法一致），应用层推出的值不被信任。
- 请求体、查询串里出现 `tenant_id`、`world`、`actor`、`principal_id`、`subject_id`、`on_behalf_of` 返回 422；应用不读 `X-NexLoop-*`、`X-Forwarded-User` 等头，Gateway（Caddy）对 `/machine/*` 删除它们（T-5）。

### 1.4 停用与级联

`nexloop.machine_client.deactivate` 在同一事务里把名下全部 `active` 密钥置 `revoked`（revoked_reason `client_deactivated`）。重新启用（`activate`）不改密钥行；旧密钥永不复活，必须重新签发（T-2）。

## 2. 三种接入形态

### 2.1 共用部分（服务端唯一）

```
客户端（任意 HTTP / SDK / MCP stdio）
  │ TLS, Authorization: Bearer nxm_…, Idempotency: intent_id（写）
  ▼
Gateway：只转发 /machine/v1/*；删身份头；/internal/* 404
  ▼
nexloop-api machine realm（新模块 nexloop_eios/machine_http.py）
  1 realm 判定（C8 清单 machine_key 路由；其他凭据 401）
  2 形状：前缀、长度；摘要（常量时间比较由 SQL 唯一索引等值命中完成，不做应用层逐把比较）
  3 请求 Schema：能力的参数 Schema，未知字段 / 身份字段 422
  4 限速与预算（PG，§3.5）
  ▼
PG：authz.nexloop_machine_call(digest, capability, intent_id, payload)  ← 最终判定
  → 读取：control.nexloop_machine_read_<verb>（投影在 SQL）
  → 写入：现有 authz.nexloop_goal_governed_action 统一入口（0124 注册表）
  ▼
审计 runtime.audit_events（进入 NX-059 哈希链）
```

### 2.2 API 密钥（M1–M3）

| 环节 | 设计 |
|---|---|
| 格式 | `nxm_` + 12 位 base32 `prefix_id` + `_` + 43 位 base64url（32 字节随机）。`prefix_id` 进审计和工作台，`secret` 只在签发响应里出现一次 |
| 签发 | 受治理 Action `nexloop.machine_key.issue:1`，subject_rule `human`，handler 只允许负责人（Q-4）。参数：client_id、world、scope（能力名列表，必须 ⊆ 主体授权）、expires_at（≤ 90 天）、可选 source_cidrs。明文由 **API 进程**生成（`secrets.token_bytes`），只把摘要与 pepper 版本交给 SQL；响应体经 `Cache-Control: no-store` 返回一次，之后任何读取都不含明文 |
| 存储 | `authz.nexloop_machine_keys(tenant_id, key_id, prefix_id unique, client_id, world, digest bytea unique, pepper_version, scope text[], expires_at, status, revoked_at, revoked_reason, issued_by, issued_intent)`；无明文列；`digest` 唯一索引用于常量时间等值查找 |
| 轮换 | `nexloop.machine_key.rotate:1` = 签发新钥 + 给旧钥设 `expires_at=min(旧到期, now+overlap)`，overlap ≤ 7 天；第三把有效密钥签发失败（`machine_key_limit`） |
| 吊销 | `nexloop.machine_key.revoke:1`，立即生效；下一次请求 401（两 API 进程测试，T-2） |
| 过期 | 由 identity snapshot 按 `clock_timestamp()` 判定，不依赖清理任务；清理任务只把过期行状态改为 `expired` 供展示 |
| pepper | 私有配置文件 `machine_key_peppers`（版本 → 32 字节），由 API 进程读取，与审计链封印密钥走同一私有备份；新签发只用最新版本；旧版本在其下最后一把密钥失效后由 doctor 报告可撤下。pepper 不进 PG、不进 Runtime（Pi 无此文件），丢失即全部机器密钥失效——恢复演练覆盖（T-1，见 Q-B） |
| 作用域 | 能力名集合（如 `object.read`、`object.list`、`commitment.record_note`），不是路由 |
| 网段 | 可选，默认关（Q-8）；Gateway 去掉转发头时以 Gateway 写入的可信头为准，见 Q-E |

### 2.3 MCP（M6）

- 只做本地 stdio 客户端 `nexloop-machine-mcp`（Python，放 `packages/machine-client/`），经 HTTP 调 `/machine/v1/*`（Q-5）。不做托管远程 MCP。
- **工具清单 = `GET /machine/v1/capabilities` 的返回**：服务端按密钥 scope ∩ 主体授权 ∩ 当前允许表计算，返回每个能力的名称、说明（由服务端允许表生成，不接受客户端注入）、参数 Schema、`kind: read|write`、`confirm_required`。MCP 不自带工具定义。
- 只读与写入分开：
  - `read` 能力映射为一个 MCP 工具，直接调用；
  - `write` 能力映射为**两个**工具 `<name>.prepare` 与 `<name>.commit`；`commit` 必须带回 `prepare` 返回的 `prepare_digest`（ADR §6，T-4）；工具描述标明“需要确认”。
- 不提供审批决定、密钥管理、授权管理类工具（这些能力本来就不在允许表中，服务端也不会列出）。
- 密钥来源：`NEXLOOP_MACHINE_KEY_FILE` 指向 0600、本人所有的文件；不接受命令行参数，不读 `NEXLOOP_MACHINE_KEY` 明文环境变量（见 Q-F）；启动时检查文件权限，不符即退出。

### 2.4 SDK（M6）

- `nexloop_machine` Python 包，只是 HTTP 的薄封装：`Client(base_url, key_file, ca_file=None)`，`capabilities()`、`read(verb, **params)`、`prepare(cap, intent_id, **params)`、`commit(cap, intent_id, prepare_digest)`、`query(intent_id)`。
- 地址与 CA 都是构造参数或配置文件，**包内无任何部署地址**（T-10）。
- 重试语义：网络错误或 `unknown` 时**先 `query(intent_id)`**，不重发 commit；`429` 按 `Retry-After`；`409` 不重试（T-6）。
- `repr`、异常、日志里不含密钥（用非十六进制标记值断言）。
- TypeScript SDK 不在本任务范围（见 Q-D）。

## 3. 授权

### 3.1 每次调用的检查顺序

1. API：realm、前缀形状、Schema、限速预检（只是提前拒绝）。
2. SQL `authz.nexloop_machine_call`（SECURITY DEFINER，`search_path` 固定，revoke PUBLIC，只 grant `nexloop_api`）：
   1. 由摘要推身份（与 §1.3 同一函数）；
   2. `capability ∈ key.scope`；
   3. `capability ∈ control.nexloop_machine_capabilities`（当前行、`enabled`）；
   4. 主体当前持有对应 Action / 读取的 EIOS grant（`nexloop_assert_action_authority`，与人类路径同一函数）；
   5. 能力策略：0327 审批门；有外部效应的能力若无审批策略则 `approval_policy_required`（第二期才会遇到）；
   6. 限速与预算原子扣减（§3.5）；
   7. 写入：意图 ID 去重（§3.3），再进 0124 入口；读取：调用投影读取函数。
3. 派发（effect 能力，第二期）：联系限制、勿扰、频率、真实派发开关在派发时照常检查，机器路径不另开（ADR §6）。

### 3.2 统一受治理入口的改动

- 0124 的 `control.nexloop_governed_capabilities.subject_rule` 只有 `human`、`agent_or_human`，且表是 append-only。**不改它的 check 约束**，另建 `control.nexloop_machine_capabilities(capability pk references nexloop_governed_capabilities 或读取动词注册, kind read|write, effect bool, param_schema jsonb, projection jsonb, description text, source_task, registered_at)`，append-only；入口在 `subject_rule` 判定前加一条分支：主体是 machine_client 且能力在机器允许表中 → 视为允许的作者，否则沿用原规则（machine_client 对 `human` 能力一律 42501）。
- 改入口只能用新迁移 `create or replace` 包装（0124 头注释约定“只加注册行”，所以 M2 需 L6 明确同意这一次包装，见 Q-A）。

### 3.3 写入：意图 ID、两步确认、unknown

- `runtime.nexloop_machine_intents(tenant_id, client_id, intent_id, capability, request_digest, prepare_digest, phase prepare|committed|unknown, result jsonb, created_at, committed_at)`，PK `(tenant_id, client_id, intent_id)`。
- `prepare`：校验参数 → 规范化 → 写一行 `phase=prepare` 返回 `{normalized, prepare_digest, expires_at}`（有效期见 Q-C 推荐 10 分钟）。
- `commit`：同一事务里锁该行，比较 `prepare_digest` 不同 409，再调用 0124 入口；先持久化后 ACK。
- 同一意图 ID：内容相同返回首次结果，内容不同 409；服务重启后仍去重（T-6）。
- effect 能力的结果为 unknown 时返回 `{status:"unknown", query:"/machine/v1/intents/{intent_id}"}`，不重发。

### 3.4 永不授予

`control.nexloop_machine_capabilities` 的触发器拒绝下列能力（按名与前缀），清单检查同时锁定（ADR §2.4，T-3）：审批决定；密钥、服务授权、成员与角色管理（含 `nexloop.machine_*` 自身）；Schema、策略、规则、关系类型发布；擦除与保留决定；真实派发开关、`channel.enable`；补救的人工决定；`service_grants.FORBIDDEN_MARKERS` 全部。NX-056 审批：机器主体既不能是审批人，也**不能在 solo 模式下自批**——0327 的 `nexloop_approval_solo_effective` 分支在 M5 加 machine_client 排除，并有测试（Q-6 在 solo 模式下的落地）。

### 3.5 速率与预算

- `runtime.nexloop_machine_rate_buckets(tenant_id, bucket_kind key|tenant|key_day_actions, bucket_key bytea, window_start, count)`，键由服务端从身份推出（HMAC，不以客户端 IP 为唯一键），与 `browser_rate_limits` 同一做法。
- 阈值来自版本化配置（告警规则同样的审查流程），不写死在代码里；超出 429 + `Retry-After`。
- 机器路由在 API 进程内有独立并发上限（信号量），连接池独立份额，避免挤占工作台（T-8）。

### 3.6 审计

每次调用（含拒绝）写 `runtime.audit_events`：actor=machine principal，`machine.key_prefix`、`intent_id`、`capability`、`trace_id`、结果码、`decision_reason`（固定原因码，如 `scope_missing`、`approval_pending`）。**不记**密钥、摘要、参数值与正文；只记参数的规范化摘要。密钥生命周期事件（签发、轮换、吊销、停用级联）同样进链（T-9）。读取按密钥按天计数，与工作台读取审计同一口径（T-12）。

## 4. 安全不变量

| # | 不变量 | 如何保证 | 由哪个测试守住 |
|---|---|---|---|
| I-1 | 密钥不进 prompt | Runtime / Agent Host 不读 pepper 文件、无 `/machine/*` 客户端；Context 组装不含 `machine_keys` 表（无 grant） | M2：`nexloop_runtime` 角色对新表无任何权限；M3：Agent Host 构建产物 grep 无 `nxm_` 前缀处理代码 |
| I-2 | 不进前端 | 工作台只显示 `prefix_id`；签发结果只在签发响应里出现一次，前端不写 localStorage | M3：web dist 无 `nxm_` 字面量；Playwright 刷新后看不到明文 |
| I-3 | 不进日志 | API 访问日志对 `Authorization` 头打码；异常链 `from None`（与 `authenticate_service` 同法） | M1/M3：用标记值请求后 grep 进程日志、审计、PG 全表 |
| I-4 | 不出现在 URL | 只接受 `Authorization` 头；查询串出现 `key`、`token`、`nxm_` 前缀 → 400 且不记录查询串 | M3 |
| I-5 | 不绕过 EIOS | 写入只经 0124 入口；机器 realm 无 SQL 直写；清单 G-3 核对机器路由的 port 只能是 `sql:nexloop_machine_call` 或读取函数 | M3 清单测试；M5 突变检查 |
| I-6 | 模拟/shadow 不能用真实效果凭据 | 密钥绑定 world；sim/shadow world 密钥的 effect 能力在 SQL 拒绝；real world 密钥不能读 sim 数据 | M2 world 矩阵 |
| I-7 | 擦除覆盖 | 新表都进 consumer/member erasure coverage；`machine_intents.result` 只存 ID 与结果码，不存消费者正文；若存 consumer_id，由擦除函数改写 | M2、M5：`test_consumer_erasure_coverage_pg`、`test_member_erasure_coverage_pg`、`test_erasure_whole_database_pg` |
| I-8 | 吊销即时 | 不缓存“有效”；两进程测试 | M2 |
| I-9 | gitleaks 能拦 | `.gitleaks.toml` 加 `nxm_` 规则 | M1 |

关于 I-7：`machine_clients` 记的是企业程序，不是人；`issued_by` 是负责人 member 的 principal ID——member 擦除覆盖要处理它（假名化，与审批表 `requester_subject` 的处理一致），M2 先跑 `test_member_erasure_coverage_pg` 确认是否被判为需要覆盖。

## 5. 契约

新增 `packages/contracts/` 文件（线路规则禁止本线改契约，以下是提案；每个新 Schema 都配 `examples/<name>.valid.json`，并跑 `test_contracts`、`test_published_contracts`、`generate --check`）：

| Schema | 用途 | 片 |
|---|---|---|
| `machine-client-view.schema.json` | 工作台机器主体列表与详情（无密钥，只 `prefix_id`、world、scope、到期、状态） | M3 |
| `machine-key-issued.schema.json` | 签发 / 轮换的一次性响应（含 `secret`，`writeOnly` 语义，文档注明只出现一次） | M3 |
| `machine-capabilities.schema.json` | `GET /machine/v1/capabilities`：能力名、kind、confirm_required、参数 Schema、说明；MCP 工具清单的唯一来源 | M3 |
| `machine-call.schema.json` | 读请求、`prepare` 请求 / 响应、`commit` 请求 / 响应、`intents/{id}` 查询；统一错误体 `{error: <code>}` | M4、M5 |
| `machine-read-<verb>.schema.json` | 每个机器读取动词的返回投影（字段集合 = 登记投影） | M4 |

此外 `deploy/authorization/api-routes.schema.json` 增加 realm `machine_key` 与 kind `machine_capability`（不在 `packages/contracts/`，C8 清单旁边），manifest_version +1。

## 6. 分片计划

通用要求：迁移用临时号 0433 起，文件名带 `nx062`；revision 只出现在 catalog.json、versions.lock.json 和一个测试常量；新表进 erasure coverage；每片跑 `tests/test_bootstrap.py` 与定向 PG 测试（≤ -n 3）；新 Action 要更新 `test_stage_register_pg` 的注册计数；新 Action 部署时需 `stage_secrets` publish（否则 404）；mutation 检查指“删掉一条关键判定后对应测试必须失败”，在实现报告里列出每次删改与失败的测试名。

### M1 密钥格式与 pepper（无迁移）

- **交付**：`nexloop_eios/machine_keys.py`（生成、解析、HMAC 摘要、pepper 文件读取与版本选择）；`.gitleaks.toml` 规则；`prepush-scan` 识别 `nxm_` 字面量。
- **测试**：单测（格式、版本选择、常量时间、`repr` 无明文）；gitleaks 命中合成密钥；mutation：去掉 pepper 版本字段 → 轮换测试失败。
- **部署影响**：无（未接入路由）。
- **验收**：单测与 gitleaks 测试通过；`check_publication` 通过。

### M2 机器主体与密钥存储、身份函数（迁移 0433、0434）

- **交付**：0433 `authz.nexloop_machine_clients`、`authz.nexloop_machine_keys`、`authz.nexloop_machine_identity_snapshot`；0434 `control.nexloop_machine_capabilities` + 永不授予触发器 + 受治理 Action `nexloop.machine_client.register/deactivate/activate`、`nexloop.machine_key.issue/rotate/revoke`（handler 只认负责人）及其注册行。EIOS 授权事实由 handler 经 configurator 同一写入函数生成（复用 `compile_principal` 的事实形状，但 `credential_kind='api_key'`、摘要换成 HMAC）。
- **测试（真实 PG）**：签发后全库扫描无明文；过期与吊销后第一次 401；第三把密钥拒绝；重叠 > 7 天拒绝；world 绑定；停用级联且重新启用不复活；两连接吊销可见性；pepper 轮换（旧版本过渡期内可认证，撤下后 401）；永不授予触发器（手工插入审批决定能力被拒）；`nexloop_runtime` 对新表无权限；erasure coverage 三项；`service_grants doctor` 不把机器主体当漂移。mutation：去掉 `client.status='active'` 条件 → 停用级联测试失败。
- **部署影响**：新迁移 + 新 Action 需 publish；无路由变化。
- **验收**：以上全部通过；bootstrap fresh catalog 通过；L6 审 SQL（SECURITY DEFINER、search_path、grant）。

### M3 machine_key realm、管理页与 C8 扩展（迁移 0435）

- **交付**：`nexloop_eios/machine_http.py`（`/machine/v1/capabilities`、`/machine/v1/intents/{id}`）；0435 `authz.nexloop_machine_call` 骨架（身份、scope、允许表、限速表 `runtime.nexloop_machine_rate_buckets`）；工作台“机器接入”页（负责人：登记、签发、轮换、吊销、停用；签发结果只显示一次）；C8 清单 realm `machine_key` 与 G-5 扩展为“路由 × {无凭据、机器密钥、workbench cookie、consumer cookie、Run 令牌、渠道凭据}”矩阵，矩阵为空或行数 ≠ 路由数 × 凭据种类数即失败；Gateway 对 `/machine/*` 删身份头；契约 `machine-client-view`、`machine-key-issued`、`machine-capabilities`。
- **测试**：G-5 矩阵（真实 PG + 真实 HTTP）；realm 互斥；身份字段 422；查询串带密钥 400；429 + Retry-After；日志 / 审计 / web dist 无明文；Playwright 签发流程；`test_stage_compose` 与解析 compose 的测试（若改 Caddy）。mutation：删掉 realm 互斥判断 → G-5 失败。
- **部署影响**：新路由、新页面、Caddy 配置；deploy gate 后再 promote；告警规则新版本（机器密钥拒绝率）。
- **验收**：stage 上用 test world 合成机器主体：capabilities 返回空列表（允许表尚无能力），其他 realm 的路由 401；执行人与判定人分开记录。

### M4 只读动词与字段投影（迁移 0436）

- **交付**：首批读取动词（推荐 `object.get`、`object.list`、`object.neighbors`、`commitment.list`，见 Q-C）登记进允许表，各自 SQL 读取函数在 SQL 内做投影；默认投影不含会话正文、消息正文、联系方式、自由文本备注（Q-9）；分页与单次上限；按密钥按天读取计数；契约 `machine-read-*`。
- **测试**：返回字段集合 = 登记投影（新增字段不登记即失败）；默认投影无正文与联系方式（非十六进制标记值）；他人 / 不存在对象 404 且响应相同；已擦除消费者、legal hold、过保留期与工作台读取结果一致（复用 NX-054 判定，不另写）；租户 A 读不到 B。mutation：把投影改为 `select *` → 字段集合测试失败。
- **部署影响**：新迁移；capabilities 开始返回读取工具。
- **验收**：stage test world 演练，机器读取与工作台读取逐字段比对一致；L6 判定。

### M5 无外部效应的写入（迁移 0437）

- **交付**：`runtime.nexloop_machine_intents`；`prepare` / `commit` 两步；0124 入口的机器分支（Q-A）；首批写能力（推荐 1–2 个无外部效应、已有人类入口的能力，例如为对象追加备注、登记一条用户陈述为待核实 Claim，见 Q-C）；0327 solo 自批排除机器主体。
- **测试**：身份 / 未知字段 422；他人对象 404；同意图同内容返回首次结果且只有一条 Action 记录；同意图不同内容 409；摘要不一致 409；重启后去重；effect 能力（人为登记一条测试能力）无审批策略时 `approval_policy_required`；机器在 solo 模式下自批被拒且不计入法定人数；审计 actor、key_prefix、intent_id 正确并进哈希链、带密钥校验通过；erasure coverage。mutation：去掉 `prepare_digest` 比较 → 409 测试失败；去掉机器分支的允许表检查 → 越权测试失败。
- **部署影响**：新迁移、入口包装、新 Action 注册计数。
- **验收**：stage test world 演练一次完整 prepare → commit → query；L6 判定。

### M6 客户端：SDK 与 MCP（无迁移）

- **交付**：`packages/machine-client/`（`nexloop_machine` SDK + `nexloop-machine-mcp` stdio）；README 写明地址与 CA 均为配置项；许可证与 `THIRD_PARTY_NOTICES` 若有新依赖需重生成。
- **测试**：真实 HTTP（起本地 API + 真实 PG）端到端：MCP 工具列表 = capabilities；read 工具；write 的 prepare/commit；unknown 时调 query 不重发（打桩网络中断）；密钥文件权限不符即退出；`--help`、异常、日志无密钥；构建产物无地址字面量；`check_publication`。
- **部署影响**：无服务端变化。
- **验收**：本地真实 HTTP 测试通过；L6 判定。外部效应能力（第二期）不在本任务的 M1–M6 内，另立分片并强制审批策略。

### 依赖与并行

M1 → M2 → M3 → {M4, M5} → M6。M4 与 M5 可两条线并行（文件不重叠：读取函数与意图表 / 入口包装分开），M6 可在 M3 合入后基于 capabilities 契约先做客户端骨架。

## 7. 需要负责人决定的事项

| # | 问题 | 选项 | 推荐 | 理由 |
|---|---|---|---|---|
| Q-A | 0124 统一入口加机器分支 | A. 新迁移包装一次，加“机器允许表”分支<br>B. 机器写入另建一个入口函数 | **A** | 只有一个受治理写入口，审批门、claim fence、commit tail 都复用；B 会多一条要同步维护的写路径。代价是破例改一次 0124 的“只加注册行”约定，需 L6 同意 |
| Q-B | pepper 放哪里 | A. API 进程读私有文件，PG 只存版本号<br>B. 放 PG（与 authority signing keys 同表） | **A** | 拿到数据库备份不足以离线撞密钥；Runtime 与 PG 角色都看不到 pepper。代价是备份与恢复演练多一项 |
| Q-C | 第一批开放的能力 | A. 读：对象详情 / 列表 / 邻居 / 承诺列表；写：对象备注、用户陈述登记为待核实 Claim<br>B. 只读，写入等第一个真实企业接入再定 | **A** | Q-1 已定“只读 + 无外部效应写”；给一两个写能力才能端到端验证两步确认与意图去重；都不产生外部效应 |
| Q-D | SDK 语言 | A. 只做 Python（SDK + MCP）<br>B. 同时做 TypeScript | **A** | MCP 与 SDK 共用一套 Python 代码；TS 客户端等有真实需求再做，HTTP 契约已足够 |
| Q-E | 来源网段白名单的地址来源 | A. 只信 Gateway 写入的可信头（Gateway 覆盖客户端同名头）<br>B. 一期不实现网段白名单 | **B** | Q-8 定的是“可选，默认关”；stage 反代会去掉转发头，可信头链路需要单独审查，等有固定出口的接入方再做 |
| Q-F | 客户端是否接受明文环境变量 | A. 只接受 0600 密钥文件路径<br>B. 也接受 `NEXLOOP_MACHINE_KEY` 明文环境变量 | **A** | 环境变量容易被进程列表、CI 日志、崩溃转储带出；文件路径同样适配容器 secret 挂载 |
| Q-G | prepare 是否保存规范化参数（L6 新增，对应 C6） | A. 不保存，commit 时客户端重传完整参数，服务端重算摘要比对<br>B. 保存，进擦除覆盖并按 TTL 清除 | **A** | prepare 行不含消费者正文，擦除面最小 |

**负责人 2026-11-09 决定**：Q-A、Q-B、Q-D、Q-E、Q-F、Q-G 全部按推荐，即 A（按 C7 的条件包装 0124）、A（pepper 用私有文件，纳入恢复演练）、A（只做 Python）、B（一期不做网段白名单）、A（只接受 0600 密钥文件）、A（不保存参数，commit 时重传）。Q-C 选“读 + 写对象备注”：读取开放对象详情、列表、邻居和承诺列表；写入只开放对象备注。Claim 登记等 AT-064 白名单处理好之后再开，不在这一期。

## 8. 不做（沿用 ADR §5）

委托与紧急提权；托管远程 MCP 与独立 Agent 网关；出站 webhook；工作台写入口通用分发；机器主体创建消费者、成员或其他主体；OAuth 第三方授权；请求签名（Q-3，第二期再评）；第二期外部效应能力。

## 9. 待实现时核实（不在本稿下结论）

- `nexloop_assert_action_authority` 对 machine_client 的 `SERVICE` 主体是否需要额外的 actor 事实字段；M2 先写失败测试再定。
- 0327 审批门 `requester_subject` 对服务主体的取值路径（当前注释称服务、Agent、Run 凭据在入口即被拒）；M5 需要确认机器请求进入待审批的路径可达。
- 工作台 human / service 主体区分处显示机器主体的方式（`machine` 标签），M3 对照现有审批视图与审计视图实现。

## 10. L6 条件（c52993cb，条件 PASS）与落实位置

负责人 2026-11-09 的决定见 §7 表下。以下条件由 L6 在对应分片的审查中逐条核对。

| # | 片 | 条件 | 落实 |
|---|---|---|---|
| C1 | M2/M3 | realm 互斥在 SQL 层保证：机器身份函数只认 `machine_keys` 的 HMAC 摘要；`nexloop_service_identity_snapshot` 不认机器的 EIOS 认证事实，反之亦然 | 机器主体的 authentication 事实不写进 service snapshot 可查到的位置（或 snapshot 显式排除 machine_client）；真实 PG 交叉测试：nxm 密钥走 service_bearer、service token 走 machine，两边都 401 |
| C2 | M2 | 先按 `prefix_id` 找行，再用该行的 `pepper_version` 算**一次** HMAC 做等值比较；不按多个 pepper 逐个试；过期、吊销、停用都用 `clock_timestamp()` | 替代 §1.3 中“候选摘要”的说法：API 传 `(prefix_id, digest)`，SQL 用 `prefix_id` 定位后比较 digest |
| C3 | M2 | 允许表不设 `enabled` 列，改为追加 revision 行（`revision`、`state active|retired`），取每个能力最新一行；只能经迁移或负责人的受治理 Action 写入，machine 主体不能写；“永不授予”黑名单之外再加白名单触发器：只接受已注册的读取动词和 `subject_rule` 不是 `human` 的 governed capability；名单锁进清单检查 | 替代 §3.1 第 3 步与 §3.2 中的 `enabled` |
| C4 | M3/M4 | 限速阈值配置缺失或解析失败时拒绝请求并告警（失败关闭），不得变成不限速；capabilities 为空返回空列表可以；G-5 矩阵为空或行数不等于路由数 × 凭据种类数判失败 | M3 测试：删掉配置后请求被拒，并产生告警 |
| C5 | M5 | commit 时重跑 §3.1 的 2.1–2.6，不沿用 prepare 的判定；`prepare_digest` 绑定 tenant、client、capability、规范化参数和 world；prepare 过期返回 410；0124 的业务意图 ID 由 `(client_id, intent_id)` 确定性推导，0124 调用与意图行更新在同一事务里；崩溃重启后用同一 intent 查询，结果与首次一致 | M5 |
| C6 | M5 | 采用 Q-G A：prepare 不保存规范化参数，只存摘要；commit 时客户端重传完整参数，服务端重算摘要比对；意图行不含消费者正文。意图表仍进入擦除覆盖检查 | 替代 §3.3 中 prepare 保存 `normalized` 的说法（响应里可以返回给客户端，但服务端不保存） |
| C7 | M5 | 0124 包装用改名加委托的方式实现，非机器路径的行为完全不变，原有回归测试保留；机器分支同时要求能力在允许表中、密钥 scope 包含该能力、`assert_action_authority` 通过；`subject_rule` 为 `human` 的能力一律 42501；变异检查：删掉其中任何一条都要有测试变红 | M5 |
| C8 | M5 | 机器不能当审批人：在审批决定入口的 SQL 里强制，不能只靠 Action 允许表；solo 模式排除机器自批，并要有测试 | M5 |
| C9 | M3 | Caddy 会在应用拿到请求前把完整 URI 记进访问日志：给 `/machine/*` 配置日志脱敏，或者不记录 query；在 deploy 测试里断言 | M3 |

范围调整（Q-C）：M5 的写入能力只有对象备注；“用户陈述登记为待核实 Claim”不在这一期，要等 AT-064 白名单处理好之后再开，届时只能经现有的 Claim 受治理 Action 写入。
