# NX-030 审计指标与告警：后端实现说明

设计稿 `NX-030-design.md`（D1–D8 全部定案）。分支 `nx030-impl`，切自 `nx028-reviewer`（基于 main `a5d7c48`），带上设计稿三个提交。迁移临时号 `0151`（调度员指定 0151–0159），合并时由调度员重排。测试用合成数据和真实 PostgreSQL；派发拒绝的测试复用 NX-022 的真实派发场景，不做改动。

## 1. 迁移 0151_nx030_observability

- **指标快照** `runtime.nexloop_metrics_snapshot(tenant, world)`：owner-only，所有指标只在这里计算一次。评估器、工作台、导出三方共用这一个函数。
  - 内容只有代码、计数、年龄和比率，不含原文、客户名或属性值。
  - 覆盖的区块：
    - `refusals`：最近 5 分钟、最近 1 小时，按代码；
    - `effects`：按状态计数，以及 unknown 的数量、最老年龄、超过 24 小时的数量；
    - `replies`：待回复的数量和最老年龄、升级按原因；
    - `commitments`：异常按原因，及最近 1 小时的新增；
    - `queues`：`runtime.jobs` 按队列统计 pending / retry_wait / running / 死信、最近 1 小时死信、最老到期时长；
    - `feeds`：work feed 按 feed 统计；
    - `extraction`：未入队的消息；
    - `guard` / `host` / `pool`：最近 5 分钟的进程样本，没有样本时为 unavailable；
    - `connections`：`pg_stat_database.numbackends`；
    - `cost`：预算比例，以及 NX-027 成本最近 24 小时的金额，按 cost_kind × data_mode × 币种统计；
    - `backup`：NX-035 之前固定为 unavailable；
    - `evaluator`：最近评估时间、规则版本、是否过期。
- **D8 派发拒绝** `runtime.nexloop_dispatch_refusals`：只追加、FORCE RLS，代码表含 NXC01–06、NXB01–03、NXM01。
  - **Run 路径**：`authz.nexloop_queue_command` 改名保留为 `_before_refusals_v0150`。新包装在 `finish` 同一事务里，当 `result.code` 是拒绝原因时追加一行；同一任务只记一次（重放的 finish 不再记）。
  - **effect 路径**：被拒的准入会整体回滚，**并没有可写的“终态记录”**（设计稿 D8 的假设与代码不符）。因此由 effect worker 在拒绝之后另开一个事务调用 `authz.nexloop_record_effect_refusal`。
    - 只接受执行者本人的 intent；
    - 同一 intent、同一代码在 10 分钟内只记一次，因为 worker 每次 claim 都会重试；
    - 派发判定和结果都不变，仍返回 `admission_unavailable`。
    - **调度员裁定（接受）**：这一写入可能失败，例如连接中断时。失败时只会让指标偏少，派发安全不受影响：拒绝已经发生，并且已经回滚。
- **进程样本** `runtime.nexloop_process_samples`：部署级，无租户列；FORCE RLS 加 owner-only 策略，应用角色没有任何权限。
  - 写入入口 `authz.nexloop_record_process_sample`：按种类只允许白名单键，值只能是数字。
  - `runtime.nexloop_observability_maintain()` 做小时汇总和保留（D5：分钟样本 7 天、小时汇总 90 天），由评估器每轮调用。
- **告警**：
  - 规则 `control.nexloop_alert_rules` 只追加，只能经 configurator 函数写入，版本必须递增，同版本同内容视为重放；
  - 状态 `control.nexloop_alert_state` 按规则和选择器去重；
  - 事件 `control.nexloop_alert_events` 只追加，记 firing / resolved；
  - 评估 `authz.nexloop_alert_evaluate`：服务签名，Action 为 `nexloop.alert.evaluate:1`，只在 `real` world 评估（D6）；
  - 规则的 metric 是快照里的点分路径，可带一个 `*` 段以遍历子对象，`selector` 选取 map 中的一个键；
  - `for_seconds` 用 `breached_since` 实现：持续满足条件这么久才触发；条件消失或选择器消失时记一次 resolved。
- **工作台读取（D7）**：独立函数 `authz.nexloop_workbench_observe_read`，不改 0117。
  - 动词 `metrics`、`alerts` 需要 `nexloop.workbench.read`；
  - `human_actions` 只给 owner：汇集控制事件、承诺人工事件、人工请求、接管与交还、员工回复、审核决定，以及 ADR-025 的读取审计，只给 ID、类别和时间。
- **D4**：新建 `nexloop_metrics` 角色（login），只能执行 `authz.nexloop_metrics_export()`，读不到任何表。

## 2. Python

- `nexloop_eios/observability.py`：
  - 规则校验与 configurator 应用；
  - `AlertEvaluator`；
  - 样本写入；
  - `GuardTimings`：达到 2 s 的调用记为超时；
  - 连接池统计（`pop_stats`）；
  - 定时采样线程；
  - Prometheus 文本渲染：map 中的键作为 `key` 标签，只输出数字；
  - loopback 导出服务（标准库 HTTP，只绑 127.0.0.1，默认不启动，D1）；
  - CLI：`python -m nexloop_eios.observability rules --check|--apply`、`export`。
- `background_services`：新增 `nexloop-alert-evaluator`（domain_worker，凭据每轮重读并重新认证）；compose 的 `background` profile 和容器入口同步追加。
- 守卫：`runtime_control` 的 `do_POST` 计时；`runtime_guard_worker` 每分钟写入 guard 时延样本和连接池样本，尽力而为，失败不影响守卫本身。
- `effect_execution.prepare_effect_dispatch`：拒绝时追加记录拒绝行，结果不变。
- 工作台：
  - `WorkbenchReader.observe`；
  - `WorkbenchQueries` 新增 `metrics`、`alerts`、`human_actions`；
  - 路由 `GET /api/v1/workbench/{metrics,alerts,human-actions}`；
  - 总览的“队列积压”块改为真实数据（原来是 unavailable）。
- 配置：
  - `deploy/configuration/alert-rules.v1.json`：D3 默认阈值，21 条；
  - `service-grants` v13：新增 `alert_evaluator` 主体，只授 `nexloop.alert.evaluate:1`。

## 3. 测试

- `tests/test_dispatch_refusals_pg.py`（4 个）：原样复用 NX-022 的真实派发场景。
  - effect 路径：暂停被拒（NXC01），恢复后快照过期又被拒（NXC02），各记一行，重新提交后成功、不再记；
  - Run 路径：暂停（NXC01）、预算耗尽（NXB01）各记一行，任务 id 对得上；成功的 Run 不记；
  - 拒绝表只追加。
- `tests/test_observability_pg.py`（5 个）：
  - 快照只含代码和计数，响应中没有原文；顾客 403；
  - 总览的积压块有数据；
  - 评估器：首轮触发；二轮不重复；死信移出时间窗后 resolved；连接池等待需持续 5 分钟才触发；
  - operator 能看告警，看不到人类 Action 审计；owner 看审计时能看到读取记录；
  - 规则只能由 configurator 写入：同版本重放、改版本必须递增、非法规则被拒、API 角色调用被拒；
  - 进程样本只收数字；
  - `nexloop_metrics` 读不到表、只能调用导出函数，其他角色不能调用；loopback 端点返回文本格式，不含原文。
- `tests/test_observability.py`（2 个）：随附规则覆盖 D3 阈值；CLI 与渲染只输出数字。
- 同步更新的已有测试：
  - `test_workbench_read_pg`：积压块由 unavailable 改为 ok；
  - `test_community_container_contract`：background 服务清单加入 alert-evaluator。

## 4. 未完成项（需排期或决定）

1. ~~Agent Host 并发样本（M09）~~：已在第二部分完成，见 §5。
2. **告警静默**（owner 专属 Action `nexloop.alert.silence:1`）：要在 0124 的统一入口注册表、business-actions 和角色清单里各加一项，这三处都与 L4 重叠，等 nx028-ui 合入后与告警页一起做。
3. **工作台的告警页、运行状态页和审计页**：按调度员要求，等 L4 的 nx028-ui 合入后再挂；后端接口已就绪。
4. ~~AT-049 统一结构化日志与哨兵扫描、doctor `--observability`~~：已在第二部分完成，见 §5。
5. **备份年龄（M12）**：等 NX-035 的备份清单表。
6. **已知限制：连接数不按角色细分**。调度员裁定不授予 `pg_read_all_stats`，因为它会让 owner 能读到所有会话的查询文本。总连接数继续取自 `pg_stat_database`。

## 5. 第二部分（调度员排期：M09、AT-049、doctor `--observability`）

- **Agent Host 并发样本（M09）**：只改了 `run-admission-gate.ts` 和 `main.ts`。
  - `run-admission-gate.ts` 维护进程级计数：已登记的 gate、admit 次数、等待超时次数，以及活跃数、排队数、上限。
  - `main.ts` 新增 `GET /internal/v1/metrics`。它与其他 internal 路由一样只接受 loopback，要求 Host 头正确、没有 Origin 和 Cookie，并要求 Bearer 内部密钥；返回内容只有数字。没有 runtime 配置时返回 `{"host":{"available":false}}`。
  - Python 侧 `host_control.read_host_metrics` 沿用健康探测的做法（不走代理、不跟随重定向、要求 Bearer），只接受规定的数字字段。
  - `observability.HostCounters` 把累计计数换算成每个窗口的增量；Host 重启后以当前值作为增量。
  - runtime worker 每分钟拉取一次并代写样本。guard 为单进程时，同一处也写 guard 时延；多进程时子进程自己写。
- **AT-049 统一结构化日志**：新增 `nexloop_eios/structured_log.py`。
  - 每个进程入口都调用 `configure(service)`：api、background services、runtime worker、effect worker、runtime guard、message relay、outbound recorder、metrics。uvicorn 用 `uvicorn_log_config`。
  - 事件名只取日志调用的模板，从不取格式化后的参数。
  - detail 只取单个 JSON 对象参数里白名单内的字段；字符串必须像代码或 ID，否则替换为 `[redacted]`。
  - 异常只保留类名，不输出堆栈、消息和 SQL。
  - 现有各进程固定输出的摘要行（本来就只有计数）保持不变。
- **哨兵扫描**：`tests/test_log_secrets_at049_pg.py` 在真实的 NX-047 链路上运行：HTTPS API、relay、带真实 Host/Pi 的 Runtime Worker、effect worker、JSON 投递服务、recorder。
  - 顾客消息中带哨兵原话和哨兵密钥。
  - 失败分支：同一幂等键改写内容（在 SQL 中抛出冲突，409）、超长正文（422）、用哨兵密钥伪造 Bearer（401）。
  - 断言：所有进程的 stdout 和 stderr、PostgreSQL 服务端日志里都没有这两个哨兵；原话只存在于应该存放的地方（顾客的 Message）。
- **doctor `--observability`**：只通过只读角色 `nexloop_metrics` 读取。
  - 检查各活跃租户 real world 的评估器在最近 5 分钟内跑过，库中规则版本与文件一致，进程样本在持续写入。
  - 备份在 NX-035 之前报告为 unavailable，不算失败；用 API 角色连接会被拒绝。
- **测试**：
  - `tests/test_host_metrics_pg.py`（3 个，真实 Host）：真实 Run 后 runs_started 加 1；错误密钥、伪造 Host 头、未鉴权都拿不到数据；无 runtime 时为 unavailable；Host 重启时的计数处理。
  - `apps/agent-host/test/run-admission-gate.test.ts` 新增计数用例。
  - `tests/test_structured_log.py`：参数、异常、未知字段、psycopg 和 uvicorn 的日志都不会带出原文或密钥。
  - `tests/test_log_secrets_at049_pg.py`：上面的哨兵扫描（AT-049）。
  - `tests/test_observability_pg.py::test_doctor_observability_gate`：评估前失败，评估后通过；CLI 退出码与输出；API 角色被拒。
