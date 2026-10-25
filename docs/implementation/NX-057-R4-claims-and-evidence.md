# NX-057 R4：认领终态、统计联动与证据抽屉

- **来源**：设计稿 `docs/design/remediation-intents.md` 第 4 版 §3.4、§8、§10、§11、§13 的 R4；负责人 Q-4 选 A（不标 compensated）。调度员 2026-10-25 派工：R3 按 Q-2 暂缓，先做 R4 和其余不依赖 R3 的部分。
- **分支**：`l7/nx057-r4`，基点 `ecc926f9`（第二十六、二十七批的组合）。这个基点还**不含** `bb9f8871`，即规划器节拍可注入的修复，它由 L4 另行补合。两边都改了 `tests/test_remediation_pg.py`，但不在同一处：`bb9f8871` 改“每分钟一轮”和 5 秒两条测试，R4 改 `failed()` 辅助函数并在文件末尾追加测试。
- **迁移**：`remediation_claims`，临时编号 0345，接在基点尾部 0343 之后。

## 做了什么

### 1. 最终失败时终结动作认领（§10，Q-4 A）

- **做法**：在 `runtime.nexloop_effect_failures` 上加 AFTER INSERT 触发器，在失败写入的同一事务里，把该效应意图尚未终结的动作认领终结为 `permanent_failure`。认领的键是 Action 名加意图 id。
  - 写入的终态：`outcome_id` 为失败记录的 id，`outcome_revision` 为 1，`outcome_digest` 是对（意图、失败、原因码）求的 sha256，`finalized_at` 取失败记录的时间。
  - 认领的 `claim_revision` 和 `fencing_token` 保持不变，与 0007 终结时的做法相同。
  - 用触发器，三条失败路径（0152、0174、0210）的函数都不用重定义，复制件守卫也不需要新登记。
- **不处理的情况**：从未派发的意图没有认领，什么也不做；已经终结的认领保持原样。
- **补救成功后**：认领仍然是 `permanent_failure`，不改为 `compensated`（Q-4 A），失败仍然是失败。
- **口径不变**（§3.4）：认领的 `permanent_failure` 不是补救的生成条件，也不计入失败率。补救和失败率都只看 `effect_failures`，R4 不改这一点。
- **出站表声明**：触发器函数属于效应路径，并且直接写 `runtime.nexloop_action_claims`，NX-041 的出站表检查因此要求声明这张表。已在 `control.nexloop_real_effect_tables` 中登记为 not guarded，理由是：这是受治理 Action 的认领记录，没有任何东西经它离开系统；模拟世界同样有认领。

### 2. `outcomes.remediation`（§8）

快照的 `outcomes` 块新增 `remediation` 子块，只有计数：

- `changes`：最近 1 小时的补救状态变化，按目标状态计数。
- `unremediated_defects`：最近 1 小时内属于 defect 的失败（`origin_expired`、`confirmed_not_delivered`）中，没有补救的数量。
- `unremediated_by_decision`：同一批失败按规划器的扫描结果分类，取值有 `not_considered`（还没有轮到）、`no_rule`、`switch_closed`、`rate_capped`、`rule_held` 等。
- 这一项不再恒为 0。失败率本身不受补救影响。

### 3. 证据抽屉（§10、§11）

- 工作台读取新增动词 `remediation_evidence`（`nexloop.workbench.read:1`，所有成员可读），用改名再包一层的方式加入。HTTP 接口为 `GET /api/v1/workbench/remediations/{remediation_id}`。
- 返回一条补救的证据链，只有 id、码和时间：
  - 源意图（Action 与版本、状态）；
  - 失败记录及其证据 id（determination、not_found observation、origin Run）；
  - 动作认领的终态；
  - 规则版本与摘要；
  - 被唤醒的计划及消费它的 Run；
  - origin 补救；
  - 每一次状态变化，不含负责人填写的原因文本。
- **可见性**：
  - Consumer id 只在成员角色对该 Consumer 有 READ 时给出（ADR-025）；
  - 正在删除的 Consumer 返回 404，与补救不存在时相同；
  - 非法 id 返回 422。
- **页面**：“补救”一栏每行多一个“证据 / 查看”按钮，展开后显示证据链与状态变化。

## 测试

| 命令 | 结果 |
|---|---|
| `pytest -n 3 tests/test_remediation_pg.py tests/test_function_copy_guard.py tests/test_published_migrations.py tests/test_nx041_stand_ins_pg.py::test_every_outbound_table_is_declared`（负载门控后） | 80 passed，其中 `test_remediation_pg.py` 21 个，R4 新增 3 个：<br>- 失败时认领终结为 `permanent_failure`（outcome 即失败记录，revision 与 fence 不变）；自动 reevaluate 唤醒成功后仍是 `permanent_failure`；已终结的认领不变；没有认领的意图照常失败；<br>- `outcomes.remediation`：本小时 `changes={'proposed': 1}`，`unremediated_defects=2`，按扫描结果为 `no_rule` 1 个、`not_considered` 1 个；被拒的一次性回复不算；失败率中 3 个 defect 都计入，与补救无关；<br>- 证据抽屉：operator 能读，链上各环节正确，Consumer 不可读时为 null，事件中没有原因文本；不存在返回 404，非法 id 返回 422，客户身份返回 403 |
| `pytest -n 3`：R0 至 R2、受影响的现有测试和全库结构性测试，共 18 个文件（另含 `test_at046_stage_pg.py` 与 `test_effect_dispatch.py`） | 205 passed |
| 真实链路，串行：`test_one_reply_per_message_pg.py`、`test_reply_reconcile_pg.py`、`test_reply_fallback_pg.py`（三条失败路径之一会触发新的触发器） | 18 passed（282 s） |
| `vitest run apps/web/test`（`remediation.test.ts` 新增证据链解析）；`tsc --noEmit` | 33 个文件、186 passed；无错误 |
| `check_definer_search_path.py`；`check_publication.py` | 0 problems；0 violations |

Agent Host 的 dist 跑测试前重新构建过（工作树里原来的是旧版本）。

## L6 复核 288da38f 之后（2026-10-25）

- **R-1（重要）**：新增 `tests/test_remediation_claim_race_pg.py`，用双连接在真实 PG 上覆盖“完成”与“失败写入”同时发生的两种顺序。
  - **屏障**：第三个会话先持有认领命令的 advisory lock（`nexloop-action:` 键，与 0007 相同）。两侧依次启动，每启动一侧，都要等 `pg_locks` 显示它已在这把锁上排队，才启动下一侧；然后释放锁，两侧按队列顺序执行。没有用 sleep 定先后，轮询只是读 `pg_locks` 的节奏。
  - **完成先提交**：真实的 finalize（`PostgresActionClaimPort` 经 `authz.nexloop_action_claim_command`）先得到锁并提交，触发器随后重查已提交的行，看到 terminal 就跳过。认领保持 succeeded，失败行照常写入。
  - **失败先提交**：认领被终结为 `permanent_failure`。之后的 finalize 报 `action_claim_outcome_conflict`，retryable 报 `action_claim_transition_invalid`，两个分支都覆盖到了。
- **S-1**：触发器在更新认领之前，先取同一把 `nexloop-action:` advisory lock，加锁顺序与 0007 一致（先这把锁，再认领行）。失败路径在此之前已经锁住意图行，与并发的认领命令之间仍可能出现 40P01，这种情况会被重新抛出，失败事务可以重跑。迁移注释里写明了这一点。上面的竞态测试也验证了 S-1：触发器如果不取这把锁，失败一侧不会进入锁队列，屏障会超时。
- **S-2（不采纳，写明理由）**：
  - EIOS 的 `TerminalOutcomeReference` 是 `extra="forbid"` 的冻结契约，在 `terminal_outcome` 里加来源字段，会让之后的 reserve 重放无法通过校验。
  - 如果把 `claim_revision` 加 1，迟到的 finalize 或 retryable 会先因为 revision 不符被拒，R-1 要求覆盖的两个分支就测不到了。
  - 因此来源由 `outcome_id` 标识，它等于 `runtime.nexloop_effect_failures.failure_id`，可以直接关联。测试断言 revision 和 fence 都不变，并且 reserve 重放能把这个终态当作合法的 terminal outcome 读回（`REPLAY` 加 `PERMANENT_FAILURE`）。
- **S-3**：竞态测试里的认领是真实认领，经生产的认领端口与 SQL 函数完成 reserve、finalize、retryable，键与效应执行器相同：Action 的 stable name 加意图 id。执行器使用的键已由 `tests/test_effect_dispatch.py` 断言（`action_name='nexloop.service.request'`、`intent_id=<意图>`）。触发器按 `effect_intents.action_name` 找认领，与之对得上。
- **测试**（负载门控后 `-n 3`）：
  - `test_remediation_claim_race_pg.py`、`test_remediation_pg.py`、复制件守卫、已发布迁移：连跑 4 轮，每轮 82 passed；
  - `test_effect_origin_expired_pg.py`、`test_effect_confirm_not_delivered_pg.py`、`test_effect_dispatch.py`、`test_postgres_action_claims.py`、`test_nx041_stand_ins_pg.py`：51 passed；
  - 真实链路 `test_one_reply_per_message_pg.py`（串行）：3 passed。

## 没有覆盖的

- R3（governed_action 预填、调用回执、`execution_refused`）按 Q-2 暂缓，§6.3a 的 P-1 至 P-3 留给 R3。
- 熔断中的“governed_action failed 3 次”同样随 R3。
- 证据抽屉不显示负责人填写的原因文本：设计 §10 规定审计只含 id 与码；原因保存在补救事件表中，只供数据库层审计。
