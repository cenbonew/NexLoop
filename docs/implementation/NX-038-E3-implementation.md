# NX-038 E3：语义访问逐次记录（实现说明，2026-10-20）

依据：NX-038 设计稿（`l4/nx038-design`）D2、§2.2、§12.5、§12.7 E3。D 项均已定案。

## 写入点

- 生产路径上的语义访问只有一个入口：`nexloop_eios/context_engine/semantic.py` 中 `SemanticAdapter.browse` 和 `resolve`。
- 适配器带 `recorder`（`SemanticAccessRecorder`）时，每次访问追加一条 `access` 记录，返回值多一个 `access_id`。
- 调用方报告实际用到了哪些 ref 时，再追加一条 `use` 记录（`recorder.used(access_id, refs)`），原来的 `access` 记录不会被修改。
- 不带 recorder 时，行为与之前完全相同。

## 记录内容（`runtime.nexloop_semantic_accesses`，临时迁移 0234）

- 记录：tenant、world、run_id、context_id；操作（browse 或 resolve）；查询种类；每个查询词项的 sha256（**不存原文**）；coverage、ambiguity；返回的定义 ref 与 revision；返回数量和未解析数量；recorded_at。
- 只追加：任何角色（包括 owner）都不能 UPDATE 或 TRUNCATE。只有下面「保留与删除」中的两条 owner 路径能 DELETE，而且必须在它们设置的事务内标志下进行。
- 表启用并强制 RLS，按租户隔离。

## 写入端口与权限

- 写入端口是 `authz.nexloop_semantic_access_record`：
  - 只有 `nexloop_api` 和 `nexloop_domain_worker` 能执行；
  - 协议为 `nexloop-semantic-access-v1`，用 HMAC 签名，密钥与 Context 读取相同；
  - tenant 和 world 取自已认证的服务身份，不使用 payload 里的值；
  - run_id 必须是同一 tenant、world 下已绑定的 Run；
  - `use` 记录只能引用同一 Run 的 `access` 记录，而且只能列出那次访问实际返回过的 ref。
- 读取端口是 `authz.nexloop_semantic_access_read`：
  - 只有 `nexloop_evo_reader` 能执行（D1）。本迁移在该角色不存在时创建它；登录凭据由 E2 负责配置；
  - 只返回 world `real`、调用方自己租户的记录，内容只有 ID、哈希、ref 和计数；
  - 该角色对表本身没有任何权限。

## 不拖慢 Run

- 写入失败（签名错误、Run 未知、数据库错误等）只会让 `recorder.failures` 加 1，并记一条 warning 日志。日志只写异常类型，不写内容。访问照常返回结果。
- 实测（开发机本地 PG，负载约 8，串行，`test_record_overhead_per_access`）：
  - 单次写入 0.31–0.40 ms；
  - 一次 browse 约 44 ms；
  - 增加的开销不到 1%。

## 保留与删除（NX-029）

- **保留**：新增保留类别 `semantic_access`，保留 90 天。
  - 保留设置升级为 v5，`deploy/configuration/retention.v1.json` 同步修改，迁移中写入的是同一份规范化文本；
  - 由 keeper 现有的清理流程执行；
  - 如果该类别有 hold，就什么都不删；
  - 每批删除都留一个 tombstone，只记计数。
- **Consumer 删除**：`control.nexloop_consumer_erasures` 的状态变为 `purging` 时，删除该 Consumer 名下所有 Run 的记录；变为 `completed` 时再删一次，以清掉删除进行期间新写入的记录。
  - 有 Consumer hold 时请求的状态变为 `completed_with_holds`，记录保留，与其他类别一致；
  - 删除时留 tombstone，原因为 `consumer_erasure`，只记计数。

## 迁移编号

- 本切片只有一个迁移 `0234_nx038_semantic_access.sql`，迁移里不引用自己的编号（发布者写作 `migration:nx038-e3`）。
- 合并时调度员可能重新编号，只需要改文件名和 catalog 中对应的条目。

## Run 路径接线（方案 A，调度员 2026-10-22，G-S6-1）

L5 准备 S6 stage 数据时发现缺口 G-S6-1：main 上只有测试调用 `browse/resolve`，真实 Run 不会留下任何语义读取记录。按设计（§2.2“只由 Context Engine 写入”），接入点放在 Context 组装，不放在 Host 工具。

- **调用点**：`ContextV6ArtifactProducer.assemble`（`nexloop_eios/context_artifacts.py`），兜底回复 Run 的组装同样经过这里。
  - 只在策略 `semantic.enabled` 打开时调用。内置的 `recent_plus_required` 是关闭的，所以默认不调用。
  - 调用内容：对当前用户消息做一次 `SemanticAdapter.browse`，`limit` 取策略的 `top_k`。recall 只走全文检索，不调用 embedding，不产生模型费用。
  - recorder 绑定当前 run_id；tenant 和 world 取自 Source 的服务身份，SQL 端口会再核对一次。
- **延迟上限**：`SEMANTIC_READ_TIMEOUT_SECONDS = 0.5`。读取放在单独的线程里执行，超时或出错只计数，并记一条只含异常类型的 warning，不影响组装，也不影响 Run。每个 Run 只读一次，来源核对失败后重新组装时不会重复记录。
- **资源上限**（L6 复核 2.1）：组装最多等 0.5 秒，但超时后后台的读取还会继续执行，所以另加了以下限制：
  - 读取使用**进程级的专用小连接池**：conninfo、角色和连接类与 Context 连接池相同，最多 2 个连接，每个连接设 `statement_timeout = 400ms`。数据库变慢时，查询会在数据库那一侧被终止，Context 连接池的连接不会被占用。
  - 使用一个进程级 executor，同时最多 2 个读取（`SEMANTIC_READ_CONCURRENCY`）。名额要等读取真正结束才释放，不是在组装停止等待时释放；超过上限的读取直接跳过，计入 `skipped`，不排队。
  - `ServiceSession` 是不可变的 frozen dataclass，可以跨线程共用。请求连接作用域是 ContextVar，executor 线程不继承它，所以后台线程不会碰主线程固定的连接。
  - **连接预算**（L6 复核 5.1，ADR-024）：这个连接池算在进程的连接预算里。
    - `connection-budget.v1.json` 的普通服务条目可以带 `semantic_read_pool_max`（0–4），`connection_budget.evaluate` 和 doctor 按“进程数 × 该值”计入总数；
    - 连接池大小由进程配置决定：message-relay 用 `--semantic-read-pool-max`，默认 0。为 0 时不开连接池、不做读取，计入 `skipped`。所以没登记预算的进程，即使策略打开了 `semantic.enabled` 也不会读取；
    - 兜底回复 Run 的组装（reply-guarantor）不做语义读取：同一条消息的消息 Run 已经记录过。所以它不需要这部分预算。
  - **打开 `semantic.enabled` 的前提**：
    1. 上面这些限制已经就位（本节）；
    2. 在 `connection-budget.v1.json` 里给 message-relay 登记 `semantic_read_pool_max`，同时在部署配置里给它加上相同的 `--semantic-read-pool-max`，doctor 预算检查通过（总数不超过 60）。现在 stage 的总数是 59，名额从哪里来由调度员决定；
    3. 打开后，用 `skipped`、`timeout`、`failed` 三个计数和 warning 日志观察。
- **端口调整**（迁移 `0313_nx038_e3_wiring.sql`，临时编号）：Context 是在 Run 激活之前组装的，此时 Run 还不在 `authz.nexloop_runtime_run_bindings` 里。所以 `authz.nexloop_semantic_access_record` 也接受这样的 Run：它的有效 Run 凭据由调用方这个 Source 在同一个 world 下签发。这样仍然绑定在服务端推导出的租户和 world 上。
- **局限**：
  - 结果**不放进 pack**，semantics 段保持为空，所以模型看不到定义，也不会写 `use` 记录；
  - access 记录的 `context_id` 为空，因为读取发生在 bind 之前。已核对 E4 归因（按 run_id 归组）和 `s6_stage_evolution.py`、`s6_stage_inputs.py`，都不依赖这个字段。
- **方案 B（后续任务）**：让定义条目进入 pack 的 semantics 段，SQL 核验定义类来源，并把 `use` 记录绑定到实际放进 pack 的 ref 上。

## 失败口径（负责人 2026-10-22，选 A）

- 一个 Run 如果语义没有命中，回复也没能完成，就算失败，沿用现有的 `attribution._failed`，不另加 miss 信号。
- **“回复没能完成”的判定**：这个消息 Run 的入站消息后来发起了兜底回复 Run（`authz.nexloop_message_fallback_issuances`，0110）。选它的理由：
  - 兜底 Run 只在“到了兜底时间，回复仍未落定”时才会发起，模型失败、超时、没有给出回复，都会走到这一步；
  - 每条消息最多发起一次，是现成可查的事实，不需要新增信号。
  - **以兜底时间为准**：模型在兜底时间之后才给出回复，这个 Run 仍然算“回复没能完成”。
  - 一条消息只对应一个消息 Run（`runtime.nexloop_message_routes` 的主键是 `(tenant_id, world, message_id)`），所以不会把同一条消息的多个 Run 都判为失败（L6 复核第 5 节）。
- **实现**：
  - `runtime.nexloop_evo_trace_runs`（0313，复制自 0233）给每个 Run 加上 `reply_fallback`；
  - `sample_from_run` 把它映射到 sample 已有的 `outcome` 字段（值为 `reply_fallback`），trajectory-sample 契约不变；
  - `_failed` 把 `outcome` 为 `error` 或 `reply_fallback` 的 sample 都算作失败。
- **测试**：`tests/test_nx038_e3_wiring_pg.py`。
