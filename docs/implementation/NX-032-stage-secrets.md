# NX-032 stage 秘密文件：清单、生成与放置（部署执行单 1.6）

依据：调度员 2026-10-14。工具：`scripts/deploy/stage_secrets.py`。测试：`tests/test_stage_secrets.py`。

本文与工具都只用参数和占位值。地址、主机名、租户名都在负责人的私有 params 文件和 stage env 里。

## 1. 阶段

| 阶段 | 含义 | 何时生成 | 何时放到主机 |
|---|---|---|---|
| **A** | 部署前可以生成：证书、私钥、CA bundle、引导口令、64 位十六进制密钥、Valkey ACL、两端共用的 webchat 渠道令牌 | 管理机上执行 `stage_secrets.py generate` | 执行单 1.6 |
| **A/B** | A 阶段生成，B 阶段（bootstrap 之后）才生效：DSN 中的角色口令、`pgpass`、recorder DSN、各 service/route/source/planner/executor/queue 凭据令牌、签名密钥 | 同上 | 文件在 1.6 放好；口令在 2.5b 生效（`role-passwords.sql`）；令牌和签名密钥在 bootstrap 之后由受信配置登记（§3） |
| **C** | NX-037 真实租户时提供：参考端点的 `enterprise-provider-config` 与 `service_token`、两个 `model.env` | 负责人 | 执行单 4.3 与周六就绪检查 R6 的同一方式 |
| **manual** | 手工填写的配置：`provider-routes`、`webchat-provider-config`、`pgbackrest.conf` | 负责人或调度员按对应文档 | `provider-routes` 与 `webchat-provider-config` 在 3.4 启动应用之前；`pgbackrest.conf` 在 2.2 之前 |

## 2. 步骤

1. **params**（私有，0600）：`data_host_addr`、`data_host_name`、`pg_port`、`db_name`、`nexloop_root`、`signing_key_id`，以及五组 SAN：
   - `gateway`：APP_HOST 的名称和地址；
   - `agent-host`：runtime worker、api 与 retention-keeper 连接 Host 时使用的名称或地址（agent link）；
   - `guard`：Host 连接 guard 时使用的名称或地址；
   - `data`：DATA_HOST 的名称和地址；
   - `loopback`：`DNS:localhost` 与 `IP:` 本机回环地址。
2. **生成**（管理机，复用已签发的中间 CA）：
   ```bash
   python3 scripts/deploy/stage_secrets.py generate --out <新的私有目录> --ca-dir <中间 CA 目录> --params <私有 params.json>
   python3 scripts/deploy/stage_secrets.py check --out <同上>
   ```
   预期：`generated` 为 119，`manual_or_phase_c` 为 7；`check` 输出 `ok: true`，`pending_manual_or_phase_c` 列出 7 个手工或阶段 C 文件。
3. **执行单 1.6，放到主机**：
   - 把 `app/secrets/<service>/` 和 `data/secrets/<service>/` 按服务目录复制到对应主机的 `$NEXLOOP_ROOT/secrets/<service>/`（目录 0700、文件 0600），每个文件用 `install -o <属主> -g <属主> -m 0600` 安装，属主取自 `manifest.json` 中的 `owner`；
   - 安装后在主机上执行 `stage_secrets.py check --out <同一棵树> --uids`。
   - `management/` 目录**不上传**，只留在管理机：
     - `signing.key.hex`：签名密钥的十六进制形式，受信配置用；
     - `role-passwords.sql`：只含 SCRAM 校验值；
     - `service-secrets.json`：凭据文件路径到令牌的映射，受信配置用。
4. **执行单 2.5b**：负责人以 `nexloop_bootstrap` 在窗口内执行 `role-passwords.sql`，例如 `psql ... -f role-passwords.sql`，经容器内 socket。文件里没有明文口令。
5. **4.1 之前（应用启动、smoke 之前）**：按 §3 登记签名密钥和全部凭据令牌；填好 `provider-routes` 与 `webchat-provider-config`。

`check` 会核对：
- `deploy_checks.secret_distribution`：权限、单链接、属主；Agent Host 目录中没有 DSN、凭据、签名或渠道文件；Compose 中没有明文环境变量；真实派发关闭；
- 每个已生成文件的 sha256 与 manifest 一致；
- 没有 manifest 之外的文件。

## 3. 阶段 B：凭据与签名密钥的登记（结论）

- **现有入口**：`python -m nexloop_eios.trusted_configuration --manifest <M> --apply --database-url-file <配置者 DSN> --signing-key-file <signing.key.hex> --signing-key-id <ID> --service-secrets-file <reference→令牌>`。它会：
  - 用十六进制密钥文件注册签名密钥：`authz.nexloop_authority_signing_keys`，经 `control.nexloop_configure_manifest`；
  - 对每个令牌取 sha256 后登记 `authz.nexloop_service_credentials`，只存哈希；
  - 写入 authority facts。
- `scripts/nx037/real_tenant_init.py execute` 通过 `service_grants.apply` 走同一条路径，使用 `deploy/authorization/service-grants.v1.json`。
- **缺什么**：
  1. `service-grants.v1.json` 只有 14 个服务主体，`credential_reference` 对应 14 个服务的 `service-credential`。Compose 还需要以下凭据，**仓库里没有任何 manifest 或工具为它们声明主体与授权**：
     - domain-worker、action-worker、webchat-delivery 的 `service-credential`；
     - message-relay 的 route、source、planner、executor；
     - reply-guarantor 的 route、source、planner、executor；
     - plan-reevaluator 的 source、planner、queue、executor。

     现在只有测试夹具（`local_message_assembly_fixture`）和负载工具会造这些主体。
  2. 14 个 reference 与 Compose 文件的对应关系没有写成文件。例如 `message_relay` 这个 reference 对应 relay 的哪一个凭据？
- **已实现（l2/stage-register）**：`deploy/authorization/stage-principals.v1.json` 加 `stage_secrets.py register`。
  - `credential_files` 把 Compose 挂载的 28 个服务凭据文件逐个映射到一个主体（同一主体的所有文件持同一令牌，`generate` 按主体发令牌）。与 `service-grants.v1.json` 合并后共 21 个主体、61 条授权，测试从 Compose 读取凭据文件核对“无重复、无遗漏”。
  - 新增 7 个主体与授权（证据的文件与行号写在每条 `purpose` 里）：

    | 主体 | 使用者 | 授权 |
    |---|---|---|
    | effect_executor | action-worker、webchat-delivery，以及 relay/reply-guarantor/plan-reevaluator 的 executor-credential | service.request、service.query、service.receipt_reconcile、enterprise.deliver |
    | message_relay（已有，扩充） | relay/reply-guarantor 的 route-credential | 增加 NexLoop.queue.operations |
    | relay_source | relay/reply-guarantor 的 source | service.request、enterprise.deliver、context.bind；artifact local_real create/read；Message READ 由 `message_read_rule`（actor、body）给出 |
    | relay_planner | relay/reply-guarantor 的 planner | Goal.create、PlanStep.create、MessageAssignment.create、plan.bind_effect_context |
    | role_source | plan-reevaluator/source | service.request、context.bind_role；artifact local_real create/read |
    | role_planner | plan-reevaluator/planner | plan.bind_effect_context |
    | role_queue | plan-reevaluator/queue | NexLoop.queue.operations |
    | artifact_maintenance | domain-worker | artifact orphans_real delete |

  - 用法（只用配置者角色，经 `service_grants.apply` → `trusted_configuration.apply_manifest_with_secrets`；令牌不打印，暂存文件 0600 用后删除；可重复执行）：
    `stage_secrets.py register --out <树> --tenant <租户> --configurator-dsn-file <文件> --signing-key-id <ID> --credential-expires-at <时间>`；`--dry-run` 只列出将登记的主体、授权与 message_read_rule。
  - 注意：effect_executor 的令牌必须就是 owner 做 `configure_effect_control` 时使用的执行者令牌（摘要须一致）。
  - 未覆盖：plan-reevaluator 的 Role-run Source 需要的逐对象 READ 只能在租户组装后（阶段 C）授予。

## 4. 核对中发现的问题（需要处理，不在本次范围）

| # | 问题 | 影响 | 建议 |
|---|---|---|---|
| F1 | **客户端证书 CN 与角色不一致**：`api/identity-dsn` 用 `nexloop_identity`，`plan-reevaluator/api-dsn` 和 `reply-guarantor/api-dsn` 用 `nexloop_api`，但这三个服务各自只有一张客户端证书，CN 是它们的主角色。pg_hba 是 `clientcert=verify-full`，要求 CN 等于用户名 | 这三条连接会被 PG 拒绝：api 的浏览器身份池、两个 launcher 池 | 二选一：Compose 为这三个服务各加一张第二角色的客户端证书；或者 pg_hba 用 `clientcert=verify-full map=<映射>`，在 pg_ident 中放行这三个 CN 到第二角色。前者改动最小。manifest 中这三条 DSN 已经注明了不一致 |
| F2 | **Agent Host 的运行文件没有挂载**：`agent-host.runtime-config.example.json` 引用 `/run/secrets/guard_key`、`guard_certificate`、`model_configuration.json`、`model_tariff.cny.json`，Compose 中 agent-host 只挂了 `host_cert`、`host_key`、`host_transport_key`、`guard_ca` | Host 启动时会拒绝运行配置，或读不到 guard 密钥 | 对齐运行配置与 Compose 的秘密名，并补上运行配置、模型配置、价目文件的挂载。其中 `guard_key` 必须与 `runtime-worker/guard-transport.key` 是同一个值 |
| F3 | **签名密钥有两种编码**：服务用 `AuthoritySigner.from_file` 读取 32 字节原始内容；受信配置读取 64 位十六进制 | 用错编码会导致签名校验失败 | 工具同时写出两份：`shared/signing.key` 是原始字节，`management/signing.key.hex` 是十六进制。`signing_key_id` 必须同时满足两边的格式（不能含冒号） |

## 5. 两端共用同一个值的文件

| 值 | 文件 |
|---|---|
| host-control 密钥 | `shared/host-control.key`、`agent-host/transport.key`、`retention-keeper/host-control.key` |
| webchat 渠道令牌 | `action-worker/webchat-provider-credential`、`webchat-delivery/provider-credential` |
| 签名密钥 | `shared/signing.key`（各服务）、`management/signing.key.hex`（受信配置） |
| 角色口令 | 同一角色的所有 DSN 共用一个口令，例如 `nexloop_api` 和 `nexloop_domain_worker` 各有多个服务使用 |

## 6. 完整清单

由 `stage_secrets.py catalog` 生成，126 个文件：app 115 个，data 11 个。生成方式中的“A/B 生效”见 §1。

| 主机 | 文件（`secrets/` 下） | 服务 | 类型 | 生成方式 | 属主 | 阶段 |
|---|---|---|---|---|---|---|
| app | `action-worker/service-credential` | action-worker | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `action-worker/dsn` | action-worker | DSN（verify-full） | role nexloop_action_worker; its password takes effect when role-passwords.sql runs (bootstrap window) | 10001 | A/B |
| app | `action-worker/enterprise-provider-config` | action-worker | 配置 | the reference enterprise endpoint (run sheet 4.3) | 10001 | C |
| app | `action-worker/enterprise-provider-ca.crt` | action-worker | CA bundle | the intermediate CA bundle (intermediate + root) | 10001 | A |
| app | `action-worker/enterprise-provider-credential` | action-worker | 配置 | the reference endpoint service_token (run sheet 4.3) | 10001 | C |
| app | `action-worker/pg-client.crt` | action-worker | PG 客户端证书（链） | CN=nexloop_action_worker | 10001 | A |
| app | `action-worker/pg-client.key` | action-worker | PG 客户端私钥 | CN=nexloop_action_worker | 10001 | A |
| app | `action-worker/provider-routes` | action-worker | 配置 | NX-037 provider routing (effect_dispatch routes by Action) | 10001 | manual |
| app | `action-worker/webchat-provider-config` | action-worker | 配置 | the action worker's HttpEffectProvider configuration of webchat-delivery | 10001 | manual |
| app | `action-worker/webchat-provider-ca.crt` | action-worker | CA bundle | the intermediate CA bundle (intermediate + root) | 10001 | A |
| app | `action-worker/webchat-provider-credential` | action-worker | 随机令牌 | shared between the action worker and webchat-delivery | 10001 | A |
| app | `alert-evaluator/service-credential` | alert-evaluator | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `alert-evaluator/dsn` | alert-evaluator | DSN（verify-full） | role nexloop_domain_worker; its password takes effect when role-passwords.sql runs (bootstrap window) | 10001 | A/B |
| app | `alert-evaluator/pg-client.crt` | alert-evaluator | PG 客户端证书（链） | CN=nexloop_domain_worker | 10001 | A |
| app | `alert-evaluator/pg-client.key` | alert-evaluator | PG 客户端私钥 | CN=nexloop_domain_worker | 10001 | A |
| app | `api/browser-rate.key` | api | 密钥（64 位十六进制） | 32 bytes as 64 lower-case hex | 10001 | A |
| app | `api/dsn` | api | DSN（verify-full） | role nexloop_api; its password takes effect when role-passwords.sql runs (bootstrap window) | 10001 | A/B |
| app | `api/identity-dsn` | api | DSN（verify-full） | role nexloop_identity; its password takes effect when role-passwords.sql runs (bootstrap window) | 10001 | A/B |
| app | `gateway/api-loopback-ca.crt` | gateway | CA bundle | the intermediate CA bundle (intermediate + root) | 0 | A |
| app | `api/loopback.crt` | api | 服务证书（链） | SANs: loopback | 10001 | A |
| app | `api/loopback.key` | api | 服务私钥 | SANs: loopback | 10001 | A |
| app | `api/pg-client.crt` | api | PG 客户端证书（链） | CN=nexloop_api | 10001 | A |
| app | `api/pg-client.key` | api | PG 客户端私钥 | CN=nexloop_api | 10001 | A |
| app | `claim-extraction-scheduler/service-credential` | claim-extraction-scheduler | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `claim-extraction-scheduler/dsn` | claim-extraction-scheduler | DSN（verify-full） | role nexloop_api; its password takes effect when role-passwords.sql runs (bootstrap window) | 10001 | A/B |
| app | `claim-extraction-scheduler/pg-client.crt` | claim-extraction-scheduler | PG 客户端证书（链） | CN=nexloop_api | 10001 | A |
| app | `claim-extraction-scheduler/pg-client.key` | claim-extraction-scheduler | PG 客户端私钥 | CN=nexloop_api | 10001 | A |
| app | `claim-extraction-worker/service-credential` | claim-extraction-worker | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `claim-extraction-worker/dsn` | claim-extraction-worker | DSN（verify-full） | role nexloop_domain_worker; its password takes effect when role-passwords.sql runs (bootstrap window) | 10001 | A/B |
| app | `claim-extraction-worker/model.env` | claim-extraction-worker | 配置 | model API key (owner, run sheet R6 pattern) | 10001 | C |
| app | `claim-extraction-worker/pg-client.crt` | claim-extraction-worker | PG 客户端证书（链） | CN=nexloop_domain_worker | 10001 | A |
| app | `claim-extraction-worker/pg-client.key` | claim-extraction-worker | PG 客户端私钥 | CN=nexloop_domain_worker | 10001 | A |
| app | `claim-matcher/service-credential` | claim-matcher | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `claim-matcher/dsn` | claim-matcher | DSN（verify-full） | role nexloop_domain_worker; its password takes effect when role-passwords.sql runs (bootstrap window) | 10001 | A/B |
| app | `claim-matcher/model.env` | claim-matcher | 配置 | model API key (owner, run sheet R6 pattern) | 10001 | C |
| app | `claim-matcher/pg-client.crt` | claim-matcher | PG 客户端证书（链） | CN=nexloop_domain_worker | 10001 | A |
| app | `claim-matcher/pg-client.key` | claim-matcher | PG 客户端私钥 | CN=nexloop_domain_worker | 10001 | A |
| app | `commercial-recorder/service-credential` | commercial-recorder | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `commercial-recorder/dsn` | commercial-recorder | DSN（verify-full） | role nexloop_domain_worker; its password takes effect when role-passwords.sql runs (bootstrap window) | 10001 | A/B |
| app | `commercial-recorder/pg-client.crt` | commercial-recorder | PG 客户端证书（链） | CN=nexloop_domain_worker | 10001 | A |
| app | `commercial-recorder/pg-client.key` | commercial-recorder | PG 客户端私钥 | CN=nexloop_domain_worker | 10001 | A |
| app | `commitment-keeper/service-credential` | commitment-keeper | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `commitment-keeper/dsn` | commitment-keeper | DSN（verify-full） | role nexloop_domain_worker; its password takes effect when role-passwords.sql runs (bootstrap window) | 10001 | A/B |
| app | `commitment-keeper/pg-client.crt` | commitment-keeper | PG 客户端证书（链） | CN=nexloop_domain_worker | 10001 | A |
| app | `commitment-keeper/pg-client.key` | commitment-keeper | PG 客户端私钥 | CN=nexloop_domain_worker | 10001 | A |
| app | `domain-worker/service-credential` | domain-worker | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `domain-worker/dsn` | domain-worker | DSN（verify-full） | role nexloop_domain_worker; its password takes effect when role-passwords.sql runs (bootstrap window) | 10001 | A/B |
| app | `domain-worker/pg-client.crt` | domain-worker | PG 客户端证书（链） | CN=nexloop_domain_worker | 10001 | A |
| app | `domain-worker/pg-client.key` | domain-worker | PG 客户端私钥 | CN=nexloop_domain_worker | 10001 | A |
| app | `gateway/server.crt` | gateway | 服务证书（链） | SANs: gateway | 0 | A |
| app | `gateway/server.key` | gateway | 服务私钥 | SANs: gateway | 0 | A |
| app | `agent-host/guard-ca.crt` | agent-host | CA bundle | the intermediate CA bundle (intermediate + root) | 10020 | A |
| app | `runtime-worker/guard.crt` | runtime-worker | 服务证书（链） | SANs: guard | 10001 | A |
| app | `runtime-worker/guard.key` | runtime-worker | 服务私钥 | SANs: guard | 10001 | A |
| app | `runtime-worker/guard-transport.key` | runtime-worker | 密钥（64 位十六进制） | 32 bytes as 64 lower-case hex | 10001 | A |
| app | `shared/host-ca.crt` | shared | CA bundle | the intermediate CA bundle (intermediate + root) | 10001 | A |
| app | `agent-host/server.crt` | agent-host | 服务证书（链） | SANs: agent-host | 10020 | A |
| app | `shared/host-control.key` | shared | 密钥（64 位十六进制） | 32 bytes as 64 lower-case hex (one value: host-control) | 10001 | A |
| app | `agent-host/server.key` | agent-host | 服务私钥 | SANs: agent-host | 10020 | A |
| app | `agent-host/transport.key` | agent-host | 密钥（64 位十六进制） | 32 bytes as 64 lower-case hex (one value: host-control) | 10020 | A |
| app | `incentive-issuer/service-credential` | incentive-issuer | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `incentive-issuer/dsn` | incentive-issuer | DSN（verify-full） | role nexloop_domain_worker; its password takes effect when role-passwords.sql runs (bootstrap window) | 10001 | A/B |
| app | `incentive-issuer/pg-client.crt` | incentive-issuer | PG 客户端证书（链） | CN=nexloop_domain_worker | 10001 | A |
| app | `incentive-issuer/pg-client.key` | incentive-issuer | PG 客户端私钥 | CN=nexloop_domain_worker | 10001 | A |
| app | `message-relay/dsn` | message-relay | DSN（verify-full） | role nexloop_api; its password takes effect when role-passwords.sql runs (bootstrap window) | 10001 | A/B |
| app | `message-relay/executor-credential` | message-relay | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `message-relay/pg-client.crt` | message-relay | PG 客户端证书（链） | CN=nexloop_api | 10001 | A |
| app | `message-relay/pg-client.key` | message-relay | PG 客户端私钥 | CN=nexloop_api | 10001 | A |
| app | `message-relay/planner-credential` | message-relay | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `message-relay/route-credential` | message-relay | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `message-relay/source-credential` | message-relay | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `outbound-recorder/service-credential` | outbound-recorder | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `outbound-recorder/dsn` | outbound-recorder | DSN（verify-full） | role nexloop_api; its password takes effect when role-passwords.sql runs (bootstrap window) | 10001 | A/B |
| app | `outbound-recorder/pg-client.crt` | outbound-recorder | PG 客户端证书（链） | CN=nexloop_api | 10001 | A |
| app | `outbound-recorder/pg-client.key` | outbound-recorder | PG 客户端私钥 | CN=nexloop_api | 10001 | A |
| app | `shared/db-ca.crt` | shared | CA bundle | the intermediate CA bundle (intermediate + root) | 10001 | A |
| app | `plan-reevaluator/api-dsn` | plan-reevaluator | DSN（verify-full） | role nexloop_api; its password takes effect when role-passwords.sql runs (bootstrap window) | 10001 | A/B |
| app | `plan-reevaluator/service-credential` | plan-reevaluator | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `plan-reevaluator/dsn` | plan-reevaluator | DSN（verify-full） | role nexloop_domain_worker; its password takes effect when role-passwords.sql runs (bootstrap window) | 10001 | A/B |
| app | `plan-reevaluator/executor-credential` | plan-reevaluator | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `plan-reevaluator/pg-client.crt` | plan-reevaluator | PG 客户端证书（链） | CN=nexloop_domain_worker | 10001 | A |
| app | `plan-reevaluator/pg-client.key` | plan-reevaluator | PG 客户端私钥 | CN=nexloop_domain_worker | 10001 | A |
| app | `plan-reevaluator/planner-credential` | plan-reevaluator | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `plan-reevaluator/queue-credential` | plan-reevaluator | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `plan-reevaluator/source-credential` | plan-reevaluator | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `recall-indexer/service-credential` | recall-indexer | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `recall-indexer/dsn` | recall-indexer | DSN（verify-full） | role nexloop_domain_worker; its password takes effect when role-passwords.sql runs (bootstrap window) | 10001 | A/B |
| app | `recall-indexer/pg-client.crt` | recall-indexer | PG 客户端证书（链） | CN=nexloop_domain_worker | 10001 | A |
| app | `recall-indexer/pg-client.key` | recall-indexer | PG 客户端私钥 | CN=nexloop_domain_worker | 10001 | A |
| app | `reply-guarantor/api-dsn` | reply-guarantor | DSN（verify-full） | role nexloop_api; its password takes effect when role-passwords.sql runs (bootstrap window) | 10001 | A/B |
| app | `reply-guarantor/service-credential` | reply-guarantor | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `reply-guarantor/dsn` | reply-guarantor | DSN（verify-full） | role nexloop_domain_worker; its password takes effect when role-passwords.sql runs (bootstrap window) | 10001 | A/B |
| app | `reply-guarantor/executor-credential` | reply-guarantor | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `reply-guarantor/pg-client.crt` | reply-guarantor | PG 客户端证书（链） | CN=nexloop_domain_worker | 10001 | A |
| app | `reply-guarantor/pg-client.key` | reply-guarantor | PG 客户端私钥 | CN=nexloop_domain_worker | 10001 | A |
| app | `reply-guarantor/planner-credential` | reply-guarantor | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `reply-guarantor/route-credential` | reply-guarantor | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `reply-guarantor/source-credential` | reply-guarantor | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `retention-keeper/service-credential` | retention-keeper | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `retention-keeper/dsn` | retention-keeper | DSN（verify-full） | role nexloop_domain_worker; its password takes effect when role-passwords.sql runs (bootstrap window) | 10001 | A/B |
| app | `retention-keeper/host-control.key` | retention-keeper | 密钥（64 位十六进制） | 32 bytes as 64 lower-case hex (one value: host-control) | 10001 | A |
| app | `retention-keeper/pg-client.crt` | retention-keeper | PG 客户端证书（链） | CN=nexloop_domain_worker | 10001 | A |
| app | `retention-keeper/pg-client.key` | retention-keeper | PG 客户端私钥 | CN=nexloop_domain_worker | 10001 | A |
| app | `runtime-worker/service-credential` | runtime-worker | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `runtime-worker/dsn` | runtime-worker | DSN（verify-full） | role nexloop_scheduler; its password takes effect when role-passwords.sql runs (bootstrap window) | 10001 | A/B |
| app | `runtime-worker/pg-client.crt` | runtime-worker | PG 客户端证书（链） | CN=nexloop_scheduler | 10001 | A |
| app | `runtime-worker/pg-client.key` | runtime-worker | PG 客户端私钥 | CN=nexloop_scheduler | 10001 | A |
| app | `shared/signing.key` | shared | 签名密钥（32 字节原始） | 32 raw bytes; the hex copy is registered by the trusted configuration | 10001 | A/B |
| app | `webchat-delivery/server.crt` | webchat-delivery | 服务证书（链） | SANs: loopback | 10001 | A |
| app | `webchat-delivery/service-credential` | webchat-delivery | 随机令牌 | random token; registered by the trusted configuration after bootstrap (see the document §3) | 10001 | A/B |
| app | `webchat-delivery/dsn` | webchat-delivery | DSN（verify-full） | role nexloop_action_worker; its password takes effect when role-passwords.sql runs (bootstrap window) | 10001 | A/B |
| app | `webchat-delivery/server.key` | webchat-delivery | 服务私钥 | SANs: loopback | 10001 | A |
| app | `webchat-delivery/pg-client.crt` | webchat-delivery | PG 客户端证书（链） | CN=nexloop_action_worker | 10001 | A |
| app | `webchat-delivery/pg-client.key` | webchat-delivery | PG 客户端私钥 | CN=nexloop_action_worker | 10001 | A |
| app | `webchat-delivery/provider-credential` | webchat-delivery | 随机令牌 | shared between the action worker and webchat-delivery | 10001 | A |
| data | `postgres/bootstrap_password` | postgres | 口令 | POSTGRES_PASSWORD_FILE of the first start | 999 | A |
| data | `postgres/server.crt` | postgres | 服务证书（链） | SANs: data | 999 | A |
| data | `postgres/server.key` | postgres | 服务私钥 | SANs: data | 999 | A |
| data | `postgres/ca.crt` | postgres | CA bundle | the intermediate CA bundle (intermediate + root) | 999 | A |
| data | `valkey/valkey.acl` | valkey | Valkey ACL | no application user yet: `user default off` | 999 | A |
| data | `valkey/server.crt` | valkey | 服务证书（链） | SANs: data | 999 | A |
| data | `valkey/server.key` | valkey | 服务私钥 | SANs: data | 999 | A |
| data | `valkey/ca.crt` | valkey | CA bundle | the intermediate CA bundle (intermediate + root) | 999 | A |
| data | `pgbackrest/pgbackrest.conf` | pgbackrest | 配置 | NX-035 runbook 2.3: rendered by the owner with the repository passphrases and object-store keys | 999 | manual |
| data | `pgbackrest/pgpass` | pgbackrest | pgpass | nexloop_bootstrap (run sheet 2.4) and nexloop_backup (2.5b) lines | 999 | A/B |
| data | `backup/recorder.dsn` | backup | DSN（本机 socket） | role nexloop_backup_recorder over the DATA_HOST socket; password from role-passwords.sql | nexloop-backup | A/B |
