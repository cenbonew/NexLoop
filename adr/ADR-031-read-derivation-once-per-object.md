# ADR-031｜一次对象读取内，relay Source 派生每个对象只做一次

状态：**已批准**（负责人 2026-10-13，ROADMAP §7 上下文准备优化②）。分支 `l2/read-derivation-once`，迁移 0191。

关联：
- ADR-019 §2：负责人限制的属性组永不派生。
- 迁移 0142（`relay-source-v1` 派生）、0182（企业 slot 的包装）。
- L4 剖析 `docs/implementation/context-prepare-profile.md`（分支 l4/context-prepare-perf 8a2082c）§4 第 3 项。
- 优化①（复用信封）归 L3，不在本 ADR 内。

## 1. 背景

relay Source 没有配置授权，它对目录对象（ServiceOffering、ConsumerServiceOffering）的 READ 来自登记派生。`authz.nexloop_read_object` 对每个属性证明调用一次 `authz.nexloop_assert_read_authority`，每次都重新执行一遍完整的 `authz.nexloop_relay_source_derivation`：读中继规则、扫描登记、核对 binding、确认对象存在，再做属性部分的检查。

同一对象的各属性，派生结果中只有属性部分不同：
- 固定的字段清单；
- 属性存在于对象的 schema 版本中；
- 负责人限制的属性组；
- 限制哈希。

其余部分对同一对象都相同。L4 剖析显示，每条来信约有 370 次派生，主要来自这种重复。

## 2. 决定

在**一次** `authz.nexloop_read_object` 调用内：

1. 第一次遇到 `relay-source-v1` 的属性证明时，对**本次读取的对象**（`eios:object:<类型>/<id>`）做一次完整派生，得到对象部分。
2. 每个属性证明仍走 `authz.nexloop_assert_relay_source_property_read`。它与 `authz.nexloop_assert_relay_source_read` 逐行相同（擦除检查、凭据加锁、身份快照、声明核对、配置授权优先、到期、与证明中派生依据逐项比对），唯一区别是派生依据改为对象部分加上 `authz.nexloop_relay_source_property_basis` 算出的属性部分。
3. `authz.nexloop_relay_source_property_basis` 只做属性部分，即 0142 中 `prop is not null` 的那段，逐行相同。它还要求该属性属于对象部分所对应的那个对象（类型与 id 都一致），否则返回 null。
4. 调用结束前，对同一对象再完整派生一次，结果必须与第 1 步相同，否则以 `derived relay source read changed`（42501）拒绝。
5. 非 relay 的证明（配置授权、其他派生）不变。

## 3. 等价性论证

**同一条语句内。** 原实现中，属性 P 的派生结果等于“对象部分”加“P 的属性部分”。对象部分不依赖 P：
- 循环里选中的登记、binding 核对、对象是否存在，与属性无关；
- 属性清单不合格时，原实现在循环前返回 null，新实现由属性部分返回 null，同样拒绝；
- 属性定义缺失或属性组受限时，原实现在循环内返回 null，新实现同样返回 null。

0182 的包装先试回复列、再试企业列。两列各自的属性部分完全相同，所以“对象级先选列、再算属性部分”与“逐属性分别选列”结果相同。

**跨语句（READ COMMITTED）。** plpgsql 函数中的每条语句取各自的快照，原实现能看到调用中途提交的变化，新实现在第 4 步补上这一点：
- binding 行、对象行、中继规则事实在第一次派生时已被 `for share` 锁住，调用期间不会改变；
- 登记表只追加。中途提交的新修订（撤回、重登记）会改变 `enrollment_revision` 或使派生为 null，会被第 4 步发现；
- 属性定义和属性组限制在每个属性上仍逐项读取，与原来一样。

所以，凡是原实现会因调用中途的变化而拒绝的情况，新实现也会拒绝，只是拒绝发生在调用末尾，错误文本可能不同（同为 42501）。双方都成功时，返回值逐字节相同。

**范围。** 缓存只是 `read_object` 里的一个局部变量：
- 只在一次调用内，调用结束即丢弃；
- 只对一个对象：本次读取的目标，且属性部分校验对象 id；
- 只在一个租户：租户与主体取自已验证的对象声明，每个属性证明仍独立验证自己的租户、主体和凭据。

## 4. SECURITY DEFINER 与 search_path

- 新增两个函数都是 SECURITY DEFINER，`search_path` 为 `pg_catalog,pg_temp`，`row_security=on`，属主 `nexloop_owner`，撤销 public 执行权，不授予任何应用角色。它们只能从 `read_object` 内部调用。
- `read_object` 用 `create or replace` 修改，名字、属主、授权、`search_path` 都不变。
- 不新增可从应用角色直接调用的入口，也不新增可写状态。

## 5. 回滚

- 旧函数体保存为 `authz.nexloop_read_object_before_derivation_once_v0420`，只有属主可执行，同时用于等价性测试。
- 回滚方式：新迁移以该函数体 `create or replace authz.nexloop_read_object`，两个新函数可以保留或删除。
- 不涉及数据迁移，回滚后立即生效。

## 6. 测试

- **等价性**：测试库中可选启用对比包装（`tests/support/read_object_compare.py`，环境变量 `NEXLOOP_TEST_READ_OBJECT_COMPARE`）。每次 `read_object` 调用（包括 SQL 函数内部的调用）都同时执行新旧两个实现，比较返回值或错误（SQLSTATE 和文本）。在代表性语料上运行：v2/v6 包、关系上下文 v4、角色上下文、评估读取、属性授权派生、工作台成员读取、撤销、擦除传播、企业 slot。
- **反例**（`tests/test_read_derivation_once_pg.py`）：
  - 负责人限制的属性组中的属性，在同一调用中对象和其他属性都允许的情况下，仍然被拒；
  - 一个对象的派生不能用于另一个对象的属性；
  - 不在任何登记中的 offering 仍然被拒。

## 7. 不做

- 不跨调用缓存，也不缓存权威事实或身份快照（剖析 §4 第 4 项，另行决定）。
- 不改 Python 读取路径和信封（优化①归 L3）。
