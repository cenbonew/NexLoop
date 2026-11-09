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

## M5c-2 交付：机器写入 HTTP 路由与契约

- **路由**（machine_key realm，先过 M3 准入和限速；URL 里不能带查询串；参数只能放在请求体里，请求体上限 16 KB）：
  - `POST /machine/v1/prepare`：请求体 {capability, intent_id, params}，返回 `machine-prepare`；
  - `POST /machine/v1/commit`：请求体同上，另加 prepare_digest。由 API 代签后调用 SQL。返回 `machine-intent`：待审批时 202，其他情况 200，包括 refused；
  - `GET /machine/v1/intents/{intent_id}`：返回 `machine-intent`。
- **错误映射**：422 invalid_request，404 not_found，403 not_permitted / approval_policy_required，401 unauthenticated，409 intent_conflict / intent_committed / digest_mismatch，410 prepare_expired，429 note_limit / note_limit_policy_missing（带 `Retry-After: 3600`），503 unavailable。错误体只有结果码，不回显请求里的任何值。
- **契约**：
  - 新增 `machine-write-request`、`machine-prepare`、`machine-intent`，各带一个示例，README 同步；
  - `machine-error` 的 code 枚举增加上述写入结果码（只增加，不删改）；
  - `scripts/generate_contracts.py --check` 通过。
- **路由清单 v16**：新增 3 条路由，SQL 端口分别为 `authz.nexloop_machine_prepare`、`authz.nexloop_machine_commit`、`authz.nexloop_machine_intent`。
  - `object.add_note` 暂时登记在 `capabilities_not_on_http`，M5c-3 加上工作台操作后移出。
  - 路由计数改为 111 和 110，端口计数改为 88，探测计数加 3。
  - G-5 凭据矩阵覆盖新路由：只有机器密钥能通过认证，其余五种凭据都返回 401。
- **修正 M5b**：已结束的意图如果参数变了，即使 prepare 摘要相同，也返回 409，不再返回首次结果。0442 还没发布，所以直接修改；测试已补。

### 测试（M5c-2）

`tests/test_machine_write_http.py`，真实 HTTP 与真实 PG，每个响应都按契约校验，并检查 `Cache-Control: no-store`：
- prepare → commit → 重放 → 查询；
- 冲突、身份字段、多余字段、坏 JSON、查询串、无密钥、对象不可见、读取动词、未知意图、意图 ID 太短、commit 缺 prepare_digest，以上每种都核对状态码和结果码；
- 错误体里查不到请求正文；
- prepare 过期返回 410；超过上限返回 429，带 Retry-After。

一轮回归 329 passed：
- 路由清单（单元与 PG）、契约与已发布契约、G-5 矩阵；
- 机器生命周期、机器读取与写入、备注；
- 全局守护：db_boundary、tenant_fk、system_catalog、copy guard、bootstrap、三项擦除覆盖、nx041 sim roles 和 pi_run、claim_store_pg；
- stage_register。

## M5c-3 交付：工作台备注入口与运行单

- **工作台写入**（V-4）：
  - 新增工作台操作 `object_note_add`，走现有的受治理 Action 通道，CSRF、会话、Origin 检查沿用现有中间件。
  - 角色清单 v56：`nexloop.object.add_note` 给 owner 和 operator（slice object_notes），不在仅限 owner 的集合里；reviewer 只能读。
  - SQL handler 会按成员当前的角色再核对一次。
  - 工作台查找 Action 的函数（`authz.nexloop_workbench_action_for`）原本只接受 human 能力。0443 改名加包装：只对已登记的机器写能力（目前只有 `object.add_note`）用同一套查找逻辑，即 human 会话、恰好一个已发布定义、成员自己的授权事实；其他能力原样委托给原函数。
  - 工作台操作的错误映射增加了 `NoDataFound → 404`，以及 `object note:` 结果码。
- **工作台读取**：
  - 新路由 `GET /api/v1/workbench/objects/{type_name}/{object_id}/notes`，走 `WorkbenchQueries.object_notes` 和 `control.nexloop_workbench_object_notes`。
  - 先核对会话；SQL 只接受两种备注类型和 64 位十六进制的 ID，其他一律 404。
  - 路由清单 v17：`object_note_add` 进入 operations，`object.add_note` 从 `capabilities_not_on_http` 移出。路由计数改为 112 和 111，端口计数 89。
- **前端**（V-1）：
  - `apps/web/src/workbench/ObjectNotes.tsx`，挂在承诺详情页上。
  - 正文只渲染为文本节点，不解析 HTML、Markdown 或链接。
  - 每条标注作者：机器写的显示“机器客户端 <client_id> 的备注 · 未核实陈述”，人写的显示“成员的备注 · 未核实陈述”。
  - 输入框上限 2000 字符，带“不要写入姓名、电话等个人信息”提示。只有角色带这个 Action 时才显示输入框。
  - CommercialRecord 在工作台还没有详情页，所以这一轮只能经 API 读写它的备注。
- **运行单**：[run-nx062-machine-access.md](../release/run-nx062-machine-access.md) §4，包括部署步骤（先 publish、角色 v56、备注上限发布、gate 之后再 promote）、stage test world 的验收演练（B 类，只准备，不执行）、给负责人的隐私说明（含 W2 限制）。

### 测试（M5c-3）

- `tests/test_object_notes_http.py`，真实工作台 HTTP 与真实 PG：
  - owner 保存一条含 HTML、`javascript:` 链接和 Markdown 的备注；
  - 缺 CSRF、CSRF 错误、跨 Origin 都被拒（401 或 403），什么都不写入；
  - 不可见的对象被拒；
  - 读取接口原样返回正文，机器备注带 client_id、不带作者 principal；
  - 不存在的对象、Consumer 类型都返回 404，带查询串返回 422，无会话返回 401；
  - operator 可以写；
  - 审计里有 3 条，都不含正文。
- `apps/web/test/workbench-object-notes.test.ts`（vitest + jsdom）：
  - 恶意正文只渲染为文本：没有 img、a、b 元素，脚本没有执行；
  - 作者标注正确；
  - 输入框提示和长度上限正确，提交时发送去掉首尾空白后的正文；
  - reviewer 看不到输入框，但能读备注。
  - `tsc --noEmit` 通过，全部 web 测试 72 个文件、400 项通过。
- 回归：
  - 一轮 410 项：机器相关与备注、路由清单、工作台（角色、操作、读取）、清单、契约、stage_register、全局守护、审批门。第一轮里唯一的失败是 owner 角色 Action 数从 106 变为 107，改完后通过。
  - 另一轮 43 项：其他读取角色清单的测试。

## L6 记录项（77728430..35c85613 PASS 后，迁移 0444）

- `runtime.nexloop_machine_intent_audit` 自己设置并恢复租户，不再依赖调用方。
- `runtime.nexloop_object_note_member_erasure` 执行完后，恢复会话原来的租户设置。
- 两个函数都是原地重定义，copy guard 登记了这两处改动。
- 测试 `test_tenant_hygiene_of_the_intent_audit_and_the_member_erasure`：以 nexloop_owner 身份、在租户为空时调用审计函数可以写入，调用后租户设置不变；成员擦除之后，会话的租户还是原来的值。
