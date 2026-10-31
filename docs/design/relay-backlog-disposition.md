# relay 旧积压的处置（S42，已决定：方案 A）

状态：负责人 2026-10-30 选方案 A，customer-1 那条直接结束、不回复；已实现（迁移 relay_backlog_settle，`nexloop.relay.backlog_settle:1`，部署单 `docs/release/run-relay-readiness-stage.md`）。起因：L6 S42。

## 1. 现状

stage 的 real 世界有 51 条 `runtime.nexloop_message_outbox` 里 `status='pending'` 的入站消息，没有 relay 会再处理它们：

| 来源 | 条数 | 为什么没人处理 |
|---|---|---|
| customer-1 | 1 | 10-22 装配整体过期期间到达，当时 relay profile 为空；重建后若该 consumer 仍无 active 登记，则一直不会被 claim |
| 已擦除的 consumer | 50 | 擦除（NX-029）把 consumer 标为 erasing / erased，`claim_next` 跳过它们，但 outbox 行仍是 `pending` |

后果：`relay_backlog_age`（`relay.oldest_pending_seconds` > 600）和 `relay_unenrolled_pending`（`relay.unenrolled_consumers_pending` > 0，critical）一直在报。告警长期常亮会让人习惯性忽略，新的真实积压就淹没在里面。这和 10-22 的情况是同一类问题：信号存在，但没人看。

`outbox.status` 现在只允许 `pending` 和 `delivered` 两个值（0046 的 check）。

## 2. 方案 A：受治理地结束这些消息

新增一个负责人专属的受治理 Action `nexloop.relay.backlog_settle:1`（owner-only），并新增迁移：

- `outbox.status` 增加终态 `settled`，另建追加写的记录表，记 `message_id`、`reason`、操作人和时间。
- `reason` 只允许两种：
  - `consumer_erased`：SQL 核对该 consumer 处于 erasing / erased；
  - `owner_declined`：负责人明确决定不回复，必须填写理由。
- 对已擦除的 consumer 还有一个补充修法：擦除流程在确认擦除的同一事务里，把该 consumer 的 pending 消息一并结束（`consumer_erased`）。这样以后不会再积压。
- 已有的 50 条由负责人执行一次 `backlog_settle`，按 `consumer_erased` 结束。
- customer-1 那 1 条有两种处理：
  - 负责人先给它重新登记，让 relay 正常回复（推荐，客户的消息应该得到回复）；
  - 或者负责人执行 `owner_declined`。

优点：
- 数据本身变正确，告警按原有规则自然消失，没有任何“例外”需要长期维护。
- 每一次结束都有审计记录，符合“只有受治理的 Action 才能改业务状态”。
- 擦除的补充修法从根上去掉了 50 条这一类积压。

代价：
- 一个新 Action、一份迁移，加 workbench-roles 新版本。按清单还要更新 register 计数和擦除覆盖测试。
- `owner_declined` 不可撤销：结束后这条消息不会再被回复。

## 3. 方案 B：告警显式排除，排除名单写进配置

新增受信配置（例如 `deploy/configuration/relay-backlog-exclusions.v1.json`，由配置员窗口应用）。名单按 `consumer_id` 或 `message_id` 列出，每一项必须带 `reason` 和 `expires_at`。

- `relay.oldest_pending_seconds` 和 `unenrolled_consumers_pending` 计算时跳过名单里的项。
- 快照另外报 `excluded_pending`，这个数字本身可见。
- 名单为空时行为与现在相同。

优点：
- 不改业务数据，不新增 Action。
- 只需要改指标 SQL 和一份配置，改动小、见效快。

代价：
- 消息仍是 `pending`：数据层面没有改正，其他读这张表的地方（工作台积压数、未来的 SLA 统计）还会看到它们。
- 名单是要长期维护的例外：
  - 必须设上限并按 `expires_at` 到期，否则会变成“永久静音”；
  - 以后擦除一个 consumer 就要再改一次名单。
- 告警可能被名单盖住：如果名单误写了一个仍在服务的 consumer，它的新积压也会被隐藏。我们的审查规则要求“覆盖为零不能读作没有问题”，这里正好相反，是人为造出一个覆盖缺口。

## 4. 比较与建议

| | A 结束消息 | B 排除名单 |
|---|---|---|
| 数据是否正确 | 是（终态 + 审计） | 否（仍 pending） |
| 是否需要长期维护 | 否（擦除时自动结束） | 是（名单、到期） |
| 误隐藏新积压的风险 | 无 | 有 |
| 实现成本 | 迁移 + Action + 角色清单 | 指标 SQL + 配置 |
| 客户的 1 条消息 | 可以先恢复服务再回复 | 被静音，不会得到回复 |

建议采用 **A**：
- 已擦除 consumer 的 50 条，由擦除流程自动结束，存量执行一次 `consumer_erased`。
- customer-1 的 1 条，先恢复它的 relay 登记（10-30 重建后核对 `relay_readiness.active_enrollments` 是否包含它），让消息得到回复。

B 只适合作为 A 上线前的临时措施。如果采用，名单的 `expires_at` 不超过 A 上线的日期。

需要负责人决定：
1. 选 A 还是 B；
2. customer-1 这条消息是“恢复服务后回复”，还是“不回复，结束”。

## 5. 已知偏差：R60 的理由码（stage 2026-10-31）

R60 按方案 A 结束了 51 条积压（`s42-settle:20261031-1`），51 条都已 `settled`，pending 为 0；但审计表里 51 条的理由码都是 `no_serviceable_enrollment`，部署单预期其中 50 条为 `consumer_erased`。

原因：擦除完成时（0305）把会话的 `consumer_id` 换成新的 `pseudonym:<hex>`，擦除表仍按原 consumer_id 记录。0376 用会话上的 consumer 去问 `runtime.nexloop_consumer_erasing`，对假名得到 false，于是落进了第二个分支。结束动作本身没有错（这 50 条确实没人会再服务），错的只是理由码的分类。

修复：迁移 relay_backlog_pseudonym（0382）把 consumer 为 `pseudonym:` 开头的消息判为 `consumer_erased`。只有擦除会往会话里写假名，所以这个判断是充分的。擦除触发器那条路径不受影响：它在擦除离开 `pending_confirmation` 时按原 consumer_id 结束消息，比 0305 的假名化（`completed` 时）早，因此只影响 0376 上线前已擦除完成的 consumer。

审计表只追加写，stage 已有的这 50 行不改。判读方法：理由码为 `no_serviceable_enrollment`、但会话 consumer 以 `pseudonym:` 开头的行，按 `consumer_erased` 理解。

```sql
select s.reason,(cv.consumer_id like 'pseudonym:%') erased,count(*) from runtime.nexloop_relay_backlog_settlements s
  join runtime.nexloop_message_outbox b using(tenant_id,world,message_id)
  join runtime.nexloop_conversations cv on cv.tenant_id=b.tenant_id and cv.world=b.world and cv.conversation_id=b.conversation_id
where s.tenant_id='<TENANT_ID>' group by 1,2;
```

R60 的预期读数：`no_serviceable_enrollment / true` 50（即实际的 consumer_erased），`no_serviceable_enrollment / false` 1（customer-1）。
