# 架构设计

## 1. 决策摘要

采用**模块化单体业务内核＋独立 TypeScript Harness 进程＋PostgreSQL 持久任务与数据**。NEX-EIOS 以项目内部受控包集成，不把其正在运行的原业务系统当作必须依赖的远程服务。API、Worker、Scheduler 可以使用同一 Python 包和镜像、不同受限入口启动；模块数量不决定进程数量。

业务与事件为主，模型运行时是可替换执行器。默认不引入 Kafka、Temporal、Kubernetes、Neo4j、独立向量数据库或复杂服务发现。

## 2. 默认技术栈（设计决定）

| 层 | 选择 | 使用边界 |
|---|---|---|
| 业务 API/Worker | Python 3.12、FastAPI、Pydantic v2、psycopg 3 | 优先复用 EIOS 原生数据库与授权抽象，不同时再引入另一套 ORM/迁移管理器 |
| Agent Host | Node.js 24 LTS、TypeScript、Pi Durable | 仅执行短生命周期 Run；无生产 Shell/SQL/渠道凭据 |
| 前端 | React 19、TypeScript、Vite、TanStack Query、Radix/shadcn 风格组件 | SPA 工作台与消费者 Web Chat；无 SEO/SSR 需求，不默认 Next.js |
| 样式与测试 | CSS tokens、可访问组件、Vitest、Playwright | 页面状态、响应式、权限与功能 E2E |
| 主数据库 | PostgreSQL 18，初始解析候选 18.6 | 全部业务权威、事件、任务、证据元数据和指标 |
| 检索 | pgvector 0.8 系列、PostgreSQL FTS＋pg_trgm | 同库混合检索；先精确召回基线，再按测试考虑 ANN |
| 缓存 | Valkey 8.1，初始解析候选 8.1.10 | 可丢失缓存、加速通知；不是任务/授权/幂等权威 |
| Artifact | 本地文件系统适配，后续可选 S3/TOS | 文件与 DB 元数据、租户绑定、hash 和 retention 一致；不得强制专有云 |
| 入口 | Caddy 2 | TLS、同源路由、静态资源；内部接口不对浏览器发布 |
| 打包部署 | Docker Compose v2 | 两主机各一份 compose，不假装共享跨机 Docker 网络 |
| CI | 本地 Git bare＋systemd 调度＋隔离构建脚本 | 不依赖 GitHub Actions 控制面；社区可直接执行相同脚本 |
| 包与锁 | Python uv.lock；JS pnpm-lock.yaml | 解析版本、哈希及镜像 digest 固定；不是部署时追 latest |

Node 24 的 LTS 状态、PostgreSQL 当前维护版本、Valkey 维护分支及 pgvector 功能已核对官方来源；具体镜像与各 JS/Python 库锁文件必须由 S0 安装验证后生成。[S07–S10] 本表不宣称所有组合已联调。

## 3. 进程图

```text
消费者 / 运营负责人
        │ HTTPS
        ▼
Gateway ──► Web SPA
        └─► NexLoop API（Identity、业务模块、EIOS Core）
                         │                    │
                         │ 事务/Outbox         └─► 受治理 Action / 执行账本
                         ▼
               PostgreSQL 持久事件与任务
                         │
              Scheduler / Domain Worker
                         │ RunCommand（内部鉴权）
                         ▼
                TypeScript Agent Host
               Pi Durable + RuntimeAdapter
                         │
        ┌────────────────┴─────────────────┐
        ▼                                  ▼
模型 Provider                     受限 NexLoop Tool Gateway
                                           │
                              Context / Memory / EIOS Action

Evo 演进工作以后台任务运行：只读轨迹→候选→评估→受控发布。
```

## 4. 四个权威边界

**业务事实权威。** EIOS 管理正式消费者、业务对象、关系和 Action；NexLoop 管理自己的目标、计划、交互与上下文记录。通过本体暴露一个业务模块对象时，采用映射/只读视图，不复制出第二个可独立写入的事实。

**授权权威。** 身份和权限在服务端解析，运行凭据绑定 tenant、actor、role、run、world、audience、有效期和 ceilings；每次写入重验当前授权。模型返回的授权字段从不被信任。

**任务权威。** PostgreSQL 管理业务唤醒与 Run 排程。EIOS 管理已受理 Action 的尝试、结果与恢复。Pi 只管理该 Run 内部推理/工具任务。三者使用不同 ID 和职责，不能同时争抢同一个执行阶段。

**知识权威。** 原始证据、消费者陈述、推测与正式状态分开。Evo 语义视图和向量库都是派生内容。

## 5. 包边界与目录目标

```text
nexloop/
  AGENTS.md                    # 工程约束（来自交接包，随实现更新）
  versions.lock.json           # 上游来源、依赖与镜像 digest 的单一锁文件
  .nvmrc  .python-version      # Node 24 / Python 3.12
  planning/                    # tasks.json、acceptance-tests.json 的实时副本（状态与证据在此更新）
  docs/handoff/                # 交接包公开部分的冻结副本（不含 local-only 与合并版文档）
  docs/s0/                     # S0 产出：source-reuse-inventory、eios-write-path-audit、migration-catalog-audit、dev-machine-inventory、server-inventory
  apps/web/                    # 工作台、消费者入口
  apps/agent-host/             # Pi Adapter、Run 服务、Provider
  packages/nexloop/            # Python 业务模块、API、Worker、Scheduler
    identity/ goals/ consumers/ conversations/
    knowledge/ context/ planning/ operations/ metrics/
  packages/eios-core/          # 从确认源码抽取的通用本体/授权/Action内核
  packages/evo-semantic/       # 选择性移植；保留源头与修改清单
  packages/contracts/         # JSON Schema（由 docs/handoff/contracts 复制后成为唯一活动副本）、生成 TS/Pydantic 类型
  migrations/eios/             # 通用内核专属、明确版本目录
  migrations/nexloop/          # 新业务专属 SQL 迁移目录
  connectors/                 # WebChat、签名事件入口、可选外部渠道
  evals/ fixtures/             # 合成数据、回归、影子与模拟
  deploy/                     # 通用 compose 与配置生成
  scripts/ci/ scripts/ops/     # 平台无关校验与部署脚本
  docs/ adr/ licenses/         # 中英说明、设计、来源与许可
```

上面是**目标代码目录**，交接包本身没有应用代码。Codex 应先生成最薄可运行纵向切片，不创建大量空目录伪装进度。

## 6. 依赖规则

API 只调用 application service，不把业务规则写入路由。业务模块依赖 EIOS 的公开契约，不能导入其私有 PostgreSQL adapter 直接 DML。Agent Host 依赖 contracts 和受限 HTTP 工具网关，不持有数据库 DSN。Context Engine 可以读取业务投影和语义视图，不负责修改授权。Evo 模块不能反向控制 Model Gateway、发布脚本或数据库管理员权限。

跨模块调用同进程时使用共享 UnitOfWork 抽象，保持一个受治理业务写入及其 outbox 的事务。无法共用事务的系统以确定的消息和业务幂等键协调，不能声称分布式原子提交。

## 7. 实时与后台分层

入站事件先持久化；关键拒绝联系等信息进入保护性检查，随后即时 Run 使用原始当前输入响应。完整抽取、记忆整理、索引和语义演进在后台。关键保护状态未能确认时限制相关触达，而不是让后台提取延迟变成可忽略拒绝的窗口。

对同一消费者的会话事件保留顺序号；控制状态有独立 revision。不要在整个 LLM 推理期间持有数据库行锁。Action dispatch 时短事务重验最新状态。

## 8. Runtime 存储例外

当前 Pi 公布 SQLite、JSONL 等存储且要求单存储单进程；本包不虚构 PostgreSQL 原生适配。[S04] 首版业务数据默认 PostgreSQL，Pi 的内部检查点使用 APP_HOST 本地 SQLite，首版采用一个 Run 一个独立文件，PG 保存受控 store_ref 和 run_id 映射。仅已验证 UUID 映射到服务端路径，不接受模型传文件路径。

一个 Host 进程持有已打开存储，终态后关闭。共享 owner 进程锁＋Run lease 阻止重复持有；恢复不在网络文件系统上直接打开 SQLite。必须验证同步级别 FULL；若上游 API 无配置接口，使用审计过的小补丁/adapter，并运行其 conformance 测试。不得通过在另外连接上执行 PRAGMA 假装修改已开的连接。

后续 PostgreSQL Storage adapter 是独立里程碑（尚无任务编号；需先以 ADR 立项并加入 `planning/tasks.json`），须通过完整存储一致性测试后替换。Pi 的文件状态和 PostgreSQL 不是共同事务；对业务生效状态始终以 EIOS 账本核对。

## 9. 开源可运行性

社区路径必须在干净 Linux 环境、一个 PostgreSQL/Valkey 和本地 Artifact 上启动。不能要求访问原 EIOS 的生产机器、TOS bucket、网球平台、私有域名或本地部署 IP。真实模型由用户自带凭据，离线 deterministic provider 只用于测试和演示，不冒充真实 AI。
