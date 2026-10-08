# JsonExportDelivery 自主恢复候选：代码已实现，未执行 PG

Root 要求 manifest-only accepted 能在明确当前权限下继续，而不是一直依赖 operator。以下方案已在候选模块/0051 未注册 SQL 实现，供 Root 独立复核；不能算 PG 授权闭环交付。

## 接受证据与恢复发现

私有 manifest 增加有界原 `parameters.message` 和 provider profile digest，保留原 intent/payload digest/product hash/size。只保存已由 POST 的完整当前 SEND guard 验证的交付请求；它是外部服务技术幂等/产物证据，不记录另一套 Consumer/Goal/事实或 bearer。首次 accepted manifest 完整写/fsync 后才发布 export。旧 manifest 没有参数时保持明确 blocked，不猜内容、不从别的 Run 获取。

服务启动单独 bounded recovery loop，每轮最多4条、固定扫描上限/分页 cursor；只认同私有根严格 UUID manifest、owner600/regular/no symlink/完整 canonical/digest，处理 manifest 存在而真实 export 缺失的记录。GET 不唤起此 loop、不提供参数、不执行重建。SIGTERM 停止新恢复，收尾当前短事务/文件操作；与 HTTP handlers 共用同根每-intent flock，进程 owner lock 阻止两份自主 loop。

## 精确原意图的 lease

现 0042 `claim` 在 QUERY 模式允许针对明确 intent 领取已有 attempt 的过期 effect lease。恢复 worker 可通过可信内部 EffectExecutionPort 同协议领取自己的 pinned executor 原 intent，不能用 hint 扫出并领取不属于本服务 manifest 的其它工作；live lease 时跳过，不抢别人的 lease。只有当前 QUERY 在该阶段能做技术领取，不能写产物。

POST 的已提交 attempt 可能已经被 query observation 标为 provider_accepted，或 effect fence 已因合法 reclaim 改变。现0051 deliver要求原 dispatching attempt/current fence/active Action lease，因此不可直接扩大为“有 QUERY 就恢复”。

## 两阶段真实恢复授权

0051 增加专用 `recover_reserve`（仍未注册草稿）：

1. 当前完整 executor SEND proof、真实 stored SourceRun 完整 SEND proof、current Consumer/Goal/Step/Control/委托/link/共享一次 reservation、immutable provider profile、当前确实 owned 的 effect fence/lease。
2. 外部 accepted manifest 原参数必须等于原 frozen_request.parameters，payload digest 和 provider reference 绑定原 intent；只允许 dispatching/unknown/provider_accepted 的已存在 attempt，无 observation fulfilled/terminal，不制造新 provider attempt或新 POST。
3. 用实际 ActionGovernor + 原 EIOS `nexloop_action_claim_command` 完整签名 reserve 原 intent/stable binding，保留原 executor principal；冻结 Governor 对真实 in_progress 拒绝签发 permit，恢复等待原 active claim 过期后才取得真实 claimed 新 revision/fencing token。不能硬改旧 claim expiry，不能伪 claimed。
4. 该短事务同时保存受 FORCE RLS/tenant-world compositeFK 保护的技术 recovery marker（准确 attempt/effect/Action triple），recover_deliver 必须匹配该已提交 marker；仅更新技术 attempt 的当前 effect/Action fence与 revision，并保留原 provider key/payload/origin/profile，不重复预算 reservation；末完整 Source+executor+plan/双 lease/TTL核验。真实 reserve 和 technical marker先 commit，尚未写产物。

随后重新生成当前 proof，通过0051独立 `recover_deliver` guard（固定原 intent/current新 fences，禁止字段猜测），持短 PG锁写固定真实文件，复用该阶段同一签名 proof 作尾核验。任何权限/目标/SourceRun/lease失效均不新建文件。文件存在但尾核/commit失败保持 unknown；不删产物、不假称无效应。

恢复 guard区别正常 POST：只对已接受的原参数在真实 new Action permit下完成同一个本地服务工作；不能将既有 attempt 普遍标可重发。仍不发第二个 HTTP POST。

## 完成与查询

恢复写成功后，同 executor 使用当前独立 query port 记录真实 fulfilled observation；原 currentSource+executor SEND仍有效时，既有实际两阶段Governor/finalize原Action路径原子形成 governed success。若随后失权，最多 observed_fulfilled false；不借 query permission finalize。这样主 effect Worker随后GET会看同 intent实际文件，且无需重新派送。

窄 terminal GET仍需当前 QUERY+pinned executor/profile+真实原terminal succeeded claim/receipt+fulfilled attempt/outbox，不要因文件有了绕过原GovClaim证明。provider只返回真实文件状态；业务 success由 EIOS 原claim决定。

## 必须实际执行的门禁

- 真正 SIGKILL在manifest durable/export前；服务自主重开，同 intent恢复文件，Provider POST总计1；原预算 reservation1；真实Humanreceipt可验证。
- 失效SourceRun/撤Source SEND/executor仅QUERY/staleGoalStepConsumerControl：loop可查询/技术领取，但 export不存在、无新执行claim成功、无 business true。
- live其它凭据lease/旧fence/expiredpermit/锁等待超TTL：不能reserve或重建；reserve与attempt技术更新整事务rollback。
- reserve commit后再kill：重启查同原claim binding/currentrevision，无新intent/预算/重复产物。
- 文件已真实生成后PG停机/尾checkfail：unknown，恢复GET原产物，source有效可真实finalize；失效仅observedfulfilled。
- arbitrary参数/路径/其它profile/无acceptedmanifest/旧缺参数manifest：拒绝；GET不会triggerSEND。

此设计明确需要0051扩展及真实PG/EIOS测试，不能只靠纯文件测试或 mock permit。目前独立 loop、原 intent QUERY reclaim、真实 Governor callback reserve、双 SEND guard、同 proof 尾核、无 POST 文件恢复与实际 query observation/finalize调用都已候选实现。旧 manifest 缺参数仍 accepted/blocked；PG/实际服务重开与完整授权尚未验证。

离线实际命令：
```sh
PYTHONPATH="$PWD/docs/tmp/nx016-candidate/packages/eios-core/src:$PWD/docs/tmp/nx016-candidate/tests" .venv/bin/python -m pytest -q docs/tmp/nx016-candidate/tests/test_local_json_delivery_files.py docs/tmp/nx016-candidate/tests/test_local_json_delivery_transport.py
```
结果 23 passed in 4.49s；首次恢复新增文件用例为 23 passed in 4.50s，无失败。旧19例同轮回归 19 passed in 4.42s。新增真实 SIGKILL只证明 manifest 参数/profile持久、GET无生成、文件恢复候选发现与分页，不能证明 PG Governor permit 或后台自主成功。未注册0051，不改 catalog/versions/planning；不能计 NX016/017 done。


## GET 零持久写审查修复

独立源审发现原 query 会 O_CREAT .lock，并清理 nlink2 staging 文件（unlink+fsync）。新实际快照测试先执行得到 4 failed,17 deselected in 0.83s：not_found/orphan 查询新增锁，两个真实 SIGKILL hardlink 查询改变 namespace/nlink。修复 query 使用已有只读 shared flock；无锁时只读不可变快照，绝不 O_CREAT；nlink2 只验证 owned exact staging，不删除/fsync。discovery scan 同样只读；实际清理由完整当前授权 guard 下 delivery 的 immutable replay完成。重跑文件+TLS 27 passed in 5.25s。快照断言目录mtime、全部文件内容/dev/inode/mode/nlink/size/mtime不变（OS read atime不算业务写）。PG仍未执行。
