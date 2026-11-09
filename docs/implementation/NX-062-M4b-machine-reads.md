# NX-062 M4b：读取动词 object.neighbors 与 commitment.list

- **依据**：投影草案 `docs/implementation/NX-062-M4-projections.md`、ADR-041 附注 A1、L6 条件 M4-2、M4-5、N1、N2。执行：L10。基线 19cff151（M4a）。
- **迁移**：`0440_nx062_machine_reads_b.sql`（开发时临时号 0441，批次 155 组批时重编号）。
- **结构**：没有修改 M4a 的读取函数。`authz.nexloop_machine_read` 改名一次为 `..._before_reads_b` 后加了一层包装：包装只处理两个新动词，其他动词原样交给 M4a。被改名函数的 EXECUTE 授权按 0434 修复的做法动态转到包装上，测试断言转移后原函数无人持有、包装只授给 nexloop_api。新增 `authz.nexloop_machine_read_gate`，检查顺序与 M4a 相同：identity → scope → capability_current → 最新授权 → 类型在授权与投影中 → 投影非空。

## 两个动词

- **object.neighbors**：中心对象必须可见，且类型已获授权，否则返回 404。
  - 边有两种：受治理关系（`ontology.relations`，按密钥 world 过滤），和本动词登记的引用属性（Commitment.supersedes、CommercialRecord.related_record_id），出向、入向都算。
  - 邻居只在类型已为本动词授权、且可见时列出（`machine_object_visible`：world 与 NX-029/054 擦除）。只给类型和 ID，不给任何值。
  - 每组最多 20 个；`more` 表示还有没有更多可见邻居。看不到的一律不计数，没有 invisible、collapsed 或 total（遵循 N2）；没有可见邻居的组整组省略。Consumer 等类型永远不会出现在邻居里。
- **commitment.list**：列出可见的 Commitment，`status_group` 取 open（默认）、ended 或 all，open 与工作台同口径（conditional、open、in_progress、breached）。每页 50 条，按 ID 排序，没有 total。可见性判断写在 WHERE 里，50 条上限只计可见行；`next_after` 是本页最后一个可见对象的 ID，只有后面还有可见行时才给出，所以既不会带出隐藏对象的 ID，也不会暴露隐藏了多少条（L6 Q1）。status 为空或取值未知的 Commitment 归入 ended（L6 Q2）。
  - 每项只给 commitment_id、revision、registered_at 和登记属性，属性与 object.get 的 Commitment 投影相同。
  - **与草案的差异**：草案原本还打算返回 evidence 的 kind、occurred_at 以及 events 的 kind。为了不在这一片里另造一套证据账本的读取，本片不返回这些字段；需要时作为新的投影修订加入。quote、consumer_display、events.detail、events.principal_id、evidence.ref、exceptions.detail 本来就不返回。
- 两个动词都按天、key_prefix、动词、类型计数（T-12）。HTTP 层先执行 M3 的准入，再进入读取函数。

## 契约

- 新增 `machine-read-object-neighbors` 与 `machine-read-commitment-list`，各配一个示例，README 表格同步补充。
- 测试校验每个响应都符合对应契约。

## 测试（真实 PG + 真实 HTTP）

`tests/test_machine_reads_b_pg.py`，3 个用例：
- **neighbors**：
  - 只授权 Commitment 时，只出现 supersedes 指向的那条 Commitment。下列都不出现：擦除中 Consumer 的入向引用、Consumer 邻居、另一 world 的关系、计数字段、标记值。
  - 加授权 CommercialRecord 后，关系组出现，两条记录都在。
  - 隐藏的中心对象、不存在的中心对象、未授权的类型，都返回同一个 404。
  - scope 不含本动词的密钥返回 403；允许表标为 retired 时返回 403，恢复后返回 200；撤销后返回 403。
- **commitment.list**：
  - 在 scope 里但未授权，返回 403；open、ended、all 三组结果正确；擦除中的承诺不出现；返回字段集合正确；标记值不出现。
  - 4 种错误参数返回 422，其中包括 tenant_id。
  - 撤销后，在第二个独立连接上（以 nexloop_api 身份直接调用读取函数）的下一次读取立即被拒。
  - 读取计数正确。
- **不影响 M4a**：object.get 和 object.list 的行为不变，能力列表随授权变化；包装与新函数的授权检查正确。

相关回归（Node 24，`-n 2`）：
- reads_b、reads、realm、lifecycle、access 五个机器测试，路由清单 G-1..G-6、copy guard、三项擦除覆盖、契约四项、sim roles，共 318 passed。
- test_bootstrap：临时把编号改为 0438、0439 后 4 passed，跑完已恢复。
- definer 0 problems。

首次失败：测试 SQL 中 jsonb_build_object 的参数没有指定类型，补上 `::text`。

## 变异检查

每次改一处，跑 `tests/test_machine_reads_b_pg.py`，跑完恢复。共 11 项，全部变红：

| 变异 | 结果 |
|---|---|
| 去掉 scope 检查 | 1 failed（补了窄 scope 密钥的用例后） |
| 去掉 capability_current | 1 failed（补了 retired 用例后） |
| 去掉授权检查 | 2 failed |
| 邻居不按授权类型过滤 | 1 failed |
| 邻居不检查可见性 | 1 failed |
| 中心对象不检查可见性 | 1 failed |
| 列表不检查可见性 | 1 failed |
| 去掉 status_group 过滤 | 1 failed |
| 不计数 | 1 failed |
| 包装不接收原函数的授权 | 3 failed |
| 关系不按 world 过滤 | 1 failed（补了另一 world 关系的用例后） |

## 部署

迁移 0441 自带两个读取动词的登记与允许表行。没有新 Action，也没有新路由，仍是 `POST /machine/v1/read`，所以注册计数与清单都不变。负责人授权之前，任何主体都读不到这两个动词的数据。

## L6 条件 PASS 的修改（bafb23dd）

- Q1：commitment.list 的可见性判断移入 WHERE，上限只计可见行，`next_after` 取最后一个可见 ID（查 51 行判断是否还有下一页）。新增测试：第 50 个候选行不可见时，`next_after` 不含它的 ID，且一页仍有 50 条。
- Q2：status 为空或未知的 Commitment 归入 ended，不再只有 all 才看得到。
- Q3 性能边界：object.neighbors 按登记的引用属性反查入边（例如谁的 `supersedes` 指向中心对象）时，对中心对象所在租户和 world 的同类对象做一次 jsonb 属性扫描，没有专门索引；commitment.list 对每个候选行调用一次可见性函数（擦除判定）。首批数据量下可接受；租户对象量到十万级时，应为登记的引用属性加表达式索引，或改为物化的边表，届时另开任务。
