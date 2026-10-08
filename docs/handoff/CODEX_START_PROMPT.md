# 给 Codex 的首次开发指令（v1.1）

以下横线之间的文本可直接粘贴给 Codex。它假定 Codex 在开发机上、工作目录为 `/Users/chenbowen/253383134/NexLoop`，交接包已解压或复制到该仓库内 `docs/tmp/` 下。

---

请基于本交接包实施 NexLoop。工作目录是 `/Users/chenbowen/253383134/NexLoop`（git 仓库，远程 `origin` 指向 GitHub 上的 **PUBLIC** 仓库 `cenbonew/NexLoop`）。交接包位于 `docs/tmp/NexLoop_Codex_交接包_v1.0/NexLoop-handoff-v1.0/`；若只拿到 zip，先解压到同一位置。

**第 0 步：防泄露，再做其它一切。** 在任何 `git add` 之前：用 `deploy/examples/PUBLICATION_EXCLUSIONS.example` 生成仓库根 `.gitignore`，并追加 `docs/tmp/`；把交接包中除 `local-only/` 和 `NexLoop_完整交接文档_*` 之外的内容复制到 `docs/handoff/`；用 `deploy/examples/env.example` 生成仓库根 `.env.example`（全部留空）；把 `planning/` 复制到仓库根作为实时副本；`contracts/` 复制到 `packages/contracts/`。首次提交前执行 `git status` 和 `grep -rn "192.168" --exclude-dir=.git --exclude-dir=docs/tmp .`，确认无私网地址进入暂存区。禁止 `git add -A`/`git add .` 直到 `.gitignore` 生效；禁止 `push --mirror`。

**阅读顺序：** `AGENTS.md`、`00_START_HERE.md`、`local-only/LOCAL_SOURCES.md`（本机源码路径与开发机事实）、`docs/01_PRD.md`、`docs/02_ARCHITECTURE.md`、`docs/07_EIOS_INTEGRATION.md`（含 §2.1 对冻结提交的复核）、`docs/06`、`docs/14_TEST_ACCEPTANCE.md`、`docs/15`、`planning/tasks.json`。这些文档是开发依据；`contracts/` 是 NexLoop 的目标契约，不是上游已有 API。

**既定方向不再重新选型：** 企业级开源持续运营系统；真实运营与执行可靠性优先，数字孪生与模拟后置；Pi Durable 作为首版 Harness（经 RuntimeAdapter 封装）；NEX-EIOS 作为项目内可开源的本体与 Action 内核；EvoOntology 选择性复用；业务数据库 PostgreSQL；本地 CI 是发布依据，GitHub 只做代码同步。双机职责见 `local-only/LAN_PLAN.md`。用户已确认 NEX-EIOS 可以开源，不要再问；但必须清理真实数据与密钥、保留第三方许可、不修改原生产系统。

**上游源码：** NEX-EIOS 冻结提交 `1e363db982daeca74058453ad12fc4a6cac333da` 已在本机只读工作树 `/Users/chenbowen/253383134/NEX-EIOS-release-rda-1e363db`，S0 一律以它为准；`/Users/chenbowen/253383134/NEX-EIOS` 主克隆落后且有未提交改动，不要使用；其它 `NEX-EIOS-*` 目录是原生产产物，不要读取。Pi 与 EvoOntology 按 `LOCAL_SOURCES.md` 克隆到 `/Users/chenbowen/253383134/upstream/` 并 checkout 冻结提交。

**S0（NX-001～NX-005）必须交付的文件：**
- `docs/s0/source-reuse-inventory.md` + `.json`：按 `docs/07` §3 的字段逐项列出 EIOS 可复用能力（source_path、commit、public_interface、transitive_dependencies、keep/adapt/exclude、tests、licensing）。
- `docs/s0/eios-write-path-audit.md`：实际读取 `0061`/`0313` 许可链代码，说明 API Key/服务主体当前能做什么、不能做什么，以及 NX-012 的扩展点。
- `docs/s0/migration-catalog-audit.md`：从 `eios.adapters.postgres.migrations` 的实际逻辑读出 required revision（已知 SQL 文件到 `0332`，README 的 `0233` 已过期），列出最小 bootstrap 依赖图与必须排除的 tennis/sports8/TOS 迁移。
- `versions.lock.json`：`upstream_sources`（三个 commit）、`packages`（Pi npm 包与冻结提交的对应关系必须验证，不能只写 1.0.4）、`images`（digest 未解析前留空并标注）。
- `docs/s0/dev-machine-inventory.md`（本机）与 `docs/s0/server-inventory.md`：两台主机已可用 `ssh hengce`（DATA_HOST）/ `ssh sice`（APP_HOST）免密登录，只读盘点已在 `local-only/SERVER_INVENTORY.md`，NX-004 只需复核、补 sudo 项并转成公开版（去 IP、去指纹）。注意其 §8：Wi-Fi 链路、Docker Hub 需镜像站、Docker 为 rootful；IP 已由路由器保留，两台 sudo 均已 NOPASSWD。
- `planning/tasks.json` 状态更新与证据；每次更新后运行 `uv run --with jsonschema --with pyyaml --with rfc3339-validator python docs/handoff/tools/validate_handoff.py --planning planning`。

**S1/S2 的最薄纵向切片：** 按 `tasks.json` 依赖顺序推进 NX-006 → NX-008 → NX-007 → NX-010 → NX-009 → NX-011 → NX-012 → NX-013 → NX-014 → NX-015 → NX-016 → NX-017。“可运行闭环”的定义是：干净 PostgreSQL 上抽取后的 EIOS core 能 bootstrap；本地 Artifact 可用且无 Memory 回退；一个受治理 service/agent 主体能经 EIOS 授权链完成实例写与一种真实效应 Action；Pi Run 在本地 SQLite（FULL 同步）持久化并能 kill/reopen 恢复；AT-013/030/031/033/034/052 等 S2 用例有真实 PG 证据。不要一次生成 23 个空模块，不要只输出计划。

**模型 Provider：** 负责人已选 DeepSeek `deepseek-flash`（OpenAI 兼容，`https://api.deepseek.com`），细节见 `LOCAL_SOURCES.md` §5。凭据按开源惯例处理：仓库根 `.env`（已被 `.gitignore` 忽略）放 `MODEL_API_KEY=`，`.env.example` 入库且留空（内容以交接包 `deploy/examples/env.example` 为基础）；stage 改用 compose secret 文件 `MODEL_CREDENTIALS_FILE`，文件非空时优先于环境变量。负责人会自己往 `.env` 填 key；key 为空时用 deterministic test provider 并把真实模型验证标 blocked。key 不得写进代码、日志、fixture、prompt 或任何提交。DeepSeek 没有 embedding 接口，S3 前 embedding provider 另定，先用 PostgreSQL FTS + pg_trgm。

**开发机环境事实：** macOS arm64；Homebrew PostgreSQL 18.4 与 pgvector 0.8.5 已装（集成测试起独立 PGDATA 与端口，不碰已有实例）；Docker Desktop 已装但 daemon 可能未运行；Node 默认 v20，需 `nvm install 24 && nvm use 24` 并提交 `.nvmrc`；用 `uv python pin 3.12`，避免 PATH 上的 miniconda 3.13；pnpm 10 可用。Codex 本机配置为无审批、全权限沙箱，所以 AGENTS.md 的护栏完全靠你自觉：破坏性命令先 dry-run。

NEX-EIOS 以 vendored copy 进入 NexLoop（ADR-006）：不把 nex-eios 当 pip 依赖或 submodule，不导入其 git 历史。

**硬约束：** 不绕过 EIOS 授权直接写业务数据（不用超级用户 DSN、不伪造人类身份、不直写 SQL）；不修改原生产系统或其数据库；未经验证备份不执行持久环境迁移；私有附件不公开推送；缺少渠道凭据或 embedding provider 时只阻塞相关验证，继续完成可独立开展的代码与测试，不编造部署或调用成功。

**每个阶段结束时报告：** 实际改动文件、任务/验收 ID、真实执行的命令、测试结果（含首次失败与重跑）、`versions.lock.json` 变化、仍然 blocked 的项目及所需输入。用中文汇报，标识符与 API 名保留英文。

现在从第 0 步开始执行。

---

这是一次实施任务的输入文本，不代表当前文档包已包含应用源码、服务器访问凭据或已执行部署。
