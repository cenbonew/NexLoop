# 持续 Message relay 与私有 Run credential vault（候选设计）

状态：Root 已在候选 head0049 注册；独立 PG/真实 CLI 共20项实际通过（49.90s），离线 vault/recipe/CLI 配置29项通过。合成故障与真实受控 HTTP provider 证据见 NX-016-message-relay-evidence.json。未合并主线，未部署真实渠道，不宣称生产上线。

## 当前真实边界

`run_credentials.issue_run_credential` 调用 0034 protected issuer：SQL 随机产生 Run UUID 对应 bearer、PG 仅保存 bearer digest，PG transaction commit 后 Python 才收到 bearer。进程可能在 commit 与私有文件落盘之间退出；当前 API 无法从 digest 恢复 bearer。重复调用会创建另一个 Run，不能作为恢复方案。TTL 由首次 PG `clock_timestamp()` 决定，最大 300 秒。

0047 `ConversationRuntimeBridge.bind_message` 已验证真实 SourceRun 对应当前 Consumer/Goal/Step/EffectControl。`deliver_one` 先 claim route，检查已提交 queue enrollment，存在时只技术 ACK；不存在时验证当前 SourceRun，再 `accept_runtime_event` commit，最后 fenced ACK。当前 bridge 只有调用方提供单个 bearer，既没有生产逐消息调度来源，也没有稳定凭据存储与根据实际 claimed message 查找 bearer 的入口。

## 0049 所需受保护协议（尚非已有 API）

新增独立 signed protocol，不改旧发行器。可信 API Service 经当前 published Action 与完整 EIOS facts 决策后提交固定 `message_id / run_id / request_id / issuance_nonce / token_digest / allowed_resources / ttl_seconds / source Plan refs`。raw bearer 绝不作为 SQL 参数。固定 message 只能对应一个发行记录；资源、Plan、消费者、request 或 digest 冲突拒绝。首次发行必须核对真实消息所属 Consumer、当前正式 Goal/Step/control 与该 Service 的 SourceRun 执行授权。发行记录与 run_credentials insert 同 PG transaction，末尾再次验证全部 current proof 与期限。相同记录只能读回首次 issued_at/expires_at，不更新到期时间、不补授权。

`lookup` 仅返回技术发行状态/固定 IDs/首次 deadline，仍要求当前 route Service proof，不得返回 token/digest。已过期的记录也可用于同一 message 的技术状态恢复，不因此获得新的 Action EXECUTE。不能将 caller supplied directory hash 或 Consumer 文本当作正式 Plan 绑定。

实际候选采用新正式 MessageAssignment 对象与 MessageAssignment.create 已发布 Action（需50provision登记），通过现有真实 services.create_object Governor路径建立。每message稳定intent独立创建Goal与PlanStep，复用已经owner治理配置的sharedcontrol且不扩大budget。生产调度必须有明确的受治理 PlanStep→Message 分配入口：当前 041 Plan/context 接口可绑定已存在 Run，但不能自己为任意聊天文本选 Plan。49 必须锁定当前实际 PlanStep/Consumer 与唯一 message assignment，注册后才能据此调用实际 planner `bind_effect_context`。每条消息一个 Run；不能复用静态 fixture SourceRun，也不能用浏览器文字推断资源、消费者或目标。尚无合法生产 Plan assignment 时 relay 不调度，输出固定 unavailable。

## 私有 vault 的写入顺序与 crash 矩阵

vault 独立私有目录（0700/euid/no symlink），不在 Artifact 或 Host runtime 目录。每条消息一条不可变记录，稳定键是 tenant/world/message_id 的 canonical SHA256。首次在进程间排他 flock 内生成 UUID Run、request、nonce 与 48-byte random bearer，写 PREPARED 0600 临时文件，fsync 文件，原子 rename，fsync 目录，然后才调用 49。vault 可含 bearer，但日志、异常、CLI argv、PG、prompt、Host SQLite 不可含 bearer。

- PREPARED 落盘前 crash：没有 PG 发行，可重新 prepare。
- PREPARED 已落盘、PG 未 commit：重启读同 record，用相同 digest/Run/nonce 再发行，重新验证真实当前权限。
- PG 已 commit、vault 未标 ISSUED：重启查询 signed lookup；若实际发行完全吻合，将首次 PG deadline 标 ISSUED。不得重新随机生成 token。
- vault ISSUED、Plan 尚未绑定：真实 current planner bind，原相同 Run/Plan exact replay；拒绝时保持未入队，不以 vault 作授权。
- route 已绑定、queue 未 commit：fresh Service + SourceRun currentproof + actual matching private bearer 才能接受。
- queue 已 commit、消息 ACK 前 crash：先 protected queue enrollment 检查，技术 ACK；不读取 bearer也可恢复，不重新执行 SourceRun。
- expiry 在入队前发生：固定 Run 记录标 expired（保留证据），message 待明确治理 replan/transition；不刷新 TTL、不换 Run、不把 pending 说成 queued。重新规划需新明确 assignment generation、旧 assignment 的终态与唯一约束迁移；首版不自动实现。
- queue 已 commit 后 expiry：只可技术 ACK；Worker 后续 current authorization 仍可能拒绝执行，不称自动续期。

vault 文件与 PG 不存在跨介质原子事务；PREPARED-first + digest-only stable PG issuance 是恢复协议，不是泛称 exactly-once。文件丢失/损坏时已发行 bearer 不可恢复：failclosed，不能重建一个新 Run 冒充原 Run。备份须保密并验证，首版不自动删除 private records。

## 持续 relay 入口

CLI 只读可信私有 DSN/signing/service credential/vault/受治理 assignment 配置，不读 `.env`，不由 Message/Run 提供。启动验证文件、目录与实际 restricted `nexloop_api` role 后才 claim；每次 poll/issuance/bind/deliver fresh authenticate。单 owner OS flock；vault条目锁使用LOCK_NB与monotonic短poll，最多1秒等待，暂停owner不会无限阻塞。SIGTERM 停新 claim，当前 bounded 操作收尾。固定 ready/status 日志，不输出 payload、URL/DSN、token 或 IDs。

49 需返回 claimed message 的安全稳定 vault key/Run ref，bridge 根据该项查询 vault，而不是把某条 bearer传给任意队列头部。应新增独立 relay admission port，不绕过 Backend lifecycle RLock 或真实授权服务。恢复查询失败只 unavailable；不得盲发新发行请求。

## 实际验收与后续边界

真实 clean PG + 独立 CLI：kill 在 PREPARED fsync 后 / PG commit 后 ISSUED 前 / context bind 后 / queue commit 后 ACK 前，均验证同 Run/request/task、无 bearer 在 PG/日志/Host 文件；fresh source/grant/Plan revoke 时零新入队；已 queuecommit 的技术 ACK 不授新 execution；超过 300 秒的 Run 不延长或改 ID；vault 丢失、权限077、symlink、损坏、目录 replacement、进程竞争均 failclosed。具体网络/PG kill checkpoints 仅合成测试进程，不对生产插入故障。


20项实际PG验证已包含四个SIGKILL窗口、0047/0049并发路由领取、真实SourceGrant撤权后的queue技术ACK、同Message贯通PiTool与HTTP效应/当前Human receipt。0050生产可信配置发布器尚待独立验收；当前证据不声称自动身份注册、生产部署或真实外部渠道完成。
