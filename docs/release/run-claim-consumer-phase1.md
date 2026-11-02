# 部署单：Claim → Consumer 属性阶段一（按属性白名单、edit_actions、rematch），stage

- **状态**：草稿。执行者：调度员。判定者：L6。证据只记数量、版本号和规则名，不记 Claim 原文、值或 id。
- **依据**：`docs/implementation/claim-to-consumer-property-plan.md` 的 §9.1（白名单）、§9.3（rematch）、§10（负责人决定）和 §1（配置与重启）。
- **适用**：分支 `l4/claim-consumer-auto-write`。迁移 `claim_rematch`（临时号 0408）和 `claim_auto_write`（临时号 0409），以组批后的编号为准。compose 没有改动。
- **本单不会自动写入任何属性**：白名单 v1 为空。打开 `edit_actions` 之后，匹配只产生待审核的提案（`proposed / auto_write_not_allowed`），Claim 停在 `needs_resolution`。
- **回放顺序（计划 §9.4）**：第 5 步（rematch 执行）放在白名单上线（第 1、2 步）以及“停止联系”（§9.2、§9.6）落实之后；执行前先 dry-run 报数量，由负责人确认。

## 1. 部署与迁移

1. 部署前备份：`sudo -n systemctl start nexloop-pg-diff.service`。
2. 部署本批镜像并迁移。迁移尾为组批后的编号，`versions.lock.json` 的 `bootstrap_revision` 与之相同。

## 2. 发布空白名单 v1（DATA_HOST，配置员窗口，一条命令）

```bash
docker run $CHECK_COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/auto_write_apply.py --manifest /opt/release/deploy/configuration/auto-write.v1.json --check
```

去掉 `--check`，加上 `--tenant <TENANT_ID> --bootstrap-dsn-file <同 alert_rules_apply 的路径>` 正式发布。预期 `{"applied": true, "version": 1, "world": "real", "properties": 0, ...}`。

- 白名单只由负责人维护：改 `deploy/configuration/auto-write.v<N+1>.json`，再用同一条命令发布。版本号只能递增；同一版本内容不同会被拒绝。
- 身份、联系方式、地址、支付类属性写进 manifest 会被数据库整份拒绝（`auto write property sensitive: <类型>/<属性>`），写进去也不会生效。
- 阶段二发布 `Consumer.delivery_windows` 后，白名单 v2 才加入它（计划 §10.1 第 6 项），不在本单。

## 3. 打开前核对（只读）

1. `Consumer.edit:1` 已发布：

   ```sql
   select resource_id, active from control.nexloop_action_definitions
    where tenant_id='<TENANT_ID>' and world='real' and resource_id='eios:action:Consumer.edit:1';
   ```

   预期 1 行，active 为 true。也可以用 `stage_secrets.py publish --dry-run`，预期“无变化”。
2. `claim_matcher` 已授权 `Consumer.edit` 和 `nexloop.claim.match`（`deploy/authorization/service-grants.v1.json`）：`stage_secrets.py register --dry-run` 预期“无变化”。
3. 白名单为空：

   ```sql
   select count(*) from control.nexloop_auto_write_properties where tenant_id='<TENANT_ID>' and world='real';   -- 预期 0
   select revision, properties from control.nexloop_auto_write_publications where tenant_id='<TENANT_ID>' and world='real';   -- 预期 1, 0
   ```

## 4. claim-match.json 与重启 claim-matcher（APP_HOST）

1. 私有 `config/claim-match.json` 改为与 `deploy/stage/claim-match.example.json` 相同：`{"edit_actions": {"Consumer": ["Consumer.edit", 1]}}`。文件权限 0600，属服务用户。
2. 只重启这一个容器：`up -d --no-deps claim-matcher`。
3. 日志里没有 `PermissionError` 和 `unavailable`（只看计数：`grep -c`）。

## 5. 78 条 type_not_writable 的重新匹配（APP_HOST，claim matcher 自己的身份）

先 dry-run，只报数量：

```bash
docker compose -p nexloop-app -f "$NEXLOOP_ROOT/compose.app.yaml" --env-file "$NEXLOOP_ROOT/compose.app.env" run --rm --no-deps \
  --entrypoint python3 claim-matcher /opt/release/scripts/deploy/claim_rematch.py \
  --database-url-file /run/secrets/claim_matcher_dsn --service-credential-file /run/secrets/claim_matcher_credential \
  --signing-key-file /run/secrets/signing_key --signing-key-id "$SIGNING_KEY_ID" --artifact-root /var/lib/nexloop/artifacts \
  --reason type_not_writable
```

- 预期 `{"verb": "dry_run", "done": true, "eligible": <约 78 或更少>, "released": 0, "conversations": <N>}`。
- `eligible` 会排除擦除中或已擦除的客户、模拟或合成客户、已更正和已取代的 Claim，所以可能少于 78。
- 把数量报给负责人。**负责人确认后**，在同一命令末尾加 `--release` 再执行，预期 `released` 等于 `eligible`。
- 可以重复执行：已经释放的 Claim 回到 `unresolved`，不会再被选中，除非重新匹配后又以同样的原因结束。

## 6. 只读验收

```sql
-- 白名单：阶段一为空
select type_name, property_name, revision from control.nexloop_auto_write_properties where tenant_id='<TENANT_ID>' and world='real';
-- 提案：全部 proposed / auto_write_not_allowed，没有 applied
select status, reason, property_name, count(*) from ontology.nexloop_mutation_proposals where tenant_id='<TENANT_ID>' and world='real' group by 1,2,3;
-- 重启之后不再出现新的 type_not_writable
select split_part(reason,':',1), outcome, count(*) from ontology.nexloop_claim_matches
 where tenant_id='<TENANT_ID>' and world='real' and recorded_at > '<重启时间>' group by 1,2 order by 3 desc;
-- 第 5 步释放的 Claim 现在的去向（按重新匹配的版本）
select m.outcome, split_part(m.reason,':',1), count(*) from ontology.nexloop_claim_matches m
 join ontology.nexloop_claim_rematch r using(tenant_id,world,claim_id)
 where m.tenant_id='<TENANT_ID>' and m.world='real' and r.cause='rematch:type_not_writable' and m.matcher_version like '%;rematch:%' group by 1,2;
-- Consumer 属性没有因此出现值
select count(*) from ontology.objects where tenant_id='<TENANT_ID>' and world='real' and type_name='Consumer' and properties<>'{}'::jsonb;
```

预期：

- 提案没有 `applied`；
- Consumer 属性的计数与上线前相同（7 个 Consumer 都为空）；
- 释放的 Claim 多数变成 `no_match`（属性候选）或 `proposed / auto_write_not_allowed`，因为 Consumer v1 没有合适的业务属性。

## 7. 回退

1. `config/claim-match.json` 改回 `{}`，再执行 `up -d --no-deps claim-matcher`。
2. 白名单不需要回退：它为空。以后如果加过属性，要撤掉时，发布一个更新版本号的空 manifest。
3. 迁移不回退。
4. 已经产生的提案留在 `proposed`，不手工删除。
