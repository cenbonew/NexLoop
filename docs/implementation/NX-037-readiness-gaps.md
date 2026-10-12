# NX-037 就绪缺口核对：从“两台主机部署完成”到“可以跑 30 次真实受控交互”

依据：`NX-037-design.md`（D1–D8，D-g/D-i）、`NX-037-prep-implementation.md`、`docs/OPERATIONS.md` §9、`scripts/nx037/real_tenant_init.py`、`scripts/nx037/run_interactions.py`、`nexloop_eios/interaction_drivers.py`（RealDriver）、`deploy/`、`planning/` 中 NX-037 / AT-015 / AT-071 / AT-072 的记录。核对基于 main a3e3a28，只读；文中只用角色名（CI 主机 / APP_HOST / DATA_HOST）。

分类：**已具备**（有代码与测试）；**缺代码**（需开发，S/M/L 粗估）；**缺配置**（需负责人提供材料）；**缺验证**（需在真实主机上验证）。

## 0. 结论

骨架基本齐全：30 次交互计划、运行入口、真实驱动、三层真实派发开关、Agent Host 独立网络命名空间、证据包。但有四处缺口即使材料齐全也会让真实试运行无法进行或失去意义，需先补代码（其中第 1、2 项已在代码中逐行核实）：

1. **币种冲突（阻塞）**：试运行预算为 CNY（`deploy/configuration/nx037-trial.v1.json:4`，`real_tenant_init.py:90` 以它作 `Budget.set` 的单位），而 Agent Host 的成本策略只接受 USD 且要求与 Run 预算币种一致（`apps/agent-host/src/pi-runtime-adapter.ts:164`），各 Run 预算也是 USD（`reply-guarantee.v2.json:15` 等）；数据库对单位不一致拒绝（`0068_nx022_goals_controls.sql` 的 budget unit mismatch）。设好 CNY 预算后每个 Run 都会被拒。
2. **WebChat 回复与企业交付共用一个 provider**：效应 worker 只持有一个 provider（`effect_dispatch.py:14-20`），Agent 回复（`nexloop.service.request:1`）和企业交付都会走它；stage Compose 里没有 WebChat 投递服务。
3. **应用侧防火墙挡住企业端点**：应用容器只允许连 DATA_HOST，其余新连接一律丢弃（`deploy/stage/firewall/app-host.nft.template:14-16`），效应 worker 到不了企业端点。
4. **开关打开的是非外部 Action**：计划的 `real_dispatch.actions` 打开的是 `nexloop.service.request:1`（`nx037-interaction-plan.v1.json:36-42`），它在分类里是 `external_effect: false`（`action-effect-categories.v2.json:7-12`），三层开关对它不起作用；企业交付 Action 尚不存在。

## 1. 企业自有幂等端点的连接器与契约测试

| 类别 | 项 | 依据 |
|---|---|---|
| 已具备 | 通用 HTTPS provider：`POST /v1/effects`（`Idempotency-Key` = 意图 id）、`GET /v1/effects/<id>` 对账；私有文件 Bearer 凭据；仅 HTTPS、数字连接地址、绝对截止时间（默认 2 s，≤30 s）、不跟随重定向、响应有界、错误脱敏 | `effect_provider.py:36-66, 127-141, 152-160, 216-252` |
| 已具备 | 对本地夹具服务器的测试：幂等、提交后超时转查询、查询不可用、`not_found`、TLS 与私有凭据、慢握手截止 | `tests/test_effect_provider_transport.py`、`tests/test_effect_provider_fixture.py`、`tests/test_effect_dispatch.py`、`tests/test_effect_worker_cli.py`，夹具 `tests/support/effect_provider.py` |
| 缺代码（L） | 按 Action 选择 provider：WebChat 回复走 NexLoop 投递，企业交付 Action 走企业端点（或按 Action 过滤的第二个效应 worker）；Compose 增加 WebChat 投递服务 | `effect_dispatch.py:14-20`，Compose action-worker 段 |
| 缺代码（M） | 企业交付 Action（`external_effect: true`）、分类 v3、服务授权，以及计划中 `real_dispatch.actions` 改为它；需负责人确认 | `action-effect-categories.v2.json:7-12`、`real-dispatch-switch-implementation.md` |
| 缺代码（M） | Agent 唯一的交付工具与服务目录写死为本地 JSON 导出（`local.json-export`、价格 0），Agent 无法请求企业交付 | `apps/agent-host/src/runtime-effect-tools.ts:56`、`context-input.ts:223` |
| 缺代码（S） | 应用防火墙增加“效应 worker → 企业端点”规则 | `app-host.nft.template:14-16` |
| 缺代码（S–M） | 企业可对自身端点运行的一致性检查（字段、POST 200/202、GET 404 `not_found`、幂等重放、`payload_digest` 回显、长度与凭据字符集）：未找到 | — |
| 缺代码（S） | 3 次 `unknown_reconciliation` 交互要求“负责人让结果暂时未知”，没有对应的工具或端点钩子 | `interaction_drivers.py:166` |
| 缺验证 | 企业端点真实幂等、支持按意图查询、2 s 内应答、TLS 链与提供的 CA/SNI 相符 | 真实主机 |

## 2. WebChat 在网关后的真实接入

| 类别 | 项 | 依据 |
|---|---|---|
| 已具备 | Caddyfile 模板：TLS 1.3 内部 CA 证书、HTTP→HTTPS、`/internal/*` 返回 404、经回环 TLS 反代两个 API、HSTS / 禁 frame / nosniff | `deploy/stage/config/Caddyfile.template:4-35` |
| 已具备 | API 校验 Origin 与 Host、登录签发 `X-CSRF-Token`；浏览器 origin 为启动参数 | Compose API 段；`tests/test_nx037_real_driver_pg.py` |
| 缺配置 | `__APP_HOST_NAME__`、网关端口、`BROWSER_ORIGIN`（占位 `https://app.stage.invalid:8443`，须与 `https://<APP_HOST_NAME>:<端口>` 完全一致）、网关证书与私钥 | `compose.app.env.example`、`stage.env.example`、Caddyfile |
| 缺配置 | `BROWSER_TENANT_ID` 及浏览器/工作台应用 id 须是 NX-037 真实租户的值（占位 `example-tenant`） | `compose.app.env.example` |
| 缺配置 | 访问只对受信 LAN 开放；扮演消费者的员工须在该 LAN 内并安装内部根证书 | `app-host.nft.template`、Caddyfile |
| 缺代码/手册（S） | 真实租户的消费者 WebChat 账号、工作台用户与业务装配（Role、上下文清单、Action、服务目录、`relay-run-profile.json`）不在 `real_tenant_init.py` 与 OPERATIONS §9 中；工具已有（`business_setup.py`、`catalog_setup.py`），缺 NX-037 的装配步骤与示例 | `scripts/nx037/real_tenant_init.py:9-17` |
| 缺验证 | SSE 经真实 Caddy（缓冲、长连接超时）；Origin/Host 校验经代理 | `AT-015-real-browser.md` §6 |

## 3. 真实运行器 real 路径的前置检查

| 类别 | 项 | 依据 |
|---|---|---|
| 已具备 | 缺参数、`--inject`、会话文件非私有即拒绝；租户 active、模型预算等于试运行币种下的累计上限并覆盖周期、告警规则 v3 生效；三层开关须与计划完全一致；3 次校准后暂停；`--resume` 需负责人确认引用 | `scripts/nx037/run_interactions.py:69-79, 126-147`；`interaction_drivers.py:147-154`；`tests/test_nx037_run_interactions.py`、`test_nx037_tenant_init_pg.py`、`test_nx037_real_driver.py`、`test_nx037_real_driver_pg.py` |
| 缺代码（S） | 负责人会话文件只检查权限、未使用 | `interaction_drivers.py:145-154` |
| 缺代码（S） | 未检查：Agent Host 处于 `deepseek-flash` 且凭据存在、企业端点可达、WebChat 投递在线、当前时间在试运行窗口内、D3 知情同意记录存在 | `run_interactions.py:69-79` |
| 缺代码（S） | 月度上限未计入本月此前的花费（`month_cost_before` 默认 0，入口未传） | `controlled_interactions.py:177`；`run_interactions.py:150-151` |
| 缺代码（S） | 成本汇总不区分币种、直接标为试运行币种 | `controlled_interactions.py:226-235` |
| 缺验证 | 端到端只覆盖 `ordinary_reply` 且绕过 `run_interactions.py`；其余四类场景（含人工接管结束条件）未跑过 | prep-implementation §8.5 |
| 文档 | OPERATIONS 仍写“真实驱动未实现”，已过时 | `docs/OPERATIONS.md:99` |

## 4. 真实模型下的费用与告警

| 类别 | 项 | 依据 |
|---|---|---|
| 已具备 | D-g 上限（累计 ¥100、单 Run ¥2、月 ¥300）与 D-i 计划（30 次、15/4/4/4/3、2 天 10:00–18:00、停止/暂停规则） | `nx037-trial.v1.json`、`nx037-interaction-plan.v1.json`；`controlled_interactions.py:56-80, 135-156` |
| 已具备 | 每次请求预留（`maximum_request_cost`）与按 pi-ai DeepSeek 费率估算的下限；Run 预算闸门；租户预算预留与结算 | `agent-host.runtime-config.example.json:6`；`trusted-model-profile.ts:22-45`；`goal_controls.py:446-453` |
| 缺代码（M，阻塞） | 币种冲突，见 §0 第 1 项；二选一：试运行改用 USD，或为 Host 与 Run 预算增加 CNY 计价 | — |
| 缺代码（S） | 单 Run 与月度上限只由运行器执行，数据库只有每租户一个模型预算；负责人在模型供应商侧的花费上限是第二道防线 | prep-implementation §2 |
| 缺代码（S） | 触发 Run 之外的模型花费（声明抽取、匹配、回复兜底、嵌入）未按交互计入 | `controlled_interactions.py:226` |
| 部分具备 | 告警规则 `budget_near_limit`（>80%）存在，但没有针对单请求/单 Run 真实模型成本的规则；告警只在应用内显示，未找到外部通知渠道 | `alert-rules.v3.json:184-191` |
| 缺验证 | 模型档案标记 `real_validation_pending`：真实 `deepseek-flash` 价格与估算是否一致、每次交互的实际成本（校准暂停时核对）、新主机上与备份相关的 critical 告警是否会触发停止条件 | `trusted-model-profile.ts:87` |

## 5. AT-015 真实浏览器的人工核对项

`planning/acceptance-tests.json` 中 AT-015 已通过（无头 Chromium 覆盖断网、刷新、服务器重启）。尚需人工核对（`AT-015-real-browser.md` §6）：移动网络切换与手机休眠/浏览器后台后的恢复（iOS Safari、Android Chrome）；Safari 与 Firefox；经真实 Caddy 代理；断开时间超过会话或 CSRF 有效期后的重连。无头用例不在默认 CI 中。

## 6. AT-071 / AT-072 的执行条件

- **AT-071**（not_run）：部署形态、真实租户、只对选定 Action 打开真实派发、受控真实模型、一个企业自有端点；≥20 次交互（D-i 为 30），每次报告模型/工具/平台时间与成本且在上限内；至少一次真实交付经 provider 查询完成对账；不含外部客户个人数据。缺凭据时记 blocked。前置：ROADMAP §8（NX-032 切片 5、AT-058、AT-050/053–057 主机证据、恢复日志加密、签名/SBOM 工具与安全联系人、历史审计、NX-052、NX-053）。
- **AT-072**（not_run）：负责人作为第二维护者，只凭公开文档在一次性机器上完成一次发布（构建、签名、验证、部署演练）和一次恢复（备份、PITR、重放、验证、打开）。依赖 NX-034、NX-035（均 in_progress，pgBackRest/restic 与恢复命令行未完成）。

## 7. 其他核对项

- **Agent Host 独立网络命名空间（D4）**：模板已实现（独立命名空间 + `agent_egress` + 内部 `agent_link` 与两个字节转发中继；防火墙：agent 命名空间只到模型白名单 443、永不到 DATA_HOST；应用命名空间永不到模型地址）。缺验证：AT-058 探测；模型供应商地址能否用固定 IP 白名单（`MODEL_PROVIDER_ALLOWLIST`）覆盖；agent 命名空间内的 DNS（非 443 新连接均被丢弃）；声明抽取与匹配服务持有模型凭据但位于应用命名空间，会被应用侧规则挡在模型之外。
- **三层真实派发开关**：已具备。第 1 层为派发进程环境变量 `REAL_DISPATCH_ENABLED`（须为 `true`，Compose 默认 `"false"`）；第 2、3 层为 `control.nexloop_real_dispatch_switches` 的租户行与（租户, Action）行（0153），负责人只能经 `POST /api/v1/workbench/actions/set_real_dispatch` 修改；派发时 SQL 复查（拒绝码 NXC07/NXC02）；`/health/ready`、`nexloop-doctor --real-dispatch` 与运行器（0163）读取状态；`tests/test_real_dispatch_switch_pg.py`。缺口：只保护声明为外部效应的 Action（即 §1 缺的那个）；没有前端页面。

## 8. 需负责人提供的材料

1. 企业端点：HTTPS origin、数字连接地址、CA 证书包、Bearer 凭据文件（16–4096 字符，`[A-Za-z0-9._~-]`）；确认幂等与按意图查询；演练中如何让结果暂时未知。
2. DeepSeek 模型凭据文件与模型配置（`--model-env-file` / `model_configuration_file`）、供应商侧花费上限、供应商 IP 白名单。
3. D-g 的币种决定（USD 或 CNY）。
4. 网关主机名、内部 CA 签发的网关证书与私钥、受信 LAN 网段、向扮演消费者的员工分发根证书。
5. 真实租户 uuid、浏览器/工作台应用 id、负责人主体、消费者与员工账号、价值目录、`relay-run-profile.json`、Role/清单/服务目录装配。
6. 负责人的 `Budget.set` 与开关打开操作、私有 metrics DSN 与负责人会话文件、参与员工的 D3 知情同意记录、试运行日期。
7. 把企业端点地址加入应用侧防火墙。

## 9. 建议的补齐顺序与可并行部分

1. **立即可并行（代码）**：
   - A 币种（先等 §8 第 3 项决定，实现 S–M）；
   - B 企业交付 Action、分类 v3、授权、Agent 工具与服务目录（M）；
   - C 按 Action 路由 provider + Compose 中的 WebChat 投递服务 + 防火墙规则（L）；
   - D 企业端点一致性检查与“暂时未知”演练钩子（S–M）；
   - E 运行器修补：`month_cost_before`、按币种汇总、补充前置检查，并更新 OPERATIONS §9（S）；
   - F 真实租户业务装配步骤与手册（S）。
2. **随后**：用合成数据把五类场景都经 `run_interactions.py` 跑通（依赖 A–C）。
3. **同时在主机侧**：完成 NX-032 切片 5、AT-058、D4 探测与 DNS；完成 NX-035 部署后做 AT-072；收集 §8 的材料。
4. **最后**：doctor 预检 → 租户初始化与核对 → 负责人 `Budget.set` → 打开三层开关 → 3 次校准交互并暂停核对成本 → 30 次交互 → AT-015 人工核对 → 证据包。

## 附：核对方法与局限

条目由只读代码审阅得出；§0 的四项已逐行抽查原文核实（`pi-runtime-adapter.ts:164`、`nx037-trial.v1.json:4` 与 `real_tenant_init.py:90`、`app-host.nft.template:14-16`、`action-effect-categories.v2.json:7-12` 与计划 `real_dispatch.actions`、`effect_dispatch.py:14-20`）。其余行号来自审阅时的定位，合入后若文件变动以当时为准。未运行任何测试，未连接任何主机。
