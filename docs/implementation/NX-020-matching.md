# NX-020 四层匹配判定与自动应用（开发线 L2）

状态：实现与定向测试完成，**未标 done**。NX-020 依赖 NX-012/019/021，其中 NX-018 仍未完成，由调度员判定。迁移 `0069_nx020_claim_matching.sql` 为临时编号。

依据：ADR-019 §3.3（三种判定与六项决定）、docs/04 §4–§7（Claim 类别、提案生命周期、事实优先规则）、契约 `ontology-mutation`、`candidate-definition`。

## 1. 交付

| 文件 | 内容 |
|---|---|
| `packages/eios-core/src/nexloop_eios/claim_matching.py` | `ClaimMatcher`（匹配、提案、候选、自动应用）、`MatchConfiguration`、`ScriptedMatchProvider`、匹配提示词 |
| `packages/eios-core/src/eios/migrations/0069_nx020_claim_matching.sql` | 三张表与签名 definer |
| `tests/test_claim_matching_pg.py` | 12 项真实 PG 测试，使用真实 EIOS 授权与受治理 Action |
| `tests/verification_real_matching.py` | 显式 opt-in 的真实模型验证（默认 CI 不收集） |
| `docs/implementation/NX-020-real-model-report.json` | 真实模型证据 |
| `tests/test_claim_store_pg.py` | NX-019 的静态边界测试：允许名单加入 `claim_matching.py` 与 nx020 迁移，Context/Role 不读 Claim 的本意不变 |

## 2. 处理链

```
authz.nexloop_read_conversation_claims（L3，需 Conversation READ）
  → 每条 Claim：
      hypothesis → 只记 outcome=hypothesis，resolution 保持 hypothesis_only，不调模型、不进审核
      非 asserted / negated / intent / commitment / 无原文 → needs_resolution（不调模型，不写）
      其余 → OntologyRecall（L2，NX-021）召回定义与实例
           → 模型在召回结果与 Action 绑定的 Schema 视图内给出四层判定（不可信）
           → 确定性守卫 → full_match / partial_match / no_match / needs_resolution / noop
  → authz.nexloop_record_claim_match（签名 + nexloop.claim.match EXECUTE）记录匹配日志、提案或候选，并更新 Claim resolution_state
  → ClaimMatcher.apply：proposed → applying → applied，经现有 GovernedObjectEditor/Creator 写入
```

### 2.1 确定性守卫（与模型无关）

| 层 | 规则 |
|---|---|
| 类型 | 顾客 Claim 固定为配置的 Consumer 类型，实例为 NX-019 服务端推导的 consumer_id（已验证会话消费者）。实体 Claim 的类型 ref 必须出现在召回结果里，且有配置的 edit/create Action |
| 实例 | 强标识的 key 必须是该类型唯一的 `primary_key`，value 必须逐字出现在原文 quote 中（防止模型编造），再按 NX-021 强标识召回：1 个命中即定位，0 个则创建，多个则 needs_resolution。只有名称时生成 `object_instance` 候选，不按相似度自动关联（docs/04 §5） |
| 属性 | ref 必须在召回结果中（属性本身或其词表值），且存在于 Action 绑定的 ObjectTypeDefinition 中，即写入时实际校验的 Schema |
| 值 | 开放属性：值必须等于 Claim 原值。封闭词表：值必须是 enum 成员；若与原值不同（同义映射），该词表值 ref 必须被召回。最后用 EIOS `PropertyDefinition.normalize_value` 校验类型 |
| 判定 | 封闭词表命中 → full_match；开放属性新值或强标识新实例 → partial_match；新类型、新属性、新词表值、仅名称实例 → no_match（候选）；与当前值相同 → noop |
| 候选 | 新类型或新属性名必须是合法标识符，value_type 只能取契约枚举。property_group 只是审核参考元数据，不在九类内时归入 `other` |

### 2.2 提案与应用（docs/04 §6）
- 每个提案按 `ontology-mutation` 契约生成，测试用 FormatChecker 校验：
  - `operations[0]` 是 `set_property`（expected_revision 为判定时读到的对象 revision）或 `create_object`
  - 更正时追加 `supersede_claim`
  - `evidence_refs` 为 `claim:<id>`、`message:<id>`；`valid_from` 为证据有效时间；`conflict_policy=reject_and_reassess`
- `business_intent_ref` 稳定，proposal_id = uuid5(intent)。同一新强标识只有一个创建意图，后续 Claim 复用同一提案。
- 状态转移在 SQL 中做 compare-and-set，合法转移见 `authz.nexloop_claim_match_transition_allowed`。`applying` 不会退回 `proposed`；中断后用记录下的同一组参数和 intent 恢复，受治理 Action 幂等重放，不会二次写入。
- revision 冲突（40001）时，提案转为 `conflict` 后重新评估：重读 revision 与晚到证据规则，生成新的 intent，最多重试 `max_reassess` 次，绝不盲目重写。
- 晚到证据：同一属性已有 effective_at 更新的已应用提案时，较旧的证据转为 `superseded(late_evidence)`，Claim 也标为 superseded。`correction` 类不受此限，并把被更正的 Claim 标为 superseded，形成更正链。
- 授权被拒绝 → `rejected`。授权不可用（可能是瞬时故障）时提案保持 `applying` 并向上抛出，便于观察积压后恢复。

### 2.3 存储（0069）
- 三张表，全部由 nexloop_owner 持有，启用并强制 RLS，对应用角色无表权限：
  - `ontology.nexloop_claim_matches`：主键 (claim, matcher_version)，同版本重跑即重放
  - `ontology.nexloop_mutation_proposals`：business_intent_ref 唯一
  - `ontology.nexloop_candidate_definitions`：(kind, dedupe_key) 唯一，同文本候选累积依赖 Claim
- 写入和读取只能经 `authz.nexloop_record_claim_match` 与 `authz.nexloop_read_claim_matching`（仅授予 domain_worker）。两个函数都要求：HMAC 签名、参数摘要、活跃的 service 凭据（拒绝浏览器会话）、当前的 `eios:action:nexloop.claim.match:1` EXECUTE。
- 依赖的已有对象：`ontology.nexloop_claims`（0066 NX-019）、`authz.nexloop_assert_action_authority`、`authz.nexloop_authority_signing_keys`、`authz.nexloop_service_credentials`、`authz.nexloop_browser_token_realms`、`extensions.hmac`；正式写入沿用 0010/0015/0054 的受治理 create/edit 链。

## 3. 交给 NX-045 的部分
1. `nexloop_candidate_definitions.status` 只允许 `staged`。merged / pending_review / published / rejected / superseded 的状态机、`merge_scores`、粘合四项特征与阈值配置都由 NX-045 扩展（需要新迁移放宽 CHECK）。
2. 去重键 `dedupe_key` 的组成：
   - 属性：(owner_type, name)
   - 词表值：(property_ref, 归一化值)
   - 类型：(name)
   - 实例：(type, 归一化名称)

   这是精确去重，相似去重属于 NX-045。表列 `dependent_claims` 会累积，而 `candidate.dependent_claim_refs` 只记录首个 Claim，NX-045 出队时应以表列为准。
3. 依赖 Claim 处于 `awaiting_definition`。批准或合并后，应把 Claim 置回 `unresolved`（或直接按新 ref 生成提案），并用新的 `matcher_version` 重跑 `ClaimMatcher.match_claim`；同版本匹配日志会被视为重放。回流时先调用 NX-021 的 `RecallIndexer.index_object_type` / `index_alias`，让召回能命中新定义（AT-069）。
4. 模拟 / shadow 世界：候选记录带 world，`runtime.nexloop_conversations` 目前只允许 real；real 队列应只取 world='real'。

## 4. 实际执行的命令与结果

环境：`source ~/.nvm/nvm.sh && nvm use 24`、`LANG=en_US.UTF-8`、`PYTHONPATH=packages/eios-core/src:tests`、Homebrew PG 18.4 + pgvector 0.8.5。

1. 镜像与 doctor（第 1 步，单独提交）：
   - 首次运行 1 failed / 13 passed / 22 errors：启动器 fixture 在本 worktree 缺 node_modules 时执行 `pnpm build:pi` 失败，属于环境问题。
   - `pnpm install --frozen-lockfile --offline` 后 36 passed，384.02s。详见 `NX-020-pgvector-image.json`。
2. `tests/test_claim_matching_pg.py` 的迭代：
   - `nexloop_read_object` 对尚未设置的属性也报 “property unavailable”，改为先在 EIOS 判定 READ，再把 SQL 侧的缺失视为未设置。
   - 测试夹具问题：hypothesis 必须有 derived_from；replay 结果的断言写错。
   - 之后 12 passed。
3. 真实模型（opt-in，deepseek / deepseek-flash，6 个合成用例；召回用确定性 embedding）：
   - 第 1 轮 failed：真实模型暴露了实现缺陷，`create_object` 提案带了 property_name，违反表 CHECK。已修复并补脚本化回归。
   - 第 2 轮 passed，一致率 5/6：模型给出九类以外的 property_group，被守卫拒绝。已改为归入 other。
   - 第 3 轮 passed，**一致率 6/6**，41.60s。报告见 `NX-020-real-model-report.json`（其中含 previous_runs），junit sha256 `bc46238ab2509660440e79d2413ae9fb55f8ad8aa24f4b7e2ef5e3deb783e761`。
4. 最终定向测试（matching、recall、claim_store、object_edits、bootstrap、db_boundary、doctor、embedding_provider）：
   - 首次 1 failed：NX-019 的静态边界测试不允许任何其他模块读 Claim，按上文 §1 更新允许名单。
   - 最终 **61 passed，69.99s**，junit sha256 `cae453c20b364d002ad30b37b3ffc9342235aa8b6b653b46be8bee2c91f2b93f`。
   - core_sandbox、compose_bootstrap、wheel_install、effect_execution_sql：15 passed，19.62s。
5. 测试结束后没有本线遗留的 PG 或 pytest 进程。

## 5. 验收结论（建议）

| AT | 建议 | 依据 |
|---|---|---|
| AT-061 全匹配自动应用 | passed（测试证据 + real 6/6） | `test_full_match_closed_vocabulary_auto_applies_with_receipt`：经 Consumer.edit 受治理 Action 写入并有 receipt（revision 2），有 Action claim 记录，无候选，提案满足契约 |
| AT-062 封闭词表新值 | passed（测试 + real） | 见下方说明 1 |
| AT-063 开放属性新值 | passed（测试 + real） | `test_open_property_new_value_auto_applies_with_evidence_time`：预算 2000 自动写入，valid_from 和证据 hash 保留 |
| AT-065 新实例分流 | passed（测试 + real） | 见下方说明 2 |
| AT-020 抽取重跑（提案层） | passed（提案层） | `test_rerun_same_claims_is_idempotent`：同进程和新凭据重跑都只是重放，不再调用模型，不新增提案、候选、写入或 Action claim。抽取层由 NX-019 的 input_digest 保证 |
| AT-021 版本冲突（提案层） | passed（提案层） | `test_same_revision_conflict_one_wins_other_reassessed`：两个提案基于同一 revision，后者第一次得到 40001 后转为 conflict，重新评估后以新 intent 应用（attempts=2）。没有 HTTP 409 层 |
| AT-022 晚到事实（提案层） | passed（提案层） | `test_late_older_evidence_does_not_overwrite_newer`：较旧证据被 superseded(late_evidence)，不覆盖较新值 |
| AT-064 隐性推断隔离 | 部分 | hypothesis 不调模型、不生成提案或候选（`test_hypothesis_and_non_assertions_never_written_or_queued`）；Context 正式属性区的隔离不在本任务内 |

说明：
1. AT-062 的依据是 `test_closed_vocabulary_new_value_becomes_candidate_and_waits`：`vocabulary_value` 候选满足契约且状态为 staged，属性没有写入，Claim 为 awaiting_definition；同文本的两条 Claim 只产生一个候选。
2. AT-065 的依据是 `test_new_instance_strong_id_created_name_only_staged`：
   - SKU-2002 经 Product.create 自动创建，两条 Claim 共用一个创建意图。
   - 已有的 SKU-1001 被定位，没有重复创建。
   - 只有名称的实例生成 `object_instance` 候选。
   - 不在原文中的伪造强标识被拒绝。

其他已覆盖的点：更正链（`test_correction_chain_supersedes_previous_claim`）；中断后的恢复（`test_interrupted_apply_resumes_with_same_intent_without_double_write`）；没有 match EXECUTE 时不记录任何内容、应用角色不能直接查表（`test_match_requires_execute_grant_and_records_nothing`）。

## 6. 局限

- PG 测试的 Claim 由 bootstrap 身份按 NX-019 表结构写入（uuid 租户，以满足契约的 uuid 格式），读取仍走 L3 的授权读函数。没有做“真实 NX-019 提取 → 匹配”的单一端到端测试，原因是 L3 夹具的租户固定为 `synthetic-a`。
- 只处理顾客实例和单一主键的实体；`link_relation`、`invalidate_property` 未实现；negated 或布尔否定一律交给 needs_resolution。
- 已有实例在只有名称时不做任何关联，一律生成候选或 needs_resolution，可能导致候选偏多，由 NX-045 粘合处理。
- 匹配是逐条 Claim 串行处理：每条一次召回、一次模型调用、若干次 EIOS 判定，没有接入后台队列或 outbox。
- Agent 规划时可见原文、Context 不放正式属性区（ADR-019 决定 5）由 Context 相关任务实现，本任务只保证不写入。
