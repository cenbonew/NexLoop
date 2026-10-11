# NX-052 评审稿：租户行 `FOR UPDATE` 改为 `FOR NO KEY UPDATE`

状态：评审稿，未实现。按调度员 2026-10-12 的安排，它是第 3c 批（会话与消息表的租户外键）和第 5 批的前置条件；评审通过后另开迁移实现。

## 1. 问题

租户外键的每次检查都会对 `control.nexloop_tenants` 中对应的父行加 `FOR KEY SHARE`。目前有两类函数在租户行上显式持有 `FOR UPDATE`，`FOR UPDATE` 与 `FOR KEY SHARE` 冲突。所以在配置发布或初始身份创建的事务提交之前，该租户所有带外键的写入都要排队。

本地测量（`scripts/load/tenant_fk_cost.py` 的锁探针，持锁 0.3 秒）：

| 表 | 无外键 | 有外键，持 `FOR UPDATE` | 有外键，持 `FOR NO KEY UPDATE` |
|---|---|---|---|
| `control.nexloop_commercial_connectors` | 0.2 ms | 307 ms | — |
| `ontology.nexloop_merge_configurations` | 0.2 ms | 312 ms | 0.7 ms |
| `ontology.nexloop_extraction_runs` | 0.9 ms | 304 ms | 0.6 ms |

第 3c 批的表位于消息受理的 ACK 前路径上。如果不改，配置发布期间该租户的消息受理会被阻塞，时长等于配置发布事务的耗时。

## 2. 改动点

只有两个函数需要改，其余 `FOR UPDATE` 都在这两个函数被替换掉的旧版本里。

| 函数 | 当前定义 | 位置 | 语句 | 作用 |
|---|---|---|---|---|
| `control.nexloop_configure_manifest` | 0142（复制自 0050、0077、0084、0086、0102 的同名函数） | 0142 第 53 行 | `select authority_revision into v_revision from control.nexloop_tenants where tenant_id=p_tenant for update` | 让同一租户的配置发布串行；读取期望的 authority_revision；随后更新 status（第 59 行）和 authority_revision（第 230 行） |
| `control.nexloop_initial_identity_create` | 0050 | 0050 第 246 行 | `perform 1 from control.nexloop_tenants where tenant_id=p_tenant and status='active' for update` | 让同一租户的初始身份创建串行，并保证创建期间租户保持 active |

改法：新开一个迁移，用 `create or replace` 复制两个函数的当前定义，只把这两处改为 `for no key update`，属主、`search_path` 和授权都不变。已发布迁移的校验和不改。

两个函数都不改 `tenant_id`，也不删除租户行；在租户行上只更新 `status` 和 `authority_revision`，都不是键列。所以函数自己的 `update` 取的本来就是 `FOR NO KEY UPDATE`。把显式锁降到同一级别，不会改变函数在提交时持有的锁强度。

## 3. 锁兼容表（PostgreSQL 行级锁）

下表回答“请求的锁是否需要等待已持有的锁”，“等”表示冲突。

| 请求 ＼ 已持有 | FOR KEY SHARE | FOR SHARE | FOR NO KEY UPDATE | FOR UPDATE |
|---|---|---|---|---|
| FOR KEY SHARE（外键检查） | — | — | — | **等** |
| FOR SHARE（授权路径，0005 至 0061 共 20 余处） | — | — | 等 | 等 |
| FOR NO KEY UPDATE（改动后的两处，以及普通 update） | — | 等 | 等 | 等 |
| FOR UPDATE（改动前的两处） | **等** | 等 | 等 | 等 |

结论：
- 改动后，配置发布或初始身份创建**不再阻塞外键检查**，这正是改动的目的。
- 两次配置发布之间、配置发布与初始身份创建之间仍然互斥，因为 `NO KEY UPDATE` 与 `NO KEY UPDATE` 冲突。串行语义不变。
- 授权路径的 `FOR SHARE` 仍然要等配置发布提交，所以授权不会读到发布中途的 authority_revision。语义不变。
- 新租户的首次发布：`select … for update` 找不到行，就走 `insert` 分支，此时没有任何锁可言；两个并发的首次发布中，后一个会因主键冲突失败。这与锁的强度无关，行为不变。

## 4. 风险

- **外键检查不再等待配置发布**：外键只关心租户行是否存在，配置发布不会删除租户行，也不会改 `tenant_id`，因此外键检查的结论不受发布影响。授权所依赖的 `status` 与 `authority_revision`，仍由授权路径的 `FOR SHARE` 和身份快照保证。
- **停用租户**：manifest 把 `status` 改为 suspended，提交之前已经开始的外键写入可以成功，这与改动前“排队到提交之后再成功”的结果相同。外键从来不检查 status，是否停用由授权路径判断。
- **将来的风险**：如果以后有代码删除租户或改 `tenant_id`，那段代码会取 `FOR UPDATE`，仍然会和外键检查冲突。这是正确的行为，本次改动不涉及。

## 5. 并发场景的测试设计

新增 `tests/test_tenant_row_lock_pg.py`，每个场景用两个连接：A 在事务中调用函数后暂不提交，B 设置 `lock_timeout='200ms'` 后执行下列操作。B 用 `55P03`（lock_not_available）判定“被阻塞”。

| # | A 持有（未提交） | B 的操作 | 改动前预期 | 改动后预期 |
|---|---|---|---|---|
| 1 | `configure_manifest`（已有租户、期望修订号正确） | 向第 2 批的外键表插入一行（例如 `control.nexloop_browser_rate_policies`） | 55P03 | 成功 |
| 2 | 同上 | 对同一租户再次调用 `configure_manifest` | 55P03 | 55P03（串行保持） |
| 3 | 同上 | 授权路径读取：`select … from control.nexloop_tenants where tenant_id=… for share`（与 0005 等的写法相同） | 55P03 | 55P03（语义保持） |
| 4 | `initial_identity_create` | 向外键表插入一行 | 55P03 | 成功 |
| 5 | `initial_identity_create` | 并发的 `configure_manifest` 把 status 改为 suspended | 55P03 | 55P03（创建期间租户保持 active） |
| 6 | `configure_manifest` | 对另一个租户插入外键行 | 成功 | 成功（对照组） |
| 7 | 无 | 两个首次发布并发、同一新租户 | 一个成功，另一个 40001 或 23505 | 不变 |

另外：
- **证明改动前确实冲突**：测试先在改动前的函数定义上断言场景 1 和场景 4 被阻塞，再应用新迁移、断言改为成功。实现时用 bootstrap 到迁移前一版本的 catalog，或者对比两份函数定义来做，以证明测试本身能区分两种状态。
- **函数正文只改了锁**：断言新函数的 `pg_get_functiondef` 与旧定义相比，除了锁这两处之外完全一致。做法是比较替换后的文本，防止复制函数时引入其他改动。
- **时延**：用 `tenant_fk_cost.py` 的锁探针（`lock='no_key_update'`）记录改动后的等待时长，判据是小于持锁时长的 10%。

## 6. 落地顺序

1. 本评审通过后，实现上述迁移和测试，作为单独的分支提交，进入一次集成。
2. 之后再做第 3c 批，并用 NX-031 矩阵并发跑“配置发布 + 消息受理”，测量端到端 p95，判据是增加不超过 5%。
3. 第 5 批（`ontology.objects`、`runtime.jobs`、`ontology.events`）排在最后。
