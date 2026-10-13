# relay context_prepare（v6）耗时剖析

依据：调度员 2026-10-13。CI 主机上 C4-low（run 20261013T101946Z-fa9a71c，relay 1 个进程）的 relay 步骤计时显示，`context_prepare` 的 p50 为 2148 ms、p95 为 2600 ms，约占 relay 总耗时的 2/3。目标是在不改变语义和复查范围的前提下，把 p50 降到 500 ms 以下。

## 1. 方法

- 在本机用负载链（`tests/load_chain.py`，20 个客户，策略 `recent_plus_required`，relay run profile 模式，负载链自己的 relay Source）连续准备 15 条消息 Run，在 Python 侧按段计时。
- 用 cProfile 看函数级耗时，用 `track_functions='all'` 看 SQL 函数的自身耗时。
- 剖析用的测试与代码只在本地临时使用，没有提交。

## 2. 分段（本机，每条消息的中位数）

| 段 | ms | 说明 |
|---|---|---|
| `_snapshot`（核心快照） | 165 | 其中 `read_catalog` 48 ms，信封 68 ms，SQL 约 45 ms |
| 绑定第 1 次（bind1） | 115 | 信封 68 ms + `authz.nexloop_context_v6_command` 约 44 ms |
| 绑定第 2 次（bind2，写入后的复查） | 108 | 同上 |
| collect（来源读取） | 4 | |
| assemble_v6（Python 组装） | 0.6 | |
| put_artifact | 7 | |
| 认领与收尾 | 约 10 | |
| **合计** | **415** | CI 上约 2148，约为本机的 5 倍 |

**信封（每次约 68 ms）每条消息构建 3 次**（快照一次，绑定两次）。每次的组成：

- 服务目录信封 47 ms（两次对象读取，每次逐个属性取 READ 证明）；
- Run 证明 10 ms；
- 动作与工件证明约 9 ms；
- 消息读取信封 2 ms。

再加上快照里的 `read_catalog`（48 ms），**同样两个对象（ServiceOffering 与 ConsumerServiceOffering）每条消息要授权读取 4 次**，约占总耗时的一半。

**时间去向**：62% 是数据库往返（psycopg 等待），每条消息约 384 条 SQL。SQL 侧自身耗时最高的几项：

| 函数 | 说明 |
|---|---|
| relay Source 派生（`nexloop_relay_source_derivation*`） | 每条消息约 370 次（每个属性的每次读取证明都在 SQL 里重新派生一次） |
| `nexloop_property_access_derivation` | |
| 权威事实与身份快照（`load_authority_fact`、`root_identity`、`service_identity`） | 每条消息数千次调用 |

单次调用都很便宜，代价来自次数。

## 3. 尝试过但没有保留的优化

1. **给 `control.nexloop_relay_enrollments` 按 offering_id、binding_id 建索引**：在 200 个登记（CI 规模）下，派生查询从 0.105 ms 降到 0.096 ms，可以忽略；外层的 OR 条件仍然走顺序扫描。没有提交。
2. **批量化 `AuthorizedObjectReader.authorities` 的回退**：relay Source 没有配置授权，每个属性都要走"批量配置授权失败 → 单个配置授权再试 → 派生"。
   - 把"单个再试"也批量化**不等价**：批量解析只预取预测的事实键，可能把单个解析能找到授权的目标判成不可用。`tests/test_evidence_read_derivation.py` 因此失败，原实现通过。
   - 此外，评估类读取器覆盖了 `_authority`，批量路径会绕过它。
   - 改成只批量取派生依据（basis）之后与原实现等价：v6 上下文包逐条按规范化字节比对一致，信封除到期时间和签名外一致。但本机合计 402–447 ms 对比原来的 415 ms，在噪声之内，没有可测的收益。这段代码会改动授权路径，收益不足以抵消风险，没有提交。

**结论：在不改变复查方式的前提下，找不到能把 context_prepare 降到 500 ms 以下的改动。**

## 4. 需要决定的方案（会改变复查方式，按要求先回报，未实施）

按预期收益从大到小：

1. **同一条消息的准备过程内复用 Source 签名信封**（ADR-029 的原则用在 relay 上）。快照、bind1、bind2 三次改为构建一次。SQL 每次使用时仍按当前状态完整复查，第二次绑定仍然是写入后的复查，只是不在 Python 侧重新签发同样的证明。
   - 预计每条省约 130 ms（本机约 −30%）。
   - ADR-029 只批准了 guard 内的复用，用到 relay 需要扩展该 ADR，由负责人批准。
2. **复用快照阶段已读到的服务目录**（`read_catalog` 与目录信封读的是同一对象），与第 1 项一起做。预计再省约 45 ms。
3. **SQL 侧：同一次 `nexloop_read_object` 内，对同一对象的各属性只派生一次 relay Source 授权**，其余属性只检查属性名单与属性组限制。每次仍按当前状态复查，但复查的执行方式改变了（安全定义函数内的缓存），需要评审，或许需要 ADR。预计 SQL 侧的派生调用从每条约 370 次降到约 30 次。
4. **SQL 侧：在一次授权检查内缓存权威事实与身份快照**（现在每条消息数千次调用）。属于安全核心实现的变更，需要 ADR。

以 CI 约为本机 5 倍估算：
- 只做 1 和 2，CI 上的 p50 约 1.3–1.4 s；
- 要到 500 ms 以下，还需要 3 和 4 中至少一项。

## 5. 报告口径修正

`scripts/load/report.py` 生成的 report.md 口径段落改为："v0.1 门槛 5 s 只适用于它（C4-low），2 s 是 v0.2 目标（观察项 idle_dispatch_v02_target，不判定）"。数值取自 `THRESHOLDS` 与 `V02_TARGETS`，并加了测试。
