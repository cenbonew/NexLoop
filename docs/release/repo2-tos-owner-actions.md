# repo2 异地备份：负责人只需做的事（一页）

先选一种做法，然后只做对应的一栏。比较与理由见 [NX-035-repo2-tos §6.2](../implementation/NX-035-repo2-tos.md)。

| | **A. 复用 NEX-EIOS 的数据保护桶**（操作最少） | **B. 新建专用桶**（误删可找回、账单分开） |
|---|---|---|
| 1 | 告诉调度员数据保护桶的**名字**（生产 env `NEX_EIOS_DATA_PROTECTION_TOS_BUCKET` 的值；不是 Artifact 桶） | 新建一个私有桶：选地域，默认加密 AES256，**开启版本控制**，不开对象锁 |
| 2 | 加子用户之前，在 NEX-EIOS 主机上跑一次上游的桶审计 audit-only，确认为 compliant | 按 `deploy/examples/tos/` 设置桶策略（只允许 TLS）和生命周期（分段上传 1 天、历史版本 14 天） |
| 3 | 建两个子用户（只开编程访问），各附一条**只限 `nexloop/` 前缀**的策略（模板 `deploy/examples/tos/iam-policy-data-host.example.json`） | 同左 |
| 4 | 各生成一对 AK/SK，直接存入钥匙串（条目名见下） | 同左 |
| 5 | 再跑一次 audit-only，仍为 compliant | — |
| 6 | 在钥匙串中生成三个随机口令（条目名见下） | 同左 |
| 7 | 交互和 AT-015 结束后，按命令单逐步确认 B 类步骤 | 同左 |
| 命令单 | [run-repo2-tos-reuse-20261017](run-repo2-tos-reuse-20261017.md) | [run-repo2-tos-20261017](run-repo2-tos-20261017.md) |
| 代价 | 版本控制只能保持关闭：NexLoop 前缀下的对象被删后无法找回；账单与 NEX-EIOS 合在一起；桶级事故会同时影响两边 | 多一个桶要管理 |

**钥匙串条目**（两种做法相同，只存在钥匙串中，并在 `~/.nexloop/vault-index.md` 中登记，不写值）：

| 条目 | 内容 |
|---|---|
| `nexloop/backup/tos-data-access-key` / `nexloop/backup/tos-data-secret-key` | DATA_HOST 子用户的 AK/SK（pgBackRest repo2 与恢复日志的 restic repo2） |
| `nexloop/backup/tos-app-access-key` / `nexloop/backup/tos-app-secret-key` | APP_HOST 子用户的 AK/SK（文件的 restic repo2） |
| `nexloop/backup/repo2-cipher-pass` | pgBackRest repo2 的加密口令，32 位以上，与 repo1 不同 |
| `nexloop/backup/restic-repo2-journal` / `nexloop/backup/restic-repo2-files` | 两个 restic repo2 的口令 |

**不要做的事**：
- 不要用主账号的 AK；
- 不要给子用户任何桶配置权限；
- A 方案下，不要改数据保护桶的任何设置；
- 不要把 AK、口令或桶名发到聊天或写进仓库。
