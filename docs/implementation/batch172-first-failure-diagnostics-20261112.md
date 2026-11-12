# NX-040 批172首败与诊断候选

首轮集成 `f69893ccefb59b03bd825236bb533f7135bd20b2` 的权威 CI608 于2026-11-12 17:34:16 +08结束：9356 passed、3 failed、0 errors/skipped；未合入main。原始result、JUnit及failure traces保留，不能用后续局部结果覆盖。

两项 `tests/test_model_surface.py` 失败由Host legacy展示投影缺少delivery_action而被新增v8检查误拒导致。修复源码 `ccc6424631db3f367ac9f03e3b5972839548b342` 恢复旧协议展示，v8严格校验与simulation隔离保持；L6限域条件PASS。原候选模型surface30p/2f、新负例红39p/2f，修后Host574p、原model_surface32p。

第三项 `test_recovery_current_authority_and_atomicity[unbound_query_id]` 在初次dispatch accepted前提因缺provider_state失败，尚未进入unbound query_id授权断言。原trace未记录实际返回分支，故根因未证；两个固定原SHA的本地单条probe分别1p/14.19s和1p/12.91s不能认定flaky或解除失败。第二次ignored诊断仅看到正常fresh accepted及reconcile observed_fulfilled，未复现首败。

本次测试差异只保存第一次run_once返回，在缺provider_state时查询失败后的DB clock、intent/outbox state、fence/lease及独立provider计数和method/status，随后仍失败；正常分支仍要求accepted。不重试、不重发、不修改timeout/lease/sleep、权限或原boundary断言。不输出token、proof、业务原文或连接信息；诊断读取失败只记录异常类型。诊断不解释原首败，只帮助下次失败定位。

修正版只纳入Host修补、测试诊断与调度证据，不夹带L7未完成Role恢复或SQL/catalog/权限/部署变更。允许送新候选check，暂不enqueue merge。新check即使绿色，第三项原始首败仍须独立复审，不自动判偶发或合并。

v8后端wire/E2E仍blocked。完整Role/D3、真实FOLLOWABLE register及retention-erasure-restore未验收；NX-040保持in_progress。全部部署、持久迁移、后继准入、REAL_DISPATCH、真实外发、release/tag冻结。

本地验证：独立环境offline frozen安装、Node24 Pi/Host build成功；原Role post-accept全文件及原model_surface合计39 passed/53.23s，load9.741/-n2。按复审建议将PG和provider诊断拆为独立try。最终缺key诊断支路用明确的纯注入返回claimed=false验证：1 failed（预期红），报告保留返回、DB状态/lease和provider独立effects=0/requests=[]；不是旧CI失败复现。移除注入正常真实PG单条再次1 passed。所有首败/注入/正常结果独立留存，无skip、无重复dispatch、无时限或权限放宽。
