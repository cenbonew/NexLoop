# D23 修订：遇到"任何输入都合法"的 Action 时怎么判（负责人 2026-11-09 定为方案 A）

- **状态**：**定稿**。负责人 2026-11-09 选方案 A（原话"同意方案 A（推荐）"），L6 判 PASS 并列出边界条件（§5）；D23 修订句见 `NX-040-design.md` D23。作者：L7。依据：main f9068e06。
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

## 5. 定稿：方案 A 的实现与边界条件（L6，2026-11-09）

1. **豁免由 `schema_gates` 自己计算**：`schema_gates.open_input(parent, candidate)` 只看 `action_schemas`，输入里的任何 open 标记（无论在 schemas、cases 还是 inputs 顶层）都被忽略。探测值集合 `schema_gates.OPEN_PROBES` 只定义这一份，r8 检查脚本从那里导入，用它挑非法用例。
2. **两版模式比较规范化 JSON**（键排序、分隔符 `,` `:`）的逐字节相等；任何差异（哪怕只是 description 或 `$comment`）都回到原规则，要合法和非法两条用例。
3. **含非本地 `$ref`（不以 `#` 开头）或任何 `$dynamicRef` 的模式不豁免**。两版用同一个 validator（`Draft202012Validator`）和同一种 format 设置（不启用 format 校验）。
4. **探测值**（父模式接受全部才算开放）：`[]`、null、`'x'`、0、`{}`、带多余字段的对象、true、`''`、-1、1.5、4096 个字符的长字符串、三层嵌套对象、多元素混合数组。例如 `{"maxLength": 3}` 会拒绝长字符串，所以不算开放，必须有非法用例。
5. **只豁免非法用例**：合法用例照样必须有，并在两版下回放；开放 Action 如果额外给了非法用例，也照常判定。
6. **报告**：评估报告的契约（`evolution-evaluation-report`，已发布、字节冻结、顶层 `additionalProperties: false`）不允许新增顶层字段，所以 open_input 放在 `tool_context_compatible` 闸门的 `evidence_ids` 里：
   - 前两条固定为 `tools:regression_set` 和 `tools:open_input:<N>`（N 为总数），不会被 64 条的上限截掉；
   - 其后是 `open_input:<Action 名>`，与 incomplete、failures 一起受 64 条上限约束；
   - open_input 不计入 incomplete。
   - r8 结果的 `tool_cases_built` 一步给出完整总数（`actions`、`cases`、`open_input`、`no_invalid_case`），名单最多 64 条；没有绑定 Action 时写 `actions: 0`。
7. **测试**（`tests/test_nx040_f4.py`、`tests/test_s6_tool_cases.py`）：两版都是 `{}` → 通过；只差一个字节 → 原规则；`{"maxLength":3}` → 必须有非法用例；外部 `$ref` 和 `$dynamicRef` → 不豁免（本地 `$ref` 可以）；输入里伪造的 open 标记被忽略；91 个开放 Action 时证据仍保留总数且不超过 64 条；stage 形状（catalog 编译出的 Consumer Action）回放通过，收紧候选的阳性对照仍判回归。
8. **方案 B 另行跟踪**：给开放模式的 Action 补真实输入模式，草稿见 `docs/planning-drafts/d23-option-b-input-schemas-20261109.md`。
