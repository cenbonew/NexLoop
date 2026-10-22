# 项目治理

本文说明 NexLoop 由谁维护、如何做决定、如何提议变更。项目仍在探索阶段（v0.1 之前），治理方式会随参与者增加而调整；调整本身也按下文的 ADR 机制记录。

## 1. 角色

| 角色 | 职责 |
|---|---|
| **负责人（owner）** | 项目唯一的最终决策人：产品范围、架构决策（ADR）、许可证、安全与隐私政策、公开发布，以及所有 B 类操作（见第 4 节）。 |
| **维护者（maintainer）** | 由负责人指定。审阅变更、把变更集成进 `main`、维护本地 CI 与发布工具；不能替负责人做需要负责人决定的事。当前维护工作由负责人和负责人授权的集成流程承担。 |
| **贡献者（contributor）** | 任何按 [CONTRIBUTING.md](CONTRIBUTING.md) 提交 issue 或代码的人。 |

目前没有理事会或投票机制。负责人可以把某类决定授权给维护者，授权须写进 ADR 或本文件。

## 2. 变更如何进入 `main`

项目采用“单负责人 + 集成（调度）流程”：

1. 每项工作对应 [`planning/tasks.json`](planning/tasks.json) 中的一个任务。实现在独立分支上完成，附带测试、可复现的证据和实现说明（`docs/implementation/`）。
2. 集成者审阅分支：确认改动只在该任务范围内、事实有据可查、没有私有信息，并核对它与交接规格和 ADR 是否一致。
3. 变更在**本地 CI** 上通过之后才能合入。本地 CI（[`scripts/ci/check`](scripts/ci/check)，以及 NX-033 的本地 Git 队列与受信判定）是发布依据，GitHub Actions 不是必需的（ADR-014）。
4. `main` 只能快进合并，不得 force push。每次合入都在 [CHANGELOG.md](CHANGELOG.md) 记录用户可感知的变化，任务状态与证据在 `planning/` 中更新。
5. 推送到 GitHub 之前，必须通过 `scripts/ci/prepush-scan.sh`（检查私有路径、受限标识、凭据值，以及锁定版本的 gitleaks 扫描；`--full` 可扫描全部历史）。

任务状态（`done` / `passed` 等）只能依据证据判定：占位实现、被跳过的关键测试、没有执行过的命令都不算完成。

## 3. 架构决策（ADR）

- ADR-001～018 是交接规格中的初始决策，见 [docs/handoff/docs/16_ADR.md](docs/handoff/docs/16_ADR.md)；ADR-019 起的新决策在 [`adr/`](adr) 目录，每份一个文件。
- 一份 ADR 写明背景、决定、理由、后果与验收方式，并注明负责人的确认（日期与原话或转述）。状态为“采用”的 ADR 才对实现有约束力。
- **不改写历史 ADR**：决定改变时新增一份 ADR，说明替代了哪一份以及原因（例如 [ADR-026](adr/ADR-026-license-mit.md) 取代了交接规格 ADR-016 的许可证建议）。冻结的 `docs/handoff/` 不随实现修改。
- 以下事项必须先有 ADR 才能实施（来自 16_ADR“需要独立 ADR 才能改变的事项”）：替换 Harness、直接写 EIOS 底表、双事实库、共享 SQLite 多进程、取消 RLS 或 Action 授权、让模拟触达真实客户、依赖 GitHub 在线 CI、修改已发布迁移、引入专有必需服务。
- 普通实现细节（经过回归的参数、UI 微调、同一兼容线内的依赖补丁升级等）不需要 ADR，按普通变更流程处理。

## 4. 需要负责人本人确认的操作（B 类）

以下操作不能由维护者、自动化或 Agent 自行执行，每一次都需要负责人本人确认：

- 连接或修改部署主机（APP_HOST、DATA_HOST）、改防火墙、建服务用户、切换 Git remote；
- 迁移任何持久数据库（迁移门槛要求先有已校验的备份，没有豁免）；
- 开通真实外发渠道、`REAL_DISPATCH_ENABLED`、真实模型或渠道凭据；
- 创建 tag、GitHub Release 或其他公开发布（见 [公开发布检查清单](docs/release/public-release-checklist.md)）；
- 从备份恢复后开放真实派发（`nexloop.restore.open_scope`）。

## 5. 贡献者如何提议变更

- **小改动**（文档修正、明显的缺陷修复）：直接提交 Pull Request，附带测试或复现说明。
- **较大的改动**：先开 issue 说明问题、动机和方案；涉及契约（`packages/contracts/*.schema.json`）、数据库迁移、部署配置、授权或安全边界的，要先讨论，必要时由维护者整理成 ADR 草案，交负责人决定。
- 所有提交必须带 DCO 签署（`Signed-off-by:`，`git commit -s`），见 [CONTRIBUTING.md](CONTRIBUTING.md)。
- 外部 Pull Request 在维护者审阅之前，不会在任何能访问内网、服务凭据、真实模型或渠道密钥的环境中运行。审阅通过后，变更才进入本地 CI。
- 只用合成数据；不要提交真实客户数据、凭据、私网地址、主机名或本机路径。
- 安全问题不要发公开 issue，按 [SECURITY.md](SECURITY.md) 报告。

## 6. 行为准则

参与本项目即同意遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

## 7. 许可证

- NexLoop 自有代码采用 [MIT](LICENSE) 许可，由 [ADR-026](adr/ADR-026-license-mit.md) 确认（负责人 2026-10-11）。
- 贡献通过 DCO 签署，按 MIT 许可提交；不使用 CLA。
- 第三方代码（vendored NEX-EIOS 内核、Pi 冻结源码等）保持各自的原许可证，来源与许可文本见 [NOTICE](NOTICE)、[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)、[`licenses/`](licenses) 与 [versions.lock.json](versions.lock.json)；依赖的许可策略在 `deploy/compliance/license-policy.v1.json`，由 `scripts/check_licenses.py` 检查。

## 8. 本文件的修改

修改本文件需要负责人确认；涉及角色、决策权或许可证的修改须同时新增 ADR。
