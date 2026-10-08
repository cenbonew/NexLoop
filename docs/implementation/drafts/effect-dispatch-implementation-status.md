# NX-015 后续代码状态（draft）

2026-10-08。head0040 full CI session12400仍是固定源码验证。这里的文件不在可加载migration目录/pytest collection内，未登记版本，不算已部署或验收通过。

实际代码：`effect_dispatch.py.draft` 实现fresh→PG dispatch admission→POST与orphan→query的控制流；POSTunknown/结果记账失败不重发；404保持unknown；只有PG真实治理完成与providerfulfilled一致时才能声明业务成功。它依赖尚未装配的真实ledger ports：claim_effect、prepare_effect_dispatch、authorize_effect_query、record_effect_observation、record_effect_unknown；不可用mock port替代验收。

`0042_effect_execution_ledger.sql.draft`定义effect attempt与独立provider observation、固定intent provider key、Action/effect双fence、tenant复合FK和FORCE RLS；observed_fulfilled不会推进治理完成。已修正fulfilled/confirmed必须等价governed_claim_finalized=true，防止仅按state误读。尚未定义protected command函数，因此不能入catalog或声称可用。

`0041_effect_context_registrar.sql.draft` / `effect_contexts.py.draft` / `test_effect_contexts.py.draft` 是Agent写的治理登记候选：实际Goal/Step/Control对象、独立planner/controlowner、真实Run/executor proof，同连接claim reserve→登记→finalize→最终复核，共享control预算。Step是实际既有EIOS64hex object_id而非UUID；槽由服务器固定。主体集合由正式PlanStep持久配置，每个Run独立获权，A/B共享意图不改变executor claim主体。此轮仅修代码与静态语法，不执行SQL/PG测试。

`test_effect_dispatch.py.draft` 使用实际持久HTTP故障服务的postcommit barrier与未来PG端口，拟覆盖Worker SIGKILL后sameintent GETonly/effect1、来源/控制撤权零POST、旧fence拒绝、query-only观察false业务成功。明确head40的synthetic executor字符串不是已注册真实执行主体，未来需真实governed registrar fixture。现未运行。

真实执行：对effect_dispatch/effect_contexts/test_effect_contexts/test_effect_dispatch四份Python draft使用Python compile进行语法检查；语法通过不等于模块导入/SQL有效/授权/故障恢复测试通过。公开扫描950paths/0index/0violations（此状态文档添加前一次记录）。完整实现尚需实际Backend组装、真实restrictedPG迁移/授权负例、lateexpiry/锁等待、真实Actionclaim atomic effect result、独立query权限和正式治理发布入口。未访问凭据、模型/真实渠道、生产系统，未add/commit/push。
