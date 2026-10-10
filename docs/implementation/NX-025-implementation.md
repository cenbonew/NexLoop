# NX-025 知识与目标闭环测试：实现说明

验收方案见 `NX-025-plan.md`（7cd156a，调度员已审），裁定按调度员六点执行。分支 `nx025-impl`，从 main `eadd5df` 切出。迁移号 `0108` 是临时编号（调度员指定从 0108 起），合并时统一重编号。全部测试使用合成数据，在本地真实 PostgreSQL 上运行；用到 Pi 的用例使用真实 Host 和确定性测试协议。

## 1. 已实现的前置工作

### G3：复评 Run 的生产启动（NX-024 后续）
- `RoleRunLauncher.issue(decision, budget)` 改为调用生产的 Role 签发函数 `role_policies.issue_role_run`：
  - Run 凭据与 Role 策略绑定在同一事务内签发；
  - Source 自己读取 RoleDefinition 的 `ceiling_ref` 和 ConsumerRoleLink 的 `scope`；
  - 策略预算即复评预算。预算超出 Role 上限时不签发 Run，计划留在 feed 中重试。
- Run 命令里的 `goal_version_ref` 改为 Role 核心要求的 Goal 对象形式。计划所属的 NX-022 目标版本由以下三处约束：
  - precheck 时的控制快照；
  - 复评 worker 把 Run 绑定到当前目标版本（复用 0068 的 `bind_run_goal`），该 Run 提交的意图在提交时记录这个目标（0097）；
  - 结果写回时检查目标是否改版（见 §1 “结果写回”）。
- 新增后台入口 `nexloop-plan-reevaluator`：
  - 进程结构：一个 domain worker 后端，加一个 `nexloop_api` 后端专门执行 Role 启动；
  - 凭据：source、planner、queue、executor 四份凭据，每次使用时重新读取并重新认证；
  - 接线：容器入口、compose（opt-in profile `background`）、pyproject 脚本。

### G4：v6 只读 plan 段（裁定 3）
- 放置位置：Consumer 的 active 计划作为 open_work 条目放进 v6 包，不新增顶层字段。条目字段为：
  - `ref` = `nexloop:plan:<plan_id>@<version>`；
  - `subsection` = `plan`；
  - `evidence_kind` = `policy`；
  - `revision` = 计划版本号；
  - `content` = `runtime.nexloop_plan_context` 的输出（目标与策略引用、策略正文、步骤）。
- SQL 校验与必备：v6 bind 时由 SQL 重新导出条目内容并逐项比对（要求当前 active 版本、本 Consumer 的计划、调用者持有 Consumer READ），写入来源清单（content_hash 与 revision）。每个 active 计划都是必备来源；缺失时 bind 失败，除非已声明 open_work 不可读。
- 契约兼容扩展：
  - `context-pack-v6` 的 open_work `evidence_kind` 增加 `policy`，并重新生成类型；
  - 0089 的 context_sources 约束放宽为允许 open_work 使用 policy；
  - Host 侧校验同步更新，并在校验结果中返回 `plans`。
- 计划仍是 runtime 记录。v6 中的条目只是投影，不构成第二份事实存储。

### G5：受治理变更唤醒计划（裁定 4，选项一）
- 0093 的 recall-instance 标记触发器（挂在 ontology.objects 上）在写入者的事务中，同时为控制快照里出现该对象的 active 计划登记 `plan-reevaluate`，触发类型为 `object_changed`。
- 唤醒只是触发，不是决策：后续由 precheck 重新判断（NXC04 对象已变、stop_if 等）。快照外的对象不会唤醒任何计划。

### 结果写回（编写 E2E-C 时发现的缺口）
- 问题：在 0106 中，目标在 Run 运行期间改版后，该 Run 基于旧目标写回的 plan_update 仍会被接受，并直接生成绑定新目标版本的计划新版本。
- 处理：0108 改为拒绝这类写回。若计划所在目标链中有目标在 Run 被关联之后发布了新版本，结果写回返回 40001。随后由 goal_version_stale 触发的新一轮复评 Run 会绑定新版本并正常写回。

### Host 确定性复评协议（仅测试）
- 新配置项 `deterministic_plan_outcome`，取值 `no_action` 或 `action_intent`。
- 只能与 `deterministic-test`、v6、`plan_outcome_tool`、`effect_tools` 一起启用，与其他确定性配置互斥；不满足时 Host 拒绝启动，有 8 项 vitest 覆盖。
- 行为：读取包中唯一的计划条目，用固定规则写回结果；`action_intent` 模式先提交一次受治理服务意图。这不是语义推理。

## 2. 端到端测试

| 文件 | 内容 | 关联 AT |
|---|---|---|
| `test_closure_knowledge_pg.py`（E2E-A，纯 PG，约 9.5 s） | 真实 Human 消息 → 后台提取（确定性 provider）→ claim-match worker 自动写入与候选粘合 → 人工审核批准并发布 Schema → 回流 → 下一轮对话召回新定义并自动应用；注入消息不产生任何 Claim；重复提取窗口不改变下游任何计数；同一 revision 两次写入时一个成功、另一个在 40001 后重新评估 | AT-061/063/062/067/069/019/020/021 的链路版本 |
| `test_plan_role_launcher_pg.py`（G3/G4） | 生产 Role 签发与 v6 激活；预算超出 Role 上限时不签发；v6 计划条目与来源清单；删除或改写计划条目被 SQL 拒绝 | AT-007 复评部分 |
| `test_plan_wake_pg.py`（G5） | Consumer 受治理编辑唤醒计划，precheck 判定 object_revision_stale；快照外对象的受治理编辑不唤醒 | AT-007 |
| `test_closure_plan_run_pg.py`（E2E-B，真实 Host/Pi） | KR 未达成但无合理触达必要 → Pi 读取 v6 计划条目，经 Host 工具写回 no_action：零意图、零外发、计划保持 active、每次模型请求都有记录；行动路径：提交意图 → 写回 action_intent → 执行器真实派发并记录观测 → 计划因外部结果触发再次复评，真实启动下一次 Run | AT-029、AT-027（复评 Run）、外部结果触发 |
| `test_closure_refusal_versions_pg.py`（E2E-C） | C1 明确拒绝优先：strict xfail，基线应失败，见 §4；C2 暂停后排队中的意图在派发时被拒且 provider 零请求，暂停期间到期的计划不启动 Run，恢复后是新一轮复评，旧意图仍被拒；C3 Run 运行中目标改版 → 结果写回 40001、意图派发被拒，新一轮 Run 绑定目标 v2；C4 客户状态经受治理写入改变 → 计划被唤醒 → stop_if 使计划失效，不启动 Run | AT-006 派发侧、AT-007、G9 |

### 2 s 工具时限
- Mac 上 E2E-B 串行 3 轮全部通过。guard 侧单次请求最大耗时：
  - `runtime_effect_tool:submit` 881 ms；
  - `authorize:inspect` 723 ms；
  - `record_plan_outcome` 636 ms。
- 每轮运行都会打印 `NX025_GUARD_MS`（加 `pytest -s` 可见）。
- E2E-B 和 E2E-C 都是 Role Run 加 v6，与 NX-049 跟踪的 Pi Role 路径相同。按裁定 6：若在部署主机全量运行时失败，先在同一检出上串行复跑取证，再按 B 类归入 NX-049。

## 3. AT 结论建议（供调度员回填，本线未修改 planning）

| AT | 建议 | 证据 |
|---|---|---|
| AT-006 | 保持 not_run；在 evidence 中写明派发侧已通过，UI 侧归 NX-028 | `test_closure_refusal_versions_pg.py::test_pause_refuses_the_queued_intent_and_resume_reevaluates_instead_of_replaying`；`test_nx022_dispatch_e2e.py` 的暂停用例；`test_goal_controls.py::test_at006_*` |
| AT-007 | passed | `test_closure_refusal_versions_pg.py::test_goal_change_during_a_run_refuses_its_outcome_and_intent_and_rebinds_the_next`、`::test_customer_state_change_wakes_the_plan_and_stop_if_ends_it_without_a_run`；`test_plan_wake_pg.py`；`test_plan_reevaluation_pg.py`；`test_goal_controls.py::test_at007_*` |
| AT-021 | passed（受治理层 40001），HTTP 409 在 v0.1 不适用 | `test_claim_matching_pg.py::test_same_revision_conflict_one_wins_other_reassessed`；链路版本 `test_closure_knowledge_pg.py::test_same_revision_updates_one_wins_other_reevaluates` |
| AT-029 | passed | `test_closure_plan_run_pg.py::test_no_reasonable_contact_is_a_normal_no_action`；`test_plan_outcome_pg.py::test_normal_outcome_records_once_replays_and_conflicts` |
| AT-014 | 不归 NX-025（调度员新开 M09 任务，S4） | — |

## 3a. ADR-023：明确拒绝的硬性停发与“来信必回”（负责人 2026-10-10，迁移 0109、0110，临时号）

- **入站即检查（0109）**：入站消息写入的同一事务内，用版本化确定性规则检查（`deploy/configuration/contact-refusal-rules.v1.json`，迁移种入的 v1 与文件逐字一致）。命中、不确定、规则缺失、匹配出错都按限制处理：经 NX-022 控制写入器写 consumer 级 `contact_restricted` 事件（推进控制 revision），记录规则版本、规则 id 与原文片段；消息本身照常持久化、照常进中继。
- **派发**：既有 `nexloop_assert_dispatch_controls` 先跑（限制之前排队的意图因 revision 过期 NXC02 被拒）；之后 `control.nexloop_contact_assert_intent`：受限期间只放行“绑定入站来信的回复”——外发记录的 `trigger_message_id` 由服务端从消息 Run 推导，不由模型给出；同会话、同 consumer、在 `reply_window_seconds`（1800 s）内、每条来信至多一条；其余 NXC05（`contact_restricted`）。回复不解除限制。
- **解除**：只有受治理的人类 Action（`goals.contact.release`，经 NX-022 governed 入口，要求 human 主体）；Agent、服务主体持同样授权也被拒；后台 constraint Claim 只补证据。查询端口 `nexloop.contact.read`（受限客户、原因、命中证据、升级记录）。
- **来信必回（0109 + 0110，方案 A 消息路径）**：每条入站消息登记 `reply-due`，`start_after_seconds`（300 s）后到期；渠道接受的绑定回复在同一事务结清。到期未结清时，回复担保 worker 启动**至多一个**兜底回复 Run：
  - 签发 `authz.nexloop_reply_fallback_command`：独立的兜底签发表（主键=消息，至多一个；同一签发可重放，第二个为冲突），MessageAssignment 检查同 0049；在 `reply-due` 行锁下与结清互斥（已结清或未到期不签发）。原单 Run 路径不变。
  - 上下文：兜底 v6（`authz.nexloop_context_v6_fallback_command`，0105 绑定 + 0053/0062 核心，走兜底签发与独立兜底绑定表），包含这条来信、会话近况与联系限制等钉住约束。
  - 按 Run 查找签发/绑定的既有函数（激活、目录元数据、范围拒绝、Artifact 读取、v6 绑定、外发记录）以最新函数体复制、只把按 Run 查找改为兼容兜底；所有按消息查找的原路径未动。
  - 工具限制在服务端：兜底 Run 的唯一效果是绑定本来信的回复 `submit`；`find`、无回复正文的提交在 SQL 拒绝；计划结果写回由 0106 拒绝（非计划 Run）；第二条不同回复因同槽位冲突被拒。
  - 派发：兜底回复始终适用绑定回复规则；本来信已有被接受的回复时兜底回复被拒。
  - 预算小且固定（`reply-guarantee.v1.json`：2 轮/2 次工具/60 s/0.20 USD，Run TTL 120 s），生产走 LLM（`deepseek-flash`），测试经配置换为确定性 provider。
  - 失败升级：兜底无法启动（`fallback_failed`）、兜底 Run 在其最长时长后仍未结清（`fallback_unanswered`，含模型不可用/超时、回复被拒、渠道故障）、未配置 launcher（`fallback_unavailable`）都写入升级记录并留证；不退回模板。
  - 配置加载校验：`start_after_seconds + max(run_ttl, active_timeout) < reply_window_seconds`，不满足则 `load_reply_policy` 报错、`nexloop-reply-guarantor` 拒绝启动（SQL 侧同一约束为表 CHECK）。
- **测试（全部真实 PG；E2E 走真实 NX-047 链路、真实 Host/Pi，确定性 provider）**：
  - `test_contact_refusal_pg.py`：配置与种入一致；规则正例 16/不确定 4/反例 9/误判负例 7/接受的多挡 2；入站同事务限制且消息不丢、待回复登记；只有负责人能解除（Agent、服务主体同授权被拒）；查询端口与到期升级。
  - `test_contact_reply_dispatch_pg.py`：受限客户来信的绑定回复派发、送达并结清；窗口外回复被拒，零投递。
  - `test_closure_refusal_versions_pg.py` C1/C1b：Pi 故意外发被拒（`contact_restricted`），provider 零请求；限制前排队的意图同样被拒。
  - `test_reply_fallback_pg.py`：未回复来信到期 → 兜底 Run（v6 带本来信）→ Pi 一条绑定回复 → 送达结清；每条来信至多一个兜底（重复触发/重启不签第二个）、`find`/计划结果/第二条回复被拒；已结清不签发；签发后原回复被接受 → 兜底回复派发被拒；兜底仍未回复 → 升级；受限客户的来信同样得到绑定兜底回复，限制不解除。
  - `test_background_services_pg.py`：`reply-guarantor` 空转与“启动时间+兜底时长≥窗口”拒绝启动。
- **未改契约**：仓库 `packages/contracts` 中没有 Message 契约，回复绑定沿用外发记录中服务端推导的 `trigger_message_id`。

## 4. 未完成与限制

- **G2 已由 ADR-023 解决**（见 §3a），E2E-C1 去掉 xfail 并通过。E2E-C1 的夹具没有浏览器通道，入站拒绝消息按 0046 提交路径的同一表结构写入（真实提交路径由 `test_contact_refusal_pg.py` 覆盖）。
- **时间注入**：待回复到期、窗口外回复两处由 admin 前移时间（`available_at`、`accepted_at`），未改业务逻辑。
- **兜底测试的授权播种**：回复担保 worker 的授权需在会话开始前播种；会话中途播种会改变 Source 目录、使已签发的原 Run 失效（既有正确行为）。
- **reviewer 身份**：E2E-A 中审核人与发消息的 Human 是同一个合成浏览器身份。授予 `ontology.schema.review` 会替换该身份的浏览器应用事实（`grant_human`），所以下一轮对话的消息在审核之前写入，其提取与匹配在审核之后进行。
- **执行器租约**：被拒绝的派发尝试会让 outbox 保持 leased 直到租约到期，再次尝试需要等待真实到期。测试用 5 s 租约加等待，没有注入故障。
- **迁移连续性**：0108 之前缺 0107，`test_bootstrap.py::test_clean_bootstrap_and_exact_reopen` 要求迁移号连续，在本分支失败，属于编号造成的预期失败。临时改名为 0107 后验证通过（命令见报告）。
- **全量运行中的偶发失败**：首次全量运行时 `test_context_v6_relationships_pg.py::test_v6_carries_v4_relationships_with_hypotheses_as_evidence_only` 失败一次（Pi 派发），单独复跑 2 项全部通过，属于负载下的偶发，与 NX-049 同类。
- **部署主机 503（s4e 退回，已修复）**：E2E-B/C1 与兜底测试原先调用 `guard_server(worker,…)` 时没有传 `spawn`，因此即使 CI 设置了 `NEXLOOP_TEST_GUARD_WORKERS=4`，guard 也始终是单进程。Run 自身的 model/tool 授权与 50 ms 一次的 inspect 轮询（每次 inspect 两次 guard 授权，Role 链单次在 Mac 上约 0.36–0.4 s）在同一进程里按 GIL 串行，在部署主机上部分请求超过 Host 的 2 s guard 超时，Host 中止请求，inspect 返回 503。修复：三处测试按 main 的写法传入与 worker 同身份、同 Backend 配置的 `spawn`（ADR-024 的部署形态为四个 guard 进程）。2 s 时限与断言不变；单进程模式下仍记录逐请求耗时。
- **Host 构建产物**：Host 的 `dist/` 被 git 忽略，修改 TS 后需要先执行 `pnpm run build`。
