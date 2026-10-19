# NexLoop

<picture>
  <img src="apps/web/public/brand/nexloop-logo.png" alt="NexLoop" width="400">
</picture>

[English](README.md) | 简体中文

**产品介绍与交互演示：** <https://www.zingnex.com/nexloop/>

NexLoop 是面向企业的开源 **持续运营系统**。负责人设定长期与阶段目标（O / KR），Agent 在本体限定的受治理 Action 边界内，持续理解消费者、履行承诺、交付价值，并根据真实结果调整计划。

- 目标、本体、关系、记忆与承诺长期保存在 PostgreSQL；每次处理是一个短生命周期、事件驱动、有预算的 Run。
- 只有受治理的 EIOS Action 能修改正式业务对象或产生外部效应；结果不明时先查询对账，不盲目重发。
- 真实动作的可靠性优先于消费者数字孪生与模拟选优。
- **项目仍在探索阶段**：不保证提高收入或付费；接口与数据结构仍会变化。Agent 的自主范围由 Action 政策（受治理 Action、角色上限、联系限制、预算）控制，而不是由模型自行决定。
- 技术栈：Python 3.12（FastAPI、psycopg，vendored NEX-EIOS 内核）、Node 24 Agent Host（vendored Pi Durable）、React 19、PostgreSQL 18 + pgvector、Valkey 8.1。模型为 DeepSeek `deepseek-flash`，运行需要自带模型 key；没有 key 时只能使用显式的确定性测试模式（测试与演示，不能当作真实模型验收）。仓库中的示例与测试数据全部是合成数据。

## 当前状态

*截至 2026-10-19（main `5b7b5f57`）；数字取自 [`planning/`](planning)。*

- **当前阶段：S5 收尾。** S0–S4 已完成。S5 的六个任务（NX-032 至 NX-037）都在进行中，每个任务还差什么见 [S5 收尾差距清单](docs/implementation/s5-closeout-gaps.md)。
- **数字**：任务 41 done / 6 in_progress / 6 not_started（共 53）；验收 61 passed / 11 not_run（共 72）。11 项中 7 项属于 S5，4 项属于 S6/S7，不在 v0.1 范围内。
- **已部署到两机 stage**：数据主机运行 PostgreSQL 与 Valkey，应用主机运行 API、各 worker 与 Agent Host。每个发布都先经本地 CI，再签名、封存，然后才部署。**v0.1 的最终验收发布（发布 22，`47aa87b8`）已部署**，此后到 v0.1.0 只收修复。stage 上已经执行过 pgBackRest 备份和一次恢复演练。
- **受控真实试运行已完成（AT-071 passed）**：36/36 次受控交互，使用真实模型与一项真实交付动作，结果经 provider 查询对账；费用合计 1.59 元，上限 100 元；未授权发送、发错对象、拒收后再联系均为 0。顾客由负责人操作的 AI 代理扮演，证据中如实标注。
- **向量召回与候选合并已在 stage 启用**：召回索引使用 embedding 模型，每次调用都计入租户的模型预算；claim-matcher 按已发布的合并配置合并候选。
- **真实派发开关**：外部效应要三层都打开才会派发：部署总闸、租户、租户与 Action。stage 上部署层平时保持打开；租户层与 Action 层由负责人在工作台开关，试运行结束后已重新关闭。
- **演示**：[产品介绍与交互演示](https://www.zingnex.com/nexloop/)。
- **第二维护者演练已通过（AT-072 passed）**：负责人只按公开文档，完成了一次发布与一次恢复。
- **v0.1 还剩**：
  - stage 切换到本地 Git 队列与合并控制器（AT-053、AT-054，定于 2026-10-20）；
  - 带删除重放的第二次恢复演练（AT-050）；
  - 在最终验收发布上的核对（AT-055 至 AT-058）；
  - 部署前后的清点、公开发布预检、证据包，以及打 tag。

  目前还没有 tag，也没有公开发布。

负责人的规划活文档（阶段出口、关键路径、待决事项）见 [planning/ROADMAP.md](planning/ROADMAP.md)，任务与验收的权威状态在 [`planning/`](planning)。哪些功能已实现、哪些仍是规划，见 [架构说明](docs/ARCHITECTURE.md)（交接规格 [docs/handoff](docs/handoff/00_START_HERE.md) 是目标设计，不等于已实现）。每次合入的变化见 [CHANGELOG](CHANGELOG.md)，v0.1 已知风险见 [known-risks-v0.1](docs/release/known-risks-v0.1.md)。

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
| [docs/CONNECTORS.md](docs/CONNECTORS.md) | 连接器开发指南：入站渠道、效应 provider 接口、签名商业事件、回环与合成 provider |
| [docs/EVALUATION.md](docs/EVALUATION.md) | 评估运行指南：确定性回归、显式 opt-in 的真实模型验证、粘合校准、压测矩阵 |
| [GOVERNANCE.md](GOVERNANCE.md) | 维护者、决策方式、ADR 机制、如何提议变更 |
| [docs/release/public-release-checklist.md](docs/release/public-release-checklist.md) | 公开发布检查清单 |

## 贡献与安全

贡献方式与 DCO 签署见 [CONTRIBUTING.md](CONTRIBUTING.md)，漏洞报告见 [SECURITY.md](SECURITY.md)，行为准则见 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

仓库是公开的。不要提交 `.env`、密钥、口令、私网地址或运行数据；模型凭据只放在被 Git 忽略的 `.env` 或私有 secret 文件中。提交前运行 `uv run python scripts/check_publication.py --staged`，推送前运行 `scripts/ci/prepush-scan.sh`。

## 许可

自有代码使用 [MIT](LICENSE) 许可（[ADR-026](adr/ADR-026-license-mit.md)，取代交接规格 ADR-016 的 Apache-2.0 建议）；第三方来源与许可见 [NOTICE](NOTICE)、[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)、[licenses/](licenses) 与 [versions.lock.json](versions.lock.json)。
