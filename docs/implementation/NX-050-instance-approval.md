# NX-050 object_instance 候选审批与回流创建（开发线 L2）

状态：实现与定向测试完成，**未标 done**。分支 `nx050-instance-approval`，从 `nx044-close`（`6633b36`）切出。临时迁移 `0100_nx050_instance_approval.sql`（接在 NX-044 的 0099 之后，合并时统一重编号）。契约未改。

依据：
- ADR-019 §3.3 决定 3：有强标识的实例自动创建，只有名称的实例必须经审核。
- ADR-019 §3.5：审核粒度是候选定义，批准后依赖 Claim 自动应用。
- ADR-020 §3：发布不写授权，授权只经可信配置。
- 调度员决定（2026-10-10）：NX-044 按方案 (a) 交付，(b) 拆为本任务；不改候选契约。

## 1. 闭环（AT-067 走到“已应用”）

以新类型 Pet 为例：
1. 审核批准类型（NX-044），同时发布 Pet v1 和 `Pet.create:1` / `Pet.edit:1`。
2. 依赖 Claim 改回 unresolved，登记到 claim-match feed，以重匹配代次 1 重新匹配。
3. 匹配器发现 Pet 还没有标识属性（`title_property` 为空，也没有 `name`）。此时**先生成该标识属性的候选**（`name`，string，分组 other）进审核，不直接生成实例候选；否则这个实例候选永远无法批准。
4. 审核批准属性 `name`：Pet v2，附带后继 Action `Pet.create:2` / `Pet.edit:2`。回流时，消费者 Claim 照旧按 NX-044 的确定性方式应用；实体 Claim 则交回四层链（release），以代次 2 重新匹配。
5. 匹配器再次判定：只有名称的实例生成 `object_instance` 候选，identifying_properties 为 `{"name":"布丁"}`，进审核。
6. 审核批准实例（本任务新增），同一事务内：
   - 门槛：类型最新 Schema 已发布全部标识属性；存在当前生效、绑定该版本的 `<Type>.create`；没有同标识的对象；没有其他审批占用同一标识。
   - 候选转为 published，决定写入 `published` 和 `reflow_status=pending`，并登记 `ontology.nexloop_instance_approvals`。
   - 不写正式对象，不写授权（授权指纹门槛）。
   - 审计记录中的 publication 只含契约字段。
7. 服务回流经受治理的 `<Type>.create` 创建对象：
   - 幂等键由决定 ID 派生，同一决定只会创建一次。
   - 没有 create 授权时，回流记为 `waiting`、原因 `awaiting_grants`，不写入，不报错；授权就绪后再创建。
   - 创建后 `instance_created` 绑定对象，并核对对象确实带有已批准的标识。之后释放依赖 Claim，进入代次 3。
8. 之后凡是“仅名称”的匹配，若命中已批准且已创建的同一标识，就直接定位到该对象，不再生成候选。属性层照常处理：
   - 名字 Claim：值已是当前值，结果为 noop，Claim 变为 resolved。
   - 品种 Claim：`breed` 是新属性，进审核。批准后释放，再次匹配时写入 `breed=英短`，结果为 applied。

## 2. 实现

迁移 0100：
- `ontology.nexloop_instance_approvals`：一条批准的标识对应一个对象；`(type_name, dedupe_key)` 唯一；对象只能绑定一次。表启用强制 RLS。
- `ontology.nexloop_review_instance_gates`，失败原因包括：
  - `identifying_property_not_published:<k>`、`identifying_value_invalid:<k>`、`identifying_properties_invalid`
  - `type_create_action_unavailable`
  - `instance_already_exists`、`instance_already_approved`
  - `instance_type_unavailable`、`invalid_type_reference`
  - `simulation_or_shadow_candidate_cannot_create_real_instance`
- `authz.nexloop_review_decide`：只接管“批准 object_instance”这一种情况，执行与 0082 完全相同的人类会话、签名、授权、重放和 CAS 校验；其他情况原样交给 `nexloop_review_decide_v0082`。
- `authz.nexloop_read_claim_matching`：新增读动作 `approved_instance`。
- `authz.nexloop_review_reflow`：
  - `pending` 增加 `instance_object_id`；
  - 新增 `instance_created`；
  - `release` 支持类型、实例、属性和词表值四类决定（`release_type` 保留）。属性和词表值只释放实体 Claim，并且不改动 0082 的回流状态。

Python：
- `claim_matching.py`：
  - 先发布标识属性的规则；
  - 已批准实例的复用；
  - 模型给出的“新属性”若当前 Schema 已发布，就按已有属性处理。
- `candidate_merge.py`：发布后，实体 Claim 不再做确定性重指向，交给 release 处理。
- `review_actions.py`：object_instance 不需要构造发布内容；`ReviewReflowWorker` 新增实例分支（创建、绑定、释放），属性和词表值发布后释放实体 Claim。

新函数的 search_path 都是 `pg_catalog, pg_temp`，`scripts/check_definer_search_path.py` 结果为 0 个问题。

## 3. 部署要点（可信配置；发布与审批都不授权）
- 回流服务主体（部署中即 `claim_matcher`）需要 `eios:action:<Type>.create:<N>` EXECUTE，才能创建已批准的实例。
- 匹配服务需要：类型 READ；属性定义 READ；新对象的对象级和属性级 READ/EDIT（可用 0084 的 `property_access_rules` 按类型派生）；以及 `<Type>.edit:<N>` EXECUTE（可声明 follow_latest_version）。
- `match-config.json` 需要随类型的版本更新到对应的 create/edit 版本（同 NX-044 §6.2，尚未自动跟随最新版本）。

## 4. 局限
- 实例批准时记录的是**当时最新**的 create Action。如果之后又有属性发布、Schema 前进，回流用旧版本创建会失败并保持 `waiting`。需要重新审核，或另行实现“按最新版本创建”。
- 新类型的强标识路径仍不存在（NX-044 §8.6）。

## 5. 测试（合成数据；`LANG=en_US.UTF-8 PYTHONPATH=packages/eios-core/src:tests uv run --frozen pytest -q …`）
- `tests/test_instance_approval_pg.py`（4 项）：
  - `test_approved_instance_is_created_by_reflow_and_dependent_claims_apply`：AT-067 闭环到 applied；授权未就绪时不写入、不报错；创建只发生一次；同一标识复用、不再生成候选。
  - `test_rejected_instance_creates_nothing_and_keeps_claims_as_evidence`
  - `test_duplicate_identity_is_refused_and_the_candidate_stays_pending`
  - `test_instance_approval_requires_published_identifying_property_and_create_action`
- NX-044 的链路测试 `test_review_type_actions_pg.py::test_approved_type_hands_claims_back_to_the_full_matching_chain` 的最后一步，在本分支上改为“先审核标识属性”（规则见 §1 第 3 步）。
- 开发中的首次失败：
  - reflow 函数中局部变量 `cause` 与列名冲突，已改名；
  - 声明的 Claim 措辞召回不到类型（测试数据问题，改了措辞）；
  - 换一个服务主体再跑回流时会重复尝试创建，因而记为缺授权：`pending` 增加了已绑定对象，绑定后不再创建；
  - 第二次回流的断言写错。
- 回归：`test_bootstrap test_db_boundary test_claim_store_pg test_instance_approval_pg test_review_type_actions_pg test_review_decisions_pg test_review_http test_review_workbench_pg test_candidate_merge_pg test_claim_matching_pg test_work_feeds_pg test_recall_pg test_property_grant_derivation_pg test_grants_follow_latest_pg test_background_services_pg`，118 passed。
