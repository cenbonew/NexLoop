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

## 3. 授权决定（调度员已确认方案 B，见 §7）
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

## 7. 调度员确认后的调整（2026-10-09）

1. **方案 B 已确认**（ADR-019/M12、ADR-020 §3）：发布事务不写任何授权事实；服务主体对后继 Action 的授权由调度员经 `deploy/authorization/service-grants.v1.json` 授予，人类主体的授权需负责人确认。AT-067 按两步记：
   - 第一步 approve：发布。
   - 第二步：授权到位后回流，才应用等待中的 Claim。
2. **等待期间状态明确**：
   - approve 后依赖 Claim **保持 `awaiting_definition`**，SQL 不再在发布时复位。
   - 回流重匹配允许处理 `awaiting_definition` 的 Claim；只有真正应用时才置为 resolved。
   - NX-020 `ClaimMatcher.apply` 遇到授权拒绝时，提案改为非终态 `conflict`（reason `authority_denied_awaiting_grant`），不再直接 rejected，授权到位后可以幂等恢复。
   - 回流报告记 `status=waiting, reason=awaiting_grants`。
   - 新增 `authz.nexloop_read_review_reflow_status` 与 `GET /api/v1/review/awaiting`；工作台显示“已决定，依赖 Claim 尚未应用”列表，其中“等待授权”单独标出。
3. **只读检查**：
   ```bash
   python -m nexloop_eios.review_actions uncovered-actions --manifest deploy/authorization/service-grants.v1.json --tenant <tenant> --database-url-file <configurator-dsn-file>
   ```
   - 列出审核发布产生、但服务授权清单尚未覆盖的 Action 版本，附前一版本在清单中的授权作为参考。存在未覆盖项时 exit 1。
   - 底层函数 `control.nexloop_review_published_actions(tenant)` 只授予 `nexloop_configurator`。
4. **不再使用 `backend._lock`**：
   - 队列与详情走公开的 `Backend.authenticate_browser_reviewer → ReviewServices.review_queue/review_candidate`（共享生命周期 + `authority_request_scope`）。
   - 决定和等待列表先调用公开的 `authenticate_browser_reviewer`（检查 backend 已打开、重新认证人类会话），再在 `authority_request_scope()` 内用只读的 `_pool/_signer` 执行。
   - 依赖的 backend 符号：`Backend.authenticate_browser_reviewer`、`ReviewServices.review_queue`、`ReviewServices.review_candidate`、`Backend._pool`、`Backend._signer`。
   - 限制：Backend 目前没有公开的决定入口，所以决定的提交期间不持有共享生命周期锁，关闭时可能打断一个正在提交的事务。该事务会整体回滚，决定 id 幂等，重试是安全的。建议 L1 修复 LifecycleLock 后提供公开入口（例如 `Backend.review_decision(session, **args)`），合并时对齐。
5. **测试（调整后）**：
   - `test_review_decisions_pg.py`：approve 后 Claim 仍为 awaiting_definition；回流 waiting/awaiting_grants；工作台等待列表显示；`uncovered-actions` 列出 `Consumer.edit:2`，清单补上后通过；授权后回流 done，等待列表清空。
   - `test_review_http.py`：新增 HTTP approve 与 `/awaiting` 用例。
   - 前端：新增等待列表用例。
   - 目标集 **154 passed，207.65s**，首次即通过；junit sha256 `63849f8c4a0c518290f0d2a0ac08b95367bd82dca13ca63e5b3567c955589f51`。
   - Web 24 passed，junit sha256 `f896ce13182d07a8683dff9b40615eb6ba6a16e48774b5530366d89aabdf1ddd`。
6. **设计建议（本次不实现）**：在可信配置中增加声明式规则“沿用某 Action 最新版本的授权”，例如在清单里为 `Consumer.edit` 声明 `follow_latest_version: true`，由 `service_grants --apply` 在审核发布产生新版本后自动生成等价授权；变更仍经可信配置路径和审计，可以减少每次发布后人工更新清单。逐对象的新属性授权仍建议走“受治理派生”（与 NX-047 同类）。

## 8. 收口（2026-10-10，分支 nx044-close，临时迁移 0099_nx044_type_actions）

### 8.1 新类型批准同时发布受治理 Action
- 批准新类型时，与 Schema v1 在同一事务中发布 `<Type>.create:1` 和 `<Type>.edit:1`。
- 两者复用租户已发布的 `ontology.object.create` / `ontology.object.edit` 能力快照（按 resource_id 取第一个），没有新增能力、scope 或风险。
- 审核端由 EIOS 模型构造这两个 Action：低风险、无需审批、无策略，幂等键为 `request_id`，目标系统 `postgres`，`created_by='nexloop-review-publication'`。
- SQL（`ontology.nexloop_review_publication_gates`，新类型分支）核对以下内容，任何偏差都会导致 `publication_failed` 并附原因，候选保持 `pending_review`：
  - 恰好 create、edit 两个 Action；
  - 每个 Action 的定义除 `contract_digest`、`created_at` 外与规范形状完全相等；
  - 能力快照逐字等于租户现有的那一份；
  - 两个 Action 绑定的是同一个 Schema 摘要，且对应资源此前不存在。
- 租户没有对应能力快照时，门槛失败原因为 `type_action_capability_unavailable:<能力名>`。
- 发布仍不写任何授权事实，0082 的指纹门槛不变。
- 新函数的 search_path 一律为 `pg_catalog, pg_temp`（`scripts/check_definer_search_path.py` 结果为 0 个问题）。

### 8.2 回流：依赖 Claim 交回四层匹配链
- `ReviewReflowWorker` 处理已发布的新类型时，先用 `index_object_type` 重建定义召回索引，再调用 `nexloop_review_reflow` 的 `release_type`：
  - 首次：仍在 `awaiting_definition` 的依赖 Claim 改为 `unresolved`。之后若匹配器把它们留在 `needs_resolution`，只有在租户授权指纹变化后（即可信配置确实授予了新权限）才再次释放。
  - 每条被释放的 Claim 在 `ontology.nexloop_claim_rematch` 中把代次加一，并在 claim-match feed（0093）中登记所属会话。
  - 回流本身不应用任何值。
- 匹配 worker 对已释放的 Claim 使用 `nx020-matcher/1;rematch:N` 版本重新运行全部四层，不会重放上一次的 no_match。没有任何层被跳过：只有名称的实例仍进审核，新属性仍进审核。新类型没有主键，因此没有强标识路径。
- 决定的回流状态：
  - 仍有 `unresolved` 的 Claim：`waiting`，原因 `awaiting_matching`；
  - 仍有 `needs_resolution` 的 Claim：`waiting`，原因 `awaiting_grants`；
  - 否则：`done`。后续的应用或新候选属于各自的流程。

### 8.3 部署时需要在可信配置中增加的条目（发布本身不授权）
对每个经审核批准的新类型 `<Type>`，若要由匹配服务自动处理，需要在 `deploy/authorization/service-grants.v1.json`（新的 manifest_version）中加入：
- `claim_matcher`：
  - `eios:object_type:<Type>` READ：召回能看到这个类型；
  - `eios:action:<Type>.create:1`、`eios:action:<Type>.edit:1` EXECUTE；
  - 需要后续版本时，对 edit 声明 `follow_latest_version`；
  - 属性写入可加一条 `property_access_rules`（`type_name=<Type>`），并在负责人限制文件中为该类型记录受限组决定（可以为空列表）。
- 匹配进程的 `--match-config-file` 中加入 `"<Type>": ["<Type>.edit", 1]`（edit_actions）与 `"<Type>": ["<Type>.create", 1]`（create_actions）。
- 授权就绪后，下一次回流会因授权指纹变化而再次释放依赖 Claim。
- 人类主体的授权仍需负责人确认。

### 8.4 局限（需决定）
在现行规则下，新类型 Claim 的链路最远走到“仅名称的实例进审核”：
- 类型 v1 没有主键，兼容性规则禁止之后再加主键，所以没有强标识路径；
- NX-044 不支持批准 `object_instance` 候选（`kind_not_publishable`）。

因此新类型的 Claim 目前不会被自动应用。若要做到“已应用”，需要另立任务：支持批准实例候选，并由回流经 `<Type>.create` 受治理地创建实例。或者改动候选契约，让类型候选带上首个属性或主键（已被否决）。

### 8.5 测试（合成数据；`LANG=en_US.UTF-8 PYTHONPATH=packages/eios-core/src:tests uv run --frozen pytest -q …`）
- `tests/test_review_type_actions_pg.py`（10 项）：
  - `test_new_type_publishes_schema_and_canonical_create_edit_actions_without_authority`
  - `test_new_type_without_tenant_create_capability_stays_pending_with_reason`
  - `test_non_canonical_new_type_actions_are_refused_by_sql`（6 种篡改）
  - `test_rejected_type_keeps_dependent_claims_as_evidence_only`（不进 feed、没有代次）
  - `test_approved_type_hands_claims_back_to_the_full_matching_chain`（AT-067 新类型链路、授权未就绪负例、授权就绪后再次释放）
- `tests/test_review_decisions_pg.py::test_agent_and_run_bound_credentials_cannot_decide_and_cause_no_side_effects`
- 回归：`test_bootstrap test_db_boundary test_claim_store_pg test_review_type_actions_pg test_review_decisions_pg test_review_http test_review_workbench_pg test_candidate_merge_pg test_claim_matching_pg test_work_feeds_pg test_property_grant_derivation_pg test_grants_follow_latest_pg test_recall_pg`：首次 106 passed / 1 failed。失败的是 AT-064 迁移命名边界，0099 引用了 Claim 表；已在边界测试中把 NX-044 回流登记为受准许的消费者，之后通过。

### 8.6 设计限制（单列，待负责人另议）
**新类型永远没有强标识路径。** 新类型 v1 按候选契约没有属性，也没有主键；EIOS 的 `assert_object_compatible` 又禁止之后修改主键（只允许增量、主键不变）。因此，新类型的实例永远不能通过强标识自动创建或定位。仅有名称的实例必须经审核（ADR-019 决定 3），实例候选的审批由 NX-050 实现。是否放宽这一点（例如允许以兼容方式首次设置主键）需另行提交负责人决定；本次不改动。

**本次交付终点（如实）：** AT-067 的新类型链路走到“仅名称的实例进入审核”为止。依赖 Claim 不会被写成“已应用”。“已应用”由 NX-050（object_instance 候选审批与回流创建）完成。
