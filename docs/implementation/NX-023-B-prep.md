# NX-023-B 前置：契约、授权与业务 Action 清单

基线 `dispatch/integration-s3o` `2f446b9`。调度员决定（2026-10-09）：1）context-manifest 来源 evidence_kind 增加三类；2）`nexloop.context.assemble:1` 进服务授权清单；3）`business_actions` 支持仅人类 owner 的 Action；4）策略变更是否推进控制 revision 留到 NX-024。未改 `runtime_activation.py`、`effect_intents.py`、`authorization.py`、`object_reads.py`、`service_offerings.py`、`role_runs.py`；无新迁移。

## 改动
- **业务 Action 清单**（`deploy/configuration/business-actions.v1.json`，manifest_version 2）：每条 Action 增加 `authority`。能力档案封闭为两类：`ontology.object.create` ↔ `service`；`context.strategy.publish` ↔ `human_owner`（executor_role 也必须是 `human_owner`）。新增 `nexloop.context.strategy.publish:1`，变更范围为元数据类型 `ContextStrategy`（`context_strategy_object_type()`，EIOS 要求每个 Action 至少一个对象类型；策略行仍只在 `control.nexloop_context_strategies`）。`compile_actions` 改为按能力名提供快照（`capabilities`，`capability` 保留为 object.create 简写），可 `select` 子集；缺快照或选择未声明的 Action 均拒绝。
- **服务授权**（`deploy/authorization/service-grants.v1.json`）：新增 `context_assembler`（nexloop_api）→ `eios:action:nexloop.context.assemble:1` execute。测试断言人类 owner Action 从不出现在服务授权里。
- **契约**：
  - `context-manifest`：`sources[].evidence_kind` 枚举**只追加** `formal_object`、`conversation`、`execution_state`。兼容性：原 7 个值全部仍有效（测试逐一断言）、旧示例仍有效；生成的 TS/Pydantic 类型只扩大枚举。
  - 新增 `context-pack-v6.schema.json`（草案）：`bindings`/`formal_facts`/`current_constraints`/`supply` 与 v2 逐字相同，`role.role_binding` 与 v3 相同、`role.role_policy` 与 v5 策略段相同，`current_event` 为 v2 用户消息（加 `kind`）或 v3 服务触发；新增 `strategy_ref`、`goal`（目标版本引用 + NX-022 控制快照）、`constraints`/`consumer_state`/`open_work`/`evidence`/`semantics`/`experience`（逐段限定 evidence_kind，hypothesis 只能在 `evidence.hypotheses`）、`budget_report`、`insufficient`。兼容性：**v2–v5 的 Python schema、SQL 绑定与 Host 解析完全不变，旧 Run 继续用 v5**；v6 只是新增协议，尚无写入方与 Host 解析（NX-023-B）。
  - 示例由 `context_engine.pack.assemble_v6` 实际组装生成；handoff 副本同步并重写 MANIFEST（只新增/更新这几个文件条目）。
- `context_engine/pack.py`：`assemble_v6`（分区校验 → 预算 → v6 线格式 → 生成的 `ContextPackV6` 模型校验）。

## 测试
`LC_ALL=en_US.UTF-8 PYTHONPATH=packages/eios-core/src:tests uv run --frozen pytest -q -p no:xdist tests/test_contracts.py tests/test_contract_generation.py tests/test_review_contracts.py tests/test_claim_contract.py tests/test_context_pack_v6.py tests/test_context_engine.py tests/test_context_engine_pg.py tests/test_business_actions.py tests/test_service_grants_pg.py tests/test_outbound_messages_pg.py tests/test_bootstrap.py tests/test_wheel_install.py` → **168 passed / 95.2s**（含 `tsc --strict` 编译生成类型）。首次失败：`test_business_actions` 测试辅助用 `model_validate` 构造能力快照被严格模式拒绝（改为 JSON 校验）；v6 示例首版用了非十六进制合成 ID，被 v6 schema 正确拒绝后修正。`generate_contracts.py --check` 通过；`validate_handoff.py` 0 失败。

## 追加：系统元数据类型清单（调度员决定 2026-10-09）
- 新增 `deploy/ontology/system-object-types.v1.json`（`nexloop-system-object-types/1`），第一条为 `ContextStrategy@1`，定义由 `context_strategy_object_type()` 生成；`nexloop_eios/system_object_types.py` 校验（身份一致、`only_edit_via_actions`、无重复）并给出可信配置 `object_types` 行。发布与业务 Action 清单同一路径（可信配置 0050），不直写表、不进任何服务授权。无迁移。
- 测试 `tests/test_system_object_types.py`（7 passed）：清单定义与生成 schema 逐字一致；清单校验负例；业务 Action 清单引用的对象类型都在已发布的会话类型（0046）或本清单中，且系统类型不出现在服务授权；在真实 PG 上经 `apply_manifest` 把 `ContextStrategy` 与由清单编译的 `nexloop.context.strategy.publish:1` 一起发布，落库内容与源一致；缺少该类型时可信配置校验直接拒绝。
- 回归：`pytest -q -p no:xdist tests/test_system_object_types.py tests/test_business_actions.py tests/test_context_engine_pg.py tests/test_wheel_install.py tests/test_bootstrap.py` → 32 passed / 37.2s，首次即通过。
