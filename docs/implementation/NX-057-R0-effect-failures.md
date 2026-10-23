# NX-057 R0：最终失败的证据，以及按事件时间统计

- **来源**：设计稿 `docs/design/remediation-intents.md` 第 3 版 §8、§10、§13 的 R0；L6 第二轮复核（`2c9e7d81`，没有确认级问题，R0 至 R2 可以推进）。调度员 2026-10-23 派工，L7 执行。
- **分支**：`l7/nx057-r0`，位于设计稿第 3 版（`073c9c55`）之上；基点 main `76a9ab00`（第二十二批）。
- **迁移**：`effect_failures`，临时编号接在基点最高的 `outcome_classes`（NX-061 B5）之后，合并时由调度员重新编号。本文和代码注释里只用文件名 stem 指代迁移，不写具体编号（L6 M-4）。
- **组批顺序**（L6 M-2）：R0 要排在 NX-056 的 `nx056_approval_expiry` 之前。那个迁移也会改名并包装一层 `runtime.nexloop_metrics_snapshot`；如果 R0 排在它后面，R0 的 `create or replace` 会把 approvals 那一层覆盖掉，复制件守卫会在合并时报错。顺序由调度员安排。
- **范围**：只做 R0。没有补救意图表、规则、规划器或工作台改动（R1 起）。

## 做了什么

### 1. 失败记录 `runtime.nexloop_effect_failures`（迁移 `effect_failures`）

- 每个最终失败的效应意图**一行**（`intent_id` 唯一），与失败**在同一事务中**写入：

  | 路径 | 函数 | `reason_code` | `evidence`（只含 id 与码） |
  |---|---|---|---|
  | ① 一次性回复在派发前被拒 | 0152 `authz.nexloop_fail_refused_effect` | `refused_one_reply:<原因>`（`already_answered`、`reply_window_closed`、`fallback_bound_elsewhere`） | `refusal_code`（NXC05）、`reason` |
  | ② 来源 Run 过期且从未派发 | 0174 `authz.nexloop_effect_origin_expired` | `origin_expired` | `origin_run_id` |
  | ③ 负责人带证据确认未送达 | 0210 `runtime.nexloop_governed_confirm_not_delivered` | `confirmed_not_delivered` | `determination_id`、`observation_id` |

- `reason_code` 用检查约束限定在上面的词表内。
- 只追加（`control.nexloop_nx022_append_only`）；FORCE RLS，策略按 `eios.tenant_id`；属主 `nexloop_owner`，从所有应用角色收回全部权限。外键指向效应意图。
- 在 `control.nexloop_real_effect_tables` 中登记为 guarded，并加上与其它 real 效应表相同的 `nx041_real_effect_world` 约束：不会有模拟或影子世界的行。
- **效应事件表 `runtime.nexloop_effect_events` 不变**：它的检查约束、`effect_fence` 以及读取它的代码都不受影响（设计稿第 3 版 §10）。
- **延迟约束触发器** `nx057_effect_failure_required`：意图的 state 改为 failed 时，在**提交时**检查失败行是否存在；不存在就拒绝整个事务（`23514 effect failure evidence missing`）。核对过基点：全库只有上面三处把效应意图置为 failed，所有插入都以 `accepted` 开始，所以这个触发器只挂在 `update of state` 上。以后如果出现第四条失败路径而忘了写失败行，它会在第一次测试时就失败，而不是悄悄漏掉补救与统计。

### 2. 修正 `outcome_classes` 的统计口径（C-2 与第 3 版的建议）

用 `create or replace` 重定义 `outcome_classes`（第二十二批）的快照包装函数。效应部分的**分子和分母都按事件时间**计数：

- **succeeded**：1 小时内有 `fulfilled` 事件的不同意图；
- **outcome_unknown**：**首条** `unknown` 事件落在 1 小时内的意图，但同一小时内已经 fulfilled 的不计，这样一个意图在一个窗口里只计一次（L6 M-1）；
  - 查询以窗口为界：先取窗口内的 unknown 事件，再用 `not exists` 排除更早有 unknown 的意图（L6 M-3）。效应事件表原来只有主键，这次新增两个索引：`(tenant_id, world, event_kind, created_at)` 和 `(intent_id, event_kind, created_at)`；
- **defect**：1 小时内记录的失败行中，原因为 `origin_expired` 或 `confirmed_not_delivered` 的；
- `refused_one_reply:*` **不计**，它已经按拒绝码计入；
- **删除了 `outcome_classes` 的 `intents` 那一支**（按意图 `created_at` 取窗口、按“是否有过拒绝”排除 failed），所以同一个失败不会计两次；
- 拒绝部分与死信部分不变。

效应表都是 FORCE RLS。包装函数现在在读取前**自己设置租户、读完恢复**，与基础快照（0131）的做法相同。`outcome_classes` 原来没有这样做，它能读到效应意图，是因为更内层的某个包装函数留下了租户设置。这不是线上缺陷，但很脆弱；修正后不再依赖它。

### 3. Python 镜像

`nexloop_eios.run_outcome_taxonomy` 新增 `FAILURE_REASON_CLASSES`（两个 defect 原因）和 `classify_failure`（`refused_one_reply:*` 返回 None，即由拒绝计入；未知原因返回 UNRESOLVED）。测试保证：SQL 检查约束接受的原因，与 Python 归类的原因一致。

### 4. 复制件守卫

在 `tests/test_function_copy_guard.py` 中登记 4 个重定义（stem `effect_failures`）：

- 三个失败函数：除了新增的那条 `insert into runtime.nexloop_effect_failures(...)`，其余与基点完全相同；
- 快照包装函数：除了租户的设置与恢复，以及 `intents` 那一支，其余与 `outcome_classes` 完全相同。

## 测试

环境：`PYTHONPATH=packages/eios-core/src:tests LC_ALL=C.UTF-8`，`uv run --frozen`，Node 24；真实 PostgreSQL 由各测试的夹具在一次性集群中引导，迁移目录包含 `effect_failures`。

| 命令 | 结果 |
|---|---|
| `pytest -n 3 tests/test_outcome_classes_pg.py tests/test_function_copy_guard.py tests/test_effect_origin_expired_pg.py tests/test_effect_confirm_not_delivered_pg.py tests/test_nx041_stand_ins_pg.py::test_every_outbound_table_is_declared` | 61 passed。其中 `test_outcome_classes_pg.py` 按事件时间改写并新增 1 个：<br>- 2 小时前创建、刚刚来源过期的意图，以及 30 小时前创建、刚刚确认未送达的意图，都计入 defect（`outcome_classes` 的口径漏掉了这两个）；<br>- 被拒后失败的一次性回复只按拒绝码计一次；<br>- 先被 NXB02 拒绝、后来过期的意图，拒绝与失败各计一次（设计 I-1）；<br>- 首条 unknown 在窗口外的不再计入；2 小时前创建、本小时 fulfilled 的计入 succeeded；<br>- 结果为 `conflict_protected=4, transient=2, authorization=1, defect=3, outcome_unknown=1, succeeded=2`，`failure_rate=6/13`（第一次提交 `03ce1f1b` 时）；<br>- 调用时不设置租户，计数不变，调用后租户设置恢复为空。<br>新测试：意图置为 failed 而没有失败行，在提交时被拒，状态仍是 accepted；有失败行时可以提交；同一意图第二行被唯一约束拒绝；失败行不能修改；词表外的原因被拒；6 个应用角色都没有读写权限；FORCE RLS。<br>三条路径各自的测试新增断言：各写一行、原因码正确、证据 id 正确；`stopped`（已派发、仍为 unknown）不写失败行 |
| `pytest -n 3 tests/test_observability_pg.py tests/test_erasure_pg.py tests/test_erasure_whole_database_pg.py tests/test_consumer_erasure_coverage_pg.py tests/test_db_boundary.py tests/test_incentives_pg.py tests/test_bootstrap.py tests/test_published_migrations.py tests/test_nx041_stand_ins_pg.py tests/test_effect_dispatch.py` | 68 passed。全库删除传播的搜索没有在失败表中找到 consumer id 或 principal id；已发布迁移的校验和不变 |
| `pytest tests/test_one_reply_per_message_pg.py tests/test_reply_reconcile_pg.py tests/test_reply_fallback_pg.py`（真实链路，串行） | 18 passed（202 s）。`test_reply_reconcile_pg` 的账本注入（“provider 明确失败”）本来直接把意图改为 failed，现在被延迟约束拒绝，所以注入时同时写入一行 `confirmed_not_delivered` 失败行 |
| L6 复核后（M-1、M-3、M-4，`2731148b`）：`pytest -n 3 tests/test_outcome_classes_pg.py tests/test_function_copy_guard.py tests/test_published_migrations.py`；`pytest -n 3 tests/test_effect_origin_expired_pg.py tests/test_effect_confirm_not_delivered_pg.py tests/test_observability_pg.py tests/test_function_copy_guard.py tests/test_bootstrap.py` | 53 passed；70 passed。新增用例：同一小时内先 unknown、后 fulfilled 的意图只计 succeeded（`succeeded=3`，`failure_rate=6/14`）；复制件守卫在新的 outcome_unknown 查询上仍只多出登记的改动 |
| `python scripts/check_definer_search_path.py`；`pytest tests/test_function_search_path.py` | 0 problems；10 passed |
| `python scripts/check_publication.py` | 0 violations |

**首次失败**：没有测试失败。准备提交时，`ruff` 报出 `run_outcome_taxonomy.py` 中的 `classify_failure` 重复定义，是一次编辑脚本重复执行造成的；删除重复的那份后重跑了上面的测试。
## 限制

- `outcome_unknown` 按首条 unknown 事件计数；一直 unknown 的意图只在它第一次 unknown 的那一小时出现。
- 基点中没有带时间的“Run 成功完成”计数，Run 路径仍然只统计拒绝和死信（与 `outcome_classes` 相同）。
- 修正后失败率**会升高**：以前漏掉的来源过期与确认未送达现在计进来了。这是口径修正，而不是退化，上线说明里需要写明。
- **新口径只对迁移之后发生的失败生效**（L6 M-5）：迁移之前已经 failed 的意图没有失败行，迁移也不回填。失败率在迁移后的第一个小时里只包含新发生的失败；R1 也只会为迁移之后的失败生成补救。上线说明里要写明这一点。
- R0 不生成任何补救。补救意图从 R1 开始。
