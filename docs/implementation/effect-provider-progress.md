# NX-015 provider transport 与独立故障服务

2026-10-08。NX-015 开始实际实现，尚未完成完整 Action Gateway/dispatch/query/reconcile 治理闭环；AT-033～037 不据此改为 passed。当前 Runtime/Pi 成功不等于业务成功。

实际新增 `packages/eios-core/src/nexloop_eios/effect_provider.py`，提供固定可信 endpoint 的 HTTPS dispatch/query。HTTP 不使用代理、不跟随重定向；私有文件读取凭据，不接收 Run/tool 的 URL/credential。可信 numeric connect address 绕过 DNS，TLS 仍验证原 hostname；TCP、TLS、响应受剩余时限约束。响应有长度/类型/重复字段/intent/digest/reference 检查，外发不确定返回固定 unknown，查询失败返回固定 unavailable，不暴露服务端原始错误。POST200/202仅表示 accepted 或明确 fulfilled；GET404仅严格 not_found 且 null digest/reference，不能自动授权重发。

该模块只是 transport，必须由完成持久 dispatch admission 与当前 EIOS 授权复核的可信 Action Worker 调用；现阶段尚未组装完整 Action Worker，没有实际渠道调用。

`tests/support/effect_provider.py` 是独立 spawn 进程的合成故障服务，使用专属 SQLite WAL/FULL 持久 receipt 和请求计数，POST commit 后可暂停响应。它不替代 NexLoop PG 业务账本，也不代表真实渠道或服务履约。`tests/test_effect_provider_fixture.py` 覆盖持久重开、同 key 并发 effect1、真实客户端 SIGKILL 后查询且不重发、无效参数零 effect。

`tests/test_effect_provider_transport.py` 实际调用 production transport，验证 accepted/query/conflict、受理后超时查询同意图且 POST count1、query unavailable、零 IO 参数拒绝、404not_found、无 redirect、固定脱敏错误、numeric address配置、真实 HTTPS 私有 credential 与认证 header。

实际命令：`uv run pytest tests/test_effect_provider_transport.py tests/test_effect_provider_fixture.py -q`。首轮最终 **18 passed，9.57s**。补充6例实际 HTTPS silent握手与trickle headers/body（Connection:close）后，最终 **24 passed，12.80s**；新增6例首次通过3.19s。fixture 首轮4 passed3.43s；transport 首轮7 passed/1 failed5.15s（POST202被client仅200拒绝），修复200/202协议后最终通过。只读安全审查发现 DNS 在旧 timer 前可能无界，改为可信 numeric连接后复核；实际 HTTPS 正常路径与silent/trickle专门反例均通过。

并行设计依据：NX-015-intent-design.md、NX-015-effect-test-design.md、NX-015-authority-boundaries.md。0040稳定Intent/outbox正在开发，尚未注册catalog或迁移；保留head0039 CI的一致性。当前迁移锁和upstream/Pi/image版本未因本transport改动而改变。消费者控制/业务预算、权威context登记、受限effectworker的当前授权与fence、完整PG故障恢复待交付。未读取真实.env，未进行实际模型/渠道/生产操作，未提交或推送。
