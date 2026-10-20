# NX-042 T1：孪生定义与版本

**依据**：设计稿 `docs/implementation/NX-042-design.md`（分支 `l5/nx042-043-design`，提交 9154db7a）的 §2、§3.3、§7 T1 和 D1、D4、D5、D7，负责人已于 2026-10-20 按推荐定案。

**迁移**：`0275_nx042_consumer_twins.sql`，临时编号。原定从 0280 起，但本分支基于 6ba7151e，迁移只排到 0274，而 bootstrap 要求编号连续，所以用 0275。集成时由调度员统一重排。

**契约**：`packages/contracts/consumer-twin.schema.json`（`nexloop-consumer-twin/1`），并附示例和 README 说明行。

## 实现

| 项 | 实现 |
| --- | --- |
| **孪生版本** | `control.nexloop_consumer_twins` 每行是一个版本，内容包括：<br>• 假设清单：每条有 key、statement，kind 固定为 `hypothesis`；<br>• 决策层参数：每个行为都是区间 `low`/`high`，加上名义覆盖率 `coverage` 和它所依据的假设（D4，不用单个概率）；<br>• 生成器版本；<br>• 定义摘要；<br>• 校准：报告引用和状态。<br>版本号由服务端分配；同一个 request 重放会得到同一个版本，同一个 request 换了定义会被拒绝。 |
| **冻结** | 版本行和状态事件都只能新增，不能 update、delete 或 truncate。作废只能追加一条 `retired` 状态事件，并写明原因。 |
| **校准状态机** | `calibration_status` 只能是 `uncalibrated` 或 `calibrated`。CHECK 规定：标为 `calibrated` 必须带报告引用。报告引用的格式固定为 `twin-calibration:<sha256>`，指向 T5 的报告表。T5 能够证明报告通过之前，发布出来的版本一律是 `uncalibrated`，即使附了报告引用也一样。 |
| **NX-043 能否选用** | `runtime.nexloop_consumer_twin_selectable(tenant, twin, version)`：已发布、未作废、并且已校准，三条同时满足才返回 true。T5 之前恒为 false。 |
| **D5** | 两张表都没有 world 列，所以不在 S2 整体删除的目标范围内，孪生定义长期保留；孪生在 sim world 里产生的事件仍按 NX-041 D2 随 world 删除。SQL 严格校验定义的字段集合，消息、consumer_id 这类字段会被拒绝。假设的文字由负责人撰写，内容由负责人负责。 |
| **受治理 Action（D7）** | `nexloop.consumer_twin.publish` / `publish_consumer_twin` 和 `nexloop.consumer_twin.retire` / `retire_consumer_twin`。两者都是 human 类，只授予 owner，只能从 real world 发起，都会写审计事件。各清单已同步：business-actions、capabilities（hash 由 `write_hashes` 生成）、system-catalog-names、workbench-roles（owner，新增分组 `consumer_twins`）、`business_actions.PROFILES`、`goal_controls`（映射和方法）、`workbench_actions_http`（操作和错误前缀 `consumer twin`）、`workbench_roles`（owner 专用）、`commercial_fixture`、`test_business_actions`、`test_governed_entry_pg`；register 计数从 90 改为 92。 |

## 测试（`tests/test_nx042_consumer_twins_pg.py`）

- 契约示例本身有效，去掉 frozen 部分后的定义也能通过 SQL 校验；单个概率不能通过契约。
- 发布：
  - 第一个版本为 1，状态为 uncalibrated、published；
  - 同一个 request 重放得到同一个版本；同一个 request 换了定义被拒；换 request 得到版本 2；
  - 附了报告引用的版本仍为 uncalibrated；
  - 两个版本都不能被 NX-043 选用；
  - 不同租户各自从版本 1 开始。
- 冻结：update（包括把校准改成 calibrated）、delete、truncate 都被拒；没有报告引用的 calibrated 行违反 CHECK。
- 10 种不合契约的定义被拒：单个概率、low 大于 high、coverage 等于 1、引用了不存在的假设、kind 为 fact、没有假设、带真实内容字段、带 consumer_id、假设 key 重复、schema 版本不对。
- 作废：
  - 追加一条状态事件，版本行不变；
  - 同一个 request 重放不重复作废；重复作废、版本不存在、原因为空都被拒；
  - 作废后的版本不能被选用。
- 只有 owner 能发布：两个 Action 都是 human 类，只授予 owner；从 test 或 sim world 发起会被拒；应用角色对这两张表和两个处理函数都没有权限。
- D5：两张表没有 world 列，也不在 S2 的删除目标里；删除一个 sim world 后，孪生版本仍在。

## 限制

- 校准报告表和「报告通过」的判定属于 T5，现在只定了引用格式和状态机。
- 群体定义（§2 群体抽样）属于 T2，不在本切片内。
