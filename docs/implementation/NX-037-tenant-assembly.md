# NX-037 真实租户业务装配手册（缺口 F）与企业端点检查（缺口 D）

依据：[NX-037 就绪缺口核对](NX-037-readiness-gaps.md) §1、§2；[NX-037 设计](NX-037-design.md)；[运维手册 §9](../OPERATIONS.md#9-nx-037-受控真实闭环操作说明负责人本人执行)。本手册把真实租户从“已初始化”推进到“消费者能在 WebChat 发消息、工作台能接管、relay 能为这个消费者签发 Run、Agent 能请求企业交付”的全部装配步骤列出来。

- **只用角色名与占位值**：示例配置在 [`deploy/examples/nx037/`](../../deploy/examples/nx037)，全部是 `<...>` 占位；真实文件放在负责人的私有目录（0600），不入库。
- **执行者**：**【B】** 负责人本人（或在负责人逐步授权下执行）；**【A】** 只读或本地检查。
- **前置**：[运维手册 §9.2](../OPERATIONS.md#92-真实租户初始化scriptsnx037real_tenant_initpy) 的真实租户初始化（plan → execute → `Budget.set` → verify）已完成。
- **【待确认】** 的项是本手册没能从现有代码核实的取值，执行前由调度员确认。

## 1. 身份：消费者 WebChat 账号与工作台用户（【B】）

每个人（扮演消费者的内部人员、负责人、运营）各建一个浏览器身份。身份只能由身份操作员按清单创建，不能自助注册；顾客主体与工作台成员互斥，同一个人要分别建两个身份。

1. 受信配置先为两个浏览器应用（WebChat 应用、工作台应用）登记 `browser_applications` 与 `identity_allowances`（每个应用允许创建的账号数），经 0050 可信配置清单写入。
2. 按 [`identity-human.example.json`](../../deploy/examples/nx037/identity-human.example.json) 为每个人写一份私有清单：`tenant_id` 为真实租户、`application_id` 为对应应用、`request_id` 为新的 uuid；`subject_id`、`principal_id`、`local_account_id` 由负责人自定；不放口令。
3. 先检查，再创建（口令文件至少 12 个字符；幂等密钥文件为 32 字节十六进制）：

   ```bash
   python -m nexloop_eios.initial_identity --manifest <IDENTITY_MANIFEST_FILE> --check
   ```

   ```bash
   python -m nexloop_eios.initial_identity --manifest <IDENTITY_MANIFEST_FILE> --create --identity-database-url-file <IDENTITY_DSN_FILE> --password-file <PASSWORD_FILE> --idempotency-key-file <IDEMPOTENCY_KEY_FILE>
   ```

   输出 `created`、`subject_id`、`principal_id`、`account_id`；重放同一清单不会重复创建。

## 2. 工作台成员与角色（【B】）

按 [`workbench-members.example.json`](../../deploy/examples/nx037/workbench-members.example.json) 写私有成员文件（owner 与 operator 各一人，主体来自第 1 步），然后：

```bash
python -m nexloop_eios.workbench_roles --roles deploy/authorization/workbench-roles.v1.json --members <MEMBERS_FILE> --check
```

```bash
python -m nexloop_eios.workbench_roles --roles deploy/authorization/workbench-roles.v1.json --members <MEMBERS_FILE> --database-url-file <CONFIGURATOR_DSN_FILE> --apply-members
```

```bash
python -m nexloop_eios.workbench_roles --roles deploy/authorization/workbench-roles.v1.json --members <MEMBERS_FILE> --compile-facts
```

顺序不能颠倒（0140 强制）：先写成员，再把编译出的授权事实放进 0050 可信配置清单应用。缩减角色或移除成员时，先撤授权再写较小的成员列表。

## 3. 每个消费者的业务装配（【B】）

对每个扮演消费者的身份依次执行；三步各自提交，重放安全。

1. **business setup**：建 Consumer、EffectControl 与消费者归属。私有 recipe 见 [`business-recipe.example.json`](../../deploy/examples/nx037/business-recipe.example.json)（`human_principal_id` 为第 1 步的消费者主体，`valid_until` 覆盖试运行期间）。

   ```bash
   python -m nexloop_eios.business_setup --database-url-file <API_DSN_FILE> --signing-key-file <SIGNING_KEY_FILE> --owner-credential-file <OWNER_SERVICE_CREDENTIAL_FILE> --executor-credential-file <EXECUTOR_CREDENTIAL_FILE> --recipe-file <BUSINESS_RECIPE_FILE> --artifact-root <ARTIFACT_ROOT> --verify-current
   ```

   输出中的 `consumer_id`、`control_id` 用于后两步。
2. **catalog setup**：为这个消费者建 ServiceOffering 与 ConsumerServiceOffering。私有 recipe 见 [`catalog-recipe.example.json`](../../deploy/examples/nx037/catalog-recipe.example.json)。Source 凭据必须属于固定主体 `nexloop-service-relay-source-principal`（[stage README §6](../../deploy/stage/README.md)）。

   ```bash
   python -m nexloop_eios.catalog_setup --database-url-file <API_DSN_FILE> --signing-key-file <SIGNING_KEY_FILE> --maintainer-credential-file <MAINTAINER_CREDENTIAL_FILE> --source-credential-file <RELAY_SOURCE_CREDENTIAL_FILE> --recipe-file <CATALOG_RECIPE_FILE> --artifact-root <ARTIFACT_ROOT>
   ```

   输出中的 `offering_id`、`binding_id` 用于下一步。
3. **relay 登记**：私有文件见 [`relay-enrollment.example.json`](../../deploy/examples/nx037/relay-enrollment.example.json)，调用者是该 EffectControl 的 owner 服务凭据。

   ```bash
   nexloop-relay-enroll --database-url-file <API_DSN_FILE> --signing-key-file <SIGNING_KEY_FILE> --owner-credential-file <OWNER_SERVICE_CREDENTIAL_FILE> --artifact-root <ARTIFACT_ROOT> --enrollment-file <ENROLLMENT_FILE>
   ```

   之后用 `nexloop-doctor --relay-source --metrics-database-url-file <METRICS_DSN_FILE>` 核对。

## 4. relay 的 Run 设置 `relay-run-profile.json`（【B】）

部署级、对所有消费者相同的 6 个键，见 [`relay-run-profile.example.json`](../../deploy/examples/nx037/relay-run-profile.example.json)：`role_ref`、`context_manifest_ref`、`runtime_profile`（真实试运行为 `deepseek-flash`）、`budget`（单 Run 上限按 D-g；币种与试运行预算一致）、`runtime_owner_epoch`、`queue`。放进 relay 的私有目录，容器入口看到它时以 `--run-profile-file` 启动 relay（与单客户 recipe 互斥）。

- 【待确认】`role_ref` 与 `context_manifest_ref` 在真实租户中的取值（Role 定义与上下文清单 Artifact 的引用）：仓库中只有测试夹具的取值（例如 `role:explicit-source`、`artifact:owned-local-demo`），真实租户由调度员按 Role 与上下文策略的部署方式确定。
- `budget.currency` 必须与租户模型预算和 Agent Host 成本策略的币种一致（就绪缺口 §0 第 1 项，D-g 已定案计价方式）。

## 5. 企业交付 Action 与企业端点（【B】，缺口 D）

1. **Action**：企业交付使用 `nexloop.enterprise.deliver:1`（L2 新增中）。它在效应类别声明中必须是 `external_effect: true`，只有三层真实派发开关都打开才派发；负责人把它授予 relay 的 Source，并在交互计划的 `real_dispatch.actions` 中列出它（而不是非外部的 `nexloop.service.request:1`）。【待确认：随 L2 的实现合入后核对 Action 名、类别版本与服务授权】
2. **一致性检查（先于任何真实派发）**：企业对自己的端点运行 [`scripts/nx037/endpoint_conformance.py`](../../scripts/nx037/endpoint_conformance.py)，报告留在私有目录：

   ```bash
   python scripts/nx037/endpoint_conformance.py --origin https://<ENDPOINT_HOST>:<PORT> --credential-file <PRIVATE_CREDENTIAL_FILE> --ca-file <CA_FILE> --connect-address <ENDPOINT_IP> --out <PRIVATE_REPORT_FILE>
   ```

   检查项：`credential_format`（私有文件、16–4096 个 `[A-Za-z0-9._~-]`）、`post_new`（POST 200/202、字段恰好四个、回显 intent 与 `payload_digest`、状态为 `fulfilled`（企业交付的效应是“工单已持久创建”；通用协议端点可加 `--allow-accepted`）、引用格式、2 s 内）、`post_replay`（同一 Idempotency-Key 幂等）、`post_conflict`（同一 intent 换载荷返回 409 且保留原效应）、`get_found`、`get_not_found`（404、`not_found`、两个空值）、`client_end_to_end`（NexLoop 自己的客户端按 TLS/CA/SNI 与绝对截止时间完成一次派发与查询）。退出码 0 全部通过、1 有失败、2 输入错误。检查默认发送 `{"summary": "NexLoop conformance check <run id>"}`，会在端点上产生两条测试效应，应对端点的测试模式运行，或用 `--parameters-file` 给一个企业认可的无副作用载荷。
   没有企业自有端点时，使用仓库提供的参考端点（外部系统替身，员工在其页面上处理交付请求）：[参考企业交付端点部署手册](../../deploy/stage/reference-endpoint/README.md)。员工标记的处理结果只留在端点内，不回传 NexLoop（局限见该手册 §8，回传是 v0.1 之后的后续项）。
3. **效应 worker 的 provider 配置**：见 [`enterprise-provider.example.json`](../../deploy/examples/nx037/enterprise-provider.example.json)（`origin`、绝对路径的凭据与 CA、`timeout` 2 s、数字 `connect_address`）。

## 6. “结果暂时未知”演练（3 次 `unknown_reconciliation`，【B】）

用 [`scripts/nx037/unknown_drill_relay.py`](../../scripts/nx037/unknown_drill_relay.py) 在效应 worker 与企业端点之间放一个只复制字节的中继（TLS 端到端，中继看不到明文、密钥或凭据）：

```bash
python scripts/nx037/unknown_drill_relay.py serve --listen 127.0.0.1:<ORIGIN_PORT> --target <ENDPOINT_IP>:<ORIGIN_PORT> --control <PRIVATE_CONTROL_FILE> --hold-seconds 10
```

- 效应 worker 的 provider 配置把 `connect_address` 设为 `127.0.0.1`（origin 主机名仍作 SNI 与证书名），中继监听 origin 的端口；不演练时中继原样转发。
- 每次 `unknown_reconciliation` 交互开始前，负责人武装一次：

  ```bash
  python scripts/nx037/unknown_drill_relay.py arm --control <PRIVATE_CONTROL_FILE> --count 1
  ```

  下一个连接的请求照常到达企业端点（端点提交了效应），但端点的应答被扣留 `--hold-seconds`（长于 2 s 截止）后断开：效应 worker 记为结果不明。武装次数用完后，下一步的查询（GET）原样通过，系统应当**先查询对账、不重新提交**。
- `status` 子命令显示剩余武装次数与已扣留次数；控制文件必须是私有文件。
- 核对：企业端点侧该 intent 只有一次 POST；交互记录中对账结果为已了结；运行器的“同一意图执行超过 1 次”停止规则未命中。
- 使用参考端点时，也可以直接用它自带的演练开关（`reference_endpoint.py drill`，只能在本机打开），见其部署手册 §7。
- 测试：`tests/test_nx037_endpoint_conformance.py` 用合成端点覆盖明文与 TLS 两种情况：武装后派发为 unknown、端点已提交、随后的查询只有一次 GET；武装期间的查询为“不可用”，从不判为已完成。

## 7. 装配完成的核对（【A】）

- `real_tenant_init.py verify` 除开关外全部通过；
- 每个消费者 `nexloop-relay-enroll` 已输出 `enrolled: true`，`nexloop-doctor --relay-source` 通过；
- 消费者能经网关登录 WebChat 并发出一条消息，工作台 owner 与 operator 能登录；
- 企业端点一致性报告 `ok: true`；
- 然后按 [运维手册 §9.3–§9.4](../OPERATIONS.md#93-开放真实派发b) 开放真实派发并运行受控交互。
