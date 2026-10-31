# 设计稿：从 dsh-superagent-prototype 吸收的设计思想（第 2 版）

- **状态**：设计稿，**不含实现，不含代码移植**。负责人已于 2026-11-01 对 §7 的问题拍板（按 L6 推荐），见 §7。
- **修订记录**：
  - 第 1 版：2026-11-01，依据对 [deepseek-ai/dsh-superagent-prototype](https://github.com/deepseek-ai/dsh-superagent-prototype)（MIT，单提交，自述研究原型）全部 8 篇设计文档与核心源码（`src/core`、`src/app/effects.ts`、`src/app/outbox.ts`、`src/executor/dsh.ts`）的通读，对照 NexLoop 的 [02_ARCHITECTURE](../handoff/docs/02_ARCHITECTURE.md)、[05_CONTEXT_AND_PLANNING](../handoff/docs/05_CONTEXT_AND_PLANNING.md)、[06_ACTION_RUNTIME_AND_RECOVERY](../handoff/docs/06_ACTION_RUNTIME_AND_RECOVERY.md)、[16_ADR](../handoff/docs/16_ADR.md)、ADR-028 / 030 / 039 / 040 / 041 与 [主动触达设计稿](proactive-outreach.md)。
- **关联**：
  - AGENTS.md 固定决定：Pi Durable 是唯一首版 Harness，第二 Harness 需 ADR；PostgreSQL 为业务与队列权威；只有受治理 EIOS Action 产生外部效应；模拟 / 影子不能调用真实效应凭据；
  - ADR-016（自有代码 Apache-2.0，第三方保留原许可）、ADR-023（联系限制与回复保证）、ADR-039（统一审批）、ADR-040（补救意图）；
  - `docs/design/proactive-outreach.md`（负责人已拍板 P1–P8，实施排在 v0.1 冻结之后）；
  - NX-023（上下文 v6 与请求快照）、NX-024（计划复评与 `run-outcome` 契约）、NX-022（控制面）。
  - 第 2 版：2026-11-01，正文保留第 1 版原意，只做增补：写入负责人对 Q-1 至 Q-4 的拍板（§7 改为“已决定”）；落 L6 复核注记 N1–N5（§3.1、§3.3、§3.4、§4）；§4 为 K0 结论留占位（待 L7）；§5 按 §3.5 格式补拍板日期列。N2 同时在 [主动触达设计稿](proactive-outreach.md) 的修订记录中登记。

## 0. 背景与结论

dsh-superagent 是“面向单个 GitHub 仓库的主动编码 Agent”，作为 DeepSeek Harness 插件包发布。它监听 GitHub 与飞书，经 Policy 判定是否受理，受理后生成 Ticket 并在独立 Session 与目录中执行，结果以 PR 或回复送回原会话。核心原则是“占用人的注意力最少，给出的价值最多”。

对照结论：

1. 两个项目在工程纪律上高度同构：先持久化再执行、授权由宿主代码保证而非提示词、外部文本只是数据、先观察再放开、一个状态一个写入者、执行器在端口之后、不宣称 exactly-once。这些 NexLoop 已经有，不需要再吸收。
2. 两者在领域、长期状态、租户与存储、执行器安全边界、授权来源、编排方式、运行时上根本不同（§1）。
3. **代码不吸收**（§2）。**五条设计思想可以吸收**（§3），另有两条工程细节待核对（§4）。

## 1. 对照摘要

| 维度 | dsh-superagent | NexLoop | 结论 |
|---|---|---|---|
| 领域 | 开发团队的代码工单 | 企业与消费者的长期运营、承诺、商业记录 | 不同 |
| 长期状态 | 无。Ticket 是唯一单元，无目标、记忆、本体 | 目标 / KR、本体、Claim、承诺、计划长期在 PG | 不同 |
| 租户与存储 | 单租户、单仓库、单 SQLite 文件，老记录不清理 | 多租户、RLS、PostgreSQL 权威，保留期与擦除（NX-029） | 不同 |
| 执行器边界 | Worker 以操作者的 OS 用户直接跑 shell；文档明说包装器“不是安全边界” | Agent Host 无 shell、无 DB、无渠道凭据，只能经 guard 调受治理 Action | 不同 |
| 授权来源 | 允许名单 + 准入表 + 规则；授予人即 GitHub / 飞书账号；Mandate 不可变、恢复前重验 | 服务端推导 tenant / actor / role；受治理 Action；派发时复查；统一审批 | 形状相近，来源不同 |
| 编排 | 主 Session（LLM）只路由，Worker Session 执行，二者长期存在 | 事件唤醒的短生命周期 Run，角色 N:M，不设常驻编排 Agent；路由由 SQL 预检决定 | 不同 |
| 先持久化再执行 | Effects 队列在命令事务内入队、提交后派发；按 (kind, key) 合并 | outbox、effect intent、Effect Worker | 同构 |
| 先观察再放开 | accept mode：off / observe / auto；observe 只记录“本来会接” | 三层真实派发开关、test / real world、影子运行 | 同构；但主动触达缺 observe 层（§3.1） |
| 提示词 | 版本化、团队可覆盖内置片段、默认更新时显示差异；模型可见面有漂移测试 | 上下文策略版本、人设版本、可信配置版本；有 context pack 预算测试 | 部分同构（§3.2） |
| 人机界面 | 面板只列待人工的事，健康状态不占位；问题以结构化字段一次问一个 | 工作台有审批、补救、告警、接管，各自成页；Run 可输出 `escalate` / `needs_information`，理由是自由文本数组 | 可借鉴（§3.3） |
| 运行时 | 绑死 DeepSeek Harness（`@deepseek-ai/cordis`、`dsh ^0.2.1-alpha`） | Pi Durable 经 RuntimeAdapter | 不兼容 |
| 成功指标 | 人的注意力消耗 | KR、必回担保、真实动作可靠性 | 不同 |

## 2. 为什么不吸收代码

1. **领域对象不对应。** `src/core` 是无 I/O 的纯领域层，但对象是 Trigger / Thread / Ticket / Mandate / Subscription，建模的是 GitHub 与飞书的会话。NexLoop 的 Consumer / Conversation / Run / Intent / Plan 已在 PG 中以 RLS 和受治理 Action 实现。移植等于用 Python 与 SQL 重写，价值在规则而不在代码。
2. **与固定决定冲突。** `src/core` 以外全部依赖 DeepSeek Harness（Session、Agent、子进程服务）与单文件 SQLite。引入即第二 Harness 与第二权威存储，违反 AGENTS.md。
3. **不是稳定上游。** 单提交、自述“可能有 bug、风格不一致”，老记录不清理，信任边界（哪些 Trigger 字段可信）仍是其 open question。不满足 vendored 上游的来源与维护要求。
4. 许可不是障碍：MIT 与 ADR-016 兼容，第三方声明可按现有流程生成。

把它当 NexLoop 自身的开发工具（替代调度员与各条线的人工流程）也不建议：它以 GitHub 为控制面，而 NexLoop 的决定是本地 CI 权威、GitHub 只做同步，且需要安装 DeepSeek Harness 运行时。

## 3. 可吸收的五条思想

每条按“现状 / 借鉴点 / 落地方案 / 验收要点 / 需要的决定”写。落地方案只给形状，迁移编号与 Action 版本在立项时定。

### 3.1 主动触达加“观察模式”

**现状。** [主动触达设计稿](proactive-outreach.md) 已定：默认关闭、按目的开启、派发时 10 道闸门逐道留证。但一个目的从“关闭”到“真实派发”只有一步；放开前负责人没有办法用真实数据看到“系统本来会给谁发什么、哪道闸门会拦”。设计稿全文没有 observe / 影子层。

**借鉴点。** dsh 的 accept mode 有 observe：候选照常研判，正向结论记录为“本来会接”，不接。文档建议“从 observe 开始，审阅记录后再切 auto”。

**落地方案。**
- 目的清单 `proactive-purposes.v<N>.json` 的每个目的增加 `mode ∈ {off, observe, live}`，默认 `off`。`live` 对应现状的“启用”；`active` 字段并入 `mode`。
- `observe`：触发源照常标记复评；复评 Run 照常判断并提交 `nexloop.outreach.send:1`；派发时走完全部 10 道闸门，**每一道的通过或拒绝都写入**，然后以新拒绝码 `purpose_observe_only` 终结，provider 收到的请求为零，不预留效应单位、不计入频率桶。
- 观察记录写入现有派发拒绝表，并带 `observed_body_hash` 与文案工件引用，供负责人在工作台“主动触达观察”视图按目的、按闸门汇总：本来会发多少条、每道闸门拦多少、文案样例。
- 从 `observe` 切到 `live` 是发布新版清单，走负责人受治理 Action，有版本、留证，与现状一致。
- 测试人设（ADR-034）在 observe 下同样只能走测试世界，§6 的排除规则不变。

**验收要点。** observe 下 provider 零请求（现有 `test_loopback_http` 断言）；每条观察记录带全部闸门结果；切 `live` 前后同一触发源的判定一致（同一 Run 输入，只有终结码不同）；清单未声明 `mode` 的目的按 `off`。

**需要的决定。** Q-1（§7），已决定：采纳三态，`active` 并入 `mode`，观察记录沿用派发拒绝表保留期。

**L6 注记（第 2 版增补）。**
- **N1**：`observe` 不得改变 `live` 路径的短路语义与拒绝码。前 10 道闸门在两种模式下的判定与顺序相同；单测断言同一输入在 `observe` 与 `live` 下**首个拒绝码一致**（`live` 被某道闸门拒绝时，`observe` 记录的首个拒绝也是这道闸门、这个码），`purpose_observe_only` 只出现在全部闸门通过之后。
- **N1（成本）**：`observe` 不是零成本。复评 Run 照常执行，产生真实的模型与 Run 预算成本；意图照常以 `world=real` 记录（只是派发终结于 `purpose_observe_only`）。负责人开启 `observe` 前应知晓这部分成本，成本视图照常计入。
- **N2**：`active` 并入 `mode` 是对主动触达设计稿 §2 目的字段的修订，已在 [主动触达设计稿](proactive-outreach.md) 修订记录中登记并指向本稿。

### 3.2 模型可见面的整体预算与漂移测试

**现状。** 上下文 v6 对 context pack 有 token 预算与字节上限测试（`tests/test_context_pack_v6.py`：`mandatory_exceeds_budget`、`section_quota`）。但模型实际读到的还有工具描述、Action 契约文本、人设节、复评指令（`plan_reevaluation.py` 中 8192 字符的指令）。这些分散在代码与可信配置里，没有一个测试把“某个角色的 Run 在一次请求里能看到的全部文本”作为整体对照 Schema、术语表与大小预算。

**借鉴点。** dsh 的 `tests/model-surface.spec.ts` 检查模型读到的一切（工具、提示词、技能、输入卡片）与工具 Schema、环境变量、术语表一致，并按角色设总大小预算；`tests/cards.spec.ts` 逐文件检查内置文本必须陈述的规则；`tests/docs.spec.ts` 检查编号文档只引用更早的文档、架构文档的表列出每个模块与测试。

**落地方案。**
- 新增 `tests/test_model_surface.py`：对每类 Run（消息回复、计划复评、兜底回复、主动触达）用确定性 provider 组装一次完整请求，断言：工具名与 `provider-tool-names.ts` 一致；每个工具描述提到的 Action 都在当前已发布目录里；人设节、Action 契约、复评指令与 context pack 合计不超过该 Run 类型的 `input_token_budget` 减 `framing_reserve`；指令文本中引用的术语在 [工作台术语表](workbench-glossary.md) 或 [世界模型术语表](world-model-glossary.md) 中有定义。
- 请求快照（NX-023）已记录工具列表与版本；测试从同一组装路径取数，不另写组装逻辑。
- 文档侧：`docs/handoff/tools/validate_handoff.py` 已校验 planning；可加一条本地检查，`docs/ARCHITECTURE.md` §4 的模块索引覆盖 `packages/eios-core/src/nexloop_eios/` 下每个模块（允许显式豁免清单）。

**验收要点。** 新增一个工具或改一段指令导致超预算或引用未发布 Action 时测试失败；豁免清单为空或每项有理由。

**需要的决定。** 不需要负责人决定，属普通实现细节（16_ADR “普通实现细节”）。

### 3.3 工作台总览“先列待人工的事”，升级问题结构化

**现状。** 工作台有统一审批（ADR-039）、补救意图（ADR-040）、告警、接管、联系限制与回复升级（`contact-restriction-view` 的 `escalations`）、候选定义审核，各自成页。Run 的 `run-outcome` 契约允许 `escalate` / `needs_information`，但只带 `reasons`（最多 8 条自由文本）与 `evidence_refs`，没有“要问谁、问什么、可选答案是什么”的结构。

**借鉴点。** dsh 的哲学：面板先列待人工决定的事，健康状态不占空间；Agent 一次只问一个具体问题，用结构化字段（`human_action`）提出，宿主把问题原样转到提问所在的会话；“等待人工”是从结构化事实推出的，不从报告自由文本推断。

**落地方案。**
- **总览**：新增或改造工作台首页为“待我处理”：按角色聚合待批审批、待处理补救建议、待确认未送达、待审核候选定义、回复升级、接管到期，每类只显示数量与最老一项的等待时长；全部为零时首页只显示一行“没有待处理事项”与最近一次健康检查时间。数据来自现有读投影，不新增写路径。
- **结构化问题**：`run-outcome` 契约新增可选 `question` 对象（仅 `kind ∈ {escalate, needs_information}` 可带）：`audience ∈ {owner, operator}`、`text`（≤ 300 字）、`options`（0–4 个短选项）、`subject_ref`（对象引用）。服务端把它登记为一条待人工事项，出现在总览与对应客户页；负责人的回答经受治理人类 Action 写回，作为下一次复评的输入。契约只加可选字段，旧 Run 输出不受影响。
- 事项的“已处理”由结构化事实判定（审批已决、问题已答、补救已采纳或驳回），不由任何自由文本判定。

**验收要点。** 一个带 `question` 的 `escalate` 结果在总览出现一条、在客户页出现一条、回答后两处同时消失且下一次复评上下文包含回答；无待办时首页只有一行；`question` 缺失时行为与现状逐字节一致（钉住）。

**需要的决定。** Q-2、Q-3（§7），已决定。Q-2：`audience` 由 Run 声明；`operator` 只答运营类问题，涉及预算、解除限制、擦除的只能 `owner` 回答；回答权限由服务端按角色能力表强制，**不信任 Run 的 `audience` 声明**（声明只决定展示位置，不授予回答权限）。Q-3：“待我处理”替换现有首页，原首页内容移入“健康”标签，旧首页路由保留并跳转。

**L6 注记（第 2 版增补）。**
- **N3**：人的回答按“用户陈述”分类入库（与事实、假设区分），保留来源与时间，可撤回、可擦除，撤回与擦除按现有更正 / 删除传播规则影响后续复评上下文。`run-outcome` 增加 `question` 属 Schema 变更（`packages/contracts/`），K4 走功能分支与 PR。

### 3.4 恢复前把“所依据的配置已变化”显式告知 Run

**现状。** 派发时复查当前权限与控制 revision（AGENTS.md、ADR-028），意图冻结 `purpose_version`、`rule_version`，目标改版推进控制 revision 并触发复评（NX-022 / NX-024）。覆盖已很完整。缺的一点：计划在 `reassess_at` 等待数天后恢复复评时，Run 拿到的是最新事实，但**没有被明确告知“你上次计划时依据的可信配置（目的清单、联系规则、人设版本、审批策略）在等待期间变了哪一项”**。

**借鉴点。** dsh 的 Mandate 记录 Policy 版本；Policy 变化不撤销工单，而是向受影响的工单投递一条“policy-changed”输入，说明变了哪条规则、请求者是否仍可发起、主动工作是否仍被接受；恢复前再 revalidate。

**落地方案。**
- 计划（`runtime.nexloop_plans`）在每次 Run 写回时记录其依据的可信配置版本集合（已有的 `purpose_version`、人设版本、联系规则版本、审批策略版本）。
- 复评 Run 的 context pack 的“必须”层新增一个确定性生成的小节“自上次计划以来变化的配置”：逐项列出变化的配置名、旧版本与新版本、一句差异摘要（取可信配置发布时的 `summary` 字段，不由模型生成）。无变化时该节为空，不占预算。
- 不改变任何授权判定：这只是输入，派发时复查照旧。

**验收要点。** 配置发布后恢复的复评 Run 的请求快照含该小节；配置未变时逐字节与现状一致；小节大小计入预算且在 `mandatory` 层。

**需要的决定。** 不需要负责人决定，属普通实现细节；但触及 context pack 的 `mandatory` 层，由 L6 复核预算影响。

**L6 注记 N4（第 2 版增补，已核实）。** 上文“取可信配置发布时的 `summary` 字段”的前提**当前不成立**。按 `git grep` 核实（基于 44cd5046）：
- 通用发布记录 `control.nexloop_configuration_publications`（迁移 0050）只有 `manifest_id`、`manifest_digest`、`configured_at`、`authority_revision`、`public_manifest`、`secret_fingerprint` 等列，**没有 `summary` 列**，此后也没有迁移给它加列；
- 目的清单 `control.nexloop_outreach_purpose_lists`（迁移 0236）只有 `version`、`definition`、`definition_digest`、`published_by`、`published_at`；`definition` 内有一段自由文本 `decision`，说明整份清单的来由，不是相对上一版的差异摘要；
- 全部迁移中 `summary` 只出现在成本视图动词与进程采样小时汇总，与配置发布无关。

因此 **K3 增加前置**：受影响的各类可信配置在发布时记录一条差异摘要（发布者填写、随版本只增不改、长度受限），由新迁移增加，不修改已发布迁移。在此之前，“变化的配置”小节只列配置名与新旧版本号，不给摘要，也不由模型补写。

### 3.5 设计稿与 ADR 增加“被否决的做法”一节

**现状。** ADR 记录采纳与代价；设计稿记录拍板。否决项散落在复核记录与对话里，后续讨论常重新提出。

**借鉴点。** dsh 的领域文档有 “Rejected concepts” 表：否决了什么、为什么。

**落地方案。** 设计稿模板增加 “N. 否决的做法” 一节，格式为两列表（做法 / 否决理由，附拍板日期）；新 ADR 的“理由与代价”后加一行“否决的替代”。本稿 §5 先按此格式写。不回填历史文档，只对新文档要求。

**验收要点。** 文档检查（§3.2 文档侧）对 `docs/design/*.md` 新文件要求存在该节；历史文件豁免。

**需要的决定。** 不需要。

## 4. 两条工程细节，待核对后决定

| 细节 | dsh 的做法 | NexLoop 待核对 |
|---|---|---|
| 队列行按 (kind, key) 合并，带 `generation` 计数 | 同 key 再次入队替换载荷、置为立即到期但保留队列位置；`generation` 递增，正在运行的尝试完成后不会删除更新的载荷 | Effect Worker 与后台服务的待办表（`effect_dispatch.py`、`plan_reevaluation.py` 的“同一计划只保留一条待办复评”）是否区分“覆盖待办载荷”与“运行中尝试完成后的删除”；若用 `FOR UPDATE SKIP LOCKED` 领取并带 fence，可能已等价 |
| “稍后再试”与“失败”分开 | 抛 `Later` 表示现在不能做（暂停、槽位满、来源关闭），重排不计失败次数；真失败才计数并指数退避，连续三次才上报健康 | 派发拒绝码中 `real_dispatch_closed`、`quiet_hours`、`budget_exhausted` 等是否计入失败重试计数与告警；若计入，勿扰时段会制造假告警 |

核对由实现线在 §8 的 K0 完成，结论写回本稿。第 2 版时 K0 已派给 L7，结论尚未返回，以下为占位，不作推断：

- **第一条（队列合并与 `generation`）**：K0 结论（待 L7）。
- **第二条（“稍后再试”与“失败”分开）**：K0 结论（待 L7）。按 L6 注记 N5，结论须列出受影响的告警规则 `rule_id`（若拒绝码计入失败计数或告警，列出会因此误报的规则；若不计入，写明依据）。

## 5. 否决的做法

按 §3.5 的格式：做法 / 否决理由，附拍板日期。

| 做法 | 否决理由 | 拍板日期 |
|---|---|---|
| vendored 其 `src/core` 为 `packages/` 下的新包 | 领域对象不对应；需重写为 Python / SQL；价值在规则 | 2026-11-01 |
| 以其 Executor 模型让 Agent Host 获得 shell 与目录 | 违反“Runtime 无生产 Shell / DB / 渠道凭据”；其文档自认不是安全边界 | 2026-11-01 |
| 引入常驻的 LLM 主 Session 做路由 | 违反“Run 短生命周期、不设常驻 Agent”；NexLoop 的路由由 SQL 预检确定性完成 | 2026-11-01 |
| 用它作 NexLoop 自身的开发代理 | 以 GitHub 为控制面，与本地 CI 权威冲突；需 DeepSeek Harness 运行时；原型成熟度不足 | 2026-11-01 |
| 把 observe 模式做成单独的派发路径 | 主动触达设计稿已定“所有检查放在现有派发检查链上，不新增派发路径”；observe 只是链尾的终结码 | 2026-11-01 |
| 让模型生成“配置变化摘要”（§3.4） | 摘要必须来自可信配置发布时的字段，模型生成会把推测混进“必须”层 | 2026-11-01 |

## 6. 不吸收的部分（有意不做）

- **“每条消息一句话、不主动确认”的消费者沟通风格。** dsh 面向开发团队，少说话是价值；NexLoop 面向消费者，ADR-023 要求每条来信都有回复，沟通风格由人设稿决定。只在负责人通知与告警上适用“只在必须行动或可用结果时才说”，这一点 §3.3 已覆盖。
- **Subscription 扇出与 follow-up 所有权。** 对应到 NexLoop 是“客户回复由哪次计划 / 承诺接手”，现状由会话流、回复绑定来信（0079）与复评触发覆盖；不另引入订阅概念。
- **主 Session 的 `noWait` 守卫。** Run 预算（8 轮 / 12 次工具 / 300 s）已经限制等待。

## 7. 负责人的决定（已决定）

第 1 版列出四个问题与建议；负责人于 2026-11-01 按 L6 推荐拍板如下。

| 编号 | 问题 | 决定 |
|---|---|---|
| Q-1 | 主动触达目的的 `mode` 三态（off / observe / live）是否采纳；observe 的观察记录保留多久 | 采纳三态；`active` 并入 `mode`；观察记录沿用派发拒绝表现有保留期，不单设 |
| Q-2 | `run-outcome` 增加可选 `question`，由谁回答 | `audience` 由 Run 声明；运营只答运营类问题（延期、确认履约）；预算、解除限制、擦除只由 `owner` 回答；服务端按角色能力表强制，不信任 Run 声明 |
| Q-3 | 工作台“待我处理”首页是否替换现有首页 | 替换；原首页内容移入“健康”标签；保留旧路由并跳转 |
| Q-4 | §3.1 至 §3.4 的实施排期 | §3.1 并入主动触达实施；§3.2、§3.5 与 K0 立即做（已派 L3 做 K1、L7 做 K0）；§3.3、§3.4 另立任务 |

## 8. 实施切片建议（立项时细化）

| 切片 | 内容 | 前置 |
|---|---|---|
| K0 | §4 两条核对，结论写回本稿 | 无 |
| K1 | §3.2 `test_model_surface.py` 与文档模块索引检查；§3.5 模板 | 无 |
| K2 | §3.1 目的 `mode` 与 `purpose_observe_only`、观察视图 | 主动触达实施切片 |
| K3 | §3.4 计划记录配置版本集合、context pack “变化的配置”小节 | NX-023 上下文 v6；发布时记录差异摘要（N4） |
| K4 | §3.3 `run-outcome.question`、待人工事项登记与回答 Action、总览首页 | Q-2、Q-3（已决定）；Schema 变更走功能分支与 PR（N3） |

每个切片按 AGENTS.md：代码、测试、证据、状态更新，先本地 CI；K2 触及可信配置与迁移，走功能分支与 PR。
