# NX-064 p99 离线判读与运行单核对（2026-11-16）

## 基线与既有覆盖

- 分支 `codex/nx064-p99-offline-20261116`；BASE `6a5ef7a98a44be8babcba71940419e3391587ff1`，新独立工作树初始干净。既有 NX-061、P01、Host 等树及改动保留；没有共享 planning 修改或 push。
- main 已有 `scripts/acceptance/nx064_p99_gate.py`、`tests/test_nx064_p99_gate.py`、两份 NX-064 运行单。既有测试已覆盖1000通过、慢样本失败、999不足、空/不可读来源、quantile rank/桶上界/开放末桶、guard估算标记、非法计数/越界桶、since筛选、无时区拒绝及意外异常拒绝。本轮直接复用，不另造计算器或重复直方图测试。
- main 的 NX-064 evidence 记录2026-11-08 host_op=0、guard_op=2，结果 insufficient；后续 `nx064-deadline-model-correction-20261112.md` 只是测试侧 deadline 模型及既有 load/held 证据，不是 stage p99。当前真实样本与部署时限未重新读取，也不由旧记录推断当前值。

## 可复现缺口与最小修复

1. JSON 默认 last-key-wins 会覆盖慢桶。例如一个快桶1000次、慢桶1000次再重复慢桶0次，原工具只看到1000次快请求，可能判 pass。现在 samples 与 host-metrics 两种 JSON 输入均拒绝重复成员，退出3；错误不回显原成员或原报文。
2. `--host-metrics` 是 Host 启动以来累计值，没有每分钟 timestamp。原工具接受 since/until 却忽略它们，可能把全时段结果当选定窗口。现在该来源带窗口直接拒绝，要求有时间戳的 `--samples`。
3. since>=until 的空/逆序请求原先落成 insufficient，混淆非法输入和有效窗口样本不足；现在退出3。正常窗口保持 `[since, until)`：包含 since、排除 until；等价时区表示得到同一结果。

只修改 JSON/CLI 输入校验和对应运行单措辞。`judge`、`quantile_bucket`、`_upper`、`_midpoint`、`_count`、`_add`、`from_host_metrics` 的函数 AST 与 BASE 完全相同；RATIO=1.1、LAST=116、MINIMUM=1000 不变。**没有改2秒产品时限、Host/后端配置、p99×1.5、2000–5000建议公式或任何采集流程**。

新增7例：重复成员两来源、空/逆序窗口两例、累计来源拒绝两种窗口参数、邻接慢分钟隔离与时区等价阳性一例。原脚本仍只读合成文件，本轮不连接网络/远端/真实READ/数据库，不部署、不准入、不重试e972。

## 实际执行顺序、首败与回归

Node24.13.0、项目已有 Python3.12.10，`pytest -n 2`；不安装依赖。每次启动前程序化检查 load1<12。

```sh
source ~/.nvm/nvm.sh && nvm use 24
export PATH="$PROJECT_VENV/bin:/opt/homebrew/bin:$PATH"
PYTHONPATH=packages/eios-core/src:tests python -m pytest -q -n 2 tests/test_nx064_p99_gate.py --junitxml=.ci-results/nx064-p99-baseline.xml
PYTHONPATH=packages/eios-core/src:tests NEXLOOP_TEST_PG_AUDIT=.ci-results/nx064-pg-audit.txt python -m pytest -q -n 2 tests/test_nx064_p99_gate.py tests/test_load_report.py --junitxml=.ci-results/nx064-p99-restored-final.xml
```

- 首次准备的 load1=12.265，负载闸 AssertionError，pytest **没有启动**，也未生成原计划的 first.log；不冒充功能测试首败。
- 其后先完成修复版回归：44 passed，1.33s，load1=9.791；此时发现基线失败证据尚未保存。
- 待负载合规，在**本线独立树的单一已改脚本**上回放固定 BASE 字节，保持新增测试，运行上面的 baseline 命令；用 try/finally 保存/恢复修复源码，恢复后 SHA 相同，不触碰其他树或旧改动。
- **基线实际失败**：6 failed /17 passed，0.81s，load1=11.576；恰为新增六类非法输入，合法窗口阳性通过。`.ci-results/nx064-p99-baseline.log` 与 `.xml` 保留。
- 最后恢复修复版再跑 p99＋load-report 两文件：44 passed、0 failed/errors/skipped，1.27s，load1=8.255，PG fixture audit 无条目；全部只用合成输入。PG/重型 load 回归不在本轮范围，不能用这些纯函数结果替代它们。
- 提交前文档/publication/definer检查、固定 HEAD 四门扫描另附原始日志；不运行全量CI。

## 运行单与剩余 stage 条件

`run-nx064-effect-request-metrics.md` §6 同步写明窗口只支持 samples、带时区、since<until、半开区间和重复 JSON 退出3。`run-nx064-stage-load.md` 的方案B导出描述改成 `window_start >= start and window_start < end`，与离线工具一致；没有执行该 SQL 或取得其读取许可。

只有既有授权来源明确且窗口内**至少1000个 Host 侧同一 operation**样本，结合实际部署时限和来源/profile/窗口/进程范围，才可作 stage 判读。guard p99+300ms 始终只是估算，不能记 Host 实测通过；CI deterministic-test 来源不代表 stage 网络，是否接受来源仍按原运行单由负责人决定。累计 Host 文件不能被本工具伪装成指定时段数据。insufficient 要记未测，refused 要修输入，均不算 pass。

本轮所有 p99 数字只来自合成直方图，不估算或宣称真实 p99，不调整部署配置。stage 样本、据样本定时限及任何新增采集/负载工作仍需单独授权和独立判定；当前运行单“备用/先等”边界未解除。仅交本地候选供 L6 独审，不自判 NX-064 done 或 stage 验收通过。
