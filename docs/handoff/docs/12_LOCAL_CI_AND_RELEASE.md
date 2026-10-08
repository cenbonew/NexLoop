# 本地 CI、代码同步与发布

## 1. 明确选择

**本地 Git＋本地 CI 控制器＋本地构建是主路径；GitHub 不是必需控制面。** 不把 GitHub Actions 的 self-hosted runner 等同于完整本地 CI，因为它仍依赖 GitHub 调度。

初版采用 APP_HOST 上受控 bare Git 仓库与 systemd 调度的 CI队列。核心流水线放 `scripts/ci/`，既能本地命令运行，也能将来接入 Forgejo/Woodpecker 等系统；不为第一版额外引入完整代码平台。

## 2. 信任和 OS 用户

`nexloop-git` 负责仓库与受限接收；`nexloop-ci` 负责隔离 checkout、测试与 rootless 构建；`nexloop-deploy` 只部署审核过的 release artifact；运行服务使用各自非root身份。真实 secret 和备份密钥不挂载给 CI。

Git post-receive hook 只校验受信 ref/SHA并写入队列，不同步执行构建、不执行任意分支里的管理脚本。队列控制器从受信安装路径启动，候选代码只在隔离环境执行。Git hook 的事件语义以官方文档为准。[S14]

公共 PR、fork 和 GitHub 同步分支不自动获得本地主机执行权。未评审外部代码须在无 secret、无内网访问的独立一次性环境测试；需要恶意代码安全边界时使用VM，不把普通容器当万能隔离。

## 3. 流水线

| 阶段 | 必须完成 | 失败处理 |
|---|---|---|
| Admission | SHA存在、信任来源、受限ref、去重、资源锁 | 拒绝进入发布队列 |
| Checkout | 指定SHA的干净工作区，锁定上游来源 | 不用可变工作目录构建 |
| Dependency | uv/pnpm frozen lock；完整hash/来源检查 | 不自动升级修复测试 |
| Static | 类型、lint、格式、导入边界、秘密扫描、许可证清单 | 失败阻止release |
| Unit/Contract | Python/TS、JSON Schema、API与状态机契约 | 保存完整报告，不吞掉失败 |
| Integration | 临时PostgreSQL/Valkey、真实角色/RLS、outbox/fencing/迁移 | 缺PG等关键测试算失败，不算skip成功 |
| Recovery/Security | 中断、重复、unknown、撤权、租户/世界隔离 | 任一关键失败阻止发布 |
| Frontend/E2E | WebChat＋工作台薄切片、SSE、权限、页面状态 | 保存截图/trace，不含真实客户数据 |
| Build | 锁定基础镜像的API/Host/Web镜像、SBOM | 不在服务运行容器内编译 |
| Package | image archive或本地registry digest、checksums、manifest、测试结果 | 不生成虚假成功attestation |
| Deploy stage | 单独发布入口、备份、迁移、受控重启、smoke | 自动停止继续开放dispatch |
| Sync | 把通过策略的代码和标签推送GitHub | 失败仅标sync_pending，不否认本地CI结果 |

网络获取依赖与测试环境分开：依赖获取阶段只向已允许 registry；测试阶段只用依赖缓存和临时服务，不接触 DATA_HOST。临时数据库运行 APP_HOST 的隔离CI命名空间，属于构建服务的一部分，不是业务数据库。

## 4. 权威构建结果

每次 CI 生成：commit SHA、父提交、文档/契约版本、上游源码锁、依赖锁hash、infra/image digests、EIOS/NexLoop迁移版本、测试集合与原始计数、失败/skip/重跑详情、SBOM、安全扫描和构建时间。

默认不通过自动重跑隐藏失败。确需重跑记录首次失败、原因与全部尝试；关键授权、执行幂等和迁移测试不得靠“重跑后绿了”通过。代码修改后必须重新生成完整发布证据。

构建输出使用 release_id＋SHA，保存 OCI/Docker image archive 与 SHA256，部署账户校验后 load；初版不强制 GHCR 或外部镜像仓库。后续可接本地 registry，但发布仍按 digest 而不是 latest。

## 5. 发布过程

部署是流水线的独立授权动作：默认受控触发 stage 发布；不要求运营业务每次 Action 人工确认。自动部署只可对明确定义的受信分支/环境开启。

发布前 drain 新的自主工作，禁止新的外部 dispatch；等待或记录 in-flight，unknown 先分类；生成可验证备份。Schema变化按兼容策略先 expand，再启新版本。Agent Host 必须停止旧 owner，再启动新版本，不能蓝绿实例同时打开同一个 SQLite 文件。

升级后校验readiness、迁移catalog、凭据scope、artifact读写、PG队列、runtime单owner和合成smoke，再恢复允许范围的dispatch。发布记录出具后运行SHA必须与通过测试的SHA一致。

## 6. 回滚

无Schema变化且数据兼容时切回上一已知可用镜像。存在Schema变化时先判断旧应用是否兼容；默认采用向前修复或expand/contract，不盲跑down迁移。需还原数据库时进入灾难恢复流程、阻断real dispatch并核对已发生外部动作。

EIOS/Pi接口或存储格式变化需专门迁移和恢复测试；旧runtime文件与新Host不兼容时保留文件并标blocked，不能删除后假装任务完成。

## 7. GitHub 同步与社区入口

本地 `origin` 为受控LAN仓库，`github` 是同步remote。默认只推受信分支和发布标签；不使用未经检查的 `git push --mirror`，避免暴露私有运维分支/历史。GitHub暂不可达时本地开发、测试和已缓存依赖的运行不停止；记录同步积压。

外部贡献可来自GitHub PR。拉到隔离review分支，不直接触发带部署能力的CI；维护者评审后进入受信路径。GitHub是协作/代码副本，不备份数据库、Artifact、密钥、备份和客户原文。

## 8. 需要实现的命令契约

Codex 应交付可实际执行的 `make lint`、`make test`、`make test-integration`、`make test-recovery`、`make test-e2e`、`make ci`、`make build-release`、`make deploy-stage`、`make verify-release`、`make sync-github`。这些是目标命令，不是本交接已实现的软件。

命令支持清晰exit code、机器可读JSON结果、失败时不中断证据保存。所有破坏性命令默认dry-run并要求目标环境、精确路径与授权；CI不得调用真实删除/退款/付款接口。

## 9. 并发和资源

同一stage环境只允许一个release lease；同一时间默认1个CI build。构建cache有配额与清理策略，不能清理正在运行的数据卷。运行服务资源优先，后台演进和全量测试可以暂停，但不能降低关键安全测试。
