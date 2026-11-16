# CI 615 测试副本存储修复

基线：`300b4c413b6b12839d4835bf148bb87f1b2277a6`。仅改测试生命周期和 case 划分，无业务代码、迁移、配额或 planning 变更。

## 证据与定位

CI 614、615 的完整 integration-parallel JUnit 都显示 `test_public_and_stage_preflight_agree_on_a_win32_only_package_and_on_a_real_difference` 遇到 `OSError: [Errno 122] Disk quota exceeded`；stage preflight 的许可/工具及过期 waiver 测试也在两轮遇到 EDQUOT。不能将级联错误全部归因于某一个测试。

615 的在途目录采样：单轮 pytest 根 2,595,124 KiB（约 2.475 GiB），`test_missing_conditions_fail_the_preflight` 单目录 612,768 KiB（约 598.406 MiB）。采样不是整轮绝对峰值。614 的本地证据没有同口径的在途逐目录采样，故不能据此宣称两轮峰值增长比例。

精确路径：`scripts/release/release.py::_clean_checkout` 为每次 preflight 创建完整 `source.tar` 并解压 `checkout`；`tests/test_release_compliance.py` 一个 case 原调用六次，public/stage 比对 case 调用四次；`tests/test_release_build_archive.py` 的 stage 条件 case 调用四次。成功 case 的 `tmp_path` 随后仍累积保留至整轮结束。许可资产测试也执行完整真实归档；NX-052 census 则在自己的 `tmp_path` 保留两份 PG 快照。

## 最小改法与边界

新增 opt-in fixture `reclaim_passed_tmp_path`（`tests/support/passed_tmp_cleanup.py`），仅登记 pytest 本 worker 直接分配的 `tmp_path`。hook 在所有 fixture finalizer 完成且 setup/call/teardown 三阶段全部 passed 后才回收该目录；失败、跳过和 teardown 异常均保留首败产物。封存发布目录先通过既有 `make_writable` 恢复可删除权限，不跟随子符号链接；替换为符号链接的根拒绝删除。清理错误变成可见 teardown 失败，不伪装成功。

仅两份 release 测试文件整体启用，许可资产归档和 NX-052 census 各一例显式启用。未启用的测试、共享模板、历史目录和其他工作树均不清理。六种 missing condition、四种 stage condition 拆为独立参数 case；public/stage 的直接比较保留，每种 difference 一个 case（两份副本）。所有原有断言、真实归档和解压保留，未用 stub 替换这些校验，也未改生产 preflight 的产物保留契约。

失败 case 仍可能保留多份副本，此为保全证据的有意边界。未扩展为全局 pytest retention 策略，也未降低并发或资源门槛。

## 同基线本地定量回归

使用现有项目 Python、Node 24；每轮启动前检查 load1 < 12，pytest `-n 2`。baseline 与修复版均对同一提交的完整源归档执行真实文件校验，不能将文件系统 allocated 数值直接外推到 CI 文件系统。

以辅助 probe 在每 case call/teardown 报告边界读取普通文件 `st_size` 与 `st_blocks * 512`；不跟随符号链接。以下是阶段采样高水位，**不是持续采样的瞬时绝对峰值**。两个 worker 高水位之和是本轮阶段采样的上界，未声称发生在同一时刻。

| 同口径指标 | 修改前 | 修改后 |
|---|---:|---:|
| 单 case 最大 allocated | 627,474,432 bytes（598.406 MiB） | 209,158,144 bytes（199.469 MiB） |
| missing conditions 单 case allocated | 六份合计 627,474,432 bytes | 每条件约 104,579,072 bytes（99.734 MiB） |
| 两 worker allocated 高水位之和 | 2,651,541,504 bytes（2.469 GiB） | 313,737,216 bytes（0.292 GiB） |
| 启用清理的目标 case teardown 后累积 logical | 2,410,836,086 bytes | 0 bytes |

修复后两个 worker 最终合计仍有 36,388,864 allocated bytes：未启用清理的其他许可测试及生命周期自测的合成证据保留，不以清空全部测试根为目标。

实际命令：

- 基线：`PYTHONPATH=packages/eios-core/src:tests:.ci-results python -m pytest -q -n 2 -p storage_probe tests/test_release_compliance.py tests/test_release_build_archive.py tests/test_license_compliance.py --basetemp=.ci-results/storage-before-tmp --junitxml=.ci-results/storage-before.xml`，另通过 `FIXTURE_STORAGE_REPORT` 指定 probe 输出。75 passed，33.41s，启动 load1 8.430。
- 新增生命周期测试首轮：`PYTHONPATH=packages/eios-core/src:tests python -m pytest -q -n 2 tests/test_passed_tmp_cleanup.py --junitxml=.ci-results/cleanup-first.xml`，4 failed、2 passed，1.14s。四项失败来自自测 glob 同时读取 pytest 的 `current` 符号链接，重复计数同一证据；修正自测过滤，原始失败日志保留。
- 修复回归：基线三个文件加 `tests/test_passed_tmp_cleanup.py`，同上 probe 参数，使用独立 `storage-after` 输出及 basetemp，90 passed，25.71s，启动 load1 9.391。75→90 = 参数拆分增加 9 case + 新增 6 个生命周期测试。
- `PYTHONPATH=packages/eios-core/src:tests python -m pytest --collect-only -q tests/test_nx052_e2e.py`：20 tests collected，1.22s。NX-052 实际 PG 执行是 not_run，遵守本次禁止 PG 的派工边界。

新增测试覆盖成功 case 跨例零积累、finalizer 仍可读取文件、只读树、子符号链接不碰邻居、未启用目录保留、setup/call/teardown 失败及 skip 证据保留、清理异常显式失败。子 pytest 中的故障均为合成测试，外层验证其预期失败，未跳过实际产品断言。

原始日志、JUnit、逐 case probe JSONL、汇总 JSON 及 probe 源保存在本工作树忽略目录 `.ci-results/`。曾两次因 load1 超过 12 被启动门禁拒绝，未启动测试。未执行真实 PG、真实 READ、服务器操作、历史目录清理、部署或 CI 重跑。本线证据不代表 CI quota 已恢复或整轮 CI 通过；L6 与集成判定由调度线完成。未分配新的 AT 编号。
