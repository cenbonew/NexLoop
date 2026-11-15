# S6 本地核对与最小修复（2026-11-16）

基线：main `6a5ef7a98a44be8babcba71940419e3391587ff1`。本批在独立 worktree、`codex/l7-s6-offline-20261116` 开发，未改共享 planning。范围仅本地源码与合成回归；没有远端、真实 READ、数据库连接/目标探测、迁移执行、部署、准入、Host 或旧诊断重试。

## 当前基线与已有证据

live `planning/tasks.json` 中 NX-038/039/040 均为 `in_progress`；`planning/acceptance-tests.json` 中 AT-023/024/048 均为 `not_run`。本批不更改状态，也不把下面的组件测试升格为整体验收。实际 catalog 为 `nexloop-eios-v1`、453 项、最后 `0453_task_b_m2_1_effect_action_version.sql`；这是仓库内容，不是远端已应用事实。

| 范围 | main 已有实现/运行单 | 已有负责人裁定与本地覆盖 | 本轮证据/边界 |
|---|---|---|---|
| NX-038 / AT-023 | `evolution/candidate.py`、`lifecycle.py`、`attribution.py`、`replay.py`；0233–0235、0237；`run-nx038-stage.md`、`S6-stage-acceptance.md` | NX-038 design §12 D15–D20；D16 合成评估不冒充真实模型。`test_evolution_e1.py` 验证接受只到 accepted_for_release；E5 的纯 L0/overlay 用例验证 real 拒绝候选叠加层 | E1 与 E5 纯用例通过。正式对象/active 不变的 PG 事务与 stage 路径本轮未执行，AT-023 不改 passed |
| NX-039 | `evaluation.py`、`l1.py`、`schema_gates.py`；0241/0242、0248–0250；`run-nx039-stage.md` | design §13：负责人2026-10-20接受 D39-1..14，生成/评估主体分离、留出最多5次、硬门槛独立于得分、人工发布/回退、D16 合成替身。live planning 已更正旧“V5三个钩子未接”笔误：0250 已接，不能重复列成当前缺口 | V2/V3/F4 纯评估门槛、复现/预算/合成用例回归通过；真实身份分离、留出消费次数、发布/回退与观察期事务未执行 |
| AT-048 | `attribution.split_cases`、E5 `check_bundle/replay_l0`；0286 consumer leakage 与0303性能修正；运行单 at048 | 按消费者簇重分组，冻结评估包再次拒绝跨侧同簇；近重复/服务端消费者推导另有 `test_at048_consumer_leakage_pg.py` | 直接复用现存 E4 分组用例（40个 seed）及 E5 BundleLeak 用例，无新增重复测试。PG 与 stage 泄漏/冻结留出验证仍未执行 |
| NX-040 / AT-024 | `evolution/schema_compat.py` F1、`schema_gates.py` G9–G13；0252–0254；`run-nx040-stage.md` | design §4.4/§5：后继对象引用只允许版本变化、定义与权限分开；D22 首批全人工发布。D23 后来的开放输入例外已由现有工具回归实现，不在本批重做。ADR-046 A′/准入冻结不变 | 修复下述 F1 引用身份遗漏；运行现存 schema/F4 纯回归。没有修改 SQL/授权/发布入口；不能据此宣称实际发布可越权或完整 AT-024 已通过 |

**AT-024 判定人冲突**：`planning/acceptance-tests.json` 的既有证据指定最终判定人为负责人，而 `docs/release/run-nx040-stage.md` 开头及证据段写为 L6。目前没有负责人变更最终判定人的裁定，不能以运行单文字或本批源码 PASS 自动改派；暂按**负责人待判、L6 技术复核**处理，调度员仍为执行人。2363b593 的 L6 源码 PASS 仅覆盖本地修复，不是 AT-024 最终验收。本次只在审计中显式记录冲突，不改共享 planning 或验收状态。

三个运行单共用演进主体、预算与合成窗口；schema_only/负责人指定模板不替代演进轨迹验收。`run-nx040-stage.md` 的共用总序把删除、语义发布/回退和可选正式 Schema 发布分开，执行、技术复核与最终判定分开，AT-024 以上述冲突说明为准。本批不运行其中任何命令、不读取凭据。旧文档中的迁移/部署数字只作历史，未当作当前目标事实。

## 实际发现与修复

`schema_compat._tool_signature` 原来 zip 比较 `object_types` 时只校验版本：改动目标的版本+1、其余版本不变，却漏了每项 `stable_name`。因此保持版本号、把 Consumer 或未改动的 Goal 引用换成 Unrelated，纯 F1 会返回 allowed=true。

先加入两个负例和一个多引用正例，未修复源码时实际得到 **2 failed / 1 passed**（43 deselected）。首败日志/JUnit 独立保留。修复只在原有 `bumped` 判断中要求每一项 stable_name 与前驱一致；原版本规则、列表长度、输入/capability hash、risk/effect 检查不变。对应测试同时覆盖被升级引用、未升级引用，以及合法“仅目标版本+1”。没有改 Schema/SQL/catalog/权限/ADR/已发布 checksum。

这修的是离线 F1 判定缺口；数据库发布链还有独立的后继/权限证明门，本批未测试该链，不能由纯函数反例推导线上可利用性。

## 已执行回归与首败

使用已有本地 Python，环境清空并显式设置 `PYTHONPATH` 指向本 worktree 的 `packages/eios-core/src:tests`，`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`、`PYTHONDONTWRITEBYTECODE=1`，pytest 一律 `--noconftest -p no:cacheprovider`。因此不加载根 conftest 的 PG bootstrap/fixtures。没有安装依赖。

| 报告 | 精确选择 | 实际结果 |
|---|---|---|
| first-red.xml/log | `tests/test_schema_compat.py -k successor_type_identity`，修复前 | 2 failed、1 passed、0 errors/skips；预期反例成立 |
| regression.xml/log | `test_schema_compat.py test_evolution_e1.py test_evolution_v2.py test_evolution_v3.py` 四文件 | 100 passed、0 failed/errors/skips |
| at048-pure.xml/log | `test_evolution_e4_pg.py` 仅选三项现有 pure test：归因、split、drafts | 3 passed；connect/socket/Popen guard触发0；PG test运行0 |
| s6-extra-pure.xml/log | `test_nx040_f4.py` 全文件与 `test_evolution_e5_pg.py` 三项 pure test：L0、BundleLeak、overlay | 27 passed；相同 guard触发0；PG test运行0 |

最终 **130 个唯一 testcase 通过**；首轮3例与最终重复部分不重复计数。三个最终报告各有11条既存 Pydantic 字段名遮蔽警告，不是错误，未在本批扩改。一次源码定位曾请求不存在的 `tests/test_schema_gates.py`；随后按实际引用找到 `test_nx040_f4.py`，不是测试失败或跳过。

实际命令的 Python 绝对路径、完整 argv、空环境白名单和四份 JUnit/log、两个 guard harness 存在本 worktree 私有 `local-only/l7-s6-offline-evidence/`，不公开机器路径。可复核选择/参数：

```text
$PYTHON -m pytest --noconftest -p no:cacheprovider tests/test_schema_compat.py -k successor_type_identity --junitxml=<first-red.xml>
$PYTHON -m pytest --noconftest -p no:cacheprovider tests/test_schema_compat.py tests/test_evolution_e1.py tests/test_evolution_v2.py tests/test_evolution_v3.py --junitxml=<regression.xml>
$PYTHON local-only/l7-s6-offline-evidence/run_at048_pure.py
$PYTHON local-only/l7-s6-offline-evidence/run_s6_remaining_pure.py
```

guard harness 只选上述现有纯节点，实际禁止 psycopg.connect、socket 和 subprocess.Popen；不运行 PG fixture 或未选的同文件 PG 用例。哈希与结果见同名 JSON 证据索引。此为合成/组件证据，不是真实模型、真实 READ 或 stage 证据。

## 交付与剩余边界

仅修改纯兼容库、现有测试文件和本审计/证据索引。NX-038/039 无新产品代码；它们已有本地覆盖映射，不另造重复测试。旧 Role/诊断 worktree 的17项保护文件哈希保持，原有 dirty11+0454 和全部 worktree/分支保留。共享 planning、versions.lock、迁移/catalog 均未修改。

本批完成局部源码修复与本地验证，固定本地提交/四门扫描后供调度交原 L6 独审；不 push。S6 完整 AT-023/024/048 仍需要原运行单规定的独立身份/PG权限/正式对象隔离/冻结留出与 stage 证据，当前没有新执行许可，不把其余冻结解除。
