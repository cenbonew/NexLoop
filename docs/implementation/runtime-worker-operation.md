# 独立 Runtime Worker 操作说明

当前入口为 `nexloop-runtime-worker`，由 `nexloop-eios-core` wheel 安装。这里是私有配置与启动说明，不代表已经在双机 stage 部署；实际验证使用独立临时 PGDATA、合成身份和本地确定性 Pi。

## 前置条件

数据库必须已经按验证流程准备好当前 NexLoop lineage；本入口不 bootstrap、不迁移、不建立身份或权限。SQL login 必须是无 elevated membership 的 `nexloop_domain_worker` 或 `nexloop_scheduler`。EIOS service credential 必须具有当前队列权限；每个已接收 Run 的原始 Agent/Service 身份和权限仍独立复核，不借用 Worker 权限。

Worker 与 Host 必须能访问同一私有 loopback HTTPS 网络空间。Host 先由 `scripts/agent_host.py` 获得现有 mode0700 本地 runtime 目录的 kernel owner lock；启用显式 mode0600 runtime JSON，将 guard_url 指向下面的 guard 端口，runtime_profile=deterministic-test。当前不安装业务工具或读取模型/渠道密钥。

配置文件由各服务所有者持有，regular file、拒绝 symlink/group/world 权限。DSN 与 service credential 是 UTF-8 私有文件；签名密钥沿用后端固定 key material 格式；Host/guard transport key 分开生成，各64位小写hex，不复用任何模型/业务凭据。TLS证书具有127.0.0.1 SAN，各CA/证书/私钥文件限32768bytes。Host挂载/环境不包含Worker DSN、签名密钥或service credential；需要共享的guard transport key/CA以各服务拥有的独立私有副本提供。

## 启动示例

以下路径必须由负责人配置为已验证的私有文件，没有环境变量回退：

```sh
nexloop-runtime-worker \
  --database-url-file /absolute/private/worker/database-url \
  --signing-key-file /absolute/private/worker/signing-key \
  --signing-key-id active \
  --service-credential-file /absolute/private/worker/service-credential \
  --artifact-root /absolute/private/worker/artifacts \
  --world real --queue operations \
  --host-origin https://127.0.0.1:8100 \
  --host-control-key-file /absolute/private/worker/host-control-key \
  --host-ca-file /absolute/private/worker/host-ca.pem \
  --guard-port 8101 \
  --guard-key-file /absolute/private/worker/guard-control-key \
  --guard-certificate-file /absolute/private/worker/guard-cert.pem \
  --guard-tls-key-file /absolute/private/worker/guard-tls-key.pem
```

`--once`执行一轮有界调度；默认持续轮询。stdout只报告固定ready信息和once状态摘要，不带Run/task/input/token/DSN。错误输出固定信息，退出码反映启动或once异常；持续模式每tick重新认证并保持失败关闭。私有文件轮转会在下一tick/guard请求读回，数据库撤权同样即时复核。

SIGTERM/SIGINT阻止新的claim，当前调度仍按原lease/fence收尾，随后释放guard/backend；不把停止信号变成新Run或盲目重发。HTTP请求有绝对deadline，PG已有10s statement、3s lock timeout；循环total_timeout不代表包含多条PG操作的函数具有严格总wallclock上界。

Worker失效后重新运行入口；新Host仍须先获kernel owner。PG reclaim重新校验当前许可并保留原Run/request/submission。0039记录的是曾获准model/tool执行，不是provider成功：marker=false且安全空存储可以初始化同Run；marker=true而文件丢失则记录技术失败并保留业务对账需求；orphan WAL/SHM保留拒绝重建。不要删除这些文件或伪造新的grant来使恢复变绿。

真实DeepSeek/provider及业务ActionGateway后续按NX-015～017推进；本入口结果明确scope=runtime_only、business_action_success=false，product_ready仍false。
