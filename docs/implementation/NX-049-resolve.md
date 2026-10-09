# NX-049 手段 8a + 8b + 8c：授权事实重复校验与重复投影的削减

分支 `nx049-resolve`，BASE `dispatch/integration-s3w`（`4448c40`）。背景见 `NX-049-analysis-round2.md` §6：
- O3 缓存交回的是同一个冻结实例，但 EIOS 核心的 `_load_fact` → `_exact_model` 随即对它做一次完整重校验（`revalidate_instances="always"`），并重算整份规范摘要；
- 在部署主机上，这部分是重叠请求在 GIL 下串行的主要 Python 开销。

## 1. 改动

| 文件 | 改动 |
|---|---|
| `packages/eios-core/src/eios/authz/_fact_resolver.py`（vendored） | 8a：新增 `verified_model_from_json`、`_reachable`、`_is_verified`、`_VERIFIED`；`_exact_model` 先查登记，命中时原样返回，否则走与上游相同的完整校验 |
| `packages/eios-core/src/eios/authz/_fact_resolver.py`（追加） | 8c：`_VIEWS`、`_cached_view`、`_reusable_views`；`project` 新增可选参数 `reuse`；解析结果的投影传入可复用的视图（§6） |
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

## 5. 部署主机实测（8a + 8b，调度员执行）

条件：
- 同一运行目录内，基线 `4448c40` 与 `2a76f14` 交替各 3 轮（`deploy-base-r{1,2,3}`、`deploy-8ab-r{1,2,3}`）。
- v2 钩子，带 PG 函数探针。
- 这次 `host-*.jsonl` 和 `pglog-*.log` 都已带回。

| 用例 | 指标 | 基线 r1 / r2 / r3 | 8a+8b r1 / r2 / r3 |
|---|---|---|---|
| v4 | `resolve_facts` 自身（authorize 中位数） | 532 / 523 / 534 | 361 / 346 / 360（−33%） |
| v4 | 单独请求 总（Python） | 1280 (745) / 1277 (680) / 1279 (677) | 1133 (606) / 1135 (605) / 1134 (607) |
| v4 | 重叠请求 总（Python + SQL） | 1943 (1088+855) / 1881 (1016+865) / 1972 (1158+814) | 1584 (697+900) / 1550 (694+901) / 1555 (689+913) |
| v4 | 墙钟 / 第一次超时 | 31 s / authorize 2001 ms ABORT_ERR | 44 s / effect submit 2001 ms ABORT_ERR |
| 两 Pi | `resolve_facts` 自身 | 768 / 769 / 773 | 567 / 555 / 561（−27%） |
| 两 Pi | 单独请求 总（Python） | 1383 (969) / 1379 (968) / 1388 (973) | 1161 (735) / 1173 (742) / 1176 (747) |
| 两 Pi | 重叠请求 总（Python + SQL） | 2220 (1485+720) / 2225 (1452+689) / 2266 (1485+666) | 1727 (1031+715) / 1720 (981+725) / 1765 (998+705) |
| 两 Pi | 墙钟 / 第一次超时 | 29 s / authorize 2002 ms | 44–55 s / authorize 2001 ms（这次已经跑到 effect submit 阶段） |

判读：
- **Python 部分的降幅与本机一致。** 重叠请求中的 Python 部分降了 32–36%。SQL 部分基本没变：v4 甚至略升，是因为测试跑得更远，进入了更重的阶段。
- **v4 现在卡在 effect submit 的服务端 SQL 上。** 以 r1 的超时请求为例（`runtime_effect_tool`，guard 2338 ms）：
  - 3 次 `runtime_activation_command` 的服务端时间合计约 856 ms，`resolve_facts` 334 ms；
  - 测量探针本身占 235 ms。这是测量开销，不带探针时这次请求约 2.1 s，依然超时；
  - 激活 SQL 内部有 675 次 `assert_read_authority`、5151 次事实加载、8496 次 `root_identity`。这是服务端 SQL 的工作量（O5a/O5b 之后剩下的部分），归 L1 / L4。
- **两 Pi 仍在 authorize 时超时。** 11 个请求的重叠组里最慢的约 2.37 s，在这种重叠深度下仍然超过 2 s。

## 6. 8c：已校验事实的只读视图跨次复用

**为什么不能直接按“冻结实例 → 视图”缓存**
- 每次 resolve 构造 `_ResolvedAuthorizationPayload` 时，嵌套事实的配置是 `revalidate_instances="always"`，pydantic 会把每个已登记的实例重新校验成一个**新的、内容相等的副本**。
- 投影的对象是 payload，也就是这些副本，所以以“已登记实例”为键的缓存永远命中不了。
- 把一个 `wrap` 校验器加在基类上也跳不过：pydantic 的 `after` 校验器会在它外层照常运行；只有在每个具体类里最后定义的 `wrap` 才是最外层。这样改需要动所有事实类，改动面太大，这次没做。

**做法**（`_fact_resolver.py`：`_cached_view`、`_reusable_views`、`project(root, reuse=)`）
- 已登记且未改动的原实例，只投影一次，视图按实例缓存在 `_VIEWS` 中（弱引用，随实例消亡）。
- 投影 payload 时，某个字段复用原实例的缓存视图，需要同时满足：
  1. 开关开启；
  2. 原实例仍通过 8a 的身份核对（`_is_verified`）；
  3. payload 中的副本与原实例类型相同且 `==`（字段、私有属性、extra 全部相等）。
- 每个原实例在一个 payload 中最多复用一次。如果同一个实例出现在两个字段（例如 caller 与 agent application 是同一个），第二个字段照常从副本投影，所以 payload 内视图之间的身份关系与上游一致。
- 其余部分（`query`、`revision_vector`、未登记或不相等的字段）照常投影。
- 视图本身不可变、不透明，在多个已签发的上下文之间共用是安全的。payload 摘要（`payload_digest`）与 `verify_integrity` 仍对 payload 本身计算，不受影响。

**信任边界与 8a 相同**：复用的前提是原实例来自唯一的登记入口且未被改动，副本与它相等，因此复用的视图与从副本投影出来的视图内容完全一致。

**测试**（`tests/test_verified_fact_registry.py` 新增 4 项，共 13 项）：

| 测试 | 断言 |
|---|---|
| `test_view_reuse_only_for_verified_equal_copies` | 只复用已校验且相等的副本；带复用的投影与不复用的投影逐项相等；开关关闭时不复用 |
| `test_view_reuse_refused_for_unregistered_unequal_or_tampered_originals` | 未登记、被篡改、内容不相等的原实例都不复用；同一原实例出现在两个字段时只复用一次，两个视图不是同一个对象 |
| `test_cached_view_dies_with_its_instance` | 实例回收后，缓存的视图随之删除 |
| `test_pg_resolved_views_identical_and_reused` | 在真实 PG 上，3 个目标（含拒绝），开关开与关时各公开字段的视图逐项相等（同一组查询对象），`verify_integrity` 通过；开启时两次 resolve 拿到同一个缓存视图，关闭时不是 |

回归（与上一轮同一组套件，`-n 4`）：**498 passed，74 s，一次通过**。

**本机前后对比**：before 是 `2a76f14`（8a+8b），after 加上 8c，交替各 2 轮，8 次全部通过，负载约 1.9–2.5。

| 用例 | 指标 | before 1 / 2 | after 1 / 2 |
|---|---|---|---|
| v4 | `resolve_facts` 自身 | 156 / 168 | 127 / 127（−19% / −24%） |
| v4 | 重叠请求 总（Python） | 534 (275) / 530 (281) | 477 (234) / 482 (234) |
| v4 | authorize p50 / max | 514 / 779，518 / 790 | 466 / 710，460 / 678 |
| 两 Pi | `resolve_facts` 自身 | 238 / 237 | 185 / 197（−22% / −17%） |
| 两 Pi | 重叠请求 总（Python） | 593 (371) / 593 (367) | 515 (309) / 510 (315) |
| 两 Pi | authorize p50 / max | 586 / 788，586 / 746 | 519 / 710，515 / 678 |

推算到部署主机：在 8a+8b 的基础上，`resolve_facts` 每个请求再少约 70 ms（v4）或 110 ms（两 Pi）。重叠请求中位数大约到 v4 1.4 s、两 Pi 1.5 s。最慢那次仍受上面提到的服务端激活 SQL 和重叠深度限制，需要叠加 L4 的手段 5 + 4a 后实测。

## 7. 剩下的热点与下一步

- 8e（候选）：payload 构造时对已登记嵌套事实的重复校验。8c 之后的 cProfile 显示它是 resolve 中最大的一块；要跳过，需要在每个事实类里把一个 `wrap` 校验器放在最外层（见 §6）。payload 摘要每次 resolve 也要算两遍（签发时一次，`verify_integrity` 一次）。两者都属于对上游的较大分叉，先等部署主机对 8a–8c 的实测。
- 服务端：effect submit 中激活 SQL 内部的 `root_identity` / 事实加载 / 读断言调用次数（§5），归 L1 / L4。
- 8d：`_canonical_value` 的快路径。8a 之后它主要剩在 O3 未命中与其他路径上，收益小。
- 手段 5 + 4a（往返削减）由 L4 实施，调度员将与 8a–8c 叠加后再到部署主机上验证。
