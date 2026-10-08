# Codex → Claude 调度交接

2026-10-09；Codex 按负责人要求停止。NX-001..NX-017 done，NX-018 **in_progress**，不再启动其他 S3 任务。

## Git 与真相源

交接编写时已同步的 origin/main：`0fe87986e493fd48f2c1e75d13c6c5859a14bbe6`，含 PR #1 的0065受治理回执恢复及已推送的两份交接文档。推送后的 planning 证据及本文快照更新还会作为后续提交同步；最终含本文的 tip 请执行 `git rev-parse origin/main`，本次最终回复也给出最终 hash。文档不能包含自身提交的 hash，以上是明确的已推送代码快照。

`planning/tasks.json` / `acceptance-tests.json` / `traceability.json` 是实时状态；public handoff 冻结在 docs/handoff，唯一 live JSON Schema 在 packages/contracts，唯一版本锁 versions.lock.json。先读 AGENTS、ADR-019 和 docs/handoff 的 ADR-019 修订段落。候选、未执行命令与 mock 通过不得冒充主线验收。NX-018 的逐项接续见 `docs/implementation/NX-018-handoff.md`。

## 已完成任务索引

下面每行给关键代码/审计路径和可复测命令；历史实际执行及首次失败以任务 evidence 指向的报告为准，**本轮未重新执行所有这些命令**。Python命令统一先设置下节 PYTHONPATH。

|任务|关键代码/证据路径|关键复测命令|
|---|---|---|
|NX-001|.gitignore；docs/s0/publication-baseline.md|`uv run --frozen python scripts/check_publication.py`|
|NX-002|docs/s0/source-reuse-inventory.json；eios-write-path-audit.md；migration-catalog-audit.md|`uv run --with jsonschema --with pyyaml --with rfc3339-validator python docs/handoff/tools/validate_handoff.py --planning planning`；源码复核不是运行测试替代品|
|NX-003|versions.lock.json；docs/s0/pi-evo-audit.md；vendor/pi|`uv run --frozen python scripts/check_pi_source.py`|
|NX-004|docs/s0/server-inventory.md；dev-machine-inventory.md|只读 `ssh hengce 'id; sudo -n true'` / `ssh sice 'id; sudo -n true'`；硬件盘点命令见公开报告，不在CI中连LAN|
|NX-005|packages/contracts；scripts/generate_contracts.py；第三方许可|`uv run --frozen python scripts/generate_contracts.py --check`；`uv run --frozen pytest -q tests/test_contracts.py tests/test_review_contracts.py tests/test_contract_generation.py`|
|NX-006|packages/eios-core/src/eios；PROVENANCE.json|`uv run --frozen pytest -q tests/test_bootstrap.py`|
|NX-007|nexloop_eios/local_artifacts.py、postgres_artifacts.py|`uv run --frozen pytest -q tests/test_local_artifacts.py tests/test_postgres_artifacts.py`|
|NX-008|eios/migrations/catalog.json；nexloop_eios/authorization.py|`uv run --frozen pytest -q tests/test_bootstrap.py tests/test_postgres_authority.py`|
|NX-009|deploy/community；compose_bootstrap.py；doctor.py|`uv run --frozen pytest -q tests/test_compose_bootstrap.py tests/test_doctor.py tests/test_community_container_contract.py`|
|NX-010|scripts/ci/check；scripts/ci/prepush-scan.sh；test-prepush-scan.py|`uv run python scripts/ci/test-prepush-scan.py`；`scripts/ci/prepush-scan.sh`；完整CI见下|
|NX-011|nexloop_eios/http_api.py；apps/web；apps/agent-host|`uv run --frozen pytest -q tests/test_http_api.py tests/test_browser_http.py`|
|NX-012|object_actions.py；run_credentials.py；action_governor.py|`uv run --frozen pytest -q tests/test_agent_governed_write.py tests/test_run_credentials.py`|
|NX-013|durable_queue.py；runtime_dispatch.py；valkey_wakeup.py|`uv run --frozen pytest -q tests/test_durable_queue.py tests/test_queue_process_recovery.py tests/test_queue_commit_lease.py`|
|NX-014|apps/agent-host/src/pi-runtime-adapter.ts；runtime_activation.py|`pnpm exec vitest run apps/agent-host/test --exclude 'docs/tmp/**'`；`pnpm exec vitest run vendor/pi/packages/durable/test/nexloop-sqlite-conformance.test.ts --exclude 'docs/tmp/**'`|
|NX-015|effect_intents.py；effect_dispatch.py；effect_execution.py|`uv run --frozen pytest -q tests/test_effect_intents.py tests/test_effect_dispatch.py tests/test_runtime_effect_recovery.py`|
|NX-016|web_chat_http.py；native_web_inbound.py；local_json_delivery.py；apps/web|`uv run --frozen pytest -q tests/test_web_chat_http.py tests/test_local_message_delivery_assembly.py tests/test_message_runtime_effect_e2e.py`|
|NX-017|tests/test_infrastructure_faults.py；test_precise_pg_admission_outage.py；test_local_effect_worker_kill.py|`uv run --frozen pytest -q tests/test_infrastructure_faults.py tests/test_precise_pg_admission_outage.py tests/test_local_effect_worker_kill.py`|

上述 nexloop_eios 模块均在 packages/eios-core/src/nexloop_eios 下。任务 done 的粒度不等于所有70个产品验收通过；当前18 passed、52 not_run。

## 本机实际环境与入口

- macOS arm64；本次实际 Node **v24.13.0**（.nvmrc）；Python **3.12.10**（uv pin3.12）；Homebrew PostgreSQL **18.4**。pgvector0.8.5为已有本机依赖；不要让PATH miniconda3.13替代uv Python。pnpm10，锁 pnpm-lock.yaml；uv依赖锁 uv.lock。
- 无 Makefile。权威入口 `bash scripts/ci/check`；它做 publication/Pi源/类型漂移/planning检查、Node/Web/Pi/Host构建、SDK/Web/Scope/Python/Pi/Runtime测试并拦关键skip。输出 .ci-results，dist/output/sqlite/coverage等忽略；不要git add -A。

```sh
cd /Users/chenbowen/253383134/NexLoop
source ~/.nvm/nvm.sh
nvm use 24
export PYTHONPATH=packages/eios-core/src:tests
uv sync --frozen
bash scripts/ci/check
```

最后一次单次完整CI在0056：Python **1636 passed / 1 failed / 1869.98s（约31.2分钟）**，总入口还包含安装、构建和其他测试，预留约35–45分钟，最新0065全量时长未测。首次失败是 Message Relay 同一时间戳的小数尾零表示比较；修复后完整相关模块25 passed68.16s，Pi24/Runtime124尾部独立跑过。报告 `head0056-ci-resolution-evidence.json`；**不是第二次完整CI通过**。0063..0065使用有记录的目标回归，不能据此声称最新全量全绿。

0065本轮实际命令：
```sh
source ~/.nvm/nvm.sh && nvm use 24 >/dev/null && PYTHONPATH=packages/eios-core/src:tests uv run --frozen pytest -q tests/test_role_post_accept.py tests/test_receipt_reconcile_roles.py tests/test_receipt_tail.py tests/test_receipt_revoke_wait.py tests/test_receipt_terminal_rows.py tests/test_role_tail.py tests/test_bootstrap.py --tb=short --junitxml=.ci-results/nx018-core65-main.xml
source ~/.nvm/nvm.sh && nvm use 24 >/dev/null && PYTHONPATH=packages/eios-core/src:tests uv run --frozen pytest -q tests/test_role_pi_effect_checkpoint.py --tb=short --junitxml=.ci-results/nx018-core65-pi.xml
```
结果33 passed327.88s + 1 passed46.96s，0skip，首次主线运行均成功；源冻结442文件零变化。见 core65-receipt-evidence.json。

## PostgreSQL 路径、端口和生命周期

`tests/conftest.py::pg` 每例创建独立 mkdtemp PGDATA、Unix socket和随机空闲端口，启动 listen_addresses=''，测试结束由 fixture fast stop 并删除**该精确测试目录**；不能接入已有业务PG。测试 bootstrap身份仅用于DDL/可信控制fixture/故障注入/只读断言；实际业务操作使用受限服务角色和EIOS授权链。

本次只读盘点保留一个既存测试PG：父PID **89849**，PPID1，端口 **49624**，PGDATA：
`/var/folders/bf/9lg4j6ws32j8qhd0l_83dc9m0000gn/T/nexloop-pg-2yn6w4yk/data`

socket为同级 `socket/`。读 control.schema_migrations 得 **0056 / 56条**；只有盘点psql自身客户端，没有活跃测试客户端。此实例不是0065回归实例；具体原测试归属未确认，Codex未停止它。

实际fixture使用的启动/停止模板（**此处文档命令未作为收尾操作执行**）：
```sh
pgbin=/opt/homebrew/opt/postgresql@18/bin
# test_root 必须是调用者自行创建的独立 disposable mkdtemp；test_port 是其随机空闲端口。
"$pgbin/initdb" -D "$test_root/data" -U nexloop_bootstrap --auth=trust --no-locale -E UTF8
"$pgbin/pg_ctl" -D "$test_root/data" -l "$test_root/postgres.log" -o "-k $test_root/socket -p $test_port -c listen_addresses=''" -w start
"$pgbin/pg_ctl" -D "$test_root/data" -m fast -w stop
```
既存上述实例如将来经负责人确认要停止，应以其明确PGDATA调用pg_ctl；**不要复制initdb命令重建该目录**。当前子进程PID为89850/89851/89852（IO）、89853（checkpointer）、89854（writer）、89856（WAL）、89857（autovacuum）、89858（logical replication）。没有本团队仍在跑的 pytest/Host/Pi；普通系统和负责人其他进程不属于本次收尾。

## 迁移目录

唯一生产定义是 `packages/eios-core/src/eios/migrations/catalog.json`：lineage nexloop-eios-v1，**全局最高0065，65条**。所有schema共享一个control.schema_migrations迁移owner；没有各schema独立流水号。旧checksum不可改；锁bootstrap_revision0065；DRAFT候选不是迁移。

静态目录按显式 CREATE/ALTER TABLE/FUNCTION/VIEW/TYPE 或 CREATE INDEX 的 schema-qualified DDL 扫描，schema最后定义变更为：

|schema|最高显式定义变更编号|最后被目录迁移引用编号|
|---|---|---|
|control|0061|0065|
|runtime|0065|0065|
|ontology|0061|0064|
|authz|0065|0065|
|extensions|无上述DDL；扩展对象由CREATE EXTENSION管理|0065|
|public|无NexLoop上述显式DDL|不作独立编号|

该表是文件静态定位，**不是持久部署已迁移证明**；新clean bootstrap必须跑到0065，仍运行的孤立PG仅0056。上下文/Role/helper引用其他schema不等于那个schema新增迁移。禁止原生产DB或无备份的持久环境迁移。

## 已知问题、验证边界与接手坑

- NX-018三个实现gap/冻结候选/准确下步见NX-018-handoff.md。Role.ceiling_ref/scope仍metadata；v5独立候选真实Pi失败，不可整包覆盖main。formal当前READ和关系v4分别有候选通过，但**组合未测**。候选留在ignored docs/tmp供本机接续，不发布原始drop/私有附件。
- AT-004/009仍not_run，不能将receipt局部修复标成全部Role验收。AT-015 mounted UI恢复不足，现有socket/parser/cache证据不代替实际页面；没有本轮新浏览器验证。planning当前没有blocked/skipped状态项，52 not_run不能解释为已通过或统一blocked。
- /health/ready503是尚无运营readiness证明的显式状态，不是改HTTP200即可完成。双并发Pi旧探索503；最新Role policy候选submit2.041s超过工具HTTP2s，须定位授权/锁/总deadline。不要只增timeout。
- 默认deterministic provider无真实模型声明；0056有独立bounded DeepSeek synthetic证据（见NX-016-core56-verification.md和real-model报告），不等于当前Role模型/生产渠道/部署成功。实际模型验证仅显式opt-in，不让CI读.env。渠道凭据不足只阻塞渠道验收。embedding纯文本/维度尚未实测，交给NX-021，禁止先建索引；S3前用FTS+pg_trgm。
- MODEL/EMBEDDING凭据非空文件优先环境变量；禁值进入代码、日志、fixture、prompt和前端。保留.env/secret私有文件，不触碰负责人填值。新的embedding变量以.env.example/ADR-019为准。
- EIOS真实metadata发布会改变directory hash，使旧ServiceSession失效；测试应以**同一个真实Worker credential重新认证**，不能借Source/伪造人类。QUERY和独立receipt Recovery不能依赖已结束Role的权限，但仍需自己的当前独立授权。后台热刷新可用性不要与绕过撤权混淆。
- PostgreSQL JSON时间格式可能截尾零：比较时刻语义而非字符串，不延长原expiry。发布ActionContract不可原地编辑；测试用新版本发布及旧版本deactivate。
- 真实effect accepted/unknown必须QUERY/reconcile先，不重复POST；一业务意图幂等不是泛化exactly-once。只有EIOS受治理Action可写正式业务对象。
- ADR-019要求人类ontology.schema.review审核；模型和pending候选不能改Schema；hypothesis/awaiting_definition不得进Context正式属性区。下一调度顺序018→044 gap→019→021 endpoint probe→020→045→046，其余以planning依赖为准。
- NEX-EIOS只读冻结release工作树才是上游依据；main clone有未提交生产改动，不读其他生产产物。独立vendor无pip/submodule/历史导入。
- 多Agent应独立ignored clone+源SHA冻结，Root单人做集成。现有owned候选都已停；半成品组合目录也明确冻结，不要当已通过。先检查 exact baseline再apply，不能用整旧模块覆盖新private wrapper链。

## 提交、推送与停止

当前pre-push hook已安装到本机.git/hooks/pre-push；新clone必须自行安装等价hook。`scripts/ci/prepush-scan.sh [ref] [base]`默认main/origin/main，检查tracked禁公开路径、每个未推送commit的限制标识、历史新增行凭据值；任何命中禁止push，先清历史。feature分支同样扫。PR #1正文已贴三项PASS输出并完成合并。禁止mirror、local-only分支、force main。

Codex将在本文与NX-018最终in_progress证据提交/扫描/同步/validator通过、status干净之后停止；不启动后续S3任务，不关闭上述PG。最终origin/main的hash由收尾回复及本地git提供。
