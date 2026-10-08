# 本地真实 JsonExportDelivery 候选：实际 PG 已验证

正式 `nexloop.service.request:1` 的已接受 `parameters.message` 通过完整当前 SourceRun+executor EIOS SEND 授权，生成固定私有目录中的真实 JSON 文件。固定 format/title、稳定 intent/payload digest、原 message、实际 hash/size 都可读回核验；不是模拟 provider receipt，也不声称正式 Artifact 对象、第三方渠道或真实模型验证。

入口 `python -m nexloop_eios.local_json_delivery` 使用显式私有 DSN/signer/executor/TLS/provider配置文件，无环境 fallback，启动不迁移或创建权限。origin 固定可信 HTTPS127.0.0.1 numeric endpoint；HTTP 请求不可选择路径、URL、Run 身份或 shell。首次 POST 的独立 executor 同时需要 QUERY 元数据准备与 SEND，provider transport credential 不授业务权。受限 role 为 `nexloop_action_worker`。

候选0051已由 Root 串行注册，旧1–50未修改。`nexloop_local_delivery_authority` 完整签名/currentproof、actual tenant/world/principal、pinned executor、当前 Consumer/Goal/PlanStep/Control/delegation/Runlink、原预算 reservation、immutable provider profile、Action/effect fence/lease 与 canonical frozen params全核验。正常 POST 文件写前/后在同短 PG transaction 复用相同 signed proof；尾核失败保留真实文件但 HTTP fixed503/业务unknown，不伪造成功。

私有 root mode700、逐操作 inode/device、nofollow/regular/euid/mode600、boundedbytes，原 intent UUID固定文件名、nonblockingflock、manifest先fsync、真实export temporaryfsync/hardlink no-replace/dirfsync。GET 只打开已存在 readonly lock（不存在不创建），只验证真实不可变快照；合法 owned nlink2 staging只验证，不unlink/fsync。actual directory/file content/inode/nlink/mode/size/mtime快照不变量已执行。清理仅实际授权 delivery 的 immutable replay进行；未知hardlink拒绝且不删除。

manifest 额外保留已验证原 parameters/profile，旧格式无参数不能恢复，不猜材料。独立 daemon 每轮最多4条、至多16候选、目录10000硬限、cursor与私有进程ownerlock。GET不唤起写。先 currentQUERY精确原 intent/reclaim已有attempt，active原Actionclaim前置检查避免常规不必要占lease；此无锁检查只是优化，并发refresh仍可能使真实Governor拒绝后技术lease暂占，绝不假称消除全部竞态。

恢复 reserve 通过实际原 ActionGovernor callback 与原 signed `nexloop_action_claim_command`，同 original stablebinding，真实 IN_PROGRESS 拒绝执行，等原 lease过期才 genuine CLAIMED 新 fence。完整 Source+executor SEND、currentplan/reservation/profile/双lease核验后，同 PG transaction 更新技术attempt并保存FORCE RLS/tenantworld compositeFK recovery triple，先 commit，再 fresh current `recover_deliver`相同proof前后guard生成同原文件；不新intent/attempt/quota，不发第二个 POST。实际 QUERY observation及原 dual-proof原子finalize决定业务成功；失权只能false/observed，不借QUERY finalize。

窄 terminal GET仅当前QUERY+pinnedexecutor/profile+准确原terminal succeededclaim/outcome receipt+fulfilledattempt/outbox一致才免lease，只读历史真实产物。非terminal仍ownedlease。关停停新recovery、drain已承接工作和HTTP，再关闭Backend/PG/store；TLS8pre-handshake slots、固定deadline、boundedheader/body/framing、固定异常无凭据/日志原文。

实际证据见 `local-json-delivery-pg-evidence.json`：最薄positive1passed2.41s；真实manifest/product两个SIGKILL+独立CLI恢复3passed12.55s；当前撤权/expiredlease/sameproof尾过期等9passed37.86s；reserve尾过期整rollback+reserve已commit/file前再SIGKILL及真实30秒lease恢复2passed43.04s。共11不同 actualPG测试通过（9+2分批，不冒充一次11suite）。首次失败均原样记录，包括错误测试import、50正确拒绝改冻credential、CLI测试字段误写。SQLSHA d475cadb8a165c4089d39394f7c18852c3efe18010afe2621ae66f6fc3d15849。

离线文件/TLS此前27passed5.19s，含先4failed证明GET持久写再修复。此次PG所有正式对象由真实50publication/initialHuman和GovernedActions建立，未直写业务SQL、无fakeauth。技术fault只fixture自有PGDATA/technicaltrigger/Runexpiry；独立CLI正常SIGTERM返回0且stdout/stderr为空。未计NX016/017done，待Root统一CI与消息/Browser/Runtime整合验收；无外部模型/第三方凭据验证。
