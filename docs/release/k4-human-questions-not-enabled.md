# K4 人工问题：数据层合入，stage 不启用

- **状态**：K4a（契约 run-outcome 1.1、workbench-pending v1）与 K4b（迁移 human_questions、回答 Action `nexloop.human_question.answer:1`）合入 main 后，**stage 不启用**。等 context-pack v7（新增“员工回答”子区）同批上线后再启用。
- **依据**：L6 复审 H1（2026-11-01）。负责人尚未就上下文方案拍板，推荐先只交数据层（C），再出 v7（A）。
- **原因**：回答会唤醒计划复评，但 context-pack v6 已冻结，复评 Run 的上下文里还没有可以放“员工回答”的位置（只有 `evidence` 区能放用户陈述，而它的子区枚举是固定的）。在 v7 之前启用，Run 能提问，却看不到人的回答。

## 合入后的实际状态

| 部分 | 状态 | 说明 |
|---|---|---|
| 契约 `run-outcome` 1.1 | 已合入 | 1.0 不变，并用测试钉住 |
| Host 的 `nexloop.plan.outcome` 工具 | **不提供提问能力** | 开关 `planQuestion` 默认关闭，Host 配置里没有打开它的项。关闭时工具只提供 1.0，描述中不提问题，1.1 请求体在发出前就以 `invalid_run_outcome` 拒绝（`apps/agent-host/test/runtime-plan-outcome.test.ts` 钉住） |
| 落库函数 | 接受 1.1 | 没有 Run 能产出 1.1，所以不会登记问题 |
| 问题表、回答表、过期动词 | 已建 | 表为空；keeper 的 `questions_expire` 每个周期返回 `{"expired": 0}` |
| 回答 Action | 在目录与角色清单中 | business-actions 是整份发布的。下一次 stage 发布若带上本次清单，该 Action 定义会被一并发布，但没有问题可答，调用只会返回 `not_found`。若要求 stage 连定义都不发布，发布前需由调度员确认处理方式（运行 stage 发布前先问） |
| 工作台“待我处理”读接口（K4c） | 未实现 | 另派 |

## 启用条件（同批上线）

1. context-pack v7：`evidence` 下新增员工回答子区（归类为用户陈述，不写成事实），由绑定时的 SQL 重新推导；撤回或更正取最新 revision，擦除后显示“回答已擦除”。
2. Host 配置增加打开 `planQuestion` 的项，并且只对使用 v7 上下文的复评 Run 打开。
3. K4c 读接口与 L9 的“待我处理”页面。
4. L6 复审通过后，按发布流程在 stage 发布并试运行。
