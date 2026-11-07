# 运行单：测试顾客模型故障开关（ADR-045，stage）

- **状态**：草稿。执行者：调度员；负责人在终端执行的步骤单独标出。判定者：L6。
- **适用**：分支 `l7/synthetic-model-fault`。迁移 `synthetic_model_fault`，临时号 0423，以组批后的编号为准。设计 `docs/design/synthetic-model-fault.md`。
- **用途**：为 NX-038/039 的 stage 实测（AT-023、AT-048、S6-R1 等）补上"失败且带检索未命中"的合成对话。失败口径不变。
- **证据**：只记计数、run_id 前缀和开关 id，不记消息内容。执行者和判定者分开写，并注明"失败由 ADR-045 故障开关注入"。

## 0. 生产隔离核对（每次生产发布后都做，不只这一次）

生产库里不应有这个 Action，四张表都应为空：

```bash
ro "select count(*) from control.nexloop_action_definitions where resource_id like 'eios:action:nexloop.synthetic_fault.set:%'"
```
```bash
ro "select (select count(*) from control.nexloop_synthetic_fault_switches),(select count(*) from control.nexloop_synthetic_fault_members),(select count(*) from runtime.nexloop_synthetic_fault_decisions),(select count(*) from runtime.nexloop_synthetic_fault_events)"
```

五个数都应为 0。不为 0 就停下，报调度员和 L6。

## 1. stage 部署（调度员）

1. 部署前备份，按现行流程执行 `sudo -n systemctl start nexloop-pg-diff.service`。
2. 部署本批并迁移。迁移尾为组批后的编号，`versions.lock.json` 的 `bootstrap_revision` 与之相同。重建 api 和 agent-host。
3. **部署标记**（只在 stage，调度员新建一次）：在 stage 的私有部署目录（与 `stage_secrets.py --out` 同一棵私有树）里放一个 0600 文件，内容为：

   ```json
   {"schema": "nexloop-deployment-marker/1", "environment": "stage"}
   ```

   生产的部署目录里不放这个文件，或者写 `"environment": "production"`。

4. **发布 stage 清单**：在平常的 publish 命令上加两个参数。先跑 dry-run，结果里应当有 `stage_manifest`（路径和 sha256）：

   ```bash
   python3 scripts/deploy/stage_secrets.py publish --out <私有树> --tenant <TENANT_ID> --dry-run --stage-capabilities deploy/stage/capabilities.stage.v1.json --deployment-marker <私有树>/deployment-marker.json
   ```

   然后按现行发布流程去掉 `--dry-run` 正式发布，`--configurator-dsn-file` 或 `--bootstrap-dsn-file`、`--signing-key-id` 与平常相同。预期 `actions_to_publish` 里只多出 `nexloop.synthetic_fault.set`。

5. **工作台角色**：按现行流程执行 `workbench_roles --apply`，**加上同样两个参数** `--stage-capabilities deploy/stage/capabilities.stage.v1.json --deployment-marker <私有树>/deployment-marker.json`，负责人角色才会加上这个 Action 的授权。全局的角色清单 `workbench-roles.v1.json` 里没有它，所以生产不会写这条授权；以后每次在 stage 重新执行 `workbench_roles --apply` 都要带这两个参数，否则这条授权会被收回。
6. **Host 配置**：在 stage 的 agent-host 运行时配置里加 `"synthetic_model_fault": true`，重建 agent-host。没有这个键时，Host 会拒绝带故障标记的准入，Run 不会开始。
7. 只读核对：

   ```bash
   ro "select resource_id, active from control.nexloop_action_definitions where tenant_id='$TENANT_ID' and resource_id like 'eios:action:nexloop.synthetic_fault.set:%'"
   ```

   应有一行，`active` 为 t。

## 2. 演练

**前提**
- 两个 relay 都用 `ontology_hybrid`（2026-11-07 已核对）。
- 没有在运行的 NX-037 run 入口：它在错误率高时会自动暂停。
- 模型预算和 evolution 预算都有效。
- 四个测试顾客：s6c-1、s6c-2、s6c-3，以及第 4 个（候选见设计 §7）。

**步骤**
1. **预热**（开关关闭）：四个顾客各发一轮普通问题，让两个 relay 的语义读取池热起来。冷池时第一次读取可能超过 0.5 秒而不留记录。
2. **打开开关**（负责人，工作台或 approval_drill，与 run-nx038 §2.0 的驱动方式相同，只用合成 owner）：

   ```json
   {"state": "open", "consumer_ids": ["<4 个 consumer_id>"], "remaining": 12, "duration_seconds": 3600, "reason": "NX-038/039 S6 P5（ADR-045）"}
   ```

   预期 200，`opened: true`，`consumers: 4`。快照 `runtime.nexloop_metrics_snapshot('$TENANT_ID','real')->'synthetic_fault'` 为 `{"active": 1, "remaining": 12, ...}`。
3. **发言**：四个顾客各发 3 轮，间隔不少于 1 分钟。每轮都带同一个未知词（例如"霁蓝釉陶片拼贴"），每个顾客再各加一个不同的词。
4. **每轮核对**（只读）：这个 Run 的失败应为 `runtime_task_failed` / `synthetic_model_fault`，并且至少有 1 条检索未命中：

   ```bash
   ro "select runtime.nexloop_nx037_run_evidence('$TENANT_ID','real','<RUN_ID>')->'failure', (select count(*) from runtime.nexloop_semantic_accesses a where a.tenant_id='$TENANT_ID' and a.run_id='<RUN_ID>' and a.kind='access' and a.coverage<1)"
   ```

   大约 5 分钟后会有兜底回复，这是预期。
5. **关闭开关**：`{"state": "closed", "consumer_ids": [], "remaining": 0, "duration_seconds": 0, "reason": "演练结束"}`，或者等次数用完、到期自动失效。
6. **审计核对**：

   ```bash
   ro "select kind, count(*) from runtime.nexloop_synthetic_fault_events where tenant_id='$TENANT_ID' group by 1"
   ```

   应有 12 条 injected，以及 1 条 closed 或 exhausted。
7. 等兜底回复都发出后，跑 `s6_stage_inputs.py`，`consumers_with_miss` 应不少于 4；然后按 run-nx038 的顺序执行 at023、r1。

**说明**
- 重跑开关的打开请求（例如网络中断后重试）用同一个 Idempotency-Key 会按重放处理，不会多开一个开关。
- 开关打开期间，错误类告警属于预期，快照的 `synthetic_fault.active` 为 1。B5 的失败率不受影响（它不统计模型失败的 Run），证据里注明开关打开的时段。

## 3. 回退

- 关闭开关，或等它自动失效；从 stage Host 配置里删掉 `synthetic_model_fault`，重建 agent-host。
- 不需要撤回 Action；迁移不回退：没有生效的开关时，判定不会命中任何 Run，也不写表。
