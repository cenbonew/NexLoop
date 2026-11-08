# planning 草稿：给开放输入模式的 Action 补真实输入模式（D23 方案 B）

- **状态**：草稿，只供调度员排期，不改 `planning/` 下任何文件和状态。作者：L7。2026-11-09。
- **来由**：负责人 2026-11-09 按方案 A 修订 D23（`docs/implementation/d23-open-input-schemas.md`），解了 Consumer v2 的 r8。但 stage real world 里绑定 Consumer 的 98 个 active Action 中有 91 个的 `input_schema` 是 `{}`（ActionDefinition 没给输入时的默认值）。方案 A 让这些 Action 只需合法用例；它们的参数在 Schema 变更时并没有被真正校验。

## 建议条目

| 字段 | 内容 |
|---|---|
| 标题 | 为输入模式为 `{}` 的业务与系统 Action 补真实的 `input_schema` |
| 归属任务 | NX-040（Schema 受控演进）的后续项；也可单列为新任务，由调度员定 |
| 范围 | `deploy/configuration/business-actions.v1.json`、`system-actions.v1.json` 中没有给 input 的 Action（本地编译结果：绑定 Consumer 的 97 个里 90 个；stage 多一个 stage 专用的 `nexloop.synthetic_fault.set`） |
| 交付物 | 每个 Action 一份输入模式（与它的受治理处理函数实际接受的请求体一致）；新版本 Action 发布；回归测试证明每个模式都至少拒绝一个探测值，从而退出 open_input |
| 依赖 | 各处理函数的请求体校验是唯一事实来源，模式要与之逐一对照；发布走现行 Action 版本流程（新版本，不改已发布版本） |
| 验收 | `schema_gates.open_input` 对这些 Action 都返回 False；r8 的 `tool_cases_built.open_input` 降到 0（或只剩确实不接受参数的 Action，并写明理由） |
| 风险 | 模式比处理函数更严会拒绝原本合法的请求：模式只能等于或宽于处理函数的校验，并用现有调用样本回放验证 |
| 估计 | 约 3–5 人日，分批做（按 capability 分组），每批送 L6 |
| 状态建议 | not_started |
