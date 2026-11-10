# R76：Consumer v2 发布后恢复效应链（ADR-046，迁移 effect_scope_successor_proof）

- **起因**：stage 2026-11-09 04:53Z 发布 Consumer v2 后，relay 对所有已有顾客的回复都失败在 effect_bind（`registrar schema scope unavailable`）。customer-5（Consumer v1）正常。
- **本次部署包含**：
  - 迁移：registrar 改为 active 的精确版本；效应 intent 的 Consumer scope 可由人工发布的等价后继证明，并留痕；registrar 选版函数；create 版本函数；就绪自检；`relay_replies` 指标。
  - 代码：effect_contexts 选版；新对象写最新已发布版本；`stage-principals.v1.json` 的 registrar follow 声明。
  - 告警规则：v37。
- **执行者与核对者**：
  - 迁移、register、规则发布和只读核对：调度员在 DATA_HOST 用文件凭据执行。
  - 三条验证消息：负责人从负责人终端发出。
  - 口令从 Keychain 经 stdin 传入，不进 argv。

## 步骤

1. **备份**：`sudo -n systemctl start nexloop-pg-diff.service`，确认本次备份成功后再继续。
2. **部署（含迁移）**：按 release manifest 部署镜像，执行 migrate。核对：
   - `control.schema_migrations` 的尾部是本迁移（编号以组批后为准）；
   - `bootstrap.verify` 通过；
   - 各服务 `/health/ready` 为 200，`reasons` 中没有 `schema_create_uncovered`。
3. **`stage_secrets.py register --dry-run --configurator-dsn-file …`**（必须带 DSN，才会计算派生授权），输出存档为证据。预期：
   - `follow_latest.derived` 恰好 2 条：`relay_planner` 和 `role_planner` 的 `eios:action:nexloop.plan.bind_effect_context:2`；
     - 每条的 `scope_diff` 都是 `[{"type":"Consumer","from":1,"to":2}]`；
     - purpose 都以 `(schema publication <id>)` 结尾；
   - `refused` 为空；
   - 还会列出 R74 已派生的 `claim_matcher → Consumer.edit:2`；
   - `business_owner → configure_control` 不派生：EffectControl 没有新版本，configure_control 没有 `:2`。
   - 结果不同，或命令以 `follow_latest_refused` 退出，就停下，报调度员。
4. **`register`**：去掉 `--dry-run` 后执行。预期 `grants_added` 只有上面派生的 `:2` 授权。
5. **规则**：用 configurator 发布 `deploy/configuration/alert-rules.v37.json`。预期下一次评估的 `rules_version` 为 37。
6. **只读核对**（tenant 替换为实际值）：
   ```sql
   -- registrar :2 已授权给两个 planner
   select entity_key[1],entity_key[2] from authz.nexloop_authority_facts
    where tenant_id='<TENANT_ID>' and fact_kind='grants' and entity_key[2]='eios:action:nexloop.plan.bind_effect_context:2';
   -- 对象版本分布（预期旧顾客为 2，customer-5 为 1；本步不改任何对象）
   select schema_version,count(*) from ontology.objects where tenant_id='<TENANT_ID>' and world='real' and type_name='Consumer' group by 1;
   -- 就绪自检与告警指标
   select authz.nexloop_schema_create_coverage();
   select runtime.nexloop_metrics_snapshot('<TENANT_ID>','real')->'relay_replies';
   ```
7. **三条验证消息**（负责人），每条都要在 60 秒内收到回复：
   1. s6c-1（旧顾客，Consumer v2）：预期 `control.nexloop_effect_scope_proofs` 新增一行，executed 为 `…service.request:1`，proof 为 `…service.request:2`；
   2. customer-5（Consumer v1）：预期走 `:1`，不新增证明行；
   3. 新建顾客（按 `run-add-synthetic-customer.md`）：预期新 Consumer 的 `schema_version=2`（Q6），回复同样成功，并新增一行证明。
   - 核对：
     ```sql
     select executed_resource,scope_proof_resource,publication_id,consumer_version,recorded_at from control.nexloop_effect_scope_proofs
      where tenant_id='<TENANT_ID>' order by recorded_at desc limit 5;
     select status,last_code,count(*) from runtime.nexloop_work_feed where tenant_id='<TENANT_ID>' and feed='claim-match' group by 1,2;
     ```
   - 任一条失败，就停下，取 relay 日志中 `message relay attempt failed` 的 stage 和 reason，报调度员。

## 不做的事

- 不改 customer-5 的 v1 对象（L6 Q7）。之后如果要统一到 v2，走 0254 同款的受治理批量升级，另写运行单；`:1` 退役前要先断言已经没有 v1 对象。
- 不停用任何 `:1` 定义，不手工改 SQL。
- 发布前门禁（Q8）和发布后冒烟（Q9）另立 NX-068。
