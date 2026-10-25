# NX-057 G1、G2：补救规则的窗口发布与部署层开关接入

- **来源**：执行单 `docs/release/run-nx057-remediation-stage.md` §0.4 列出的两个缺口。调度员 2026-10-25 派工：G1、G2 放在一个分支上，基于 main 94baecc3；随 R4 进下一批；G2 属于部署配置变更，合并时由调度员确认。
- **分支**：`l7/nx057-g1-g2`。它的第一个提交就是执行单本身（96b047dd，父提交 94baecc3）。
- **迁移**：`remediation_rule_publications`，临时编号 0344，接在 main 尾部 0343 之后。R4 的 `remediation_claims` 也用了临时编号 0344（两条分支都基于 main 的尾部），由调度员合入时一起重排。编号必须连续：`test_42_actual_bootstrap_and_publication_checksum` 要求迁移数等于锁定的 bootstrap_revision，所以这里不能跳到 0345。

## G1：补救规则经配置员窗口发布，并记录 sha256 与 expected_revision

- **新命令** `scripts/deploy/remediation_rules_apply.py`，与 `alert_rules_apply.py`、`approval_policies_apply.py` 同形：
  - `--bootstrap-dsn-file` 在进程内打开配置员窗口（`configurator_window.py`），发布后关闭；
  - `--check` 只校验，不连库，输出版本、规则数和文件的 sha256；
  - 输出一行 JSON，返回码 0 或 2；
  - 失败时只输出异常类型。数据库的拒绝（SQLSTATE 22023、40001、42501）附上数据库自己的拒绝原文。不打印 DSN。
- **按文件原文发布**：`nexloop_eios.remediation.publish_rules(text, tenant, world, expected_revision, database_url_file)` 把文件的原文和它的 sha256 一起交给 SQL。
  - `apply_rules(manifest, …)` 仍然保留，给内存中的清单用（发布它的规范化文本），同样要求 `expected_revision`。
  - `python -m nexloop_eios.remediation rules --apply` 也改走 `publish_rules`，需要 `--expected-revision`。
- **SQL**：`control.nexloop_publish_remediation_rules(tenant, world, text, sha256, expected_revision)`，只允许配置员调用。
  1. sha256 必须是原文的 sha256，`expected_revision` 不能为空或负数，否则返回 `remediation rules: invalid`（22023）。
  2. 同一租户和 world 同时只有一个发布（advisory lock）。
  3. 同一版本、同一 sha256 已经发布过：视为重放，什么也不写，返回第一次记录的 `expected_revision`。
  4. 同一版本、不同原文：拒绝（40001，`version N published with another text`）。
  5. 否则，`expected_revision` 必须等于本租户当前的规则版本（没有规则时为 0），不符就拒绝（40001），什么也不写。
  6. 然后交给原有的 `control.nexloop_configure_remediation_rules`（0338）。原有的检查全部保留：形状、源 Action 必须是声明过的外部效应、版本只能增加。
  7. 最后写入 `control.nexloop_remediation_rule_publications`：版本、sha256、expected_revision、发布者、时间。
- **记录表**：`control.nexloop_remediation_rule_publications`。
  - 只追加；
  - 有租户外键；
  - 外键指向它发布的那一行规则；
  - `expected_revision < manifest_version`；
  - 行级安全与其他补救表相同，对配置员和各服务角色都不授权。
- **不能绕过记录**：配置员不再能直接执行 `control.nexloop_configure_remediation_rules`。新函数以属主身份调用它，会话用户仍是配置员，所以原函数自己的“只允许配置员”检查照样成立。
- **与审批策略的差别**：审批策略把 `expected_revision` 写在清单文件里。补救规则清单的格式（`nexloop-remediation-rules/1`，键集合固定）不变，v1 文件也不改；`expected_revision` 由执行者在命令行给出，由 SQL 核对并记录。

## G2：`REMEDIATION_ENABLED` 接入 compose

- `deploy/stage/compose.app.example.yaml`：`plan-reevaluator` 的 `environment` 合并公共锚点（`REAL_DISPATCH_ENABLED` 照旧），再加 `REMEDIATION_ENABLED: "${REMEDIATION_ENABLED:-false}"`。
  - 只有这一个服务有这个变量：规划器只在这个进程里运行。
  - 负责人手动唤醒走 API，不看部署层开关。
- `deploy/stage/compose.app.env.example`：`REMEDIATION_ENABLED=false`，并写明只有精确的 `true` 才打开。
- 连接预算不变：没有新增服务或连接池。

## 测试

- `tests/test_remediation_rules_apply.py`（不连库）：
  - `--check` 输出文件的 sha256；
  - 缺少租户、bootstrap 文件或 expected_revision 时拒绝，并且不开窗口；
  - 发布时传的是文件原文；
  - 任何异常都不带出 DSN；
  - 数据库的拒绝原文按 SQLSTATE 附上。
- `tests/test_remediation_rule_publications_pg.py`（真实 PG、真实配置员）：
  - v1 文件经命令发布。expected_revision 不符时被拒，什么也不写；
  - 发布记录中的 sha256 等于文件的 sha256，expected_revision 为 0，发布者是配置员；
  - 重跑是重放；同版本换原文被拒；
  - 配置员不能直接调用原配置函数；sha256 不符、expected_revision 为空都被拒；其他会话被拒；
  - 记录表只追加。
- `tests/test_remediation_pg.py`：
  - 夹具按当前规则版本给出 expected_revision；
  - “同版本换内容”的断言改为新的 40001 拒绝；
  - 新增“版本可以跳号，但不能回退”。
- `tests/test_stage_compose.py`：新增 `REMEDIATION_ENABLED` 只出现在 `plan-reevaluator`，默认 false，可由 env 打开；有 docker 时用 `docker compose config` 实际渲染核对。

## L6 对执行单 96b047dd 的复核（同一分支处理）

- I-1：执行单写明 env 示例的准确路径 `deploy/stage/compose.app.env.example`，以及它在 APP_HOST 上对应的私有文件 `$NEXLOOP_ROOT/config/compose.app.env`。
- I-2：执行单 §4.1 加上停止条件：积压没有清空，或者规则版本不是恰好 1 时，不打开部署层开关。
- I-3：默认 false 由 `test_stage_compose` 的新测试锁住（示例 env 中的行、未设置和空值都渲染为 false）。
- S-1：写明第 1 步和第 2 步可以互换。
- S-2：P2 的查询列（`resource_id`、`active`）已对照 0008 核对，执行单要求先只读试跑一次。
- S-3：证据中记下 `$LAST_REGISTER` 的实际值。
- S-4：§4.3 由负责人在工作台执行，调度员用查询判定。
