# NX-039 V5：删除传播与观察期（实现说明，2026-10-20）

依据：NX-039 设计稿 §8.4、§9、§11 V5；D39-9、D39-10、D39-11、D39-12（负责人 2026-10-20 定案）。分支 `l2/nx039-v5`，起点 `l2/nx039-v3`（c66e2b5f）。

## 迁移 0250（临时编号，`0250_nx039_erasure_watch.sql`）

### 1. 用例引用（D39-11）

- 结论函数 `authz.nexloop_evo_evaluation_conclude` 原地替换（函数体沿用 0248）。写入报告时：
  - `swaps` 里的用例 ID、证据里的 `case:` 和 `access:` ID，都换成随机假名；
  - 假名到原始 ID（以及它所属的 Run）的对应关系，存在 `runtime.nexloop_evo_case_refs`，这张表可以删除。
- 报告只追加，里面只剩假名、digest 和计数。删除引用后，假名就无法再关联回原始 ID。
- 报告 digest 按实际存储的假名化报告计算。是否为重放，用提交内容的 digest（`runtime.nexloop_evo_report_submissions`）判断，同一份提交重放时返回原结果。

### 2. 删除传播（§9）

Consumer 的删除请求进入 purging 时（插入或状态变化都会触发），对这个 Consumer 名下的 Run 做以下处理：

- 删除指向这些 Run 的用例引用；
- 包含其中任何一个 Run 的留出集：登记作废（`runtime.nexloop_evo_holdout_invalidations`，原因 `missing_data`），并从留出集中去掉这些 Run ID。留出集本身仍然冻结，只有删除路径在事务内标志下可以“只减不增”地改动；
- 仍在评估中、用例涉及这些 Run 的候选：转为 `incomplete/missing_data`（E4 已经处理 proposed 和 accepted 状态的候选）；
- 关闭这些候选的 L1 会话，并清空快照；
- 留一条 tombstone，只记计数。

**Run 的来源**：`runtime.nexloop_evo_consumer_runs` 取两部分的并集：删除流程原有的 Run 列表（role 绑定与 runtime 绑定），以及 Consumer 的消息被路由到的 Run。原有列表不含消息 Run，只用它会漏掉。

### 3. 发布项（等 V4）

已发布发布项的删除过滤，通过钩子函数 `runtime.nexloop_evo_release_erasure(tenant, world, consumer, ref, by)` 接入：

- 本迁移里它返回 0；
- V4 合入后原地替换它，在同一个删除事务里发布一个原因为 `erasure` 的新版本（去掉以该 Consumer 为来源的发布项），并在回退时过滤这些发布项；
- 返回值计入 tombstone 的 `release_items`。

### 4. 保留期（D39-12）

- 新增保留类别 `evolution_records`，保留 365 天。保留设置升到 v6，`retention.v1.json` 同步修改。
- 到期时删除报告、审核关联、用例引用和提交 digest，留 tombstone。
- 当前版本和前一版本永久保留，由 `runtime.nexloop_evo_retained_reports(tenant, world)` 给出需要保留的报告。这里还是空实现，V4 替换它，返回当前和前一个语义版本所引用的报告 digest。
- 报告、审核关联和评估包仍然冻结，只有保留期清理在事务内标志下可以删除前两者。

### 5. 7 天观察期（D39-10）

- `runtime.nexloop_evo_watch_start(tenant, world, release_ref, published_at)` 开始 7 天观察。V4 的发布在同一个事务中调用它，没有授予任何角色。
- `runtime.nexloop_evo_release_watch` 用 E3 的记录比较两段时间的未命中率和歧义率：发布前 7 天，与发布之后到现在。
  - 任一项上升超过 2 个百分点，而且两段时间各至少有 20 次访问，就标为 regressing，并给出建议 `rollback`；
  - 不会自动回退。
- 指标快照增加 `evolution.release_watch` 块：`active`、`regressing`、`releases`。

## 告警规则 v7

`deploy/configuration/alert-rules.v7.json`：保留 v6 的全部规则，新增 `evolution_release_regressing`（warning），在 `evolution.release_watch.regressing` 大于 0 时触发。

## 测试

- `tests/test_evolution_v5_pg.py`（5 项）：
  - 报告只存假名，引用表可以把假名对应回原始 ID，重放和“报告 id 被复用”的判断照旧；
  - 删除时：只删除被删 Consumer 的引用，其他 Consumer 的保留；留出集作废并剔除被删的 Run；报告行不变；tombstone 记录计数；
  - 评估中的候选转为 `incomplete/missing_data`，L1 会话关闭，快照清空；
  - 观察期：两段比率相同时不报警；劣化超过 2 个百分点时 regressing，建议 rollback；超过 7 天的发布不再观察；观察只读，不改任何东西；
  - 保留期：未满 365 天不删；已满但属于 V4 要保留的报告不删；其余连同引用一起删除；评估包仍然冻结。
- `tests/test_evolution_v5_alert_pg.py`：告警阳性对照。应用 v7 后，比率持平时不触发，劣化后触发；v6 的规则全部保留。
- `tests/test_retention_pg.py`、`tests/test_background_services_pg.py`：keeper 的类别数从 18 改为 19。

## 需要等 V4 的部分

- 发布项的删除过滤和回退过滤：需要 V4 替换 `runtime.nexloop_evo_release_erasure`；
- 当前版本和前一版本的报告永久保留：需要 V4 替换 `runtime.nexloop_evo_retained_reports`；
- 观察期的起点：V4 发布时调用 `runtime.nexloop_evo_watch_start`。
