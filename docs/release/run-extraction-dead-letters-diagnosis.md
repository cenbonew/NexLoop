# 诊断单：stage 告警 `data_health_extraction_dead_letters`（值 5，自 2026-10-21 02:17Z 起持续）

只读诊断，不改数据。查询由调度员在 stage 上执行，结果交回 L3 判断。

## 1. 这个值是怎么算的

- **规则**：`deploy/configuration/alert-rules.v13.json:303`（v14、v15 相同），`data_health.extraction.dead_lettered > 0`，`for_seconds: 0`。
- **指标**：`runtime.nexloop_data_health`（0245）的 extraction 块，`dead_lettered` 等于 `runtime.jobs` 中这个租户、这个 world、`queue='claim-extraction'`、`status='dead_lettered'` 的行数。
  - **没有时间窗口，也不看是否已经处理**：只要死信行还在，就一直计数。
  - 终态任务按保留期类 `job_records` 要 365 天之后才会删除，所以一旦出现死信，在没有人处理的情况下，告警会持续一年。
- **一条任务怎样进死信**（0035/0036 的队列命令，0209 补了原因码）：
  1. worker（`claim_extraction_jobs.ClaimExtractionWorker.run_once`）以 `retry_wait` 结束，并且次数已到上限（`max_attempts` 默认 3，重试间隔 30 秒、60 秒……）。这时 `result.code` 为 `provider_<code>`（模型服务不可用、超时、限流）、`output_rejected`（模型输出不合格）或 `unexpected`（其他异常）。
  2. 任务租约过期没有续上（worker 崩溃或卡住），再次领取时次数已到上限。事件消息为 `lease attempts exhausted`，`result.code` 为 `runtime_lease_exhausted`（0209）。
  - `denied`（授权被拒）是 `failed`，不会进死信。
- **死信之后会怎样**：
  - 没有任何重试或复核入口。唯一能看到它的地方，是提取 feed 的 `backlog` 读取（0069）列出的最近 20 条。规则说明里“直到 owner 重试或复核”所说的路径并不存在。
  - **但内容可能已经自愈**：调度器每次为一个会话取“截至最新消息的最近 50 条消息”作为提取窗口。如果这个会话之后又有新消息，新任务会覆盖死信任务里的那些消息，它们的 Claim 会在新任务中提取出来（同一输入摘要按重放处理）。即使这样，死信行仍然留在表里，告警照样计数。

## 2. 只读诊断查询（stage，DATA_HOST）

使用执行单 `docs/release/run-nx056-approvals-enable.md` §0.3 的只读 owner 会话函数 `ro()`（bootstrap 经本地 socket 登录，在只读事务中 `set local role nexloop_owner`，设置租户后 `rollback`）。所有查询只有 `select`，只输出 id、代码、计数和时间，不输出消息正文、Claim 值或载荷正文。

**Q1：五条死信的明细**

```
ro "select j.job_id, j.created_at, j.updated_at, j.attempts, j.max_attempts,
  j.result->>'code' as result_code, j.error->>'code' as error_code,
  j.normalized_input->>'conversation_id' as conversation_id, j.normalized_input->>'through_sequence' as through_sequence,
  jsonb_array_length(j.normalized_input->'message_ids') as message_count, j.normalized_input->'message_ids' as message_ids,
  (select string_agg(e.status||'@'||to_char(e.created_at at time zone 'UTC','MM-DD HH24:MI:SS')||coalesce(':'||e.message,''),' > ' order by e.sequence)
     from runtime.job_events e where e.tenant_id=j.tenant_id and e.job_id=j.job_id) as events,
  (select count(*) from runtime.jobs s where s.tenant_id=j.tenant_id and s.world=j.world and s.queue='claim-extraction' and s.status='succeeded'
     and s.normalized_input->>'conversation_id'=j.normalized_input->>'conversation_id' and s.created_at>j.created_at
     and (s.normalized_input->'message_ids') @> (j.normalized_input->'message_ids')) as later_success_covering_all,
  (select count(*) from ontology.nexloop_claims c where c.tenant_id=j.tenant_id
     and c.source_message_id in (select jsonb_array_elements_text(j.normalized_input->'message_ids'))) as claims_from_these_messages
 from runtime.jobs j where j.tenant_id='$TENANT_ID' and j.world='real' and j.queue='claim-extraction' and j.status='dead_lettered'
 order by j.created_at"
```

**Q2：同一时段整个提取队列的情况**（判断是否是一次性的依赖故障）

```
ro "select date_trunc('hour',updated_at) as hour, status, result->>'code' as code, count(*)
 from runtime.jobs where tenant_id='$TENANT_ID' and world='real' and queue='claim-extraction'
  and updated_at between timestamptz '2026-10-20 18:00Z' and timestamptz '2026-10-21 12:00Z'
 group by 1,2,3 order by 1,2,3"
ro "select status, result->>'code' as code, count(*), min(updated_at), max(updated_at)
 from runtime.jobs where tenant_id='$TENANT_ID' and world='real' and queue='claim-extraction' group by 1,2 order by 1,2"
```

**Q3：死信之后是否还有新的失败**（判断是否仍在持续）

```
ro "select status, result->>'code' as code, count(*) from runtime.jobs
 where tenant_id='$TENANT_ID' and world='real' and queue='claim-extraction' and updated_at>timestamptz '2026-10-21 03:00Z'
 group by 1,2 order by 1,2"
```

- **括号必须保留**（stage 2026-10-26 首次执行时缺括号报 `operator does not exist: boolean -> unknown`）：`@>` 与 `->` 的优先级相同、从左到右结合，不加括号会先算 `... -> 'message_ids' @> j.normalized_input`，再对布尔值取 `->`。

所用的列都已核对（`runtime.jobs` 的 `world`、`queue`、`attempts`、`max_attempts` 由 0035 加入；`runtime.job_events`、`ontology.nexloop_claims.source_message_id` 见 0001、0066）。

## 3. 按结果判断

| Q1/Q2/Q3 的结果 | 判断 | 处理 |
|---|---|---|
| 五条集中在 10-21 02:17Z 前后几分钟，代码是 `provider_*` 或 `unexpected`，同一时段 Q2 也有大量 `retry_wait`；Q3 之后只有 `succeeded` | **一次性的历史故障**（当时某个依赖不可用：模型服务、签名命令时钟偏差等。10-20 21:31 的签名命令 TTL 热修 38d1a073 可以对照时间） | 内容层面：看 Q1 的 `later_success_covering_all` 和 `claims_from_these_messages`。已被后续任务覆盖的，只需把死信标记为已处理；没有覆盖的，需要重新入队提取。告警层面：见 §4 |
| 代码是 `output_rejected` | 模型输出对这些消息持续不合格。重试同样的输入多半还会失败 | 需要人看（不看正文的前提下，先核对提取器版本和这些消息的类型），然后决定重新入队还是放弃 |
| 代码是 `runtime_lease_exhausted` | worker 在处理这些任务时崩溃或超过 120 秒租约 | 查 worker 日志的同一时段；如果是超长会话导致超时，属于缺陷（窗口或租约需要调整） |
| Q3 在 03:00Z 之后仍然出现新的 `dead_lettered` 或大量 `retry_wait` | **仍在持续的缺陷** | 按代码定位，不是本诊断能结束的 |

## 4. 初步结论（在拿到 Q1–Q3 结果之前，仅凭代码）

- **告警口径不对，同时缺少处理入口，这两点不论结果如何都成立**：
  - 指标数的是“历史上所有仍在表里的死信”，而不是“仍待处理的死信”。
  - 系统没有提供重试或标记已处理的入口。
  - 所以哪怕是一次性故障、内容早已自愈，告警也会持续 365 天。
  - 现在的 5 条与 10-21 02:17Z 起一直报警、数值不再变化的现象一致：故障之后没有新的死信，旧的也没人处理。
- 根因（依赖故障、输出不合格还是租约耗尽）要看 Q1 的 `result_code`、`events` 和 Q2 的时段分布。

## 5. 修法建议（stage 结果确认后已实施：迁移临时编号 0355，告警规则 v17，测试 `tests/test_extraction_dead_letters_pg.py`）

1. **处理入口**：新的受治理人类 Action `claim_extraction.dead_letter_resolve`（owner），对一条死信二选一：
   - `requeue`：把这条任务覆盖的 feed 行放回待处理（`task_id` 置空），调度器按正常窗口重新入队提取；
   - `dismiss`：写明原因，标记为不再处理。
   - 处理记录写入一张只追加的新表 `runtime.nexloop_claim_extraction_dispositions(job_id, disposition, reason, decided_by, decided_at)`。死信行本身不改，保持原样作为证据。
   - 工作台数据健康页的提取一栏增加列表和这两个按钮。
2. **告警口径**：`dead_lettered` 改为只数“没有处理记录，并且没有被同一会话之后的成功任务完整覆盖”的死信（`unresolved_dead_letters`），原值保留为 `dead_lettered_total` 供参考。规则改用新指标，告警规则加一个版本。
3. **过渡处理**（不需要等代码）：如果 Q1 显示五条都已被后续任务覆盖（`later_success_covering_all > 0`），那么这次告警本身就是口径问题，内容不缺；可以先在告警页静默它（`silence_alert`，带期限），等第 1、2 点上线。
4. **迁移**：0355 新表、Action 与 handler、指标重定义（`runtime.nexloop_data_health` 的重新包装，登记复制件守护）；随新 Action 的整套同步（business-actions、capabilities、catalog names、workbench-roles、PROFILES、goal_controls、OPERATIONS 与相关测试计数）。
