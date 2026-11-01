# 运行单片段：I36-e 已擦除消费者遗留的无日期承诺（stage）

- 适用分支：`l2/i36e-erasure-backfill`，基于第 84 批 579a8ff8。迁移 0394（临时号）。
- 内容：0388 §d 的存量块，去掉"登记行仍在"这个前提。规则、事件、转换都与 0388 相同，幂等。
- 执行者：调度员。判定者：L6。证据只记 id 前缀、计数和状态，不展示个人内容。

## 1. 迁移前只读预检（必须）

```bash
ro "select o.object_id from ontology.objects o where o.tenant_id='<TENANT_ID>' and o.type_name='Commitment' and o.properties->>'made_to' ~ '^erasure:[A-Za-z0-9._:-]{1,190}$' and o.properties->>'status' in ('conditional','open','in_progress') and (o.properties->>'due_at' is null or exists(select 1 from runtime.nexloop_commitment_events e where e.tenant_id=o.tenant_id and e.world=o.world and e.subject_ref='commitment:'||o.object_id and e.kind='cancel_requested'))"
ro "select properties->>'status', count(*) from ontology.objects where tenant_id='<TENANT_ID>' and type_name='Commitment' group by 1 order by 1"
```

- 第一条预期**恰好 1 行**，id 以 `6135e98283ea` 开头。**多于 1 行或 id 不同就停下**，交调度员，不要迁移。
- 第二条预期 cancelled 155、open 1，作为对照。

## 2. 迁移

- 按部署单先备份，再迁移 0394。
- 迁移块在迁移事务里执行一次；它不是函数，以后不会再运行。

## 3. 迁移后读回

```bash
ro "select properties->>'status', count(*) from ontology.objects where tenant_id='<TENANT_ID>' and type_name='Commitment' group by 1 order by 1"
ro "select kind, coalesce(detail->>'source', detail->>'reason') from runtime.nexloop_commitment_events where tenant_id='<TENANT_ID>' and subject_ref like 'commitment:6135e98283ea%' and kind in ('cancel_requested','status') order by event_number"
```

- 第一条预期 cancelled 156、open 0。
- 第二条预期恰好两行：`cancel_requested consumer_erasure`，`status consumer_erasure`。
- 第 1 步的第一条查询重跑，预期 0 行。

## 撤回

作废不能撤回。这条承诺对应的客户已经被擦除，没有可以恢复的对象。
