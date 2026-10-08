# 来源、事实基线与待核验项

核验日期：2026-10-07。来源用于确认上游能力和依赖边界；本包的部署架构、模块、指标、预算、API和任务编号是NexLoop设计，不是上游已提供的现成功能。

## 1. 用户确认

名称NexLoop；企业开源；真实运营先于模拟；NEX-EIOS代码可开源并是项目组成部分；Pi Durable方向；Evo选择性整合；约1,000消费者；稀疏事件、N:M角色和短Run；PostgreSQL默认；指定两机职责；本地CI、GitHub备份同步。以上来自当前会话，不需要重新征询。

## 2. 一手来源清单

| ID | 来源与本次确认内容 |
|---|---|
| S01 | [NEX-EIOS README](https://github.com/cenbonew/NEX-EIOS/blob/1e363db982daeca74058453ad12fc4a6cac333da/README.md)：本体/Action、身份、API Key实例写限制、生产装配说明。README可能滞后，实际行为需源码测试。 |
| S02 | [NEX-EIOS pyproject](https://github.com/cenbonew/NEX-EIOS/blob/1e363db982daeca74058453ad12fc4a6cac333da/pyproject.toml)：Python、FastAPI、Pydantic、psycopg及现有入口/依赖。 |
| S03 | [NEX-EIOS调查提交](https://github.com/cenbonew/NEX-EIOS/commit/1e363db982daeca74058453ad12fc4a6cac333da)：main快照；根LICENSE读取404，不等于没有用户授权，也不证明所有子组件许可。 |
| S04 | [Pi Durable README](https://github.com/earendil-works/pi/blob/adae8246453a2928a268ccecb9fb55125d96d0af/packages/durable/README.md)：实验性API、requestId、工具replay、SQLite/JSONL、单owner、conformance。 |
| S05 | [Pi LICENSE](https://github.com/earendil-works/pi/blob/adae8246453a2928a268ccecb9fb55125d96d0af/LICENSE)：MIT，保留来源声明。 |
| S06 | [EvoOntology架构](https://github.com/ruc-datalab/EvoOntology/blob/f64413dae88d88645b1f2c069cf4e17308ad0f89/docs/architecture.md)、[runtime](https://github.com/ruc-datalab/EvoOntology/blob/f64413dae88d88645b1f2c069cf4e17308ad0f89/evoontology/runtime/runtime.py)、[session](https://github.com/ruc-datalab/EvoOntology/blob/f64413dae88d88645b1f2c069cf4e17308ad0f89/evoontology/evolution/session.py)、[LICENSE](https://github.com/ruc-datalab/EvoOntology/blob/f64413dae88d88645b1f2c069cf4e17308ad0f89/LICENSE)：确定性核心/Skill分工、语义访问、候选评估与MIT。 |
| S07 | [PostgreSQL versioning](https://www.postgresql.org/support/versioning/)：本次页面列18.6为18系列维护版本。部署要验证镜像可得与扩展兼容。 |
| S08 | [Node releases](https://nodejs.org/en/about/previous-releases)：Node24为LTS，26当时为Current；选择24兼容线。 |
| S09 | [Valkey releases](https://valkey.io/download/releases/)和[项目首页](https://valkey.io/)：8.1.10为受支持旧分支更新，BSD开源。 |
| S10 | [pgvector README](https://github.com/pgvector/pgvector)：本次文档安装示例0.8.7；精确与ANN检索、维度/索引限制。 |
| S11 | [PostgreSQL18 Row Security](https://www.postgresql.org/docs/18/ddl-rowsecurity.html)：RLS/owner与FORCE语义，必须按实际角色测试。 |
| S12 | [PostgreSQL18 locking](https://www.postgresql.org/docs/18/explicit-locking.html)与[SELECT](https://www.postgresql.org/docs/18/sql-select.html)：队列领取与锁语义设计参考；SQL需实测。 |
| S13 | [Docker firewall](https://docs.docker.com/engine/network/packet-filtering-firewalls/)和[rootless](https://docs.docker.com/engine/security/rootless/)：端口暴露/防火墙与rootless权限边界。 |
| S14 | [Git hooks](https://git-scm.com/docs/githooks)：本地接收事件作为CI排队触发，不把hook当重型执行器。 |
| S15 | [pgBackRest User Guide](https://pgbackrest.org/user-guide.html)：备份、WAL与恢复能力；本包RPO/RTO为目标，未验证。 |
| S16 | [Codex AGENTS.md官方指南](https://developers.openai.com/codex/guides/agents-md/)：仓库工程约束放AGENTS.md，详细规范用引用避免过大。 |
| S17 | [Apache许可证说明](https://www.apache.org/licenses/)与[应用许可FAQ](https://www.apache.org/foundation/license-faq.html)：自有代码Apache-2.0建议与许可文件实践。 |
| S18 | [Caddy Automatic HTTPS](https://caddyserver.com/docs/automatic-https)：本地CA/证书信任需部署配置，不能当公网CA。 |

部分非核心操作手册链接作为进一步实现参考，不等于本次已逐条验证其全部内容。没有把某篇论文的 benchmark 提升外推为 NexLoop 付费增长。

## 3. 已读源码调查快照，不等于最终运行锁

| 项目 | 调查commit | 运行锁要求 |
|---|---|---|
| NEX-EIOS | `1e363db982daeca74058453ad12fc4a6cac333da` | 抽取后有独立core版本、来源清单与契约测试 |
| Pi | `adae8246453a2928a268ccecb9fb55125d96d0af` | 解析实际npm包及依赖/源码补丁、锁hash与恢复兼容 |
| EvoOntology | `f64413dae88d88645b1f2c069cf4e17308ad0f89` | 每个移植文件记录来源与修改，禁止双份漂移 |

PostgreSQL18.6、Valkey8.1.10、pgvector0.8.7是本次官方页面候选，不提供未经读取的镜像digest。Python3.12、Node24、React19等是选定兼容线，其实际补丁与包锁在S0安装/测试后固定。

## 4. 待核验项及处置

| 项目 | 当前状态 | Codex行动 | 未解决时允许继续什么 |
|---|---|---|---|
| 两机OS/CPU/RAM/磁盘/端口/SSH | 2026-10-07 已只读盘点（私有附件），地址已由路由器保留 | 复核并转公开版 | 本地文档/契约/源码/单元工作 |
| EIOS自主写实例路径 | README指出API Key受限 | 读实际权限链并做真实PG验证/受控扩展 | 不带真实效应的开发；禁止绕过 |
| EIOS最小catalog/domain依赖 | 未做完整代码抽取 | 生成依赖图、独立bootstrap及回归 | 标相关集成为blocked |
| 本地Artifact可用性 | 当前上游生产文档涉及TOS | 实现同契约filesystem adapter | 不声称社区版独立可运行 |
| Pi FULL同步配置与版本兼容 | 当前README默认NORMAL | 实际连接/补丁与conformance测试 | 合成单元；不声称掉电恢复保证 |
| 真实模型/embedding/渠道 | 模型已选定（私有附件记录 provider/model/base_url，key 在私有路径）；embedding 与外部渠道未定 | 凭据以 `_FILE` 注入并做受控 smoke；embedding 在 S3 前另定 | 显式deterministic test provider；检索先用 FTS |
| 真实商业付款数据源 | 未确认具体系统 | 通用签名事件adapter、受控联调 | 技术运营验证，不声称收入增长 |
| 独立离线/异地备份 | 仅两机规划 | 增设目的地并演练 | 明确仅试运行、非HA |
| 开源版权主体/安全联系人 | 未给具体发布信息 | 公开release前填LICENSE/SECURITY | 内部实现不阻塞，公开release前解决 |

## 5. 本包验收状态

生成文档和机器契约可做本地静态验证；实际服务器部署、模型调用、EIOS抽取、容器启动、端口/TLS和灾难恢复均尚未执行。最终静态检查报告位于 `planning/handoff-validation.json`，它只证明交接包内部一致性，不证明软件已经实现。
