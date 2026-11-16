# NX-061 E4/A1/B5/B9 本地证据核对与 B5 判读修复

## 基线与范围

- 分支 `codex/nx061-local-evidence-20261116`，BASE `6a5ef7a98a44be8babcba71940419e3391587ff1`；独立工作树从该 main 创建，初始干净。保留原工作树和全部既有改动。
- 本轮只改 `run_outcome_taxonomy.py` 的纯判读函数、新增一个纯合成测试文件和本文；无 SQL、迁移、权限、契约、共享 planning 或锁文件变更。
- 没有远端、真实 READ、部署、准入、SDK/数据库目标探测、e972 重试或 Host 工作。没有安装依赖。使用现有项目 Python3.12.10、Node24.13.0、pytest `-n 2`。

## 四项核对：已有什么、还缺什么

| 项 | main 上已有实现/证据 | 本轮本地核验 | 当前 stage 仍需的独立证据 |
|---|---|---|---|
| E4 | `eios/api/metrics.py` 路由模板/有界序列，`http_api.py` middleware、`ApiTimings` 读写计数，load report 合并直方图。main 最新规则文件 v39 保留 `api_read_p95_high`：read p95 >500ms 持续600s、warning。旧“告警阈值未定”已不是当前源码状态。 | 复用原 registry、load report 和 `test_request_classes_count_errors_and_slow_requests`，不复制现有 middleware/HTTP 测试。验证慢请求500ms边界、5xx错误、读写分类、合并口径与 read target 仅观察。 | 当前实际已发布规则版本、各 API 进程近5分钟样本与新鲜度、路由模板计数/500ms桶；若需验证持续10分钟阳性/恢复，另审隔离合成演练。本轮未启动导出端口或读取真实接口，未声称当前 p95 达标。 |
| A1 | `schema_impact.py` 静态依赖图、`/check` 和 `schema_proposal.py` CLI 按类别列名/版本/变更属性；完整/截断/unreadable/not_reported 不混同。负责人既定“只展示，不接 F4”保持。main 规划已记录 batch49 的 F1 修复/L6 复核。 | 复用 schema impact、上游图测试，以及 CLI 的纯配置/合成回答/退出码测试；唯一真实 PG+HTTP 端到端用例明确不选，不把它记作本轮通过。 | 当前批准的租户/角色，通过受授权 `/check` 或 CLI 核一项真实提案的清单、属性与类别一致性，截断/不可读显式不完整；操作者403/跨租户404的真实集成证据。本轮不登录、不读真实 proposal，不把静态清单当发布许可。 |
| B5 | 分类和未知码失败关闭已实现；v31起样本门槛10、rate>0.2 warning。main 的 NX-061 规划记录 R71 已部署至 v32、含该规则；最新源码 v39 仍保留规则。R0 已改用事件时间计最终失败，未知结局不算成功。 | 本轮发现并修复**纯判读**对非有限值/负数/分数计数及越界比率误判为 measured 的缺口，见下节。未改 SQL evaluator、统计窗口、阈值或 Web 卡片。 | 当前实际 outcomes 快照的 decided/gate/gated rate、实际规则版本及告警状态；不足10或无数据只能记未判定。真实“≥10且>0.2”阳性、恢复及未知/主动保护拒绝口径，需已有可归因证据或另审隔离合成演练；不为凑样本制造真实效果或写 SQL。 |
| B9 | 原定时/手工区分与 file-backup stale 规则已有；v39 保留 scheduled failure、file-backup 2700s、maintenance 93600s，另有 v33起 `timer_not_reported` 防缺报静默。**旧文档2026-10-26未启用记录不能当当前事实**：main 的 NX-035 后续 note 明确记载2026-10-28文件备份已启用、两 timer 启用、一次4份 verified、v21已应用。 | 复用 `test_host_health_probe.py` 的 scripted runner 测定时/手工/不可读/缺失/超时等输入，没有调用真实 systemctl/docker。已定位该历史部署 receipt 文件并留摘要定位；仅核本地文件存在/hash，不把历史记录变成当前 timer 状态。 | 当前实际 host-health timer 配置、对应 selector/报告新鲜度、LastTrigger 与 invocation 的定时/手工归因、近24h失败计数以及 stale/not_reported 告警。若需产生失败阳性，另审隔离场景，不停真实备份、不用手工补跑刷新掩盖失败。 |

依据：`docs/implementation/NX-061-eios-gap-batch2.md`、`docs/release/run-nx061-b5-threshold.md`、main 的 `planning/tasks.json` 中 NX-061/NX-035 既有 evidence、`deploy/configuration/alert-rules.v39.json`。历史 note 是归属于原执行者/判定者的证据，本轮不重新宣称其 live 验证。当前任务仍由调度判定，本线不改状态。

## 实际补齐：B5 离线快照不得因坏数字判通过

原 `failure_rate_gate` 只检查 Python 数字类型，然后比较门槛。例如 decided=10、rate=NaN 或负数会落到 `within`；decided=NaN/Infinity/10.5 也可能成为已判样本。纯 Python/归档 JSON 判读应在无效输入时保留未判定，不能把“不大于阈值”理解为有效达标。

修复要求 decided 非负、整数值且有限（保留10.0兼容）；样本达到门槛且 gated rate 是数字时，必须在0–1内，该比较同时拒绝NaN/Infinity。无效数字返回 `no_data`；不足10、旧快照缺 gated 字段继续 `insufficient_data`，都不算通过。合法0/0.2仍 within，略大于0.2/1仍 warning，负责人阈值未改。

新增15个纯函数合成测试：10个坏数字反例、4个有效边界、1个不足/旧格式对照。已有 A1/E4/B9 测试直接复用，不重造覆盖。此修复针对本地证据判读，不声称真实数据库已产出坏数字或修复了 SQL/告警部署。

## 实际命令、首次失败和结果

运行前分别程序化检查 load1<12；首次7.925，修复回归6.571，扩大回归7.636。所有产物在 ignored `.ci-results/`，不提交。

```sh
source ~/.nvm/nvm.sh && nvm use 24
export PATH="$PROJECT_VENV/bin:/opt/homebrew/bin:$PATH"
PYTHONPATH=packages/eios-core/src:tests python -m pytest -q -n 2 tests/test_outcome_failure_rate_gate.py --junitxml=.ci-results/nx061-b5-first.xml
PYTHONPATH=packages/eios-core/src:tests python -m pytest -q -n 2 tests/test_outcome_failure_rate_gate.py tests/test_schema_impact.py tests/upstream/test_eios_impact_analysis.py tests/upstream/test_eios_api_metrics.py tests/test_load_report.py tests/test_host_health_probe.py --junitxml=.ci-results/nx061-offline-final.xml
PYTHONPATH=packages/eios-core/src:tests NEXLOOP_TEST_PG_AUDIT=.ci-results/nx061-pg-audit.txt python -m pytest -q -n 2 tests/test_outcome_failure_rate_gate.py tests/test_schema_impact.py tests/upstream/test_eios_impact_analysis.py tests/upstream/test_eios_api_metrics.py tests/test_load_report.py tests/test_host_health_probe.py tests/test_schema_proposal_tool.py tests/test_api_request_metrics.py::test_request_classes_count_errors_and_slow_requests -k 'not test_check_verify_reject_and_publish_end_to_end' --junitxml=.ci-results/nx061-offline-expanded.xml
```

- **修复前首败**：10 failed / 5 passed，0.47s，恰为新增10个非法数字反例；日志/JUnit `nx061-b5-first.*` 保留。
- 修复后六文件：74 passed，1.40s。
- 最终扩大离线范围：107 passed，0 failed/errors/skipped，2.17s；24条既有 Pydantic 字段 warning 保留；PG fixture audit 无条目。只选择纯本地部分，真实 PG/HTTP端到端明确 not_run，不用 skip 冒充通过。
- 后续仅调整纯函数 docstring 以描述输入域，算法与最终回归一致。
- 提交前执行文档/publication/definer检查及四门扫描；固定新 HEAD 仍须对 BASE 全出站扫描，精确 HEAD 与原始日志随私有交付提供。

## 结论边界

本轮离线缺口已给出代码、先红后绿和固定提交证据，供 L6 独审；NX-061 整体/stage 验收仍 not_run，不自判任务完成。当前实际规则、指标、timer/权限状态不由旧 note 或本地合成测试推断。所有远端/真实 READ、部署、准入等冻结保持；没有 e972 重试、Host 工作或共享 planning 修改。
