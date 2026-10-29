# 执行单：release 54 的 stage 部署（第 50 批）

release 54 = 第 50 批的排练提交 **f56cd5af**（`f56cd5af624b9a434a1178d2c46b1ff5efe1504e`，第一父 5988b5be，即 release 53）。本单写成可以照着执行的步骤，每一步写明预期结果和怎么撤回。

- **执行者**：调度员。**判定者**：L6。证据里两者分开写，测试、演练和真实结果分开记录。
- **人工步骤**：调度员在管理机和两台主机上执行的命令，以及 S8 用合成账号 approver3 做的浏览器走查；负责人账号不参与。
- **按 f56cd5af 核对过**（只读核对源码，没有连接主机）：与 5988b5be 相比，`deploy/` 下只改了 `api-routes.v1.json`，迁移只多 0370、0371；三个来源分支头 503aa660、28aab168、c12a6bf7 都已包含在内。

| 项 | f56cd5af 上的值 | 来源 |
|---|---|---|
| 新迁移 | 0370 `ui_experiments_view`（实验空间的只读视图；`authz.nexloop_workbench_observe_read` 加动词 `experiments`，只给 owner）；0371 `nx058_invite_sso`（邀请可指定仅限 SSO；成员视图包装 0356 的视图，加布尔 `sso_pending`）。迁移尾为 0371 | `packages/eios-core/src/eios/migrations/catalog.json` |
| 业务 Action | **没有新 Action**。实验空间第 2 片用已发布的 `nexloop.experiment.register/start/stop/report:1`；邀请仅限 SSO 是已有 `invite_workbench_member` 的一个可选字段（SQL 函数 `create or replace`，Action 定义不变） | 两个分支都没有改 `deploy/` 下的清单 |
| 工作台角色 | 不变（v48；owner 已含 4 个实验 Action） | `deploy/authorization/workbench-roles.v1.json` |
| 授权、主体、审批策略 | 不变（service-grants v22、stage-principals v7、approval-policies revision 5） | 同上 |
| compose、连接预算、host-health、告警规则 | 不变（app-net 18 个服务、连接 60/60、告警 v21） | 同上 |
| Host 运行配置 | 不变 | — |
| API 路由清单 | `api-routes` v3 → v4：新增 `GET /api/v1/workbench/experiments`（realm `workbench_session`，端口 `backend.workbench:experiments`）。这是仓库内由 CI 核对的清单，随镜像生效，stage 上没有要应用的步骤 | `deploy/authorization/api-routes.v1.json` |
| 前端 | 实验空间开放（不再标“未启用”）；Object Type 关系图（每个节点都在画布内，超过 64 个有关系的类型按名称排网格）；运营人设页（多个人设逐行列出）；确认面板（行内操作先确认，请求进行中不能关闭）；成员页的操作选择器与“待 SSO 关联”标记；客户详情页先列最近 5 次对话 | `l5/experiments-ui`、`claude/ui-ux-05f384` 28aab168、`claude/nx058-invite-sso` |
| SSO | stage 不配置 SSO provider：SSO 端点照旧 404；指定 `sso_provider` 的邀请被拒为 `provider_unknown`（409）。不重跑 `stage_secrets.py browser` | `docs/implementation/NX-058-slice-F.md` |

## 0. 记号

同 `run-release53-stage.md` §0：`$NEXLOOP_ROOT`、`<树>`、`<检出>`（管理机上 f56cd5af 的检出）、`ro "<SQL>"`、`stage_release.py` 各步。每一步先 `--dry-run` 看命令再正式执行；结果与预期不符就停下，交调度员判断。

## 1. 前提（不满足就不开始）

| # | 前提 | 怎么核对 |
|---|---|---|
| P1 | f56cd5af 的 CI 通过，release 已构建、签名、封存 | `stage_release.py --step build`、`--step sign` 都是 ok（签名前核对提交在 main 上，第 50 批合入 main 之后才执行本单） |
| P2 | stage 当前是 release 53（迁移尾 0369），known_good 记录在案 | `ro "select max(version) from control.schema_migrations"` 返回 0369 |
| P3 | release 53 的部署单已完整执行，S13 的连接数记录在案 | release 53 的证据 |
| P4 | 最近一次 pgBackRest 备份成功 | OPERATIONS §8a.2 |
| P5 | 部署门禁参照 `$NEXLOOP_ROOT/ops/baseline-r36/baseline.json` 存在；真实派发三层开关现状记录在案（门禁预期 20/21） | `test -f`；上次门禁记录 |
| P6 | Keychain 里有 approver3 的口令（S8 用），工作台 approver3 是 owner | `~/.nexloop/vault-index.md` 有条目（只看名字，不看值） |

## 2. 步骤

### S1 迁移前差异备份

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step backup
```

- 预期：`nexloop-pg-diff.service` 退出 0，日志里有本次差异备份完成的记录。
- **撤回**：不需要（只多一份备份）。

### S2 只读预检

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step precheck
```

预检 SQL 清单（私有 JSON）至少包含：

| name | sql | expect |
|---|---|---|
| migration_head | `select max(version) from control.schema_migrations` | `0369` |

另记一条数值（`precheck` 只记录输出的 sha256，看不到数值，所以不放进预检清单）：

```bash
ro "select count(*) from control.nexloop_workbench_invitations where state='issued'"
```

- 把这个数写进证据。这是迁移前**全部**未用的邀请，不是 SSO 邀请：迁移前还没有 `sso_provider` 列，SSO 邀请无从计数，所以不设固定预期值。S7 用它对照：
  - `ro "select count(*) from control.nexloop_workbench_invitations where state='issued'"` 与此数相同（0371 不改变已有邀请）；
  - `ro "select count(*) from control.nexloop_workbench_invitations where sso_provider is not null"` 为 0（stage 不配置 SSO provider）。
- **撤回**：不需要（只读）。

### S3 迁移 0370、0371

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step migrate
```

- 迁移门槛必须回答 `allowed: true`；迁移输出 ok；**之后的 definer 检查必须是 `0 problems`**（`migrate` 一步做完这三项，任一不符就停）。涉及 10 个 `security definer` 函数：0370 新建 2 个（实验视图、新的 `observe_read`）；0371 新建 5 个（4 个触发器函数、新的成员视图）并 `create or replace` 3 个已有函数（邀请、邀请查询、口令许可）。都设了 `search_path=pg_catalog,pg_temp`，definer 检查逐个核对。
- 预期迁移后 `ro "select max(version) from control.schema_migrations"` 返回 0371。
- **迁移时遇到 `DeadlockDetected`**：先确认迁移尾仍是 0369（整体回滚），重试一次；再遇到就停掉 APP_HOST 的 worker 服务再迁移。
- **迁移成功后再做一次差异备份**（重跑 S1）。
- **影响**：
  - 0370：只增加 owner 的只读视图；其他动词不变。改名保留的旧函数 `nexloop_workbench_observe_read_before_experiments` 对 `nexloop_api` 已撤销执行权限；
  - 0371：已有邀请的 `sso_provider` 为空，行为不变；接受仅限 SSO 的邀请不再设口令；stage 没有 SSO provider，所以实际不会产生这种邀请。
- **撤回**：迁移只能前进。要退回 0369 只能用 S1 的迁移前备份恢复（pgBackRest，恢复点之后的数据丢失），需调度员决定，并与镜像一起退回 release 53。不要手工删除迁移建的对象。

### S4 Action 发布：只预演，确认不需要发布

不执行 `stage_release.py --step publish`（它预演之后会接着发布）；`stage_release.py --dry-run` 只打印命令、什么都不跑，也不能代替预演。直接在 APP_HOST 上执行私有配置里 `publish.dry_run` 那一条命令（`stage_secrets.py publish --dry-run` 加连接文件等参数，与 release 53 的 S4 相同），只预演、不写：

```bash
python3 <检出>/scripts/deploy/stage_secrets.py publish --dry-run <私有配置中 publish.dry_run 的参数>
```

- 第 50 批没有新 Action，也没有改 Action 定义，**不需要发布**。仍带连接文件预演一次，与库里已发布的状态比较。
- 预期：**没有变化**；编译计数与 release 53 相同（object_types 20、actions 128、functions 2）。
- 预演出现任何变化就停下：说明第 50 批带了本单没有列出的 Action，按 release 53 的 S4 改写本步（先预演，再发布，再核角色）。
- 角色清单不变，不执行 `--step roles`，记录“无变化”。
- **撤回**：不需要（只预演）。

### S5 镜像与重建

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step rebuild
```

- 镜像只按签名清单里的 digest 载入（`rebuild` 会比对）；compose 不变，app-net 仍被 18 个服务引用，按 OPERATIONS §8a.10 一起处理。
- 外联执行器的 compose 不变，P9 仍应为 ok：重建前照 release 53 的 S9 第 1 步跑一次 `outreach_sender_p9.py`，不是 ok 就停。
- 预期：`docker compose ps` 全部服务在运行；工作台前端镜像为新 digest。
- **撤回**：按 known_good（release 53）的镜像和 compose 重建，引用方同 §8a.10。**更正**：release 53 的镜像**不能**在 0371 的库上运行。`assembly.open_core` 要求迁移谱系与镜像的目录完全相等，否则 `MigrationDrift`，服务启动即退出。退回 release 53 要先用 S1 的迁移前备份把库恢复到 0369，再换镜像（同 `run-release53-54-stage.md` S9a）。

### S6 部署门禁

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step gate
```

- 私有配置：`expected_gate_failures: ["real_dispatch_closed"]`；`--before` 用 `$NEXLOOP_ROOT/ops/baseline-r36/baseline.json`。
- 预期 **20/21**，唯一不通过的是 `real_dispatch_closed`。出现别的失败就停，不 promote。
- **撤回**：不需要（只检查）。

### S7 promote 与部署后核对

```bash
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step promote
python3 <检出>/scripts/deploy/stage_release.py --config <私有配置> --step post
```

- `promote` 只在本 release 最近一次门禁只因预期项失败时执行；`post` 的 `post_deploy_check` 为 `pass: true`。
- 只读核对：
  - `ro "select max(version) from control.schema_migrations"` 为 0371；
  - S2 记下的邀请对照两条（未用邀请数不变；`sso_provider` 非空的为 0）；
  - `nexloop-doctor` 全部通过；工作台“告警”页没有新的 firing；
  - 连接数：服务没有增加，照 release 53 的 S13 记录一次各角色连接数和总数，应用服务总数不超过 60。
- **撤回**：按 known_good 退回 release 53（S5 的撤回）。

### S8 浏览器走查（调度员，合成账号 approver3）

stage 工作台，用 approver3（合成 owner）登录；口令从 Keychain 取，只在浏览器的登录框里输入，不进命令行、不进截图。**走查只看不改**：不点任何“确认”提交，打开的编辑器和确认面板都用“取消”或“关闭”退出。每页记录：页面能打开、没有整页空白、没有错误边界的告警框、浏览器控制台没有报错；截图不含口令和地址。

**重点三处（负责人报过）**：

| # | 页面（侧栏） | 怎么看 | 预期 |
|---|---|---|---|
| W1 | 运营 › **Action / 异常** | 打开页面，等加载完成；再从别的页面切回来一次 | 页面内容正常显示，**整个工作台不白屏**；加载中和不可用时显示空状态与“重试”；侧栏和顶栏始终在 |
| W2 | 模型与资产 › 企业世界模型 › **Object Type**，切到关系图 | 看默认视图，再点“适应画布”；点一个类型（如 Consumer），再用 Tab/Enter、方向键、+/− 操作 | **每个节点都在画布内**，不再只看到 Consumer 一个和从画布外伸进来的箭头；标签不小于 12 px，可读；“适应画布”后仍框住全部节点；同一对类型间的多条关系分开画；放不下的关系名在选中一端类型时显示。类型超过 64 个时按名称排成网格并提示去看表格视图（stage 目录不到这个数，应是正常的关系图） |
| W3 | **运营人设** | 看列表；点“新建人设”打开编辑器再“关闭”；在某一行点“发布新版本…”再“关闭” | 租户的**每个人设单独一行**，有当前版本、状态和分配位置（“租户默认”、命名它的分配规则或“未分配”）；没有人设时空状态说明可以建多个人设；编辑器在列表下方打开，焦点在标题，关闭后焦点回到原按钮；新人设从版本 1 开始，已占用的标识不能再用；“版本记录”列出每个版本 |

**其他变化**：

| # | 页面 | 怎么看 | 预期 |
|---|---|---|---|
| W4 | **实验空间** | 打开列表和一条实验的详情（没有实验就看空状态） | 侧栏不再标“未启用”；列表只有计数，不出现任何客户；预算演练的实验带“预算演练”标记和提示“预算演练，孪生未校准，结论不代表实验效果”；结束时间的输入提示“按北京时间填写，提交时换算为 UTC”。登记、开始、停止只打开确认面板看效果说明，然后“取消” |
| W5 | 成员与账号 | 打开一行的操作选择器；选一个操作打开确认面板，再“取消”；打开邀请表单看登录方式 | 每行只列这行允许、approver3 角色持有的操作，危险操作有标记；确认面板写明效果和理由，“取消”可关；没有成员标“待 SSO 关联”（stage 没有 SSO provider） |
| W6 | 客户 › 任一合成客户的详情 | 打开详情 | 先列最近 5 次对话（其余点一下展开），再是承诺、操作，然后是人设与偏好；已分配但版本全停用的人设显示为当前选择、只读、标“已停用” |
| W7 | 确认面板（W4、W5 中任一） | 打开后按 Escape；回答前看“取消”的状态 | 未提交时 Escape 和“取消”都能关闭；本次走查不提交，所以不验证“请求进行中不能关闭”（由 `workbench-confirm` 测试覆盖），证据里写明 |

- 任一项不符：截图（不含敏感信息）、记录控制台报错，交调度员；不在 stage 上改任何东西。W1–W3 任一失败就不判本 release 通过。
- **撤回**：不需要（只看）。

## 3. 证据与判定

| 步骤 | 执行者记录 | 判定者（L6）核对 |
|---|---|---|
| S1–S3 | 各步 `stage_release.py` 日志行（已脱敏）、迁移前后的迁移尾、definer 输出 | 迁移尾 0371、definer 0 problems |
| S4 | 预演输出和计数 | 没有变化，计数与 release 53 相同 |
| S5 | P9 输出、`docker compose ps`、镜像 digest | digest 与签名清单一致 |
| S6–S7 | 门禁失败项名单、promote 与 post 输出、连接数 | 失败项只有 `real_dispatch_closed` |
| S8 | W1–W7 每项的结论、截图、控制台记录 | W1–W3 全部符合预期 |

证据里不贴 DSN、口令、令牌、地址或主机名，只写角色名（APP_HOST、DATA_HOST、CI 主机）。

## 4. 回退总表

| 步骤 | 怎么撤回 | 代价 |
|---|---|---|
| S3 迁移 | 只能用迁移前备份恢复，与镜像一起退回 release 53 | 恢复点之后的数据丢失；需要调度员决定 |
| S4 发布预演 | 不需要 | 无 |
| S5 重建 | 先把库恢复到 0369，再退回 release 53 的镜像（镜像不能在 0371 上运行，见 S5 的更正） | 前端回到 release 53：实验空间重新“未启用”，三处修复失效 |
| S6 门禁 | 不需要 | 无 |
| S7 promote | 同 S5，known_good 指回 release 53 | 同 S5 |
| S8 走查 | 不需要 | 无 |
