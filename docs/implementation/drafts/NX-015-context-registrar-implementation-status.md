# 0041 draft 实际代码与待验证边界

0040 full CI 活跃期间仅写 `docs/implementation/drafts/`；没有改 source/migrations/catalog/tests/planning。下列代码尚未移入包、尚未在 PostgreSQL 执行，不能宣称测试通过：

- `0041_effect_context_registrar.sql.draft`：共享 control ledger（预算key不含revision）、正式 Plan binding、每intent唯一共享预算reservation；三个新表FORCE RLS及应用直接访问拒绝。configure/bind definer 核当前完整EIOS签名、独立实际主体、published contract与精确窄参数schema、actualontology schema/对象/revision、真实Run/action范围、固定executor；登记与Runlink只经signed endpoint。
- `effect_contexts.py.draft`：真实事实解析与发布bundle、Backend提供的真实认证session；实际同restrictedconnection Actionclaim reserve→context登记→signed finalize→最终完整proof重验，commit后return。payload不包含raw Run/executor token，exception固定脱敏。context语义冲突显式409。
- `test_effect_contexts.py.draft`：真实PG fixture的schema/permission发布为合成配置；Consumer/Goal/PlanStep/EffectControl全部用现有受治理create Action正式创建。覆盖登记/claim原子、缺owner、跨两个真实步骤共享预算、Run不能升为planner/自报slot、撤权、runlink写失败回滚、直接表和旧inner拒绝。尚未实际执行，不能把这些断言列为通过。

精确Backend组装待下一轮：owner `configure_effect_control(control_id,control_revision,executor_token)`；planner `bind_effect_context(step_id,step_revision,goal_revision,consumer_revision,control_revision,run_id,run_token,executor_token)`。raw token由Backend真实authenticate/ authenticate_run仅内存解析，Pythondraft内部port只接这些实际session；不接受自己构造的principal/context。Host/Runtime无这个入口。

新增owner-only `authz.nexloop_assert_effect_plan(context_uuid,tenant,world)` 供后续dispatch在自己的实际授权/租约边界后调用：control ledger→context→排序正式Goal/Step/Control对象，检查当前revision/control/state/validity/pinnedexecutor，返回共享control额度。它没有应用EXECUTE，不是公开裸读表口。外发仍需当前完整来源+executor proofs，helper里的身份snapshot不是发送许可。

0041替换同名Intent definer并封闭v0040 inner EXECUTE；先验HMAC/TTL/role再读owned linkage，锁与复核真实plan，然后同事务调用原40 admission、每新intent共享额度一次reserve、最终当前链/实际plan重验。40只Consumer expected_versions的限制保留于inner；Goal/Step/Control由新技术binding和真实objectrevision验证，并冻结在goal_version_ref语义中，不伪装inner已经支持泛型资源revision。

修正设计中的对象身份：现有ontology Action生成的object_id是64hexSHA256，故以实际Step object_id固定身份+服务器 `service.request:primary` 槽，只有context为UUID。不为Runtime另造UUID对象或任意槽。

尚待实际运行验证/审查：SQL语法与全部PLpgSQL路径；冻结Action models实际序列化与claimrequestdigest；复杂锁等待、迟到proof/TTL回滚与完整negativecases；Backend窄端口组装；正式schema/Action治理发布工具。目前fixture发布配置只提供集成测试authority，不证明生产发布流程已装配。Controlowner/executor credential轮换当前保守pin digest会拒绝替换，需明确独立治理rotation接口，不能自动换身份绕claimprincipal。独立executor/query/dispatch/unknown闭环另由后续真实worker实现，不把registrar成功视外部效应fulfilled/confirmed。

本次实际执行两次内存 `compile(draft_python_source, path, 'exec')`，只验证Python语法，不导入/运行模块，不生成pyc；无SQL执行、provider调用、迁移或测试结果。

本轮补 distinct roles：正式 PlanStep.submitter_principals 是独立 planner Action 写入的 canonical、bounded1–32、distinct string array；SQL 从实际持久Step读取，不接受caller数组。每个来源Run各自完整授权并验证membership；context不归首个submitter，同Step/槽A、B两真实principals绑定同context。新增真实A/B各Run bind+submit断言同intent/outbox/sharedquota一次；第三真实有Action许可但无Step委托主体拒绝仍保留。原executor Actionclaim principal检查不变。新增用例尚未运行。
