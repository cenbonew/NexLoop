# S6 收口：stage 实测脚本（NX-038 / NX-039）

脚本：`scripts/acceptance/s6_stage_evolution.py`（NX-038/039 部分；NX-040 部分是 L2 的 `scripts/acceptance/s6_stage_schema.py`）。两边共用 `scripts/acceptance/s6_common.py`：配置加载、结果格式、blocked 语义和脱敏。示例配置只有一份：`deploy/examples/s6-stage.example.json`。本地阳性对照：`tests/test_s6_stage_evolution_pg.py`。依据 L5 收口清单 §2 的 “stage 实测” 列。

## 用法

```
uv run --frozen python scripts/acceptance/s6_stage_evolution.py <检查> --config <私有配置> [--dry-run]
```

- **检查**：`at046` `r5` `at048` `r4` `at023` `r1` `r3` `r7-prepare` `r7-verify` `candidate-template` `all`。`all` 按清单 §4.3 的顺序执行前七项。
- **R7 分两步**：先执行 `r7-prepare`；再由负责人用 `scripts/deploy/erase_synthetic_consumer.py erase` 删除合成顾客；最后执行 `r7-verify`。
- **`candidate-template`**：把本诊断下一个打开的候选写进 `inputs.candidate_template`（权限 0600）。L2 的 NX-040 检查从这里开始。S6-R6 要用的已接受语义候选，就是 `at023` 的 L0 候选，它的 id 在 at023 的结果里。
- **配置**：用 `s6_common` 的 `nexloop-s6-stage/1`，即私有 JSON（0600），顶层是 executor、release、tenant_id、results_dir。本部分用到的字段：
  - `roles.writer` / `roles.evaluator` / `roles.reader`：各含 `{dsn_file, token_file}`；
  - `roles.evaluation`：`{token_file}`，只含 test 的评估凭据；
  - `roles.inspector`：`{dsn_file}`；
  - `roles.registrar`：`{dsn_file}`，可选，只用于 R5 的登记负例；
  - `inputs.evolution`：window、personas、label、parent_revision、evaluator_version、model_profile、created_at、l0_inputs、protected_refs、erasure_consumer_id；
  - `inputs.candidate_template`：模板文件路径。

  R7 的私有状态放在 `results_dir/state`。连接串和凭据都在私有文件里，配置只写路径；仓库里只有 example。
- **连接方式**：reader、writer、evaluator 三个连接分别以 `nexloop_evo_reader`、`nexloop_evo_writer`、`nexloop_evo_evaluator` 角色登录，各用自己的服务凭据，与服务本身的连接方式相同。inspector 连接只做只读探测（受保护表的 digest、候选与留出集的状态、凭据与角色权限）；stage 上用 bootstrap DSN，连上后设为只读，见下文。
- **dry-run** 不调用任何写入口。它只做这些事：校验配置；确认每个连接的角色；用 reader 读取输入；在内存中规划每个候选；探测留出集的使用次数。本地测试核对了 dry-run 前后整库 digest 不变，并且 dry-run 不写结果文件。
- **输出**：用 `s6_common` 的 `nexloop-s6-stage-result/1`。每个检查一行 JSON，同时以 0600 写进 results_dir，字段有 check、acceptance、release、executor、executed_at、dry_run、world、passed、blocked、steps。steps 的每一项是 `{name, expected, observed, passed, evidence}`。前提不满足时按 blocked 记录，`passed=null`，绝不算通过。输出经过 `Redactor` 脱敏：配置里的路径、租户、DSN 都替换成标记，不出现令牌和消息正文。
- **退出码**：0 表示通过（dry-run 时表示没有 blocked）；1 表示失败或 blocked；2 表示输入被拒。

## 每个 AT 做什么

| 命令 | AT | 步骤与断言 |
| --- | --- | --- |
| `at046` | AT-046 回归（S6 部分） | 评估凭据只属于 world test 且已登记；该凭据在 real 认证被拒。writer 和 evaluator 的凭据是 real 的服务凭据。两个角色能执行的 security definer 函数只有各自的演进入口：writer 是 `authz.nexloop_evo_*` 加 NX-040 的 `authz.nexloop_schema_proposal_*`，evaluator 是 `authz.nexloop_evo_*`，另外都有公共的 `runtime.nexloop_erasure_permits`；没有任何效应入口。world test 的真实派发：部署开关关闭时关闭，强制打开时仍由 world 层关闭。 |
| `r5` | S6-R5 | 在 evaluator 端口上用 writer 凭据开评，被拒；writer 角色直接调用开评函数，报 42501；evaluator 开评成功（阳性对照）。用非 test 凭据开 L1，报 `test-only evaluation credential`；用已登记的 test 凭据开 L1 成功（阳性对照）。配置了 registrar 时，再验证登记 writer 的 real 凭据为评估凭据会被拒（`world test only`）。 |
| `at048` | AT-048 | 两条泄漏路线：(a) 按 Run 分组，让同一消费者的 Run 落在两侧；(b) 两个不同消费者、问了同一组词的近重复 Run 分在两侧。期望开评返回 `leakage_detected`。按消费者分组的候选，以及把近重复并入同一簇的候选，开评通过。 |
| `r4` | S6-R4 | 同一留出集上的 6 个候选：前 5 个开评成功，使用序号 1–5；第 6 个返回 `holdout_exhausted`，候选仍为 proposed。用未开放的维度（`semantic_tool`）提出候选，在 propose 时被拒。 |
| `at023` | AT-023 | 一次 L0（accepted_for_release）；一次 L1：在 test world 开会话、取定义快照，用 D16 的合成替身运行，结论 accepted，关闭会话并清掉快照；一个被拒的候选（未测量延迟导致 G7 失败，状态 rejected）。前后对比受保护表 digest，不变。 |
| `r1` | S6-R1 | 同一冻结输入跑两次 L0：两份报告的 digest 相同，每一次都通过自身的复现检查（D39-2）；第二次提交结论时按重放处理，库里保存的 digest 与第一次相同。 |
| `r3` | S6-R3 | 先在内存中找到这样一个范围内的定义：给它加 alias，会把配置的受保护定义挤出浏览上限。用它构造 G5 失败的候选，再构造一个同 target、不带 term 的对照候选。G5 候选评分最高，但 G5 失败，结论被拒（`hard_gate_failed`，状态 rejected）。 |
| `r7-prepare` / `r7-verify` | S6-R7 | prepare：建一个用例包含该合成顾客的候选，开评并开 L1 快照，状态写进私有状态文件。删除顾客后执行 verify：留出集被作废（missing_data），候选变为 incomplete/missing_data，L1 会话以 candidate_ended 关闭、快照清空，该顾客的 Run 离开 case_refs。 |

## stage 前提（dry-run 会逐项报告）

- 负责人已登记 writer、evaluator 两个服务主体和一张只含 test 的评估凭据，并设置了 evolution 预算。这些都属于受信配置，脚本自己不登记。负责人此前定了“stage 暂不注册演进服务主体”，调度员会另行请示。
- 时间窗口里要有合成顾客（persona 由配置指定，例如 simulated_agent）的失败 Run，并且带检索未命中的语义读取记录，足以产生一个 semantic_content 草稿；还要至少 4 个“簇单元”，才能给 7 个 AT 各分一个留出集（每个留出集最多判定 5 个候选，D39-3）。
- AT-048 的 (b) 路线要求窗口里有一对近重复 Run；R3 要求某次读取的结果超过 10 条，并包含配置的受保护定义。
- 用新的 `label` 重跑时，要换一批新的合成数据：旧的留出集已经用过。同一个 `label` 重跑则全部按重放处理。

## 本地发现与限制

- **AT-048 的缺口（已修复，迁移 `0286_at048_consumer_leakage.sql`）**：原来开评时只检查 writer 给出的分组和近重复 Run，writer 按 Run 分组就能把同一消费者的 Run 放在两侧而不被发现。现在开评由服务端推导每个用例 Run 的消费者（`runtime.nexloop_evo_run_consumer`，与 trace reader 0233 的推导相同：消息路由、fallback 绑定或角色绑定）。同一消费者的 Run 同时落在两侧时，一律返回 `leakage_detected`；推导不出消费者（null）的 Run 不算同一消费者。`s6_stage.py` 的 AT-048 现在四条断言都要求通过：(a) 按 Run 分组的泄漏、(b) 近重复泄漏、按消费者分组、重分组。测试见 `tests/test_at048_consumer_leakage_pg.py`。
- **`dimension_closed`（调度员 2026-10-21 认可）**：当前 main 上 semantic_content 和 ontology_schema 两个维度都已开放，而 propose 只接受这两个维度，所以开评的拒绝码 `dimension_closed` 在当前 main 上**无法触发**（本地 v1 测试是靠 admin 删除开放维度来覆盖的）。R4 改为验证“未开放的维度（`semantic_tool`）在 propose 阶段就被拒”。
- **L1 是合成替身**（D16）：只返回计数和标记，不跑 Run，也不调用模型。仓库里没有生产的 L1 runner。
- **L0 的测量输入**（延迟等）来自配置，是 D16 的合成测量。
- **inspector 连接**（调度员 2026-10-21 定）：不新增审计角色。stage 上 inspector 用 bootstrap DSN，由调度员放在私有配置里；脚本连上后先执行 `set default_transaction_read_only=on`，并在 dry-run 和每条命令里核对这个设置确实生效（“inspector is read only”）。inspector 只做只读探测，不写入任何东西。本地测试同样用 superuser 加只读事务。

## 集成说明（0286）

- 临时编号用 0286，没有用调度员给的 0295。原因是 main（927c1ba6）的迁移止于 0258，而 bootstrap 要求编号连续。集成时由调度员重排。
- `authz.nexloop_evo_evaluation_open` 被重定义，与 0241 相比只多了检查 3 中的一个条件。main 上还没有复制件守护（`tests/test_function_copy_guard.py` 随 NX-041 一批进入），所以集成到含守护的分支时，要登记下面这一条：

```python
Allowed('at048_consumer_leakage', 'authz.nexloop_evo_evaluation_open',
        'AT-048: check 3 also refuses one Consumer on both sides (runtime.nexloop_evo_consumer_leak); nothing else',
        ((r"\s*-- AT-048 \(S6 stage finding\)[^\n]*\n\s*or runtime\.nexloop_evo_consumer_leak\(v_tenant,x\.design_case_ids,x\.validation_case_ids\)", ''),)),
```

## AT-048 修复的性能与已开评候选（L6 `at048-fix-review.md`）

- **性能（审查 4）**：
  - 原来的写法是在每一对 case 上各调用两次推导函数，成本随 case 数平方增长；而且推导函数把 `run_id` 转成 text 再比较，用不上索引。
  - 现在由 `runtime.nexloop_evo_consumer_leak` 对每个 case 只推导一次 Consumer，再用一次 hash join 求两侧的交集，复杂度是线性的。
  - `runtime.nexloop_evo_run_consumer` 改为逐步查找，每一步都是一次索引查找：先用 uuid 查 Run 的 `run_id`（0047、0110、0063 的唯一索引），再按 message_id 找消息（0134 的索引），最后由会话得到 Consumer。这样不依赖统计信息：本地测试发现，刚写入、没有统计信息的数据会让一条 join 写法退化为全表扫描。
  - 不需要新增索引。
  - **规模测试**：1000 个 case（超过一个候选最多能容纳的 512 个），有泄漏和无泄漏两种情况都在 1 秒内完成；EXPLAIN 显示走的是索引扫描，没有顺序扫描。
- **持锁时间**：这项检查仍然在开评函数的 `for update` 事务里执行，因为它要读取候选行。但现在最多只有 512 次索引查找，持锁时间是毫秒级，所以没有把它挪到加锁之前。
- **审查 1.1：修复之前已经开评的候选不会被重新检查**。开评函数的重放路径会直接返回已经 evaluating 的候选。部署 0259 之后，由 owner 执行一次只读检查；如果有命中，就用 `candidate_stop` 终止对应的候选：

```sql
select c.candidate_id from runtime.nexloop_evo_candidates c where c.world='real' and c.status in ('evaluating','accepted_for_release')
  and runtime.nexloop_evo_consumer_leak(c.tenant_id,c.design_case_ids,c.validation_case_ids);
```

- 第八批预演（c5b756b6）里这个迁移的编号是 0286。本分支修改了迁移的内容，合并时要以本分支为准，并更新复制件守护里的登记（见上面的 Allowed 条目）。

## 输入辅助工具与执行顺序（stage）

### `s6_stage_inputs.py`：查找 inputs 的取值（只读）

```
uv run --frozen python scripts/acceptance/s6_stage_inputs.py --config <私有配置> [--days 14] [--personas simulated_agent,synthetic]
```

- 只用 `roles.inspector` 连接（stage 上是 bootstrap DSN），经 `s6_common.connect_read_only`：连上后先设为只读，并确认设置已生效，然后才执行查询（NX-043 的 stage 脚本共用这个函数）。不调用任何函数入口写数据。本地测试会核对运行前后整库 digest 没有变化。
- 输出一份 JSON（`nexloop-s6-stage-inputs/1`），包含三部分：
  - `inputs`：可以直接并入 `nexloop-s6-stage/1` 的 inputs 草稿。查不到的项保留配置里原来的值；`candidate_template` 的路径不回显。
  - `sources`：逐项给出 `{status, value, source, candidates, reason, remedy}`。
  - `blocked`：查不到的项。

  输出里 id 照常给出，不含令牌、DSN、租户、路径或个人内容。
- 退出码：0 表示全部找到；1 表示有 blocked；2 表示配置被拒。

| 项 | 来源 | blocked 时怎么补 |
| --- | --- | --- |
| `evolution.window` | 回看期内（默认 14 天），合成 Consumer 的 Run 在 `runtime.nexloop_semantic_accesses` 里的读取记录。persona 来自 `control.nexloop_consumer_personas`；Consumer 由 `runtime.nexloop_evo_run_consumer` 按 Run 推导。窗口两侧各留 1 小时余量；`evidence` 给出 Run 数、有未命中的 Run 数、Consumer 数，以及占满浏览上限的读取数。 | 回看期内没有带未命中的合成 Run，或者这类 Consumer 少于 4 个（留出集至少要 4 个簇单元）：先跑一段合成交互，再查一次。 |
| `evolution.parent_revision` | `ontology.nexloop_semantic_release_pointers`，即当前已发布的语义版本。 | 还没有发布过语义版本：由调度员定一个基线标签（例如 `sem-0`）并记录下来，或者先由负责人发布一个版本。 |
| `evolution.protected_refs` | 窗口内读取结果里的联系限制或拒绝定义（按字样识别，取到的值请人工确认）。按 ref 中的字样识别，例如 contact、refus、opt-out、consent、limit；优先选出现在占满浏览上限（10 条）的读取中的那个。 | 读取结果里没有这类定义，或者它没有出现在占满上限的读取中：让合成交互的浏览范围更宽，再查一次。`candidates` 会列出 ontology 里同类定义作为参考。 |
| `evolution.erasure_consumer_id` | 窗口内未命中最多的那个合成 Consumer。**只从 simulated_agent / synthetic 中选**：即使 `--personas` 含 real，real Consumer 也不会被选中，也不出现在 candidates 里（R7 的擦除不可逆）。擦除工具 `erase_synthetic_consumer.py` 本身是第二道防线：precheck 显示 real 时不发送，SQL 也以 `persona_real` 拒绝（`tests/test_erase_synthetic_consumer_pg.py`）。 | 同 window。 |
| `candidate_template` | 只检查配置的文件是否存在，不读取内容。 | 先配置一个 0600 的路径，再执行 `s6_stage_evolution.py candidate-template`。 |
| `semantic` | `runtime.nexloop_evo_candidates` 中 accepted_for_release、semantic_content、尚未发布的候选；items 的 ref 取自它的变更范围，alias 文本是占位值。 | 先执行 `at023`，再查一次。 |
| `schema_type` / `new_property` / `new_type` | `ontology.object_type_versions` 中各类型的最新版本：有 Consumer 就选 Consumer，并核对新属性名和新类型名都还不存在。 | 租户还没有 Object Type：先初始化租户的本体。 |
| `escalation_action` | `control.nexloop_action_definitions` 中 active 的 Action；有 `eios:action:Grant.issue:1` 时优先用它。 | 先发布租户的 Action（`stage_secrets publish`）。 |

### 执行顺序

前一步的产物会被后一步用到。顺序不能打乱的原因写在各步里。

1. **`s6_stage_inputs.py`**：把查到的值填进私有配置。`semantic` 这时通常是 blocked，正常。
2. **`s6_stage_evolution.py all --dry-run`**：核对配置、连接角色和留出集规划。没有 blocked 之后再进入第 3 步。
3. **`at046`、`r5`**：只依赖角色和凭据的登记。
4. **`at048`、`r4`**：各自使用自己的留出集。r4 会故意用完它那份。
5. **`at023`**：产出一个 L0 accepted_for_release 语义候选，它的 id 在 at023 的结果里。之后用 `--only semantic` 再执行一次第 1 步：只取 `semantic`（这个 id 供第 9 步的 S6-R6 使用），`evolution` 的 window、protected_refs、erasure_consumer_id、parent_revision 都保持第一次的值，因为第 4、6 步的留出集和窗口依赖它们。
6. **`r1`、`r3`**：r3 依赖 `protected_refs`。
7. **`candidate-template`**：写入 `inputs.candidate_template`，供第 8 步的 NX-040 检查使用。
8. **`s6_stage_schema.py at024`、`r10`、`r8`**：都从 candidate_template 开始。r8 的 `--publish` 是负责人的 B 类步骤。
9. **`s6_stage_schema.py r6`**：依赖第 5 步产出的 `semantic.candidate_id`，会发布一个语义版本再回滚。发布会移动语义版本指针，所以 r6 要放在用到 `parent_revision` 的演进检查之后；配置里的 `parent_revision` 保持第 1 步取到的值，不要改。
10. **`r7-prepare`**，然后负责人执行 `erase_synthetic_consumer.py erase`，再执行 **`r7-verify`**：必须放在最后。删除会让这个 Consumer 的 Run 离开窗口，前面各步的诊断输入会随之改变。

重跑时：同一个 `label` 一律按重放处理；换新 `label` 就要换一批合成数据，因为旧的留出集已经用过。

## 合成交互：合成顾客发言（`synthetic_customer.py`）

合成交互数据执行单（分支 `l5/s6-stage-data`，ec9bf9a0，尚未进 main）第 2 步“执行”中的顾客发言改用本工具；合入后以本节为准。调度员在自己的 Mac 上发言；观察方仍是 APP_HOST 上的 run 入口。

- **发言**：`scripts/acceptance/synthetic_customer.py` 代替操作员在合成 WebChat 中逐轮发言，走与真人相同的顾客路径（`nexloop_eios.consumer_channel`：登录、会话、带 CSRF 与 Idempotency-Key 的消息、登出），不经任何后门。
  - 私有配置（0600）：`{"schema": "nexloop-synthetic-customer/1", "origin": "https://<APP_HOST_NAME>:<PORT>", "ca_file": "<GATEWAY_CA_FILE>", "reader_dsn_file": "<只读 DSN 文件>", "accounts": {"customer-1": "<用户名>", …}}`。
  - 私有计划（0600）：`{"schema": "nexloop-synthetic-customer-plan/1", "plan_id": "s6-<日期>-1", "turns": [{"account": "customer-1", "body": "<顾客发言>", "wait_reply_seconds": 120}, …]}`（1–200 轮），按执行单第 1 步的 15 轮写。
  - 口令只经 stdin，每个账号一行 `别名<TAB>口令`，由钥匙串管道送入；不进 argv、输出或日志。必须运行脚本文件，不要用 `python -`（stdin 是口令通道）：

```
( for a in customer-1 customer-2 customer-3 customer-5; do printf '%s\t%s\n' "$a" "$(security find-generic-password -s nexloop/<stage>/$a -w)"; done ) \
  | uv run --frozen python scripts/acceptance/synthetic_customer.py --config <CUSTOMERS.json> --plan <PLAN.json>
```

- **预检（任何发言之前）**：每个账号登录一次，并经只读 DSN（`s6_common.connect_read_only`：连接后设只读并核对）查它的 Consumer 的 persona（`control.nexloop_consumer_owners` → `control.nexloop_consumer_personas`）。任何一个不是 simulated_agent / synthetic，整次运行拒绝，什么都不发；已擦除或没有 Consumer 的（例如 customer-4）报 `conversation_unavailable` 并跳过，本工具不会替它新建。
- **每轮**：登录（429 按 Retry-After 退避，最多 5 次）→ 该账号最近的会话，没有就新建（403 → `conversation_unavailable`，此后跳过该账号）→ 发言（Idempotency-Key = `<plan_id>-<轮次>-<别名>`，同一计划重跑只重放，不重复发言）→ 可选等待回复（轮询；超时报 `timeout`，不重发）→ 登出。
- **输出**：每轮一行 JSON（轮次、账号别名、状态码、是否收到回复、耗时）和一行汇总；不含正文、标识、用户名、cookie、CSRF 或口令。退出码 0 = 全部已发或重放，1 = 有未发出的轮次，2 = 输入或预检被拒。
- **与 `run_interactions.py --driver real` 配合**：run 入口照常在 APP_HOST 上以负责人 staff 的操作员身份运行，先 `--preflight`，退出码 0 后正式运行；它自己不发送，每轮由本工具发言，它通过 metrics 角色观察 Run 和效应，两者按同一份轮次顺序推进（每轮超时 900 秒会暂停）。前 3 轮校准后 run 入口暂停等负责人确认单次成本，所以发言计划分两份：前 3 轮一份，其余一份（不同 `plan_id`）。
- **本地测试**：`tests/test_synthetic_customer.py`（无网络、无数据库）：429 退避、real persona 整次拒绝且不发、已擦除与 403 跳过、重跑只重放、等待超时不重发、口令格式错误（含把脚本当作 stdin 送入）被拒且不回显、口令/用户名/正文/标识不出现在输出中。
