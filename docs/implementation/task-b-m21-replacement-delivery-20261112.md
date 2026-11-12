# L7 接续 M2.1：G1、0453 与定向回归交付

本文是本地可复审候选，不能作为独立 L6 PASS、组批、部署或真实运营验收结论。没有更新 planning 状态/证据。

## 1. 分支与基线

- 工作树：沿用原 L7 树；接续分支 `codex/l7-m21-0453`。
- BASE：批次 170 `8d9f41531d38557a9e281e2734fcdf927c2af4e3`。
- 原 HEAD：`3e1de250a2a48f9208b8be06fc9eb5cad8fbd887`，原 `l7/task-b-m2`、`l7/task-b-m2-2` 均保留，未改指针。
- 重放：`0d2156a6` → `dc2f32eb`；`3e1de250` → `37fdd5ea`。冲突发生于 catalog 和 copy guard，保留批次 170 的 alert split 并追加 M2.1。
- 最终 HEAD 由此报告提交后的 `git rev-parse HEAD` 和私有短交付记录给出，避免文档自引用提交哈希。

## 2. 文件、迁移与依赖

- 新迁移 `packages/eios-core/src/eios/migrations/0453_task_b_m2_1_effect_action_version.sql`，SQL 内容与原 `3e1de250` 的临时 0452 相同，SHA-256 为 `98dfe1b983604bf3fc5371476b4f34ef74c143ec8c293e19b33f07520154a369`。
- 原有 0001..0452 共 452 条 catalog 和文件 checksum 全部核对不变，尤其 `0452_alert_evaluate_split.sql`。catalog 新增 0453，`versions.lock.json` 同步；copy guard 两套登记并存，测试通过 stem 定位迁移，不引入散落编号常量。
- 依赖：0451 的准入表、版本选择/退役函数；0446/0449 的 A′ 与 admissibility；原 registrar、intent v0040/v0041/v0052、message issuance v0049/v0190、fallback v0110、catalog v0401/hint、local delivery authority。A′ 保留到 M4。
- 重放包含 Python `effect_contexts`、`effect_execution`、`local_json_delivery`、`message_relay`、`reply_fallback`、`runtime_activation`，以及原有 fixture、copy guard、ceiling 分类登记和 M2.1 测试。
- 本次新增 G1 owner/GUC/阳性对照验证和运行单；角色签发最小诊断测试/范围报告；固定回归清单。没有修改 Role 生产实现、Host、contracts、planning 或旧迁移。

## 3. 实际执行与首次失败

完整命令参数、退出码、耗时以本地 `local-only/l7-replacement-m21/` 的 JSON、JUnit XML 和原始日志为准；该目录被 gitignore 排除。

- `git switch -c codex/l7-m21-0453 8d9f41531d38557a9e281e2734fcdf927c2af4e3`；`git cherry-pick 0d2156a6 3e1de250`；两次冲突显式解决后 `git -c core.editor=true cherry-pick --continue`。
- `LC_ALL=C.UTF-8 PYTHONPATH=packages/eios-core/src:tests .venv/bin/python -m pytest -n 2 -q -p no:warnings tests/test_task_b_m2_1_pg.py -k g1`：1 passed，6.60s。执行运行单中的 SQL 本体；实际非 superuser/bypassrls owner 有准入时拒绝、缺 GUC 看到假零、空租户拒绝、空的合成租户通过、应用角色报 ACL 错误。
- `.venv/bin/python local-only/l7-replacement-m21/run-lock-mutations.py`：每轮都先检查 load<12，刷新变异 catalog checksum；调用 `.venv/bin/python -m pytest -n 2 -q -p no:warnings tests/test_task_b_m2_1_pg.py -k waits_for_a_bind`。去掉选择 `FOR SHARE`：1 failed，6.66s（不等待）；去掉退役 `FOR UPDATE`：1 failed，6.52s（错误退役成功）。两者均在目标断言失败，非 bootstrap/环境失败；finally 原样恢复 SQL/catalog。
- Role 首轮 `-n 2`：2 failed，13.19s（错误列断言、未准入 v2 bind 拒绝未归因）；收窄为 v1 基线/已准入 v2 后，2 passed，13.53s。增加 proof 断言首次 `-n 0`：1 failed / 1 passed，20.75s（期待 ValueError，实际封装为 AuthorizationUnavailable）；保留日志并修正拒绝类型。在原夹具上补齐 proof 断言后 2 passed / 18.09s（保留为 before-fresh-step）。按 R1 改用全新 PlanStep 的最终探针，带 `--junitxml=local-only/l7-replacement-m21/role-probe-final.xml`，`tests/test_task_b_role_issuance_probe_pg.py -n 0`：2 passed / 18.03s。
- `LC_ALL=C.UTF-8 PYTHONPATH=packages/eios-core/src:tests .venv/bin/python scripts/check_definer_search_path.py`：1535 routines pin search_path，1223 SECURITY DEFINER，0 problems，退出 0。
- `.venv/bin/python docs/handoff/tools/validate_handoff.py --planning planning`：passed=true，failures=[]。仅校验，没有更新状态。
- `.venv/bin/python local-only/l7-replacement-m21/check-migration-integrity.py`：452 条已发布迁移保持不变、全部 checksum 匹配、0453/lock 一致。首次探索检查误用 lock 的 `runtime` 键导致 KeyError；改用真实 `packages.nexloop-eios-core` 后通过，未更改产品代码。
- `scripts/ci/prepush-scan.sh HEAD 8d9f41531d38557a9e281e2734fcdf927c2af4e3` 首次退出 1：系统 Git 2.24.3 不识别 `--path-format=absolute`，随后 dirname 报 illegal option；只有 gate 1 已执行，未算扫描通过。改用 `PATH="$PWD/.venv/bin:/opt/homebrew/bin:$PATH" scripts/ci/prepush-scan.sh HEAD 8d9f41531d38557a9e281e2734fcdf927c2af4e3`，四项均 PASS（gitleaks 8.30.1，base configuration）。已有 `.git/hooks/pre-push` 已核实调用相同脚本，无需改钩子；提交后仍须扫描最终 HEAD。
- `git diff --check`；`git diff --quiet BASE -- planning packages/contracts`；原 L7 两分支指针复核：通过。

### “78 文件”的精确还原

从旧 run 命令按原 HEAD 的文件树恢复，不载入旧聊天长历史。旧 run1 为 78 个参数、77 个独立文件；run2 在此基础上追加 `test_nx041_stand_ins_pg.py` 和重复的 `test_real_dispatch_switch_pg.py`，共 80 个参数、79 个独立文件。

本次保存 `regression-files-original.txt`（80 参数）与 `regression-files.txt`（79 独立文件），公开可复测清单为 `docs/implementation/task-b-m21-regression-files.txt`。完整覆盖两轮的集合，没有为凑 78 删除测试。

旧原始日志均复制保留：run1 为 750 passed / 4 failed（含 A′、bootstrap/checksum）；run2 为 781 passed / 1 failed（ceiling 未分类）。旧结果不作为本次通过证明。

本次完整回归通过 `.venv/bin/python local-only/l7-replacement-m21/run-regression.py` 执行；精确 pytest argv 在结果 JSON 内，形式为 `.venv/bin/python -m pytest -n 2 -q -p no:warnings --junitxml=local-only/l7-replacement-m21/regression-attempt1.xml` 后跟清单的每行参数。首轮启动 load=7.73583984375；上限 3000s；未跑全量 `bash scripts/ci/check`。

首轮完整回归：775 passed / 4 errors / 0 failed / 0 skipped，825.51s。四个 `test_nx068_publish_path_pg` 用例在 `test_stage_secrets.tree` 初始化证书时因系统 LibreSSL 的 `openssl req` 失败，未进入业务断言。第二轮通过 `.venv/bin/python local-only/l7-replacement-m21/run-regression-retry.py`，固定 PATH 前缀 `/opt/homebrew/opt/openssl@3/bin`（OpenSSL 3.6.4），使用相同 79 文件、-n2 和独立 attempt2 JSON/XML/log；启动 load=7.2822265625。没有修改产品代码或跳过测试来规避错误。

R1：`.venv/bin/python local-only/l7-replacement-m21/run-r1-comparison.py` 首轮在批次170和候选上各 1 failed（10.24s / 10.30s），共同定位到 claim reservation。追加“旧 step conflict / 新 step success”的完整矩阵后，两边各 2 passed；最终相同探针/fixture版本通过 `run-r1-comparison-final.py` 执行：基线 17.61s、候选 18.19s。精确 argv 为项目 Python 的 `-m pytest -n 0 -q -s -p no:warnings tests/test_task_b_role_r1_pg.py`，分别在基线一次性源码夹具与候选源码目录执行，PYTHONPATH 各自固定；命令和 hash 在 `r1-comparison-final.json`。一次性源码夹具已按精确路径清理。首次提取误用系统 Python 3.8 的 tarfile filter 参数失败，改用项目 Python 3.12 后成功；没有修改任何原树或额外创建 worktree。

第二轮完整回归：**779 passed / 0 failed / 0 errors / 0 skipped，815.78s**。原始 JSON 记录 wrapper wall time；JUnit XML 记录全套结果。bootstrap、copy guard、ceiling、M2.1 全部 13 条（含 G1、并发和 erasure）均在此轮通过。清单 SHA-256 为 `f2f4ed23793083d8a8bd11b4e269e6d91053ec6bf283a65dbe6a890092baa9ce`。

正式整理的可审计结果为 `docs/implementation/task-b-m21-replacement-evidence-20261112.json`：精确 pytest argv、两轮结果、关键门禁计数、最终 Role/R1 测试与源码 hash、两锁变异及原始日志 hash。full79 选择文件和产品代码从第二轮启动至最终提交保持相同；额外 Role/R1 文件以其最终独立运行与 hash 为证，不借用 18.09s 的旧夹具结果。提交后的扫描结果见私有短交付记录。

## 4. 验收关联与证据等级

- 本地 G1、两锁并发/变异与 Role 诊断结论见上述命令及 `task-b-m21-role-issuance-review.md`。诊断测试为 passed 意味着成功固定了拒绝现象，**不意味着 Role 后继兼容已修复**。
- 对应任务是方案 B M2.1（NX-040 相关链路），当前最低未完成阶段仍 S6。本轮不标 done。
- 关联 AT-001/002（租户/身份）、AT-026（erasure）、AT-033..037（效应恢复/幂等/当前授权/并发）、AT-047（关键测试不可 skip）：只报告所选本地回归的检查结果，不改写完整 AT 判定；完整产品与 stage 验收为 not_run。
- 测试使用隔离 disposable PostgreSQL、合成业务数据和本地 provider/受治理端口。没有真实消费者、真实 register、真实渠道外发或部署证据；没有把测试/demo 算为真实运营证据。

## 5. 未完成项与冻结

- **Role 缺口已确认，未修复**：配置 `:1` 的 Role reevaluation Run 不跟随 Consumer 选择出的 `:2`；精确 proof 无法满足 bind。需调度员/L6 定后续修复范围，禁止自动扩 Role ceiling 权限。详见专项报告。
- Host v7/v8、0110/0179/0181/0317/0398 pack 构建、所有 `:1` 位置及 Host vitest/contracts 检查留 M2.2；未新增 0454，未改 contracts。
- M2.3 必须有阻止提前于 M2.1+M2.2 部署的机制，并安排真实 register/FOLLOWABLE 验证。Role 缺口解决并经独立复核前，不能据 Host v8 单独解除准入限制。无准入 v2 的旧 PlanStep 重绑 claim 冲突在批次170已存在；新 step 对照两边均成功，未发现候选新增回退，详见 R1 报告。
- G1 运行单只在本地验证。未核实 stage 的租户清单与可 SET ROLE 的维护连接；缺少授权连接时应 blocked，不得临时授予 owner、改 RLS 或使用超级用户代替集成。报错不等于零。
- P5 r2/§6/§7 没有本轮新证据，部署冻结持续；没有连接服务器、迁移持久环境、手动准入实际租户、切 REAL_DISPATCH、真实外发、push、合 main、公开 release/tag。
- 替代 L6 已给 11:47 阶段 blocked（非最终 PASS），本轮按 R1 补归因；最终复审仍待进行，本候选不能自判正式 PASS 或自行组批。

## G1 hash 追溯补正

旧 `g1_sql_sha256=dd1c439a…` 的准确算法是 `text.split('```sql',1)[1].split('```',1)[0]`，保留 fence 后的一个 LF；测试使用 `re.search(r'```sql\n(.*?)```', text, re.S)[1]`，结果为 `d2e20c3c…`。UTF-8、无 BOM、不做 strip/dedent/换行归一化。两份输入为1367/1366字节，逐字节验证旧输入恰为一个 LF 加测试输入。旧字段及原记录保留，不抹去证据错误。

`task-b-m21-g1-input/` 保存两份原始SQL字节、完整hash/算法/0ce9c43a来源及可重现verify.py。命令 `.venv/bin/python docs/implementation/task-b-m21-g1-input/verify.py` 通过；原G1命令补跑1 passed/7.92s。没有修改运行单、业务SQL、测试或产品代码；779项回归仍仅对应原受审输入，本提交只补证据。旧reviewed-file-hashes.json是过程快照，其四项旧hash不得作为最终HEAD证据；最终覆盖范围以原交付JSON逐项hash及此次补正为准。未重跑重型回归，未扩展Role/M2.2。
