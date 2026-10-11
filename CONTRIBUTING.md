# 参与贡献 NexLoop

感谢参与。NexLoop 仍在探索阶段，接口与数据结构会变化；提交前请先读 `AGENTS.md`（工程约定）与 `docs/handoff/docs/16_ADR.md`、`adr/` 中的架构决定。

## 开发环境与本地 CI

- Python 用 `uv`（`uv sync --frozen`），Node 24 与 `pnpm`（`pnpm install --frozen-lockfile`）。
- `scripts/ci/check` 是权威 CI，在本地运行；GitHub 只是代码同步与社区入口，不是必需的 CI。
- 只用合成数据。真实客户数据、日志、备份、私钥、模型或渠道密钥，以及私网地址、主机名、本机路径都不得出现在代码、fixture、文档或提交中。`scripts/check_publication.py` 和 `scripts/ci/prepush-scan.sh` 会拒绝这类内容。

## 提交要求

- 每个改动都附带测试和可以复现的证据；占位实现、被跳过的关键测试、没有执行过的命令不算完成。
- 提交说明格式：`NX-0xx: <任务标题>`（与任务无关的小修可用简短的祈使句）。
- 改动契约（`packages/contracts/*.schema.json`）、迁移或部署配置时，先开 issue 或 ADR 讨论。
- 依赖变化须同时更新锁文件，并通过 `scripts/check_licenses.py --check`；许可策略在 `deploy/compliance/license-policy.v1.json`，弱 copyleft 依赖只能按包逐个列入。`THIRD_PARTY_NOTICES.md` 由 `scripts/check_licenses.py --write-notices` 生成，不手工编辑。

## 贡献签署：Developer Certificate of Origin

本项目使用 [Developer Certificate of Origin 1.1](https://developercertificate.org/)（DCO），不使用 CLA。每个提交都要带一行与作者姓名和邮箱一致的签署：

```
Signed-off-by: 你的名字 <你的邮箱>
```

`git commit -s` 会自动添加。签署表示你确认有权按本项目的 MIT 许可提交这段代码。维护者合入外部改动前运行：

```bash
python3 scripts/check_signoff.py <base> <head>
```

区间内任何非合并提交缺少与作者一致的 `Signed-off-by` 都会失败。

## 外部 Pull Request 的运行规则

- 未经维护者审阅的外部代码，不在任何能访问内网、数据主机端口、服务凭据、真实模型或渠道密钥的环境里运行。
- 仓库不设置会在 `pull_request_target` 或带秘密的上下文里运行外部 PR 的工作流。`scripts/check_ci_isolation.py`（本地 CI 的一部分）检查 `.github/` 下的工作流：禁止 `pull_request_target`，禁止引用 `secrets`，权限只能是只读。
- 维护者先审阅差异；确需运行时，在一次性的隔离环境中运行（无内网、无秘密、无容器宿主 socket）。合入 main 之后，改动才进入 CI 主机的本地 CI。

## 安全问题

请不要在公开 issue 中报告安全漏洞，报告方式见 `SECURITY.md`。

## 行为准则

参与本项目即表示同意遵守 `CODE_OF_CONDUCT.md`。
