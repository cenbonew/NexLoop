# NX-040 Host v7/v8 兼容本地候选交付（2026-11-12）

## 1. 分支、基线与范围

- 分支：`codex/host-v7-v8`。
- BASE：`9e6c572d479f9c65134508270f9c4c91f878910f`，开工时 HEAD 与此一致、工作树干净。
- 候选 HEAD：以包含本文件的本地分支提交为准，`git rev-parse codex/host-v7-v8`；精确最终 hash 随调度短交付提供。
- 范围：`apps/agent-host/src/context-input.ts`、`runtime-host.ts`、`runtime-effect-tools.ts`，以及 Host 对应测试和本文。未修改 Role 恢复文件、Python/SQL、迁移、catalog、版本锁、部署配置、planning 或任何契约文件。
- 使用 main 已有的 `context-pack-v8.schema.json`、`examples/context-pack-v8.valid.json`、`generated/contracts.ts`；不修改 v7/v8 契约迁就 Host。

解析保留原始 pack 与 attestation；v8 内部复用各层已有 core 校验，精确 Action 字符串不会改写为 `:1`。只有已验证 v8 envelope 能开启内部版本化规则，外部 v1–v7 入口不能传入该开关。v8 配置兼容 v6/v7，v6/v7 配置拒绝 v8。v7 原有 Role Run 限制保留，v8 支持 schema 的 message/Role 形状；human_answers 仍只进入 Role Run 的 evidence，不能变成正式事实。

执行 Action 只从已准入 activation 的 pack 读取：service 来自 `current_constraints.action`，enterprise 来自 `effect_supplies[].action`。仅允许两个已知 family，版本 `1–9999`；offering 的 `delivery_action` 按 `stable_name` 比较，执行保留所绑定的精确版本。两个 enterprise 版本不能同时占同一 offering slot，不选最高版本。v8 的 `:1` 同样精确发送；旧协议请求形状保留。v8 primary 为 enterprise 时不挂 service 工具，避免把 enterprise resource 当回复资源。

## 2. 迁移与依赖

无迁移，无新表，无依赖锁变更。没有执行 register、真实准入或部署。

**尚未接通的后端边界：** 当前 `runtime_control.py` 的 effects HTTP 入口只接受 stable name；`runtime_activation.py` 也以 stable name 识别 slot，再从受信 Run 的 `allowed_resources` 推导唯一精确版本。Host 的 v8 精确 `action` 请求会被现有 HTTP 入口拒绝。本候选保留拒绝，不删版本、不尝试旧协议、不以 HTTP fixture 代替真实 guard 验证。

最小后端提案（另线另审，本线未实现）：为 effects submit/find 增加明确的精确 resource 输入规则，严格解析已知 stable name 和版本，并将请求版本与受信 Run/已绑定 effect context 的唯一 `action_resource` 比较；不匹配立即拒绝。旧协议继续原有 stable-name 请求规则；不能仅将精确输入截掉版本后执行。该工作与 Python 五个 pack 构建函数、后继版本准入和 Role 签发补项分别验收。

## 3. 实际工具链、命令与首次失败

Node `v24.13.0`，pnpm `10.32.1`，TypeScript `6.0.2`，Vitest `4.1.11`，Python `3.12.10`，OpenSSL `3.6.4`。命令均在本线独立工作树运行。`PROJECT_PYTHON` 指向项目既有 Python 虚拟环境；TLS 测试 PATH 固定本地 OpenSSL 3。未运行全量 CI 或 PG 重型 lane。

```sh
source ~/.nvm/nvm.sh && nvm use 24
pnpm install --frozen-lockfile
pnpm run build:pi
pnpm --filter @nexloop/agent-host build
pnpm exec vitest run apps/agent-host/test/context-v8.test.ts --maxWorkers 2
PATH=/opt/homebrew/opt/openssl@3/bin:$PATH pnpm exec vitest run apps/agent-host/test --exclude 'docs/tmp/**' --maxWorkers 2 --reporter default --reporter junit --outputFile.junit=.ci-results/host-v7-v8-complete.xml
"$PROJECT_PYTHON" scripts/generate_contracts.py --check
PYTHONPATH=packages/eios-core/src:tests "$PROJECT_PYTHON" -m pytest -q -n 2 tests/test_context_pack_v8_contract.py tests/test_contract_generation.py tests/test_generated_contracts_import.py tests/test_contracts.py tests/test_published_contracts.py --junitxml=.ci-results/host-v8-contracts-complete.xml
"$PROJECT_PYTHON" scripts/check_docs.py
"$PROJECT_PYTHON" scripts/check_publication.py
"$PROJECT_PYTHON" scripts/check_definer_search_path.py
PATH="$PROJECT_VENV/bin:$PATH" bash scripts/ci/prepush-scan.sh HEAD 9e6c572d479f9c65134508270f9c4c91f878910f
```

- frozen 安装成功；首次因 vendor Pi 尚未 build，提示缺 `pi-ai/dist/cli.js` 的 bin 链接；按仓库 `build:pi` 构建后 Host build 成功，未提交产物或 symlink。
- 最初 v8 解析定向：32 passed，0.751s。
- 首轮 Host 全套：560 passed / 1 failed / 0 skipped，32.66s（load1=6.412）。失败为模拟工具的占位 enterprise 数据被新增真实 Action 检查提前拒绝；修正为模拟路径不构建真实 enterprise 工具，原模拟测试保留。
- 修正后全套：562 passed / 0 failed / 0 skipped，32.72s（load1=6.091）。
- 补充 Role policy 阳性/负例和公开 example 消费后，定向 37 passed / 1 failed；扩大回归 568 passed / 1 failed，33.38s（load1=5.444）。新夹具混用了两个 Run 的 trigger，原 command/trigger 绑定正确拒绝；只修正夹具的 `current_event`，未放宽生产校验。
- 修正夹具后四文件定向：82 passed / 0 skipped，7.03s。
- 契约生成检查：50 canonical contracts 一致；初轮三个消费文件 100 passed / 0 skipped，3.05s。已有 Pydantic `schema` 字段遮蔽 warning 保留。
- 首次 prepush 扫描前三门 PASS，第四门因系统 `python3` 为 3.8.2，执行受信 gitleaks 配置结构检查时报 traceback 而失败；将项目 3.12.10 虚拟环境的 bin 前置 PATH 后重跑，不修改扫描规则或配置。
- 最终完整结果见下节；所有首轮与重跑 JUnit/log 保存在 ignored `.ci-results/`，不提交运行产物。

## 4. 验收证据

- Host build 成功；最终完整 Host 回归：48 文件、569 passed、0 failed/errors/skipped，33.00s，启动 load1=6.802。证据：`.ci-results/host-v7-v8-complete.xml`、同名 `.log`。
- 最终五文件契约检查：160 passed、0 failed/errors/skipped，2.23s（已有 warnings 保留）；生成检查 50 canonical contracts 一致。证据：`.ci-results/host-v8-contracts-complete.xml`、同名 `.log`。
- 提交前文档检查 0 findings，publication 0 violations，definer search_path 0 problems；最终候选再次执行同组检查及精确 `BASE..HEAD` 四门扫描，结果随调度短交付。
- 首次失败与夹具扩展失败分别保留在 `.ci-results/host-v7-v8-first.xml` 和 `.ci-results/host-v7-v8-final-expanded.xml`。

覆盖：v7/v6 既有回归；v8 service/enterprise 的 `1/2/37/9999` 精确透传；stable_name offering 匹配；非法版本、跨 family、重复 slot、绑定 tenant/world/command/provenance、预算、READ decision、Role policy 和协议错配拒绝；实际 TLS submit/find 请求体；模型参数不能覆盖绑定 Action；HTTP 拒绝后无降级重试；模拟仍只调用 stand-in。

NX-040 仅提供 Host 局部本地证据。AT-024/AT-027 的整体产品验收、真实 PG guard 的 v8 HTTP 集成、stage 和外部效果均为 `not_run`；不因本地测试通过宣称正式 PASS 或任务 done。最终独立判定交 L6。

## 5. 局限与调度后续

本候选停止在本地分支，不 push、不合 main、不连接服务器。现有 Python HTTP 的精确版本输入缺口须另行实现和审查；在此之前 v8 effects 调用 fail closed，不具备端到端可用性。无部署、持久迁移、`:N` 准入、REAL_DISPATCH、真实外发、release/tag 的新授权；Role 恢复产品策略及封停清理不在本线范围。
