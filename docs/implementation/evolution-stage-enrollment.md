# 演进服务主体的 stage 登记（`scripts/deploy/evolution_enroll.py`）

**依据**：
- 负责人 2026-10-21 定案（按推荐）：在 stage 上注册演进 writer 和 evaluator，用来执行 S6 的 stage 验收；
- NX-038 E4/E5（0235、0237）；
- NX-039 V1/V3（0241、0248）。

本工具不新增迁移，也不修改契约。

## 需要哪些东西（分析结论）

| 项 | 是否必需 | 依据 |
|---|---|---|
| **writer 的服务凭据** | 是 | `runtime.nexloop_evo_writer_tenant`（0235/0241）要求同时满足：<br>- subject_kind 为 `service`；<br>- 没有 Run context；<br>- 不是浏览器 token；<br>- world 为 real；<br>- 主体没有登记为 evaluator。<br>**不需要任何 EIOS grant**：这些入口只读取身份快照，不做 Action 授权 |
| **evaluator 的服务凭据** | 是 | `runtime.nexloop_evo_evaluator_identity`（0241）的要求与 writer 相同，另外要求主体在 `authz.nexloop_evo_principals` 中登记为 evaluator |
| **`authz.nexloop_evo_principals`** | 是 | 每个主体一行：writer 一行，evaluator 一行。主键是 principal_id，所以一个主体只能有一种用途（D39-1，即 S6-R5） |
| **测试用评估凭据（D18）** | 是（L1 需要） | `authz.nexloop_evo_l1_open`（0248）要求该凭据已登记在 `authz.nexloop_evo_evaluation_credentials`，`worlds={test}`，并且**没有任何 Action grant**。overlay（0237）也会用到它，经由 `nexloop_api` 会话 |
| **授权来源** | 不需要授权清单 | 这三个主体都不需要 grant，所以不写入 service-grants 或 stage-principals 清单。工具复用 `nexloop_eios.service_grants` 的 `compile_principal`、`plan`、`apply`，写入身份类事实，然后通过 0050 configurator 签发凭据。凭据绑定中有一个标签作用域 `nexloop.evolution`，因为绑定要求至少 1 个作用域。没有任何入口检查这个作用域，也没有对应的 scope authority 事实 |
| **数据库 LOGIN 角色 `nexloop_evo_writer`、`nexloop_evo_evaluator`** | **是** | 入口都检查 `session_user`，必须正好是这两个角色名。现有的应用角色调用不了，因为函数只授权给这两个角色。两个角色已由 0235 和 0241 创建，属性是 LOGIN、noinherit，**没有口令**。所以需要：<br>- 在 bootstrap 窗口中为它们设置口令，口令进钥匙串；<br>- 在 DATA_HOST 的 pg_hba 中加一条规则。<br>这两步都不由本工具完成，工具会输出 `operator_sheet` |
| **两张登记表的写入权限** | 需要 bootstrap 窗口 | 这两张表对所有应用角色和 configurator 都做了 revoke，也没有 definer 入口，原设计写的是“由 trusted configuration 登记”。所以登记这一步只能用超级用户或 nexloop_owner 会话，也就是 `registry_database_url_file`。以后如果需要，可以加一个 configurator definer |
| **常驻进程** | 不需要 | 诊断、候选、评估（包括 L1 会话）都通过 `CandidateStore` 和 `EvaluatorStore` 的一次性调用完成，不需要常驻服务。验收脚本以对应角色连接，读取凭据文件即可 |

## 工具

- **调用方式**：私有配置文件（0600）加一条命令。配置示例见 `deploy/examples/evolution-enroll.example.json`。

```bash
python3 scripts/deploy/evolution_enroll.py --config <私有配置> --dry-run
```

```bash
python3 scripts/deploy/evolution_enroll.py --config <私有配置>
```

```bash
python3 scripts/deploy/evolution_enroll.py --config <私有配置> --check
```

- **执行步骤**：每一步都可以重复执行，第二次执行时不会有任何变化。
  1. **token**：在 `<secret_dir>` 下生成三个凭据文件：`evolution-writer/service-credential`、`evolution-evaluator/service-credential`、`evolution-evaluation/test-credential`。
     - 文件权限 0600，只生成一次；已经存在的文件原样复用。
     - 三个 token 必须互不相同。
  2. **凭据**：通过 configurator 签发凭据。writer 和 evaluator 用 world real，评估凭据用 world test。不写任何 grant、scope 或 policy。
  3. **登记**：在 bootstrap 窗口中写入 `nexloop_evo_principals` 和 `nexloop_evo_evaluation_credentials`。如果主体已经以其他用途或在其他租户下登记过，工具会拒绝。
  4. **自检**：
     - `writer_evaluator_exclusive`：writer 和 evaluator 用途互斥；
     - `evaluation_credential_test_only`：评估凭据只属于 test；
     - `no_effect_authority`：三个主体都没有任何 grant、scope、policy 或 rule，作用域只有标签作用域，也就没有效应权限（AT-046）；
     - 两个 LOGIN 角色都存在。
- **dry-run**：只读取数据库，打印计划，不生成文件，也不写数据库。
- **输出**：一个 JSON 对象，不包含 token、口令或 DSN。出现异常时只输出异常的类名。
- **退出码**：0 表示完成；1 表示自检失败或被数据库拒绝；2 表示输入被拒绝。

## 操作单（工具不做的部分）

1. 在 bootstrap 窗口中为 `nexloop_evo_writer` 和 `nexloop_evo_evaluator` 设置口令。口令直接生成进钥匙串；DSN 分别写入 0600 文件，与凭据文件放在一起。
2. pg_hba：两个角色只允许从执行演进命令的主机连接，使用 scram-sha-256 或 stage 的客户端证书规则，不允许 trust。具体写法由调度员确定。
3. 凭据有效期由 `credential_expires_at` 指定。到期后需要轮换：换新 token，并签发新的 credential_id。这不在本工具的范围内。

## 测试（`tests/test_evolution_enroll_pg.py`，真实 PostgreSQL）

- dry-run 不写入任何东西：五张相关表的摘要不变，也不生成文件。
- 登记之后自检全部通过；第二次执行 changed 为 false，dry-run 显示无事可做，数据库没有任何变化。
- 互斥：同一主体无法登记第二种用途；evaluator 的 token 不能写入，writer 的 token 不能开评；把同一个 token 复用到两个文件时会被拒绝。
- 评估凭据只属于 test：登记行存在、`worlds=['test']`、在 real 下无法认证；登记表的触发器会拒绝 real 凭据；未登记的 token 会让自检失败（阳性对照）。
- 没有效应权限：没有 grants、scope、policies 或 controls 事实；手工加入一条效应 grant 后自检会变红（阳性对照）。
- 验收前提：只用登记出来的凭据，依次完成以下步骤：
  1. writer 开诊断；
  2. 提出候选；
  3. 冻结 L0+L1 bundle；
  4. evaluator 开评；
  5. 用测试评估凭据开 L1 会话，并关闭会话。
- 输入被拒绝时数据库不会被改动，包括配置文件权限不是 0600 的情况。
