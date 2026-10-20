# NX-040 F1：兼容性判定库（实现说明，2026-10-20）

依据：NX-040 设计稿（`l5/nx040-decisions`，e4dee217）§4.1–§4.5、§6、§7.2、§11 F1；D21–D27 已定案。分支 `l2/nx040-f1`，起点 `side/batch3-tenantfk`（b6cd9947）。没有迁移。

## `nexloop_eios/evolution/schema_compat.py`（纯逻辑：不连数据库，不调用网络，没有效应）

- **白名单**：
  - `add_optional_property`、`add_vocabulary_value`：minor，可以列入自动发布的范围（`auto_publish_eligible`）。D22 规定首批一律人工发布，所以这只是“有资格”，不是开关。
  - `add_object_type`：minor，只能人工发布。
  - `add_relation_type`：refused（D24）。
  - 其余一律 major，原因 `breaking_change`，并附 `operation_not_whitelisted`。如果这次变更带了现有类型的下一个版本，还会附上它破坏了什么的细分码（§6）。
- **复用 vendored 核心**：对 Parent 和变更后的定义调用 `eios.ontology.semantics.assert_object_compatible`，原函数不改。本库另外补上更严的规则：
  - 封闭词表只能增加值，去掉整个词表也算删除值（vendored 函数允许去掉）；
  - 新属性必须可选，有默认值或允许为空，值类型是标量，可见性不高于 normal，不能自带词表。
- **四个维度**，任一 fail 都关闭这次发布（§4）：
  - **迁移**：不产生 DDL 或表达式索引（`requires_ddl`）；不改指标口径；Parent 到变更后的定义必须兼容；迁移对象数不超过 10 000（D25）。
  - **索引**：嵌入维度不变；重流的向量数和费用不超过上限（D26）；Parent 中被语义发布引用的 ref 仍然存在。
  - **工具签名**：每个引用该类型的 Action 恰好有一个后继，后继只把版本号和 object_types 的版本加 1，输入和 capability 的 schema_hash、risk_level、effect 类别都不变；新属性不能成为必填参数；没有前驱的后继或新类型附带的 Action 判 `authority_content`；工具回归集在新旧 revision 下的结果一致（D23）。
  - **契约**：不改 `packages/contracts/`。
- **输出**：`judge(change, parent)` 返回 `{op, change_class, allowed, reason, codes, dimensions, auto_publish_eligible, candidate_definition}`；`classify(change, parent)` 给出 `minor`、`major` 或 `refused`。
- **固定码**：全部列在 `CODES` 中。其中 `_MAJOR` 里的码（§6 的破坏性变更、非白名单操作、编辑模式开放、Parent 不兼容）使变更判为 major。其他码只让某个维度失败：这次发布关闭，候选仍判 minor，但 `classify` 对任何不允许的变更都偏向返回 major（D39-7）。

## 与 NX-039 的对接

`evaluation.classify_change` 新增可选参数 `schema_parent`：`ontology_schema` 维度提供了 Schema Parent 时，交给本库分级；不提供时仍判 major，与原行为一致。

## 测试 `tests/test_schema_compat.py`（纯逻辑）

- 三种白名单操作各有正例：四个维度都 pass，给出变更后的定义；新类型不在自动发布资格内。
- 30 个关闭用例，每个固定码至少一个，并逐一核对分级与原因。
- 边界：必填属性、改类型、删词表值、去掉整个词表、删属性、改主键，一律判 major，并给出对应的细分码。
- vendored 函数本身行为不变：它允许去掉 enum，本库判为删除。
- 与 `classify_change` 的对接；有一个测试断言 `CODES` 中每个码都有对应用例。
