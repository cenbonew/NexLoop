# ADR-031 实现说明：一次对象读取内，relay Source 派生每个对象只做一次

依据：ADR-031（负责人 2026-10-13 批准，ROADMAP §7 优化②）。迁移 0191。

## 1. 改动

- `authz.nexloop_read_object`（`create or replace`，名字、属主、授权、`search_path` 不变）：遇到 `relay-source-v1` 的属性证明时，本次调用对目标对象只派生一次；每个属性证明改走 `authz.nexloop_assert_relay_source_property_read`；调用结束前对对象再完整派生一次，结果必须与第一次相同。
- `authz.nexloop_relay_source_property_basis`：0142 派生中的属性部分，逐行相同，并要求属性属于对象部分对应的那个对象。
- `authz.nexloop_assert_relay_source_property_read`：与 `authz.nexloop_assert_relay_source_read` 逐行相同，只是派生依据改为“对象部分 + 属性部分”；开头先做 `runtime.nexloop_assert_not_erasing`（原来由 `authz.nexloop_assert_read_authority` 先做）。
- 旧函数体保存为 `authz.nexloop_read_object_before_derivation_once_v0420`（只有属主可执行），用于回滚和等价性对比。
- Python 代码没有改。

## 2. 等价性证据

- **对比包装**（`tests/support/read_object_compare.py`，测试库中通过 `NEXLOOP_TEST_READ_OBJECT_COMPARE` 启用）：每次 `read_object` 调用（包括 SQL 函数内部的调用）都同时执行新旧两个实现，比较返回值或错误（SQLSTATE 和文本）。
- **语料**：20 个测试文件，覆盖 v2/v6 包、关系上下文 v4、角色上下文 v6、证据与消息读取派生、属性授权派生、服务授权、工作台成员读取、relay 读取规则、多消费者 relay、上下文工件、目录配置、企业 slot（中继与派发）、评估后端、擦除传播、数据导出、消息 relay（含真实 Pi）。
- **结果**：220 项测试全部通过（216 项 `-n 3` 并发，4 项真实 Pi 串行）。其中 131 项有对象读取，共比较 9961 次，**0 次不一致**。

## 3. 反例（`tests/test_read_derivation_once_pg.py`）

- 负责人限制了某属性所在的属性组后，即使同一调用中对象和其他属性都允许，该属性仍被拒；去掉该属性后，同一读取成功。ServiceOffering 已发布的 schema 中，目录字段都不属于任何属性组，所以测试在可丢弃的测试库里给 `title` 注入一个属性组，作为显式注入。
- 用一个对象的派生去校验另一个对象的属性，返回 null；不在任何登记中的 offering 仍被拒。

## 4. 测量（本机小模板，未提交的临时测试）

方法：
- 负载链有 10 个消费者，策略 `recent_plus_required`，relay 以 run profile 模式运行，用负载链自己的 Source。
- 连续准备 10 条消息 Run，取 relay 步骤计时中 `context_prepare` 的中位数。
- SQL 函数调用次数取自 `track_functions='all'` 下的 `pg_stat_user_functions`，按每条消息平均。
- “优化前”是在同一份库上把 `read_object` 换回旧函数体（只在测试中这样做）。前后各跑两次，结果一致。

| 每条消息 | 优化前 | 优化后 |
|---|---|---|
| `context_prepare` 中位数（ms） | 404 / 408 / 407 | 409 / 409 / 428 |
| SQL 函数调用总数 | 约 29 000 | 约 27 500–27 900 |
| `nexloop_read_object` | 32 | 30 |
| relay Source 派生（`nexloop_relay_source_derivation`） | 360 | 183–185 |
| `nexloop_assert_relay_source_read`（整条派生核对） | 288 | 64 |
| 属性部分（`nexloop_relay_source_property_basis`） | 0 | 206 |
| 权威事实快照（`load_authority_fact_snapshot`） | 3 270 | 3 210–3 270 |
| 身份快照（service + root） | 约 3 260 | 约 3 130–3 160 |

**结论：派生次数减半，但在本机上 `context_prepare` 的耗时没有可测的变化（差异在噪声内）。**
- 单次派生本来就便宜（L4 剖析：约 0.1 ms）。
- 耗时主要来自数据库往返和权威事实、身份快照：每条消息仍有约 6 400 次快照调用，这属于剖析 §4 第 4 项，不在本次范围内。
- 剩余约 183 次派生的来源：
  - 约 71 次来自 Python 构建证明时逐属性调用的 `property_access_basis`；
  - 约 64 次来自每次读取开头和结尾对对象声明的核对；
  - 其余是本 ADR 的对象派生（每次读取开头一次、结尾复核一次）。

## 5. ADR-032：一次读取检查内，身份只解析一次（迁移 0192）

- **改动**：批量取事实 `authz.nexloop_load_authority_facts` 和读取检查 `authz.nexloop_assert_read_authority_inner_before_erasure_v0142`，都在第一条事实时解析一次身份，再传给每条事实的读取（新增 `authz.nexloop_load_authority_fact_with_identity`、`authz.nexloop_load_authority_fact_snapshot_with_identity`）。保护写入的检查不改。旧函数体保存为 `_before_identity_once_v0421`。
- **等价性**：对比包装扩展到上述两个函数，与 `read_object` 一起对比。
  - 语料与 ADR-031 相同，另加 `test_identity_once_per_check_pg.py`、`test_run_credentials.py`。
  - 239 项测试全部通过（236 项分两段以 `-n 3` 并发运行，3 项真实 Pi 串行），其中 211 项有对比，三个函数共对比 55 868 次，**0 次不一致**。
- **反例**（`tests/test_identity_once_per_check_pg.py`，3 项）：
  - 撤销在检查前提交：读取被拒，批量取事实全部为 `error`；
  - 检查进行中：撤销被凭据锁挡住，`lock_timeout` 超时，检查结束后撤销提交，下一次读取被拒；
  - 同一事务中两次批量取事实之间修改事实、撤销凭据：第二次看到新的记录哈希，第三次全部为 `error`。
- **测量**：方法同 §4，每种变体各跑两次。测量时机器负载约 10，耗时数字的绝对值偏高。

| 每条消息 | 优化前（基线） | 只有 ADR-031 | ADR-031 + ADR-032 |
|---|---|---|---|
| `context_prepare` 中位数（ms） | 442 / 446 | 443 / 439 | 425 / 425 |
| SQL 函数调用总数 | 27 800–28 100 | 27 500–27 800 | 24 400–24 600 |
| `root_identity` | 5 800–5 860 | 5 820–5 890 | 4 220–4 250 |
| `service_identity` | 4 570–4 620 | 4 590–4 640 | 2 975–2 990 |
| `load_authority_fact`（自行解析身份） | 3 210–3 260 | 3 230–3 270 | 1 460 |
| `load_authority_fact_with_identity` | 0 | 0 | 1 850–1 900 |
| relay Source 派生 | 332–336 | 181–184 | 184–187 |

**结论**：
- 身份解析减少约 30–35%，函数调用总数减少约 12%，`context_prepare` 中位数约下降 4%（约 18 ms）。在当时的负载下，这个差别不能视为可靠。
- 剩余的约 1 460 次自行解析身份的事实读取，来自保护写入的检查（动作、工件等），按 ADR-032 有意不改。
- 剩余的约 4 200 次 `root_identity` 来自各检查开头与结尾的身份快照，以及凭据加锁。
- 本机上 `context_prepare` 的大头仍是数据库往返（剖析 §2）。要降到 CI 上 500 ms 以下，主要应依靠优化①（L3，复用信封、减少往返）。
