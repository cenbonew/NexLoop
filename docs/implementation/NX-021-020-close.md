# NX-021 / NX-020 收口：实例索引回流、Claim 后台匹配与可部署进程入口（开发线 L2）

状态：实现与定向测试完成，**未标 done**；任务与验收状态由调度员判定。临时迁移 `0092_nx020_nx021_work_feeds.sql`（调度员派工要求从 0093 起，但 `test_bootstrap` 要求 catalog 版本连续，main 最高为 0091，因此本地只能用 0092；合并时统一重编号）。revision 字符串只出现在 `catalog.json` 与 `versions.lock.json`。

依据：ADR-019 §3.2、§3.3、§3.6；ADR-020 §3（服务主体授权只经可信配置）；ADR-022（功能收口）。

## 1. 缺口与处理

| 缺口 | 处理 |
|---|---|
| NX-021：生产代码中 `RecallIndexer.index_instance` / `remove_source` 没有调用方，受治理的创建、编辑、删除都不会更新实例索引 | `ontology.objects` 的 insert/update/delete 触发器，在写入的同一事务里登记 `recall-instance` 变更；`RecallInstanceIndexWorker` 按 SQL 从存储对象重新派生索引行，对象不存在时移除 |
| NX-020：新提取的 Claim 没有队列或 worker 触发 `match_claim` | `ontology.nexloop_claims` 的 insert 触发器（即提取运行记录 Claim 的那个事务）登记 `claim-match`；`ClaimMatchWorker` 调 `ClaimMatcher.process_conversation`（逐条 `match_claim` + 受治理自动应用），再调 `CandidateGluer.process_staged` |
| 提取调度器、提取 worker、匹配器（含粘合）、实例 indexer 没有可部署入口 | `nexloop_eios.background_services` 四个入口 + pyproject scripts + container 入口 job + compose（opt-in profile `background`） |

## 2. 设计

### 2.1 变更 feed（迁移 0092）
- `runtime.nexloop_work_feed(tenant_id, world, feed, item_key, payload, change_seq, …)`：每个 (租户, world, feed, 条目) 一行。新的变更把 `change_seq` 加一并重新置为 pending（死信条目也会因新变更复活）。表由 `nexloop_owner` 拥有，强制 RLS，应用角色没有任何表权限。
- 触发器函数 `authz.nexloop_work_feed_on_object()` / `_on_claim()` 调用 `authz.nexloop_work_feed_touch()`。登记与业务写入在**同一事务**内（先持久化，后 ACK）。touch 在插入时切换到被写行的租户上下文，结束后恢复调用方原来的 `eios.tenant_id`。
- 端口 `authz.nexloop_work_feed(digest, world, text, signature, payload)` 只授予 `nexloop_domain_worker`，要求同时满足：
  - HMAC 签名有效（协议 `nexloop-work-feed-v1`）；
  - 调用方是 service 凭据，不是 browser，也不是 Run；
  - 持有该 feed 的技术 Action 授权 `eios:action:NexLoop.feed.<feed>:1`，在调用前后各断言一次。
- 支持四个动作：
  - `claim`：`for update skip locked`，租约以当时的 `change_seq` 作栅栏；
  - `complete`：若 `change_seq` 未变则删除该行；若处理期间来了新变更，返回 `changed`，该行保持 pending，下一轮从头重做；
  - `retry`：指数退避，达到 `max_attempts` 后进入死信；
  - `backlog`：返回 pending 数、最久等待秒数和死信列表。
  - 租约丢失或已被替换时一律返回 `lease_lost`，不会完成或重试别人的工作。
- 存量数据：迁移把已有对象登记为 upsert，把含 `resolution_state='unresolved'` Claim 的 Conversation 登记为待匹配。

### 2.2 实例索引 worker（`recall_index_worker.py`）
- 只为配置的实例类型建索引（`types`：类型名 → instance spec 或 null，null 表示用定义默认的 `title_property`/`primary_key`）。其它类型的变更直接确认，不建索引。
- 处理规则：
  - `delete`：移除该对象的索引源；
  - upsert 时对象或定义已不存在（`recall object unavailable` / `recall definition unavailable`）：同样移除；
  - 索引期间对象版本被改（40001）：立即 retry，由新变更重新驱动。
- 撤权**不需要重建索引**：召回仍按读者自己当前的 EIOS READ 预过滤和校验（NX-021 §2.4）。测试同时验证了这一点。

### 2.3 匹配 worker（`claim_match_worker.py`）
- 匹配按 (Claim, matcher_version) 幂等，所以重复登记或重放的 Conversation 不会产生重复的提案、候选或写入。
- 写入走既有的提案状态机：版本冲突时重新评估，缺授权时进入 `awaiting_grant`。
- 失败分类：provider 不可用、输出被拒、授权拒绝（`PermissionError` / `AuthorizationFactDenied` / `AuthorizationUnavailable`）、其它。所有失败都退避重试，最终进入死信，没有任何写入。

### 2.4 进程入口（`background_services.py`）

| 入口 | DB 角色 | 额外私有文件 |
|---|---|---|
| `nexloop-claim-extraction-scheduler` | nexloop_api | — |
| `nexloop-claim-extraction-worker` | nexloop_domain_worker | `--model-env-file` |
| `nexloop-claim-matcher` | nexloop_domain_worker | `--model-env-file`、`--match-config-file`、可选 `--embedding-env-file` |
| `nexloop-recall-indexer` | nexloop_domain_worker | `--types-file`、可选 `--embedding-env-file` |

- 不做迁移。DSN、签名键、服务凭据、模型和 embedding 配置都只来自显式指定的私有文件；读取 profile 时传入空环境，不会回退到进程环境变量。
- 每个 tick 重新读取凭据并重新认证，撤权即时生效。
- 启动时校验 DB 角色，角色不符时退出 1，并打印固定文案。
- 参数错误时退出 2，打印固定文案，不回显参数。
- `--once` 只跑一个 tick，输出只含计数。
- 模型 provider 延迟构造。profile 缺失或为 test 时，任务以 provider 错误退避重试，不会编造输出。
- 不配置 embedding 时是显式的 FTS + trgm 模式。
- 容器：`container_entrypoint` 新增四个 job，私有文件位于 `/private/background/<job>/`。compose 新增四个 profile 为 `background` 的服务：只读卷 `background_config`、非 root、`cap_drop: ALL`、无端口。默认栈不会启动它们。

### 2.5 服务授权（`deploy/authorization/service-grants.v1.json`，manifest_version 4）
- 新主体 `recall_indexer`（nexloop_domain_worker），只持有 `eios:action:NexLoop.feed.recall-instance:1` EXECUTE。它不需要业务 READ/EDIT。
- `claim_matcher` 新增 `eios:action:NexLoop.feed.claim-match:1` EXECUTE。
- 提取调度器和提取 worker 沿用已有授权（`claim.extract`、`NexLoop.queue.claim-extraction`）。

## 3. 范围说明

- **memory_items / search_chunks**：两者是 docs/03 数据模型里的记忆与通用检索分块。ADR-019 §3.2 的召回范围只有三项：类型/属性定义、已有对象实例、该消费者已有属性值。仓库里也没有这两张表。因此不在本任务范围，未实现。
- **AT-021 的 409 层**：仓库没有对象编辑 HTTP 接口（`http_api` 只有健康检查和 Artifact；`web_chat_http` / `review_http` 的 409 对应各自资源）。AT-021 在受治理编辑层的含义是：两个基于同一 revision 的更新，一个成功，另一个得到 40001 并重新评估。这由 `test_claim_matching_pg.py::test_same_revision_conflict_one_wins_other_reassessed` 覆盖。HTTP 409 映射属于未来的对象编辑 API，本次未新增 HTTP 接口。
- **部署授权缺口（需要决定，未在本线修改）**：只靠清单授权时，`claim_matcher` 与提取 worker 读不到具体的 Conversation/Message。
  - 原因：`ClaimMatcher` 读 Claim 时需要 `eios:object:Conversation/<id>` READ，提取 worker 需要 Message READ。消息读派生（0077/0086）要求同一主体对该 Consumer 持有**已配置的** READ，而 `claim_matcher` 对 Consumer 只有 0084 的类型派生，服务清单也不能表达 `message_read_rule`。
  - 现状：`AuthorizedObjectReader`、`object_reads.py` 和授权 SQL 包装层按派工要求不改（L1 O5b 正在进行），所以本次端到端测试使用逐对象的测试授权。
  - 需要后续决定：派生链是否允许以派生的 Consumer READ 作为消息读派生的前提，或在服务清单中增加 `message_read_rules` 段。

## 4. 实际执行的命令与结果

环境：`source ~/.nvm/nvm.sh && nvm use 24`，`LANG=en_US.UTF-8 PYTHONPATH=packages/eios-core/src:tests`，`uv run --frozen pytest -q …`。

1. `tests/test_bootstrap.py tests/test_db_boundary.py`
   - 首次 2 failed：临时号 0093 与 catalog 连续性冲突，改为 0092 后 6 passed。
2. `tests/test_work_feeds_pg.py` 的首次失败：
   - admin 夹具写对象时触发器违反 feed 的 RLS：touch 改为切换并恢复租户上下文。
   - 测试 worker 在授权变更后会话过期，报 `WorkFeedDenied`：测试改为每个 tick 重新认证，与部署进程一致。
   - 端到端重放断言错误：重放时 `applied` 计数的是既有提案状态，改为断言写入次数和模型调用次数不变。
   - 授权拒绝分类为 `match_failed`：补上 `AuthorizationFactDenied` / `AuthorizationUnavailable`。
   - 最终 8 passed。
3. 回归：
   - 首次：`test_bootstrap test_db_boundary test_recall_pg test_claim_matching_pg test_candidate_merge_pg test_claim_extraction_jobs_pg test_claim_store_pg test_review_decisions_pg test_object_edits test_conversation_messages test_work_feeds_pg` → 95 passed / 1 failed。失败项是 AT-064 边界测试，要求触及 Claim 的迁移名带 `_nx020_` 等标签；迁移改名为 `0092_nx020_nx021_work_feeds.sql` 后，`test_bootstrap` + `test_claim_store_pg` 24 passed。
4. `tests/test_community_container_contract.py`：7 passed。
5. `tests/test_background_services_pg.py`：
   - 首次 5 failed：后端签名键文件应为原始 32 字节（不是 hex），修正测试夹具。
   - 最终 7 passed。
6. 清单与打包回归：`test_service_grants_pg test_grants_follow_latest_pg test_grants_follow_latest test_property_grant_derivation_pg test_review_decisions_pg test_wheel_install test_doctor test_compose_bootstrap test_publication test_community_container_contract test_background_services_pg` → 82 passed。

## 5. 可回填的验收（证据均为合成数据测试，不是真实环境证据）

| AT | 测试 |
|---|---|
| AT-025 检索隔离 | `test_work_feeds_pg.py::test_other_tenant_and_world_never_see_or_drain_the_instance`、`::test_erased_or_revoked_instance_is_never_recalled`；已有 `test_recall_pg.py` 的租户/world/属性权限用例 |
| AT-061 全匹配自动应用 | `test_work_feeds_pg.py::test_extracted_claims_are_matched_and_applied_by_the_background_worker`（真实提取 → feed → 匹配 → 受治理写入）；已有 `test_claim_matching_pg.py::test_full_match_closed_vocabulary_auto_applies_with_receipt` |
| AT-062 封闭词表新值 | `test_claim_matching_pg.py::test_closed_vocabulary_new_value_becomes_candidate_and_waits`；worker 路径下的候选暂存与粘合见 `test_work_feeds_pg.py::test_match_worker_matches_marked_conversation_then_glues_staged_candidates` |
| AT-063 开放属性新值 | `test_claim_matching_pg.py::test_open_property_new_value_auto_applies_with_evidence_time` |
| AT-065 新实例分流 | `test_claim_matching_pg.py::test_new_instance_strong_id_created_name_only_staged`；新建实例进入实例索引见 `test_work_feeds_pg.py::test_governed_create_and_edit_mark_the_instance_and_reflow_makes_it_recallable` |
| AT-020 抽取重跑 | `test_claim_matching_pg.py::test_rerun_same_claims_is_idempotent`；worker 重放见 `test_work_feeds_pg.py::test_extracted_claims_are_matched_and_applied_by_the_background_worker`（重新登记后无重复写入、无模型调用）；提取侧见 `test_claim_extraction_jobs_pg.py::test_debounce_and_crash_between_accept_and_mark_is_idempotent` |
| AT-021 版本冲突 | `test_claim_matching_pg.py::test_same_revision_conflict_one_wins_other_reassessed`（受治理层 40001 后重新评估；HTTP 409 见 §3） |
| AT-022 晚到事实 | `test_claim_matching_pg.py::test_late_older_evidence_does_not_overwrite_newer` |
