# ADR-042｜受信配置允许"只新增属性组"的类型版本递增

状态：**调度员 2026-10-31 定 A2（实现方式见 §2.1，与原定"包装"不同，请 L6 一并审核）**。这条改的是信任根，L6 PASS 之前不组批；调度员会告知负责人，合入前负责人可以叫停。来源：S77（激励目录对象的受治理创建与撤下）。实现：迁移 0390（临时号），函数 `control.nexloop_configure_group_increment`。

## 1. 背景

- 受信配置（0050，最新版在 0381，`control.nexloop_configure_manifest`）对 object_types 只接受第 1 版：遇到 `version<>1` 就拒绝，错误为 `schema increment not supported`。
- S77 需要让 catalog_maintainer 撤下（active true→false）它创建的激励目录对象。EIOS 的通用 `ontology.object.edit` 要求对**这个对象**和**这个属性**都有 EDIT 授权。对象创建之前没有 id，所以只能用 0084 的 `property_access_rule`（按主体和类型派生）。但 0084 只派生**有属性组**的属性，而 IncentiveOffering 和 ConsumerIncentiveOffering 已发布的 v1 没有任何属性组。
- 要让这两个类型可以按规则派生 edit，唯一需要的改动是：发布 v2，给已有的 `active` 属性加一个组。

## 2. 决定

受信配置新增一类、而且只新增这一类版本递增，称为"只增属性组"。这条规则是**通用的**：信任根里不出现任何业务类型名。

一个 object_types 条目同时满足以下全部条件，才算"只增属性组"（`control.nexloop_group_only_increment`）：

1. `version = N ≥ 2`，库里已有本租户 N-1 版；
2. 除 `version` 和 `property_groups` 之外，所有键与 N-1 版逐字段完全相同：不增删属性，不改值类型、required、enum，也不改展示字段；
3. N-1 版的每个属性组，在 N 版里原样存在：组名、展示名、成员都不变；
4. 至少新增一个组；新组的组名与旧组不重复，成员非空、不重复，每个成员都是**已有**属性，在 N-1 版里不属于任何组，也不同时出现在两个新组里。

### 2.1 实现：独立的配置步骤，不包装 configure_manifest

- 调度员原定的做法是"把 `control.nexloop_configure_manifest` 改名再包一层"。实现时发现这条路**走不通**：
  - 每插入一行类型版本，0009 的 epoch 触发器都会把租户的 `authority_revision` 加 1，0004 的守卫又保证这个值只能单调递增；
  - 所以包装先插入 N 版之后，原函数检查 `expected_revision` 时必然对不上；
  - 而修订号不能回退，只能复制原函数体，或者改 epoch 触发器，这两样都比本方案碰的信任根更多。
- 因此改为新增函数 `control.nexloop_configure_group_increment(tenant, text, digest, key_id, key)`。它和 configure_manifest **处于同一信任层**，是一个独立入口，不是旁路：

  | 项 | configure_manifest | configure_group_increment |
  |---|---|---|
  | 形态 | security definer，owner 为 nexloop_owner，`search_path=pg_catalog,pg_temp`，`row_security=on`，`timezone=UTC` | 相同 |
  | 调用方 | 只有 `session_user='nexloop_configurator'`；EXECUTE 只授给 configurator，对 api 和各 worker 角色撤销 | 相同 |
  | 文本与摘要 | `p_digest` 必须等于 `p_text` 的 sha256；租户以文本中的为准 | 相同；文本的 `kind` 必须是 `group_increment`，键集合固定为 6 个 |
  | 签名密钥 | 新密钥会写入；要求存在一把 active 且内容相同的密钥 | 只接受已存在且 active 的同一把密钥，不新增密钥 |
  | 并发与顺序 | 同一把 advisory lock；`expected_revision` 必须等于当前修订号 | 相同 |
  | 重放 | 同一 manifest_id：摘要和指纹都相同则返回已记录的结果，否则冲突 | 相同 |
  | 记录 | 修订号加 1，写一条 `configuration_publications` | 相同（`public_manifest.kind=group_increment`） |
  | 内容 | 全部配置 | 只有 v≥2 的 object_types，且必须通过 `nexloop_group_only_increment` |

  Python 侧的 `trusted_configuration.apply_group_increments` 与 `apply_manifest_with_secrets` 一样：用 configurator 登录，校验没有 superuser、bypassrls，生成规范文本和摘要，读取签名密钥。
  - 说明：configure_manifest 本身**没有** PG_CONTEXT 守卫（PG_CONTEXT 只用在 0381 的定义读取派生里），所以新函数也没有。两者的调用方限制都是 `session_user` 加上 EXECUTE 授权。
- 与 0082 的关系：0082 是业务层的人工审核发布，只能新增候选属性，由审核角色执行，不经过受信配置；本入口是配置层，只能给已有属性加组，由 configurator 执行。两者互不替代，也互不调用。
- `control.nexloop_configure_manifest`（0381 的函数体）**一字不动**，仍然拒绝任何它找不到"已存在且内容相同"的 v≥2 条目。
- 发布顺序（`catalog_publish.publish`）：
  1. 先发一份只含 v1 类型的清单；
  2. 再执行只增属性组这一步；
  3. 最后发完整清单，这时 N 版已经存在且内容相同，原函数照常放行。

### 2.2 结果

- 满足条件：插入 N 版，修订号前进；
- N 版已存在且内容相同：不做任何事，修订号不变；
- N 版已存在但内容不同：`configuration conflict`；
- 其他情况：跳版本、没有前一版、v1、加属性、删属性、改类型、改 required、改展示字段、组指向不存在的属性、重复成员、同一属性进两个组、复用旧组名、改旧组、修订号过期，全部拒绝，什么都不写。

## 3. 为什么不走 0082 的人工审核发布

- 0082 发布的是**候选属性**：新增一个属性，归入一个组，经人审核后加一个版本。它不能给**已有**属性加组。
- 0082 的发布者是业务审核角色，作用于业务数据层；而属性组决定的是 0084 授权派生的范围，属于配置层，应当由受信配置决定，和规则（`property_access_rule`）、owner 限制（`property_group_restriction`）走同一条通道。
- 改一个组不改变任何数据形状，已有对象不受影响。0084 派生按对象自己的 schema_version 读取定义，所以 v1 对象仍然没有派生的 edit 权限，只有 v2 对象有。

## 4. 影响

- 已发布的 v1 不变。新建对象绑定 v2（Action 的 object_types 写 `[[T, 2]]`）。stage 目前应当没有 v1 实例，运行单里有只读核对步骤。
- 以后任何类型需要按规则派生 edit 时，都可以走同一条路，不用再改信任根。
- 风险：允许受信配置给属性加组，等于允许它扩大**以后**可以派生授权的属性范围。缓解措施：
  - 规则本身仍需单独发布，而且必须列出组名；
  - 必须有 owner 限制事实，派生才会生效；
  - 业务上的最终防线在对象守卫（S77 是 0389）。
