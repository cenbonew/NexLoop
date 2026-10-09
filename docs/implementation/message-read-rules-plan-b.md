# 部署授权方案 B：服务清单 `message_read_rules`（临时迁移 0099）

状态：已实现，分支 `message-read-rules`（BASE main `00ff5bb`），L4。
关联：ADR-020 §1/§3，NX-048，NX-019，NX-020；`docs/implementation/NX-021-020-close.md` §3 记录的部署授权缺口。

## 1. 缺口

只靠服务清单（`deploy/authorization/service-grants.v1.json`）授权时，提取 worker 读不到它要处理的 Conversation/Message，`claim_matcher` 读不到 Conversation 的 Claim。

- 0077/0086 的消息读派生要求同一主体对该 Consumer 持有**已配置的** READ。服务主体不应逐个 Consumer 配置 READ。
- 服务清单也不能表达 `message_read_rule`。
- 此前端到端测试因此使用逐对象的测试授权。

## 2. 决定（调度员 2026-10-10 指派的方案 B）

服务清单新增 `message_read_rules` 段，按用途放开读取；规则在 SQL 中逐次派生：

| 用途 `read_purpose` | 主体 | 可读 | 依据 |
|---|---|---|---|
| `claim_extraction` | `claim_extraction_worker` | 提取作业引用的 Conversation（对象、`consumer_id`、`owner_principal`）和该作业 `message_ids` 中的 Message（规则列出的字段） | `claim-extraction` 队列中一个正在运行的作业：**当前凭据持有租约**、租约未到期、fence 与签证明时一致 |
| `claim_matching` | `claim_matcher` | 待匹配 Claim 的证据 Message（`body`）和该 Claim 所在 Conversation **对象**（用来读它的 Claim） | 一条 Claim 仍待匹配：`unresolved`、`needs_resolution`、`awaiting_definition`，或尚无匹配记录的 `hypothesis_only` |

两种用途都要求：

- 主体当前仍持有该用途 Action 的 EXECUTE：`nexloop.claim.extract:1`（提取），`nexloop.claim.match:1`（匹配）。
- 清单校验还要求：
  - 提取规则所属主体，在同一清单中同时持有 `nexloop.claim.extract:1` 和 `NexLoop.queue.claim-extraction:1`；
  - 匹配规则所属主体，持有 `nexloop.claim.match:1`。

## 3. 实现

**迁移 `0099_nx019_nx020_message_purpose_reads.sql`**（只追加，0001..0098 不变）：

- 新 fact kind `message_purpose_rule`：
  - key 为 `[principal, purpose]`；
  - payload 恰为七键：`tenant_id`、`principal_id`、`purpose`、`type_name='Message'`、`fields`、`active`、`valid_until`；
  - `fields` 是 {accepted_at, actor, body, conversation_id, sequence} 的有序子集，且必须含 `body`；
  - 主体必须已是 service 的 `subject_authority`；
  - 只能由 `control.nexloop_configure_manifest` 写入。configure_manifest 以 0086 版本为底，只加了这一种 kind 及其形状校验。
- `authz.nexloop_message_purpose_basis_for`：只查依据，不加锁，不授予任何权限，返回 `mode='purpose'` 和作业/Claim 依据。
- `nexloop_message_read_basis` / `nexloop_conversation_read_basis`（沿用 0086 函数体）：
  - 在 accepted-Message 规则不适用时，回落到用途依据；
  - 主体有生效的用途规则时，`configured` 答案带 `purpose_rules=true`。
- `authz.nexloop_assert_purpose_message_read`：每次使用都在 share 锁下重读：
  - 凭据锁和身份快照；
  - 仅限 `real` world、service 主体、无 Run 上下文；
  - 主体在目标对象或其任一字段上若有已配置 grants（即使为空），已配置优先，派生被拒（`superseded`）；
  - 规则的 hash、`active` 和期限；
  - 用途 Action 的 grants 不为空；
  - Message 对象存在且字段存在、已受理（入站 outbox 或外发记录）、所属 Conversation 一致；
  - 提取用途：作业行的 queue、status、租约期限、`lease_credential = 本凭据 digest`、fence、payload 中的会话和消息；证明期限不超过租约；
  - 匹配用途：Claim 行的会话、证据消息、待匹配状态。
- 派生链：在 `nexloop_assert_derived_message_read` 最外层加一层分派，`derivation='purpose-message-v1'` 走新函数，其余原样进入 0086 → 0080 → 0077。
  - `authz.nexloop_assert_read_authority` 及其 O5b memo 包装（0096）都没有改动。
  - 带 derivation（type-property-v1 除外）的声明在 0096 中本来就绕过 memo、始终做完整检查，所以 memo 的键与失效条件照旧成立。
- 所有新函数和重建的函数都是 `set search_path=pg_catalog,pg_temp`；SECURITY DEFINER 函数均设置了 search_path。

**Python**：

- `message_read.MessagePurposeRule` 进入 `trusted_configuration.FACT_MODELS`。
- `DerivedEvidenceReads`（`object_reads._authority` 的 Message/Conversation 分支）处理 `mode='purpose'`：
  - 签出 `purpose-message-v1` 声明，期限取规则期限、租约期限、now+25s 三者的最小值；
  - purpose 依据（以及带 `purpose_rules` 的 configured 依据）只缓存在当前 authority request scope（一个工作单元）内，不跨作业复用。
- `service_grants`：
  - `message_read_rules` 段的校验与编译；
  - apply 写规则，清单中移除规则时写 `active:false` 停用（从不删除）；
  - doctor 通过 fact drift 报告规则偏差。
- 清单升到 `manifest_version` 5，新增两条规则；`deferred` 中关于 Message READ 的条目改写为说明派生来源。

## 4. 失效（全部在下一次使用时由 SQL 判定，不靠缓存）

| 变化 | 结果 |
|---|---|
| 清单移除规则（apply 写 `active:false`） | `rule unavailable`；发布同时推进 epoch，旧证明因 directory_hash 失效 |
| 用途 Action 被撤（grants 置空） | `action revoked` |
| 主体对该 Message/Conversation 有已配置 grants（含空集） | `superseded`：已配置优先，派生不补权 |
| Message 删除 | `purpose message unavailable` |
| Message 不在作业引用内，或已不在该 Conversation | `task unavailable` / `conversation changed` |
| 作业完成、失败、租约到期、被其他凭据重新租用（fence 变化） | `task unavailable`；租约内签出的证明在作业结束后由 SQL 拒绝 |
| Claim 已解决、被取代或定义被拒 | `matching claim unavailable` |
| 证明中依据被篡改（fence、job_id、purpose、conversation_id） | 拒绝 |

## 5. 测试（`tests/test_message_purpose_reads_pg.py`，9 例，真实 PG，合成数据）

服务主体只用仓库清单经 `service_grants.apply`（`nexloop_configurator`、可信配置）授权；断言两个主体在 Message/Conversation/Consumer 上的 grants 计数为 0，且受理消息不推进 authority_revision。

1. `test_manifest_only_worker_extracts_and_matcher_reads_claim_evidence`：
   - 部署路径：Message → feed → 调度器入队 → worker 领取 → 读取窗口 → 记录 Claim；
   - matcher 读出该 Conversation 的 Claim 视图，并读到被引用消息的 body；
   - 负例：未被引用的消息、规则外字段（actor）、Conversation 属性、无 Claim 的 Conversation 均被拒；worker 在租约外什么都读不到。
2. `test_worker_reads_exactly_the_leased_task_and_nothing_after_it_ends`：
   - 租约内可读作业消息和 Conversation 属性；
   - 另一会话的消息被拒；
   - 作业 finish 后，租约内签出的证明重放给 `nexloop_read_object`，被 SQL 拒绝；新读取也被拒。
3. `test_tampered_basis_and_other_credential_are_refused`：
   - 篡改 fence、job_id、purpose、conversation_id 均被拒；
   - matcher 与调度器凭据不能借 worker 的租约读取。
4. `test_rule_removal_deactivates_and_denies_immediately`：清单移除规则 → apply 写 `active:false` → 租约仍在，但新会话读取被拒；doctor 中无该规则的漂移。
5. `test_purpose_action_revoked_or_configured_empty_grant_denies`：
   - 已配置的空 grant 优先，该消息被拒、其他消息仍可读；
   - 撤销 `claim.extract` 后，全部读取被拒。
6. `test_deleted_message_and_resolved_claim_are_denied`：
   - Claim 解决 → 其证据消息被拒；
   - 全部 Claim 解决 → Conversation 被拒；
   - `needs_resolution` 恢复可读；
   - 删除 Message → 被拒。
7. `test_message_deleted_during_lease_is_denied_and_the_task_fails_closed`：租约中删除窗口内一条消息 → 该消息被拒，提取以 `ClaimExtractionDenied` 失败，0 Claim。
8. `test_manifest_rule_validation`：
   - 未知用途、缺 body、未排序、非 UTC 期限、重复、缺用途 Action grant、多余键均拒绝；
   - 用途文本含 REAL_DISPATCH_ENABLED 按策略拒绝。
9. `test_apply_is_idempotent_doctor_in_sync_and_sql_refuses_malformed_rules`：
   - 重复 apply 无写入；
   - 绕过 Python 校验直接调 configure_manifest 时，SQL 拒绝 7 种畸形规则；
   - 合法形状可写入；doctor 报告与清单的偏差，apply 恢复。

## 6. 未覆盖与限制

- matcher 端到端只验证到读取层：Claim 视图（即 `ClaimMatcher.match_conversation` 的读取调用）和证据消息。
  - 匹配后的正式写入走 0084 的属性派生，不在本方案范围；
  - 本轮没有用"只靠清单"的 matcher 跑完整的 `ClaimMatchWorker`（召回索引、模型、正式写入）。
- 其他租户、其他 world、Run 凭据：由 SQL 的身份快照检查覆盖，与 0077/0086 相同的代码路径，本文件没有单独再写这三类负例。
- 匹配用途下，一条 Conversation 只要还有待匹配的 Claim，matcher 就能读该 Conversation 的全部 Claim 视图，而不只是那一条。这是 `nexloop_read_conversation_claims` 以 Conversation 对象为单位授权所决定的。
