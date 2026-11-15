# NX-056 审批演练离线证据核对与最小修复

## 基线与既有交付

- BASE `6a5ef7a98a44be8babcba71940419e3391587ff1`，分支 `codex/nx056-drill-offline-20261116`，新独立工作树初始干净；此前 NX-061/NX-064/P01/Host 等树及所有改动保留。
- main 已有 `scripts/stage/approval_drill.py`、其 fake transport 测试、内存 Approval state machine/expiry 测试，以及 `run-nx056-approvals-enable.md` §8b 第2天标准。main 规划只记第1天完成与后续原因码实现，不据此宣称第2天过期演练已完成或当前 stage 状态已核。
- 本轮不运行 stage、不触及数据库/网络/实际凭据、不改产品策略或补救意图、不改共享 planning、不 push。没有安装依赖、注册账号或产生任何实际审批请求。

## 四类证据如何判读

| 项 | 已有源/纯本地覆盖 | 本轮边界与仍需 stage 的证据 |
|---|---|---|
| 过期 | 内存 expiry 扫描覆盖 due_by、未到期、无 due、幂等、revision/竞态跳过和 observer；原脚本在请求不在 to_execute 时只报 not_in_to_execute。本轮新增矛盾行防线见下节。 | 不在 to_execute **不等于** 已过期，可能是权限或视图变化。运行单8.8仍要求 expired 状态、approval.expired 审计、等待告警 resolved 和 execute被拒。NX-056 SQL还覆盖未消费 approved 请求的过期，不能拿只扫描 waiting 的上游内存测试替代该 PG/stage 证据；本轮没跑它。 |
| 拒绝 | `test_approval_state_machine.py` 已验证 quorum2 时一次 approve 后一次 reject 即终结，后续投票被拒；drill fake transport 验 HTTP拒绝与固定码输出。 | HTTP调用完成/脚本退出0不是整体演练通过。运行单8.7的 rejected、approval.decided outcome及告警恢复仍须逐项核，不从假 transport 推断真实执行。 |
| 自批原因 | 既有 `test_stage_approval_drill.py` 的 fake HTTP409/not_allowed_in_state/details.reason=self_approval 验投影，free message不输出；cooldown与自批按不同原因判读。 | 第2天明确要求 details_reason=self_approval，不能拿旧发布只有409的第1天结果代替。角色/subject同人别名拒绝是既有真实PG测试的范围，本轮不连接、不重跑或伪称已验证真实身份。 |
| 至少 N 人 | 复用内存 quorum2、两名不同 actor、同人第二次投票拒绝、一票拒绝终结；resolver boundary要求显式受信 authority verifier。 | 这些是合成 actor 语义，不证明真实成员当前资格、24h冷却或同一人别名映射。真实证据须同一 approval/policy_version 的 quorum、distinct eligible subject批准和批准前不可执行，保留原审计条件，不由客户端按简单计数创造权限或改 solo mode。 |

## 可证最小缺口

原 `run_step(execute)` 仅按 `approval_id` 从 `to_execute` 选行，再核 inputs/operation，未检查该行 status。用内存替身把 expired/rejected/cancelled/waiting_approval 或缺 status 的行放入该列，即仍会发执行调用，并可因替身返回200而报 consumed。这是本地演练对矛盾证据的处理缺口，不是已证明后端绕过审批。

现在只在已有可执行行条件中增加 `status == approved`，否则沿用 `not_executable_from_workbench`，没有执行 POST。合法 approved 行仍把原 frozen inputs **同一对象**提交一次，不重算输入、变更 operation、投票、quorum、过期时间、权限或补救意图。后端派发时当前授权/审批/到期检查仍是权威，本客户端检查不能替代它。

新增6个纯内存用例：5个矛盾/缺失状态反例，1个 approved 冻结输入阳性。自批、拒绝、N人、expiry 直接复用既有测试，不另写审批状态机。

## 实际命令和首败

Node24.13.0、项目现有 Python3.12.10、pytest `-n 2`；启动时程序化负载闸<12，首轮8.901、最终8.429。

```sh
source ~/.nvm/nvm.sh && nvm use 24
export PATH="$PROJECT_VENV/bin:/opt/homebrew/bin:$PATH"
PYTHONPATH=packages/eios-core/src:tests python -m pytest -q -n 2 tests/test_stage_approval_drill_evidence.py --junitxml=.ci-results/nx056-drill-first.xml
PYTHONPATH=packages/eios-core/src:tests NEXLOOP_TEST_PG_AUDIT=.ci-results/nx056-pg-audit.txt python -m pytest -q -n 2 tests/test_stage_approval_drill_evidence.py tests/test_stage_approval_drill.py tests/test_approval_state_machine.py tests/upstream/test_eios_approval_expiry.py tests/test_approval_resolver_boundary.py -k 'not test_the_drill_against_the_real_workbench_request_decide_execute' --junitxml=.ci-results/nx056-drill-final.xml
```

- 首败：5 failed /1 passed，0.54s，恰为新增5个非 approved 状态；`.ci-results/nx056-drill-first.log`、`.xml` 原样保留。
- 最终：28 passed、0 failed/errors/skipped，2.03s；2条既有 warning 保留，PG fixture audit 无条目。明确不选择真实 workbench/PG端到端用例，不用 skip 冒充通过。
- 既有脚本测试的登录报文只经过 Fake 内存 transport，password文件只由测试在 tmp目录写入合成占位串；不访问 Keychain、真实用户文件、DSN/密钥、真实登录或端点。新6例直接调用 run_step＋MemoryWorkbench，不走登录/凭据/网络。
- 提交前 docs/publication/definer静态检查与固定HEAD四门扫描另附原始输出。没有运行全量CI、PG或stage演练。

## 交付边界

仅改演练脚本一项条件、新增纯内存测试及本文；无状态机、业务审批/补救实现、SQL/迁移/catalog/契约/权限/锁或规划状态改动。固定提交供 L6 独审；NX-056 整体/第2天 stage 演练及真实 N人身份/权限/expiry 审计仍 not_run。原运行单回退/补救流程不改，不因本地合成通过开启任何运行或部署许可。
