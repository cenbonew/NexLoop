# 运维、备份与灾难恢复手册

## 1. 运维目标

在明确证据和授权下恢复服务，不通过“重启所有东西”“清空队列”或盲目重发外部请求掩盖故障。所有以下命令入口为Codex需实现的运维契约；本包不包含已安装的服务工具。

## 2. 健康检查

`/health/live` 只报告进程活着；`/health/ready` 检查当前服务所需的DB/catalog、身份配置、Artifact、Run owner等。API不可因Valkey缓存暂不可用就假装数据写成功；可以明确cache-degraded。关键Action执行依赖不可达时 readiness/dispatch gate fail closed。

监控：DB连接/事务/磁盘/WAL归档延迟；任务积压与最老age；Run活跃数；模型/工具延迟；Action unknown数量与age；执行权限拒绝；承诺逾期；抽取/索引水位；备份年龄/恢复校验；CI资源；语义版本发布。

## 3. 日常操作

查看一个Run时从event_id→goal/context→submission→action receipt→provider evidence串联。不要求读取所有原始客户对话；需要原文时按受权Artifact访问。

暂停新Run与暂停外部dispatch分别操作。维护停机先禁止新dispatch，等待短任务完成，记录unknown，再停止Agent Host、Worker和API。恢复先检查数据库和账本，再逐步启用服务，最后开放scope。

## 4. 备份分级

**开发/合成演示。** 每日逻辑pg_dump（custom格式）＋全局角色/配置的受控备份＋迁移前备份；使用加密归档并传至另一主机。至少做一次空实例恢复。此级别RPO可能达24h，不适用于声称低数据损失的真实运营。

**受控真实试运行。** 采用pgBackRest完整/增量或差异备份＋连续WAL归档到APP_HOST专用备份目录。目标数据库RPO≤5分钟、RTO≤2小时，必须以故障和恢复演练验证；设置archive_timeout不等于永远达成RPO，归档积压必须告警。[S15]

Artifact与Runtime文件从APP_HOST加密备份至DATA_HOST。目标文件RPO≤15分钟；闭合Run文件可直接按manifest备份，活跃SQLite用受支持backup API或暂停/关闭后连同一致状态处理，不能仅复制主db文件漏WAL。

备份清单记录DB时间/LSN、各schema版本、release SHA、Artifact hash清单、Run store refs、encryption key reference和删除水位。密钥与备份不放同一可公开位置，CI不能读取。

## 5. 一致性与无法保证的部分

PostgreSQL、Artifact、Pi本地检查点和外部服务不是一笔分布式事务。备份可以分别更近，但恢复必须比较它们的时间和引用，处理缺失/超前状态。关键外部业务结果以可核对证据确认，不能仅恢复一个SQLite文件就宣布任务完成。

两主机互备只覆盖部分单机故障，不是异地/离线备份。正式对外服务前新增独立加密备份目的地与恢复凭据管理。

## 6. 恢复流程

1. 确认精确目标环境、恢复点和备份校验；恢复到新目录/新实例，先不覆盖故障原卷。
2. 强制 `real_dispatch_enabled=false`，停止原写入端，隔离所有可能的旧Host owner。
3. 恢复DB/catalog并核验受限角色、策略和schema；恢复Artifact与Run存储清单。
4. 先应用删除/撤权清单；旧备份里的消费者数据和授权不能直接重新生效。
5. 对恢复点之后的外部效应窗口进行渠道对账。若DB回退丢失意图记录，不能重新生成不同ID盲发；根据外部账单/消息reference重建，无法核实时保持受影响scope阻断并人工处置。
6. Run存储超前于DB、丢失或不兼容时标记不一致，先对账再创建新的reevaluation；不要让过时Pi任务自行恢复写入权限。
7. 执行合成smoke、跨租户安全、重复事件与恢复测试，确认单owner。
8. 负责人/维护者按范围恢复real dispatch，记录实际RPO/RTO与仍未解决事项。

## 7. 常见故障处理

| 症状 | 先检查 | 禁止做法 |
|---|---|---|
| DB不可用 | TLS、连接池、磁盘、PG健康与catalog | 改用Memory假装服务正常 |
| API Key写实例403 | service/agent许可链与资源发布 | 换superuser或伪造human cookie |
| Action unknown | receipt、provider reference、幂等查询 | 自动换key重发 |
| 收到重复事件 | provider_event_id、inbox唯一约束 | 删除去重记录后继续 |
| 任务不运行 | due_at、lease/fence、leader、queue limits | 直接把所有任务state改queued |
| 模型超时 | Provider状态、预算、请求大小、重试标记 | 无上限更换模型重试 |
| 缓存不可用 | Valkey/TLS、容量；PG fallback水位 | 把缓存作为任务唯一记录 |
| 索引滞后 | outbox、source_revision、重建job | 用旧索引绕过最新联系限制 |
| Runtime文件被占用 | 单Host锁、孤儿进程、部署状态 | 两进程同时打开同一文件 |
| 磁盘告警 | WAL/backup/artifact/CIcache分类 | 删除未知数据卷或未归档WAL |

## 8. 日志与证据导出

正常日志只记录IDs、状态、耗时和脱敏错误；模型完整请求、消息原文存受权Artifact。发布和故障导出包含必要证据hash和版本，默认不包含客户原文、secret或完整environment。

## 9. 演练频率

试运行前完整恢复一次；之后每月隔离恢复一次，每次重大Schema/Pi存储升级后重复。每天校验备份年龄/归档延迟，每周检查保留和磁盘增长。频率为初始运维计划，可依据实际风险调整并记录。
