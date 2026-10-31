# 部署单：relay 到期预警（S43/S45）与积压处置（S42 方案 A）

适用分支 `l7/s43-expiry-alerts`（两个提交：到期预警，backlog_settle）。迁移是**临时编号** 0375、0376，由调度员合并时重编号；下文以“迁移 A（relay_dependency_expiry）”“迁移 B（relay_backlog_settle）”称呼，执行时换成实际编号。

## 事实表

| 项 | 本次 | 依据 |
|---|---|---|
| 迁移 A | 新函数 `runtime.nexloop_relay_readiness`，指标快照（real）多出 `relay_readiness` 块；不建表 | `0375_relay_dependency_expiry.sql` |
| 迁移 B | outbox 状态加 `settled`；新表 `runtime.nexloop_relay_backlog_settlements`（追加写，只存 ID、理由码、来源、执行人、意图、负责人的说明）；governed 处理函数和能力登记 `relay.backlog_settle`；擦除表上的触发器（擦除离开 `pending_confirmation` 时在同一事务里结束该 consumer 的 pending 消息） | `0376_relay_backlog_settle.sql` |
| 业务 Action | `business-actions` v54（v53），新增 `nexloop.relay.backlog_settle:1`（human_owner、low、无审批）；编译计数 object_types 20、actions **130**、functions 2 | 本机 `stage_secrets.py publish --out <空目录> --tenant t --dry-run`：`{"actions": 130, "functions": 2, "object_types": 20}` |
| 能力快照 | `capabilities` v7（v6），新增 `relay.backlog_settle` | `deploy/configuration/capabilities.v1.json` |
| 路由清单 | `api-routes` v6（v5），新增操作 `settle_relay_backlog`（`POST /api/v1/workbench/actions/{operation}`，无新路由） | `deploy/authorization/api-routes.v1.json` |
| 工作台角色 | `workbench-roles` v50（v49）；owner **96** 个 Action（95），新增 `nexloop.relay.backlog_settle:1`，在 owner-only 集合内，slice `relay_backlog`；operator 12、reviewer 1 不变 | `deploy/authorization/workbench-roles.v1.json` |
| 告警规则 | `alert-rules` v23（v22）：86 条（81），新增 5 条 `relay_dependency_expiring`（warning）、`relay_dependency_expired`（critical）、`relay_unserviceable_enrollment`（critical，60 秒）、`relay_no_active_enrollment`（warning，10 分钟）、`relay_stalled_consumers`（warning，5 分钟） | `deploy/configuration/alert-rules.v23.json` |
| 新工具 | `scripts/deploy/relay_backlog_settle.py`（负责人登录工作台，每批 ≤ 50 条，重跑即重放）；`nexloop-relay-enroll` 支持撤销文件 `{"withdraw": true, "consumer_id", "intent_id"}` | 两者都有 PG 测试 |
| 服务授权 / principals | **无变化** | — |

## 前置

- P1 备份：DATA_HOST 按惯例做一次 PG 差异备份并核对标签（`sudo -n systemctl start nexloop-pg-diff.service`），没有可验证的备份不迁移。
- P2 stage 当前版本：business-actions v53、workbench-roles v49、告警规则 v22、迁移头为本分支之前的最后一个。任一项不符就停下。
- P3 10-30 重建的装配期限是 `2026-12-01T00:00:00Z`。

## 步骤

### S1 迁移（DATA_HOST）

照常用 `stage_release.py` 的迁移步骤迁到迁移 B。预期：迁移头前进两个版本；`check_definer_search_path` 0 problems。

只读核对：

```sql
select runtime.nexloop_metrics_snapshot('<TENANT_ID>','real')->'relay_readiness';
```

预期 `active_enrollments` ≥ 1、`earliest_valid_until` 不晚于 `2026-12-01T00:00:00Z`。旧 customer-1/2/3 的登记若仍 active 且依赖已过期，`dependencies_expired` 和 `unserviceable_enrollments` 大于 0，由 S4 处理。

### S2 发布业务 Action（DATA_HOST，配置员窗口）

1. 预演：`stage_secrets.py publish --dry-run`。预期**只有一个**变化：`nexloop.relay.backlog_settle:1`，计数 (20, 130, 2)。
2. 正式发布。发布前该 Action 对工作台是 404（`Action not published`）。

### S3 工作台角色 v50（DATA_HOST，配置员窗口）

`workbench_roles --apply`（命令同 R54 S5）。预期 ok；owner 授权数 **96**，多出的一项是 `eios:action:nexloop.relay.backlog_settle:1`；operator 12、reviewer 1。

### S4 撤销依赖已过期的旧登记（business_owner）

部署 v23 之前先撤销，否则 v23 一上线 `relay_dependency_expired` 和 `relay_unserviceable_enrollment` 就会持续报 critical。

1. 只读列出要撤销的 consumer（DATA_HOST，`ro()`）：最新一次登记为 active、且当前 relay profile 为空的全部 consumer。不写死名单，已擦除的 consumer（例如 customer-4、refusal-1/2）也可能在里面：

   ```sql
   select e.consumer_id,e.enrollment_revision from (select distinct on (x.consumer_id) x.* from control.nexloop_relay_enrollments x
     where x.tenant_id='<TENANT_ID>' and x.world='real' order by x.consumer_id,x.enrollment_revision desc) e
   where e.state='active' and runtime.nexloop_relay_profile(e.tenant_id,e.world,e.consumer_id,null) is null order by 1;
   ```

2. 每个 consumer 一个撤销文件（0600，不进仓库）：`{"withdraw": true, "consumer_id": "<64 hex>", "intent_id": "relay-withdraw-<新 UUID>"}`，用 business_owner 的凭据执行：

   ```bash
   nexloop-relay-enroll --database-url-file F --signing-key-file F --owner-credential-file <business_owner 凭据> --artifact-root D --enrollment-file <撤销文件>
   ```

   预期每次输出 `{"withdrawn":true,...}`，凭据不出现在输出里；重跑同一意图为重放。撤销后，这些 consumer 之后到达的消息会由 `relay_unenrolled_pending` 报出（预期行为）。
3. **只读核对**（撤销之后、发布 v23 之前）：

   ```sql
   select runtime.nexloop_relay_readiness('<TENANT_ID>','real',clock_timestamp());
   ```

   预期 `active_enrollments` = 3（只剩 s6c 的登记）、`dependencies_expired` = 0、`unserviceable_enrollments` = 0。任一项不符就停下，不发布 v23。

### S5 告警规则 v23（DATA_HOST，配置员窗口）

```bash
docker run $CHECK_COMMON "$BACKEND_IMAGE" /opt/release/scripts/deploy/alert_rules_apply.py --manifest /opt/release/deploy/configuration/alert-rules.v23.json --check
```

再去掉 `--check` 正式应用（`--expected-revision` 等参数同 R54 S8）。预期 86 条。

### S6 结束现有积压（负责人或 approver3，一条命令）

1. 只读列出要结束的消息（DATA_HOST，`ro()`）：

   ```sql
   select b.message_id from runtime.nexloop_message_outbox b join runtime.nexloop_conversations cv
     on cv.tenant_id=b.tenant_id and cv.world=b.world and cv.conversation_id=b.conversation_id
   where b.tenant_id='<TENANT_ID>' and b.world='real' and b.status='pending' order by b.message_id;
   ```

   预期 51 条（customer-1 的 1 条 `2e31f0a2…`，已擦除 consumer 的 50 条）。任何一条属于当前仍可服务的 consumer 都会被 SQL 以 `consumer_serviceable` 拒绝，整批不动。
2. 配置文件（0600，不进仓库），格式见 `deploy/examples/relay-backlog-settle.example.json`：`confirmation_ref` 用 `s42-settle:<日期>-1`，`reason` 写负责人的决定（“S42 方案 A：已擦除 consumer 与 customer-1 的积压，不回复”），`message_ids` 为上一步的 51 个 ID。
3. 先 `--dry-run`（不登录）：预期 `batches: [50, 1]`。
4. 执行（负责人密码从 Keychain 经 stdin 传入）：

   ```bash
   security find-generic-password -s <keychain service> -a <account> -w | python3 scripts/deploy/relay_backlog_settle.py --origin https://<APP_HOST_NAME>:<PORT> --ca-file <CA> --username-file <F> --password-stdin --config <F>
   ```

   预期 `ok: true`，两批都是 `settled`。理由码：customer-1 那条为 `no_serviceable_enrollment`；已擦除 consumer 的 50 条，在迁移 relay_backlog_pseudonym（0382）上线后为 `consumer_erased`，上线前为 `no_serviceable_enrollment`（R60 实际如此：擦除完成后会话的 consumer 已是假名，0376 识别不出，见 `docs/design/relay-backlog-disposition.md` 第 5 节）。中断后重跑同一配置为 `replayed`，不会重复结束。
5. 拒绝码对照：`unknown_message`（ID 不在 outbox）、`not_pending`（已投递或已结束）、`consumer_serviceable`（该 consumer 有可服务的登记：应由 relay 正常回复，不该结束）、`invalid`（请求体）。403 表示登录者不是 owner。

### S7 核对

- `relay.pending_messages` 为 0（或只剩 S6 之后新到的消息）；`relay_backlog_age`、`relay_unenrolled_pending` 在下一次评估后恢复。
- `select reason,source,count(*) from runtime.nexloop_relay_backlog_settlements where tenant_id='<TENANT_ID>' group by 1,2`：`no_serviceable_enrollment / owner` 1，`consumer_erased / owner` 50（修复后执行）；R60（修复前执行）实际为 `no_serviceable_enrollment / owner` 51，这些行不改，按设计文档第 5 节的查询判读：其中会话 consumer 为 `pseudonym:` 的 50 条即 consumer_erased。
- `relay_readiness`：执行 S4 后 `dependencies_expired` 和 `unserviceable_enrollments` 为 0；`dependencies_expiring_7d` 在 11-24 前后开始大于 0，这是预期的提前量。

## 证据

| 步 | 记录 | 预期 |
|---|---|---|
| S1 | 迁移日志行、definer 检查、`relay_readiness` 快照 | 头前进两版；0 problems |
| S2 | 预演与发布输出 | 只多 `relay.backlog_settle:1`；(20, 130, 2) |
| S3 | roles 输出 | owner 96 |
| S4 | 撤销清单查询结果、每次撤销输出、撤销后的 `relay_readiness` | `withdrawn: true`；active 3、expired 0、unserviceable 0 |
| S5 | `--check` 与应用输出 | 86 条 |
| S6 | dry-run 与执行输出（已脱敏） | `[50, 1]`；全部 settled |
| S7 | 快照与审计计数 | 积压为 0；审计 1 + 50（R60：51 条 no_serviceable_enrollment，其中 50 条假名） |

## 回退

- 迁移 B 之前的 stage 镜像不能运行在迁移后的库上（MigrationDrift），完整回退先恢复 P1 的备份。
- 已结束的消息不会自动恢复为 pending（审计表追加写，有意不可撤销）；只回退告警时，重新应用 v22 即可。
- S4 撤销后要恢复服务，用新的 intent 重新登记（续期后的装配）。
