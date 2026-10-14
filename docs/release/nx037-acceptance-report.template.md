# NX-037 受控真实闭环验收报告（模板）

> **客户为模拟、系统为真实。** 本次试运行的消费者由负责人操作的 AI 代理（Claude Code / Codex 通过浏览器操作）扮演。负责人是唯一的员工：工作台接管回复和工单标记由负责人本人或负责人操作的代理完成。部署、网关、DeepSeek 真实模型、企业端点与 WebChat 都是真实的。交互记录与证据包标注 `persona: simulated_agent`；按客户行为得出的结论（满意度、话术效果等）不适用于真实客户。

- 试运行 ID：`<TRIAL_UUID>`；日期：`<DATES>`；提交：`<COMMIT>`；发布清单：`<RELEASE_ID>`
- 运行入口：`run_interactions.py --driver real`；证据包：`<EVIDENCE_DIR>`（`index.json` 的 `real_evidence` 应为 true，`interactions/summary.json` 的 `persona` 为 `simulated_agent`）

## 1. 授权与参与者

- 负责人操作授权：sha256 `<AUTHORIZATION_SHA256>`（文件私有保存，不随报告分发）
- 参与者（consumer id、kind、persona）：见证据包 `interactions/participants.json`

## 2. 结果

| 项 | 结果 |
|---|---|
| 交互数（按类型） | `<BY_KIND>` |
| 结局（ok / error） | `<BY_OUTCOME>` |
| 停止 / 暂停及原因 | `<STATUS_AND_RULES>`；恢复确认：`<RESUMES>` |
| 费用（CNY）：触发 Run 合计 / 计入合计（期间实际增量） | `<COST_TOTAL>` / `<COST_COUNTED>` |
| 单 Run 最高费用 | `<MAX_RUN>`（上限 2） |
| 结果不明的效应：均已查询对账 | `<RECONCILIATION>` |
| 企业交付工单与端点记录一致，无重复 POST | `<ENTERPRISE>` |
| 告警（critical / warning） | `<ALERTS>` |

## 3. 人工补偿项的执行记录

- 单 Run 成本人工查看：`<WHEN_AND_WHO>`
- 告警值守：`<SHIFTS>`
- 录入器存活核对（第一次交互后回复已进入会话）：`<CHECK>`

## 4. 限制

- 客户为模拟：代理的提问方式与真实客户不同，不能据此评估客户体验。
- 测试账号只在证据里区分：交互记录与证据包标注 `persona: simulated_agent`，测试账号列在 `participants`。系统内（数据库、工作台、导出）没有测试人设标记（v0.1 不做，后续项需要 ADR），后续分析须按 participants 清单排除这些账号的数据。
- `<OTHER_LIMITATIONS>`

## 5. 结论

`<VERDICT>`（负责人签署：`<OWNER_REF>`）
