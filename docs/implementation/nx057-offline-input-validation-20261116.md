# NX-057 离线规则输入验证（2026-11-16）

基线：`6a5ef7a98a44be8babcba71940419e3391587ff1`；独立分支 `codex/nx057-offline-20261116`。
本切片供原 L6 对固定提交独审；没有宣告 NX-057 完成，没有修改共享 planning。
当前最低未完成阶段为 S6；`planning/acceptance-tests.json` 没有 NX-057 的映射项。

## 已有交付与边界

已检查发布脚本、原有三个离线测试、规则 v1、规则 Python 校验器、ADR-040、设计稿与执行单。
历史交付 `16686822`、`de6ba2e7` 已覆盖配置员窗口发布、版本重放及 passed/recorded revision 区分；本切片不重复实现。

- Python `validate_rules` 仅接受 `origin_expired`、`confirmed_not_delivered`；unknown、permanent_failure、一次性回复拒绝不能被配置为触发类型。
- ADR-040 暂缓 R3：当前仅开放 `reevaluate`/`escalate` 与 `automatic`/`manual`。`manual` 是补救处理模式，不是 NX-056 审批策略；合成规则的离线 valid 不证明其来源已被数据库声明为 external effect。
- `governed_action` 和额外的 `approval_required` 字段被拒。未来目标能力的审批需求仍归 NX-056；此处没有增加或更改审批策略。
- 发布替身抛出 22023/40001/42501 时保留允许的数据库拒绝摘要；08006 或未知错误只输出类型，退出 2，单次调用、不重试。`applied: false` 是命令报错，不证明连接中断时数据库未提交，仍需按既有流程核对。

## 首败与最小修复

新增 LF/CRLF 合成文件回归后，原脚本 `read_text` 转换 CRLF，违反脚本文档“文件精确文本与 sha256sum 一致”的已有约定。
首轮实际结果：`1 failed, 25 passed in 0.61s`，失败项：
`test_file_bytes_are_preserved_for_check_and_publication[\r\n]`。

首败的文件字节 SHA256 为 `547039fe5ac781147af819e9de1641450250d8d2eb40bafa774bfe0bf992419b`，
脚本错误报告 `ccb7a8149c3a6551cb5c9d4e5e3c06bfff9a6f574f5ee7c1c5bdc6de400ac41c`。
断言停在 check 阶段；首轮没有执行该参数后面的发布替身断言。

仅将部署脚本读取改为 `read_bytes().decode('utf-8')`，保留 UTF-8 原文本的换行。
修后同一回归实际结果：`26 passed in 0.39s`；LF/CRLF 的 check 摘要及传给发布替身的字节均与原文件一致。
没有修改通用规则校验器、SQL、迁移、规则文件或产品策略。

## 实际验证命令

在本切片工作树执行（使用已有本机虚拟环境；不安装依赖）。为避免公开本机用户路径，以下用 `$BASE_CHECKOUT` 代指原主检出的绝对路径，执行时使用的是该绝对路径：

```sh
PYTHONPATH=packages/eios-core/src "$BASE_CHECKOUT/.venv/bin/python" -m pytest -q tests/test_remediation_rules_apply.py -n 0
git diff --check
```

pytest 命令实际执行两次：首败及修后结果如上；`git diff --check` 退出 0。
新增用例以 socket connect/connect_ex 与 psycopg.connect 的禁止替身防止意外连接。
所有 publication 调用均为内存替身；无配置员真实窗口、无数据库、无网络、无凭据读取。
没有运行真实 apply 入口、stage、NX-056 审批演练或 OIDC，也没有推送。
这些是纯离线测试证据，不是模拟 stage 成功或真实部署证据。stage 复查与真实审批验收仍在本切片之外。
