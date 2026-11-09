# NX-062 M5a：机器授权事实、机器 Action 授权断言与审批排除

- **依据**：[NX-062-M5-design.md](NX-062-M5-design.md) §2、§5、§9 的 R1、R6，以及“测试写能力只在夹具里登记”的要求。
- **迁移**：`0441_nx062_machine_authority.sql`（临时号），`versions.lock.json` 的 bootstrap_revision 改为 0441。
- **范围**：本片不新增路由、Action 或契约，机器仍然没有任何写入路径。M5b 的机器核心会调用这里的断言。

## 交付

1. **机器授权事实在快照时从授权表派生**：`authz.nexloop_machine_authority_fact_snapshot(prefix, digest, kind, key)`，只授权给 `nexloop_api`。
   - 六类事实：subject、actor、machine_authentication、grants、scope、policies。种类、key 和内容写在迁移头注释里。
   - 每类事实只能取本密钥、本主体名下的；别人的 key 或未知种类一律 42501。
   - grants 事实包含最新授权行的 revision（R1），撤销后用相同类型重新授权，hash 也会改变。
   - 事实里不出现密钥摘要。
2. **机器 Action 授权断言**：`authz.nexloop_machine_assert_action_authority(prefix, digest, world, claims)`，不授权给任何应用角色。
   - 要求六类事实齐全，逐条重算 `record_hash` 并比对。
   - hash 对得上还不够，事实本身也必须是允许的：主体和 actor 是 active，授权是 active，scope 里包含该能力，允许表当前行是 active 的 write 且不在禁止名单里。
   - claims 的有效期最长 30 秒。
   - 对机器主体行、密钥行、该能力的最新授权行加 `FOR SHARE`。授权和撤销的 handler 对主体行加 `FOR UPDATE`，所以并发撤销会和断言串行（R1）。
3. **不存储机器事实行**：`authz.nexloop_authority_facts` 上加触发器，entity_key 里出现机器主体表的 principal 或 subject 就拒绝（R1）。
4. **审批排除**（C8、R6，都按机器主体表判定，不读 payload）：
   - **审批决定**：`control.nexloop_approval_decisions` 上加 BEFORE INSERT/UPDATE 触发器，approve 或 reject 的决定行中，actor 是机器 principal 或 subject 就返回 42501。
     - 与设计稿的差异：设计稿原计划包装审批决定的 handler。但这个 handler 在受治理注册表里是按 oid 登记的，改名包装后注册表仍然指向旧函数，只能用 `create or replace` 重写原函数，而 copy guard 不接受整段重写。改在决定表上判定，任何写入决定的路径都绕不过去。
   - **solo 判定**：`control.nexloop_approval_solo_effective` 改名加包装，请求人是机器主体时返回 false。
   - **可审批人员名单**：`control.nexloop_approval_eligible_subjects` 改名加包装，结果里剔除机器主体，机器也就不计入法定人数。
   - 两个包装都接手原函数的 EXECUTE 授权；改名后的原函数不授权给任何角色。

## 测试

`tests/test_machine_authority_pg.py`，真实 PG：

- 事实派生：六类齐全；越权的 key 和未知种类返回 42501；撤销后按相同类型重授，hash 改变；数据库里没有机器事实行，直接插入会被拒。
- 断言：
  - 合法 claims 通过；`nexloop_api` 不能执行断言函数。
  - 六个 hash 逐个篡改都被拒；缺事实、重复事实、world、principal、client、credential、operation 不符，以及过期或有效期过长的 claims 都被拒。
  - 换一把 scope 里没有该能力的密钥，事实照抄也被拒；授权撤销、允许表退役后，事实照抄也被拒。
- 并发撤销，用两个连接：撤销事务持有主体行不提交，断言会阻塞等待；撤销提交后，断言得到 42501。
- 审批：
  - 机器作为 actor（subject 或 principal）的 approve 或 reject 决定行被拒，人的决定行照常写入。
  - 在回滚的事务里把被包装的原函数替换成桩：可审批名单剔除了机器主体；solo 对机器主体（subject 和 principal 都试）返回 false，对人返回 true。
  - 改名后的原函数不授权给任何角色。
- 守护测试：跑完全部迁移后，允许表里没有任何 write 能力，受治理注册表里没有 `test.` 开头的能力；部署目录的 manifest 和迁移文件里不出现 `test.machine`。
- 验证用的写能力 `test.machine.write_probe` 由夹具在测试 PG 里登记：handler 放在 runtime schema、属主是 nexloop_owner；授权行在 M5b 扩展授权守卫之前用 replica 模式写入。

变异检查（逐项删掉后跑本文件，都是 1 failed）：
- 事实行守卫；
- grants 事实里的 revision；
- 主体行的 FOR SHARE；
- scope 拒绝；
- 授权状态拒绝；
- 决定行触发器；
- solo 包装；
- 名单包装。

全局守护测试与回归，共 307 passed：
- 守护：db_boundary、tenant_fk、system_catalog、copy guard、bootstrap、三项擦除覆盖、nx041 sim roles 和 pi_run、claim_store_pg；
- 审批：NX-056 审批全套、budget、instance、outreach；
- 机器与注册：机器相关的全部测试、stage_register。

## 留给 M5b

- 授权守卫（`authz.nexloop_machine_grant_guard`）目前只接受读取动词，M5b 要把它扩展到允许表里 kind 为 write 的能力。
- 0327 审批门用 `nexloop_approval_subject_of` 推导请求人；机器主体没有成员身份，推导结果为空。M5b 需要给机器请求一个请求人 subject（机器主体表的 subject_id），这样待审批路径才走得通。

## 合入记录

- 批次 156 合入 main：origin/main = 23f3e2b9（本地 CI 8994 passed / 0 failed，catalog head 0441），2026-11-10 由调度员通知。按 L10 的线路规则，`planning/` 里的任务证据由调度员写入，本行只在这里记录。
