# R74：Schema 发布后的前驱与后继并存（迁移 schema_publication_successor_coexistence）

- **起因**：R73 之后（2026-11-09 12:53 +08），负责人在 stage 跑了 r8 `--publish`，发布 Consumer v2。所有绑定 Consumer 的 98 个 Action 都生成了后继 `:2`，`:1` 仍然 active，这是 F3 的设计。随后出了两个问题：
  - 0125 的 `authz.nexloop_workbench_action_for` 遇到同一能力有多个 active 定义就报错，工作台写入因此全部失败；
  - 0083 的 lineage 只认 NX-044 的审核决定，所以 register 报 `follow_latest_refused`。
- **迁移的改动**（临时编号，组批时由调度员重新编号）：
  - `action_for` 改名后包一层：同一 Action 有多个版本时，选调用者持有授权的最高版本；一个版本都没有授权时返回 `granted: false`；不同 Action 共用同一个能力时，照旧拒绝。
  - lineage 增加 `schema_publication_id` 字段：取 human 发布、权限 digest 前后相等的那次 F3 发布。
  - 不改授权，不停用任何定义。治理入口仍按精确 resource 检查执行权限。
- **测试**：
  - `tests/test_workbench_action_successor_pg.py`：发布后照常写入，执行的是 `:1`；roles 改为 `:2` 后自动切到 `:2`；无授权时 forbidden；强行指定 `:2` 会被拒绝；两个不同 Action 共用能力时照旧拒绝。
  - `tests/test_grants_follow_schema_publication_pg.py`：真实走 F3 发布后，register 只新增 `claim_matcher → Consumer.edit:2` 这一条；非 human 发布不被认可；digest 不等的发布无法写入。
  - `tests/test_grants_follow_latest.py`。

## 步骤

执行者：迁移、register 和 claim-match 由调度员在 DATA_HOST 用文件凭据执行；工作台验证由负责人在负责人终端执行，口令从 Keychain 经 stdin 传入。

1. **迁移**：按惯例先备份（`sudo -n systemctl start nexloop-pg-diff.service`），再执行 migrate。核对：`control.schema_migrations` 的尾部是本迁移；`bootstrap.verify` 通过。
2. **验证一次工作台写入**（负责人）：选影响最小的写操作。推荐对一条已经 firing 的告警执行 `silence_alert`，`until` 设为 1 小时后，并写明原因。
   - 预期返回 200 且 `silenced: true`。
   - 核对：工作台 `/api/v1/workbench/human-actions` 的第一条是 `alert.silence`，`target_ref` 是这条告警的规则。执行的版本由授权决定，授权仍是 `:1`。
   - 没有 firing 告警时，改为写一次“问题回答”，或者跳过这一步；不要为了验证去造告警。
3. **`stage_secrets.py register --dry-run`**：预期 follow_latest 的 `derived` 恰好 1 条，即 `claim_matcher → eios:action:Consumer.edit:2 execute`，purpose 以 `(schema publication <id>)` 结尾；`refused` 为空。结果不同就停下，报调度员。
4. **`register`**：去掉 `--dry-run` 后执行。预期 `grants_added` 里后继版本只有这 1 条。
5. **claim-match.json** 中的 edit Action 改为 `Consumer.edit:2`（这是配置文件，不改代码），然后重启 claim-matcher。核对：
   - work feed 里 `edit_action_stale_schema` 的待处理条目会被消化；
   - 不应出现 `dead_lettered`：
     ```sql
     select status,last_code,count(*) from runtime.nexloop_work_feed
      where tenant_id='<TENANT_ID>' and feed='claim-match' group by 1,2;
     ```

## 不做的事

- 工作台角色文件仍是 `:1`。工作台照常执行 `:1`，等 roles 支持 follow，或改为 `:2` 以后再切换，见 NX-040 后续 ADR 草稿 `docs/planning-drafts/nx040-predecessor-retire-adr-draft-20261109.md`。
- 不停用 `:1`，不再 hide，不手工改 SQL。
