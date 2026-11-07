# ADR-045｜测试顾客的模型调用故障开关（stage 专用）

- **状态**：提议（负责人 2026-11-07 选定方案；待 L6 复核设计 `docs/design/synthetic-model-fault.md`）
- **依据**：NX-038/NX-039 stage 实测（AT-023、AT-048、S6-R1/R5/R7）缺少"失败且带检索未命中"的合成 Run；run-s6-stage-prep A3（负责人 10-22 失败口径 A，10-29 不改口径）；ADR-034（测试人设）；ADR-023（每条来信都有回复）；ADR-043（stage 专用开关的先例）

## 1. 背景

- NX-038 的归因只把 outcome 为 error 或 reply_fallback 的 Run 当作失败（attribution.py:92-96）。
- stage 上的真实模型几秒内就会正常回复，所以合成顾客的对话没有失败结局，验收的前提 P5 无法满足。
- 已经试过、但不成立的办法：
  - 参考端点延迟演练：不明结果会被查询和对账收敛，Run 照样成功（2026-11-07 实测 ca29349d）；
  - 把对账也弄失败：这违背"不明结果先对账"。
- 负责人 2026-11-07 选定：给测试顾客加一个模型调用故障开关，失败口径不变。

## 2. 决定

1. **故障点在 Agent Host 发起模型请求之前**：
   - 命中时，这一次模型调用直接以模型错误结束，Run 记 `model.failed > 0`；
   - 不调用任何工具，不发任何效应，不产生不明结果，因此也没有要对账的东西。
   - 失败口径不变：这些 Run 按现行规则算作 error 失败。
2. **只对测试顾客生效，由服务端判定，只在 Run 首次 start 时判定一次，resume 沿用首次结果**：
   - 在 Run 准入（guard admission，SQL）时判定。persona 来自数据库里 Consumer 的受治理属性（ADR-034 的 `nexloop.consumer.set_persona`，`runtime.nexloop_consumer_persona`），不来自顾客输入，也不来自上下文包；
   - 只有 persona 为 `synthetic` 或 `simulated_agent`，并且 Consumer 在本次开关的名单里，才会命中；
   - persona 为 `real` 的 Consumer，在任何配置下都不会命中。
3. **开关由负责人经受治理 Action 打开和关闭，带审计，并会自动失效**：
   - Action 为 `nexloop.synthetic_fault.set:1`，只有负责人能执行，取值为 open 或 close；
   - 打开时必须给出 Consumer 名单（1–8 个）、剩余次数 N（1–50）、有效时长（最长 2 小时）；
   - 次数用完或到期，开关自动失效，不需要人工关闭；
   - 每次打开、关闭、命中、失效都写入只追加的审计表。
4. **生产隔离**：真正的依据是**生产库没有这个 Action、开关表是空的**，每次生产发布后只读确认（L6）。下面两道只是补充：Host 的运行档案限制挡不住生产（生产也可能是 deepseek-flash 加 real），check_publication 也看不到私有的运行时配置。
   - 具体做法：
     - Action 只出现在 stage 专用的发布清单里；
     - Host 只有在运行时配置写了 stage 专用键 `synthetic_model_fault: true` 时，才执行准入应答带来的故障标记；没有这个键时，带标记的准入一律拒绝；
     - check_publication 新增一条规则：stage 部署文件之外，任何公开文件出现这个键或这个 Action 都拒绝（同 ADR-043 的做法）。
5. **这是对 ADR-034 §2 第 4 条（"persona 不改变受治理路径"）的一个受控例外**：
   - 只在负责人打开开关、并且在有效期内才生效；
   - 只让测试顾客的模型调用失败，不放宽任何授权、联系限制、预算或派发约束；
   - 回复保证照常工作。

## 3. 理由与代价

否决的替代：
- **参考端点延迟演练**：不明结果会被查询和对账收敛，Run 不失败（2026-11-07 实测）。
- **同时让对账失败（`--get-unavailable-seconds`）**：违背"不明结果先对账"，而且是用"不明"演练去伪造失败结局。
- **改失败口径**：负责人 10-29 决定不改。
- **把测试顾客路由到必然失败的模型配置**：模型档案是整台 Host 的设置，没有按 Consumer 路由的入口；改用 simulation 世界又不再是真实的 Run。

代价：
- 真实世界里故意出现的模型失败，会进入错误类指标和告警。处理办法见设计 §5：在快照里单列演练期间的注入次数。B5 的失败率不受影响（0418 的 outcomes 本来就不含模型失败的 Run），不做扣减；B5 的证据里注明开关打开的时段。
- 准入 SQL 要多一次判定，名单为空时几乎没有开销。

## 4. 影响

- 迁移 0423（临时号）：开关表、审计表、Action 的 SQL 入口、准入函数的重定义（登记 copy guard）、快照块。
- Agent Host：新增一个运行时配置键，模型请求前加一处检查。
- stage 发布清单：新增这个 Action。
- 新增 check_publication 规则。
- 运行单：`docs/release/run-synthetic-model-fault.md`（草稿）。
- 契约不变：准入应答是内部的 SQL、守卫、Host 通道，不在 `packages/contracts` 里。

## 5. 回滚

- 用 close 关闭开关，或者等它自动失效；
- 从 stage 运行时配置删掉 `synthetic_model_fault` 键；
- 迁移不回退：没有打开的开关时，准入判定不会命中任何 Run。
