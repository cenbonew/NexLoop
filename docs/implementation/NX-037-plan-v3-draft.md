# NX-037 交互计划 v3 草案（待调度员审定）

依据：负责人 2026-10-14 的变更（ROADMAP §7）：客户由负责人操作的 AI 代理扮演，可以扩展场景。草案阶段只有本文档，没有新增计划文件，运行入口也没有改。审定后再生成 `nx037-interaction-plan.v3.json`，并相应扩展 schema 与加载器。

## 1. 原则

- **五个交互类型不变**：`ordinary_reply`、`real_delivery`、`restricted_after_refusal`、`human_takeover`、`unknown_reconciliation`。停止规则、暂停规则、预算上限（累计 100 / 单 Run 2 / 月 300 CNY）都按类型判定，保持不变。
- **新增"变体"**：每次交互除了类型，再带一个 `variant`，写给扮演客户的代理和员工侧代理看（指令文本），执行器的判定不受它影响。
- **总数从 30 增加到 36**：10/18–19 两天跑完，每天 18 次，在 10:00–18:00 窗口内平均约 27 分钟一次。

## 2. 建议配比（36 次）

| 类型 | 次数 | 变体（次数） |
|---|---|---|
| ordinary_reply | 16 | single_question 单问（8）；long_conversation 长对话，6–10 轮（4）；vague_need 模糊需求，需要澄清（4） |
| real_delivery | 6 | direct_order 明确下单（4）；vague_then_order 先模糊、澄清后下单（2） |
| restricted_after_refusal | 5 | single_refusal 一次拒收（2）；repeated_refusal 拒收后再来信并再次拒收（2）；refusal_then_question 拒收后又来问问题（1，验证 0190：绑定回复可以发出，不算违规） |
| human_takeover | 5 | takeover_reply 接管后员工回复并交还（3）；takeover_after_delivery 交付后接管，员工侧代理在端点标记工单（2） |
| unknown_reconciliation | 4 | post_delayed 端点延迟应答（3）；get_unavailable 查询暂时不可用（1，drill `--get-unavailable-seconds`） |

## 3. 需要审定的问题

1. **账号分组**：拒收会让该消费者受限，受限后企业交付在派发时会被拒（效应类别 v3：customer_contact）。建议：
   - 拒收类 5 次使用 2 个专用账号，这 2 个账号不再用于交付和结果未知类；
   - 其余类型共用 3–4 个账号。
   - 计划的 `consumers` 需要相应按变体或按次数指定账号。
2. **长对话的观测**：RealDriver 目前只观测本次交互的第一条来信及其 Run，长对话里后续几轮的 Run 不会单独判定。
   - 它们的花费已经由"期间实际增量"计入累计；
   - 但这几轮 Run 是否出错、是否发生拒收后联系，不在本次交互的判定范围内。
   - 两种做法：驱动改为观测本次交互内的全部来信（S–M）；或者接受这个局限，写入验收报告的限制说明。
3. **费用估计**：
   - 36 次交互，加上长对话额外约 25 个 Run，共约 60 个以上的 Run。按 deepseek-flash 每个 Run 约 0.2–0.5 CNY 估算，合计约 12–30 CNY，在累计 100 CNY 以内。
   - 校准阶段（10/17）的实测单次费用为准；80% 累计费用暂停线不变。
4. **schema 与加载器**：计划 v3 的 `order` 项从字符串改为 `{kind, variant, consumer}`。加载器同时接受 v2 和 v3；配比校验仍按类型计数。试运行配置的 `plan.total` 和 `mix` 需要随之出一个新版本 `nx037-trial.v3.json`，上限与规则字段不变。
