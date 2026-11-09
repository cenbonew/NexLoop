# NX-062 M5c：对象备注

- **依据**：[NX-062-M5-design.md](NX-062-M5-design.md) §11（负责人 2026-11-10 选 A），验收项 V-1–V-4、W1–W3，以及“审计不记正文”。
- **迁移**：`0443_nx062_object_notes.sql`（临时号），bootstrap_revision 改为 0443。
- **分三次提交**：
  - M5c-1（本提交）：SQL 部分与清单；
  - M5c-2：机器 HTTP 路由与契约；
  - M5c-3：工作台备注入口（V-1、V-4）与运行单。

## M5c-1 交付

- **表** `runtime.nexloop_object_notes`：
  - 租户外键，RLS 并强制 RLS；任何应用角色都没有直接读写权限。
  - 只追加不修改。例外只有两个，都由专用函数在事务内打标记后执行：擦除改写（只改正文、作者和擦除标记），以及随对象删除。
- **Action `nexloop.object.add_note:1`**：受治理能力 `object.add_note`，`agent_or_human`，handler `runtime.nexloop_governed_object_note`。已登记进允许表，作为唯一的写能力；也登记进写能力注册表（类型 Commitment、CommercialRecord，参数 type_name、object_id、text）。
  - 正文去掉控制字符和双向控制符、去掉首尾空白后，长度须在 1–2000 字符。
  - 人：当前工作台角色是 owner 或 operator。
  - 机器：机器主体必须是 active，M5b 核心已经核对过密钥、允许表、授权和类型。
  - 对象可见性与机器读取、工作台相同：对象存在、类型正确、不属于擦除中或已擦除的消费者；否则 404。
  - note_id 由请求推导，重放返回同一条。
  - 不改对象，不改 revision。
- **W3 机器上限**：
  - 新表 `control.nexloop_machine_note_limits`，新清单 `deploy/configuration/machine-note-limits.v1.json`（每个客户端每天 200 条，每个对象每天所有机器合计 20 条），由 `python -m nexloop_eios.machine_access publish-note-limits` 经 configurator 发布。
  - 与设计稿的差异：没有放进 M3 的限速清单，因为 M3 的发布函数已经发布，不改它。
  - 超限或策略缺失时返回 `NX429`；意图保持 prepare，不被消耗。人不受这个上限约束。
- **审计**：`object.note.add` 只记 note_id、作者类型、作者 principal、client_id、类型、对象和 world，不记正文、正文长度或摘要。
- **V-3 擦除与删除**：
  - 消费者擦除（离开 pending_confirmation 时）：改写挂在该消费者对象上的备注，以及正文里写了该消费者 ID 的备注。
  - 成员擦除：作者为该成员的备注，作者和正文都改写。
  - `ontology.objects` 上的 AFTER DELETE 触发器：对象删除时，备注随之删除。
- **工作台读取函数** `control.nexloop_workbench_object_notes`：凭浏览器会话，当前角色为 owner、operator 或 reviewer 才能读；对象必须可见；最多返回 200 条，值原样返回。只授权给 `nexloop_api`，HTTP 路由在 M5c-3。
- **清单**：
  - business-actions v59（新 Action）、capabilities v12（`object.add_note` 能力快照）；
  - system-catalog-names 增加这个 Action；
  - `business_actions.PROFILES` 增加 `object.add_note`；
  - `test_stage_register_pg` 的 Action 计数改为 145。
  - 部署前需要先 publish（新 Action 未发布前调用返回 404），运行单在 M5c-3。

## W2 限制（写进运行单与隐私说明，M5c-3）

正文里写了某人的姓名或电话，而备注所在的对象没有关联到该消费者、正文也没写出消费者 ID 时，消费者擦除无法识别这条备注，只有对象删除时才会随之删除。工作台输入框和机器能力说明都提示“不要写入姓名、电话等个人信息”。消费者导出和 NX-054 导出包不包含备注。

## 测试（M5c-1）

`tests/test_object_notes_pg.py`，真实 PG：

- **机器备注**：
  - 经 prepare / commit 写入，控制字符和双向控制符被去掉；
  - 审计字段集合正确，所有审计行都查不到正文里的标记值；
  - 对象的 revision 不变；
  - 空正文、只有空白和控制字符、超过 2000 字符的正文都被拒；不可见的对象返回 404。
- **上限**：
  - 策略缺失时返回 NX429，意图保持 prepare；
  - 同一对象的第 3 条（上限设为 2）、同一客户端的第 4 条（上限设为 3）被拒；
  - 人写的备注不受影响。
- **人写备注**：owner 和 operator 可以写，重放返回同一条；不是成员的人、`agent` 主体被拒；不可见的对象返回 404；直接 UPDATE 被拒（只追加）。
- **擦除与删除**：
  - 消费者擦除改写了挂在对象上的备注和正文里写了该消费者 ID 的备注，无关的备注不变；
  - 成员擦除改写了作者和正文；
  - 对象删除带走它的备注。CommercialRecord 只能经保留期删除，所以测试在回滚的事务里暂时停用它的删除守卫，代替保留期执行删除。
- **V-2、W1、W2 守护**：
  - Runtime、domain_worker、action_worker、scheduler 和 api 角色直读备注表都是 42501；读取函数只授权给 `nexloop_api`，handler 不授权给任何角色。
  - 引用备注表的数据库函数恰好是登记的 5 个，所以机器读取、证据校验和导出都碰不到备注；目前没有任何数据库函数调用读取函数。
  - Python 包里只有工作台模块可以出现备注名；会话抽取、两个导出模块和两个机器模块里不出现。

变异检查（逐项删掉后都是 1 failed）：每对象上限、每客户端上限、角色检查、“正文含消费者 ID”这一擦除条件、可见性检查。

全局守护与回归一轮：
- 守护：db_boundary、tenant_fk、system_catalog、copy guard、bootstrap、三项擦除覆盖、nx041 sim roles 和 pi_run、claim_store_pg；
- 机器相关的全部测试，加上本文件；
- 清单与注册：business_actions、catalog_manifests、stage_capabilities、workbench_roles、business_setup、stage_register；
- 审批：solo 与审批门；
- workbench_actions_http。

本地 `test_model_surface` 一开始失败，原因是这个 worktree 里的 agent-host 编译产物停在 10-21；重新编译后 32 passed，与这次改动无关。
