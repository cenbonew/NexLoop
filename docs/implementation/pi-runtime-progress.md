# NX-014 Pi RuntimeAdapter 实施进度

2026-10-08。NX-014 保持 in_progress。新增可运行的冻结 Pi 适配器及真实 PostgreSQL 授权守卫，尚未接入正式 Host Run HTTP admission。Compose 继续诚实返回 product_ready=false。

实际改动：apps/agent-host/src/runtime-adapter.ts、pi-runtime-adapter.ts、runtime-provider-boundary.ts；对应原生测试与进程测试；packages/eios-core/src/nexloop_eios/runtime_authority.py、durable_queue.py、backend.py；新增 0036_runtime_task_lease_guard.sql 和 catalog 条目。CI 调整为先构建四个冻结 Pi 包再构建 Host，并要求 RuntimeAdapter JUnit 无失败、无关键跳过。Host package/lock 新增本仓库 Pi workspace 链接，没有替换上游版本。

每 Run 独立本地 SQLite，持久保存完整命令/input 摘要及 conversation/submission 绑定；每次打开读回 WAL/FULL，不允许 Memory 回退。入口验证完整 RunCommand，恢复缺文件拒绝；模型和工具真实调用入口检查当前授权、持久预算与期限。冻结 Pi 的 hook 异常会被吞掉，因此 hook 不能作为安全闸门。自动 compaction 关闭，工具不得直接访问 Models 或任意 shell。错误转换为固定公开 code，合成 secret sentinel 验证日志、SQLite/WAL/SHM 和后续 transcript 均无泄露。

真实后台守卫将任务 lease/fence/Worker credential、完整 PG 持久命令与当前 EIOS Run 身份联检。租约仅是调度权，不授予 Action 权限。0036 是追加迁移，原 35 条 SQL 与已测试 wheel 字节一致；版本锁仅将 extracted core head 从 0035 更新为 0036，三个 upstream commit 不变。

已执行 18 项真实 Pi Harness 测试、10 项真实 PG 守卫测试和 3 项真实 PG/Pi/OS owner 联合测试；联合测试使用私有测试 Unix socket，后台持有 Run token/DSN/signer，Node 只收到无密钥命令及继承 owner FD。实际第二进程争锁失败，SIGKILL 后恢复同一文件/receipt，撤权与 PG 租约过期阻止实际工具入口。详细命令、首次失败与重跑见 pi-runtime-evidence.json。

AT-038 已有实际进程锁证据。AT-039 的业务对账、AT-033 的外部未知核对、AT-034 的公开 HTTP409 和 AT-013 的完整渠道闭环仍未完成，不能用 SQLite 恢复替代它们。真实 DeepSeek 调用、可信成本上界和渠道凭据验证没有执行；这些不阻止 deterministic provider 与独立代码继续开发。正式 Host owner/PG transport/Run admission 装配仍需完成。

接入审计另发现：当前真实测试后台将 RunCredential token 保存在内存；现有 PG Run 行仅保存 digest，后台重启不能仅凭 credential_ref 恢复原 token。正式恢复还需受控 opaque activation/ref 解析端口或 backend-only sealed vault，并证明当前租约、撤权及旧 fence 拒绝。禁止为此将明文 token 写入 PG、SQLite 或 Artifact。

最终 scripts/ci/check exit0：996 Python（583.51s）、24 SQLite conformance、18 RuntimeAdapter（2.33s），四个冻结 Pi 包及 Web/Host build 通过，零关键跳过。全 CI 在新增联合测试文件前收集，因此另行运行的 3 项联合测试（5.27s、JUnit 无跳过）单独报告；当前 collect-only 是 999 项，不宣称完整 CI 单次运行了 999 项。新建 head0036 的 11-service Compose bootstrap、实际 HTTPS 登录与 Worker foundation 验证通过；仅停止经标签确认的本项目四个常驻容器，全部 exit0，保留 volumes。
