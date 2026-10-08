# NexLoop｜开发交接文档包

版本：1.1 · 决策基线：2026-10-07 · 复核修订：2026-10-07 · 面向：产品负责人、Codex、开发者、维护者和本地运维。

**交付性质：完整产品与工程规格，不是已经实现或部署的软件。** 本包包含 PRD、设计、验收标准、开发任务和部署配置模板。没有连接或修改两台服务器，没有运行 NEX-EIOS/Pi Durable 的集成测试。GitHub 仓库 `cenbonew/NexLoop` 已存在且为 **PUBLIC**，目前只有一个空提交；交接内容尚未推送，推送前必须先排除私有附件。

## 项目定义

NexLoop 是面向企业的开源持续运营系统。负责人给出长期与阶段目标，Agent 在本体限定的 Action 边界内，持续理解消费者、履行承诺、交付价值，并根据真实结果调整计划。目标是探索可持续的客户关系与持续付费，而不是最大化消息数量或建立永不停止的聊天循环。

持续存在的是目标、业务对象、关系、记忆与承诺；运行实例按事件和时间启动，完成后释放。

## 已确认的边界

- 第一阶段先实现真实动作可靠执行；消费者数字孪生、模拟选优随后建设，不绑定行业。
- 第一版保存约 1,000 个消费者，Agent 并发以测试决定；角色与消费者允许 N:M。
- 使用 Pi Durable；NEX-EIOS 是项目内部可开源的本体与 Action 核心。
- EvoOntology 选择性复用语义访问、缺口诊断、演进与评估，不建立第二个业务事实库。
- 默认 PostgreSQL；本地双机部署。本地 CI 是发布依据，GitHub 用于代码备份、同步与社区协作。
- 用户已确认 NEX-EIOS 代码可以开源。无需重新讨论该授权决定；仍须清除密钥和真实数据、保留第三方许可。

## 阅读顺序

1. [PRD](docs/01_PRD.md)：产品目标、23 个逻辑模块、范围与验收。
2. [架构](docs/02_ARCHITECTURE.md)、[数据模型](docs/03_DOMAIN_DATA_MODEL.md)：实现边界和事实归属。
3. [NEX-EIOS 集成](docs/07_EIOS_INTEGRATION.md)、[Pi 与 Action 恢复](docs/06_ACTION_RUNTIME_AND_RECOVERY.md)：先解决兼容与授权，再扩展业务。
4. [部署](docs/11_DEPLOYMENT.md)、[本地 CI](docs/12_LOCAL_CI_AND_RELEASE.md)、私有双机附件（私有附件，公开副本不包含）。
5. [任务与里程碑](docs/15_BACKLOG_AND_MILESTONES.md)、[AGENTS.md](AGENTS.md)、[Codex 启动指令](CODEX_START_PROMPT.md)。
6. 私有附件（不入公开仓库）：双机映射（私有附件，公开副本不包含）、本机源码位置与开发机事实（私有附件，公开副本不包含）。

完整专项设计还包括对话到本体、上下文与规划、EvoOntology、API、工作台、安全与开源、测试、ADR、运维及来源记录。`contracts/` 为拟采用的 v1 机器契约；不是上游现有 API。

## 必须先处理的三项集成事实

NEX-EIOS 当前 README 说明 API Key 通道没有开放普通实例写入；PostgreSQL 生产装配仍涉及精确 Migration Catalog、Capability Pack 和 TOS；现有业务域包含网球专用组件。Codex 必须检查实际源代码，交付受治理的 Agent 写入路径、最小可独立启动的内核和本地 Artifact 适配，不能通过超级用户直写、伪造人类身份或静默 Memory 回退解决。[S01–S03]

Pi Durable 当前为实验性接口，公开持久化后端不包含 PostgreSQL。因此采用**业务数据全部 PostgreSQL，Pi 内部 Run 检查点暂用应用机本地 SQLite**的明确例外；单存储单进程持有，必须测试 FULL 同步配置和故障恢复。[S04]

## 文档与代码的状态标记

**已确认**：来自用户的项目约束。**设计决定**：本包选定的默认实现，可通过 ADR 改变。**待核验**：必须由实际代码/服务器检查解决，不表示重新问用户已经回答的问题。**验收目标**：要求测量，不是当前达成的性能。

所有配置模板均不包含密码、可用镜像 digest、服务器登录凭据或模型凭据。Codex 完成实现与预检后才能生成实际部署配置。`local-only/` 只用于私有运维交接，不能随整个目录直接推到公开仓库。

## v1.1 复核修订（2026-10-07）

- 新增 `local-only/LOCAL_SOURCES.md`：NEX-EIOS 冻结提交在本机的只读工作树路径、Pi/EvoOntology 克隆位置、目标仓库（远程为 PUBLIC）与开发机工具链事实。
- `docs/07` 新增 §2.1：对冻结提交的本地复核（Migration Catalog 实际到 `0332`、API Key 实例写限制原文、TOS/Playwright 硬依赖、tennis 专用入口）。
- 任务依赖：NX-009（单机 Compose）不再依赖 NX-004（双机盘点），S1/S2 不受服务器访问缺失阻塞。AT-015 调整为 S2，由 NX-016 承接。
- 契约：`ontology-mutation` 的 `epistemic_kind` 与 `docs/04` 对齐（9 类），新增 `business_intent_ref`/`risk_class`/`rationale_summary` 与 `invalidate_property`；所有 `date-time` 字段附带正则。`docs/08` 状态机与 `evolution-candidate` 对齐。
- 统一锁文件名为仓库根 `versions.lock.json`；统一 S0 产出目录 `docs/s0/`；`docs/02` 目标目录树补充 `planning/`、`docs/handoff/`、`docs/s0/`。
- 部署模板：后端容器获得 `MODEL_*`/`EMBEDDING_*`/`AGENT_HOST_INTERNAL_URL`；Agent Host 声明 `AGENT_HOST_PORT`；Caddy 跳转使用 `HTTPS_PORT`；`valkey.acl` 进入 secret 清单。
- 2026-10-07 晚：两台主机已 SSH 只读盘点（`local-only/SERVER_INVENTORY.md`）；ADR-006 明确 EIOS 为 vendored copy。
- 校验器：定义任务/验收状态词表与证据格式，允许进度更新；新增 `MANIFEST.json` 哈希校验与 `--write-manifest`；移除 `local-only` 之外的 `.73`/`140/73` 地址片段。
