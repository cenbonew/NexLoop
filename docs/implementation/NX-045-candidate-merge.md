# NX-045 候选暂存、粘合与审核队列（开发线 L2，先行开发）

状态：实现与定向测试完成，**未标 done**。NX-045 依赖 NX-044，后者尚未开始，任务状态由调度员判定。迁移 `0071_nx045_candidate_merge.sql` 为临时编号。

依据：ADR-019 §3.4（粘合）、§3.5（审核由人类做出，属 NX-044/046）、§4（候选状态机）、决定 6（四项可确定性计算的特征）；docs/04 §5（实体不得只凭相似度合并）。

## 1. 交付

| 文件 | 内容 |
|---|---|
| `packages/eios-core/src/eios/migrations/0071_nx045_candidate_merge.sql` | 状态机、事件审计、租户粘合配置、别名表、粘合记录、队列读取 |
| `packages/eios-core/src/nexloop_eios/candidate_merge.py` | 四项特征、`calibrate`、`CandidateGluer`、`ReviewQueueReader` |
| `packages/eios-core/src/nexloop_eios/claim_matching.py` | `match_claim` 增加 `matcher_version`/`provider` 参数，供合并后重匹配 |
| `tests/test_candidate_merge_pg.py` | 8 项测试，其中 7 项为真实 PG |
| `tests/data/nx045_merge_calibration.json` | 42 对合成标注（21 正例，含易混淆负例与白名单例） |
| `tests/data/nx045_merge_config.json` | 由校准产生的初始配置 |
| `tests/verification_real_merge_calibration.py` | 显式 opt-in，用真实 embedding 校准 |
| `docs/implementation/NX-045-real-calibration.json` | 真实 embedding 校准报告（real 证据） |
| `tests/test_claim_store_pg.py` | NX-019 边界测试的允许名单加入 nx045：审核队列需要展示原文证据，Context 仍不读 Claim |

## 2. 状态机（单一入口 `ontology.nexloop_candidate_transition`）

| 从 | 到 | 主体 |
|---|---|---|
| staged | merged / pending_review / superseded | service（粘合） |
| pending_review | published / merged / rejected | **human**（NX-044 的人类审核 Action 调用） |
| pending_review | superseded | service |

- 每次转移都带 expected revision（不符返回 40001），并写入 `ontology.nexloop_candidate_events`，记录 actor_kind 与 actor_ref。
- 该函数没有任何应用角色授权：服务侧只能通过 `authz.nexloop_record_candidate_glue` 间接调用，而该函数只会发起 service 允许的转移。NX-044 的受治理人类 Action definer 在验证 reviewer 与 `ontology.schema.review` 之后直接调用此入口，并负责写入 review-decision 与 Schema 发布。
- 新增列：`revision`、`merge_scores`（契约形状）、`merge_target_ref`、`config_version`、`superseded_by`、`status_reason`；用 CHECK 保证 merged 必有目标、superseded 必有指向、merged/pending_review 必有分数和配置版本。

## 3. 粘合

1. **目标**是同租户、同类的已有定义，文本由数据库派生（NX-021 `definition_texts`），再加上已有别名：
   - 属性 ↔ 同一 owner 类型的属性
   - 词表值 ↔ 同一属性的词表值
   - 类型 ↔ 召回到的类型
   - **object_instance 一律 pending_review**，原因记为 `instance_identity_requires_review`（docs/04 §5）
2. **四项特征**（各 0–1，`FEATURE_VERSION=nx045-glue-features/1`）：
   - 词相似度：中文字二元组与英文词集合的 Dice 系数
   - 核心词包含：去掉通用前后缀后互相包含记 1；否则取最长公共子串比例，**少于 2 个字记 0**（订单/退货单、线上/线下不算共享核心词）
   - 向量：召回所用 embedding profile 下的余弦相似度
   - 白名单：租户配置中的同义组，或“文本 → 目标 ref”规则
3. **配置**：`weighted_total = Σ w·f`，权重与阈值来自租户当前激活的 `ontology.nexloop_merge_configurations`（有版本号，只有一个激活）。配置只能由可信配置身份 `nexloop_configurator` 通过 `control.nexloop_publish_merge_configuration` 发布。SQL 会再次核对版本一致以及“分数达到阈值才允许合并”。
4. **相似去重**：在精确同文本去重之外，与更早的、同类同范围的 staged/pending_review 候选比较，达到 dedupe 阈值时本候选转为 superseded，依赖 Claim 并入对方。出队与重指向都以表中累积的 `dependent_claims` 为准。
5. **合并**的处理顺序：
   1. 写入别名表 `ontology.nexloop_definition_aliases`，作为别名的真实来源
   2. 转为 merged
   3. 依赖 Claim 从 awaiting_definition 复位为 unresolved
   4. 调用 NX-021 `RecallIndexer.index_alias` 回流召回索引
   5. 用确定性的“重指向判定”（属性 → 目标属性；词表值 → 目标 enum 成员）以 matcher 版本 `nx020-matcher/1;merge:<candidate>` 重匹配；**NX-020 的全部守卫照常执行**，再经受治理 edit Action 自动应用
   - 类型合并只复位 Claim，交给常规匹配器重新判定。
6. **AT-069 防重复**：
   - 回流后召回直接命中 canonical 定义，模型选到已有定义即可自动应用。
   - 即使模型再次把同一文本提成“新定义”，精确去重也会把 Claim 挂到已合并的候选上。`repoint_merged` 清扫会把它重指向并应用，不会进入审核队列。

## 4. 审核队列读取（供 NX-046）

`authz.nexloop_read_review_queue` / `ReviewQueueReader.pending(limit)`：
- 只返回 pending_review 候选；tenant 和 world 由凭据推导，因此 simulation/shadow 候选不会出现在 real 队列。
- 调用方必须对 `eios:action:ontology.schema.review:1` 持有当前 EXECUTE（HMAC 签名，需要 `nexloop_assert_action_authority` 支持浏览器人类会话）。
- 每项包含：候选契约、merge_scores、config_version、revision（供 NX-044 做 CAS）、依赖 Claim 数，以及原文证据（quote、message、span、resolution_state）。

## 5. 校准过程

- **数据**：42 对合成标注，21 个正例。
  - 正例覆盖同义属性名、带通用后缀的属性名、数量词表、类型的“X / X信息”。
  - 负例刻意包含高字面重叠的不同概念：联系时间偏好/联系方式偏好、肤质/发质、一万元以上/五千元以上、线上/线下、订单/退货单。
  - 2 对为白名单例，其中一对除“付”字外无共享字。
- **方法**：
  - 权重在单纯形上以 0.1 为步长网格搜索，阈值取 0.300–0.900、步长 0.025。
  - 约束：白名单权重 ≥ 阈值，即白名单单独命中就能合并（显式业务规则）。
  - 目标按优先级：精确率为 1（错误合并会悄悄改写语义，漏合并只多一次人工审核）→ 召回率 → 正负例间隔 → 稳定的平局规则。
- **迭代记录**（首次结果保留）：
  1. 第一版核心词特征对只共享一个字的无关对也给 0.5，区分度差，改为至少 2 个字才计分。
  2. 不加约束时，最优解把白名单权重降到 0（白名单样本只有 2 个），于是加入“白名单单独可达阈值”的约束。
- **CI 初始配置**（确定性测试 embedding，`nexloop-test-ngram-v1@64`）：
  - 权重：词 0.2 / 核心 0.2 / 向量 0.2 / 白名单 0.4；merge 与 dedupe 阈值都是 0.375
  - 精确率 1.0，召回 0.571（12/21），间隔 0.054
  - `test_initial_configuration_is_reproduced_by_calibration` 断言提交的配置正好等于校准输出。
- **真实 embedding**（opt-in，`doubao-embedding-vision-251215@1024`）：
  - 权重：词 0.4 / 核心 0.1 / 向量 0.1 / 白名单 0.4，阈值 0.35
  - 精确率 1.0，召回 0.667（14/21），间隔 0.023，54.8s
  - 这是建议的生产初始值，需由调度员经可信配置发布。
- 漏掉的正例（如 肤质类型/皮肤类型、所在城市/居住城市）都会进入人工审核，不会被错误合并。

## 6. 实际执行的命令与结果

环境：`source ~/.nvm/nvm.sh && nvm use 24`、`LANG=en_US.UTF-8`、`PYTHONPATH=packages/eios-core/src:tests`。

1. `tests/test_bootstrap.py tests/test_db_boundary.py`：迁移初稿的 CHECK 用了子查询，在提交运行前已改写；之后 6 passed。
2. `tests/test_candidate_merge_pg.py`：
   - 首次 7 failed / 1 passed：测试里 float 参数需要 `::numeric` 转换；校准测试中 `0.8 > 0.8` 的断言写错。均为测试问题。
   - 修正后 8 passed。
3. 目标集（candidate_merge、claim_matching、recall、claim_store、bootstrap、db_boundary、doctor、core_sandbox、compose_bootstrap、wheel_install）：
   - 首次 1 failed：NX-019 边界测试不允许新迁移读取 Claim，已更新允许名单。
   - 最终 **71 passed，104.26s**，junit sha256 `9a1f2c36051f03cef01c1f7df353581dc9bdab26b97947e9a53dc1fc8416ddf6`。
4. 真实 embedding 校准（opt-in）：1 passed，54.82s。
5. 测试结束后没有本线遗留进程。

## 7. 验收结论（建议）

| AT | 建议 | 依据 |
|---|---|---|
| AT-066 候选粘合 | **passed（测试证据；生产权重另有 real 校准）** | 见下方说明 1 |
| AT-069 发布回流 | “不再产生重复候选”部分 passed（合并路径）；发布路径依赖 NX-044，整体 not_run | 见下方说明 2 |

1. AT-066 的依据是 `test_glue_merges_above_threshold_and_reviews_below`：
   - “喜欢的运动项目”达到阈值，合并到 `favorite_sport`，记录别名，Claim 被重指向后经受治理 edit 自动写入。
   - “常用付款方式”未达阈值，进入 pending_review。
   - 两者的分数都符合契约并带 config_version；配置只能由可信配置身份发布（`test_glue_requires_active_configuration_and_match_grant`）。
   - 粘合可幂等重跑。
2. AT-069 的依据是 `test_reflowed_alias_prevents_duplicate_candidates`：
   - 合并后，同类对话直接召回 canonical 定义并自动应用，候选数不变。
   - 模型再次提出同一“新定义”时会被重指向并应用，队列为空。

其他：
- 相似去重：`test_similar_open_candidates_are_deduplicated_and_claims_accumulate`。
- 实例不按相似度合并：`test_object_instances_never_merge_by_similarity`。
- 队列只含本租户本 world、需要审核权限、simulation 隔离：`test_review_queue_scoped_to_tenant_world_and_review_permission`。
- 人类专属转移与无应用入口：`test_human_only_transitions_and_no_application_entry`。

## 8. 留给 NX-044 / NX-046

- NX-044：以人类受治理 Action 实现 approve / merge_into / reject，持久化 review-decision，并在通过发布门槛后调用 `ontology.nexloop_candidate_transition(... 'human' ...)`。
  - merge_into 应复用本模块的别名写入与重指向。目前别名只在粘合路径中写入，建议把写别名、复位 Claim、回流抽成 NX-044 也能调用的 definer。
  - approve 之后要发布 Schema、调用 `RecallIndexer.index_object_type`，再重匹配依赖 Claim。
- NX-046：在 `ReviewQueueReader.pending` 之上实现工作台，并实现 reject 后的同文本冷却期（AT-068，本任务未做）。

## 9. 局限

- 校准集只有 42 对合成数据，还没有真实脱敏数据；召回率较低是有意的保守选择。
- 每个候选都要和全部同类目标两两计算特征，向量逐条调用 embedding，规模变大后需要缓存或批处理。
- 类型合并之后不能自动推出属性，依赖 Claim 只复位，交给常规匹配器。
- 粘合配置的发布入口与 0050 可信配置清单尚未打通，目前是独立的配置身份函数。
