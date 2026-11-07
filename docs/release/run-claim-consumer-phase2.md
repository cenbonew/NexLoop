# 部署单：Claim → Consumer 属性阶段二（Consumer v2 `delivery_windows`、改绑 `Consumer.edit:2`、白名单 v2），stage

- **状态**：草稿。执行者：调度员。判定者：L6。证据只记数量、版本号、规则名和原因码，不记 Claim 原文、属性值或 id。
- **依据**：
  - 设计 `docs/implementation/claim-consumer-delivery-windows-design.md` v2（L6 条件 C1–C3）；
  - 计划 `docs/implementation/claim-to-consumer-property-plan.md` §2、§3、§10（负责人 2026-11-02 决定）；
  - NX-040 运行单 `docs/release/run-nx040-stage.md`；
  - 阶段一部署单 `docs/release/run-claim-consumer-phase1.md`。
- **适用**：分支 `l4/claim-consumer-delivery-windows`。迁移 `schema_structured_property`（临时号 0416）和 `workbench_property_sources`（临时号 0417），以组批后的编号为准。compose 没有改动。
- **前提**：阶段一已上线（R69）。白名单 v1 为空，私有 `config/claim-match.json` 为 `{"edit_actions": {"Consumer": ["Consumer.edit", 1]}}`。
- **顺序不能调换**：
  1. 迁移
  2. Consumer v2 发布
  3. register
  4. 改绑
  5. 重启 claim-matcher
  6. 负责人确认后发布白名单 v2
  7. 演示客户验收

  第 2 步到第 5 步之间是窗口期：匹配器对 Consumer 的 Claim 不做任何记录，claim-match 条目被延后（`edit_action_stale_schema`，不消耗尝试次数，最长一周）。所以**改绑（第 3–5 步）要紧跟在发布（第 2 步）后面**，在同一个执行窗口里做完。
- **白名单 v2 的正式发布由负责人确认**（第 6 步）。在那之前，`delivery_windows` 的提案只会停在 `proposed / auto_write_not_allowed`，不会写入。

只读 SQL 用 DATA_HOST 上的只读角色执行（`ro "<SQL>"`，同阶段一）。`<TENANT_ID>` 是 stage 租户。

## 1. 迁移（DATA_HOST）

1. 部署前备份：`sudo -n systemctl start nexloop-pg-diff.service`。
2. 部署本批镜像并迁移。迁移尾为组批后的编号，`versions.lock.json` 的 `bootstrap_revision` 与之相同。

**核对**

```sql
select to_regprocedure('runtime.nexloop_structured_descriptor_valid(jsonb)') is not null,
       to_regprocedure('authz.nexloop_workbench_property_sources(text,text,text)') is not null,
       (select count(*) from pg_trigger where tgname='nexloop_mutation_proposal_value_check');
-- 预期 true, true, 1
select runtime.nexloop_schema_change_valid('add_optional_property','Consumer','{"property_name":"x","value_type":"json"}');
-- 预期 false（json 必须带合规的结构化描述符）
```

**回退**：迁移不回退。新函数在第 2 步之前没有调用方；值校验触发器只对带结构化描述符的属性生效，而此时还没有这样的属性。

## 2. Consumer v2 发布（NX-040 提案与人工审核）

1. **提案**：照 NX-040 运行单 §3.3 跑 `s6_stage_schema.py r8`，不带 `--publish`。私有配置的 `inputs` 写：
   - `schema_type`：`Consumer`；
   - `new_property`：`delivery_windows`；
   - `property_change`：下面这个变更，原文取自设计 §1，经调度员和 L6 审过，不要改动：

   ```json
   {"property_name": "delivery_windows", "value_type": "json",
    "type_descriptor": {"kind": "array", "max_items": 40, "element": {"kind": "struct", "fields": [
      {"name": "weekday", "required": true, "descriptor": {"kind": "string", "enum": ["mon","tue","wed","thu","fri","sat","sun","any"]}},
      {"name": "part", "required": true, "descriptor": {"kind": "string", "enum": ["morning","daytime","afternoon","evening","any"]}}]}},
    "display_name": "送货时间",
    "description": "顾客说过方便收货的星期与时段（如“周五白天”），由对话中的陈述经规范化规则表得出；不含具体钟点。"}
   ```

   预期：G9–G13 结论为 accept、类别 minor，状态 `accepted_for_release`，并签发发布许可；迁移目录和 `bootstrap.verify` 摘要前后相同。
2. **审核**：审核员（`ontology.schema.review`）用 `schema_proposal.py check` 复核：
   - 报告为 allowed、minor，新版本为 2；
   - 看影响清单列出了哪些定义，按 `check` 的 verdict 判断，`not_reported` 时停下；
   - 审核员在工作台发布页逐字读一遍 `display_name` 和 `description`（自由文本，设计 §2.5），确认与上面的原文一致。
3. **发布**：`schema_proposal.py publish`，rationale 写“送货时间窗（负责人 2026-11-02 决定，claim-consumer-plan §10 第 2 项）”。预期 `published: true`，`version_after: 2`，`successor_actions: ["eios:action:Consumer.edit:2"]`。

**核对**

```sql
select p->>'value_type',p->>'display_name',p->'type_descriptor'->>'max_items'
  from ontology.object_type_versions t,jsonb_array_elements(t.definition->'properties') p
 where t.tenant_id='<TENANT_ID>' and t.type_name='Consumer' and t.version=2 and p->>'property_name'='delivery_windows';
-- 预期 json, 送货时间, 40
select resource_id,active from control.nexloop_action_definitions where tenant_id='<TENANT_ID>' and world='real' and resource_id like 'eios:action:Consumer.edit:%';
-- 预期 :1 和 :2 都在
select schema_version,count(*) from ontology.objects where tenant_id='<TENANT_ID>' and world='real' and type_name='Consumer' group by 1;
-- 预期全部为 2
select permission_digest_before=permission_digest_after from ontology.nexloop_schema_publications where tenant_id='<TENANT_ID>' order by published_at desc limit 1;
-- 预期 true（发布不写授权）
```

窗口期核对：有新消息时，claim-match 条目被延后，而不是失败：

```sql
select status,attempts,last_code,count(*) from runtime.nexloop_work_feed where tenant_id='<TENANT_ID>' and feed='claim-match' group by 1,2,3;
-- 可能出现 pending, 0, edit_action_stale_schema；不应出现 dead_lettered
```

**回退**：用同一 CLI 执行 `schema_proposal.py hide`（NX-040 §6）。它会发布下一个 Schema 版本，把这个属性设为 hidden，并生成后继 `Consumer.edit:3`；之后要按第 3–5 步把配置改到 `:3`。Schema 版本不能删除。

## 3. register（DATA_HOST）

1. `stage_secrets.py register --dry-run`，按 OPERATIONS.md §8a.11 第 3 步的方法比较清单与库中授权。**预期新增恰好一条**：`claim_matcher → eios:action:Consumer.edit:2 execute`，由 service-grants v2 的 `follow_latest_version` 解析出来，授权文件不改。工作台角色文件里没有 `Consumer.edit`，预期不新增。属性级 EDIT 由类型规则按对象推导，不新增授权行。
2. 差异与预期不同时停下，报调度员。一致时去掉 `--dry-run` 执行；`grants_added` 应当为 1。

**核对**

```sql
select entity_key[1]||' '||entity_key[2] from authz.nexloop_authority_facts where tenant_id='<TENANT_ID>' and fact_kind='grants'
   and entity_key[2]='eios:action:Consumer.edit:2';
-- 预期 1 行，主体为 claim_matcher 的 principal_id（取自清单，不要按短名推算）
```

**回退**：不需要。这条授权只在 `claim-match.json` 指向 `:2` 时才会被用到；不改绑，它就不会被使用。

## 4. claim-match.json 改为 2（APP_HOST）

私有 `config/claim-match.json` 改为 `{"edit_actions": {"Consumer": ["Consumer.edit", 2]}}`。文件权限 0600，属服务用户。仓库里的 `deploy/stage/claim-match.example.json` 保持 `:1`：装配预演把它当作 stage 配置，而预演环境里没有 Consumer v2。

**回退**：改回 `:1` 后重启。改回之后，匹配器会再次进入窗口期判断，把条目延后，不会写错版本。

## 5. 重启 claim-matcher（APP_HOST）

只重启这一个容器：`up -d --no-deps claim-matcher`。日志里不应出现 `PermissionError` 和 `unavailable`（只看计数：`grep -c`）。

**核对**

```sql
select status,last_code,count(*) from runtime.nexloop_work_feed where tenant_id='<TENANT_ID>' and feed='claim-match' group by 1,2;
-- 一个延后周期（60 秒）之后，edit_action_stale_schema 的条目被处理完
select split_part(reason,':',1),count(*) from ontology.nexloop_mutation_proposals where tenant_id='<TENANT_ID>' and world='real' and property_name='delivery_windows' group by 1;
-- 白名单 v2 发布前：只有 auto_write_not_allowed（或没有行）
```

**回退**：回到第 4 步的回退。

## 6. 发布白名单 v2（DATA_HOST，配置员窗口，**负责人确认后**）

清单是 `deploy/configuration/auto-write.v2.json`：只含 `Consumer.delivery_windows`，带 reason，`approved_by_decision` 为 `2026-11-02#claim-consumer-plan-s10-1`。这一步要等负责人对这份清单明确确认后再执行；确认的原话写进证据。

```bash
docker run $CHECK_COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/auto_write_apply.py --manifest /opt/release/deploy/configuration/auto-write.v2.json --check
```

去掉 `--check`，加上 `--tenant <TENANT_ID> --bootstrap-dsn-file <同阶段一>`，正式发布。输出两行：

- 第一行 `definitions`：`delivery_windows` 在本体中的当前定义（类型版本 2、值类型 json、显示名“送货时间”和说明），先核对；
- 第二行：预期 `{"applied": true, "version": 2, "properties": 1, ...}`。

第 2 步之前执行会被拒绝（`auto write property not published`）。

**核对**

```sql
select type_name,property_name,reason<>'' from control.nexloop_auto_write_properties where tenant_id='<TENANT_ID>' and world='real';
-- 预期 1 行：Consumer, delivery_windows, true
select revision,approved_by_decision from control.nexloop_auto_write_publications where tenant_id='<TENANT_ID>' and world='real' order by revision;
-- 预期 1 和 2，都是 2026-11-02#claim-consumer-plan-s10-1
```

**回退**：发布 `auto-write.v3.json`，`properties` 为空，`approved_by_decision` 写撤回的决定。版本号只能递增。已经写入的值不会因此撤回；撤回值要走更正链。

## 7. 验收：persona 为 real 的演示客户

stage 上现有的客户都是 `simulated_agent` 人设，rematch 和匹配验收都会跳过它们。所以照 `run-contact-channel-demo.md` §0–§1 建一个 persona 为 real 的合成客户，核对 `runtime.nexloop_consumer_persona(...)` 返回 `real`；演示结束当天照该单 §3 退役。

演示客户在 WebChat 依次说下面四句：

| 说法 | 预期 |
|---|---|
| 周五白天我在家 | 提案 `applied`，Consumer 出现 `[{"weekday":"fri","part":"daytime"}]`，Claim 为 `resolved` |
| 一般周末上午也可以 | 新的陈述整体替换旧值：`sat/morning`、`sun/morning` |
| 周五晚上七点以后 | 带钟点，不规范化：匹配原因 `value_not_normalized`，Claim 为 `needs_resolution`，值不变 |
| 周六可能不在 | 不确定的说法：被 modality 守卫挡住，或者不规范化；值不变 |

**核对**（只看数量和原因码）

```sql
select status,reason,count(*) from ontology.nexloop_mutation_proposals
 where tenant_id='<TENANT_ID>' and world='real' and property_name='delivery_windows' and target_ref='eios:object:Consumer/<CONSUMER_ID>' group by 1,2;
select split_part(reason,':',1),outcome,count(*) from ontology.nexloop_claim_matches m join ontology.nexloop_claims c using(tenant_id,world,claim_id)
 where m.tenant_id='<TENANT_ID>' and m.world='real' and c.consumer_id='<CONSUMER_ID>' group by 1,2;
select jsonb_array_length(properties->'delivery_windows') from ontology.objects where tenant_id='<TENANT_ID>' and object_id='<CONSUMER_ID>';
-- 预期 2
```

画像接口（L9 做前端）：以 owner 身份请求 `GET /api/v1/workbench/consumers/<CONSUMER_ID>`，`property_sources` 按 `packages/contracts/workbench-consumer-property-sources.schema.json` 校验：

- `delivery_windows` 的 `status` 为 `applied`；
- `display` 为“周六 上午；周日 上午”；
- `source` 只有 id；
- `stated_at` 是第二句消息的陈述时间（Claim 的 `valid_time.anchor`：签名渠道时间在偏差窗口内时取渠道时间，否则取接收时间，见设计 §9）；
- `effective_at` 是应用时间。

**回退**：演示客户按 `run-contact-channel-demo.md` §3 退役。stage 上其他客户的属性不受影响。

## 8. 排查

- **`feed_backlog_age:claim-match` 告警，或 claim-match 条目一直不动**：看最近的结果码（只读）：

  ```sql
  select last_code,count(*) from runtime.nexloop_work_feed where tenant_id='<TENANT_ID>' and feed='claim-match' and status='pending' group by 1;
  ```

  如果是 `edit_action_stale_schema`，说明第 2 步已经发布，但第 3–5 步还没做完。处理：register，再把 `claim-match.json` 改绑到新版本，再重启 claim-matcher。**改绑必须紧跟在发布后面**：拖得越久，Consumer 的 Claim 被延后得越久，一周后转为计次重试，最终会进死信。运行手册 OPERATIONS.md §8a.12 有同一条。
- **提案停在 `proposed / auto_write_not_allowed`**：白名单 v2 还没发布（第 6 步），这是预期的状态。
- **提案原因是 `value_not_normalized`**：提案值不是规范化后的值。正常路径不会出现这种情况；出现时报 L4，不要手工修改。

## 9. 证据与局限

- **证据**：
  - 第 1 步：迁移尾编号；
  - 第 2 步：publication 的版本号和许可编号；
  - 第 3 步：`grants_added` 计数；
  - 第 6 步：负责人确认的原话和发布版本号；
  - 第 7 步：上面各条查询的计数结果。
  - 执行者和判定者分开写。
- **历史回放**（计划 §10 第 3 项）不在本单。白名单 v2 上线之后另开一单，先 dry-run 报数量。
- **stage 窗口期**：第 2 步到第 5 步之间，进来的消息会延后匹配，不会丢失；最长一周后转为计次重试。
