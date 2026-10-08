# NX-015 稳定 Intent/outbox 的真实 PG 切片

2026-10-08。已实现 admission，不表示 provider 外发或业务成功；NX-015 仍 in_progress，AT-033～037 未据此通过。

新增 `packages/eios-core/src/eios/migrations/0040_effect_intents.sql` / catalog：trusted context/run linkage、immutable semantic slot、Intent/receipt、submission、effect outbox。只有 protected signed definer 可操作，应用直接读写拒绝。两个实际 EIOS Run 主体分别当前获权后关联同意图，保留原 Action claim principal 检查。相同slot不含Run/toolcall/Action版本；业务digest包含真实版本/Action语义，不同payload冲突409，不覆盖原记录。context lock内intent/outbox/submission/预算同事务，末尾proof/TTL再核对，失败整笔回滚。SQL独立校验明确支持的object/string输入schema子集和实际Consumer revision；未知schema关键字/非Consumer目标版本拒绝，不能假装已支持任意Goal/Step版本。

新增 `nexloop_eios/effect_intents.py`，从真实session派生身份并获得current EIOS proof与published Action contract，服务器生成digest，不接受任意slot/tenant/actor。`backend.py`新增真实authenticated Run服务 `submit_effect_intent` / `find_effect_receipt`，通过同生命周期锁和真实端口组装。第一Action是`nexloop.service.request` version1；这里只保存accepted、business_action_success=false。

新增 `tests/test_effect_intents.py`，Consumer通过真实GovernedObjectCreator创建；published契约与planner context/controls由专属临时PG合成fixture预配置，不能作为生产planner注册完成证据。首次真实端口10 passed9.40s；扩展SQL绕Python输入验证拒绝、unsupported目标authority拒绝、Run撤权不可读等最终13 passed11.38s。Backend真实公开组装后的14项测试12.28s通过，含非Runroot拒绝。

命令：`uv run pytest -q tests/test_effect_intents.py --tb=short`；Root实际head前置命令 `source ~/.nvm/nvm.sh && nvm use && uv run pytest -q tests/test_bootstrap.py tests/test_core_sandbox.py tests/test_doctor.py tests/test_db_boundary.py tests/test_compose_bootstrap.py tests/test_wheel_install.py --tb=short --junitxml=.ci-results/effect-intent-head-prerequisites.xml` 首次20 passed/2 failed19.59s：Root批量head替换误将bootstrap历史列表0039改成重复0040，已纠正；另两新runtime表缺FORCE RLS，被既有database边界检查拒绝，已在0040补ENABLE/FORCE RLS，不降低原检查。真实Backend14case再跑12.52s全过；head前置重跑22 passed18.74s。完整稳定head40 CI尚未终态。

0040 SHA256 `64a5e630269dc9946eee1c71cd5311c642a52ff7ab3a259f7309dea4db3cebb1`。原1–39SQL校验和未更改。`versions.lock.json`仅bootstrap_revision0039→0040；上游三个commit、Pi冻结/npm对应与image历史记录不变；没有head40部署或image验证。

明确剩余：生产governed context registrar/真实目标与计划权威、固定真实executor Action claim、Action独立lease/fence/attempt、dispatch当刻当前授权与消费者controls/预算复核、provider query-first与receipt/claim原子记账、Pi execute_action工具、真实服务履约。缺context默认拒绝是安全边界，不是产品完成方案；下一步必须实现治理登记与效应Worker，不能永久停在fixture可用。没有读取实际.env、调用真实模型/渠道、生产迁移、提交或推送。

固定head0040完整CI命令：`source ~/.nvm/nvm.sh && nvm use && scripts/ci/check`，将执行于当前冻结源码；结果待终态。迁移完整性实核见effect-intent-migration-integrity-evidence.json（旧39目录条目与SQL SHA256一致，所有40checksum匹配）。

固定head0040完整CI终态exit0：1146 Python1068.32s（1warning）、24实际SQLite、25实际PiRuntime1.92s、零关键跳过，Web/Host/四Pi包构建全部通过。保留此前所有失败记录。Runtime恢复交付完成不等于AT039业务对账或NX015真实效应完成。
