# NX-028 切片 1：企业成员身份与读端口（实现说明）

设计稿见 `NX-028-design.md`（D1–D10 已定，§15 切片 1）。分支 `nx028-s1`，从 main `036c640` 切出，带上设计稿提交。迁移号 `0140`、`0141` 是临时编号（调度员指定 0140–0149），分别对应设计稿占位的 0117、0119，合并时由调度员重排。测试使用合成数据，在本地真实 PostgreSQL 上运行；HTTP 测试用真实登录 Cookie。

## 1. 企业成员身份（0140，D1/D2）

- **独立应用**：工作台是一个独立的浏览器应用，在 `control.nexloop_browser_applications` 和 `control.nexloop_browser_business_applications` 中各有一行。API 为它开了独立的登录域：
  - 路由 `/api/v1/workbench/auth/{login,session,csrf,logout}`，Cookie 为 `__Host-nexloop_workbench`；
  - 与顾客的 WebChat 共用身份库、租户和 origin，但会话互不通用。WebChat 的 Cookie 访问工作台返回 401。
  - 部署时由 `nexloop-api --workbench-application-id <id>` 开启；未配置时不挂任何工作台路由。
- **成员资格**：表 `control.nexloop_workbench_configurations`，只追加。
  - 每次写入是该租户的下一个配置版本，记录工作台应用、角色清单、成员→角色映射，以及角色清单版本（不可回退）。
  - 写入入口只有 `control.nexloop_configure_workbench`，SQL 只认 `nexloop_configurator`。
  - 成员必须是已存在且有效的浏览器 Human；配置只能引用身份，不能创建身份。没有自助注册路径。
- **顾客主体永远不能拿到工作台授权**：顾客主体指在 `control.nexloop_consumer_owners` 里有记录的 principal。
  - 配置时拒绝把顾客主体设为成员；
  - 触发器 `nx028_workbench_grant_guard`（挂在 `authz.nexloop_authority_facts` 上）拒绝把任何工作台 Action 授权给顾客主体，也拒绝授权给非成员或超出角色范围；
  - 触发器 `nx028_customer_owner_guard` 拒绝成员成为顾客主体；
  - 缩减角色或移除成员时，若仍有当前授权，配置被拒，必须先撤销授权。
- **角色（D2）**：公开清单 `deploy/authorization/workbench-roles.v1.json`，含 owner、operator、reviewer。
  - 解除联系限制、承诺取消、标记沟通类承诺、目标、指标、预算、暂停只给 owner；
  - 切片 2、3 的 Action 名按设计稿 §15.2 预先列入。
  - `nexloop_eios.workbench_roles` 负责校验清单，并按成员把角色编译成 Human 授权事实，与服务授权使用同一 EIOS 模型。编译结果交给 0050 可信配置清单下发；成员通过 `--apply-members` 经 configurator 写入。
  - 私有成员文件不入库。

## 2. 读端口（0141）

- `authz.nexloop_workbench_read`，协议 `nexloop-workbench-read-v1`。只接受工作台应用的浏览器 Human 会话，并且要求同时满足：
  - 是当前成员；
  - 角色包含该动词对应的读 Action；
  - 不是顾客主体；
  - `assert_action_authority` 通过，即当前持有 EXECUTE。
- 动词与读 Action：
  - `nexloop.workbench.read`：goals、consumers、consumer、conversation、plans、actions、takeovers、settings（settings 另限 owner）；
  - `nexloop.commitment.read`：commitments、commitment；
  - `nexloop.contact.read`：contact。
- 复用已有视图，不复制逻辑：`runtime.nexloop_commitment_view`、0109 的限制表、NX-051 的消息投影、`runtime.nexloop_plan_current` / `nexloop_active_plans`。
- **派发预判**：调用切片 2 的 `control.nexloop_intent_dispatch_prediction(text,text,uuid)`。函数不存在或出错时，返回 `{"status":"unavailable"}`，不猜测。
- **takeovers**：切片 3 的接管表上线前，固定返回 `status:"unavailable"`。

## 3. 消息内容与 AT-003

- 消息正文、发送者、承诺原文、联系限制命中的原文都属于 Message 内容。
- 读端口只给消息引用，内容由 `WorkbenchReader` 用调用者自己的 Message READ 读取（`AuthorizedObjectReader`）。
- 没有 READ 时：
  - 正文显示为 `content: {"status":"restricted"}`；
  - 承诺原文为 `quote: null` 加 `quote_status: "restricted"`；
  - 命中原文为 `matched_text: null` 加 `matched_text_status: "restricted"`，界面显示“已命中规则 X（原文需授权）”。
- Consumer 属性按属性逐项 READ；读不到时 `properties.status` 为 `"forbidden"`。

## 4. HTTP 与服务

- `workbench_http.py`：`GET /api/v1/workbench/{overview, goals, consumers, consumers/{id}, conversations/{id}, plans, actions, commitments, commitments/{id}, contact, takeovers, settings}`。
  - 每次请求都在 PG 里重新认证，所以撤销授权后下一次请求即返回 403。
  - 状态码固定：401 / 403 / 404 / 422 / 503。
  - 未知或重复的查询参数返回 422。
- `Backend.authenticate_workbench` → `WorkbenchServices`（只读）。
- 总览由 Python 按区块分别调用，每块带 `{status: ok | forbidden | unavailable, data}`。缺权限的块是 forbidden，未实现的块（队列积压、NX-027 商业与费用、接管）是 unavailable，不会显示为 0。
- `http_api.py`（只做追加）：
  - `ApiConfiguration.workbench`；
  - 独立的 session store；
  - 挂载工作台登录与读路由；
  - `/workbench/*` 返回打包后的页面（SPA 回退）。
- `browser_http.router` 增加 `prefix` / `cookie` / `store_attribute` 三个可选参数，默认值不变。

## 5. 契约（D8）

- 新增 `packages/contracts/commitment-view.schema.json` 和 `contact-restriction-view.schema.json`，各带一个 example；三处生成产物已重新生成。
- 生成器不支持 `$ref`，所以复用的结构直接内联。
- 真实数据校验：
  - `/contact` 的 HTTP 响应用 `jsonschema` 校验；
  - 真实注册的 NX-026 承诺按 0141 的方式投影后校验。

## 6. 前端（apps/web）

- 路径 `/workbench/<page>[/<id>]` 渲染 `WorkbenchApp`，有独立的登录页；其他路径仍渲染顾客端 `App`。
- `workbench/api.ts`：读客户端与 AT-045 状态模型。
  - 状态分为 loading、empty、unauthenticated、forbidden、not_found、invalid_request、unavailable、invalid；
  - 响应形状不符时整体拒绝，不渲染半截数据；
  - `unknown` 显示为“待核对”。
- 页面：`workbench/pages/*.tsx`，包括总览、目标与对齐、消费者列表与详情、会话、计划与运行、Action / 异常、承诺列表与详情（四栏证据，“已交付”注明不代表兑现）、联系限制、设置与治理（只读成员与角色）、知识工作台入口，以及“未启用”的本体 / 演进、实验空间。
- 每个页面预留 `actions` 插槽（ReactNode），默认不渲染。切片 2、3 通过 `WorkbenchApp` 的 `slots` 挂载。
- `styles.css` 只追加。

## 7. 测试

- `tests/test_workbench_read_pg.py`（5 个，真实 PG 与真实登录 Cookie）：
  - 顾客不能成为成员或拿到授权；成员不能成为顾客；operator 拿不到 owner 专属 Action；非成员拿不到授权；必须先撤销再缩减角色；API 角色不能配置；配置只追加。
  - HTTP：401、WebChat Cookie 无效、顾客登录工作台返回 403 且无内容、owner 总览各块状态、settings 只给 owner、422 / 404、跨 host 返回 403。
  - 部分授权得到部分区块；撤销后下一次请求即 403。
  - AT-003：命中原文、正文、属性不可见并有标记；`/contact` 响应符合契约。
  - 读端口拒绝顾客的 WebChat 会话和服务角色。
- `tests/test_workbench_commitment_view_pg.py`：真实注册的承诺符合 commitment-view 契约；原文是否可见取决于 Message READ；“已交付”栏为空（AT-040）。
- `tests/test_workbench_http.py`：不依赖 PG 的 422 / 503 / 403 边界，以及 `/workbench/*` 的页面回退。
- `tests/test_workbench_roles.py`：D2 的 owner 专属约束、编译结果确定且只产生 Human 事实、成员文件校验、CLI。
- `apps/web/test/workbench-api.test.ts` 与 `workbench-render.test.ts`（vitest）：
  - 状态码映射；
  - 部分数据各块独立；格式错误整体拒绝；
  - 原文需授权，restricted 状态下夹带原文即拒绝；
  - 承诺状态只取对象，最晚界标“推导值”，问题解决栏为“不可用”；
  - 待核对与预判不可用的文案；
  - 路由；
  - 服务端渲染下，forbidden / unavailable 块不显示 0。

## 8. 已知限制与需要决定的事项

1. ~~员工看不到任何消息原文~~：已由 ADR-025 后续实现解决，见 §9。
2. **总览的“队列积压”块**是 unavailable：切片 1 没有人类可读的 work feed backlog 端口。
3. **派发预判**依赖切片 2 的 0116；合并前统一显示“派发预判暂不可用”。
4. **其他依赖块**：接管块依赖切片 3 的 0118，商业与费用块依赖 NX-027，此前都是 unavailable。
5. **测试中授权事实的写入方式**：由 admin 写入，代替 0050 清单应用（0050 已有覆盖）；成员配置走真实的 configurator 函数。

## 9. 后续：工作台成员读取消息原文与客户资料（ADR-025，分支 `nx028-s1b`）

- **迁移 `0145_nx028_workbench_member_read.sql`**（临时号 0145–0149）新增读派生 `workbench-member-v1`：
  - `authz.nexloop_assert_read_authority` 改名保留为 `_before_workbench_v0144`（即 0107 的 memo 包装，逻辑不变）；
  - 新包装只把带 `derivation='workbench-member-v1'` 的声明交给新判定，其余声明（顾客、服务、Agent、Run、配置授权或已有派生）原样交给原函数；
  - `authz.nexloop_fact_coverage` 不涉及，也没有改动。
- **判定**：每次读取都在 SQL 中重新计算，依据是当前的工作台会话、成员资格、角色和读取目标。
  - 会话必须是工作台应用的 Human 会话，主体是当前成员，且不是顾客主体；
  - owner、operator：本租户、本 world 的 Message（对象与字段），以及 Consumer 和它的属性；属性不在任何属性组里，或所在组被负责人标为受限（`property_group_restriction`），都不开放；
  - reviewer：只能读 `pending_review` 候选所依赖 Claim 的证据消息，候选状态改变后立即失效；
  - 其他租户、其他 world、伪造声明一律拒绝。
- **审计**：每次派生读取，在同一事务里按对象写一条 `runtime.nexloop_workbench_read_audit`。
  - 字段：成员、角色、对象类别、目标、用途（页面）、时间；
  - 表只追加、FORCE RLS，应用角色无权限；审计写入失败时读取一并失败；
  - 审计只经 `authz.nexloop_workbench_audit_read` 开放给 owner（`GET /api/v1/workbench/audit`）。ADR 提到的“审计角色”在 D2 中没有定义，目前只有 owner。
- **Consumer 字段**：`authz.nexloop_workbench_consumer_fields` 给出字段名和可读标记；受限字段在界面上列为 `withheld`，从不读取它的值。
- **Python**：`WorkbenchReader.read_object` 用派生声明读取，用途取自所在页面（conversation、contact、commitment、consumer、review_evidence），不再走 `AuthorizedObjectReader` 的配置授权路径。
- **测试**：`tests/test_workbench_member_read_pg.py` 按 ADR-025 §3 逐项覆盖：
  - owner 和 operator 能读正文、命中原文和属性，每次读取有一条审计（AT-003 正例补回）；
  - 受限属性组读不到；
  - 只限本租户、本 world；
  - reviewer 只能读证据，审核结束后失效；
  - 角色变更、移除成员、会话吊销后，下一次读取即失效；
  - 审计写入失败则读取失败，非 owner 读不到审计；
  - 回归：顾客会话与服务主体在每条读取路径上，改动前后结果一致；工作台派生对它们无效，也不留审计。

## 10. 后续：审核人证据页（分支 `nx028-reviewer`，解除 NX-028 的已知限制）

- 不需要新迁移：
  - 队列和候选详情沿用 NX-046 的审核读取（`nexloop_read_review_queue` / `nexloop_read_review_candidate`，要求 `ontology.schema.review` 的 EXECUTE），由工作台会话调用；
  - 证据消息原文走 ADR-025 的 reviewer 派生，每读一次审计一条，用途为 `review_evidence`。
- 接口：
  - `GET /api/v1/workbench/review`：待审核条目，含名称、类别、证据数；
  - `GET /api/v1/workbench/review/{candidate_id}`：证据列表，读到原文时 `content.status` 为 `ok`，否则为 `restricted`。
  - 候选一旦决定或被取代，审核读取不再返回它，此时返回 `{"status":"ended","evidence":[]}`：不读取原文，也不写审计。
- 页面：知识工作台入口改成只读页 `pages/Knowledge.tsx`，含列表和证据详情；审核结束后显示“审核已结束，证据原文不再可读”。审核决定仍在 NX-046 的审核页完成。
- 权限：reviewer 角色只有审核 Action，看不到工作台其他页面（403）；owner 和 operator 的角色不含审核 Action，看不到这一页（403）；顾客登录工作台也是 403。
- 测试：
  - `tests/test_workbench_review_evidence_pg.py`（2 个）：待审核证据可读且留审计；决定后显示已结束，不读取、不写审计；其他页面 403；无审核授权时 403；顾客 403；
  - vitest：证据解析、结束状态、路由。
