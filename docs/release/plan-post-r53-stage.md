# 总执行顺序：release 53+54 部署之后的 stage 验收运行单

- **执行人**：调度员。**判定人**：L6。各单的证据里两者分开写，测试、演练和真实结果也分开写。
- 本文只排顺序、写清依赖与互斥，不重复各单的命令。具体步骤以各单为准；本文与某张单冲突时，以本文的顺序和停止条件为准，命令和预期仍按那张单。
- 本文按第 52 批 d60a833f 只读核对源码与文档，**没有连接任何主机**。下文的耗时是按各单的步骤估计的，不是实测值。

## 0. 涉及的单与版本

| 代号 | 单 | 来源 | 写入什么 |
|---|---|---|---|
| **DEP** | `run-release53-54-stage.md`（S1–S13 部署，S14 浏览器走查） | `l7/release54-sheet` 78c38b28（还没有合入 d60a833f；d60a833f 上的 `run-release53-stage.md` 是只含 release 53 的旧版） | 迁移 0364–0371、发布、register、审批策略 v5、重建 |
| **S6** | `run-nx038-stage.md`（L2）、`run-nx039-stage.md`（L3）、`run-nx040-stage.md`（L4），三张单合并为一串 | d60a833f；合并顺序见 run-nx040 §1 | 演进候选、评估与留出集；038-9 删除一个合成 Consumer（不可逆）；040 的 r6 语义版本和可选的 r8 正式发布 |
| **BUD** | NX-038 §2.0：evolution 预算的放宽审批 | d60a833f | 一条审批与一行预算 |
| **E1** | `run-nx055-stage.md` §1：授权漂移检查 | d60a833f | 不写库 |
| **E2** | `run-nx055-stage.md` §2：崩溃循环演练 | d60a833f | APP_HOST 的探针配置（临时）和一次性容器；一次 `crash_loop` critical 告警，最长约 1 小时 |
| **REM** | `run-nx057-remediation-stage.md` | 本分支更新了 §0.1 P5 和 §0.5 | 补救规则 v1、`REMEDIATION_ENABLED`；重建 `plan-reevaluator`。规则 v1 的 `deliver_final_failure` 是自动复评：投递最终失败后唤醒等待的计划，新的复评 Run 可能再提出投递或外联意图（派发时重新过全部闸门；外联只有回环 outbox） |
| **OUT** | `run-outreach-slice5-stage.md`（L4） | d60a833f | 外联记录；s6c-2 的联系限制（要恢复） |

本分支同时改了两张单：run-nx039 里预算由谁设定的三处，改为“approver3 发起和执行，approver2 批准”；run-nx057 的 P5 和 §0.5，见 §3。run-nx040 §0 的“P3（evolution 预算，NX-038 §2.0 由 approver3 设定）”是 L4 的单，这里没有改，按 NX-038 §2.0 的现行写法执行。

release 53+54 = ad8f42dc，迁移尾 0371。下文各单前提里写的旧发布号和旧迁移尾（17aa74c1、release 50 等）都按“ad8f42dc 或之后”理解。

## 1. 依赖与互斥

### 1.1 先后依赖

| # | 依赖 | 原因 |
|---|---|---|
| D1 | 所有单都在 DEP 的 S13 promote 完成、S12 门禁为 20/21 之后开始 | 各单都以 release 53+54 为前提 |
| D2 | BUD 依赖 DEP 的 S4（发布 `nexloop.budget.loosen`）、S5（角色 v48）、S8（审批策略 v5） | 新设预算只能走 `loosen_budget`；策略 v5 没有应用时发起返回 409 `approval_unavailable` |
| D3 | BUD 还要求 approver2 已过 24 小时投票冷却 | 冷却未过时批准返回 401/403，见 §3 的核对命令 |
| D4 | S6 的所有项都依赖 BUD（P3），三张单**共用这一次**预算设定 | 开诊断要 evolution 上限 |
| D5 | OUT 依赖 DEP 的 S7（`outreach_sender` 授权）、S9（执行器部署）、S10（G-2 Host 配置） | OUT 的 P2、P3、P9 |
| D6 | E2 在 DEP 的 S11（host-health 安装，新增 `nexloop-app-outreach-sender-1`）之后 | E2 的演练配置要从 S11 写好的 `/etc/nexloop/host-health-install.json` 复制；在 S11 之前做，S11 会覆盖演练配置，或者演练收尾又把 S11 的改动撤回 |
| D7 | E1 的期望快照按 ad8f42dc 的检出生成 | E1 的 P1/P2：快照的 `catalog_revision` 要等于库的迁移尾 0371 |
| D8 | S14 在 S13 之后（DEP 自己的规定） | — |

### 1.2 互斥与顺序约束

| # | 约束 | 原因 | 做法 |
|---|---|---|---|
| X1 | **r7 的删除（038-9）在这一轮判 blocked，不做** | 10-30 起 S6 与 OUT 都只用 s6c-1/2/3（customer-1..4 的授权已撤销或已擦除，装配过期）。`erasure_consumer_id` 只能从这三个里选，而 OUT 也要用这三个；同时只有 3 个簇单元，r7 依赖的第 4 个簇单元不存在（S6 prep §2.1a，负责人 10-30：r7 一律不做） | r7 照实判 blocked，原因写“只有 3 个合成 Consumer，且都被 OUT 使用”。不等 OUT 结束后再补做：等下去也凑不出第 4 个簇单元，而删除不可逆，会让 OUT 的阳性或阴性对照之一永久失效。以后补 r7 时，要先有第 4 个不被 OUT 使用的合成 Consumer，并排在 OUT 全部完成（含 §4.4）之后 |
| X2 | **E2 期间不重装文件备份** | 文件备份安装的 R6 会改写同一个 `/etc/nexloop/host-health-install.json`，和 E2 收尾用原配置恢复会互相覆盖（run-nx055 §2.1） | 从 E2 的 2.1 到 2.5 第 4 条，不执行任何文件备份安装（APP_HOST 部分）。文件备份安装放在 E2 收尾之后 |
| X3 | **E2 期间 APP_HOST 不重建、不重启任何容器，也不重装 host-health** | E2 的通过条件之一是“现有服务没有任何重启”；REM 4.2 重建 `plan-reevaluator`，DEP 的 S9/S10 重建和重启 Host | REM 4.2 放在 E2 之前完成；E2 期间不执行任何 `docker compose up`，也不跑 `host_health_install.sh`（E2 自己的两次除外） |
| X4 | **OUT 会给 s6c-2 加联系限制，跑完必须恢复；而且必须等 S6 发言结束** | OUT §3.1 由 s6c-2 说“以后不要再联系我了。”，规则 v2 `stop-contact` 生效；s6c-2 也是 S6 的发言账号 | OUT §3.1 与 G-3（s6c-1/2 重新组装 Role、重新登记）都排在 S6 的 9 轮发言全部结束、window 固定（`date -u` 晚于 `until`）之后。OUT 只要做过 §3.1，无论结果如何都要做 §4.4（approver3 用 `release_restriction.py` 执行 `release_contact_restriction`，负责人终端一条命令），再用 §3.1 的查询确认 `active = f`。§4.4 完成之前：不用 s6c-2 跑新的合成交互 |
| X5 | **S6 的 `window` 要在 OUT 开始之前固定，并且结束时间早于 OUT §3.1** | OUT 会产生 s6c-1..3 的新 Run（s6c-2 的发言、计划复评 Run）。如果它们落进 S6 窗口，簇单元就会变化，三张单要一起换 `label` | 先用 `s6_stage_inputs.py` 生成 S6 配置并记下 sha256（run-nx039 P8），确认 `window` 的结束时间早于 OUT 开始的时间，然后才开始 OUT |
| X6 | **REM 4.2 不与 OUT §3.2 等待计划复评的时段重叠** | REM 4.2 重建 `plan-reevaluator`；OUT 靠常驻的 `plan-reevaluator` 认领到期的计划步骤 | REM 全部完成后再开始 OUT §3.2；或者等 OUT §4 结束后再执行 REM 4.2 |
| X7 | **BUD 只做一次** | 三张 S6 单共用一次预算；重复发起会在等待期间按重放返回同一条审批，但执行之后再发起会以 `budget_changed` 被拒 | 发起前先用 NX-038 P3 的查询看有没有行，有行就按 §2.0 填 `from_*` |
| X8 | **040 的 r6 在 038-10 之后；r8 的正式发布（可选，Q5）放在最后** | run-nx040 §1 | 照 run-nx040 §1 的总序 14、15 |

### 1.3 并行与串行

管理机上只有调度员一个人在操作，所谓并行，是指一张单在等待（计划复评到期、告警恢复、批准）时，可以去做另一张单。

| 可以并行（互不影响） | 必须串行 |
|---|---|
| E1（DATA_HOST，只读）与 S14、S6 的 dry-run、各单的只读前提核对 | DEP 的 S1–S13 自身 |
| E2 收尾之后等待 `crash_loop` 恢复的约 1 小时内，可以执行 S6（管理机）、BUD、OUT 的只读部分；**但不能做 X3 列出的重建，也不能做 X2 的文件备份安装** | E2 的 2.1–2.5 第 4 条（约 10 分钟，APP_HOST 独占） |
| OUT 等待计划复评（§3.2，每轮约 5 分钟）期间可以执行 S6 的 1–9 步（S6 的写入只在演进侧，不碰外联） | S6 的 0–15 步按 run-nx040 §1 的总序，不能打乱 |
| REM 的只读核对（A 步）随时可以做 | REM 第 3 步在第 4 步之前；4.2 不能与 E2、OUT §3.2 重叠 |
| | 038-9 删除：本轮判 blocked，不做（X1，10-30） |

## 2. 推荐的总顺序

| 阶段 | 内容 | 写入 | 估计耗时 |
|---|---|---|---|
| 0 | DEP S1–S13（ad8f42dc）；S13 之后记录连接数实测 | 是 | 按 DEP 单，约 1.5–2 小时 |
| 1 | S14 浏览器走查（approver3，只看）。同时在 DATA_HOST 做 E1 的 1.1–1.3（只读） | 否 | S14 约 20 分钟；E1 约 15 分钟 |
| 2 | REM：前提核对，按 §0.5 新的预期（策略 5）→ 第 3 步 → 第 4 步（含 4.2 重建 `plan-reevaluator`）。已经做过的步骤按单里的“只核对”执行 | 是 | 约 30 分钟 |
| 3 | E2：2.1–2.5（APP_HOST 独占约 10 分钟），然后等 `crash_loop` 恢复（最长约 1 小时，可以与阶段 4、5 重叠，但要遵守 X2、X3） | 是（临时） | 主动约 15 分钟，恢复等待最长约 1 小时 |
| 4 | S6 准备：生成并固定 S6 配置（X5）→ S6 的 dry-run（038 §2.1、039 §2.1、040 §3.0）→ BUD（approver3 发起，approver2 批准，approver3 执行）→ S6 总序 1–9（at046、r5、at048、r4、at023、r1 加 `--only semantic`、r3、candidate-template、at024、r10、r8） | 是（演进侧） | 配置与 dry-run 约 20 分钟；BUD 约 10 分钟；总序 1–9 约 45–90 分钟 |
| 5 | OUT：§2 只读 → G-3（s6c-1/2）→ §3.1（s6c-2）→ §3.2 → §4.1–4.3 → **§4.4 解除限制（必做）**。G-3 与 §3.1 排在 S6 的 9 轮发言结束、window 固定之后（X4，10-30）；§2 只读可以提前 | 是 | 约 30–60 分钟（每个顾客最多触发 3 次，见 OUT G-4） |
| 6 | S6 总序 10–13：038-7 `r7-prepare` → 038-8 → 038-9 → 038-10 `r7-verify`：**本轮全部判 blocked，不执行**（X1，10-30：只有 3 个合成 Consumer，且都被 OUT 使用） | — | 0 |
| 7 | S6 总序 14：040 r6；总序 15（Q5 为“执行”时）：r8 路 2 正式发布，再 `hide` | 是 | 约 20 分钟 |
| 8 | 收尾核对（§5） | 否 | 约 10 分钟 |

阶段 1–3 都可以挪到阶段 4–7 之后，但 E2 必须满足 X2、X3，E1 必须在 DEP 之后（D7）。阶段 5 不能晚于阶段 6（X1）。

## 3. 每张单的前提核对

`ro` 见 run-nx057 §0.3。核对时只记结果，不贴 DSN、口令、用户名或主机地址。

| 单 | 前提核对（摘要；完整的见各单） | 通过时看到什么 |
|---|---|---|
| 全部 | `ro "select max(version) from control.schema_migrations"` | `0371` |
| 全部 | `ro "select max(manifest_version) from control.nexloop_approval_policy_publications"` | `5`（DEP S8 之后） |
| BUD | `ro "select count(*) from control.nexloop_approval_policies where capability='goals.budget.loosen'"` | `1` |
| BUD | approver2 的投票冷却：`ro "select control.nexloop_workbench_vote_eligible_from(m.tenant_id,'real',m.subject_id,array['owner']) <= clock_timestamp() from control.nexloop_browser_memberships m where m.tenant_id='$TENANT_ID' and m.principal_id='stage-principal-approver2'"` | `t`。为 `f` 或空就不能批准：等冷却结束，或判 BUD blocked，**不改用负责人** |
| BUD、REM、OUT、S14 | approver3、approver2 的成员状态：run-nx057 §0.5 的 approver3 查询；把 `stage-principal-approver3` 换成 `stage-principal-approver2` 再查一次 | 各一行 `active|active|owner` |
| BUD | NX-038 P4 的三条查询（授权 3 行、两个 Action 已发布、策略不小于 5） | 见 run-nx038 §0 |
| S6 | run-nx038 §0 P1–P6；run-nx039 P7、P8、P9（`r4 --dry-run` 的 `units` ≥ 4）；run-nx040 Q1–Q5 | 见各单 |
| S6 | X5：S6 配置的 `inputs.evolution.window` 的结束时间 < OUT 开始时间；记下配置的 sha256 | 记录在 S6 证据 |
| E1 | run-nx055 P1–P4；`git -C <检出> rev-parse HEAD` = ad8f42dc | 快照的 `catalog_revision` = `0371` |
| E2 | run-nx055 P5–P8；另加：DEP S11 已完成，`/etc/nexloop/host-health-install.json` 已含 `outreach-sender`（`sudo grep -c outreach-sender /etc/nexloop/host-health-install.json` ≥ 1）；没有正在进行的文件备份安装 | P8 通知负责人的时间已记录 |
| REM | run-nx057 §0.1 P1–P6（P5 按本分支的新写法）、§0.5（策略预期改为 5） | 见该单 |
| OUT | run-outreach-slice5 §1 P1–P9（P2、P3、P9 由 DEP S9/S10 满足，仍要逐项核对）；P7 勿扰时段之外；X5 已满足 | 见该单 |
| S14 | DEP S13 已完成 | — |

## 4. 人工步骤用哪个合成账号

负责人的账号在所有单里都不登录、不操作，也不被选作批准人。

| 步骤 | 账号 | 说明 |
|---|---|---|
| BUD 第 1 步发起 `loosen_budget`、第 3 步执行 | **approver3** | run-nx038 §2.0 |
| BUD 第 2 步批准 | **approver2** | 不能是 approver3（会以 409 `self_approval` 被拒），也不能是负责人 |
| S6：at024、r10、r8、r6、Schema 提案的 check/publish/hide、038-8/038-9 删除 | **approver3** | 各单 |
| REM：P3 看“补救”一栏；4.3 只在租户开关被暂停时恢复；§5 工作台上的回退 | **approver3** | 补救 Action 不在策略 v5 里，approver2 不参与（run-nx057 §0.5） |
| OUT §1 P6 外联目的、§4.4 解除 s6c-2 的联系限制 | **approver3**（§4.4 用 `release_restriction.py`，负责人终端一条命令） | — |
| OUT §3.1 触发联系限制 | 合成顾客 **s6c-2**（`out-restrict.sh`，负责人终端一条命令） | 不是工作台账号 |
| S14 浏览器走查 | **approver3** | 只看，不提交 |
| E1、E2 | 不需要工作台账号 | 调度员的运维账号；E2 用钥匙串里的记录器口令 |

## 5. 停止条件：某张单 failed 时，哪些还能继续

总原则：

- 先看失败是否说明**发布本身**有问题，或者**真实效应**可能已经发生。是这两种情况就全部停下。
- 否则只停这张单，以及依赖它的单。
- `blocked`（缺前提）和“未触发”（OUT G-4）不算 failed，只影响这张单。

| 失败的单 / 现象 | 立即停止 | 还能继续 |
|---|---|---|
| DEP 的 S1–S13 任一步不符预期 | **全部**。按 DEP 的回退表处理；整体撤回要先把库还原到 0363（DEP S9a） | 无 |
| DEP 的 S14：W1–W3 任一不符 | 由调度员决定是否撤回 release。决定撤回时**全部停止**，已有结果作废（它们针对一个被撤回的发布） | 决定不撤回时，其余各单照常（都不依赖页面）；本 release 不判通过 |
| E1 有 D 类（真正的漂移） | 漂移涉及 S6 用到的角色（演进 writer/evaluator/reader、`nexloop_owner`）时停 S6；涉及 `outreach_sender` 或运行角色时停 OUT；涉及迁移角色的属主或权限时**全部停止**，报 L6 | 其余不受影响的单；E2 |
| E1 只有 R 类或 V 类 | 不停（R 类走 `IGNORED` 提交，V 类按正确的提交重做快照） | 全部 |
| E2：现有容器发生了重启 | 立刻按 E2 的 2.5 收尾；在原因查清之前，**APP_HOST 上的写入步骤全部暂停**（REM 4.2、DEP 的回退、OUT §3.2） | 只读核对、S6（只在管理机和 DATA_HOST） |
| E2 的其他失败（`restarts_1h` 为 0、`crash_loop` 未触发等） | E2 本身 | 全部 |
| BUD 失败或 blocked（401/403/409/422） | **全部 S6**（没有预算，开诊断就会失败） | E1、E2、REM、OUT、S14 |
| S6：at046 的 F2（评估凭据不是 test-only，或在 real 能认证） | **全部 S6**（run-nx039 F2） | 其他单 |
| S6：AT-023 受保护表 digest 变化（F7） | **全部 S6**，并停止 040 的 r8 正式发布与 r6，报负责人 | 其他单 |
| S6：其他单项 failed（at048、r1、r3、r4、r5、at024、r10、r8） | 只判这一项；**不跳过 038-9 的顺序约束** | S6 的其余项（每项用自己的留出集）；其他单 |
| S6：r8 的 `catalog_and_verify_unchanged` 失败（040 F5） | 040 的正式发布（总序 15）和 r6 | 038、039 的其余项；其他单 |
| S6：038-8 预检显示 persona 是 real，或 `erasure_consumer_id` 是 OUT 还要用的顾客 | 038-9（不删）。前一种：重新取 `erasure_consumer_id`；后一种：等 OUT 结束 | 其他单 |
| REM：出现任何回环以外的发送（run-nx057 4.4） | **全部停止**，同 OUT 的同类一行；另由 approver3 暂停补救 | 无 |
| REM：停止条件（积压不为 0、规则不是 v1、策略覆盖补救）或 4.2 后 `plan-reevaluator` 没起来 | REM；`plan-reevaluator` 没起来时 **OUT 也停**（OUT 靠它认领计划步骤） | S6、E1、E2（X3 仍然适用）、S14 |
| OUT：P9 不满足（执行器挂了渠道凭据，或 provider 不是回环） | OUT 不开始 | 全部其他单 |
| OUT：任何迹象表明消息到了真实渠道（outbox 之外有发送） | **全部停止**。按真实派发面板关掉租户层开关（approver3，审批策略覆盖“打开”，关闭不需要审批），报调度员和负责人 | 无 |
| OUT：V1/V3/V4/V5 某项 failed | OUT 的后续触发；**仍要执行 §4.4 解除 s6c-2 的联系限制** | 全部其他单（038-9 本轮 blocked，X1） |

## 6. 收尾核对（阶段 8，只读）

- s6c-2 的联系限制：OUT §3.1 的查询，`active = f`；
- E2：`docker ps -a --filter label=nexloop.drill=host-health` 为空；`/etc/nexloop/host-health-install.drill.json` 已删除；`crash_loop` 的恢复事件已记录；
- S6：`results_dir` 下各结果文件的 sha256；r8 正式发布时 `hide` 已执行；
- REM：4.4 的查询，包括由补救唤醒的复评 Run 数和它们产生的意图数；
- 真实派发三层开关：与 OUT P8 执行前读出的状态相同；
- 各单的证据都分开写了执行人（调度员）、人工步骤的账号和判定人（L6）。

## 7. 局限

- 本文没有连接任何主机。§2 的耗时是估计值，以第一次实测为准修订。
- DEP 引用的是 `l7/release54-sheet` 78c38b28；它合入之前如果再改，以合入的版本为准，重新核对 D2、D5、D6 引用的步骤号。
- REM 在 release 50 时已完成第 1、2 步的核对（2026-10-28，authority_revision 3197）。第 3、4 步是否已经做过，要以前提核对的结果为准；做过的只核对，不重做。
