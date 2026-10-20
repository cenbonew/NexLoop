# NX-039 V4：语义发布与回退（实现说明，2026-10-20）

- **依据**：NX-039 设计稿 §7、§8、§11 V4，以及负责人 2026-10-20 已定案的 D39-7、8、9、10、12、13、14。
- **分支**：`l4/nx039-v4`，起点 `int/post-v010-batch3-final`（ca578721）。
- **迁移**：临时编号 0248。编号只出现在文件名和 catalog 里，集成时由调度员统一重编。

## 存储

- **表**：
  - `ontology.nexloop_semantic_releases`：每个版本一行。版本号格式为 `MAJOR.MINOR.PATCH`，按租户和 world 只增不减。记录内容包括：方式（publish 或 rollback）、来源候选、报告 digest、分级、回退目标、回退报告、负责人原因、发布人和意图。
  - `ontology.nexloop_semantic_release_items`：一个版本的完整内容，包括别名和补充说明。每一项都记录来源候选，以及该候选的来源 Consumer（D17）。
  - `ontology.nexloop_semantic_release_pointers`：当前版本的指针。
- **约束**：
  - 发布项单独建表，NX-045 的别名表不动（D39-13）。
  - 版本与发布项不可修改。只有保留期清理能删除它们，而且必须在清理函数设置的事务内标志下进行。

## 两个受治理的人类 Action（只授予 owner，D39-8）

`ontology.semantic.release` 这个 scope 落地为一对 Action，只授予 owner，并列入仅负责人集合（workbench-roles v24）：

- `nexloop.semantic_release.publish:1`（capability `semantic_release.publish`，operation `publish_semantic_release`）；
- `nexloop.semantic_release.rollback:1`（capability `semantic_release.rollback`，operation `rollback_semantic_release`）。

之所以没有单独新建一个 scope：工作台角色编译出来的目录 scope 只有 `action.execute`，新的 scope 无法生效；用 Action 授权和仅负责人集合来表达，才能真正生效。

### 发布

满足以下全部条件才能发布：

- 候选维度是 `semantic_content`，并且维度没有被停止；
- 候选状态是 `accepted_for_release`，最近一份报告的结论是 accept；
- 分级取自报告里的 `change_class`，也就是评估器 `classify_change` 的结果，只允许 patch 或 minor：
  - 请求里不接受分级字段（D39-7）；
  - major 和 refused 一律关闭，原因码 `breaking_change`。
- 如果存在审核关联，它的报告 digest 必须与最新报告一致；
- Parent 必须是当前版本：已有发布时为 `release:<version>`；还没有发布时，Parent 不能是 `release:` 开头；
- 来源 Consumer 没有处于删除中。

发布项的文字由负责人填写：

- **别名**：去掉首尾空白后求 sha256，必须等于候选补丁中的某个词项哈希（即 E3 的 term hash）。也就是说，只能把证据里未命中的那个词登记为别名。目标 ref 必须属于补丁的 target_refs。如果同样的文字已经指向别的定义（NX-045 别名或当前发布），就拒绝（G8）。
- **补充说明**：最多 200 字，不能含控制字符（D39-14）。补充说明只能来自发布项。

新版本的内容是当前版本的全部项，加上这次的新项；同一 ref 的新补充说明会替换旧的。版本号按分级递增：patch 加 PATCH，minor 加 MINOR。发布后会在候选事件表写一条 `published` 事件。

### 回退

- 回退会生成一个**更高**的新版本，内容等于目标版本的内容（§7.2，例如从 0.1.0 回退到 0.0.1，得到 0.1.1）。
- 目标版本中，来源 Consumer 正在删除的项不会被恢复（§9）。
- **Claim 一律不改动**（D39-9）。回退报告记录：
  - 被移除的别名数；
  - 当前版本生效期间，经这些别名所指定义匹配到的 Claim 的数量与 ID（最多 1000 个，来源是 `ontology.nexloop_claim_matches.recall`）；
  - 因删除而未恢复的项数；
  - `claims_changed: 0`。
- 不做自动回退（D39-10）；7 天观察期的告警属于 V5。

### 原因码

格式为 `semantic release: <code>`，工作台返回 409 与 reason：

- `unknown_candidate`、`dimension_closed`、`dimension_stopped`、`not_accepted`、`already_published`、`breaking_change`、`report_mismatch`、`parent_moved`、`missing_data`；
- `item_not_in_patch`、`alias_not_in_patch`、`alias_conflict`、`invalid_text`；
- `no_release`、`unknown_version`、`already_current`。

## 读取

- **定义读取**：`authz.nexloop_context_read` 被改名并包一层（原函数体不变）。对 `definitions` 读取，把当前发布中对应 ref 的别名并入 `aliases`，并加上 `semantic_note`。语义 browse/resolve 的结果（即 Context `semantics` 段的来源）因此带上了发布内容；语义版本字符串本来就包含 aliases，所以发布后版本字符串也会变化。
- **召回**：每次发布或回退之后，按当前版本重写 `release-alias:*` 召回条目。这些是词法行，没有向量。定义还没被索引的 ref 跳过，不会让发布失败。
- **工作台**：只有 owner 能用 observe 动词 `semantic_releases`（`GET /api/v1/workbench/semantic-releases`）。它返回：当前版本；最近 20 个版本，含发布项和回退报告；可发布的候选（ID、分级、报告 digest、目标 ref、词项哈希个数、Parent 是否仍然一致）。不返回任何消息原文。

## 保留期（D39-12）

- 保留类 `semantic_release` 为 365 天，对应保留设置 v6，`deploy/configuration/retention.v1.json` 同步升到 v6。
- 清理时，以下记录**永远保留**：当前版本、它之前的一个版本、仍被保留的发布记录或审核关联引用的评估报告。
- 评估报告的冻结触发器只允许清理函数在事务内标志下删除，其他删除仍然拒绝。

## 工作台

- 知识工作台的审核队列下方新增“语义发布”区块，只有当前角色带发布或回退 Action 时才显示。
- 区块内容：
  - 当前版本；
  - 可发布候选的表格。分级只显示，不能填写；Parent 已变化的候选显示“基线已变化，需重新评估”；
  - 每行一个发布表单：选择目标、填写别名或补充说明（最多 200 字）、填写原因；
  - 版本历史，回退报告以文字说明 Claim 数量和“未改动，需人工复核”；
  - 回退表单。
- 按 UI 规范草案：实体名用 EIOS 英文，状态码一律显示中文徽标，空状态与“不可用”区分开。

## 测试

`tests/test_semantic_release_pg.py`（6 项）：

- 只有通过评估、且分级允许的候选能发布：未评估、被拒、major、refused 都被拒绝；分级不能在请求里填写；补丁之外的项、哈希对不上的别名、别名冲突、Parent 已变化，都被拒绝；
- 发布记录、指针、候选事件都有审计信息；AT-023 受保护表 digest 不变；NX-045 别名表不变；发布记录不可修改；
- `semantic_note` 的来源和长度限制，表层面也有约束；
- 回退：生成更高版本，报告列出 Claim 的数量与 ID，Claim 不变；再回退会恢复别名；
- 保留期：保留当前版本和前一个版本；过期报告被清理；被引用的报告保留；
- 非 owner：发布、回退和读取都返回 403。owner 经受治理入口调用，原因码以 409 返回，带分级字段的请求返回 422。每次调用都留在 `runtime.nexloop_action_claims`；
- 通过真实的 SemanticAdapter 验证：browse 结果并入了发布的别名与补充说明，召回中有对应的别名行。

web 新增 `test/workbench-semantic-release.test.ts`（3 项）：严格解析、只有 owner 可见、回退报告的文字。

## 限制

- 召回中的发布别名只有词法行，没有向量。向量回填目前只处理类型来源，后续可以把 `release-alias:*` 纳入回填。
- 回退报告里“经被移除别名匹配”的判定，依据的是 Claim 匹配记录中召回命中的 ref 与被移除别名所指的 ref 相同。这是近似判定，所以报告只供人工复核，不据此自动改动任何数据。
- 7 天观察告警、按维度停止的受治理 Action、发布项的删除传播（发布一个原因为 `erasure` 的新版本），都在 V5。
