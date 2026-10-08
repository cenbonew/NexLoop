# Effect Worker 与 RuntimeEffectClient 安全复核

范围：当前实际 `effect_worker.py`、`effect_dispatch.py`、`effect_provider.py`、`runtime-effect-tools.ts` 与0042 attempt表；只读源码，无生产访问。日期2026-10-08。

## 必要修复：既有 attempt 未绑定 provider identity

`packages/eios-core/src/nexloop_eios/effect_worker.py:82` 每tick重新读取可信配置并创建 provider；`:85` 将新实例交给 Dispatcher。`effect_dispatch.py:55` reconcile用该实例query，没有与原POST endpoint绑定。0042 `runtime.nexloop_effect_attempts` 保存 provider_key（等于intent UUID）、payload digest、provider reference及原Run，但没有provider origin/profile revision。

可复核路径：A POST accepted或ACK丢失→attempt已经存在→进程内/重启后配置改为B→reclaim正确选择reconcile，却GET B `/v1/effects/{original_intent}`。B返回相同id/digest/ref fulfilled会成为原A的观察并可在完整当前权限下finalize；B404则A成功仍变unknown。现有digest/key校验不能区分provider实体，QUERY-first仅保证不新增POST，不能保证查询原provider。这是配置轮换触发的实际来源错误，不是模型可以指定URL。

进程启动冻结origin/connect_address/CA identity仅阻止同进程漂移，重启仍缺持久来源绑定。最小完整修复是append-only migration：admit前将可信provider profile identity/revision签入当前双proof命令并与attempt同事务持久化；query/observe/finalize须核同profile。端点/CA迁移必须显式治理，credential内容可轮换而不改变profile identity。缺原profile映射时只能blocked query，不能静默选新provider。旧1–43保持checksum不可修改。

## 当前有效边界

- Worker启动只读服务所有者、regular、无symlink、非group/world可读、有界文件；无环境fallback。真实数据库role必须 `nexloop_action_worker`。每个ledger调用经 `_FreshExecutor.service()` 重新认证真实当前凭据，rotation不会把旧授权缓存当新权。
- Provider仅可信配置控制origin，生产只HTTPS、固定numeric connect_address、原hostname SNI、无DNS/proxy/redirect、绝对deadline与有界framing。Runtime参数只含正式message/intentid，不含provider endpoint或凭据。
- POST未知时 Dispatcher返回固定 `business_action_success:false`，尽力记录unknown；即使记录失败，已提交dispatching/attempt驱动query-first。accepted维度为dispatching/provider_state accepted/false；只有PG返回完整finalized真实claim+receipt才true，Pi终态与provider HTTP200不会自行升级业务成功。
- CLI stdout仅固定ready或allowlisted claimed/status，异常固定文本，pool logger暂时关闭；不打印Run、intent、provider reference、DSN、原exception。SIGTERM停止新claim并收尾当前run_once，没有blindcancel/resend。
- RuntimeEffectClient固定loopback HTTPS guard origin，私有CA/key逐请求读取；2000ms AbortSignal、65536B请求/32768B响应，无redirect跟随。原RunCommand和opaqueactivation随调用绑定。严格receipt字段与sameRun/findintent，scopeeffect_intent且业务flag必须false；409只映射固定conflict code，其他异常无cause/原消息。Domain Worker桥不会借executor credentials展示govtrue。

## 证据归属与边界

本人真实 `uv run pytest -q tests/test_runtime_effect_bridge.py` 为9 passed /16.63s（先前7fail为旧tenant fixture设置，已换真实UUIDhelper）。证明实际PG授权/043refs/最终lease全回滚与HTTPS409/403，不证明provider profile绑定。Root报告的新Effect Worker CLI10case属于另一Agent实际执行证据，本人未重复执行、不自称跑过。该新漏洞尚待Root修复与真实A/Bendpoint重配置恢复负例，不能仅凭当前9bridge/10CLI报告通过该边界。

## 已实施044与实际复测

Root随后授权实际修复。新增正式044表 `runtime.nexloop_effect_provider_bindings`：tenant/world compositeFK、FORCERLS、无application直接读写、不可update/delete。signed execution wrapper在完整currentproof/HMAC核后，普通admit/query/observe/finalize强制可信profile SHA256；admit与原042 attempt同事务一次绑定，其他操作须匹配既有绑定，错误或legacy无binding整tx拒绝。旧函数重命名且application执行撤销，保留原source/executor双许可、ownedfence、claim与lease；终态technicalreceipt读取无需profile但须有真实原binding且原042完整terminal一致性检查。return前保存的effectlease/当前Actionlease/Run/proof再次检查，所有失败整事务回滚。Root将canonical origin/numeric route/CA hash冻结进profile，credential内容/路径不进入identity，HTTP使用同snapshotCA。

本人实际命令均为 `uv run pytest -q tests/test_effect_provider_binding.py`：初3例3 passed /6.43s；扩展5例5 passed /7.78s；最后6例 **6 passed /13.08s /exit0**。新增测试才重跑，无被隐藏的测试失败。双独立真实HTTP故障provider A/B：A已POST后Backend重开，B拥有同intent/digest fulfilled证据也不能获query/observe/finalize，obs/claim不变；A恢复仅GET原key并完成真实EIOS claim，历史terminalreceipt读取通过。legacy无binding只query_unavailable、零GET/observation；非法profile零attempt/binding；credential rotation不变profile，origin/CA变更profile变化，既有CA snapshot保留；四application roles实际直读binding表/调用旧042内函数均InsufficientPrivilege。真实PGbinding AFTERINSERT延迟4s跨3s effectlease，attempt/binding/state全部rollback。最后044/catalog SHA256 `04b88ecb763988adfc30f8fffa507c358f6d85e76ffe6ebf5e5e19a389647183`；旧1–43未改。

这些是隔离PG与synthetic durableHTTPprovider的真实代码证据，不是实际外部渠道或真实模型验证。profile迁移/找不到原endpoint时仍failclosed，不能把新的可信配置替换原受理provider；凭据撤权/失效只阻断相关真实外部调用。
