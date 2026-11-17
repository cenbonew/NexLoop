# CI631: G1 metadata access and the simulated ledger boundary

基线 `45224cbdbafe8accf113cde21f40f14d0960fcda`。CI631 为9540 passed/1 failed，首败位于 `test_only_the_stand_in_functions_and_the_whole_deletion_read_the_simulated_ledger` 的 catalog enumerator 登记断言。隔离 PG 在未修代码上实际复现同一失败，1 failed（4.06s）；原CI回执和本地红报告保留。

## 原因与最小整改

0454 的 G1 函数使用 `pg_class`/`pg_namespace` 检查所有者、FORCE RLS 和专用身份是否误得业务表权限。守护测试将这种元数据扫描识别为“可能间接访问 simulated ledger”的入口，但尚未对它分类。实际新增访问是 catalog/ACL 元数据，不是 ledger 行。扫描包含模拟表的权限，才能拒绝其潜在错误授权；删除扫描会削弱 G1。

不改产品 SQL、权限、迁移、catalog、lock 或 G1 CLI，也不把函数名称直接加入旧 `CATALOG_ENUMERATORS` 名单。新分类必须同时满足：

- 受审 `prosrc` SHA256 精确为 `4b7aa394a41503c53d9de8dc203692126294cf2284d4c6ec2fa4febf0006d6ae`；今后任何函数体变化必须重新审核分类。
- 无参数/jsonb、STABLE、SECURITY DEFINER、owner=nexloop_owner；固定 search_path、row_security 与空 tenant GUC。
- 直接执行授权只有 owner 和专用 verifier。

原 ledger 表的所有非owner角色/PUBLIC ACL检查、显式函数/视图/规则/策略检查，以及其他未知 catalog enumerator 拒绝均保留。仅经过上述校验的元数据入口可通过此项登记，不能用同名函数获得永久豁免。

## 新增反例与真实运行证据

1. 在一次性夹具中为同名函数插入 ledger `count(*)`，分类必须拒绝函数体变化；不会继承登记。
2. 通过已有模拟入口实际产生一条 simulated intent；以专用非owner身份在只读RR事务调用 G1，四张 ledger 表的本事务扫描/读取/写入计数前后一致。统计查询只看 OID/计数，不读 ledger 行。
3. 一次性管理员误授 verifier 对 simulated intents 的 SELECT（仍无 schema USAGE），G1 必须报 authority/coverage invalid，证明 catalog ACL检查的必要性。

新增统计测试首轮使用 verifier 做 regclass 参数转换，被 schema runtime 的权限拒绝（2 passed/1 failed）；没有给它加权限。改为夹具管理员先解析固定 ledger OID，verifier只读 pg_catalog统计。该失败日志保留。

最终精确首败用例 + 两个新增边界用例 + 受限G1全文件：**30 passed / 0 failed/errors/skips，27.26s**。使用一次性本地PG、Unix socket、LC_ALL=C、独立basetemp，无现场连接、业务READ或准入。没有重跑完整CI，也不把此局部回归称为集成放行。

本树忽略目录 `local-only/ci631-evidence/` 保存基线红、统计测试首败、最终JUnit/log及原CI文件摘要引用。未修改原工作树、原首败、封存制品或 planning；待原L6固定提交独审与新集成CI。
