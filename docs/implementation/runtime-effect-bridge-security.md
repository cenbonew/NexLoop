# Runtime service.request submit/find 可信桥审查

2026-10-08，独立只读当前runtime_activation/runtime_dispatch/backend、040/041/042与EffectIntentPort。仅新增本文，不修改production code/SQL，不读取.env/生产凭据，不执行provider或重叠suite。当前缺桥事实与建议分开，不把设计标通过。

## 精确已有边界

- runtime_activation.py:145–161：opaqueactivation hint先验证当前owned task，再resolve→真实storedRun _identity→完整Runproof authorize；private digest最终剥离。但分成独立事务，没有把后续EffectIntent受理放同事务。
- backend.py:224–233与263–266：RuntimeActivationPort和EffectIntentPort分别组装，后者只接受真实Run session，不接受domain Worker root session。直接先authorize后另一次submit会留下lease/fence撤销或失效间隙。
- runtime_dispatch.py:129–142与151–162：当前job lease/fence重验/renew、opaqueactivation及sameRun/start恢复；这些queue/runtime许可不是外发Action许可，不能授provider credential。
- 0037_runtime_activation_registry.sql:146–160：最终source/Run/完整proof+当前joblease/command TTL校验，_run_digest只供可信后端使用，Host不能获得。
- 0041_effect_context_registrar.sql:269–284：真实Run关联登记的context，actualPlan/Consumer/control重验、sharedquota与intent同事务。稳定Step由真实64hex object_id，固定service.request:primary，不由模型提交slot。
- effect_intents.py:33–74与85–88：真实Run、当前发布contract/input schema/Actionproof，submit/find protected040/041；find按当前context归属过滤。当前回包scope=effect_intent、business_action_success固定false，不能凭已存state或模型文本补true。

## 最小 Backend bridge

建议专门submit_runtime_effect/find_runtime_effect接口，只接opaqueactivation_ref、完整原command以及parameters或intent_id。它由独立domain Worker当前queue身份承载，Backend私有组装拥有pool/signer；Runtime/Host只持局部transportkey和activation_ref，不能提供raw Run token、actor/tenant/world、context/Step/slot/executor或provider endpoint/credential。

一次真实private connection transaction执行：

1. 认证Worker及其current queue/action proof，protectedopaque hint/resolve只查它当前owned task/lease/fence；匹配原command/input/owner epoch，不能任意RunID lookup。
2. 以resolve实际_run_digest私有_identity构造真实已签发Run，完整source/service/agent链、allowed resources/发布definition、TTL与epoch当前校验；不是假扮人类或伪造源session。对tool操作运行signedactivation授权，marker按既有规则持久。
3. private同连接执行该真实Run的signed040/041 submit/find。SQL从Run已登记context取actualConsumer/Goal/Step/control/sharedquota及固定executor；canonical参数与版本不匹配按409整事务回滚，同Run/tool_call重投仍原stable intent。
4. 最终同连接再次signedactivation authorize(tool)+全部currentRunproof，及queue lease/fence/command.not_after；受理许可也完整重验。任何等待过期/撤权/绑定变化失败，intent/outbox/quota/marker与授权链相关事务写全部回滚。
5. transaction commit后才ACK/返回allowlist receipt。不能用现有公共authorize()加公共submit()两次独立提交冒称原子；需要privateprepare/execute支持既有connection，并保持公共pool/Run digest不可见。

同事务只完成受理/查询，不调用HTTP。独立action_worker仍按042自有currentexecutor+sourceproof、sharedbudget/controls、独立Action/effect fence准备dispatch；queue lease绝不代替Action lease。已经提交的intent在Runtime/Host失联后可独立处理，Run/source失权则新的外发仍拒绝，unknown始终query-first。

## 上下文绑定与共享 receipt

Consumer/Goal/Step权限来源只能是041真实登记和实际ontology对象。RunCommand consumer_ref/goal_version_ref是待比对声明，不能认权；必须明确canonical reference格式并与已登记object_id/revision一致，不能把现有64hex object_id硬转UUID。真实Step及其submitter_principals delegated membership、消费者world、Goal/Step/control revisions、pinnedexecutor在提交和dispatch时再核；任何声明不同应拒绝而非切context重新造slot。

A/B合法Run绑定同Step/context后共享同stable intent与receipt；其他Action授权主体没有Step委托仍拒绝。find传intent UUID仅选择已有记录，不获得归属；当前tenant/world/Consumer/Goal/Step/slot过滤与完整Run许可后才读。不能删除EIOS claim principal检查，不能从Run换新toolcall/contractversion暗中生成新业务意图。

当前042 read_effect_receipt是pinned actualaction_worker query能力下的历史终态读，不可让domain Worker冒executor或把executor token传Host。若桥find要展示真实governed成功，需要新append-only保护SQL，在原Run当前context/Action可见性下核persisted govflag、terminal succeeded claim/receipt/outbox/attempt一致，再返回结果；不得修改旧40/41，不把既有固定false在Python中无证据升级true。provider query权限与本地receipt可见性分别表达，find不调用外部provider、不授新send。

## 公开输出与恢复要求

仅白名单intent_id/receipt_id、整体state、provider_state、governed_claim_finalized、business_action_success与必要conflict code；不返parameters、frozen_request、Run/source digest、SQL proof/facts/身份、provider secrets/reference或原错误。接受状态区分intent accepted、provider accepted/dispatching、observed_fulfilled false与governed fulfilled true。Pi submission done/runtime succeeded只证明推理任务，不改变业务receipt。

submit/find工具声明replay:safe的前提是实际同slot参数冲突、commit-beforeACK、kill/reopen/newtoolcall重复返回原intent已验证。桥ACK丢失后再次submit/find不造成POST；effectworker provider受理后kill由042query原key恢复，仍不以新工具调用发第二次。缺真实模型/channel凭据不阻断桥实际PG/Pi/独立provider测试，但其证据类型须标明。

需真实验证：错activation/其他worker/旧fence/lease过期与Run/source撤权零受理；锁等待后授权过期intent/outbox/quota整rollback；真实A/B相同Step共享一个intent；第三主体和错Consumer/Step拒绝；sameparams重投同receipt、differentparams409原请求不覆盖；Pi实际tool+FULLSQLite kill/reopen+provideraccepted故障查询发送计数不增；终态find当前权撤销拒读且不借executor凭据。当前只读rg/nl/sed上述实际路径，未执行这些新测试。

## 043 candidate 与本轮实际证据

Root 的实际桥已把 private intent admission 放进 activation 的同一连接事务，前后真实 signed tool authorization；末次 lease/proof 失败不能留下 marker、intent、outbox、shared quota。初始 command refs 尚需绑定实际 context，已新增候选 `docs/implementation/drafts/0043_runtime_effect_context_binding.sql.draft`：optional signed `runtime_refs` 恰含 Consumer/Goal 两声明，先验证完整当前 source proof，原041按既有 ledger→context 锁序执行，随后同事务比对实际 Run/context 的 `consumer:` + SHA64 与实际 goal_version_ref；不符整事务回滚。旧1–42未改变。候选 SHA256 `0d3b001f1b79b26b8b7a2c900835c13a3a5daaee35272fd0404be4aff582c8d7`，尚未注册或执行，不宣称 PG 通过。

本人实际首跑 `uv run pytest -q tests/test_runtime_effect_bridge.py`：7 failed / 9.30s，全部在 fixture enrollment 因旧 `synthetic-a` tenant 不是 RunCommand 要求的 UUID；没有到达桥安全断言，不构成实现失败或通过。已换用另一 Agent 所有的 `runtime_effect_plan` UUID tenant/真实 governed objects/真实Run与activation共享 fixture。`python3 -m py_compile tests/test_runtime_effect_bridge.py` 实际 exit 0。遵照 Root 注册043后才重新运行 PG 的并发规则，目前只静态准备负例。

Root 随后正式迁入043、注册catalog/head43；实际文件 SHA256 `2719550a2c7befb37e76ae1e589f54072f49940e655e4bac8ce3447a2691b0cc`（仅候选首行注释修正）。本人重跑同命令：**9 passed / 16.63s / exit 0**。用真实受限 domain Worker 与实际 EIOS published Action/Run/context/opaque activation 验证 submit/find/replay、payload conflict 保留原结果、错 activation/command、current source grant 撤销、未知 intent 拒绝且零业务受理/marker写；1秒 queue lease 与实际PG AFTER INSERT 2秒触发器等待证明最终许可失败使 intent/outbox/submissions/shared reservation/context quota/ledger quota/marker 全回滚。另两例用新真实Run和真实 planner bind、首次 enrollment 故意错误 Consumer 或 Goal revision，command 原样 create activation 后 submit 仍拒绝，证明043不是只依赖 command digest 防篡改。实际 HTTPS guard 200/409/403 映射与响应无 raw Run/private digest 断言通过。

上述证据为本机隔离 PG/HTTPS 正式实现集成测试，authority/schema配置与 payload 为 synthetic，未调用外部生产 effect、未验证真实模型，也未声称业务 Action 已 fulfilled。旧1–42及正式043源码无本轮测试修补。
