# AT-048 运行单证据判读修复

基线 ccffcad90494da8a947bc17bdfbf8b517c7d641b。NX-038/NX-039仍为in_progress，AT-023/048仍为not_run；本修复不修改正式验收，也不授权stage执行。

## 具体缺陷与最小修改

AT-048原实现允许缺路线时只记note，剩余阳性可使总结果passed。0303开评入口对既有evaluating候选先返回replayed，不重做泄漏检查；运行单仅判断opened，未区分这种历史回执。现改为：四路线缺任一项，在任何propose/freeze/open前blocked；只对AT-048增加冻结包与当前plan的candidate、输入快照、两侧cases和groups比对；每路记录候选、输入快照/cohort/冻结包摘要及两个replayed标记。阳性开评重放记blocked，不新增SQL或修改已发布迁移。负例的冻结重放与开评重放分开，拒绝响应缺replayed保留null。

现有历史服务端修复0286/0303、四路线PG用例及固定P5/独立AT048窗口运行单继续使用；没有发现相同的在途脚本修复。既有可用工作树相关文件未见未提交改动；26个失效工作树登记未清理，也不据此声称检查了不可读工作树的文件。

## 本地验证

新增11项无数据库的判读/冻结绑定用例；在基线脚本上选取缺路线和旧阳性用例，5项均按预期失败。修复后连同现有test_s6_stage_evolution_pg.py全文件共18项通过（11条既有模型字段警告）。PG为tests/conftest.py管理的本机一次性数据库，未连接现场或stage。

首次基线复现子进程因macOS继承__PYVENV_LAUNCHER__选错标准库失败，清除该子进程变量后取得上述5项真实复现。首次PG回归17通过/1失败，原因是新增断言把拒绝响应未提供replayed误写成False；改为如实断言None，随后18/18通过。原始日志由调度私有证据保存。git diff --check通过。

AT-023现有完整本地PG用例核对受保护表前后digest相等，L0/L1接受只到accepted_for_release，拒绝分支由缺测量触发；本次不更改其实现。L1依旧是D16合成runner，不能声称真实模型或stage验收。

## 仍需的真实前提

按run-nx038-stage.md P1–P6：指定部署/迁移、reader/writer/evaluator与test-only凭据、有效evolution预算及受治理owner授权、足够的合成cohort、只读配置和执行许可。AT048必须实际具备同顾客多Run、跨顾客近重复对及其余顾客，四路线齐备；保留独立S6_AT048窗口，不改变其他段的P5窗口。新cohort/label不能自动生成或重发；旧阳性回放不能作为修复后的重新检测。部署、stage执行、正式验收与发布均仍待原闸门。
