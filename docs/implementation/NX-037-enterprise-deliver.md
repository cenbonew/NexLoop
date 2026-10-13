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
