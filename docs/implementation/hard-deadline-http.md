# 外部调用的硬期限（eios-capability-gap D2，调度员 2026-10-22）

## 做法

上游 NEX-EIOS 冻结提交的 `src/eios/adapters/_deadline_http.py` 已 vendored 到 `packages/eios-core/src/eios/adapters/_deadline_http.py`，provenance 记在 `PROVENANCE.json`，按 ADR-006 处理。工作方式如下：

- 每个请求放在一个 forkserver 子进程里执行，子进程自成一个进程组；
- 父进程只按**一个总期限**等待，连接、TLS、写入、读取都算在这一个期限里，不是每一段各设一个 timeout 再叠加；
- 期限一到，父进程就杀掉整个进程组并回收。

所以 DNS、TLS 卡住，或者 provider 在响应中途停住，都不会让 worker 线程、数据库连接或连接池槽位被占过期限。

两处 adaptation：

- **支持原始请求体字节**：上游只支持传 json。
- **去掉每段操作 5 秒的上限**：模型补全常常要 30 秒以上才返回第一个字节，加这个上限会误判超时；总期限仍由父进程强制执行。

上游的 11 项测试已原样搬到 `tests/upstream/test_eios_deadline_http.py`（记录在 `UPSTREAM_ADAPTER_TESTS.json`），在 adaptation 之后全部通过。

NexLoop 的共用入口是 `nexloop_eios.deadline_http.post(url, headers, body, deadline_seconds)`：

- 正常时返回 `(status, body)`；
- 超过期限时抛出 `DeadlineExceeded`，它是 `TimeoutError` 和 `OSError` 的子类，现有的重试与分类逻辑可以直接识别；
- 其他传输问题抛出 `DeadlineTransportError`，带上 `code`。

## 出站 HTTP 客户端盘点

| 客户端 | 调用 | 处理 |
|---|---|---|
| `embedding_provider.ArkMultimodalEmbeddingProvider` | 外部 embedding（只读） | **改用硬期限**，`timeout`（默认 10 秒）现在是整次请求的总期限；超时在 `attempts` 次以内重试（没有副作用） |
| `conversation_extraction.OpenAICompatibleExtractionProvider` | 外部模型推理（Claim 抽取，claim-matcher 也用它） | **改用硬期限**，`timeout`（默认 60 秒）；超时归为错误码 `timeout` |
| `decision_eval.https_transport` | 外部模型推理（决策评估） | **改用硬期限**，`timeout`（默认 5 秒），在客户端原有的绝对期限和退避重试之内；超时归为 `timeout` |
| Agent Host `model-fetch.ts`、`pi-runtime-adapter.ts`（Node） | 外部模型推理（Run） | **不改**：每次模型请求都有自己的 `timeoutMs`，并且不超过 Run 的截止时间；连接和 TLS 由 `phasedConnector` 限时；Run 的计时器到点就中止。Node 的 fetch 跑在事件循环上，一个卡住的请求不会占住 worker 线程 |
| `effect_provider`、`outreach`、`incentives` 的 provider 调用 | 外部效应 | **不用硬期限**：已有绝对期限，未知结果按“先查询、再对账”处理，不会杀掉后盲目重试（测试锁定这三个模块不引用硬期限） |
| `runtime_dispatch`、`host_control`、`retention`、`sim_world_files`、`browser_test_profile`、`host_test_profile`、`container_entrypoint` | 本机回环上的内部服务（Host 控制、留存清理、模拟文件、自检） | 不在本项范围：内部调用，已有各自的 socket timeout |
| `consumer_channel` | 给人用的 CLI 客户端，调用 NexLoop API | 不在本项范围 |

## 进程启动

claim-extraction-worker、claim-matcher、recall-indexer 三个服务在读取任何凭据、打开任何连接之前，会先启动 forkserver（`ensure_request_runtime_ready`）。这样请求子进程都从一个干净的模板派生，不会继承凭据或连接。其他进程（例如 `scripts/nx_decision_shadow.py`）在第一次调用时才按需启动 forkserver。

## 测试

`tests/test_deadline_http.py` 用一个“接受 TCP 连接、之后一个字节都不回”的假 provider，模拟网络或代理卡住。覆盖以下几点：

- 共用入口在总期限内抛出 `DeadlineExceeded`，子进程被回收，假服务看到连接被关闭；
- 连接被拒绝时报传输错误，不当作超时；
- 只接受 https，期限必须在 0 到 120 秒之间；
- embedding（2 次尝试）、Claim 抽取（错误码 `timeout`）、决策评估的传输层都在各自的期限内返回超时，并释放资源；
- 外部效应模块不使用硬期限。
