# 上下文来源为空（required_source_unreadable）与失败 Run 无原因（2026-10-16）

本文只用角色名。所有核对都是只读查询，在 DATA_HOST 的一次性 backend 容器里以 bootstrap 只读事务执行，形态同 run-model-smoke §2.2。分支为 `l2/context-sources`，基于 main 0602c88。

**类别标注**：**B** 表示需负责人确认。

## 1. 根因

stage 的 relay Source 没有逐对象的配置授权，只有两条派生授权：0142 的 relay_source_read_rule 派生 Consumer READ，0077/0102 的 message_read_rule 派生 Message READ 和 Conversation READ。以下四个原因叠加，导致 pack 中 open_work 缺 Consumer、evidence 缺 Conversation，并出现 `required_source_unreadable`：

1. **Context 不认派生授权**：`context_engine/sources.collect` 取 Consumer 的 `read_proof` 时只接受配置授权，不认 0142 派生的 Consumer READ。
2. **消息派生依赖配置授权**：`message_read.DerivedEvidenceReads._derived` 要求 Source 对 Consumer 有**配置的** READ，才派生 Message 和 Conversation READ。stage Source 没有这项配置，所以派生链在第一步就断了。
3. **stage 规则字段不全**：`stage-principals.v1.json` 中 relay_source 的 message_read_rule 字段不含 `conversation_id`。按 0102 的规定，不含该字段就不派生 Conversation READ。
4. **pack 不带原始对话轮次**：`conversation_items` 没有被使用，v6 校验器 `authz.nexloop_context_v6_source_in` 也不支持 `Message/<id>` 条目，所以即使授权齐全，第二条消息的 pack 里也没有上一轮对话。

## 2. 修复

| 位置 | 改动 |
|---|---|
| `context_engine/sources.py` | 新增 `consumer_read_proof`：配置授权被拒时，回退到读取器的派生授权（`_authority`）。新增 consumer_state 条目（`Consumer/<id>`，带 revision，不带属性）。新增对话轮次视图，通过 `authz.nexloop_read_conversation_turns` 读取，只放当前消息**之前**的轮次（id 不同，且 sequence 更小），条数为 2×recent_turns，限制在 2..64 |
| `message_read.py` | 派生 Consumer READ 时改用 `_authority`，同时接受配置授权和 0142 派生授权 |
| `context_artifacts.py` | 把当前消息的 id 和 sequence 传给 collect |
| `deploy/authorization/stage-principals.v1.json` | relay_source 规则字段改为 `accepted_at, actor, body, conversation_id, sequence` |
| 迁移 **0201** `context_conversation_turns`（临时编号） | 新增 `authz.nexloop_read_conversation_turns`：仅限 api/domain_worker 调用，校验方式同 claims 读取；排除已擦除消息和未被接受的出站消息。重定义 v6 校验器，新增 `Message/<id>` 分支：section 为 evidence，subsection 为 conversation，revision 取 sequence，proof 目标为 `Conversation/<conv>`。`catalog.json` 已加条目，`versions.lock.json` 的 `bootstrap_revision` 改为 0201 |
| Host 与派发器（Run c0bb25d7，见 §5） | 失败的 Run 写入 `failure_code`（`runtime_tool_error` 或 `runtime_task_failed`）和 `failure_detail`（只写代码，不写文本） |

没有直接改业务表。授权变化全部经由治理配置（register）和迁移完成。

## 3. stage 应用步骤（B）

1. **迁移**：先做备份校验，再走迁移门槛应用 0201。合入 main 时由调度员按 catalog 末尾重编号。
2. **重跑注册**：重跑 `stage_secrets.py register`。它通过 `_apply_extra_facts` 以受治理的可信配置更新 message_read_rule 的字段，是幂等的。不要手工写授权表。
3. **发布镜像**：发布新的 backend 和 agent-host 镜像，镜像 digest 要与测试过的发布清单一致。重建时，agent-host 与 agent-link-relay 一起重建。
4. **验证**：同一会话中连续发送两条消息，然后按 §4 只读确认。

## 4. 只读确认 pack sections 非空

```
select b.created_at,
       b.pack_text::jsonb -> 'insufficient' as insufficient,
       (select count(*) from jsonb_array_elements(b.pack_text::jsonb -> 'items') i
         where i ->> 'subsection' = 'conversation') as conversation_items,
       (select count(*) from jsonb_array_elements(b.pack_text::jsonb -> 'items') i
         where i ->> 'section' = 'consumer_state') as consumer_state_items
  from runtime.nexloop_context_artifact_bindings b
 order by b.created_at desc limit 5;
```

第二条消息对应的那一行应满足以下三点：

- `insufficient` 为空数组；
- `conversation_items` ≥ 1（上一轮对话）；
- `consumer_state_items` ≥ 1。

如果 pack 的 JSON 路径与上面不同，以 `jsonb_pretty(pack_text::jsonb)` 的实际结构为准，判定标准不变。

## 5. Run c0bb25d7（failed，工具调用 1 次、模型调用 2 次、无原因）

- **与空上下文的关系**：很可能相关。模型拿不到 Consumer 状态和历史轮次，只能盲调一次工具，工具返回错误（例如 `outside_catalog_terms` 这类业务码）。Host 遇到工具结果 isError 就把 Run 判为 failed，而原先的 inspection 只对授权类失败给出 `failure_code`，所以任务结果里没有原因。这一判断尚未在 stage 上验证，见下文的只读查询。
- **修复**：
  - `pi-runtime-adapter` 记录工具错误：优先用结果 JSON 中的 `code`，否则记为 `tool_error`；也记录失败任务的消息，但只接受形如 `^[a-z][a-z_]{0,63}$` 的代码。
  - inspect 输出 `failure_code` 和 `failure_detail`，授权码仍然优先。
  - `runtime_dispatch` 接受这两个新码。`failure_detail` 也必须匹配同一正则，然后写入任务结果。错误文本不会进入结果、日志或提示。
- **只读确认**：部署后，新的失败 Run 应在任务结果中带 `failure_code` 和 `failure_detail`。c0bb25d7 是存量记录，不会回填；如需确认，可在 agent-host 侧只读查看该 Run 的工具结果码。

## 6. 测试

- **新增** `tests/test_context_sources_stage_pg.py`：覆盖 stage 规则字段；一个只有派生授权的 Source 能读取所有必需来源（未注册的 consumer 仍判为 insufficient）；端到端场景下，第二条消息的 pack 带上一轮、consumer_state 非空、insufficient 为空。
- **更新**：`test_stage_register_pg`、`test_context_envelope_reuse_pg`（proof 到期时间依赖当前时刻，比较时归一化 decision ref）、`test_context_v6_pg`（预算省略项可以包含 Consumer）。另有 Host 的两个 vitest 文件。
