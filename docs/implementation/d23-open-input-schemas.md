# 提案：D23 遇到"任何输入都合法"的 Action 时怎么判（交 L6，需负责人修订 D23）

- **状态**：提案，待 L6 判定；若采纳方案 A，需负责人修订 D23（原决定：负责人 2026-10-20，`NX-040-design.md` D23）。作者：L7。依据：main f9068e06。
- **起因**：Consumer v2（`delivery_windows`）的 r8 在 stage 实跑（R72，2026-11-09 06:23 +08）被 `no_forbidden_regression` 和 `tool_context_compatible` 拒绝。stage real world 里绑定 Consumer 的 active Action 有 98 个，其中 91 个的 `input_schema` 是 `{}`（ActionDefinition 没给输入时的默认值）。

## 1. 已在检查脚本一侧修好的（本次提交）

- 旧的 `gates_inputs` 给每个 Action 一律造 `params: []`、`expect: invalid`，`{}` 接受 `[]`，所以记为失败；valid 样本对 string 一律用 `'x'`，遇到 `pattern`、`format: uuid`、`const`、`minLength` 时，父模式自己就判不过。
- 现在 `tool_cases`（`scripts/acceptance/s6_stage_schema.py`）：
  - valid 样本按模式自身的约束生成（const、enum、required 属性、pattern、长度、上下限、数组项），生成后先用父模式校验，不过就 `Refused`，不交给闸门误判；
  - invalid 样本从一组候选（`[]`、null、`'x'`、0、`{}`、带多余字段的对象、true）里挑父模式确实拒绝的那个；
  - 父模式什么都不拒绝时，**不伪造 invalid 用例**，把这个 Action 列出来（r8 结果的 `tool_cases_built` 一步），回放按 D23 报 `incomplete`，`tool_context_compatible` 照样失败。
- 在 catalog 编译出的 Consumer Action 上验证过：7 个有约束的模式全部生成合法和非法用例，回放 0 失败；收紧候选模式的阳性对照仍判为回归（`tests/test_s6_tool_cases.py`）。

## 2. 还剩的问题：开放模式按 D23 无法满足

D23 要求"每个 Action 至少一个合法和一个非法参数用例"。`{}` 没有任何可拒绝的输入，非法用例根本不存在。所以只要类型绑定了开放模式的 Action，r8 就**永远**被 `tool_context_compatible` 拒绝。这是 F4 已知的局限（`tests/test_s6_stage_schema_pg.py` 的 fixture 注释），以前的 S6 r8 能过，是因为那次的类型没有绑定 Action。

文档（NX-040 设计、F4 实现说明）没有为开放模式另作约定，所以不能在脚本里自行处理，交 L6 判定。

## 3. 方案

| 方案 | 做法 | 改动 | 评价 |
|---|---|---|---|
| **A（建议）** | `replay_tools` 对"父、候选输入模式逐字节相同，且父模式接受全部探测值"的 Action，不要求 invalid 用例；这些 Action 在报告里单独列为 `open_input`（evidence），不计入 incomplete。只要两版模式有任何差异，或者父模式拒绝任一探测值，就照旧要求两个用例 | `schema_gates.py` 一处，加单元测试；D23 加一句修订 | 闸门语义"两个版本结果一致"仍成立：模式相同，对任何输入结果都相同，这是可证明的，不是跳过。不会变成永远 pass：模式一变就回到原规则 |
| B | 给这 91 个 Action 补真实的输入模式 | 系统目录和业务 Action 清单大改，每个都要新版本发布 | 本该有，但工作量大，和 Consumer v2 发布无关，不建议作为解锁条件 |
| C | 维持现状 | 无 | 绑定 Consumer 的类型永远发布不了 Schema 变更，与 NX-040 目标冲突 |

**否决的做法**：给开放模式伪造一个 invalid 用例（例如把 expect 改成 valid 凑数），或者在脚本里跳过这些 Action——这会让闸门对它们静默通过（review-silent-pass-unconfigured）。

## 4. 回放规模

- 98 个 Action，每个 1–2 个用例，每份报告回放两遍，每个用例在两个版本下各校验一次 jsonschema：本地约 100 个 Action 的一次回放远小于 1 秒（测试里限 5 秒）。
- 闸门的 `evidence_ids` 已经在 `_gate` 里截到 64 条；91 个开放 Action 列在报告里时同样受这个上限约束，完整名单在 r8 结果的 `tool_cases_built` 一步（也截到 64 条，另给总数）。
