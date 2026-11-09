# NX-062 M5b：机器写入机制（prepare / commit / 查询与 0124 机器分支）

- **依据**：[NX-062-M5-design.md](NX-062-M5-design.md) §3、§4、§5，以及 §9 的 R2–R5 和“审批通过后的执行重跑全部检查”。
- **迁移**：`0442_nx062_machine_writes.sql`（临时号），`versions.lock.json` 的 bootstrap_revision 改为 0442。
- **范围**：
  - 本片只做写入机制，不登记任何写能力，生产中机器写入仍然是关闭的（M5a 的守护测试继续成立）。
  - 对象备注（Action、表、工作台入口，V-1–V-4、W1–W3）和 HTTP 路由、契约放到 M5c。
  - Python 端只有代签模块 `nexloop_eios/machine_writes.py`，还没有接到路由上。

## 交付

1. **写能力注册表** `control.nexloop_machine_write_capabilities`：append-only。
   - 每个写能力登记它的对象类型和参数名。
   - 参数名不得包含任何身份字段（tenant_id、principal_id、client_id、world 等）。
   - 只能登记 `agent_or_human` 的受治理能力。
2. **授权守卫扩展到写能力**：新增 `control.nexloop_machine_grantable`（是读取动词，或是已登记、允许表当前行为 write 的写能力）。
   - 授权守卫触发器和授权 handler 的“必须是读取动词”检查改为调用它。
   - 两者都按 oid 被引用，所以原地重定义，在 copy guard 里登记了唯一的改动。
   - `control.nexloop_machine_verb_types` 改名加包装，写能力的类型取自注册表。
3. **机器请求的审批请求人**：`control.nexloop_approval_subject_of` 改名加包装，机器主体返回它的 subject_id，人照旧。
   - 0327 审批门有了请求人，就能为机器请求开审批。
   - M5a 的三处排除保证机器不能批准、不计入名单、不能 solo。
4. **意图表** `runtime.nexloop_machine_intents`：
   - 只存参数摘要和 prepare 摘要，不存参数和正文（C6）。
   - phase 取值 prepare / pending_approval / committed / refused（R4）。
   - result 只记结果码、outcome_id、approval_id。
5. **0124 机器分支**（C7、R2）：
   - 迁移开头用 `pg_proc` 核对现行入口函数体的哈希等于 0348 版本，不等就中止。
   - 入口改名为 `_before_machine`，原名换成包装：`nxm:<prefix>:<digest>` 形状的请求交给 `authz.nexloop_machine_governed_core`，其他原样委托。改名后的入口不授权给任何角色，对机器摘要仍由 service 身份拒绝（C1）。
   - 机器核心依次做：
     1. 核对签名命令。命令绑定 client、intent、prepare 摘要、world（R5），有效期 ≤30 秒。
     2. 锁意图行，核对 prepare 摘要，并用请求体里的参数重算摘要。
     3. 业务意图 ID 为 `sha256(tenant, client, intent)`（R3）。
     4. 调用 M5a 的机器授权断言（身份、scope、允许表、最新授权，加锁）。
     5. 拒绝 `human` 能力。
     6. 核对 Action 定义契约。
     7. 在同一事务里建立或续上认领行。
     8. 进入审批门、调用 handler、消费审批。这一段是 0348 原文，未改一字。
     9. 写结果、更新意图行、写审计。
     10. 提交前再断言一次授权。
6. **prepare / commit / 查询**：`authz.nexloop_machine_prepare`、`authz.nexloop_machine_commit`、`authz.nexloop_machine_intent`、`authz.nexloop_machine_write_action`，只授权给 `nexloop_api`。
   - **prepare**：
     - 依次核对：scope、允许表当前行为 write、已授权、未在禁止名单；
     - 有外部效应却没有审批策略的能力返回 `approval_policy_required`（C5 2.5）；
     - 参数名只能是已登记的；type 必须是已授权的类型；对象必须可见，否则 404；
     - 有效期 10 分钟。同一意图同参数重复 prepare 返回同一摘要，参数不同返回 409。
   - **commit**：
     - 已结束的意图（committed 或 refused），摘要相同就返回首次结果；不同返回 409。这一步在判过期之前（R4）。
     - 然后才判 prepare 过期（410）。
     - 授权被拒（42501）时，handler 的写入回滚，意图记为 refused / `not_permitted`。
   - **待审批**：返回 pending_approval 和 approval_id。批准后客户端用同一 intent 再 commit 一次，机器核心重跑全部检查后才执行（L6 对 098caeac 的要求）。
7. **API 代签**（R5）：`machine_writes.commit` 在一个事务里依次读身份、Action resource、六类事实，用数据库时钟加 20 秒作为有效期，用权威签名密钥签名，然后调用 `authz.nexloop_machine_commit`。签名密钥只在 API 进程里。
8. **错误码**：参数错误 22023，不可见或不存在 P0002，权限 42501，冲突或摘要不符 `NX409`，prepare 过期 `NX410`。M5c 的 HTTP 层再映射为 422、404、403、409、410。

## 与设计稿的差异和发现

- **待审批路径在生产中目前走不到**：NX-056 的审批策略守卫只接受 `human` 能力，而机器只能用 `agent_or_human` 能力，所以现在没有任何机器请求会进入审批。测试里绕过守卫登记策略，证明了路径可用（待审批 → 批准 → 再 commit 执行；审批期间撤销 → 拒绝）。第二期的效应能力需要放宽这个守卫，到时另行设计。
- **对象备注**：按调度员的允许拆到 M5c。
- **签名命令里 client、world 的绑定**：在机器核心里和 M5a 授权断言里各核对一次。变异检查只删掉核心里那一处时，断言仍会拒绝，所以测试不会变红；这是有意保留的两道检查。

## 测试

`tests/test_machine_writes_pg.py`，真实 PG，走 API 自己的模块、`nexloop_api` 连接池和真实的权威签名密钥。测试写能力 `test.machine.write_probe` 只在夹具里登记，它的 handler 记录每次调用。

| 测试 | 内容 |
|---|---|
| prepare → commit → 查询 → 重放 | handler 只被调用一次，传入的主体类型是 `machine`；重放返回首次结果；认领行的意图 ID 是 R3 的推导；意图行不含参数；审计只有 ID、结果码和 key_prefix |
| prepare 拒绝 | 身份字段、未登记参数、未授权类型、不可见对象、缺 type、过短的意图 ID、读取动词、scope 里没有该能力的密钥、授权已撤销、有效应却无审批策略 |
| commit 摘要、过期与顺序 | 改参数（包括末尾多一个空格）返回 409；假摘要返回 409；未知意图返回 404；过期返回 410；已提交后再过期仍返回首次结果（R4），再次 prepare 返回 409 |
| commit 时重跑授权 | prepare 后撤销授权、允许表退役，commit 都是 refused / not_permitted，handler 没有被调用 |
| human 能力 | 绕过守卫把能力改成 human（模拟错误登记），commit 被拒 |
| 签名命令与入口 | 用真密钥重签、只改 client、intent、prepare 摘要、world 中的一项，都被拒；假签名、形状错误的 nxm 都被拒；`nexloop_api` 和 owner 用机器摘要直调 `_before_machine` 都是 42501；真命令仍可提交 |
| 审批后重跑 | 进入 pending_approval，批准后再 commit 执行；另一条在审批期间撤销授权，再 commit 被拒，handler 没有被调用 |
| 入口一致性 | `_before_machine` 还原名字后的哈希等于 0348 基准；0348 的审批门到消费这一段原文包含在机器核心里；只有包装引用 `_before_machine`；只有 `_before_machine` 和机器核心动态调用 handler |
| 授权对象 | 改名后的函数和机器核心不授权给任何角色；包装接手了 `nexloop_api`；四个新入口只授权给 `nexloop_api`；机器的审批请求人是它的 subject |

变异检查，逐项删掉后跑本文件，都变红：
- 核心里的参数重算摘要；
- 两次授权断言；
- human 拒绝；
- 判断顺序改成先判过期（不看 phase）；
- prepare 的 scope 检查。

（签名绑定的那一项见上文“差异和发现”。）

全局守护与回归一轮：
- 守护：db_boundary、tenant_fk、system_catalog、copy guard、bootstrap、三项擦除覆盖、nx041 sim roles 和 pi_run、claim_store_pg；
- 审批：NX-056 审批全套、budget、instance、outreach；
- 机器与注册：机器相关的全部测试、stage_register、workbench_actions_http。
