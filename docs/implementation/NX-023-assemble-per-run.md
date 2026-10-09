# NX-023 收尾：`nexloop.context.assemble:1` 随 Run 签发（临时迁移 0104）

状态：已实现，分支 `nx023-assemble-per-run`（BASE main `fb61852`），L4。
依据：调度员裁定——assemble 不作常驻授权，放进按 Run 签发的 Source 凭证；Run 结束或撤销后立即失效（NX-023-B §7、NX-023-C §4、NX-023-D）。

## 1. 现状（改动前）

assemble 有两个使用点，都要求 Source 凭证本身**常驻**持有 `nexloop.context.assemble:1` EXECUTE：

- 策略读取：`authz.nexloop_context_read` 的 `strategy` 分支（0090）；
- v6 绑定：`authz.nexloop_context_v6_command`（最新函数体在 0099）和 `authz.nexloop_role_context_v6_command`（0094）。

服务清单里的常驻 `context_assembler` 主体没有任何代码使用。测试夹具则直接给 Source 加了常驻 assemble。

说明：`nexloop.context.bind:1` / `bind_role:1` 目前同样是 Source 的常驻授权，并不在 Run 凭证里。本次只改 assemble。

## 2. 设计

Source 签发 Run 时（0034 `authz.nexloop_run_credentials.source_digest` 记录签发的 Source 凭证），随之获得只对这个 Run 有效的 assemble：

- 声明带 `run_assemble: {run_id, run_digest}`，其余字段与常驻证明同形，`facts=[]`。
- SQL helper `authz.nexloop_assert_context_assemble` 在每次使用时校验：
  - 调用者是 service 凭证（不是 Run 凭证），tenant、principal、credential、directory_hash、world 与当前身份一致；
  - 期限在 25 秒窗口内；
  - 指名的 Run 凭证 share 锁读取：
    - 由**本 Source 凭证**签发，且 `run_id` 与 `run_digest` 对应；
    - world 一致，`status='active'`，未过期，声明期限不超过 Run 期限；
    - Source 目录未变化；
  - Run 未结束：Run 一旦登记了 runtime 任务，任务不能已是 succeeded、failed 或 dead_lettered；
  - v6 bind 还要求 `run_assemble.run_id` 等于 pack 自己的 `bindings.run_id`，不能用别的 Run（即使同一 Source 签发）。
- 声明不带 `run_assemble` 时，仍走原来的常驻 EIOS 检查（`nexloop_assert_action_authority`），行为不变。

**迁移 `0104_nx023_assemble_per_run.sql`**（只追加）：

- 新 helper；
- 三个函数体的 `create or replace`：0090 的 `nexloop_context_read`、0099 的 `nexloop_context_v6_command`、0094 的 `nexloop_role_context_v6_command`。
  - 只把其中的 `perform authz.nexloop_assert_action_authority(p_digest,p_world,a);`（每个函数两处）换成 helper 调用，其余文本逐字相同；
  - search_path 统一为 `pg_catalog,pg_temp`。
- 授权 SQL 包装层（`nexloop_assert_read_authority` / O5b memo）未动。

**Python**：

- `context_engine.authority.run_assemble_claims`；
- `StrategyRegistry(..., run=None)`：传入 run 时用随 Run 的声明；
- `context_artifacts.v6_call(..., run=None)`；
- `ContextV6ArtifactProducer` 和 `RoleContextV6ArtifactProducer` 在 `prepare` 中取得已认证的 Run 凭证后，策略读取和 v6 绑定都只用它，不再需要常驻授权。

**服务清单 v6**：

- 移除 `context_assembler` 主体及其授权。
- 新增 `retired_principals` 段：
  - apply 时撤销这些主体仍持有的全部授权集（写空集，从不删除）；
  - doctor 把它们视为受管主体，遗留授权报告为偏差，而不是外来授权。
- `deferred` 中写明 assemble 随 Run 签发。
- 凭证本身不改状态（0050 不支持改凭证状态）。它已没有任何授权；如需吊销凭证，属于运维动作。

## 3. 改动范围（凭证签发相关）

- 没有修改 `run_credentials.py`、`role_runs.py`、`role_policies.py`、0034/0049 的签发逻辑，Run 的 `allowed_resources` 也不变。
- 只读取 `authz.nexloop_run_credentials` 和 `authz.nexloop_runtime_run_bindings` 作为依据。

## 4. 测试

**`tests/test_context_assemble_per_run_pg.py`**（4 例）：

1. 无常驻授权时 Source 单独读策略被拒；凭随 Run 的声明可读；Run 撤销后被拒；Run 过期后被拒。
2. Run 的任务登记后仍可读；任务变为 succeeded 后被拒（`run assemble ended`）。
3. 以下情况均被拒：
   - 其他服务凭证出示该 Run；
   - Run 凭证自己当 Source；
   - 改租户、改 world（含在 simulation world 出示）；
   - 换 run_digest、未知 run_id、目录 hash 不符、`run_assemble` 多键、带 facts；
   - Run 改为其他 world。
4. v6 bind 换用同一 Source 的另一个 Run：被拒（`run assemble other Run`），0 binding、0 pack。

**v6 链路**：`test_context_v6_pg`、`test_role_context_v6_pg`、`test_context_v6_relationships_pg` 的夹具去掉了 Source 的常驻 assemble，全部用随 Run 签发的路径通过。其中包括 AT-027 相关的逐次模型调用记录、Manifest 与 hash，以及 AT-028 相关的预算裁剪、否定与约束保留、显式 insufficient。

**清单**：

- `tests/test_service_grants_retired_pg.py`：
  - 先 apply v5（fb61852 版本），assembler 可决策 assemble；
  - doctor 对 v6 报告受管偏差；apply v6 撤销该授权，doctor 同步，assembler 不再有 assemble，再 apply 无变化；
  - `retired_principals` 的形状校验。
- `test_business_actions` 的断言改为：没有服务主体持有 assemble，退役名单为 `context_assembler`。

## 5. 限制

- 常驻路径仍保留在 SQL 中（声明不带 `run_assemble` 时）。部署清单已不授予任何主体，测试 `test_context_engine_pg` 仍用它验证常驻语义。若要从 SQL 中彻底移除，需要另行决定。
- "Run 结束"以 Run 凭证到期或撤销、或其 runtime 任务进入终态为准。仓库目前没有单独的 Run 撤销 API，测试以管理连接修改状态来模拟撤销。
