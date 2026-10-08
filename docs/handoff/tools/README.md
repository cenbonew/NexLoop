# 交接包静态检查

`validate_handoff.py` 检查 JSON/YAML、契约示例与 11 种负例、模块/任务/验收引用、依赖 DAG、本地文档链接、私有 IP 的存放范围、任务/验收状态词表与证据、以及 `MANIFEST.json` 的文件哈希。运行需要 Python 3.11+、`jsonschema`、`PyYAML`、`rfc3339-validator`（后者缺失时 date-time 负例依赖 schema 内置正则，检查器会单独报告）。

执行（推荐隔离环境）：

```bash
uv run --with jsonschema --with pyyaml --with rfc3339-validator python tools/validate_handoff.py
```

修改交接包内任何文件后重建清单：

```bash
uv run --with jsonschema --with pyyaml --with rfc3339-validator python tools/validate_handoff.py --write-manifest
```

结果写入 `planning/handoff-validation.json`（该文件与 `MANIFEST.json` 本身不参与哈希）。此工具只证明交接包的结构一致性，不执行 Docker、SSH、LLM、真实业务或故障演练。

## 状态词表

- `planning/tasks.json` 的 `status`：`not_started | in_progress | blocked | done`；`done` 必须有非空 `evidence`。
- `planning/acceptance-tests.json` 的 `status`：`not_run | passed | failed | blocked | skipped`；`passed`/`failed` 必须有非空 `evidence`。
- `evidence` 为对象数组，每项至少含 `type`（`command | test_report | file | commit | note`）、`ref`（路径/命令/commit）、`summary`、`recorded_at`（ISO 8601）。

在实现仓库中以 `--planning planning` 指向仓库根的实时副本（其余检查仍读取 `docs/handoff/` 冻结副本）。Codex 推进任务时直接更新这两个文件并重跑检查器；不要为了“保持初始状态”把已执行进度改回 `not_started`。
