# 本地任务提交纪律与补录说明

负责人要求补建 S0–S2 回滚点。此次提交按当前文件的任务职责分组，是事后补录，不伪造原阶段开发时间或各中间提交独立通过 CI 的事实。共享组合文件具有后续任务依赖；完整集成基线以最终检查点为准。

每项完成任务必须具备本地实现提交和 planning/tasks.json 的 commit evidence。in_progress 检查点不等于 done；状态变更与实现提交在同一工作步骤完成，提交后立即记录 hash 并运行交接校验器。只在本地提交，未经负责人明确指令不得 push。

提交消息使用 NX-0xx 或相邻任务范围。实现提交的 hash 逐项记录；最后的证据汇总提交只收录前述实现 hash，不要求不可能的提交自身 hash 自引用。

运行产物与依赖安装目录不作为源码入库。CI 按需重建被忽略的 .ci-results/、dist/；Host 打包的专属 staging 使用 TemporaryDirectory，在成功或异常退出时清理。源码交接附件保持 docs/tmp/ 忽略。

本次 source 校验依据：head0048 完整 CI 为1265 Python+24 Pi+33 Runtime；新 head0051 集中回归154 Python与61 Node通过。head0051 完整 CI 尚未执行，NX-016保持 in_progress。清理前已将报告数量、摘要与 SHA256写入公开 evidence，原始运行产物按负责人要求删除，不转移到其它隐藏目录。
