# NX-062 M1：机器密钥格式与 pepper

- **依据**：`docs/implementation/NX-062-design.md` §2.2、§6 M1，L6 条件 C2，负责人 2026-11-09 Q-B（pepper 放私有文件）、Q-F（只接受 0600 密钥文件）。执行：L10。
- **范围**：只有库代码和扫描规则。没有迁移、没有路由、没有部署影响。真实 PG 和真实 HTTP 测试从 M2、M3 开始（本片没有可测的 PG 对象或 HTTP 路由）。

## 改动

| 文件 | 内容 |
|---|---|
| `packages/eios-core/src/nexloop_eios/machine_keys.py` | `nxm_<12 位 base32 prefix_id>_<43 位 base64url secret>` 的生成与严格解析；HMAC-SHA256 摘要（带域分隔前缀 `nexloop-machine-key-v1:`）。按 C2，摘要只用存储行的 pepper 版本算一次，用常量时间比较；pepper 私有文件解析，未知版本、文件为空、格式不对、版本重复、材料复用、属主或权限不符都失败关闭；`repr`、`str` 和异常里都不含密钥材料 |
| `scripts/ci/prepush-scan.sh` | 第 3 道门（新增行中的凭据值）增加 `nxm_` 密钥形状。`history_audit.credential_expression()` 直接从该脚本读这条表达式，所以全历史审计同步生效 |
| `scripts/ci/test-prepush-scan.py` | 历史凭据值的样例中加入一个合成的 `nxm_` 值（由字符串拼接得到，不是字面量） |
| `tests/test_machine_keys.py` | 31 个用例 |

## 与设计稿的差异

设计稿 §2.2 和 §4 I-9 写的是“在 `.gitleaks.toml` 加 `nxm_` 规则”。但 NX-060 的配置检查（`scripts/ci/check_gitleaks_config.py`）禁止自定义 rules，所以改为在 prepush 第 3 道门里拦截，效果相同：推送前阻断，全历史审计也能覆盖。gitleaks 配置保持不变，测试会断言配置检查仍然通过。

## 测试与变异检查

- `uv run --frozen python -m pytest -q tests/test_machine_keys.py tests/test_gitleaks_config.py tests/test_history_audit.py`：54 passed。
- `uv run --frozen python scripts/ci/test-prepush-scan.py`：全部 PASS（19 秒）。
- 变异检查：每次只改一处，跑 `tests/test_machine_keys.py`，跑完恢复原文件。

| 变异 | 结果 |
|---|---|
| 摘要改用 `peppers.current`，不用存储的版本 | 3 failed |
| `matches` 直接返回 True | 2 failed |
| 允许复用 pepper 材料 | 1 failed |
| 允许空的 pepper 文件 | 2 failed |
| 去掉 `from None`（异常链） | 1 failed |
| `repr` 显示 secret | 1 failed |
| 解析时篡改 secret | 1 failed |
| `fullmatch` 改为 `match(strip())` | **存活**：长度检查与锚定的固定长度正则互为冗余，两处只改一处等价 |
| 去掉长度检查 | **存活**：原因同上，由 `fullmatch` 兜底 |

两个存活变异都属于有意保留的双重检查；同时去掉两者时，`t + 'A'` 等用例会失败。
