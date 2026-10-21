# S6 收口：stage 实测脚本（NX-038 / NX-039）

脚本：`scripts/acceptance/s6_stage.py`。示例配置：`deploy/examples/s6-stage.example.json`。本地阳性对照：`tests/test_s6_stage_acceptance_pg.py`。依据 L5 收口清单 §2 的 “stage 实测” 列。

## 用法

```
uv run --frozen python scripts/acceptance/s6_stage.py <命令> --config <私有配置> [--dry-run] [--out <结果文件>]
```

- 命令：`at046` `r5` `at048` `r4` `at023` `r1` `r3` `r7-prepare` `r7-verify` `all`。`all` 按清单 §4.3 的顺序执行前七项。
- R7 分两步：先执行 `r7-prepare`；再由负责人用 `scripts/deploy/erase_synthetic_consumer.py erase` 删除配置里的合成顾客；最后执行 `r7-verify`。
- 配置是私有 JSON（0600）。连接串和凭据都放在私有文件里，配置里只写这些文件的路径；仓库里只有 example。
- **连接方式**：reader、writer、evaluator 三个连接分别以 `nexloop_evo_reader`、`nexloop_evo_writer`、`nexloop_evo_evaluator` 角色登录，各用自己的服务凭据，与服务本身的连接方式相同。inspector 是只读连接，只用来探测：受保护表的 digest、候选与留出集的状态、凭据与角色权限。registrar 可选，只用于 R5 的“登记非 test 凭据被拒”这个负例。
- **dry-run** 不调用任何写入口。它只做这些事：校验配置；确认每个连接的角色；用 reader 读取输入；在内存中规划每个候选；探测留出集的使用次数。本地测试核对了 dry-run 前后整库 digest 不变。
- **输出**：每个 AT 一条 JSON（`nexloop-s6-stage-result/1`），包含步骤、断言、digest、是否通过，以及一条可直接用作 planning evidence 的条目；`executed_by` 和 `judged_by` 留空。输出里不出现令牌、连接串或消息正文。
- **退出码**：0 表示全部通过（dry-run 时表示前提都满足）；1 表示某项失败或缺少前提；2 表示输入被拒。

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

- **AT-048 的缺口**：开评时只检查 writer 给出的分组，以及近重复 Run。如果 writer 按 Run 分组，把同一消费者的 Run 放在两侧，候选能正常开评，不会报 `leakage_detected`。本地测试如实反映：(a) 路线失败，(b) 路线和重分组都通过，AT-048 结论为未通过。要关闭这个缺口，开评时需要按用例的消费者再查一遍，这是服务端的修改，不在本任务范围内。
- **`dimension_closed`**：当前 main 上 semantic_content 和 ontology_schema 两个维度都已开放，而 propose 只接受这两个维度，所以服务无法让开评走到 `dimension_closed`（本地 v1 测试是靠 admin 删除开放维度来覆盖的）。R4 改为验证“未开放的维度在 propose 时被拒”。
- **L1 是合成替身**（D16）：只返回计数和标记，不跑 Run，也不调用模型。仓库里没有生产的 L1 runner。
- **L0 的测量输入**（延迟等）来自配置，是 D16 的合成测量。
- **inspector 需要的权限**：对演进表和 authz 凭据表的只读权限，以及 `authz.nexloop_service_identity_snapshot`、`control.nexloop_real_dispatch_closed_layer`、`runtime.nexloop_evo_holdout_digest` 的执行权。stage 上需要一个只读审计角色，由调度员安排；本地用 superuser 加只读事务代替。
