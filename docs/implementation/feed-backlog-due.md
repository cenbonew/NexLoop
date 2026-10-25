# work feed 积压时长只算已到期、未被领取的项（stage 告警 feed_backlog_age:commitment-monitor）

- **来源**：调度员 2026-10-26 派工。stage 的 `feed_backlog_age:commitment-monitor` 从 10-16 起一直 firing，当前值约 296000 s；规则版本 v13。
- **分支**：`l7/feed-backlog-scheduled`，基点 origin/main e8a22d72。
- **迁移**：`feed_backlog_due`，临时编号 0354。main 的尾部是 0351，第三十批占 0352、0353，合入时由调度员重排。

## 原因

- **规则**：`feed_backlog_age`。v1 起各版本相同，v13–v16 也没变：
  - `metric`：`feeds.*.oldest_pending_seconds`；
  - 条件：`gt`，阈值 600；`for_seconds` 为 0。
- **指标的计算位置**：`runtime.nexloop_metrics_snapshot` 的 `feeds` 块。
  - 由 0131 定义，0141 重定义时原样保留；
  - 算法：`now - min(changed_at)`，对象是该 feed 中**所有** `status='pending'` 的项。
- **0149 加过 `due`、`scheduled`、`oldest_due_seconds`**，但没有改 `oldest_pending_seconds`，规则也仍然读它。另外，0149 的 `oldest_due_seconds` 同样是从 `changed_at` 起算的。
- **承诺监视器的写法**：
  - 每处理完一个阶段，它在原行上把 `available_at` 改到下一阶段，`changed_at` 改为这次处理的时间。见 `nexloop_commitment_command` 的 next stage 分支（0111，0263 重定义）。
  - 这一行仍然是 `pending`，不会删除。
  - 结果：只要还有下一阶段，处理完 10 分钟后，这个指标就超过 600。一直到下一阶段到期，再处理一次，才会短暂回落。
- **调度员读到的那 10 条**：
  - `available_at` 在 7 天后；`attempts` 为 0，没有租约，从未被领取；
  - `changed_at` 等于 `created_at`，都是 10-22 07:17Z，所以 now − changed_at 等于 now − created_at，约 3.4 天。
  - 调度员推断“用 created_at 计算”，实际用的是 `changed_at`，对这些行两者相同。结论一致：未到期的定时项被当成了积压。

## 10-16 起的那一段是不是同一个原因

按代码推断是同一个机制（我没有连 stage，结论需要用下面的只读查询核实）：

- 告警评估每分钟一次。指标只要一直大于 600，`breached_since` 就不会重置，告警也不会解除。
- 10-16 起，stage 上的承诺一直有等待下一阶段的监视项。处理一次之后，行留在 `pending`，`changed_at` 变成处理时间，下一分钟起指标又只增不减。
- 10-22 07:17 那次处理之后，值从 0 附近重新累积到现在的约 296000 s。

工作 feed 的行处理完会删除或原地改写，不留历史，所以下面的只读查询只能间接核对。请在 stage 只读会话（run-nx056 §0.3 的 `ro`）中执行：

```
ro "select breached_since, firing, first_fired_at, fire_count, last_value, rules_version from control.nexloop_alert_state where dedupe_key='feed_backlog_age:commitment-monitor'"
ro "select kind, value, recorded_at, rules_version from control.nexloop_alert_events where dedupe_key='feed_backlog_age:commitment-monitor' order by event_id"
ro "select count(*) filter (where available_at>clock_timestamp()) scheduled, count(*) filter (where available_at<=clock_timestamp()) due,
    min(available_at), min(changed_at) from runtime.nexloop_work_feed where feed='commitment-monitor' and world='real' and status='pending'"
```

- **同一原因的预期**：
  - `first_fired_at` 在 10-16；`fire_count` 为 1，或者很小；
  - 10-16 之后没有 `resolved` 事件，或者只在“当时没有任何 pending 行”的时刻有；
  - 现在 `due=0`、`scheduled=10`。
- **出现以下情况就不是同一原因，需要另查**：某段时间 `due>0` 而且这些项长时间没有被领取，比如承诺监视器停了。

## 修复

迁移 `feed_backlog_due`：`runtime.nexloop_metrics_snapshot` 再改名包一层，做法与 0149 相同。每个 feed 的两个值改为同一口径：

- `oldest_pending_seconds`，以及 0149 的 `oldest_due_seconds`；
- **统计范围**：`status='pending'`、`available_at <= now`，并且没有被有效租约占用（`lease_until` 为空或已过期）的项；
- **起算时间**：`available_at`，也就是项变为可领取的时刻；
- 没有这样的项时为 null。规则只读数值，null 不触发，已经 firing 的告警在下一轮评估时解除。

**为什么用这个口径**：

- **从 `available_at` 起算**：这是项“开始等待”的时刻。
  - `created_at` 和 `changed_at` 记录的是写入或上次处理的时间，对定时项没有意义；
  - 同一项在 pending 期间又被 touch 时，`available_at` 取较早者，`least(...)`，见 0111、0263 的 touch。所以它记的是“最早应当处理的时刻”，不会因为重复 touch 被冲淡。
  - job 队列的 `queues.*.oldest_pending_seconds` 一直就是这个口径（0131、0141：只算已到期的，从 `available_at` 起算），feed 与它对齐。
- **排除有效租约**：
  - 被领取的项正在处理，不算在等待。派工要求的“到期后未被领取”，指的就是这一点。
  - 租约最长 600 s。worker 卡住、租约过期之后，在下一次被领取之前，这一项重新计入，仍然从它最初到期的 `available_at` 算。
  - **但这个口径看不到崩溃循环**（L6 复核）：
    - 租约只在领取时写入（0093:103），是否进死信只在 retry 时判定（0093:128）；
    - worker 领取后崩溃，没来得及调 retry 或 complete，这一项就会在每次租约过期后被再次领取。attempts 一直涨，却永远进不了死信；
    - 它几乎一直处在有效租约中，所以 `feed_backlog_age` 不报。旧口径反而能报出来。
    - 这一类由下面的 `max_pending_attempts` 和告警规则 v17 报告。
  - 正常失败的项：worker 调 retry，`available_at` 被推后；达到它的重试上限时进入死信，由 `feed_dead_letter` 报告。
- **`pending`、`due`、`scheduled` 三个计数含义不变**（0149）。`due` 仍然包含正被处理的项。
- **`feed_backlog_age` 本身不用改**，它仍读 `feeds.*.oldest_pending_seconds`。

### 崩溃循环：`max_pending_attempts` 与告警规则 v17

- **指标** `feeds.<feed>.max_pending_attempts`：该 feed 中 pending 项（不论有没有租约）的最大 attempts；没有 pending 项时为 0。
- **规则** `feed_attempts_over_ceiling`（v17 = v16 + 这一条）：`max_pending_attempts > 20`，critical，与 `feed_dead_letter` 同级。
- **为什么阈值是 20，而且所有 feed 共用一个**：
  - 每一份 claim/retry 的 SQL（0093、0106、0109、0111、0118、0126）都把 retry 的上限限制在 1..20，并在 `attempts >= 上限` 时进死信；
  - 领取一次加 1；touch，或者处理中途来了新的改动，都会把 attempts 清零；
  - 所以 pending 项的 attempts 最多是 20，这是第 20 次领取还在处理中的情况。超过 20，说明至少有一次领取既没有 retry 也没有 complete；
  - 各 feed 的上限不在库里：它在 worker 的设置里，而且因拒绝原因而不同，从 1 到 20 都有。例如 reply-due 是 `min(20, fallback+5)`，plan-reevaluate 节流时是 20，takeover-expiry 是 20；
  - 规则的 metric 路径里写不了带连字符的 feed 名，没法按 feed 设阈值；
  - 低于 20 时，无法区分“这个 feed 的上限”和“正常的重试”。
- **代价**：上限低的 feed（例如上限 5、租约 300 s），要第 21 次领取后才告警，最长约 21 × 租约时长，也就是 1.75 小时左右。换来的是不会误报，也不依赖各 worker 的设置。
- **发布**：v17 按 run-nx057 §1 的形态用 `alert_rules_apply.py` 发布（v16 之后）。L3 的死信口径改动用 v18，组批时合并。

### 工作台（可选项 5，已做）

- “总览 → 队列积压 → 后台任务”和“运行观测 → 后台任务与提取”两张卡：
  - 改动在 `apps/web/src/workbench/pages/Overview.tsx`、`Observability.tsx`；
  - “待处理 N”改为已到期的数量（`due`，没有拆分时退回 `pending`）；
  - 有定时项时另外写出“（另有 M 个定时未到期）”；
  - “最久等待”读的就是新的 `oldest_pending_seconds`。

**部署之后**：stage 上这 10 条在 10-29 07:17Z 之前都是 scheduled，指标为 null，告警在下一轮评估时解除，产生一条 `resolved` 事件。到期之后，如果承诺监视器在 10 分钟内领取，就不会再报警。

## 测试

`tests/test_feed_backlog_due_pg.py`，真实 PG，NX-030 夹具，规则 v1（崩溃循环一项用 v17），真实告警评估：

- **定时项不计入**：
  - 按 stage 的形态造 3 条：`changed_at` 是 3 天前，`available_at` 在 3.6 天后；
  - 结果：`pending/due/scheduled = 3/0/3`，两个时长都是 null；评估后告警不 firing。
- **到期未领取的项从 `available_at` 起算**：
  - 一条一周前创建、20 分钟前到期的项，计为约 1200 s，不是一周；评估后 firing，值约 1200；
  - 一条 2 小时前到期、但租约有效的项不计入；租约过期后计为约 7200 s；
  - 删掉所有已到期的项之后，值为 null，评估后告警解除。
- **崩溃循环**：
  - 经 feed 真实的签名端口领取，不调 retry/complete，每次领取后让租约过期，再领取；
  - 每一轮，`max_pending_attempts` 都等于领取次数，等待时长为 null（有效租约）；
  - 第 1–20 轮评估不 firing，第 21 轮 `feed_attempts_over_ceiling` firing（critical，值 21）；`feed_backlog_age` 始终不 firing；
  - 最后正常 retry（上限 20）后进入死信，指标回到 0，告警解除。
- **v17 = v16 + 一条**，规则形状校验通过。
- **工作台**：`apps/web/test/workbench-observability.test.ts` 新增断言：拆分后的 feed 显示“待处理 0（另有 10 个定时未到期）”。

## 回归

- 在本分支上，跑了所有读取快照的测试，以及告警、负载报告、复制件守卫、迁移目录、`check_definer_search_path`。
- `test_42_actual_bootstrap_and_publication_checksum` 在本分支上失败，原因是它要求迁移编号连续，而 0352、0353 还空着，属于预期。把本迁移临时改为 0352 后，这个测试通过（没有提交这个改动）。第三十批进 main 之后，或者调度员重排编号之后，这个失败会消失。
