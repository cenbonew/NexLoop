# NexLoop — Codex 开发交接包 v1.1

这是产品与工程交接规格，**不是已经实现的NexLoop应用**。名称、双机职责、PostgreSQL、本地CI、EIOS/Pi/Evo整合与研发顺序已按本次决策整理。

开始阅读：[入口](00_START_HERE.md) → [完整PRD](docs/01_PRD.md) → [架构](docs/02_ARCHITECTURE.md) → [Codex指令](CODEX_START_PROMPT.md)。

开发资产：23个逻辑模块；43项有依赖任务；60个验收场景；6份JSON Schema与合成示例；双机部署模板；18份专项文档。任务/验收均尚未执行，不以文件存在作为交付完成。

机器清单：[任务](planning/tasks.json)、[验收](planning/acceptance-tests.json)、[追溯矩阵](planning/traceability.json)、[静态检查报告](planning/handoff-validation.json)（由 `tools/validate_handoff.py` 生成，见 [tools/README.md](tools/README.md)）。

部署：[通用方案](docs/11_DEPLOYMENT.md)、[配置模板](deploy/README.md)、[本地CI](docs/12_LOCAL_CI_AND_RELEASE.md)、私有LAN附件（私有附件，公开副本不包含）、本机源码位置（私有附件，公开副本不包含）。本包含私有运维附件，不要直接将整个交接包推送公开GitHub；目标仓库 `cenbonew/NexLoop` 的远程是 PUBLIC。

`AGENTS.md`可以作为目标实现仓库的工程约束起点；`CODEX_START_PROMPT.md`是首次开发输入。目标代码目录、命令、API与镜像需由Codex真正实现并测试。`tools/validate_handoff.py`只检查此交接基线，不验证软件已部署或已通过业务验收。
