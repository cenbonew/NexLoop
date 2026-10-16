# 回复兜底提速（reply fail-fast）与 tool_choice 评估

状态：后备方案（分支 `l2/reply-failfast`），等 stage 实测 `e32322d`（Run 指示）的效果后再决定是否合入。

## 1. 问题

10/16 stage 冒烟中，一个 deepseek-flash 消息 Run 只回了一段文本，没有调用 `nexloop.service.request`。文本不会投递给任何人，但 Run 仍记为 `succeeded`。reply-guarantor 要等到消息接收后 300 秒（`start_after_seconds`）才会接手。

## 2. 做法

- **迁移 0200**（临时编号，由调度员合入时重编），新增函数 `authz.nexloop_message_run_reply_settle(p_digest, p_world, p_run)`：
  - 用调用方自己的服务会话确定租户；
  - 判断这个 Run 是不是消息 Run（`runtime.nexloop_message_routes`）或兜底回复 Run（`runtime.nexloop_fallback_context_bindings`）；
  - 判断它有没有提交过 `nexloop.service.request`（`nexloop_effect_submissions` 关联 `nexloop_effect_intents`）；
  - 没有提交时，把该消息的 `reply-due` 待办（0109）的 `available_at` 提前到现在。它只会往前移，不写别的任何东西，也不代替模型生成回复。
  - 执行权授给 `nexloop_scheduler` 和 `nexloop_domain_worker`（runtime-worker 的两种连库角色）。
- **`runtime_dispatch.py`**：
  - Host 报告 Run 结束（succeeded、failed 或 cancelled）后调用上面的函数，这一步尽力而为，失败不影响队列结果；
  - 对消息 Run 或兜底回复 Run，如果原本是 succeeded 但没有提交回复，任务改判为 `failed`，结果码为 `runtime_no_reply_submitted`。
  - 失败或取消的 Run 状态不变，同样会让 reply-due 立即到期。
- **兜底 Run 的指示**：兜底回复 Run 与消息 Run 走同一个 Host，在 deepseek-flash 下同样会带上 `e32322d` 的 `RUN_INSTRUCTIONS`，回复也只能经 `nexloop.service.request`。兜底 Run 同样没有提交时，这个函数会让待办立即到期；guarantor 看到“已有兜底 Run”，按原规则上报负责人（`fallback_unanswered`）。

## 3. tool_choice 评估

- **DeepSeek 一侧**：它的接口与 OpenAI Chat Completions 兼容，文档列出 `tool_choice` 可取 `none`、`auto`、`required`，也可以指定某个函数。思考模式下是否支持没有核实，需要在 stage 用真实密钥做一次请求验证。
- **Pi 一侧（冻结版本）**：
  - `pi-ai` 的 `openai-completions` 适配器支持 `toolChoice`（`api/openai-completions.js` 第 613 行起，把它写进 `params.tool_choice`）；
  - 但 `pi-durable` harness 传给每次生成的只有 `ConversationStreamOptions`，其中没有 `toolChoice`（`harness/types.d.ts` 第 303 行起），按会话也没有可以设置它的地方。
  - **所以不改冻结的 Pi，就无法传入 tool_choice。**
- **即使能传，也有问题**：
  - 好处：强制模型调用工具，从根上消除“只写文本”。
  - 坏处 1：harness 的设置作用于整个会话的每一轮。`required` 会让模型在拿到工具结果后还得再调一次工具，没有正常结束的路径，只能耗到轮数或工具调用上限，Run 以 `runtime_budget_exhausted` 失败。
  - 坏处 2：指定函数（`nexloop.service.request`）同样会每轮都调用。重复调用虽然返回原收据，不会重复投递，但会浪费轮数和成本。
  - 坏处 3：计划重评 Run 要用 `nexloop.plan.outcome`，不能共用同一设置。
  - **只在第一轮强制**需要 harness 支持按轮设置，属于改冻结 Pi 的范围，要单独立 ADR。
- **建议**：先用 `e32322d` 的指示加本分支的 fail-fast；如果 stage 实测仍然常见只写文本，再立 ADR，评估“只在第一轮强制调用工具”的 Pi 改动。
