# 执行单：NX-068 发布前门禁与发布后冒烟上线

内容：迁移 `nx068_publish_gate_smoke`（0449）、stage register 写 follow 标记、告警规则 v38。设计见 `docs/planning-drafts/nx068-publish-gate-and-smoke-20261111.md` §10。

## 0. 时间窗口（C4 / H-d）

- **部署不得落在 P5 执行单 §4–§6 之间。** 要么在 P5 §4 开始之前完成本单全部步骤，要么等 P5 §6 结束以后再做。
- 原因：本单会换掉 `ontology.nexloop_schema_publication_gates` 和 `runtime.nexloop_observability_maintain`，还会改写授权 fact。P5 §4–§6 期间的判定和观测不能有两套口径。
- 开始前确认 P5 的进度，并在部署记录里写明是“P5 §4 之前”还是“P5 §6 之后”。

## 1. 部署与迁移

1. 部署包含本提交的镜像并迁移。迁移尾与 `versions.lock.json` 的 `bootstrap_revision` 相同。
2. 迁移后只读核对：

   ```sql
   select to_regclass('runtime.nexloop_schema_publish_smokes') is not null,
          to_regprocedure('authz.nexloop_effect_successor_admissible(text,text,jsonb,jsonb,jsonb,jsonb,integer,jsonb)') is not null,
          has_function_privilege('nexloop_api','authz.nexloop_read_schema_publish_readiness(text,text,text,text,text)','execute');
   ```

   三列都应为 true。

## 2. K1：先 register，再核对 readiness

**0449 上线后、任何 Schema 发布之前先做。** 没有 follow 标记时，门禁会拒绝 `Consumer` 的发布（`registrar_holder_without_follow`、`create_holder_without_follow`）。

1. dry-run（命令照最近一次部署单的 `stage_secrets.py register --dry-run`）。**预期恰好改写 4 条授权**：
   - relay_planner：`eios:action:nexloop.plan.bind_effect_context:1`
   - role_planner：`eios:action:nexloop.plan.bind_effect_context:1`
   - business_owner：`eios:action:Consumer.create:1`
   - business_owner：`eios:action:nexloop.effect.configure_control:1`

   每条只是 `grant_id` 加后缀 `:follow_latest_version`，business_owner 的两条在同一条 fact 里（共 3 条 fact）。**不新建凭据、不轮换、没有其他差异。** claim_matcher 的 `Consumer.edit:1` 虽然声明了 follow，但不在 `FOLLOWABLE` 内，不会改。
   - dry-run 出现 `credential_rotation_required`、新主体、其他授权差异，或者改写数不是 4：**停止**，不要正式执行，把输出交给调度员。
   - 注意：dry-run 的输出不含 `grant_id`，看不到上面 4 条改写本身。正式执行前后各取一次全量 `grant_id` 快照（每行：主体、资源、`grant_id`、去掉 `grant_id` 后授权内容的 md5），两次快照的 diff 应恰好是这 4 行，且只差 `:follow_latest_version` 后缀、md5 不变（R77 采用此法，L6 R1）。
2. 正式执行，命令同 dry-run 去掉 `--dry-run`，加上最近一次部署单里的 bootstrap DSN 文件、签名 key id 和凭据到期时间参数。
3. 再跑一次 dry-run，应当没有差异（幂等）。
4. 只读核对标记：

   ```sql
   select entity_key, g->>'grant_id'
     from authz.nexloop_authority_facts f cross join jsonb_array_elements(f.payload->'grants') g
    where f.tenant_id = :'tenant' and f.fact_kind = 'grants' and g->>'grant_id' like '%:follow_latest_version';
   ```

   预期恰好 4 行，就是上面列的 4 条。
5. readiness 只读核对：挑一个**测试提案**（没有冻结发布意图的、用于验证的 `Consumer` 提案），以负责人身份调用 `GET /api/v1/workbench/schema-proposals/{proposal_id}/check`，只看返回里的 `readiness`：
   - `verdict` 为 `pass` 或 `warn`，没有 `level: refuse` 的项；
   - 每一项都有 `remedy`；`warn` 时 `acknowledge_required` 为 true。
   - **不要点发布。** check 是只读的，不消耗 permit，不写任何东西。
   - 如果仍有 `*_holder_without_follow`，说明第 2–4 步没有生效，回到第 1 步排查。

## 3. K2：同次部署切换告警规则 v38

与第 1 步同一次部署完成，不要拖到下一次。

```bash
docker run $CHECK_COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/alert_rules_apply.py --manifest /opt/release/deploy/configuration/alert-rules.v38.json --check
```

`--check` 应显示版本 38，只比 v37 多一条 `schema_publish_smoke_failed`（指标 `schema_publish_smoke.problems`，critical）。然后去掉 `--check` 正式应用，再确认当前生效的规则版本是 38。

只读核对指标存在（没有发布时应为全 0）：

```sql
select runtime.nexloop_metrics_snapshot(:'tenant','real')->'schema_publish_smoke';
-- 预期 {"failed_24h": 0, "overdue": 0, "problems": 0}
```

## 4. K3：`publish_smokes` 的含义

告警评估器每分钟调用 `runtime.nexloop_observability_maintain()`，返回里多了 `publish_smokes`：

| 值 | 含义 |
|---|---|
| `0` | 没有到期的冒烟轮次（常态） |
| `n > 0` | 本次执行了 n 个到期轮次 |
| `-1` | 冒烟执行器本身出错。错误已被捕获，告警评估照常继续，到期轮次保持 `pending`。持续 30 分钟后这些轮次算逾期，`schema_publish_smoke_failed` 会触发 |

偶尔一次 `-1` 不算故障；连续出现时查数据库日志里 `nexloop_run_schema_publish_smokes` 的报错。**不要手工把轮次改成 passed**，那样会把真实的问题藏起来。

冒烟轮次：每次发布排两轮，第 1 轮在 1 分钟后（版本覆盖、A′），第 2 轮在 15 分钟后（持有者授权、relay 回复失败）。第 2 轮之前要完成发布后的 register，否则第 2 轮会报 `holder_without_grant:` 并触发告警，这是预期的提醒。

## 5. 回退

- 告警规则：重新应用 v37。
- 迁移不回退。快照多出的 `schema_publish_smoke` 块和 `publish_smokes` 字段不影响已有规则。
- follow 标记不需要回退：SQL 不读 `grant_id`，授权内容没有变化。
