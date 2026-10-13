# NX-037 人民币计价实现说明（D-g 方案 2）

依据：负责人 2026-10-13 定案 D-g 方案 2（Host 成本策略与 Run 预算支持人民币，按币种分别记录，不做任何汇率换算，币种不一致一律拒绝）；调度员 2026-10-13 对三个遗留问题的裁定。

## 1. Host
- 运行配置 `cost_currency`（默认 `USD`，只接受 `USD`/`CNY`）决定成本策略币种；Run 预算币种必须相同，否则 `runtime_cost_policy_missing`。
- `deepseek-flash` 用非 USD 币种时必须配置价目 `model_tariff_file`（[`deepseek-flash.cny.v1.json`](../../deploy/configuration/model-tariffs/deepseek-flash.cny.v1.json)，DeepSeek 官方人民币价，记录来源与核对日期）；缺失则拒绝启动。价目替换 Pi 模型对象的费率（去掉 USD 阶梯价），Pi 的 `calculateCost` 与预留下限因此直接是人民币。
- 价目一律按高峰价：低谷时段会高估约一倍，只会多挡、不会超支（调度员裁定保留）。

## 2. 预留下限
- 原来按完整上下文（1M token）估算，人民币下为 ¥2.03276800，超过单 Run 上限 ¥2。
- 现在按请求大小上限 `MAXIMUM_REQUEST_BYTES = 196608`（规范化请求 JSON 字节数）限定输入 token：

  floor = (min(contextWindow, 196608) × max(input, cache_read, cache_write) + 4096 × output) / 1e6

  人民币 (196608 × 2 + 4096 × 8) / 1e6 = **¥0.425984**；USD (196608 × 0.3 + 4096 × 1.2) / 1e6 = 0.0638976。
- 依据：字节级 BPE 的每个 token 至少覆盖 1 个字节；规范化 JSON 自身的键与引号多于对话模板的少量特殊 token。
- 适配器对**每一次**模型请求（无论是否记录请求）先检查大小，超过即 `runtime_model_request_too_large`，在任何授权与预留之前拒绝。实际费用仍逐次与 `maximum_request_cost` 比对（`runtime_model_cost_mismatch`）。

## 3. 人民币版策略与金额依据
每次请求预留取 Host 示例 `maximum_request_cost` = ¥0.45（≥ 下限 ¥0.425984，留少量余量）。

| 文件 | 预算 | 依据 |
|---|---|---|
| `reply-guarantee.v3.json` | 2 轮 / 2 次工具 / 60 s / **¥1.00** | 2 × ¥0.45 = ¥0.90 ≤ ¥1.00 ≤ 单 Run 上限 ¥2 |
| `plan-reevaluation.v2.json` | **4 轮** / 12 次工具 / 300 s / **¥2.00** | ¥2.00 = 单 Run 上限；4 × ¥0.45 = ¥1.80，第 5 次预留必然超限，所以轮数从 8 降为 4 |

- 已发布的 `reply-guarantee.v1/v2.json`、`plan-reevaluation.v1.json`（USD）不变。数据库 `control.nexloop_reply_policies` 只取窗口值（v3 与 v2 相同），不需要新迁移。兜底 Run 预算来自 Worker 的 `--policy-file`。
- 测试：`tests/test_nx037_run_interactions.py::test_cny_budgets_fit_the_tariff_floor_and_the_per_run_cap`；`apps/agent-host/test/cny-cost-currency.test.ts`（下限 < 单 Run 上限；超过请求上限的请求在 Host 侧拒绝）。

## 4. 运行入口
`scripts/nx037/run_interactions.py` real 模式：`--host-config`；前置检查 `host_not_real_model`、`host_currency_differs`、`model_tariff_missing`、`model_tariff_invalid`、`reply_fallback_currency_differs`（对 `reply-guarantee.v3.json`）；月度费用起点经 `authz.nexloop_nx037_month_model_cost`（迁移 0185）；按币种汇总，其他币种的非零费用立即停止（`currency_mismatch`）。见 [OPERATIONS §9](../OPERATIONS.md)。
