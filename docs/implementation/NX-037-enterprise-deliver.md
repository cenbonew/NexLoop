# NX-037 缺口 B：企业交付 Action `nexloop.enterprise.deliver:1`

依据：`NX-037-readiness-gaps.md` §0 第 4 项与 §1；调度员 2026-10-13 同意的两切片方案与条件。本文记录切片 1（后端）。切片 2（契约与 Agent）另行提交。

## 1. 设计

- **新 Action**：`nexloop.enterprise.deliver` 版本 1。名字固定，因为 L3 按这个名字做 provider 路由。它经 EIOS 受治理的效应路径执行：计划步骤 → 效应上下文 → 意图 → 独立效应 worker → 企业端点。
- **类别**：`customer_contact`，`external_effect: true`（`action-effect-categories.v3.json`）。
  - 理由：执行方是企业自己的系统，NexLoop 无法核实交付是否会接触客户；ADR-023 §3 要求不确定时宁可多挡。所以联系限制下它和消息一样会被拒。
  - 企业书面确认不接触客户后，再出 v4 改为 `non_contact_service`。
- **部署级允许表** `control.nexloop_effect_actions`（迁移 0400）：
  - 内容：每个效应 Action 一行，记录名字、版本和计划 slot。种入两行：`nexloop.service.request:1` 对应 `service.request:primary`，`nexloop.enterprise.deliver:1` 对应 `enterprise.deliver:primary`。
  - 写入权限：只能由迁移种入和修改。表上开启并强制 RLS，所有运行时角色（api、domain_worker、action_worker、scheduler、identity）都没有任何权限，连读也不行。
  - 读取方式：只在 SECURITY DEFINER 函数内部读取。
- **Action 的绑定规则**：
  - 计划步骤的 `action_name` 必须在允许表里；
  - 效应上下文的 slot 取该 Action 的 slot；
  - 意图的 Action 就是其上下文 slot 对应的 Action，并且必须与签名声明中的资源相同；
  - 执行侧从意图冻结的 `action_definition` 推出目标资源，不再使用进程级常量。
- **服务目录**：
  - 企业交付类 offering 有自己的固定形状：
    - `service_code` 以 `enterprise.` 开头；
    - `delivery_action` 为 `nexloop.enterprise.deliver:1`；
    - `content_kind` 为 `enterprise-delivery`；
    - `evidence_kind` 为 `enterprise_receipt`；
    - 价格是 CNY 的十进制字符串，不必为 0。
  - 目录提示要求 offering 的 `delivery_action` 必须等于意图的 Action。以前只有一个效应 Action，没有这条约束。
  - 公开 scope 在 SQL 和 Python 两侧的形状一致（`service_offerings.delivery_scope`）。

## 2. 迁移 0400（临时号）

- 新建允许表，并种入两行。
- 用 `create or replace` 复制下列函数的当前定义，只替换写死的 Action 名或 slot：
  - `authz.nexloop_assert_effect_plan`
  - `authz.nexloop_effect_context_command`
  - `authz.nexloop_effect_intent_command_v0040`、`_v0041`、`_v0052`
  - `authz.nexloop_effect_catalog_hint`
  - `authz.nexloop_assert_service_offering`
  - `authz.nexloop_service_catalog_scope`

  这些函数的属主、授权和 `search_path` 都不变，已发布迁移和校验和都不改。函数定义取自 bootstrap 后的库（`pg_get_functiondef`），每处替换都断言恰好命中一次。
- **有意不改、保留回复专用的路径**：
  - 消息 Run 的签发（`allowed_actions`）：要等 context pack 能携带企业 offering 后才放开，属于切片 2；
  - 兜底回复 Run：ADR-023 规定它只有一个回复工具；
  - 本地 JSON 投递授权；
  - 会话回执与作用域拒绝的查询；
  - 各 context pack 构建函数中的 `current_constraints.action`：属于切片 2。

## 3. Python

- `EffectContextRegistrar` 和 `EffectIntentPort` 新增 `action=` 参数，默认是回复 Action；值必须在 `effect_contexts.EFFECT_ACTIONS` 中。SQL 侧以允许表为准。
- 后端 `configure_effect_control`、`bind_effect_context`、`submit_effect_intent`、`find_effect_receipt` 都新增可选参数 `action`，缺省为回复 Action，所以现有调用方不受影响。
- `effect_execution`：`_send(metadata)` 从意图冻结的定义推出 `eios:action:<名>:<版本>`，用于 claim、admit、finalize、来源证明和目录信封。`SEND` 常量保留，只给本地 JSON 投递（回复通道）使用。
- `service_offerings`：新增 `enterprise_delivery_example`；`delivery_scope` 接受企业 offering。
- `service_grants.FORBIDDEN_MARKERS` 加入新名字。效应 Action 只能由租户的受信配置授予（执行者和 Run 持有者的 authority facts），不能经由服务授权清单授予。
- NX-037 配置：
  - `nx037-trial.v2.json`：只把 `effect_categories` 改为指向 v3；
  - `nx037-interaction-plan.v2.json`：`real_dispatch.actions` 改为 `eios:action:nexloop.enterprise.deliver:1`；
  - 读取方同时接受 v1 和 v2，默认使用 v2。
  
  v1 文件、分类 v2 和服务授权 v1 都保持不变。

## 4. 测试（`tests/test_enterprise_deliver_pg.py`）

- **允许表**：内容是这两行；五个运行时角色执行 insert、update、delete、select 都报 `InsufficientPrivilege`。
- **三层开关**，使用真实独立 worker 和本地 provider：
  - 总闸关闭时，派发被拒，记下 NXC07，provider 收到零请求；
  - 三层都打开时，恰好一次 POST；
  - 单独关闭租户层，或单独关闭 Action 层，都会被拒。只打开回复 Action 的开关，打不开企业交付：`closed_layer` 返回 `action`；
  - 关闭之后新提交的意图在派发时被拒，provider 收到零请求。
- **派发时复查**：在 admit 之前撤销来源授权，或关闭效应控制，结果都是 `admission_unavailable`，provider 收到零请求，意图不会被标为成功或失败。
- **结果未知时先查询**：provider 接受并提交后，worker 被 SIGKILL；下一个 worker 先 GET 同一意图，不会再 POST。
- **分类 v3**：新条目为 `customer_contact`，外部效应；v3 只在 v2 之后追加；应用后 `effect_is_external` 为 true。
- **目录不匹配**：Action 是企业交付但 offering 是导出类时，提交被拒，不会产生意图。
- **服务授权**：禁止标记中包含新名字。
- fixture 支持 `execution_plan` 的 `'enterprise-deliver'` 和 `'enterprise-mismatch'` 两个参数；缺省仍是回复 Action。

## 5. 对 L3 的接口（provider 路由）

- 对外常量和映射：
  - `effect_contexts.EFFECT_ACTIONS` 是 Python 侧允许的效应 Action 名；
  - SQL 侧的权威来源是 `control.nexloop_effect_actions`；
  - 一个意图的 Action 是 `runtime.nexloop_effect_intents.action_name` 和 `action_version`，也就是 worker 元数据中 `action_definition.stable_name` 和 `version`。
- 当前 `claim_effect` 和 `prepare_effect_dispatch` 的返回值不含 Action 名，`effect_dispatch.py` 若要按 Action 路由，需要从这两个返回值之一取得 Action 名。这属于 L3 的接口，本切片没有改它，由调度员协调。

## 6. 留给切片 2

- context-pack-v6 中的 `action` 和 `delivery_action` 由 const 改为 enum；同步修改 `contracts.py`、`context_pack.py` 和 Host 的 `context-input.ts`。
- `runtime-effect-tools.ts` 按服务目录提供企业交付工具；后端 effects 路由传入 action。
- 消息 Run 签发时，`allowed_actions` 按目录授予企业交付。

## 7. 切片 2：消息 Run 的企业 slot（ADR-030，已批准）

### 7.1 迁移

- **0401**：`control.nexloop_effect_run_contexts` 的键改为 `(run_id, slot_identity)`。slot 取自上下文，由触发器填写。
  - 意图受理按所声明 Action 的 slot 取上下文；
  - 只服务于回复的读取优先取回复 slot，因此单 slot 的 Run 行为不变；
  - 目录元数据按 Action 选择 slot 和 offering：消息 Run 的回复用 `supply`，企业交付用 `effect_supplies` 中对应的那一项。
- **0402**：
  - **登记表**：转发登记表新增三个可空列 `enterprise_control_id`、`enterprise_offering_id`、`enterprise_binding_id`，三列必须同时有值或同时为空（check 约束）。
  - **登记命令**：只能通过受治理的登记命令写入这三列；该命令是 0141 命令的包装，要求负责人主体、带意图 id，登记行只追加，本身就是审计记录。运行时角色无权写这张表。
  - **profile**：`runtime.nexloop_relay_profile` 只在三列有值且当前仍然有效时才带上 `enterprise` 块；已登记但失效时，带 `enterprise_skipped` 和原因。三列为空时，profile 逐字节不变。
  - **签发**：`allowed_actions` 只接受两种值：只有回复，或回复加企业交付。后者要求第二份来源证明，并且登记的企业 slot 当前有效；Run 凭据同时授予两个 Action。

- **0403**：消息 Run 的 context pack（v2 路径）在 Run 绑定了企业 slot、且登记的企业 slot 当前有效并通过 `assert_service_offering` 时，带上 `effect_supplies`（一项，`action` 为 `nexloop.enterprise.deliver:1`）。快照和绑定两阶段算法相同；没有企业 slot 的 Run，pack 不变。
- **0404**：中继 Source 读取企业 offering 和企业绑定。企业意图的目录信封来自 Source 对这两个对象的签名 READ，而 0142 的 `relay-source-v1` 派生只覆盖回复那两列，企业意图因此在受理前就被拒。做法是包装：0142 函数体不变（改名），它不派生时，再对当前有效登记的企业列应用同一规则（同一中继规则、同一固定字段表、同样排除删除中的消费者和负责人限制的属性组，绑定须指向本 Source、本消费者和本 offering），返回形状相同。offering 本身是否有效，仍由提交和派发时的 `assert_service_offering` 判断。

### 7.2 中继

- 只有 profile 带 `enterprise` 块时，中继才会：
  1. 在同一个目标下多建一个企业计划步骤（`action_name` 为 `nexloop.enterprise.deliver`，`control_id` 为企业控制）；
  2. 让路由的 `allowed_actions` 包含两个 Action；
  3. 用两份来源证明完成签发；
  4. 通过 `bind_effect_contexts` 在同一事务中绑定两个 slot，顺序固定为先回复、后企业。
- 已登记的企业 slot 失效时，中继只签发回复 slot，并记一条结构化日志：logger `nexloop.message_relay`，事件 `enterprise_slot_skipped`，带 `consumer_id` 和 `reason`（`control_mismatch`、`control_invalid`、`offering_invalid` 或 `binding_invalid`）。这不会让 Run 失败。

### 7.3 负责人登记企业 slot（给 L5 的租户装配手册）

前置条件，每个消费者各做一次：

1. **企业效应控制**：负责人创建一个 EffectControl。`consumer_id` 是该消费者；`executor_principal` 是效应执行者，它必须同时持有 `nexloop.service.request:1` 和 `nexloop.enterprise.deliver:1` 的执行权；`budget_units` 是企业交付的预算单位，与回复控制的预算各自独立；`allow_effect` 为 `true`；`valid_until` 按需设置。创建后执行配置：
   ```
   owner.configure_effect_control(control_id=<企业控制>, control_revision=1, executor_token=<执行者令牌>, action='nexloop.enterprise.deliver')
   ```
2. **企业 offering**：目录维护者创建 ServiceOffering，形状由 `service_offerings.enterprise_delivery_example(service_code='enterprise.<代码>', title=…, price_amount='<CNY 金额>', valid_until=…)` 给出；再创建 ConsumerServiceOffering，把该 offering 绑定到这个消费者，`source_principal` 是中继的 Source 主体。
3. **授权**：受信配置要给 Source 主体和效应执行者加上 `nexloop.enterprise.deliver:1` 的执行权。通过 authority facts 下发，服务授权清单不能授予这个 Action。

登记命令：负责人主体，必须是回复控制和企业控制的属主。

```
relay_enrollment.enroll(owner_service, consumer_id=<消费者>, control_id=<回复控制>, offering_id=<导出 offering>, binding_id=<导出绑定>,
                        relay_principal=<中继主体>, intent_id=<8–160 位意图 id>,
                        enterprise={'control_id': <企业控制>, 'offering_id': <企业 offering>, 'binding_id': <企业绑定>})
```

- **登记时的校验**：
  - 同一租户、同一 world；
  - 调用者是企业控制的属主，企业控制的 `consumer_id` 等于登记的消费者，并且 `allow_effect` 为 `true`；
  - 绑定属于这个 offering 和这个消费者，并且有效；
  - offering 的 `delivery_action` 是 `nexloop.enterprise.deliver:1`，并且有效；
  - 企业控制不能与回复控制是同一个。
- **清除**：不带 `enterprise` 参数再登记一次，会生成新的一个修订，三列为空。
- **撤回整个登记**：用 `relay_enrollment.withdraw`。

### 7.4 Host 工具

- 后端 effects 请求体可带 `action`；`runtime_activation` 校验它在 `EFFECT_ACTIONS` 中，并且 Run 凭据授予了它。
- Host 只在本 Run 的 context pack 带有企业 `effect_supplies` 时提供 `nexloop.enterprise.deliver` 和 `nexloop.enterprise.find`。参数为 `summary`（必填，≤200）、`details`（可选，≤2000）、`consumer_ref`（缺省为本 Run，只能等于本 Run）；不带 tenant_id，未知键拒绝。
- 配置了 `context_strategy` 或 `relationship_recipe`（v6/v4 pack）的中继不签发企业 slot，日志原因为 `context_unsupported`；这两种 pack 目前不携带 `effect_supplies`。

### 7.5 派发时复查（`tests/test_enterprise_slot_dispatch_pg.py`）

同一个中继签发的消息 Run，回复和企业交付各提交一个意图（提交时都是 accepted），由真实独立 worker 和本地 provider 派发：

| 场景 | 企业交付 | 回复 |
|---|---|---|
| 全部检查通过（基线） | POST | POST |
| 来信即拒收联系（真实 0109 规则） | 派发时拒绝，NXC05，provider 零请求 | POST（ADR-023 §2.6） |
| 企业 offering 在提交后失效 | 受理时目录断言拒绝，无拒绝码，provider 零请求 | POST |
| 只打开回复 Action 的真实派发开关 | NXC07，provider 零请求 | POST |

开关须在提交前打开：提交后的开关变化会使快照被取代（NXC02）。


## 8. L4 审查 0400 的处理（`docs/reviews/0400-enterprise-deliver-review.md`，分支 l4/review-0400）

| 项 | 处理 | 测试 |
|---|---|---|
| F1 `action_version` 用 LIKE 比较 | 0405 以当前定义（0401 之后）复制三条意图命令（v0040、v0041、v0052），只把该比较换成与允许表行版本的精确等值；NULL 也会被拒 | `test_the_signed_action_version_must_equal_the_allow_list_version`：签名后的 find 载荷缺少版本、或为 `%`、`_` 时，SQL 都返回 `runtime effect context unavailable`（与 0400 之前相同）；正常 find 作为对照通过 |
| F2 允许表可改、revoke 不全 | 0405 加行级 update/delete 和语句级 truncate 触发器（`control.nexloop_nx022_append_only`），连属主也不能改，只能由迁移先删触发器；revoke 补全到所有应用角色 | `test_effect_action_allow_list_is_append_only_even_for_its_owner` |
| F3 同一消费者不能同时用两个 offering | 消息 Run 由切片 2 解决：回复用 `supply`，企业用 `effect_supplies`，按 slot 取目录 | `test_enterprise_slot_dispatch_pg.py` 的基线用例：同一消费者同时有回复和企业 offering，两个意图都发到 provider。非消息 Run 仍要求恰好一个匹配所声明 Action 的 offering（ADR-030 §4 不改） |
| F4 测试只断言 `Exception` | 改为断言 SQL 拒绝原因。0401 之后，计划 Run 的目录元数据只选交付 Action 与意图一致的 offering，因此原因是 `effect catalog registration unavailable`（审查时在 0400 上测到的是 `outside catalog terms`） | `test_an_offering_that_delivers_through_another_action_is_outside_the_catalog` |
| 未覆盖：Run 有两个 Action 授权，只有企业 slot 上下文，却提交回复 | 已能拒绝，无需改代码 | `test_a_run_holding_both_actions_with_only_the_enterprise_context_cannot_submit_a_reply`：中继签发双 Action 的 Run 后，测试删掉回复 slot 的上下文行，回复意图在 SQL 被拒（`effect catalog Plan unavailable`），没有生成意图 |

以上反例测试在去掉 0405 后都会失败，加上 0405 后都通过。
