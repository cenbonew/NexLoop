# NexLoop

[English](README.md) | 简体中文

NexLoop 是面向企业的开源 **持续运营系统**。负责人设定长期与阶段目标（O / KR），Agent 在本体限定的受治理 Action 边界内，持续理解消费者、履行承诺、交付价值，并根据真实结果调整计划。

- 目标、本体、关系、记忆与承诺长期保存在 PostgreSQL；每次处理是一个短生命周期、事件驱动、有预算的 Run。
- 只有受治理的 EIOS Action 能修改正式业务对象或产生外部效应；结果不明时先查询对账，不盲目重发。
- 真实动作的可靠性优先于消费者数字孪生与模拟选优。
- **项目仍在探索阶段**：不保证提高收入或付费；接口与数据结构仍会变化。Agent 的自主范围由 Action 政策（受治理 Action、角色上限、联系限制、预算）控制，而不是由模型自行决定。
- 技术栈：Python 3.12（FastAPI、psycopg，vendored NEX-EIOS 内核）、Node 24 Agent Host（vendored Pi Durable）、React 19、PostgreSQL 18 + pgvector、Valkey 8.1。模型为 DeepSeek `deepseek-flash`，运行需要自带模型 key；没有 key 时只能使用显式的确定性测试模式（测试与演示，不能当作真实模型验收）。仓库中的示例与测试数据全部是合成数据。

## 当前状态

- **S0–S3 已完成，S4 大部分完成**：对话→本体处理链与人工审核、目标与控制面、上下文引擎 v6、计划与复评、承诺、商业事件与费用、负责人工作台与人工接管都已合入 `main`。保留/删除/导出（NX-029）已完成；审计指标告警（NX-030）只差备份年龄；1,000 消费者并发矩阵（NX-031）进行中。
- **尚未部署**：S5 只完成了本地部分：本地 Git 队列与隔离 CI 控制器（NX-033，主机侧步骤未执行）、发布工具（NX-034：发布清单、签名验签、回滚检查）与备份恢复（NX-035：备份清单、恢复日志、恢复围栏、迁移门槛），都只在本机运行与演练；双机部署未开始（NX-032 的参数化部署模板、证书脚本与部署门槛检查已在仓库中，未连接任何服务器）；没有发布版本或可部署镜像；`/health/ready` 保持 503。
- **真实外发关闭**：真实外发渠道未开通，`REAL_DISPATCH_ENABLED` 保持关闭。

哪些功能已实现、哪些仍是规划，以 [架构说明](docs/ARCHITECTURE.md) 第 6 节为准（交接规格 [docs/handoff](docs/handoff/00_START_HERE.md) 是目标设计，不等于已实现）。任务与验收的实时状态见 [`planning/`](planning)，每次合入的变化见 [CHANGELOG](CHANGELOG.md)。

## 快速开始

需要 Python 3.12（[uv](https://docs.astral.sh/uv/)）、Node 24.13.0（`.nvmrc`）、pnpm 10、带 pgvector 的 PostgreSQL 18（测试只会创建一次性实例，不连接已有数据库；非默认位置用 `NEXLOOP_TEST_PG_BIN` 指定 bin 目录）。

```bash
nvm use
```

```bash
uv sync --frozen && pnpm install --frozen-lockfile
```

运行一个定向测试：

```bash
LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=packages/eios-core/src:tests uv run --frozen pytest -q tests/test_bootstrap.py
```

在本机起一次性的工作台冒烟环境（仅开发机；合成数据、回环地址、退出时自动清理）：

```bash
scripts/dev/workbench_smoke.sh
```

完整本地 CI（耗时数十分钟）。本地 CI 是发布依据，GitHub Actions 不是必需的：

```bash
bash scripts/ci/check
```

更多内容（社区测试 Compose、测试约定、贡献须知）见 [架构说明 §5](docs/ARCHITECTURE.md#5-开发者上手)。

## 文档导航

| 文档 | 内容 |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 按当前实现撰写的业务架构与技术架构、模块索引、开发者上手、已知限制 |
| [docs/handoff/](docs/handoff/00_START_HERE.md) | 冻结的交接规格（PRD、目标架构、数据模型、部署、验收等），是目标设计，不等于已实现 |
| [adr/](adr) | ADR-019 起的架构决策（ADR-001～018 见 [16_ADR](docs/handoff/docs/16_ADR.md)） |
| [docs/implementation/](docs/implementation) | 按任务的设计稿、实现说明与证据 |
| [CHANGELOG.md](CHANGELOG.md) | 每次合入 `main` 的用户可感知变化 |
| [AGENTS.md](AGENTS.md) | 工程契约 |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | 运维手册：部署、doctor、告警、备份恢复、发布与回滚的入口 |
| [GOVERNANCE.md](GOVERNANCE.md) | 维护者、决策方式、ADR 机制、如何提议变更 |
| [docs/release/public-release-checklist.md](docs/release/public-release-checklist.md) | 公开发布检查清单 |

## 贡献与安全

贡献方式与 DCO 签署见 [CONTRIBUTING.md](CONTRIBUTING.md)，漏洞报告见 [SECURITY.md](SECURITY.md)，行为准则见 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

仓库是公开的。不要提交 `.env`、密钥、口令、私网地址或运行数据；模型凭据只放在被 Git 忽略的 `.env` 或私有 secret 文件中。提交前运行 `uv run python scripts/check_publication.py --staged`，推送前运行 `scripts/ci/prepush-scan.sh`。

## 许可

自有代码使用 [MIT](LICENSE) 许可（[ADR-026](adr/ADR-026-license-mit.md)，取代交接规格 ADR-016 的 Apache-2.0 建议）；第三方来源与许可见 [NOTICE](NOTICE)、[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)、[licenses/](licenses) 与 [versions.lock.json](versions.lock.json)。
