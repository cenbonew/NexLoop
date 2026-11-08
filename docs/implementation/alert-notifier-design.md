# 工作台外的告警通知（群机器人 webhook）：设计

- **来源**：负责人 2026-11-08 选定 `docs/implementation/NX-035-file-backup-runtime-wal.md` §6 的方案，通道用群机器人 webhook。起因：`timer_stale_file_backup` 从 10-30 起触发了 11 次，只在工作台可见，一直没人看到。
- **作者**：L4，基于 main `41684401`，分支 `l4/alert-notifier`。设计和代码一起交，送 L6 审。
- **范围**：APP_HOST 上一个只读的通知器，加一个 timer，再加上它自己的 freshness 告警。不改告警评估器，不改已有规则，不新增业务表。

## 1. 组成

| 部件 | 文件 |
|---|---|
| 通知器 | `scripts/deploy/alert_notify.py`：标准库加 psycopg，用 host-health 已有的 venv |
| 读取函数 | 迁移 `alert_notify_export`（临时号 0425，接在批次 133 的 0424 `timers_not_reported` 之后）：`authz.nexloop_alerts_firing_export()`，只授权给 `nexloop_metrics` |
| systemd | `deploy/stage/systemd/nexloop-alert-notify.{service,timer}.example`：每 15 分钟一次（07、22、37、52 分），oneshot |
| 配置示例 | `deploy/examples/alert-notify.example.json` |
| 自身健康 | host-health 新增 timer key `alert-notify`；告警规则 v34（批次 133 的 v33 加一条）新增 `timer_stale_alert_notify`（warning，45 分钟） |
| 运行单 | `docs/release/run-alert-notifier.md` |

## 2. 读什么、用什么身份（要求 1、5）

- **身份：复用 APP_HOST 上已有的只读登录 `nexloop_metrics`**。部署门禁和 `nexloop-doctor --observability` 已经在用它，pg_hba 只允许 APP_HOST 用客户端证书登录（`hostssl … nexloop_metrics … cert`），不新增角色。
- **为什么还需要一个迁移**：`nexloop_metrics` 现在只能执行 `authz.nexloop_metrics_export()`（0131 D4）。快照里有评估器是否过期，但没有“哪些告警正在触发”。所以只新增一个函数、只授权给这个角色，不授予任何表权限：
  - `authz.nexloop_alerts_firing_export()`：只有 `session_user` 为 `nexloop_metrics` 时才能执行，其他登录报 42501；
  - 按租户、world 返回：每条正在触发的告警的 `dedupe_key`、`rule_id`、`severity`、`first_fired_at`、`fire_count`，以及评估器最近一次评估的时间；
  - 不返回 `last_value`、阈值、`selector` 字段，不读业务数据或顾客数据。`dedupe_key` 是规则名加选择器，例如 `timer_stale_file_backup:app_host:file-backup`，内容是 host role、timer 名或 feed 名这类运维标识；
  - 通知器在只读事务里调用（`set transaction read only`），并核对 `current_user` 是 `nexloop_metrics`，不是就拒绝。

## 3. 什么时候发（要求 2）

状态存在本地文件 `/var/lib/nexloop-alert-notify/state.json`（0600，systemd `StateDirectory`），不新增表。键是 `租户/world/dedupe_key`。

| 事件 | 条件 | 次数 |
|---|---|---|
| 新触发 | 正在触发、状态里没有；或者 `first_fired_at` 变了（恢复后又触发） | 一次 |
| 恢复 | 状态里发过通知，现在不再触发 | 一次 |
| 持续 | critical，仍在触发，距上次通知已满 24 小时（`critical_repeat_seconds`） | 每 24 小时一次 |
| 评估器停了 | real world 的评估器超过 10 分钟没有评估（这时不会有任何告警触发） | 按一条 critical 处理，同样的规则 |

- 一轮里所有事件合成**一条消息**，最多列 40 条，其余写“另有 N 条”。
- `levels` 默认是 critical 和 warning。不在 `levels` 里的告警会记进状态但不发；它恢复时也不发。
- **只在 webhook 接受之后才写状态**。发送失败时状态不变，下一轮同样的事件会再发一次。
- 状态文件损坏时从空开始：正在触发的告警会各再发一次，这比漏发好。

## 4. webhook（要求 3）

`format` 四选一，用配置选择：

| format | 请求体 | 成功的判断 |
|---|---|---|
| `generic` | `{"source":"nexloop","host":…,"events":[…],"text":…}` | HTTP 2xx |
| `feishu` | `{"msg_type":"text","content":{"text":…}}` | 2xx，且应答的 `code`（或旧版的 `StatusCode`）为 0 |
| `wecom` | `{"msgtype":"text","text":{"content":…}}` | 2xx，且 `errcode` 为 0 |
| `dingtalk` | 同 wecom | 2xx，且 `errcode` 为 0；机器人如果设了“自定义关键词”，关键词用 `NexLoop`，消息开头总是带它 |

- 飞书和钉钉的“加签”安全设置这次不支持；需要时另行扩展（再加一个 0600 的密钥文件）。
- **URL 只在 0600 文件里**（root 所有，`O_NOFOLLOW`，必须是普通文件，权限必须是 0600，内容必须是 https）。它不在仓库、unit、进程参数、日志或错误信息里：
  - 发送失败时只记录异常类名和 HTTP 状态码。urllib 的异常文字可能带 URL，所以从不打印它；
  - 测试用一个带标记的合成 URL，断言输出和错误流里都没有这个标记。

## 5. 失败不静默（要求 4）

读取失败或发送失败时：

- 向 stderr 写一行 `<3>alert-notify …`（journal 里是 err 级别，可以用 `journalctl -p err -t nexloop-alert-notify` 查到），以退出码 1 结束，unit 失败；
- 定时运行没有成功，host-health 就看到 timer `alert-notify` 过期。超过 45 分钟，规则 v34 的 `timer_stale_alert_notify` 触发（warning）。它在工作台可见，NX-055 E3 检查也会报 freshness fail；
- 通知器没法通知自己坏了，所以兜底靠工作台和 E3 检查。

**与 L2 的 `timers_not_reported` 对齐**（批次 133，迁移 0424、规则 v33 的 `timer_not_reported`）：它读规则里 `timer_stale_%` 的选择器，所以 v34 的 `app_host:alert-notify` 会被计为一个应上报的 timer；host-health 配置里漏掉 `alert-notify` 时，`timer_not_reported` 会报出来。不需要额外接线；`tests/test_timers_not_reported_pg.py` 已在 v34 上跑过。

## 6. 测试（要求 7）

- `tests/test_alert_notify.py`（纯函数，用替身读取器和替身机器人）：
  - 去重：新触发只发一次，之后几轮不发；恢复发一次；
  - 恢复后再次触发，按新告警发；
  - critical 每 24 小时发一次，warning 不重发；
  - 发送失败：网络异常、HTTP 500、钉钉 `errcode≠0` 三种，都以退出码 1 结束，写 err 行，不写状态；下一轮成功重发；
  - URL 和 DSN 不出现在输出或错误里；
  - 四种格式，以及机器人应答 200 但没有成功码时判为失败；
  - levels 过滤；评估器停了；dry-run 不发也不写状态；
  - 配置被拒的几种情况（格式、多余键、级别、重发间隔太短、非 https、URL 文件权限不对）；
  - 仓库里的示例配置能够装载。
- `tests/test_alert_notify_pg.py`：`nexloop_metrics` 只读到正在触发的告警，字段只有那五个（不含 selector 和 value），已恢复的不列出；`nexloop_api` 和 `nexloop_domain_worker` 都被拒。
- `tests/test_host_health_install.py`：示例配置与 v34 的 `timer_stale_*` 选择器双向一致。

## 7. 不做

- 不改告警评估器，也不在 SQL 里发通知：SQL 不连外网。
- 不发顾客或业务内容。消息里只有规则名、级别、选择器、首次触发时间和次数，以及“详情见工作台告警页”。
- 不做多通道、按人路由或值班表：负责人只选了一个群。
