# NX-018 开发线 L1 报告

BASE_SHA `f58f3ee35ba0296271a5d0498693d474d220d801`（分支 claude/nx018-finish-997fcd）。迁移使用临时编号，调度员合并时重编号。本报告只登记本线实际执行的定向测试；不是全量 CI，不改变 planning 状态。

环境：macOS arm64，Node v24.13.0，Python 3.12.10（uv），Homebrew PostgreSQL 18.4。**本会话 shell 缺省 LANG/LC_ALL 为空，PG18 拒绝启动**（`postmaster became multithreaded during startup`），所有命令前需 `export LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8`。

## 第 1 步：当前 formal Source READ 贯穿 model/start/submit/admit/finalize（临时 0066）

- 基线核对：`docs/tmp/nx018-formal-current-0066-integration-review/ROOT_0065_BASE_SHA.json` 中 13 个文件的 sha256 与本线 BASE_SHA 逐项一致；`git apply --check formal-current-precise.diff` 成功后应用（role_runs / runtime_activation / effect_intents / effect_execution 四个 Python 增量）。
- `0066_nx018_formal_current.sql` = `FORMAL_CURRENT_DRAFT.sql`，私有 alias `*_before_formal_current_DRAFT` 改为 `*_before_formal_current_v0065`；包裹 0065 的 public command 链（activation / intent / execution），0065 receipt recovery 和 `nexloop_receipt_reconcile_command` 不受 formal 条件影响（execution 仅 admit/finalize 且带 role_envelope 时检查）。共享 helper `nexloop_assert_context_proof_deadlines`、`nexloop_context_formal_current` 只在 0066 安装一次。catalog 追加 0066 条目，lock bootstrap_revision 0065→0066；0001..0065 文件与 checksum 未改。
- 测试：复制 main-ready `tests/test_role_formal_current.py`、`tests/test_role_formal_sql.py`（无手工 DRAFT）。

### 首次失败与诊断（保留）

1. `pytest tests/test_bootstrap.py tests/test_role_formal_current.py tests/test_role_formal_sql.py`：2 passed / 21 errors / 15.37s — 环境：PG 因 locale 无法启动（见上）。加 LC_ALL 后 **23 passed / 168.91s**。
2. receipt/role 回归 6 文件：**29 passed / 343.53s**（与 Pi 并发跑）。
3. 真实 Pi `tests/test_role_pi_effect_checkpoint.py`：**首次 FAILED（23.41s），复跑 FAILED（45.35s）**，inspect 返回 503 `runtime_authorization_denied`。同一命令在 BASE_SHA 独立 worktree 上 1 passed / 50.62s → 回归由集成引入。
   - 逐层计时（诊断用临时测试副本，已删除）：所有 `authorize_runtime_activation` 均 allowed；`effect.submit` 2.100s、`effect.find` 2.049s，超过 Host guard 的 2s 总 deadline（`runtime-host.ts`/`runtime-effect-tools.ts` 的 `AbortSignal.timeout(2000)`）。
   - 根因：`backend._invoke` 持有全局 `self._lock`（"This first host is synchronous"），Host 的 inspect 轮询与 model/tool/submit 请求在 guard 端串行。锁计时：BASE 最大 hold 1.307s（submit），最大 wait+hold 约 1.77s，本已很紧；集成后 submit hold 1.674s，再排在一次 inspect（~0.5s）之后即 >2s。`RuntimeActivationPort.authorize` 本身仅 +90ms（423→515ms）；额外成本来自同一 submit 请求中 guard 与 intent 各自重复生成同一 Run 的 Source 签名 Role envelope（~140ms）和 formal READ envelope（~65ms），以及 SQL 侧 before/after 当前性复核。
   - 修复（**未放宽任何 timeout**）：`role_runs.request_envelope_scope()`，只在一次 `runtime_effect_tool` 调用（同一 DB 事务）内复用同一 Run 的 Role/formal 签名 envelope；SQL 在每次使用时仍按当前授权完整复核（读取撤销、修订、期限），不跳过任何检查，不跨请求缓存。修复后锁计时：最大 hold 1.164s、最大 wait+hold 1.662s（优于 BASE）。

### 最终（同一工作树，单次命令）

```sh
export LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8; source ~/.nvm/nvm.sh && nvm use 24
PYTHONPATH=packages/eios-core/src:tests uv run --frozen pytest -q tests/test_bootstrap.py tests/test_role_formal_current.py tests/test_role_formal_sql.py tests/test_role_post_accept.py tests/test_receipt_reconcile_roles.py tests/test_receipt_tail.py tests/test_receipt_revoke_wait.py tests/test_receipt_terminal_rows.py tests/test_role_tail.py tests/test_role_pi_effect_checkpoint.py -n 0 --tb=short --junitxml=.ci-results/l1-step1-final.xml
```

**53 passed / 578.02s，0 skipped**（bootstrap 4 + formal 19 + receipt/role 29 + 真实两 Pi Run 1）。

### 剩余风险

- guard 全局锁串行化是结构性延迟来源：BASE 的 inspect 已有 ~1.77s 的 wait+hold。任何新增授权工作（第 3 步 policy 校验）都会再次逼近 2s。建议（需调度员/负责人决定）：把 `_invoke` 的全局锁改为"请求共享 / 关闭独占"的读写锁，保留关闭时不关 FD/pool 的保证，同时让独立请求并发；这改变 backend 并发模型，需全量 CI。

## 第 2 步：关系更正进入新 explicit v4 Context（临时 0067）——**部分完成，未通过**

- 候选基线：`nx018-relationship-context-tail-candidate` 冻结于 `c6e5065`（0062）。对 7 个改动文件做三方合并（base=c6e5065，ours=本线，theirs=候选），保留 v3 Role 分支、0061 identity、0062 Message READ、0064 期限、0065 recovery、0066 formal-current；新增 relationship_context*.py 三个模块。`nx018-v4-main-merge-candidate` 未采用（它删掉了 `nexloop_assert_relationship_deadlines`，且把 `nexloop_context_artifact_read_dependency` 返回类型由 text 改为 jsonb，会破坏 0063 Role `_v2` 中的 message 分支）。
- `0067_nx018_relationship_context_v4.sql`：reader/recipe/command SQL 原样追加（共享 helper 用 0066 的，不重复安装）；v4 activation 用 `create or replace` 替换链底私有 `nexloop_runtime_activation_command_before_roles_v0062`（已核对候选函数体 = 0053 原函数体 + v4 分支）；v4 依赖读取另建私有 `nexloop_relationship_context_read_dependency`，public `_v2` 包一层按 v4 绑定分流；`nexloop_read_local_artifact_before_roles_v0062` 改名后由 v4 分支接管。未修改 0001..0066。
- **刻意不采用候选的 `guard_timeout_ms`（候选测试用 10000ms）**：Host guard 与工具 HTTP 保持 2s 总 deadline。测试副本 `tests/test_relationship_context_v4.py` 去掉手工 DRAFT 安装与 `guard_timeout_ms`。
- 首次运行 `tests/test_bootstrap.py tests/test_relationship_context_v4.py`：**36 passed / 1 failed / 160.61s**；失败为 `test_real_human_message_v4_bound_artifact[complete]`（两个真实 Host/Pi Run 正向），`runtime_transport_unavailable`。
- 诊断：v4 每次 authorize 自身 ~0.75s（Source 签名关系 envelope ~0.46s、catalog ~0.27s、formal ~0.08s，主要是 `AuthorizedObjectReader._authority` 每次约 8ms、数百条查询）；submit 内 guard 前后各一次，且全局 backend 锁让并发请求排队，submit 2.46–2.58s。
- 已做（单独提交 `21ddfc3`）：backend 生命周期锁改为请求共享 / 关闭独占；第 1 步的请求内 envelope 复用扩展到 v4 关系/formal/catalog envelope。之后 submit/find 1.6–2.07s。
- 尝试过并**已撤回**：跨请求 3s 复用已签发 envelope（最坏 1.33s，v4 正向通过），但使 `test_role_tail::test_context_bind_lock_expiry_rolls_back_binding_and_job` 失败（测试替换短期 READ envelope 验证锁等待后的最终期限，复用绕过了重新生成），也改变了"每次 guard 重新生成证明"的语义，故不保留。
- 当前结果（撤回后）：`tests/test_role_tail.py tests/test_role_pi_effect_checkpoint.py test_relationship_context_v4.py[complete]` 8 passed / 186.87s；随后 `[complete]` 单独连跑 3 次 **全部 failed**（~68s）。即 v4 正向两 Pi 闭环在 2s deadline 下约 1/4 通过，**AT-009 不能判 passed**。
- 定向回归（撤回前，含跨请求复用）：30 个测试文件 280 passed / 10 failed / 1061.51s；Host vitest 141 passed。10 个失败中 8 个是 main 既有回归（message_relay 7、local_message_delivery_assembly 1，见下节），另 2 个（role_tail、两 Pi）由跨请求复用引起，撤回后复跑通过。

建议方案（需调度员/负责人决定）：降低每次 guard 的授权成本而不是放宽 deadline——(a) 授权事实解析在一个只读事务内批量加载同一 Source 的多个 resource（现在每个 `_authority` 独立连接与事务）；(b) 第二次（后置）guard 只复核 SQL 尾检所需最小集合；(c) 或由负责人基于实测确认工具 HTTP 预算。

## main f58f3ee 回归（调度员指派）

21 例统一根因：`c6e5065`（0062）要求 Source 对 `Message/<id>` 的当前 READ，但没有任何生产路径为新受理 Message 发布授权事实（只能可信配置写入）。实证：`test_message_relay.py::test_independent_cli_governed_assignment_and_persist_before_ack` 在 `045c93f` 1 passed / 5.08s，在 `c6e5065` failed / 3.66s。本线未修复（调度员否决只改夹具的方案 A）；方案 B 设计稿见 `docs/implementation/NX-018-message-read-derivation.md`，等待负责人决定。本线回归运行中这 21 例里出现的 8 例仍失败，其余 13 例本线未单独运行。

## 第 3 步：Role 权限上限与 scope 强制——未开始实现

按调度员指示在方案 B 决定前暂停。已读候选 `nx018-role-policy-candidate`（基线 e98d7c7 + 0064，早于 0065）：可按同样的外层 wrapper 方式移植为下一临时编号，其 `create or replace nexloop_context_artifact_read_dependency_v2` 需改落到 0067 的 `_v2_before_relationship`。已知问题"两 Pi inspect 503 / submit 2.041s"与本线第 1、2 步定位的同一根因一致（全局锁串行 + 每次 guard 重复生成签名证明）。另需 v5 live contracts（packages/contracts，本线禁止修改，需提案）以及 EDIT 撤权、TTL、effect 预算并发负例，这些均未实现。
