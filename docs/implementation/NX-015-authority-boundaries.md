# NX-015 权限边界与最薄 SQL 接口审查

2026-10-08，只读当前 extracted core 与目标契约。没有修改源码、SQL、planning，没有读取凭据/原生产或执行外部效应。现有接口不是完整Gateway，不把设计建议计为已实现。

## 四种权限不能合并

| 层 | 允许 | 必须拒绝 |
|---|---|---|
| Runtime受限工具 | 当前activation绑定的submit/find，返回脱敏权威receipt | 自选tenant/actor/world、外发凭据、直接SQL、任意Run lookup派生grant |
| 可信意图服务 | 从认证Run/可信业务上下文确定稳定intent，校验发布Action、目标与payload，短事务受理intent/outbox | 使用tool_call_id生成每次新业务键、借用户提交actor假扮人类、受理即宣称效应成功 |
| 受限effect worker | 自身有效service/agent许可及源意图约束下，独立Action lease/fence、dispatch前当前控制复核、持久attempt后外发 | 把queue/Run lease当Action执行权、假扮原主体、unknown blind retry、长PG事务包HTTP |
| query/reconcile | 当前独立query能力下查询同provider稳定key，持久证据与原receipt状态 | 查询权限升级成发送权限、用Runtime文本宣称provider已送达、以撤权后reconcile借机重新dispatch |

当前PostgresActionClaimPort.reserve/mark_retryable/finalize都解析真实当前session的Action EXECUTE许可，签名有界proof后调用protected SQL。0007及后续authority覆盖绑定principal/credential/world/fact revisions；当前principal不同即binding conflict。因此不能让API/Runtime先reserve，再让effect worker以另一个principal接管同claim。最薄可行模式是可信服务先受理intent/outbox，effect worker按自身真正获准的Action主体reserve；若业务必须使用源Run执行身份，需要显式、完整、当前的双主体delegation协议，不能靠伪造session或删除principal检查。

## 跨主体共享 receipt

共享“同一业务意图的权威结果”与共享“某主体的claim执行权”是不同权限。AT-037应让多个获准角色找到同可信业务键/intent并读同receipt，内部仍一个effect worker拥有claim与attempt fence。receipt read需当前tenant/world/consumer/goal/plan-step归属及独立Action/结果读取权限，先校验再允许terminal replay；不同主体仅知道intent UUID不足以读取参数、provider reference或原证据。未经授权的find与未知intent应使用一致脱敏错误，避免枚举。

canonical payload冲突必须在同业务键锁内比较，不因第二角色改actor/run/tool-call元数据而错误生成第二效应，也不能把真正不同消费者/目标版本的新意图合并。intent的业务参数digest与每次调用的认证/审计元数据分开，后者保留真实主体。新合法意图通过显式supersedes处理旧unknown，不自动换provider key。

## 当前消费者约束与预算缺口

govern_published_action从发布definition/capability读取，非空Action policy_refs与所需approval目前failclosed；空policy_refs不证明消费者禁触达、频次、静默期、负责人暂停/control revision、资源版本或业务额度已检查。当前RunCommand budget仅限制模型/tool次数、活跃时间与模型成本，不能当发送额度/消费者预算。NX-015需要实际PG消费者control与预算reservation，或明确选择不需这些条件的已发布Action并限制验收范围；不能使用任意客户端“allowed=true”或静态fixture替代dispatch时当前约束。

预算必须在资源锁内reserve，unknown不能自动refund再新发；provider确认未受理后才按明确政策释放/重试。受理与dispatch都读取当前control revision，等待锁后与commit前复查。simulation/shadow不可选real-effect provider凭据；test provider与真实服务交付证据分别报告。

## 建议最小受保护 SQL 端口

1. `intent_submit`：仅可信API，认证Run/源主体proof＋完整Action许可＋固定发布contract与上下文；tenant/world从proof派生。按可信business key固定顺序锁资源，冲突409，原子intent/outbox/control snapshot/budget reservation后commit才ACK。允许重投同参数返回原receipt，不对外发claim做换主体接管。
2. `intent_find`：API/受限查询服务当前read proof；校验同tenant/world及消费上下文归属，只返回allowlist状态/receipt IDs，参数与provider引用按需独立授权。
3. `effect_claim` / `effect_begin_dispatch`：仅action_worker，真实当前Action主体proof、当前source/intent binding、独立lease/fence/revision。持久provider adapter/target/key及dispatching attempt；未知或已有未决attempt只进入reconcile。最后授权/TTL/控制重验后commit，HTTP在事务外。
4. `effect_reconcile_claim` / `effect_record_query`：仅有明确query能力的受限worker，原attempt key不可替换；旧fence/CAS拒绝，查询证据受限写入。unknown→confirmed更新原receipt；unknown→safe_to_retry必须有协议可验证未受理证据，下一外发再次完整EXECUTE及当前controls授权。
5. `effect_finalize`：匹配attempt fence/revision与原claim，固定状态机、规范化相同terminal结果幂等重放/不同结果冲突；不允许client自定义principal、SQLrole或外部已完成状态。持久receipt/outbox同事务；查询失败继续unknown，不能伪造permanent_failure。

新表owner-only/FORCE RLS与显式tenant policy、全撤application直接DML，只grant必要definer EXECUTE；search_path=pg_catalog、row_security=on、严格NULL/type/长度/有界时间与签名parameters_digest绑定。采用既有完整Action authorization链，不自建轻量grant表替代；对新增source/query delegation扩展显式proof并在锁后/提交前重验。一个迁移owner，新迁移不改已发布checksum。

## 验证要求与本轮证据

真实PG验证两主体同intent共享receipt且仅一次provider send、未授权主体/跨tenant/world读取拒绝、源撤权/消费者control变更/预算不足时零外发、旧fence结果拒绝。独立durable HTTP provider验证accepted后本地kill恢复query原key、unknown不重发、只有确认未受理且当前许可有效才retry。PG不可达/缺control/query权时failclosed。受控provider故障证据与真实渠道交付分开，缺凭据只阻相关外部验收。

实际只读命令：`cat docs/handoff/docs/06_ACTION_RUNTIME_AND_RECOVERY.md`、`cat docs/implementation/NX-015-effect-source-audit.md`、`cat packages/eios-core/src/nexloop_eios/postgres_action_claims.py`、`cat packages/eios-core/src/nexloop_eios/action_governor.py`，以及rg/sed读取0007、ActionIntent/RunCommand与当前模块名。首次检索两个预期governance文件路径不存在，随后读取实际action_governor.py；没有执行测试或把预期接口当已有实现。

## 实际 HTTP provider 初版审查

已只读effect_provider.py。固定可信origin/config，不接受工具endpoint；生产只HTTPS且要求私有credential、可选私有CA，TLS>=1.2与证书校验；test HTTP仅显式127.0.0.1且禁止credential。HTTPConnection不读proxy、不跟redirect；provider idempotency key为原intent，payload canonical digest先本地检查。响应限定16KiB、严格framing与字段集合、重复JSON键拒绝、原intent/digest匹配、reference有界。POST不确定统一unknown，query失败独立unavailable，not_found仅证据且不能自行授retry。该adapter本身没有EIOS授权，必须由真实governed begin_dispatch后调用；未执行provider或测试。

已告Root具体deadline缺口：absolute socket timer在connect返回之后才建立，生产hostname的DNS getaddrinfo不受socket timeout控制，可超过声明deadline；剩余时间还在SSL context/CA读取前计算，connect使用旧预算。需要connect前重算，并明确有界DNS或可信预解析地址+保持TLS server_hostname校验。无法取消的无限后台resolver thread不能当作harddeadline实现。当前不能以固定loopback传输测试证明生产DNS路径绝对有界。

0040/effect_intents在本次检索尚未出现；未对不存在的实现给通过结论。待fresh文件审查stable context/slot、权威context登记、payload冲突与dispatch当前proof。

fresh effect_provider源码已覆盖原DNS/connect缺口：可信numeric connect_address或numeric origin必需，实际numeric socket连接跳过DNS；计时器在connect前建立，TCP与TLS前重算剩余timeout，TLS仍校验原origin hostname/SNI，保留wrapped socket给Connection-close响应截止。POST接受200/202，GET404仅允许strict not_found且reference/digest均null；accepted/fulfilled仍必须原intent/digest与有界reference匹配。未见新增具体秘密输出或redirect问题。TLS握手期间raw socket可能已detach，依靠wrap_socket继承的剩余sockettimeout提供握手边界，实际silent/trickle TLS测试仍应留证。本审查只读未执行；transport验证不等于governed Action或真实效应交付验收。

## 0040 intent admission 候选 fresh 审查

已读取draft SQL与effect_intents.py，不运行重叠PG suite。真实Run为前提；context不从Run输入选择，只有owner保护的Run-context映射，缺生产registrar明确failclosed。唯一业务键为tenant/world/consumer/goal identity/plan-step identity/slot/action，不含Run、tool call或contract/goal version；版本与参数在semantic digest中变化时同slot409，原intent/receipt不覆盖。两获准角色通过各自真实Run/context及当前完整Actionproof找到同receipt，既有0007 principal限制没有删除。

SQL先核HMAC/固定resource/payload digest/有界expiry及完整EIOS链，锁Run linkage/context与Consumer实际revision/发布definition；context与intent FORCE RLS按认证tenant，technical linkage/outbox非tenant RLS但直接application访问全撤。ctx行锁串行quota及stable slot受理，intent/outbox/submission/reserved_units同事务，最终完整proof/Run身份/TTL检查失败回滚。当前未发现可复核授权绕过或payload覆盖缺陷。

范围必须准确：Python可信端签名前按已发布input_schema检查参数，SQL校验签名、发布定义匹配、参数文本/JSON/digest一致，并没有独立SQL JSONSchema解释器。expected_versions数组仅纳入semantic，除Consumer实际revision外没有验证全部目标/goal/plan版本；当前消费者allow/budget依靠可信context行，不能声称通用消费者约束全部装配。后续registrar须真实治理登记、最新资源证明与固定锁序，不能将合成测试admin seed当生产方案。

此版只有submit/find admission，没有独立Action worker claim、dispatch时当前源/worker完整proof、provider attempt或unknown reconcile；不能凭受理证明AT-033/036通过。下一阶段实际begin_dispatch必须重查源/Run或已明确持久意图执行授权、当前context/control/预算/目标版本及workerAction权限，不复用过期受理proof；跨主体共享只限当前授权receipt读取，不授claim执行权。本审查只读命令cat/rg上述文件，没有执行外发或修改SQL/源码/planning。
