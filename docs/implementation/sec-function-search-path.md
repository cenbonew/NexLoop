# 收紧 definer 函数 search_path 与 TEMP 权限（分支 `sec-pgtemp-fix`）

BASE main `7b831ef`；临时迁移 `0099_tighten_function_search_path.sql`，编号由调度员统一调整。

## 防护措施
1. **TEMP 权限**：在当前库上对 PUBLIC 和全部运行时角色撤销 TEMPORARY，只授给 `nexloop_owner`（O5b 的会话 memo 需要它）。
2. **search_path**：把应用 schema 中所有固定了 search_path 的函数（包括 SECURITY DEFINER 函数、invoker 函数和触发器函数）统一设为 `pg_catalog, pg_temp`，其余配置（例如 `row_security`）保持不变。扩展自带的函数不在范围内。
3. **回归防护**：新增 `scripts/check_definer_search_path.py`，并接入 `scripts/ci/check`。它在一次性集群上 bootstrap 后检查：
   - 每个固定了 search_path 的应用函数都把 `pg_temp` 放在最后；
   - 每个 SECURITY DEFINER 函数都固定了 search_path；
   - PUBLIC 和运行时角色没有 TEMPORARY；
   - `nexloop_owner` 有 TEMPORARY。

## 审计数量（main 0091，bootstrap 后）
- 应用 schema 中固定了 search_path 的函数共 285 个：authz 214、control 44、ontology 26、runtime 1。其中 SECURITY DEFINER 238 个，invoker 47 个。修复前全部是 `search_path=pg_catalog`；没有不带配置的应用函数。
- 扩展函数 186 个（`extensions` schema），不在范围内。
- 修复前，PUBLIC 和全部 7 个运行时角色都有 TEMPORARY（数据库 ACL 为默认值）。

## 验证
- **静态检查**：在修复前的库（main 0091）上运行，报出 **293 个问题**（285 个函数 + PUBLIC + 7 个角色），退出码非 0；修复后为 0 个问题。与 O5b 的 0095 一起时为 291 个函数、0 个问题。
- **`tests/test_function_search_path.py`**（10 项）：
  - 修复后检查结果干净；
  - 检查能识别人为放入的未固定函数、顺序错误的函数，以及 PUBLIC 的 TEMP；
  - 6 个可登录的运行时角色都无法创建任何临时对象（表、类型、函数、视图）；
  - `nexloop_owner` 仍能创建临时表；
  - 其余函数配置保持不变。
- **与 o5b-impl 的组合**（临时分支，验证后已删除）：`test_read_assert_memo.py` 与本文件共 30 项全部通过。会话角色不能再创建临时对象之后，O5b 中两项伪造负例改为断言“连准备伪造都不可能”（`o5b-impl` `741b502`）；属主校验保留，作为纵深防御。
- **`test_bootstrap`**：2 项因临时编号失败（92 个迁移对 0099），重编号后会消失。
- **回归**：142 个相关测试文件，6 个 worker 并行，`.ci-results/sec-pgtemp-regression.xml`，778 s。结果 1557 passed / 8 failed：
  - 3 项是临时编号的预期失败：`test_bootstrap` ×2、`test_effect_execution_sql::test_42`；
  - 5 项是并行负载下的 B 类时延用例：`local_message_delivery_assembly`、`message_driven_delivery`、`relationship_context_v4[complete]`、`outbound_messages_pg::test_scope_denied_reply…`、`two_pi_role_runs`。这 5 项串行复跑全部通过（155.96 s，当时 load avg 7–10）。
- **产品代码**：Python、TS、SQL 中都没有由运行时角色创建临时对象的用法，撤销 TEMP 不影响业务路径。

## 后续
- 上游冻结提交中同类写法的函数需要上游另行处理；它们目前依靠“业务角色没有 TEMP 权限”这一条防护。
