# Restricted G1 verifier

基线 `4369820013c2f91afb15ced86956fa239ebfbfcf`；独立新增迁移 `0454_restricted_g1_verifier.sql`，接在 0453 后。0451–0453 字节不变，封存批176不变。本增量不是现场安装、身份启用、准入或部署许可；未修改原运行单及 planning。

## 入口与权限

`nexloop_maintenance.nexloop_g1_verify()` 无参数，只返回固定五键汇总：`status`、`tenants_checked`、`suspended_checked`、`effect_actions`、`active_admissions`。不返回租户、主体、业务对象、Action 行或凭据。

专用 `nexloop_g1_verifier` 默认 **NOLOGIN、无口令**，无 SUPERUSER/BYPASSRLS/CREATEROLE/CREATEDB/REPLICATION/INHERIT，无 owner 成员资格。仅在独立 maintenance schema 获 USAGE 与此函数 EXECUTE，不给业务表 SELECT/写入/DDL，不开放业务 authz schema。现场限时启用、凭据安全注入和停止既有会话须独立批准；VALID UNTIL 不自动结束已建立连接。

函数是 STABLE、SECURITY DEFINER，owner 为既有 `nexloop_owner`；固定 `search_path=pg_catalog,pg_temp`、`row_security=on` 和空 tenant GUC，表名全限定。实际登录必须是专用身份，会话须为 READ ONLY + REPEATABLE READ 或 SERIALIZABLE；普通身份即使误得 EXECUTE，也不能调用。运行时拒绝 owner/verifier 的危险角色属性、专用角色任何成员关系、表 owner/FORCE RLS 或策略结构异常。

owner 是具有广泛对象所有者能力的受信 definer，不是普通只读角色；本方案没有把其成员资格交给调用者。若受信迁移所有者被攻破或篡改函数，此方案不提供对超级用户的隔离。普通 READ/业务角色的授权没有扩大。

## 全租户与快照

从当前数据库 `control.nexloop_tenants` 自行读取全部注册租户，包含 suspended；拒绝该注册表出现 RLS，防止它成为被过滤的局部来源。逐租户设置局部 GUC，在仍受 FORCE RLS 约束的 admissions 表统计 `retired_at is null` 的行。注册表为空、全局 effect_actions 阳性为空、扫描数不等于同快照注册数或任何 SQL 错误都拒绝，不变成零。显式核对唯一 tenant policy 的 roles/command/USING/WITH CHECK，防止错策略隐藏非零。

STABLE 函数及事务快照保证本次注册列表和所有计数处于同一快照；并发新租户/准入会在下一事务才能看到。它证明的是该固定数据库快照，不保证函数返回后无人新增准入，也不替代现场目标绑定或准入冻结。没有对调用者 tenant 列表的信任，也不写正式对象或审计表。GUC/row_security 的函数设置在返回/错误后恢复，不污染调用方会话。

## 调用端必须停止的条件

`scripts/acceptance/g1_verify.py --database-url-file <已批准身份的私有文件>` 只有固定查询，不接受 tenant/SQL/role 参数，没有身份回退或配置窗口。它开启 REPEATABLE READ READ ONLY，连接上限5秒、statement 10秒、lock 1秒；先校验完整固定字段、整数域及状态一致性，并明确成功 ROLLBACK，才输出汇总。

| 结果 | CLI退出码 | 含义 |
|---|---:|---|
| `status=clear`，租户和阳性数均大于0、active=0 | 0 | 仅该快照 G1 为零，仍非部署/准入授权 |
| `status=blocked_nonzero`，active>0 | 1 | **必须停止**；SQL执行成功不是放行 |
| SQL/身份/策略/覆盖/格式/连接/ROLLBACK任一不可确认 | 2 | `blocked / verification_or_rollback_unavailable`；必须停止，不输出原异常/DSN |

执行器应私有留存固定源码/数据库对象绑定引用、调用时间、登录身份与结果/退出码及快照条件；不得留存口令/DSN。函数不写数据库审计记录，避免“只读检查”产生业务写入；部署方必须接既有受信外部执行审计，不得通过新增普通 READ 入口调用。现场若需将检查与后续切换绑定，须另审冻结/互斥或同事务方案；当前 CLI 的 ROLLBACK 后不存在原子部署保证。

## 隔离验证与已知边界

`tests/test_restricted_g1_verifier_pg.py` 在一次性 PG 中由测试管理员显式启用无口令测试身份。种入 active/suspended 两租户，模拟准入仅发生在可丢弃夹具，不是现场准入。

覆盖零、挂起租户非零、调用端退出0/1/2、caller tenant/row_security污染及恢复、并发注册/准入快照、空注册、SQL错误、策略隐匿/关闭FORCE RLS/多授成员资格拒绝、普通角色拒绝、第三方误得EXECUTE仍拒绝、默认NOLOGIN/无凭据、无表/DDL/owner权限，以及业务命名空间可调用函数仅此一项。

首轮 PG 因缺 LC_ALL 的 macOS 启动错误未跑到迁移；设置 LC_ALL=C 后16项通过。改独立schema、补默认NOLOGIN等后，权限枚举测试首次因psycopg参数化SQL中的字面百分号未转义失败（10 passed/1 failed）；仅修测试的 `%%`。随后相关基础门142 passed，新增调用端后的本文件25 passed。原始失败/重试日志留私有，不把它们删成首轮全绿。

catalog/checksum 与 lock revision 更新到0454；该临时编号与另一受保护Role树可能冲突，交调度集成时重排，本树不修改另一棵树。正式合入前仍须原L6独审及新固定集成SHA完整CI；没有现场身份启用、执行、生产证据或正式任务完成声明。

最终实际验证：本文件27项（含固定CLI退出0/1/2及ROLLBACK失败闭锁），连同 bootstrap、DB boundary、published checksum、function copy guard、NX067 ceiling guard 合计 **151 passed / 0 failed/errors/skips**（31.28s）。另 grant drift、effect Action admissions 全文件及原 G1 owner 守护 **45 passed / 0 failed/errors/skips**（117.83s）；两份报告去重 **196 个唯一 testcase**。最后将任意写权限负例显式切为 READ WRITE，避免只读默认值遮蔽ACL问题，该节点再验 **1 passed**（3.35s，不重复计数）。独立 `scripts/check_definer_search_path.py` 用一次性集群得到 **1536 routines pin search_path、1224 SECURITY DEFINER、0 problems**。

实际pytest均用本树 `packages/eios-core/src:tests`、禁插件自动加载、`LC_ALL=C`、`-p no:cacheprovider` 和每轮独立 `/tmp/nxg1-1117-*` basetemp；根conftest只创建并销毁本轮一次性PG，Unix socket、空listen_addresses，不连接现场。完整argv/JUnit/首败和重试日志在本树忽略的 `local-only/g1-evidence/`。SQL不写业务状态，测试管理员的合成注入只在一次性夹具。
