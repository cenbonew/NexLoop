# NX-015 governed context registrar：下一轮实施草案

本文件仅设计下一轮真实切片。head0040 完整 CI 正在运行期间，仅新增本文；不修改 source、SQL、catalog、tests、planning，不读取凭据或生产系统。0040 admission 已能持久受理，但生产没有 context registrar，不能把合成 context seed 当成生产业务闭环。

## 当前真实接入位置

`backend.py:AuthenticatedServices.create_object/edit_object/link_relation/read_object` 已接真实 EIOS port；`object_actions.py:GovernedObjectCreator.create` 读取 published Action/schema bundle、验证属性、取得真实 Governor permit，再签名调用受限 `nexloop_create_object_action`。重放仍保留原 Action claim 的 principal 比较。`object_edits.py` 是既有 expected_revision 编辑入口，不能用无条件更新替代。

`action_definitions.py:PostgresActionDefinitionReader.get_with_schemas` 是发布契约来源；不存在正式 Goal/PlanStep published schema 的证据，故不能直接假定名称即可运行。`0040_effect_intents.sql` 当前 context identity immutable，Run-context 关联引用真实 Run；受理仅核实际 Consumer revision。新增 Goal/Step/control authority 必须进入未来真实 SQL 检查，不仅放在 Python 或 expected_versions 审计 JSON。

`backend.py:AuthenticatedServices.submit_effect_intent/find_effect_receipt` 已调用真实 Run session；Runtime 不具有 context 写权限。`run_credentials.py` 的实际签发与 `authz.nexloop_run_credentials` 的 source digest/主体/Action资源，是登记关联必须核实的来源，不接受 caller 自报 tenant、principal、executor。

## 最薄业务模型与授权分工

正式发布 `Goal`、`PlanStep`、`EffectControl` 三个 `only_edit_via_actions` 本体 schema，保留 actual object UUID 和 `nexloop_revision`，不另建业务事实库。

- Goal 保存 Consumer 引用、目标状态与有效期；其身份是实际 object UUID，版本是实际 revision。
- PlanStep 保存所属 Goal/Consumer、执行种类 `nexloop.service.request`、稳定步骤语义、状态；只允许独立 authenticated planner 的 published create/edit Action。Run 的授权资源列表不得包含这些 planner/control Actions。
- EffectControl 保存 Consumer、当前允许效应、整数预算上限、有效期、固定 executor principal 引用与独立控制 revision。只有真实控制 owner 的 published create/edit Action 可改；planner 不能自行增加预算或更换 executor。

第一版一个 PlanStep 只允许一个固定语义槽 `service.request:primary`。registrar 从实际 PlanStep UUID + 固定服务器 Action 槽派生 stable slot，不接收 `slot_identity`、任意新 intent ID 或 tool-call ID。版本、Run 和重新规划请求不得生成新槽。需要第二次业务服务请求时，必须由独立 planner 的正式新 PlanStep Action 显式 supersedes，且先处理旧 unknown；不能在 Runtime 里随便换 step。

这能防止无 planner 权限的 Run 通过创建步骤绕重发；拥有 planner 权限者仍受控制 owner 的总预算与真实修订约束。不能把“planner 独立主体”本身当充分授权，仍需真实 EIOS grant、schema、published Action/current facts。

## 建议窄端口

```python
planner.bind_effect_context(
    plan_step_id, expected_step_revision,
    expected_goal_revision, expected_consumer_revision,
    expected_control_revision, run_token,
)
```

端口拒绝 Run session，只允许真实 authenticated planner service/session 的 `nexloop.plan.bind_effect_context:1` Action。raw Run token 仅 backend 内存用于实际 authenticate_run；不返回给 Host，不存表、prompt 或日志。外部只返回 opaque context_ref、版本与固定槽，不返回原始事实或密钥。

绑定 Run 必须：实际 tenant/world 一致；Run source principal 与正式 PlanStep 的受托执行主体一致；当前 source credential/release/profile/membership/Run TTL 仍有效；Run action_resources 包含当前服务 Action；运行消费者/目标/步骤上下文必须来自持久权威。不能凭已知 Run UUID 在数据库 lookup 后构造伪造 Run session。

executor 必须来自真实 EffectControl，核当前 EIOS registered service principal 与其当前 `nexloop.service.request` 权限；不能由 planner 参数、Run 或 ENV 切换。它不是 submitter 的别名；后续 Action claim 仍由这个真实 executor 身份持有，原 principal 检查不删。

## 0041 append-only 技术边界

新 `effect_contexts.py` 私有 proof prepare/execute，Backend 仅组装上述窄 port。新 0041 增 signed registrar definer，以及技术 context 对 Goal/Step/Control 实际 object ID/revision 的 FK 或验证字段；不改 0040 或旧 published checksum。

事务内先完整认证 planner Action proof，设置可信 tenant GUC；按统一对象 ID 排序锁 Consumer、Goal、PlanStep、EffectControl 的真实 ontology 行，验证类型、world、引用链、state、revision、valid_until 与 controls。使用 published schema/contracts，必须在 SQL 内重复核实际对象属性/关联和受支持 schema，不能只信签名前 Python。

随后验证真实 Run/source 与 pinned executor 的分别签名 current authority proofs；锁实际 Run 行及 stable context，检查同稳定身份已有登记。exact replay 返回同 context；同槽修改 Consumer/Goal/Step身份/executor 拒绝。控制修订更改不得无声替换已 accepted 的 frozen intent，后续 admission/dispatch 需 current control 审核；Goal/Step版本变更同槽仍不新建 intent，影响语义则 409。

context、Run关联、registrar Action terminal receipt 在同一短事务提交；提交后才 ACK。现有 GovernedObjectCreator 的 Governor reserve 可以独立提交，不能借它宣称 registrar 的所有账本原子：需私有共连接调用真实 signed claim/registrar，或明确 pre-reserved claim 与最终 registrar outcome 的恢复协议。首选真实同连接执行，不提供裸 pool 公共接口。

预算持久上限从 EffectControl actual revision 派生，quota 使用 owner 技术账本锁/CAS；跨多个 PlanStep 的 Consumer/control 总额度不能按 context 各自复制全额。0040 的 per-context budget 不足以证明全局预算，0041 必须加共享 control reservation ledger 或只允许单活跃槽，不能扩大宣传。

所有新 runtime 表 ENABLE/FORCE RLS，无应用直接 SELECT/INSERT/UPDATE；旧 inner functions revoke 所有应用 EXECUTE。取锁/写后重新完整 proof、Run TTL、validity、objects revision 和 PG 时钟检查；失败整事务回滚。Executor 当前撤权必须阻断登记或 dispatch，不能仅保存登记时 snapshot。

## 最小验收与明确缺口

真实 PG 上通过受限 Backend 正式 Actions 创建 Consumer/Goal/PlanStep/EffectControl；published schema/Action 发布采用独立治理 bootstrap，不把 fixture admin INSERT 当生产 registrar。然后真实 planner + owner + submitter Run + executor 四个有许可主体完成登记与 admission。

覆盖 Run 自报 slot/新step拒绝、非planner/rootrole拒绝、跨tenant/world/Run来源拒绝、executor不匹配/撤权拒绝、actual Goal/Step/Consumer/control revision改变拒绝、总预算并发不超额、锁等待后proof过期全回滚、同context跨Run重放、版本变化仍同槽409、应用直接表拒绝。成功证据只是正式登记与受理；没有 provider execution 不标 fulfilled/confirmed。

当前待解决的精确缺口：三种正式 schema及其发布治理接口；绑定登记的 actual planner Action permit 原子组装；Run受托主体在真实PlanStep schema中的权威字段；共享control总预算模型；executor当前proof与 dispatch再检查。0041实施应逐项装配并真实测试，不能以永久缺context的 failclosed 代替可运行业务入口。

## 本次实际只读命令

`rg/sed/cat` 读取 `backend.py`、`object_actions.py`、`object_edits.py` 路径、`action_definitions.py`、`0040_effect_intents.sql` 和既有 NX-015 design。一次候选 `object_mutations.py` 路径不存在，未据此推断 API。未执行测试、迁移、provider 调用或生产盘点。
