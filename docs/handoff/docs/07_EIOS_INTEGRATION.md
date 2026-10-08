# NEX-EIOS 集成与开源内核抽取

## 1. 不变的决定

NEX-EIOS 是 NexLoop 的项目组成部分，以 **vendored copy** 方式进入：复制通用内核源码，不以 pip 包、git submodule 或运行时服务的形式依赖 NEX-EIOS 仓库（ADR-006）。用户已确认其代码可以开源。不是远程依赖现有网球生产服务，也不是重新发明一套本体/Action 系统。目标是复用已存在的通用能力，并使新项目独立安装、测试和发布。

核验起点：`cenbonew/NEX-EIOS`，读取的 main 提交为 `1e363db982daeca74058453ad12fc4a6cac333da`（2026-10-06）。此处是调查快照，不代表未经测试即可作为新项目生产锁定版。[S01–S03]

## 2. 已读取的信息与不可假定的能力

| 已读取事实 | 对 NexLoop 的意义 |
|---|---|
| Python ≥3.11、FastAPI、Pydantic、psycopg/psycopg-pool | 业务内核优先 Python，保留其授权与存储抽象 |
| Registry、Capability、Job/Attempt/Lease、Outbox、Artifact、本体对象/关系 | 优先复用，不另建重复执行治理 |
| API Key 的租户由服务端解析，拒绝客户端 X-Tenant-Id/X-Scopes 等身份头 | NexLoop 工具网关不得用可伪造 header 取得权限 |
| README 说明 API Key 仅开放受限 schema 注册，实例写暂未开放 | 必须核验/建设受治理的 autonomous service/agent 写入通路 |
| PostgreSQL 启动要求精确 Catalog/Pack 与 Artifact 后端；当前文档涉及 TOS | 社区版要有本地 Artifact 与独立最小装配，不能靠 Memory 回退 |
| 包中包含明显的 tennis/venue 专用入口 | 专业业务 pack 不默认加载，不泄露既有配置和数据 |

上述是文档与依赖信息，不是已完成源码级功能证明。README 中的 migration 描述可能落后于实际代码，**以目标提交的 Migration Catalog 和测试为准**，不把旧版本字符串硬编码进 NexLoop。

### 2.1 对冻结提交 `1e363db` 的本地复核（2026-10-07，只读）

| 复核项 | 实际观察 | 对 S0 的要求 |
|---|---|---|
| Migration Catalog | `src/eios/migrations/` 下 SQL 文件最高编号为 `0332_redrive_damping_anchor.sql`；README 仍写“精确 Revision `0233`” | README 已滞后约 100 个版本；S0 必须从 `eios.adapters.postgres.migrations` 的实际 catalog 逻辑读取 required revision，不引用 README 数字 |
| 实例写限制 | README 第 62 行：API Key 走 `0313` 平行许可链，仅允许三条 schema 注册；“实例写(object upsert、relation link 等)暂不对 API key 开放” | NX-012 的起点是 `0313` 链与 `0061` 浏览器会话链；受治理 service/agent 写入路径须在该许可链上扩展 |
| 硬依赖 | `pyproject.toml` 把 `tos==2.9.0`（火山云对象存储 SDK）、`playwright`、`mcp` 列为**必需**依赖 | 抽取 core 时必须把 TOS/Playwright/MCP 降为可选 extra；社区版不得因缺少 TOS SDK 无法安装 |
| 入口脚本 | `[project.scripts]` 有 50 余个入口，其中超过 20 个为 tennis/sports8/easyclub/yundong8/volc 专用 | 这些入口属于 domain pack，不进入 `packages/eios-core/`；core 只保留 api/migrate/worker/scheduler/access-admin 等通用入口 |
| 许可文件 | 仓库根目录无 `LICENSE`/`NOTICE` | 与 S03 一致；NX-005 需为抽取后的 core 补齐许可与来源记录 |
| 仓库可见性 | GitHub `cenbonew/NEX-EIOS` 为私有仓库 | Codex 在本机读取本地只读工作树（路径见 `local-only/LOCAL_SOURCES.md`），不要求从 GitHub 克隆 |

## 3. S0 输出：复用清单和差距清单

Codex 首先定位并列出：Ontology Schema/Object/Relation Store、Definition Registry、Action/Capability 注册、Policy 与 Permit、service/agent identity、Property 权限、租户上下文/GUC、UnitOfWork、Job lease/fencing、Outbox、Artifact、迁移装配、审计与测试入口。

每项输出 `source_path + commit + public_interface + transitive_dependencies + keep/adapt/exclude + tests + licensing`。对只能通过未确认私有接口访问的能力，标记待适配，不直接从 API 层穿透调用存储。

必须用真实 PostgreSQL 验证：认证读；创建/读取 Consumer；修改一个偏好；建立 Relation；拒绝无权写；撤权；业务幂等；错误 tenant/actor；属性级读取；两租户隔离；授权依赖缺失 fail closed。测试 fixture 不能使用 superuser DSN 代替应用角色。

## 4. 自主 Agent 授权路径

建立受治理 `NexLoopApplication` 和运行角色 ceiling。服务端产生/换取短时 run-bound 凭据，校验服务身份属于租户、应用发布状态、role、resource scope、world、audience、有效期、policy revision 与当前 resource grants。

只在授权测试通过后开放实例写入 Action。不存在现成服务路径时在 EIOS 内核扩展窄接口和许可链，保持相同 Policy/Permit/Action 规则；**不能退回直写数据库、超级 API Key、伪造 human Subject 或临时浏览器 Cookie**。

需要人类委托的 Action 仍要求合法 delegation。Agent 可自动执行授权范围内的业务，不代表系统内所有类型都自动对它开放。

## 5. 最小包策略

建议把通用代码导入 `packages/eios-core/`，保留来源文件注释与差异清单。固定原始提交，在私有开发工作区保存可核对源码；公开仓库只带可公开的必要源码和许可证，不把旧仓库全部历史直接镜像出去。

划分 core 与 domain packs：核心包括身份/权限、本体、受治理 Action、执行和存储；tennis、venue、专有连接器、生产巡检与环境配置留在可选 pack 或原项目，不进入 NexLoop 默认启动。

不能为抽取方便删掉一半迁移后继续声称使用原精确 Catalog。为抽取后的新空数据库建立**新的明确 bootstrap lineage**，逐个说明来源对象和依赖，验证与保留能力契约等价。旧 EIOS 数据库迁移到新内核不在 v0.1 范围；绝不在旧生产数据库执行新 lineage。

## 6. 数据库迁移规则

采用 EIOS 原生校验/SQL 迁移方式或兼容的单一 runner。`migrations/eios` 与 `migrations/nexloop` 有独立清单及依赖顺序，由统一迁移命令 orchestrate，但各自管理自己的 schema。已发布迁移 checksum 不得改写；新能力追加迁移。

禁止引入 Alembic 再管理同一批 EIOS 表。数据库管理员仅在专门 migration 作业中使用，不注入常驻 API、Worker 或 Agent Host。schema 版本和应用兼容范围写入 release manifest。

## 7. Local Artifact Adapter

首版必须实现本地 filesystem backend：保存 tenant/world/path/hash/size/media_type/encryption/retention 等元数据；数据路径由服务端生成，不信任模型或用户文件名。临时写入、hash 校验、原子 rename、孤儿文件回收、受权读取和 range/下载权限均需测试。

不得直接把 TOS URL 字段替换成本地路径然后绕过授权。用户下载应通过受权 API 或短时受限链接；真实源代码/对话 Artifact 不进入公共静态目录。API 与 Worker 使用同一应用机持久卷；跨机备份单独管理。

S3/TOS 可作为后续 adapter。生产模式不再强制只有 TOS 可用，但存储配置不正确仍必须阻止启动。

## 8. Identity 与工作台

优先复用 EIOS 原生身份会话和权限语义，保持 HttpOnly/Secure cookie、CSRF 和单会话当前租户。需要新的 BFF 时只作适配，不另创一套与 EIOS 不一致的用户权限。Node Agent Host 不访问登录数据库或身份管理 RPC。

消费者 Web Chat 使用受限 consumer session，只能读写自己的对话和允许权益；不得复用企业管理员 session 或共享全租户 key。

## 9. 交付门槛

抽取成功需要：干净 DB 可启动；无网球/原生产平台凭据；本地 Artifact 可用；无 Memory fallback；受限应用角色能完成必要授权 Action；全核心契约和负向安全测试通过。若缺失 service/agent 写入路径，在解决前不得把 UI 演示称为“可自主运营版本”。
