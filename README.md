# NexLoop

企业级开源持续运营系统。目标、业务事实和 Action 治理由项目内 vendored EIOS 与 PostgreSQL 持有；Pi Durable 通过 RuntimeAdapter 执行短生命周期 Run；EvoOntology 仅选择性用于语义与评估。

每次合入 `main` 的变化记录在 [更新日志](CHANGELOG.md)。

当前已完成 S0 源码、许可和环境审计，并实现 S1 的首批抽取内核、独立空库 bootstrap、受限角色、Artifact 文件层、真实 PostgreSQL 授权元数据、已发布 Action/schema 校验、受治理实例创建、属性修改、Relation 建立与严格本地 CI。S2 已打通隔离环境中的用户消息、受治理 Run、真实 ContextArtifact、Pi SQLite FULL、真实 HTTPS 文件交付与用户可读 receipt；使用 deterministic test provider，尚未完成 S2 全部可靠性验收与真实模型验证，也未部署。 详见 [S0 报告](docs/implementation/S0-report.md)、[S1 进展](docs/implementation/S1-progress.md)、[Artifact 进展](docs/implementation/artifact-governance-progress.md) 、[受治理创建进展](docs/implementation/governed-create-progress.md) 、[受治理修改进展](docs/implementation/governed-object-edit-progress.md) 、[Relation 进展](docs/implementation/governed-relation-link-progress.md) 和实时 [任务状态](planning/tasks.json)。

开发环境：Python 3.12、Node 24.13.0、pnpm 10.32.1、PostgreSQL 18。使用 `uv sync --frozen` 安装锁定 Python 依赖，`nvm use` 选择 Node。PostgreSQL 测试使用独立临时 PGDATA，不连接已有实例；非 Homebrew 环境用 `NEXLOOP_TEST_PG_BIN` 指定 PostgreSQL bin 目录。运行 `scripts/ci/check` 验证锁定依赖、实时契约、真实 PG、Pi 源码构建与 SQLite FULL；缺工具或关键测试被跳过会失败。

EIOS 受治理写入现已支持服务端绑定的 AGENT 身份及当前 release/application/父级 ceiling。实际受限 PG 测试验证创建、回执重用和撤权窗口；服务端签发的短时 Run 凭据已加入：绑定 Run/world/audience 与获准 Action，并在使用时复核撤权。正式 Host/Pi 已有受控 HTTPS Run admission、实际 SQLite FULL 恢复与 PG activation 引用；真实业务工具与消息交付已通过受治理 PG 集成测试；完整可靠性矩阵与 stage 装配仍待完成。见 [Agent 写入进展](docs/implementation/agent-governed-write-progress.md)。

可信 Python 应用服务可通过 `nexloop_eios.backend.open_backend` 组装现有服务：启动时验证受限 PG 角色、精确 migration head 和签名密钥，再打开私有 Artifact 目录。经认证的服务入口支持受治理创建、授权读取和 Artifact 操作；API/React 登录和 Node Agent Host 已有独立进程基础；同源登录已通过真实 PG/HTTPS 浏览器验收；Pi RuntimeAdapter 与受限 Worker 已接入原子入站、Run admission 和同 Run reclaim 恢复；独立常驻 Worker 已通过完整 head0040 CI 和实际 kill/reopen 恢复验证；业务 Action 对账已实现，stage 装配与全阶段验收仍待完成，见 [Runtime Worker 进展](docs/implementation/runtime-worker-progress.md)。用法见 [后端组装](docs/implementation/backend-composition-progress.md)，当前启动行为见 [密钥启动校验](docs/implementation/backend-startup-progress.md)。

已有隔离测试配置时，可用 `nexloop-doctor --mode test --database-url-file <私有DSN文件> --artifact-root <已有私有目录>` 只读检查底座。文件配置失败不会回退到其它数据库。`nexloop-artifact-smoke --mode test` 会实际写入并读回短期测试 Artifact；这些入口用于底座诊断；完整运营闭环仍需后续验收。见 [doctor 文件配置](docs/implementation/doctor-private-config-progress.md) 与 [smoke 用法](docs/implementation/artifact-smoke-progress.md)。

没有预先配置的测试数据库时，可运行 `uv run --frozen nexloop-core-sandbox --mode test --pg-bin <PostgreSQL bin目录>`。它创建自己的私有临时 PG，完成 bootstrap、测试身份配置和 Artifact smoke 后关闭并清理；不接受已有 DSN，也不调用真实模型。这是短期 core 诊断，运营闭环尚未完成。见 [sandbox 报告](docs/implementation/core-sandbox-progress.md)。

单机隔离测试 Compose 已实际验证 PG、Artifact、doctor、HTTPS 前端登录、内部 Host 鉴权、Valkey TLS/ACL 和独立受治理 Worker。每次启动使用新的目录与随机项目名，不复用已有数据库或卷：

```sh
nvm use
uv run --frozen python scripts/community_test.py prepare --output .ci-results/my-community-test
uv run --frozen python scripts/community_test.py run --output .ci-results/my-community-test
```

启动器返回当前 localhost HTTPS 地址。测试证书仅用于显式 test 模式，默认浏览器可能不信任；不能将它作为部署证书。真实模型凭据不会被读取。已有同名项目会被拒绝，失败也不会自动删除卷。实际证据和当前限制见 [Compose Worker 验证](docs/implementation/community-worker-runtime-progress.md)。产品 readiness 保持503，直到 S2 Run 与真实动作可靠性闭环通过。

冻结交接规范见 [docs/handoff](docs/handoff/00_START_HERE.md)。该目录是去除私有附件后的基线；planning 是实时副本；packages/contracts 是目标契约，不是上游现有 API。Pi npm 1.0.4 与冻结提交不一致，必须使用 vendor/pi 的冻结源码构建。PostgreSQL、Python、Valkey、Node 基础镜像已锁定 OCI index digest，并通过隔离容器实际构建和运行验证；应用镜像尚未发布，不能据此声称已部署。

Agent Host 的启动、内部鉴权和目录独占锁见 [Host 使用说明](apps/agent-host/README.md) 与 [真实进程验证](docs/implementation/agent-host-progress.md)。实际 Pi Run kill/reopen 与受限 Worker 恢复证据见 [Runtime Worker 进展](docs/implementation/runtime-dispatch-progress.md)；业务对账仍未通过，当前 readiness 为503。

模型固定为 DeepSeek deepseek-flash。负责人自行填写被 Git 忽略的 `.env` 中 MODEL_API_KEY。`.env.example` 全部为空；stage 使用 MODEL_CREDENTIALS_FILE 指向 secret。凭据不得进入源码、测试、日志或提交。缺少 key 时真实模型验证保持 blocked。

仓库为 PUBLIC；提交前运行 `uv run python scripts/check_publication.py`，暂存后再加 `--staged`。私有附件、原始交接包、运行数据和密钥不入库。许可证与第三方来源见 LICENSE、NOTICE、licenses/ 和 versions.lock.json。

PostgreSQL 持久 Inbox/Outbox 与任务队列已通过真实进程崩溃、去重、租约与 fence 测试；受限 Backend 已接入独立 Worker。Pi Run 的原子入站与受限 Worker 调度已接入；渠道消息和真实效应可靠性仍待实现。见 [队列进展](docs/implementation/durable-queue-progress.md)。

治理 Goal/PlanStep/EffectControl 登记、稳定 Intent/outbox、共享预算已通过真实 PostgreSQL/Backend 测试；外发 attempt 与 query-first 对账仍在实现，业务 readiness 保持503。见 [治理登记进展](docs/implementation/effect-context-progress.md)。

消息 Run 当前使用真实 ContextArtifact：Source 必须具备已发布 `nexloop.context.bind` Action 及 Artifact CREATE/READ 权限，队列保留完整规范化 pack；每次 model/tool 调用复核当前授权、Artifact 元数据、Run 租约与 fence。合成输入解析必须显式配置 `context_input_protocol=nexloop.context-pack.v1`，不猜测任意 JSON。恢复保留原 SQLite submission 与业务 Intent；见 [ContextArtifact 协议](docs/implementation/context-artifact-protocol.md)。这不代表每次模型请求都重读物理 Artifact 文件，也不代表 S3 ContextEngine 已完成。
