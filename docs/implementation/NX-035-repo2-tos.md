# NX-035 repo2：火山引擎 TOS 上的异地备份（调研与方案）

**依据**：负责人 2026-10-17：“NEX-EIOS 就有对接火山引擎对象存储的方式，你调研下，用同样的方式。”repo2 在 ROADMAP 2026-10-14 中记为已知风险：S3 兼容对象存储暂缺。

**范围**：
- 调研上游冻结工作树 `NEX-EIOS-release-rda-1e363db`（只读，规则允许的唯一上游依据）中对接 TOS 的方法；
- 设计 NexLoop 的 repo2：pgBackRest 的 repo2，以及 restic 文件仓库的 repo2。

**不做**：
- 不复制上游的业务域代码（sports8 归档、tennis 等）；
- 不引入 TOS SDK 依赖；
- 不写任何真实凭据、桶名或账号。

命令单见 [run-repo2-tos-20261017](../release/run-repo2-tos-20261017.md)。

## 1. 上游怎么对接 TOS（调研结论）

下表只记方法和出处，不记业务内容。

| 方面 | 上游做法 | 出处 |
|---|---|---|
| **客户端** | 官方 TOS SDK `tos.TosClientV2`，即 TOS 原生协议，**不是** S3 协议。全仓库没有使用 S3 兼容端点 | `src/eios/adapters/tos/storage.py` 的 `create_tos_client` |
| **endpoint 与 region** | 数据面使用 VPC 内网域名 `https://tos-cn-shanghai.ivolces.com`，注释写明“`.ivolces.com` 在公网不可解析”；预签名下载使用公网域名 `tos-cn-shanghai.volces.com`；region 为 `cn-shanghai`。三者都写死并校验形状 | `src/eios/persistence/artifact_settings.py` 第 12–21 行及 `_validate_endpoint` |
| **凭据** | 三种互斥的来源：**ECS 实例角色**（首选，磁盘上没有凭据材料，从元数据服务取 STS）；`credential_process`，即 `/usr/local/sbin` 下 root 所有的 broker 输出 STS JSON；静态 AK/SK（`StaticCredentialProvider`）。配置文件为 root 0600，拒绝组和其他人可读、拒绝符号链接；“禁止在脚本中保存 AK/SK”。2026-08-16 的裁定是不给备份身份铸长期 AK/SK，期望状态写成 `CredentialState: Disabled`，一旦出现长期密钥就报漂移 | `src/eios/adapters/tos/credentials.py`；`ops/tos/governance.json.example`；`docs/operations/data-protection.md` “安装与凭据” |
| **桶** | 与业务 Artifact 桶完全独立的私有桶：ACL private，拒绝匿名访问；Bucket Policy 拒绝非 TLS 请求 | `data-protection.md` “不可变安全边界”；`src/eios/apps/data_protection_bucket.py` 的期望状态 |
| **前缀** | 写死的前缀白名单：`disaster-recovery/daily/`、`disaster-recovery/monthly/`、`archive/<channel>/`；写入时校验 key 必须在白名单内 | `src/eios/adapters/tos/data_protection.py` 的 `_PREFIXES` |
| **加密** | **服务端**：桶默认加密和每次写入都是 SSE-TOS（`AES256`）。**客户端**：灾难恢复文件上传前先用 age/X25519 加密；私钥离线、双人保管，有指纹仪式 | `DATA_PROTECTION_WRITER_CONTRACT`；`data-protection.md` “age 指纹仪式” |
| **版本与覆盖** | Versioning 必须为 **Disabled**，依靠 forbid-overwrite 保证单个对象不可覆盖；Enabled 或 Suspended 时审计器 fail closed，需人工迁桶 | `data-protection.md`；`_versioning_is_acceptable` |
| **生命周期** | 每个前缀一条过期规则：daily 90 天、monthly 365 天、archive 永不过期（禁止被空前缀或宽泛前缀的规则覆盖）；未完成的分段上传 1 天后终止 | `data_protection_bucket.py` 中 `lifecycle` 的期望值 |
| **IAM** | 每个身份一套、按前缀限定：备份身份只能读写两个 DR 前缀，**没有删除权限、没有桶配置权限**；恢复身份默认关闭，恢复时临时启用，只读；Artifact 身份对这个桶零权限 | `identity_access`、`RecoveryActivationTemplate` |
| **TOS 特有写法**（实测） | 列举动作是 `tos:ListObjects`，前缀条件键必须是全小写的 `tos:prefix`（`prefix`、`tos:Prefix` 都会被拒）；桶策略写 `"Version": "1.0"`、`Principal: ["*"]`、`"tos:securetransport": ["false"]`。照抄 AWS 写法会让整条策略被拒 | `data_protection_bucket.py` 第 831–900 行的注释 |
| **审计与演练** | 每日只读漂移审计，覆盖 ACL、Policy、SSE、Versioning、Lifecycle、IAM、访问日志、云审计；每月 checksum 演练；每季度做隔离恢复 | `data-protection.md` |

## 2. NexLoop 的条件与上游有哪些不同

1. **DATA_HOST、APP_HOST 都不在火山引擎 VPC 里**：内网域名 `*.ivolces.com` 解析不到，ECS 实例角色也没有。所以：
   - 只能走**公网 TLS 端点**；
   - 凭据只能是**静态 AK/SK**，必须是专用子用户的、按前缀限定的密钥，不能用主账号的；
   - 这与上游“首选无磁盘凭据”的方向不同，是条件所限，在 §5 的风险中记录。
2. **工具是 pgBackRest 和 restic，不是自写的上传器**：两者都只支持 S3 协议，所以用 TOS 的 **S3 兼容端点** `tos-s3-<region>.volces.com`。上游没有用过这个端点，它的可用性以命令单第 2、4 步的实测为准。
3. **备份工具自己管理保留期**：pgBackRest 的 expire、restic 的 forget/prune 都会删除自己的旧对象，所以：
   - 备份身份需要 `tos:DeleteObject`，这与上游“备份身份无删除权限”不同；
   - 桶生命周期**不能**对仓库前缀设置过期，否则会删掉仓库中仍被引用的对象，仓库就损坏了。上游按前缀过期的做法不能照搬。
4. **覆盖语义**：pgBackRest 和 restic 都会改写自己的元数据对象（例如 `backup.info`、restic 的 index），所以上游 forbid-overwrite 加 Versioning Disabled 的契约不适用。

## 3. 方案

### 3.1 桶与前缀

- **一个新的专用私有桶**，不与任何业务桶共用。region 由负责人选（建议与负责人的其他资源同地域，例如 `cn-shanghai`）。
- **ACL**：private。
- **Bucket Policy**：拒绝非 TLS 请求。示例 `deploy/examples/tos/bucket-policy.example.json`，按上游实测的 TOS 写法。
- **默认加密**：SSE-TOS（AES256），与上游相同。
- **前缀**：

  | 前缀 | 写入者 | 内容 |
  |---|---|---|
  | `nexloop/pgbackrest/` | DATA_HOST 子用户 | pgBackRest repo2，即 `repo2-path=/nexloop/pgbackrest` |
  | `nexloop/restic-journal/` | DATA_HOST 子用户 | 恢复日志的 restic repo2 |
  | `nexloop/restic-files/` | APP_HOST 子用户 | Artifact 与已终态 Run 目录的 restic repo2 |

- **生命周期**（示例 `deploy/examples/tos/lifecycle.example.json`）：
  - 只设一条：未完成的分段上传 1 天后终止；
  - 开启版本控制时，再加一条：历史版本 14 天后过期；
  - **不对 `nexloop/` 设任何过期规则。**
- **版本控制**：负责人决定。我建议**开启**，并让历史版本 14 天后过期：
  - 好处：AK 泄露后，攻击者删除或覆盖的对象可以在 14 天内找回；
  - 代价：多占 14 天的历史版本空间。
  - 上游要求 Disabled 是为了 forbid-overwrite，这里不适用（见 §2 第 4 点）。
- **对象锁**（WORM）：**不建议**开启。它会让 pgBackRest expire 和 restic prune 删不掉过期对象，仓库只增不减。

### 3.2 身份与凭据

- **两个专用子用户**，各自一对 AK/SK，按前缀限定（示例 `deploy/examples/tos/iam-policy-data-host.example.json`）：
  - DATA_HOST：`tos:ListObjects`（加 `tos:prefix` 条件），以及对 `nexloop/pgbackrest/`、`nexloop/restic-journal/` 的 Get、Put、Delete 和分段上传相关动作；
  - APP_HOST：同样的形状，只限 `nexloop/restic-files/`；
  - 都没有任何桶配置权限。
- **恢复身份**（可选，与上游相同）：一个只读子用户，平时停用，恢复演练时临时启用，对 `nexloop/` 只有 Get 和 List。
- **保管**：负责人存入 macOS 钥匙串，只用一处存放，见 `~/.nexloop/vault-index.md`。条目名：
  - `nexloop/backup/tos-data-access-key`、`nexloop/backup/tos-data-secret-key`；
  - `nexloop/backup/tos-app-access-key`、`nexloop/backup/tos-app-secret-key`；
  - repo2 的 pgBackRest 口令：`nexloop/backup/repo2-cipher-pass`；
  - 两个 restic repo2 口令：`nexloop/backup/restic-repo2-journal`、`nexloop/backup/restic-repo2-files`。
- **下发**：从钥匙串经管道写到主机上的私有文件，不经命令行参数、shell 历史或聊天：
  - DATA_HOST：
    - pgBackRest 的 key 和 secret 写进 `secrets/pgbackrest/pgbackrest.conf` 的 repo2 段（uid 999，0600，与 repo1 的口令在同一个文件）；
    - restic 用 `/etc/nexloop/restic-repo2.env`，以及 `restic.env` 中 repo2 的几行（root:nexloop-backup，0600）；
  - APP_HOST：`nexloop-file-backup --environment-file` 指向的 env 文件，0600，只接受 `AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY`、`AWS_DEFAULT_REGION` 三个键（`file_backup._ENVIRONMENT_KEYS`）。
- **轮换**：在 TOS 控制台为子用户新建第二对 AK，替换主机上的文件，用 `pgbackrest check` 和 `restic snapshots` 确认可用，再停用旧 AK。

### 3.3 加密

与上游一样是双层加密：
- 服务端：SSE-TOS（AES256），作为桶默认加密；
- 客户端：pgBackRest `repo2-cipher-type=aes-256-cbc`，使用 repo2 自己的口令，与 repo1 不同；restic 本身对仓库加密，每个仓库各自一个口令。

客户端口令与 AK/SK 分开保管，所以即使 AK 泄露，对方拿到的也只是密文。

### 3.4 配置模板（本分支）

| 文件 | 用途 |
|---|---|
| `deploy/stage/config/pgbackrest-repo2-tos.conf.example` | 加到 DATA_HOST `pgbackrest.conf` 的 `[global]` 段：`repo2-type=s3`，端点 `tos-s3-__TOS_REGION__.volces.com`，`repo2-s3-uri-style=host`，校验 TLS，`repo2-path=/nexloop/pgbackrest`，cipher，保留期与 repo1 相同（full 35 天，diff 14 天） |
| `deploy/stage/config/restic-repo2.env.example` | DATA_HOST 的 journal restic repo2 环境：repository 和 password 都是文件路径，加上三项 AWS 变量 |
| `deploy/stage/config/file-backup-repo2.env.example` | APP_HOST `nexloop-file-backup --repo repo2` 的环境文件：只有三个键 |
| `deploy/examples/tos/*.json` | 桶策略、生命周期、DATA_HOST 子用户的 IAM 策略：只表达规则，取值由负责人在控制台填写 |
| `deploy/configuration/backup.v2.json` | 备份计划 v2：repo2 改为 required，其余与 v1 相同 |

`pgbackrest.conf.example` 不变（repo2 仍是注释）。测试 `test_stage_secrets` 要求示例中没有生效的 repo2 设置，所以 repo2 的内容放在单独的片段文件中。

### 3.5 与现有作业的关系（基本不需要改 unit）

- `nexloop-pg-full` 和 `nexloop-pg-diff` 本来就是“先 repo1、后 repo2”两条 `ExecStart`（`test_stage_systemd_units` 会核对这一点）。repo2 配好之前，带 repo2 那一行的 unit 每次都会失败，所以 stage 上安装的可能是只有 repo1 的版本（安装时 repo2 还没有）。命令单第 5 步会先核对，必要时换成两行的版本。
- `nexloop-journal-restic@repo2.timer`（每 15 分钟）和 `nexloop-restic-maint` 中 repo2 的 forget、prune、check 已经写好，只需要 env 文件和启用。
- APP_HOST 的 `nexloop-file-backup` 在 repo2 那几行带 `--environment-file`。
- **WAL 归档**：pgBackRest 配了两个仓库后，`archive-push` 会把 WAL 推到两个仓库。repo2 不可达时（例如公网中断），归档会报错，`pg_wal` 会积压，`doctor --backup` 的 `wal_archiving` 和告警会发现这一点。
  - pgBackRest 在多仓库下推送失败的具体行为，命令单第 7 步用断网演练实测（只在维护窗口做）；
  - 不要为了避免积压而设置 `archive-push-queue-max`：那样会丢 WAL，PITR 就断了。

### 3.6 首次全量、校验与恢复演练

1. `stanza-create` 只在 repo2 上创建 stanza，然后 `check` 校验两个仓库的归档。
2. 用执行器做首次全量：`nexloop-backup pg --repo repo2 --kind pg_full`。它会执行 pgBackRest backup 和 verify，并把 info 写进清单，预期 `verify_status: verified`。
3. 初始化 restic repo2（journal 和 files 各一个），各做一次备份并 check。
4. **恢复演练**：形态同 run-33，`nexloop-restore --source pgbackrest --repo 2`（参数取自 `backup.py` 的 restore CLI，`--repo` 可选 1 或 2）。演练用恢复身份的只读 key，`tool_seconds` 中包含公网下载时间，也就是 repo2 的 RTO。
5. 证据：清单行、`info` 的 JSON、演练报告，都放进 v0.1 证据包的 `backup-restore/`。

### 3.7 doctor --backup

- **代码改动**（本分支）：`doctor.load_backup_plan` 接受两种仓库组合：
  - v1：`required: [repo1]`，`optional: [repo2]`；
  - v2：`required: [repo1, repo2]`，`optional: []`；
  - 其他组合一律拒绝。
- 新增 `deploy/configuration/backup.v2.json`。repo2 的首次全量验证通过之后，把 `--backup-plan` 换成 v2。从那时起，repo2 超过 `max_backup_age_seconds`（26 小时）没有新的已验证备份，`required_repos_recent` 就为 false。
- 告警规则 v4 中 repo2 的年龄规则（selector `repo2`）在 repo2 有备份之后自动生效，不需要改。
- **测试**：`tests/test_api_stage_ready_pg.py::test_doctor_backup_plan_v2_requires_the_offsite_repo2`：
  - v2 与 v1 只差 version、decision 和 repos；
  - repo2 从未备份时失败；repo2 在限期内时通过；repo2 过期时失败；
  - 三种非法组合被拒。

### 3.8 网络

- **DATA_HOST**：现有的 `data-host.nft.template` 只限制入站（PG、Valkey）。容器的出站策略是 accept，postgres 容器里的 pgBackRest 可以直接访问公网 443，**不需要新的放行规则**。
  - 可选的加固：限定 postgres 容器只能访问 TOS 端点的 443。但 TOS 的地址会变（DNS），按 IP 写死会造成误杀，所以暂不做。真要做的话，应在专门的出站网络上按域名代理。
- **APP_HOST**：`nexloop-file-backup` 在主机上运行，不在 compose 网络里，`app-host.nft` 不限制主机自己的出站，所以也不需要改动。
- **带宽**：首次全量要上传整个数据库（压缩并加密后），先用 `pgbackrest info` 看 repo1 的备份大小，估算上传时间，选在低峰时做。

## 4. 需要的代码改动与测试（本分支已完成）

| 改动 | 测试 |
|---|---|
| `doctor.BACKUP_REPOS` 和 `load_backup_plan` 接受 v1、v2 两种组合 | `test_doctor_backup_plan_v2_requires_the_offsite_repo2`；原有的 `test_doctor_backup_*` 不变 |
| `backup.v2.json` | 同上 |
| repo2 的配置片段、env 示例和 TOS 规则示例 | `tests/test_repo2_tos_examples.py`：端点是 TOS 的 S3 域名而不是 `ivolces`；repo2 有自己的 cipher；env 中只有执行器接受的键；桶策略只允许 TLS，且使用 TOS 写法；IAM 按前缀限定，没有桶配置动作；生命周期不让仓库对象过期；示例中没有任何像密钥或真实桶域名的值 |

**没有改的**：
- 没有引入 TOS SDK：pgBackRest 和 restic 走 S3 协议；
- 没有写桶的审计器：上游的每日漂移审计依赖 SDK 和 IAM、云审计的控制面，是一个独立的大件。v0.1 用命令单第 1 步的人工核对清单代替，审计器留作后续项。

## 5. 风险与待验证

| 项 | 说明 | 怎么验证 |
|---|---|---|
| TOS 的 S3 兼容端点 | 上游没有使用过；域名格式 `tos-s3-<region>.volces.com`、是否要求 virtual-hosted 风格，都以火山引擎文档和实测为准 | 命令单第 2 步的 `stanza-create`、`check`，第 4 步的 `restic init` |
| restic 的寻址方式 | restic 对非 AWS 端点默认用 path 风格（`s3.bucket-lookup=auto`）。如果 TOS 只接受 virtual-hosted 风格，`init` 会失败，需要加 `-o s3.bucket-lookup=dns`。现有 unit 和 `file_backup` 都还不能传这个选项 | 第 4 步。失败时**先停下**，代码改动作为后续项：`file_backup` 和 unit 增加一个只允许 `dns`/`path` 的选项 |
| 静态 AK/SK | 上游首选 ECS 角色，但 NexLoop 的主机没有这个条件 | 按前缀限定、每台主机一个子用户、文件 0600、可轮换、开启版本控制以便找回误删 |
| 公网中断时的 WAL 归档 | repo2 推送失败会让 `archive-push` 报错，WAL 积压 | 第 7 步断网演练，观察 `wal_archiving` 和 `pg_wal` 的增长 |
| 桶治理没有自动审计 | 上游每天审计，这里只有人工核对 | 每月按第 1 步的核对清单人工复核一次；审计器作为后续项 |
