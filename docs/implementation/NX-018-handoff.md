# NX-018 交接：in_progress

2026-10-09 收尾。NX-018 **未完成，不可标 done**。其余 S3 任务未启动。本轮因负责人要求交接停止，采用明确的未完成交接路径；以下三个实现缺口不是缺少外部凭据造成的。

## 已进入 main

- RoleDefinition / ConsumerRoleLink 受治理定义与映射、真实 Source-derived service-trigger、v3 Context Artifact、短 Run 注册与当前 Role 检查：0063。
- 0064 修复多重授权/Artifact 锁等待之后的最终期限检查。真实目录 bootstrap PG40 + Pi1 passed，详见 `NX-018-core64-role-deadline-evidence.json`。
- 0065 独立受治理 receipt recovery：Role.end 阻止新 SEND；拥有独立当前 recovery 和 QUERY 权限的服务主体可把已观测的 fulfilled 回执原子写成正式终态。保留原 claim/fence/owner，检查每个 terminal UPDATE 的影响行数；禁止用查询重放伪造新的 POST。
- 本轮 0065：真实 clean-catalog PG **33 passed / 327.88s**；实际两个短 Pi Run、SQLite WAL/FULL、同一业务意图和一次 owned loopback 效应 **1 passed / 46.96s**，均无 skip、首次主线执行即通过。442 个源文件冻结检查零变化，0001..0064 checksum 不变。证据 `NX-018-core65-receipt-evidence.json`。
- 0065 实现 `75ff5da`、证据 `8d8eeb0`，经 PR #1 合入 main；`versions.lock.json` 仅 bootstrap_revision 0064→0065。

## 尚缺与具体接续次序

1. **当前 formal Source READ 必须贯穿 model/start/submit/admit/finalize**。主线当前 snapshot/bind 的 READ 检查不等于下游撤权传播。已验证候选 `docs/tmp/nx018-role-formal-current-candidate/`：独立19例 passed172.22s，八种 READ 撤销禁止模型/提交并且 POST0；八种缓存签名在实际 SQL 层拒绝；Role.end 后独立 QUERY 保留 durable observation。并非主线0066测试。
   - 最小集成复核在 `docs/tmp/nx018-formal-current-0066-integration-review/HANDOFF.md`。其精确四 Python diff 在0065上 `git apply --check` 成功，提供 BASE_SHA、SQL和去手工DRAFT的测试副本；仅AST检查，**未执行新的 fresh-catalog PG 组合**。
   - 下一步核对 BASE_SHA，在 feature 分支应用精确差分，追加0066/catalog checksum/lock，保留旧私有 wrapper 链和全部0065 recovery 方法。`authz.nexloop_context_formal_current` 与关系候选的同名 helper 内容一致，仅安装一次。依交接内命令执行19例及 receipt/role/Pi 回归，冻结前后源 SHA。不可把候选19例写成主线 passed。
2. **关系更正必须进入新的 Context，而非把 hypothesis 变成正式事实**。已验证候选 `docs/tmp/nx018-relationship-context-tail-candidate/` 有33例 passed219.31s：真实 Human correction、两个 Host/Pi Run、受限关系字段/READ/多项期限、bind 失败回滚。其独立冻结不包含当前主线的所有0061..0065增量。
   - 组合准备目录 `docs/tmp/nx018-v4-main-merge-candidate/` 仅复制0065、应用formal四Python、初步拼SQL；准备脚本 substring lookup 曾报 ValueError，**没有 pytest/build 结果，不可直接 promotion**。v4 Python/Host接线与测试安装尚未完成。
   - 下一步在0066通过之后最小整合 explicit v4；保留 v3 分支、0061 identity、0062 Message READ、0064期限、0065 recovery。不要覆盖 public Role wrappers，应适配正确的 private alias。运行关系33例、v3/identity/message/receipt兼容及真实Pi，按 actual evidence 判 AT-009。
3. **Role 权限上限和 scope 现在仍是 metadata，未满足 M02**。候选 `docs/tmp/nx018-role-policy-candidate/STOP_HANDOFF.md` 已冻结18文件；实际 governed policy CRUD/原子 Run 签发、scope拒绝、两Role不叠加等有独立 PG 通过证据，v5 Artifact/Runtime6 passed77.44s。
   - **两 Pi 闭环仍失败**：inspect503、tool runtime_tool_unavailable→model runtime_authorization_denied。治理 submit 实测2.041s，超过现工具HTTP总deadline2s。不能靠放宽timeout伪称可靠性已解决。
   - 尚需 policy EDIT 后旧Run/dispatch/finalize 撤权与TTL、effect预算并发测试，v5 live contracts/generated类型，以及严格日期 calendar-roundtrip 校验（候选仅 Date.parse/UTC suffix，弱于旧v3）。formal-current还未整合。
   - 下一步先审计并优化实际授权调用/锁等待/HTTP边界，保留首失败；完成独立签名 policy envelope、Source∩Run∩Role/scope资源交集以及真实两Pi+效应闭环。所有未知 metadata-only ref 必须 fail closed。禁止把多个Role权限合并来补权限。

## 关闭 NX-018 的门槛

上面三项在 fresh catalog 的主线组合中实测通过，AT-004/009 的实际要求逐项复核，代码、契约、当前权限/期限、受治理实例写/真实效应证据和提交齐全，再将 NX-018 标 done。当前 AT-004、AT-009 均为 not_run；部分候选测试不改变此状态。整体最新 head 的完整单次 CI 尚未重新执行，不声称全绿。

完成后才交给 Claude 推进 NX-044（先写 NX-012/NX-015 gap），然后按最新 planning/ADR-019 次序推进。Codex 本次停止，不启动 NX-019/021/022/044。

## 本地资料与安全

上述候选位于 `.gitignore` 覆盖的 docs/tmp，为本机继续开发资料，未公开提交。冻结报告保留首次失败、准确命令、SHA 与 patch 基线。不得直接整目录覆盖主线或把手工 DRAFT bootstrap 当正式 migration。没有私人渠道/embedding成功声明；商用渠道验证缺凭据仅阻塞对应验证。S2真实模型有独立 bounded synthetic 证据，不能替代当前Role路径验证。
