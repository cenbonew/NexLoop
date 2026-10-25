# Consumer 擦除覆盖缺口（NX-029 / AT-026）

- **来源**：`tests/test_consumer_erasure_coverage_pg.py` 的 `KNOWN_GAPS` 列出了 21 张带 `consumer_id` 的 real 表，NX-029 的擦除传播覆盖不到（L5 aeb5f43a；L6 审查 1.6）。
- **目标**：每张表给出一个结论，并且有行级端到端证明。
- **结论分为四类**：
  - **A**：已经被擦除覆盖，只是被启发式误判；
  - **B**：外键级联删除，或者由保留期清理覆盖；
  - **C**：需要补擦除；
  - **D**：法律或审计上必须保留，保留事实、去掉文本。
- **做法与 NX-029 一致**：
  - 擦除**完成**（state 变为 completed）时，由 `control.nexloop_consumer_erasures` 上的触发器执行（0296 的分配表步骤是在进入 purging 时）。放在完成时，是因为 purge 的各阶段要靠会话表和 plans 表上的原 id 找到这个人的消息和计划，这两张表只能等最后再改；为了让所有表共用一个假名，6 张表也一起放到完成时；
  - 只经过 `runtime.nexloop_erasure_open` / `nexloop_erasure_permits` 打开的擦除范围写入；
  - 文本改成 `''`、`null` 或 `{"erased":true}`，`consumer_id` 改成同一个随机的 `pseudonym:<32hex>`；
  - 每次擦除留一条只含计数的墓碑。
- **抑制不受影响**：这个 Consumer 的抑制仍然有效。擦除自己写入的 contact restriction（0132）保留原 id，所以退订或拒绝记录改成假名之后，不会重新放开联系。

## 第一批：存有 Consumer 原话或运营对他的备注的表（0305，已完成）

| 表 | 原话或备注所在列 | 结论 | 擦除后保留什么 |
|---|---|---|---|
| `control.nexloop_contact_refusal_hits` | `matched_text`（拒绝时的原话） | C + D | 规则、certainty、control revision、时间；`matched_text=''`，`consumer_id` 改成假名 |
| `control.nexloop_reply_escalations` | `detail` | C | 原因代码和时间；`detail={"erased":true}`，`consumer_id` 改成假名 |
| `control.nexloop_takeovers` | `reason`（接管理由） | C + D | 接管期间、结束方式、revision；`reason=''`，`consumer_id` 改成假名；scope 为 consumer 时 `scope_ref` 也改成假名 |
| `runtime.nexloop_outreach_sends` | `body`（发出的正文）、`body_digest` | C | 状态、purpose、topic、回执；`body=''`，`body_digest=null`（摘要可以用来确认猜测的正文，L6 评审 1.3），`consumer_id` 改成假名。还在 dispatching 或 unknown 的行，等它收尾时再擦，见下面的说明 |
| `runtime.nexloop_marketing_optouts` | `matched_text`（退订原话）、`reason`（运营备注） | D | 退订或恢复这件事本身、来源、topic、时间；`matched_text=null`，`reason=''`（原本为 null 的保持 null），`consumer_id` 改成假名 |
| `runtime.nexloop_marketing_consents` | `reason`（运营记录的同意说明）、`evidence_ref` | D | 授予或撤回、来源、时间；`reason=''`，`evidence_ref` 改成假名（它可能是通话或单据编号，甚至是电话号码，L6 评审 1.2），`consumer_id` 改成假名 |
| `runtime.nexloop_conversations` | `consumer_id`、`principal_id`、`idempotency_key` | C | 会话行和序号保留；`consumer_id` 和 `principal_id`（客户的主体，同样能识别到人；L5 合入）改成同一个假名，`idempotency_key` 每行换成一个随机假名。**L6 评审 1.1（确认级）**：之前擦除从不改这张表，所以其他表保留下来的 message_id 和 conversation_id 都能经它关联回原 id。原来的启发式只看“表名出现在擦除函数里”，把这里只做查询的表误判为已覆盖 |
| `runtime.nexloop_plans` | `consumer_id`、`recipe`、`control_snapshot` | C | 计划的版本和状态链保留；`consumer_id` 改成同一个假名，`recipe` 和 `control_snapshot` 改成 `{"erased":true}`（计划内容可能写到这个人，control_snapshot 里有原 id；L5 合入）。新的启发式发现这里也只是被查询，同样可以用来关联 |

- **guard 只给擦除路径放行**：原来的不可变或只追加触发器，换成了先询问 `erasure_permits` 的新函数（共享的原函数没有重定义），其余规则逐字保留。擦除范围之外，同样的擦除值也会被拒，delete 也会被拒。
- **约束**：要求 64 位 hex id 或非空备注的约束，同时接受擦除值。备注为空只允许出现在已经改成假名的行上。
- **在途的 outreach**：处于 dispatching 或 unknown 的发送行由它自己的状态机负责，做法和 0138 对待 unknown 效应意图一样。擦除的 blocking 步骤已经拒绝任何新的联系。
  - 这一行收尾时，`runtime.nexloop_outreach_sends` 上的触发器 `nx029_outreach_late_erasure` 会补擦：清空正文和摘要，`consumer_id` 改成一个新的假名，并补一条墓碑。
  - 在它收尾之前，这一行仍然保留原文和原 id，这是唯一能关联回原 id 的行。测试明确断言了这一点，也断言了它收尾之后就关联不上了。
- **覆盖测试的启发式（L6 评审 1.1、4.2）**：一张表要算“被擦除覆盖”，必须是擦除函数里某条 `update` 或 `delete` 语句的目标。只被查询是不算的。
  - 改了之后，新发现了 3 张只被查询的表：会话表和 plans 表这次已经纳入；`control.nexloop_contact_restrictions` 是有意保留原 id 的，已移入 `NO_ERASURE_NEEDED` 并写明理由（擦除之后这个人永远不能再被联系）。
- **关联测试**：把这些表保留下来的 message_id、conversation_id、scope_ref、intent_id、evidence_ref，和所有既有 consumer_id、又有其中一种 id 列的表做 join，再经会话表 join 消息表，断言找不到原 consumer_id。
  - 注意：后续 15 张表中有几张（例如 native_web_events、scope_denials、relay_handoffs）也带 message_id 或 conversation_id。它们会在下一批处理，目前测试的种子数据没有用到它们。
- **测试**：`tests/test_erasure_coverage_pg.py`
  - 端到端：owner 的受治理擦除 Action 加上保留期执行器，直到 completed；
  - 断言 6 张表中没有原文、也没有原 id（在途那一行除外）；
  - 事实保留，6 张表共用同一个假名；
  - 墓碑计数正确，抑制仍然有效；
  - 每张表在擦除范围之外 update 和 delete 都被拒。

## 第三批：其余 15 张（0306，已完成；会话和计划在第二批 0305）

擦除**完成（completed）**时，由触发器 `zz_nx029_2_consumer_reference_erasure` 执行。有保留期冻结时（`completed_with_holds`）不执行，全部保留。这一批使用自己的假名；擦除后留一条只含计数的墓碑（`consumer_references`）。

| 表 | 结论 | 处理 | 理由 |
|---|---|---|---|
| `control.nexloop_consumer_owners` | C（删除） | 删除行 | principal 到 Consumer 的关联本身，擦除后不再需要；guard 只管插入和更新 |
| `control.nexloop_relationship_context_recipes` | C（删除） | 删除行 | 配方里的 assessment 已经被擦除删除（0138），配方失去意义 |
| `runtime.nexloop_evo_patch_sources` | C（删除） | 删除行 | 只是“补丁来源 Consumer”的关联；补丁本身保留 |
| `runtime.nexloop_message_relay_leases` | C（删除） | 删除行 | 短期租约；擦除已经等效应收尾 |
| `runtime.nexloop_commitments` | C（删除） | 删除登记行 | 登记表的 guard 本来就允许擦除路径删除；Formal 承诺对象由擦除的 commitments 阶段处理（made_to 已改写） |
| `authz.nexloop_role_run_bindings` | C | consumer_id 改成假名 | Run 与 Consumer 的绑定；Run 本身保留 |
| `control.nexloop_consumer_identity_link_receipts` | C | identity_ref（主键，每行一个假名）、consumer/subject/account id 改成假名，request_payload 改成 `{"erased":true}` | payload 和 identity_ref 是渠道身份 |
| `control.nexloop_data_exports` | C | consumer_id 改成假名，reason 改成 `''` | 导出文件另有到期删除；行保留“发生过导出”这件事 |
| `control.nexloop_effect_contexts` | C | consumer_id 改成假名，expected_versions 改成 `{"erased":true}` | expected_versions 里写着 `consumer:<原 id>`，是行级测试发现的 |
| `control.nexloop_effect_control_ledger` | C | consumer_id 改成假名 | 只有 id |
| `ontology.nexloop_extraction_runs` | C | consumer/principal id 改成假名，rejected 改成 `[]` | 被拒的抽取结果可能引用原话 |
| `runtime.nexloop_commitment_exceptions` | C | consumer_id 改成假名，detail 改成 `{"erased":true}` | 备注可能含原话 |
| `runtime.nexloop_native_web_events` | C | consumer/principal id 改成假名；`native_web_transports` 的外键改成 on update cascade，principal 随之改写 | 只有 id 和摘要；消息正文已经由 NX-029 擦除 |
| `runtime.nexloop_relay_handoffs` | C | consumer_id 改成假名 | 只有原因代码 |
| `runtime.nexloop_scope_denials` | C | consumer_id 改成假名，scope 和 request_scope 改成 `{"erased":true}` | scope 里可能有参数原文 |

- **guard**：
  - `nexloop_nx022_append_only`、`nexloop_commitment_registry_guard` 本来就先询问擦除范围；
  - `effect_contexts` 的身份不可变 guard 和 `scope_denials` 的不可变 guard 换成 v2：先询问擦除范围，其余规则不变；
  - 擦除范围之外，同样的擦除值也被拒（测试 `test_outside_an_erasure_the_new_guards_still_refuse`）。
- **约束**：放宽了三处约束，擦除值只出现在已改成假名的行上：
  - `data_exports` 的 consumer_id 和 reason；
  - `effect_contexts` 的 expected_versions；
  - `native_web_transports` 的外键，改成 on update cascade。
- **覆盖测试**：`KNOWN_GAPS` 现在为空，并且锁定为 0：以后新增带 `consumer_id` 的表，要么被擦除写到，要么登记理由。
- **行级测试**（`tests/test_erasure_coverage_references_pg.py`）：
  - 16 张表各种一行，外加 `native_web_transports`。擦除前每张表都含这个人（阳性对照）；擦除后没有任何一行含原 id、principal、身份标识或原文；
  - 删除类的表不留假名行；改成假名的表共用这一步的同一个假名；
  - 另一个 Consumer 的行不受影响；
  - 有保留期冻结时全部保留，不写墓碑。

## 第四批：全库搜索兜底与 L6 的两条确认级问题（0307，已完成）

- **L6 确认级 1**：`runtime.nexloop_conversation_messages.record.actor` 保存着客户的 principal。
- **L6 确认级 2**：`runtime.jobs.normalized_input.run_command.consumer_ref` 保存着 `consumer:<原 id>`。
- **全库搜索（行为部分）**：`tests/test_erasure_whole_database_pg.py`。
  - **场景**：造一个 Consumer，走完下面这些路径：
    - 入站消息和回复；
    - Claim、计划、承诺；
    - 付款事件（CommercialRecord）；
    - 一个先 unknown、后对账收尾的效应；
    - 一条 Run 的 job 和 invocation；
    - 一个客户级暂停和它的控制事件；
    - 一条 cohort 指标观测。
  - **执行**：owner 擦除，保留期执行器把擦除跑到 completed。
  - **搜索与断言**：对全库每张表的每个 text、varchar、json、jsonb、uuid 列搜索原 consumer id 和他的 principal id。除白名单中写明理由的行以外，一处都不能有。
  - **阳性对照**：擦除前，搜索能在 15 张以上的表中找到这个人，并且确实命中了 L6 指出的两处。
- **搜索发现的残留与处理**（0307，擦除完成时由 `zz_nx029_1_consumer_identity_erasure` 执行，使用自己的假名）：
  - 这个触发器的名字排在第二、第三批之前，所以能在会话被改成假名之前读到客户的 principal。

| 位置 | 处理 |
|---|---|
| `conversation_messages.record`（actor） | principal 和 id 按文本替换成假名，record 的结构不变 |
| `runtime.jobs`（normalized_input、plan_snapshot、result、error、actor_id） | 按文本替换 |
| `runtime.invocations`（actor_id、result、error） | 按文本替换 |
| `runtime.nexloop_action_claims.claim`（受治理的 Consumer.create 记录） | 按文本替换 |
| `ontology.nexloop_claims.consumer_id` | 改成假名（引文和值之前已经擦除） |
| `runtime.nexloop_effect_intents.consumer_id` | 改成假名（frozen_request 之前已经擦除） |
| `runtime.nexloop_effect_dispatch_controls.snapshot` | 改成 `{"erased":true}` |
| `runtime.nexloop_work_feed` | 删除 payload 中写着这个人的条目（例如 reply-due）。recall 索引的删除条目本身保留：它是瞬时的，recall worker 处理完就会删掉 |
| CommercialRecord 的 `consumer_ref` | 按已验证的事件重新推导。客户关联已经删除，所以推导结果是未关联；商业记录的 guard 只接受推导出来的状态 |
| `control.nexloop_control_scopes`（客户级暂停） | 删除 |
| `control.nexloop_control_events`（客户 scope 的事件） | scope_ref 改成假名；擦除自己的抑制事件除外 |
| `control.nexloop_metric_observations.subject_ref` | 改成假名。这个人因此不再计入 cohort 指标的分子（他的 cohort 成员已经被单独改成假名，0227） |

- **白名单**：有意保留原 id，都附了理由：
  - 擦除请求行；
  - 擦除写入的联系限制及其控制事件；
  - NX-035 的删除台账和恢复日志：恢复之后要重放这次擦除，必须知道删除的对象；
  - 墓碑；
  - 瞬时的 recall 删除条目。
- **结构部分**：`test_every_identity_column_table_is_erased_or_has_a_reason`。
  - 凡是带 principal_id、actor、actor_id、consumer_ref、subject_ref、scope_ref、subject_key、member_ref 等列的表，要么擦除函数中有对它的写入，要么登记理由（多数是员工、owner 或服务主体）。
  - 已知会写到这个人的 jsonb 路径单独登记，标明“已擦除”或“保留”。
  - 这只能证明“登记过”；“确实搜不到”由上面的全库搜索兜底，以后新加的表也会被它拦住。

### 第四批复核修复（L6 erasure-coverage-3，0307 内）

- **2.1 带边界的替换**：
  - jobs、invocations 和 action claims 只替换完整的 JSON 字符串 `"<id>"`、`"consumer:<id>"`，以及完全相等的 `actor_id`，不会替换另一个 id 中的子串；
  - 先按 `run_command.consumer_ref`、`actor_id` 或带边界的匹配缩小候选行，再做替换；
  - invocations 通过本 world 的 jobs 关联来限定范围；
  - 测试：构造一个旁观者，他的 consumer_ref 和 principal 包含被擦除者的 id，擦除后断言旁观者的 job 和 invocation 没有变化。
- **2.2 两种写法的客户级暂停**：`<id>` 和 `consumer:<id>` 两种写法都会删除，对应的事件都改成假名；测试中各造了一条。
- **2.3 hold 解除之后补做擦除**：
  - 机制：触发器 `nx029_erasure_resume_after_hold`。这个 Consumer 的**最后一个** hold 被解除时，`completed_with_holds` 的擦除回到 purging（从第一个阶段开始），保留期执行器重新执行全部阶段；完成时第一到第四批照常执行。
  - 已解除的 hold 行中 `scope_ref` 也改成假名，它属于历史记录。擦除完成时不可能还有生效中的 hold，否则不会完成。
  - **剩余边界**：hold 只是到期（`valid_until`）而没有被解除时，不会产生 update，不会自动恢复，需要负责人执行一次解除。
  - 测试：`test_an_erasure_held_back_by_a_hold_completes_when_the_hold_is_released`。hold 期间全部保留；解除后回到 purging；完成后全库搜索干净。
- **测试覆盖**：
  - needle 取会话、所有权、web 事件三处 principal 的并集；
  - 场景补上 Commitment、外发（含一条在途发送，完成后再收尾）和接管；
  - 白名单中的 recall 删除条目要求 `object_id` 等于被擦除者；
  - **列类型**：tsvector、text、varchar、json、jsonb、uuid、bytea 等都在搜索范围内。其余列必须属于不可能存放 id 的类型（数值、时间、pg_lsn、向量等），否则断言失败，所以以后新增其他类型的列会主动报错，不会被静默跳过。

## 擦除后残留与决策（负责人 2026-10-21）

一个 Consumer 的擦除完成后，原 id 只在下面几处出现。每一处都是负责人确认过的，全库搜索测试（`tests/test_erasure_whole_database_pg.py`）的白名单与这里逐条对应：

1. **联系限制保留原 id（有意为之）**：
   - 擦除自己写入的联系限制（`rule_id='consumer_erasure'`）和它的修订事件、擦除请求行本身，都保留原 id；
   - `runtime.nexloop_consumer_erasing` 依靠它们永久拒绝再联系、拒绝解除限制，所以被擦除的人不会再被联系；
   - NX-035 的删除台账和恢复日志也保留原 id：从备份恢复之后要重放这次擦除，必须知道删除的对象；
   - 这些都只有 id，不含任何文本。
2. **墓碑改用带密钥的 HMAC**（迁移 `0301_erasure_keyed_ids.sql`）：
   - Consumer 擦除的墓碑中，`subject_key` 保存的是 `hmac:<hex>`，由每个租户的擦除密钥（`control.nexloop_erasure_salts`，0132）计算，`tombstone_id` 也由它推导。实现方式是墓碑表上的插入前触发器；已有的墓碑在迁移中一次性改写（只在这一条语句期间暂停只追加 guard，做法同 0273）。
   - **读取方式**：按 consumer id 查回执时，先调用 `runtime.nexloop_erasure_subject_key(tenant, consumer)` 算出 HMAC，再用它查询。
   - **边界**：持有原 id、并且能调用这个验证函数的人，仍然可以确认两者是否匹配；HMAC 只防止从表里直接读出原 id。
3. **确定性哈希**：
   - **已有数据是接受的残留**：外联 intent id `outreach:sha256(tenant, consumer, purpose, origin, day)`，以及身份链接 request_id `sha256(tenant:world:consumer:ref)`。持有原 id 的人可以重新算出它们，但不能反推原 id。
   - **今后新生成的改用带密钥的哈希**：用同一份租户密钥做 HMAC（`runtime.nexloop_outreach_intent_id`，以及身份链接的 prepare）。同一组输入仍然得到同一个 id，所以幂等不受影响。
   - **上线当天的重放**：带密钥的 id 下还没有发送记录、而同一组输入按旧算法算出的 id 已经有记录时，沿用旧 id。所以跨越这次变更重放的请求只会找到原来的 intent，不会重复发送（测试 `test_a_request_replayed_across_the_change_finds_its_legacy_intent`）。
   - **身份链接**：prepare 按 identity_ref 返回已存储的 payload，所以变更前已经准备好的链接继续使用原来的 request_id。
   - **测试**：全库搜索把“按旧算法对同样输入算出的值”作为额外的 needle。新生成的发送擦除后搜不到它；旧算法生成的那一条作为已接受的残留列入白名单（负责人 2026-10-21 决定）。

另外，这次用真实的 accept 路径造了一条发送，发现 `outreach_sends.control_snapshot` 里也写着原 consumer id。现在擦除时把它改成 `{"erased":true}`，在途发送收尾时的补擦也一样。

**触发器顺序**：第二到第四批的完成触发器改名为 `zz_nx029_1_consumer_identity_erasure`、`zz_nx029_2_consumer_reference_erasure`、`zz_nx029_3_consumer_text_erasure`，保证它们在其他完成触发器之后执行。nx038 的语义访问和演进、nx039、联系偏好这些触发器要先按原 id 找到这个人。这是语义访问测试中“清理中途写入的记录”发现的。

## 待定事项（需要产品决定，下面附推荐做法）

1. **退订、同意、拒绝记录要不要保留原话。**
   - 推荐：不保留原话，只保留“发生了退订、同意或拒绝”这件事、来源和时间，主体改成假名。0305 就是这样实现的。
   - 理由：抑制本身由擦除写入的 contact restriction 保证，原 id 仍然在那里；改成假名的记录用于统计和审计，不需要能回溯到个人。
   - 如果法务要求能够向这个人证明“曾经取得过同意”，就需要改为保留原 id。但这和擦除请求相冲突，请负责人决定。
2. **在途 outreach 收尾后的补擦**：已经实现，做法是在这一行收尾时由触发器补擦。
   - 它和其他行用的不是同一个假名。如果需要同一个，就要把假名存下来，而存下来的假名本身又可以关联回原 id，所以有意不这样做。
   - 另一种做法是让擦除停在 `waiting_unknown_effects`，等它收尾。那样要改擦除状态机，没有采用。
3. **墓碑的 `subject_key` 保存的是原 consumer id。**
   - 这是现有 NX-029 的做法：墓碑用来证明这个人已被擦除。0305 沿用了它，这里只是指出来，没有改动。
4. **`control.nexloop_consumer_erasures` 和墓碑保留原 consumer id。**
   - 和第 3 条同类：`runtime.nexloop_consumer_erasing` 依靠擦除请求行上的原 id，判断“这个人正在或已经被擦除”，从而永久拒绝再联系和释放限制。
   - 推荐：保留。改成假名就会失去这道永久抑制，这是擦除义务本身需要的最小残留。
5. **会话、事件中的 `principal_id`。**
   - 第二批和第三批已经把会话和 native web 事件中的 principal 改成假名。浏览器账号本身（身份层，EIOS 的 identity 表）不在本工作线范围内，账号是否随 Consumer 擦除一起删除，需要负责人决定。
   - 推荐：Consumer 擦除时停用这个客户账号，账号删除走身份层自己的流程。
6. **`runtime.nexloop_commitments` 登记行改为删除。**
   - 推荐：删除。Formal 承诺对象保留，并已去掉原 id；登记行只是 dedupe 和 intent 的副本。
   - 风险：如果以后某个读取方依赖“已擦除 Consumer 的承诺登记行”，它会读不到。现有测试没有发现这样的读取方。
7. **确定性哈希与墓碑 subject_key**：负责人 2026-10-21 已决定，见上一节“擦除后残留与决策”。

## NX-056：审批记录中的身份（ADR-039 §6.5，B1）

| 位置 | 内容 | 结论 | 说明 |
|---|---|---|---|
| `control.nexloop_approval_requests.body`（冻结请求体） | Consumer ID（例如 `consumer.erase` 的 `consumer_id`） | C | 擦除开始时，终结仍在等待或已批准、尚未消费的请求；完成时，把完整字符串值替换为本次擦除的假名（0331），只改 body |
| 同上 | 目标成员的 principal（`target_subject_field`，员工身份）、`invite_owner` 的被邀请人用户名 | D | 保留的员工身份：审批的审计需要知道处置的是谁。与 NX-058 片 D 的员工身份保留口径相同。目前没有员工身份的擦除路径 |
| 审批视图的 `target_member` 块 | 目标成员的用户名、标签、角色、状态；被邀请人用户名 | 不存储 | 读时渲染，不保存副本，也不写审计。将来员工身份被擦除或假名化（`pseudonym:` 开头的 principal、subject 或 username）时显示 `{"kind":"erased"}`，不回退到请求体原值 |

## NX-058 片 E：员工离职擦除（0349，片 E1）

设计：`docs/design/nx058-member-erasure.md` v2.4。owner 经 NX-056 审批（`workbench_member.erase_member`，目标状态 `{revoked}`）擦除全部 membership 都已撤销的成员（按 subject）。结构检查：`tests/test_member_erasure_coverage_pg.py`（凡带员工身份列的表，要么擦除写入，要么登记理由）。

| 表 | 处理 | 说明 |
|---|---|---|
| `control.nexloop_browser_accounts` | 改写 | 停用；口令哈希与历史清空；用户名（列与 payload）与邮箱（payload）改为 `erased-` 假名（E1=A）；会话 epoch +1 |
| `control.nexloop_browser_subjects` | 改写 | payload status 置为 disabled（0198、0334 的第二道保险） |
| `control.nexloop_browser_sessions`、`authz.nexloop_browser_token_realms` | 删除 | 该 subject 的会话及其令牌定位行 |
| `control.nexloop_workbench_invitations` | 删除 / 改写 | 该 subject 接受的邀请删除；它签发的保留，只清 `issuer_source`（E2） |
| `control.nexloop_workbench_reset_codes` | 删除 / 改写 | 指向该 subject 的码删除；它签发的只清 `issuer_source` |
| `control.nexloop_approval_requests` | 定点改写 | 它入职那份 `invite_owner` 的冻结体 `username` 改为同一假名（已终结的请求；ADR-039 R3 §8.7） |
| `control.nexloop_workbench_member_erasures` | 新表，永久 | 擦除台账（tenant、subject、principals、approval_id、执行人、时间）；只追加，判定 erased、already_erased、member_erased |
| `control.nexloop_deletion_ledger` | kind `workbench_member` | 恢复重放日志，cutoff 为擦除时间；365 天后照常清理；`runtime.nexloop_replay_deletions` 幂等重放 |
| `control.nexloop_browser_login_events` | 90 天后改写（片 E2，0350） | 登录事件里没有来源或用户名摘要（盘点见设计 §2.5）；失败登录事件 `result_snapshot` 里的明文 `username`、`verified_email` 在擦除满 90 天后由保留期服务（verb `member_login_clear`）改为账号上的 `erased-` 假名，其余键不动。**代价**：0023 的失败回执比对与请求时间早晚无关——清除执行之后，这些事件的同一 request_id 重放一律不再命中；清除只在擦除满 90 天后执行 |
| `control.nexloop_workbench_member_login_clearances` | 新表 | 清除记录（subject、时间、条数），只追加，判定幂等；恢复丢失后由下一轮重做 |
| `control.nexloop_workbench_grant_instances`、`control.nexloop_workbench_role_history` | 保留（E5） | 只含 principal 与摘要；撤销时已写撤销实例与 stop，擦除不做空写入 |
| 其余带员工身份列的表 | 保留 | 动作者的 principal（审计可追溯）、客户或服务的 principal，理由见覆盖测试 `STAFF_KEPT` |
