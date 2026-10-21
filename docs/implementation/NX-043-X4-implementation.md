# NX-043 X4：线上分配

- **依据**：NX-043 设计稿 §3、§4、§5，D2、D9。依赖 [X1](NX-043-X1-implementation.md)。
- **起点**：`c11313b9`（X1 及其上限修复）。
- **迁移**：`0292_nx043_online_allocation.sql`。这是临时编号，以合入后的 catalog 为准。临时编号造成空号，`test_bootstrap.py` 的两项计数断言在重新编号前会失败。
- **契约**：`experiment-registration.schema.json` 新增可选成员 `eligibility`。

## 1. 资格检查（冻结在预登记中）

- 资格规则写在预登记里，登记后不可修改。默认规则是 `runtime.nexloop_experiment_eligibility_default()`，按设计 §3：
  - persona 为 real；
  - 没有生效中的联系限制；
  - 不在人工接管中。
- 登记时没写的成员取默认值。任何成员都不能放宽，否则以 `eligibility_looser_than_default` 拒绝。
- 为了实现这一点，X1 的两个函数按 0290 的函数体重定义，只增加了 eligibility 这一项，并已在复制件守护登记：
  - `runtime.nexloop_experiment_registration_problem`
  - `runtime.nexloop_experiment_registration_frozen`
- 0292 之前冻结的登记没有 `eligibility` 成员，读取时按默认规则处理。
- **按阶段区分**：persona 检查只用于 online 阶段。screening 和 shadow 阶段的消费者都在模拟世界或影子世界中，本来就不是真人；联系限制和人工接管两项在各阶段都检查，按实验所在世界查。
- **不合格的消费者**：记一行排除原因（`persona_not_real`、`contact_restricted`、`in_takeover`），计入排除计数，之后不再重试。

## 2. 分配（D2）

- `control.nexloop_experiment_allocations`：每个（实验、消费者）一行，记录会话聚类键、组别（candidate 或 control）或排除原因。只能追加，update、delete、truncate 一律拒绝。同一消费者在同一实验里始终在同一组。
- `runtime.nexloop_experiment_assign(tenant, experiment, consumer, cluster)`：
  - **何时分配**：只在实验处于 started 且未到结束时间时分配新消费者；已经分配过的消费者直接返回原来的组。
  - **会话聚类**：调用方传入会话聚类键，不传时就是消费者本身。同一聚类只有一个组；如果聚类所在组已满，新成员以 `candidate_cluster_full` 或 `sample_full` 排除。
  - **新聚类怎么分**：用分配种子算哈希，按登记的比例 candidate /（candidate + control）抽签。
  - **比例与上限**：候选组任何时刻都不超过 `sample_size.candidate`（最多 20），也不超过对照组的人数，所以对照组在任何时刻都至少占 50%。对照组最多 `sample_size.control`；两组都满后，新消费者以 `sample_full` 排除。
  - **不重复参加**：同一世界里另一个进行中的实验已经分配了这个消费者，就以 `in_other_experiment` 排除。
  - **D9**：实验选用了孪生时，每次新分配前都检查孪生当前可选用，否则以 `twin_not_calibrated` 拒绝。已有的分配保留不动。
- 按实验加 advisory 锁，所以并发分配下计数仍然准确。

## 3. 决策时按组取策略

- `runtime.nexloop_experiment_strategy_for(tenant, world, consumer, cluster)` 返回：
  - 实验进行中：候选组取候选策略版本，对照组取对照策略版本，都附带该版本的杠杆；
  - **实验停止或到了结束时间**：所有人都回到对照策略（`applied: control`）；
  - 对照策略为 null 时返回的策略也是 null，表示沿用当前策略；
  - 消费者不在任何实验里：返回 null，沿用当前策略。
- 这两个函数**只选择策略版本**。外部效应仍然要过全部外联闸门和三层真实派发开关；两个函数都不读也不写真实派发开关，测试会核对开关表前后一致。
- **接线还没做**：没有把这两个函数授予任何应用角色，现在只有在 owner 下运行的函数能调用。接入外联的决策路径和 Context 装配放在后续切片，需要重定义外联或装配函数。

## 4. 与 X1 的关系

- 开始和停止仍然是 X1 的 `nexloop.experiment.start` / `stop`，只有负责人能执行。
- online 阶段在 X5 的停止规则与预算阻断完成之前，仍然以 `online_not_available` 拒绝开始。online 的资格测试用直接写入的 started 事件模拟开始。

## 5. 测试

`tests/test_nx043_allocation_pg.py`（10 项）：

- 资格规则冻结在预登记中，放宽被拒；
- 分配稳定，并且不可修改；
- 同一会话聚类在同一组；
- 每一步都满足比例和上限；
- 不合格的消费者被排除并计数（联系限制、人工接管、已在另一个实验中）；
- online 阶段只接受 persona 为 real 的消费者；
- 决策按组取策略，停止后回到对照；
- 校准被收回后不再分配新消费者（D9）；
- 开关表不变，函数体不涉及真实派发；
- 应用角色都不能执行这两个函数，开始和停止仍然只有负责人能执行。

X1 的 10 项测试也一起重跑，全部通过。

## 6. 未完成项

1. 把 `strategy_for` 接入外联决策与 Context 装配（后续切片）。
2. X5：护栏指标、自动停止、实验级预算阻断；完成后 online 才能开始。
3. 消费者被删除时，要从两组中同步移除并计数（设计 §3），目前还没有接入删除传播。
4. 同一消费者以不同的会话聚类键出现时，保留它最初的组，不会把聚类合并。

## 7. L6 审查的处理（2026-10-21）

| 项 | 处理 |
|---|---|
| 3.1（确认）候选簇后加入的成员没有检查“候选组不超过对照组” | 已修复（`0293_nx043_allocation_review.sql`，第九批预演编号）：簇已经在候选组时，新成员也要满足候选组不超过上限、并且不超过对照组人数这两个条件，不满足就以 `candidate_cluster_full` 排除。这里不把新成员改分到对照组，因为那样会把一个会话簇拆到两边，造成泄漏（D2、AT-048）。测试 `test_a_growing_candidate_cluster_never_breaks_the_share_or_the_cap`：簇不断加入新成员，每一步都核对两个约束 |
| 1.1 正在被擦除的 Consumer | 已修复：以 `erasing` 排除（`runtime.nexloop_consumer_erasing`） |
| 4.1 返回策略时不再检查策略状态 | 已修复：决策时，候选组只有在候选策略版本仍是已发布、并且选用的孪生仍可选用时，才取候选策略，否则取对照策略；对照策略已作废时返回 null，沿用当前策略 |
| 1.2 资格只在首次分配时检查 | 不改：派发时仍然会检查联系约束、偏好与接管（AGENTS 不变量），所以分配之后出现的限制不会导致违规联系；分配之后的拒绝与退订是 X5 的护栏指标；这类消费者对实验结论的影响留给 X6 的统计层处理（排除计数） |
| 2 cluster key 由调用方传入 | 记为接线要求：接入决策路径时，由服务端根据会话或身份链接推导 cluster key，不接受调用方任意传入 |
| 6.1 X4 之前的登记请求重试会失败 | 写明：只影响用原 request 重试，不影响已存储的数据；部署时没有尚未完成的登记请求，就没有影响 |

两个函数 `runtime.nexloop_experiment_assign`、`runtime.nexloop_experiment_strategy_for` 按 0292 的函数体重定义，只做了上面这些改动，已在复制件守护登记。排除原因的检查约束增加了 `erasing`。

## 8. 第二轮审查（L6，2026-10-21）：擦除义务与建议项

迁移 `0295_nx043_erasure_and_review.sql`（临时编号）。重定义的函数都按最新版本的函数体，只做了下面这些改动，已在复制件守护登记。

- **3.1（确认）分配表与擦除**：
  - 正在被擦除的 Consumer 调用 `assign` 时**不写任何行**，直接返回 `{arm: null, excluded_reason: erasing, allocated: false}`；`strategy_for` 对他返回 null（3.3.b）。
  - 分配表纳入 NX-029 的擦除传播：擦除进入 purging 时，由 `control.nexloop_consumer_erasures` 上的触发器把该世界里他的分配行改写为随机墓碑值：
    - `consumer_id` 和 `cluster_key` 换成墓碑值，组别和时间保留，统计不受影响；
    - 其他行中 `cluster_key` 等于他 ID 的，也一并替换。
  - `nx043_frozen` 只对擦除路径放行（`runtime.nexloop_erasure_permits`，做法同 `sim_ledger_guard`）。触发器只关闭自己这张表的擦除授权，不影响擦除步骤在同一事务里打开的其他表。
  - **`experiment_spend.spend_ref` 不含 Consumer**：它是 Run 的预留 ID（`run:<uuid>`），或调用方的引用（有格式检查），实验代码从不由 Consumer 推导它。
- **结构测试** `tests/test_consumer_erasure_coverage_pg.py`：每张带 `consumer_id` 列的表，必须满足下面三者之一：
  - 表名出现在某个擦除函数或擦除触发器的函数体里；
  - 登记了不需要擦除的理由（目前 7 张：模拟世界与孪生表，只有合成 Consumer，随世界整体删除）；
  - 在 **已知缺口清单** 里。
- **已知缺口**：检查发现，real 中有 21 张表带 `consumer_id`，却不在 NX-029 擦除范围内。这是 NX-043 之前就存在的问题，不属于本切片。
  - 其中 5 张存有顾客本人的文字：`contact_refusal_hits.matched_text`、`marketing_optouts.matched_text`、`outreach_sends.body`、`takeovers.reason`、`reply_escalations.detail`。
  - 我没有把它们写成“不需要擦除”。这份清单只能缩小：哪张表被擦除覆盖了，测试就要求把它移出清单；新表不能加进来。
  - 补齐这些表的擦除需要单独立项。

## 9. 同一组对比的排除（X6 接线要求，L3，2026-10-21）

迁移 `0300_nx043_same_comparison.sql`（临时编号）。

- **规则**：一个 Consumer 如果已经在同一组对比的另一个实验里分了组，新的分配就以 `in_same_comparison` 排除，不论那个实验现在处于什么状态。同一组对比是指：候选和对照的策略版本都相同、同一租户、同一 world。这样可以避免多轮实验之间交叉污染。
- **不写任何行**：排除时不写分配行，与 erasing 的做法一致。
- **墓碑不会触发**：被擦除者的行已经改成假名，所以不会与任何人匹配。
- **重定义**：`assign` 按 0295 的函数体重定义，只加了这一项检查，已在复制件守护登记。
- **待对齐**：集成时要与 L3 的 `runtime.nexloop_experiment_leave_out` 对齐。
- **测试**，三项：
  - 第 1 轮的对照者在第 2 轮被排除，第 2 轮不写行，新人照常分配；
  - 不同对比的实验之间不受影响；
  - 被擦除者的墓碑不会排除任何人，被擦除者本人仍以 erasing 排除。
