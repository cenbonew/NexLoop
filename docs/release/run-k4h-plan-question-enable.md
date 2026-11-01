# 运行单草稿：K4h 打开计划 Run 的提问（planQuestion），stage

- **状态**：草稿。**不在 stage 执行**，等负责人点头后由调度员按 ack-policy 安排。
- **适用**：分支 `l4/k4h-plan-question-config`（基于第 101 批 91caf276）。Host 代码改动，没有迁移，也没有改 compose。
- **开关**：私有的 Host 运行配置 `agent-host.runtime-config.json`，键 `plan_question_tool`。
  - 和 `plan_outcome_tool`、`outreach_tool` 一样走 Host 运行配置。v7 的另一半在计划重评估服务上，是 `NEXLOOP_CONTEXT_V7`，属于另一个进程。
  - 默认没有这个键，就是关闭。
  - 只接受 `true`，并且要求 `plan_outcome_tool: true`、`effect_tools: true`、`context_input_protocol: nexloop.context-pack.v7`，否则 Host 拒绝启动（`runtime configuration refused`）。
- **生效范围（负责人：上下文先 C 后 A；Q2 先只给计划 Run）**：只有本计划自己的重评估 Run，并且这次 Run 的包声明为 v7，才会拿到带 `question` 的 1.1 结论工具。
  - 消息 Run、v6 的计划 Run、Role 或兜底 Run，仍然只拿到 1.0 工具。
  - 这些 Run 如果提交 1.1，Host 在发请求之前就以 `invalid_run_outcome` 拒绝，`nexloop_human_questions` 不会被写入（`apps/agent-host/test/plan-question-config.test.ts` 钉住）。
- **执行者**：调度员。**判定者**：L6。证据只记计数、id 前缀和协议串，不展示问题或回答的正文。

## 打开前核对（全部满足才继续）

1. K4c 的第 1–4 步已完成（`docs/release/run-k4c-context-v7.md`）：Host 的 `context_input_protocol` 已改为 v7；声明 v7 的策略版本已经发布；计划重评估服务已设 `NEXLOOP_CONTEXT_V7=plan`。
2. 最近有计划 Run 用的是 v7 包：
   `ro "select protocol, count(*) from runtime.nexloop_context_packs where tenant_id='<TENANT_ID>' and created_at>now()-interval '24 hours' group by 1"`，结果里应有 `nexloop.context-pack.v7`。
3. 问题表是空的：
   `ro "select count(*) from runtime.nexloop_human_questions where tenant_id='<TENANT_ID>'"`，结果应为 0。
4. K4d 读接口与 L9 首页的问题卡片已在同一批，L6 已复审通过。
5. 迁移前备份已完成（本步没有迁移，但同批如果有迁移，照常先备份）。

## 打开

1. 生成新的配置文件：`jq '.plan_question_tool=true' <原文件> > <同目录临时文件>`。文件权限 0600，属 Host 用户，然后 `mv -f` 覆盖原文件。
2. 只看键，不贴文件内容：`jq -r '[.effect_tools,.plan_outcome_tool,.plan_question_tool,.context_input_protocol]|@csv' <文件>`，预期为 `true,true,true,"nexloop.context-pack.v7"`。
3. `up -d --no-deps agent-host`。Host 日志里没有 `runtime configuration refused`。
4. 只读核对：
   - 消息 Run 照常回复。
   - 下一次计划 Run 之后，`ro "select state, count(*) from runtime.nexloop_human_questions where tenant_id='<TENANT_ID>' group by 1"` 只会出现 `open` 等合法状态。是否提问由模型决定，0 行也是正常结果。

## 回退

1. 去掉这个键：`jq 'del(.plan_question_tool)' <原文件> > <临时文件>`，然后 `mv -f` 覆盖，再 `up -d --no-deps agent-host`。之后所有 Run 都只拿到 1.0 工具。
2. 已经登记的问题和回答留在表里：到期（7 天）后由 keeper 置为 expired，或在计划关闭时置为 superseded。不手工删除。
3. v7 可以保持打开（`run-k4c-context-v7.md` 的回退是另一组步骤）：没有新问题时，v7 包只是带着旧回答。
