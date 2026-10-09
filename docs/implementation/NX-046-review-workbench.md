# NX-046 最小审核工作台与回流（开发线 L2，先行开发）

状态：实现与定向测试完成，**未标 done**。三种审核决定依赖 NX-044，由调度员判定。迁移 `0073_nx046_review_workbench.sql` 为临时编号。

依据：ADR-019 §3.5–3.6、AT-068 / AT-070、AT-045 口径（未知、加载、403、部分数据都要有准确标签，不得出现空白伪成功）。

## 1. 交付

| 文件 | 内容 |
|---|---|
| `0073_nx046_review_workbench.sql` | 同文本键改为部分唯一；拒绝冷却表与触发器；替换 NX-020 的 `authz.nexloop_record_claim_match`（冷却期不排队、冷却后重开）；替换 NX-045 粘合函数改用共享效果函数；`ontology.nexloop_candidate_merge_effects` / `_publish_effects`（NX-044 入口）；`authz.nexloop_read_review_candidate` |
| `nexloop_eios/candidate_merge.py` | `ReviewQueueReader.candidate()`；无法授予审核权限时报 PermissionError；回流入口 `CandidateGluer.reflow_merged()` / `reflow_published()`；重匹配只处理复位后的 unresolved Claim |
| `nexloop_eios/review_http.py` | `GET /api/v1/review/queue`、`GET /api/v1/review/candidates/{id}`；**没有决定接口** |
| `nexloop_eios/backend.py`、`http_api.py` | `Backend.authenticate_browser_reviewer` / `ReviewServices`，每次读取都重新做人类会话的 PG 认证；在生产 `create_app` 中注册 |
| `apps/web/src/review-api.ts`、`Review.tsx`、`Account.tsx`、`styles.css` | 队列、候选详情、禁用的决定按钮 |
| `tests/test_review_workbench_pg.py` | 4 项真实 PG |
| `tests/test_review_http.py` | 3 项：生产 app 装配、真实登录 Cookie、浏览器人类 EIOS 授权 |
| `apps/web/test/review.test.ts` | 5 项 vitest（`react-dom/server` 渲染 + 模拟 fetch，没有引入新框架） |
| `tests/test_claim_store_pg.py` | NX-019 边界测试的允许名单加入 nx046：0073 替换了读取 Claim 的匹配记录函数并为工作台读取证据，Context 仍不读 Claim |

## 2. 工作台（AT-070 读取部分）
- 只有当前对 `eios:action:ontology.schema.review:1` 持有 EXECUTE 的人类会话可见，tenant 和 world 由会话推导。
- HTTP 响应：

| 情形 | 状态码 | 前端表现 |
|---|---|---|
| 未登录 | 401 | 提示重新登录 |
| 已认证但无审核权限，或权限被撤销 | 403 | 队列整体不渲染 |
| 会话无法在业务侧核验（例如未绑定浏览器业务应用）、依赖不可用 | 503 | 显示“暂不可用（权限或数据暂时无法核验）” |

  这些情形都不会返回任何候选内容。
- 队列项包含：类型、显示名、依赖 Claim 数、加权总分。
- 候选详情包含：
  - 原文证据：quote、消息 id、字符区间、谓词、Claim 状态
  - 召回结果（ref / 方法 / 分数）
  - 四项粘合分数、加权总分与阈值、最接近的已有定义、配置版本
  - 依赖 Claim 数
  - 相似候选（相似去重后并入本候选的那些）
  - 同文本的冷却信息
- 三个决定按钮（批准发布 / 并入已有定义 / 拒绝）为 `disabled` + `aria-disabled`，不绑定点击事件，旁边有 status 文案“未启用……等待 NX-044”。服务端每次响应都带 `decisions:{enabled:false,reason:'awaiting_nx044_review_actions'}`；前端只接受 `enabled:false`，收到其他值按“响应无效”处理。
- 错误态：加载中、401、403、404（已不在队列）、422、503、格式无效各有准确文案；队列为空时明确显示“审核队列为空”。

## 3. 拒绝冷却（AT-068 的数据与判定）
- 无论哪条路径把候选改为 rejected（NX-044 的人类 Action 经 `ontology.nexloop_candidate_transition`），触发器都会：
  - 写入 `ontology.nexloop_candidate_rejections`，冷却期取租户粘合配置的 `reject_cooldown_seconds`（默认 30 天，新增列）
  - 把依赖 Claim 从 awaiting_definition 改为 `rejected_definition`
- 冷却期内同文本再次出现：Claim 只追加为已拒绝候选的依赖，直接置为 `rejected_definition`，不新建候选、不排队、不写本体。
- 冷却期过后同文本重新开放：生成新的候选 id（同步写入契约 JSON），经粘合后重新进入队列。
- 同文本键改为对 rejected / superseded 以外状态的**部分唯一索引**。0070 自动生成的约束名超过 63 字节被截断，迁移按约束定义查找后删除。

## 4. 回流入口（供 NX-044）

| 决定 | SQL（在 NX-044 的人类 Action definer 事务中） | Python（提交后） |
|---|---|---|
| merge_into | `ontology.nexloop_candidate_merge_effects(tenant,world,candidate,target_ref,alias_text,config)`：写别名、依赖 Claim 复位为 unresolved；实例候选拒绝（需要身份解析，不能用别名）。然后 `ontology.nexloop_candidate_transition(...'pending_review','merged','human'...)` | `CandidateGluer.reflow_merged(candidate)`：`index_alias` 回流召回，再按 NX-020 守卫重匹配并经受治理 edit 自动应用 |
| approve | Schema 发布后 `transition(...'published','human'...)`，再 `ontology.nexloop_candidate_publish_effects(...)` 复位 Claim | `CandidateGluer.reflow_published(candidate)`：`index_object_type` 重建索引后重匹配 |
| reject | `transition(...'rejected','human'...)`；冷却与 Claim 状态由触发器完成 | 无 |

- 服务侧的粘合（0072）也改为调用同一个 `merge_effects`，两条路径共用一处实现。
- Schema 发布或写入新的授权事实都会改变 directory hash，原有服务会话会按设计 fail closed。NX-044 在发布之后必须用同一凭据重新认证，再调用 `reflow_*`（测试中已体现）。
- approve 之后要真正应用值，还需要 NX-044 同时发布包含新定义的 edit Action 版本。在此之前，NX-020 守卫会让 Claim 保持 needs_resolution（`test_publish_reflow_entry_reindexes_and_rematches` 断言了这一点，不会冒充成功）。

## 5. 实际执行的命令与结果

环境：`source ~/.nvm/nvm.sh && nvm use 24`、`LANG=en_US.UTF-8`、`PYTHONPATH=packages/eios-core/src:tests`。

1. `tests/test_bootstrap.py tests/test_db_boundary.py`：
   - 首次 3 failed，`UndefinedObject`：0070 的唯一约束名被截断。改为按约束定义删除。
   - 之后 6 passed。
2. `tests/test_review_workbench_pg.py`：
   - 首次 2 failed：审核人凭据写入新授权事实、以及 Schema 发布，都会使原有会话失效（fail closed）。测试改为在这些节点之后重新认证；在 recall 上的断言改为检查索引行，因为匹配会话没有新属性的读权限。
   - 之后 4 passed。
3. `tests/test_review_http.py`：
   - 首次 1 failed：测试用户没有绑定任何浏览器业务应用，返回的是 503 而不是 403。测试改为分开覆盖两种情形：未绑定业务应用 → 503；有业务访问但没有审核权限 → 403。
   - 之后 3 passed。
4. Web：
   - `npx tsc --noEmit` 通过。
   - `pnpm exec vitest run apps/web/test`：12 passed，含新增 5 项，首次即通过；junit sha256 `8134d6b5f00caf7244f73c95cacb20bdf83fe5422d270c49e369544de5800e08`。
   - `pnpm build`（apps/web）构建成功。
5. 后端目标集（review_workbench、review_http、candidate_merge、claim_matching、recall、claim_store、web_chat_http、browser_http、http_api、backend、bootstrap、db_boundary、doctor、core_sandbox、compose_bootstrap、wheel_install）：
   - 首次 1 failed：NX-019 边界测试的允许名单，已更新。
   - 最终 **107 passed，150.51s**，junit sha256 `6b9c4dc94f930f53df818342f33f7ec0c7baea08e4fdc8ca7dcf74a8d40d36cd`。
6. 本线没有遗留进程。期间看到的测试 PG 属于 L1 正在运行的测试。

## 6. 验收结论（建议）

| AT | 建议 | 依据 |
|---|---|---|
| AT-070 最小审核工作台 | **部分** | 见下方说明 1 |
| AT-068 审核拒绝 | **部分** | 见下方说明 2 |
| AT-069 发布回流 | 合并路径的“不重复”已在 NX-045 验证；本任务补充了 NX-044 merge_into 的回流 | `test_merge_into_reflow_entry_reindexes_and_applies`。approve 路径要等 NX-044 发布 Action 版本 |
| AT-045 界面错误态 | 审核页范围内 passed（测试证据） | `review.test.ts` 覆盖加载 / 401 / 403 / 404 / 422 / 503 / 格式无效 / 空队列，以及禁用按钮的准确标签 |

1. AT-070：
   - 已满足：原文 span、召回结果、粘合分数、依赖 Claim 数可见（`test_reviewer_sees_queue_detail_and_disabled_decisions`、`test_workbench_detail_shows_span_recall_scores_dependents_and_similar`）；无权限用户看不到队列（`test_without_review_permission_the_queue_is_invisible`、`test_revoked_review_grant_hides_queue_immediately`）。
   - 未满足：“三种决定可用”与“每个决定有审计记录”依赖 NX-044，目前明确显示为未启用。
2. AT-068：
   - 已满足：冷却期数据结构与判定、Claim 置为 rejected_definition、冷却期内不再排队、冷却期后重新开放、不写入本体（`test_reject_cooldown_keeps_evidence_and_blocks_requeue_until_expiry`，用 NX-044 将调用的转移入口模拟人类 reject）。
   - 未满足：真正的人类 reject Action 与审计依赖 NX-044；“不进 Context 正式属性”沿用 NX-019/020 已有的隔离，本任务没有新增。

## 7. 局限
- 工作台挂在登录后的 Account 页上，没有路由，也没有分页（limit 默认 50）。
- 原文证据显示的是 Claim 的 quote 和字符区间，没有显示消息全文，因为工作台没有对 Message 对象的 READ。
- 冷却期配置只有一个新增列（默认 30 天），还没有发布入口；`control.nexloop_publish_merge_configuration` 的签名未改，需要由 NX-044 或可信配置后续补上。

## 8. 生产租户粘合配置（负责人 2026-10-09 批准，调度员追加）

- 文件：`deploy/ontology/merge-config.v1.json`，`config_version=nx045-glue-v1-doubao1024`。
  - 权重：词 0.4 / 核心 0.1 / 向量 0.1 / 白名单 0.4；merge 与 dedupe 阈值均为 0.35；拒绝冷却 30 天。
  - embedding：`doubao-embedding-vision-251215`，维度 1024。
  - 校准来源：`tests/data/nx045_merge_calibration.json`（42 对合成标注），real 证据见 `docs/implementation/NX-045-real-calibration.json`。精确率 1.0，召回 0.667（14/21），误合并 0。
  - **whitelist 为空**：校准里的两条白名单是合成数据；业务白名单须由负责人提供，并以新的 config_version 发布。
- CI 仍使用 `tests/data/nx045_merge_config.json`（测试 embedding `nexloop-test-ngram-v1@64`）。测试断言两者是不同版本，且生产文件的数值与真实校准报告完全一致。
- 发布路径与 NX-048 一致，只经 `nexloop_configurator`（`configurator_connection` 拒绝超级用户、BYPASSRLS 和 owner 成员会话）：
  ```
  python -m nexloop_eios.merge_configuration --manifest deploy/ontology/merge-config.v1.json --check
  python -m nexloop_eios.merge_configuration --manifest deploy/ontology/merge-config.v1.json --tenant <tenant> --apply  --database-url-file <configurator-dsn-file>
  python -m nexloop_eios.merge_configuration --manifest deploy/ontology/merge-config.v1.json --tenant <tenant> --doctor --database-url-file <configurator-dsn-file>
  ```
- 0073 新增两个只授予 `nexloop_configurator` 的函数：
  - `control.nexloop_publish_merge_configuration_manifest(tenant, manifest)`：
    - 要求清单的 embedding profile 正是租户当前激活的召回 profile，否则拒绝。部署顺序因此是：先激活召回 profile（`RecallIndexer.activate_profile`），再发布粘合配置。
    - 版本不可变：同一版本换了内容会被拒绝；同一内容重复发布不产生变更；重新发布旧版本即回滚激活。
    - 记录由 SQL 自行计算的清单 sha256，以及 `published_by`。
  - `control.nexloop_read_merge_configuration(tenant)`：供 doctor 只读核对。
- 0072 的 `control.nexloop_publish_merge_configuration` 保留不变，CI 测试仍用它。
- Python 侧 `nexloop_eios.merge_configuration.validate` 会校验：键集合、四个权重之和为 1、阈值范围、白名单权重不低于合并阈值（校准约束）、`feature_version` 与部署代码一致，以及校准时误合并为 0。
- 测试 `tests/test_merge_configuration_pg.py` 全部通过：
  - 清单数值等于真实校准结果
  - 6 种非法清单被拒绝
  - 召回 profile 不匹配时拒绝发布
  - 发布 / 重放 / 版本不可变 / 回滚
  - 只有配置身份可以发布
  - CLI
- 与 NX-046 及相关测试合并运行：80 passed，111.35s，junit sha256 `565257c0baa409140c020a7774ce6e5705c6fef68251bdc69e63873e5674d1ba`。

## 9. 冷却期时间戳缺陷修复（2026-10-09，分支 nx046-cooldown-fix）

- 缺陷：0074 的 `ontology.nexloop_candidate_rejected()` 在同一条 INSERT 中两次调用 `clock_timestamp()`，`rejected_at` 与 `cooldown_until` 分别取自两个时刻，二者之差不是精确的冷却时长。在 sice（较慢）上串行运行稳定失败。
- 修复：临时迁移 `0075_nx046_cooldown_single_instant.sql`（0074 未改）。
  - 函数开始处只取一次 `v_now:=clock_timestamp()`，两个字段都由它推导。
  - 新增列 `cooldown_seconds`，并加约束 `nexloop_candidate_rejections_single_instant`：`cooldown_until = rejected_at + cooldown_seconds`，存在偏差的数据无法写入。
  - 编号用 0075 而不是指定的 0078：`test_bootstrap` 断言迁移版本 1..N 连续，本分支 main 最高为 0074。
- 同类排查（0067 / 0070 / 0072 / 0074 中的 `clock_timestamp()` / `now()`）：只有上面这一处在同一语句里多次取时间来推导相关字段。其余用法都无害：
  - 表默认值，如 created_at / updated_at / recorded_at：各列独立表示写入时刻，没有任何逻辑依赖它们相等或相差某个固定值。
  - 单列 `updated_at=clock_timestamp()`。
  - 签名过期判断 `expires_at>clock_timestamp()+30s`：一次取值，只用于比较。
  - 冷却判断 `cooldown_until>clock_timestamp()`：一次取值，只与当前时刻比较。
- 测试：
  - 原断言（冷却时长恰为 30 天）保留。
  - “冷却期满”的模拟改为把整条记录前移 31 天，不再直接改写 `cooldown_until`，以保持不变量。
  - 新增 `test_cooldown_fields_come_from_one_instant`，结果与机器速度无关：
    - 断言函数定义中只出现一次 `clock_timestamp()`；
    - 冷却设为 7 秒时，两字段之差恰为 7 秒且满足约束；
    - 人为制造 1 微秒偏差的更新被 CheckViolation 拒绝。
  - 工作台测试连续 3 次均为 5 passed；目标集 82 passed，118.70s，junit sha256 `f0447b17b794e06b42d00eca1e0e85af5494d80d452ed12e395bb567de0b2e62`。
