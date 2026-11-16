# PG fixture setup failure cleanup

基线：`da78698de57c0c84beb400f1ea8270e46b1a3ae4`。本次仅修测试基础设施，无迁移、业务行为或 planning 状态变更。

## 缺口与修复

`tests/conftest.py` 在进入服务器生命周期的 `try/finally` 前执行 `initdb` 或 `copytree`。这些步骤失败时，本次 `mkdtemp` 创建的部分目录没有被回收；模板初始化也有同样缺口。基线尚未修复。

新增仅供 setup 失败路径调用的清理函数：限定为刚创建的临时根目录，在服务器启动前回收。清理失败给原异常附加异常类型说明，随后继续抛出原异常。分配目录自身失败不触发删除；服务器启动后的处理与正常模板缓存路径保持原样。此修复不处理未知启动状态下的进程或历史残留目录。

## 离线验证

命令：`PYTHONPATH=packages/eios-core/src:tests python -m pytest -q -n 2 tests/test_pg_fixture_setup_cleanup.py`，各轮另加 `--junitxml` 保存报告。使用现有项目 Python 环境；启动测试前确认 load1 小于 12。

首次运行：7 failed、2 passed（0.44s）；其中一项是新增测试未正确推进 session fixture teardown，已修正该测试调用。修正测试并增加目录分配失败对照后，未改源码的基线为 6 failed、5 passed（0.50s），六项均复现 setup 清理缺口。修复后 11 passed（0.65s）。原始日志与 JUnit 保存在忽略目录 `.ci-results/pg-fixture-cleanup-{first,baseline,final}.{log,xml}`。

测试使用 stub 命令、stub 连接和合成临时文件：覆盖初始化与复制失败、清理失败时保留原异常、既有目录不受影响、分配失败不清理、正常路径 teardown 以及模板缓存与 session 清理。没有执行真实 PG、远端操作或清理既有目录。

此证据只证明本地 setup 失败路径修复，不代表 CI quota 根因已定位或配额恢复；完整 CI、实际 PG 和 L6 复核未在本线执行。未分配新的 AT 编号。
