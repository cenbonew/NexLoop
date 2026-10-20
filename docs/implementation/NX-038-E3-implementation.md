# NX-038 E3：语义访问逐次记录（实现说明，2026-10-20）

依据：NX-038 设计稿（`l4/nx038-design`）D2、§2.2、§12.5、§12.7 E3。D 项均已定案。

## 写入点

- 生产路径上的语义访问只有一个入口：`nexloop_eios/context_engine/semantic.py` 中 `SemanticAdapter.browse` 和 `resolve`。
- 适配器带 `recorder`（`SemanticAccessRecorder`）时，每次访问追加一条 `access` 记录，返回值多一个 `access_id`。
- 调用方报告实际用到了哪些 ref 时，再追加一条 `use` 记录（`recorder.used(access_id, refs)`），原来的 `access` 记录不会被修改。
- 不带 recorder 时，行为与之前完全相同。

## 记录内容（`runtime.nexloop_semantic_accesses`，临时迁移 0231）

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

- 本切片只有一个迁移 `0231_nx038_semantic_access.sql`，迁移里不引用自己的编号（发布者写作 `migration:nx038-e3`）。
- 合并时调度员可能重新编号，只需要改文件名和 catalog 中对应的条目。
