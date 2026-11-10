# 操作单：标记测试人设并删除合成顾客（迁移 0220，临时编号）

用途：AT-050 第二次恢复演练需要在 stage 上删除一个合成顾客，并在恢复后的重放里看到这次删除。负责人用“私有配置文件 + 一条命令”完成，口令从 Keychain 经管道传入，不使用浏览器控制台。

依据：ADR-034（测试人设标记，这里提前实施其最小一步）；NX-029 删除流程；调度员 2026-10-18 选定方案 A。

## 机制

- **persona**：每个消费者有一个人设值 `real`、`simulated_agent` 或 `synthetic`。没有记录即为 `real`。只能经负责人 Action `nexloop.consumer.set_persona:1` 修改，每次修改有修订号和一条只追加的事件（原值、新值、执行人、请求、理由）。改回 `real` 即撤销。
- **合成删除**：负责人 Action `nexloop.consumer.erase_synthetic:1`。SQL 先读 persona，为 `real` 时拒绝（工作台返回 409，`details.reason` 为 `persona_real`）；否则调用原有的 NX-029 删除处理函数，阻断立即生效，清除由保留期 keeper 完成。
- **不变的部分**：真实顾客的删除请求仍走原有的 `nexloop.consumer.erase:1`，不受 persona 影响。恢复后的删除重放（0135）直接调用原处理函数，不再检查 persona。
- **只读预查**：`GET /api/v1/workbench/erasure-precheck/<consumer_id>`，仅负责人可用。返回 persona 及其修订号、删除请求的状态（不含理由），以及仍保留的数量：会话、消息、仍带正文的消息、Claim、值尚未清除的 Claim、数据导出，以及 `grants_held` / `grants_trace`（迁移 0429，临时编号）。不返回姓名、正文或登录主体。
- **删除不撤销 WebChat 授权，删除后也追溯不到登录主体**（0429）：0220 的删除清掉顾客的数据和它与登录主体的对应，但不动 0050 发布的授权事实，只有私有配方里写 `revoke: true` 并用 `webchat_consumers.py` 发布一次，授权才会清空（stage 2026-11-09：customer-4 删除后仍有 `nexloop.conversation.read:1` 等 5 个授权）。删除完成后，对应关系已删除；联系键只来自 Consumer 的 `external_id_refs`，而 WebChat 顾客从不做浏览器身份关联，里面没有浏览器本地引用，所以删除后无从找回登录主体（`tests/test_erase_synthetic_consumer_pg.py::test_after_a_real_erasure_a_webchat_principal_is_untraceable`，真实流程）。因此顺序固定为**撤销 → 删除 → 核对**，并在删除时把关：
  - 合成删除（`nexloop.consumer.erase_synthetic:1`）在登记新请求前查这个顾客的登录主体：仍有非空授权时拒绝（工作台 409，`details.reason` 为 `grants_held`），什么都不写；
  - 通过时在删除登记上记一个布尔值 `grants_cleared`（不含登录主体）：登录主体已知、并且每个都有授权事实且全部为空时为 true；没有登录主体，或登录主体没有任何授权事实（从未配置，不能证明已撤销）时为 false；0429 之前的登记、`nexloop.consumer.erase:1` 和恢复后的删除重放为空。
  - `grants_trace` 取值：`owner_link`（删除前，按对应关系计数）、`contact_keys`（删除后经联系键找回，WebChat 顾客通常不会出现）、`no_grant_facts`（找到了登录主体但没有任何授权事实，`grants_held` 为 null）、`cleared_at_erasure`（删除时已核对清空，`grants_held` 为 0）、`untraceable`（已删除、追溯不到、也没有 true 标记，`grants_held` 为 null）、`no_owner`（未删除、没有登录主体，只出现在删除前）。

## 顺序

| 步 | 内容 | 由谁 | 类别 |
|---|---|---|---|
| 1 | 部署含 0220 的版本；`stage_secrets publish` 发布 business-actions（新增两个 Action）；按 workbench-roles 清单 v14 应用成员与授权 | 调度员 | B 类 |
| 2 | `mark`：从私有配方补标测试账号为 `simulated_agent`（先 `--dry-run`） | 负责人 | — |
| 3 | AT-050：记录恢复点 | 调度员 | — |
| 4 | **撤销**：在私有 webchat-consumers 配方里把这个顾客改为 `revoke: true`，用 `webchat_consumers.py` 发布一次（技术配置员） | 负责人 | — |
| 5 | `erase --dry-run`：核对 persona、将删除的数量，以及 `grants_held` 为 0、`grants_trace` 为 `owner_link`（不为 0 时结果是 `grants_held_revoke_first`，回到第 4 步） | 负责人 | — |
| 6 | **删除**：`erase` | 负责人 | — |
| 7 | **核对**：keeper 跑完后再次 `erase --dry-run`，作为事后核对（`grants_held` 为 0，`grants_trace` 属于 {`owner_link`, `contact_keys`, `cleared_at_erasure`}，新删除的应为 `cleared_at_erasure`；见“事后核对”） | 负责人 | — |
| 8 | AT-050：恢复到第 3 步的恢复点，执行删除重放。撤销和 `grants_cleared` 都在数据库里：恢复会把撤销一起回退，重放不经合成删除、不写标记。所以先重做第 4 步，再按下文“存量顾客”的只读 SQL 按登录主体核对授权为 0；这时的 `erase --dry-run` 会是 `erased_grants_unknown`（预期，不当作 0） | 调度员、负责人 | — |

第 2 步要放在恢复点之前：恢复后的数据库里仍保留 persona 标记，与恢复前一致。重放本身不依赖 persona。

## 命令

```bash
security find-generic-password -s <keychain 服务名> -a <账号> -w | \
  python3 scripts/deploy/erase_synthetic_consumer.py mark --origin https://<APP_HOST_NAME>:<PORT> --ca-file <CA> \
  --username-file <用户名文件> --password-stdin --config <persona 配置> --recipe <webchat-consumers 私有配方> --dry-run
```

```bash
security find-generic-password -s <keychain 服务名> -a <账号> -w | \
  python3 scripts/deploy/erase_synthetic_consumer.py erase --origin https://<APP_HOST_NAME>:<PORT> --ca-file <CA> \
  --username-file <用户名文件> --password-stdin --config <删除配置> --dry-run
```

去掉 `--dry-run` 即正式执行。配置示例见 `deploy/examples/consumer-persona.example.json` 与 `deploy/examples/erase-synthetic-consumer.example.json`；配置文件和用户名文件须为 0600，并且属于执行者本人。

- `mark` 只接受私有配方中 persona 为 `simulated_agent` 且未撤销的登录主体；写入的 persona 取自配方。
- `erase` 对预查显示为 `real` 的顾客不发送任何请求；服务端同样会拒绝。
- 同一配置可以重跑：幂等键由顾客和 `confirmation_ref` 派生，重跑即重放。

## 输出与退出码

输出一个 JSON 对象，只含 ID、persona、状态和数量，不含姓名、用户名、口令、cookie、CSRF 令牌或理由。

| 退出码 | 含义 |
|---|---|
| 0 | 全部完成；或 dry-run 中每个顾客都可以执行 |
| 1 | 有顾客被拒绝或不符合条件 |
| 2 | 输入被拒 |

`result` 取值：

| 子命令 | 取值 |
|---|---|
| `mark` | `would_mark`、`marked`、`already_marked`、`unchanged`、`not_in_recipe`、`refused` |
| `erase` | `would_erase`、`erasing`、`replayed`、`already_erasing`、`already_erased`、`grants_held_revoke_first`（授权未撤销，不发送；服务端也会 409 拒绝）、`erased_grants_held`（已删除但仍追溯到非空授权）、`erased_grants_unknown`（已删除，无法证明授权已清空：`grants_held` 为 null），后三者退出码 1、`persona_real`、`consumer_unavailable`、`refused` |

## 事后核对

删除完成后，预查应满足以下全部条件：

- `erasure.state` 为 `completed`；
- `held.messages_with_body` 为 0；
- `held.claims_not_erased` 为 0；
- `held.data_exports` 为 0；
- `consumer_object` 为 false；
- `held.grants_held` 为 0，并且 `held.grants_trace` 属于 {`owner_link`, `contact_keys`, `cleared_at_erasure`}（L6 2026-11-10）。三种取值都表示“追溯到了登录主体、它们的授权集都为空”，或者“删除时已核对清空”：
  - 新删除的顾客应为 `cleared_at_erasure`；
  - `owner_link` 出现在 0306 之前完成的删除上：owner 行残留，迁移 0442（临时编号）会清理，清理后变为 `cleared_at_erasure`；
  - `contact_keys` 是删除后经联系键找回的。
- `grants_held` 为 null（`untraceable`、`no_grant_facts`）时是 `erased_grants_unknown`，不当作 0：按下节用只读 SQL 按登录主体核对。大于 0 时是 `erased_grants_held`：先撤销。

## 存量顾客（0429 之前删除的 customer-4、refusal-1、refusal-2）

它们删除时没有把关，也没有 `grants_cleared` 标记，事后核对一律是 `erased_grants_unknown`。改为直接撤销，再按登录主体只读核对（阳性对照，由调度员执行；`$T` 为租户，`<PRINCIPAL>` 取自私有配方）：

1. 撤销前（预期每个登录主体都大于 0，stage 11-09 读数为 5）：

   ```bash
   ro "select f.entity_key[1] principal, count(*) filter (where jsonb_array_length(coalesce(f.payload->'grants','[]'::jsonb))>0) n_grants, count(*) n_facts from authz.nexloop_authority_facts f where f.tenant_id='$T' and f.fact_kind='grants' and f.entity_key[1] in ('<PRINCIPAL-customer-4>','<PRINCIPAL-refusal-1>','<PRINCIPAL-refusal-2>') group by 1 order by 1"
   ```

2. 在私有配方里把这三个改为 `revoke: true`，用 `webchat_consumers.py` 发布一次。
3. 撤销后再跑第 1 步的 SQL：`n_grants` 为 0，`n_facts` 不变（5，空授权集，证明是撤销而不是查不到）。`n_facts` 为 0 时不算通过：说明登录主体写错或事实不在。

以后的删除一律按上文顺序：撤销 → 删除 → 核对。

## 已删除顾客的 owner 行残留（迁移 0442，临时编号）

- **现象**：stage 2026-11-10，customer-4 的 dry-run 结果是 `already_erased`、`grants_held` 0、`grants_trace` `owner_link`。它的 Consumer 已在 10-19 删除，`control.nexloop_consumer_owners` 里却还有它的一行（world real，幂等键为 `business-setup-<uuid>-ownership`，是建档时写入的那一行）。
- **原因：历史残留**。
  - 10-19 的删除跑在发布 22 上（bootstrap 修订号 0221）。
  - 删除 owner 行是 0306 才加的（10-21）。它挂在删除登记表的 AFTER UPDATE 触发器上，只在状态**变为** `completed` 的那一刻执行（0306 第 72 行，0388 保留了这个条件）。0306 之前已经完成的删除不会再被处理。
  - 删除之后无法重新绑定：受治理的 `ConsumerOwnership.create`（0138 `commit_owner`）要求 Consumer 对象存在，而删除流程会删掉这个对象（0138 第 268 行）。
- **清理（受治理，不写 SQL）**：迁移 0442 部署时，对每个租户执行一次 `control.nexloop_erasure_owner_residue_settle`：
  - 对象是已完成、不在保留期内、仍有 owner 行的删除。
  - 登录主体已没有非空授权：把 0429 的 `grants_cleared` 标记按同一规则补上（只补原来为 null 的），再删除 owner 行。
  - 仍有授权：保留这一行，预查继续显示 `grants_held` 大于 0；先撤销，再由下一次部署或负责人请求的再次执行处理。
  - 同时新增守卫：删除请求确认之后（`pending_confirmation` 以外的任何状态），不允许再给这个 Consumer 插入或改写 owner 行（`consumer erased`）。
- **部署后只读核对**（调度员）：

  ```bash
  ro "select e.consumer_id, e.grants_cleared, (select count(*) from control.nexloop_consumer_owners o where o.tenant_id=e.tenant_id and o.world=e.world and o.consumer_id=e.consumer_id) owners from control.nexloop_consumer_erasures e where e.tenant_id='$T' and e.state='completed' order by e.completed_at"
  ```

  - 预期每行 `owners` 都为 0。customer-4 的 `grants_cleared` 为 true，它的 dry-run 变为 `cleared_at_erasure`。
  - refusal-1、refusal-2 当初没有 owner 行可以追溯的，`grants_cleared` 仍为 null，事后核对仍按上节的只读 SQL 进行。
  - 审计（部署证据）：每个被清理或保留的 Consumer 有一条 `nexloop.consumer_erasure.owner_residue`（只有 tenant、world、consumer_id、request_id、outcome，不含登录主体）；每个租户还有一条 `nexloop.consumer_erasure.owner_residue_run`，带 settled 和 kept 数，同时写一条迁移日志 NOTICE：

    ```bash
    ro "select action, payload from runtime.audit_events where tenant_id='$T' and action like 'nexloop.consumer_erasure.owner_residue%' order by created_at"
    ```

    预期：customer-4 一条 `outcome=settled`；run 一条 `settled=1, kept_grants_held=0`。

删除完成后 persona 标记行也随之删除，`persona` 显示为 `real`；它的修改事件保留取值和时间，顾客 ID 换成假名，理由清空（0227）。

数据导出文件如果已经下载到 Fileshare 或本机，不在数据库可见范围内，需要按导出记录另行清理。

## 限制

- 配置里 `consumer_id` 与配方中登录主体的对应关系由负责人自己保证，服务端不核验。服务端只保证：未标记或标为 `real` 的顾客不能经合成删除路径删除。
- ADR-034 的其余部分（Consumer 对象属性、渠道绑定检查、报表与导出按 persona 排除）尚未实施。
