# NX-053 删除后联系键的执行点与撤销入口：实现说明

分支 `nx053-contact-keys`，从 `dispatch/integration-s4z` `fa3c153` 切出。迁移 `0240_nx053_erased_contact_keys.sql`（临时号）。依据：负责人 2026-10-11 立项（NX-037 前置）；NX-029 D5（删除后保留不含身份的联系键，负责人可撤销）；AT-059。

## 1. 现状与目标

- **现状**：NX-029 删除 Consumer 时，对它的每个外部身份引用（`external_id_refs`）计算一个按租户加盐的 HMAC，存入 `control.nexloop_erased_contact_keys`（盐在 owner 侧）。但没有任何地方用它拒绝联系，也没有撤销入口。
- **目标**：
  - 建立联系关系之前比对已删除的联系键，命中就拒绝，并且不说明命中的是哪个键；
  - owner 可以撤销，撤销必须写理由；
  - 恢复后，撤销和拦截都不丢失、不回退。

## 2. 比对

- **`control.nexloop_contact_key_erased(tenant, world, ref)`**：用租户的盐对引用计算 HMAC，与未撤销的键比对。只用哈希比对，不保存、也不还原原值。没有盐，或者是其他租户、其他 world，都不算命中。
- **`control.nexloop_principal_identity_refs(tenant, world, principal)`**：一个主体对应的浏览器身份引用。取法是该主体所属的 subject 下每个本地账户各一个，与 0061 身份关联推导引用的方式完全相同。

## 3. 执行点（拒绝码 `NXK01 contact unavailable`）

- **(a) Consumer 兜底触发器**（`ontology.objects`）：
  - 一个 Consumer 新增的外部身份引用如果命中，就拒绝。覆盖身份关联，以及任何写入 `external_id_refs` 的导入。
  - 触发器名排在 0061 身份守卫之前（PostgreSQL 按名字顺序触发），所以命中时报 NXK01，而不是“缺少关联凭据”。
- **(b) 来信主体建立 Consumer 归属**：`authz.nexloop_conversation_command` 的 0138 函数体重命名保留为 `…_before_contact_keys_v0240`，外面加一层包装。
  - 在 `prepare_owner` / `commit_owner` 阶段，原有校验通过之后，检查该主体的身份引用；命中就拒绝，归属不会建立。
  - 已有归属的重放不算新的联系。
- **覆盖面说明**：
  - NX-029 删除后，原主体的归属行仍然指向已删除的 Consumer。这个主体既不能再建会话，也不能另建归属（主键阻挡），这一点不变。
  - 本任务补上的是：同一个人以新的成员主体、或者新的身份关联重新接入时，被已删除的联系键拦住。
- **审计**：
  - 被拒的尝试整体回滚。调用方（`conversation_messages.ConversationOwnershipRegistrar`、`identity_link.BrowserConsumerIdentityLink`）随后在新事务里调用 `authz.nexloop_record_contact_key_refusal`。
  - 这个函数从已认证的上下文重新推导引用，不采用调用方给的值；只有确实命中时才写一条审计事件，审计内容只有路径、world 和代码 NXK01，不含键、引用或任何身份值。
  - 对外表现为 `ConversationDenied` / `ContactKeyRefused`，同样不带细节。

## 4. 撤销（owner 专属）

- **入口**：0124 注册表新增能力 `contact_key.revoke`，操作为 `revoke_contact_key`，subject 只能是 human。
  - Action `nexloop.contact_key.revoke:1`（business-actions v17）只授予 owner（workbench-roles v10，并列入 owner-only 校验集）；
  - 处理函数还会再核对当前的工作台角色必须是 owner；
  - 工作台写入口是 `POST /api/v1/workbench/actions/revoke_contact_key`，不做前端页面。
- **请求内容**：
  - `key_digest`（单个键）与 `erasure_request_id`（一次删除请求的全部键）二选一；
  - 理由必填（1–500 字），记在行上的 `revoke_reason`；
  - 键不存在或参数不合法时返回 409。
- **撤销后**：该键不再拦截。
- **再次删除**：同一身份再次被删除时，插入前的触发器会把已撤销的键重新激活（撤销信息清空，版本号加一），所以撤销不会比新的删除活得更久。

## 5. 删除传播、保留与恢复（NX-035）

- **版本号**：键新增 `revision` 字段。撤销和重新激活都让版本号加一。
- **恢复日志**：日志条目带版本号，但不带理由（理由是自由文本）。原值本来就从未保存。
- **重放**：`runtime.nexloop_restore_replay` 重命名保留为 `…_before_contact_keys_v0240`，外面加一层包装，由包装自己处理已删除联系键的条目，其余条目交给原函数。
  - 只有日志里的版本号更新时才采用日志中的状态：撤销不会丢失，重新激活也不会被较旧的撤销覆盖；
  - 0154 之前写入的、没有版本号的条目，沿用原规则：重放永远不撤回一次撤销；
  - 恢复后，撤销理由显示为 `restored from journal <ref>`。
- **保留**：已删除联系键随 NX-029 的保留规则处理，本任务没有改变它的保留期。

## 6. 文件

- **迁移**：`0240_nx053_erased_contact_keys.sql`，`catalog.json` 已同步，`bootstrap_revision` 为 `0154`。
- **Python**：
  - 新增 `contact_keys.py`；
  - 修改 `conversation_messages.py`、`identity_link.py`（在新事务里记录拒绝）；
  - 修改 `goal_controls.py`（`revoke_contact_key`、`CAPABILITIES`）、`business_actions.py`、`workbench_roles.py`、`workbench_actions_http.py`。
- **配置**：business-actions v17、workbench-roles v10（接在真实派发开关的 v16 / v9 之后；从 s5b 9058f02 变基）。
- **测试**：
  - 新增 `tests/test_erased_contact_keys_pg.py`；
  - 修改 `test_business_actions.py`、`test_governed_entry_pg.py`（注册表映射）、`commercial_fixture.py`（注册表 Action）。

## 7. 测试（`tests/test_erased_contact_keys_pg.py`，5 个）

- **比对**：
  - 只按 HMAC 比对，表里不含原值；
  - 其他引用、其他租户、world 为 test 时都不命中；
  - 撤销后不再拦截；再次删除后重新拦截，版本号为 3。
- **Consumer 兜底**：新增命中的引用报 NXK01，错误里不含引用；撤销后不再报 NXK01，此时由 0061 守卫按自己的规则判断。
- **来信建立归属**（真实登记服务，签名命令）：
  - 已删除身份的新成员主体被拒（`ConversationDenied`），归属没有建立；
  - 审计只有路径、world、代码；
  - 原主体的已有归属照常重放；
  - 撤销后可以建立归属；
  - 撤销后再调用记录函数，不会写入审计。
- **撤销**（工作台 HTTP）：
  - operator 403；理由为空 422；两种标识同时给出 409；键不存在 409；
  - 按 digest 撤销只影响那一个键，按删除请求撤销覆盖该请求的全部键；
  - 理由写在行上；恢复日志条目带版本号，不含理由和引用；
  - 应用角色对表和处理函数都没有权限。
- **重放**：
  - 较新的撤销被采用；
  - 较新的重新激活不会被随后出现的旧撤销条目覆盖；
  - 没有版本号的旧条目按原规则处理；
  - journal 位置覆盖所有条目。
- **AT-064**：本任务没有改动或复制涉及 `nexloop_claims` 的 SQL，白名单不受影响。

## 8. 已知事项

- **其他联系键**：NX-029 只对外部身份引用（浏览器本地身份）生成联系键；relay 等渠道的主体标识目前不生成联系键。如需覆盖，要在删除步骤里加入，属于 NX-029 / 渠道接入的后续工作。
- **身份关联路径没有做端到端测试**：由兜底触发器和单元测试覆盖。完整的身份关联夹具非常重（见 `test_verified_identity_pg`）。
