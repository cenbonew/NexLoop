# NX-062 M4a：机器主体授权表与首批读取动词（object.get、object.list）

- **依据**：ADR-041 附注 A1（负责人 2026-11-09 确认，提交 112d4c25），L6 的 M4 方案判定（条件 M4-1 至 M4-5），以及投影草案 `docs/implementation/NX-062-M4-projections.md`（b4b9221e）。执行：L10。
- **迁移**：`0440_nx062_machine_reads.sql`（临时号；0438、0439 是在途批次的号）。
- **拆分**：M4 拆成两片。M4a（本片）做授权表、两个受治理 Action、object.get 和 object.list；M4b 做 object.neighbors 和 commitment.list。拆分的原因是：neighbors 要按关系类型和属性边逐个判定可见性，commitment.list 要从承诺视图和证据账本逐字段投影，各自的测试量都和 M4a 相当。

## 落实 L6 条件

| 条件 | 落实 |
|---|---|
| M4-1 写入者 | `authz.nexloop_machine_client_grants`：只追加（revision 连续，最新一行生效；revoked 即撤销）；force RLS；所有应用角色和 configurator 都没有权限；不能删除，也不能 truncate。只能经 `nexloop.machine_client.grant` 和 `nexloop.machine_client.revoke_grant` 写入：subject_rule 为 human，handler 用 0435 的公共检查（在任负责人、world=real、请求体封闭）。触发器与 handler 双重检查：能力必须当前在允许表、是已登记的读取动词、没有命中永不授予清单；对象类型非空、无通配、无重复，并且属于该动词投影里登记的类型。这两个 Action 名中带 grant，会命中永不授予规则，按预期不能进入机器允许表（有测试） |
| M4-2 读取函数 | `authz.nexloop_machine_read(prefix, digest, verb, params)` 依次检查：machine_identity_snapshot（同一函数、同一错误）→ scope 包含该动词 → capability_current → 授权表最新 revision 为 active → 对象类型在授权范围内且属于投影类型 → 数据按密钥的 world 过滤。全部用当前事实，不缓存。投影为空即报错（失败关闭）；返回值的顶层字段如果超出登记字段也报错。不存在、他人租户、其他 world、授权类型之外、Consumer 在擦除中或已擦除的对象，都返回同一个 404，判定直接调用 NX-029/054 的 `runtime.nexloop_object_consumers_read` 和 `runtime.nexloop_consumer_erasing`。属性值若指向擦除中的 Consumer 则省略；owner 限制组的属性不返回。每次读取按天、key_prefix、动词、对象类型计数（T-12）。HTTP 层先执行 M3 的准入（限速、计数），再调用读取函数 |
| M4-3 单一来源 | 0437 的能力列表改为 scope ∩ 允许表 ∩ 授权表（create or replace，已在 copy guard 登记）。机器的 EIOS 事实到 M5 由这张表派生，并做一致性校验，这部分在 M5 实施，ADR 附注 A1 第 3 点已经写明。C1 的写入侧触发器保持不变 |
| M4-4 擦除 | `issued_by_principal` 记的是负责人，按 `machine_clients` 的同一做法列入 member erasure coverage 的 STAFF_KEPT。读取不落任何正文；计数表只记动词、类型、key_prefix 和次数。consumer、member 和全库三项擦除覆盖测试都通过 |
| M4-5 测试 | 见下 |

## HTTP 与契约

- `POST /machine/v1/read`，请求体是 `{"verb": ..., "params": {...}}`，参数不进 URL。
  - 任何查询串返回 400；请求体中任何一层出现身份字段（tenant_id、world、actor、principal_id、subject_id、on_behalf_of、client_id）返回 422；请求体上限 4 KiB。
  - 结果映射：not_found 返回 404，not_permitted 返回 403，参数不对返回 422，投影不可用返回 503。
- C8 清单 api-routes v15：新增这条路由和两个工作台操作。
- 新契约：`machine-read-object`、`machine-read-object-list`。
- `machine-error` 增加错误码 invalid_request、not_permitted、not_found。
- `machine-capabilities` 的说明改为“只列出主体持有有效授权的能力”。
- 测试校验了每个响应都符合对应契约。

## 清单与部署

- business-actions v58、capabilities v11、workbench-roles v55（owner 106 个 Action）、api-routes v15。`test_stage_register_pg` 的计数从 142 改为 144。
- **部署前必须先 publish**：`stage_secrets.py publish` 先 dry-run，确认计划中有 `nexloop.machine_client.grant` 和 `.revoke_grant`。未发布前，工作台调用返回 404。之后重新配置工作台角色清单 v55。
- 迁移 0440 自带 object.get 和 object.list 的读取动词登记与允许表行。负责人授权之前，没有任何主体能读到数据。

## 测试（真实 PG + 真实 HTTP）

`tests/test_machine_reads_pg.py`，5 个用例：
- **授权**：
  - 被拒的授权返回固定原因码：write 能力、Consumer 类型、未登记类型、未登记动词、永不授予的能力；
  - 空列表、通配、请求体带身份字段返回 422；操作者返回 403；
  - revision 依次为 1、2，然后撤销；update、delete 被拒；应用角色和 configurator 都没有表权限；两个 Action 名被永不授予清单命中。
- **投影**：返回字段集合等于登记的投影；结果中不出现标记值（条件文本、消息引用、外部单号、执行主体、消费者引用）；object.list 的分页与 invisible 正确；只为 Commitment 授权 list 时，CommercialRecord 返回 404；读取计数正确。
- **404 一致**：不存在、他人租户、test world、擦除中的 Consumer、授权类型之外、Consumer 类型，6 种情况的响应码和正文都一样；可见对象上指向擦除中 Consumer 的值被省略。
- **授权变化立即生效**：
  - scope 里有、授权表里没有，返回 403；
  - 授权后返回 200，能力列表随之出现；
  - 只含 object.list 的密钥读 object.get 返回 403；
  - 撤销后下一次读取返回 403，重新授权后恢复；
  - 允许表在另一个连接上标为 retired 后，读取返回 403，能力列表变空；
  - 身份字段返回 422，查询串返回 400，未授权的动词返回 403。
- **机器与工作台一致**：机器返回的每个属性，与工作台 `/api/v1/workbench/objects/{type}/{id}` 的同一属性逐字段相等；revision 和 schema_version 也一致。

相关回归（Node 24，`-n 2`）：machine_reads、machine_realm、machine_lifecycle、machine_access、路由清单 G-1..G-6、stage_register、governed_entry、business_actions、workbench_roles、workbench_read、catalog_manifests、copy guard、三项擦除覆盖、契约四项、sim roles、workbench_actions_http 全部通过。test_bootstrap 因编号空缺有 2 项失败，临时把 0440 改成 0438 后 4 passed，跑完已恢复。definer 0 problems。

首次失败：
- 新 Action 没有 publish（夹具补上）；
- 工作台读取路径写错；
- 读取响应没有契约（新增两份）；
- lifecycle 的两项测试原先写死了 machine_key 路由集合和注册行集合，已收窄为只看自己的 6 个 Action。

## 变异检查

每次改一处，跑 `tests/test_machine_reads_pg.py`，跑完恢复：

| 变异 | 结果 |
|---|---|
| 去掉 scope 检查 | 1 failed |
| 去掉 capability_current | 1 failed |
| 去掉授权表检查 | 1 failed |
| 去掉授权类型检查 | 2 failed |
| 可见性不按 world 过滤 | 1 failed |
| 去掉对象擦除判定 | 1 failed |
| 去掉属性值擦除判定 | 1 failed |
| 授权取最早一行而不是最新一行 | 1 failed |
| handler 去掉类型预检 | 1 failed |
| handler 去掉“必须是读取动词”预检 | 1 failed |
| 投影不按类型取属性 | **存活**：属性还要在该对象的类型定义中存在才会返回，两层冗余，有意保留 |

## M4b（下一片）

object.neighbors（深度 1、每组上限、属性边可见性、未登记类型只计入 invisible）和 commitment.list（排除 quote、consumer_display、events.detail 和 principal_id、evidence.ref、exceptions.detail），按投影草案实施。迁移号在 0440 之后顺延。

## L6 条件 N1、N2（de9235ad 条件 PASS）

- **N1（legal hold 与保留期）**：我核对了 NX-029/NX-054 在 `ontology.objects` 上的语义，并照它原样执行，没有另写判定。
  - **legal hold**（`control.nexloop_retention_holds`，0132）：只阻止删除与擦除，从不隐藏对象。工作台照常显示被 hold 的对象，机器读取也照常返回，值相同。
  - **保留期到期**：保留守护进程直接删除过期对象（0129 中 CommercialRecord 的 `delete from ontology.objects`，只留无内容的墓碑）。删除后工作台查不到，机器读取返回 404，与不存在的对象相同。
  - **擦除**：Consumer 擦除中或已擦除（包括 completed_with_holds）时，工作台拒绝读取，机器读取返回同一个 404。
  - 新对照测试 `test_hold_expiry_and_erasure_match_the_workbench`：对同一个对象，分别做机器读取和工作台读取，覆盖 hold、过期、擦除三种情况，并断言 object.list 中只列出被 hold 的那一个。
- **N2（total 泄露数量）**：object.list 不再返回 `total`，也不再返回 `invisible`，因为两者都会透露看不到的对象有多少。一页可能少于扫描数，`next_after` 仍是扫描游标。投影、契约 `machine-read-object-list`、示例、README 和投影草案都已同步修改。变异检查：加回 total，有 2 项测试变红。
- **(i) 两个连接**：原先的撤销用例改为显式用两个连接。负责人经 API 的连接撤销授权；第二个独立连接（以 nexloop_api 身份直接调用 `authz.nexloop_machine_read`）撤销前读取成功，撤销提交后的下一次读取立即抛出 `not_permitted`。
- **(ii) 机器读取与工作台读取逐字段一致**：`test_machine_and_workbench_reads_agree` 原本就有。新的 N1 对照测试又在 hold 场景下逐字段比对了一次。
- 测试：test_machine_reads_pg 6 passed；契约相关测试全部通过，共 61 passed（Node 24，`-n 2`）。
