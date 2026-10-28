# 外联第 5 片 stage 验收的两个前置缺口：G-1 执行器服务、G-2 Host 运行配置（调度员 2026-10-28）

依据：L4 执行单 `docs/release/run-outreach-slice5-stage.md` §0（分支 `l4/outreach-s5-stage` 3bf31a57）。负责人 10-28 接受“Agent 主动外发在 stage 上可用”，要求把第 5 片验收提前。本分支 `l7/outreach-executor` 基于 main 17aa74c1，不需要迁移。

## G-1：外联执行器 `nexloop-outreach-sender`

### 做了什么

- **服务入口**：`nexloop_eios.background_services` 新增 `outreach-sender`（`nexloop-outreach-sender`，数据库角色 `nexloop_domain_worker`），与 `incentive-issuer` 同一套进程约定：
  - 只从显式给出的私有文件读 DSN、签名密钥和服务凭据，不读环境变量里的秘密；每个 tick 重读凭据并重新认证，撤销立即生效；
  - 不迁移数据库；启动时核对数据库角色必须是 `nexloop_domain_worker`；
  - `--once` 跑一个 tick，打印一行只含计数的 JSON（`admitted`、`refused`、`fulfilled`、`failed`、`unknown`），与 `OutreachSender.run_once()` 的返回值相同；常驻时失败只记异常类别和次数（`TickFailures`），不打正文、不打参数。
- **逻辑沿用已测过的 `nexloop_eios.outreach.OutreachSender`**，没有改动：
  - 先由 SQL 准入（`authz.nexloop_outreach_command` 的 `admit`）：联系限制、请求方当前权限（Agent 发起时 Role 上限仍须含发送）、负责人控制，再过目的、营销资格、测试人设、勿扰、频率、消费者偏好、三层真实派发开关、模型预算。拒绝是终态，provider 收不到任何东西；
  - **持久化后再确认**：准入把意图改为 `dispatching` 并发放 fence，这一步提交之后才调用 provider；provider 的回答再以 fence 写回（`observe`），之后才处理下一条；
  - **未知结果先查询**：provider 超时或断开记为 `unknown`。下一次 tick 只按意图 id 查询（`query`），从不重发；查不到（回环 outbox 写入前就丢失）是确定的失败；仍然未知就保持 `unknown`；过期的 `dispatching` 租约同样只查询。
- **provider 只有回环 outbox**（`LocalOutreachOutbox`）：每个意图一条 fsync 过的 JSON 记录，只存正文的摘要，不存正文。目录由 `--outbox-directory` 指定；已存在时必须是本用户的真实目录、没有组和其他人的任何权限（`private_directory`），否则启动即失败。
- **硬约束：provider 只能是回环 outbox**（L6，调度员 2026-10-28）。三层真实派发开关都打开时，这是挡住对外发送的唯一一道：
  - `--provider` 默认 `loopback`。其它任何值，没有 `--real-channel-enabled` 就拒绝启动；带了也拒绝，因为本版本没有任何真实外联 provider，真实渠道要另做一次评审过的改动；
  - 这道检查在读取任何凭据、打开任何连接之前执行；
  - stage compose 不传 `--provider` 和 `--real-channel-enabled`，只分发执行器自己的 4 个 secret 加签名密钥和 PG CA，不挂任何渠道凭据（`tests/test_stage_compose.py::test_the_stage_compose_gives_the_sender_no_channel_credential` 守着）。
- **秘密**：只有数据库连接（DSN 与 PG 客户端证书）、签名密钥和自己的服务凭据。回环 outbox 没有渠道密钥，执行器也不持有任何渠道凭据、模型凭据或其他服务的凭据。
- **授权**：
  - `service-grants` **v22**：新主体 `outreach_sender`（`nexloop_domain_worker`），只有一条授权 `eios:action:nexloop.outreach.execute:1`；不持有发送、提交或任何人工 Action；
  - `stage-principals` **v7**：凭据文件 `outreach-sender/service-credential` 对应 `outreach_sender`；
  - **编号说明**：main 17aa74c1 上分别是 v20、v5；v21（退役 relation_backfiller）与 stage-principals v6 在第 47 批（31884a17）。本分支直接写成 v22、v7。合并第 47 批与本分支时，`manifest_version` 与 `decision` 两行会冲突：取 v22 / v7，`decision` 保留 v21 / v6 的那一句再接本分支追加的一句；第 47 批删除 relation_backfiller 的主体、授权和凭据映射照常保留。
- **部署件**：
  - `deploy/stage/compose.app.example.yaml`：服务 `outreach-sender`，沿用 `x-service` 锚点（`network_mode: service:app-net`、`read_only: true`、`cap_drop: [ALL]`、`no-new-privileges`、只有 `/tmp` 的 tmpfs）；只挂自己的 4 个 secret 文件和签名密钥、PG CA；卷只有 Artifact 目录和 `${NEXLOOP_ROOT}/data/outreach-outbox`；
  - `deploy/community/compose.test.yaml`：同名服务（profile `background`）与卷 `outreach_outbox`；
  - 容器入口 `container_entrypoint.py`：任务 `outreach-sender`，outbox 在 `/var/lib/nexloop/outreach-outbox`；
  - `deploy/stage/connection-budget.v1.json`：`outreach-sender` 1 个进程、pool 1；
  - `deploy/stage/deploy-gate.app.example.json`：容器用户 10001；
  - `deploy/examples/host-health-install.app-host.example.json`：期望容器 `nexloop-app-outreach-sender-1`。
- **stage 上线前要做的（调度员按 ack-policy）**：
  - 建目录 `${NEXLOOP_ROOT}/data/outreach-outbox`，0700，属 10001。若由 Docker 自动创建，目录属 root，执行器会按上面的检查拒绝启动，不会在别人可读的目录里写；
  - 用 `stage_secrets` 生成并发布 `secrets/outreach-sender/` 下的 4 个文件；
  - register 应用 service-grants v22 与 stage-principals v7；
  - APP_HOST 的 host-health 配置按新示例重装，才会把这个容器列入期望。

### 测试

见本分支提交说明与回报：`tests/test_background_services_pg.py`（空 tick 的计数、受理意图在派发时被拒且 outbox 为空）、`tests/test_outreach_send_pg.py`（部署入口发送一次、输出只有计数；未知结果经入口只查询、不重发；outbox 目录权限），以及所有涉及 compose、授权清单和服务列表的测试。

## G-2：stage Host 运行配置打开 `outreach_tool` 与 `plan_outcome_tool`

`deploy/stage/agent-host.runtime-config.example.json` 增加 `"plan_outcome_tool": true` 和 `"outreach_tool": true`。Host 只接受 `true`，并要求 `effect_tools: true`；`outreach_tool` 还要求 `plan_outcome_tool`，否则 Host 拒绝启动（`runtime configuration refused`）。

**打开后的效果**：

- `plan_outcome_tool`（**行为变化，部署时由调度员按 ack-policy 处理**）：Host 给 Run 提供 `nexloop.plan.outcome` 工具。影响：
  - 以前 stage 的计划复评 Run 没有这个工具，无法记录结论；打开后，模型可以在每次复评时记录一次结论；
  - `plan_update` 会用新步骤替换计划（计划版本前进），`reassess_at` 会设定下次复评时间（仍受 `min_reassess_seconds` 与 `self_trigger_cap` 约束）；`escalate` 会升级给负责人；
  - 结论只经 guard 与 SQL 写入，SQL 校验 Run、计划和当前计划版本；不直接改业务对象，也不发送任何东西；
  - 关闭方法：从运行配置删掉 `plan_outcome_tool`（同时必须删掉 `outreach_tool`，否则 Host 拒绝启动），重启 Host。
- `plan_outcome_tool` 的工具说明：计划复评 Run 用它记录一次结论（`no_action`、`needs_information`、`waiting_external`、`escalate`、`plan_update` 替换计划步骤、`action_intent` 指向本 Run 已提交的意图）；记录走 guard 与 `authz.nexloop_record_plan_outcome`，SQL 校验 Run 与计划版本（NX-024）。**这是行为变化**：stage 上的计划复评 Run 从此能改写计划和设置下次复评时间，需调度员确认（执行单 §0 G-2 已注明）。
- `outreach_tool`：只给非消息 Run（计划复评 Run）提供 `nexloop.outreach.send`，参数只有目的和正文。工具调用经 runtime worker 的 `nexloop.outreach.run_submit:1` 受理，SQL 仍要求：是计划复评 Run（兜底 Run 一律拒绝）、Run 凭据与 Role 上限都含 `nexloop.outreach.send:1`、目的已启用。**受理不等于发送**：发送只由 G-1 的执行器在派发时决定，三层真实派发开关、联系限制、频率和测试人设都在那时再查一次。
- 两项都不打开任何真实渠道：外联目前只有回环 outbox。
