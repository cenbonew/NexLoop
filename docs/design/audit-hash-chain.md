# 设计稿｜NX-059 审计防篡改与导出校验（审计哈希链）

状态：**已实现，待复核**（l2/audit-hash-chain，迁移 0320 临时编号，`nexloop_eios/audit_chain.py`，`scripts/ops/audit_chain.py`，测试 `tests/test_audit_hash_chain_pg.py`）。负责人 2026-10-22 Q6 按推荐 A 拍板：做防篡改和导出校验，不外送外部安全平台（不做 SIEM），校验密钥放在私有配置中，已发布的审计表不改。
依据：docs/design/eios-capability-gap.md C1；上游 NEX-EIOS 1e363db `src/eios/migrations/0059_authorization_audit.sql`（sha256 `741b3f76…58501`）。只移植语义（链头、逐条摘要、前驱链接、校验函数、不可变触发器、HMAC），没有复制代码。上游的 SIEM outbox、假名化、保留策略表不搬，NexLoop 的保留与擦除已经由 0129/0132/0140 实现。

## 1. 哪些表进链

| 表 | 进链 | 理由 |
|---|---|---|
| `runtime.audit_events` | 是 | 受治理 Action、负责人操作、导出下载、擦除与保留都在这里留痕，是合规要证明“没被删改”的主表 |
| `runtime.nexloop_effect_recovery_audits` | 是 | 外部效应恢复的人工决定，量很小，安全相关 |
| `runtime.nexloop_workbench_read_audit` | 否 | 每次读都写一行，量大；D6 规定到期后只保留按天计数，逐行进链的意义有限。以后需要时按天汇总进链 |

## 2. 结构：旁路链表 + 触发器，已发布的审计表不改

- `runtime.nexloop_audit_chain_pending`：进链表的 AFTER INSERT 触发器（security definer）往这里写一行：来源表、事件键、租户、**该行的内容摘要**（`sha256(to_jsonb(row)::text)`）、捕获时间。触发器不加锁，也不读链头，所以业务事务不会因为审计而按租户串行。
- `runtime.nexloop_audit_chain`：每个租户一条链。`seq` 从 1 连续递增；`link = sha256('nexloop-audit-chain/1|'||tenant||'|'||seq||'|'||source||'|'||event_key||'|'||record_digest||'|'||prev_link)`；创世前驱是 `sha256('nexloop-audit-chain/1|genesis|'||tenant)`。另记 `appended_at`、`pruned_at`、`pruned_ref`。
- `runtime.nexloop_audit_chain_heads`：每个租户的 `(seq, link)`。
- `runtime.nexloop_audit_chain_seals`：库外工具对链头的 HMAC 封印（见 §4）。
- 链表、链头和封印都只能追加。唯一允许的 UPDATE 是由擦除或保留事务把 `pruned_at` 从空设成有值（见 §6），其余 UPDATE、DELETE、TRUNCATE 一律被触发器拒绝。

## 3. 并发写入下的链序

- 业务事务只写 pending。追加由 `runtime.nexloop_audit_chain_append(p_limit)` 完成：逐个租户取 `pg_advisory_xact_lock`，`for update` 锁住链头，再按 pending 的 id 顺序取**已提交**的行，逐条计算 link 并写入链表，最后删除这些 pending 行。
- 链序就是追加时看到的顺序，不是插入顺序。还没提交的行下一轮再进链，不会漏，也不会重复（pending 主键加上链表的 `(source, event_key)` 唯一约束）。
- 追加由封印工具定时调用（systemd timer，与备份类似）。也可以由 scheduler 调用，不需要新的常驻进程。

## 4. 密钥：库内纯哈希，库外 HMAC 封印

- 如果 HMAC 在库内计算，密钥就得放进库里，DBA 可以重算整条链，防篡改就失去意义。所以库内只做纯 SHA-256 链接。
- 库外的 `scripts/ops/audit_chain.py seal` 从私有配置读取密钥（0600 文件，可以有多把，用 `key_id = sha256(key)` 的前 16 位标识，便于轮换），计算 `HMAC-SHA256(key, 'nexloop-audit-seal/1|tenant|seq|link|sealed_at')`，写入封印表。写入前，SQL 核对 `link` 确实等于链上该 `seq` 的 link。
- 有人想重算整条链来掩盖篡改时，已有封印上的 link 对不上；没有密钥又伪造不出新封印。最后一次封印之后到现在的这段，只靠哈希链接保护，这段窗口就是封印周期（建议 15 分钟）。密钥不进库、日志、fixture、提交或 prompt。

## 5. 校验与导出

- 库内 `runtime.nexloop_audit_chain_verify(tenant)`（不需要密钥）：
  - 逐条重算 link，检查 seq 是否连续；
  - 链头是否等于最后一条；
  - 源表中仍存在的行，重算内容摘要后是否等于链上记录的摘要；
  - 源表的每一行，都要么已在链上，要么还在 pending；
  - 链上没打 pruned 标记的条目，其源行必须存在；
  - 每个封印的 link 是否等于链上对应位置的 link。
  
  输出问题清单，不含行内容。
- 库外 `audit_chain.py verify --tenant T`：先调用库内校验，再用密钥逐个校验封印的 HMAC。
- 导出 `audit_chain.py export --tenant T --out DIR`：写出 `chain.jsonl`（每行一个链条目，源行仍存在时附带 `to_jsonb(row)::text` 原文）、`seals.jsonl` 和 `manifest.json`（范围、链头、条目数、各文件的 sha256）。
- `verify-export DIR`：完全离线，用导出的原文重算摘要，再重算 link 和封印的 HMAC。导出本身不需要库内额外授权：工具用 owner 连接，这是数据主机上的运维工具，和备份同一类。受治理的工作台导出（M23）另起任务，可以直接复用这个格式。

## 6. 擦除与保留传播

- 链上只有摘要和事件键，没有内容，所以擦除不需要改链。
- 源行被保留规则（0140 `governance_audit`）或擦除删除时，源表上的 AFTER DELETE 触发器只在“受治理的擦除或保留事务”中才放行，判据是该事务在 `control.nexloop_erasure_scope` 里有记录。放行时给对应的链条目设置 `pruned_at` 和 `pruned_ref`（取 erasure scope 的 request_ref）；如果该行还在 pending，就标在 pending 上，进链时一并带上。其他删除一律拒绝。
- 校验时：
  - 有 pruned 标记但源行缺失：合法，link 照样可验，因为 link 只依赖摘要；
  - 没有 pruned 标记而源行缺失：报“删除”；
  - 源行仍存在但摘要不同：报“篡改”；
  - 源表有行，但链和 pending 里都没有：报“插入”。
- 已知限制：超级用户可以绕过触发器，删掉源行并伪造 pruned 标记。pruned 标记不在 link 里，这种情况与合法的保留删除无法区分，只能从封印与导出存档的对比中看出来（导出里有原文）。所以文档要求导出定期存档到库外。

## 7. 性能

- 写路径：每条审计多一次 pending 的 INSERT，加一次 sha256 计算，没有锁。
- 追加：每批（例如 1000 条）每个租户一把事务级锁，与业务写入不冲突。
- 校验：全表扫描，按租户运行，属于离线和运维操作。

## 8. 迁移与权限

- 迁移使用临时编号：新增四张表，在两张源表上加触发器，新增 append、verify、seal、export 四个函数，全部归 nexloop_owner 所有，不授权给应用角色。
- 四张新表都启用并强制 RLS，使用 `tenant_boundary` 策略（按 `eios.tenant_id` 隔离）；只有 append 在事务级标志 `nexloop.audit_chain_append` 下跨租户读取队列。它们镜像 `runtime.audit_events`，而后者本身就没有租户外键（vendored core 的已知例外），所以这四张表也登记为已知例外，理由写在 `tests/test_tenant_fk_pg.py`。
- 不改动已发布的审计表结构和既有函数。存量行不回填：迁移时链是空的，存量行由第一次 append 带入 pending（迁移里 `insert … select` 一次，按创建时间排序）。
