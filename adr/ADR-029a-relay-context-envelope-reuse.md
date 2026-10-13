# ADR-029a｜在同一条来信的上下文准备内复用 Source 签名信封（扩展 ADR-029）

状态：**已批准（负责人 2026-10-13）**。依据是 ROADMAP §7 批准的 relay context_prepare 优化第 ① 项（main 819820d）。实现在分支 `l3/context-envelope-reuse`（从 main 819820d 切出），只改 Python，不改 SQL 语义。

关联：
- ADR-029（guard 在同一 Run 的多次授权间复用 Source 签名信封）：本 ADR 把同一原则用到 relay 的上下文准备上，范围更窄。
- AGENTS.md"派发时按当前权限复查"。
- L4 的剖析：`docs/implementation/context-prepare-profile.md`（分支 `l4/context-prepare-perf`，提交 8a2082c）。

## 1. 背景

relay 为一条来信准备消息 Run 的上下文（v6），要经过三个签名步骤：

1. SQL 核心快照（snapshot）；
2. 第一次绑定（bind）；
3. 写入后的复查，即第二次绑定（bind recheck，与第 2 步在同一事务里）。

每一步都会重新构建同一组 Source 签名证明：

- 绑定 Action 的 EXECUTE 证明；
- Artifact CREATE/READ 证明；
- Run 证明；
- 消息 READ 信封；
- 服务目录信封，其中包含 ServiceOffering 和 ConsumerServiceOffering 两个对象 READ 信封。

快照阶段读取服务目录（`read_catalog`）时，还要对这两个对象再授权、再读取一次。

剖析结果：

- 每次构建信封约 68 ms，每条来信 3 次；
- 两个目录对象每条来信要授权读取 4 次；
- CI 主机上 context_prepare 的 p50 为 2148 ms，约占 relay 耗时的 2/3。

## 2. 决定

1. **复用范围**：只在同一条来信的这一次准备内复用。快照、第一次绑定和写后复查共用一组 Source 签名证明（`nexloop_eios.prepare_scope.PrepareScope`）。
   - 这组证明在准备开始时创建，结束时丢弃，无论成功还是失败。
   - 不跨来信、不跨 Run、不跨租户、不跨 world：缓存只挂在这次准备的 producer 上，身份就是 producer 自己的 Source 会话（同一租户、world=real），键里带着来信 id、Run 摘要、目录对象 id 和请求范围。
2. **有效期**：每个证明里所有 `expires_at` 中最早的那个，剩余不足 5 s 就重建。读不出有效期的证明一律不缓存。
3. **SQL 校验不变**：SQL 每次使用证明时，仍按当前事实、记录摘要和目录摘要完整校验，与新建的证明完全相同。
   - 校验失败时，这次准备就失败；准备内部不重试，缓存随准备结束一起丢弃。
   - 下一次准备（relay 重新认领这条来信时）从头构建全部证明。
4. **写后复查不能跳过**：第二次绑定照常执行，结果必须与第一次相同，否则准备失败。复用的只是 Python 一侧签好的证明，不跳过任何 SQL 调用。
5. **目录复用**：快照读取目录时，使用的就是这次准备里目录信封所含的那两个对象 READ 信封。值读取走 `authz.nexloop_read_object`，与 `read_object` 构建的信封逐字节相同。
   - 目录信封带着这两个读信封和请求范围（含快照读到的 offering 修订）。
   - 绑定时由 SQL 核对目录摘要和修订：`nexloop_assert_service_offering` 要求当前修订等于请求修订；`nexloop_service_catalog_scope` 复查读证明、Source 身份和目录摘要。
   - 目录在快照之后发生变化，绑定会被拒绝（`offering revision stale`）。

## 3. 理由

- 与 ADR-029 相同：信封只是"Source 在某一时刻能读到这些内容"的签名证明，是否仍然成立由 SQL 每次按当前状态判定。复用一个仍在有效期内的证明，与现场新建一个内容相同的证明，授权结果等价。
- 本 ADR 的范围比 ADR-029 更窄：ADR-029 在同一 Run 的多次授权之间复用，最长约 25 s；这里只在一次准备内复用，通常不到 1 秒。
- 第一次绑定与写后复查在同一事务里。两次之间，绑定事务持有它已核对过的行：撤销 Source 凭据或改动目录都要等这个事务提交后才能生效，因此复查看到的状态与第一次绑定相同。测试证明了这一点：两次绑定之间尝试修改，会一直等到锁超时。

## 4. 验证

`tests/test_context_envelope_reuse_pg.py`：

- **等价性**：对同一条来信、同一个 Run 和命令，不做复用的准备组装出的 v6 包，与复用时绑定的包按规范化字节比对一致，快照也一致。
- **反例**：
  - 证明在快照时构建之后、绑定之前，撤销 Source 凭据或改动目录：复用这些证明的绑定被 SQL 拒绝（分别为 `service authentication denied` 和 `offering revision stale`），不留下任何绑定、上下文包或来源记录。
  - 同样的修改放在第一次绑定和复查之间：修改无法落地（锁超时），绑定按原状态完成；绑定提交后修改正常生效。
  - 复查前证明只剩不足 5 s 有效期：复查前全部重建，绑定成功。
- **复用范围**：准备结束后 producer 上不再持有任何证明；第二次准备从空的缓存开始，拿不到第一次准备的证明对象。
- **有效期读取**：能读出嵌套的 `expires_at`，包括 JSON 文本里的；没有有效期的证明不缓存；剩余不足 5 s 时重建。

## 5. 不在本 ADR 范围内

- SQL 侧的 `read_object` 推导优化（ROADMAP §7 第 ② 项，L2）。
- 跨来信或跨 Run 复用。
- 关系上下文核心（v4 链，`RelationshipContextArtifactProducer`）自己的信封：它不在这条路径上（消息 Run 的 v6 核心是 v2 快照），本次未改动。
