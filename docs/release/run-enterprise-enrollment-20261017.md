# 命令单：在 stage 为消费者开通企业交付（2026-10-17）

**背景**：校准第 2 条（`real_delivery`）失败：模型在对话中没有 `nexloop.enterprise.deliver` 工具。

Host 只在以下情况提供这个工具：
- 这次消息 Run 的上下文带企业 offering，即 pack 的 `effect_supplies` 中有一项 `action` 为 `nexloop.enterprise.deliver:1`（`apps/agent-host/src/runtime-host.ts` 的 `enterpriseSupply`，以及 `runtime-effect-tools.ts` 的 `toolsForRun`）；
- 而 relay 只在 Run 凭据授予 `nexloop.enterprise.deliver:1` 时才放入这一项（ADR-030，迁移 0180）。

两者都来自 relay 登记中的**企业 slot**（NX-037 租户装配手册 §6）。stage 执行 run-assembly 时，6.3 enterprise setup 和 §8 的企业键都没有做（run-34 记为阶段 C），所以每个消费者的登记中三个 `enterprise_*` 列都为空。

**本单范围**：
1. 为交付类账号开通企业 slot：§6.2 `enterprise_setup`，然后 §6.3 重新执行 `relay_enrollment`，带上企业键；
2. 只读核对 Run 确实拿到了工具；
3. 用防火墙模板替换 10/16 晚手工加的员工页面规则；
4. 工作台开关页的评估（附录）。

**不做**：直接写 SQL（本单的 SQL 全部是只读事务）；为拒收账号开通。

**依据**：
- NX-037-tenant-assembly §5–§6；
- `nexloop_eios.enterprise_setup` 与 `nexloop_eios.relay_enrollment` 的 argparse（命令名 `nexloop-enterprise-setup`、`nexloop-relay-enroll`，见 pyproject `[project.scripts]`）；
- run-assembly §0.3（`$APPTOOL`、`$BACKEND`）、§6、§8；
- `deploy/authorization/stage-principals.v1.json`；
- OPERATIONS §8a.7。

类别标注：**B** 表示需负责人确认；**sudo-A** 表示需要 APP_HOST 的 sudo；**sudo-D** 表示需要 DATA_HOST 的 sudo。只用角色名和变量。

## 0. 前提（只读核对）

| 前提 | 核对 | 依据 |
|---|---|---|
| relay 的 Source 与效应执行者都持有 `nexloop.enterprise.deliver:1` 的执行权 | `stage_secrets.py register --dry-run`（run-release7-deploy §3.1 的形态）输出的 `grants` 中有 `effect_executor action eios:action:nexloop.enterprise.deliver:1 execute` 和 `relay_source action eios:action:nexloop.enterprise.deliver:1 execute`。这两条在 stage 授权清单中（stage-principals 第 159、189 行），随发布 7 的 register 已写入，`grants_added: 0` 说明库里已经有了 | 租户装配手册 §6.1 |
| 效应类别 v3 把 deliver 声明为外部效应 | `real_tenant_init.py verify` 输出的 `effect_category_declarations` 中包含 deliver | 手册 §5 第 1 步、OPERATIONS §9.2 |
| 企业端点一致性检查通过 | 参考端点 README §4；运行入口开跑时还会再跑一次（守卫 `enterprise_endpoint`） | 手册 §6 前提二 |
| action-worker 的路由含 enterprise.deliver | 调度员 10/16 晚已接通，只读查询返回 not_found，属于正常 | 手册 §5 第 3 步 |
| 三层开关 | 开通企业 slot **不需要**打开开关；派发时才检查开关。校准当天按 run-calibration §1.4 打开 | 手册 §6 前提一 |

**开通哪些账号**：只开通交付类账号 `<CUSTOMER_1..4>`，这些账号会做 `real_delivery`、`unknown_reconciliation`，也可能做 `takeover_after_delivery`。**不开通** `<REFUSAL_1..2>`：拒收后受限，企业交付在派发时会被拒，计划也禁止它们做交付（`account groups`）。不开通的账号不受任何影响，profile 逐字节不变（手册 §6）。

## 1. 准备（APP_HOST；**B**，sudo-A）

- **容器形态**：沿用 run-assembly §0.3 的 `$APPTOOL` 和 `$BACKEND`：`--network container:<app-net>`、`--user 10001`，只读挂载 api 的 DSN、证书和签名密钥，`/in` 放私有输入。`$APP_RUN/src` 换成发布 8 提交的源码树（run-assembly §0.4 的 `git archive` 形态），PYTHONPATH 优先。
- **`/in` 中的凭据**：与 run-assembly §6 的表相同，从管理树 `setup/` 复制，属主 10001，0600，全部步骤结束后删除：
  - `business-owner-credential`：owner，也负责 relay 登记；
  - `business-executor-credential`：effect_executor；
  - `catalog-maintainer-credential`：catalog_maintainer；
  - `catalog-source-credential`：relay_source，维护者与 Source 必须是不同主体。
- **每个账号现有登记的 ID**：重新登记时要原样带上 6.1、6.2 和 §7 的 ID。从负责人当时记下的输出 JSON 取；没有记录的，用 §4.1 的只读查询取最新一行。

## 2. 建企业 offering、绑定与企业控制：`enterprise_setup`（每个账号一次；**B**）

recipe：由 `deploy/examples/nx037/enterprise-setup-recipe.example.json` 填出，保存为 `/in/enterprise-<C>.json`：

| 键 | 取值 |
|---|---|
| `request_id` | 新的 uuid（固定下来，重放时用同一个） |
| `consumer_id` | 该账号的 consumer_id（run-assembly 6.1 的输出） |
| `offering.service_code` | `enterprise.<后缀>`，后缀为小写字母、数字、`.`、`-`（Host 校验 `^enterprise\.[a-z0-9][a-z0-9.-]{0,62}$`） |
| `offering.title` | 1–256 字，模型看到的服务名，写企业目录中的服务，不写真实租户名 |
| `offering.price_amount` | CNY 十进制字符串，可以是 `0`（Host 校验 `^(0\|[1-9][0-9]{0,11})(\.[0-9]{1,2})?$`） |
| `offering.valid_until`、`control.valid_until` | 试运行期末，未来的 UTC 时间：`2026-10-22T16:00:00Z` 或更晚 |
| `control.budget_units` | 示例值 30（1–1000000）；每个账号计划中的交付次数远少于这个值 |

```
docker run $APPTOOL "$BACKEND_IMAGE" -m nexloop_eios.enterprise_setup $BACKEND --owner-credential-file /in/business-owner-credential \
  --executor-credential-file /in/business-executor-credential --maintainer-credential-file /in/catalog-maintainer-credential \
  --source-credential-file /in/catalog-source-credential --recipe-file /in/enterprise-<C>.json
```

- **出处**：
  - 参数取自 `enterprise_setup.main`，以下都是必填：`--database-url-file`、`--signing-key-file`、`--artifact-root`、`--owner-credential-file`、`--executor-credential-file`、`--maintainer-credential-file`、`--source-credential-file`、`--recipe-file`；`--signing-key-id` 缺省为 `active`，`$BACKEND` 中显式给出；
  - 形态同 run-assembly §6 的第三条命令。
- **预期**：
  - 退出码 0，一行 JSON：`configured: true`、`consumer_id`、`enterprise_control_id`、`enterprise_offering_id`、`enterprise_binding_id`、`delivery_action: nexloop.enterprise.deliver:1`、`source_ref`、`executor_ref`、`enrolled: false`；
  - 记下三个 `enterprise_*` id，下一步使用。
- **重放**：同一份 recipe 再执行一次，返回相同的对象，不会重复建。
- **失败**：退出码 1，标准错误只有 `Enterprise setup unavailable`；recipe 不合法时什么都不会建。参数错误时退出码 2，输出 `Enterprise setup configuration unavailable`。
- **撤销**：对象只能经受治理 Action 设为失效（`allow_effect=false`、`active=false`），不删除（手册 §6.5）。

## 3. 重新登记，带上企业 slot：`relay_enrollment`（每个账号一次；**B**）

登记文件 `/in/enroll-enterprise-<C>.json` 由 `relay-enrollment-enterprise.example.json` 填出：

- `consumer_id`、`control_id`、`offering_id`、`binding_id`：与现有登记相同（6.1、6.2 的输出）；
- `relay_principal`：`nexloop-service-message-relay-principal`；
- `enterprise_control_id`、`enterprise_offering_id`、`enterprise_binding_id`：第 2 步的输出。**三个键要么全写，要么都不写**；
- `role_id`、`link_id`、`step_id`：与现有登记相同（run-assembly §7 的输出），三者同时出现（run-assembly §8）。不带这三个键，重新登记会把角色组装清掉；
- `intent_id`：新的值，`relay-enroll-<新 UUID>`。

```
docker run $APPTOOL "$BACKEND_IMAGE" -m nexloop_eios.relay_enrollment $BACKEND --owner-credential-file /in/business-owner-credential \
  --enrollment-file /in/enroll-enterprise-<C>.json
```

- **出处**：参数取自 `relay_enrollment.main`，以下都是必填：`--database-url-file`、`--signing-key-file`、`--owner-credential-file`、`--artifact-root`、`--enrollment-file`；`--signing-key-id` 缺省为 `active`。`$BACKEND` 中多出的 `--artifact-root` 本来就是必填项。
- **身份**：`business_owner`，它必须同时是回复控制和企业控制的属主。
- **预期**：
  - `{"enrolled": true, …}`，并带新的 `enrollment_revision`；
  - 失败时只输出 `Relay enrollment unavailable`，退出码 1，登记不变。
- **登记时的校验**（手册 §6.3）：
  - 同一租户、同一 world；
  - 调用者是企业控制的属主，企业控制属于这个消费者，且 `allow_effect` 为 true；
  - 绑定与 offering 匹配，`delivery_action` 是 deliver:1，并且都有效；
  - 企业控制不是回复控制。
- **撤销企业交付**（只清企业 slot，回复不受影响）：
  - 用原登记文件的形状，即不带三个 `enterprise_*` 键、带角色三键，换新的 `intent_id`，用同一条命令再登记一次；
  - `enrollment_revision` 会比开通前多 2，这是预期的。
- 要立即停止全部企业交付，关闭 (租户, `eios:action:nexloop.enterprise.deliver:1`) 这一层开关即可，不必撤销登记（手册 §6.5）。

## 4. 核对（只读）

### 4.1 登记带企业 slot（DATA_HOST 一次性容器，bootstrap 只读事务）

容器形态同 run-model-smoke §2.2（`$COMMON`、`$WINDOW`，`--user` 按 run-release7-deploy §0 取 `$RUN` 的属主）。

```
docker run $COMMON $WINDOW -e TENANT_ID="$TENANT_ID" -i "$BACKEND_IMAGE" - <<'PY'
import json, os, psycopg
t = os.environ['TENANT_ID']
with psycopg.connect(open('/run/bootstrap-dsn').read().strip()) as c:
    c.execute('set transaction read only')
    rows = c.execute("""select distinct on (consumer_id) consumer_id, enrollment_revision, enterprise_control_id is not null,
                               enterprise_offering_id is not null, enterprise_binding_id is not null, role_id is not null
                          from control.nexloop_relay_enrollments where tenant_id=%s order by consumer_id, enrollment_revision desc""", (t,)).fetchall()
    print(json.dumps(rows, default=str))
PY
```

- **出处**：run-assembly §8 的“核对”查询；列名取自那条查询。
- **预期**：`<CUSTOMER_1..4>` 四行，三个企业列都为 true，`role_id` 为 true；`<REFUSAL_1..2>` 两行，企业列都为 false。

### 4.2 relay 没有跳过企业 slot（APP_HOST）

```
sudo docker compose -p nexloop-app logs --since 30m message-relay message-relay-2 | grep -c enterprise_slot_skipped
```

- **预期**：`0`。
- 不是 0 时，日志中的 `reason`（`control_mismatch`、`control_invalid`、`offering_invalid`、`binding_invalid`）指出哪一个对象失效了，按手册 §6.5 修复，不要直接改表。

### 4.3 模型确实拿到了工具（一个交付类账号发一条普通消息之后）

由客户代理用 `<CUSTOMER_1>` 在 WebChat 发一条普通消息，不要提交易请求，这样不会产生交付。等回复出现后，在 DATA_HOST 执行（`T0` 为发送前的时间）：

```
docker run $COMMON $WINDOW -e TENANT_ID="$TENANT_ID" -e T0="$T0" -i "$BACKEND_IMAGE" - <<'PY'
import json, os, psycopg
t, t0 = os.environ['TENANT_ID'], os.environ['T0']
with psycopg.connect(open('/run/bootstrap-dsn').read().strip()) as c:
    c.execute('set transaction read only')
    rows = c.execute("""select b.created_at, b.pack_text::jsonb -> 'bindings' ->> 'run_id',
             (select count(*) from jsonb_array_elements(coalesce(b.pack_text::jsonb -> 'effect_supplies', '[]')) s
               where s ->> 'action' = 'nexloop.enterprise.deliver:1'),
             (select s ->> 'offering_id' from jsonb_array_elements(coalesce(b.pack_text::jsonb -> 'effect_supplies', '[]')) s
               where s ->> 'action' = 'nexloop.enterprise.deliver:1' limit 1),
             (select 'eios:action:nexloop.enterprise.deliver:1' = any(r.allowed_resources) from authz.nexloop_run_credentials r
               where r.run_id::text = b.pack_text::jsonb -> 'bindings' ->> 'run_id')
           from runtime.nexloop_context_artifact_bindings b where b.created_at >= %s order by b.created_at""", (t0,)).fetchall()
    print(json.dumps(rows, default=str))
PY
```

- **出处**：
  - pack 字段取自 ContextPackV6 契约：`bindings.run_id`，以及 `effect_supplies[].action`、`offering_id`；
  - Run 凭据的 `allowed_resources` 来自 `authz.nexloop_run_credentials`（0049 与 0180）；
  - 只输出 id、计数和布尔值，不输出 pack 内容。
- **预期**：该消息 Run 对应的那一行：
  - `effect_supplies` 中 deliver 项为 1，`offering_id` 等于第 2 步该账号的 `enterprise_offering_id`；
  - Run 凭据中的 deliver 为 true。
- **Host 的工具清单**：
  - Host 不把每个 Run 的工具清单写进日志或数据库，所以“工具清单含 `nexloop.enterprise.deliver`”无法直接读出；
  - 但这个清单完全由上面两项决定：`runtime-host.ts` 第 330 行把 `enterpriseSupply(activation input)` 传给 `toolsForRun`，`runtime-effect-tools.ts` 第 291–305 行在 `enterprise.action === nexloop.enterprise.deliver:1` 时注册 `nexloop.enterprise.deliver` 和 `nexloop.enterprise.find`；
  - pack 带 deliver 项（Host 收到的 activation input 就是这份 pack），就等于工具已提供；
  - 行为上的最终证据是校准第 2 条：`runtime.nexloop_effect_intents` 中出现一条 `action_name` 为 `nexloop.enterprise.deliver` 的意图（run-calibration §3.2 的 effects 查询）。
- **不符时**：
  - deliver 项为 0 而凭据为 true：pack 的组装没有带上 offering，查看 relay 和 Context 日志，报调度员；
  - 两项都为 false：登记没有生效，回到 4.1、4.2。

## 5. 员工页面的防火墙规则：用模板替换 10/16 晚的手工规则（APP_HOST；**B**，sudo-A）

**现状**：10/16 晚，调度员在 APP_HOST 手工加了一条规则，只放行负责人的 Mac 访问参考端点端口（员工页面 `/staff` 与 POST 接口同端口），放在 include 文件 `app-host-20261016-staff.nft` 中。模板里没有这条规则，下一次 `systemctl reload nexloop-nft` 或主机重启都会把它丢掉，或者把它和模板的结果混在一起。

**模板**（本分支）：
- `app-host.nft.template` 在参考端点的“效应网络放行”和“其余全部丢弃”之间，新增一行：`ip saddr { __STAFF_ALLOW_CIDRS__ } accept`；
- 新参数 `STAFF_ALLOW_CIDRS`（`stage_render.PARAMETERS`）：逗号分隔的严格 CIDR，单台主机写 `/32`，不允许重复；
- **缺省为空**：参数缺失或为空时，渲染器整行不输出（nft 不接受空集合），员工页面对任何网络都不开放。

步骤（OPERATIONS §8a.7 的“改规则”）：

1. 备份并编辑私有 `stage.env`：加 `STAFF_ALLOW_CIDRS=<负责人 Mac 的地址>/32`（与手工规则中的地址相同）。
2. 渲染到新文件，并与现行规则比对：
   ```
   R=$NEXLOOP_ROOT/config/firewall
   sudo python3 "$RELEASE_CHECKOUT/scripts/deploy/render.py" --env "$STAGE_ENV" --template "$RELEASE_CHECKOUT/deploy/stage/firewall/app-host.nft.template" --out "$R/app-host.rules.nft.new"
   sudo diff "$R/app-host.rules.nft" "$R/app-host.rules.nft.new"
   ```
   预期 diff 只有新增的员工规则一行及其注释。其他差异说明 env 或模板与现行规则不同步，先停下核对。
3. 替换规则文件，重新生成加载文件。加载文件按 nexloop-nft 的说明由 `--nft-transaction` 生成；`render.py` 拒绝覆盖已存在的输出，所以先删掉旧文件：
   ```
   sudo cp -p "$R/app-host.rules.nft" "$R/app-host.rules.nft.before-staff"; sudo mv "$R/app-host.rules.nft.new" "$R/app-host.rules.nft"
   sudo cp -p "$R/app-host.nft" "$R/app-host.nft.before-staff"; sudo shred -u "$R/app-host.nft"
   sudo python3 "$RELEASE_CHECKOUT/scripts/deploy/render.py" --nft-transaction "$R/app-host.rules.nft" --out "$R/app-host.nft"
   sudo nft -c -f "$R/app-host.nft" && echo nft-ok
   ```
   - 新的加载文件只 include 模板渲染出的规则文件。如果 10/16 晚的 `include "…app-host-20261016-staff.nft"` 是加在旧加载文件里的，它在这里自然就去掉了。
4. 原子替换：`sudo systemctl reload nexloop-nft`。**不要** `restart`，见 OPERATIONS §8a.7。
   - 如果手工规则是用 `nft add rule` 加进内存表的，reload 会把整张表原子替换，它也随之消失。
5. 核对：
   - `sudo nft list table inet nexloop_stage | grep -c 'ip saddr {'` 中，员工规则恰好一条，地址与 env 相同；
   - 从负责人的 Mac 能打开员工页面；从受信 LAN 的其他地址被丢弃；action-worker 到端点的交付不受影响（下一次交付或只读查询正常）；
   - 门禁的 `compose_networks_match_firewall`、`running_networks_match_firewall` 为 true。
6. 清理手工文件：确认 `app-host-20261016-staff.nft` 已不被任何加载文件引用（`sudo grep -r app-host-20261016-staff $R`，预期无输出）后删除。
7. **撤销**：恢复两个 `.before-staff` 文件，执行 `reload`。

**测试**：`tests/test_deploy_render.py::test_the_staff_page_admits_only_the_configured_staff_networks` 覆盖：
- 缺省不渲染这一行，员工地址被丢弃；
- 一个或两个员工网络被放行，其他 LAN 地址仍被丢弃；
- 效应网络的放行不受影响；
- 非法值被拒：格式错误、前缀长度越界、重复、注入、带主机位的网段。

## 6. 证据

- 第 2 步每个账号的输出 JSON（只有 ID）、第 3 步的 `enrollment_revision`、4.1 与 4.3 的查询结果，放入 `local-only/evidence/v0.1-evidence/trial/`（AT-071 的 `note`）。
- 防火墙：diff 输出、`nft -c` 结果、核对结果，放入 `environment/`。
- 不记录凭据、DSN 或地址：员工地址只写“负责人 Mac（/32）”。

## 附录：工作台的真实派发开关页（评估，先不实现）

**现状**：
- 后端写入口已经有了：`workbench_actions_http.py` 的 `set_real_dispatch`，校验 `scope_kind`（tenant 或 action_type）、`enabled`、`reason`、`action_type`、`data_mode`；
- 由 `GoalGovernedActions.set_real_dispatch` 调用受治理 Action `nexloop.real_dispatch.set:1`；workbench-roles v9 规定只有 owner 持有；
- 前端没有入口，10/16 晚负责人只能在浏览器控制台里 POST。

**缺的部分**：
1. **读当前状态**：
   - 工作台的读接口（`workbench_reads.py`）没有开关状态；
   - `authz.nexloop_nx037_open_switches`（0163）只授权给 metrics 角色；
   - 需要一个 owner 专用的读函数，返回租户层（enabled、data_mode）和已打开的 Action 列表。可以新增一个只读 definer 函数，用工作台会话鉴权，只允许 owner；也可以在 settings 读中加一个只对 owner 返回的字段；
   - **部署层**是进程环境变量，API 进程用 `real_dispatch.deployment_open()` 读到的就是同一个变量（run-calibration §1.4 已统一到 compose env），可以只读显示。
2. **前端**：
   - 在“设置与治理”页（`pages/Settings.tsx`）下方，加一个只对 owner 显示的区块：三层的当前状态，租户层开关（含 data_mode 选择），Action 层逐条开关（只列已发布的外部效应 Action），每次操作都必须填原因；
   - 复用 `actions.tsx` 的 `Form` 组件和 `GOVERNED_SLOTS`，写法同 `SilenceForm`；
   - 部署层只显示，不提供按钮：它要改 compose env 并重建服务，属于 B 类主机操作。
3. **测试**：
   - PG 测试：owner 能读能改；operator、reviewer 读不到状态，也不会看到开关；关闭后派发被拒；
   - vitest：区块只对 owner 渲染，原因为空时不能提交，提交后刷新状态。

**工作量估计**：
- 后端读：S，约 0.5 天（新的只读函数要一个临时迁移，加授权测试）；
- 前端区块：S，约 0.5 天；
- 测试与文档：约 0.5 天；
- **合计约 1–1.5 天**，可以单独开一个 L2/L4 任务。

在那之前，负责人仍用浏览器控制台向 `set_real_dispatch` 接口 POST（受治理 Action，有审计）。不要为了方便而直接写开关表。
