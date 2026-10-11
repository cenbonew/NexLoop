# NX-037 A 类前置：实现说明

分支 `nx037-prep`，从 main `9d01169` 切出。迁移 `0161_nx037_trial_readiness.sql`（合入 s5g 时由临时号 0280 重编号）。依据：`NX-037-design.md` §2（初始化脚本和交互执行器先在合成环境跑通）、§3.1（停止条件）、§3.2 与 14 §7（证据包），ROADMAP D-g / D-i 定案值，11_DEPLOYMENT §9 D4（真实租户独立于合成 demo 租户初始化）。

本次产物只在合成或测试环境中运行：不接真实渠道、不接真实模型，也不打开任何真实派发开关。

## 1. 试运行配置 `deploy/configuration/nx037-trial.v1.json`

- **D-g 费用上限**：
  - 累计 ¥100；
  - 单次 Run ¥2；
  - 租户月度 ¥300；
  - 先用 3 次交互校准。
- **D-i 计划**：
  - 共 30 条交互；
  - 五类场景配比：普通回复 15、真实交付 4、拒收后受限回复 4、人工接管 4、结果不明对账 3；
  - 两个工作日，10:00–18:00。
- **停止规则**：D-i 的 4 条，加上设计 §3.1 的跨租户读取、单次 Run 超限、critical 告警、负责人叫停。
- **暂停阈值**：连续 10 次中错误率超过 10%；费用达到上限的 80%；存在未了结的不明效应。
- **引用的配置**：告警规则 v3，效应类别 v2。

## 2. 真实租户初始化脚本 `scripts/nx037/real_tenant_init.py`

### 参数

- 租户 ID：必须是 uuid，并且不能是合成 demo 租户 `SANDBOX_TENANT`；
- 负责人主体：不透明引用；
- 价值目录文件：只记录它的 sha256；
- 渠道配置引用：不透明引用；
- D-g 三个值：可覆盖，但必须满足“单次 ≤ 累计 ≤ 月度”；
- 试运行期间。

### 三种模式

- **`plan`（默认 dry-run）**：只输出将要执行的受治理步骤，什么都不执行：
  1. `service_grants`（受信配置；走 0050 路径，同时创建租户和服务主体）；
  2. `effect_categories`（受信配置）；
  3. `alert_rules`（受信配置）；
  4. `model_budget`（负责人 Action `Budget.set`；输出完整的请求体，request_id 是稳定的业务意图 ID）；
  5. `value_catalog`、`channel_configuration`（只作为引用）；
  6. `verify`。
- **`execute`**：只调用受信配置的入口，也就是 `service_grants.apply`、`effect_categories.apply`、`observability.apply_rules`。它们都以 configurator 角色、经配置函数执行，可以重复执行。
  - **效应类别**：只为租户已经发布的 Action 声明，其余 Action 记为 `deferred`。未声明的 Action 按“触达客户、外部效应”处理（fail closed），所以推迟声明不会打开任何东西。
  - **预算**：必须由负责人在自己的会话中提交（工作台），脚本本身不提交。没有传入负责人的 `owner_actions` 时，这一步记为 `owner_pending`，并给出 request_id。
  - **不做的事**：不写任何表，不打开开关，不读取渠道密钥。
- **`verify`**：只读，以 metrics 角色调用 `authz.nexloop_nx037_tenant_state`（0161）。以下条件全部满足才算 ready：
  - 租户为 active；
  - 真实派发的三层开关都关闭：本进程的 `REAL_DISPATCH_ENABLED` 不是 `true`，租户开关没有打开，没有任何打开的 Action 开关；
  - model 预算等于 D-g 的累计上限，币种一致，并覆盖整个试运行期间；
  - 告警规则的生效版本等于所配置的版本。
  - 效应类别的声明数量只报告，不作为 ready 条件，因为未声明的 Action 本来就是外部效应、处于关闭状态。

### 限制：单次 Run 上限和月度上限没有对应的数据库预算

- **原因**：数据库里每个租户、每个 world 只有一个 model 预算（`control.nexloop_budget_limits` 的主键是 (tenant, world, kind)），无法同时表达三个上限。
- **当前做法**：
  - 数据库中只设置累计上限（试运行期间的 model 预算）；
  - 单次 Run 上限和月度上限由执行器的停止和暂停规则执行；
  - verify 在 `enforced_by_executor` 中列出这两个值。
- **提案（需要定）**：如果要在派发时强制执行这两个上限，需要扩展预算表，或者新增按 Run 的预留检查。这属于契约和迁移层面的改动，本次没有做。
- **第二道防线**：模型账号侧的花费上限，由负责人在 Provider 侧设置。

## 3. 受控交互执行器 `nexloop_eios/controlled_interactions.py`

### 调度与执行

- **调度 `schedule`**：按配比均匀交错排列（最大余数法），前 10 条就包含全部五类场景。每条交互有稳定的 ID（uuid5(trial_id, seq)），重试和重启后不变。
- **执行 `run`**：逐条调用 driver 的 `interact` 发起交互，然后由 collector 按 run_id 采集证据。每条记录在下一条交互开始之前写入 `interactions.jsonl` 并 fsync；结束时原子写入 `state.json`。
- **恢复**：
  - 已记录的交互不会再执行；
  - 暂停之后可以恢复，恢复后错误窗口重新计数（负责人已经复核过）；
  - 已停止（stopped）的试运行不能恢复，需要用新的 trial id 重新开始。

### 证据（每条交互一条记录）

- **耗时分解**：
  - 端到端：执行器自己计时；
  - 模型：各次调用 completed_at − requested_at 之和；
  - 工具：0133 记录的工具耗时；
  - 平台：端到端减去模型和工具。
  - 分解方法与 NX-031 §4.2 相同。
- **其他字段**：
  - 模型调用次数、失败次数、结果不明次数；
  - 按类别汇总的费用；
  - 意图的状态与 ID；
  - effect 路径的拒绝码；
  - 交付回执 ID；
  - 对账结果；
  - 规则判定。

### 规则（`evaluate`，停止优先于暂停）

- **出现 1 次即停止**：
  - `unauthorized_send`（越权发送）；
  - `wrong_recipient`（发错对象）；
  - `contact_after_refusal`（拒收后仍联系）；
  - `duplicate_external_effect`（同一意图被执行超过 1 次）；
  - `cross_tenant_read`（跨租户读取）；
  - `per_run_cost_exceeded`（单次 Run 超过 ¥2）；
  - `critical_alert`（critical 告警）；
  - `owner_stop`（负责人叫停）。
- **暂停**：
  - `error_rate`：最近 10 次中，错误数除以 10 大于 10%，即至少 2 次错误；
  - `cumulative_cost`：累计费用 ≥ 累计上限的 80%；
  - `monthly_cost`：本月已有费用加上累计费用 ≥ 月度上限的 80%；
  - `unresolved_unknown_effect`：对账窗口结束时仍有结果不明的效应。

### 采集器

- **`PgCollector`**：以 metrics 角色调用 `authz.nexloop_nx037_run_evidence`（0161）。返回结果只有计数、毫秒数、金额、币种、状态码和 ID。

### Driver

- **本次只有合成 driver**：`tests/support/nx037_synthetic.py`，由确定性 provider 加回环渠道组成。
  - 停止规则依据的事实，都从回环渠道和 provider 实际留下的记录推出：发送记录、收件人、授权、每个意图的执行次数、上下文中读取的租户。
  - 因此注入的违规与真实违规一样，都是通过检查记录发现的。
- **真实 driver**：通过 WebChat 和企业服务端点发起交互的 driver，属于 NX-037 B 类工作。

## 4. 证据包 `scripts/nx037/evidence_pack.py`

### 证据项（14 §7 / 设计 §3.2）

- inventory_before / inventory_after
- release_manifest
- ci_result
- restore_drill
- effect_reconciliation
- load_report
- public_preflight
- third_party_notices
- known_risks
- interactions

### 处理规则

- **缺失项**：没有提供的项记为 `missing`，从不编造。
- **一般来源文件**：只记录 sha256 和字节数。这些文件可能包含主机名、路径或人名，所以不复制任何内容。
- **发布清单**：只抽取固定字段：commit、tree、release id、CI 结果 digest、catalog、契约 digest、锁文件哈希，以及镜像 digest 和归档哈希。
- **交互**：输出三个文件：
  - `interactions/summary.json`：按场景类别和结果的计数、耗时分解的 p50/p95、费用、停止或暂停的结论；
  - `interactions/reconciliation.json`：意图 ID 及其对账状态；
  - `interactions/records.json`。

### 值的限制

- **允许的值**：uuid、十六进制哈希或 commit、时间与日期、十进制数、布尔值、null、release id，以及固定词表中的代码（场景类别、规则、状态）。
- **校验**：写入时校验，`check(DIR)` 可以复查；不允许的值会被拒绝。
- **输出目录**：不允许写入已存在的目录。

## 5. 迁移 0161

- **新增函数**：两个只读、security definer 的 owner 函数，各自带一个 authz 包装函数，只授权给 `nexloop_metrics`：
  - `nexloop_nx037_tenant_state`（租户状态、开关、预算、告警规则版本和 digest、效应类别声明数）；
  - `nexloop_nx037_run_evidence`（单个 Run 的证据）。
- **search_path**：`pg_catalog,pg_temp`。
- **RLS**：函数读取 FORCE RLS 的表，所以先设置当前事务的 `eios.tenant_id`，读取后恢复。
- **不涉及的部分**：不写数据，也不改动任何已有的函数或表。

## 6. 测试

### `tests/test_nx037_controlled_interactions.py`（14 个，不需要 PG）

- **配置**：D-g / D-i 的值，以及非法配置被拒绝；
- **调度**：配比正确，ID 稳定；
- **合成环境完整运行**：30 条全部完成；耗时分解正确；对账全部了结；拒收和人工接管的交互不发送；每个意图只执行一次；对已完成的试运行重新执行时，不会再发起任何交互；
- **停止规则**：7 条停止规则各注入一次违规，都在出现的那一条交互上停止；停止前证据已经写入；停止后不能恢复；
- **负责人叫停**；
- **错误率**：10 次中 1 次错误时不暂停，2 次时暂停；恢复后不重复执行已记录的交互；
- **费用暂停**：累计费用达到 80% 时暂停；月度费用达到 80% 时暂停；
- **不明效应**：存在未了结的不明效应时暂停，且不会重发；
- **证据包**：只包含允许的值；缺失项如实记录；inventory 的内容不会被复制；自由文本会被 check 找出；拒绝写入已存在的目录。

### `tests/test_nx037_tenant_init_pg.py`（3 个，干净 catalog）

- **dry-run**：步骤与预算请求正确；拒绝 demo 租户和非法的费用上限；缺少受信配置文件时 execute 不执行。
- **execute 与 verify**：
  - 初始化前 verify 不通过；
  - execute 后只差预算一项；
  - 负责人经自己的会话提交 `Budget.set`（受治理路径）后，verify 通过；
  - 部署层开关打开、租户开关打开、预算值不符时，verify 都不通过；
  - 输出中不出现 DSN。
- **run_evidence**：只返回数字；metrics 角色不能直接调用 runtime 下的函数。
