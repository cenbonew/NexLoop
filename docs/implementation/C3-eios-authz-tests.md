# C3：补齐上游 EIOS authz / identity 内核测试

- **来源**：`docs/design/eios-capability-gap.md` §A.1 第 1 项（C3）。调度员 2026-10-22 派工，L7 执行。
- **分支** `l7/eios-authz-tests`，基点 main `1892c9ac`。
- **范围**：只加测试。不改产品代码、不加迁移、不改契约。按 ADR-006 手工复制单个文件，记录来源。
- **上游**：冻结提交 `1e363db`（`versions.lock.json` 的 `upstream_sources.eios.commit`）。下文上游路径都相对上游仓库根。

## 1. 结论

- **新移植 18 个上游测试文件**：authz 13 个，identity 5 个。另外，已有的 3 个 authz 测试此前没有登记，本次补登。
  - 新增的 authz 13 个里，9 个逐字复制；
  - 4 个按 NexLoop 的重定义或目录结构改写：`facts` 1 处断言 + 删除 1 个不适用用例 + 领域字符串中性化，另 3 个只改 helper 的导入路径；
  - identity 5 个全部逐字复制。
- **运行结果**：
  - 首次运行 476 passed、2 failed，两处都是移植适配问题（§3），不是产品缺陷；
  - 适配后，`tests/upstream` 加 `tests/test_upstream_*.py` 共 **1029 passed**。
- **登记**：
  - 新建 `packages/eios-core/UPSTREAM_AUTHZ_TESTS.json`，登记 16 个 authz 测试：新增 13 个，加此前未登记的 `policy_domain`、`policy_effects`、`application_restrictions`；
  - `UPSTREAM_IDENTITY_TESTS.json` 追加 5 个；
  - `versions.lock.json` 的 `packages.nexloop-eios-core` 增加 `test_provenance`，指向三份清单；
  - `tests/test_vendor_imports.py` 增加两个守卫：authz 清单的 sha 校验（并要求“逐字复制 ⇔ 没有 adaptations”）；`tests/upstream/*.py` 与 `tests/test_upstream_*.py` 必须与三份清单完全一致。
- **没有发现确认级的产品缺陷**。发现一个**重要级的覆盖缺口**：vendored intersection 的等号边界，上游测试没有固定（§4）。已用 NexLoop 自有测试 `tests/test_authz_kernel_boundaries.py` 补上，只加测试。

## 2. 映射表

“NexLoop 使用”一栏的含义：

- **生产**：`nexloop_eios/` 的运行路径直接或间接调用该模块；
- **内核内部**：只被其它 vendored 模块调用；
- **契约**：NexLoop 运行时不用，只作为 vendored 契约保留。

NexLoop 的 SQL 侧授权（各迁移中的 `authz.*` definer 函数）不在这些测试范围内，由 NexLoop 自己的 PG 测试覆盖。

### 2.1 authz（`tests/upstream/test_eios_<名>.py`）

| 上游测试 | 被测 vendored 模块 | NexLoop 使用 | 处理 |
|---|---|---|---|
| `tests/authz/test_policy_domain.py` | `eios/authz/policy/*`（parser、evaluator、canonical、validator） | 生产：`service_grants.py`、`workbench_roles.py`、`sandbox_profile.py` 生成和校验策略 | 已存在、逐字一致；**本次补登记** |
| `test_policy_effects.py` | `eios/authz/policy/registry.py`（`apply_policy_effect`） | 生产：intersection 应用策略效果 | 已存在、逐字一致；补登记 |
| `test_application_restrictions.py` | `eios/authz/applications.py`、`grants.py` | 生产：各 ApplicationContext、`service_grants.py` | 已存在、**已改写**：资源类型词表加了 object、property、relation（PROVENANCE 已记录这一 NexLoop 扩展）；补登记 |
| `test_audit.py` | `eios/authz/audit.py`（审计记录模型） | 契约 | 新增，逐字复制 |
| `test_audited_resolution.py` | `eios/authz/facts.py`、`_fact_resolver.py` | 生产：`AuthorizationFactsResolver` 被 `action_definitions.py`、`object_reads.py`、`claim_store.py`、`recall.py` 等 15 处以上使用 | 新增；只改了 helper 导入（`.test_facts` 改为 `test_eios_facts`，因为 `tests/upstream` 不是包） |
| `test_context.py` | `eios/authz/context.py` | 内核内部（被 `service.py`、`grants.py` 使用） | 新增，逐字复制 |
| `test_decision_service_cache.py` | `eios/authz/service.py`、`cache.py` | 生产：`AuthorizationDecisionService().decide_resolved` | 新增；只改了 helper 导入 |
| `test_decisions.py` | `eios/authz/decisions.py` | 内核内部 | 新增，逐字复制 |
| `test_facts.py`（2962 行） | `eios/authz/facts.py`、`_fact_resolver.py` | 生产，同上 | 新增、**改写**，见 §3 |
| `test_grants.py` | `eios/authz/grants.py` | 生产 | 新增，逐字复制（关键词扫描命中的“revenue”“mirroring”是误报，不是领域代码） |
| `test_grants_adversarial.py` | `eios/authz/grants.py` | 生产 | 新增，逐字复制 |
| `test_intersection.py` | `eios/authz/intersection.py` | 生产：经 `decide_resolved` | 新增；只改了 helper 导入 |
| `test_policy_json_collections.py` | `eios/authz/policy/*` | 生产 | 新增，逐字复制 |
| `test_policy_registry.py`（1797 行） | `eios/authz/policy/registry.py` | 生产 | 新增，逐字复制 |
| `test_property_security.py` | `eios/authz/property_security.py` | 生产：经 policy registry；`schema_gates.py` 引用其规则名 | 新增，逐字复制 |
| `test_resources.py`（1269 行） | `eios/authz/resources.py` | 生产：资源引用在 `nexloop_eios` 中有 20 处以上使用 | 新增，逐字复制。5 个 `performance` 用例按计数断言，不看墙钟，在负载约 7 的开发机上并行跑通过，不需要放进低并发 lane |

### 2.2 identity（`tests/test_upstream_identity_<名>.py`，与已有的 `test_upstream_identity_sessions.py` 同一套命名）

| 上游测试 | 被测 vendored 模块 | NexLoop 使用 | 处理 |
|---|---|---|---|
| `tests/identity/test_admin_lifecycle.py` | `eios/identity/admin_lifecycle.py` | 契约（C5 身份管理的基础） | 新增，逐字复制 |
| `test_identity_settings.py` | `eios/identity/settings.py` | 内核内部 | 新增，逐字复制 |
| `test_password_reset_delivery.py` | `eios/identity/delivery.py` | 契约 | 新增，逐字复制 |
| `test_password_reset_request_inbox.py` | `eios/identity/delivery.py` | 契约 | 新增，逐字复制 |
| `test_rate_limits.py` | `eios/identity/rate_limits.py` | 生产：`browser_rate_limits.py` 用其决策模型 | 新增，逐字复制 |

### 2.3 不适用（已登记在 `UPSTREAM_AUTHZ_TESTS.json` 的 `not_applicable` 中）

| 上游测试 | 原因 |
|---|---|
| `tests/authz/test_architecture_boundaries.py` | 断言上游仓库布局与领域 composition 模块（223 处领域词），这些模块没有 vendored |
| `tests/authz/test_resource_inventory.py` | 需要 `eios.authz.inventory`、`eios.api.scopes`、`eios.packs.core`，都没有 vendored；对应缺口项 C8 |
| `tests/authz/test_echo_authority_manifest.py`、`test_echo_publication_call_sequence_golden.py`、`echo_publication_recording.py` | 需要上游 `ops/` 目录与 golden 调用序列，没有 vendored |
| identity 中的 `test_admin_credentials`、`test_admin_service`、`test_bff_service`、`test_composition_e2e`、`test_crypto_keyring`、`test_internal_assertions`、`test_invitations`、`test_models`、`test_oidc*`、`test_password_reset_request_envelope`、`test_recovery_codes` | 被测模块（OIDC、邀请、恢复码、BFF、keyring、断言、`login.passwords` 等）没有 vendored，对应缺口项 C4、C5、C10。等这些模块移植时一并搬入 |

（`test_upstream_local_accounts.py`、`test_upstream_identity_sessions.py`、`test_eios_action_*` 以前已经搬入，未改动。）

## 3. 改写明细（`tests/upstream/test_eios_facts.py`）

1. **`test_large_grant_handle_property_reads_do_not_rehash_payload`**
   - 首次运行失败：`TypeError: counted_projection() takes 1 positional argument but 2 were given`，被 resolver 包装成 `AuthorizationUnavailable`。
   - 原因：vendored 的 `_fact_resolver._project_authority_view` 在 NX-049 8c 中增加了可选参数 `reuse`；`_cached_view` 还会对每个已登记事实各投影一次。这些都在 PROVENANCE 里登记为 NexLoop 改写。
   - 改写：
     - 替身函数转发全部参数；
     - 单独统计“载荷投影”的次数，断言**恰好 1 次**；
     - 解析过程中允许事实级的缓存投影；
     - **解析之后的 60 轮属性读取不得再投影、不得再算哈希**。这条是上游测试的核心不变式，原样保留。
2. **`TestFactDenialCarriesTheDatabaseReason.test_database_message_is_redacted_and_single_line` 删除**
   - 它测的是 `eios.adapters.postgres.authorization_facts._redact_database_message`，这个模块没有 vendored，首次运行报 `ModuleNotFoundError`。
   - 核对：`nexloop_eios` 中没有任何地方用数据库错误消息构造 `AuthorizationFactDenied`，所以这一条不适用。
3. **领域字符串中性化**：
   - 请求 scope 由 `tennis.court.block` 改为 `orders.refund.approve`；
   - 拒绝 detail 去掉了上游厂商名。

   断言语义不变。

## 4. 覆盖缺口：intersection 的等号边界（重要级，非产品缺陷）

- **方法**：把 vendored `eios/authz/intersection.py` 中的边界判断逐个改坏，跑全部 `tests/upstream`（793 个），每次跑完都用 `git checkout` 还原，工作区未残留改动。

| 变异 | 位置 | 上游测试 | 判断 |
|---|---|---|---|
| `application.expires_at <= now` 改为 `<` | `_application_allows` | 全部通过（存活） | 系统层面不可达：resolver 在 `_fact_resolver.py:1414` 以 `now >= expires_at` 先拒收，这里是纵深防御 |
| `clearance.valid_until <= now` 改为 `<` | `_control_satisfied` | 存活 | 纵深防御，同类 |
| `clearance.rank >= minimum_rank` 改为 `>` | `_control_satisfied` | 存活 | **只在这里判定**：等级恰好等于最低要求时的放行与否，没有任何测试固定 |
| 授权窗口 `now < grant.valid_until` 改为 `<=` | `_evaluate_payload` | 存活 | 系统层面不可达：探针确认 `valid_until == trusted_now` 的授权在 resolver 阶段就报 `grant facts contain an inactive grant` |

- **处理**：新增 NexLoop 自有测试 `tests/test_authz_kernel_boundaries.py`（3 个用例）。它固定以下边界：
  - 等级相等时放行，低一级拒绝；
  - clearance 恰在到期时刻失效；
  - 应用恰在到期时刻不放行。

  对前 3 个变异重新验证：新测试分别 1、1、2 个失败，都能抓到。
- **不是产品缺陷**：当前代码的行为与文档注释一致（`expires_at <= now` 视为到期）。缺口在于没有测试锁住它，以后 cherry-pick 或优化时可能悄悄改变。

## 5. 实际执行的命令与结果

环境：`source ~/.nvm/nvm.sh && nvm use 24`；`PYTHONPATH=packages/eios-core/src:tests`；`uv run --frozen`。开发机 1 分钟负载约 6–8。没有 PG 测试，没有遗留进程。

| 命令 | 结果 |
|---|---|
| `pytest -x` 新增 18 个文件 | 首次失败：`test_eios_facts.py::test_large_grant_handle_property_reads_do_not_rehash_payload`（§3-1） |
| `pytest -n 3` 新增 18 个文件 | 476 passed，2 failed（§3-1、§3-2） |
| 改写后 `pytest -n 3 tests/upstream tests/test_upstream_*.py` | **1029 passed**，7.0 秒 |
| `pytest -m performance tests/upstream`（串行） | 6 passed，最慢 0.64 秒 |
| `pytest tests/test_vendor_imports.py` | 5 passed（含 2 个新守卫） |
| 变异检查（§4），每次后 `git checkout` 还原 | 上游测试 4 个变异全部存活；新边界测试抓到 3 个 |
| `pytest tests/test_authz_kernel_boundaries.py` | 3 passed |
| 测试文件 basename 唯一性（`find tests -name 'test_*.py' … | uniq -d`） | 没有重复 |

## 6. 验收与限制

- 对应缺口文档中的 C3。没有对应的 AT 编号；任务与验收状态由调度员判定。
- 这些是**纯内核单元与对抗测试**，用的是内存 provider，不证明 PostgreSQL 生产授权。NexLoop 的 SQL 侧授权由各自的 `*_pg` 测试承担。
- 本次没有跑全量 `scripts/ci/check`，留给调度员合并时统一跑。新文件不需要进入 `HEAVY_TESTS`（性能用例按计数断言），所以没有修改 `scripts/ci`。
