# NX-049 多进程 guard 原型（`--guard-workers N`，默认 1）

分支 `nx049-mp-proto`，BASE main `1d28796`（已含 L4 的同 Run 锁顺序修复）。

这是 `NX-049-multiprocess-guard.md` §7 第 1、2 步的原型：**可开关、默认关闭**，不改默认值，也不改部署形态（那两件事需要 ADR 和负责人确认）。没有迁移，也没有新的 SQL 函数。

## 1. 改动

| 文件 | 改动 |
|---|---|
| `packages/eios-core/src/nexloop_eios/runtime_control.py` | `create_runtime_guard_server` 新增可选参数 `listen_socket`：接受父进程创建的监听套接字，不自己 bind，也不 listen。<br>套接字必须是 `AF_INET`/`SOCK_STREAM`，`getsockname()` 等于 `('127.0.0.1', port)`，端口在 1024..65535 之间；平台支持时，还要求 `SO_ACCEPTCONN` 为真（macOS 不支持查询，跳过这一项）。<br>不满足就 `ValueError`。<br>继承的套接字设为非阻塞：多个子进程会同时被唤醒，没抢到连接的 `accept()` 返回 EAGAIN，socketserver 本来就会忽略，不会卡住 `serve_forever`。<br>`GuardServer` 新增 `drain(timeout)`：在 `shutdown()` 之后，取回全部 8 个连接槽，确认每个已接受的连接都已应答。<br>处理器与全部校验逻辑都没有改动 |
| `packages/eios-core/src/nexloop_eios/runtime_guard_worker.py`（新增） | 子进程入口 `python -m nexloop_eios.runtime_guard_worker --listen-fd …`：自己打开 `Backend`，`verify_application_role` 必须是 `nexloop_domain_worker` 或 `nexloop_scheduler`，用 `_FreshGuard` 预先认证一次，然后才开始 accept。<br>就绪时输出固定的 ready 行；失败时只向 stderr 输出固定文本。<br>收到 SIGTERM/SIGINT，或父进程的管道 EOF（父进程退出）后：停止 accept → `drain(15)` → 关闭监听 → 退出 `Backend`（`LifecycleLock` 会等在途的 commit/fsync）。<br>同一文件里还有父进程侧的 `GuardWorkerPool`：创建并绑定监听套接字（`SO_REUSEADDR`，**不用 `SO_REUSEPORT`**），以 `pass_fds` 拉起 N 个子进程（exec 新的解释器，不 fork 带线程的进程）；子进程的 stdout 只用来读 ready 行，stderr 丢弃。<br>机密只以私有文件路径传给子进程 |
| `packages/eios-core/src/nexloop_eios/runtime_worker.py` | 新增 `--guard-workers N`（1..16，默认 1，越界时 fail-closed）。N=1 时走原来的代码路径，没有改动。<br>N>1 时，调度器留在父进程，guard 由 `GuardWorkerPool` 提供；所有子进程都 ready 之后，才输出 `Runtime Worker ready` |
| `tests/test_runtime_host_admission.py` | 夹具 `guard_server(worker, tmp_path, key, spawn=None)`：设置 `NEXLOOP_TEST_GUARD_WORKERS=N`（N>1）且调用方传入 `spawn`（与 `worker` 相同的 DSN、签名密钥、Artifact 根目录和服务 token）时，用 `GuardWorkerPool` 拉起 N 个子进程。<br>其余情况与原来完全相同 |
| `tests/test_relationship_context_v4.py`、`tests/runtime_effect_fixture.py`、`tests/test_role_pi_effect_checkpoint.py` | v4[complete] 与两 Pi 用例补传 `spawn`（`plan['worker_spawn']`），所以这两个用例可以在多进程模式下跑 |
| `tests/test_runtime_worker.py` | 新增 7 项多进程测试（§3） |
| `scripts/perf/nx049_profile.sh`、`scripts/perf/nx049_report.py` | `host.txt` 记录 `guard_workers` 与 `pgfunc`。<br>报告的 `concurrency` 新增 `processes`，以及 `overlapped_any_process`（不论是否在同一进程，只要请求有重叠就计入） |

## 2. 父进程监管

- **子进程意外退出**：在 `restart_window`（60 s）内累计重启不超过 `max_restarts`（4）次时，立即拉起替代进程，并等待它 ready。
- **超过上限，或替代进程起不来**：`failed=True`，父进程的 `stop` 被置位。父进程停止 claim，向 stderr 输出 `Runtime guard workers unavailable`，以退出码 1 退出，由外层服务管理器决定是否重启。
- **停机**（SIGTERM/SIGINT 或 `--once` 结束）：
  1. 父进程停止 claim；
  2. 向每个子进程发 SIGTERM，子进程各自排空；
  3. 父进程等待最多 20 s，超时仍未退出的子进程会被 SIGKILL，并以 `guard shutdown unavailable` 失败退出，与 N=1 时 guard 线程停不下来的处理一致；
  4. 最后关闭监听套接字，释放端口。
- **父进程被 SIGKILL**：子进程的 stdin 管道 EOF，子进程自行排空并退出，不留孤儿进程。子进程使用独立的会话，所以终端发给进程组的信号不会直接打到子进程，统一由父进程转发。

## 3. 测试

新增（`tests/test_runtime_worker.py`，真实的 Runtime Worker CLI、真实 TLS 的 Node Host、受限 PG）：

| 测试 | 断言 |
|---|---|
| `test_cli_guard_workers_once_consumes_persisted_runtime_task` | `--once --guard-workers 2` 能消费已持久化的任务，Run 成功，`business_action_success=false`，输出已脱敏 |
| `test_cli_guard_workers_stop_drains_in_flight_request_and_releases_port` | **停机排空**：子进程正拿着一个请求（有效的传输密钥，body 还没发完）时，向父进程发 SIGTERM。0.5 s 后父进程仍在等待；补发 body 后，客户端拿到完整的 400 应答；随后父进程以 0 退出，子进程全部消失，端口可以重新绑定 |
| `test_cli_guard_child_killed_mid_request_is_replaced` | **子进程中途被杀**：请求进行中时 SIGKILL 全部子进程。客户端连接结束且没有任何应答，Host 侧等同于超时：authorize 按拒绝处理，effect 走 `find` 对账。父进程拉起 2 个新的子进程（pid 都不同），新请求能正常得到应答 |
| `test_cli_guard_children_crashing_repeatedly_stop_the_worker` | **反复崩溃**：持续 SIGKILL 子进程，父进程以退出码 1 退出，stderr 是固定文本，子进程全部消失，端口释放；之后新进来的任务仍然是 `pending`，fencing token 为 0，没有被 claim |
| `test_cli_guard_workers_invalid_count_fails_closed[0/17/x]` | 进程数非法时启动失败，不 claim 任务 |

回归（本机）：

| 模式 | 范围 | 结果 |
|---|---|---|
| N=1（默认，现有测试应逐项照旧） | `test_runtime_worker`（含新增）、`test_runtime_guard_transport`、`test_runtime_host_admission`（含 SIGKILL 后 reopen）、`test_backend_lifecycle_capacity`、`test_queue_permit_expiry`、`test_receipt_tail`、`test_effect_intents`、`test_receipt_reconcile_roles`、`test_local_effect_worker_kill`、`test_runtime_allocated_before_pi_kill`，全部使用 `guard_server` 的用例（context v6、relationships、artifacts、message relay、commit window、effect e2e/recovery/tools/bridge、offering runtime、scope denials、runtime dispatch、role context v6、v4、两 Pi），以及 `test_vendor_imports`、`test_wheel_install`、`test_function_search_path` | **260 passed，305 s**（`-n 4`），一次通过 |
| `NEXLOOP_TEST_GUARD_WORKERS=4` | `test_relationship_context_v4`（整个文件）、`test_role_pi_effect_checkpoint`、`test_runtime_worker`、`test_runtime_guard_transport` | **78 passed，116 s**（`-n 4`），跑完后没有残留的 `runtime_guard_worker` 进程 |
| 逐个用例 | v4[complete] 与两 Pi，在 N=1 / 2 / 4 下各跑一次 | 都是 2 passed |

开发中的首次失败（已修复，都是原型代码自身的问题）：
1. macOS 不支持查询 `SO_ACCEPTCONN`（`OSError 42`）：改为查询失败时跳过这一项。
2. 子进程退出时，守护线程卡在 `sys.stdin.buffer` 的锁上（`_enter_buffered_busy`）：改用无缓冲的 `os.read(0)`。
3. 停机卡住：多个子进程共享阻塞的监听套接字时，没抢到连接的一方会阻塞在 `accept()`，导致 `shutdown()` 等不到 `serve_forever` 退出。改为把继承的监听套接字设为非阻塞。

§7 第 2 步中“锁等待后最终期限”系列（`test_receipt_tail`、`test_queue_permit_expiry`、`test_backend_lifecycle_capacity`）和 O3/8a/8c 的等价测试，测的都是进程内的 Backend 和 SQL 语义，与 guard 是否在子进程中无关，所以只在 N=1 下跑了。多进程影响的只是 guard 的传输与监管，这部分由上面新增的测试覆盖。

## 4. 本机 N=1 与 N=4 的交替对比（仅供参考）

命令（探针在这个分支上默认仍是开启的，这里显式关闭）：

```bash
NEXLOOP_PERF_PGFUNC=0 NEXLOOP_TEST_GUARD_WORKERS=4 scripts/perf/nx049_profile.sh mp-n4-1 1
```

条件：交替各跑 2 轮。**本机负载 10–13**（其他会话在跑测试），两种模式都出现了与负载相关的失败：
- N=1 第 1 轮，两 Pi：`runs/inspect` 返回 503；
- N=4 第 1 轮，两 Pi：测试客户端读 Host 超时；
- N=4 第 2 轮，v4：effect submit 2003 ms 超时。这次请求在服务端的激活 SQL 用了 3.36 s，时间窗内 PG 活动采样有 125 次 `IO:DataFileExtend`（约 2.5 s），没有锁等待，是磁盘 IO 停顿。

| 用例 | 指标 | N=1（第 1 / 2 轮） | N=4（第 1 / 2 轮） |
|---|---|---|---|
| v4 | authorize p50 / p95 | 585 / 934，585 / 782 | 494 / 873，435 / 757 |
| v4 | 重叠请求中位数（任意进程），总耗时（Python 部分） | 599 (340) / 614 (328) | 517 (251) / 441 (229) |
| v4 | effect submit p50 | 1046 / 918 | 770 / 756 |
| 两 Pi | authorize p50 / p95 | 681 / 1007，630 / 745 | 510 / 920（只跑到 6 次 authorize），484 / 707 |
| 两 Pi | 重叠请求中位数（任意进程） | 679 (488) / 640 (451) | — / 484 (328) |

方向与设计稿 §6 一致：重叠请求的 Python 部分降了约 26–30%，authorize p50 降了 15–25%。但负载过高，失败与否不能用来判断。正式结论以部署主机的交替对比为准。

## 5. 给部署主机对比的说明

在同一个运行目录内交替执行：

```bash
NEXLOOP_PERF_PGFUNC=0 NEXLOOP_TEST_GUARD_WORKERS=1 scripts/perf/nx049_profile.sh deploy-mp-n1-r1 1
```

```bash
NEXLOOP_PERF_PGFUNC=0 NEXLOOP_TEST_GUARD_WORKERS=4 scripts/perf/nx049_profile.sh deploy-mp-n4-r1 1
```

- 这个分支基于 `1d28796`，不含 `nx049-mp-design` 里“探针默认关闭”的改动，所以需要显式设 `NEXLOOP_PERF_PGFUNC=0`。
- 每个 guard 子进程都会写自己的 `proc-<pid>.json`。看 `summary.json` 的 `concurrency.processes` 和 `overlapped_any_process`：N=4 时，同一进程内的重叠应该明显减少。
- 资源方面，每个子进程有自己的连接池（`NEX_EIOS_DB_POOL_MAX`，默认 4）。v4 用例中 guard 会先后起两次（本机看到两组各 4 个子进程，时间上不重叠），峰值是 4 个子进程 × 4 个连接，再加上测试进程自己的连接池。一次性测试集群的 `max_connections` 默认是 100，够用。
