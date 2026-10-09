# NX-018 开发线 L1 报告

BASE_SHA `f58f3ee35ba0296271a5d0498693d474d220d801`；按调度员要求先 rebase 到 `dispatch/integration-s3b`（`ccdd33d`），再 rebase 到 main `3ad4061`（迁移至 0073，0074 预留给 NX-046）。迁移临时编号：第 1 步 **0075**（最初 0066），第 2 步 **0076**（最初 0067），Message READ 派生 **0077**（曾为 0073）。下文正文中的 0066/0067/0073 即现在的 0075/0076/0077。由于 0074 空缺，`tests/test_bootstrap.py` 的连续性两例在本分支上必然失败；本地临时改为连续 0074–0076 后 4 passed（未提交），合并定号后恢复（分支 claude/nx018-finish-997fcd）。迁移临时编号：第 1 步 0071（原 0066），第 2 步 0072（原 0067），Message READ 派生 0073。下文第 1、2 步正文中的 0066/0067 即现在的 0071/0072。迁移使用临时编号，调度员合并时重编号。本报告只登记本线实际执行的定向测试；不是全量 CI，不改变 planning 状态。

环境：macOS arm64，Node v24.13.0，Python 3.12.10（uv），Homebrew PostgreSQL 18.4。**本会话 shell 缺省 LANG/LC_ALL 为空，PG18 拒绝启动**（`postmaster became multithreaded during startup`），所有命令前需 `export LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8`。

## 第 1 步：当前 formal Source READ 贯穿 model/start/submit/admit/finalize（临时 0066）

- 基线核对：`docs/tmp/nx018-formal-current-0066-integration-review/ROOT_0065_BASE_SHA.json` 中 13 个文件的 sha256 与本线 BASE_SHA 逐项一致；`git apply --check formal-current-precise.diff` 成功后应用（role_runs / runtime_activation / effect_intents / effect_execution 四个 Python 增量）。
- `0066_nx018_formal_current.sql` = `FORMAL_CURRENT_DRAFT.sql`，私有 alias `*_before_formal_current_DRAFT` 改为 `*_before_formal_current_v0065`；包裹 0065 的 public command 链（activation / intent / execution），0065 receipt recovery 和 `nexloop_receipt_reconcile_command` 不受 formal 条件影响（execution 仅 admit/finalize 且带 role_envelope 时检查）。共享 helper `nexloop_assert_context_proof_deadlines`、`nexloop_context_formal_current` 只在 0066 安装一次。catalog 追加 0066 条目，lock bootstrap_revision 0065→0066；0001..0065 文件与 checksum 未改。
- 测试：复制 main-ready `tests/test_role_formal_current.py`、`tests/test_role_formal_sql.py`（无手工 DRAFT）。

### 首次失败与诊断（保留）

1. `pytest tests/test_bootstrap.py tests/test_role_formal_current.py tests/test_role_formal_sql.py`：2 passed / 21 errors / 15.37s — 环境：PG 因 locale 无法启动（见上）。加 LC_ALL 后 **23 passed / 168.91s**。
2. receipt/role 回归 6 文件：**29 passed / 343.53s**（与 Pi 并发跑）。
3. 真实 Pi `tests/test_role_pi_effect_checkpoint.py`：**首次 FAILED（23.41s），复跑 FAILED（45.35s）**，inspect 返回 503 `runtime_authorization_denied`。同一命令在 BASE_SHA 独立 worktree 上 1 passed / 50.62s → 回归由集成引入。
   - 逐层计时（诊断用临时测试副本，已删除）：所有 `authorize_runtime_activation` 均 allowed；`effect.submit` 2.100s、`effect.find` 2.049s，超过 Host guard 的 2s 总 deadline（`runtime-host.ts`/`runtime-effect-tools.ts` 的 `AbortSignal.timeout(2000)`）。
   - 根因：`backend._invoke` 持有全局 `self._lock`（"This first host is synchronous"），Host 的 inspect 轮询与 model/tool/submit 请求在 guard 端串行。锁计时：BASE 最大 hold 1.307s（submit），最大 wait+hold 约 1.77s，本已很紧；集成后 submit hold 1.674s，再排在一次 inspect（~0.5s）之后即 >2s。`RuntimeActivationPort.authorize` 本身仅 +90ms（423→515ms）；额外成本来自同一 submit 请求中 guard 与 intent 各自重复生成同一 Run 的 Source 签名 Role envelope（~140ms）和 formal READ envelope（~65ms），以及 SQL 侧 before/after 当前性复核。
   - 修复（**未放宽任何 timeout**）：`role_runs.request_envelope_scope()`，只在一次 `runtime_effect_tool` 调用（同一 DB 事务）内复用同一 Run 的 Role/formal 签名 envelope；SQL 在每次使用时仍按当前授权完整复核（读取撤销、修订、期限），不跳过任何检查，不跨请求缓存。修复后锁计时：最大 hold 1.164s、最大 wait+hold 1.662s（优于 BASE）。

### 最终（同一工作树，单次命令）

```sh
export LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8; source ~/.nvm/nvm.sh && nvm use 24
PYTHONPATH=packages/eios-core/src:tests uv run --frozen pytest -q tests/test_bootstrap.py tests/test_role_formal_current.py tests/test_role_formal_sql.py tests/test_role_post_accept.py tests/test_receipt_reconcile_roles.py tests/test_receipt_tail.py tests/test_receipt_revoke_wait.py tests/test_receipt_terminal_rows.py tests/test_role_tail.py tests/test_role_pi_effect_checkpoint.py -n 0 --tb=short --junitxml=.ci-results/l1-step1-final.xml
```

**53 passed / 578.02s，0 skipped**（bootstrap 4 + formal 19 + receipt/role 29 + 真实两 Pi Run 1）。

### 剩余风险

- guard 全局锁串行化是结构性延迟来源：BASE 的 inspect 已有 ~1.77s 的 wait+hold。任何新增授权工作（第 3 步 policy 校验）都会再次逼近 2s。建议（需调度员/负责人决定）：把 `_invoke` 的全局锁改为"请求共享 / 关闭独占"的读写锁，保留关闭时不关 FD/pool 的保证，同时让独立请求并发；这改变 backend 并发模型，需全量 CI。

## 第 2 步：关系更正进入新 explicit v4 Context（临时 0067）——**部分完成，未通过**

- 候选基线：`nx018-relationship-context-tail-candidate` 冻结于 `c6e5065`（0062）。对 7 个改动文件做三方合并（base=c6e5065，ours=本线，theirs=候选），保留 v3 Role 分支、0061 identity、0062 Message READ、0064 期限、0065 recovery、0066 formal-current；新增 relationship_context*.py 三个模块。`nx018-v4-main-merge-candidate` 未采用（它删掉了 `nexloop_assert_relationship_deadlines`，且把 `nexloop_context_artifact_read_dependency` 返回类型由 text 改为 jsonb，会破坏 0063 Role `_v2` 中的 message 分支）。
- `0067_nx018_relationship_context_v4.sql`：reader/recipe/command SQL 原样追加（共享 helper 用 0066 的，不重复安装）；v4 activation 用 `create or replace` 替换链底私有 `nexloop_runtime_activation_command_before_roles_v0062`（已核对候选函数体 = 0053 原函数体 + v4 分支）；v4 依赖读取另建私有 `nexloop_relationship_context_read_dependency`，public `_v2` 包一层按 v4 绑定分流；`nexloop_read_local_artifact_before_roles_v0062` 改名后由 v4 分支接管。未修改 0001..0066。
- **刻意不采用候选的 `guard_timeout_ms`（候选测试用 10000ms）**：Host guard 与工具 HTTP 保持 2s 总 deadline。测试副本 `tests/test_relationship_context_v4.py` 去掉手工 DRAFT 安装与 `guard_timeout_ms`。
- 首次运行 `tests/test_bootstrap.py tests/test_relationship_context_v4.py`：**36 passed / 1 failed / 160.61s**；失败为 `test_real_human_message_v4_bound_artifact[complete]`（两个真实 Host/Pi Run 正向），`runtime_transport_unavailable`。
- 诊断：v4 每次 authorize 自身 ~0.75s（Source 签名关系 envelope ~0.46s、catalog ~0.27s、formal ~0.08s，主要是 `AuthorizedObjectReader._authority` 每次约 8ms、数百条查询）；submit 内 guard 前后各一次，且全局 backend 锁让并发请求排队，submit 2.46–2.58s。
- 已做（单独提交 `21ddfc3`）：backend 生命周期锁改为请求共享 / 关闭独占；第 1 步的请求内 envelope 复用扩展到 v4 关系/formal/catalog envelope。之后 submit/find 1.6–2.07s。
- 尝试过并**已撤回**：跨请求 3s 复用已签发 envelope（最坏 1.33s，v4 正向通过），但使 `test_role_tail::test_context_bind_lock_expiry_rolls_back_binding_and_job` 失败（测试替换短期 READ envelope 验证锁等待后的最终期限，复用绕过了重新生成），也改变了"每次 guard 重新生成证明"的语义，故不保留。
- 当前结果（撤回后）：`tests/test_role_tail.py tests/test_role_pi_effect_checkpoint.py test_relationship_context_v4.py[complete]` 8 passed / 186.87s；随后 `[complete]` 单独连跑 3 次 **全部 failed**（~68s）。即 v4 正向两 Pi 闭环在 2s deadline 下约 1/4 通过，**AT-009 不能判 passed**。
- 定向回归（撤回前，含跨请求复用）：30 个测试文件 280 passed / 10 failed / 1061.51s；Host vitest 141 passed。10 个失败中 8 个是 main 既有回归（message_relay 7、local_message_delivery_assembly 1，见下节），另 2 个（role_tail、两 Pi）由跨请求复用引起，撤回后复跑通过。

建议方案（需调度员/负责人决定）：降低每次 guard 的授权成本而不是放宽 deadline——(a) 授权事实解析在一个只读事务内批量加载同一 Source 的多个 resource（现在每个 `_authority` 独立连接与事务）；(b) 第二次（后置）guard 只复核 SQL 尾检所需最小集合；(c) 或由负责人基于实测确认工具 HTTP 预算。

## main f58f3ee 21 个回归（调度员指派，负责人选定方案 B）

先在 rebase 后分支上复跑 21 例：**21 failed / 120.37s**（`.ci-results/l1-r21-before.xml`）。按错误签名分组后，在独立 scratch worktree 对 045c93f(0061)/c6e5065(0062)/02d921c(0063)/24ec99f(0064)/75ff5da(0065) 逐一定位引入点——21 例并非同一根因：

| 组 | 例数 | 引入 | 根因（一句话） | 修复路径 |
|---|---|---|---|---|
| message_relay | 7 | 0062 | Context 生成要求 Source 当前 Message READ，但没有生产路径为新受理 Message 发布授权事实 | 0073 派生（Source 静态配置规则 + Consumer READ，`message_offering_fixture`） |
| local_message_delivery_assembly / message_driven_delivery / native_web_delivery | 3 | 0062 | 同上 | 0073 派生 |
| offering_runtime_pg | 6 | 0062 | `context_message` 夹具每条消息重发可信配置（`configure_message_read`）推进 epoch，catalog editor 会话 `service credential binding is stale` | 0073 派生；夹具去掉逐消息配置，Source 静态配置规则 + Consumer READ |
| effect_dispatch / effect_worker_cli / local_effect_worker_kill | 3 | 0065 | ① CLI Effect Worker 的 ledger 代理白名单漏了 0065 新增的 `record_effect_query_observation`，reconcile 阶段恒为 AttributeError→`record_unavailable`（产品缺陷）；② `effect_execution_fixture` 缺 0065 的独立 `receipt_reconcile` Action 定义与 executor 授权 | `effect_worker.py` 白名单补一项；夹具补 Action 与 executor 授权；`test_query_only_after_revoke…` 显式撤销 executor recovery 授权以保留"无 recovery 授权只观测"语义；故障注入例 status 期望由 `record_unavailable` 改为 0065 两段式的 `observed_fulfilled`（其余断言不变，且对照证明故障确实命中） |
| context_mode_configuration[extra2] | 1 | 0063 | 用例断言 Host 拒绝 `context-pack.v3`，而 0063 已把 v3 作为受支持 wire | 用例改为未知协议 v9 仍被拒（v3/v4 为受支持 wire） |
| scope_denials real Pi | 1 | 早于 0061（045c93f 已失败） | 被拒 submit 在 guard 内回滚后 `record_runtime_scope_denial` 重新生成全部签名 envelope，耗时 1.92–1.94s，贴 2s 工具 deadline，时过时不过 | 请求内 envelope 复用覆盖整个 `effect_tool`（含拒绝记录），实测 1.41–1.63s；未改 timeout |

最终（同一工作树，广回归 82 个测试文件串行）：**21/21 passed**，见 `.ci-results/l1-broad.xml`。每例路径：message_relay 7 + 投递 3 + offering 6 = 16 例走 **派生**；effect 3、context_mode 1、scope_denials 1 = 5 例与 Message READ 无关。保留可信配置 `configure_message_read` 的只有本意测试"已配置 Message READ 的撤权"（`test_context_source_read_reproduction` 全部 11 例，现在先配置再撤，同时验证撤掉的已配置授权不会被派生复活）以及 v4 关系测试中的人类更正消息（v4 读取尚未切到派生）。

方案 B 实现：`0077_nx018_message_read_derivation.sql`（报告初稿中的 0073）、`nexloop_eios/message_read.py`，设计与实现差异见 `docs/implementation/NX-018-message-read-derivation.md` 第 9、10 节。新增 `tests/test_message_read_derivation.py` **13 passed / 34.89s**：正向（无任何 per-Message 授权事实、epoch 不变、Context 生成走派生）、他租户、他 world（参数与签名声明两种）、未受理、已删除、Consumer READ 撤权、规则停用、规则过期（三者走受治理发布路径）、会话改属其他 Consumer、越界字段/伪造 facts/超规则期限/伪造 Consumer 依据。未覆盖：Run 凭据不派生（实现于 basis，无专门测试）。

广回归命令与结果：

```sh
export LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8; source ~/.nvm/nvm.sh && nvm use 24
PYTHONPATH=packages/eios-core/src:tests uv run --frozen pytest -q -p no:xdist <82 个 message|context|relationship|role_|receipt|effect|delivery|scope|offering|assessment|claim|backend|runtime_|bootstrap|identity|conversation|native_web|trusted_configuration 测试文件> --junitxml=.ci-results/l1-broad.xml
pnpm exec vitest run apps/agent-host/test --exclude 'docs/tmp/**'
```

第一次（基于 ccdd33d）**1031 passed / 1 failed / 2254.82s**。rebase 到 main 3ad4061 后重跑（84 个文件 + Host vitest）：**1070 passed / 2 failed / 2384.57s**，21/21 仍 passed；两例失败为 `test_effect_execution_sql.py::test_42_actual_bootstrap_and_publication_checksum`（同样断言迁移数=版本号，0074 预留空缺所致）与已知的 `test_relationship_context_v4.py::test_real_human_message_v4_bound_artifact[complete]`（2s guard deadline 时序，见第 2 步）。Host vitest 141 passed。

## 第 3 步：Role 权限上限与 scope 强制——阶段 A 已完成，阶段 B 待 L4 O1/O4

基线 `dispatch/integration-s3f` `90daa7b`（0074 NX-046、0075–0077 本线已定号）。新迁移临时 **0078_nx018_role_policy.sql**。按调度员分工，阶段 A 不改 `runtime_activation.py` / `backend.py`。

### 阶段 A（本次）

来源：候选 `docs/tmp/nx018-role-policy-candidate`（基线 e98d7c7+0064）的 DRAFT 前半（策略形状、管理、校验、绑定、签发），私有 alias `_before_role_policy_DRAFT` → `_before_role_policy_v0077`。对候选的改动：

1. **候选缺陷修复**：`nexloop_role_policy_current` / `_bind` 用 `to_jsonb(row)` 与 validate 结果比较，validate 固定 `timezone='UTC'` 而它们没有，`expires_at` 按会话时区（本机 +08）渲染，**正向路径永远不等**；两函数补 `set timezone='UTC'`。（候选测试未覆盖 `current`，所以没暴露。）
2. **未知 metadata-only ref fail closed（新）**：包裹 `authz.nexloop_role_run_bind`：Run 必须已有策略绑定，且 `RoleDefinition.ceiling_ref` / `ConsumerRoleLink.scope` 必须恰为已绑定的 `RoleExecutionCeiling` / `RoleAssignmentScope` 对象，否则 `Role execution policy mandatory` / `unknown Role ceiling|scope`。`role_policy_bind` 因此改为先写策略绑定再调用 `role_run_bind`。直接 `bind_role_run` 不再能绑定 Role（`role_activation` 的重放调用在策略已绑定时照常通过）。
3. **不叠加**：每个 Run 恰绑定一个 Role 的一对 ceiling/scope；Run.allowed_resources、Consumer、Goal、Step、预算须同时落在两者之内（Source ∩ Run ∩ Ceiling ∩ Scope），策略从不授权（`grants_authority:false`）。
4. 延后到阶段 B：DRAFT 中 `nexloop_role_policy_claims_current` 与 activation/intent/execution 三个调度期 wrapper（需要在 `runtime_activation.py` 的 `_signed` 与 effect 路径附带 `context_policy_envelope`，否则所有 Role Run 立即被拒），以及 v5 Context（pack/Artifact/Host 解析）。

Python：`role_policies.py`（候选原样）、`run_credentials.py` 拆出 `_prepare_run_credential`（行为不变，供原子签发复用）。夹具：`role_run_fixture` 改为真实创建 ceiling/scope 对象并经 `bind_policy_run` 绑定（Source 增加对两对象全字段的 READ），预算与 Run 命令同为 8/8/60/1.0 USD；`test_role_runs_first` 的正向例改为断言 metadata-only 选择 fail closed。

### 测试

- 候选移植（去掉手工 DRAFT 安装）：`test_role_policy_types` 11、`test_role_policy_governance` 5、`test_role_policy_binding` 9 → **25 passed / 21.56s**（首次即通过）。
- 新增 `tests/test_role_policy_enforcement.py`：metadata-only 直接绑定被拒；受治理 `RoleExecutionCeiling.edit` 收窄 action_resources / 停用后，已绑定策略 current 校验拒绝（断言 revision 2 已实际生效）；短 TTL 策略到期后拒绝。首次 3 failed（即上面的时区缺陷，正向 current 也失败），修复后与 binding 一起 **13 passed / 26.65s**。
- 新增 `test_message_read_derivation.py::test_run_credential_never_derives`：Run 凭据 basis 恒为 `configured`，形状与签名都正确的派生声明在 Run 身份下被 SQL 拒绝，**1 passed**。
- Role 相关全集（21 文件：`test_role_*`、`test_receipt_*`、派生、bootstrap、run_credentials）：**172 passed / 1000.60s**，含真实两 Pi Role Run。
- **首次广回归失败（保留）**：76 个其余相关文件 11 failed / 1035 passed / 1633.44s。除已知 v4 `[complete]` 外 10 例（claim_matching 7、candidate_merge 2、review_workbench 1，`rejected`≠`applied` 等）在基线 `90daa7b` 独立 worktree 上 24 passed，确认由本步引入。根因：候选 DRAFT 的 `create/edit_object_action` wrapper 只授权 `nexloop_api`，而当前 main 这两个函数实际 ACL 为 api + domain_worker + action_worker（在基线 bootstrap 上查询 `pg_proc.proacl` 确认），收窄后 Claim 匹配/合并 worker 的受治理写被拒。修复为与基线完全一致的 ACL，并新增 `test_policy_wrappers_preserve_prior_public_acl` 锁定；修复后三个文件 + 该测试 + bootstrap 33 passed / 53.43s。
- **最终（修复后同一工作树单条命令）**：Role 全集 21 文件 + 其余相关 76 文件共 97 个文件串行：**1218 passed / 1 failed / 2563.70s**，`.ci-results/l1-step3-final.xml`；唯一失败为已知 v4 `[complete]`（2s guard 时序）。Host vitest 141 passed。

### 阶段 A 未覆盖 / 阶段 B 待办

- 调度期（model/tool/submit/admit/finalize）对策略 EDIT/撤权/到期的实时复核与 effect_units 并发上限压力负例：需 `claims_current` + 三个 wrapper + `runtime_activation.py` / `effect_intents.py` / `effect_execution.py` 附带 policy envelope。等 L4 O1/O4 合入后做，届时先告知调度员。
- v5 Context 与契约：候选 v5 pack 新增 `role_policy` 段：`binding{run_id,tenant_id,world,ceiling_id,ceiling_revision,scope_id,scope_revision,budget,effect_units,expires_at}`、`ceiling`、`scope`、`ceiling_provenance`、`scope_provenance`、`grants_authority:false`。它属于 v5 Context，与阶段 B 一起实现；阶段 A **未修改 packages/contracts**。候选 Host 日期校验为 `Date.parse`+UTC 后缀，弱于 v3 的 calendar round-trip，阶段 B 需改为严格校验。
- `effect_units` 语义（每 Run 不同 submission 上限，策略行 FOR UPDATE 串行）需负责人确认。
- 派生未接入：v4 关系读取、Assessment 证据、NX-019 Claim 证据（仍走已配置 READ）。

### 给 L4 的设计点（单个只读事务内批量授权判定，对应 O1/O2）

实测（第 1、2 步诊断）：一次 guard 授权中 `AuthorizedObjectReader._authority` 约 8ms/次，v4 一次 authorize 调用数百次，Python 侧签名证明生成占 v4 authorize ~0.75s 中的 ~0.6s；每个 `_authority` 都独立 `pool.connection()` + 事务 + `_identity`。建议：

1. **批量解析入口**：`authorize_many(session, [(kind, resource_id, operation), ...])` 在**一个** repeatable-read 只读事务内完成身份快照与目录哈希核对一次、按 `(kind,key)` 去重加载 authority facts 一次（同一 Source 的 subject/membership/actor/authentication/application/subject_authority/revision 对所有资源相同，只有 resource_graph/grants/scope/controls/policies 按目标），再逐目标做决策。证明的 `facts` 与 `record_hash` 与现在逐个生成的完全相同，SQL 校验端无需改动。
2. **请求内去重**：同一请求（如 submit 的前后两次 guard、intent 构建、拒绝记录）用 contextvar 作用域复用已签名 envelope——本线已在 `role_runs.request_envelope_scope` 实现并用于 Role/formal/v4/catalog envelope，O1 可泛化到 `_authority` 级别。**不跨请求**：本线试过 3s 跨请求复用，会绕过"每次 guard 重新生成证明"语义（`test_role_tail` 锁等待后最终期限用例失败），已撤回。
3. **期限**：批量结果的 `expires_at` 仍按每个决策 `min(decision.expires_at, now+25s)`，批量事务时间点作为 `now`，不延长任何证明。
4. **锁**：本线已把 backend 全局锁改为请求共享 / 关闭独占（`4b48c3b`），与 O4 方向一致；O4 若进一步拆分，请保留"关闭等待所有 in-flight commit/fsync"的保证与线程本地重入。

## LifecycleLock 连接池饥饿修复（调度员批准，单独提交，基于 main `ae1d308`）

### 问题与根因

我在 `4b48c3b` 把 backend 全局 RLock 改成"请求共享 / 关闭独占"后，请求并发不再受限。每个请求持有一个外层事务连接，签名证明/授权判定又从同一池嵌套取连接；默认 `pool_max_size=4`（`NEX_EIOS_DB_POOL_MAX`），≥4 个并发请求时全部持有外层连接、等嵌套连接 → `PoolTimeout(30s)` + idle-in-transaction 超时。在未修复的 main 上，`test_eight_concurrent_requests_complete_on_pool_of_four` 同一检出复现失败（全部 `EffectIntentUnavailable`）。

### 修复

- `LifecycleLock(capacity=max(1, pool_max_size//2), wait_seconds=10.0)`：请求最外层进入时占一个名额，最外层离开时释放；同线程重入不占名额；关闭（`exclusive()`）仍等待所有在途请求。
- 等待名额有界：超过 `wait_seconds` 抛 `BackendBusy('backend request capacity unavailable')`（可重试、明确），不挂起。等待发生在请求构建任何证明、执行任何 SQL 之前，因此等待时间不会延长证明有效期；之后 SQL 的期限检查照常按 `clock_timestamp()` 执行。
- 线程本地 `request_depth()`，供插件判断"是否处于 backend 请求内"。
- **公开入口（供 L2 审核决定 HTTP 使用）**：

  ```python
  Backend.run_request(operation: Callable[[ConnectionPool, BackendSigner], T]) -> T
  ```

  在共享生命周期持有（受名额限制、关闭会等待）+ 一个 O1 `authority_request_scope()` 内执行 `operation(pool, signer)`；`operation` 不得在调用之外保留 pool/signer；名额等待超时抛 `BackendBusy`，关闭后抛 `BackendClosed`。

### "外层 + 至多 1 个嵌套连接"的验证（不是假设）

`tests/support/pool_depth_plugin.py`（`-p support.pool_depth_plugin`）：包装 `psycopg_pool.ConnectionPool.getconn/putconn`，对处于 backend 请求内（`request_depth()>0`）的线程逐线程计数同时持有的连接；**第 3 个立即抛 `PoolDepthExceeded` 使测试失败**，并按调用栈汇总深度 2 的嵌套路径（写入 `NEXLOOP_POOL_DEPTH_REPORT`）。广回归在插件开启下运行，结果见下。

已观测的嵌套取连接路径（外层请求事务内，再经 `authorization.open_unit_of_work` 取第二个连接；按次数排序，节选）：

1. `role_runs._role_envelope_for_run → role_binding_envelope → service_offerings._read_envelope → object_reads._authority → authorization.resolve_authority → open_unit_of_work`
2. `role_runs._formal_reads_for_role → _read_envelope → _authority → … → open_unit_of_work`
3. `service_offerings.catalog_envelope_from_hint → _catalog_envelope → _read_envelope → _authority → … → open_unit_of_work`
4. `backend._invoke → effect_contexts.bind/configure_control → _execute → register/_claim → _proof → open_unit_of_work`

插件开启的广回归（121 个测试文件串行，含 Role/receipt/derivation/context/relationship/effect/delivery/outbound/claim/review/http/queue 等）：**单个请求同时持有的连接数最大 2，从未出现第 3 个**；共 165 条不同的深度 2 调用栈。按嵌套点聚合（次数 | 栈尾三层）：

| 次数 | 嵌套点 |
|---|---|
| 8553 | `service_offerings.py:_read_envelope > object_reads.py:_authority > authorization.py:resolve_authority` |
| 3402 | `effect_contexts.py:_execute > effect_contexts.py:register > effect_contexts.py:_proof` |
| 1084 | `effect_contexts.py:_execute > effect_contexts.py:_claim > effect_contexts.py:_proof` |
| 963 | `conversation_messages.py:_signed > conversation_messages.py:_proof > authorization.py:open_unit_of_work` |
| 944 | `relationship_context.py:envelopes > object_reads.py:_authority > authorization.py:resolve_authority` |
| 900 | `assessment_actions.py:get > object_reads.py:_authority > authorization.py:resolve_authority` |
| 439 | `effect_execution.py:_execute > effect_execution.py:_signed > action_definitions.py:get` |
| 430 | `role_runs.py:role_envelope_for_run > role_runs.py:scoped_envelope > role_runs.py:<lambda>` |
| 366 | `effect_execution.py:_call > effect_execution.py:_execute > effect_execution.py:_signed` |
| 336 | `action_definitions.py:get > action_definitions.py:get_with_schemas > authorization.py:resolve_authority` |
| 278 | `effect_execution.py:_signed > effect_execution.py:_proof > authorization.py:resolve_authority` |
| 257 | `action_definitions.py:get_with_schemas > authorization.py:resolve_authority > authorization.py:open_unit_of_work` |
| 240 | `runtime_activation.py:effect_tool > runtime_activation.py:_effect_tool > effect_intents.py:_execute_in_transaction` |
| 238 | `backend.py:_invoke_browser > conversation_messages.py:create_conversation > conversation_messages.py:_call` |

即：外层请求事务（`_invoke`/`_invoke_browser`/ports 的 `_execute`/`_call`）内，签名证明与授权判定（`_read_envelope`/`_authority`/`_proof`/`action_definitions.get`/`resolve_authority`）经 `open_unit_of_work` 取第二个连接；没有在嵌套连接内再取连接的路径。

### 测试

`tests/test_backend_lifecycle_capacity.py` 7 passed / 52.35s（插件开启）；同一"8 并发"用例在未修复 main 上失败（对照）。广回归（插件开启）**1349 passed / 0 failed / 2696.28s**，`.ci-results/l1-lockfix.xml`，Host vitest 141 passed（含 v4 `[complete]` 通过）：容量限制且重入不占名额、超时明确报错 <2s、关闭等待在途请求、容量随池大小（4 → 2）、**8 个并发请求（4× `runtime_effect_tool` 同意图 submit + 4× authorize）在 pool_max_size=4 下全部完成**、等待名额期间 Run 截止到达 → 获得名额后被拒且 0 意图、名额等待超时以 `BackendBusy` 返回、`run_request` 在请求深度 1 下执行。

### 能否解释 sice 上 B 类（retry_wait / runtime_transport_unavailable）失败

判断：**大概率不能**。

1. `REGRESSIONS-main-f58f3ee.md` 的 7 例 sice-only 发生在 f58f3ee，那时还没有 LifecycleLock（全局 RLock 串行化，不存在并发取池），不可能是饥饿。
2. 之后基线中的 B 类：在本工作树把容量强制关掉（等同修复前 main），对 `test_context_host_export`、`test_outbound_messages_pg::…agent_reply…`、`test_message_relay::…cli_same_message…`、`test_runtime_effect_tools::…rebuilt…`、`test_role_pi_effect_checkpoint` 测量：**单个 backend 上并发请求峰值 1–2，最长 getconn 等待 ≤13ms**。饥饿需要 ≥`pool_max_size`（4）个请求同时持有外层连接并等待嵌套连接；峰值 2 时每个请求至多 2 个连接，刚好占满但不会死锁。
3. 这些失败与 2 s 工具 deadline 的时延画像一致（每次 guard 授权 0.5–1s，sice 更慢）。
4. 建议在 sice 上用同一探针（`concurrency_probe` 插件，报告并发峰值与 getconn 等待）跑一次 B 类用例确认；若出现峰值 ≥4 或 getconn 长等待，则饥饿是成因之一。

### 后续（本次不做）

- 嵌套授权证明复用外层请求的连接（把当前事务连接传入 `_authority`/`open_unit_of_work`，或 O2 在外层事务内批量判定），从根本上去掉"每请求两个连接"，届时容量可回到 `pool_max_size`。
- 同一 Run 并发 guard 在 0039 `nexloop-runtime-execution:<run>` advisory 锁与行锁之间的锁序循环（PostgreSQL 检测并中止一方，结果正确、可重试）：统一锁序。
