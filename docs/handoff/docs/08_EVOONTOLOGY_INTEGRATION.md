# EvoOntology 选择性整合

## 1. 冻结方向

整合的是**语义访问和演进机制**，不是替换 NEX-EIOS。正式消费者对象、Schema、权限与 Action 仍由 EIOS 管理。真实事实更新不取决于模型评估分数。

调查快照：`ruc-datalab/EvoOntology` master `f64413dae88d88645b1f2c069cf4e17308ad0f89`。MIT 许可的来源与修改必须保留。[S06]

## 2. 复用优先级

| 源码/机制 | NexLoop 目标 | 处理 |
|---|---|---|
| `runtime/runtime.py` 的 browse/resolve、manifest、ambiguity | Context Engine 的语义入口 | 保留输出契约思想，替换数据与检索 adapter |
| `ontology/models.py` 的 Term/Mapping/Constraint/Evidence | 概念解释与 EIOS 类型映射 | 做语义视图，不用作业务实例数据库 |
| `evolution/session.py` | 演进任务与候选预算/状态 | 持久化迁移到 PG/业务任务；不并行复制其工作区发布权威 |
| `evaluation/evaluation.py` | 候选对比和匿名 A/B | 增加硬约束、样本门槛与重复评估 |
| `evolution/adapter.py` | 统一评估接口 | 实现 NexLoop 对话、语义、上下文任务 adapter |
| `validate.py` | 记录与引用检查 | 扩展类型、权限、兼容和迁移检查 |
| evolve/build Skills | 缺口诊断与局部假设流程 | 改写为受限后台任务提示，不赋予代码/发布任意修改权 |

Python 核心提供确定性流程，演进智能主要在 Skill，不能只 import 一个类就认为会自主完成演进。[S06]

## 3. 不移植的默认实现

不把 JSON 版本目录用作业务主存储；不把全部 MCP 管理操作暴露给运营 Agent；不让模型选择任意本地 workspace；不直接导入 Claude Code/Codex 插件的主机脚本执行能力；不把同名 Constraint 的描述当作真正的 Action 拦截器。

上游名为 Semantic 的查询主要是词项/名称/别名匹配，并非现成完整向量召回。中文提取和消费者身份识别另有本项目测试。

## 4. 三条独立更新路径

**实时事实路径**：消息/事件→Claim→核验→EIOS Instance Mutation。拒绝联系或付款核验不等待评分。

**语义优化路径**：执行轨迹→Content/Tool/Schema 归因→概念/映射/上下文返回候选→离线测试→语义版本发布。

**正式 Schema 路径**：结构缺口→EIOS schema proposal→兼容性/迁移/权限/工具回归→正式发布→投影与索引刷新。

允许针对不同模型优化语义呈现，但所有模型共用同一真实业务定义和权限。模型不同不应导致“已付款”“允许触达”含义不同。

## 5. 演进工作流

冻结 Parent、eios revision、task collection、留出集、目标指标、硬拒绝条件和模型预算。候选只处理一个主要可检验假设；归因可能是知识不足、检索遗漏、上下文丢失，不能动辄新增类型。

状态（与 `contracts/evolution-candidate.schema.json` 一致）：`proposed → evaluating → accepted_for_release|rejected|incomplete`；诊断属于 `proposed` 内的工作，`accepted_for_release` 不等于已发布。NexLoop 可以将上游 running/reject 循环映射为多轮候选，预算耗尽或输入缺失时保留 Parent。不得为了满足“必须变好”的目标伪造评分或无限消耗。

发布门槛：引用与结构有效；零越权/零跨租户/零事实污染；不存在禁止的行为回退；性能/成本不超过冻结上限；独立测试改善或达到预设非劣标准。评分相等是否接受由任务定义，不允许 Agent 临时改变规则。

## 6. 阶段交付

> **ADR-019 修订：** 别名/映射更新与候选粘合提前到 v0.1（NX-045），由人工审核而非候选评估驱动；v0.2a 起才由评估驱动。

v0.1：EIOS→语义快照导出；只读 browse/resolve；实际调用与缺口记录；至少一套确定性语义回归。无自动正式 Schema 修改。

v0.2a：后台候选和评估；可发布描述、别名与映射更新；保留人类停止/回退入口。

v0.2b：对新增可选属性、受约束新类型开放有限自动 Schema 发布；删除/改类型/权限/指标口径属于更高风险维护路径。

v0.3：结合模拟与真实实验，但模拟结果仍不改写 real 事实。

## 7. 来源跟踪

仓库根 `versions.lock.json` 的 `upstream_sources` 段记录 repo、commit、原路径、目的路径、license、内容 hash、修改原因、测试映射和负责人（同一文件的 `packages`/`images` 段记录依赖与镜像 digest；见部署文档）。固定一个核心来源，避免同时复制根目录与插件内的同一份核心。更新上游先比较差异与运行契约测试，不自动追 master。
