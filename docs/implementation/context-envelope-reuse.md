# relay 上下文准备内的签名证明复用（ADR-029a 实现说明）

依据：ADR-029a（负责人 2026-10-13 批准，ROADMAP §7 第 ① 项）。分支 `l3/context-envelope-reuse`，从 main 819820d 切出。只改 Python，不改 SQL 语义。

## 1. 改动

| 文件 | 内容 |
|---|---|
| `packages/eios-core/src/nexloop_eios/prepare_scope.py`（新增） | `PrepareScope`：一次准备内的证明缓存。<br>- 有效期取证明里所有 `expires_at` 中最早的一个，包括嵌套在 JSON 文本里的；<br>- 剩余不足 5 s 就重建；<br>- 读不出有效期的证明不缓存。 |
| `context_artifacts.py` | 两个 `prepare` 都改为在 `_scoped()` 里执行：消息 Run v2、v6，以及继承它们的兜底 v6。缓存对象在准备开始时创建，结束时丢弃。<br>`_envelope` 里以下证明在一次准备内只构建一次：<br>- 绑定 Action 证明；<br>- Artifact CREATE/READ 证明；<br>- Run 证明；<br>- 消息 READ 信封；<br>- 目录信封。 |
| 同上 | 快照阶段读取目录（`_read_catalog`）时，使用目录信封里同样的两个对象 READ 信封，通过 `authz.nexloop_read_object` 读值，核对规则与 `read_catalog` 相同。 |
| `service_offerings.py` | `read_catalog` 增加可选参数 `read`，`_catalog_envelope` 增加可选参数 `read_envelope`。不传时行为与原来相同。 |
| `object_reads.py` | 把 `AuthorizedObjectReader.read_signed` 从 `get` 中拆出来，SQL 调用本身不变。 |

SQL 每次使用证明时的完整校验、第一次绑定和写后复查、两次绑定结果必须一致的检查，都没有变化。

## 2. 测试

`tests/test_context_envelope_reuse_pg.py`，8 个用例：

- **等价性**：同一条来信、同一个 Run 和命令，复用时绑定的 v6 包，与不复用时组装的包按规范化字节比对一致，快照也一致。
- **证明的构建与复用**：每类证明只构建一次、其余次数都是复用，复用次数至少 12 次。
- **反例 1**：快照之后、绑定之前撤销 Source 凭据或改动目录，复用旧证明的绑定被 SQL 拒绝，分别报 `service authentication denied` 和 `offering revision stale`。之后没有任何绑定、上下文包或来源记录。
- **反例 2**：同样的修改放在第一次绑定和复查之间，修改无法落地，因为绑定事务持有它核对过的行，修改等到锁超时；绑定按原状态完成，提交后修改正常生效。
- **临近过期**：让复查前的证明只剩不足 5 s 有效期，复查前全部重建，绑定成功。
- **复用范围**：准备结束后 producer 不再持有任何证明；第二次准备从空的缓存开始，拿不到第一次准备的证明对象。
- **有效期读取**：单元用例。

另有回归测试：上下文 v2/v6、关系上下文、角色上下文、兜底回复、服务目录、读取推导、负载链、message relay 等 14 个文件，共 146 个用例，全部通过。

## 3. 计时（本机小模板）

测量方法：

- 负载链（`tests/load_chain.py`），20 个客户，策略 `recent_plus_required`，relay 使用 run profile 模式；
- 连续准备 16 条消息 Run，复用开、关交替；
- 每种模式去掉第一条（预热），取其余 7 条的中位数；
- 测量用的临时测试没有提交。

开发机当时负载约 9–10，共测了 3 轮，结果一致：

| 段（ms，中位数） | 不复用 | 复用 |
|---|---|---|
| snapshot（含快照里的证明构建和目录读取） | 205–209 | 150 |
| bind1，仅 SQL 调用 | 49–51 | 49 |
| bind2，仅 SQL 调用 | 42–44 | 42 |
| **context_prepare 合计** | **487–512** | **268–270** |

- 合计约下降 46%。节省来自两次绑定前不再重建信封，每次约 110 ms；以及快照里目录对象不再重复授权，约 55 ms。
- SQL 调用本身的耗时没有变化，符合"SQL 校验不变"。
- 按 CI 主机约为本机 5 倍估算，CI 上的 p50 大约从 2.1 s 降到 1.2 s 左右，以 CI 实测为准。要降到 500 ms 以下，还需要 L2 的 SQL 侧推导优化（第 ② 项）。

## 4. 与 L2 的边界

- L2 改的是 SQL 侧 `nexloop_read_object` 的推导（第 ② 项）。
- 本分支只改 Python 一侧，没有改任何迁移。
- Python 文件中，`object_reads.py` 和 `service_offerings.py` 只增加了可选参数和一个拆出的方法。L2 如果也改这两个文件，合并时需要留意。
