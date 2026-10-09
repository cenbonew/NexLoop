# NX-044 人类审核 Action 与人工批准的 Schema 发布（开发线 L2，先行开发）

状态：实现与定向测试完成，**未标 done**。临时迁移 `0082_nx044_review_actions.sql`。差距检查见 `NX-044-gap.md`。

## 1. 交付

| 文件 | 内容 |
|---|---|
| `0082_nx044_review_actions.sql` | `ontology.nexloop_review_decisions`（review-decision 持久化与审计）；`authz.nexloop_review_decide`（人类审核 definer）；`authz.nexloop_read_review_publication_basis`；`ontology.nexloop_review_publication_gates`（门槛）；`authz.nexloop_tenant_authority_fingerprint`；`authz.nexloop_review_reflow`（服务回流记账） |
| `nexloop_eios/review_actions.py` | `ReviewDecisionPort`（只接受人类会话）、`build_publication`（EIOS 模型 + `assert_object_compatible` + `schema_contract_digest`）、`ReviewReflowWorker`、`workbench_ports`（http_api 装配） |
| `nexloop_eios/review_http.py`、`http_api.py` | `POST /api/v1/review/candidates/{id}/decisions`：同源、CSRF、Idempotency-Key、严格请求体；`decisions.enabled` 由装配的端口是否真正支持决定来决定 |
| `nexloop_eios/candidate_merge.py` | 审核读取签名支持指定协议；回流重匹配包含 needs_resolution，便于在授权就绪后恢复 |
| `apps/web/src/review-api.ts`、`Review.tsx` | 三个决定接到真实 Action：理由必填并写入审计；merge_into 只提供同类已有定义作为目标；每种结果都有明确文案 |
| 测试 | `tests/test_review_decisions_pg.py`（6）、`tests/test_review_http.py`（4，其中决定 1）、`apps/web/test/review.test.ts`（7） |
| 夹具 | `tests/test_claim_matching_pg.py` 的 `env` 增加可选的间接租户参数，默认值不变 |

## 2. 规则
- **决定者**：
  - 只接受浏览器人类会话：摘要在 `authz.nexloop_browser_token_realms` 中，且身份快照 binding 的 `subject_kind='human'`。
  - 必须对 `eios:action:ontology.schema.review:1` 持有当前 EXECUTE（`nexloop_assert_action_authority`，浏览器分支会重新核对授权事实）。
  - service 与 agent（模型）凭据在 Python（`_human`）和 SQL 两层都会被拒绝；`reviewer_ref` 由服务端写为 `human:<principal>`。
- **幂等与并发**：
  - `decision_id = uuid5(tenant:principal:Idempotency-Key)`；同一 decision_id 重放时返回首次结果。
  - 候选的 `expected_revision` 不一致返回 409；reviewer 或 decision 不同而 id 相同，同样拒绝。
- **审计**：
  - 每个决定写入一行：`record` 为 review-decision 契约形状；`authority` 为授权事实摘要；另含发布结果、回流状态与报告。
  - 候选状态转移另写 `nexloop_candidate_events`，actor_kind 为 `human`。
- **reject**：转移为 rejected，由 0074/0078 触发器写入冷却期（单一时刻）并把 Claim 置为 rejected_definition。
- **merge_into**：
  - 目标必须是最新定义中**同类同范围**的现存定义：属性只能指向同一 owner 类型的属性；词表值只能指向同一属性的词表值。
  - 实例候选拒绝，需要走身份解析而不是别名。
  - 执行 `merge_effects`（写别名、复位 Claim）后转移为 merged，回流状态记为 pending。
- **approve**：
  1. 后端按“发布依据”构建下一版本 Schema 与后继 Action：EIOS 模型校验、`assert_object_compatible`、摘要计算。构建失败的原因作为门槛失败原因记录。
  2. SQL 门槛复核（docs/08 §5）：
     - 仅 real world 的候选可以发布；
     - owner 类型或属性存在，名称合法且不重复；
     - 新属性只能是非封闭的可选属性；词表值只能追加到封闭 enum，且类型匹配；
     - 新定义必须**恰好**等于“旧定义加审核过的那一项”；
     - 后继 Action 必须恰好覆盖绑定旧版本的全部有效 Action，并且只改版本号、对象类型引用、摘要。
  3. 通过后在**同一事务**中完成：插入新 Schema 版本、插入后继 Action 版本、把旧版本对象的 `schema_version` 升级到新版本（仅增量，属性不动）、转移为 published、调用 `publish_effects` 复位 Claim。
  4. 发布前后会比对本租户授权事实的指纹，不一致即整体回滚。**发布不写任何授权。**
  5. 门槛不通过：候选保持 pending_review，记录 `status_reason` 和一条 `publication_failed` 决定（含 gate_failures），不自动重试。
- **回流**（服务凭据，`ReviewReflowWorker.run_pending`）：
  - merged：执行 `reflow_merged`。
  - published：执行 `reflow_published`。
  - 全部 Claim 都已应用时记为 done；否则记为 waiting，并在报告中给出原因（例如后继 Action 或新属性未授权），之后可再次执行，已建立的提案会幂等地恢复应用。

## 3. 授权决定（已向调度员提问，答复前按方案 B）
发布后要应用等待中的 Claim，需要对新 resource 的授权：后继 Action（`eios:action:Consumer.edit:2`），以及新属性的定义级 READ 和逐对象的 READ/EDIT。按 ADR-020 §3，这些授权由可信配置授予；发布本身不改变任何授权，这一点由门槛保证。
- 测试 `test_human_approve_publishes_additively_and_waiting_claims_apply_after_grant` 先断言：未授权时回流状态为 waiting，属性没有写入。
- 之后用可信配置同等的授权写入（测试夹具）模拟授权，再次回流：Claim 经受治理的 `Consumer.edit:2` 写入，`applied_claim_count=1`，回流状态 done。
- 对方案 A（复制 :1 上的授权到 :2）的补充：新属性的逐对象 EDIT 是全新的 resource，复制 :1 的授权并不能覆盖它，所以方案 A 本身也不完整。

## 4. 命令与结果

环境：`source ~/.nvm/nvm.sh && nvm use 24`、`LANG=en_US.UTF-8`、`PYTHONPATH=packages/eios-core/src:tests`。

1. `tests/test_bootstrap.py tests/test_db_boundary.py`：6 passed。
2. `tests/test_review_decisions_pg.py`：
   - 首次 3 failed / 3 passed：`ActionDefinition.model_validate(dict)` 在 Python 模式下要求枚举实例，改用 `model_validate_json`。这一次失败同时验证了失败路径：发布被拒、候选保持 pending_review、原因被记录。
   - 第二次 3 failed：SQL 中 `->` 与 `-` 的优先级缺括号；`text[] || '字面量'` 被当作数组字面量（改为 `array_append`）。
   - 第三次 6 passed。
3. `tests/test_review_http.py`：
   - 首次 1 failed：NX-046 的旧断言写的是“决定未启用”，按本任务更新。
   - 再次 1 failed：夹具直接插入的 Claim 状态是 unresolved，应为 awaiting_definition（触发器只改后者，行为正确）。
   - 修正后 4 passed。
4. Web：
   - `tsc --noEmit` 通过。
   - vitest 首次 1 failed：断言没有考虑 `selected=""` 属性，属于测试写法问题。
   - 修正后 23 passed，junit sha256 `1c35dc0fb7de82770d653379b26670e85f96c6fffa0987bf252cfdcd15803a6f`；`pnpm build` 成功。
5. 后端目标集（review_decisions、review_http、review_workbench、merge_configuration、candidate_merge、claim_matching、recall、claim_store、web_chat_http、browser_http、http_api、backend、object_edits、action_definitions、bootstrap、db_boundary、doctor、core_sandbox、compose_bootstrap、wheel_install）：**153 passed，204.51s**，首次即通过；junit sha256 `ea70c121ece0d25713428e62c8dc200b4375657b2e20d921a8641db5550c6b29`。
6. 本线没有遗留进程；运行期间看到的 PG/pytest 属于 L1。

## 5. 验收结论（建议）

| AT | 建议 | 依据 |
|---|---|---|
| AT-067 审核批准发布 | **passed（测试证据），“等待 Claim 全部应用”依赖可信配置授权这一步** | 见下方说明 1 |
| AT-068 审核拒绝 | **passed（测试证据）** | 见下方说明 2 |
| AT-070 最小审核工作台 | **passed（测试证据）** | 见下方说明 3 |
| AT-069 发布回流 | 合并路径 passed；发布路径在授权就绪后 passed | `test_human_merge_into_reflows_and_applies`；发布路径同 AT-067 的回流部分；回流后不再产生重复候选的部分见 NX-045 |

1. AT-067：
   - 发布成功：`test_human_approve_publishes_additively_and_waiting_claims_apply_after_grant`、`test_human_approve_vocabulary_value_extends_closed_enum`。
   - 门槛失败：`test_gate_failure_keeps_candidate_pending_with_reason_and_no_schema_change`。
   - 被拒且无副作用：`test_service_agent_and_ungranted_principals_cannot_decide`（service 主体、Python 层被绕过的伪造调用、无审核权限的人类）。
   - 模型主体：agent 凭据与 service 凭据一样不属于浏览器 realm，被同一检查拒绝；没有单独做 agent invocation 的夹具测试。
2. AT-068：`test_human_reject_records_decision_cooldown_and_replays` 与 HTTP 上的 reject 测试——由人类受治理决定触发，有审计；Claim 置为 rejected_definition；冷却期满足单一时刻约束；支持重放，过期 revision 返回 409。
3. AT-070：读取部分见 NX-046；决定部分由 `test_reviewer_decides_through_http_with_csrf_idempotency_and_cas` 覆盖（三种决定可用，每个决定有审计记录）；前端测试覆盖理由必填、同类目标、各种结果与错误文案。

## 6. 局限与需要决定的事
1. **授权方案**：方案 B 等待调度员确认。生产环境中，approve 后需要由可信配置授予后继 Action 与新属性（含逐对象授权），然后执行回流。逐对象属性授权最好走“受治理派生”，与 NX-047 的 Message READ 派生同类，这需要单独的任务。
2. **匹配器配置**：`MatchConfiguration` 中 Action 的版本需要随发布更新到后继版本。目前由部署方（测试中为 worker 构造参数）设置，还没有自动跟随最新版本。
3. **类型发布**：新类型只发布 v1 Schema，没有生成 create/edit Action，依赖 Claim 会复位但无法应用，需要另行发布 Action。
4. **对象升级**：发布时在单个事务内升级全部旧版本对象的 `schema_version`，数据量大时需要分批或在线迁移方案。
5. **装配**：`workbench_ports` 读取了 Backend 的私有属性 `_pool`、`_signer`、`_lock`，以避免修改禁区内的 backend.py。L1 阶段 B 完成后，建议把它收回成 Backend 的正式方法。
