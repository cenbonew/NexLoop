# O3：授权事实解析结果按内容寻址缓存

分支 `perf-o3-fact-cache`，BASE `7669395c31fa3e9ee8c9856b262954e92592ed70`。背景与优化编号见 `perf-authz-diagnosis.md` §5。无迁移，不改任何时限。

## 实现
- `nexloop_eios/authorization.py` 新增 `FactParseCache` / `FACT_PARSE_CACHE`：有界 LRU（2048 条），`threading.Lock` 保护。服务端事实适配器与浏览器事实适配器（`browser_authorization.py`）的 `_load` 都改为经缓存解析。
- **每次决策仍从 PostgreSQL 读取每条事实**，取到的 `record_hash` 照常记入签名 claims；缓存只省掉同一文本的 JSON→模型重复解析。
- 缓存键 = (事实模型类, 数据库本次返回、补上 repository witness 后的精确 JSON 文本)。tenant、principal、world 相关字段、grants、revision、snapshot_digest 都在文本里，所以任何授权事实变化都会产生新键。命中时返回的对象，就是对同一文本做一次新解析会得到的结果。
- 事实模型是 frozen + strict，校验不依赖时钟，因此共享实例安全。解析失败不缓存。
- 会话 directory hash 与身份绑定的校验路径（`open_unit_of_work`）没有改动。缓存与授权判定无关，判定仍由 EIOS resolver 在每次决策时完成。

## 正确性测试（`tests/test_authority_fact_cache.py`，8 passed）
- 命中结果与新解析完全相同，解析失败不缓存。
- **撤权**：grant 置空后，旧会话因 directory 变化被拒；重新认证的会话也被拒，且被撤销的 grant 是新解析（不是旧缓存）。
- **grant 变化**（execute→read）：变更后的 grant 产生新键，决策被拒。
- **directory hash 变化**（tenant authority_revision +1）：缓存是热的，旧会话仍被拒；新会话的 directory hash 不同并能通过。
- **跨主体 / 跨租户 / 跨 world**：另一身份的 principal 绑定事实全部是它自己文本的新解析，与原主体条目没有交集；租户 B 的条目只属于租户 B；simulation 与 real 的 world 策略是两条不同的解析结果。
- **并发**：8 线程 64 次并发决策无错误、全部允许；8 线程向 16 条上限的缓存交叉解析 400 个不同文本，无串值，大小有界。

## 回归
以下 12 个文件共 171 passed（203.8s，`.ci-results/o3-regression.xml`）：`test_authority_fact_cache`、`test_bootstrap`、`test_postgres_authority`、`test_browser_business_authorization`、`test_runtime_activation`、`test_runtime_authority`、`test_role_mapping`、`test_agent_governed_write`、`test_action_definitions`、`test_goal_controls`、`test_service_grants_pg`、`test_run_credentials`。

## 前后对比（同一代码，测量钩子中关闭/开启缓存，本机 M4 Pro 串行）

命令：

```bash
NEXLOOP_PERF_NO_FACT_CACHE=1 scripts/perf/run_profile.sh o3-micro-before scripts/perf/test_micro_authz.py
```

```bash
scripts/perf/run_profile.sh o3-micro-after scripts/perf/test_micro_authz.py
```

B 类 7 例用同样的方式运行（`o3-b-before` / `o3-b-after`），两轮均 7/7 passed。

### 微基准（ms）

| 调用 | p50 前 | p50 后 | 均值 前 | 均值 后 |
|---|---|---|---|---|
| 单次授权决策 | 4.56 | 4.08 | 4.78 | 4.25 |
| Action 定义读取 | 6.82 | 5.85 | 7.07 | 6.10 |
| 受治理 create | 27.1 | 24.7 | 28.4 | 27.4 |

缓存命中 3900 次、未命中 12 次。

### B 类 7 例

- 每条事实的 Python 解析自身时间：0.131 ms → **0.031 ms（−77%）**。
- 命中率：111646 / (111646 + 2930) = **97.4%**。
- 7 例合计约省 11 s Python CPU。

受 2 s 时限约束的请求（单轮测量，带钩子）：

| 请求 | p50 前→后 | p95 前→后 | max 前→后 |
|---|---|---|---|
| runtime_effect_tool | 754→745 | 1802→**1586** | 1828→**1603** |
| authorize_runtime_activation | 258→252 | 967→**860** | 1863→**1537** |
| prepare_message_context | 994→1149 | 1887→1834 | 2047→2367 |

`prepare_message_context` 以服务端 `context_artifact_command` 的 SQL 与锁为主，O3 对它几乎没有作用。它这一轮的 p50/max 上升属于单轮噪声：同一请求在诊断时的三轮测量中 max 在 1986–2125 ms 之间波动。

## 结论与限制
- O3 让单次决策快约 10%，2 s 敏感请求的尾部快约 12–18%，单靠它不能把这些请求拉出时限风险区（effect_tool 的 max 仍约 1.6 s，越限慢化系数 k 只有约 1.25）。
- 主要收益仍在 O1（请求内决策复用）和 O4（Backend 锁），两者按调度员决定等 L1 方案 B 合入后再做。
- 数据为单轮测量，带钩子开销 1–7%；sice 未实测。
