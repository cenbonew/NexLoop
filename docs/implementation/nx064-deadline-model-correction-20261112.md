# NX-064 测试助手的绝对观察 deadline

批172-r2固定集成5539819e的CI610首轮终态failed：9363 passed、1 failed、0 errors/skipped。唯一失败为NX-064 load-8成功路径观测到2.0771508789621294秒，超过原2.0+0.05秒；首败完整result与JUnit保留。原CI608的两项Host展示回归已修；旧Role post-accept首败本次未复现，但具体原因仍未证明，不能抹去历史或称已证偶发。

L6源码审查确认测试助手的确定缺口：started在executor.submit前，随后future.result仍等待完整limit；已提交开销未扣除，已完成future也可能被晚观察而当作准时成功。原CI没有submit、completion与waiter时序，无法判定77ms具体发生在哪个空窗；本修复不宣称重现该次调度或修复生产Host。

本候选只改测试：每attempt使用从submit前开始的单调绝对deadline，扣除提交耗时；明确以调用者**观察返回**的时刻决定是否在限内，而非只看后台call完成时间。即使future早已完成，晚观察的成功或确定拒绝也按过期/ambiguous处理；deadline优先。准时确定拒绝仍不重试。保持原body最多重发一次，所有abandoned future仍由原负载测试drain后核对持久账本。

新增非敏感attempt trace：submit返回、call开始/结束、future done回调、waiter返回、deadline和outcome；不保存body、token、proof或异常原文。原负载用例JUnit追加这些时序和前后账本计数。没有修改2秒配置、0.05容差、次数/body、intent/outbox/预算或held分支断言，亦未修改生产Host、后端SQL、权限或部署。

纯可控clock/executor先在旧助手上得到5 failed/2 passed（非环境错误）；修后首轮7 passed/0.43s。根据独立审阅再补“晚观察到确定拒绝”及deadline优先，最终8 passed/0.43s。覆盖submit开销、已过期不再等待完整limit、ready/未ready的晚观察、两次晚答、两次真正timeout、准时成功/准时拒绝及晚拒绝。不是sleep碰运气重现77ms。

实际load-8首轮因新账本诊断含Decimal不能直接JSON序列化失败，22.42s，首败保留且未执行后继held分支。仅对诊断序列化使用default=str，原数值账本断言未变。随后资源门槛内串行重验原load-8及held_past_the_limit-8，实际结果与固定源码提交在调度证据追加；不以诊断修正隐藏原计时断言。

这是测试模型和观测修补，不能据局部通过称真实负载SLO达标、完整CI通过或原始首败已解释。需固定提交独立审查和新集成CI；当前不合并，不部署、不准入、不REAL_DISPATCH或外发。Role恢复代码完全不在本候选范围。

最终有界PG验证（均-n0、程序load<12）：load-8为1 passed/23.29s，load5.1377，16 Run均ok，最大首请求观察0.704s；held_past_the_limit-8为1 passed/24.16s，load5.4165，8 ok_after_resend+8 ok，超时首请求最大观察2.006s被明确重试而非当准时成功。两者均16 intent/16 outbox/预算16，每consumer1 intent；held共24 attempt，无第三次重试。JUnit保留每attempt非敏感时序和账本计数，原所有断言执行通过。此为本地分支验证，非生产或完整CI证明。
