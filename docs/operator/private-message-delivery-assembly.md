# 私有消息交付装配（0053 候选，尚未完成整头验收）

本文装配现有实际接口，不代表正式部署或 product_ready。以独立数据库和显式私有配置为前提；不得对原生产实例 bootstrap/migrate。所有口令、DSN、签名材料、TLS key 和 Run vault 均为服务账户所有，目录 0700、文件 0600，不进入 Git、argv 值、日志、prompt、Host SQLite。公开 manifest 只有声明；service-secrets JSON 是私有文件。下列大写变量仅代表文件路径或公开配置引用，不代表凭据值。

## 1. 发布技术配置与建立真实 Human

使用已完成 clean bootstrap 的独立环境。先核验 release manifest、迁移 checksums、受限角色及备份。50 发布器只发布 typed schema/Action/Function、authority facts、服务元数据、浏览器 app map/rate policy 和初始身份额度；它不会创建业务对象。

```sh
python -m nexloop_eios.trusted_configuration --manifest "$MANIFEST" --check
python -m nexloop_eios.trusted_configuration --manifest "$MANIFEST" --apply \
  --database-url-file "$CONFIGURATOR_DSN_FILE" --signing-key-file "$SIGNING_HEX_FILE" \
  --signing-key-id "$KEY_ID" --service-secrets-file "$SERVICE_SECRETS_FILE"
python -m nexloop_eios.initial_identity --manifest "$HUMAN_MANIFEST" --create \
  --identity-database-url-file "$IDENTITY_DSN_FILE" --password-file "$PASSWORD_FILE" \
  --idempotency-key-file "$IDENTITY_SEAL_HEX_FILE"
```

初始身份额度必须由已授权 operator 显式配置。manifest 的 expected_revision 必须来自技术配置操作的真实 receipt；冲突不覆盖。Signing HEX 文件用于技术发布器，Backend signing 文件是同一密钥的原始 32 字节私有文件，两者不能互换。Human 由真实 initializer 建立，后续 login/password evidence/session 都使用真实身份目录；不能把 Human 包装成 Service。

技术配置须包含真实已发布 Consumer/EffectControl/Goal/PlanStep/MessageAssignment/Conversation/Message/ConsumerOwnership schemas，create Actions、control/context registrar、queue、message route、service request/query Actions，以及 48 的 service receipt Function。每个调用主体须分别有当前真实 grants/application/scope/policy/controls；权限清单不是从用户消息推断，也不是因为存在某 definition 就自动得到授权。

## 2. 治理业务初始化

私有 setup recipe 严格只有 schema_version、稳定 UUID request_id、已存在 Human principal_id、control 的 budget_units 与 UTC valid_until。Owner 和 Executor 分别从私有文件重新 authenticate；仅真实 Governor 能创建业务对象。

```sh
python -m nexloop_eios.business_setup \
  --database-url-file "$API_DSN_FILE" --signing-key-file "$SIGNING_RAW_FILE" --signing-key-id "$KEY_ID" \
  --owner-credential-file "$OWNER_CREDENTIAL_FILE" --executor-credential-file "$EXECUTOR_CREDENTIAL_FILE" \
  --recipe-file "$SETUP_RECIPE_FILE" --artifact-root "$SETUP_ARTIFACT_ROOT"
```

输出 consumer_id/control_id、ownership_id 和 expected_consumer_revision/expected_control_revision。这些是已提交治理操作的预期 revision，不声称当前 revision。各步独立提交；相同 recipe 重试复用稳定 IDs，变更 payload 冲突不会覆盖。可选 --verify-current 需要额外真实 instance READ 与 property grants；默认流程不会自行授权 READ。后续 relay 必须再次核验当前 revision。

## 2.1. 正式供给目录与 Source READ

独立数据库须通过精确 catalog 登记 0053。技术配置先显式发布 ServiceOffering、ConsumerServiceOffering schemas 及对应 CREATE/EDIT Actions；这些是维护者配置的固定应用类型，不是模型候选审核。目录维护主体与 delivery Source 必须是同 tenant 的不同真实服务主体。目录 CLI 不发布 schema、不授予权限、不伪造 Human。

```sh
python -m nexloop_eios.catalog_setup \
  --database-url-file "$API_DSN_FILE" --signing-key-file "$SIGNING_RAW_FILE" --signing-key-id "$KEY_ID" \
  --maintainer-credential-file "$CATALOG_MAINTAINER_CREDENTIAL_FILE" \
  --source-credential-file "$SOURCE_CREDENTIAL_FILE" \
  --recipe-file "$CATALOG_RECIPE_FILE" --artifact-root "$CATALOG_ARTIFACT_ROOT"
```

私有 recipe 严格只有 schema_version=1.0、稳定 UUID request_id、已治理 Consumer 的 consumer_id 和 UTC valid_until。CLI 经真实 Governor 创建免费本地 JSON 供给与该 Source 的 Consumer 关联；相同请求复用 IDs，不同 payload 不覆盖。输出 configured 仅表示登记，dispatch_authorized=false；不是 Consumer 存在、当前授权或可交付证明。

拿到 offering_id/binding_id 后，由可信技术配置者通过既有 manifest 发布流程显式配置 Source 对这两个正式对象及 12+5 个属性的 READ，以及原有 service EXECUTE/Artifact CREATE+READ。凭据和 application digest 必须匹配当前配置；重新 authenticate 后才签发新 Run。Source 不得获得目录 CREATE/EDIT。relay recipe 配置 offering_id 和 offering_binding_id；实际生产器检查当前绑定、revision、有效期和 Source READ，不能用登记 receipt 跳过授权。目录维护者的撤权、SKU 失效或 revision 改动必须导致当前模型/工具/发送拒绝。

## 3. TLS API 与短生命周期 Runtime

现有 http_api CLI 只接受 `--mode test`，尚不是正式生产 profile launcher。create_app 可装配真实 BrowserConfiguration，API 仍提供真实密码登录、受治理消息写、PG SSE replay 与 Human Function receipt。其 /health/ready 故意保持 product_ready=false，并列出 host_dispatch 缺口；健康探针不能当全链成功证明。

```sh
python -m nexloop_eios.http_api --mode test \
  --database-url-file "$API_DSN_FILE" --signing-key-file "$SIGNING_RAW_FILE" --signing-key-id "$KEY_ID" \
  --artifact-root "$API_ARTIFACT_ROOT" --port "$API_PORT" --web-root "$WEB_ROOT" \
  --tls-certificate-file "$API_CERT_FILE" --tls-key-file "$API_TLS_KEY_FILE" \
  --identity-database-url-file "$IDENTITY_DSN_FILE" --browser-rate-key-file "$BROWSER_RATE_HEX_FILE" \
  --browser-tenant-id "$TENANT_ID" --browser-application-id "$BROWSER_APP_ID" --browser-origin "$BROWSER_ORIGIN"
python scripts/agent_host.py --node "$NODE24" --runtime-root "$RUNTIME_ROOT" \
  --internal-key-file "$HOST_CONTROL_KEY_FILE" --port "$HOST_PORT" \
  --tls-certificate-file "$HOST_CERT_FILE" --tls-key-file "$HOST_TLS_KEY_FILE" \
  --runtime-config-file "$HOST_PRIVATE_CONFIG"
python -m nexloop_eios.runtime_worker \
  --database-url-file "$DOMAIN_WORKER_DSN_FILE" --signing-key-file "$SIGNING_RAW_FILE" --signing-key-id "$KEY_ID" \
  --service-credential-file "$DOMAIN_WORKER_CREDENTIAL_FILE" --artifact-root "$RUNTIME_WORKER_ARTIFACT_ROOT" \
  --world real --queue operations --host-origin "$HOST_ORIGIN" --host-control-key-file "$HOST_CONTROL_KEY_FILE" \
  --host-ca-file "$HOST_CA_FILE" --guard-port "$GUARD_PORT" --guard-key-file "$GUARD_KEY_FILE" \
  --guard-certificate-file "$GUARD_CERT_FILE" --guard-tls-key-file "$GUARD_TLS_KEY_FILE"
```

Host 私有配置只能选择已信任 runtime profile；Host 不持有 DB、Run raw token 或业务 delivery provider credential。缺 MODEL_API_KEY 时 deterministic profile 只提供测试证据，不证明真实模型调用。Node 必须 24；compiled entrypoint 必须匹配已测试 source manifest。0053 消息链须显式选择 context_input_protocol=nexloop.context-pack.v2；完整 pack 交给模型，不能仅抽出用户文本。Runtime root 单 OS owner，SQLite FULL/WAL；Pi done 仅 runtime_outcome，不代表业务交付成功。

## 4. 持续消息 relay 与 vault

relay 私有 recipe 包含初始化 receipt 得到的 consumer/control 及 expected revisions、明确 valid_until、固定 role/context/profile、预算、owner epoch、queue、正式 offering_id 与 offering_binding_id。不是所有消息共享一个静态 Plan：每条真实 Message 都经 Planner 创建独立 Goal、PlanStep、MessageAssignment；shared control 仅复用 Owner 已治理预算，不自动扩额。

```sh
python -m nexloop_eios.message_relay_cli \
  --database-url-file "$API_DSN_FILE" --signing-key-file "$SIGNING_RAW_FILE" --signing-key-id "$KEY_ID" \
  --route-credential-file "$ROUTE_CREDENTIAL_FILE" --source-credential-file "$SOURCE_CREDENTIAL_FILE" \
  --planner-credential-file "$PLANNER_CREDENTIAL_FILE" --executor-credential-file "$EXECUTOR_CREDENTIAL_FILE" \
  --artifact-root "$RELAY_ARTIFACT_ROOT" --vault-root "$RUN_VAULT_ROOT" --recipe-file "$RELAY_RECIPE_FILE"
```

PREPARED vault fsync 先于 PG digest issuance；稳定 Run/request 不因重启重建。PG 从不保存 raw token。期限最长 300 秒，排队前已过期必须 requires_governed_replan，不延 TTL 或换 Run 自动继续。队列已提交的恢复仅执行当前 route 授权下的 exact technical ACK；不得扩大原 Source 权限。vault 不能位于 Artifact/Runtime tree；丢失 prequeue vault 会 fail closed。

## 5. 真实 LocalJsonDelivery 与独立 Effect Worker

此服务产生实际本地 JSON 文件，不是 synthetic HTTP fixture，也不证明任何外部渠道。专用私有 delivery root 必须与 Artifact、Runtime、vault 分离。它的 Executor 身份及 provider credential 均为私有文件；每次请求真实授权，51 对提交时 authority/fence 再核验。

```sh
python -m nexloop_eios.local_json_delivery \
  --database-url-file "$ACTION_WORKER_DSN_FILE" --signing-key-file "$SIGNING_RAW_FILE" --signing-key-id "$KEY_ID" \
  --service-credential-file "$EXECUTOR_CREDENTIAL_FILE" --artifact-root "$DELIVERY_ARTIFACT_ROOT" \
  --delivery-root "$DELIVERY_ROOT" --origin "$DELIVERY_ORIGIN" \
  --certificate-file "$DELIVERY_CERT_FILE" --tls-key-file "$DELIVERY_TLS_KEY_FILE" \
  --provider-ca-file "$DELIVERY_CA_FILE" --provider-credential-file "$PROVIDER_CREDENTIAL_FILE"
python -m nexloop_eios.effect_worker \
  --database-url-file "$ACTION_WORKER_DSN_FILE" --signing-key-file "$SIGNING_RAW_FILE" --signing-key-id "$KEY_ID" \
  --service-credential-file "$EXECUTOR_CREDENTIAL_FILE" --artifact-root "$EFFECT_WORKER_ARTIFACT_ROOT" \
  --world real --provider-config-file "$EFFECT_PROVIDER_CONFIG"
```

provider config 是私有严格 JSON `{origin, credential_file, ca_file, timeout, connect_address?}`。LocalJsonDelivery origin 固定 numeric loopback HTTPS；与 Effect Worker 使用完全相同 origin/CA/connect profile。更换 credential 不改变 provider identity；更换 origin/CA/connect target 不能把旧 attempt 转发或查询到新 provider。已有 attempt 只能 QUERY 原 intent，未知结果不盲目 POST 重试。

## 验收与停止

从真正浏览器密码登录后接受 Message，消息 202 只证明 message/inbox/outbox 已提交。随后分别核对 47 message→稳定 Run/task、真实 Pi tool receipt、runtime terminal receipt（单 submission/FULL）、provider export 文件，以及 48 当前 Human 查询的同 intent/receipt `provider_state=fulfilled`、`governed_claim_finalized=true`、`business_action_success=true`。只检查其中一项不能宣称全链完成。

退出时先停止新消息与 relay claim，等待已持有 lease 的 Worker 有界收尾，再终止 Runtime/Effect Worker、Host/API/provider。SIGKILL 恢复必须依原 vault、PG lease/fence 与 SQLite，不删除旧证据。运行环境缺失权限/私有配置/TLS/模型凭据时只阻塞相关步骤；不改 SQL 业务数据制造成功。当前本文命令是实际入口装配说明，尚未整体执行；后续全链测试报告将单独提供实际命令与首次失败记录。
