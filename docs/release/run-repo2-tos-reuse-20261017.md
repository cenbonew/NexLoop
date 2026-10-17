# 命令单（复用版）：repo2 放进 NEX-EIOS 的数据保护桶（2026-10-17 起，交互结束后执行）

**方案**：[NX-035-repo2-tos §6](../implementation/NX-035-repo2-tos.md#6-复用方案放进-nex-eios-现有的数据保护桶负责人-2026-10-17-追问)。

**与新建桶版的关系**：本单是 [run-repo2-tos-20261017](run-repo2-tos-20261017.md) 的复用版本。只有第 0、1 步不同；第 2–9 步照新建桶版执行，差别列在下面的 §2。

只用角色名和变量。**B** 表示需负责人确认；**sudo-A**、**sudo-D** 表示需要 APP_HOST、DATA_HOST 的 sudo。

## 0. 负责人需要提供（全部 B）

| 项 | 说明 | 保存位置 |
|---|---|---|
| NEX-EIOS 数据保护桶的名字 | 生产 env `NEX_EIOS_DATA_PROTECTION_TOS_BUCKET` 的值。**不是** Artifact 桶，代码规定两者不得相同 | NexLoop 的私有 env（`__TOS_BUCKET__`） |
| region | 上游写死为 `cn-shanghai`；端点 `tos-s3-cn-shanghai.volces.com` | 同上 |
| 两个子用户的 AK/SK | DATA_HOST 子用户（`nexloop/pgbackrest/`、`nexloop/restic-journal/`），APP_HOST 子用户（`nexloop/restic-files/`） | 钥匙串 `nexloop/backup/tos-data-access-key`、`tos-data-secret-key`、`tos-app-access-key`、`tos-app-secret-key` |
| 三个客户端口令 | pgBackRest repo2 cipher、restic journal repo2、restic files repo2 | 钥匙串 `nexloop/backup/repo2-cipher-pass`、`restic-repo2-journal`、`restic-repo2-files` |
| 接受的代价 | 版本控制保持 Disabled（上游审计的要求）：NexLoop 前缀下的对象一旦被删就无法找回；账单与 NEX-EIOS 合在同一个桶上 | 记录在变更记录中 |

**不需要**：新建桶，修改桶策略、生命周期、加密或版本控制。

## 1. 控制台：只建子用户（负责人本人；B）

1. **加子用户之前**，在 NEX-EIOS 的数据库主机上跑一次上游的桶审计 audit-only。命令与参数见上游 `docs/operations/data-protection.md` 的“Bucket 审计与人工执行”，**只做 audit，不带 `--apply`**。
   - 预期 `compliant`，退出码 0；
   - 不是 compliant 时先停下：桶的现状与上游期望不一致，复用的前提不成立。
2. 在访问控制中新建两个子用户：只开通编程访问，不开通控制台登录。
3. 给每个子用户附一条自定义策略，按 `deploy/examples/tos/iam-policy-data-host.example.json` 填写：
   - `__TOS_BUCKET__` 填数据保护桶的名字；
   - 列举动作 `tos:ListObjects` 加全小写的条件键 `tos:prefix`，只放 `nexloop/...` 的前缀；
   - 对象动作 Get、Put、Delete 以及分段上传相关的动作，资源只写 `nexloop/...` 下的路径；
   - APP_HOST 子用户只放 `nexloop/restic-files/`；
   - **不给**任何桶配置动作，**不给** `disaster-recovery/`、`archive/` 下的任何资源。
4. 各生成一对 AK/SK，直接存入钥匙串（条目名见第 0 步）。
5. **加子用户之后**，再跑一次上游的 audit-only，预期仍为 compliant。
   - 如果报了 IAM 漂移：停用这两个子用户的 AK，回到第 3 步检查策略；或者改用新建桶版。
6. **核对**（以后每月一次）：
   - 桶策略、生命周期、版本控制、加密都与第 1 步之前的审计结果一致，NexLoop 没有改动任何一项；
   - 两个子用户的权限只覆盖 `nexloop/`；
   - 上游 audit-only 为 compliant。

## 2. 第 2–9 步：照新建桶版执行，以下几点不同

- **配置值**：`__TOS_BUCKET__` 填数据保护桶的名字，`__TOS_REGION__` 填 `cn-shanghai`。模板 `pgbackrest-repo2-tos.conf.example`、`restic-repo2.env.example`、`file-backup-repo2.env.example` 不需要改。
- **第 2.2 步**：`stanza-create` 只在 `nexloop/pgbackrest` 下建立 stanza，不会触碰上游的前缀。
- **第 3、4 步**：首次全量和 restic 初始化都只写 `nexloop/` 下的对象。完成后，可以在控制台按前缀查看对象：`disaster-recovery/`、`archive/` 下的对象数量和时间应与之前相同。
- **第 7.1 步（恢复演练）**：恢复子用户如果要用，也只授权 `nexloop/` 的 Get 和 List。
- **撤销（第 8 步）**：
  - 停用并删除两个子用户；NexLoop 的配置回到只有 repo1；
  - `nexloop/` 下的对象：保留，或由负责人在控制台**按前缀**删除。**不要**删除桶，也不要设置会影响整个桶的规则。

## 3. 证据

与新建桶版相同，另外加上第 1 步加子用户前后两次 audit-only 的结论（只记 `compliant` 和退出码，不记账号、桶名或身份名）。
