# 草稿：方案 B M1 测试清单（实施前准备）

- **状态**：L7 草稿，2026-11-11。负责人 11-11 已拍板：B1 批准（context-pack v8）、H1 选 (a)、H2 摘要后 drop。
- **前提**：按合并实施顺序，M1 要等 NX-068 合入后才开工。本清单只列测试，不含实现。
- **依据**：设计稿 `docs/planning-drafts/b-effect-action-follows-schema-version-20261111.md`（下称“设计稿”）§2、§3、§4、§7。
- **测试原则**：
  - 测试名是草拟的；
  - 每条拒绝测试都配一个变异，删去对应条件后，该测试必须失败；
  - 不使用 xfail 或 skip；
  - 迁移号用临时号，测试读取 catalog 的末尾版本，不写死迁移号。

## 1. M1 范围（设计稿 §4 第 2a 行）

1. 租户级准入表 `control.nexloop_effect_action_admissions`（B2）。
2. 全局允许名单的兼容视图。
3. `nexloop_effect_contexts.action_resource`：已有行回填 `:1`。
4. 选择函数 `authz.nexloop_effect_action_version`。
5. 准入函数 `control.nexloop_admit_effect_action_successor` 和退役函数：先调用 NX-068 的 `effect_successor_admissible`，判定通过才写准入行。
6. intent 同时接受“`ctx.action_resource` 精确匹配”和旧条件；A′ 保留。
7. H1 (a)：发布事务内由 gate wrapper 写准入行。按设计稿 §4，这一项放在 2b（M2）与 G1 的切换一起做，M1 只提供准入函数。

## 2. 新文件 `tests/test_effect_action_admissions_pg.py`

### 2.1 表与回填
| 测试 | 断言 | 变异 |
|---|---|---|
| `test_existing_contexts_backfill_to_version_1` | 迁移前造 2 个 context，迁移后 `action_resource = …service.request:1` / `…enterprise.deliver:1`，列 not null | 回填写成 null 或 `:2` |
| `test_seed_admissions_are_version_1_per_tenant` | 每个已有租户、每个效应 stable_name 有一行 `:1`，`admitted_by='migration'` | 漏 seed 一个租户 |
| `test_no_runtime_role_writes_admissions` | api、domain_worker、scheduler、action_worker、identity、metrics 的 insert/update/delete 全部拒绝；RLS force 打开 | grant insert 给 nexloop_api |
| `test_one_admission_per_consumer_version` | 同一 (tenant, world, stable_name, consumer_version) 的第二行 ⇒ unique violation | 去掉 unique |
| `test_admissions_are_append_only` | update 或 delete 被拒绝；退役只追加一行 inactive 记录（或置位且不可逆，以实现为准，测试断言不可回到 active） | 允许 delete |

### 2.2 选择函数 `nexloop_effect_action_version`
| 测试 | 断言 | 变异 |
|---|---|---|
| `test_v1_consumer_selects_version_1` | Consumer v1 ⇒ 1 | — |
| `test_v2_consumer_selects_the_admitted_successor` | 真实 F3 发布 v2 并准入 `:2` ⇒ 2 | 比对 Consumer 版本的条件被删 |
| `test_unadmitted_successor_is_not_selected` | F3 已发布 `:2`、未准入 ⇒ 报 `effect Action version unavailable` | 不查准入表，只查定义 |
| `test_inactive_definition_or_retired_admission_is_not_selected` | `:2` 定义 inactive，或准入已退役 ⇒ 拒绝（A′ 期间回到 `:1` 由 A′ 证明，见 §2.4） | 去掉 active 条件 |
| `test_two_candidates_refuse_never_highest` | 绕过约束造两个覆盖同一版本的候选 ⇒ 拒绝 | 改成 `order by version desc limit 1` |
| `test_other_tenant_or_world_admission_is_ignored` | 租户 B 准入 `:2` 不影响租户 A；sim world 的准入不影响 real | 去掉 tenant 或 world 条件 |

### 2.3 准入函数（调用 `effect_successor_admissible`）
- 逐条复用 A′ 第 7 组的 11 个场景（设计稿 §7.2 第 5 条），断言：拒绝，准入表行数不变，审计没有新行。
- 另加 4 条：

| 测试 | 断言 | 变异 |
|---|---|---|
| `test_admission_calls_the_shared_judgement` | 准入函数的 `pg_get_functiondef` 引用 `effect_successor_admissible`，且不含另一份等价判定（检查 `authz.nexloop_effect_definition_unversioned` 等关键片段只出现在判定函数中） | 在准入函数里内联一份判定 |
| `test_admission_is_configurator_only` | 非 configurator 调用 ⇒ 42501 | 去掉 session_user 检查 |
| `test_admission_records_a_revision` | 成功后写一条修订，含 who、publication_id、consumer_version、检查摘要 | 不写修订 |
| `test_admission_is_idempotent` | 同一准入执行两次 ⇒ 第二次 no-op，返回 unchanged | 第二次报 unique 错误 |

### 2.4 bind 与 intent（A′ 保留期间）
| 测试 | 断言 | 变异 |
|---|---|---|
| `test_bind_stores_the_selected_version` | v2 顾客且已准入 ⇒ `ctx.action_resource = …:2`；executor proof 和 run proof 按 `:2` 签名 | proof 仍签 `:1` |
| `test_bind_recomputes_and_refuses_a_different_version` | Python 传入的 E 与 SQL 重算不等（例如传 `:1` 而 Consumer 是 v2 且已准入）⇒ 42501，不写 context | 去掉重算 |
| `test_concurrent_publication_between_select_and_bind` | 第 4 步之后发布 v3 ⇒ bind 拒绝，没有部分 context（Q3） | 不在同一事务里 `for share` |
| `test_intent_takes_only_the_context_version` | context 是 `:2`、intent 用 `:1` ⇒ `effect authority unavailable` | intent 退回允许名单匹配 |
| `test_old_contexts_still_run_at_1_through_a_prime` | M1 之前绑定的 `:1` context（v2 顾客）在 M1 后仍能通过，并写 A′ proof 行 | 删除 A′ 分支（此时该测试应失败，证明 M1 没有提前删 A′） |
| `test_admitted_contexts_write_no_proof_row` | `:2` context 的 intent 成功，`nexloop_effect_scope_proofs` 不增加 | 无论是否精确都调用 proof |
| `test_configure_selects_by_the_controls_consumer` | configure 时的 executor proof 版本按 EffectControl 指向的 Consumer 选取 | configure 写死 `:1` |

### 2.5 端到端（复用 `test_consumer_v2_stage_path_pg` 的装配）
1. v1 顾客：relay 全链回复成功，`ctx.action_resource` 为 `:1`，没有 proof 行。
2. v2 顾客：已准入且 register 派生了效应 Action `:2` 授权 ⇒ 回复成功，`:2`，没有 proof 行。
3. v2 顾客，未准入 ⇒ 仍由 A′ 兜住，回复成功，`:1`，有 proof 行。这是 M1–M3 期间的预期，M4 之后这条翻转。
4. 企业交付与以上 1、2 对称各一条。

## 3. 需要改的现有测试和守卫
- `test_effect_scope_successor_proof_pg.py::test_the_transitional_rule_goes_with_option_b`：M1 把允许名单改成准入表之后会变红。
  - 按 L6 的顺序，翻转在 M4。
  - M1 先把它的识别条件改为“A′ 函数仍存在，且准入表存在”，断言 A′ 仍在；M4 再删除这个测试，换成 `test_option_b_left_no_successor_proof`。
- `test_stage_follow_registrar.py`：FOLLOWABLE 集合相等改为 5 条，并新增 `not_admitted` 拒绝场景。B5 的这一项属于 M2，M1 不改 FOLLOWABLE。
- 新迁移必跑的守卫：db_boundary、tenant_fk、system_catalog、function copy guard（0179/0183/0446 函数体的复制登记）、erasure coverage（准入表无个人信息，登记为 tenant 级配置）、nx041 sim、real_effect_tables、`check_definer_search_path`（需 `LC_ALL=C.UTF-8`）、bootstrap/catalog-head。
- `test_wheel_install`：准入脚本 `effect_action_admit.py` 放在 `scripts/deploy/`，nexloop_eios 包内不得导入。

## 4. 已提交的契约草案（M2 使用）
- `packages/contracts/context-pack-v8.schema.json` 与 `examples/context-pack-v8.valid.json`（7627f8ad），生成物已更新。
- 测试 `tests/test_context_pack_v8_contract.py`：
  - 例子能通过校验；
  - 其他 Action、版本 0、前导零、非数字、超过 4 位都被拒绝；
  - 企业交付字段只接受 enterprise；
  - v8 相对 v7 只改了 5 个效应 Action 字段，加上 schema_version、$id、title、description。
- **未登记到 `published.json`**：登记后 sha 冻结，所以在 M2 实现合入时再登记，与 v7 当初的做法一致。
