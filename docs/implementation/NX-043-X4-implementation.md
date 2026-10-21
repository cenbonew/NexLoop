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
