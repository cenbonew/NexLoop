# 签名拒绝原因（迁移 0276）

## 背景

2026-10-20 stage 故障期间，`queue signature denied` 这一句报错同时对应 6 种失败原因：key、protocol、resource、两种时间窗口、digest、HMAC。由于无法区分，排查花了很长时间。

0276 之后，签名关卡的 **errcode 仍是 42501，报错主文字也不变**，客户端照旧按主文字判断。失败的具体条件放在 PostgreSQL 报错的 `DETAIL` 中，取值是下面这组固定枚举：

| DETAIL | 含义 |
|---|---|
| `key_unknown` | claims 中的 `key_id` 不存在，或者不是 active |
| `protocol` | protocol 不是该入口要求的值 |
| `resource` | `resource_id` 不是该入口要求的值 |
| `expired` | `expires_at` 缺失、无法解析，或者不晚于数据库的 `clock_timestamp()` |
| `too_far_ahead` | `expires_at` 超过了 `clock_timestamp() + 25 s`，通常是客户端主机的时钟比数据库主机快。此时 `HINT` 为 `ahead_by_seconds=N.N`，表示超出上限的秒数，取整到 0.1 秒 |
| `digest` | `parameters_digest` 与请求参数不符 |
| `signature` | 以上各项都没问题，但 HMAC 不符：key 材料不同，或者签名内容被改过 |
| `other` | 同一道关卡里与签名无关的条件，例如调用方角色、world、尺寸。目前只有 context v6 的第一道关卡会给出这个值 |

`DETAIL` 和 `HINT` 只包含上表中的枚举值和秒数，**不包含**任何 key、参数、claims 内容或请求值。

如果同时有多个条件失败，只报第一个，顺序为：`key_unknown`、`protocol`、`resource`、`expired`、`too_far_ahead`、`digest`、`signature`。

## 覆盖范围

- **本次已覆盖的签名关卡**（函数体按集成头逐字复制，只修改报错那一句，已在 `tests/test_function_copy_guard.py` 中登记）：
  - `authz.nexloop_queue_command_before_refusals_v0150`：durable queue，报错主文字 `queue signature denied`；
  - `authz.nexloop_runtime_activation_command_v0037`：runtime activation，报错主文字 `activation denied`；
  - `authz.nexloop_effect_execution_command_v0052`：effect execution，0044 的关卡，0053 中改名，报错主文字 `effect provider authority unavailable`；
  - `authz.nexloop_context_v6_command`：context v6 入口，函数体是 0227 的最新版本，两道关卡都已覆盖，报错主文字 `context v6 unavailable`。
- **尚未覆盖**：同样用一句报错覆盖多个签名条件的关卡，盘点时共找到 44 个函数定义，其中有改名前的旧版本。尚未覆盖的有：action claim、action definition、object read、relation link、orphan sweep、effect context、effect intent（及其各个版本）、message runtime、assessment、receipt reconcile、claim feed 和 claim match、review read 和 review decision、outbound message、derived reads、context strategy、plan port、workbench read、observe 和 audit、goal permit、work feed、conversation claims 和 turns、relay、message run issuance、schema publication 和 migration。
- 以后按需逐个处理：使用同一组枚举和两个 helper 函数（`authz.nexloop_signed_denial_cause`、`authz.nexloop_signed_ahead_hint`），每改一个，就在复制件守护中登记一条 `denial(函数名, 报错文字)`。

## 在 PG 日志中查看原因（仅说明，不改服务器配置）

- `log_error_verbosity` 的默认值是 `default`。在这个设置下，每条 ERROR 记录后面会跟着 `DETAIL:` 和 `HINT:` 行（有内容时才出现），所以**不需要改配置**就能在 DATA_HOST 的 PG 日志中看到原因，例如：
  ```
  ERROR:  queue signature denied
  DETAIL:  too_far_ahead
  HINT:  ahead_by_seconds=0.1
  CONTEXT:  PL/pgSQL function authz.nexloop_queue_command_before_refusals_v0150(...) line 20 at RAISE
  ```
- 只有在 `log_error_verbosity=terse` 时，DETAIL 和 HINT 才不会写进日志。可以先用只读语句确认当前值：`show log_error_verbosity;`。如果确实是 terse，要改成 default，需要负责人决定后改 `postgresql.conf` 并 reload，本次不做。
- 如果日志是 csvlog 或 jsonlog 格式，原因写在 `detail` 和 `hint` 字段中。
- 按原因统计的示例：`grep -A1 'queue signature denied' <日志> | grep DETAIL | sort | uniq -c`。
- 客户端（Python）收到的异常也会带上这两项：`error.diag.message_detail` 和 `error.diag.message_hint`。目前客户端的错误处理不读这两项，行为没有变化。如果以后要在应用日志中记录，只记录这两项的值即可，因为里面没有敏感内容。
- `post_deploy_check` 的日志检查目前统计的是 tick failed、Traceback、42883 等类别。今后可以在其中加一类：按签名拒绝原因计数，例如 `too_far_ahead` 出现次数大于 0 即告警，提示检查主机之间的时钟偏差。这一项本次没有改，留作后续。
