# NX-027 商业事件与费用指标：实现说明

设计稿 `NX-027-design.md`（调度员已审，D1–D5、D7 已裁定，D6 负责人已定）。分支 `nx027-impl`，从 `af68908`（NX-026 交付）切出，带上设计稿提交。迁移号 0130–0133 为临时编号（L2 的 NX-051 占 0120–0129），合并时由调度员统一重编号。

## 1. 迁移

| 临时号 | 内容 | 依赖的已有对象 |
|---|---|---|
| 0130 `nx027_commercial_intake` | 设置表（`commercial.v1.json` 逐字种入）；连接器（configurator 写，real 连接器须负责人确认，test 连接器只写非 real 世界，D1）、客户关联、回执、原始事件、记录登记、异常；签名入口 `authz.nexloop_commercial_ingest`（HMAC v1、当前/上一把密钥、重放窗口、严格载荷、去重/冲突）；记录状态推导 `runtime.nexloop_commercial_target`（顺序无关：晚到、更正、退款链、币种冲突）；CommercialRecord 对象守卫；recorder 端口与读端口；D6 假名化端口；work feed `commercial-record` | 0093/0111 work feed、0084 属性授权派生、0106 `_SignedPort` 端口 |
| 0131 `nx027_metrics` | 观察表加更正链列（supersedes / retracted / excluded_reason / subject_ref）；指标来源声明（configurator）；记录投影为观察（净额/总额/不适用退款规则，异币种排除）；`authz.nexloop_compute_key_result` 改名包一层：只取链上最新、冻结 cohort 比率 | 0068 控制面 |
| 0132 `nx027_costs` | 模型请求结果带 Run 预算币种（D4）；费用条目 model / channel / discount（append-only）；Run 任务终态时追加释放行结算模型预留（D5）；cost_entry 指标投影；费用读端口 | 0089/0100 模型请求、0042 effect 账本、0068 预算、0001 jobs、0131 |
| 0133 `nx027_commercial_links` | recorder 下游钩子：每个对象新修订一次，唤醒关联 Consumer 的 active 计划（NX-024，触发种类 `commercial_event`），并给已绑定的承诺写 `commercial_event` 证据（NX-026）；承诺绑定表与 owner 内部绑定接口 | 0106 计划 feed、0111 承诺证据与 touch |

已发布迁移一字未改（0131 在切片三中放宽了自己未发布的 cost_entry 检查）。新函数一律 `search_path=pg_catalog,pg_temp`，SECURITY DEFINER 函数 owner 为 `nexloop_owner` 并撤销 PUBLIC。

## 2. 关键语义（与设计稿一致的部分不再重复）

- **入口**：只有已核验的首次到达写事件并在同一事务标记记录；重复 → 200、冲突 → 409 + 异常；签名错误、过期、超大、停用连接器不留事件。HTTP 层另有每连接器令牌桶（429）和早期大小限制；响应不带原因、租户或密钥。
- **记录**：CommercialRecord 的全部属性由 SQL 从该记录所有已核验事件推导，守卫只接受该状态；币种在首次到达时固定，后续异币种事件记冲突异常、不改记录。顾客自己的话永远不进入这条链（AT-011）。
- **指标**：投影与记录写入同事务；值变化才追加更正观察，记录退出统计口径时追加撤回观察；real 世界 KR 读不到 test 数据（AT-042，结构上成立）。cohort 比率在窗口开始后首次计算时冻结成员（窗口前已付费的 Consumer），之后的晚到成员只计数不入分母。
- **费用**：
  - model：每个记录了结果且带 cost 的模型请求一条（`provider_reported`，主币单位）；币种由触发器从 Run 命令的 `budget.currency` 写入，只能随结果写一次。Host 已拒绝 provider 档案币种与预算币种不同的 Run（`pi-runtime-adapter.ts`），所以记录的费用就是该币种。
  - channel：effect attempt 首次到达 `provider_accepted` / `observed_fulfilled` / `fulfilled` 时每个 intent 一条、1 单位；当前设置版本对该 Action 版本配置了格式合法的单价才写金额（`configured_rate`），否则 `unpriced` 只记单位（D7）。
  - discount：每笔 incentive 预留一条（最小货币单位，`budget_reservation`）。
  - 汇总按种类、币种、单位分别给出，从不跨币种相加。
- **结算（D5）**：Run 的任务进入 succeeded / failed / dead_lettered 时，对 `run:<run_id>` 模型预留追加一次释放行 `run:<id>:settle`（金额 = 实际 − 预留 ≤ 0，`recorded_at` 等于预留时间，所以不跨期）；任一调用无结果或结果未知时不释放（`result_pending_or_unknown`）；实际超过预留只记录（`exceeded`），不补扣。审计表 `runtime.nexloop_budget_settlements` 每个预留一行。负数只允许出现在 `:settle` 行；预算表仍 append-only。
- **衔接**：同一对象修订只唤醒、只写证据一次（`runtime.nexloop_commercial_wakes`）；未关联或已假名化的记录不唤醒任何计划；计划触发只带引用（`commercial:<object_id>`、种类、状态），不带金额与客户标识。承诺证据只写给明确绑定了该商业引用（连接器、种类、外部单号）的承诺，且记录的 Consumer 必须等于承诺的 `made_to`；不一致写所有者异常 `commitment_consumer_mismatch`，不写证据。证据时间取记录状态时间；provider 时钟略超前时，承诺在该时间到达后再评估。
- **D6**：删除 Consumer 时由 NX-029 调用 `runtime.nexloop_commercial_pseudonymize_consumer`：随机假名、不保存对应关系、删除关联、记录重新推导；`financial_retention_days` 默认 365（设置文件，`load_settings` 校验 1–36500）。

## 3. 部署材料

- `deploy/configuration/commercial.v1.json`（设置：事件类型映射、币种指数、重放窗口、载荷上限、保留期、渠道单价 `[]`、worker 参数）。
- `deploy/ontology/business-object-types.v1.json`：CommercialRecord v1（`commercial_identity` / `commercial_state` 两组）。
- `deploy/configuration/business-actions.v1.json` v6：CommercialRecord.create / .edit（service，commercial_recorder）。
- `deploy/authorization/service-grants.v1.json` v11：commercial_recorder 主体、feed、record、commercial.read、cost.read、两个 Action、对象类型 READ、`commercial_state` 属性规则。
- 连接器、客户关联、指标来源用 configurator 函数配置（`nexloop_eios.commercial.configure_connector` 等；密钥从私有文件读取，不回显）。
- 后台入口 `nexloop-commercial-recorder`（`--world real|test`，容器作业 `commercial-recorder`，compose 可选 profile `background`）；HTTP `POST /api/v1/webhooks/commercial/{connector_id}`。

## 4. 与设计稿的差异

1. **指标投影同步执行**：在 recorder 的 `recorded` 事务里完成，没有单独的 metric_projector 服务和 feed。
2. **没有 `commercial-raw` / `commercial-verified` feed**：入口只存已核验事件（核验同步完成），下游衔接用 recorder 事务内的钩子；计划唤醒直接调用 0106 的 `authz.nexloop_plan_feed_touch`。
3. **连接器与客户关联是部署配置**（configurator 函数），不是人类 Action。
4. **承诺匹配用绑定表**：af68908 的 Commitment 没有 `commercial_match` 参数，所以新增 `runtime.nexloop_commercial_commitment_bindings` 与 owner 内部接口 `runtime.nexloop_commercial_bind_commitment`；它的人类 Action 调用方属于 NX-028，本分支不授予任何角色（与 0106 T7 的做法相同）。在该 Action 落地之前，生产中不会产生商业证据。
5. **结算由触发器完成**：没有新增 `authz.nexloop_settle_budget` 签名端口，任务进入终态时自动结算，派发器无需改动。
6. **费用条目 ID** 为可读的确定性 ID（`model:<run>:<seq>`、`channel:<intent>`、`discount:<consumption_id>`），而不是哈希；未计价渠道条目的币种为空（D7）。
7. **费用指标的单位**：金额来源的定义单位必须写 `major` 或 `minor`，与条目尺度不同的记 `other_unit` 排除；cost_entry 来源也可以 `count`（计单位数）。
8. **载荷校验在 SQL**：契约提案见下文 §6，未改 `packages/contracts/`。

## 5. 未完成与缺口

- service / labour 费用的人类 Action（`nexloop.cost.record:1`）与 `/costs` HTTP 读接口：随 NX-028 工作台；现在只有读端口 `CostReadPort`。
- 折扣 Action：仓库里还没有发放折扣的 Action，discount 条目来自任何 incentive 预留。
- J01 端到端（真实 Host/Pi：付款 → 唤醒 → 复评结论“不再提醒”）未做；本分支验证到计划 feed 被标记为止，复评本身由 NX-024/025 的测试覆盖。
- `financial_retention_days` 短于指标成熟窗口时的 doctor 告警未做；到期清理由 NX-029 执行。
- `test_service_grants_pg` 三项失败：CommercialRecord 缺负责人的受限属性组决定（`owner-property-restrictions.json`），等负责人决定后补。

## 6. 契约提案（未改契约文件）

`packages/contracts/commercial-event.schema.json`（建议）：对象，`additionalProperties:false`；必填 `event_id`（1–200）、`type`（设置中的事件类型）、`external_id`（1–200）、`occurred_at`（RFC 3339，UTC）、`amount_minor`（非负整数，≤ 9007199254740991）、`currency`（`^[A-Z]{3}$`）、`customer_ref`（1–200）；可选 `provider_sequence`（非负整数）、`related_kind`（`order|payment|renewal`）与 `related_external_id`（两者同时出现，退款必填）。签名头 `x-nexloop-timestamp`（Unix 秒）与 `x-nexloop-signature`（`v1=<hex HMAC-SHA256("<timestamp>.<raw body>")>`）。

## 7. 测试（真实 PG，合成数据）

- `tests/test_commercial_intake_pg.py`（16）：AT-012 去重与并发、冲突、签名/窗口/载荷/停用、密钥轮换、晚到与更正、退款、币种冲突、AT-011 守卫、关联/取消关联、D1、D6、角色、读端口、HTTP。
- `tests/test_commercial_metrics_pg.py`（7）：净额/总额、补记退款、更正、撤回、异币种排除、AT-042、成熟度、冻结 cohort。
- `tests/test_costs_pg.py`（4）：AT-043 模型费用与 D4 币种、终态释放、未知结果不释放、折扣费用与成本指标、D7 单价。
- `tests/test_commercial_links_pg.py`（2）：付款证据使承诺兑现并唤醒计划、不匹配/未绑定/test 世界不写证据。
- `tests/test_outbound_messages_pg.py` 真实 Pi 链路追加：已接受的投递得到一条未计价渠道费用。
