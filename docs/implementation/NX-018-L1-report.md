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
