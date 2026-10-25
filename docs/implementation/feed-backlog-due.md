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
  - 租约最长 600 s。worker 卡住或退出、租约过期之后，这一项重新计入，并且仍然从它最初到期的 `available_at` 算，不会漏报。
  - 反复失败的项由 retry 推后 `available_at`，最终进入死信，由 `feed_dead_lettered` 报告，不靠这条规则。
- **`pending`、`due`、`scheduled` 三个计数含义不变**（0149）。`due` 仍然包含正被处理的项。
- **告警规则文件不需要改**，`feed_backlog_age` 仍读 `feeds.*.oldest_pending_seconds`，不涉及新版本的告警规则。

**部署之后**：stage 上这 10 条在 10-29 07:17Z 之前都是 scheduled，指标为 null，告警在下一轮评估时解除，产生一条 `resolved` 事件。到期之后，如果承诺监视器在 10 分钟内领取，就不会再报警。

## 测试

`tests/test_feed_backlog_due_pg.py`，真实 PG，NX-030 夹具，规则 v1，真实告警评估：

- **定时项不计入**：
  - 按 stage 的形态造 3 条：`changed_at` 是 3 天前，`available_at` 在 3.6 天后；
  - 结果：`pending/due/scheduled = 3/0/3`，两个时长都是 null；评估后告警不 firing。
- **到期未领取的项从 `available_at` 起算**：
  - 一条一周前创建、20 分钟前到期的项，计为约 1200 s，不是一周；评估后 firing，值约 1200；
  - 一条 2 小时前到期、但租约有效的项不计入；租约过期后计为约 7200 s；
  - 删掉所有已到期的项之后，值为 null，评估后告警解除。

## 回归

- 在本分支上，跑了所有读取快照的测试，以及告警、负载报告、复制件守卫、迁移目录、`check_definer_search_path`。
- `test_42_actual_bootstrap_and_publication_checksum` 在本分支上失败，原因是它要求迁移编号连续，而 0352、0353 还空着，属于预期。把本迁移临时改为 0352 后，这个测试通过（没有提交这个改动）。第三十批进 main 之后，或者调度员重排编号之后，这个失败会消失。
