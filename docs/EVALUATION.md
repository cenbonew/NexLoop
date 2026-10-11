# 评估运行指南（简明版）

本文说明在仓库里**已有**的评估与验证怎么跑：确定性回归、显式 opt-in 的真实模型验证、候选粘合校准、压测矩阵。只写已有事实；不确定的标【待确认】。测试的一般约定见 [ARCHITECTURE.md §5](ARCHITECTURE.md#5-开发者上手)。

## 1. 原则

- 默认 CI 不调用真实模型、不读 `.env`、不联网；关键测试缺依赖时失败，不跳过。
- 所有数据集都是合成的；不要放入真实客户数据。
- 确定性 provider 的结果只证明管道与守卫，不证明模型质量；真实模型的一致率只做测量和报告。

## 2. 确定性回归（默认 CI 内）

| 内容 | 数据 / 测试 | 说明 |
|---|---|---|
| 对话提取（NX-019） | `tests/data/nx019_extraction_cases.json`（v2，100 例中文合成用例，冻结），由 `tests/test_conversation_extraction.py` 使用 | 覆盖条件与否定、相对时间、反复投诉、提示注入、隐性推断、更正链、跨租户等；每例含模拟模型输出（部分刻意错误）、预期 Claim、禁止模式，见 [NX-019](implementation/NX-019-extraction.md) §5 |
| 候选粘合（NX-045） | `tests/data/nx045_merge_calibration.json`（42 对合成标注）、`nx045_merge_config.json` | 测试断言提交的配置正好等于校准输出，见 [NX-045](implementation/NX-045-candidate-merge.md) |
| 知识与目标闭环（NX-025） | `tests/test_closure_*_pg.py` | 真实 Host/Pi + 确定性测试协议，见 [NX-025](implementation/NX-025-implementation.md) |

运行示例：

```bash
LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=packages/eios-core/src:tests uv run --frozen pytest -q tests/test_conversation_extraction.py
```

粘合校准的回归在 `tests/test_candidate_merge_pg.py`（需要 PostgreSQL）。

## 3. 显式 opt-in 的真实验证（默认 CI 不收集）

这些文件名不是 `test_*`，默认不被收集。每个都要求用环境变量指向一个 0600 的私有 env 文件，key 只留在进程内存里，不打印、不写日志；输入全部是合成数据。

| 文件 | 内容 | 需要的变量 |
|---|---|---|
| `tests/verification_real_extraction.py` | NX-019：真实模型跑冻结数据集，断言守卫不变式，测量召回 | `NEXLOOP_NX019_REAL_ENV_FILE`（`MODEL_*`），报告 `NEXLOOP_NX019_REAL_REPORT` |
| `tests/verification_real_matching.py` | NX-020：真实模型做四层匹配判定，测量与预期的一致率 | `NEXLOOP_NX020_REAL_ENV_FILE`，报告 `NEXLOOP_NX020_REAL_REPORT` |
| `tests/verification_real_embedding.py` | NX-021：真实 embedding 端点 + 真实 PG 召回 | `NEXLOOP_REAL_EMBEDDING_ENV_FILE`（`EMBEDDING_*`） |
| `tests/verification_real_merge_calibration.py` | NX-045：用真实 embedding 校准粘合权重；生产租户配置须在自己的 embedding profile 上校准并经可信配置发布 | `NEXLOOP_REAL_EMBEDDING_ENV_FILE` |
| `tests/verification_real_model.py` | 端到端：HTTPS 消息 → relay → Pi → 导出，需要显式的私有 stage 配置 | 见文件头说明 |

已有的真实证据报告：[NX-019](implementation/NX-019-real-model-report.json)、[NX-020](implementation/NX-020-real-model-report.json)、[NX-045](implementation/NX-045-real-calibration.json)。

## 4. 压测矩阵（NX-031）

- 矩阵：[`deploy/load/matrix.v1.json`](../deploy/load/matrix.v1.json)，8 格，门槛格只有 C4（1 条/秒）与 C4-low（0.2 条/秒）；模型为注入对数正态时延的确定性 provider（中位 1.5 s、p95 4 s），**从不使用真实模型**。
- 步骤：`scripts/load/build_template.sh [--seed] [--consumers] [--out]` 构建固定种子的合成模板；`scripts/load/matrix.py --template <dir> --matrix deploy/load/matrix.v1.json --out <dir> --lane-root <dir> [--cells C4,C4-low] [--timing-scale 0.05]` 逐格运行；`scripts/load/report.py` 按 [结果契约](implementation/NX-031-results-contract.md) 生成报告。`--timing-scale` 只用于本机小规模检查，正式运行为 1.0。
- 提取使用只在 `--mode test` 下可用的 load-rules provider，其提取质量不作为任何验收依据。
- 1,000 客户的整轮在 CI 主机的夜间窗口由调度员执行；当前结论与限制见 [NX-031 实现说明](implementation/NX-031-implementation.md) 与 [planning/ROADMAP.md](../planning/ROADMAP.md)。

## 5. 上下文策略评估

- 交接规格要求至少提供 `recent_plus_required_v1` 与 `ontology_hybrid_v1` 两种策略并在相同快照下比较（[05_CONTEXT_AND_PLANNING](handoff/docs/05_CONTEXT_AND_PLANNING.md) §9）。仓库中的策略版本见 [`context-strategies.v1.json`](../deploy/configuration/context-strategies.v1.json)；每次模型请求都记录上下文来源与请求摘要，可经人类审计接口读取。
- 策略对比的离线评估脚本【待确认：仓库中尚未找到】；Evo 驱动的候选评估属于 S6（NX-038、NX-039），未开始。
