# NX-049 手段 8a + 8b：授权事实重复校验的削减

分支 `nx049-resolve`，BASE `dispatch/integration-s3w`（`4448c40`）。背景见 `NX-049-analysis-round2.md` §6：
- O3 缓存交回的是同一个冻结实例，但 EIOS 核心的 `_load_fact` → `_exact_model` 随即对它做一次完整重校验（`revalidate_instances="always"`），并重算整份规范摘要；
- 在部署主机上，这部分是重叠请求在 GIL 下串行的主要 Python 开销。

## 1. 改动

| 文件 | 改动 |
|---|---|
| `packages/eios-core/src/eios/authz/_fact_resolver.py`（vendored） | 8a：新增 `verified_model_from_json`、`_reachable`、`_is_verified`、`_VERIFIED`；`_exact_model` 先查登记，命中时原样返回，否则走与上游相同的完整校验 |
| `packages/eios-core/src/eios/authz/applications.py`（vendored） | 8b：`_typed_resource_id` 与 `ResourceRestriction` 的限制项摘要（`_restriction_digest`）做有界 LRU 记忆化（各 8192 项）；只对精确的 `str` 参数生效；异常不缓存；`_typed_resource_id` 仍返回调用方传入的原对象 |
| `packages/eios-core/src/nexloop_eios/authorization.py` | O3 登记点，共 2 行：新增 `from eios.authz._fact_resolver import verified_model_from_json`；`FactParseCache.parse` 中 `model.model_validate_json(text)` 改为 `verified_model_from_json(model, text)` |
| `packages/eios-core/PROVENANCE.json` | 两个 vendored 文件各登记一条 adaptation（改了哪些函数、为什么），并更新 `nexloop_sha256` |
| `tests/test_verified_fact_registry.py` | 新增等价测试与负例（§3） |

关闭开关：`NEXLOOP_EIOS_FULL_REVALIDATION=1`（或把模块变量 `_TRUST_VERIFIED` 设为 False）。关闭后每个实例都完整校验，与上游一致。

## 2. 8a 的信任边界

**哪些路径能登记**
- 只有 `verified_model_from_json(model, text)` 写入登记，并且只登记**它自己刚刚用完整 `model_validate_json` 产生的那个实例**。
- 没有任何函数接受调用方传入的实例来登记，所以“先构造、再登记”这条路不存在。
- 只登记同时满足以下条件的模型：`frozen`、`strict`、`revalidate_instances="always"`，且 `type(instance) is model`。
- 实例图中出现未知类型时不登记，该实例照常完整校验。
- 产品代码中唯一的调用点是 O3 的 `FactParseCache.parse` 未命中分支。输入是本次从 PostgreSQL 读到的事实 JSON，校验调用与原来完全相同。

**登记里记了什么，命中时怎么核对**
- 记录：指向实例的弱引用，以及登记时从实例可达的**每一个对象的身份**，按确定顺序排列。包括模型的字段字典、私有属性（如 `ResourceRestriction._validated_seal`）、extra、元组/列表/集合的元素、`dict` 与 `FrozenJsonMap` 内部字典的键和值、所有标量与类引用。
- 命中前核对三件事：
  1. id 对得上；
  2. 弱引用指向的正是这个对象，从而排除 id 被复用；
  3. 重新遍历一遍，逐个用 `is` 比较身份。
- 只要任何一处被原地改过（`object.__setattr__`、替换 `__dict__`、往内部字典或容器里加东西），遍历结果就不同，于是走上游的完整校验：内容不一致时由印章校验拒绝；内容一致时，与上游一样得到同值的新实例。
- 实例被回收时，弱引用回调删除登记，所以不会泄漏，也不会出现 id 复用后误命中。

**为什么进程内其它代码无法伪造登记**
- `model_construct`、`model_copy`、手工 `__new__` 产生的都是新对象，从未登记，必然完整校验。
- 改动已登记的实例，会被身份遍历发现。
- 要绕过这些检查，只能直接写私有的 `_VERIFIED` 字典，或者猴子补丁 `_exact_model` / `_is_verified`。能这样做的代码同样能补丁上游的 `_exact_model` 或判定服务本身；上游的“总是重校验”也挡不住它。因此 8a 没有降低对进程内代码的防护等级。
- 身份比较把以下对象视为不可再分：`str`、`int`、`float`、`bool`、`bytes`、`None`、`Enum` 成员、`datetime`、类对象。前几类本身不可变；修改类对象等于修改代码，与上一条同属一类。

**跳过的是什么，不跳过的是什么**
- 跳过的只是对同一个不可变对象、相同输入的重复纯计算：重新跑字段校验器，重算 `snapshot_digest`、限制项摘要等。这些校验器不读时钟（`eios/authz` 中没有 `now`、`monotonic`、`clock` 之类的调用），所以对没变的对象，结果只可能与上次相同。
- 不受影响的环节：
  - 每次判定仍从 PostgreSQL 读取事实（O3 的原有约束）；
  - `verify_repository_witness`；
  - 与时间相关的判定（`trusted_now`、到期时间，在 resolver 和 intersection 中计算，与模型校验无关）；
  - 所有 SQL 侧复核。
- 查询对象（`AuthorizationFactQuery`）从不登记，照常完整校验。

## 3. 测试

`tests/test_verified_fact_registry.py`（9 项）：

| 测试 | 断言 |
|---|---|
| `test_registered_instance_is_returned_unchanged_and_equals_full_validation` | 已登记的实例原样返回；关闭开关后完整校验得到的新实例与其相等，`model_dump_json` 与 `snapshot_digest` 逐字节一致 |
| `test_unregistered_construct_and_copy_are_fully_validated` | 登记之外解析的实例、`model_copy`、`model_construct` 都走完整校验 |
| `test_forged_construct_and_stale_copy_are_rejected` | `model_construct` 改字段，以及 `model_copy(update=…)` 留下过期摘要，都被拒绝 |
| `test_in_place_tampering_of_a_registered_instance_is_rejected` | 已登记实例被 `object.__setattr__` 改顶层字段、改嵌套的 grant，都被拒绝；整体替换 `__dict__`（内容不变）后不再命中，改走完整校验 |
| `test_tampered_container_inside_registered_instance_is_detected` | 已登记实例内部的字典被加键后，不再命中 |
| `test_registry_entries_die_with_their_instance` | 实例回收后登记被删除 |
| `test_switch_forces_full_validation` | 关闭开关后不登记，也不跳过 |
| `test_resource_id_memo_matches_uncached_and_never_caches_errors` | 8b 记忆化结果与未缓存一致；非法 id 每次都抛异常；`str` 子类绕过缓存，内部检查照常执行 |
| `test_pg_decisions_identical_with_and_without_shortcut` | 在真实 PostgreSQL 上，用两个身份、4 个目标（含允许、拒绝、不可用），在固定 `trusted_now` 下比较开关开与关的结果：判定字段、`expires_at`、证明所绑定的事实 `record_hash` 清单全部一致；开启时确有命中，关闭时没有 |

回归（本机，`-n 4`，**494 passed，81 s**，一次通过）：
- `tests/upstream`：EIOS 上游的应用限制、策略、动作治理用例；
- 新增的 `test_verified_fact_registry`；
- `test_authority_fact_cache`（O3 不得返回过期权限）、`test_authority_request_memo*`、`test_postgres_authority`、`test_runtime_authority`、`test_scope_denial_authority`、`test_verified_identity_pg`、`test_trusted_configuration_pg`、`test_action_definitions`；
- `test_vendor_imports` 与 `test_wheel_install`（PROVENANCE 散列）；
- v4[complete] 与两 Pi 两个功能用例。

## 4. 本机前后对比（Mac，按 `NX-049-analysis-round2.md` §8 的方法）

- 方法：`scripts/perf/nx049_profile.sh`，before 与 after 交替各跑 2 轮。before 是把上面三个文件临时换回 `4448c40` 的版本。v2 钩子，带 PG 函数探针。
- 本机负载在 3.4 到 5.6 之间上升（另一个工作区同时在跑回归），所以只看配对的相对变化。
- 8 次运行全部通过。

`authz:resolve_facts` 自身耗时（authorize，中位数，毫秒）：

| 用例 | before 1 / 2 | after 1 / 2 | 降幅 |
|---|---|---|---|
| v4[complete] | 231 / 258 | 160 / 168 | 31% / 35% |
| 两 Pi | 388 / 411 | 259 / 247 | 33% / 40% |

请求级的单独 / 重叠中位数（总耗时，括号内为 Python + SQL），以及 Host 端 authorize p50 / max：

| 用例 | 轮 | before：单独 / 重叠 | after：单独 / 重叠 | authorize p50 / max（before → after） |
|---|---|---|---|---|
| v4 | 1 | 458 (273+182) / 654 (360+309) | 405 (224+179) / 582 (285+274) | 632 / 948 → 525 / 860 |
| v4 | 2 | 518 (313+200) / 757 (403+349) | 451 (243+197) / 595 (308+297) | 725 / 1059 → 587 / 957 |
| 两 Pi | 1 | 544 (388+146) / 840 (571+263) | 456 (310+158) / 669 (416+246) | 821 / 1055 → 669 / 844 |
| 两 Pi | 2 | 580 (420+159) / 914 (602+295) | 470 (302+161) / 619 (393+227) | 909 / 1087 → 617 / 826 |

- 重叠请求的 Python 部分：v4 降了 21–24%，两 Pi 降了 27–35%，符合“Python 每少一份，重叠时按请求数成倍见效”的预期。
- 重叠请求中的 SQL 部分也有下降（v4 309/349 → 274/297）。这些 SQL 往返本身没有变，下降的是重叠时等待 GIL 的时间。
- effect submit 的最大值：v4 1006 / 1102 → 977 / 917；两 Pi 1304 / 1298 → 1010 / 964。

**对部署主机的预期**：按本机 `resolve_facts` 降幅 31–40% 推算，部署主机每个请求的 `resolve_facts` 少约 150–210 ms（v4）或 250–310 ms（两 Pi）。
- 重叠请求最慢一次：v4 从 1.69–2.19 s 降到约 1.3–1.9 s，两 Pi 从 2.02–2.43 s 降到约 1.4–1.9 s。
- 两个用例都回到 2 s 以内，但上沿余量不大。
- 实际以部署主机实测为准。

## 5. 剩下的热点与下一步

- 8c：`_fact_resolver.project` 的只读投影按冻结实例跨次复用。cProfile 中约占 resolve 的 27%，8a 不涉及这一块。
- 8d：`_canonical_value` 的快路径。8a 之后它主要剩在 O3 未命中与其他路径上，收益小。
- 之后按 `NX-049-analysis-round2.md` §7 的顺序做 5 与 4a（`authorization.py`，由 L1 负责）。
