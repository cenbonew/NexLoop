# repo2（方案 B，桶已建好）：负责人下一步（一页）

负责人 2026-10-17 选了方案 B，在 `cn-shanghai` 新建了专用桶 `<TOS_BUCKET>`（桶名只在私有 env 中）。剩下的事按顺序做，每步约几分钟。详细步骤见 [run-repo2-tos-20261017](run-repo2-tos-20261017.md) §1。

| # | 在哪里 | 做什么 | 完成的标志 |
|---|---|---|---|
| 1 | TOS 控制台，桶的设置页 | 按 §1.1 的 9 项逐项核对和设置：**私有 ACL**、**默认加密 SSE-TOS**、**开启版本控制**、对象锁**关闭**、**只允许 TLS 的桶策略**（按 `deploy/examples/tos/bucket-policy.example.json`）、**生命周期**（`nexloop/` 的分段上传 1 天，历史版本 14 天，**不设当前版本过期**）、CORS 等不开 | 9 项都符合 |
| 2 | 访问控制 | 建两个只开编程访问的子用户 `nexloop-backup-data`、`nexloop-backup-app`，各附一条只限自己前缀的自定义策略（§1.2；条件键写全小写的 `tos:prefix`，不给桶配置权限） | 两个用户各有一条策略 |
| 3 | 访问控制，然后在本机终端 | 为两个用户各生成一对 AK/SK，**立即**用 `security add-generic-password -s <条目> -a "$USER" -w` 交互式存入钥匙串（值不写在命令行上）。条目：`nexloop/backup/tos-data-access-key`、`…/tos-data-secret-key`、`…/tos-app-access-key`、`…/tos-app-secret-key` | 四个条目都在；`~/.nexloop/vault-index.md` 中登记了条目名 |
| 4 | 本机终端 | 再生成三个随机口令，存入钥匙串：`nexloop/backup/repo2-cipher-pass`、`…/restic-repo2-journal`、`…/restic-repo2-files` | 三个条目都在 |
| 5 | 本机终端（在发布检出中） | 只读验证：`python3 scripts/deploy/tos_prefix_probe.py --bucket "$TOS_BUCKET" --role data`，然后同样执行 `--role app`；都通过后各加 `--write` 再跑一次 | 每次输出 `ok: true`；**把输出（只有检查名和状态码）贴给调度员** |
| 6 | — | 等试运行和 AT-015 结束后，由调度员按命令单 §2 起逐步执行，B 类步骤由你逐一确认 | — |

**`tos_prefix_probe.py` 结果的含义**：
- 自己的前缀 200，整个桶和对方的前缀 403：子用户的权限正确；
- `list_path_style` 的结果供调度员判断 restic 是否需要额外的寻址选项，你不用处理。

**不要做的事**：
- 不要用主账号的 AK；
- 不要给子用户 `tos:*` 或任何桶配置权限；
- 不要开对象锁；
- 不要给 `nexloop/` 设置“过期删除当前版本”的生命周期规则；
- 不要把 AK、SK、口令或桶名贴到聊天、文档或仓库里。
