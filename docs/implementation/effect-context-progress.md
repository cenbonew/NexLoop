# NX-015 治理 context 登记

2026-10-08。新增0041_effect_context_registrar.sql / effect_contexts.py / test_effect_contexts.py，升级test_effect_intents.py真实fixture；backend.py新增configure_effect_control与bind_effect_context，执行实际executor authenticate与Run audience/run_id认证，凭据只留内存，不转给Runtime。版本锁bootstrap_revision0040→0041，六个head预期随实际catalog更新；上游三个commit、Pi/npm对应和历史image记录未改；Agent Host status从过期的no_run_admission修正为已验证正式Run admission/FULL恢复/可信Worker（runtime_only）。

实际Goal/PlanStep/EffectControl由真实GovernedObjectCreator/Editor写；schema模型来自effect_plan_schemas，配置/绑定入口使用registrar_schema与published契约。独立owner/planner、真实Run/executor分别current EIOS proof。Step使用actual64hex object_id+服务器固定槽，正式主体集合由planner治理，不接受Run指定slot/主体集合。A/B真实角色可共享context/Intent而executorclaimprincipal不变。sharedcontrol预算跨Step同账本，revision更新不重置占用。登记与Actionclaim reserve/finalize在同连接事务，最终再验完整proof/有效期。

实际命令：`uv run pytest -q tests/test_effect_contexts.py tests/test_effect_intents.py --tb=short`。最终26 passed32.90s，包括原14Intent断言升级真registrar、多主体/第三主体拒绝、Goal正式edit失效、sharedbudget，以及真实control行锁等待导致缩短的真实proof过期整事务回滚。首次SQL CASE括号、FrozenJsonMap JSON比较、严格模型JSON解析、fixture缓存executor身份问题均修正；缓存身份一轮7pass1fail，最终26全过，没有替换授权为stub。

Root命令与结果：`source ~/.nvm/nvm.sh && nvm use && uv run pytest -q tests/test_bootstrap.py tests/test_core_sandbox.py tests/test_doctor.py tests/test_db_boundary.py tests/test_compose_bootstrap.py tests/test_wheel_install.py --tb=short --junitxml=.ci-results/effect-context-head-prerequisites.xml`，22 passed19.79s。原1–40条目录与SQL SHA256保持；所有41checksum匹配，0041 SHA2565896ad202a2a08775c1daa5eda03abdc99d8a100443d57b4cd98189d0d4a8eef。

当前仅已完成治理登记与持久受理，不标NX015 done或AT033～037/039 passed。完整CI最后成功是head0040（1146Python/24SQLite/25PiRuntime），head0041还没有完整CI；不将此前结果伪称覆盖当前全源码。原0040XML/log已保留到忽略目录.ci-results/head0040-reports，原SHA不变。

生产治理发布工具与credentialrotation仍待装配；测试publisher/authority配置是独立PG合成fixture，不是生产发布成功。042 effectledger、可信Worker与真实PG/provider故障恢复正在独立draft实现，尚未执行迁移。无实际模型/渠道/持久环境迁移/镜像部署，未读取真实.env、未add/commit/push；product_ready=false。
