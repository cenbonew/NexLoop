# NX-041 总说明：模拟世界与 Action 替身（第七批，`935aaf62`）

- **用途**：把 NX-041 各切片的实现汇总成一份，便于运维、负责人和下游（NX-042、NX-043）查阅。**正文细节以各切片文档为准**，本文只做整合与链接。
- **设计**：[NX-041 设计稿](NX-041-design.md)，D1–D13 已定案。
- **切片文档**：
  - [S1 世界校验](NX-041-S1-implementation.md)
  - [S2 世界的建立与删除](NX-041-S2-implementation.md)
  - [S3 合成与结构生成](NX-041-S3-implementation.md)
  - [S4 Action 替身与模拟台账](NX-041-S4-implementation.md)
  - [S5 模拟时钟](NX-041-S5-implementation.md)
  - [S6 进程组与 Host 隔离](NX-041-S6-implementation.md)，以及出口规则示例 [NX-041-S6-simulation.nft.example](NX-041-S6-simulation.nft.example)
  - [S6b Host 效应工具接替身](NX-041-S6b-implementation.md)
  - [S6c 模拟专用数据库角色](NX-041-S6c-implementation.md)
  - [S7 消费者端口与 AT-046 验收](NX-041-S7-implementation.md)
  - [S7b 模拟 Run 的上下文来源](NX-041-S7b-implementation.md)
  - [Host 模拟上下文协议](NX-041-host-sim-context.md)
  - [按引用登记与 manifest](NX-041-sim-context-ref-implementation.md)
  - 参照：[AT-046 凭据守卫](AT-046-simulation-credential-guard.md)
- **迁移**：0259–0275。编号以 catalog 为准，各切片文档里的编号是临时编号。

## 1. 切片一览

| 切片 | 交付 | 迁移 |
|---|---|---|
| S1 | 统一的 world 校验：`real`、`test`、`sim:<uuid>`、`shadow:<uuid>`；`simulation` 从此只作为数据模式（D1、D9）。Python 与 SQL 用同一套规则 | 0259 |
| S2 | 模拟世界登记表、建立与删除 Action、每个世界的凭据规则、整体删除与 30 天摘要 | 0260 |
| S3 | 合成或结构生成：只在 `sim:` 世界，只由负责人在 real 中发起；同一（类型、种子）只生成一次，每个世界最多 10 次 | 0261 |
| S4 | Action 替身目录、替身配置、模拟意图与回执台账、替身执行器；real 的效应表拒绝模拟行 | 0262 |
| S5 | 每个世界的模拟时钟；承诺到期、计划复评、外联受理与审批读取世界时间 | 0263 |
| S6 | 世界进程组、按世界划分的 Pi 目录与文件清除、模拟预算、模拟 compose 项目 | 0264、0265 |
| S6b | Host 的效应工具在模拟世界中改走替身（没有迁移） | — |
| S6c | 模拟专用数据库角色，最小权限 | 0268 |
| S7 | 消费者端口（孪生桩）与 AT-046 完整验收 | 0266 |
| S7b | 模拟 Run 的上下文包：只有模拟来源，渲染为“模拟” | 0267 |
| Host 协议 | 模拟 Host 只接受模拟上下文协议，有单独的运行说明 | 0269 |
| 按引用登记 | 上下文包在登记时固定，按引用传给 Run；manifest 记录每个来源 | 0270 |
| 预算回填 | S6 之前建立的世界补上模型预算行 | 0271 |
| 世界上限 | 每个租户最多 20 个活动的模拟世界；有效期最长 30 天，到期后由保留清理整体删除 | 0272 |
| D10 三凭据 | 每个模拟世界按用途（api、worker、agent）各一个凭据；sim 进程组可以跑完整的 Pi Run | 0273 |
| D10 受信配置 | 受信配置的 manifest 签发 sim 凭据时带上用途 `sim_use`（部署 D10 前必须） | 0274 |
| D10 授权复核 | 新入口加上 S6c 的第二道防线（模拟角色出了本世界即拒绝），每项授权都有必要性说明 | 0275 |

## 2. 模拟世界的生命周期

1. **建立**（`nexloop.sim_world.create:1`）：世界 ID 由租户和请求 ID 推出，重试得到同一个世界。建立时同时建立一个**暂停**的时钟，以及该世界的模型预算。状态为 active。
2. **生成数据**（`nexloop.sim_world.generate:1`）：合成（按分组）或结构生成（按已发布定义），只读定义，不读 real 的业务数据（D8）。
3. **推进时钟**（`nexloop.sim_clock.control:1`）：推进、暂停、恢复、设置速率。
4. **运行**：模拟进程组在该世界中运行 Run，效应只经替身执行，输出写入模拟台账。
5. **数量上限与到期**（负责人 2026-10-21 定）：每个租户最多 20 个 active 的模拟世界；世界默认 30 天到期，期限不得超过 30 天，到期后自动转入整体删除（与手动删除相同的流程）。这一项随 `92291357` 实现，不在 `935aaf62` 中。
6. **删除**（`nexloop.sim_world.delete:1`）：状态依次为 deleting → deleted，只能前进，不能删行。删除分批执行，可以中断后继续；先取快照，再清除；保留 30 天摘要（只有计数与 digest，保留类别 `sim_world_summary`）；写入删除台账；各 Host 确认文件已清除后才算完成。

四个 Action 都只有负责人能执行（工作台角色清单 v29 的 `sim_worlds` 范围，属于负责人专属集合）。负责人的用法见 [负责人操作指南 §5](../operator/workbench-owner-guide.md#5-模拟世界)。

## 3. 凭据与角色

- **每个世界的凭据（D10）**：
  - 原定案（负责人 2026-10-20）：一个服务凭据，`worlds` 恰好是该世界，登记后不可修改，世界删除时吊销。S2 用触发器和唯一索引强制；
  - **D10 已定案（负责人 2026-10-21 放宽为三凭据），实现中**：完整的 Pi Run 需要同一世界的三个服务凭据，所以每个模拟世界改为三个凭据（api、worker、agent），每个只绑定这一个世界，删除世界时一并吊销。各切片文档里“D10 待定”的说法都已加注为这一定案。
- **放宽实现之后的接线顺序**：
  1. `nexloop_sim_api` 为 Run 签发 Run 凭据；
  2. 调用 `sim_context_register`，得到固定的 `pack_text`；
  3. 用这份 `pack_text` 作为输入登记 Run（输入 digest 不一致时以 `input_not_registered` 拒绝）；
  4. Host 解析并渲染这个包，guard 核对输入 digest。
- **数据库角色**：

  | 角色 | 用途 |
  |---|---|
  | `nexloop_stand_in_worker` | 替身执行器，只能执行替身的领取、执行与查询 |
  | `nexloop_sim_files` | 模拟文件清除 |
  | `nexloop_sim_worker`、`nexloop_sim_guard`、`nexloop_sim_api` | 模拟进程组的 worker、guard 与 API，最小权限（S6c） |
  | `nexloop_twin_port` | 消费者端口，只能访问端口 |

- 角色口令按部署说明经钥匙串保存，连接文件权限 0600，详见 S6、S6c。

## 4. 替身与台账

- **替身目录**（D11）：服务请求（HTTP）、服务请求（本地 JSON）、优惠发放、外联发送，版本都是 1.0.0。没有替身的外部效应在模拟世界中不可用（fail closed）。
- **替身配置**（D12）：默认全部成功、零延迟；故障配置必须显式写入，写入后冻结；`outcome_seed` 保证重跑结果一致；可以把冻结的配置复制到新世界重跑。
- **台账**：模拟回执要求 `simulated=true`、`output_kind='simulated_output'`；real 的 20 张效应表用约束拒绝模拟行。
- **执行器**：替身执行器不导入任何网络库；模拟进程组的出口规则只允许访问 DATA_HOST 的 PostgreSQL 与模型 provider 的 443 端口，其他新连接一律丢弃（出口规则示例见 S6）。

## 5. 模拟时钟

- 每个世界一个时钟（`runtime.nexloop_world_now`、`runtime.nexloop_world_wall_at`）。
- **读世界时间的逻辑**：承诺到期与违约、承诺与计划的 feed、计划复评到期、外联的受理、审批与过期、外联的模型预算闸门与指标。
- **仍用墙钟的逻辑**（有意保留）：租约、审计、凭据有效期、世界到期、30 天摘要、预算周期。
- **回复时限**：相关表只允许 real，模拟世界中按 D5 禁用。

## 6. 进程组隔离

- **两个进程组**：`real` 组服务 real 与 test；`simulation` 组每次服务一个 `sim:` 或 `shadow:` 世界。跨组的 Run 以 `invalid_run_world` 拒绝。
- **部署**：`deploy/stage/compose.simulation.example.yaml`，独立 compose 项目，默认关闭，有自己的网络，只使用模拟专用的连接文件；有独立的签名密钥 ID。
- **模型预算**（D6）：每个世界 100 元，按租户每月合计不超过 100 元（UTC 月），超限以 NXB01 拒绝。设计要求独立的模型账号与模型配置文件。
- **mode 比较**：服务端拒绝伪造的 mode；模拟 Host 比较进程组，并且只接受模拟上下文协议。

## 7. 上下文来源

- 模拟 Run 的上下文只来自：模拟对象、模拟人设和端口事件。每一条都标为 `source: simulated`，有数量上限（例如 256 个事件）。目标、供给和关系三节渲染为空。
- **按引用登记**：上下文包在登记时固定；manifest 按顺序记录每个来源；渲染版本写在 Run 绑定行上；只有 `nexloop_sim_api` 能登记。

## 8. AT-046 覆盖矩阵

| 负例 | 测试（S7 及以前） | 状态 |
|---|---|---|
| 伪造 world 进入 6 个服务端入口、跨世界凭据、进程参数 | `test_at046_simulation_isolation_pg.py`（v0.1，8 个测试） | 通过 |
| 伪造 mode 被服务端拒绝 | `test_at046_a_forged_mode_is_refused_by_the_server` | 通过 |
| 模拟凭据不能认证 real | `test_at046_a_sim_credential_never_authenticates_real` | 通过 |
| real 的效应路径拒绝模拟世界，由替身服务 | `test_at046_the_real_effect_path_refuses_a_sim_world_and_the_stand_in_serves_it` | 通过 |
| 模拟输出不能成为事实或已履行的承诺 | `test_at046_simulated_outputs_never_become_facts_or_fulfilled_commitments` | 通过 |
| 端口角色只能访问端口 | `test_the_port_role_reaches_the_port_only` | 通过 |
| 跨进程组的 Run 被拒 | Host 进程组测试 | 通过 |
| 上下文与恢复的伪造 | S7 评审补充的负例 | 通过 |
| 替身零外部调用、整体删除、冻结输入可复现 | `test_acceptance_*` 三项（设计 §8 建议新增） | 通过 |
| **模拟进程组跑完一次完整的 Pi Run** | `test_at046_a_simulation_process_group_runs_a_complete_pi_run`（strict xfail） | **未通过**：D10 已定案（三凭据），实现中；还要 S6c 角色与 Host 协议的接线 |

AT-046 在 planning 中的状态由调度员登记；按上表，还差完整 Pi Run 这一项。

## 9. 剩下的前提与未完成项

1. **D10 已定案（负责人 2026-10-21 放宽为三凭据），实现中**；实现后按 §3 的顺序接线，然后取消上面的 xfail。
2. `nexloop_sim_api` 的入队服务还不存在。
3. 部署时的手工步骤：pg_hba、Host 清除密钥登记、签名密钥登记、出口规则（详见 S6、S6c）。
4. 模拟世界中的外联闸门，以及孪生的 Host 效应工具：推迟到 NX-042。
5. 上下文中的目标、供给与关系三节：推迟到 NX-042、NX-043。
6. 每个租户的世界数量上限（20 个 active）与到期自动删除（默认 30 天，最长 30 天）：负责人 2026-10-21 已定，随 `92291357` 实现。
7. 会阻止删除的表：外联意图。
8. claim 契约的 `world_id` 格式与 Host 适配器的检查还没有接上新规则。
9. 模拟的履约证据：推迟到 NX-042、NX-043。
10. 负责人操作四个 Action 的命令行工具 `scripts/deploy/sim_world.py` 已由 L2 完成（`875c3c7c`，用法见操作指南 §5）；工作台仍没有对应页面。
