# ADR-032｜一次读取授权检查内，调用者身份只解析一次

状态：**已批准**（负责人 2026-10-13，ROADMAP §7 上下文准备优化第 4 项）。分支 `l2/read-derivation-once`（基于 ec0101a / ADR-031），迁移 0421（临时号）。

关联：ADR-031（一次对象读取内，派生只做一次）；L4 剖析 `docs/implementation/context-prepare-profile.md` §4 第 4 项。

## 1. 背景

每取一条权威事实，`authz.nexloop_load_authority_fact` 都会重新解析一次调用者身份（`service_identity` → `root_identity`，并检查凭据、租户、Run 凭据）。批量取事实（`authz.nexloop_load_authority_facts`，一批约 20 条）和读取检查（`authz.nexloop_assert_read_authority_inner_before_erasure_v0142`，逐条复核声明里的事实）都是这样。一条来信的上下文准备因此要解析身份数千次。

## 2. 决定

只改两个**读取**检查：

1. `authz.nexloop_load_authority_facts`：在第一条事实时解析一次身份，然后把它传给每条事实的读取（`authz.nexloop_load_authority_fact_snapshot_with_identity` → `authz.nexloop_load_authority_fact_with_identity`）。
   - 解析放在第一条事实自己的异常块里。解析失败时，每一条都标记为 `error`，与原来逐条解析、逐条失败的结果相同。
2. `authz.nexloop_assert_read_authority_inner_before_erasure_v0142`：在事实循环的第一条时解析一次身份，传给每条事实的读取。
   - 循环之后照旧重新取身份快照，并比对目录哈希与到期时间。

`authz.nexloop_load_authority_fact_with_identity` 与 `authz.nexloop_load_authority_fact` 逐行相同，唯一区别是身份由调用方传入。

**保护写入的检查不改**：动作（`nexloop_nonbrowser_assert_action_authority_v0044`）、编辑、关系创建、工件、孤儿对象。任何写入或变更路径都读不到这里的身份变量。

## 3. 缓存范围与键

- **范围**：只是一次函数调用（一次检查）里的局部变量（plpgsql 变量），调用返回即丢弃。
  - 不跨检查：同一事务中，每次检查都重新解析；
  - 不跨事务、不跨会话；
  - 不使用 GUC、临时表或任何持久存储。这些函数都带 `SET` 子句，PostgreSQL 会在函数返回时撤销函数内设置的 GUC，所以 GUC 本来也带不出函数。
- **键**：由 (令牌摘要, world) 解析出的身份，其中包含 tenant 与 principal 的绑定。
  - 一次调用只服务这一对 (令牌摘要, world)；
  - 每条事实仍按 (tenant, kind, key) 实时读取，并按种类校验键是否属于该身份（principal、credential、application 等），与原来相同；
  - 事实本身不缓存。

## 4. 语义差异与时间窗

- **批量取事实**：`load_authority_facts` 与它调用的函数都是 STABLE，整批本来就共用调用语句的同一个快照。逐条解析身份与解析一次，看到的是同一份数据。**没有语义差异。**
- **读取检查**（volatile plpgsql，READ COMMITTED 下每条语句取新快照）：原来每条事实都按当时的快照重新解析身份，现在用第一条事实时解析的身份。时间窗是这一次检查的事实循环耗时，本机为亚毫秒到毫秒级。
- **为什么可以接受**：
  - **凭据**：检查一开始（`lock_credential`）就对凭据行（或 Run 凭据和它的来源凭据）加 `for share` 锁。撤销凭据需要更新这一行，因此在检查所在事务结束前不能提交。窗口内不会出现已提交的凭据撤销。
  - **租户和目录**：循环后照旧对租户行加 `for share`，并重新取身份快照、比对目录哈希。窗口内提交的租户停用或权威修订，在检查结束前都会被发现，检查拒绝。
  - **事实**：每条事实仍实时读取、加 `for share`，并比对记录哈希，与原来相同。
  - **派发时复查和写后复查**：它们是之后独立的检查，各自重新解析身份，不受影响。效应在派发时、绑定在写入后，仍按当时的状态复查。
- **结论**：凡是原实现在窗口内会拒绝的情况（凭据撤销、租户变化、目录修订、事实变化），新实现要么因为加锁而使其不可能在窗口内提交，要么在同一次检查结束前拒绝。

## 5. 回滚

- 旧函数体保存为以下两个函数（只有属主可执行），也用于等价性对比：
  - `authz.nexloop_load_authority_facts_before_identity_once_v0421`
  - `authz.nexloop_assert_read_authority_inner_before_identity_once_v0421`
- 回滚方式：新迁移以这两个函数体 `create or replace` 原函数名。两个 `_with_identity` 函数可以保留。
- 不涉及数据。

## 6. SECURITY DEFINER 与 search_path

- 新增和保存的函数都是 SECURITY DEFINER，`search_path` 为 `pg_catalog,pg_temp`，`row_security=on`，属主 `nexloop_owner`，撤销 public，不授予任何应用角色。
- 改动的两个函数用 `create or replace`，名字、属主、授权、易变性（STABLE / volatile）、`search_path` 都不变。

## 7. 测试

- **等价性**：沿用 ADR-031 的对比包装，扩展到这两个函数。每次调用都同时执行新旧两个实现，比较返回值或错误。
- **撤销与不泄漏**（`tests/test_identity_once_per_check_pg.py`）：
  - 撤销在检查开始之前提交：检查拒绝；
  - 撤销在检查进行中：因凭据被加锁而无法提交，检查结束后才提交，下一次检查拒绝；
  - 同一事务中两次检查之间，事实变化和撤销都被第二次检查看到。
